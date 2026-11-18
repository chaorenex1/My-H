"""Agent Hub role settings: ``subagent.config`` stores per-agent custom configuration.

The row lives in the profile's ``state.db`` (``agent_config``), keyed by ``(agent_id, source)``.
The source is the named-agent definition's prompt path, resolved with the ``agent`` tool's own
scan (``tools.agent_tool.discover_agent_definitions``): the session's project/running directory
first, then the profile (``HERMES_HOME``), then the installation. That keeps scopes distinct
without writing any YAML; an explicit ``source`` (or ``prompt_path``) pins one of them.
"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path

from gateway.session_promoted import require, scoped
from hermes_state_runtime import RuntimeStoreError


def handlers(connection):
    return {"subagent.config": partial(configure, connection)}


def _text(value, field):
    if value is not None and not isinstance(value, str):
        raise RuntimeStoreError("invalid_params", field=field)
    return value


def _toggle(value, field):
    if value is not None and not isinstance(value, bool):
        raise RuntimeStoreError("invalid_params", field=field)
    return value


def _result(row: dict, subagent_id: str, source: str) -> dict:
    def flag(name):
        value = row.get(name)
        return None if value is None else bool(value)

    return {
        "subagent_id": subagent_id,
        "source": source,
        "model": row.get("model"),
        "provider": row.get("provider"),
        "prewalk": flag("prewalk"),
        "advisor": flag("advisor"),
        "thinking": row.get("reasoning_effort"),
        "pattern": row.get("pattern"),
        "prompt_path": row.get("prompt_path") or source,
        "updated_at": row.get("updated_at"),
    }


def _root(connection, ref, params, home: str) -> str:
    """The scope's starting directory: the live session's workspace, else the profile home."""
    if not ref.session_id:
        return home
    if ref.session_id not in connection.authority.sessions:
        raise RuntimeStoreError("not_found")
    from gateway.session_busy_controls import authorize

    authorize(connection, ref, params, "session:control")
    from gateway.session_policy import policy_for_source

    live = connection.authority.sessions[ref.session_id]
    policy = policy_for_source(connection.authority.runner, live.source)
    return policy.cwd if policy is not None else home


def _source(cwd: str, home: str, subagent_id: str, explicit, prompt_path) -> str:
    """The definition's prompt path that owns this agent id, or the scope a new one belongs to."""
    if explicit is not None:
        return explicit.strip()
    if prompt_path:
        return prompt_path.strip()
    from tools.agent_tool import discover_agent_definitions

    for definition in discover_agent_definitions(cwd):
        if definition["agent_id"] == subagent_id or definition["name"] == subagent_id:
            return definition["source"]
    base = cwd if cwd and os.path.abspath(cwd) != os.path.abspath(home) else home
    return str(Path(base) / "agents" / f"{subagent_id}.md")


async def configure(connection, ref, params):
    subagent_id = params.get("subagent_id")
    if not isinstance(subagent_id, str) or not subagent_id:
        raise RuntimeStoreError("invalid_params", field="subagent_id")
    profile = _text(params.get("profile"), "profile")
    if profile is not None:
        from hermes_cli.profiles import profile_matches_home

        if not profile_matches_home(profile, Path(connection.authority.profile_id)):
            raise RuntimeStoreError("profile_mismatch")
    explicit_source = _text(params.get("source"), "source")
    prompt_path = _text(params.get("prompt_path"), "prompt_path")
    model = _text(params.get("model"), "model")
    provider = _text(params.get("provider"), "provider")
    pattern = _text(params.get("pattern"), "pattern")
    thinking = _text(params.get("thinking"), "thinking")
    advisor = _toggle(params.get("advisor"), "advisor")
    prewalk = _toggle(params.get("prewalk"), "prewalk")
    if thinking and thinking.strip():
        from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort

        if parse_reasoning_effort(thinking) is None:
            raise RuntimeStoreError("invalid_params", field="thinking",
                                    detail=f"use none or one of: {', '.join(VALID_REASONING_EFFORTS)}")
    require(connection, "session:control")

    # Key PRESENCE is the write: an explicit null / empty string clears the column, an omitted
    # key leaves it untouched.
    updates: dict = {}
    for param, column, value in (("model", "model", model), ("provider", "provider", provider),
                                 ("thinking", "reasoning_effort", thinking), ("pattern", "pattern", pattern),
                                 ("prompt_path", "prompt_path", prompt_path)):
        if param in params:
            updates[column] = (value or "").strip() or None
    if "advisor" in params:
        updates["advisor"] = advisor
    if "prewalk" in params:
        updates["prewalk"] = prewalk

    def write():
        home = str(connection.authority.profile_id)
        cwd = _root(connection, ref, params, home)
        source = _source(cwd, home, subagent_id, explicit_source, prompt_path)
        if updates:
            # The row records the definition path it configures.
            updates["prompt_path"] = source
        db = connection.authority.db
        row = db.upsert_agent_config(subagent_id, updates, source) if updates else db.get_agent_config(
            subagent_id, source)
        return _result(row or {}, subagent_id, source)

    return await scoped(connection, write)
