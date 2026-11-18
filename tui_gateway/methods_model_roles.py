"""Models-hub Roles JSON-RPC handlers: Hermes config is the single truth.

``model.roles.get`` projects the picker slots (``aux_picker_keys()``), the imported omp roles Hermes
backs outside ``auxiliary`` (``task`` → ``delegation:``, ``advisor`` → ``advisor:``) and
plugin-registered tasks (``_plugin_aux_tasks()``) with the route each role stores in the addressed
profile's ``config.yaml``; built-in rows carry ``AUX_TASK_META``'s label/hint, imported and plugin
rows the ones their registry declares. Every row wears an omp tag from ``aux_slots.omp_role_tags``.

``model.roles.set`` writes one role: an auxiliary slot through the dashboard's own assignment
chokepoint (``_apply_aux_assignment_sync``), an imported role through
``hermes_cli/imported_roles.py`` — then re-reads the row from disk. ``model.cycle.set`` persists
``omp.cycle_order``, and ``model.presets.*`` composes the same writers over the
``model_presets`` config section. Nothing here touches the shell's own omp settings.

Bodies are rebound onto server.py's globals (``method_ctx.bind_module``) and reference them bare.
"""

from __future__ import annotations

import logging

from .method_ctx import HandlerRegistry, bind_module

logger = logging.getLogger(__name__)

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

# model.roles.* error codes. 5126/5127 are the pair reserved for the set path's validation
# failures; 5128 is the generic handler boundary (the log of last resort), like model.options' 5033.
_ROLE_UNKNOWN = 5126
_ROLE_INVALID = 5127
_ROLE_FAILED = 5128


def _catch(fail_code: int):
    """Handler body exceptions → ``_err(rid, fail_code, str(e))``."""

    def deco(body):
        def handler(rid, params: dict) -> dict:
            try:
                return body(rid, params)
            except Exception as e:
                logger.exception("model.roles handler failed")
                return _err(rid, fail_code, str(e))
        handler.__doc__ = body.__doc__
        return handler
    return deco


def _main_route(cfg: dict) -> dict:
    """(provider, model) from config's ``model`` section, which may be a plain string."""
    model_cfg = cfg.get("model", "")
    if isinstance(model_cfg, dict):
        return {"provider": str(model_cfg.get("provider") or ""),
                "model": str(model_cfg.get("default", model_cfg.get("name", "")) or "")}
    return {"provider": "", "model": str(model_cfg or "")}


def _default_thinking_level(cfg: dict) -> str:
    """``agent.reasoning_effort`` as the TUI's own ``config.get reasoning`` reports it."""
    raw_effort = (cfg.get("agent") or {}).get("reasoning_effort", "")
    if isinstance(raw_effort, dict):  # {enabled, effort} form: render the tier, never str(dict)
        from hermes_constants import parse_reasoning_effort

        parsed = parse_reasoning_effort(raw_effort) or {}
        raw_effort = False if parsed.get("enabled") is False else parsed.get("effort")
    # YAML `reasoning_effort: false` means thinking disabled, not "unset".
    return "none" if raw_effort is False else str(raw_effort or "medium")


def _cycle_order(cfg: dict) -> list[str]:
    """``omp.cycle_order`` as the hub reports it; ``[]`` when unset or hand-edited."""
    omp = cfg.get("omp")
    raw = omp.get("cycle_order") if isinstance(omp, dict) else None
    return [str(role) for role in raw] if isinstance(raw, list) else []


def _imported_rows(cfg: dict) -> list[dict]:
    """The omp roles backed by ``delegation:``/``advisor:``, in picker order."""
    from hermes_cli.aux_slots import IMPORTED_ROLE_META, omp_role_tag
    from hermes_cli.imported_roles import read_imported_route

    rows = []
    for role, (label, hint, target) in IMPORTED_ROLE_META.items():
        route = read_imported_route(cfg, role)
        rows.append({
            "id": role, "label": label, "hint": hint, "tag": omp_role_tag(role, label),
            "section": "chat", "provider": route["provider"], "model": route["model"],
            "base_url": route["base_url"], "reasoning_effort": route["reasoning_effort"],
            "fallback_chain": route["fallback_chain"], "group": "imported", "target": target,
            "inherit_from": None,
        })
    return rows


def _role_rows(cfg: dict) -> list[dict]:
    """Picker roles in canonical order: built-ins, imported omp roles, then plugin tasks.

    Route fields are the role's own stored values on every row (a plugin row that inherits keeps
    its own blanks and names its base in ``inherit_from``), exactly like the dashboard's
    auxiliary rows.
    """
    from hermes_cli.aux_fallback import public_fallback_chain
    from hermes_cli.aux_slots import AUX_TASK_META, aux_picker_keys, omp_role_tag
    from hermes_cli.web_server_config import _plugin_aux_tasks

    aux_cfg = cfg.get("auxiliary")
    if not isinstance(aux_cfg, dict):
        aux_cfg = {}

    def _row(slot: str, label: str, hint: str, group: str, inherit_from: str | None,
             tag: str) -> dict:
        slot_cfg = aux_cfg.get(slot) if isinstance(aux_cfg.get(slot), dict) else {}
        return {
            "id": slot, "label": label, "hint": hint, "tag": tag, "section": "chat",
            "provider": str(slot_cfg.get("provider", "auto") or "auto"),
            "model": str(slot_cfg.get("model") or ""),
            "base_url": str(slot_cfg.get("base_url") or ""),
            "reasoning_effort": str(slot_cfg.get("reasoning_effort") or "") or None,
            "fallback_chain": public_fallback_chain(slot_cfg.get("fallback_chain")),
            "group": group,
            "target": "auxiliary",
            "inherit_from": inherit_from,
        }

    rows = [_row(slot, AUX_TASK_META[slot][0], AUX_TASK_META[slot][1], "builtin", None,
                 omp_role_tag(slot, AUX_TASK_META[slot][0]))
            for slot in aux_picker_keys()]
    rows.extend(_imported_rows(cfg))
    rows.extend(
        _row(entry["key"], str(entry.get("display_name") or entry["key"]),
             str(entry.get("description") or ""), "plugin", entry.get("inherit_from") or None,
             omp_role_tag(entry["key"], str(entry.get("display_name") or entry["key"])))
        for entry in _plugin_aux_tasks())
    return rows


def _presets_summary(cfg: dict) -> dict:
    from hermes_cli.model_presets import presets_summary

    return presets_summary(cfg)


def _model_chains(cfg: dict) -> dict[str, list[dict]]:
    """``auxiliary.fallback_chains`` keys, each route list sanitized like a row's chain."""
    from hermes_cli.aux_fallback import public_fallback_chain

    aux = cfg.get("auxiliary")
    raw = aux.get("fallback_chains") if isinstance(aux, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(key): public_fallback_chain(value) for key, value in raw.items()}


@method("model.roles.get")
@_profile_scoped
@_catch(_ROLE_FAILED)
def _(rid, params: dict) -> dict:
    """Every role with its stored route, plus the main model, cycle order, presets and chains."""
    from hermes_cli.config import load_config

    cfg = load_config()
    return _ok(rid, {"roles": _role_rows(cfg), "main": _main_route(cfg),
                     "default_thinking_level": _default_thinking_level(cfg),
                     "cycle_order": _cycle_order(cfg), "presets": _presets_summary(cfg),
                     "model_chains": _model_chains(cfg)})


def _imported_effort(rid, role: str, raw) -> tuple[dict, dict | None]:
    """``(route update, error)`` for an imported role's submitted effort knob.

    The hub always sends its thinking selection, and "inherit"/"" is how it says "no override".
    An imported role with no effort key of its own must treat that as a no-op rather than an
    error, or its rows could never be assigned from the hub.
    """
    if raw is None:
        return {}, None
    effort = str(raw).strip()
    if role == "advisor":
        if effort in ("", "inherit"):
            return {}, None
        return {}, _err(rid, _ROLE_INVALID,
                        "advisor has no reasoning_effort override in Hermes "
                        "(advisor config stores only provider/model)")
    if effort == "inherit":
        return {"reasoning_effort": None}, None
    from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort

    parsed = parse_reasoning_effort(effort)
    if parsed is None:
        return {}, _err(rid, _ROLE_INVALID, "reasoning_effort must be one of: inherit, none, "
                                            + ", ".join(VALID_REASONING_EFFORTS))
    return {"reasoning_effort": "none" if parsed.get("enabled") is False else parsed["effort"]}, None


def _imported_chain(rid, role: str, raw) -> tuple[dict, dict | None]:
    """``(route update, error)`` for an imported role's submitted fallback chain."""
    if raw is None:
        return {}, None
    if role == "advisor":
        if not raw:
            return {}, None  # an empty chain is the hub's "none configured", not a write
        return {}, _err(rid, _ROLE_INVALID,
                        "advisor has no fallback chain in Hermes "
                        "(advisor runs the one configured review model)")
    from hermes_cli.aux_fallback import normalize_fallback_chain

    try:
        return {"fallback_chain": normalize_fallback_chain(raw)}, None
    except ValueError as exc:
        return {}, _err(rid, _ROLE_INVALID, f"invalid fallback_chain: {exc}")


def _set_imported_role(rid, params: dict, role: str) -> dict:
    """Assign/clear ``task`` (``delegation:``) or ``advisor`` (``advisor:``)."""
    from hermes_cli.config import _CONFIG_LOCK, load_config, save_config
    from hermes_cli.imported_roles import write_imported_route
    from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK

    clear = bool(params.get("clear"))
    provider = str(params.get("provider") or "").strip()
    if not clear and not provider:
        return _err(rid, _ROLE_INVALID, "provider is required unless clear=True")
    route = {"provider": provider, "model": str(params.get("model") or "").strip(),
             "base_url": str(params.get("base_url") or "").strip(),
             "api_key": str(params.get("api_key") or "").strip()}
    for knob, parse in (("reasoning_effort", _imported_effort),
                        ("fallback_chain", _imported_chain)):
        value, error = parse(rid, role, params.get(knob))
        if error is not None:
            return error
        route.update(value)

    with _CONFIG_MUTATION_LOCK, _CONFIG_LOCK:
        cfg = load_config()
        try:
            write_imported_route(cfg, role, route, clear=clear)
        except ValueError as exc:
            return _err(rid, _ROLE_INVALID, str(exc))
        save_config(cfg)
        updated = load_config()

    row = next((candidate for candidate in _role_rows(updated) if candidate["id"] == role), None)
    if row is None:
        return _err(rid, _ROLE_UNKNOWN, f"unknown role: {role}")
    return _ok(rid, {"role": row})


def _set_aux_role(rid, params: dict, role: str) -> dict:
    """Assign/clear one auxiliary slot; the profile's config.yaml stays the single truth."""
    # Two lock objects guard config writes: the dashboard holds `_CONFIG_MUTATION_LOCK` around its
    # own load→apply→save span (`web_routers/_common.config_write_scope`), while `save_config`
    # takes `_CONFIG_LOCK` for the file write. Holding only the latter would let a dashboard write
    # interleave between this handler's load and save and lose one side, so take both in the
    # dashboard's outer-to-inner order (mutation lock → config lock).
    from hermes_cli.config import _CONFIG_LOCK, clear_model_endpoint_credentials, load_config
    from hermes_cli.web_server_config import _apply_aux_assignment_sync
    from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK

    clear = bool(params.get("clear"))
    provider = str(params.get("provider") or "").strip()
    model = str(params.get("model") or "").strip()
    base_url = str(params.get("base_url") or "").strip()
    api_key = str(params.get("api_key") or "").strip()
    if not clear and not provider:
        return _err(rid, _ROLE_INVALID, "provider is required unless clear=True")

    # Omitted (None on the wire) leaves the stored override alone; "inherit" clears it, which the
    # assignment path expresses as an explicit None ("" on a slot that ships a default level).
    effort_kwargs: dict = {}
    if params.get("reasoning_effort") is not None:
        effort = str(params["reasoning_effort"]).strip()
        if effort == "inherit":
            effort_kwargs["reasoning_effort"] = None
        else:
            from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort

            parsed = parse_reasoning_effort(effort)
            if parsed is None:
                return _err(rid, _ROLE_INVALID, "reasoning_effort must be one of: inherit, none, "
                                               + ", ".join(VALID_REASONING_EFFORTS))
            effort_kwargs["reasoning_effort"] = "none" if parsed.get("enabled") is False else parsed["effort"]

    chain_kwargs: dict = {}
    if params.get("fallback_chain") is not None:
        from hermes_cli.aux_fallback import normalize_fallback_chain

        try:
            chain_kwargs["fallback_chain"] = normalize_fallback_chain(params["fallback_chain"])
        except ValueError as exc:
            return _err(rid, _ROLE_INVALID, f"invalid fallback_chain: {exc}")

    with _CONFIG_MUTATION_LOCK, _CONFIG_LOCK:
        cfg = load_config()
        if clear:
            # "auto" means the slot keeps no stale endpoint or inline key: `_apply_aux_assignment_sync`
            # only drops them on a provider CHANGE, so an already-"auto" slot would keep theirs.
            aux_cfg = cfg.get("auxiliary")
            block = aux_cfg.get(role) if isinstance(aux_cfg, dict) else None
            if isinstance(block, dict):
                block.pop("base_url", None)
                clear_model_endpoint_credentials(block)
            provider, model, base_url, api_key = "auto", "", "", ""
            effort_kwargs = {"reasoning_effort": None}
            chain_kwargs = {"fallback_chain": []}
        _apply_aux_assignment_sync(cfg, provider, model, role, base_url, api_key,
                                   **effort_kwargs, **chain_kwargs)
        updated = load_config()

    row = next((candidate for candidate in _role_rows(updated) if candidate["id"] == role), None)
    if row is None:
        return _err(rid, _ROLE_UNKNOWN, f"unknown role: {role}")
    return _ok(rid, {"role": row})


def _set_chain_key(rid, params: dict, role: str) -> dict:
    """Replace (or remove, with ``[]``) one model-keyed ``auxiliary.fallback_chains`` entry."""
    from hermes_cli.aux_fallback import normalize_fallback_chain, normalize_model_chain_key
    from hermes_cli.config import _CONFIG_LOCK, load_config, save_config
    from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK

    try:
        key = normalize_model_chain_key(role)
    except ValueError:
        return _err(rid, _ROLE_UNKNOWN, f"unknown role: {role or '<empty>'}")
    if params.get("clear"):
        return _err(rid, _ROLE_INVALID,
                    "clear=True is not supported for a fallback chain key; write the shortened "
                    "fallback_chain (omit the removed routes) to change the entries")
    try:
        chain = normalize_fallback_chain(params.get("fallback_chain"))
    except ValueError as exc:
        return _err(rid, _ROLE_INVALID, f"invalid fallback_chain: {exc}")

    with _CONFIG_MUTATION_LOCK, _CONFIG_LOCK:
        cfg = load_config()
        aux = cfg.get("auxiliary") if isinstance(cfg.get("auxiliary"), dict) else {}
        stored = aux.get("fallback_chains")
        chains = dict(stored) if isinstance(stored, dict) else {}
        if chain:
            chains[key] = [dict(entry) for entry in chain]
        else:
            chains.pop(key, None)
        aux["fallback_chains"] = chains
        cfg["auxiliary"] = aux
        save_config(cfg)

    row = {
        "id": key, "label": key, "hint": "", "tag": key.upper(), "section": "chat",
        "provider": "", "model": "", "base_url": "", "reasoning_effort": None,
        "fallback_chain": chain, "group": "chain", "target": "chain", "inherit_from": None,
    }
    return _ok(rid, {"role": row})


@method("model.roles.set")
@_profile_scoped
@_catch(_ROLE_FAILED)
def _(rid, params: dict) -> dict:
    """Assign or clear one role; the profile's config.yaml stays the single truth."""
    from hermes_cli.aux_slots import IMPORTED_ROLE_META
    from hermes_cli.web_server_config import _aux_task_slots

    role = str(params.get("role") or "").strip()
    if role in IMPORTED_ROLE_META:
        return _set_imported_role(rid, params, role)
    if role in _aux_task_slots():
        return _set_aux_role(rid, params, role)
    # A role id always wins over a chain key (a plugin task could be named like one). A keyed
    # chain is only addressable when the write carries the chain the hub is editing — or a flag
    # the chain path refuses (``clear=True``), so that refusal is not reported as an unknown role.
    if params.get("fallback_chain") is not None or params.get("clear"):
        return _set_chain_key(rid, params, role)
    return _err(rid, _ROLE_UNKNOWN, f"unknown role: {role or '<empty>'}")


@method("model.cycle.set")
@_profile_scoped
@_catch(_ROLE_FAILED)
def _(rid, params: dict) -> dict:
    """Persist omp's role cycle order (``omp.cycle_order``)."""
    from hermes_cli.aux_slots import IMPORTED_ROLE_IDS
    from hermes_cli.config import _CONFIG_LOCK, load_config, save_config
    from hermes_cli.web_server_config import _aux_task_slots
    from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK

    raw = params.get("order")
    if not isinstance(raw, list):
        return _err(rid, _ROLE_INVALID, "order must be a list of role ids")
    order = [str(role or "").strip() for role in raw]
    known = set(_aux_task_slots()) | set(IMPORTED_ROLE_IDS)
    unknown = next((role for role in order if role not in known), "")
    if unknown:
        return _err(rid, _ROLE_UNKNOWN, f"unknown role: {unknown or '<empty>'}")

    with _CONFIG_MUTATION_LOCK, _CONFIG_LOCK:
        cfg = load_config()
        omp = cfg.get("omp") if isinstance(cfg.get("omp"), dict) else {}
        omp["cycle_order"] = order
        cfg["omp"] = omp
        save_config(cfg)
    return _ok(rid, {"order": order})


@method("model.presets.save")
@_profile_scoped
@_catch(_ROLE_FAILED)
def _(rid, params: dict) -> dict:
    """Snapshot every assignable role into ``model_presets.<name>``, overwriting."""
    from hermes_cli.config import _CONFIG_LOCK, load_config, save_config
    from hermes_cli.model_presets import presets_section, snapshot, validate_preset_name
    from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK

    try:
        name = validate_preset_name(params.get("name"))
    except ValueError as exc:
        return _err(rid, _ROLE_INVALID, str(exc))

    with _CONFIG_MUTATION_LOCK, _CONFIG_LOCK:
        cfg = load_config()
        presets = presets_section(cfg)
        presets[name] = snapshot(cfg)
        cfg["model_presets"] = presets
        save_config(cfg)
    return _ok(rid, {"name": name})


@method("model.presets.apply")
@_profile_scoped
@_catch(_ROLE_FAILED)
def _(rid, params: dict) -> dict:
    """Restore a preset; roles absent from the snapshot reset to auto."""
    from hermes_cli.config import _CONFIG_LOCK, load_config, save_config
    from hermes_cli.model_presets import (apply_preset, presets_section, presets_summary,
                                          validate_preset_name)
    from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK

    try:
        name = validate_preset_name(params.get("name"))
    except ValueError as exc:
        return _err(rid, _ROLE_INVALID, str(exc))

    with _CONFIG_MUTATION_LOCK, _CONFIG_LOCK:
        cfg = load_config()
        if name not in presets_section(cfg):
            return _err(rid, _ROLE_UNKNOWN, f"unknown preset: {name}")
        try:
            apply_preset(cfg, name)
        except (KeyError, ValueError) as exc:
            return _err(rid, _ROLE_INVALID, str(exc))
        save_config(cfg)
        updated = load_config()
        summary = presets_summary(updated)
        rows = _role_rows(updated)
        cycle_order = _cycle_order(updated)
    return _ok(rid, {"roles": rows, "presets": summary, "cycle_order": cycle_order})


@method("model.presets.delete")
@_profile_scoped
@_catch(_ROLE_FAILED)
def _(rid, params: dict) -> dict:
    """Remove a preset (unknown name answers 5126)."""
    from hermes_cli.config import _CONFIG_LOCK, load_config, save_config
    from hermes_cli.model_presets import presets_section, validate_preset_name
    from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK

    try:
        name = validate_preset_name(params.get("name"))
    except ValueError as exc:
        return _err(rid, _ROLE_INVALID, str(exc))

    with _CONFIG_MUTATION_LOCK, _CONFIG_LOCK:
        cfg = load_config()
        presets = presets_section(cfg)
        if name not in presets:
            return _err(rid, _ROLE_UNKNOWN, f"unknown preset: {name}")
        presets.pop(name)
        cfg["model_presets"] = presets
        save_config(cfg)
    return _ok(rid, {"name": name})


def register(server) -> None:
    bind_module(globals(), server, skip=("_", "_catch"))
