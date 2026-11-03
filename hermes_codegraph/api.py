"""``CodeIntelligence``: the public facade for one repository.

Opening resolves the Git root before touching the graph, binds exactly one
repository, and never imports a parser or backend at module import time.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from .config import CodeGraphLimits
from .exceptions import CodeGraphError, CodeGraphUnavailableError, LanguageUnsupportedError
from .languages import (
    LanguageRegistry,
    default_registry,
    install_target_gap,
)
from .scanner import RepositoryInfo, resolve_repository


class CodeIntelligence:
    def __init__(
        self,
        client: Any,
        repository: RepositoryInfo,
        limits: CodeGraphLimits,
        registry: LanguageRegistry,
    ):
        self._client = client
        self.repository = repository
        self.limits = limits
        self.registry = registry

    # ── lifecycle ────────────────────────────────────────────────────────
    @classmethod
    def open(
        cls,
        root: str | Path | None = None,
        *,
        read_only: bool = False,
        limits: CodeGraphLimits | None = None,
        registry: LanguageRegistry | None = None,
    ) -> CodeIntelligence:
        repository = resolve_repository(root)
        from hermes_graph import GraphClient

        try:
            client = GraphClient.open(read_only=read_only)
        except Exception as exc:
            name = type(exc).__name__
            if name in ("GraphUnavailableError", "GraphLockedError", "GraphReadOnlyError"):
                details = dict(getattr(exc, "details", {}) or {})
                raise CodeGraphUnavailableError(str(exc), details=details) from exc
            raise
        return cls(
            client=client,
            repository=repository,
            limits=limits or CodeGraphLimits.from_config(),
            registry=registry or default_registry(),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CodeIntelligence:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ── indexing ─────────────────────────────────────────────────────────
    def index(self, *, full: bool = False, semantic: bool = False) -> dict[str, Any]:
        from .indexer import Indexer

        if self._client.read_only:
            raise CodeGraphError("cannot index through a read-only graph client")
        indexer = Indexer(self._client, self.repository, self.limits, self.registry)
        result = indexer.run(full=full, semantic=semantic)
        self.repository = indexer.repo
        return result

    # ── read surfaces ────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        from .query import status_payload

        return status_payload(self._client, self.repository, self.registry)

    def find_symbols(
        self,
        *,
        name: str | None = None,
        kind: str | None = None,
        language: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        from .query import find_symbols

        return find_symbols(
            self._client,
            self.repository,
            self.registry,
            name=name,
            kind=kind,
            language=language,
            limit=limit,
        ).to_dict()

    def get_symbol(self, symbol_id: str) -> dict[str, Any]:
        from .query import get_symbol

        return get_symbol(
            self._client, self.repository, self.registry, symbol_id
        ).to_dict()

    def get_definition(self, symbol_id: str) -> dict[str, Any]:
        from .query import get_definition

        return get_definition(
            self._client, self.repository, self.registry, symbol_id
        ).to_dict()

    def get_references(
        self, symbol_id: str, *, policy: str = "balanced", limit: int = 50
    ) -> dict[str, Any]:
        from .query import get_references

        return get_references(
            self._client,
            self.repository,
            self.registry,
            symbol_id,
            policy=policy,
            limit=limit,
        ).to_dict()

    def get_callers(
        self, symbol_id: str, *, depth: int = 2, policy: str = "strict",
        limit: int = 200,
    ) -> dict[str, Any]:
        from .query import get_callers

        return get_callers(
            self._client, self.repository, self.registry, symbol_id,
            depth=depth, policy=policy, limit=limit,
        ).to_dict()

    def get_callees(
        self, symbol_id: str, *, depth: int = 2, policy: str = "balanced",
        limit: int = 200,
    ) -> dict[str, Any]:
        from .query import get_callees

        return get_callees(
            self._client, self.repository, self.registry, symbol_id,
            depth=depth, policy=policy, limit=limit,
        ).to_dict()

    def get_call_path(
        self, source_id: str, target_id: str, *, max_depth: int = 5,
        policy: str = "balanced",
    ) -> dict[str, Any]:
        from .query import get_call_path

        return get_call_path(
            self._client, self.repository, self.registry, source_id, target_id,
            max_depth=max_depth, policy=policy,
        ).to_dict()

    def get_type_hierarchy(
        self, symbol_id: str, *, direction: str = "both", depth: int = 3,
        policy: str = "balanced", limit: int = 200,
    ) -> dict[str, Any]:
        from .query import get_type_hierarchy

        return get_type_hierarchy(
            self._client, self.repository, self.registry, symbol_id,
            direction=direction, depth=depth, policy=policy, limit=limit,
        ).to_dict()

    def get_implementations(
        self, symbol_id: str, *, policy: str = "balanced", limit: int = 200
    ) -> dict[str, Any]:
        from .query import get_implementations

        return get_implementations(
            self._client, self.repository, self.registry, symbol_id,
            policy=policy, limit=limit,
        ).to_dict()

    def get_dependencies(
        self, module_id: str, *, depth: int = 2, policy: str = "balanced",
        limit: int = 200,
    ) -> dict[str, Any]:
        from .query import get_dependencies

        return get_dependencies(
            self._client, self.repository, self.registry, module_id,
            depth=depth, policy=policy, limit=limit,
        ).to_dict()

    def get_cycles(
        self, *, scope: str = "modules", policy: str = "balanced", limit: int = 100
    ) -> dict[str, Any]:
        from .query import get_cycles

        return get_cycles(
            self._client, self.repository, self.registry,
            scope=scope, policy=policy, limit=limit,
        ).to_dict()

    def get_impact(
        self, symbol_id: str, *, depth: int = 3,
        include: tuple[str, ...] = (
            "callers", "references", "dependencies", "implementations", "tests"
        ),
        policy: str = "strict", limit: int = 200,
    ) -> dict[str, Any]:
        from .query import get_impact

        return get_impact(
            self._client, self.repository, self.registry, symbol_id,
            depth=depth, include=include, policy=policy, limit=limit,
        ).to_dict()

    def get_tests_for_symbol(
        self, symbol_id: str, *, policy: str = "balanced", limit: int = 200
    ) -> dict[str, Any]:
        from .query import get_tests_for_symbol

        return get_tests_for_symbol(
            self._client, self.repository, self.registry, symbol_id,
            policy=policy, limit=limit,
        ).to_dict()

    def import_coverage(
        self, path: str, *, revision: str | None = None, format: str = "auto"
    ) -> dict[str, Any]:
        from .coverage import import_coverage

        return import_coverage(
            self._client, self.repository, path, revision=revision, format=format
        )

    def get_coverage(self, symbol_id: str, *, limit: int = 200) -> dict[str, Any]:
        from .query import get_coverage

        return get_coverage(
            self._client, self.repository, self.registry, symbol_id, limit=limit
        ).to_dict()

    def get_diagnostics(
        self, *, path: str | None = None, rule: str | None = None,
        severity: str | None = None, limit: int = 50,
    ) -> dict[str, Any]:
        from .query import get_diagnostics

        return get_diagnostics(
            self._client, self.repository, self.registry,
            path=path, rule=rule, severity=severity, limit=limit,
        ).to_dict()

    def get_changes(
        self, symbol_id: str, *, from_revision: str | None = None,
        to_revision: str | None = None, limit: int = 200,
    ) -> dict[str, Any]:
        from .query import get_changes

        return get_changes(
            self._client, self.repository, self.registry, symbol_id,
            from_revision=from_revision, to_revision=to_revision, limit=limit,
        ).to_dict()

    def get_revision_changes(
        self, revision_id: str, *, limit: int = 500
    ) -> dict[str, Any]:
        from .query import get_revision_changes

        return get_revision_changes(
            self._client, self.repository, self.registry, revision_id, limit=limit
        ).to_dict()

    def prune(
        self, *, keep: int = 50, older_than: str | None = None
    ) -> dict[str, Any]:
        from .query import prune_revisions

        if self._client.read_only:
            raise CodeGraphError("cannot prune through a read-only graph client")
        return prune_revisions(
            self._client, self.repository, keep=keep, older_than=older_than
        )

    def refresh_file(
        self, path: str, *, semantic: bool = False, semgrep: bool = False
    ) -> dict[str, Any]:
        from .update import refresh_file
        from .scanner import resolve_repository

        if self._client.read_only:
            raise CodeGraphError("cannot update through a read-only graph client")
        self.repository = resolve_repository(self.repository.root)
        return refresh_file(
            self._client, self.repository, self.registry, self.limits, path,
            semantic=semantic, semgrep=semgrep,
        )

    def apply_evidence(
        self,
        path: str,
        facts: list[dict[str, Any]],
        *,
        expected_revision_id: str,
        expected_content_hash: str,
    ) -> dict[str, Any]:
        from .update import apply_evidence

        if self._client.read_only:
            raise CodeGraphError("cannot update through a read-only graph client")
        try:
            return apply_evidence(
                self._client, self.repository, self.registry, self.limits, path, facts,
                expected_revision_id=expected_revision_id,
                expected_content_hash=expected_content_hash,
            )
        except Exception as exc:  # health: allow BLE001 -- unwraps graph transaction errors from update.py
            raise _unwrap_transaction_error(exc) from None

    def invalidate(self, ids: list[str], *, reason: str = "") -> dict[str, Any]:
        from .update import invalidate

        if self._client.read_only:
            raise CodeGraphError("cannot update through a read-only graph client")
        try:
            return invalidate(self._client, self.repository, ids=ids, reason=reason)
        except Exception as exc:  # health: allow BLE001 -- unwraps graph transaction errors from update.py
            raise _unwrap_transaction_error(exc) from None

    def build_context(
        self, target: str, *, budget_tokens: int = 6000
    ) -> dict[str, Any]:
        from .context import build_context

        return build_context(
            self._client, self.repository, self.registry, target,
            budget_tokens=budget_tokens,
        )

    def languages(self) -> list[dict[str, Any]]:
        enabled = {
            descriptor.language
            for descriptor in self.registry.descriptors()
            if descriptor.extra is None
        }
        enabled.update(self.limits.languages)
        items: list[dict[str, Any]] = []
        for descriptor in self.registry.descriptors():
            grammar_available = _module_available(descriptor.grammar_package)
            items.append(
                {
                    **descriptor.to_dict(),
                    "installed": grammar_available,
                    "enabled": descriptor.language in enabled,
                }
            )
        return items

    def install_language(self, language: str) -> dict[str, Any]:
        descriptor = self.registry.get(language)
        extra = descriptor.extra
        if not extra:
            raise LanguageUnsupportedError(
                f"{descriptor.language!r} is a built-in language; its grammar ships "
                "with the [codegraph] extra",
                details={"language": descriptor.language, "extra": "codegraph"},
            )
        from pm import extras, store

        gap = install_target_gap(store.current_target())
        if gap:
            raise CodeGraphUnavailableError(
                gap,
                details={
                    "language": descriptor.language,
                    "extra": extra,
                    "platform_target": store.current_target(),
                },
            )
        extras.ensure_import(extra)
        return {
            "language": descriptor.language,
            "extra": extra,
            "installed": _module_available(descriptor.grammar_package),
        }

    def doctor(self) -> dict[str, Any]:
        from .query import doctor_payload

        graph_available, codegraph_available, target = _extras_state()
        return doctor_payload(
            self._client,
            self.repository,
            self.registry,
            graph_available=graph_available,
            codegraph_available=codegraph_available,
            target=target,
        )


def _unwrap_transaction_error(exc: Exception) -> Exception:
    """Surface the original CodeGraphError from a graph transaction wrapper."""
    if type(exc).__name__ == "TransactionError":
        cause = exc.__cause__
        if isinstance(cause, CodeGraphError):
            return cause
    return exc


def _module_available(module: str) -> bool:
    import sys

    anchor = module.replace("-", "_")
    if anchor in sys.modules:
        return True
    try:
        return importlib.util.find_spec(anchor) is not None
    except (ImportError, ValueError):
        return False


def _extras_state() -> tuple[bool, bool, str | None]:
    try:
        from pm import extras

        graph_available = extras.available("graph")
        codegraph_available = extras.available("codegraph")
    except Exception:  # health: allow BLE001 -- optional PM extras probe; status must work without PM
        graph_available = _module_available("ladybug")
        codegraph_available = _module_available("tree_sitter")
    try:
        from pm import store

        target = store.current_target()
    except Exception:  # health: allow BLE001 -- optional PM store probe; status must work without PM
        target = None
    return graph_available, codegraph_available, target
