"""Contracts: the Models-hub Roles view, backed by Hermes config.

Handlers: ``tui_gateway/methods_model_roles.py``. Hermes has one kind of auxiliary model role
(``auxiliary.<task>``) plus two imported omp roles Hermes backs elsewhere: ``task`` →
``delegation:`` and ``advisor`` → ``advisor:``. ``target`` says which section a row writes and
``group`` whether it is a built-in picker slot, a plugin-registered task, or imported.

``tag`` is the omp badge the shell renders (VISION, TINY, MEMORY, …); the map lives server-side in
``hermes_cli/aux_slots.py`` so the client never keeps a second copy.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import Result
from .common import ProfileParams
from .registry import method


class ModelRoleFallbackEntry(Result):
    """One ``auxiliary.<task>.fallback_chain`` route (``hermes_cli/aux_fallback.py``):
    ``provider`` + ``model`` are required to serve, ``base_url`` pins an explicit endpoint."""

    provider: str = ""
    model: str = ""
    base_url: str = ""


class ModelRoleRow(Result):
    """One role as the profile's ``config.yaml`` stores it.

    ``reasoning_effort`` is the slot's own override: a level, ``"none"``, or ``null`` (inherit
    ``default_thinking_level``). ``fallback_chain`` is ``[]`` while unset. ``target`` is the config
    section the row writes (``auxiliary``, ``delegation``, ``advisor`` or ``chain`` —
    ``auxiliary.fallback_chains[<id>]``); ``group`` separates built-in picker slots,
    plugin-registered tasks, imported omp roles and model-keyed chains. A plugin row names the base
    slot it inherits from in ``inherit_from`` (its own ``provider``/``model``/``base_url`` stay the
    slot's stored values, exactly like the dashboard's auxiliary rows)."""

    id: str
    label: str = ""
    hint: str = ""
    tag: str = ""
    section: Literal["chat"] = "chat"
    provider: str = "auto"
    model: str = ""
    base_url: str = ""
    reasoning_effort: str | None = None
    fallback_chain: list[ModelRoleFallbackEntry] = Field(default_factory=list)
    group: Literal["builtin", "plugin", "imported", "chain"] = "builtin"
    target: Literal["auxiliary", "delegation", "advisor", "chain"] = "auxiliary"
    inherit_from: str | None = None


class ModelRolesMain(Result):
    """The main model route roles inherit from (``provider: auto`` / ``main`` resolve to it)."""

    provider: str = ""
    model: str = ""


class ModelPresetsSummary(Result):
    """Configured preset names plus the one whose snapshot equals the live assignments (``""`` none)."""

    names: list[str] = Field(default_factory=list)
    active: str = ""


class ModelRolesGetResult(Result):
    roles: list[ModelRoleRow] = Field(default_factory=list)
    main: ModelRolesMain = Field(default_factory=ModelRolesMain)
    # ``agent.reasoning_effort`` as the TUI reports it (``config.get reasoning``); a role with
    # ``reasoning_effort: null`` inherits it.
    default_thinking_level: str = ""
    # omp's role cycle order (``model.cycle.set`` → ``omp.cycle_order``); ``[]`` = registry order.
    cycle_order: list[str] = Field(default_factory=list)
    presets: ModelPresetsSummary = Field(default_factory=ModelPresetsSummary)
    # ``auxiliary.fallback_chains``: model/provider-keyed rescue chains, keys as stored and routes
    # sanitized exactly like a row's ``fallback_chain``. ``{}`` while unset.
    model_chains: dict[str, list[ModelRoleFallbackEntry]] = Field(default_factory=dict)


method("model.roles.get", params=ProfileParams, result=ModelRolesGetResult,
       doc="Every role (picker slots in canonical order, then imported roles, then plugin tasks) "
           "with its stored route, the main route, cycle order and presets.")


class ModelRolesSetParams(ProfileParams):
    """Assign one role. ``clear`` resets it to its "auto" route with no endpoint/override.

    ``reasoning_effort``: a level, ``"none"``, or ``"inherit"`` to clear the override; ``null``
    (omitted) leaves it unchanged. ``fallback_chain``: ``null`` (omitted) leaves it unchanged,
    ``[]`` clears it, a list replaces it. The imported ``advisor`` role has neither knob and
    refuses them with 5127.

    ``role`` may instead be a model-keyed chain (``provider/model`` or ``provider/*``) when it
    carries ``fallback_chain``: the list replaces ``auxiliary.fallback_chains[<key>]`` and ``[]``
    removes the entry. ``clear`` is refused for a chain key (write the shortened chain instead)."""

    role: str
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    reasoning_effort: str | None = None
    fallback_chain: list[ModelRoleFallbackEntry] | None = None
    clear: bool = False


class ModelRolesSetResult(Result):
    role: ModelRoleRow


method("model.roles.set", params=ModelRolesSetParams, result=ModelRolesSetResult,
       doc="Assign or clear one role (or a model-keyed fallback chain); the profile's config.yaml "
           "stays the single truth.")


class ModelCycleSetParams(ProfileParams):
    """omp's ``cycle_order``: role ids in cycle order. Every id must be an assignable role."""

    order: list[str] = Field(default_factory=list)


class ModelCycleSetResult(Result):
    order: list[str] = Field(default_factory=list)


method("model.cycle.set", params=ModelCycleSetParams, result=ModelCycleSetResult,
       doc="Persist the Models-hub role cycle order (``omp.cycle_order``).")


class ModelPresetParams(ProfileParams):
    """A preset name: 1–64 characters of ``[A-Za-z0-9._-]`` (5127 otherwise)."""

    name: str


class ModelPresetsSaveResult(Result):
    name: str


method("model.presets.save", params=ModelPresetParams, result=ModelPresetsSaveResult,
       doc="Snapshot every assignable role into ``model_presets.<name>``, overwriting.")


class ModelPresetsApplyResult(Result):
    """Same shapes as ``model.roles.get`` minus the main route."""

    roles: list[ModelRoleRow] = Field(default_factory=list)
    presets: ModelPresetsSummary = Field(default_factory=ModelPresetsSummary)
    cycle_order: list[str] = Field(default_factory=list)


method("model.presets.apply", params=ModelPresetParams, result=ModelPresetsApplyResult,
       doc="Restore a preset; roles absent from the snapshot reset to auto.")


class ModelPresetsDeleteResult(Result):
    name: str


method("model.presets.delete", params=ModelPresetParams, result=ModelPresetsDeleteResult,
       doc="Remove a preset (5126 when it does not exist).")
