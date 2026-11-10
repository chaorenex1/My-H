"""Value serializers for the cache engine."""

from __future__ import annotations

import json
import pickle
from typing import Any, Protocol

from .errors import SerializationError


class Serializer(Protocol):
    """Encode and decode cache values to opaque bytes."""

    name: str

    def dumps(self, value: Any) -> bytes: ...

    def loads(self, payload: bytes) -> Any: ...


class JSONSerializer:
    """Safe default serializer for JSON-compatible values."""

    name = "json.v1"

    def dumps(self, value: Any) -> bytes:
        try:
            text = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise SerializationError(f"value is not JSON-serializable: {exc}") from exc
        return text.encode("utf-8")

    def loads(self, payload: bytes) -> Any:
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise SerializationError(f"invalid JSON cache payload: {exc}") from exc


class PickleSerializer:
    """Unsafe for untrusted data; use only for trusted local values."""

    name = "pickle.v1"

    def dumps(self, value: Any) -> bytes:
        try:
            return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:  # boundary: pickle raises many concrete types
            raise SerializationError(f"value is not picklable: {exc}") from exc

    def loads(self, payload: bytes) -> Any:
        try:
            return pickle.loads(payload)
        except Exception as exc:  # boundary: malformed pickle raises many types
            raise SerializationError(f"invalid pickle cache payload: {exc}") from exc


class RawSerializer:
    """Pass-through serializer for bytes-only values."""

    name = "raw.v1"

    def dumps(self, value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, (bytearray, memoryview)):
            return bytes(value)
        raise SerializationError("RawSerializer only accepts bytes-like values")

    def loads(self, payload: bytes) -> Any:
        return bytes(payload)
