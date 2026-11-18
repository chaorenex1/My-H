"""Public schema registry API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..exceptions import ValidationError
from ..schema.definitions import EdgeType, NodeType, definition_from_dict

if TYPE_CHECKING:
    from ..context import GraphContext


class SchemaService:
    def __init__(self, ctx: GraphContext):
        self.ctx = ctx

    def register_node(self, definition: NodeType | dict[str, Any]) -> NodeType:
        node = self._coerce(definition, NodeType)
        with self.ctx.exclusive():
            changed = self.ctx.schema.maybe_node(node.name) != node
            registered = self.ctx.schema.register_node(node)
        if changed:
            self.ctx.bump_scope(registered.scope)
        return registered

    def register_edge(self, definition: EdgeType | dict[str, Any]) -> EdgeType:
        edge = self._coerce(definition, EdgeType)
        with self.ctx.exclusive():
            changed = self.ctx.schema.maybe_edge(edge.name) != edge
            registered = self.ctx.schema.register_edge(edge)
        if changed:
            self.ctx.bump_scope(registered.scope)
        return registered

    def register(self, payload: dict[str, Any]) -> list[NodeType | EdgeType]:
        if not isinstance(payload, dict):
            raise ValidationError("schema payload must be a JSON object")
        nodes = payload.get("nodes") or []
        edges = payload.get("edges") or []
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValidationError("schema nodes/edges must be lists")
        registered: list[NodeType | EdgeType] = []
        for item in nodes:
            registered.append(self.register_node(item))
        for item in edges:
            registered.append(self.register_edge(item))
        return registered

    def list_types(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for definition in self.ctx.schema.node_types():
            items.append(definition.to_dict())
        for definition in self.ctx.schema.edge_types():
            items.append(definition.to_dict())
        return items

    @staticmethod
    def _coerce(definition: Any, expected: type) -> Any:
        if isinstance(definition, dict):
            definition = definition_from_dict(definition)
        if not isinstance(definition, expected):
            raise ValidationError(f"expected a {expected.__name__} definition")
        return definition
