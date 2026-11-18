"""Validation and persistence for auxiliary fallback chains.

Two shapes share one route grammar (``agent/auxiliary_client._try_configured_fallback_chain``):

* ``auxiliary.<task>.fallback_chain`` — an ordered list for one task;
* ``auxiliary.fallback_chains`` — a map from ``provider/model`` (or ``provider/*``) to that list,
  so one rescue policy can cover the route that just failed regardless of task.

Every route needs a ``provider`` and ``model``; ``base_url`` is optional. Keeping this out of
``web_server_config`` leaves that facade's assignment path inside its complexity/size budget.
"""

from __future__ import annotations

from typing import Any

MAX_FALLBACK_CHAIN_ENTRIES = 8

_FIELDS = ("provider", "model", "base_url")


def _normalize_entry(entry: Any, index: int) -> dict[str, str]:
    """One submitted route in its persisted shape; ``ValueError`` names the index."""
    dump = getattr(entry, "model_dump", None)
    if callable(dump):  # the REST body's AuxFallbackRoute
        entry = dump()
    if not isinstance(entry, dict):
        raise ValueError(f"fallback_chain[{index}] must be an object")
    route = {name: str(entry.get(name) or "").strip() for name in _FIELDS}
    for required in ("provider", "model"):
        if not route[required]:
            raise ValueError(f"fallback_chain[{index}] requires a {required}")
    return {name: route[name] for name in _FIELDS if name != "base_url" or route[name]}


def normalize_fallback_chain(raw: Any) -> list[dict[str, str]]:
    """Validate a submitted chain into its persisted shape.

    ``[]``/``None`` clears. Raises ``ValueError`` naming the offending index for a non-list, more
    than ``MAX_FALLBACK_CHAIN_ENTRIES`` entries, an entry that is not an object, or one whose
    ``provider``/``model`` is blank. Blanks are stripped; ``base_url`` is kept only when
    non-empty, so a route pinning no endpoint stays byte-identical to the shipped shape.
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("fallback_chain must be a list of routes")
    if len(raw) > MAX_FALLBACK_CHAIN_ENTRIES:
        raise ValueError(f"fallback_chain allows at most {MAX_FALLBACK_CHAIN_ENTRIES} entries "
                         f"(got {len(raw)})")
    return [_normalize_entry(entry, index) for index, entry in enumerate(raw)]


def normalize_model_chain_key(raw: Any) -> str:
    """Validate one ``auxiliary.fallback_chains`` key: ``provider/model`` or ``provider/*``.

    Splits on the FIRST ``/`` because model ids carry their own slashes
    (``anthropic/claude-sonnet-4``). Raises ``ValueError`` for a key without ``/`` or with an
    empty side, naming the offending key.
    """
    key = str(raw or "").strip()
    provider, sep, model = key.partition("/")
    provider, model = provider.strip(), model.strip()
    if not sep or not provider or not model:
        raise ValueError(
            f"fallback_chains key {key!r} must be 'provider/model' or 'provider/*' "
            "(model ids may contain '/' after the first)")
    return f"{provider}/{model}"


def normalize_model_fallback_chains(raw: Any) -> dict[str, list[dict[str, str]]]:
    """Validate a submitted ``auxiliary.fallback_chains`` map into its persisted shape.

    ``{}``/``None`` clears. Each key goes through :func:`normalize_model_chain_key`; each value is
    the same ordered route list as a per-task chain (max 8, entries need ``provider``+``model``),
    so a ``ValueError`` from :func:`normalize_fallback_chain` is re-raised naming its key.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("fallback_chains must be a map of provider/model keys to route lists")
    chains: dict[str, list[dict[str, str]]] = {}
    for raw_key, raw_chain in raw.items():
        key = normalize_model_chain_key(raw_key)
        if key in chains:
            raise ValueError(f"fallback_chains has more than one entry for {key!r}")
        try:
            chains[key] = normalize_fallback_chain(raw_chain)
        except ValueError as exc:
            raise ValueError(f"fallback_chains[{key!r}]: {exc}") from exc
    return chains


def public_fallback_chain(raw: Any) -> list[dict[str, str]]:
    """A stored chain as the REST surface reports it: ``[]`` when unset, unusable entries dropped.

    A hand-edited config must never make the Models page 500, so this is lenient where
    :func:`normalize_fallback_chain` refuses the write.
    """
    if not isinstance(raw, list):
        return []
    routes: list[dict[str, str]] = []
    for entry in raw[:MAX_FALLBACK_CHAIN_ENTRIES]:
        try:
            routes.extend(normalize_fallback_chain([entry]))
        except ValueError:
            continue
    return routes


def set_slot_fallback_chain(slot_cfg: dict[str, Any], raw: Any) -> list[dict[str, str]]:
    """Persist a validated chain on one slot block and return the stored copy."""
    chain = normalize_fallback_chain(raw)
    slot_cfg["fallback_chain"] = [dict(route) for route in chain]
    return chain


def clear_slot_fallback_chain(slot_cfg: dict[str, Any]) -> None:
    """Reset a slot to the shipped default (``_aux`` supplies ``[]``)."""
    slot_cfg.pop("fallback_chain", None)
