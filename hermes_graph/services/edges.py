"""Relationship CRUD."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..exceptions import GraphReadOnlyError, NotFoundError, SchemaError, ValidationError
from ..models import Edge, QueryResult
from ..schema.definitions import EdgeType
from ..serialization import (
    EDGE_RESERVED_FIELDS,
    new_node_id,
    utc_now,
    validate_node_id,
    validate_properties,
)
from ._rows import edge_select, row_to_edge, split_properties, validate_property_names

_List = list

if TYPE_CHECKING:
    from ..context import GraphContext
    from .transaction import Transaction


class EdgeService:
    def __init__(self, ctx: GraphContext, tx: Transaction | None = None):
        self.ctx = ctx
        self.tx = tx

    def _read(self, fn: Callable[[Any], Any]) -> Any:
        if self.tx is not None:
            return fn(self.tx.connection)
        return self.ctx.run_read(fn)

    def _write(self, fn: Callable[[Any, Transaction], Any]) -> Any:
        if self.tx is not None:
            if self.tx.read_only:
                raise GraphReadOnlyError("transaction is read-only")
            return fn(self.tx.connection, self.tx)
        return self.ctx.run_write(lambda tx: fn(tx.connection, tx))

    def _definition(self, name: str) -> EdgeType:
        if not isinstance(name, str):
            raise ValidationError("edge type must be a string")
        return self.ctx.schema.edge(name)

    def _execute(self, connection: Any, statement: str, params: dict[str, Any]) -> _List[dict[str, Any]]:
        return self.ctx.store.execute(connection, statement, params)

    # ── CRUD ─────────────────────────────────────────────────────────────
    def create(
        self,
        source_id: str,
        target_id: str,
        type: str,
        properties: dict[str, Any] | None = None,
    ) -> Edge:
        definition = self._definition(type)
        source_id = validate_node_id(source_id)
        target_id = validate_node_id(target_id)
        props = validate_properties(properties)
        validate_property_names(props, set(EDGE_RESERVED_FIELDS))
        edge_id = new_node_id()

        def _do(connection: Any, tx: Transaction) -> Edge:
            self._require_endpoint(tx, source_id, definition.source)
            self._require_endpoint(tx, target_id, definition.target)
            declared, props_json = split_properties(
                definition.fields, props, max_bytes=self.ctx.limits.max_properties_bytes
            )
            stamp = utc_now()
            now = stamp.isoformat()
            params: dict[str, Any] = {
                "source_id": source_id,
                "target_id": target_id,
                "id": edge_id,
                "created_at": now,
                "props": props_json,
            }
            fields = ["id:$id", "created_at:$created_at", "props:$props"]
            for name in definition.fields:
                params[name] = declared.get(name)
                fields.append(f"{name}:${name}")
            self._execute(
                connection,
                f"MATCH (a:{definition.source} {{id:$source_id}}), "
                f"(b:{definition.target} {{id:$target_id}}) "
                f"CREATE (a)-[r:{definition.name} {{{', '.join(fields)}}}]->(b)",
                params,
            )
            self._execute(
                connection,
                "MERGE (r:GraphEdgeRef {id:$id}) SET r.type=$type",
                {"id": edge_id, "type": definition.name},
            )
            tx.mark(definition.scope)
            return Edge(
                id=edge_id,
                source_id=source_id,
                target_id=target_id,
                type=definition.name,
                properties=dict(props),
                created_at=stamp,
            )

        return self._write(_do)

    def get(self, edge_id: str) -> Edge | None:
        edge_id = validate_node_id(edge_id)

        def _do(connection: Any) -> Edge | None:
            ref = self._get_ref(connection, edge_id)
            if ref is None:
                return None
            definition = self._definition(ref)
            rows = self._execute(
                connection,
                f"MATCH (a:{definition.source})-[r:{definition.name}]->"
                f"(b:{definition.target}) WHERE r.id=$id "
                f"RETURN {edge_select(definition)}",
                {"id": edge_id},
            )
            return row_to_edge(definition, rows[0]) if rows else None

        return self._read(_do)

    def list(
        self,
        source_id: str | None = None,
        type: str | None = None,
        *,
        target_id: str | None = None,
        limit: int = 100,
    ) -> QueryResult:
        limit = self._normalise_limit(limit)
        if source_id is not None:
            source_id = validate_node_id(source_id)
        if target_id is not None:
            target_id = validate_node_id(target_id)
        if type is not None:
            definitions = (self._definition(type),)
        else:
            definitions = self.ctx.schema.edge_types()

        def _do(connection: Any) -> QueryResult:
            collected: _List[Edge] = []
            for definition in definitions:
                collected.extend(
                    self._list_one(
                        connection, definition, source_id, target_id, limit + 1
                    )
                )
            collected.sort(key=lambda edge: edge.id)
            has_more = len(collected) > limit
            return QueryResult(items=list(collected[:limit]), has_more=has_more)

        return self._read(_do)

    def delete(self, edge_id: str) -> None:
        edge_id = validate_node_id(edge_id)

        def _do(connection: Any, tx: Transaction) -> None:
            ref = self._get_ref(connection, edge_id)
            if ref is None:
                raise NotFoundError(f"edge {edge_id} not found")
            definition = self._definition(ref)
            rows = self._execute(
                connection,
                f"MATCH (a:{definition.source})-[r:{definition.name}]->"
                f"(b:{definition.target}) WHERE r.id=$id RETURN r.id AS id",
                {"id": edge_id},
            )
            if not rows:
                raise NotFoundError(f"edge {edge_id} not found")
            self._execute(
                connection,
                f"MATCH (a:{definition.source})-[r:{definition.name}]->"
                f"(b:{definition.target}) WHERE r.id=$id DELETE r",
                {"id": edge_id},
            )
            self._execute(
                connection,
                "MATCH (r:GraphEdgeRef {id:$id}) DELETE r",
                {"id": edge_id},
            )
            tx.mark(definition.scope)

        self._write(_do)

    # ── internals ────────────────────────────────────────────────────────
    def _require_endpoint(
        self, tx: Transaction, node_id: str, expected_type: str
    ) -> None:
        from .nodes import NodeService

        node = NodeService(self.ctx, tx=tx).get(node_id)
        if node is None:
            raise NotFoundError(f"node {node_id} is not an active {expected_type}")
        if node.type != expected_type:
            raise SchemaError(
                f"edge endpoint {node_id} is a {node.type}, expected {expected_type}"
            )

    def _list_one(
        self,
        connection: Any,
        definition: EdgeType,
        source_id: str | None,
        target_id: str | None,
        limit: int,
    ) -> _List[Edge]:
        where: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        if source_id is not None:
            where.append("a.id = $source_id")
            params["source_id"] = source_id
        if target_id is not None:
            where.append("b.id = $target_id")
            params["target_id"] = target_id
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = self._execute(
            connection,
            f"MATCH (a:{definition.source})-[r:{definition.name}]->"
            f"(b:{definition.target}){clause} "
            f"RETURN {edge_select(definition)} ORDER BY r.id LIMIT $limit",
            params,
        )
        return [row_to_edge(definition, row) for row in rows]

    def _get_ref(self, connection: Any, edge_id: str) -> str | None:
        rows = self._execute(
            connection,
            "MATCH (r:GraphEdgeRef {id:$id}) RETURN r.type AS type",
            {"id": edge_id},
        )
        return str(rows[0]["type"]) if rows else None

    def _normalise_limit(self, limit: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValidationError("limit must be a positive integer")
        return min(limit, self.ctx.limits.max_search_limit)
