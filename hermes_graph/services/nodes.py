"""Node CRUD, upsert, and search."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..exceptions import GraphReadOnlyError, NotFoundError, SchemaError, ValidationError
from ..models import Node, QueryResult
from ..schema.definitions import NodeType
from ..serialization import (
    NODE_RESERVED_FIELDS,
    coerce_field_value,
    derived_id,
    new_node_id,
    utc_now,
    validate_key,
    validate_node_id,
    validate_properties,
)
from ._rows import node_select, row_to_node, split_properties, validate_property_names

if TYPE_CHECKING:
    from ..context import GraphContext
    from .transaction import Transaction


class NodeService:
    def __init__(self, ctx: GraphContext, tx: Transaction | None = None):
        self.ctx = ctx
        self.tx = tx

    # ── execution plumbing ───────────────────────────────────────────────
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

    def _definition(self, name: str) -> NodeType:
        if not isinstance(name, str):
            raise ValidationError("node type must be a string")
        return self.ctx.schema.node(name)

    def _execute(self, connection: Any, statement: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return self.ctx.store.execute(connection, statement, params)

    # ── CRUD ─────────────────────────────────────────────────────────────
    def create(
        self,
        type: str,
        properties: dict[str, Any] | None = None,
        *,
        id: str | None = None,
        key: str | None = None,
    ) -> Node:
        if key is not None:
            raise ValidationError("create() does not accept key; use upsert(type, key)")
        definition = self._definition(type)
        props = validate_properties(properties)
        validate_property_names(props, set(NODE_RESERVED_FIELDS))
        node_id = validate_node_id(id) if id is not None else new_node_id()

        def _do(connection: Any, tx: Transaction) -> Node:
            if self._node_row(connection, definition, node_id) is not None:
                raise ValidationError(f"node {node_id} already exists")
            stamp = utc_now()
            now = stamp.isoformat()
            declared, props_json = split_properties(
                definition.fields, props, max_bytes=self.ctx.limits.max_properties_bytes
            )
            self._insert_node(connection, definition, node_id, None, declared, props_json, now)
            self._put_ref(connection, "GraphNodeRef", node_id, definition.name)
            tx.mark(definition.scope)
            return Node(
                id=node_id,
                type=definition.name,
                properties=dict(props),
                key=None,
                created_at=stamp,
                updated_at=stamp,
            )

        return self._write(_do)

    def upsert(
        self, type: str, key: str, properties: dict[str, Any] | None = None
    ) -> Node:
        definition = self._definition(type)
        key = validate_key(key)
        props = validate_properties(properties)
        validate_property_names(props, set(NODE_RESERVED_FIELDS))
        node_id = derived_id(definition.name, key)

        def _do(connection: Any, tx: Transaction) -> Node:
            declared, props_json = split_properties(
                definition.fields, props, max_bytes=self.ctx.limits.max_properties_bytes
            )
            stamp = utc_now()
            now = stamp.isoformat()
            existing = self._node_row(connection, definition, node_id)
            created_at = stamp
            if existing is None:
                self._insert_node(connection, definition, node_id, key, declared, props_json, now)
            else:
                if existing.get("key") not in (None, key):
                    raise SchemaError(
                        f"id {node_id} already belongs to key {existing.get('key')!r}"
                    )
                self._update_node(
                    connection, definition, node_id, key, declared, props_json, now, None
                )
                created_at = row_to_node(definition, existing).created_at or stamp
            self._put_ref(connection, "GraphNodeRef", node_id, definition.name)
            tx.mark(definition.scope)
            return Node(
                id=node_id,
                type=definition.name,
                properties=dict(props),
                key=key,
                created_at=created_at,
                updated_at=stamp,
            )

        return self._write(_do)

    def get(self, node_id: str) -> Node | None:
        node_id = validate_node_id(node_id)

        def _do(connection: Any) -> Node | None:
            ref = self._get_ref(connection, "GraphNodeRef", node_id)
            if ref is None:
                return None
            definition = self._definition(ref)
            row = self._node_row(connection, definition, node_id)
            if row is None or row.get("deleted_at") is not None:
                return None
            return row_to_node(definition, row)

        return self._read(_do)

    def update(self, node_id: str, properties: dict[str, Any] | None = None) -> Node:
        node_id = validate_node_id(node_id)
        props = validate_properties(properties)
        validate_property_names(props, set(NODE_RESERVED_FIELDS))

        def _do(connection: Any, tx: Transaction) -> Node:
            ref = self._get_ref(connection, "GraphNodeRef", node_id)
            if ref is None:
                raise NotFoundError(f"node {node_id} not found")
            definition = self._definition(ref)
            row = self._node_row(connection, definition, node_id)
            if row is None or row.get("deleted_at") is not None:
                raise NotFoundError(f"node {node_id} not found")
            current = row_to_node(definition, row).properties
            merged = {**current, **props}
            for name, value in props.items():
                if value is None:
                    merged.pop(name, None)
            declared, props_json = split_properties(
                definition.fields, merged, max_bytes=self.ctx.limits.max_properties_bytes
            )
            stamp = utc_now()
            self._update_node(
                connection, definition, node_id, row.get("key"), declared, props_json,
                stamp.isoformat(), None,
            )
            tx.mark(definition.scope)
            return Node(
                id=node_id,
                type=definition.name,
                properties=merged,
                key=row.get("key"),
                created_at=row_to_node(definition, row).created_at,
                updated_at=stamp,
            )

        return self._write(_do)

    def delete(self, node_id: str, hard: bool = False) -> None:
        node_id = validate_node_id(node_id)

        def _do(connection: Any, tx: Transaction) -> None:
            ref = self._get_ref(connection, "GraphNodeRef", node_id)
            if ref is None:
                raise NotFoundError(f"node {node_id} not found")
            definition = self._definition(ref)
            row = self._node_row(connection, definition, node_id)
            if row is None or row.get("deleted_at") is not None:
                raise NotFoundError(f"node {node_id} not found")
            edge_scopes = self._delete_node_edges(connection, definition.name, node_id)
            if hard:
                self._execute(
                    connection,
                    f"MATCH (n:{definition.name} {{id:$id}}) DETACH DELETE n",
                    {"id": node_id},
                )
                self._delete_ref(connection, "GraphNodeRef", node_id)
            else:
                now = utc_now().isoformat()
                self._execute(
                    connection,
                    f"MATCH (n:{definition.name} {{id:$id}}) "
                    "SET n.deleted_at=$now, n.updated_at=$now",
                    {"id": node_id, "now": now},
                )
            tx.mark(definition.scope)
            for scope in edge_scopes:
                tx.mark(scope)

        self._write(_do)

    def search(
        self,
        type: str,
        filters: dict[str, Any] | None = None,
        *,
        limit: int = 20,
    ) -> QueryResult:
        definition = self._definition(type)
        if filters is not None and not isinstance(filters, dict):
            raise ValidationError("filters must be a dict")
        filters = dict(filters or {})
        limit = self._normalise_limit(limit)
        where, params = self._build_filters(definition, filters)

        def _do(connection: Any) -> QueryResult:
            params["limit"] = limit + 1
            query = (
                f"MATCH (n:{definition.name}) WHERE n.deleted_at IS NULL"
                + (f" AND {' AND '.join(where)}" if where else "")
                + f" RETURN {node_select(definition)} ORDER BY n.id LIMIT $limit"
            )
            rows = self._execute(connection, query, params)
            has_more = len(rows) > limit
            nodes = [row_to_node(definition, row) for row in rows[:limit]]
            return QueryResult(items=list(nodes), total=None, has_more=has_more)

        return self._read(_do)

    # ── internals ────────────────────────────────────────────────────────
    def _normalise_limit(self, limit: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValidationError("limit must be a positive integer")
        return min(limit, self.ctx.limits.max_search_limit)

    def _build_filters(
        self, definition: NodeType, filters: dict[str, Any]
    ) -> tuple[list[str], dict[str, Any]]:
        where: list[str] = []
        params: dict[str, Any] = {}
        allowed = {"id", "key", *definition.fields}
        for name, value in filters.items():
            if name not in allowed:
                raise ValidationError(
                    f"filter {name!r} is not id/key or a declared field of "
                    f"{definition.name}"
                )
            if name == "key":
                params["filter_key_id"] = derived_id(definition.name, validate_key(value))
                where.append("n.id = $filter_key_id")
            elif name == "id":
                where.append("n.id = $filter_id")
                params["filter_id"] = validate_node_id(value)
            else:
                where.append(f"n.{name} = $filter_{name}")
                params[f"filter_{name}"] = coerce_field_value(
                    name, definition.fields[name], value
                )
        return where, params

    def _node_row(
        self, connection: Any, definition: NodeType, node_id: str
    ) -> dict[str, Any] | None:
        rows = self._execute(
            connection,
            f"MATCH (n:{definition.name} {{id:$id}}) RETURN {node_select(definition)}",
            {"id": node_id},
        )
        return rows[0] if rows else None

    def _insert_node(
        self,
        connection: Any,
        definition: NodeType,
        node_id: str,
        key: str | None,
        declared: dict[str, Any],
        props_json: str,
        now: str,
    ) -> None:
        params: dict[str, Any] = {
            "id": node_id,
            "key": key,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
            "props": props_json,
        }
        fields = ["id:$id", "key:$key", "created_at:$created_at",
                  "updated_at:$updated_at", "deleted_at:$deleted_at", "props:$props"]
        for name in definition.fields:
            params[name] = declared.get(name)
            fields.append(f"{name}:${name}")
        self._execute(
            connection,
            f"CREATE (n:{definition.name} {{{', '.join(fields)}}})",
            params,
        )

    def _update_node(
        self,
        connection: Any,
        definition: NodeType,
        node_id: str,
        key: str | None,
        declared: dict[str, Any],
        props_json: str,
        now: str,
        deleted_at: str | None,
    ) -> None:
        params: dict[str, Any] = {
            "id": node_id,
            "key": key,
            "updated_at": now,
            "deleted_at": deleted_at,
            "props": props_json,
        }
        assignments = ["n.key=$key", "n.updated_at=$updated_at",
                       "n.deleted_at=$deleted_at", "n.props=$props"]
        for name in definition.fields:
            params[name] = declared.get(name)
            assignments.append(f"n.{name}=${name}")
        self._execute(
            connection,
            f"MATCH (n:{definition.name} {{id:$id}}) SET {', '.join(assignments)}",
            params,
        )

    def _get_ref(self, connection: Any, table: str, node_id: str) -> str | None:
        rows = self._execute(
            connection,
            f"MATCH (r:{table} {{id:$id}}) RETURN r.type AS type",
            {"id": node_id},
        )
        return str(rows[0]["type"]) if rows else None

    def _put_ref(self, connection: Any, table: str, node_id: str, type_name: str) -> None:
        self._execute(
            connection,
            f"MERGE (r:{table} {{id:$id}}) SET r.type=$type",
            {"id": node_id, "type": type_name},
        )

    def _delete_ref(self, connection: Any, table: str, node_id: str) -> None:
        self._execute(
            connection,
            f"MATCH (r:{table} {{id:$id}}) DELETE r",
            {"id": node_id},
        )

    def _delete_node_edges(
        self, connection: Any, node_type: str, node_id: str
    ) -> set[str]:
        edge_ids: dict[str, str] = {}
        scopes: set[str] = set()
        for definition in self.ctx.schema.edge_types():
            found = False
            if definition.source == node_type:
                found |= self._collect_edges(
                    connection,
                    f"MATCH (a:{node_type} {{id:$id}})-[r:{definition.name}]->() "
                    "RETURN r.id AS id",
                    node_id,
                    definition.name,
                    edge_ids,
                )
                self._execute(
                    connection,
                    f"MATCH (a:{node_type} {{id:$id}})-[r:{definition.name}]->() "
                    "DELETE r",
                    {"id": node_id},
                )
            if definition.target == node_type:
                found |= self._collect_edges(
                    connection,
                    f"MATCH ()-[r:{definition.name}]->(b:{node_type} {{id:$id}}) "
                    "RETURN r.id AS id",
                    node_id,
                    definition.name,
                    edge_ids,
                )
                self._execute(
                    connection,
                    f"MATCH ()-[r:{definition.name}]->(b:{node_type} {{id:$id}}) "
                    "DELETE r",
                    {"id": node_id},
                )
            if found:
                scopes.add(definition.scope)
        for edge_id in edge_ids:
            self._delete_ref(connection, "GraphEdgeRef", edge_id)
        return scopes

    def _collect_edges(
        self,
        connection: Any,
        query: str,
        node_id: str,
        edge_type: str,
        edge_ids: dict[str, str],
    ) -> bool:
        rows = self._execute(connection, query, {"id": node_id})
        for row in rows:
            edge_ids[str(row["id"])] = edge_type
        return bool(rows)
