"""Static scoped agent definitions for Agent Hub, merged with their dynamic config.

The definitions are the named-agent prompt files the ``agent`` tool itself loads
(``tools.agent_tool.discover_agent_definitions``): project (running directory / ``.agents`` /
``agents`` / skills / plugins), profile (``HERMES_HOME``), then installation. Dynamic values live
in the profile ``state.db`` ``agent_config`` table keyed by ``(agent_id, source)`` where ``source``
is the definition's prompt path. A row with no current definition still appears
(``scope="custom"``), so saved configuration is never hidden.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _scope_for_source(source: str, project_dir: str) -> str:
    if not source or not os.path.isabs(source):
        return "custom"
    try:
        target = Path(source).resolve()
        project = Path(project_dir).resolve()
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home()).resolve()
        if project != home and (target == project or project in target.parents):
            return "project"
        if target == home or home in target.parents:
            return "profile"
    except (OSError, RuntimeError):
        pass
    return "custom"


def _dynamic(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    return {
        "model": row.get("model"),
        "provider": row.get("provider"),
        "advisor": None if row.get("advisor") is None else bool(row["advisor"]),
        "prewalk": None if row.get("prewalk") is None else bool(row["prewalk"]),
        "thinking": row.get("reasoning_effort"),
        "pattern": row.get("pattern"),
        "prompt_path": row.get("prompt_path"),
    }


def _merged(definition: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "agent_id": definition["agent_id"],
        "name": definition["name"],
        "source": definition["source"],
        "scope": definition["scope"],
        "enabled": None,
        "model": definition.get("model"),
        "provider": definition.get("provider"),
        "thinking": definition.get("thinking"),
        "prompt_path": definition["source"],
        "config": _dynamic(row),
    }


def _config_only(row: dict[str, Any], project_dir: str) -> dict[str, Any]:
    source = str(row.get("source") or "")
    return {
        "agent_id": str(row.get("agent_id")),
        "name": str(row.get("agent_id")),
        "source": source,
        "scope": _scope_for_source(source, project_dir),
        "enabled": None,
        "model": None,
        "provider": None,
        "thinking": None,
        "prompt_path": source,
        "config": _dynamic(row),
    }


def list_agent_definitions(project_dir: str, db: Any = None) -> list[dict[str, Any]]:
    """Static named-agent definitions plus config-only rows, each merged with ``agent_config``."""
    from tools.agent_tool import discover_agent_definitions

    rows = [dict(row) for row in (db.list_agent_configs() if db is not None else [])]
    used: set[int] = set()

    def take(agent_id: str, source: str) -> dict[str, Any] | None:
        for index, row in enumerate(rows):
            if index in used:
                continue
            if str(row.get("agent_id")) == agent_id and str(row.get("source") or "") == source:
                used.add(index)
                return row
        return None

    agents: list[dict[str, Any]] = []
    for definition in discover_agent_definitions(project_dir):
        row = take(definition["agent_id"], definition["source"])
        if row is None:
            row = take(definition["name"], definition["source"])
        agents.append(_merged(definition, row))

    for index, row in enumerate(rows):
        if index not in used:
            agents.append(_config_only(row, project_dir))
    return agents
