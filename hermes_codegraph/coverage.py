"""Per-test coverage import (ADR-8 D7/D8).

Only reports that attribute coverage to a test context can produce
``CODE_COVERS`` edges: ``coverage.py`` contexts and ``pytest-cov``
``--cov-context=test``. Aggregated lcov/c8 output is rejected instead of
inventing ``TestCase -> Symbol`` edges.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_graph.serialization import derived_id, new_node_id

from .exceptions import (
    CodeGraphError,
    CoverageContextMissingError,
    RevisionMismatchError,
)
from .scanner import RepositoryInfo

logger = logging.getLogger(__name__)

FORMATS = ("auto", "coverage-py-context", "pytest-cov-context")
MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_LINES = 2_000_000


def import_coverage(
    client: Any,
    repository: RepositoryInfo,
    path: str | Path,
    *,
    revision: str | None = None,
    format: str = "auto",
) -> dict[str, Any]:
    if format not in FORMATS:
        raise CodeGraphError(f"format must be one of {FORMATS}")
    repository_node = client.nodes.get(derived_id("CodeRepository", repository.key))
    props = dict(repository_node.properties) if repository_node is not None else {}
    if props.get("index_status") != "complete":
        raise CodeGraphError(
            "coverage import needs a complete index; run `hermes code index` first",
            details={"index_status": props.get("index_status", "missing")},
        )
    current_snapshot = props.get("snapshot_hash") or ""
    expected = _expected_revision(repository, revision)
    if expected is not None and expected != repository.revision_key:
        raise RevisionMismatchError(
            "coverage report targets a different revision",
            details={
                "expected": expected,
                "current": repository.revision_key,
            },
        )
    report_path = Path(path)
    try:
        size = report_path.stat().st_size
    except OSError as exc:
        raise CodeGraphError(f"cannot read coverage report: {exc}") from exc
    if size > MAX_REPORT_BYTES:
        raise CodeGraphError("coverage report exceeds the size limit")
    try:
        document = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise CodeGraphError(f"invalid coverage report: {exc}") from exc
    if not isinstance(document, dict):
        raise CodeGraphError("coverage report must be a JSON object")
    detected = _detect_format(document)
    if detected is None:
        raise CoverageContextMissingError(
            "coverage report has no per-test contexts; aggregated reports cannot "
            "produce COVERS edges (use coverage.py contexts or pytest-cov "
            "--cov-context=test)"
        )
    contexts = _contexts(document)
    if not contexts:
        raise CoverageContextMissingError(
            "coverage report contains contexts but none map to executed lines"
        )
    tests = _load_tests(client, repository)
    symbols = _load_symbols(client, repository)
    run_key = f"run_{new_node_id()}"
    run_id = derived_id("CodeAnalysisRun", run_key)
    observed = datetime.now(UTC).isoformat()
    edges = 0
    with client.transaction.begin() as tx:
        tx.nodes.upsert(
            "CodeAnalysisRun",
            run_key,
            {
                "repository_id": repository.repository_id,
                "kind": "coverage",
                "status": "completed",
                "started_at": observed,
                "finished_at": observed,
                "target_revision": repository.revision_key,
                "snapshot_hash": current_snapshot,
                "files_total": len(contexts),
                "files_indexed": 0,
                "files_skipped": 0,
                "files_failed": 0,
                "error": "",
            },
        )
        for context_name, files in contexts.items():
            test = _match_test(tests, context_name)
            if test is None:
                continue
            for file_path, lines in files.items():
                for line in sorted(lines):
                    for symbol in _symbols_at(symbols, file_path, line):
                        evidence_key = (
                            f"{repository.key}@{file_path}#{line}:1-{line}:1:"
                            f"coverage:{detected}:{symbol.get('symbol_id')}"
                        )
                        tx.nodes.upsert(
                            "CodeEvidence",
                            evidence_key,
                            {
                                "repository_id": repository.repository_id,
                                "evidence_kind": "coverage",
                                "source_tool": detected,
                                "source_tool_version": "",
                                "language": symbol.get("language") or "",
                                "file_path": file_path,
                                "start_line": line,
                                "end_line": line,
                                "start_column": 1,
                                "end_column": 1,
                                "excerpt_hash": symbol.get("content_hash") or "",
                                "analysis_run_id": run_key,
                                "revision_id": repository.revision_key,
                                "observed_at": observed,
                                "context": context_name,
                            },
                        )
                        evidence_id = derived_id("CodeEvidence", evidence_key)
                        tx.edges.upsert(
                            test.id,
                            derived_id("CodeSymbol", symbol["symbol_id"]),
                            "CODE_COVERS",
                            properties={
                                "evidence_ids": [evidence_id],
                                "resolution_status": "verified",
                                "confidence": 1.0,
                                "resolution_method": detected,
                                "confidence_method": "runtime_coverage",
                                "context": context_name,
                            },
                        )
                        edges += 1
        tx.nodes.upsert(
            "CodeAnalysisRun",
            run_key,
            {
                "repository_id": repository.repository_id,
                "kind": "coverage",
                "status": "completed",
                "started_at": observed,
                "finished_at": datetime.now(UTC).isoformat(),
                "target_revision": repository.revision_key,
                "snapshot_hash": current_snapshot,
                "files_total": len(contexts),
                "files_indexed": len(contexts),
                "files_skipped": 0,
                "files_failed": 0,
                "error": "",
                "covers_edges": edges,
                "analysis_run_id": run_key,
            },
        )
    return {
        "status": "complete",
        "format": detected,
        "analysis_run_id": run_key,
        "revision_id": repository.revision_key,
        "snapshot_hash": current_snapshot,
        "contexts": len(contexts),
        "covers_edges": edges,
        "run_id": run_id,
    }


def _expected_revision(repository: RepositoryInfo, revision: str | None) -> str | None:
    if not revision:
        return None
    text = str(revision).strip()
    if text in (repository.revision_key, repository.commit_hash):
        return repository.revision_key
    if text.startswith(f"{repository.key}@"):
        return text
    return text


def _detect_format(document: dict[str, Any]) -> str | None:
    files = document.get("files")
    if isinstance(files, dict):
        for payload in files.values():
            if isinstance(payload, dict) and isinstance(payload.get("contexts"), dict):
                return "coverage-py-context"
    contexts = document.get("contexts")
    if isinstance(contexts, dict) and contexts:
        return "pytest-cov-context"
    return None


def _contexts(document: dict[str, Any]) -> dict[str, dict[str, set[int]]]:
    """Normalize both supported shapes to ``context -> file -> lines``."""
    result: dict[str, dict[str, set[int]]] = {}
    files = document.get("files")
    if isinstance(files, dict):
        for file_path, payload in files.items():
            if not isinstance(payload, dict):
                continue
            raw = payload.get("contexts")
            if not isinstance(raw, dict):
                continue
            for line_text, names in raw.items():
                try:
                    line = int(line_text)
                except (TypeError, ValueError):
                    continue
                for name in names if isinstance(names, list) else [names]:
                    result.setdefault(str(name), {}).setdefault(
                        str(file_path).replace("\\", "/"), set()
                    ).add(line)
    raw_contexts = document.get("contexts")
    if isinstance(raw_contexts, dict):
        for name, payload in raw_contexts.items():
            if not isinstance(payload, dict):
                continue
            for file_path, lines in payload.items():
                bucket = result.setdefault(str(name), {}).setdefault(
                    str(file_path).replace("\\", "/"), set()
                )
                for line in lines if isinstance(lines, list) else []:
                    try:
                        bucket.add(int(line))
                    except (TypeError, ValueError):
                        continue
    return result


def _load_tests(client: Any, repository: RepositoryInfo) -> list[Any]:
    return client.nodes.search(
        "CodeTestCase", {"repository_id": repository.repository_id}, limit=1000
    ).items


def _load_symbols(client: Any, repository: RepositoryInfo) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    after = ""
    while True:
        rows = client.query.cypher(
            "MATCH (s:CodeSymbol) WHERE s.repository_id=$repo "
            "AND (s.symbol_status IS NULL OR s.symbol_status <> 'deleted') "
            "AND s.symbol_id>$after "
            "RETURN s.symbol_id AS symbol_id, s.file_path AS file_path, "
            "s.language AS language, s.start_line AS start_line, "
            "s.end_line AS end_line, s.content_hash AS content_hash "
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


def _match_test(tests: list[Any], context: str) -> Any | None:
    raw = context.split("|", 1)[0].strip()
    if not raw:
        return None
    name = raw.rsplit("::", 1)[-1]
    for test in tests:
        props = test.properties
        if props.get("name") == name:
            return test
    for test in tests:
        props = test.properties
        qualified = str(props.get("qualified_name") or "")
        if qualified == raw or qualified.endswith(f"::{raw}") or qualified.endswith(f".{raw}"):
            return test
    return None


def _symbols_at(
    symbols: list[dict[str, Any]], file_path: str, line: int
) -> list[dict[str, Any]]:
    normalized = file_path.replace("\\", "/")
    return [
        symbol for symbol in symbols
        if symbol.get("file_path") == normalized
        and symbol.get("kind") != "module"
        and int(symbol.get("start_line") or 0) <= line <= int(symbol.get("end_line") or 0)
    ]
