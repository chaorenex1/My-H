"""Declarative node/edge type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..exceptions import ValidationError
from ..serialization import (
    EDGE_RESERVED_FIELDS,
    NODE_RESERVED_FIELDS,
    validate_edge_type_name,
    validate_field_name,
    validate_field_type,
    validate_node_type_name,
    validate_scope,
)


def _clean_fields(
    fields: dict[str, str] | None, reserved: frozenset[str]
) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for name, field_type in (fields or {}).items():
        cleaned[validate_field_name(name, reserved)] = validate_field_type(field_type)
    return cleaned


@dataclass(frozen=True)
class NodeType:
    name: str
    scope: str
    fields: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_node_type_name(self.name))
        object.__setattr__(self, "scope", validate_scope(self.scope))
        object.__setattr__(
            self, "fields", _clean_fields(self.fields, NODE_RESERVED_FIELDS)
        )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "node", "name": self.name, "scope": self.scope,
                "fields": dict(self.fields)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NodeType:
        return cls(
            name=payload.get("name", ""),
            scope=payload.get("scope", ""),
            fields=dict(payload.get("fields") or {}),
        )


@dataclass(frozen=True)
class EdgeType:
    name: str
    source: str
    target: str
    scope: str
    fields: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_edge_type_name(self.name))
        object.__setattr__(self, "source", validate_node_type_name(self.source))
        object.__setattr__(self, "target", validate_node_type_name(self.target))
        object.__setattr__(self, "scope", validate_scope(self.scope))
        object.__setattr__(
            self, "fields", _clean_fields(self.fields, EDGE_RESERVED_FIELDS)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "edge",
            "name": self.name,
            "source": self.source,
            "target": self.target,
            "scope": self.scope,
            "fields": dict(self.fields),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EdgeType:
        return cls(
            name=payload.get("name", ""),
            source=payload.get("source", ""),
            target=payload.get("target", ""),
            scope=payload.get("scope", ""),
            fields=dict(payload.get("fields") or {}),
        )


def definition_from_dict(payload: dict[str, Any]) -> NodeType | EdgeType:
    kind = payload.get("kind")
    if kind == "node":
        return NodeType.from_dict(payload)
    if kind == "edge":
        return EdgeType.from_dict(payload)
    raise ValidationError(f"unknown schema kind {kind!r}")
