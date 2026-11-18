"""Load/save canonicalization steps split out of :mod:`hermes_cli.config`.

The facade composes these with the model-section canon; keeping the relocation step here keeps
``config.py`` inside its code-health line budget.
"""

from __future__ import annotations

from typing import Any


def normalize_max_turns_config(config: dict[str, Any]) -> dict[str, Any]:
    """Move legacy root-level ``max_turns`` under ``agent``; the schema default is injected only
    when the user set max_turns somewhere (so save_config can otherwise omit it)."""
    config = dict(config)
    agent_config = dict(config.get("agent") or {})
    if "max_turns" in config and "max_turns" not in agent_config:
        agent_config["max_turns"] = config["max_turns"]
    if agent_config or "agent" in config:  # a sparse save must not grow an `agent: {}` section
        config["agent"] = agent_config
    config.pop("max_turns", None)
    return config
