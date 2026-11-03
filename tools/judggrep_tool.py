"""``judggrep``: semantic grep returning file ranges (ADR-11 D14).

Read-only and service-gated like ``judgglob``. The cascade is bounded: keyword
prefilter over at most 20 files, 24 windows of 8 KiB per file, sketch scoring
at a 0.45 cutoff, at most 40 verified passages, 16 requests in flight, and a
20 s wall budget. A partial or unavailable coverage is reported as such — it is
never presented as proof that nothing matches.
"""

from __future__ import annotations

import json
from typing import Any

from tools.registry import registry, tool_error


def _available() -> bool:
    from agent.judgment.find import judgment_tools_available

    return judgment_tools_available()


JUDGGREP_SCHEMA = {
    "name": "judggrep",
    "description": (
        "Semantic grep: find file ranges implementing a described behavior. Read-only. Use "
        "when you can describe the behavior but not the identifier; use search_files/grep "
        "for exact text. Results may be partial — not evidence of absence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The behavior to locate."},
            "path": {
                "type": "string",
                "description": "Directory or file scope; defaults to cwd.",
            },
            "glob": {
                "type": "string",
                "description": "Optional include glob, e.g. 'src/**/*.py'.",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": "Optional identifiers/terms for the lexical prefilter.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 20,
            },
            "context_lines": {
                "type": "integer",
                "minimum": 0,
                "maximum": 8,
                "default": 2,
            },
        },
        "required": ["query"],
    },
}


def _clamped_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _keywords(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("keywords must be an array of strings")
    return [str(item) for item in raw if str(item).strip()]


def judggrep_tool(args: dict[str, Any], **_: Any) -> str:
    from agent.judgment.find import judggrep, resolve_find_judge, validate_scope
    from agent.judgment.types import (
        JudgmentError,
        JudgmentUnavailable,
        JudgmentValidationError,
    )

    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query must be a non-empty description")
    path = str(args.get("path") or "").strip() or None
    glob_pattern = str(args.get("glob") or "").strip() or None
    try:
        keywords = _keywords(args.get("keywords"))
        limit = _clamped_int(args.get("limit"), 20, 1, 50)
        context_lines = _clamped_int(args.get("context_lines"), 2, 0, 8)
        validate_scope(path, glob_pattern)
        judge = resolve_find_judge("judggrep")
        payload = judggrep(
            query, judge=judge, path=path, glob_pattern=glob_pattern,
            keywords=keywords, limit=limit, context_lines=context_lines)
    except JudgmentUnavailable as exc:
        return tool_error(f"judgment unavailable; use search_files/grep instead ({exc})")
    except (JudgmentValidationError, ValueError, OSError) as exc:
        return tool_error(str(exc))
    except JudgmentError as exc:
        return tool_error(f"judggrep failed: {exc}")
    if payload["coverage"] == "unavailable":
        return tool_error(
            "every judgment request failed, so nothing was judged; this is not evidence "
            "of absence. Use search_files/grep instead.")
    return json.dumps(payload, ensure_ascii=False)


registry.register(
    name="judggrep",
    toolset="judgment",
    schema=JUDGGREP_SCHEMA,
    handler=judggrep_tool,
    check_fn=_available,
    description="Semantic grep via judgments",
    emoji="🧭",
    max_result_size_chars=40000,
)

__all__ = ["JUDGGREP_SCHEMA", "judggrep_tool"]
