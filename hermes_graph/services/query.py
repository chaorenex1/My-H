"""Parameterised query facade: typed find, read-only Cypher, neighbours."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ..exceptions import QueryError, QueryLimitError
from ..models import Edge, Node, QueryResult
from .cypher_guard import validate_read_only
from .nodes import NodeService

if TYPE_CHECKING:
    from ..context import GraphContext
    from .transaction import Transaction


class QueryService:
    def __init__(self, ctx: GraphContext, tx: Transaction | None = None):
        self.ctx = ctx
        self.tx = tx

    def find(
        self,
        type: str,
        filters: dict[str, Any] | None = None,
        *,
        limit: int = 20,
    ) -> QueryResult:
        return NodeService(self.ctx, tx=self.tx).search(type, filters, limit=limit)

    def cypher(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
        *,
        read_only: bool = True,
        timeout_ms: int | None = None,
        max_rows: int | None = None,
    ) -> QueryResult:
        if not read_only:
            raise QueryError(
                "write Cypher is not supported; use nodes/edges/transactions"
            )
        validate_read_only(statement)
        if params is not None and not isinstance(params, dict):
            raise QueryError("Cypher parameters must be a dict")
        row_limit = self._row_limit(max_rows)
        timeout = self._timeout(timeout_ms)
        connection = self._connection()
        started = time.perf_counter()
        rows = self.ctx.store.execute(
            connection,
            statement,
            params or {},
            timeout_ms=timeout,
            max_rows=row_limit,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._check_bytes(rows)
        return QueryResult(
            items=list(rows), execution_time_ms=round(elapsed_ms, 3)
        )

    def neighbors(
        self,
        node_id: str,
        direction: str = "both",
        depth: int = 1,
        relation_types: list[str] | None = None,
    ) -> QueryResult:
        from .traversal import TraversalService

        nodes = TraversalService(self.ctx, tx=self.tx).bfs(
            node_id,
            max_depth=depth,
            relation_types=relation_types,
            direction=direction,
        )
        neighbours = [node for node in nodes if node.id != node_id]
        items: list[Node | Edge | dict[str, Any]] = list(neighbours)
        return QueryResult(items=items, total=len(neighbours))

    def _connection(self) -> Any:
        if self.tx is not None:
            return self.tx.connection
        return self.ctx.read_connection()

    def _timeout(self, timeout_ms: int | None) -> int:
        if timeout_ms is None:
            return self.ctx.limits.query_timeout_ms
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 1:
            raise QueryError("timeout_ms must be a positive integer")
        return timeout_ms

    def _row_limit(self, max_rows: int | None) -> int:
        if max_rows is None:
            return self.ctx.limits.max_query_rows
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
            raise QueryError("max_rows must be a positive integer")
        return min(max_rows, self.ctx.limits.max_query_rows)

    def _check_bytes(self, rows: list[dict[str, Any]]) -> None:
        total = 0
        for row in rows:
            try:
                total += len(
                    json.dumps(row, ensure_ascii=False, default=str).encode("utf-8")
                )
            except (TypeError, ValueError):
                continue
            if total > self.ctx.limits.max_query_bytes:
                raise QueryLimitError(
                    "query result exceeds the byte limit",
                    details={"max_bytes": self.ctx.limits.max_query_bytes},
                )

