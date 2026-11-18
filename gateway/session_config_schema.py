"""Settings panel: ``config.schema`` serves the same flattened schema the dashboard renders.

The dashboard's builder (``hermes_cli.web_server_config``) already walks ``DEFAULT_CONFIG`` and
applies select options / dynamic provider choices; this reuses it instead of hard-coding keys in
the client. Reads only — writes stay on ``config.set``.
"""

from __future__ import annotations

from functools import partial

from gateway.session_promoted import require, scoped
from hermes_state_runtime import RuntimeStoreError

_TYPES = frozenset({"string", "boolean", "number", "list", "object", "select"})


def handlers(connection):
    return {"config.schema": partial(config_schema, connection)}


async def config_schema(connection, ref, params):
    profile = params.get("profile")
    if profile is not None and not isinstance(profile, str):
        raise RuntimeStoreError("invalid_params", field="profile")
    require(connection, "session:read")

    def read():
        from hermes_cli.config import DEFAULT_CONFIG, cfg_get
        from hermes_cli.web_server_config import _schema_with_dynamic_provider_options

        settings = []
        for key, entry in _schema_with_dynamic_provider_options().items():
            if key.startswith("_"):
                continue
            kind = str(entry.get("type") or "string")
            if kind not in _TYPES:
                kind = "string"
            options = entry.get("options")
            settings.append({
                "key": key,
                "type": kind,
                "default": cfg_get(DEFAULT_CONFIG, *key.split(".")),
                "enum": [str(option) for option in options] if isinstance(options, list) else None,
                "description": str(entry.get("description") or ""),
                "category": str(entry.get("category") or "general"),
                "scope": "profile",
                # Hermes re-reads settings per turn; no schema field declares a restart requirement.
                "restart_required": False,
            })
        return {"settings": settings}

    return await scoped(connection, read)
