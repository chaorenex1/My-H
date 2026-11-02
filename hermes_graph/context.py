"""Runtime context shared by a client's services.

One ``GraphContext`` owns exactly one database file: the Ladybug store, the
schema registry, the process-wide write lock, and thread-local read
connections. ``GraphClient`` is the public facade over it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, TypeVar

from .config import GraphLimits
from .exceptions import GraphReadOnlyError, TransactionBusyError
from .schema.registry import SchemaRegistry

T = TypeVar("T")



class GraphContext:
    def __init__(self, home: Path, path: Path, *, read_only: bool, limits: GraphLimits):
        from .adapters.ladybug.store import LadybugStore

        self.home = home
        self.path = path
        self.read_only = read_only
        self.limits = limits
        self.store = LadybugStore(path, read_only=read_only)
        self._write_lock = threading.Lock()
        self._tls = threading.local()
        self.last_used = time.monotonic()
        self.closed = False
        try:
            self._bootstrap_schema()
        except BaseException:
            self.store.close()
            raise
        self.index = self._build_index()

    # ── schema ───────────────────────────────────────────────────────────
    def _bootstrap_schema(self) -> None:
        registry = SchemaRegistry(self.schema_execute)
        if self.read_only:
            registry.load_existing()
        else:
            registry.bootstrap()
        registry.verify(self.schema_execute)
        self.schema = registry

    def schema_execute(
        self, statement: str, params: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Run one DDL/registry statement on a short-lived connection.

        The registry keeps this bound method (not a connection), so it stays
        valid after ``admin.backup()`` swaps the underlying store.
        """
        connection = self.store.connect()
        try:
            return self.store.execute(connection, statement, params)
        finally:
            self.store.close_connection(connection)

    def _build_index(self) -> Any:
        from .services.index import IndexService

        return IndexService(self)

    # ── connections ──────────────────────────────────────────────────────
    def read_connection(self) -> Any:
        self.last_used = time.monotonic()
        connection = getattr(self._tls, "read_connection", None)
        if connection is None:
            connection = self.store.connect()
            self._tls.read_connection = connection
        return connection

    def run_read(self, fn: Callable[[Any], T]) -> T:
        self.last_used = time.monotonic()
        self.closed = False
        return fn(self.read_connection())

    def run_write(self, fn: Callable[[Any], T]) -> T:
        from .services.transaction import Transaction

        self.last_used = time.monotonic()
        result: T
        with Transaction(self) as transaction:
            result = fn(transaction)
        return result

    @contextmanager
    def exclusive(self) -> Generator[Any, None, None]:
        """Serialise a schema/DDL operation against writers and yield a connection."""
        if self.read_only:
            raise GraphReadOnlyError("this graph client is read-only")
        self.last_used = time.monotonic()
        if not self._write_lock.acquire(timeout=self.limits.write_lock_timeout_s):
            raise TransactionBusyError(
                "write_busy",
                details={"timeout_s": self.limits.write_lock_timeout_s},
            )
        connection: Any = None
        try:
            connection = self.store.connect()
            yield connection
        finally:
            if connection is not None:
                self.store.close_connection(connection)
            self._write_lock.release()

    def acquire_write_lock(self) -> None:
        if not self._write_lock.acquire(timeout=self.limits.write_lock_timeout_s):
            raise TransactionBusyError(
                "write_busy",
                details={"timeout_s": self.limits.write_lock_timeout_s},
            )

    def release_write_lock(self) -> None:
        self._write_lock.release()

    # ── scope versions ───────────────────────────────────────────────────
    def bump_scope_versions(self, connection: Any, scopes: set[str]) -> None:
        for scope in sorted(scopes):
            self.store.execute(
                connection,
                "MERGE (v:GraphScopeVersion {scope:$scope}) "
                "ON CREATE SET v.version = 1 "
                "ON MATCH SET v.version = v.version + 1",
                {"scope": scope},
            )

    def scope_version(self, scope: str, connection: Any | None = None) -> int:
        def _read(conn: Any) -> int:
            rows = self.store.execute(
                conn,
                "MATCH (v:GraphScopeVersion) WHERE v.scope=$scope "
                "RETURN v.version AS version",
                {"scope": scope},
            )
            return int(rows[0]["version"]) if rows else 0

        if connection is not None:
            return _read(connection)
        return self.run_read(_read)

    def scope_versions(self, connection: Any | None = None) -> dict[str, int]:
        def _read(conn: Any) -> dict[str, int]:
            rows = self.store.execute(
                conn,
                "MATCH (v:GraphScopeVersion) RETURN v.scope AS scope, "
                "v.version AS version ORDER BY v.scope",
                None,
            )
            return {str(row["scope"]): int(row["version"]) for row in rows}

        if connection is not None:
            return _read(connection)
        return self.run_read(_read)

    def bump_scope(self, scope: str) -> None:
        """Increment a scope version outside a user transaction (schema changes)."""
        with self.exclusive() as connection:
            self.bump_scope_versions(connection, {scope})
        self.index.prune_scope(scope)
