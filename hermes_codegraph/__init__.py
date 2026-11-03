"""Hermes code intelligence: a persistent, evidence-backed code graph.

``import hermes_codegraph`` stays import-light: Tree-sitter, Ladybug and
NetworkX are touched only when ``CodeIntelligence.open()`` / ``index()`` is
actually used.
"""

from __future__ import annotations

from .api import CodeIntelligence
from .exceptions import (
    CodeGraphError,
    CodeGraphUnavailableError,
    CoverageContextMissingError,
    EvidenceRejectedError,
    GraphConflictError,
    IndexLimitError,
    IndexStaleError,
    LanguageDescriptorError,
    LanguageUnsupportedError,
    ParseFailedError,
    RevisionMismatchError,
    RevisionUnavailableError,
    UnsupportedFeatureError,
)
from .models import (
    SCHEMA_VERSION,
    CodeQueryMetadata,
    CodeQueryResult,
    SymbolLocator,
)

__all__ = [
    "SCHEMA_VERSION",
    "CodeGraphError",
    "CodeGraphUnavailableError",
    "CodeIntelligence",
    "CodeQueryMetadata",
    "CodeQueryResult",
    "CoverageContextMissingError",
    "EvidenceRejectedError",
    "GraphConflictError",
    "IndexLimitError",
    "IndexStaleError",
    "LanguageDescriptorError",
    "LanguageUnsupportedError",
    "ParseFailedError",
    "RevisionMismatchError",
    "RevisionUnavailableError",
    "SymbolLocator",
    "UnsupportedFeatureError",
]
