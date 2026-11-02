"""Thin, private wrapper around a Ladybug ``Database``.

Nothing outside ``hermes_graph.adapters.ladybug`` may import this module's
Ladybug objects. The store owns database/connection lifetime; callers pass a
connection handle back for every query.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from ...exceptions import (
    GraphError,
    GraphLockedError,
    QueryError,
    QueryLimitError,
    QueryTimeoutError,
    GraphUnavailableError,
)

logger = logging.getLogger(__name__)


class LadybugStore:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._lock = threading.Lock()
        self._connections: set[Any] = set()
        self._closed = False
        self._ladybug = self._load_module()
        if not read_only:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError:
                pass
        self._database = self._open_database()

    @staticmethod
    def _load_module() -> Any:
        try:
            import ladybug  # optional extra, imported lazily
        except (ImportError, OSError) as exc:
            raise GraphUnavailableError(
                "the graph backend is not installed; run "
                "`hermes pm install --extra graph`",
                details={"extra": "graph", "module": "ladybug"},
            ) from exc
        return ladybug

    def _open_database(self) -> Any:
        try:
            return self._ladybug.Database(str(self.path), read_only=self.read_only)
        except RuntimeError as exc:
            message = str(exc)
            lowered = message.lower()
            if "read only mode" in lowered:
                raise GraphUnavailableError(
                    f"graph database {self.path} does not exist",
                    details={"path": str(self.path)},
                ) from exc
            if "could not set lock" in lowered or "lock on file" in lowered:
                raise GraphLockedError(
                    f"graph database {self.path} is locked by another process",
                    details={
                        "path": str(self.path),
                        "remedy": "stop the process holding it, or read through "
                        "that process; do not open a second writer",
                    },
                ) from exc
            raise GraphUnavailableError(
                f"could not open graph database {self.path}: {message}",
                details={"path": str(self.path)},
            ) from exc

    # ── connections ──────────────────────────────────────────────────────
    def connect(self) -> Any:
        if self._closed:
            raise GraphError("graph store is closed")
        connection = self._ladybug.Connection(self._database)
        with self._lock:
            if self._closed:
                connection.close()
                raise GraphError("graph store is closed")
            self._connections.add(connection)
        return connection

    def close_connection(self, connection: Any) -> None:
        with self._lock:
            self._connections.discard(connection)
        try:
            connection.close()
        except Exception:  # a connection already closed by a failed tx
            logger.debug("error closing graph connection", exc_info=True)

    # ── queries ──────────────────────────────────────────────────────────
    def execute(
        self,
        connection: Any,
        statement: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_ms: int | None = None,
        max_rows: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            connection.set_query_timeout(timeout_ms if timeout_ms is not None else 0)
        except Exception:
            logger.debug("failed to set graph query timeout", exc_info=True)
        try:
            result = connection.execute(statement, params or {})
        except (RuntimeError, ValueError, TypeError) as exc:
            raise self._map_query_error(exc) from exc
        if isinstance(result, list):
            for item in result:
                item.close()
            raise QueryError("multi-statement Cypher is not supported")
        try:
            result.rows_as_dict()
            if max_rows is not None:
                rows = result.get_n(max_rows + 1)
                if len(rows) > max_rows:
                    raise QueryLimitError(
                        f"query returned more than {max_rows} rows",
                        details={"max_rows": max_rows},
                    )
                return rows
            return result.get_all()
        except RuntimeError as exc:
            raise self._map_query_error(exc) from exc
        finally:
            result.close()

    @staticmethod
    def _map_query_error(exc: Exception) -> GraphError:
        message = str(exc)
        if "timeout" in message.lower() or "interrupt" in message.lower():
            return QueryTimeoutError(message)
        return QueryError(message)

    # ── maintenance ──────────────────────────────────────────────────────
    def checkpoint(self, connection: Any) -> None:
        try:
            result = connection.execute("CHECKPOINT", {})
        except RuntimeError:
            logger.debug("graph checkpoint failed", exc_info=True)
            return
        result.close()

    def storage_version(self) -> str:
        try:
            return str(self._database.get_storage_version())
        except Exception:
            logger.debug("failed to read graph storage version", exc_info=True)
            return ""

    def file_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            try:
                connection.close()
            except Exception:
                logger.debug("error closing graph connection", exc_info=True)
        try:
            self._database.close()
        except Exception:
            logger.debug("error closing graph database", exc_info=True)
