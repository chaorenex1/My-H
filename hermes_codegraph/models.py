"""Public data types for code intelligence.

``hermes_graph`` models and parser/backend types never appear in these
signatures. ``CodeQueryResult``/``CodeQueryMetadata`` are the stable envelope
every query method returns (ADR-8 D8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class SymbolLocator:
    """Where a symbol was observed. Not an identity: compare ``symbol_id``."""

    repository_id: str
    file_path: str
    qualified_name: str
    kind: str
    language: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "file_path": self.file_path,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "language": self.language,
        }


@dataclass(frozen=True)
class CodeQueryMetadata:
    total: int
    truncated: bool
    index_status: str
    stale: bool
    freshness: str
    unresolved_count: int
    coverage_available: bool
    policy: str
    analysis_timestamp: datetime
    limits: dict[str, int] = field(default_factory=dict)
    unavailable: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "total": self.total,
            "truncated": self.truncated,
            "index_status": self.index_status,
            "stale": self.stale,
            "freshness": self.freshness,
            "unresolved_count": self.unresolved_count,
            "coverage_available": self.coverage_available,
            "policy": self.policy,
            "analysis_timestamp": self.analysis_timestamp.isoformat(),
            "limits": dict(self.limits),
        }
        if self.unavailable:
            payload["unavailable"] = self.unavailable
        return payload


@dataclass(frozen=True)
class CodeQueryResult:
    schema_version: str
    query_id: str
    repository_id: str
    revision_id: str
    snapshot_hash: str | None
    results: list[dict[str, Any]]
    metadata: CodeQueryMetadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "query_id": self.query_id,
            "repository_id": self.repository_id,
            "revision_id": self.revision_id,
            "snapshot_hash": self.snapshot_hash,
            "results": list(self.results),
            "metadata": self.metadata.to_dict(),
        }
