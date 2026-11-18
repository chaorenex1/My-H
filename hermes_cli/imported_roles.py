"""The two omp model roles Hermes backs outside ``auxiliary``.

omp's ``task`` role is Hermes' ``delegation:`` section (the delegated-child route) and ``advisor``
is ``advisor:`` (the review model). ``model.roles.*`` and ``model.presets.*`` read and write them
through this module so the wire never grows a second copy of the mapping; ``aux_slots.py`` owns the
role ids, labels and omp tags, ``aux_fallback.py`` the chain shapes both roles share.

"Auto" is ``provider: ""`` / ``model: ""`` for these roles: the child inherits the parent's route
and the advisor falls back to its own ``WATCHDOG.yml`` entries. Hermes has no advisor fallback chain
or reasoning-effort override, so writes for those two knobs are refused instead of silently dropped.
"""

from __future__ import annotations

from typing import Any

#: Absent from a route dict: the caller did not submit the knob, so the stored value stays.
_UNSET = object()


def _section(cfg: dict, name: str) -> dict:
    block = cfg.get(name)
    return block if isinstance(block, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _provider(value: Any) -> str:
    """An imported role's provider: omp's ``"auto"`` is the empty provider (inherit the parent)."""
    provider = _text(value)
    return "" if provider.lower() == "auto" else provider


def imported_fallback_chain(cfg: dict) -> list[dict[str, str]]:
    """The ``task`` role's chain: ``delegation.fallback_providers`` in the shared route shape."""
    from hermes_cli.aux_fallback import public_fallback_chain

    return public_fallback_chain(_section(cfg, "delegation").get("fallback_providers"))


def read_imported_route(cfg: dict, role: str) -> dict:
    """One imported role's stored route in the same shape a ``model.roles.get`` row uses."""
    if role == "task":
        section = _section(cfg, "delegation")
        return {
            "provider": _text(section.get("provider")),
            "model": _text(section.get("model")),
            "base_url": _text(section.get("base_url")),
            "reasoning_effort": _text(section.get("reasoning_effort")) or None,
            "fallback_chain": imported_fallback_chain(cfg),
        }
    if role == "advisor":
        section = _section(cfg, "advisor")
        return {
            "provider": _text(section.get("provider")),
            "model": _text(section.get("model")),
            "base_url": "",
            "reasoning_effort": None,
            "fallback_chain": [],
        }
    raise KeyError(role)


def _write_task(cfg: dict, route: dict, *, clear: bool, exact: bool = False) -> None:
    from hermes_cli.aux_fallback import normalize_fallback_chain

    section = dict(_section(cfg, "delegation"))
    previous_provider = _text(section.get("provider")).lower()
    previous_base_url = _text(section.get("base_url"))
    provider = "" if clear else _provider(route.get("provider"))
    base_url = "" if clear else _text(route.get("base_url"))
    section["provider"] = provider
    section["model"] = "" if clear else _text(route.get("model"))
    if clear:
        section.pop("base_url", None)
        section.pop("api_key", None)
    elif base_url:
        section["base_url"] = base_url
        api_key = _text(route.get("api_key"))
        if api_key:
            section["api_key"] = api_key
        elif exact and base_url != previous_base_url:
            # A preset snapshots routes, never credentials. Keep the key that already belongs to
            # this endpoint; drop it only when the endpoint itself moved (it would not be accepted
            # by the new host), which is the case the previous "exact" rule was really about.
            section.pop("api_key", None)
    elif exact or provider.lower() != previous_provider:
        # ``delegation.api_key`` only keys ``delegation.base_url``; a cleared endpoint drops it.
        section.pop("base_url", None)
        section.pop("api_key", None)
    if clear:
        section.pop("reasoning_effort", None)
        # ``null`` = inherit the parent chain, which is what "auto" means for this role.
        section["fallback_providers"] = None
    else:
        effort = route.get("reasoning_effort", _UNSET)
        if effort is not _UNSET:
            if effort is None:
                section.pop("reasoning_effort", None)
            else:
                section["reasoning_effort"] = _text(effort)
        chain = route.get("fallback_chain", _UNSET)
        if chain is not _UNSET:
            section["fallback_providers"] = [dict(entry) for entry in normalize_fallback_chain(chain)]
    cfg["delegation"] = section


def advisor_route_problem(route: dict) -> str:
    """Why Hermes cannot store *route*'s extra knobs on ``advisor`` (``""`` when it can).

    A preset snapshot carries ``None``/``[]`` for these (no override); an actual value is a write
    Hermes cannot store, so it is refused instead of dropped silently.
    """
    effort = route.get("reasoning_effort", _UNSET)
    if effort is not _UNSET and effort is not None:
        return ("advisor has no reasoning_effort override in Hermes "
                "(advisor config stores only provider/model)")
    chain = route.get("fallback_chain", _UNSET)
    if chain is not _UNSET and chain:
        return ("advisor has no fallback chain in Hermes "
                "(advisor runs the one configured review model)")
    return ""


def _write_advisor(cfg: dict, route: dict, *, clear: bool) -> None:
    if not clear:
        problem = advisor_route_problem(route)
        if problem:
            raise ValueError(problem)
    section = dict(_section(cfg, "advisor"))
    section["provider"] = "" if clear else _provider(route.get("provider"))
    section["model"] = "" if clear else _text(route.get("model"))
    cfg["advisor"] = section


def write_imported_route(cfg: dict, role: str, route: dict, *, clear: bool = False,
                         exact: bool = False) -> None:
    """Apply one imported role's route to *cfg* in place (the caller owns the config lock + save).

    ``exact`` (preset apply) makes a route describe the whole endpoint: a route without
    ``base_url`` clears a stored one even when the provider is unchanged. Raises
    :class:`ValueError` for a knob Hermes cannot store on that role (advisor only).
    """
    if role == "task":
        _write_task(cfg, route, clear=clear, exact=exact)
    elif role == "advisor":
        _write_advisor(cfg, route, clear=clear)
    else:
        raise KeyError(role)
