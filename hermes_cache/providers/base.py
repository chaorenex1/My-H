"""Provider contracts for the cache subsystem."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """Opaque value plus storage metadata; providers never interpret policy."""

    namespace: str
    key: str
    payload: bytes
    codec: str
    created_at: float
    expires_at: float | None


@dataclass(frozen=True, slots=True)
class ProviderStats:
    entries: int
    size_bytes: int


class CacheProvider(Protocol):
    """Storage-only contract. TTL, serialization and L1 live in the engine."""

    def get(self, namespace: str, key: str) -> ProviderEntry | None: ...

    def get_many(
        self, namespace: str, keys: Sequence[str]
    ) -> dict[str, ProviderEntry]: ...

    def set(self, entry: ProviderEntry) -> None: ...

    def set_many(self, entries: Sequence[ProviderEntry]) -> None: ...

    def delete(self, namespace: str, key: str) -> bool: ...

    def delete_many(self, namespace: str, keys: Sequence[str]) -> int: ...

    def delete_namespace(self, namespace: str) -> int: ...

    def clear(self) -> int: ...

    def apply_batch(
        self,
        puts: Sequence[ProviderEntry],
        deletes: Sequence[tuple[str, str]],
    ) -> None: ...

    def purge_expired(self, now: float, limit: int = 1000) -> int: ...

    def trim_to_bytes(self, max_bytes: int, now: float) -> int: ...

    def stats(self) -> ProviderStats: ...

    def close(self) -> None: ...
