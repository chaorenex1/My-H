"""Read-only code graph queries for the model (ADR-8 D10).

One tool with an ``action`` enum instead of a dozen schemas. The ``check_fn``
only answers reachability: config switch plus the two optional extras. It never
opens the graph, indexes, or reads a repository.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

ACTIONS = (
    "status",
    "find_symbols",
    "get_symbol",
    "get_definition",
    "get_references",
    "callers",
    "callees",
    "path",
    "types",
    "implementations",
    "dependencies",
    "cycles",
    "impact",
    "tests",
    "coverage",
    "diagnostics",
    "changes",
    "context",
)

CODE_INTEL_SCHEMA = {
    "name": "code_intel",
    "description": "Query the persistent code graph for this repository.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "Query to run.",
            },
            "symbol_id": {"type": "string", "description": "Symbol id (sym_...)."},
            "source_id": {"type": "string", "description": "Path source symbol id."},
            "target_id": {"type": "string", "description": "Path target symbol id."},
            "module_id": {
                "type": "string",
                "description": "Module qualified name (or its CodeModule node id).",
            },
            "name": {"type": "string", "description": "Exact symbol name for find_symbols."},
            "kind": {"type": "string", "description": "Symbol kind filter."},
            "language": {"type": "string", "description": "Language id filter."},
            "path": {"type": "string", "description": "File path filter for diagnostics."},
            "rule": {"type": "string", "description": "Diagnostic rule id filter."},
            "severity": {"type": "string", "description": "Diagnostic severity filter."},
            "policy": {
                "type": "string",
                "enum": ["strict", "balanced", "exploratory"],
                "description": "Confidence policy; impact defaults to strict.",
            },
            "depth": {"type": "integer", "minimum": 1, "maximum": 5},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "budget_tokens": {"type": "integer", "minimum": 200, "maximum": 100000},
            "from_revision": {"type": "string"},
            "to_revision": {"type": "string"},
            "include": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["callers", "references", "dependencies", "implementations", "tests"],
                },
                "description": "Explicit impact categories.",
            },
            "root": {"type": "string", "description": "Repository path (default: cwd)."},
        },
        "required": ["action"],
    },
}


def _codegraph_available() -> bool:
    try:
        from pm import extras

        if not extras.available("graph") or not extras.available("codegraph"):
            return False
    except Exception:  # health: allow BLE001 -- optional PM probe; the tool is hidden when PM is unavailable
        return False
    try:
        from hermes_codegraph.config import CodeGraphLimits

        return CodeGraphLimits.from_config().enabled
    except Exception:  # health: allow BLE001 -- config is optional; a broken config hides the tool
        return False


def code_intel_tool(args: dict[str, Any], **_: Any) -> str:
    action = str(args.get("action") or "").strip()
    if action not in ACTIONS:
        return tool_error(f"action must be one of {', '.join(ACTIONS)}")
    root = args.get("root") or os.getcwd()
    try:
        from hermes_codegraph import CodeIntelligence
        from hermes_codegraph.exceptions import (
            CodeGraphError,
            CodeGraphUnavailableError,
        )

        with CodeIntelligence.open(root=root, read_only=True) as ci:
            status = ci.status()
            if status.get("index_status") == "missing":
                return json.dumps(
                    {
                        "status": "not_indexed",
                        "repository_id": status.get("repository_id"),
                        "hint": "run `hermes code index` in this repository",
                    },
                    ensure_ascii=False,
                )
            payload = _dispatch(ci, action, args)
    except CodeGraphUnavailableError as exc:
        if "does not exist" in exc.message:
            return json.dumps(
                {
                    "status": "not_indexed",
                    "hint": "run `hermes code index` in this repository",
                },
                ensure_ascii=False,
            )
        return json.dumps(exc.to_dict(), ensure_ascii=False)
    except CodeGraphError as exc:
        return json.dumps(exc.to_dict(), ensure_ascii=False)
    except Exception as exc:
        if type(exc).__name__ == "GraphUnavailableError":
            return json.dumps(
                {
                    "status": "not_indexed",
                    "hint": "run `hermes code index` in this repository",
                },
                ensure_ascii=False,
            )
        logger.debug("code_intel failed", exc_info=True)
        return tool_error(f"{type(exc).__name__}: {exc}")
    return json.dumps(payload, ensure_ascii=False)


def _dispatch(ci: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    policy = str(args.get("policy") or "")
    depth = _int(args.get("depth"), 2)
    limit = _int(args.get("limit"), 50)
    symbol_id = str(args.get("symbol_id") or "")
    if action == "status":
        return ci.status()
    if action == "find_symbols":
        return ci.find_symbols(
            name=args.get("name"), kind=args.get("kind"),
            language=args.get("language"), limit=limit,
        )
    if action == "get_symbol":
        return ci.get_symbol(symbol_id)
    if action == "get_definition":
        return ci.get_definition(symbol_id)
    if action == "get_references":
        return ci.get_references(
            symbol_id, policy=policy or "balanced", limit=limit
        )
    if action == "callers":
        return ci.get_callers(
            symbol_id, depth=depth, policy=policy or "strict", limit=limit
        )
    if action == "callees":
        return ci.get_callees(
            symbol_id, depth=depth, policy=policy or "balanced", limit=limit
        )
    if action == "path":
        return ci.get_call_path(
            str(args.get("source_id") or ""), str(args.get("target_id") or ""),
            max_depth=depth, policy=policy or "balanced",
        )
    if action == "types":
        return ci.get_type_hierarchy(
            symbol_id, direction="both", depth=depth,
            policy=policy or "balanced", limit=limit,
        )
    if action == "implementations":
        return ci.get_implementations(
            symbol_id, policy=policy or "balanced", limit=limit
        )
    if action == "dependencies":
        return ci.get_dependencies(
            str(args.get("module_id") or ""), depth=depth, limit=limit
        )
    if action == "cycles":
        return ci.get_cycles(scope="modules", limit=limit)
    if action == "impact":
        include = args.get("include")
        if not isinstance(include, list) or not include:
            include = [
                "callers", "references", "dependencies", "implementations", "tests"
            ]
        return ci.get_impact(
            symbol_id, depth=depth, include=tuple(str(item) for item in include),
            policy=policy or "strict", limit=limit,
        )
    if action == "tests":
        return ci.get_tests_for_symbol(symbol_id, limit=limit)
    if action == "coverage":
        return ci.get_coverage(symbol_id, limit=limit)
    if action == "diagnostics":
        return ci.get_diagnostics(
            path=args.get("path"), rule=args.get("rule"),
            severity=args.get("severity"), limit=limit,
        )
    if action == "changes":
        return ci.get_changes(
            symbol_id, from_revision=args.get("from_revision"),
            to_revision=args.get("to_revision"), limit=limit,
        )
    if action == "context":
        return ci.build_context(
            symbol_id, budget_tokens=_int(args.get("budget_tokens"), 6000)
        )
    return {"error": f"unsupported action {action!r}"}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


registry.register(
    name="code_intel",
    toolset="codegraph",
    schema=CODE_INTEL_SCHEMA,
    handler=code_intel_tool,
    check_fn=_codegraph_available,
    description="Read-only code graph queries",
    emoji="🧭",
    max_result_size_chars=60000,
)

__all__ = ["ACTIONS", "CODE_INTEL_SCHEMA", "code_intel_tool"]
