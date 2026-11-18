"""Native Ladybug transactions with process-wide writer serialisation."""

from __future__ import annotations

import logging
import threading
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any

from ..exceptions import GraphError, TransactionError
from .edges import EdgeService
from .nodes import NodeService
from .query import QueryService

if TYPE_CHECKING:
    from ..context import GraphContext

logger = logging.getLogger(__name__)


class Transaction:
    def __init__(self, ctx: GraphContext, *, read_only: bool = False):
        self.ctx = ctx
        self.read_only = read_only
        self.connection: Any = None
        self.dirty_scopes: set[str] = set()
        self._lock_held = False
        self._active = False
        self.nodes: NodeService
        self.edges: EdgeService
        self.query: QueryService

    def __enter__(self) -> Transaction:
        self.ctx.last_used = time.monotonic()
        if getattr(self.ctx._tls, "transaction_active", False):
            raise TransactionError("nested transactions are not supported")
        if self.ctx.read_only and not self.read_only:
            from ..exceptions import GraphReadOnlyError

            raise GraphReadOnlyError("this graph client is read-only")
        if not self.read_only:
            self.ctx.acquire_write_lock()
            self._lock_held = True
        self.ctx._tls.transaction_active = True
        self._active = True
        try:
            self.connection = self.ctx.store.connect()
            statement = "BEGIN TRANSACTION"
            if self.read_only:
                statement += " READ ONLY"
            self.ctx.store.execute(self.connection, statement)
        except BaseException:
            self._cleanup()
            raise
        self.nodes = NodeService(self.ctx, tx=self)
        self.edges = EdgeService(self.ctx, tx=self)
        self.query = QueryService(self.ctx, tx=self)
        return self

    def mark(self, scope: str) -> None:
        self.dirty_scopes.add(scope)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        try:
            if exc_type is None:
                self._commit()
                return False
            self._rollback()
            if isinstance(exc, GraphError):
                return False
            raise TransactionError(f"transaction aborted: {exc}") from exc
        finally:
            self._cleanup()

    def _commit(self) -> None:
        try:
            if self.dirty_scopes:
                self.ctx.bump_scope_versions(self.connection, self.dirty_scopes)
            self.ctx.store.execute(self.connection, "COMMIT")
        except Exception as exc:
            try:
                self.ctx.store.execute(self.connection, "ROLLBACK")
            except Exception:
                logger.debug("rollback after failed commit failed", exc_info=True)
            raise TransactionError(f"commit failed: {exc}") from exc
        for scope in sorted(self.dirty_scopes):
            self.ctx.index.prune_scope(scope)

    def _rollback(self) -> None:
        if self.connection is None:
            return
        try:
            self.ctx.store.execute(self.connection, "ROLLBACK")
        except Exception:
            logger.debug("transaction rollback failed", exc_info=True)

    def _cleanup(self) -> None:
        if self.connection is not None:
            self.ctx.store.close_connection(self.connection)
            self.connection = None
        if self._active:
            self.ctx._tls.transaction_active = False
            self._active = False
        if self._lock_held:
            self.ctx.release_write_lock()
            self._lock_held = False


class TransactionService:
    def __init__(self, ctx: GraphContext):
        self.ctx = ctx

    def begin(self, read_only: bool = False) -> Transaction:
        return Transaction(self.ctx, read_only=read_only)
