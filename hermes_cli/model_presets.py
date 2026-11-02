"""``model_presets``: named snapshots of the Models-hub role assignments (wire v2).

A preset is ``{<role id>: {provider, model, base_url, reasoning_effort, fallback_chain}}`` under
the profile config's ``model_presets`` section. Saving snapshots every role the hub can assign —
the built-in picker slots plus the imported omp roles ``task`` and ``advisor`` (plugin-registered
tasks stay outside the domain: their keys come from whichever plugins are installed, so a preset
must not claim to restore them). Applying writes the snapshot back through the same writers
``model.roles.set`` uses and resets every other role in the domain to auto, matching omp's "a
preset describes the complete state" semantics.

``model.roles.set`` remains the single-role path; this module only composes it.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from hermes_cli.aux_slots import IMPORTED_ROLE_IDS, aux_picker_keys
from hermes_cli.imported_roles import (advisor_route_problem, read_imported_route,
                                       write_imported_route)

PRESET_NAME_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"
_PRESET_NAME_RE = re.compile(PRESET_NAME_PATTERN)


def validate_preset_name(raw: Any) -> str:
    """A preset name: 1–64 characters of ``[A-Za-z0-9._-]``. ``ValueError`` names the rule."""
    name = str(raw or "").strip()
    if not _PRESET_NAME_RE.match(name):
        raise ValueError("preset name must be 1-64 characters of [A-Za-z0-9._-]")
    return name


def preset_role_ids() -> tuple[str, ...]:
    """Every role a preset covers, in picker order: built-in slots, then imported omp roles."""
    return aux_picker_keys() + IMPORTED_ROLE_IDS


def presets_section(cfg: dict) -> dict[str, Any]:
    """The ``model_presets`` mapping (empty when unset or hand-edited to a non-mapping)."""
    section = cfg.get("model_presets")
    return dict(section) if isinstance(section, dict) else {}


def _aux_route(cfg: dict, role: str) -> dict:
    from hermes_cli.aux_fallback import public_fallback_chain

    aux = cfg.get("auxiliary")
    slot = aux.get(role) if isinstance(aux, dict) else None
    slot = slot if isinstance(slot, dict) else {}
    return {
        "provider": str(slot.get("provider") or "auto"),
        "model": str(slot.get("model") or ""),
        "base_url": str(slot.get("base_url") or ""),
        "reasoning_effort": str(slot.get("reasoning_effort") or "") or None,
        "fallback_chain": public_fallback_chain(slot.get("fallback_chain")),
    }


def normalize_route(role: str, route: dict) -> dict:
    """One route in its comparison/persisted shape; blanks collapse to the role's inherit value."""
    from hermes_cli.aux_fallback import public_fallback_chain

    raw = route if isinstance(route, dict) else {}
    provider = str(raw.get("provider") or "").strip()
    if role in IMPORTED_ROLE_IDS:
        # omp's "auto" for an imported role is the empty provider (inherit the parent route).
        if provider.lower() == "auto":
            provider = ""
    elif provider in ("", "auto"):
        provider = "auto"
    effort = raw.get("reasoning_effort")
    effort = None if effort in (None, "") else str(effort).strip() or None
    return {
        "provider": provider,
        "model": str(raw.get("model") or "").strip(),
        "base_url": str(raw.get("base_url") or "").strip(),
        "reasoning_effort": effort,
        "fallback_chain": public_fallback_chain(raw.get("fallback_chain")),
    }


def current_routes(cfg: dict, roles: Optional[Iterable[str]] = None) -> dict[str, dict]:
    """The effective assignments of *roles* (default: the preset domain), normalized."""
    routes: dict[str, dict] = {}
    for role in roles or preset_role_ids():
        raw = read_imported_route(cfg, role) if role in IMPORTED_ROLE_IDS else _aux_route(cfg, role)
        routes[role] = normalize_route(role, raw)
    return routes


def snapshot(cfg: dict) -> dict[str, dict]:
    """The full preset payload for *cfg*: every role in the domain with its effective route."""
    return current_routes(cfg)


def presets_summary(cfg: dict) -> dict:
    """``{"names": [...], "active": name}``; ``active`` is the preset equal to the live state."""
    presets = presets_section(cfg)
    names = sorted(str(name) for name in presets)
    current = current_routes(cfg)
    active = ""
    for name in names:
        stored = presets.get(name)
        if not isinstance(stored, dict):
            continue
        if all(normalize_route(role, stored.get(role) if isinstance(stored.get(role), dict) else {})
               == route for role, route in current.items()):
            active = name
            break
    return {"names": names, "active": active}


def _apply_role(cfg: dict, role: str, route: dict) -> None:
    if role in IMPORTED_ROLE_IDS:
        # A preset describes the complete route, so a route without an endpoint clears a stale one.
        write_imported_route(cfg, role, route, exact=True)
        return
    from hermes_cli.config import clear_model_endpoint_credentials
    from hermes_cli.web_server_config import _apply_aux_assignment_sync

    if not route["base_url"]:
        # The dashboard writer only drops an endpoint on a provider change; a restore is exact.
        aux = cfg.get("auxiliary")
        block = aux.get(role) if isinstance(aux, dict) else None
        if isinstance(block, dict):
            block.pop("base_url", None)
            clear_model_endpoint_credentials(block)
    _apply_aux_assignment_sync(
        cfg, route["provider"], route["model"], role, route["base_url"], "",
        reasoning_effort=route["reasoning_effort"], fallback_chain=route["fallback_chain"])


def _clear_role(cfg: dict, role: str) -> None:
    if role in IMPORTED_ROLE_IDS:
        write_imported_route(cfg, role, {}, clear=True)
        return
    from hermes_cli.config import clear_model_endpoint_credentials
    from hermes_cli.web_server_config import _apply_aux_assignment_sync

    aux = cfg.get("auxiliary")
    block = aux.get(role) if isinstance(aux, dict) else None
    if isinstance(block, dict):
        block.pop("base_url", None)
        clear_model_endpoint_credentials(block)
    _apply_aux_assignment_sync(cfg, "auto", "", role, "", "",
                               reasoning_effort=None, fallback_chain=[])


def validate_route(role: str, route: dict) -> dict:
    """A normalized route whose effort level Hermes can store for *role*.

    The advisor's unsupported knobs are left to :func:`write_imported_route` so its message names
    the reason; this rejects only a malformed level on a role that does store one.
    """
    normalized = normalize_route(role, route)
    effort = normalized["reasoning_effort"]
    if role != "advisor" and effort is not None:
        from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort

        if parse_reasoning_effort(effort) is None:
            raise ValueError(f"invalid reasoning_effort for {role}: "
                             f"{', '.join(VALID_REASONING_EFFORTS)}")
    return normalized


def apply_preset(cfg: dict, name: str) -> None:
    """Restore preset *name* onto *cfg* (in place); roles absent from the snapshot reset to auto.

    ``KeyError`` when the preset is unknown. The caller holds the config locks and saves.
    """
    presets = presets_section(cfg)
    if name not in presets:
        raise KeyError(name)
    stored = presets[name]
    stored = stored if isinstance(stored, dict) else {}
    # Validate every submitted route before the first write: an aux write saves immediately, so a
    # bad route discovered halfway would leave a partial apply on disk.
    routes: dict[str, dict] = {}
    for role in preset_role_ids():
        route = stored.get(role)
        if route is not None:
            routes[role] = validate_route(role, route)
            if role == "advisor":
                problem = advisor_route_problem(routes[role])
                if problem:
                    raise ValueError(problem)
    for role in preset_role_ids():
        route = routes.get(role)
        if route is None:
            _clear_role(cfg, role)
        else:
            _apply_role(cfg, role, route)


__all__ = [
    "PRESET_NAME_PATTERN",
    "apply_preset",
    "current_routes",
    "normalize_route",
    "preset_role_ids",
    "presets_section",
    "presets_summary",
    "snapshot",
    "validate_preset_name",
    "validate_route",
]
