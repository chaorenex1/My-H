"""Row (de)serialisation shared by the node/edge services."""

from __future__ import annotations

from typing import Any

from ..exceptions import ValidationError
from ..models import Edge, Node
from ..schema.definitions import EdgeType, NodeType
from ..serialization import (
    coerce_field_value,
    decode_properties,
    encode_properties,
    parse_timestamp,
)

_NODE_BASE_COLUMNS = ("id", "key", "created_at", "updated_at", "deleted_at", "props")
_EDGE_BASE_COLUMNS = ("id", "source_id", "target_id", "created_at", "props")


def node_select(definition: NodeType) -> str:
    columns = [f"n.{name} AS {name}" for name in _NODE_BASE_COLUMNS]
    columns.extend(f"n.{name} AS {name}" for name in definition.fields)
    return ", ".join(columns)


def edge_select(definition: EdgeType) -> str:
    columns = [
        "r.id AS id",
        "a.id AS source_id",
        "b.id AS target_id",
        "r.created_at AS created_at",
        "r.props AS props",
    ]
    columns.extend(f"r.{name} AS {name}" for name in definition.fields)
    return ", ".join(columns)


def row_to_node(definition: NodeType, row: dict[str, Any]) -> Node:
    properties = decode_properties(row.get("props"))
    for name in definition.fields:
        value = row.get(name)
        if value is not None:
            properties[name] = value
    return Node(
        id=str(row["id"]),
        type=definition.name,
        properties=properties,
        key=row.get("key"),
        created_at=parse_timestamp(row.get("created_at")),
        updated_at=parse_timestamp(row.get("updated_at")),
        deleted_at=parse_timestamp(row.get("deleted_at")),
    )


def row_to_edge(definition: EdgeType, row: dict[str, Any]) -> Edge:
    properties = decode_properties(row.get("props"))
    for name in definition.fields:
        value = row.get(name)
        if value is not None:
            properties[name] = value
    return Edge(
        id=str(row["id"]),
        source_id=str(row["source_id"]),
        target_id=str(row["target_id"]),
        type=definition.name,
        properties=properties,
        created_at=parse_timestamp(row.get("created_at")),
    )


def split_properties(
    fields: dict[str, str],
    properties: dict[str, Any],
    *,
    max_bytes: int,
) -> tuple[dict[str, Any], str]:
    """Split into declared typed columns and the undeclared JSON blob."""
    encode_properties(properties, max_bytes=max_bytes)
    declared: dict[str, Any] = {}
    undeclared: dict[str, Any] = {}
    for name, value in properties.items():
        if name in fields:
            declared[name] = coerce_field_value(name, fields[name], value)
        else:
            undeclared[name] = value
    return declared, encode_properties(undeclared, max_bytes=max_bytes)


def validate_property_names(properties: dict[str, Any], reserved: set[str]) -> None:
    clash = reserved.intersection(properties)
    if clash:
        raise ValidationError(
            f"property names collide with reserved fields: {sorted(clash)}"
        )
