"""``git_stage``: judge-driven selective staging (ADR-17 D7).

The model passes only the instruction (what the user wants staged) — the judge
decides which hunks and files match. Restricted by construction: index only, no
commit, no worktree write. Service-gated by a usable judgment route.
"""

from __future__ import annotations

import json
from typing import Any

from tools.registry import registry, tool_error


def _available() -> bool:
    from hermes_cli.git_stage import judge_available

    return judge_available()


GIT_STAGE_SCHEMA = {
    "name": "git_stage",
    "description": (
        "Stage the changes a description names, without committing. Every unstaged "
        "hunk and whole file is judged against the instruction; only the matching "
        "units are written to the index. The worktree is never modified and no "
        "commit is created. Use when the user asks to stage part of a dirty tree "
        "(e.g. 'stage the parser fix, not the formatting')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": (
                    "Natural-language description of the changes to stage. Naming a "
                    "file, a kind of edit, or a feature is enough."),
            },
            "path": {
                "type": "string",
                "description": (
                    "Restrict judgment to one file or a directory subtree "
                    "(repo-relative). Omit to consider every unstaged change."),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Report what would be staged without touching the index."),
            },
        },
        "required": ["instruction"],
        "additionalProperties": False,
    },
}


def git_stage_tool(args: dict[str, Any], **_: Any) -> str:
    instruction = str(args.get("instruction") or "").strip()
    if not instruction:
        return tool_error("instruction must be a non-empty description of what to stage")
    from agent.judgment import JudgmentError

    from hermes_cli import git_stage

    raw_path = args.get("path")
    path = str(raw_path).strip() if raw_path is not None else ""
    try:
        summary = git_stage.stage(
            instruction,
            path=path or None,
            dry_run=bool(args.get("dry_run")),
        )
    except (RuntimeError, JudgmentError, OSError) as exc:
        return tool_error(f"git stage failed: {exc}")
    return json.dumps(summary, ensure_ascii=False)


registry.register(
    name="git_stage",
    toolset="judgment",
    schema=GIT_STAGE_SCHEMA,
    handler=git_stage_tool,
    check_fn=_available,
    description="Judge-driven selective git staging (index only)",
    emoji="🗂️",
    max_result_size_chars=8000,
)

__all__ = ["GIT_STAGE_SCHEMA", "git_stage_tool"]
