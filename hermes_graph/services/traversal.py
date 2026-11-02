"""Graph traversal over Ladybug adjacency (BFS, DFS, shortest path, subgraph)."""

from __future__ import annotations

import heapq
from collections import deque
from typing import TYPE_CHECKING, Any, Callable

from ..exceptions import NotFoundError, ValidationError
from ..models import Edge, GraphPath, GraphSnapshot, Node
from ..serialization import utc_now, validate_node_id
from ._rows import edge_select, node_select, row_to_edge, row_to_node

if TYPE_CHECKING:
    from ..context import GraphContext
    from .transaction import Transaction

_DIRECTIONS = frozenset({"out", "in", "both"})


class TraversalService:
    def __init__(self, ctx: GraphContext, tx: Transaction | None = None):
        self.ctx = ctx
        self.tx = tx

    def _read(self, fn: Callable[[Any], Any]) -> Any:
        if self.tx is not None:
            return fn(self.tx.connection)
        return self.ctx.run_read(fn)

    def bfs(
        self,
        start_id: str,
        max_depth: int = 1,
        relation_types: list[str] | None = None,
        *,
        direction: str = "out",
    ) -> list[Node]:
        nodes, _ = self._walk(
            start_id, max_depth, relation_types, direction, breadth_first=True
        )
        return nodes

    def dfs(
        self,
        start_id: str,
        max_depth: int = 1,
        relation_types: list[str] | None = None,
        *,
        direction: str = "out",
    ) -> list[Node]:
        nodes, _ = self._walk(
            start_id, max_depth, relation_types, direction, breadth_first=False
        )
        return nodes

    def shortest_path(
        self,
        source_id: str,
        target_id: str,
        weight: str | None = None,
        *,
        direction: str = "out",
    ) -> GraphPath | None:
        source_id = validate_node_id(source_id)
        target_id = validate_node_id(target_id)
        if weight is not None and not isinstance(weight, str):
            raise ValidationError("weight must be an edge property name")
        self._validate_direction(direction)

        def _do(connection: Any) -> GraphPath | None:
            source = self._get_node(connection, source_id)
            target = self._get_node(connection, target_id)
            if source is None:
                raise NotFoundError(f"node {source_id} not found")
            if target is None:
                raise NotFoundError(f"node {target_id} not found")
            if weight is None:
                return self._unweighted_path(connection, source, target, direction)
            return self._weighted_path(connection, source, target, direction, weight)

        return self._read(_do)

    def subgraph(
        self,
        node_ids: list[str],
        max_depth: int = 1,
        *,
        direction: str = "both",
    ) -> GraphSnapshot:
        if not isinstance(node_ids, (list, tuple)):
            raise ValidationError("node_ids must be a list")
        if not node_ids:
            return GraphSnapshot(
                nodes=[], edges=[], scope="subgraph", version=0, created_at=utc_now()
            )

        def _do(connection: Any) -> GraphSnapshot:
            collected: dict[str, Node] = {}
            edges: dict[str, Edge] = {}
            for node_id in node_ids:
                nodes, found = self._walk(
                    node_id,
                    max_depth,
                    None,
                    direction,
                    breadth_first=True,
                    connection=connection,
                )
                for node in nodes:
                    collected[node.id] = node
                for edge in found:
                    edges[edge.id] = edge
            return GraphSnapshot(
                nodes=list(collected.values()),
                edges=list(edges.values()),
                scope="subgraph",
                version=0,
                created_at=utc_now(),
            )

        return self._read(_do)

    # ── internals ────────────────────────────────────────────────────────
    def _walk(
        self,
        start_id: str,
        max_depth: int,
        relation_types: list[str] | None,
        direction: str,
        *,
        breadth_first: bool,
        connection: Any | None = None,
    ) -> tuple[list[Node], list[Edge]]:
        start_id = validate_node_id(start_id)
        self._validate_direction(direction)
        depth = self._depth(max_depth)
        relation_types = self._relation_types(relation_types)
        if connection is None:
            return self._read(
                lambda conn: self._walk(
                    start_id,
                    depth,
                    relation_types,
                    direction,
                    breadth_first=breadth_first,
                    connection=conn,
                )
            )
        start = self._get_node(connection, start_id)
        if start is None:
            raise NotFoundError(f"node {start_id} not found")
        if breadth_first:
            return self._walk_bfs(connection, start, depth, relation_types, direction)
        return self._walk_dfs(connection, start, depth, relation_types, direction)

    def _walk_bfs(
        self,
        connection: Any,
        start: Node,
        max_depth: int,
        relation_types: list[str] | None,
        direction: str,
    ) -> tuple[list[Node], list[Edge]]:
        order: list[Node] = [start]
        seen = {start.id}
        edges: dict[str, Edge] = {}
        queue: deque[tuple[Node, int]] = deque([(start, 0)])
        while queue:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for edge, neighbour in self._adjacent(
                connection, node, direction, relation_types
            ):
                edges[edge.id] = edge
                if neighbour.id in seen:
                    continue
                seen.add(neighbour.id)
                order.append(neighbour)
                queue.append((neighbour, depth + 1))
        return order, list(edges.values())

    def _walk_dfs(
        self,
        connection: Any,
        start: Node,
        max_depth: int,
        relation_types: list[str] | None,
        direction: str,
    ) -> tuple[list[Node], list[Edge]]:
        order: list[Node] = []
        seen: set[str] = set()
        edges: dict[str, Edge] = {}
        stack: list[tuple[Node, int]] = [(start, 0)]
        while stack:
            node, depth = stack.pop()
            if node.id in seen:
                continue
            seen.add(node.id)
            order.append(node)
            if depth >= max_depth:
                continue
            adjacent = sorted(
                self._adjacent(connection, node, direction, relation_types),
                key=lambda pair: (pair[0].id, pair[1].id),
                reverse=True,
            )
            for edge, neighbour in adjacent:
                edges[edge.id] = edge
                if neighbour.id not in seen:
                    stack.append((neighbour, depth + 1))
        return order, list(edges.values())

    def _unweighted_path(
        self, connection: Any, source: Node, target: Node, direction: str
    ) -> GraphPath | None:
        parent: dict[str, tuple[str, Edge] | None] = {source.id: None}
        queue: deque[Node] = deque([source])
        while queue:
            node = queue.popleft()
            if node.id == target.id:
                return self._reconstruct(source, target, parent)
            for edge, neighbour in self._adjacent(connection, node, direction, None):
                if neighbour.id in parent:
                    continue
                parent[neighbour.id] = (node.id, edge)
                queue.append(neighbour)
        return None

    def _weighted_path(
        self,
        connection: Any,
        source: Node,
        target: Node,
        direction: str,
        weight_name: str,
    ) -> GraphPath | None:
        distances: dict[str, float] = {source.id: 0.0}
        parent: dict[str, tuple[str, Edge] | None] = {source.id: None}
        heap: list[tuple[float, str]] = [(0.0, source.id)]
        nodes = {source.id: source}
        while heap:
            distance, node_id = heapq.heappop(heap)
            if distance > distances.get(node_id, float("inf")):
                continue
            if node_id == target.id:
                return self._reconstruct(source, target, parent, distances[target.id])
            node = nodes.get(node_id)
            if node is None:
                node = self._get_node(connection, node_id)
                if node is None:
                    continue
                nodes[node_id] = node
            for edge, neighbour in self._adjacent(connection, node, direction, None):
                step = self._edge_weight(edge, weight_name)
                candidate = distance + step
                if candidate < distances.get(neighbour.id, float("inf")):
                    distances[neighbour.id] = candidate
                    parent[neighbour.id] = (node.id, edge)
                    nodes[neighbour.id] = neighbour
                    heapq.heappush(heap, (candidate, neighbour.id))
        return None

    @staticmethod
    def _edge_weight(edge: Edge, name: str) -> float:
        value = edge.properties.get(name, 1.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(
                f"edge {edge.id} has non-numeric weight {name!r}"
            )
        if value < 0:
            raise ValidationError(f"edge {edge.id} has a negative weight")
        return float(value)

    def _reconstruct(
        self,
        source: Node,
        target: Node,
        parent: dict[str, tuple[str, Edge] | None],
        weight: float | None = None,
    ) -> GraphPath:
        connection = self.ctx.read_connection() if self.tx is None else self.tx.connection
        nodes: list[Node] = []
        edges: list[Edge] = []
        current: str | None = target.id
        by_id: dict[str, Node] = {source.id: source, target.id: target}
        while current is not None:
            entry = parent.get(current)
            node = by_id.get(current)
            if node is None:
                node = self._get_node(connection, current)
            if node is not None:
                nodes.append(node)
            if entry is None:
                break
            parent_id, edge = entry
            edges.append(edge)
            current = parent_id
        nodes.reverse()
        edges.reverse()
        return GraphPath(nodes=nodes, edges=edges, length=len(edges), weight=weight)

    def _get_node(self, connection: Any, node_id: str) -> Node | None:
        rows = self.ctx.store.execute(
            connection,
            "MATCH (r:GraphNodeRef {id:$id}) RETURN r.type AS type",
            {"id": node_id},
        )
        if not rows:
            return None
        definition = self.ctx.schema.maybe_node(str(rows[0]["type"]))
        if definition is None:
            return None
        rows = self.ctx.store.execute(
            connection,
            f"MATCH (n:{definition.name} {{id:$id}}) RETURN {node_select(definition)}",
            {"id": node_id},
        )
        if not rows or rows[0].get("deleted_at") is not None:
            return None
        return row_to_node(definition, rows[0])

    def _adjacent(
        self,
        connection: Any,
        node: Node,
        direction: str,
        relation_types: list[str] | None,
    ) -> list[tuple[Edge, Node]]:
        pairs: dict[str, tuple[Edge, Node]] = {}
        for definition in self.ctx.schema.edge_types():
            if relation_types is not None and definition.name not in relation_types:
                continue
            if direction in {"out", "both"} and definition.source == node.type:
                rows = self.ctx.store.execute(
                    connection,
                    f"MATCH (a:{definition.source} {{id:$id}})-"
                    f"[r:{definition.name}]->(b:{definition.target}) "
                    f"RETURN {edge_select(definition)} ORDER BY r.id",
                    {"id": node.id},
                )
                for row in rows:
                    edge = row_to_edge(definition, row)
                    neighbour = self._get_node(connection, edge.target_id)
                    if neighbour is not None:
                        pairs[edge.id] = (edge, neighbour)
            if direction in {"in", "both"} and definition.target == node.type:
                rows = self.ctx.store.execute(
                    connection,
                    f"MATCH (a:{definition.source})-[r:{definition.name}]->"
                    f"(b:{definition.target} {{id:$id}}) "
                    f"RETURN {edge_select(definition)} ORDER BY r.id",
                    {"id": node.id},
                )
                for row in rows:
                    edge = row_to_edge(definition, row)
                    neighbour = self._get_node(connection, edge.source_id)
                    if neighbour is not None:
                        pairs[edge.id] = (edge, neighbour)
        return list(pairs.values())

    def _relation_types(self, relation_types: list[str] | None) -> list[str] | None:
        if relation_types is None:
            return None
        if not isinstance(relation_types, list):
            raise ValidationError("relation_types must be a list of edge type names")
        for name in relation_types:
            self.ctx.schema.edge(name)
        return relation_types

    def _depth(self, max_depth: int) -> int:
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
            raise ValidationError("max_depth must be a non-negative integer")
        return min(max_depth, self.ctx.limits.max_traversal_depth)

    @staticmethod
    def _validate_direction(direction: str) -> None:
        if direction not in _DIRECTIONS:
            raise ValidationError(
                f"direction must be one of {sorted(_DIRECTIONS)}; got {direction!r}"
            )
