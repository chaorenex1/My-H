"""``GraphClient``: the public facade and profile-scoped client registry."""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from pathlib import Path

from hermes_constants import get_hermes_home, hermes_home_key

from .config import GraphLimits
from .context import GraphContext
from .exceptions import GraphLockedError, ValidationError
from .models import Capabilities, Health
from .services.admin import AdminService
from .services.algorithms import AlgorithmService
from .services.edges import EdgeService
from .services.nodes import NodeService
from .services.query import QueryService
from .services.schema import SchemaService
from .services.transaction import TransactionService
from .services.traversal import TraversalService

logger = logging.getLogger(__name__)

_REGISTRY: dict[tuple[str, str], GraphClient] = {}
_REGISTRY_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


def _database_path(home: Path) -> Path:
    return home / "graph" / "graph.lbdb"


def _registry_key(home: Path, path: Path) -> tuple[str, str]:
    return (hermes_home_key(home), os.path.normcase(os.path.abspath(str(path))))


class GraphClient:
    """Facade over one profile's graph database.

    Clients are process-scoped and keyed by ``(hermes_home_key, db path)``.
    Two ``open()`` calls for the same file return the same object; a
    read-only client never upgrades to a writer.
    """

    def __init__(
        self,
        home: Path,
        path: Path,
        *,
        read_only: bool,
        limits: GraphLimits,
    ):
        self.home = home
        self.path = path
        self.read_only = read_only
        self.limits = limits
        self._key = _registry_key(home, path)
        self.ctx = GraphContext(home, path, read_only=read_only, limits=limits)
        self.nodes = NodeService(self.ctx)
        self.edges = EdgeService(self.ctx)
        self.query = QueryService(self.ctx)
        self.traverse = TraversalService(self.ctx)
        self.algorithms = AlgorithmService(self.ctx)
        self.index = self.ctx.index
        self.transaction = TransactionService(self.ctx)
        self.schema = SchemaService(self.ctx)
        self.admin = AdminService(self.ctx)

    # ── lifecycle ────────────────────────────────────────────────────────
    @classmethod
    def open(
        cls, read_only: bool = False, limits: GraphLimits | None = None
    ) -> GraphClient:
        global _ATEXIT_REGISTERED
        if not isinstance(read_only, bool):
            raise ValidationError("read_only must be a bool")
        if limits is not None and not isinstance(limits, GraphLimits):
            raise ValidationError("limits must be a GraphLimits instance")
        home = Path(get_hermes_home())
        path = _database_path(home)
        key = _registry_key(home, path)
        with _REGISTRY_LOCK:
            existing = _REGISTRY.get(key)
            if existing is not None and existing.ctx.closed:
                _REGISTRY.pop(key, None)
                existing = None
            if existing is not None:
                if not read_only and existing.read_only:
                    raise GraphLockedError(
                        f"{path} is already open read-only in this process; "
                        "close it before opening a writer",
                        details={"path": str(path)},
                    )
                existing.ctx.last_used = time.monotonic()
                return existing
            client = cls(
                home,
                path,
                read_only=read_only,
                limits=limits or GraphLimits(),
            )
            _REGISTRY[key] = client
            if not _ATEXIT_REGISTERED:
                atexit.register(_close_all)
                _ATEXIT_REGISTERED = True
            return client

    def __enter__(self) -> GraphClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self.admin.close()
        with _REGISTRY_LOCK:
            if _REGISTRY.get(self._key) is self:
                del _REGISTRY[self._key]

    @classmethod
    def close_all(cls) -> None:
        _close_all()

    @classmethod
    def close_idle(cls, ttl_s: float = 300.0) -> list[str]:
        cutoff = time.monotonic() - ttl_s
        closed: list[str] = []
        with _REGISTRY_LOCK:
            clients = list(_REGISTRY.items())
        for key, client in clients:
            if client.ctx.last_used > cutoff:
                continue
            client.close()
            closed.append(str(client.path))
        return closed

    # ── introspection ────────────────────────────────────────────────────
    def capabilities(self) -> Capabilities:
        algorithms: tuple[str, ...] = ()
        try:
            import networkx  # availability probe only
        except ImportError:
            pass
        else:
            algorithms = (
                "pagerank",
                "betweenness",
                "closeness",
                "louvain",
                "shortest_path",
                "connected_components",
                "topological_sort",
                "degree",
            )
        features = ["snapshot", "scope_version", "profile_scoped"]
        if self.read_only:
            features.append("read_only")
        return Capabilities(
            storage=("property_graph", "cypher", "transactions"),
            algorithms=algorithms,
            features=tuple(features),
        )

    def health(self) -> Health:
        return self.admin.health()


def _close_all() -> None:
    with _REGISTRY_LOCK:
        clients = list(_REGISTRY.values())
        _REGISTRY.clear()
    for client in clients:
        try:
            client.admin.close()
        except Exception:
            logger.debug("error closing graph client at exit", exc_info=True)
