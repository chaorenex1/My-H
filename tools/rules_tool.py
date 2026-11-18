"""Model tools for the ``rules`` toolset: discover, inspect, and manage rule documents.

Rules are Markdown + YAML frontmatter files; discovery and normalization live in
``agent/rule_utils.py``. ``rule_manage`` writes only under ``HERMES_HOME/.agents/rules`` so a
model can never edit project, installation, or imported corpus files in place.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from tools.registry import registry, tool_error

_LIST_PROPERTIES = {
    "scope": {
        "type": "string",
        "description": "Optional trust scope filter: project, profile, or installation.",
    },
    "agent_name": {
        "type": "string",
        "description": "Optional agent name used to filter rules whose `agents` gate excludes it.",
    },
}

RULES_LIST_SCHEMA = {
    "name": "rules_list",
    "description": (
        "List rule files visible to this session (name, description, path scope, source). "
        "Rules are Markdown with YAML frontmatter; use rule_view for the full body."),
    "parameters": {"type": "object", "properties": _LIST_PROPERTIES},
}

RULE_VIEW_SCHEMA = {
    "name": "rule_view",
    "description": (
        "Read one addressable rule's full Markdown body and parsed frontmatter by rule id or "
        "unique name. Addressable = rulebook (has a description), always-apply, or registered "
        "TTSR; disabled and non-rulebook rules are not readable here."),
    "parameters": {
        "type": "object",
        "properties": {
            "rule_id": {
                "type": "string",
                "description": "Rule id (for example 'python/coding-style') or its unique frontmatter name.",
            },
        },
        "required": ["rule_id"],
    },
}

RULE_MANAGE_SCHEMA = {
    "name": "rule_manage",
    "description": (
        "Create, update, or delete a profile-owned rule under the active profile's "
        ".agents/rules directory. Frontmatter is written in oh-my-pi rule shape."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "delete"]},
            "rule_id": {
                "type": "string",
                "description": "Relative rule id without suffix, for example 'python/coding-style'.",
            },
            "content": {
                "type": "string",
                "description": (
                    "Full Markdown file (frontmatter + body). Mutually exclusive with metadata/body."),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Frontmatter fields to merge: description, condition, astCondition, question, "
                    "scope, globs, interruptMode, alwaysApply, agents, enabled."),
            },
            "body": {"type": "string", "description": "Markdown body without frontmatter."},
            "overwrite": {
                "type": "boolean",
                "description": "Allow create to replace an existing profile-owned rule (default false).",
            },
            "confirm": {
                "type": "boolean",
                "description": "Required true for delete.",
            },
        },
        "required": ["action", "rule_id"],
    },
}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _public_rule(rule: dict[str, Any], *, include_body: bool = False) -> dict[str, Any]:
    from agent.rule_utils import rule_summary

    return rule_summary(rule, include_body=include_body)


def rules_list(scope: str = "", agent_name: str = "") -> str:
    from agent import rule_utils

    rules = rule_utils.discover_rules(agent_name=agent_name)
    if scope:
        wanted = str(scope).strip().casefold()
        rules = [rule for rule in rules if rule.get("scope", "").casefold() == wanted]
    return _json({
        "success": True,
        "count": len(rules),
        "rules": [_public_rule(rule) for rule in rules],
    })


def rule_view(rule_id: str) -> str:
    from agent import rule_utils
    from agent.rule_buckets import addressable_rules
    from agent.ttsr_runtime import load_ttsr_settings

    try:
        catalog = addressable_rules(rule_utils.discover_rules(), load_ttsr_settings())
        rule = rule_utils.find_rule(rule_id, rules=catalog)
    except rule_utils.RuleError as exc:
        return tool_error(str(exc), success=False)
    if rule is None:
        return tool_error(f"Rule not found: {rule_id}", success=False)
    return _json({"success": True, "rule": _public_rule(rule, include_body=True)})


def rule_manage(action: str, rule_id: str, content: Optional[str] = None,
                metadata: Optional[dict[str, Any]] = None, body: Optional[str] = None,
                overwrite: bool = False, confirm: bool = False) -> str:
    from agent import rule_utils

    action = str(action or "").strip().lower()
    if action not in {"create", "update", "delete"}:
        return tool_error(f"Unknown rule_manage action: {action!r}", success=False)
    if action == "delete":
        if not confirm:
            return tool_error("Deleting a rule requires confirm=true.", success=False)
        try:
            removed = rule_utils.delete_rule(rule_id)
        except rule_utils.RuleError as exc:
            return tool_error(str(exc), success=False)
        return _json({"success": removed, "rule_id": rule_id,
                      **({"error": "Rule not found"} if not removed else {})})
    if content is not None and (metadata is not None or body is not None):
        return tool_error("content is mutually exclusive with metadata/body.", success=False)
    try:
        path = rule_utils.rule_file_path(rule_id)
        existing = None
        if path.is_file():
            existing = rule_utils.parse_rule_text(
                path.read_text(encoding="utf-8-sig"), source=str(path), rule_id=rule_id)
        if action == "create" and existing is not None and not overwrite:
            return tool_error(f"Rule already exists: {path}. Pass overwrite=true to replace it.",
                              success=False)
        if action == "update" and existing is None:
            return tool_error(f"Rule not found: {path}", success=False)
        if content is not None:
            text = content if content.endswith("\n") else content + "\n"
        else:
            base_meta = existing["metadata"] if existing else {}
            base_body = existing["body"] if existing else ""
            merged = {**base_meta, **(metadata or {})}
            text = rule_utils.format_rule(merged, body if body is not None else base_body)
        rule_utils.write_rule(rule_id, text, overwrite=True)
    except rule_utils.RuleError as exc:
        return tool_error(str(exc), success=False)
    except OSError as exc:
        return tool_error(f"Cannot write rule: {exc}", success=False)
    return _json({"success": True, "action": action, "rule_id": rule_id, "path": str(path)})


registry.register(
    name="rules_list", toolset="rules", schema=RULES_LIST_SCHEMA, emoji="📜",
    handler=lambda args, **kw: rules_list(
        scope=args.get("scope", ""), agent_name=args.get("agent_name", "")),
    max_result_size_chars=120_000,
)
registry.register(
    name="rule_view", toolset="rules", schema=RULE_VIEW_SCHEMA, emoji="📜",
    handler=lambda args, **kw: rule_view(args.get("rule_id", "")),
    max_result_size_chars=120_000,
)
registry.register(
    name="rule_manage", toolset="rules", schema=RULE_MANAGE_SCHEMA, emoji="📜",
    handler=lambda args, **kw: rule_manage(
        action=args.get("action", ""), rule_id=args.get("rule_id", ""),
        content=args.get("content"), metadata=args.get("metadata"),
        body=args.get("body"), overwrite=bool(args.get("overwrite", False)),
        confirm=bool(args.get("confirm", False))),
)
