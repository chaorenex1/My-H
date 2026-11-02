"""Hermes graph subsystem: Ladybug storage + NetworkX algorithms.

``import hermes_graph`` deliberately does not import either optional backend.
Only ``GraphClient.open()`` (Ladybug) and algorithm/snapshot calls (NetworkX)
touch them, so the package stays importable on machines without the
``[graph]`` extra.
"""

from __future__ import annotations

from .client import GraphClient
from .config import GraphLimits
from .exceptions import (
    AlgorithmError,
    AlgorithmTimeoutError,
    AlgorithmUnavailableError,
    GraphError,
    GraphLockedError,
    GraphReadOnlyError,
    GraphUnavailableError,
    NotFoundError,
    PropertySizeError,
    QueryError,
    QueryLimitError,
    QueryTimeoutError,
    SchemaError,
    SnapshotTooLargeError,
    TransactionBusyError,
    TransactionError,
    ValidationError,
)
from .models import (
    Capabilities,
    Edge,
    GraphPath,
    GraphSnapshot,
    Health,
    Node,
    QueryResult,
)
from .schema.definitions import EdgeType, NodeType

__all__ = [
    "AlgorithmError",
    "AlgorithmTimeoutError",
    "AlgorithmUnavailableError",
    "Capabilities",
    "Edge",
    "EdgeType",
    "GraphClient",
    "GraphError",
    "GraphLimits",
    "GraphLockedError",
    "GraphPath",
    "GraphReadOnlyError",
    "GraphSnapshot",
    "GraphUnavailableError",
    "Health",
    "Node",
    "NodeType",
    "NotFoundError",
    "PropertySizeError",
    "QueryError",
    "QueryLimitError",
    "QueryResult",
    "QueryTimeoutError",
    "SchemaError",
    "SnapshotTooLargeError",
    "TransactionBusyError",
    "TransactionError",
    "ValidationError",
]
