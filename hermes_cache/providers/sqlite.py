"""SQLite-backed cache provider (v1 storage backend)."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path

from ..errors import ProviderError
from .base import ProviderEntry, ProviderStats

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_SCHEMA_SQL = """
PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS cache_entries (
    namespace  TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    payload    BLOB    NOT NULL,
    codec      TEXT    NOT NULL,
    created_at REAL    NOT NULL,
    expires_at REAL,
    size_bytes INTEGER NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE INDEX IF NOT EXISTS idx_cache_entries_expires
    ON cache_entries(expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cache_entries_created
    ON cache_entries(created_at);
"""

_UPSERT_SQL = """
INSERT INTO cache_entries
    (namespace, key, payload, codec, created_at, expires_at, size_bytes)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(namespace, key) DO UPDATE SET
    payload = excluded.payload,
    codec = excluded.codec,
    created_at = excluded.created_at,
    expires_at = excluded.expires_at,
    size_bytes = excluded.size_bytes
"""

_SELECT_ONE_SQL = """
SELECT payload, codec, created_at, expires_at
FROM cache_entries
WHERE namespace = ? AND key = ?
"""

_DELETE_EXPIRED_SQL = """
DELETE FROM cache_entries
WHERE rowid IN (
    SELECT rowid FROM cache_entries
    WHERE expires_at IS NOT NULL AND expires_at <= ?
    LIMIT ?
)
"""

_DELETE_OLDEST_SQL = """
DELETE FROM cache_entries
WHERE rowid IN (
    SELECT rowid FROM cache_entries
    ORDER BY created_at ASC
    LIMIT ?
)
"""


class SQLiteProvider:
    """Thread-safe, multi-process-safe SQLite storage for cache entries."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 5.0,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        self._lock = threading.Lock()
        self._closed = False
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._after_fork)

    def _check_open(self) -> None:
        if self._closed:
            raise ProviderError("cache provider is closed")

    def _connect(self) -> sqlite3.Connection:
        self._check_open()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if not self.path.parent.exists():
            raise ProviderError(
                f"cache directory does not exist: {self.path.parent}"
            )
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                str(self.path),
                timeout=self.timeout,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            self._apply_pragmas(conn)
            self._ensure_schema(conn)
            self._secure_file()
        except ProviderError:
            if conn is not None:
                conn.close()
            raise
        except (sqlite3.Error, OSError) as exc:
            if conn is not None:
                conn.close()
            raise ProviderError(
                f"could not open cache database {self.path}: {exc}"
            ) from exc
        self._local.conn = conn
        with self._lock:
            self._connections.add(conn)
        return conn

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError as exc:
            logger.debug("cache WAL mode unavailable: %s", exc)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA temp_store=MEMORY")

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        version = int(
            conn.execute("PRAGMA user_version").fetchone()[0]
        )
        if version > _SCHEMA_VERSION:
            raise ProviderError(
                f"cache schema version {version} is newer than supported "
                f"{_SCHEMA_VERSION}; delete the cache database"
            )
        if version == 0:
            conn.executescript(_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        row = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='cache_entries'"
        ).fetchone()
        if row is None:
            raise ProviderError("cache schema is missing cache_entries")

    def _secure_file(self) -> None:
        if os.name == "nt" or not self.path.exists():
            return
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            logger.debug("could not chmod cache database: %s", self.path)

    def get(self, namespace: str, key: str) -> ProviderEntry | None:
        conn = self._connect()
        try:
            row = conn.execute(
                _SELECT_ONE_SQL, (namespace, key)
            ).fetchone()
        except sqlite3.Error as exc:
            raise ProviderError(f"cache get failed: {exc}") from exc
        if row is None:
            return None
        return ProviderEntry(
            namespace=namespace,
            key=key,
            payload=bytes(row["payload"]),
            codec=str(row["codec"]),
            created_at=float(row["created_at"]),
            expires_at=(
                None
                if row["expires_at"] is None
                else float(row["expires_at"])
            ),
        )

    def get_many(
        self, namespace: str, keys: Sequence[str]
    ) -> dict[str, ProviderEntry]:
        unique = list(dict.fromkeys(keys))
        if not unique:
            return {}
        conn = self._connect()
        result: dict[str, ProviderEntry] = {}
        try:
            for start in range(0, len(unique), 500):
                chunk = unique[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    "SELECT key, payload, codec, created_at, expires_at "
                    f"FROM cache_entries WHERE namespace=? AND key IN ({marks})",
                    (namespace, *chunk),
                ).fetchall()
                for row in rows:
                    result[str(row["key"])] = ProviderEntry(
                        namespace=namespace,
                        key=str(row["key"]),
                        payload=bytes(row["payload"]),
                        codec=str(row["codec"]),
                        created_at=float(row["created_at"]),
                        expires_at=(
                            None
                            if row["expires_at"] is None
                            else float(row["expires_at"])
                        ),
                    )
        except sqlite3.Error as exc:
            raise ProviderError(f"cache get_many failed: {exc}") from exc
        return result

    @staticmethod
    def _entry_row(entry: ProviderEntry) -> tuple[object, ...]:
        return (
            entry.namespace,
            entry.key,
            entry.payload,
            entry.codec,
            entry.created_at,
            entry.expires_at,
            len(entry.payload),
        )

    def set(self, entry: ProviderEntry) -> None:
        conn = self._connect()
        try:
            conn.execute(_UPSERT_SQL, self._entry_row(entry))
        except sqlite3.Error as exc:
            raise ProviderError(f"cache set failed: {exc}") from exc

    def set_many(self, entries: Sequence[ProviderEntry]) -> None:
        if not entries:
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                _UPSERT_SQL, [self._entry_row(entry) for entry in entries]
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback(conn)
            raise ProviderError(f"cache set_many failed: {exc}") from exc

    def apply_batch(
        self,
        puts: Sequence[ProviderEntry],
        deletes: Sequence[tuple[str, str]],
    ) -> None:
        if not puts and not deletes:
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if puts:
                conn.executemany(
                    _UPSERT_SQL, [self._entry_row(entry) for entry in puts]
                )
            if deletes:
                conn.executemany(
                    "DELETE FROM cache_entries WHERE namespace=? AND key=?",
                    deletes,
                )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback(conn)
            raise ProviderError(f"cache apply_batch failed: {exc}") from exc

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            logger.debug("cache rollback failed", exc_info=True)

    def delete(self, namespace: str, key: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM cache_entries WHERE namespace=? AND key=?",
                (namespace, key),
            )
        except sqlite3.Error as exc:
            raise ProviderError(f"cache delete failed: {exc}") from exc
        return cur.rowcount > 0

    def delete_many(self, namespace: str, keys: Sequence[str]) -> int:
        unique = list(dict.fromkeys(keys))
        if not unique:
            return 0
        conn = self._connect()
        total = 0
        try:
            for start in range(0, len(unique), 500):
                chunk = unique[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    "DELETE FROM cache_entries "
                    f"WHERE namespace=? AND key IN ({marks})",
                    (namespace, *chunk),
                )
                total += max(cur.rowcount, 0)
        except sqlite3.Error as exc:
            raise ProviderError(f"cache delete_many failed: {exc}") from exc
        return total

    def delete_namespace(self, namespace: str) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM cache_entries WHERE namespace=?", (namespace,)
            )
        except sqlite3.Error as exc:
            raise ProviderError(
                f"cache delete_namespace failed: {exc}"
            ) from exc
        return max(cur.rowcount, 0)

    def clear(self) -> int:
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM cache_entries")
        except sqlite3.Error as exc:
            raise ProviderError(f"cache clear failed: {exc}") from exc
        return max(cur.rowcount, 0)

    def purge_expired(self, now: float, limit: int = 1000) -> int:
        conn = self._connect()
        return self._purge_expired(conn, now, limit)

    @staticmethod
    def _purge_expired(
        conn: sqlite3.Connection, now: float, limit: int
    ) -> int:
        try:
            cur = conn.execute(_DELETE_EXPIRED_SQL, (now, limit))
        except sqlite3.Error as exc:
            raise ProviderError(
                f"cache purge_expired failed: {exc}"
            ) from exc
        return max(cur.rowcount, 0)

    def trim_to_bytes(self, max_bytes: int, now: float) -> int:
        conn = self._connect()
        deleted = self._purge_expired(conn, now, 100000)
        try:
            total = self._total_bytes(conn)
            while total > max_bytes:
                cur = conn.execute(_DELETE_OLDEST_SQL, (500,))
                removed = max(cur.rowcount, 0)
                if removed == 0:
                    break
                deleted += removed
                total = self._total_bytes(conn)
        except sqlite3.Error as exc:
            raise ProviderError(f"cache trim_to_bytes failed: {exc}") from exc
        return deleted

    @staticmethod
    def _total_bytes(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM cache_entries"
        ).fetchone()
        return int(row[0]) if row else 0

    def stats(self) -> ProviderStats:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) "
                "FROM cache_entries"
            ).fetchone()
        except sqlite3.Error as exc:
            raise ProviderError(f"cache stats failed: {exc}") from exc
        if row is None:
            return ProviderStats(entries=0, size_bytes=0)
        return ProviderStats(entries=int(row[0]), size_bytes=int(row[1]))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                logger.debug("cache connection close failed", exc_info=True)
        self._local = threading.local()

    def _after_fork(self) -> None:
        # The child inherits a possibly-held lock and parent thread-local state.
        # It is single-threaded after fork: drop both without taking the lock.
        self._local = threading.local()
        self._connections = set()
