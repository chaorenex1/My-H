"""Evidence-constrained graph updates (ADR-8 D15).

``refresh_file`` is the authoritative single-file rebuild; ``apply_evidence``
only materializes facts observed by an analysis tool, with optimistic
concurrency and excerpt-hash validation; ``invalidate`` retires evidence or an
edge. Every failure is zero-partial-write because each action runs in one
transaction.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_graph.serialization import derived_id, new_node_id

from . import schema as code_schema
from .config import CodeGraphLimits
from .exceptions import (
    CodeGraphError,
    EvidenceRejectedError,
    GraphConflictError,
)
from .languages import LanguageRegistry
from .scanner import RepositoryInfo, ScannedFile

logger = logging.getLogger(__name__)

ALLOWED_FACT_KINDS = frozenset(
    {"definition", "reference", "call", "type", "import", "test", "diagnostic"}
)
RELATION_BY_KIND = {
    "reference": "CODE_REFERENCES",
    "call": "CODE_CALLS",
    "definition": "CODE_DEFINES",
    "import": "CODE_IMPORTS",
    "test": "CODE_TESTS",
}
TYPE_RELATIONS = {
    "inherits": "CODE_INHERITS",
    "implements": "CODE_IMPLEMENTS",
    "overrides": "CODE_OVERRIDES",
    "has_type": "CODE_HAS_TYPE",
    "returns_type": "CODE_RETURNS_TYPE",
}
STATUS_PRIORITY = {"verified": 4, "resolved": 3, "inferred": 2, "ambiguous": 1}
MAX_FACTS = 500


def refresh_file(
    client: Any,
    repository: RepositoryInfo,
    registry: LanguageRegistry,
    limits: CodeGraphLimits,
    path: str,
    *,
    semantic: bool = False,
    semgrep: bool = False,
) -> dict[str, Any]:
    from .indexer import Indexer
    rel_path = _validated_path(repository, path)
    descriptor = registry.by_path(rel_path)
    if descriptor is None:
        raise CodeGraphError(
            f"{rel_path!r} has no language descriptor",
            details={"path": rel_path},
        )
    absolute = (repository.root / rel_path).resolve()
    try:
        size = absolute.stat().st_size
    except OSError as exc:
        raise CodeGraphError(f"cannot read {rel_path}: {exc}") from exc
    if size > limits.max_file_bytes:
        raise CodeGraphError("file exceeds max_file_bytes")
    indexer = Indexer(client, repository, limits, registry)
    indexer._ensure_repository()
    indexer._ensure_revision()
    indexer._create_run()
    scanned = ScannedFile(
        rel_path=rel_path,
        path=absolute,
        size_bytes=size,
        language=descriptor.language,
    )
    prepared = indexer._prepare(scanned, full=True)
    indexer._flush([prepared])
    indexer._apply_moves()
    indexer._resolve()
    if semantic:
        indexer._enhance_semantic()
    semgrep_result = None
    if semgrep:
        if limits.semgrep_enabled and limits.enabled:
            from .scanner import ScanResult

            scan_result = ScanResult(files=[scanned])
            # ADR-9 D8: the explicit ``semgrep=true`` query returns what the scan just observed on
            # the current bytes, not a graph lookup that may describe an older revision.
            semgrep_result = indexer._run_semgrep(
                scan_result, _snapshot_hash(client, repository), include_findings=True)
        else:
            semgrep_result = {"status": "disabled", "findings": 0}
    snapshot = _mark_patched(client, repository)
    indexer._write_run(
        kind="refresh_file",
        status="completed",
        files_total=1,
        files_indexed=1 if prepared.action == "index" else 0,
        files_failed=1 if prepared.action == "failed" else 0,
        snapshot_hash=snapshot,
        finished_at=datetime.now(UTC).isoformat(),
    )
    payload = {
        "status": "patched",
        "path": rel_path,
        "action": prepared.action,
        "snapshot_hash": snapshot,
        "analysis_run_id": indexer.run_key,
    }
    if semgrep_result is not None:
        payload["semgrep"] = semgrep_result
    return payload


def delete_file(
    client: Any,
    repository: RepositoryInfo,
    registry: LanguageRegistry,
    limits: CodeGraphLimits,
    path: str,
) -> dict[str, Any]:
    """Delete one file's graph facts, keeping symbol nodes for the Change Graph."""
    from .indexer import Indexer

    rel_path = _validated_path(repository, path)
    indexer = Indexer(client, repository, limits, registry)
    indexer._ensure_repository()
    indexer._ensure_revision()
    with client.transaction.begin() as tx:
        previous = indexer._previous_symbols(tx, rel_path)
        indexer._remove_file_graph(tx, rel_path, previous)
        for node in previous:
            props = dict(node.properties)
            props["symbol_status"] = "deleted"
            tx.nodes.upsert("CodeSymbol", node.key or node.id, props)
        file_id = derived_id("CodeFile", indexer._file_key(rel_path))
        try:
            tx.nodes.delete(file_id, hard=True)
        except Exception:
            logger.debug("file delete failed for %s", rel_path, exc_info=True)
    snapshot = _mark_patched(client, repository)
    return {"status": "deleted", "path": rel_path, "snapshot_hash": snapshot}


def apply_evidence(
    client: Any,
    repository: RepositoryInfo,
    registry: LanguageRegistry,
    limits: CodeGraphLimits,
    path: str,
    facts: list[dict[str, Any]],
    *,
    expected_revision_id: str,
    expected_content_hash: str,
) -> dict[str, Any]:
    rel_path = _validated_path(repository, path)
    if not isinstance(facts, list) or not facts:
        raise EvidenceRejectedError("facts must be a non-empty list")
    if len(facts) > MAX_FACTS:
        raise EvidenceRejectedError(f"at most {MAX_FACTS} facts per call")
    file_key = f"{repository.key}@{rel_path}"
    file_node = client.nodes.get(derived_id("CodeFile", file_key))
    if file_node is None:
        raise GraphConflictError(
            "file is not indexed; run refresh_file first",
            details={"path": rel_path},
        )
    props = dict(file_node.properties)
    if expected_revision_id not in (repository.revision_key, props.get("indexed_revision")):
        raise GraphConflictError(
            "expected_revision_id does not match the indexed revision",
            details={
                "expected": expected_revision_id,
                "current": props.get("indexed_revision"),
            },
        )
    if expected_content_hash != props.get("content_hash"):
        raise GraphConflictError(
            "expected_content_hash does not match the indexed file",
            details={"expected": expected_content_hash, "current": props.get("content_hash")},
        )
    if props.get("content_hash") != props.get("indexed_hash"):
        raise EvidenceRejectedError(
            "file content differs from its last successful parse; run refresh_file",
            details={"path": rel_path},
        )
    if props.get("index_status") not in ("complete", "patched"):
        raise EvidenceRejectedError(
            "apply_evidence needs a complete or patched file",
            details={"index_status": props.get("index_status")},
        )
    absolute = (repository.root / rel_path).resolve()
    data = absolute.read_bytes()
    run_key = f"run_{new_node_id()}"
    observed = datetime.now(UTC).isoformat()
    updated = 0
    conflicts: list[dict[str, Any]] = []
    removed = 0
    with client.transaction.begin() as tx:
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                raise EvidenceRejectedError(f"fact {index} must be an object")
            kind = str(fact.get("kind") or "")
            if kind not in ALLOWED_FACT_KINDS:
                raise EvidenceRejectedError(f"fact {index}: unknown kind {kind!r}")
            evidence = fact.get("evidence")
            if not isinstance(evidence, dict):
                raise EvidenceRejectedError(f"fact {index}: evidence is required")
            source_tool = str(evidence.get("source_tool") or "")
            if not source_tool or source_tool == "model_observation":
                raise EvidenceRejectedError(
                    f"fact {index}: source_tool must name an analysis tool"
                )
            span = evidence.get("span") or {}
            start_line = int(span.get("start_line") or 0)
            start_column = int(span.get("start_column") or 1)
            end_line = int(span.get("end_line") or start_line)
            end_column = int(span.get("end_column") or 1)
            if start_line < 1 or end_line < start_line:
                raise EvidenceRejectedError(f"fact {index}: span is outside the file")
            excerpt_hash = str(evidence.get("excerpt_hash") or "")
            if not _span_matches(data, start_line, start_column, end_line, end_column, excerpt_hash):
                raise EvidenceRejectedError(
                    f"fact {index}: excerpt_hash does not match the current file span"
                )
            evidence_key = (
                f"{repository.key}@{rel_path}#{start_line}:{start_column}-"
                f"{end_line}:{end_column}:{kind}:{source_tool}:{excerpt_hash}"
            )
            tx.nodes.upsert(
                "CodeEvidence",
                evidence_key,
                {
                    "repository_id": repository.repository_id,
                    "evidence_kind": _evidence_kind(kind),
                    "source_tool": source_tool,
                    "source_tool_version": str(evidence.get("source_tool_version") or ""),
                    "language": props.get("language") or "",
                    "file_path": rel_path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "start_column": start_column,
                    "end_column": end_column,
                    "excerpt_hash": excerpt_hash,
                    "analysis_run_id": run_key,
                    "revision_id": repository.revision_key,
                    "observed_at": str(evidence.get("observed_at") or observed),
                },
            )
            evidence_id = derived_id("CodeEvidence", evidence_key)
            if kind == "diagnostic":
                tx.nodes.upsert(
                    "CodeDiagnostic",
                    f"{repository.key}@{rel_path}#{start_line}:{start_column}:"
                    f"{fact.get('rule_id') or 'analysis'}:{source_tool}",
                    {
                        "repository_id": repository.repository_id,
                        "file_path": rel_path,
                        "line_number": start_line,
                        "column_number": start_column,
                        "code": str(fact.get("rule_id") or ""),
                        "tool": source_tool,
                        "severity": str(fact.get("severity") or "warning"),
                        "message": str(fact.get("message") or "")[:2000],
                        "revision_id": repository.revision_key,
                    },
                )
                updated += 1
                continue
            edge_type = _edge_type(fact, kind)
            definition = next(
                (edge for edge in code_schema.EDGE_TYPES if edge.name == edge_type),
                None,
            )
            if definition is None:
                raise EvidenceRejectedError(f"unknown edge type {edge_type!r}")
            source_id = _fact_node_id(
                tx, definition.source, str(fact.get("source_id") or "")
            )
            target_id = _fact_node_id(
                tx, definition.target, str(fact.get("target_id") or "")
            )
            if tx.nodes.get(source_id) is None or tx.nodes.get(target_id) is None:
                raise EvidenceRejectedError(
                    f"fact {index}: source/target nodes must already exist; "
                    "run refresh_file for the target file first"
                )
            status = str(fact.get("resolution_status") or "resolved")
            if status not in ("inferred", "resolved"):
                raise EvidenceRejectedError(
                    f"fact {index}: resolution_status must be inferred or resolved"
                )
            existing_edges = tx.edges.list(
                source_id=source_id, target_id=target_id, type=edge_type, limit=10
            ).items
            previous = (
                str(existing_edges[0].properties.get("resolution_status") or "")
                if existing_edges else ""
            )
            if previous and STATUS_PRIORITY.get(previous, 0) > STATUS_PRIORITY.get(status, 0):
                conflicts.append(
                    {
                        "source_id": fact.get("source_id"),
                        "target_id": fact.get("target_id"),
                        "edge_type": edge_type,
                        "previous_status": previous,
                        "incoming_status": status,
                    }
                )
                _set_edge_status(tx, existing_edges[0], "ambiguous")
                continue
            tx.edges.upsert(
                source_id, target_id, edge_type,
                properties={
                    "evidence_ids": [evidence_id],
                    "resolution_status": status,
                    "confidence": 0.8 if status == "resolved" else 0.5,
                    "resolution_method": source_tool,
                    "confidence_method": "status_mapping",
                },
            )
            updated += 1
        _set_file_patched(tx, file_key, props, rel_path)
        tx.nodes.upsert(
            "CodeAnalysisRun",
            run_key,
            {
                "repository_id": repository.repository_id,
                "kind": "model_update",
                "status": "completed" if not conflicts else "conflict",
                "started_at": observed,
                "finished_at": datetime.now(UTC).isoformat(),
                "target_revision": repository.revision_key,
                "snapshot_hash": _snapshot_hash(client, repository),
                "files_total": 1,
                "files_indexed": 1,
                "files_skipped": 0,
                "files_failed": 0,
                "error": "",
                "facts": len(facts),
                "conflicts": len(conflicts),
            },
        )
    snapshot = _mark_patched(client, repository)
    return {
        "status": "patched",
        "path": rel_path,
        "updated": updated,
        "removed": removed,
        "conflicts": conflicts,
        "snapshot_hash": snapshot,
        "analysis_run_id": run_key,
    }


def invalidate(
    client: Any,
    repository: RepositoryInfo,
    *,
    ids: list[str],
    reason: str = "",
) -> dict[str, Any]:
    if not isinstance(ids, list) or not ids:
        raise EvidenceRejectedError("ids must be a non-empty list")
    invalidated = 0
    removed = 0
    with client.transaction.begin() as tx:
        for item in ids:
            edge = tx.edges.get(str(item))
            if edge is not None:
                properties = dict(edge.properties)
                properties["resolution_status"] = "invalidated"
                properties["invalidation_reason"] = reason[:500]
                tx.edges.upsert(edge.source_id, edge.target_id, edge.type, properties=properties)
                if not _has_valid_evidence(tx, properties):
                    tx.edges.delete(edge.id)
                    removed += 1
                else:
                    invalidated += 1
                continue
            node = tx.nodes.get(str(item))
            if node is None:
                continue
            props = dict(node.properties)
            props["resolution_status"] = "invalidated"
            props["invalidation_reason"] = reason[:500]
            tx.nodes.upsert(node.type, node.key or node.id, props)
            invalidated += 1
            for edge in _edges_with_evidence(tx, repository.repository_id, str(item)):
                edge_props = dict(edge.properties)
                remaining = [
                    evidence_id for evidence_id in edge_props.get("evidence_ids") or []
                    if evidence_id != str(item)
                    and _evidence_valid(tx, evidence_id)
                ]
                if remaining:
                    edge_props["evidence_ids"] = remaining
                    tx.edges.upsert(
                        edge.source_id, edge.target_id, edge.type, properties=edge_props
                    )
                else:
                    tx.edges.delete(edge.id)
                    removed += 1
    snapshot = _mark_patched(client, repository)
    return {
        "status": "patched",
        "invalidated": invalidated,
        "removed": removed,
        "snapshot_hash": snapshot,
    }


# ── helpers ──────────────────────────────────────────────────────────────
def _validated_path(repository: RepositoryInfo, path: str) -> str:
    if not path:
        raise CodeGraphError("path is required")
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(repository.root.resolve())
        except ValueError as exc:
            raise CodeGraphError("path escapes the repository root") from exc
    rel = str(candidate).replace("\\", "/")
    from .scanner import _excluded_path

    if _excluded_path(rel, ()):
        raise CodeGraphError("path is excluded from code indexing", details={"path": rel})
    absolute = (repository.root / rel).resolve()
    try:
        absolute.relative_to(repository.root.resolve())
    except ValueError as exc:
        raise CodeGraphError("path escapes the repository root") from exc
    return rel


def _evidence_kind(kind: str) -> str:
    return {
        "definition": "definition",
        "reference": "reference_expression",
        "call": "call_expression",
        "type": "type_reference",
        "import": "import",
        "test": "test_annotation",
        "diagnostic": "diagnostic",
    }.get(kind, "reference_expression")


def _edge_type(fact: dict[str, Any], kind: str) -> str:
    if kind == "type":
        relation = str(fact.get("relation") or "has_type")
        if relation not in TYPE_RELATIONS:
            raise EvidenceRejectedError(f"unknown type relation {relation!r}")
        return TYPE_RELATIONS[relation]
    relation = str(fact.get("relation") or "")
    if relation in TYPE_RELATIONS:
        return TYPE_RELATIONS[relation]
    return RELATION_BY_KIND.get(kind, "CODE_REFERENCES")


def _fact_node_id(tx: Any, node_type: str, value: str) -> str:
    if not value:
        raise EvidenceRejectedError(f"{node_type} id is required")
    from hermes_graph.exceptions import NotFoundError, ValidationError

    try:
        node = tx.nodes.get(value)
    except (ValidationError, NotFoundError):
        node = None
    if node is not None:
        if node.type != node_type:
            raise EvidenceRejectedError(
                f"{value!r} is {node.type}, expected {node_type}"
            )
        return node.id
    if node_type == "CodeSymbol":
        return derived_id("CodeSymbol", value)
    raise EvidenceRejectedError(
        f"{value!r} is not a {node_type} node id; pass a graph node id for this fact kind"
    )


def _span_matches(
    data: bytes,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
    expected: str,
) -> bool:
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start_line > len(lines) or end_line > len(lines):
        return False
    if start_line == end_line:
        fragment = lines[start_line - 1][max(start_column - 1, 0): max(end_column - 1, 0)]
    else:
        parts = [lines[start_line - 1][max(start_column - 1, 0):]]
        parts.extend(lines[start_line:end_line - 1])
        parts.append(lines[end_line - 1][: max(end_column - 1, 0)])
        fragment = "\n".join(parts)
    return hashlib.sha256(fragment.encode("utf-8")).hexdigest() == expected


def _edges_with_evidence(tx: Any, repository_id: str, evidence_id: str) -> list[Any]:
    found: list[Any] = []
    for definition in code_schema.EDGE_TYPES:
        rows = tx.query.cypher(
            f"MATCH (a)-[r:{definition.name}]->(b) "
            "WHERE a.repository_id=$repo RETURN r.id AS id",
            {"repo": repository_id},
            max_rows=100,
        ).items
        for row in rows:
            edge = tx.edges.get(row["id"])
            if edge is not None and evidence_id in (edge.properties.get("evidence_ids") or []):
                found.append(edge)
    return found


def _evidence_valid(tx: Any, evidence_id: str) -> bool:
    node = tx.nodes.get(evidence_id)
    if node is None:
        return False
    return node.properties.get("resolution_status") != "invalidated"


def _set_file_patched(
    tx: Any, file_key: str, props: dict[str, Any], rel_path: str
) -> None:
    updated = dict(props)
    updated["index_status"] = "patched"
    updated["path"] = rel_path
    tx.nodes.upsert("CodeFile", file_key, updated)
    repository_id = str(updated.get("repository_id") or "")
    if not repository_id:
        return
    symbols = tx.nodes.search(
        "CodeSymbol",
        {"repository_id": repository_id, "file_path": rel_path},
        limit=1000,
    ).items
    for symbol in symbols:
        symbol_props = dict(symbol.properties)
        symbol_props["file_content_hash"] = updated.get("content_hash") or ""
        symbol_props["file_indexed_hash"] = updated.get("indexed_hash") or ""
        symbol_props["file_index_status"] = "patched"
        tx.nodes.upsert("CodeSymbol", symbol.key or symbol.id, symbol_props)


def _set_edge_status(tx: Any, edge: Any, status: str) -> None:
    properties = dict(edge.properties)
    properties["resolution_status"] = status
    properties["confidence"] = 0.3
    tx.edges.upsert(edge.source_id, edge.target_id, edge.type, properties=properties)


def _has_valid_evidence(tx: Any, properties: dict[str, Any]) -> bool:
    ids = properties.get("evidence_ids") or []
    for evidence_id in ids:
        node = tx.nodes.get(evidence_id)
        if node is not None and node.properties.get("resolution_status") != "invalidated":
            return True
    return False


def _snapshot_hash(client: Any, repository: RepositoryInfo) -> str:
    lines: list[str] = []
    after = ""
    while True:
        rows = client.query.cypher(
            "MATCH (f:CodeFile) WHERE f.repository_id=$repo "
            "AND f.deleted_at IS NULL AND f.path>$after "
            "RETURN f.path AS path, f.indexed_hash AS hash ORDER BY f.path",
            {"repo": repository.repository_id, "after": after},
            max_rows=100,
        ).items
        if not rows:
            break
        for row in rows:
            lines.append(f"{row.get('path')}\x00{row.get('hash') or ''}")
        last = str(rows[-1].get("path") or "")
        if not last or last == after or len(rows) < 100:
            break
        after = last
    repo_node = client.nodes.get(derived_id("CodeRepository", repository.key))
    patch_seq = int((repo_node.properties.get("patch_seq") if repo_node else 0) or 0)
    return hashlib.sha256(
        ("\n".join(sorted(lines)) + f"\npatch:{patch_seq}").encode("utf-8")
    ).hexdigest()


def _mark_patched(client: Any, repository: RepositoryInfo) -> str:
    repo_node = client.nodes.get(derived_id("CodeRepository", repository.key))
    props = dict(repo_node.properties) if repo_node is not None else {}
    patch_seq = int(props.get("patch_seq") or 0) + 1
    props.update(
        {
            "repository_id": repository.repository_id,
            "schema_version": code_schema.CODEGRAPH_SCHEMA_VERSION,
            "index_status": "patched",
            "patch_seq": patch_seq,
            "indexed_at": datetime.now(UTC).isoformat(),
        }
    )
    client.nodes.upsert("CodeRepository", repository.key, props)
    snapshot = _snapshot_hash(client, repository)
    props["snapshot_hash"] = snapshot
    client.nodes.upsert("CodeRepository", repository.key, props)
    return snapshot
