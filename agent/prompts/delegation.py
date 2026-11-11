"""Delegation policy resolver for section 09 (ADR-18 D2).

The tool-policy chapter is rendered from one explicit policy dict. This module is the
producer: config first, then an agent definition's front matter, with the host defaults
matching omp's Delegation switches. It only shapes prompt text — it never blocks a spawn.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

#: Host defaults; must match ``DEFAULT_CONFIG["delegation"]`` (the registered config keys), because
#: a definition field of the same name overrides them per agent. ``max_concurrency`` is the
#: ``agent_loop.max_workers`` pool size, never a second prompt-only cap.
DEFAULT_POLICY: dict[str, Any] = {
    "delegation_bias": "eager",
    "eager_tasks": False,
    "eager_tasks_always": False,
    "inline_first": True,
    "max_concurrency": 8,
}

#: Definition front-matter field -> policy key.
_DEFINITION_KEYS = {
    "delegation_bias": "delegation_bias",
    "eager_tasks": "eager_tasks",
    "eager_tasks_always": "eager_tasks_always",
    "inline_first": "inline_first",
}

#: config.yaml ``delegation.<key>`` -> policy key.
_CONFIG_KEYS = {
    "bias": "delegation_bias",
    "eager_tasks": "eager_tasks",
    "eager_tasks_always": "eager_tasks_always",
    "inline_first": "inline_first",
}


def _bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"delegation.{key} must be a boolean, got {value!r}")


def _worker_limit(config: Mapping[str, Any]) -> int:
    """The prompt's concurrency line is the agent pool's worker count, not a second cap."""
    raw = (config.get("agent_loop") or {}).get("max_workers", 0)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def resolve_delegation_policy(
    config: Optional[Mapping[str, Any]] = None,
    *,
    definition: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Resolve the section-09 switches: defaults, then config, then *definition*.

    ``definition`` is an agent definition's parsed front matter. An invalid value raises
    ``ValueError`` instead of rendering a policy the runtime would not honor.
    """
    if config is None:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    raw = config.get("delegation") or {}
    policy = dict(DEFAULT_POLICY)
    for config_key, policy_key in _CONFIG_KEYS.items():
        if config_key not in raw:
            continue
        value = raw[config_key]
        policy[policy_key] = _bool(value, config_key) if isinstance(DEFAULT_POLICY[policy_key], bool) \
            else str(value).strip()
    for definition_key, policy_key in _DEFINITION_KEYS.items():
        if definition is None or definition_key not in definition:
            continue
        value = definition[definition_key]
        policy[policy_key] = _bool(value, definition_key) if isinstance(
            DEFAULT_POLICY[policy_key], bool) else str(value).strip()
    if policy["delegation_bias"] not in {"gated", "eager"}:
        raise ValueError(
            f"delegation.bias must be 'gated' or 'eager', got {policy['delegation_bias']!r}")
    policy["max_concurrency"] = _worker_limit(config)
    return policy


__all__ = ["DEFAULT_POLICY", "resolve_delegation_policy"]
