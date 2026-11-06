"""``tool_output.artifacts`` — session tool-output archive settings (ADR-16 D3).

The archive writes what each tool call actually put in front of the model
(``<step>.<tool>.log``) and, only when the wrapper had to truncate, the pre-truncation text
(``<step>.<tool>-original.log``). This module only resolves the config; P1 owns the writer.

Reads are forgiving by design: a missing section, a malformed value, or an unreadable config
falls back to the defaults instead of failing a tool call. Values are cached per profile home
because one multiplexed gateway process serves several profiles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hermes_constants import hermes_home_key

DEFAULT_ENABLED = True
DEFAULT_REDACT = True
#: Contract-shaped / already self-limited results are never archived twice.
DEFAULT_EXCLUDE_TOOLS: tuple[str, ...] = ("read_file", "todo", "agent_out")

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class ArtifactSettings:
    enabled: bool
    redact: bool
    exclude_tools: tuple[str, ...]


_settings_cache: dict[str, ArtifactSettings] = {}


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return default


def _coerce_tool_names(value: Any) -> tuple[str, ...]:
    """A list of tool names, or one bare name; malformed entries are dropped, order kept."""
    if isinstance(value, str):
        candidates: tuple[Any, ...] = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        candidates = tuple(value)
    else:
        return DEFAULT_EXCLUDE_TOOLS
    if not candidates:
        return ()
    names: list[str] = []
    for item in candidates:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _resolve(section: Any) -> ArtifactSettings:
    if not isinstance(section, dict):
        section = {}
    return ArtifactSettings(
        enabled=_coerce_bool(section.get("enabled"), DEFAULT_ENABLED),
        redact=_coerce_bool(section.get("redact"), DEFAULT_REDACT),
        exclude_tools=_coerce_tool_names(section.get("exclude_tools")),
    )


def get_artifact_settings() -> ArtifactSettings:
    """Resolved ``tool_output.artifacts``; never raises. Cached per profile home only when the
    config loaded — a transient load failure is not cached, so a later call can recover."""
    key = hermes_home_key()
    cached = _settings_cache.get(key)
    if cached is not None:
        return cached
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        section = cfg.get("tool_output") if isinstance(cfg, dict) else None
        if isinstance(section, dict):
            section = section.get("artifacts")
    except Exception:  # health: allow BLE001 -- config is optional; a broken config must not fail a tool call
        return _resolve(None)
    settings = _resolve(section)
    _settings_cache[key] = settings
    return settings


def _reset_artifact_settings_cache() -> None:
    """Drop the cached settings (tests, and after a config hot-reload)."""
    _settings_cache.clear()


def is_excluded_tool(tool_name: Any, settings: ArtifactSettings | None = None) -> bool:
    """Whether ``tool_name`` is in the configured exclusion list (case-sensitive, exact)."""
    resolved = settings or get_artifact_settings()
    return str(tool_name or "") in resolved.exclude_tools
