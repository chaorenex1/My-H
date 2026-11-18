"""Public exception hierarchy for the Hermes graph subsystem.

Every failure surfaced by ``hermes_graph`` is one of these types. Adapter
libraries (Ladybug, NetworkX) never leak their own exception classes through
this package; callers can rely on ``except GraphError``.
"""

from __future__ import annotations

from typing import Any


class GraphError(Exception):
    """Base class for every graph subsystem failure."""

    code = "graph_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class GraphUnavailableError(GraphError):
    """The graph backend cannot be loaded on this machine (missing extra/wheel)."""

    code = "graph_unavailable"


class GraphLockedError(GraphError):
    """Another process owns the database file, or the client is read-only."""

    code = "graph_locked"


class GraphReadOnlyError(GraphError):
    """A write was attempted through a read-only client or transaction."""

    code = "graph_read_only"


class SchemaError(GraphError):
    """Schema registration or lookup failed."""

    code = "schema_error"


class NotFoundError(GraphError):
    """A node or edge id does not exist (or is soft-deleted)."""

    code = "not_found"


class ValidationError(GraphError):
    """Input rejected before it reaches the database."""

    code = "validation_error"


class PropertySizeError(ValidationError):
    """A serialized property map exceeds the configured limit."""

    code = "property_too_large"


class TransactionError(GraphError):
    """A transaction could not start, commit, or rolled back."""

    code = "transaction_error"


class TransactionBusyError(TransactionError):
    """Another writer holds the process-wide write lock past the timeout."""

    code = "write_busy"


class QueryError(GraphError):
    """A Cypher/query execution failure."""

    code = "query_error"


class QueryLimitError(QueryError):
    """A query exceeded its row/byte/time budget."""

    code = "query_limit"


class QueryTimeoutError(QueryLimitError):
    """A query exceeded its timeout budget."""

    code = "query_timeout"


class AlgorithmError(GraphError):
    """An algorithm could not complete on the requested scope."""

    code = "algorithm_error"


class AlgorithmUnavailableError(AlgorithmError):
    """NetworkX is not installed, so graph algorithms are unavailable."""

    code = "algorithm_unavailable"


class AlgorithmTimeoutError(AlgorithmError):
    """An algorithm exceeded its execution budget."""

    code = "algorithm_timeout"


class SnapshotTooLargeError(GraphError):
    """A scope is too large to materialise as a NetworkX snapshot."""

    code = "snapshot_too_large"
