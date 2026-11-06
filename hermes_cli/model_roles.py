"""``@role`` model selectors: omp's ``modelRoles`` behind ``model: "@role"``.

A named-agent definition (or a caller) may pin a model three ways: ``provider:model``, a bare
model name, or ``@role``. A role is resolved here, at call time, against the profile's live
configuration:

* ``@default`` — the host's own route (empty provider/model, inherits the parent conversation);
* ``@task`` / ``@advisor`` — omp's imported roles (``delegation:`` / ``advisor:``);
* ``@<auxiliary task>`` — an ``auxiliary.<task>`` slot from :data:`AUX_TASK_KEYS`.

Fail closed: an unknown role raises before a run is constructed, so a typo never falls back
silently to the host model. Resolution is per call, so changing ``auxiliary.<task>`` takes effect
without editing a definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hermes_cli.aux_slots import AUX_TASK_KEYS, IMPORTED_ROLE_IDS, imported_role_target

#: The role that means "no pin": inherit the host conversation's route.
DEFAULT_ROLE = "default"


@dataclass(frozen=True)
class ModelRoute:
    """One resolved selector: where the run goes and the knobs the role owns."""

    provider: str = ""
    model: str = ""
    base_url: str = ""
    #: The slot's own credential for that endpoint (auxiliary.<task>.api_key); a route pinned to
    #: a base URL needs it or the provider ladder would resolve the wrong endpoint's key.
    api_key: str = ""
    #: A level string, a config dict (``{"enabled": ..., "effort": ...}``) or ``""``; the run
    #: parses it through ``hermes_constants.parse_reasoning_effort``.
    reasoning_effort: Any = ""
    fallback_chain: tuple[dict[str, str], ...] = ()
    #: The ``@role`` name this route came from; ``""`` for a literal ``model`` selector.
    role: str = ""


def role_names() -> tuple[str, ...]:
    """Every role a selector may name, sorted for a stable error message."""
    return tuple(sorted({DEFAULT_ROLE, *IMPORTED_ROLE_IDS, *AUX_TASK_KEYS}))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _split_route(text: str) -> tuple[str, str]:
    """``(provider, model)`` for a literal selector; a bare value pins only the model."""
    provider, sep, model = text.partition(":")
    if not sep:
        return "", text
    provider, model = provider.strip(), model.strip()
    if not provider or not model:
        raise ValueError(
            f"model must be 'provider:model', a bare model name or '@role', got {text!r}")
    return provider, model


def _config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly() or {}
    except Exception:  # health: allow BLE001 -- an unreadable config means "inherit"
        return {}


def _aux_route(role: str) -> ModelRoute:
    """One ``auxiliary.<task>`` slot, read through the same seam every aux call uses."""
    from agent.auxiliary_task_config import _get_auxiliary_task_config
    from hermes_cli.aux_fallback import public_fallback_chain

    block = _get_auxiliary_task_config(role)
    provider = _text(block.get("provider"))
    effort = block.get("reasoning_effort")
    return ModelRoute(
        provider="" if provider.lower() == "auto" else provider,
        model=_text(block.get("model")),
        base_url=_text(block.get("base_url")),
        api_key=_text(block.get("api_key")),
        reasoning_effort="" if effort is None else effort,
        fallback_chain=tuple(public_fallback_chain(block.get("fallback_chain"))),
        role=role,
    )


def _imported_route(role: str) -> ModelRoute:
    from hermes_cli.imported_roles import read_imported_route

    cfg = _config()
    route = read_imported_route(cfg, role)
    section = cfg.get(imported_role_target(role))
    provider = _text(route.get("provider"))
    return ModelRoute(
        # imported_roles' "auto" is the empty provider (inherit the parent route); passing the
        # literal would skip the config's own provider in resolve_requested_provider.
        provider="" if provider.lower() == "auto" else provider,
        model=_text(route.get("model")),
        base_url=_text(route.get("base_url")),
        # ``read_imported_route`` is the credential-free wire shape; the endpoint's key lives on
        # the section itself (``delegation.api_key`` / ``advisor.api_key``).
        api_key=_text(section.get("api_key")) if isinstance(section, dict) else "",
        reasoning_effort=route.get("reasoning_effort") or "",
        fallback_chain=tuple(route.get("fallback_chain") or ()),
        role=role,
    )


def resolve_model_selector(value: Any) -> ModelRoute:
    """Resolve one ``model`` selector; an unknown role is a :class:`ValueError` naming the roles.

    A list is refused rather than half-read: v1 takes exactly one selector per definition.
    """
    if isinstance(value, (list, tuple)):
        raise ValueError("model takes one selector, not a list; use '@role' or 'provider:model'")
    text = _text(value)
    if not text:
        return ModelRoute()
    if not text.startswith("@"):
        provider, model = _split_route(text)
        return ModelRoute(provider=provider, model=model)
    role = text[1:].strip()
    if not role:
        raise ValueError("model selector '@' names no role; use '@default', '@task', '@advisor' "
                         "or '@<auxiliary task>'")
    level = ""
    if ":" in role:
        from hermes_constants import parse_reasoning_effort

        role, _, level = role.partition(":")
        role, level = role.strip(), level.strip()
        if not role or not level or parse_reasoning_effort(level) is None:
            raise ValueError(f"model selector {text!r} has an unknown reasoning level "
                             f"{level!r}")
    if role == DEFAULT_ROLE:
        route = ModelRoute(role=role)
    elif role in IMPORTED_ROLE_IDS:
        route = _imported_route(role)
    elif role in AUX_TASK_KEYS:
        route = _aux_route(role)
    else:
        raise ValueError(f"unknown model role '@{role}'; available: "
                         + ", ".join(f"@{name}" for name in role_names()))
    if level:
        from dataclasses import replace

        route = replace(route, reasoning_effort=level)
    return route


__all__ = ["DEFAULT_ROLE", "ModelRoute", "resolve_model_selector", "role_names"]
