"""Persistent schema registry and Ladybug DDL compilation."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from ..exceptions import SchemaError
from ..serialization import INTERNAL_TABLES
from .definitions import EdgeType, NodeType, definition_from_dict

logger = logging.getLogger(__name__)

_EXECUTE = Callable[[str, dict[str, Any] | None], list[dict[str, Any]]]

_INTERNAL_DDL = (
    "CREATE NODE TABLE IF NOT EXISTS GraphNodeRef("
    "id STRING PRIMARY KEY, type STRING)",
    "CREATE NODE TABLE IF NOT EXISTS GraphEdgeRef("
    "id STRING PRIMARY KEY, type STRING)",
    "CREATE NODE TABLE IF NOT EXISTS GraphScopeVersion("
    "scope STRING PRIMARY KEY, version INT64)",
    "CREATE NODE TABLE IF NOT EXISTS GraphSchema("
    "name STRING PRIMARY KEY, kind STRING, definition STRING, version INT64)",
)

_SCHEMA_VERSION = 1


class SchemaRegistry:
    """In-memory view of ``GraphSchema`` plus DDL compilation."""

    def __init__(self, execute: _EXECUTE):
        self._execute = execute
        self._nodes: dict[str, NodeType] = {}
        self._edges: dict[str, EdgeType] = {}

    # ── loading ──────────────────────────────────────────────────────────
    def bootstrap(self) -> None:
        for statement in _INTERNAL_DDL:
            self._execute(statement, None)
        self._load()

    def load_existing(self) -> None:
        """Load an existing database without attempting DDL (read-only clients)."""
        self._load()

    def _load(self) -> None:
        rows = self._execute(
            "MATCH (s:GraphSchema) RETURN s.name AS name, s.kind AS kind, "
            "s.definition AS definition, s.version AS version",
            None,
        )
        for row in rows:
            if int(row.get("version") or 0) > _SCHEMA_VERSION:
                raise SchemaError(
                    f"schema entry {row.get('name')!r} uses version "
                    f"{row.get('version')}, newer than this Hermes ({_SCHEMA_VERSION})"
                )
            try:
                payload = json.loads(row["definition"])
                definition = definition_from_dict(payload)
            except Exception as exc:  # surfaced as SchemaError below
                raise SchemaError(
                    f"corrupt schema entry {row.get('name')!r}: {exc}"
                ) from exc
            if isinstance(definition, NodeType):
                self._nodes[definition.name] = definition
            else:
                self._edges[definition.name] = definition

    # ── lookups ──────────────────────────────────────────────────────────
    def node(self, name: str) -> NodeType:
        try:
            return self._nodes[name]
        except KeyError as exc:
            raise SchemaError(f"node type {name!r} is not registered") from exc

    def edge(self, name: str) -> EdgeType:
        try:
            return self._edges[name]
        except KeyError as exc:
            raise SchemaError(f"edge type {name!r} is not registered") from exc

    def maybe_node(self, name: str) -> NodeType | None:
        return self._nodes.get(name)

    def maybe_edge(self, name: str) -> EdgeType | None:
        return self._edges.get(name)

    def node_types(self) -> tuple[NodeType, ...]:
        return tuple(self._nodes[name] for name in sorted(self._nodes))

    def edge_types(self) -> tuple[EdgeType, ...]:
        return tuple(self._edges[name] for name in sorted(self._edges))

    def node_types_for_scope(self, scope: str) -> tuple[NodeType, ...]:
        return tuple(t for t in self.node_types() if t.scope == scope)

    def edge_types_for_scope(self, scope: str) -> tuple[EdgeType, ...]:
        return tuple(t for t in self.edge_types() if t.scope == scope)

    # ── registration ─────────────────────────────────────────────────────
    def register_node(self, definition: NodeType) -> NodeType:
        if definition.name in self._edges:
            raise SchemaError(f"{definition.name!r} is already registered as an edge")
        current = self._nodes.get(definition.name)
        if current is None:
            self._create_node_table(definition)
        else:
            definition = self._additive_node_migration(current, definition)
        self._persist(definition)
        self._nodes[definition.name] = definition
        return definition

    def register_edge(self, definition: EdgeType) -> EdgeType:
        if definition.name in self._nodes:
            raise SchemaError(f"{definition.name!r} is already registered as a node")
        self.node(definition.source)
        self.node(definition.target)
        current = self._edges.get(definition.name)
        if current is None:
            self._create_edge_table(definition)
        else:
            definition = self._additive_edge_migration(current, definition)
        self._persist(definition)
        self._edges[definition.name] = definition
        return definition

    def _create_node_table(self, definition: NodeType) -> None:
        columns = [
            "id STRING PRIMARY KEY",
            "key STRING",
            "created_at STRING",
            "updated_at STRING",
            "deleted_at STRING",
            "props STRING",
        ]
        columns.extend(f"{name} {field_type}" for name, field_type in definition.fields.items())
        self._execute(
            f"CREATE NODE TABLE {definition.name}({', '.join(columns)})", None
        )

    def _create_edge_table(self, definition: EdgeType) -> None:
        columns = ["id STRING", "created_at STRING", "props STRING"]
        columns.extend(f"{name} {field_type}" for name, field_type in definition.fields.items())
        self._execute(
            f"CREATE REL TABLE {definition.name}(FROM {definition.source} "
            f"TO {definition.target}, {', '.join(columns)})",
            None,
        )

    def _additive_node_migration(self, current: NodeType, new: NodeType) -> NodeType:
        if current.scope != new.scope:
            raise SchemaError(
                f"scope for {current.name!r} cannot change "
                f"({current.scope!r} -> {new.scope!r})"
            )
        self._require_superset(current.name, current.fields, new.fields)
        for name in sorted(set(new.fields) - set(current.fields)):
            self._execute(
                f"ALTER TABLE {current.name} ADD {name} {new.fields[name]}", None
            )
        return new

    def _additive_edge_migration(self, current: EdgeType, new: EdgeType) -> EdgeType:
        if (current.source, current.target) != (new.source, new.target):
            raise SchemaError(
                f"endpoints for {current.name!r} cannot change "
                f"({current.source}->{current.target} vs {new.source}->{new.target})"
            )
        if current.scope != new.scope:
            raise SchemaError(
                f"scope for {current.name!r} cannot change "
                f"({current.scope!r} -> {new.scope!r})"
            )
        self._require_superset(current.name, current.fields, new.fields)
        for name in sorted(set(new.fields) - set(current.fields)):
            self._execute(
                f"ALTER TABLE {current.name} ADD {name} {new.fields[name]}", None
            )
        return new

    @staticmethod
    def _require_superset(
        name: str, existing: dict[str, str], proposed: dict[str, str]
    ) -> None:
        for field, field_type in existing.items():
            if field not in proposed:
                raise SchemaError(
                    f"destructive migration rejected: field {field!r} of {name!r} "
                    "would be dropped"
                )
            if proposed[field] != field_type:
                raise SchemaError(
                    f"destructive migration rejected: field {field!r} of {name!r} "
                    f"changes type {field_type} -> {proposed[field]}"
                )

    def _persist(self, definition: NodeType | EdgeType) -> None:
        payload = json.dumps(definition.to_dict(), sort_keys=True, separators=(",", ":"))
        row = self._execute(
            "MATCH (s:GraphSchema {name:$name}) RETURN s.name AS name",
            {"name": definition.name},
        )
        if row:
            self._execute(
                "MATCH (s:GraphSchema {name:$name}) SET s.kind=$kind, "
                "s.definition=$definition, s.version=$version",
                {
                    "name": definition.name,
                    "kind": "node" if isinstance(definition, NodeType) else "edge",
                    "definition": payload,
                    "version": _SCHEMA_VERSION,
                },
            )
        else:
            self._execute(
                "CREATE (s:GraphSchema {name:$name, kind:$kind, "
                "definition:$definition, version:$version})",
                {
                    "name": definition.name,
                    "kind": "node" if isinstance(definition, NodeType) else "edge",
                    "definition": payload,
                    "version": _SCHEMA_VERSION,
                },
            )

    # ── verification ─────────────────────────────────────────────────────
    def verify(self, execute: _EXECUTE) -> None:
        rows = execute("CALL show_tables() RETURN name AS name", None)
        tables = {row["name"] for row in rows}
        expected = set(self._nodes) | set(self._edges) | set(INTERNAL_TABLES)
        missing = expected - tables
        if missing:
            raise SchemaError(
                f"database is missing registered tables: {sorted(missing)}"
            )
        unknown = tables - expected
        if unknown:
            raise SchemaError(
                f"database contains unregistered tables: {sorted(unknown)}"
            )
