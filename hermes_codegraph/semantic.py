"""Optional LSP semantic enhancement (ADR-8 D6).

The syntax layer always builds a complete baseline. When semantic indexing is
explicitly enabled and a trusted LSP server is available for a file, this pass
upgrades inferred call/reference edges to ``resolved`` or, when LSP disagrees,
invalidates the inferred target and writes the LSP target instead. A missing or
slow server never fails the index: every query returns empty on failure.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from hermes_graph.serialization import derived_id

from .scanner import RepositoryInfo

logger = logging.getLogger(__name__)

MAX_QUERIES = 500


def enhance(
    client: Any,
    repository: RepositoryInfo,
    *,
    run_key: str,
    revision_id: str,
    service: Any | None = None,
    max_queries: int = MAX_QUERIES,
) -> dict[str, Any]:
    if service is None:
        try:
            from agent.lsp.manager import LSPService

            service = LSPService.create_from_config()
        except Exception as exc:  # optional dependency/config
            logger.debug("semantic indexing unavailable", exc_info=True)
            return {"status": "skipped", "reason": str(exc)[:500], "upgraded": 0}
    if service is None or not getattr(service, "is_active", lambda: False)():
        return {"status": "skipped", "reason": "lsp disabled", "upgraded": 0}
    symbols = _load_symbols(client, repository)
    symbols_by_path: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        symbols_by_path.setdefault(str(symbol.get("file_path") or ""), []).append(symbol)
    observed = datetime.now(UTC).isoformat()
    plans, counts = _collect_plans(
        client,
        repository,
        service,
        symbols_by_path,
        run_key=run_key,
        revision_id=revision_id,
        observed=observed,
        max_queries=max_queries,
    )
    _apply_plans(client, plans)
    return {"status": "complete", **counts}


def _collect_plans(
    client: Any,
    repository: RepositoryInfo,
    service: Any,
    symbols_by_path: dict[str, list[dict[str, Any]]],
    *,
    run_key: str,
    revision_id: str,
    observed: str,
    max_queries: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    plans: list[dict[str, Any]] = []
    counts = {"upgraded": 0, "conflicts": 0, "ambiguous": 0, "queries": 0}
    for edge_type in ("CODE_CALLS", "CODE_REFERENCES", "CODE_INSTANTIATES"):
        for edge in _list_edges(client, repository, edge_type):
            if counts["queries"] >= max_queries:
                break
            plan = _plan_edge(
                client,
                repository,
                service,
                symbols_by_path,
                edge,
                run_key=run_key,
                revision_id=revision_id,
                observed=observed,
            )
            if plan is None:
                continue
            counts["queries"] += 1
            plans.append(plan)
            counts[plan["op"]] += 1
        if counts["queries"] >= max_queries:
            break
    return plans, counts


def _plan_edge(
    client: Any,
    repository: RepositoryInfo,
    service: Any,
    symbols_by_path: dict[str, list[dict[str, Any]]],
    edge: Any,
    *,
    run_key: str,
    revision_id: str,
    observed: str,
) -> dict[str, Any] | None:
    if edge.properties.get("resolution_status") not in (
        "inferred", "ambiguous", "unresolved"
    ):
        return None
    evidence = _first_evidence(client, edge.properties.get("evidence_ids"))
    if evidence is None:
        return None
    path = str(evidence.properties.get("file_path") or "")
    absolute = repository.root / path
    if not absolute.is_file():
        return None
    try:
        if not service.enabled_for(str(absolute)):
            return None
        locations = service.query_definition(
            str(absolute),
            int(evidence.properties.get("start_line") or 1),
            int(evidence.properties.get("start_column") or 1),
        )
    except Exception:
        logger.debug("semantic query failed", exc_info=True)
        return None
    candidates = _symbols_for_locations(symbols_by_path, locations)
    if not candidates:
        return None
    lsp_evidence = _lsp_evidence(
        repository, path, evidence.properties, observed, run_key,
        revision_id, "lsp_definition",
    )
    if len(candidates) == 1:
        target_id = derived_id("CodeSymbol", candidates[0]["symbol_id"])
        if target_id == edge.target_id:
            return {"op": "upgraded", "edge": edge}
        return {
            "op": "conflicts",
            "edge": edge,
            "target_id": target_id,
            "evidence": lsp_evidence,
        }
    return {
        "op": "ambiguous",
        "edge": edge,
        "targets": [
            derived_id("CodeSymbol", item["symbol_id"])
            for item in candidates[:3]
        ],
        "evidence": lsp_evidence,
    }


def _apply_plans(client: Any, plans: list[dict[str, Any]]) -> None:
    if not plans:
        return
    with client.transaction.begin() as tx:
        for plan in plans:
            edge = plan["edge"]
            if plan["op"] == "upgraded":
                _upgrade(tx, edge, status="resolved", method="lsp_definition")
            elif plan["op"] == "conflicts":
                _invalidate(tx, edge)
                _write_lsp_edge(
                    tx, edge, plan["target_id"], status="resolved",
                    method="lsp_definition", evidence=plan["evidence"],
                )
            else:
                _invalidate(tx, edge)
                for target_id in plan["targets"]:
                    _write_lsp_edge(
                        tx, edge, target_id, status="ambiguous",
                        method="lsp_definition", evidence=plan["evidence"],
                    )


def _load_symbols(client: Any, repository: RepositoryInfo) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    after = ""
    while True:
        rows = client.query.cypher(
            "MATCH (s:CodeSymbol) WHERE s.repository_id=$repo "
            "AND (s.symbol_status IS NULL OR s.symbol_status <> 'deleted') "
            "AND s.symbol_id>$after "
            "RETURN s.symbol_id AS symbol_id, s.file_path AS file_path, "
            "s.start_line AS start_line, s.end_line AS end_line, "
            "s.kind AS kind, s.qualified_name AS qualified_name "
            "ORDER BY s.symbol_id",
            {"repo": repository.repository_id, "after": after},
            max_rows=100,
        ).items
        if not rows:
            break
        symbols.extend(row for row in rows if isinstance(row, dict))
        last = str(rows[-1].get("symbol_id") or "")
        if not last or last == after or len(rows) < 100:
            break
        after = last
    return symbols


def _list_edges(client: Any, repository: RepositoryInfo, edge_type: str) -> list[Any]:
    edges: list[Any] = []
    after = ""
    while True:
        rows = client.query.cypher(
            f"MATCH (a)-[r:{edge_type}]->(b) "
            "WHERE a.repository_id=$repo AND r.id>$after "
            "RETURN r.id AS id ORDER BY r.id",
            {"repo": repository.repository_id, "after": after},
            max_rows=100,
        ).items
        if not rows:
            break
        for row in rows:
            edge = client.edges.get(row["id"])
            if edge is not None:
                edges.append(edge)
        last = str(rows[-1].get("id") or "")
        if not last or last == after or len(rows) < 100:
            break
        after = last
    return edges


def _first_evidence(client: Any, evidence_ids: Any) -> Any | None:
    ids = evidence_ids if isinstance(evidence_ids, list) else []
    for evidence_id in ids:
        node = client.nodes.get(evidence_id)
        if node is not None:
            return node
    return None


def _symbols_for_locations(
    symbols_by_path: dict[str, list[dict[str, Any]]], locations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for location in locations:
        path = str(location.get("path") or "").replace("\\", "/")
        line = int(location.get("start_line") or 0)
        candidates = [
            symbol for symbol in symbols_by_path.get(path, [])
            if symbol.get("kind") != "module"
            and int(symbol.get("start_line") or 0) <= line <= int(symbol.get("end_line") or 0)
        ]
        if not candidates:
            continue
        candidates.sort(
            key=lambda symbol: int(symbol.get("end_line") or 0)
            - int(symbol.get("start_line") or 0)
        )
        best = candidates[0]
        if best not in found:
            found.append(best)
    return found


def _upgrade(tx: Any, edge: Any, *, status: str, method: str) -> None:
    properties = dict(edge.properties)
    properties.update(
        {
            "resolution_status": status,
            "resolution_method": method,
            "confidence": 0.8 if status == "resolved" else 0.3,
            "confidence_method": "status_mapping",
        }
    )
    tx.edges.upsert(edge.source_id, edge.target_id, edge.type, properties=properties)


def _invalidate(tx: Any, edge: Any) -> None:
    properties = dict(edge.properties)
    properties["resolution_status"] = "invalidated"
    properties["confidence"] = 0.0
    tx.edges.upsert(edge.source_id, edge.target_id, edge.type, properties=properties)


def _write_lsp_edge(
    tx: Any,
    edge: Any,
    target_id: str,
    *,
    status: str,
    method: str,
    evidence: dict[str, Any],
) -> None:
    tx.nodes.upsert("CodeEvidence", evidence["key"], evidence["props"])
    evidence_id = derived_id("CodeEvidence", evidence["key"])
    tx.edges.upsert(
        edge.source_id,
        target_id,
        edge.type,
        properties={
            "evidence_ids": [evidence_id],
            "resolution_status": status,
            "confidence": 0.8 if status == "resolved" else 0.3,
            "resolution_method": method,
            "confidence_method": "status_mapping",
        },
    )


def _lsp_evidence(
    repository: RepositoryInfo,
    path: str,
    source_props: dict[str, Any],
    observed: str,
    run_key: str,
    revision_id: str,
    kind: str,
) -> dict[str, Any]:
    line = int(source_props.get("start_line") or 1)
    column = int(source_props.get("start_column") or 1)
    key = (
        f"{repository.key}@{path}#{line}:{column}-{line}:{column}:"
        f"{kind}:lsp:{run_key}"
    )
    return {
        "key": key,
        "props": {
            "repository_id": repository.repository_id,
            "evidence_kind": kind,
            "source_tool": "lsp",
            "source_tool_version": "",
            "language": source_props.get("language") or "",
            "file_path": path,
            "start_line": line,
            "end_line": line,
            "start_column": column,
            "end_column": column,
            "excerpt_hash": source_props.get("excerpt_hash") or "",
            "analysis_run_id": run_key,
            "revision_id": revision_id,
            "observed_at": observed,
        },
    }
