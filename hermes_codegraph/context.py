"""Deterministic agent context for one symbol (ADR-8 D11).

The result is plain text with ``file:line`` and ``symbol_id`` references. It is
built from the graph plus the current file bytes, and never mutates the graph,
the system prompt or conversation history. A snippet is included only when the
file hash still matches the indexed hash.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from hermes_graph.serialization import derived_id

from . import query
from .models import SCHEMA_VERSION
from .scanner import RepositoryInfo

MAX_FRAGMENT_LINES = 80


def build_context(
    client: Any,
    repository: RepositoryInfo,
    registry: Any,
    target: str,
    *,
    budget_tokens: int = 6000,
) -> dict[str, Any]:
    budget = max(200, min(int(budget_tokens), 100_000))
    symbol = client.nodes.get(derived_id("CodeSymbol", target))
    if symbol is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "target": target,
            "text": "",
            "truncated": False,
            "budget_tokens": budget,
            "estimated_tokens": 0,
            "not_found": True,
        }
    props = dict(symbol.properties)
    sections: list[tuple[int, str]] = []
    sections.append((0, _definition_section(client, repository, props)))
    for label, result, order in (
        ("callers", query.get_callers(client, repository, registry, target, depth=1), 1),
        ("callees", query.get_callees(client, repository, registry, target, depth=1), 1),
        ("references", query.get_references(client, repository, registry, target), 2),
        ("types", query.get_type_hierarchy(client, repository, registry, target, depth=2), 3),
        ("tests", query.get_tests_for_symbol(client, repository, registry, target), 5),
        ("coverage", query.get_coverage(client, repository, registry, target), 5),
    ):
        items = result.results
        if not items:
            continue
        sections.append((order, _list_section(label, items)))
    sections.sort(key=lambda item: item[0])
    lines = [f"# code context: {props.get('qualified_name')} ({props.get('symbol_id')})"]
    used = 0
    truncated = False
    for _order, text in sections:
        tokens = max(1, len(text) // 4)
        if used + tokens > budget:
            truncated = True
            break
        lines.append(text)
        used += tokens
    return {
        "schema_version": SCHEMA_VERSION,
        "target": target,
        "text": "\n\n".join(lines),
        "truncated": truncated,
        "budget_tokens": budget,
        "estimated_tokens": used,
        "not_found": False,
    }


def _definition_section(
    client: Any, repository: RepositoryInfo, props: dict[str, Any]
) -> str:
    path = str(props.get("file_path") or "")
    start = int(props.get("start_line") or 1)
    end = int(props.get("end_line") or start)
    header = (
        f"## definition\n{props.get('kind')} {props.get('qualified_name')}\n"
        f"{path}:{start}-{end}  symbol_id={props.get('symbol_id')}"
    )
    file_node = client.nodes.get(
        derived_id("CodeFile", f"{repository.key}@{path}")
    )
    expected = (
        str(file_node.properties.get("content_hash") or "")
        if file_node is not None else ""
    )
    snippet = _snippet(repository, expected, path, start, end)
    return header + (f"\n```\n{snippet}\n```" if snippet else "")


def _snippet(
    repository: RepositoryInfo,
    expected_hash: str,
    path: str,
    start: int,
    end: int,
) -> str:
    if not path:
        return ""
    absolute = (repository.root / path).resolve()
    try:
        absolute.relative_to(repository.root.resolve())
    except ValueError:
        return ""
    try:
        data = absolute.read_bytes()
    except OSError:
        return ""
    if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
        # The indexed file moved on, so only the location is trustworthy.
        return ""
    lines = data.decode("utf-8", errors="replace").splitlines()
    fragment = lines[max(start - 1, 0): min(end, len(lines))][:MAX_FRAGMENT_LINES]
    return "\n".join(fragment)


def _list_section(label: str, items: list[dict[str, Any]]) -> str:
    rows: list[str] = [f"## {label}"]
    for item in items:
        name = item.get("qualified_name") or item.get("path") or item.get("name")
        if not name:
            continue
        location = item.get("file_path") or item.get("path") or ""
        line = item.get("start_line")
        where = f"{location}:{line}" if location and line else location
        status = (item.get("edge") or {}).get("resolution_status")
        suffix = f" [{status}]" if status else ""
        rows.append(f"- {name}  {where}{suffix}")
    return "\n".join(rows)
