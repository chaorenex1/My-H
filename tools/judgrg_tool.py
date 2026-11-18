"""``judgrg``: judgment-routed, read-only search (ADR-11 D14).

The tool accepts only the request — a user question, an agent task, or a code
analysis goal. A restricted choice question decides the intent and therefore the
tool (see ``agent/prompts/judge/find.json``), then ripgrep or ast-grep runs on
the local filesystem. Read-only and service-gated by ``judgment.tools.enabled``;
a partial or unavailable run is reported as such and never presented as proof
that nothing matches.
"""

from __future__ import annotations

import json
from typing import Any

from tools.registry import registry, tool_error


def _available() -> bool:
    from agent.judgment.find import judgment_tools_available

    return judgment_tools_available()


JUDGRG_SCHEMA = {
    "name": "judgrg",
    "description": (
        "Read-only search: judgment routes a request to ripgrep or ast-grep. "
        "Pass the user question, agent task, or code-analysis goal; the intent, "
        "search strategy, and tool are judged, not specified by the caller. "
        "Use search_files/grep when you already know the literal text or glob. "
        "Runs on the local filesystem, not a sandboxed task environment. "
        "Results may be partial — not evidence of absence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The user question, agent task, or code-analysis goal. Naming a "
                    "symbol or a called function is enough; a full code pattern such "
                    "as `foo($X)` is used verbatim for structural search."),
            },
        },
        "required": ["query"],
    },
}


def judgrg_tool(args: dict[str, Any], **_: Any) -> str:
    from agent.judgment.find import FindBackendUnavailable, judgrg, resolve_find_judge
    from agent.judgment.types import (
        JudgmentError,
        JudgmentUnavailable,
        JudgmentValidationError,
    )

    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query must be a non-empty description")
    try:
        judge = resolve_find_judge("find")
        payload = judgrg(query, judge=judge)
    except JudgmentUnavailable as exc:
        return tool_error(f"judgment unavailable; use search_files instead ({exc})")
    except FindBackendUnavailable as exc:
        return tool_error(str(exc))
    except (JudgmentValidationError, ValueError, OSError) as exc:
        return tool_error(str(exc))
    except JudgmentError as exc:
        return tool_error(f"judgrg failed: {exc}")
    if payload["coverage"] == "unavailable":
        return tool_error(
            "every judgment request failed, so no search ran; this is not evidence "
            "of absence. Use search_files instead.")
    return json.dumps(payload, ensure_ascii=False)


registry.register(
    name="judgrg",
    toolset="judgment",
    schema=JUDGRG_SCHEMA,
    handler=judgrg_tool,
    check_fn=_available,
    description="Judgment-routed ripgrep/ast-grep search",
    emoji="🧭",
    max_result_size_chars=40000,
)

__all__ = ["JUDGRG_SCHEMA", "judgrg_tool"]
