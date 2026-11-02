"""Public data types for the graph subsystem.

These dataclasses are the contract between ``GraphClient`` and its callers.
Ladybug's ``Database``/``Connection``/``QueryResult`` and NetworkX's graph
classes MUST NOT appear in any field, argument, or return value here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    properties: dict[str, Any] = field(default_factory=dict)
    key: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "properties": self.properties,
            "key": self.key,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "deleted_at": _iso(self.deleted_at),
        }


@dataclass(frozen=True)
class Edge:
    id: str
    source_id: str
    target_id: str
    type: str
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "type": self.type,
            "properties": self.properties,
            "created_at": _iso(self.created_at),
        }


@dataclass(frozen=True)
class QueryResult:
    items: list[Node | Edge | dict[str, Any]]
    total: int | None = None
    has_more: bool = False
    next_cursor: str | None = None
    execution_time_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [_item_to_dict(item) for item in self.items],
            "total": self.total,
            "has_more": self.has_more,
            "next_cursor": self.next_cursor,
            "execution_time_ms": self.execution_time_ms,
        }


def _item_to_dict(item: Node | Edge | dict[str, Any]) -> Any:
    if isinstance(item, (Node, Edge)):
        return item.to_dict()
    return item


@dataclass(frozen=True)
class GraphPath:
    nodes: list[Node]
    edges: list[Edge]
    length: int
    weight: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "length": self.length,
            "weight": self.weight,
        }


@dataclass(frozen=True)
class GraphSnapshot:
    nodes: list[Node]
    edges: list[Edge]
    scope: str
    version: int
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "scope": self.scope,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class Capabilities:
    storage: tuple[str, ...]
    algorithms: tuple[str, ...]
    features: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "storage": list(self.storage),
            "algorithms": list(self.algorithms),
            "features": list(self.features),
        }


@dataclass(frozen=True)
class Health:
    status: str
    path: str
    read_only: bool
    writable: bool
    storage_version: str | None = None
    node_count: int = 0
    edge_count: int = 0
    scope_versions: dict[str, int] = field(default_factory=dict)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "path": self.path,
            "read_only": self.read_only,
            "writable": self.writable,
            "storage_version": self.storage_version,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "scope_versions": dict(self.scope_versions),
            "reason": self.reason,
        }
