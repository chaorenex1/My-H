"""Public exception hierarchy for the code intelligence subsystem.

Every failure surfaced by :mod:`hermes_codegraph` is one of these types.
Tree-sitter, Ladybug and NetworkX exception classes never leak through the
public API.
"""

from __future__ import annotations

from typing import Any


class CodeGraphError(Exception):
    """Base class for every code-graph failure."""

    code = "codegraph_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class CodeGraphUnavailableError(CodeGraphError):
    """The graph or codegraph extras are missing on this machine."""

    code = "codegraph_unavailable"


class UnsupportedFeatureError(CodeGraphError):
    """A feature belongs to a later phase and is not implemented yet."""

    code = "unsupported"


class LanguageUnsupportedError(CodeGraphError):
    """The language has no complete descriptor in the registry."""

    code = "language_unsupported"


class LanguageDescriptorError(CodeGraphError):
    """A language descriptor is incomplete or internally inconsistent."""

    code = "language_descriptor_invalid"


class IndexLimitError(CodeGraphError):
    """A repository exceeds a scan/index limit; completed files are kept."""

    code = "index_limit"


class ParseFailedError(CodeGraphError):
    """One file could not be parsed (recorded, never fatal for the run)."""

    code = "parse_failed"


class IndexStaleError(CodeGraphError):
    """The index does not describe the requested snapshot."""

    code = "index_stale"


class RevisionMismatchError(CodeGraphError):
    """A report or patch targets a revision other than the current snapshot."""

    code = "revision_mismatch"


class RevisionUnavailableError(CodeGraphError):
    """Historical full-graph queries are not supported."""

    code = "revision_unavailable"


class CoverageContextMissingError(CodeGraphError):
    """A coverage report has no per-test context to attribute COVERS edges."""

    code = "coverage_context_missing"


class GraphConflictError(CodeGraphError):
    """Optimistic concurrency check failed; nothing was written."""

    code = "graph_conflict"


class EvidenceRejectedError(CodeGraphError):
    """Evidence does not satisfy the write contract; nothing was written."""

    code = "evidence_rejected"
