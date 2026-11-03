"""Scope snapshots and their NetworkX projection.

Ladybug is the source of truth. A snapshot is derived from a scope version;
the NetworkX graph exists only as a cache entry and is never written back.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from ..exceptions import QueryLimitError, SnapshotTooLargeError, ValidationError
from ..models import Edge, GraphSnapshot, Node
from ..schema.definitions import EdgeType, NodeType
from ..serialization import utc_now, validate_scope
from ._rows import edge_select, node_select, row_to_edge, row_to_node

if TYPE_CHECKING:
    from ..context import GraphContext

logger = logging.getLogger(__name__)


class _CacheEntry:
    __slots__ = ("graph", "snapshot")

    def __init__(self, snapshot: GraphSnapshot):
        self.snapshot = snapshot
        self.graph: Any = None


def _filter_key(
    node_defs: tuple[NodeType, ...], filters: dict[str, Any] | None
) -> tuple[tuple[str, Any], ...]:
    """Validate a snapshot filter and turn it into a stable cache-key component.

    Filters are equality matches on declared, typed fields only; a type without
    the field is excluded rather than matched unfiltered.
    """
    if filters is None:
        return ()
    if not isinstance(filters, dict):
        raise ValidationError("filters must be a dict")
    declared = {name for definition in node_defs for name in definition.fields}
    items: list[tuple[str, Any]] = []
    for name, value in filters.items():
        if not isinstance(name, str) or name not in declared:
            raise ValidationError(
                f"filter {name!r} is not a declared field of the selected node types"
            )
        if not isinstance(value, (str, int, float, bool)):
            raise ValidationError(f"filter {name!r} must be a scalar value")
        items.append((name, value))
    return tuple(sorted(items))


class IndexService:
    def __init__(self, ctx: GraphContext):
        self.ctx = ctx
        self._cache: OrderedDict[tuple[Any, ...], _CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    # ── versions ─────────────────────────────────────────────────────────
    def version(self, scope: str) -> int:
        return self.ctx.scope_version(validate_scope(scope))

    def invalidate(self, scope: str | None = None) -> None:
        with self._lock:
            if scope is None:
                self._cache.clear()
                return
            scope = validate_scope(scope)
            for key in [key for key in self._cache if key[0] == scope]:
                self._cache.pop(key, None)

    def prune_scope(self, scope: str) -> None:
        self.invalidate(scope)

    # ── snapshots ────────────────────────────────────────────────────────
    def snapshot(
        self,
        scope: str,
        node_types: list[str] | None = None,
        edge_types: list[str] | None = None,
        *,
        filters: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> GraphSnapshot:
        scope = validate_scope(scope)
        nodes = self._resolve_node_types(scope, node_types)
        edges = self._resolve_edge_types(scope, edge_types)
        filter_key = _filter_key(nodes, filters)
        names = (
            tuple(t.name for t in nodes),
            tuple(t.name for t in edges),
            filter_key,
        )
        if use_cache:
            cached = self._get((scope, self.ctx.scope_version(scope), *names))
            if cached is not None:
                return cached.snapshot
        snapshot = self._build(scope, nodes, edges, filters=dict(filters or {}))
        self._put((scope, snapshot.version, *names), snapshot)
        return snapshot

    def nx_graph(self, scope: str, *, filters: dict[str, Any] | None = None) -> Any:
        scope = validate_scope(scope)
        nodes = self._resolve_node_types(scope, None)
        edges = self._resolve_edge_types(scope, None)
        filter_key = _filter_key(nodes, filters)
        names = (
            tuple(t.name for t in nodes),
            tuple(t.name for t in edges),
            filter_key,
        )
        key = (scope, self.ctx.scope_version(scope), *names)
        entry = self._get(key)
        if entry is not None and entry.graph is not None:
            return entry.graph
        snapshot = (
            entry.snapshot
            if entry is not None
            else self.snapshot(scope, filters=filters)
        )
        graph = self._to_nx(snapshot)
        with self._lock:
            entry = self._cache.get((scope, snapshot.version, *names))
            if entry is not None:
                entry.graph = graph
            else:
                self._put_locked(
                    (scope, snapshot.version, *names), _CacheEntry(snapshot), graph
                )
        return graph

    # ── building ─────────────────────────────────────────────────────────
    def _build(
        self,
        scope: str,
        node_defs: tuple[NodeType, ...],
        edge_defs: tuple[EdgeType, ...],
        *,
        filters: dict[str, Any] | None = None,
    ) -> GraphSnapshot:
        """Read a consistent snapshot inside one read-only Ladybug transaction."""
        self.ctx.last_used = time.monotonic()
        connection = self.ctx.store.connect()
        store = self.ctx.store
        try:
            store.execute(connection, "BEGIN TRANSACTION READ ONLY")
            try:
                version = self._read_version(connection, scope)
                node_rows = self._read_nodes(connection, node_defs, filters or {})
                edges = self._read_edges(connection, edge_defs, node_rows)
                self._check_bytes(node_rows, edges)
                store.execute(connection, "COMMIT")
            except BaseException:
                try:
                    store.execute(connection, "ROLLBACK")
                except Exception:
                    logger.debug("snapshot rollback failed", exc_info=True)
                raise
            return GraphSnapshot(
                nodes=list(node_rows.values()),
                edges=edges,
                scope=scope,
                version=version,
                created_at=utc_now(),
            )
        finally:
            store.close_connection(connection)

    def _read_version(self, connection: Any, scope: str) -> int:
        rows = self.ctx.store.execute(
            connection,
            "MATCH (v:GraphScopeVersion) WHERE v.scope=$scope "
            "RETURN v.version AS version",
            {"scope": scope},
        )
        return int(rows[0]["version"]) if rows else 0

    def _read_nodes(
        self,
        connection: Any,
        node_defs: tuple[NodeType, ...],
        filters: dict[str, Any],
    ) -> dict[str, Node]:
        node_rows: dict[str, Node] = {}
        for definition in node_defs:
            if any(name not in definition.fields for name in filters):
                # A type without the filtered field cannot match; never widen
                # the filter to "all rows of this type".
                continue
            where = " AND ".join(
                f"n.{name}=${name}" for name in sorted(filters)
            )
            statement = (
                f"MATCH (n:{definition.name}) WHERE n.deleted_at IS NULL"
                + (f" AND {where}" if where else "")
                + f" RETURN {node_select(definition)} ORDER BY n.id"
            )
            remaining = self.ctx.limits.max_snapshot_nodes - len(node_rows)
            try:
                rows = self.ctx.store.execute(
                    connection,
                    statement,
                    dict(filters),
                    max_rows=max(remaining, 0),
                )
            except QueryLimitError as exc:
                raise SnapshotTooLargeError(
                    f"snapshot exceeds {self.ctx.limits.max_snapshot_nodes} nodes",
                    details={"max_nodes": self.ctx.limits.max_snapshot_nodes},
                ) from exc
            for row in rows:
                node = row_to_node(definition, row)
                node_rows[node.id] = node
                self._check_size(len(node_rows), None)
        return node_rows

    def _read_edges(
        self,
        connection: Any,
        edge_defs: tuple[EdgeType, ...],
        node_rows: dict[str, Node],
    ) -> list[Edge]:
        edges: list[Edge] = []
        for definition in edge_defs:
            remaining = self.ctx.limits.max_snapshot_edges - len(edges)
            try:
                rows = self.ctx.store.execute(
                    connection,
                    f"MATCH (a:{definition.source})-[r:{definition.name}]->"
                    f"(b:{definition.target}) RETURN {edge_select(definition)} "
                    "ORDER BY r.id",
                    {},
                    max_rows=max(remaining, 0),
                )
            except QueryLimitError as exc:
                raise SnapshotTooLargeError(
                    f"snapshot exceeds {self.ctx.limits.max_snapshot_edges} edges",
                    details={"max_edges": self.ctx.limits.max_snapshot_edges},
                ) from exc
            for row in rows:
                edge = row_to_edge(definition, row)
                if edge.source_id not in node_rows or edge.target_id not in node_rows:
                    continue
                edges.append(edge)
                self._check_size(None, len(edges))
        return edges

    def _check_size(self, node_count: int | None, edge_count: int | None) -> None:
        if node_count is not None and node_count > self.ctx.limits.max_snapshot_nodes:
            raise SnapshotTooLargeError(
                f"snapshot exceeds {self.ctx.limits.max_snapshot_nodes} nodes",
                details={"max_nodes": self.ctx.limits.max_snapshot_nodes},
            )
        if edge_count is not None and edge_count > self.ctx.limits.max_snapshot_edges:
            raise SnapshotTooLargeError(
                f"snapshot exceeds {self.ctx.limits.max_snapshot_edges} edges",
                details={"max_edges": self.ctx.limits.max_snapshot_edges},
            )

    def _check_bytes(self, nodes: dict[str, Node], edges: list[Edge]) -> None:
        total = 0
        for node in nodes.values():
            total += len(json.dumps(node.properties, default=str).encode("utf-8"))
        for edge in edges:
            total += len(json.dumps(edge.properties, default=str).encode("utf-8"))
        if total > self.ctx.limits.max_snapshot_bytes:
            raise SnapshotTooLargeError(
                "snapshot exceeds the estimated byte budget",
                details={"max_bytes": self.ctx.limits.max_snapshot_bytes},
            )

    @staticmethod
    def _to_nx(snapshot: GraphSnapshot) -> Any:
        try:
            import networkx as nx  # optional extra, imported lazily
        except ImportError as exc:
            from ..exceptions import AlgorithmUnavailableError

            raise AlgorithmUnavailableError(
                "graph algorithms need networkx; run "
                "`hermes pm install --extra graph`",
                details={"module": "networkx"},
            ) from exc
        graph = nx.MultiDiGraph()
        for node in snapshot.nodes:
            attributes = dict(node.properties)
            attributes["node_type"] = node.type
            graph.add_node(node.id, **attributes)
        for edge in snapshot.edges:
            attributes = dict(edge.properties)
            attributes["relation"] = edge.type
            attributes["edge_id"] = edge.id
            graph.add_edge(
                edge.source_id,
                edge.target_id,
                key=edge.id,
                **attributes,
            )
        return graph

    # ── cache plumbing ───────────────────────────────────────────────────
    def _get(self, key: tuple[Any, ...]) -> _CacheEntry | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                self._cache.move_to_end(key)
            return entry

    def _put(self, key: tuple[Any, ...], snapshot: GraphSnapshot) -> None:
        with self._lock:
            self._put_locked(key, _CacheEntry(snapshot), None)

    def _put_locked(self, key: tuple[Any, ...], entry: _CacheEntry, graph: Any) -> None:
        if graph is not None:
            entry.graph = graph
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > self.ctx.limits.snapshot_cache_size:
            self._cache.popitem(last=False)

    def _resolve_node_types(
        self, scope: str, names: list[str] | None
    ) -> tuple[NodeType, ...]:
        if names is not None and not isinstance(names, (list, tuple)):
            raise ValidationError("node_types must be a list")
        if names is None:
            return self.ctx.schema.node_types_for_scope(scope)
        resolved = []
        for name in names:
            if not isinstance(name, str):
                raise ValidationError("node_types must contain strings")
            definition = self.ctx.schema.node(name)
            if definition.scope != scope:
                raise ValidationError(
                    f"node type {name!r} belongs to scope {definition.scope!r}"
                )
            resolved.append(definition)
        return tuple(resolved)

    def _resolve_edge_types(
        self, scope: str, names: list[str] | None
    ) -> tuple[EdgeType, ...]:
        if names is not None and not isinstance(names, (list, tuple)):
            raise ValidationError("edge_types must be a list")
        if names is None:
            return self.ctx.schema.edge_types_for_scope(scope)
        resolved = []
        for name in names:
            if not isinstance(name, str):
                raise ValidationError("edge_types must contain strings")
            definition = self.ctx.schema.edge(name)
            if definition.scope != scope:
                raise ValidationError(
                    f"edge type {name!r} belongs to scope {definition.scope!r}"
                )
            resolved.append(definition)
        return tuple(resolved)

