"""Cache policy defaults and validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .errors import InvalidTTLError


class EvictionPolicy(str, Enum):
    """Victim-selection order used by L1 and L2 eviction."""

    LRU = "lru"
    LFU = "lfu"
    FIFO = "fifo"
    RR = "rr"

    @classmethod
    def coerce(cls, value: object) -> EvictionPolicy:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            aliases = {
                "lru": cls.LRU,
                "lfu": cls.LFU,
                "fifo": cls.FIFO,
                "rr": cls.RR,
                "random": cls.RR,
            }
            try:
                return aliases[normalized]
            except KeyError:
                pass
        raise ValueError(f"invalid eviction policy: {value!r}")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return math.isfinite(float(value))


def _positive_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"CachePolicy.{name} must be a positive integer")


def _non_negative_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"CachePolicy.{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """Limits and failure behavior for one cache instance."""

    default_ttl: float | None = None
    l1_enabled: bool = True
    max_l1_entries: int = 2048
    max_l1_bytes: int = 64 * 1024 * 1024
    l1_max_age: float | None = 5.0
    max_value_bytes: int = 32 * 1024 * 1024
    max_key_bytes: int = 4096
    maintenance_every_writes: int = 256
    max_l2_bytes: int | None = None
    l1_eviction: EvictionPolicy = EvictionPolicy.LRU
    l2_eviction: EvictionPolicy = EvictionPolicy.FIFO
    background_cleanup: bool = True
    cleanup_interval: float = 300.0
    cleanup_batch_limit: int = 1000
    provider_retries: int = 2
    provider_retry_delay: float = 0.05
    fail_open: bool = True
    raise_on_corrupt: bool = False

    def __post_init__(self) -> None:
        if self.default_ttl is not None:
            if not _finite(self.default_ttl) or float(self.default_ttl) < 0:
                raise InvalidTTLError("CachePolicy.default_ttl must be finite and >= 0")
        if not isinstance(self.l1_enabled, bool):
            raise ValueError("CachePolicy.l1_enabled must be a bool")
        _non_negative_int("max_l1_entries", self.max_l1_entries)
        _positive_int("max_l1_bytes", self.max_l1_bytes)
        if self.l1_max_age is not None:
            if not _finite(self.l1_max_age) or float(self.l1_max_age) <= 0:
                raise ValueError("CachePolicy.l1_max_age must be finite and > 0")
        _positive_int("max_value_bytes", self.max_value_bytes)
        _positive_int("max_key_bytes", self.max_key_bytes)
        _non_negative_int(
            "maintenance_every_writes", self.maintenance_every_writes
        )
        if self.max_l2_bytes is not None:
            _positive_int("max_l2_bytes", self.max_l2_bytes)
        object.__setattr__(
            self, "l1_eviction", EvictionPolicy.coerce(self.l1_eviction)
        )
        object.__setattr__(
            self, "l2_eviction", EvictionPolicy.coerce(self.l2_eviction)
        )
        if not isinstance(self.background_cleanup, bool):
            raise ValueError("CachePolicy.background_cleanup must be a bool")
        if not _finite(self.cleanup_interval) or self.cleanup_interval <= 0:
            raise ValueError("CachePolicy.cleanup_interval must be finite and > 0")
        _positive_int("cleanup_batch_limit", self.cleanup_batch_limit)
        _non_negative_int("provider_retries", self.provider_retries)
        if not _finite(self.provider_retry_delay) or self.provider_retry_delay < 0:
            raise ValueError(
                "CachePolicy.provider_retry_delay must be finite and >= 0"
            )
        if not isinstance(self.fail_open, bool):
            raise ValueError("CachePolicy.fail_open must be a bool")
        if not isinstance(self.raise_on_corrupt, bool):
            raise ValueError("CachePolicy.raise_on_corrupt must be a bool")
