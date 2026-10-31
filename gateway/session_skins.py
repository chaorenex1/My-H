"""Theme panel: ``skin.list`` reads the profile's installed Hermes skins.

The omp renderer's own themes stay client-side; this serves the Hermes skin engine (built-in and
``<profile>/skins/*.yaml``). Switching still goes through ``config.set{key: "skin"}``.
"""

from __future__ import annotations

from functools import partial

from gateway.session_promoted import require, scoped
from hermes_state_runtime import RuntimeStoreError


def handlers(connection):
    return {"skin.list": partial(list_skins, connection)}


def _profile(params):
    profile = params.get("profile")
    if profile is not None and not isinstance(profile, str):
        raise RuntimeStoreError("invalid_params", field="profile")


async def list_skins(connection, ref, params):
    _profile(params)
    require(connection, "session:read")

    def read():
        from hermes_cli.config import load_config
        from hermes_cli.skin_engine import list_skins as installed, load_skin

        display = (load_config() or {}).get("display") or {}
        current = str(display.get("skin") or "default") if isinstance(display, dict) else "default"
        rows = []
        for entry in installed():
            name = str(entry.get("name") or "")
            if not name:
                continue
            skin = load_skin(name)
            rows.append({
                "name": name,
                "display": str(entry.get("description") or name),
                "colors": dict(skin.colors),
                "dark": dict(skin.dark_colors),
                "light": dict(skin.light_colors),
            })
        return {"current": current, "skins": rows}

    return await scoped(connection, read)
