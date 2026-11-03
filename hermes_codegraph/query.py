"""Read-only query service and status payloads."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from hermes_graph.serialization import derived_id, new_node_id

from .exceptions import CodeGraphError
from .models import (
    SCHEMA_VERSION,
    CodeQueryMetadata,
    CodeQueryResult,
)
from .scanner import RepositoryInfo

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _registry_report(registry: Any) -> dict[str, Any] | None:
    report = getattr(registry, "report", None)
    return report.to_dict() if hasattr(report, "to_dict") else None


def status_payload(
    client: Any, repository: RepositoryInfo, registry: Any
) -> dict[str, Any]:
    node = client.nodes.get(derived_id("CodeRepository", repository.key))
    if node is None:
        return {
            "status": "unindexed",
            "repository_id": repository.repository_id,
            "repository": repository.to_dict(),
            "index_status": "missing",
            "snapshot_hash": None,
            "freshness": _freshness(repository, {}),
            "files": 0,
            "symbols": 0,
            "evidence": 0,
            "edges": 0,
            "languages": registry.supported_languages(),
            "language_registry": _registry_report(registry),
        }
    props = dict(node.properties)
    revision_id = f"{repository.commit_hash or 'nogit'}" + (
        "@dirty" if repository.is_dirty else ""
    )
    files = _count(
        client,
        "MATCH (f:CodeFile) WHERE f.repository_id=$repo "
        "AND f.deleted_at IS NULL RETURN count(*) AS count",
        {"repo": repository.repository_id},
    )
    symbols = _count(
        client,
        "MATCH (s:CodeSymbol) WHERE s.repository_id=$repo "
        "AND (s.symbol_status IS NULL OR s.symbol_status <> 'deleted') "
        "RETURN count(*) AS count",
        {"repo": repository.repository_id},
    )
    evidence = _count(
        client,
        "MATCH (e:CodeEvidence) WHERE e.repository_id=$repo RETURN count(*) AS count",
        {"repo": repository.repository_id},
    )
    stale = _count(
        client,
        "MATCH (f:CodeFile) WHERE f.repository_id=$repo "
        "AND f.deleted_at IS NULL "
        "AND f.index_status IN ['stale', 'partial', 'indexing'] "
        "RETURN count(*) AS count",
        {"repo": repository.repository_id},
    )
    failed = _count(
        client,
        "MATCH (f:CodeFile) WHERE f.repository_id=$repo "
        "AND f.deleted_at IS NULL "
        "AND f.parse_status = 'failed' RETURN count(*) AS count",
        {"repo": repository.repository_id},
    )
    deleted_symbols = _count(
        client,
        "MATCH (s:CodeSymbol) WHERE s.repository_id=$repo "
        "AND s.symbol_status = 'deleted' RETURN count(*) AS count",
        {"repo": repository.repository_id},
    )
    edges = _count_edges(client, repository.repository_id)
    try:
        db_bytes = client.path.stat().st_size
    except OSError:
        db_bytes = 0
    return {
        "status": "ok" if props.get("index_status") in ("complete", "patched") else "partial",
        "repository_id": repository.repository_id,
        "repository": repository.to_dict(),
        "schema_version": props.get("schema_version"),
        "index_status": props.get("index_status", "missing"),
        "snapshot_hash": props.get("snapshot_hash") or None,
        "indexed_at": props.get("indexed_at") or None,
        "revision_id": revision_id,
        "revision_key": repository.revision_key,
        "commit_hash": props.get("commit_hash") or repository.commit_hash,
        "dirty": repository.is_dirty,
        "freshness": _freshness(repository, props),
        "files": files,
        "symbols": symbols,
        "deleted_symbols": deleted_symbols,
        "evidence": evidence,
        "edges": edges,
        "stale_files": stale,
        "failed_files": failed,
        "db_bytes": db_bytes,
        "languages": registry.supported_languages(),
        "language_registry": _registry_report(registry),
    }


def doctor_payload(
    client: Any | None,
    repository: RepositoryInfo,
    registry: Any,
    *,
    graph_available: bool,
    codegraph_available: bool,
    target: str | None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = None) -> None:
        item: dict[str, Any] = {"name": name, "ok": bool(ok)}
        if detail is not None:
            item["detail"] = detail
        checks.append(item)

    check("graph_extra", graph_available, "pm.available('graph')")
    check("codegraph_extra", codegraph_available, "pm.available('codegraph')")
    check("platform_target", True, target)
    try:
        from .analysis import semgrep_available

        available = semgrep_available()
        check(
            "semgrep_available",
            True,
            {
                "available": available,
                "install_hint": None if available else "hermes pm install semgrep",
                "note": "optional; only needed for diagnostics",
            },
        )
    except Exception as exc:
        logger.debug("semgrep availability probe failed", exc_info=True)
        check("semgrep_available", True, {"error": str(exc)[:300]})
    descriptors = registry.descriptors()
    report = _registry_report(registry)
    check(
        "language_registry",
        bool(descriptors),
        {"languages": [item.language for item in descriptors], "merge": report},
    )
    if report is not None:
        check(
            "language_profile_registry",
            report.get("profile_error") is None,
            {
                "profile_path": report.get("profile_path"),
                "profile_error": report.get("profile_error"),
                "profile_skipped": report.get("profile_skipped"),
                "profile_conflicts": report.get("profile_conflicts"),
                "conflicts": report.get("conflicts"),
            },
        )
    if client is not None:
        from . import schema as code_schema

        missing = code_schema.missing_types(client)
        check("schema_registered", not missing, {"missing": missing})
        try:
            status = status_payload(client, repository, registry)
            check(
                "index_present",
                status["index_status"] != "missing",
                {
                    "index_status": status["index_status"],
                    "snapshot_hash": status["snapshot_hash"],
                },
            )
            check(
                "no_stale_files",
                status["stale_files"] == 0 and status["failed_files"] == 0,
                {
                    "stale_files": status["stale_files"],
                    "failed_files": status["failed_files"],
                },
            )
            if status["index_status"] != "missing":
                node = client.nodes.get(derived_id("CodeRepository", repository.key))
                props = node.properties if node is not None else {}
                check(
                    "schema_version",
                    props.get("schema_version") == code_schema.CODEGRAPH_SCHEMA_VERSION,
                    {
                        "expected": code_schema.CODEGRAPH_SCHEMA_VERSION,
                        "found": props.get("schema_version"),
                    },
                )
                check(
                    "freshness",
                    status["freshness"] == "exact",
                    status["freshness"],
                )
        except Exception as exc:
            logger.debug("code doctor could not read the index", exc_info=True)
            check("index_readable", False, f"{type(exc).__name__}: {exc}")
    return {
        "status": "ok" if all(item["ok"] for item in checks) else "issues",
        "repository_id": repository.repository_id,
        "checks": checks,
    }


def find_symbols(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    *,
    name: str | None = None,
    kind: str | None = None,
    language: str | None = None,
    limit: int = 20,
) -> CodeQueryResult:
    limit = max(1, min(int(limit), 200))
    filters: dict[str, Any] = {"repository_id": repository.repository_id}
    if name is not None:
        filters["name"] = name
    if kind is not None:
        filters["kind"] = kind
    if language is not None:
        filters["language"] = language
    result = client.nodes.search("CodeSymbol", filters, limit=limit)
    items = [
        _symbol_payload(node)
        for node in result.items
        if node.properties.get("symbol_status") != "deleted"
    ]
    return _envelope(
        client,
        repository,
        registry,
        results=items,
        total=len(items),
        truncated=bool(result.has_more),
        policy="balanced",
        limits={"limit": limit, "max_results": 200},
    )


def get_symbol(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    symbol_id: str,
) -> CodeQueryResult:
    node = client.nodes.get(derived_id("CodeSymbol", symbol_id))
    results = (
        [_symbol_payload(node)]
        if node is not None and node.properties.get("symbol_status") != "deleted"
        else []
    )
    return _envelope(
        client,
        repository,
        registry,
        results=results,
        total=len(results),
        truncated=False,
        policy="balanced",
        limits={"max_results": 1},
    )


def get_definition(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    symbol_id: str,
) -> CodeQueryResult:
    node = client.nodes.get(derived_id("CodeSymbol", symbol_id))
    results: list[dict[str, Any]] = []
    if node is not None:
        edges = client.edges.list(
            target_id=node.id, type="CODE_DEFINES", limit=20
        )
        for edge in edges.items:
            file_node = client.nodes.get(edge.source_id)
            if file_node is not None:
                results.append(_file_payload(file_node))
    return _envelope(
        client,
        repository,
        registry,
        results=results,
        total=len(results),
        truncated=False,
        policy="balanced",
        limits={"max_results": 20},
    )


def get_references(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    symbol_id: str,
    *,
    limit: int = 50,
    policy: str = "balanced",
) -> CodeQueryResult:
    limit = max(1, min(int(limit), 200))
    node = client.nodes.get(derived_id("CodeSymbol", symbol_id))
    results: list[dict[str, Any]] = []
    if node is not None:
        edges = client.edges.list(
            target_id=node.id, type="CODE_REFERENCES", limit=limit
        )
        for edge in edges.items:
            if not _policy_allows(edge.properties.get("resolution_status"), policy):
                continue
            source = client.nodes.get(edge.source_id)
            payload = _symbol_payload(source) if source is not None else {}
            payload["edge"] = {
                "resolution_status": edge.properties.get("resolution_status"),
                "confidence": edge.properties.get("confidence"),
                "evidence_ids": edge.properties.get("evidence_ids", []),
            }
            results.append(payload)
    return _envelope(
        client,
        repository,
        registry,
        results=results,
        total=len(results),
        truncated=bool(node is not None and len(results) >= limit),
        policy=policy,
        limits={"limit": limit, "max_results": 200},
    )


def get_callers(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    symbol_id: str,
    *,
    depth: int = 2,
    policy: str = "strict",
    limit: int = 200,
) -> CodeQueryResult:
    return _related(
        client, repository, registry, symbol_id,
        edge_types=("CODE_CALLS", "CODE_INSTANTIATES"),
        direction="in", depth=depth, policy=policy, limit=limit,
    )


def get_callees(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    symbol_id: str,
    *,
    depth: int = 2,
    policy: str = "balanced",
    limit: int = 200,
) -> CodeQueryResult:
    return _related(
        client, repository, registry, symbol_id,
        edge_types=("CODE_CALLS", "CODE_INSTANTIATES"),
        direction="out", depth=depth, policy=policy, limit=limit,
    )


def get_call_path(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    source_id: str,
    target_id: str,
    *,
    max_depth: int = 5,
    policy: str = "balanced",
) -> CodeQueryResult:
    start = client.nodes.get(derived_id("CodeSymbol", source_id))
    goal = client.nodes.get(derived_id("CodeSymbol", target_id))
    if start is None or goal is None:
        return _envelope(
            client, repository, registry, results=[], total=0, truncated=False,
            policy=policy, limits={"max_depth": _depth(max_depth), "max_results": 200},
        )
    max_depth = _depth(max_depth)
    queue: list[tuple[Any, list[dict[str, Any]]]] = [(start, [])]
    seen: set[str] = {start.id}
    path: list[dict[str, Any]] | None = None
    while queue and path is None:
        node, trail = queue.pop(0)
        if len(trail) >= max_depth:
            continue
        edges = []
        for edge_type in ("CODE_CALLS", "CODE_INSTANTIATES"):
            edges.extend(
                client.edges.list(
                    source_id=node.id, type=edge_type, limit=500
                ).items
            )
        for edge in edges:
            if not _policy_allows(edge.properties.get("resolution_status"), policy):
                continue
            target = client.nodes.get(edge.target_id)
            if target is None:
                continue
            step = _step_payload(node, edge, target)
            if target.id == goal.id:
                path = [*trail, step]
                break
            if target.id in seen:
                continue
            seen.add(target.id)
            queue.append((target, [*trail, step]))
    results = path or []
    return _envelope(
        client, repository, registry, results=results, total=len(results),
        truncated=False, policy=policy,
        limits={"max_depth": max_depth, "max_results": 200},
    )


def get_type_hierarchy(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    symbol_id: str,
    *,
    direction: str = "both",
    depth: int = 3,
    policy: str = "balanced",
    limit: int = 200,
) -> CodeQueryResult:
    if direction not in ("in", "out", "both"):
        raise CodeGraphError("direction must be 'in', 'out' or 'both'")
    return _related(
        client, repository, registry, symbol_id,
        edge_types=("CODE_INHERITS", "CODE_IMPLEMENTS", "CODE_OVERRIDES"),
        direction=direction, depth=depth, policy=policy, limit=limit,
    )


def get_implementations(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    symbol_id: str,
    *,
    policy: str = "balanced",
    limit: int = 200,
) -> CodeQueryResult:
    return _related(
        client, repository, registry, symbol_id,
        edge_types=("CODE_IMPLEMENTS", "CODE_OVERRIDES"),
        direction="in", depth=1, policy=policy, limit=limit,
    )


def get_dependencies(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    module_id: str,
    *,
    depth: int = 2,
    policy: str = "balanced",
    limit: int = 200,
) -> CodeQueryResult:
    return _related(
        client, repository, registry, module_id,
        edge_types=("CODE_DEPENDS_ON",),
        direction="out", depth=depth, policy=policy, limit=limit,
        id_field="qualified_name",
    )


def get_cycles(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    *,
    scope: str = "modules",
    policy: str = "balanced",
    limit: int = 100,
) -> CodeQueryResult:
    if scope not in ("modules", "types"):
        raise CodeGraphError("scope must be 'modules' or 'types'")
    try:
        cycles = client.algorithms.cycles(
            "code", filters={"repository_id": repository.repository_id}
        )
    except Exception as exc:
        if type(exc).__name__ == "AlgorithmUnavailableError":
            return _envelope(
                client, repository, registry, results=[], total=0,
                truncated=False, policy=policy,
                limits={"limit": limit, "max_results": 200},
                unavailable=str(exc),
            )
        raise
    results: list[dict[str, Any]] = []
    for cycle in cycles:
        members = []
        for node_id in cycle:
            node = client.nodes.get(node_id)
            if node is None:
                continue
            if node.type not in ("CodeModule", "CodeSymbol"):
                continue
            if scope == "modules" and node.type != "CodeModule":
                continue
            if scope == "types" and node.type != "CodeSymbol":
                continue
            members.append(_node_payload(node))
        if len(members) >= 2:
            results.append({"scope": scope, "members": members})
        if len(results) >= limit:
            break
    return _envelope(
        client, repository, registry, results=results, total=len(results),
        truncated=len(cycles) > len(results), policy=policy,
        limits={"limit": limit, "max_results": 200},
    )


def get_impact(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    symbol_id: str,
    *,
    depth: int = 3,
    include: tuple[str, ...] = ("callers", "references", "dependencies", "implementations", "tests"),
    policy: str = "strict",
    limit: int = 200,
) -> CodeQueryResult:
    mapping = {
        "callers": (("CODE_CALLS", "CODE_INSTANTIATES"), "in"),
        "references": (("CODE_REFERENCES",), "in"),
        "dependencies": (("CODE_DEPENDS_ON",), "out"),
        "implementations": (("CODE_IMPLEMENTS", "CODE_OVERRIDES"), "in"),
        "tests": (("CODE_TESTS",), "in"),
    }
    unknown = [item for item in include if item not in mapping]
    if unknown:
        raise CodeGraphError(f"unknown impact include(s): {', '.join(unknown)}")
    results: list[dict[str, Any]] = []
    truncated = False
    seen: set[tuple[str, str]] = set()
    for name in include:
        edge_types, direction = mapping[name]
        if name == "dependencies":
            module_id = _module_for_symbol(client, repository, symbol_id)
            if not module_id:
                continue
            partial = get_dependencies(
                client, repository, registry, module_id,
                depth=depth, policy=policy, limit=limit,
            )
        else:
            partial = _related(
                client, repository, registry, symbol_id, edge_types=edge_types,
                direction=direction, depth=depth, policy=policy, limit=limit,
            )
        truncated = truncated or partial.metadata.truncated
        for item in partial.results:
            key = (name, str(item.get("symbol_id") or item.get("qualified_name")))
            if key in seen:
                continue
            seen.add(key)
            results.append({**item, "impact": name})
    return _envelope(
        client, repository, registry, results=results[:limit],
        total=len(results), truncated=truncated or len(results) > limit,
        policy=policy, limits={"depth": _depth(depth), "limit": limit, "max_results": 200},
    )


def get_tests_for_symbol(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    symbol_id: str,
    *,
    policy: str = "balanced",
    limit: int = 200,
) -> CodeQueryResult:
    return _related(
        client, repository, registry, symbol_id,
        edge_types=("CODE_TESTS",), direction="in", depth=1,
        policy=policy, limit=limit, id_field="qualified_name",
    )


def get_coverage(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    symbol_id: str,
    *,
    limit: int = 200,
) -> CodeQueryResult:
    return _related(
        client, repository, registry, symbol_id,
        edge_types=("CODE_COVERS",), direction="in", depth=1,
        policy="strict", limit=limit, id_field="qualified_name",
    )


def get_diagnostics(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    *,
    path: str | None = None,
    rule: str | None = None,
    severity: str | None = None,
    limit: int = 50,
) -> CodeQueryResult:
    limit = max(1, min(int(limit), 200))
    filters: dict[str, Any] = {"repository_id": repository.repository_id}
    if path is not None:
        filters["file_path"] = path
    if rule is not None:
        filters["code"] = rule
    if severity is not None:
        filters["severity"] = severity
    result = client.nodes.search("CodeDiagnostic", filters, limit=limit)
    items = [_node_payload(node) for node in result.items]
    items.sort(key=lambda item: (item.get("file_path") or "", item.get("line_number") or 0))
    return _envelope(
        client, repository, registry, results=items, total=len(items),
        truncated=bool(result.has_more), policy="balanced",
        limits={"limit": limit, "max_results": 200},
    )


def _related(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    start_id: str,
    *,
    edge_types: tuple[str, ...],
    direction: str,
    depth: int,
    policy: str,
    limit: int,
    id_field: str = "symbol_id",
) -> CodeQueryResult:
    max_depth = _depth(depth)
    limit = max(1, min(int(limit), 200))
    max_nodes = 10_000
    start = _resolve_start(client, repository, start_id)
    if start is None:
        return _envelope(
            client, repository, registry, results=[], total=0, truncated=False,
            policy=policy, limits={"depth": max_depth, "limit": limit, "max_nodes": max_nodes},
        )
    visited = {start.id}
    frontier: list[tuple[Any, int]] = [(start, 0)]
    results: list[dict[str, Any]] = []
    truncated = False
    while frontier:
        node, level = frontier.pop(0)
        if level >= max_depth:
            continue
        for edge_type in edge_types:
            for edge, neighbor in _neighbours(client, node, edge_type, direction):
                if not _policy_allows(edge.properties.get("resolution_status"), policy):
                    continue
                if neighbor.id in visited:
                    continue
                visited.add(neighbor.id)
                if len(visited) > max_nodes:
                    truncated = True
                    break
                payload = _node_payload(neighbor)
                payload["depth"] = level + 1
                payload["edge"] = {
                    "type": edge.type,
                    "resolution_status": edge.properties.get("resolution_status"),
                    "confidence": edge.properties.get("confidence"),
                    "resolution_method": edge.properties.get("resolution_method"),
                    "evidence_ids": edge.properties.get("evidence_ids", []),
                }
                results.append(payload)
                if len(results) >= limit:
                    truncated = True
                    break
                frontier.append((neighbor, level + 1))
            if truncated:
                break
        if truncated:
            break
    return _envelope(
        client, repository, registry, results=results, total=len(results),
        truncated=truncated, policy=policy,
        limits={"depth": max_depth, "limit": limit, "max_nodes": max_nodes},
    )


def _module_for_symbol(
    client: Any, repository: RepositoryInfo, symbol_id: str
) -> str | None:
    symbol = client.nodes.get(derived_id("CodeSymbol", symbol_id))
    if symbol is None:
        return None
    path = str(symbol.properties.get("file_path") or "")
    if not path:
        return None
    file_node = client.nodes.get(
        derived_id("CodeFile", f"{repository.key}@{path}")
    )
    if file_node is None:
        return None
    return str(file_node.properties.get("module_name") or "") or None


def _resolve_start(client: Any, repository: RepositoryInfo, start_id: str) -> Any | None:
    from hermes_graph.exceptions import NotFoundError, ValidationError

    try:
        node = client.nodes.get(start_id)
        if node is not None:
            return node
    except (ValidationError, NotFoundError):
        node = None
    try:
        modules = client.nodes.search(
            "CodeModule",
            {"repository_id": repository.repository_id, "qualified_name": start_id},
            limit=1,
        ).items
        if modules:
            return modules[0]
    except Exception:
        logger.debug("module lookup by qualified name failed", exc_info=True)
    try:
        return client.nodes.get(derived_id("CodeSymbol", start_id))
    except (ValidationError, NotFoundError):
        return None


def _neighbours(
    client: Any, node: Any, edge_type: str, direction: str
) -> list[tuple[Any, Any]]:
    pairs: list[tuple[Any, Any]] = []
    if direction in ("out", "both"):
        for edge in client.edges.list(source_id=node.id, type=edge_type, limit=1000).items:
            neighbor = client.nodes.get(edge.target_id)
            if neighbor is not None:
                pairs.append((edge, neighbor))
    if direction in ("in", "both"):
        for edge in client.edges.list(target_id=node.id, type=edge_type, limit=1000).items:
            neighbor = client.nodes.get(edge.source_id)
            if neighbor is not None:
                pairs.append((edge, neighbor))
    return pairs


def _step_payload(source: Any, edge: Any, target: Any) -> dict[str, Any]:
    return {
        "source": _node_payload(source),
        "target": _node_payload(target),
        "edge": {
            "type": edge.type,
            "resolution_status": edge.properties.get("resolution_status"),
            "confidence": edge.properties.get("confidence"),
        },
    }


def _node_payload(node: Any) -> dict[str, Any]:
    if node.type == "CodeSymbol":
        return _symbol_payload(node)
    if node.type == "CodeFile":
        return _file_payload(node)
    props = dict(node.properties)
    payload = {key: props.get(key) for key in (
        "qualified_name", "name", "language", "path", "kind", "file_path",
        "line_number", "column_number", "code", "tool", "severity", "message",
        "commit_hash", "branch", "is_dirty", "status", "snapshot_hash",
        "start_line", "end_line", "start_column", "end_column", "framework",
        "ecosystem", "version",
    )}
    payload["node_id"] = node.id
    payload["node_type"] = node.type
    return payload


def _depth(value: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 2
    return max(1, min(value, 5))


def get_changes(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    symbol_id: str,
    *,
    from_revision: str | None = None,
    to_revision: str | None = None,
    limit: int = 200,
) -> CodeQueryResult:
    symbol = client.nodes.get(derived_id("CodeSymbol", symbol_id))
    results: list[dict[str, Any]] = []
    if symbol is not None:
        for edge_type in ("CODE_CHANGED_IN", "CODE_DELETED_IN"):
            for edge in client.edges.list(
                source_id=symbol.id, type=edge_type, limit=limit
            ).items:
                revision = client.nodes.get(edge.target_id)
                if revision is None:
                    continue
                payload = {
                    "change_kind": edge.properties.get("change_kind"),
                    "edge_type": edge.type,
                    "revision_key": revision.key,
                    "revision_id": revision.properties.get("revision_key"),
                    "commit_hash": revision.properties.get("commit_hash"),
                    "before_hash": edge.properties.get("before_hash"),
                    "after_hash": edge.properties.get("after_hash"),
                    "before_file_id": edge.properties.get("before_file_id"),
                    "after_file_id": edge.properties.get("after_file_id"),
                }
                if from_revision and not _revision_matches(payload, from_revision, "from"):
                    continue
                if to_revision and not _revision_matches(payload, to_revision, "to"):
                    continue
                results.append(payload)
    results.sort(key=lambda item: (str(item.get("revision_key") or ""), str(item.get("change_kind") or "")))
    return _envelope(
        client, repository, registry, results=results[:limit],
        total=len(results), truncated=len(results) > limit, policy="balanced",
        limits={"limit": limit, "max_results": 200},
    )


def get_revision_changes(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    revision_id: str,
    *,
    limit: int = 500,
) -> CodeQueryResult:
    revision = _find_revision(client, repository, revision_id)
    results: list[dict[str, Any]] = []
    if revision is not None:
        for edge_type in ("CODE_CHANGED_IN", "CODE_DELETED_IN"):
            for edge in client.edges.list(
                target_id=revision.id, type=edge_type, limit=limit
            ).items:
                symbol = client.nodes.get(edge.source_id)
                if symbol is None:
                    continue
                results.append(
                    {
                        "symbol_id": symbol.properties.get("symbol_id") or symbol.key,
                        "qualified_name": symbol.properties.get("qualified_name"),
                        "file_path": symbol.properties.get("file_path"),
                        "change_kind": edge.properties.get("change_kind"),
                        "edge_type": edge.type,
                        "before_hash": edge.properties.get("before_hash"),
                        "after_hash": edge.properties.get("after_hash"),
                    }
                )
    results.sort(key=lambda item: (str(item.get("file_path") or ""), str(item.get("qualified_name") or "")))
    return _envelope(
        client, repository, registry, results=results[:limit],
        total=len(results), truncated=len(results) > limit, policy="balanced",
        limits={"limit": limit, "max_results": 500},
    )


def prune_revisions(
    client: Any,
    repository: RepositoryInfo,
    *,
    keep: int = 50,
    older_than: str | None = None,
) -> dict[str, Any]:
    keep = max(1, int(keep))
    revisions = client.nodes.search(
        "CodeRevision", {"repository_id": repository.repository_id}, limit=1000
    ).items
    clean = [
        node for node in revisions
        if not node.properties.get("is_dirty")
    ]
    clean.sort(
        key=lambda node: str(node.properties.get("indexed_at") or ""),
        reverse=True,
    )
    removed: list[Any] = clean[keep:]
    if older_than:
        cutoff = str(older_than)
        removed = [
            node for node in removed
            if str(node.properties.get("indexed_at") or "") < cutoff
        ]
    for node in revisions:
        if node.properties.get("is_dirty") and node.key != repository.revision_key:
            removed.append(node)
    removed = list({node.id: node for node in removed}.values())
    with client.transaction.begin() as tx:
        for node in removed:
            for edge_type in ("CODE_CHANGED_IN", "CODE_DELETED_IN"):
                for edge in tx.edges.list(target_id=node.id, type=edge_type, limit=1000).items:
                    tx.edges.delete(edge.id)
            for edge in tx.edges.list(source_id=node.id, type="CODE_PARENT_OF", limit=1000).items:
                tx.edges.delete(edge.id)
            for edge in tx.edges.list(target_id=node.id, type="CODE_PARENT_OF", limit=1000).items:
                tx.edges.delete(edge.id)
            try:
                tx.nodes.delete(node.id, hard=True)
            except Exception:
                logger.debug("revision prune failed", exc_info=True)
        deleted_symbols = client.nodes.search(
            "CodeSymbol",
            {"repository_id": repository.repository_id, "symbol_status": "deleted"},
            limit=1000,
        ).items
        for symbol in deleted_symbols:
            try:
                tx.nodes.delete(symbol.id, hard=True)
            except Exception:
                logger.debug("deleted symbol prune failed", exc_info=True)
    return {
        "status": "complete",
        "repository_id": repository.repository_id,
        "kept": len(revisions) - len(removed),
        "removed": len(removed),
        "symbols_removed": len(deleted_symbols),
        "keep": keep,
    }


def _find_revision(
    client: Any, repository: RepositoryInfo, revision_id: str
) -> Any | None:
    for key in (
        revision_id,
        f"{repository.key}@{revision_id}",
    ):
        node = client.nodes.get(derived_id("CodeRevision", key))
        if node is not None:
            return node
    return None


def _revision_matches(payload: dict[str, Any], value: str, side: str) -> bool:
    candidates = {
        str(payload.get("revision_key") or ""),
        str(payload.get("revision_id") or ""),
        str(payload.get("commit_hash") or ""),
    }
    if value in candidates:
        return True
    return any(item.endswith(f"@{value}") for item in candidates if item)


def _envelope(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    *,
    results: list[dict[str, Any]],
    total: int,
    truncated: bool,
    policy: str,
    limits: dict[str, int],
    unavailable: str | None = None,
) -> CodeQueryResult:
    node = client.nodes.get(derived_id("CodeRepository", repository.key))
    props = dict(node.properties) if node is not None else {}
    index_status = props.get("index_status", "missing")
    freshness = _freshness(repository, props)
    if policy == "strict":
        if index_status not in ("complete", "patched"):
            from .exceptions import IndexStaleError

            raise IndexStaleError(
                "strict policy requires a complete index",
                details={"index_status": index_status},
            )
        if index_status != "patched" and freshness != "exact":
            from .exceptions import IndexStaleError

            raise IndexStaleError(
                "strict policy requires the index to match HEAD",
                details={"freshness": freshness},
            )
    revision_id = f"{repository.commit_hash or 'nogit'}" + (
        "@dirty" if repository.is_dirty else ""
    )
    return CodeQueryResult(
        schema_version=SCHEMA_VERSION,
        query_id=f"query_{new_node_id()}",
        repository_id=repository.repository_id,
        revision_id=revision_id,
        snapshot_hash=props.get("snapshot_hash") if index_status in ("complete", "patched") else None,
        results=results,
        metadata=CodeQueryMetadata(
            total=total,
            truncated=truncated,
            index_status=index_status,
            stale=freshness != "exact",
            freshness=freshness,
            unresolved_count=_unresolved_count(client, repository),
            coverage_available=_coverage_available(client, repository),
            policy=policy,
            analysis_timestamp=_now(),
            limits=limits,
            unavailable=unavailable,
        ),
    )


def _unresolved_count(client: Any, repository: RepositoryInfo) -> int:
    total = 0
    after = ""
    while True:
        rows = client.query.cypher(
            "MATCH (f:CodeFile) WHERE f.repository_id=$repo "
            "AND f.deleted_at IS NULL AND f.path>$after "
            "RETURN f.path AS path, f.props AS props ORDER BY f.path",
            {"repo": repository.repository_id, "after": after},
            max_rows=100,
        ).items
        if not rows:
            break
        from hermes_graph.serialization import decode_properties

        for row in rows:
            total += int(decode_properties(row.get("props")).get("unresolved_count") or 0)
        last = str(rows[-1].get("path") or "")
        if not last or last == after or len(rows) < 100:
            break
        after = last
    return total


def _coverage_available(client: Any, repository: RepositoryInfo) -> bool:
    try:
        rows = client.query.cypher(
            "MATCH (r:CodeAnalysisRun) WHERE r.repository_id=$repo "
            "AND r.kind='coverage' AND r.status='completed' "
            "RETURN count(*) AS count",
            {"repo": repository.repository_id},
            max_rows=1,
        ).items
    except Exception:
        logger.debug("coverage availability probe failed", exc_info=True)
        return False
    return bool(rows and int(rows[0].get("count") or 0) > 0)


def _symbol_payload(node: Any) -> dict[str, Any]:
    props = node.properties
    return {
        "symbol_id": props.get("symbol_id") or node.key,
        "node_id": node.id,
        "kind": props.get("kind"),
        "name": props.get("name"),
        "qualified_name": props.get("qualified_name"),
        "language": props.get("language"),
        "file_path": props.get("file_path"),
        "start_line": props.get("start_line"),
        "end_line": props.get("end_line"),
        "start_column": props.get("start_column"),
        "end_column": props.get("end_column"),
        "signature": props.get("signature"),
        "content_hash": props.get("content_hash"),
        "file_content_hash": props.get("file_content_hash"),
        "file_indexed_hash": props.get("file_indexed_hash"),
        "file_index_status": props.get("file_index_status"),
        "container_symbol": props.get("container_symbol"),
        "is_exported": props.get("is_exported"),
        "symbol_status": props.get("symbol_status"),
    }


def _file_payload(node: Any) -> dict[str, Any]:
    props = node.properties
    return {
        "node_id": node.id,
        "path": props.get("path"),
        "language": props.get("language"),
        "content_hash": props.get("content_hash"),
        "indexed_hash": props.get("indexed_hash"),
        "index_status": props.get("index_status"),
        "parse_status": props.get("parse_status"),
        "symbol_count": props.get("symbol_count"),
    }


def _freshness(repository: RepositoryInfo, props: dict[str, Any]) -> str:
    indexed_commit = props.get("commit_hash") or ""
    if not indexed_commit:
        return "unknown"
    if repository.is_dirty:
        return "dirty_unknown"
    return "exact" if indexed_commit == repository.commit_hash else "head_changed"


def _policy_allows(status: Any, policy: str) -> bool:
    if status == "invalidated":
        return False
    if policy == "strict":
        return status in ("resolved", "verified")
    if policy == "exploratory":
        return True
    return status in ("resolved", "verified", "inferred")


def _count(client: Any, statement: str, params: dict[str, Any]) -> int:
    try:
        result = client.query.cypher(statement, params, max_rows=1)
    except Exception:
        logger.debug("code count query failed", exc_info=True)
        return 0
    if not result.items:
        return 0
    return int(result.items[0].get("count") or 0)


def _count_edges(client: Any, repository_id: str) -> int:
    from .schema import EDGE_TYPES

    total = 0
    for edge in EDGE_TYPES:
        total += _count(
            client,
            f"MATCH (a)-[r:{edge.name}]->(b) WHERE a.repository_id=$repo "
            "RETURN count(r) AS count",
            {"repo": repository_id},
        )
    return total
