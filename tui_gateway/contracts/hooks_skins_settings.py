"""Extensions / theme / settings panel contracts: shell hooks, skins and the flattened config schema.

All three are profile-scoped (no session state); the handlers are ``gateway/session_hooks.py``,
``gateway/session_skins.py`` and ``gateway/session_config_schema.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import JsonValue, Result
from .common import ProfileParams
from .registry import canonical_method

# ── hooks.* ────────────────────────────────────────────────────────────────────────────────────


class HooksListParams(ProfileParams):
    session_id: str | None = None


class HookRow(Result):
    name: str
    event: str
    enabled: bool
    scope: str = "profile"
    source: str = "config.yaml"
    description: str = ""


class HooksListResult(Result):
    hooks: list[HookRow] = Field(default_factory=list)


canonical_method("hooks.list", params=HooksListParams, result=HooksListResult,
                 doc="The profile's configured shell hooks and whether each is approved to run.")


class HookToggleParams(ProfileParams):
    name: str
    enabled: bool
    request_id: str
    session_id: str | None = None


class HookToggleResult(Result):
    name: str
    enabled: bool


canonical_method("hooks.toggle", params=HookToggleParams, result=HookToggleResult,
                 doc="Approve or revoke one configured shell hook (the allowlist is the enabled state).")


# ── skin.list ──────────────────────────────────────────────────────────────────────────────────


class SkinListParams(ProfileParams):
    pass


class SkinEntry(Result):
    name: str
    display: str = ""
    colors: dict[str, str] = Field(default_factory=dict)
    dark: dict[str, str] = Field(default_factory=dict)
    light: dict[str, str] = Field(default_factory=dict)


class SkinListResult(Result):
    current: str = "default"
    skins: list[SkinEntry] = Field(default_factory=list)


canonical_method("skin.list", params=SkinListParams, result=SkinListResult,
                 doc="Installed Hermes skins and the active one; ``config.set{key:\"skin\"}`` switches.")


# ── config.schema ──────────────────────────────────────────────────────────────────────────────


class ConfigSchemaParams(ProfileParams):
    pass


class ConfigSetting(Result):
    key: str
    type: Literal["string", "boolean", "number", "list", "object", "select"]
    default: JsonValue | None = None
    enum: list[str] | None = None
    description: str = ""
    category: str = "general"
    scope: str = "profile"
    restart_required: bool = False


class ConfigSchemaResult(Result):
    settings: list[ConfigSetting] = Field(default_factory=list)


canonical_method("config.schema", params=ConfigSchemaParams, result=ConfigSchemaResult,
                 doc="Flattened settings schema so a client renders typed controls; writes still use config.set.")
