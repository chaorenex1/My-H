"""Evidence-constrained code graph writes for the model (ADR-8 D15).

The model is not a fact source: every fact must name an analysis tool and carry
a span plus excerpt hash that match the current file. ``refresh_file`` is the
authoritative rebuild; ``apply_evidence`` and ``invalidate`` are optimistic and
conflict-checked. All actions run in one transaction and reject partially.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

ACTIONS = ("refresh_file", "apply_evidence", "invalidate")

CODE_GRAPH_UPDATE_SCHEMA = {
    "name": "code_graph_update",
    "description": (
        "Write analysis-tool observations into the local code graph. "
        "`refresh_file` re-parses one file; `apply_evidence` materializes facts "
        "with source_tool/span/excerpt_hash and optimistic concurrency; "
        "`invalidate` retires stale evidence. The model cannot be the source of "
        "a fact: source_tool='model_observation' is rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "path": {"type": "string", "description": "Repository-relative file path."},
            "semantic": {"type": "boolean", "description": "Use LSP during refresh_file."},
            "semgrep": {
                "type": "boolean",
                "description": "Run Semgrep during refresh_file (off by default).",
            },
            "expected_revision_id": {"type": "string"},
            "expected_content_hash": {"type": "string"},
            "facts": {
                "type": "array",
                "description": "Observed facts for apply_evidence.",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "definition", "reference", "call", "type",
                                "import", "test", "diagnostic",
                            ],
                        },
                        "source_id": {"type": "string"},
                        "target_id": {"type": "string"},
                        "relation": {"type": "string"},
                        "resolution_status": {
                            "type": "string",
                            "enum": ["inferred", "resolved"],
                        },
                        "rule_id": {"type": "string"},
                        "severity": {"type": "string"},
                        "message": {"type": "string"},
                        "evidence": {
                            "type": "object",
                            "properties": {
                                "source_tool": {"type": "string"},
                                "source_tool_version": {"type": "string"},
                                "span": {
                                    "type": "object",
                                    "properties": {
                                        "start_line": {"type": "integer"},
                                        "start_column": {"type": "integer"},
                                        "end_line": {"type": "integer"},
                                        "end_column": {"type": "integer"},
                                    },
                                },
                                "excerpt_hash": {"type": "string"},
                                "observed_at": {"type": "string"},
                            },
                            "required": ["source_tool", "span", "excerpt_hash"],
                        },
                    },
                    "required": ["kind", "evidence"],
                },
            },
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Evidence or edge ids for invalidate.",
            },
            "reason": {"type": "string"},
            "root": {"type": "string", "description": "Repository path (default: cwd)."},
        },
        "required": ["action"],
    },
}


def _update_available() -> bool:
    try:
        from pm import extras

        if not extras.available("graph") or not extras.available("codegraph"):
            return False
    except Exception:  # health: allow BLE001 -- optional PM probe; the tool is hidden when PM is unavailable
        return False
    try:
        from hermes_codegraph.config import CodeGraphLimits

        limits = CodeGraphLimits.from_config()
        return bool(limits.enabled and limits.update)
    except Exception:  # health: allow BLE001 -- config is optional; a broken config hides the tool
        return False


def code_graph_update_tool(args: dict[str, Any], **_: Any) -> str:
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

        with CodeIntelligence.open(root=root, read_only=False) as ci:
            if action == "refresh_file":
                payload = ci.refresh_file(
                    str(args.get("path") or ""),
                    semantic=bool(args.get("semantic")),
                    semgrep=bool(args.get("semgrep")),
                )
            elif action == "apply_evidence":
                payload = ci.apply_evidence(
                    str(args.get("path") or ""),
                    args.get("facts") if isinstance(args.get("facts"), list) else [],
                    expected_revision_id=str(args.get("expected_revision_id") or ""),
                    expected_content_hash=str(args.get("expected_content_hash") or ""),
                )
            else:
                payload = ci.invalidate(
                    [str(item) for item in args.get("ids") or []],
                    reason=str(args.get("reason") or ""),
                )
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
        logger.debug("code_graph_update failed", exc_info=True)
        return tool_error(f"{type(exc).__name__}: {exc}")
    return json.dumps(payload, ensure_ascii=False)


registry.register(
    name="code_graph_update",
    toolset="codegraph",
    schema=CODE_GRAPH_UPDATE_SCHEMA,
    handler=code_graph_update_tool,
    check_fn=_update_available,
    description="Evidence-constrained code graph updates",
    emoji="✍️",
    max_result_size_chars=40000,
)

__all__ = ["ACTIONS", "CODE_GRAPH_UPDATE_SCHEMA", "code_graph_update_tool"]
