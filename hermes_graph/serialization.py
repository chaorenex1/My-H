"""Validation, JSON encoding, and deterministic id helpers."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from .exceptions import PropertySizeError, ValidationError

#: UUIDv5 namespace for ``(type, key)`` identities. Changing this would
#: silently fork every existing upsert identity, so it is versioned in the
#: name string rather than being configurable.
ID_NAMESPACE = uuid.NAMESPACE_URL

_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_NODE_TYPE_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*$")
_EDGE_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

FIELD_TYPES = frozenset({"STRING", "INT64", "DOUBLE", "BOOL"})

NODE_RESERVED_FIELDS = frozenset(
    {"id", "key", "created_at", "updated_at", "deleted_at", "props"}
)
EDGE_RESERVED_FIELDS = frozenset({"id", "created_at", "props", "source_id", "target_id"})

INTERNAL_TABLES = frozenset(
    {"GraphNodeRef", "GraphEdgeRef", "GraphScopeVersion", "GraphSchema"}
)

# Identifiers are interpolated into DDL, so reject Cypher keywords as well as
# the characters that would need quoting.
_CYPHER_KEYWORDS = frozenset(
    {
        "alter", "and", "as", "attach", "begin", "by", "call", "case",
        "checkpoint", "commit", "copy", "create", "delete", "detach",
        "distinct", "drop", "else", "end", "foreach", "from", "insert",
        "limit", "load", "match", "merge", "not", "null", "optional", "or",
        "order", "remove", "return", "rollback", "set", "skip", "then",
        "to", "true", "false", "unwind", "when", "where", "with",
    }
)

_uuid_lock = threading.Lock()
_last_ms = -1
_counter = 0


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationError(f"invalid stored timestamp: {value!r}") from exc


def new_node_id() -> str:
    """RFC 9562 UUIDv7: time-ordered, monotonic within one process."""
    global _last_ms, _counter
    with _uuid_lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _last_ms = ms
            _counter = secrets.randbelow(1 << 12)
        else:
            ms = _last_ms
            _counter += 1
            if _counter >= (1 << 12):
                # 4096 ids in one millisecond: wait for the next tick rather
                # than reusing a timestamp+counter pair.
                while ms <= _last_ms:
                    ms = time.time_ns() // 1_000_000
                _last_ms = ms
                _counter = secrets.randbelow(1 << 12)
        rand_a = _counter & 0x0FFF
        rand_b = secrets.randbits(62)
        value = ((ms & ((1 << 48) - 1)) << 80) | (0x7 << 76) | (rand_a << 64)
        value |= (0b10 << 62) | rand_b
        return str(uuid.UUID(int=value))


def derived_id(node_type: str, key: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"hermes_graph:v1:{node_type}:{key}"))


def derived_edge_id(
    edge_type: str,
    source_id: str,
    target_id: str,
    key: str | None = None,
) -> str:
    """Stable identity for ``edges.upsert``: ``(type, source, target, key)``.

    Deliberately separate from :func:`derived_id`: edges are not nodes and the
    empty key is a legitimate "ungrouped" identity. Callers must validate an
    explicit key before reaching here.
    """
    return str(
        uuid.uuid5(
            ID_NAMESPACE,
            f"hermes_graph:v1:edge:{edge_type}:{source_id}:{target_id}:{key or ''}",
        )
    )


def validate_scope(scope: str) -> str:
    if not isinstance(scope, str) or not _SCOPE_RE.match(scope):
        raise ValidationError(f"invalid scope {scope!r}; expected lower_snake_case")
    return scope


def validate_node_type_name(name: str) -> str:
    if not isinstance(name, str) or not _NODE_TYPE_RE.match(name):
        raise ValidationError(f"invalid node type {name!r}; expected PascalCase")
    if name in INTERNAL_TABLES or name.startswith("Graph"):
        raise ValidationError(f"node type {name!r} is reserved")
    if name.lower() in _CYPHER_KEYWORDS:
        raise ValidationError(f"node type {name!r} is a Cypher keyword")
    return name


def validate_edge_type_name(name: str) -> str:
    if not isinstance(name, str) or not _EDGE_TYPE_RE.match(name):
        raise ValidationError(f"invalid edge type {name!r}; expected UPPER_SNAKE_CASE")
    if name in INTERNAL_TABLES or name.startswith("Graph"):
        raise ValidationError(f"edge type {name!r} is reserved")
    if name.lower() in _CYPHER_KEYWORDS:
        raise ValidationError(f"edge type {name!r} is a Cypher keyword")
    return name


def validate_field_name(name: str, reserved: frozenset[str]) -> str:
    if not isinstance(name, str) or not _FIELD_RE.match(name):
        raise ValidationError(f"invalid field name {name!r}")
    if name in reserved:
        raise ValidationError(f"field name {name!r} is reserved")
    if name.lower() in _CYPHER_KEYWORDS:
        raise ValidationError(f"field name {name!r} is a Cypher keyword")
    return name


def validate_field_type(field_type: str) -> str:
    if field_type not in FIELD_TYPES:
        raise ValidationError(
            f"invalid field type {field_type!r}; expected one of {sorted(FIELD_TYPES)}"
        )
    return field_type


def validate_node_id(node_id: str) -> str:
    if not isinstance(node_id, str) or not node_id:
        raise ValidationError("node id must be a non-empty string")
    try:
        uuid.UUID(node_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValidationError(f"invalid node id {node_id!r}; expected a UUID") from exc
    return node_id


def validate_key(key: str) -> str:
    if not isinstance(key, str) or not key or "\x00" in key:
        raise ValidationError("key must be a non-empty string")
    return key


def validate_properties(properties: dict[str, Any] | None) -> dict[str, Any]:
    if properties is None:
        return {}
    if not isinstance(properties, dict):
        raise ValidationError("properties must be a dict")
    for name in properties:
        if not isinstance(name, str):
            raise ValidationError("property names must be strings")
    return dict(properties)


def encode_properties(properties: dict[str, Any], *, max_bytes: int) -> str:
    try:
        text = json.dumps(
            properties,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f"properties must be JSON-serialisable: {exc}") from exc
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        raise PropertySizeError(
            f"properties serialise to {size} bytes, limit is {max_bytes}",
            details={"size": size, "max_bytes": max_bytes},
        )
    return text


def decode_properties(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValidationError("stored properties are not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError("stored properties are not a JSON object")
    return value


def coerce_field_value(field_name: str, field_type: str, value: Any) -> Any:
    """Validate a declared field value without silently retyping it."""
    if value is None:
        return None
    if field_type == "STRING":
        if not isinstance(value, str):
            raise ValidationError(f"field {field_name!r} expects STRING")
        return value
    if field_type == "BOOL":
        if not isinstance(value, bool):
            raise ValidationError(f"field {field_name!r} expects BOOL")
        return value
    if field_type == "INT64":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"field {field_name!r} expects INT64")
        if not -(2**63) <= value < 2**63:
            raise ValidationError(f"field {field_name!r} is outside INT64 range")
        return value
    if field_type == "DOUBLE":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"field {field_name!r} expects DOUBLE")
        return float(value)
    raise ValidationError(f"field {field_name!r} has unsupported type {field_type!r}")
