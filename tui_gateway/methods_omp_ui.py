"""``omp.appearance.*``: the omp-ui shell's Appearance panel is profile Hermes config.

Every setting on the panel is an ordinary Hermes config key under one section, nested by the
shell's setting id::

    omp:
      appearance:
        theme:
          dark: titanium
        symbolPreset: nerd

The defaults in ``DEFAULT_CONFIG`` are omp's own registry values
(``hermes_cli/config_defaults_omp.py``); config.yaml stores only the profile's overrides.
``omp.appearance.get`` therefore answers every setting (default, then stored value), flattened to
dotted ids (``theme.dark``), and the shell applies them as runtime setting overrides at boot.
``omp.appearance.set`` writes one setting (``value: null`` removes it) through the config backend,
so a remote backend's locked key is refused instead of silently dropped. Bodies are rebound onto
server.py's globals (method_ctx.bind_module) and reference them bare.
"""

from __future__ import annotations

import logging
import re

from .method_ctx import HandlerRegistry, bind_module

logger = logging.getLogger(__name__)

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

_SECTION = ("omp", "appearance")
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_MAX_KEY_SEGMENTS = 8


def _valid_key(key: str) -> bool:
    segments = key.split(".")
    return len(segments) <= _MAX_KEY_SEGMENTS and all(_SEGMENT.fullmatch(segment) for segment in segments)


def _appearance_section() -> dict:
    node = _load_cfg()
    for part in _SECTION:
        node = node.get(part) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


@method("omp.appearance.get")
@_profile_scoped
def _(rid, params: dict) -> dict:
    from hermes_cli.config_defaults_omp import effective_appearance

    try:
        values = effective_appearance(_appearance_section())
    except Exception as e:  # deliberate boundary: a broken defaults copy is a typed refusal, not a crash
        logger.exception("omp.appearance.get could not build the effective appearance")
        return _err(rid, 5001, str(e))
    return _ok(rid, {"values": values})


@method("omp.appearance.set")
@_profile_scoped
def _(rid, params: dict) -> dict:
    key = str(params.get("key") or "")
    if not _valid_key(key):
        return _err(rid, 4002, f"invalid omp-ui appearance key: {key!r}")
    value = params.get("value")
    from hermes_cli.config_backend import Changes, write_changes

    dotted = ".".join((*_SECTION, key))
    try:
        if value is None:
            write_changes(_active_config_path(), Changes(unset=(dotted,)))
        else:
            write_changes(_active_config_path(), Changes(set={dotted: value}))
    except Exception as e:  # deliberate boundary: every backend write failure is a 4002 refusal
        logger.exception("omp.appearance.set failed for %s", dotted)
        return _err(rid, 4002, str(e))
    _drop_cfg_cache()
    return _ok(rid, {"key": key, "value": value})


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
