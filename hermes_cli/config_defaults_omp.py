"""omp-rendered TUI defaults, mirrored from omp-ui's setting registry.

The ``omp-ui`` shell owns its Appearance page: the ids, types and current defaults live in
``omp-ui/src/config``. Hermes owns the *profile's* values, which config.yaml stores under
``omp.appearance``; this module carries omp's defaults across the boundary so
``DEFAULT_CONFIG``/``config.schema`` and ``omp.appearance.get`` can present every setting before a
profile saves one.

``data/omp_appearance_defaults.json`` is generated — never hand-edit it:

    python scripts/gen_omp_appearance_defaults.py

``omp-ui/src/modes/rpc/gateway-appearance.test.ts`` fails when the committed copy drifts from the
shell's registry.
"""

from __future__ import annotations

import json
from pathlib import Path

_DEFAULTS_PATH = Path(__file__).with_name("data") / "omp_appearance_defaults.json"


def _load_defaults() -> dict[str, object]:
    with _DEFAULTS_PATH.open(encoding="utf-8") as handle:
        values = json.load(handle)
    if not isinstance(values, dict) or not values:
        raise ValueError(f"{_DEFAULTS_PATH} must contain a non-empty setting-id → default object")
    return values


#: Flat ``setting id → default`` map, exactly as omp registers the Appearance tab.
OMP_APPEARANCE_DEFAULTS: dict[str, object] = _load_defaults()


def _nest(flat: dict[str, object]) -> dict:
    """Dotted ids → the nested mapping shape config.yaml/``DEFAULT_CONFIG`` use."""
    nested: dict = {}
    for dotted, value in flat.items():
        node = nested
        *parents, leaf = dotted.split(".")
        for part in parents:
            child = node.get(part)
            if not isinstance(child, dict):
                child = node[part] = {}
            node = child
        node[leaf] = value
    return nested


#: ``DEFAULT_CONFIG["omp"]["appearance"]``: omp's defaults in config.yaml's nested shape.
OMP_APPEARANCE_CONFIG: dict = _nest(OMP_APPEARANCE_DEFAULTS)

#: The ``omp`` root ``DEFAULT_CONFIG`` exposes (its Appearance page is the only part today).
OMP_CONFIG_SECTION: dict = {"omp": {"appearance": OMP_APPEARANCE_CONFIG}}


def flatten_appearance(node: dict, prefix: str = "") -> dict[str, object]:
    """One config.yaml ``appearance`` mapping → dotted setting ids (values, not empty branches)."""
    flat: dict[str, object] = {}
    for raw_key, value in node.items():
        key = str(raw_key)
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            flat.update(flatten_appearance(value, path))
        elif value is not None:
            flat[path] = value
    return flat


def effective_appearance(section: dict | None) -> dict[str, object]:
    """Every Appearance setting: omp's default, overridden by the profile's stored value."""
    stored = flatten_appearance(section if isinstance(section, dict) else {})
    return {**OMP_APPEARANCE_DEFAULTS, **stored}
