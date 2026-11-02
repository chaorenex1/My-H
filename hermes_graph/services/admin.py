"""Operational surface: stats, backup, and close."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..exceptions import GraphError, GraphReadOnlyError
from ..models import Health

if TYPE_CHECKING:
    from ..context import GraphContext


class AdminService:
    def __init__(self, ctx: GraphContext):
        self.ctx = ctx

    def stats(self) -> dict[str, Any]:
        def _read(connection: Any) -> dict[str, Any]:
            node_count = 0
            for definition in self.ctx.schema.node_types():
                rows = self.ctx.store.execute(
                    connection,
                    f"MATCH (n:{definition.name}) RETURN count(*) AS count",
                    {},
                )
                node_count += int(rows[0]["count"]) if rows else 0
            edge_count = 0
            for definition in self.ctx.schema.edge_types():
                rows = self.ctx.store.execute(
                    connection,
                    f"MATCH ()-[r:{definition.name}]->() RETURN count(*) AS count",
                    {},
                )
                edge_count += int(rows[0]["count"]) if rows else 0
            return {
                "path": str(self.ctx.path),
                "size_bytes": self.ctx.store.file_size(),
                "storage_version": self.ctx.store.storage_version(),
                "node_count": node_count,
                "edge_count": edge_count,
                "scope_versions": self.ctx.scope_versions(connection),
            }

        return self.ctx.run_read(_read)

    def health(self) -> Health:
        reason = None
        if self.ctx.closed:
            return Health(
                status="closed",
                path=str(self.ctx.path),
                read_only=self.ctx.read_only,
                writable=False,
                reason="client closed",
            )
        try:
            stats = self.stats()
        except GraphError as exc:
            return Health(
                status="error",
                path=str(self.ctx.path),
                read_only=self.ctx.read_only,
                writable=False,
                reason=str(exc),
            )
        return Health(
            status="ok",
            path=stats["path"],
            read_only=self.ctx.read_only,
            writable=not self.ctx.read_only and not self.ctx.closed,
            storage_version=stats["storage_version"],
            node_count=stats["node_count"],
            edge_count=stats["edge_count"],
            scope_versions=stats["scope_versions"],
            reason=reason,
        )

    def backup(self, destination: str | Path | None = None) -> str:
        if self.ctx.read_only:
            raise GraphReadOnlyError("cannot back up through a read-only client")
        target = self._backup_path(destination)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.ctx.exclusive() as connection:
            self.ctx.store.checkpoint(connection)
            self.ctx.store.close()
            try:
                shutil.copy2(self.ctx.path, target)
            finally:
                self._reopen()
        self.ctx._tls.read_connection = None
        self.ctx.schema.verify(self.ctx.schema_execute)
        return str(target)

    def checkpoint(self) -> None:
        if self.ctx.read_only:
            raise GraphReadOnlyError("cannot checkpoint a read-only client")
        with self.ctx.exclusive() as connection:
            self.ctx.store.checkpoint(connection)

    def close(self) -> None:
        self.ctx.index.invalidate()
        self.ctx.store.close()
        self.ctx.closed = True
        self.ctx._tls.read_connection = None

    def _reopen(self) -> None:
        from ..adapters.ladybug.store import LadybugStore

        self.ctx.store = LadybugStore(self.ctx.path, read_only=False)

    def _backup_path(self, destination: str | Path | None) -> Path:
        if destination is not None:
            target = Path(destination).expanduser()
            if target.is_dir():
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                target = target / f"graph-{stamp}.lbdb"
            return target
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.ctx.path.parent / "backups" / f"graph-{stamp}.lbdb"
