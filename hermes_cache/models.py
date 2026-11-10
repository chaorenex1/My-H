"""Public cache data models and sentinels."""

from __future__ import annotations

from dataclasses import dataclass

MISSING = object()
DEFAULT_NAMESPACE = "default"


@dataclass(frozen=True, slots=True)
class CacheStats:
    """Process-local cache statistics snapshot."""

    l1_hits: int = 0
    l2_hits: int = 0
    misses: int = 0
    expired: int = 0
    writes: int = 0
    deletes: int = 0
    evictions_l1: int = 0
    errors: int = 0
    batches: int = 0
    purges: int = 0

    @property
    def hits(self) -> int:
        return self.l1_hits + self.l2_hits

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
