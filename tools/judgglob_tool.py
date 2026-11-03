"""``judgglob``: semantic file-name search (ADR-11 D14).

Read-only and service-gated: the tool appears only when a judgment route is
reachable (``judgment.tools.enabled``). It never accepts questions/state — it
builds the restricted name-relevance questions itself — and never reads file
contents.
"""

from __future__ import annotations

import json
from typing import Any

from tools.registry import registry, tool_error


def _available() -> bool:
    from agent.judgment.find import judgment_tools_available

    return judgment_tools_available()


JUDGGLOB_SCHEMA = {
    "name": "judgglob",
    "description": (
        "Rank file paths semantically by a plain-language description. Read-only; judges "
        "names and locations, not contents. Use when you know what you want but not what it "
        "is called; use glob when you know the pattern."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What the files do or contain.",
            },
            "path": {
                "type": "string",
                "description": "Directory or file scope; defaults to cwd.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 128,
                "default": 32,
            },
            "hidden": {
                "type": "boolean",
                "default": False,
                "description": "Include dotfiles/dot-directories.",
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


def judgglob_tool(args: dict[str, Any], **_: Any) -> str:
    from agent.judgment.find import judgglob, resolve_find_judge, validate_scope
    from agent.judgment.types import (
        JudgmentError,
        JudgmentUnavailable,
        JudgmentValidationError,
    )

    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query must be a non-empty description")
    path = str(args.get("path") or "").strip() or None
    limit = _clamped_int(args.get("limit"), 32, 1, 128)
    hidden = bool(args.get("hidden", False))
    try:
        validate_scope(path)
        judge = resolve_find_judge("judgglob")
        payload = judgglob(query, judge=judge, path=path, limit=limit, hidden=hidden)
    except JudgmentUnavailable as exc:
        return tool_error(f"judgment unavailable; use glob instead ({exc})")
    except (JudgmentValidationError, ValueError, OSError) as exc:
        return tool_error(str(exc))
    except JudgmentError as exc:
        return tool_error(f"judgglob failed: {exc}")
    if payload["coverage"] == "unavailable":
        return tool_error(
            "every judgment request failed, so no path was judged; this is not evidence "
            "of absence. Use glob instead.")
    return json.dumps(payload, ensure_ascii=False)


registry.register(
    name="judgglob",
    toolset="judgment",
    schema=JUDGGLOB_SCHEMA,
    handler=judgglob_tool,
    check_fn=_available,
    description="Semantic file-name search via judgments",
    emoji="🧭",
    max_result_size_chars=20000,
)

__all__ = ["JUDGGLOB_SCHEMA", "judgglob_tool"]
