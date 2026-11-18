"""Process-local limits for the graph subsystem.

v1 intentionally has no ``graph.*`` config section: these defaults are fixed
and callers that need different budgets (tests, future integrations) pass a
``GraphLimits`` instance to ``GraphClient.open``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .exceptions import ValidationError


@dataclass(frozen=True)
class GraphLimits:
    max_properties_bytes: int = 256 * 1024
    max_snapshot_nodes: int = 100_000
    max_snapshot_edges: int = 500_000
    max_snapshot_bytes: int = 256 * 1024 * 1024
    max_query_rows: int = 100
    max_query_bytes: int = 1024 * 1024
    query_timeout_ms: int = 2000
    write_lock_timeout_s: float = 5.0
    snapshot_cache_size: int = 8
    max_search_limit: int = 1000
    max_traversal_depth: int = 10

    def __post_init__(self) -> None:
        for name in (
            "max_properties_bytes",
            "max_snapshot_nodes",
            "max_snapshot_edges",
            "max_snapshot_bytes",
            "max_query_rows",
            "max_query_bytes",
            "query_timeout_ms",
            "snapshot_cache_size",
            "max_search_limit",
            "max_traversal_depth",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValidationError(f"GraphLimits.{name} must be a positive integer")
        if not isinstance(self.write_lock_timeout_s, (int, float)) or isinstance(
            self.write_lock_timeout_s, bool
        ) or self.write_lock_timeout_s <= 0:
            raise ValidationError("GraphLimits.write_lock_timeout_s must be positive")
