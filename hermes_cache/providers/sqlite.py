"""SQLite-backed cache provider (v2 storage backend)."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from ..errors import ProviderError
from ..policy import EvictionPolicy
from .base import ProviderEntry, ProviderStats

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SCHEMA_VERSION = 2
_SCHEMA_SQL = """
PRAGMA user_version = 2;

CREATE TABLE IF NOT EXISTS cache_entries (
    namespace  TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    payload    BLOB    NOT NULL,
    codec      TEXT    NOT NULL,
    created_at REAL    NOT NULL,
    expires_at REAL,
    size_bytes INTEGER NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 0,
    accessed_at REAL   NOT NULL DEFAULT 0,
    PRIMARY KEY (namespace, key)
);
"""

_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_cache_entries_expires
    ON cache_entries(expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cache_entries_created
    ON cache_entries(created_at);

CREATE INDEX IF NOT EXISTS idx_cache_entries_accessed
    ON cache_entries(accessed_at, created_at);

CREATE INDEX IF NOT EXISTS idx_cache_entries_hits
    ON cache_entries(hits, created_at);
"""

_UPSERT_SQL = """
INSERT INTO cache_entries
    (namespace, key, payload, codec, created_at, expires_at, size_bytes,
     accessed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(namespace, key) DO UPDATE SET
    payload = excluded.payload,
    codec = excluded.codec,
    created_at = excluded.created_at,
    expires_at = excluded.expires_at,
    size_bytes = excluded.size_bytes,
    hits = 0,
    accessed_at = excluded.accessed_at
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

_TOUCH_SQL = """
UPDATE cache_entries SET accessed_at = ?
WHERE namespace = ? AND key = ?
"""

_TOUCH_HIT_SQL = """
UPDATE cache_entries SET accessed_at = ?, hits = hits + 1
WHERE namespace = ? AND key = ?
"""

_SELECT_VICTIMS_SQL = {
    EvictionPolicy.FIFO: """
        SELECT rowid, size_bytes FROM cache_entries
        ORDER BY created_at ASC
        LIMIT ?
    """,
    EvictionPolicy.LRU: """
        SELECT rowid, size_bytes FROM cache_entries
        ORDER BY accessed_at ASC, created_at ASC
        LIMIT ?
    """,
    EvictionPolicy.LFU: """
        SELECT rowid, size_bytes FROM cache_entries
        ORDER BY hits ASC, created_at ASC
        LIMIT ?
    """,
    EvictionPolicy.RR: """
        SELECT rowid, size_bytes FROM cache_entries
        ORDER BY random()
        LIMIT ?
    """,
}

_RETRY_MARKERS = ("locked", "busy", "interrupted", "disk i/o")
_CORRUPT_MARKERS = ("malformed", "not a database", "encrypted")


def _is_retryable(exc: sqlite3.Error) -> bool:
    if isinstance(exc, (sqlite3.ProgrammingError, sqlite3.InterfaceError)):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _RETRY_MARKERS)


def _is_corrupt(exc: sqlite3.Error) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _CORRUPT_MARKERS)


class SQLiteProvider:
    """Thread-safe, multi-process-safe SQLite storage for cache entries."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 5.0,
        busy_timeout_ms: int = 5000,
        retries: int = 2,
        retry_delay: float = 0.05,
        recover_corrupt: bool = True,
    ) -> None:
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ValueError("retries must be a non-negative integer")
        if retry_delay < 0:
            raise ValueError("retry_delay must be >= 0")
        self.path = Path(path)
        self.timeout = timeout
        self.busy_timeout_ms = busy_timeout_ms
        self.retries = retries
        self.retry_delay = retry_delay
        self.recover_corrupt = recover_corrupt
        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        self._lock = threading.Lock()
        self._closed = False
        self._pid = os.getpid()
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._after_fork)

    def _check_open(self) -> None:
        with self._lock:
            if self._closed:
                raise ProviderError("cache provider is closed")

    def _connect(self) -> sqlite3.Connection:
        self._check_open()
        if self._pid != os.getpid():
            self._after_fork()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if not self.path.parent.exists():
            raise ProviderError(
                f"cache directory does not exist: {self.path.parent}"
            )
        attempts = 2 if self.recover_corrupt else 1
        for attempt in range(attempts):
            try:
                conn = self._open_connection()
            except sqlite3.DatabaseError as exc:
                if (
                    self.recover_corrupt
                    and attempt + 1 < attempts
                    and _is_corrupt(exc)
                ):
                    self._quarantine_database()
                    continue
                raise ProviderError(
                    f"could not open cache database {self.path}: {exc}"
                ) from exc
            except OSError as exc:
                raise ProviderError(
                    f"could not open cache database {self.path}: {exc}"
                ) from exc
            with self._lock:
                if self._closed:
                    conn.close()
                    raise ProviderError("cache provider is closed")
                self._connections.add(conn)
            self._local.conn = conn
            return conn
        raise ProviderError(
            f"could not open cache database {self.path}"
        )  # pragma: no cover

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path),
            timeout=self.timeout,
            isolation_level=None,
        )
        try:
            conn.row_factory = sqlite3.Row
            self._apply_pragmas(conn)
            self._ensure_schema(conn)
            self._secure_file()
        except BaseException:
            conn.close()
            raise
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
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > _SCHEMA_VERSION:
            raise ProviderError(
                f"cache schema version {version} is newer than supported "
                f"{_SCHEMA_VERSION}; delete the cache database"
            )
        if version == 0:
            conn.executescript(_SCHEMA_SQL)
        row = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='cache_entries'"
        ).fetchone()
        if row is None:
            raise ProviderError("cache schema is missing cache_entries")
        columns = {
            str(info[1])
            for info in conn.execute("PRAGMA table_info(cache_entries)")
        }
        if "hits" not in columns:
            conn.execute(
                "ALTER TABLE cache_entries "
                "ADD COLUMN hits INTEGER NOT NULL DEFAULT 0"
            )
        if "accessed_at" not in columns:
            conn.execute(
                "ALTER TABLE cache_entries "
                "ADD COLUMN accessed_at REAL NOT NULL DEFAULT 0"
            )
            conn.execute(
                "UPDATE cache_entries SET accessed_at = created_at"
            )
        conn.executescript(_INDEXES_SQL)
        if version != _SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def _secure_file(self) -> None:
        if os.name == "nt" or not self.path.exists():
            return
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            logger.debug("could not chmod cache database: %s", self.path)

    def _attempt(
        self,
        operation: str,
        run: Callable[[sqlite3.Connection], T],
    ) -> T:
        attempts = self.retries + 1
        for attempt in range(attempts):
            conn = self._connect()
            try:
                return run(conn)
            except sqlite3.Error as exc:
                self._discard_connection(conn)
                if attempt + 1 >= attempts:
                    raise ProviderError(
                        f"cache {operation} failed: {exc}"
                    ) from exc
                if self.recover_corrupt and _is_corrupt(exc):
                    self._quarantine_database()
                    continue
                if not _is_retryable(exc):
                    raise ProviderError(
                        f"cache {operation} failed: {exc}"
                    ) from exc
                if self.retry_delay:
                    time.sleep(self.retry_delay)
        raise ProviderError(
            f"cache {operation} failed"
        )  # pragma: no cover

    def _transaction(
        self,
        operation: str,
        run: Callable[[sqlite3.Connection], T],
    ) -> T:
        def transaction(conn: sqlite3.Connection) -> T:
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = run(conn)
                conn.execute("COMMIT")
                return result
            except sqlite3.Error:
                self._rollback(conn)
                raise

        return self._attempt(operation, transaction)

    def _discard_connection(self, conn: sqlite3.Connection) -> None:
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None
        with self._lock:
            self._connections.discard(conn)
        try:
            conn.close()
        except sqlite3.Error:
            logger.debug("cache connection close failed", exc_info=True)

    def _quarantine_database(self) -> None:
        target = self.path.with_name(
            f"{self.path.name}.corrupt-{int(time.time() * 1000)}"
        )
        try:
            if self.path.exists():
                os.replace(self.path, target)
            for suffix in ("-wal", "-shm"):
                sidecar = self.path.with_name(self.path.name + suffix)
                try:
                    sidecar.unlink()
                except FileNotFoundError:
                    pass
        except OSError as exc:
            raise ProviderError(
                f"could not quarantine corrupt cache database "
                f"{self.path}: {exc}"
            ) from exc
        logger.warning("quarantined corrupt cache database at %s", target)

    def get(self, namespace: str, key: str) -> ProviderEntry | None:
        def run(conn: sqlite3.Connection) -> ProviderEntry | None:
            row = conn.execute(
                _SELECT_ONE_SQL, (namespace, key)
            ).fetchone()
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

        return self._attempt("get", run)

    def get_many(
        self, namespace: str, keys: Sequence[str]
    ) -> dict[str, ProviderEntry]:
        unique = list(dict.fromkeys(keys))
        if not unique:
            return {}

        def run(conn: sqlite3.Connection) -> dict[str, ProviderEntry]:
            result: dict[str, ProviderEntry] = {}
            for start in range(0, len(unique), 500):
                chunk = unique[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    "SELECT key, payload, codec, created_at, expires_at "
                    f"FROM cache_entries WHERE namespace=? AND key IN ({marks})",
                    (namespace, *chunk),
                ).fetchall()
                for row in rows:
                    key = str(row["key"])
                    result[key] = ProviderEntry(
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
            return result

        return self._attempt("get_many", run)

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
            entry.created_at,
        )

    def set(self, entry: ProviderEntry) -> None:
        self._attempt(
            "set", lambda conn: conn.execute(_UPSERT_SQL, self._entry_row(entry))
        )

    def set_many(self, entries: Sequence[ProviderEntry]) -> None:
        if not entries:
            return
        rows = [self._entry_row(entry) for entry in entries]
        self._transaction(
            "set_many", lambda conn: conn.executemany(_UPSERT_SQL, rows)
        )

    def apply_batch(
        self,
        puts: Sequence[ProviderEntry],
        deletes: Sequence[tuple[str, str]],
    ) -> None:
        if not puts and not deletes:
            return
        rows = [self._entry_row(entry) for entry in puts]

        def run(conn: sqlite3.Connection) -> None:
            if rows:
                conn.executemany(_UPSERT_SQL, rows)
            if deletes:
                conn.executemany(
                    "DELETE FROM cache_entries WHERE namespace=? AND key=?",
                    deletes,
                )

        self._transaction("apply_batch", run)

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            logger.debug("cache rollback failed", exc_info=True)

    def delete(self, namespace: str, key: str) -> bool:
        def run(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "DELETE FROM cache_entries WHERE namespace=? AND key=?",
                (namespace, key),
            )
            return cur.rowcount > 0

        return self._attempt("delete", run)

    def delete_many(self, namespace: str, keys: Sequence[str]) -> int:
        unique = list(dict.fromkeys(keys))
        if not unique:
            return 0

        def run(conn: sqlite3.Connection) -> int:
            total = 0
            for start in range(0, len(unique), 500):
                chunk = unique[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    "DELETE FROM cache_entries "
                    f"WHERE namespace=? AND key IN ({marks})",
                    (namespace, *chunk),
                )
                total += max(cur.rowcount, 0)
            return total

        return self._transaction("delete_many", run)

    def delete_namespace(self, namespace: str) -> int:
        return self._attempt(
            "delete_namespace",
            lambda conn: max(
                conn.execute(
                    "DELETE FROM cache_entries WHERE namespace=?",
                    (namespace,),
                ).rowcount,
                0,
            ),
        )

    def clear(self) -> int:
        return self._attempt(
            "clear",
            lambda conn: max(
                conn.execute("DELETE FROM cache_entries").rowcount, 0
            ),
        )

    def purge_expired(self, now: float, limit: int = 1000) -> int:
        return self._attempt(
            "purge_expired",
            lambda conn: self._purge_expired(conn, now, limit),
        )

    @staticmethod
    def _purge_expired(
        conn: sqlite3.Connection, now: float, limit: int
    ) -> int:
        cur = conn.execute(_DELETE_EXPIRED_SQL, (now, limit))
        return max(cur.rowcount, 0)

    def touch(
        self,
        namespace: str,
        key: str,
        now: float,
        *,
        count_hit: bool,
    ) -> None:
        sql = _TOUCH_HIT_SQL if count_hit else _TOUCH_SQL
        self._attempt(
            "touch",
            lambda conn: conn.execute(sql, (now, namespace, key)),
        )

    def touch_many(
        self,
        namespace: str,
        keys: Sequence[str],
        now: float,
        *,
        count_hit: bool,
    ) -> None:
        unique = list(dict.fromkeys(keys))
        if not unique:
            return
        sql = _TOUCH_HIT_SQL if count_hit else _TOUCH_SQL
        params = [(now, namespace, key) for key in unique]
        self._transaction(
            "touch_many", lambda conn: conn.executemany(sql, params)
        )

    def trim_to_bytes(
        self,
        max_bytes: int,
        now: float,
        policy: EvictionPolicy = EvictionPolicy.FIFO,
    ) -> int:
        select_sql = _SELECT_VICTIMS_SQL[EvictionPolicy.coerce(policy)]

        def run(conn: sqlite3.Connection) -> int:
            deleted = self._purge_expired(conn, now, 100000)
            total = self._total_bytes(conn)
            while total > max_bytes:
                rows = conn.execute(select_sql, (500,)).fetchall()
                if not rows:
                    break
                victims: list[int] = []
                for row in rows:
                    victims.append(int(row[0]))
                    total -= int(row[1])
                    if total <= max_bytes:
                        break
                marks = ",".join("?" for _ in victims)
                conn.execute(
                    f"DELETE FROM cache_entries WHERE rowid IN ({marks})",
                    victims,
                )
                deleted += len(victims)
            return deleted

        return self._transaction("trim_to_bytes", run)

    @staticmethod
    def _total_bytes(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM cache_entries"
        ).fetchone()
        return int(row[0]) if row else 0

    def stats(self) -> ProviderStats:
        def run(conn: sqlite3.Connection) -> ProviderStats:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) "
                "FROM cache_entries"
            ).fetchone()
            if row is None:
                return ProviderStats(entries=0, size_bytes=0)
            return ProviderStats(entries=int(row[0]), size_bytes=int(row[1]))

        return self._attempt("stats", run)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
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
        self._pid = os.getpid()
