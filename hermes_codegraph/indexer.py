"""Per-file incremental indexing for the code graph.

Parsing happens outside the write transaction; graph updates are grouped into
batches of ``CodeGraphLimits.batch_size`` files, one transaction per batch.
A parse failure keeps the previous successful symbols and marks the file
``stale``; it never empties the graph.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from hermes_graph.serialization import derived_id, new_node_id

from . import schema as code_schema
from .adapters.languages.base import ExtractedFile as ParsedFile
from .adapters.languages.base import ExtractedReference, ExtractedSymbol
from .adapters.languages.extractor import extract as extract_file
from .config import CodeGraphLimits
from .exceptions import (
    CodeGraphError,
    IndexLimitError,
)
from .languages import LanguageDescriptor, LanguageRegistry
from .scanner import (
    RepositoryInfo,
    ScanResult,
    ScannedFile,
    content_hash,
    is_binary,
    read_source,
    scan,
)

logger = logging.getLogger(__name__)

_STATUS_CONFIDENCE = {
    "resolved": (0.8, "status_mapping"),
    "inferred": (0.5, "status_mapping"),
    "ambiguous": (0.3, "status_mapping"),
    "unresolved": (0.0, "status_mapping"),
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _Prepared:
    scanned: ScannedFile
    descriptor: LanguageDescriptor
    data: bytes | None
    data_hash: str | None
    line_count: int
    action: str
    parsed: ParsedFile | None = None
    error: str | None = None
    previous_hash: str | None = None
    skip_reason: str | None = None


@dataclass
class _Stats:
    files_total: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    files_deleted: int = 0
    symbols_written: int = 0
    parse_failures: list[dict[str, str]] = field(default_factory=list)


class Indexer:
    def __init__(
        self,
        client: Any,
        repository: RepositoryInfo,
        limits: CodeGraphLimits,
        registry: LanguageRegistry,
    ):
        self.client = client
        self.repo = repository
        self.limits = limits
        self.registry = registry
        self.run_key = f"run_{new_node_id()}"
        self.run_node_id = derived_id("CodeAnalysisRun", self.run_key)
        self.revision_key = repository.revision_key
        self.revision_node_id = derived_id("CodeRevision", self.revision_key)
        self.revision_id = (
            f"{repository.commit_hash or 'nogit'}"
            + ("@dirty" if repository.is_dirty else "")
        )
        self._stats = _Stats()
        self._hash_by_path: dict[str, str] = {}
        self._new_symbols: dict[str, dict[str, Any]] = {}
        self._deleted_symbols: list[dict[str, Any]] = []
        self._modified_symbols: list[tuple[dict[str, Any], str, str]] = []
        self._moved_symbols: list[dict[str, Any]] = []

    # ── entry point ──────────────────────────────────────────────────────
    def run(self, *, full: bool = False, semantic: bool = False) -> dict[str, Any]:
        use_semantic = bool(semantic or self.limits.semantic)
        from .scanner import resolve_repository

        self.repo = resolve_repository(self.repo.root)
        self.revision_key = self.repo.revision_key
        self.revision_node_id = derived_id("CodeRevision", self.revision_key)
        self.revision_id = (
            f"{self.repo.commit_hash or 'nogit'}"
            + ("@dirty" if self.repo.is_dirty else "")
        )
        code_schema.register(self.client)
        self._ensure_repository()
        self._ensure_revision()
        self._create_run()
        started = time.monotonic()
        scan_result = scan(
            self.repo,
            self.registry,
            max_files=self.limits.max_files,
            max_total_bytes=self.limits.max_total_bytes,
            max_file_bytes=self.limits.max_file_bytes,
            extra_excludes=self.limits.exclude,
        )
        self._stats.files_total = len(scan_result.files)
        try:
            self._index_files(scan_result, full=full)
            failed = scan_result.limit_error
            if failed is None:
                self._delete_missing(scan_result)
            else:
                raise failed
            self._apply_moves()
            resolver_result = self._resolve()
            semantic_result = (
                self._enhance_semantic()
                if use_semantic
                else {"status": "disabled"}
            )
            snapshot_hash = self._finalize(scan_result)
            elapsed = round(time.monotonic() - started, 3)
            return {
                "status": "complete",
                "repository_id": self.repo.repository_id,
                "revision_id": self.revision_id,
                "snapshot_hash": snapshot_hash,
                "analysis_run_id": self.run_key,
                "elapsed_seconds": elapsed,
                "resolver": resolver_result,
                "semantic": semantic_result,
                **_stats_payload(self._stats),
            }
        except IndexLimitError as exc:
            self._mark_failed(exc)
            return {
                "status": "partial",
                "repository_id": self.repo.repository_id,
                "revision_id": self.revision_id,
                "snapshot_hash": None,
                "analysis_run_id": self.run_key,
                "error": exc.to_dict(),
                **_stats_payload(self._stats),
            }
        except BaseException as exc:
            self._mark_failed(exc)
            raise

    def _apply_moves(self) -> None:
        """Reuse the old ``symbol_id`` when a symbol moved across files uniquely.

        Identity is only merged on an exact ``(kind, language, signature,
        content_hash)`` match with exactly one disappeared and one new symbol;
        ambiguity keeps both identities (D4).
        """
        if not self._deleted_symbols or not self._new_symbols:
            return
        def group(items: list[dict[str, Any]]) -> dict[tuple[str, str, str, str], list[dict[str, Any]]]:
            grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
            for item in items:
                key = (
                    str(item.get("kind") or ""),
                    str(item.get("language") or ""),
                    str(item.get("signature") or ""),
                    str(item.get("content_hash") or ""),
                )
                if all(key):
                    grouped.setdefault(key, []).append(item)
            return grouped

        deleted = group(self._deleted_symbols)
        added = group(list(self._new_symbols.values()))
        moves: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for key, old_items in deleted.items():
            new_items = added.get(key) or []
            if len(old_items) != 1 or len(new_items) != 1:
                continue
            old, new = old_items[0], new_items[0]
            if old.get("file_path") == new.get("file_path"):
                continue
            moves.append((old, new))
        if not moves:
            return
        edge_types = [edge.name for edge in code_schema.EDGE_TYPES]
        with self.client.transaction.begin() as tx:
            for old, new in moves:
                old_key = str(old.get("symbol_id") or "")
                new_key = str(new.get("symbol_id") or "")
                if not old_key or not new_key:
                    continue
                old_id = derived_id("CodeSymbol", old_key)
                new_id = derived_id("CodeSymbol", new_key)
                for edge_type in edge_types:
                    for edge in tx.edges.list(
                        source_id=new_id, type=edge_type, limit=1000
                    ).items:
                        tx.edges.upsert(
                            old_id, edge.target_id, edge_type,
                            properties=dict(edge.properties),
                        )
                        tx.edges.delete(edge.id)
                    for edge in tx.edges.list(
                        target_id=new_id, type=edge_type, limit=1000
                    ).items:
                        tx.edges.upsert(
                            edge.source_id, old_id, edge_type,
                            properties=dict(edge.properties),
                        )
                        tx.edges.delete(edge.id)
                try:
                    tx.nodes.delete(new_id, hard=True)
                except Exception:
                    logger.debug("moved symbol cleanup failed", exc_info=True)
                props = dict(new)
                props["symbol_id"] = old_key
                props["symbol_status"] = "active"
                props["before_file_id"] = str(old.get("file_path") or "")
                tx.nodes.upsert("CodeSymbol", old_key, props)
                self._moved_symbols.append(
                    {
                        "symbol_id": old_key,
                        "before_file": old.get("file_path"),
                        "after_file": new.get("file_path"),
                        "before_hash": old.get("content_hash"),
                        "after_hash": new.get("content_hash"),
                    }
                )
        self._new_symbols = {
            key: value for key, value in self._new_symbols.items()
            if key not in {str(new.get("symbol_id")) for _old, new in moves}
        }

    def _enhance_semantic(self) -> dict[str, Any]:
        from .semantic import enhance

        try:
            return enhance(
                self.client,
                self.repo,
                run_key=self.run_key,
                revision_id=self.revision_id,
            )
        except Exception as exc:
            logger.debug("semantic enhancement failed", exc_info=True)
            return {"status": "failed", "reason": str(exc)[:500]}

    def _resolve(self) -> dict[str, int]:
        from .resolver import Resolver

        return Resolver(
            self.client,
            self.repo,
            self.registry,
            run_key=self.run_key,
            revision_key=self.revision_key,
            revision_id=self.revision_id,
        ).resolve()

    # ── run lifecycle ────────────────────────────────────────────────────
    def _ensure_repository(self) -> None:
        existing = self.client.nodes.get(derived_id("CodeRepository", self.repo.key))
        props = dict(existing.properties) if existing is not None else {}
        props.update(
            {
                "repository_id": self.repo.repository_id,
                "name": self.repo.name,
                "remote_url": self.repo.remote_url or "",
                "schema_version": code_schema.CODEGRAPH_SCHEMA_VERSION,
                "index_status": props.get("index_status", "indexing"),
                "indexed_at": props.get("indexed_at", ""),
                "commit_hash": props.get("commit_hash", ""),
                "is_git": self.repo.is_git,
                "root": str(self.repo.root),
            }
        )
        self.client.nodes.upsert("CodeRepository", self.repo.key, props)

    def _ensure_revision(self) -> None:
        existing = self.client.nodes.get(self.revision_node_id)
        props = dict(existing.properties) if existing is not None else {}
        props.update(
            {
                "repository_id": self.repo.repository_id,
                "commit_hash": self.repo.commit_hash,
                "branch": self.repo.branch,
                "is_dirty": self.repo.is_dirty,
                "status": props.get("status", "indexing"),
                "snapshot_hash": props.get("snapshot_hash", ""),
                "indexed_at": props.get("indexed_at", ""),
                "revision_key": self.revision_key,
            }
        )
        self.client.nodes.upsert("CodeRevision", self.revision_key, props)

    def _create_run(self) -> None:
        self.started_at = _now_iso()
        self._write_run(status="running")

    def _run_props(self, **values: Any) -> dict[str, Any]:
        return {
            "repository_id": self.repo.repository_id,
            "kind": values.get("kind", "index"),
            "status": values.get("status", "running"),
            "started_at": values.get("started_at", self.started_at),
            "finished_at": values.get("finished_at", ""),
            "target_revision": self.revision_key,
            "files_total": int(values.get("files_total", 0)),
            "files_indexed": int(values.get("files_indexed", 0)),
            "files_skipped": int(values.get("files_skipped", 0)),
            "files_failed": int(values.get("files_failed", 0)),
            "snapshot_hash": values.get("snapshot_hash", ""),
            "error": values.get("error", ""),
        }

    def _write_run(self, **values: Any) -> None:
        self.client.nodes.upsert(
            "CodeAnalysisRun", self.run_key, self._run_props(**values)
        )

    def _write_run_tx(self, tx: Any, **values: Any) -> None:
        tx.nodes.upsert("CodeAnalysisRun", self.run_key, self._run_props(**values))

    def _mark_failed(self, exc: BaseException) -> None:
        try:
            self._write_run(
                status="failed",
                files_total=self._stats.files_total,
                files_indexed=self._stats.files_indexed,
                files_skipped=self._stats.files_skipped,
                files_failed=self._stats.files_failed,
                finished_at=_now_iso(),
                error=str(exc)[:2000],
            )
            self._upsert_repository_props(index_status="partial")
        except Exception:
            logger.debug("failed to record code index failure", exc_info=True)

    def _upsert_repository_props(self, **updates: Any) -> None:
        node = self.client.nodes.get(derived_id("CodeRepository", self.repo.key))
        props = dict(node.properties) if node is not None else {}
        props.update(updates)
        props.setdefault("repository_id", self.repo.repository_id)
        props.setdefault("schema_version", code_schema.CODEGRAPH_SCHEMA_VERSION)
        props.setdefault("name", self.repo.name)
        props.setdefault("remote_url", self.repo.remote_url or "")
        props.setdefault("is_git", self.repo.is_git)
        self.client.nodes.upsert("CodeRepository", self.repo.key, props)

    # ── parsing (outside the write transaction) ──────────────────────────
    def _index_files(self, scan_result: ScanResult, *, full: bool) -> None:
        batch: list[_Prepared] = []
        for scanned in scan_result.files:
            batch.append(self._prepare(scanned, full=full))
            if len(batch) >= self.limits.batch_size:
                self._flush(batch)
                batch = []
        if batch:
            self._flush(batch)

    def _prepare(self, scanned: ScannedFile, *, full: bool) -> _Prepared:
        descriptor = self.registry.get(scanned.language)
        data = read_source(scanned.path, max_bytes=self.limits.max_file_bytes)
        if data is None:
            return _Prepared(
                scanned=scanned,
                descriptor=descriptor,
                data=None,
                data_hash=None,
                line_count=0,
                action="failed",
                error="file exceeded max_file_bytes while indexing",
            )
        digest = content_hash(data)
        line_count = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        previous = self.client.nodes.get(
            derived_id("CodeFile", self._file_key(scanned.rel_path))
        )
        previous_props = previous.properties if previous is not None else {}
        previous_hash = previous_props.get("indexed_hash") or None
        self._hash_by_path[scanned.rel_path] = previous_hash or ""
        versions_match = (
            previous_props.get("parser_version") == descriptor.parser_version
            and previous_props.get("grammar_version") == descriptor.grammar_version
            and (previous_props.get("semantic_provider_version") or "")
            == (descriptor.semantic_provider or "")
        )
        if (
            not full
            and previous is not None
            and previous_props.get("index_status") == "complete"
            and previous_props.get("parse_status") in ("success", "partial")
            and previous_props.get("content_hash") == digest
            and previous_props.get("indexed_hash") == digest
            and versions_match
        ):
            return _Prepared(
                scanned=scanned,
                descriptor=descriptor,
                data=data,
                data_hash=digest,
                line_count=line_count,
                action="skip",
                previous_hash=digest,
            )
        if is_binary(data):
            return _Prepared(
                scanned=scanned,
                descriptor=descriptor,
                data=data,
                data_hash=digest,
                line_count=line_count,
                action="unsupported",
                error="binary content",
                previous_hash=previous_hash,
            )
        if not descriptor.extraction:
            return _Prepared(
                scanned=scanned,
                descriptor=descriptor,
                data=data,
                data_hash=digest,
                line_count=line_count,
                action="unsupported",
                error=(
                    f"language {scanned.language!r} has no extraction profile; "
                    "add one to the descriptor's extraction block"
                ),
                previous_hash=previous_hash,
            )
        started = time.monotonic()
        try:
            parsed = extract_file(data, descriptor, scanned.rel_path)
        except Exception as exc:  # parser failures are data, not run failures
            logger.debug("parse failed for %s", scanned.rel_path, exc_info=True)
            return _Prepared(
                scanned=scanned,
                descriptor=descriptor,
                data=data,
                data_hash=digest,
                line_count=line_count,
                action="failed",
                error=f"{type(exc).__name__}: {exc}"[:1000],
                previous_hash=previous_hash,
            )
        elapsed = time.monotonic() - started
        if elapsed > self.limits.parse_timeout_s:
            return _Prepared(
                scanned=scanned,
                descriptor=descriptor,
                data=data,
                data_hash=digest,
                line_count=line_count,
                action="failed",
                error=f"parse exceeded {self.limits.parse_timeout_s}s ({elapsed:.1f}s)",
                previous_hash=previous_hash,
            )
        return _Prepared(
            scanned=scanned,
            descriptor=descriptor,
            data=data,
            data_hash=digest,
            line_count=line_count,
            action="index",
            parsed=parsed,
            previous_hash=previous_hash,
        )

    # ── batch write ──────────────────────────────────────────────────────
    def _flush(self, batch: list[_Prepared]) -> None:
        try:
            with self.client.transaction.begin() as tx:
                for prepared in batch:
                    if prepared.action == "skip":
                        self._write_skip(tx, prepared)
                    elif prepared.action == "failed":
                        self._write_failed(tx, prepared)
                    elif prepared.action == "unsupported":
                        self._write_unsupported(tx, prepared)
                    else:
                        self._write_indexed(tx, prepared)
        except Exception as exc:
            cause = exc.__cause__
            if isinstance(cause, CodeGraphError):
                raise cause from None
            raise
        batch.clear()

    def _write_skip(self, tx: Any, prepared: _Prepared) -> None:
        file_id = derived_id("CodeFile", self._file_key(prepared.scanned.rel_path))
        node = tx.nodes.get(file_id)
        props = dict(node.properties) if node is not None else {}
        props.update(
            {
                "indexed_revision": self.revision_key,
                "content_hash": prepared.data_hash,
                "indexed_hash": prepared.data_hash,
                "index_status": "complete",
                "size_bytes": prepared.scanned.size_bytes,
                "line_count": prepared.line_count,
            }
        )
        tx.nodes.upsert("CodeFile", self._file_key(prepared.scanned.rel_path), props)
        self._stats.files_skipped += 1
        self._hash_by_path[prepared.scanned.rel_path] = prepared.data_hash or ""

    def _write_failed(self, tx: Any, prepared: _Prepared) -> None:
        key = self._file_key(prepared.scanned.rel_path)
        node = tx.nodes.get(derived_id("CodeFile", key))
        props = dict(node.properties) if node is not None else {}
        props.update(
            {
                "repository_id": self.repo.repository_id,
                "path": prepared.scanned.rel_path,
                "language": prepared.scanned.language,
                "content_hash": prepared.data_hash or "",
                "parse_status": "failed",
                "index_status": "stale",
                "size_bytes": prepared.scanned.size_bytes,
                "line_count": prepared.line_count,
                "parser_version": prepared.descriptor.parser_version,
                "grammar_version": prepared.descriptor.grammar_version,
                "semantic_provider_version": prepared.descriptor.semantic_provider or "",
            }
        )
        if "indexed_hash" not in props:
            props["indexed_hash"] = ""
        tx.nodes.upsert("CodeFile", key, props)
        self._write_diagnostic(
            tx,
            prepared.scanned.rel_path,
            1,
            1,
            "parse_failed",
            "error",
            prepared.error or "parse failed",
        )
        self._stats.files_failed += 1
        self._stats.parse_failures.append(
            {"path": prepared.scanned.rel_path, "error": prepared.error or ""}
        )
        self._hash_by_path[prepared.scanned.rel_path] = props.get("indexed_hash") or ""

    def _write_unsupported(self, tx: Any, prepared: _Prepared) -> None:
        previous = self._previous_symbols(tx, prepared.scanned.rel_path)
        self._remove_file_graph(tx, prepared.scanned.rel_path, previous)
        props = {
            "repository_id": self.repo.repository_id,
            "path": prepared.scanned.rel_path,
            "language": prepared.scanned.language,
            "content_hash": prepared.data_hash or "",
            "indexed_hash": prepared.data_hash or "",
            "index_status": "complete",
            "parse_status": "unsupported",
            "parser_version": prepared.descriptor.parser_version,
            "grammar_version": prepared.descriptor.grammar_version,
            "semantic_provider_version": prepared.descriptor.semantic_provider or "",
            "indexed_revision": self.revision_key,
            "size_bytes": prepared.scanned.size_bytes,
            "line_count": prepared.line_count,
            "symbol_count": 0,
            "imports": [],
            "type_relations": [],
            "tests": [],
            "call_sites": [],
            "exports": [],
            "reexports": [],
        }
        tx.nodes.upsert("CodeFile", self._file_key(prepared.scanned.rel_path), props)
        self._write_diagnostic(
            tx,
            prepared.scanned.rel_path,
            1,
            1,
            "unsupported_content",
            "warning",
            prepared.error or "unsupported content",
        )
        self._stats.files_skipped += 1
        self._hash_by_path[prepared.scanned.rel_path] = prepared.data_hash or ""

    def _write_indexed(self, tx: Any, prepared: _Prepared) -> None:
        parsed = prepared.parsed
        assert parsed is not None
        if (
            self._stats.symbols_written + len(parsed.symbols)
            > self.limits.max_symbols
        ):
            raise IndexLimitError(
                f"repository exceeds {self.limits.max_symbols} symbols",
                details={"max_symbols": self.limits.max_symbols},
            )
        rel_path = prepared.scanned.rel_path
        file_key = self._file_key(rel_path)
        module_key = self._module_key(parsed.module_name, parsed.language)
        module_id = derived_id("CodeModule", module_key)
        file_id = derived_id("CodeFile", file_key)
        repository_id = derived_id("CodeRepository", self.repo.key)

        previous_symbols = self._previous_symbols(tx, rel_path)
        self._remove_file_graph(tx, rel_path, previous_symbols)

        symbol_ids: dict[str, str] = {}
        symbol_node_ids: dict[str, str] = {}
        previous_by_identity: dict[tuple[str, str], Any] = {}
        for node in previous_symbols:
            props = node.properties
            previous_by_identity[
                (props.get("kind", ""), props.get("qualified_name", ""))
            ] = node
        for symbol in parsed.symbols:
            key = (symbol.kind, symbol.qualified_name)
            previous = previous_by_identity.get(key)
            symbol_ids[symbol.qualified_name] = (
                previous.key if previous is not None and previous.key
                else f"sym_{new_node_id()}"
            )
            symbol_node_ids[symbol.qualified_name] = derived_id(
                "CodeSymbol", symbol_ids[symbol.qualified_name]
            )
            if previous is not None and previous.key:
                previous_hash = previous.properties.get("content_hash")
                if previous_hash and previous_hash != symbol.content_hash:
                    self._modified_symbols.append(
                        (dict(previous.properties), previous_hash, symbol.content_hash)
                    )

        module_props = {
            "repository_id": self.repo.repository_id,
            "language": parsed.language,
            "qualified_name": parsed.module_name,
            "path": rel_path,
        }
        tx.nodes.upsert("CodeModule", module_key, module_props)

        file_props = {
            "repository_id": self.repo.repository_id,
            "path": rel_path,
            "language": parsed.language,
            "module_name": parsed.module_name,
            "content_hash": prepared.data_hash or "",
            "indexed_hash": prepared.data_hash or "",
            "index_status": "complete",
            "parse_status": parsed.parse_status,
            "parser_version": prepared.descriptor.parser_version,
            "grammar_version": prepared.descriptor.grammar_version,
            "semantic_provider_version": prepared.descriptor.semantic_provider or "",
            "indexed_revision": self.revision_key,
            "size_bytes": prepared.scanned.size_bytes,
            "line_count": prepared.line_count,
            "symbol_count": len(parsed.symbols),
            "imports": _serialize_imports(parsed.imports),
            "type_relations": _serialize_relations(parsed.type_relations),
            "tests": _serialize_tests(parsed.tests),
            "call_sites": _serialize_references(parsed.references),
            "exports": list(parsed.exports),
            "reexports": list(parsed.reexports),
        }
        tx.nodes.upsert("CodeFile", file_key, file_props)

        evidence_ids: dict[str, str] = {}
        observed = _now_iso()
        reused_ids = {
            node.key for node in previous_symbols if node.key
        }
        for symbol in parsed.symbols:
            symbol_id = symbol_ids[symbol.qualified_name]
            symbol_props = {
                "repository_id": self.repo.repository_id,
                "symbol_id": symbol_id,
                "kind": symbol.kind,
                "name": symbol.name,
                "qualified_name": symbol.qualified_name,
                "language": parsed.language,
                "file_path": rel_path,
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
                "start_column": symbol.start_column,
                "end_column": symbol.end_column,
                "content_hash": symbol.content_hash,
                "symbol_status": "active",
                "container_symbol": symbol.container,
                "is_exported": symbol.is_exported,
                "signature": symbol.signature,
                "language_kind": symbol.language_kind,
                "parser_version": prepared.descriptor.parser_version,
                "file_content_hash": prepared.data_hash or "",
                "file_indexed_hash": prepared.data_hash or "",
                "file_index_status": "complete",
            }
            tx.nodes.upsert("CodeSymbol", symbol_id, symbol_props)
            if symbol_id not in reused_ids:
                self._new_symbols[symbol_id] = dict(symbol_props)
            evidence_id = self._upsert_evidence(
                tx,
                rel_path,
                "definition",
                symbol.start_line,
                symbol.start_column,
                symbol.end_line,
                symbol.end_column,
                symbol.content_hash,
                observed,
                parsed.language,
                prepared,
            )
            evidence_ids[symbol.qualified_name] = evidence_id

        file_evidence = self._upsert_evidence(
            tx,
            rel_path,
            "definition",
            1,
            1,
            max(prepared.line_count, 1),
            1,
            prepared.data_hash or "",
            observed,
            parsed.language,
            prepared,
        )

        module_symbol_evidence = evidence_ids.get(parsed.module_name, file_evidence)
        self._edge(
            tx, repository_id, file_id, "CODE_CONTAINS", [file_evidence],
            "resolved", "tree_sitter",
        )
        self._edge(
            tx, file_id, module_id, "CODE_DECLARES", [module_symbol_evidence],
            "resolved", "tree_sitter",
        )
        for symbol in parsed.symbols:
            symbol_id = symbol_ids[symbol.qualified_name]
            symbol_node_id = symbol_node_ids[symbol.qualified_name]
            definition_evidence = evidence_ids[symbol.qualified_name]
            self._edge(
                tx, file_id, symbol_node_id, "CODE_DEFINES", [definition_evidence],
                "resolved", "tree_sitter",
            )
            self._edge(
                tx, symbol_node_id, module_id, "CODE_BELONGS_TO", [definition_evidence],
                "resolved", "tree_sitter",
            )
            if symbol.container:
                parent_id = symbol_node_ids.get(symbol.container)
                if parent_id:
                    self._edge(
                        tx, parent_id, symbol_node_id, "CODE_CONTAINS_SYMBOL",
                        [definition_evidence], "resolved", "tree_sitter",
                    )

        for reference in parsed.references:
            source_id = symbol_node_ids.get(reference.source_qualified_name)
            target_id = symbol_node_ids.get(reference.target_qualified_name)
            if source_id is None or target_id is None:
                continue
            evidence_id = self._upsert_reference_evidence(
                tx, rel_path, reference, observed, parsed.language, prepared
            )
            self._edge(
                tx, source_id, target_id, "CODE_REFERENCES", [evidence_id],
                "inferred", reference.resolution_method,
            )

        new_ids = set(symbol_node_ids.values())
        for node in previous_symbols:
            if node.id in new_ids:
                continue
            props = dict(node.properties)
            props["symbol_status"] = "deleted"
            self._deleted_symbols.append(dict(props))
            tx.nodes.upsert("CodeSymbol", node.key or node.id, props)

        for diagnostic in parsed.diagnostics:
            self._write_diagnostic(
                tx, rel_path, diagnostic.line, diagnostic.column,
                diagnostic.code, diagnostic.severity, diagnostic.message,
            )

        self._stats.files_indexed += 1
        self._stats.symbols_written += len(parsed.symbols)
        self._hash_by_path[rel_path] = prepared.data_hash or ""

    # ── cleanup ──────────────────────────────────────────────────────────
    def _previous_symbols(self, tx: Any, rel_path: str) -> list[Any]:
        file_id = derived_id("CodeFile", self._file_key(rel_path))
        nodes: list[Any] = []
        try:
            edges = tx.edges.list(
                source_id=file_id, type="CODE_DEFINES", limit=1000
            )
        except Exception:
            logger.debug("could not list previous symbols for %s", rel_path, exc_info=True)
            return nodes
        for edge in edges.items:
            node = tx.nodes.get(edge.target_id)
            if node is not None:
                nodes.append(node)
        return nodes

    def _remove_file_graph(
        self, tx: Any, rel_path: str, previous_symbols: list[Any]
    ) -> None:
        file_id = derived_id("CodeFile", self._file_key(rel_path))
        for edge_type in code_schema.FILE_SOURCE_EDGES:
            self._delete_edges(tx, source_id=file_id, edge_type=edge_type)
        for node in previous_symbols:
            for edge_type in code_schema.SYMBOL_SOURCE_EDGES:
                self._delete_edges(tx, source_id=node.id, edge_type=edge_type)
            for edge_type in code_schema.SYMBOL_TARGET_EDGES:
                self._delete_edges(tx, target_id=node.id, edge_type=edge_type)
        self._delete_file_evidence(tx, rel_path)

    def _delete_edges(
        self, tx: Any, *, source_id: str | None = None,
        target_id: str | None = None, edge_type: str,
    ) -> None:
        try:
            result = tx.edges.list(
                source_id=source_id, target_id=target_id, type=edge_type, limit=1000
            )
        except Exception:
            logger.debug("could not list %s edges for cleanup", edge_type, exc_info=True)
            return
        for edge in result.items:
            try:
                tx.edges.delete(edge.id)
            except Exception:
                logger.debug("edge delete failed", exc_info=True)

    def _delete_file_evidence(self, tx: Any, rel_path: str) -> None:
        try:
            result = tx.nodes.search(
                "CodeEvidence",
                {"repository_id": self.repo.repository_id, "file_path": rel_path},
                limit=1000,
            )
        except Exception:
            logger.debug("evidence search failed for %s", rel_path, exc_info=True)
            return
        for node in result.items:
            try:
                tx.nodes.delete(node.id)
            except Exception:
                logger.debug("evidence delete failed", exc_info=True)

    def _delete_missing(self, scan_result: ScanResult) -> None:
        present = {item.rel_path for item in scan_result.files}
        present.update(
            item["path"] for item in scan_result.skipped
            if not str(item.get("reason", "")).startswith("stat_failed")
        )
        missing = [path for path in self._all_files() if path not in present]
        if not missing:
            return
        for start in range(0, len(missing), self.limits.batch_size):
            chunk = missing[start:start + self.limits.batch_size]
            with self.client.transaction.begin() as tx:
                for rel_path in chunk:
                    previous = self._previous_symbols(tx, rel_path)
                    self._remove_file_graph(tx, rel_path, previous)
                    for node in previous:
                        props = dict(node.properties)
                        props["symbol_status"] = "deleted"
                        self._deleted_symbols.append(dict(props))
                        tx.nodes.upsert("CodeSymbol", node.key or node.id, props)
                    file_id = derived_id("CodeFile", self._file_key(rel_path))
                    try:
                        tx.nodes.delete(file_id)
                    except Exception:
                        logger.debug("file delete failed for %s", rel_path, exc_info=True)
                    self._stats.files_deleted += 1

    def _all_files(self) -> list[str]:
        paths: list[str] = []
        after = ""
        while True:
            result = self.client.query.cypher(
                "MATCH (f:CodeFile) WHERE f.repository_id=$repo "
                "AND f.deleted_at IS NULL AND f.path>$after "
                "RETURN f.path AS path ORDER BY f.path LIMIT 100",
                {"repo": self.repo.repository_id, "after": after},
                max_rows=100,
            )
            rows = [row for row in result.items if isinstance(row, dict)]
            if not rows:
                break
            paths.extend(str(row["path"]) for row in rows)
            after = str(rows[-1]["path"])
            if len(rows) < 100:
                break
        return paths

    # ── finalization ─────────────────────────────────────────────────────
    def _finalize(self, scan_result: ScanResult) -> str:
        snapshot_hash = self._snapshot_hash(scan_result)
        finished = _now_iso()
        with self.client.transaction.begin() as tx:
            repo_node = tx.nodes.get(derived_id("CodeRepository", self.repo.key))
            repo_props = dict(repo_node.properties) if repo_node is not None else {}
            repo_props.update(
                {
                    "repository_id": self.repo.repository_id,
                    "name": self.repo.name,
                    "remote_url": self.repo.remote_url or "",
                    "schema_version": code_schema.CODEGRAPH_SCHEMA_VERSION,
                    "index_status": "complete",
                    "snapshot_hash": snapshot_hash,
                    "indexed_at": finished,
                    "commit_hash": self.repo.commit_hash,
                    "is_git": self.repo.is_git,
                    "root": str(self.repo.root),
                }
            )
            tx.nodes.upsert("CodeRepository", self.repo.key, repo_props)
            revision = tx.nodes.get(self.revision_node_id)
            revision_props = dict(revision.properties) if revision is not None else {}
            revision_props.update(
                {
                    "repository_id": self.repo.repository_id,
                    "commit_hash": self.repo.commit_hash,
                    "branch": self.repo.branch,
                    "is_dirty": self.repo.is_dirty,
                    "status": "complete",
                    "snapshot_hash": snapshot_hash,
                    "indexed_at": finished,
                    "revision_key": self.revision_key,
                }
            )
            tx.nodes.upsert("CodeRevision", self.revision_key, revision_props)
            self._write_change_graph(tx)
            for rel_path in self._hash_by_path:
                key = self._file_key(rel_path)
                node = tx.nodes.get(derived_id("CodeFile", key))
                if node is None:
                    continue
                props = dict(node.properties)
                props["indexed_revision"] = self.revision_key
                tx.nodes.upsert("CodeFile", key, props)
            self._write_run_tx(
                tx,
                status="completed",
                files_total=self._stats.files_total,
                files_indexed=self._stats.files_indexed,
                files_skipped=self._stats.files_skipped,
                files_failed=self._stats.files_failed,
                snapshot_hash=snapshot_hash,
                finished_at=finished,
                error="",
            )
        return snapshot_hash

    def _write_change_graph(self, tx: Any) -> None:
        """Write PARENT_OF/CHANGED_IN/DELETED_IN for clean revisions only.

        A dirty working tree only updates the current graph; writing change
        edges there would fabricate history from every save.
        """
        if self.repo.is_dirty:
            return
        current = derived_id("CodeRevision", self.revision_key)
        parent = self._previous_clean_revision()
        if parent is not None:
            tx.edges.upsert(
                current, parent.id, "CODE_PARENT_OF",
                properties={"repository_id": self.repo.repository_id},
            )
        moved_old_keys = {
            str(item.get("symbol_id")) for item in self._moved_symbols
        }
        for symbol_id, props in self._new_symbols.items():
            tx.edges.upsert(
                derived_id("CodeSymbol", symbol_id), current, "CODE_CHANGED_IN",
                properties={
                    "change_kind": "added",
                    "after_hash": props.get("content_hash") or "",
                    "after_file_id": props.get("file_path") or "",
                },
            )
        for previous, before_hash, after_hash in self._modified_symbols:
            symbol_id = str(previous.get("symbol_id") or "")
            if not symbol_id:
                continue
            tx.edges.upsert(
                derived_id("CodeSymbol", symbol_id), current, "CODE_CHANGED_IN",
                properties={
                    "change_kind": "modified",
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "before_file_id": previous.get("file_path") or "",
                    "after_file_id": previous.get("file_path") or "",
                },
            )
        for moved in self._moved_symbols:
            tx.edges.upsert(
                derived_id("CodeSymbol", moved["symbol_id"]), current,
                "CODE_CHANGED_IN",
                properties={
                    "change_kind": "moved",
                    "before_hash": moved.get("before_hash") or "",
                    "after_hash": moved.get("after_hash") or "",
                    "before_file_id": moved.get("before_file") or "",
                    "after_file_id": moved.get("after_file") or "",
                },
            )
        for previous in self._deleted_symbols:
            symbol_id = str(previous.get("symbol_id") or "")
            if not symbol_id or symbol_id in moved_old_keys:
                continue
            tx.edges.upsert(
                derived_id("CodeSymbol", symbol_id), current, "CODE_DELETED_IN",
                properties={
                    "before_hash": previous.get("content_hash") or "",
                    "before_file_id": previous.get("file_path") or "",
                },
            )

    def _previous_clean_revision(self) -> Any | None:
        try:
            candidates = self.client.nodes.search(
                "CodeRevision",
                {"repository_id": self.repo.repository_id, "is_dirty": False},
                limit=1000,
            ).items
        except Exception:
            logger.debug("previous revision lookup failed", exc_info=True)
            return None
        complete = [
            node for node in candidates
            if node.properties.get("status") == "complete"
            and node.key != self.revision_key
        ]
        complete.sort(
            key=lambda node: str(node.properties.get("indexed_at") or ""),
            reverse=True,
        )
        return complete[0] if complete else None

    def _snapshot_hash(self, scan_result: ScanResult) -> str:
        import hashlib

        lines = [
            f"{path}\x00{digest}"
            for path, digest in sorted(self._hash_by_path.items())
            if digest
        ]
        return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

    # ── primitives ───────────────────────────────────────────────────────
    def _file_key(self, rel_path: str) -> str:
        return f"{self.repo.key}@{rel_path}"

    def _module_key(self, module_name: str, language: str = "") -> str:
        return f"{self.repo.key}@{language}:{module_name}"

    def _edge(
        self,
        tx: Any,
        source_id: str,
        target_id: str,
        edge_type: str,
        evidence_ids: list[str],
        status: str,
        method: str,
    ) -> None:
        confidence, confidence_method = _STATUS_CONFIDENCE[status]
        tx.edges.upsert(
            source_id,
            target_id,
            edge_type,
            properties={
                "evidence_ids": list(evidence_ids),
                "resolution_status": status,
                "confidence": confidence,
                "resolution_method": method,
                "confidence_method": confidence_method,
            },
        )

    def _evidence_key(
        self,
        rel_path: str,
        kind: str,
        start_line: int,
        start_column: int,
        end_line: int,
        end_column: int,
        excerpt_hash: str,
    ) -> str:
        return (
            f"{self.repo.key}@{rel_path}#{start_line}:{start_column}-"
            f"{end_line}:{end_column}:{kind}:tree-sitter:{excerpt_hash}"
        )

    def _upsert_evidence(
        self,
        tx: Any,
        rel_path: str,
        kind: str,
        start_line: int,
        start_column: int,
        end_line: int,
        end_column: int,
        excerpt_hash: str,
        observed_at: str,
        language: str,
        prepared: _Prepared,
    ) -> str:
        key = self._evidence_key(
            rel_path, kind, start_line, start_column, end_line, end_column,
            excerpt_hash,
        )
        props = {
            "repository_id": self.repo.repository_id,
            "evidence_kind": kind,
            "source_tool": "tree-sitter",
            "source_tool_version": prepared.descriptor.grammar_version,
            "language": language,
            "file_path": rel_path,
            "start_line": start_line,
            "end_line": end_line,
            "start_column": start_column,
            "end_column": end_column,
            "excerpt_hash": excerpt_hash,
            "analysis_run_id": self.run_key,
            "revision_id": self.revision_id,
            "observed_at": observed_at,
        }
        tx.nodes.upsert("CodeEvidence", key, props)
        return derived_id("CodeEvidence", key)

    def _upsert_reference_evidence(
        self,
        tx: Any,
        rel_path: str,
        reference: ExtractedReference,
        observed_at: str,
        language: str,
        prepared: _Prepared,
    ) -> str:
        key = self._evidence_key(
            rel_path,
            reference.evidence_kind,
            reference.start_line,
            reference.start_column,
            reference.end_line,
            reference.end_column,
            reference.excerpt_hash,
        )
        props = {
            "repository_id": self.repo.repository_id,
            "evidence_kind": reference.evidence_kind,
            "source_tool": "tree-sitter",
            "source_tool_version": prepared.descriptor.grammar_version,
            "language": language,
            "file_path": rel_path,
            "start_line": reference.start_line,
            "end_line": reference.end_line,
            "start_column": reference.start_column,
            "end_column": reference.end_column,
            "excerpt_hash": reference.excerpt_hash,
            "analysis_run_id": self.run_key,
            "revision_id": self.revision_id,
            "observed_at": observed_at,
        }
        tx.nodes.upsert("CodeEvidence", key, props)
        return derived_id("CodeEvidence", key)

    def _write_diagnostic(
        self,
        tx: Any,
        rel_path: str,
        line: int,
        column: int,
        code: str,
        severity: str,
        message: str,
    ) -> None:
        key = f"{self.repo.key}@{rel_path}#{line}:{column}:{code}:tree-sitter"
        tx.nodes.upsert(
            "CodeDiagnostic",
            key,
            {
                "repository_id": self.repo.repository_id,
                "file_path": rel_path,
                "line_number": line,
                "column_number": column,
                "code": code,
                "tool": "tree-sitter",
                "severity": severity,
                "message": message[:2000],
            },
        )


def _serialize_imports(imports: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "module": item.module,
            "imported_names": list(item.imported_names),
            "alias": item.alias,
            "is_relative": item.is_relative,
            "is_reexport": item.is_reexport,
            "level": item.level,
            "start_line": item.start_line,
            "end_line": item.end_line,
            "start_column": item.start_column,
            "end_column": item.end_column,
            "excerpt_hash": item.excerpt_hash,
        }
        for item in imports
    ]


def _serialize_relations(relations: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "source_qualified_name": item.source_qualified_name,
            "target_name": item.target_name,
            "relation": item.relation,
            "start_line": item.start_line,
            "end_line": item.end_line,
            "start_column": item.start_column,
            "end_column": item.end_column,
            "excerpt_hash": item.excerpt_hash,
        }
        for item in relations
    ]


def _serialize_tests(tests: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "qualified_name": item.qualified_name,
            "name": item.name,
            "framework": item.framework,
            "target_names": list(item.target_names),
            "start_line": item.start_line,
            "end_line": item.end_line,
            "start_column": item.start_column,
            "end_column": item.end_column,
            "excerpt_hash": item.excerpt_hash,
        }
        for item in tests
    ]


def _serialize_references(references: list[Any]) -> list[dict[str, Any]]:
    # Call sites are the resolver's input; cap one file's payload so a single
    # huge generated file cannot exceed the graph property limit.
    limit = 5000
    return [
        {
            "source_qualified_name": item.source_qualified_name,
            "target_qualified_name": item.target_qualified_name,
            "target_name": item.target_name,
            "relation": item.relation,
            "is_instantiation": item.is_instantiation,
            "evidence_kind": item.evidence_kind,
            "resolution_method": item.resolution_method,
            "start_line": item.start_line,
            "end_line": item.end_line,
            "start_column": item.start_column,
            "end_column": item.end_column,
            "excerpt_hash": item.excerpt_hash,
        }
        for item in references[:limit]
    ]


def _stats_payload(stats: _Stats) -> dict[str, Any]:
    return {
        "files_total": stats.files_total,
        "files_indexed": stats.files_indexed,
        "files_skipped": stats.files_skipped,
        "files_failed": stats.files_failed,
        "files_deleted": stats.files_deleted,
        "symbols_written": stats.symbols_written,
        "parse_failures": list(stats.parse_failures),
    }
