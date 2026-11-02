"""Model/provider-keyed auxiliary fallback chains (``auxiliary.fallback_chains``).

omp's ``retry.fallbackChains`` keys a rescue chain by the model that just failed, so one map can
cover every auxiliary task without repeating the same per-task ``fallback_chain``. Tiers, most
specific first (``agent.auxiliary_client._try_configured_fallback_chain``):

    auxiliary.fallback_chains[provider/model] -> auxiliary.fallback_chains[provider/*]
        -> auxiliary.<task>.fallback_chain -> global fallback_providers

Keys are ``provider/model`` (split on the FIRST ``/``; model ids carry slashes) or
``provider/*``. Validation lives in ``hermes_cli.aux_fallback``; this module is the read side.
"""

from __future__ import annotations

import re
from typing import Any

LABEL_RE = re.compile(r"^fallback_chains\[(?P<key>[^\]]+)\]\[(?P<index>\d+)\]")


def auxiliary_section() -> dict[str, Any]:
    """The effective ``auxiliary`` config section (a gateway session's frozen view wins)."""
    from agent.auxiliary_task_config import _SESSION_AUXILIARY

    aux = _SESSION_AUXILIARY.get()
    if aux is None:
        try:
            from hermes_cli.config import load_config_readonly
            config = load_config_readonly()
        except ImportError:
            return {}
        aux = config.get("auxiliary", {}) if isinstance(config, dict) else {}
    return aux if isinstance(aux, dict) else {}


def model_chain_keys(failed_provider: str, failed_model: str | None) -> list[str]:
    """Keys to try for a failed route, most specific first.

    ``failed_model`` is not always known (auth/payment failures are credential-wide), in which case
    only the provider wildcard can match.
    """
    provider = (failed_provider or "").strip()
    if not provider:
        return []
    wildcard = f"{provider}/*"
    model = (failed_model or "").strip()
    return [f"{provider}/{model}", wildcard] if model and model != "*" else [wildcard]


def model_fallback_chains_for_route(
    failed_provider: str, failed_model: str | None = None, *,
    auxiliary: dict[str, Any] | None = None,
) -> list[tuple[str, list[Any]]]:
    """``[(key, chain)]`` configured for the failed route: exact ``provider/model``, then ``provider/*``."""
    keys = model_chain_keys(failed_provider, failed_model)
    if not keys:
        return []
    aux = auxiliary_section() if auxiliary is None else auxiliary
    chains = aux.get("fallback_chains") if isinstance(aux, dict) else None
    if not isinstance(chains, dict):
        return []
    found: list[tuple[str, list[Any]]] = []
    for key in keys:
        chain = chains.get(key)
        if isinstance(chain, list) and chain:
            found.append((key, chain))
    return found


def label_for(key: str, index: int, provider: str) -> str:
    """Display/quarantine label for one model-keyed candidate (parsed back by ``fallback_chain_entry``)."""
    return f"fallback_chains[{key}][{index}]({provider})"


def chain_entry_for_label(label: str) -> dict[str, Any] | None:
    """The stored entry behind a ``fallback_chains[<key>][<i>](<provider>)`` label, if it still resolves."""
    match = LABEL_RE.match(label or "")
    if not match:
        return None
    chains = auxiliary_section().get("fallback_chains")
    chain = chains.get(match.group("key")) if isinstance(chains, dict) else None
    index = int(match.group("index"))
    entry = chain[index] if isinstance(chain, list) and index < len(chain) else None
    return entry if isinstance(entry, dict) else None


# ── Configured-chain labels (shared by per-task and model-keyed chains) ─────────────────────────
# Lives here so both label formats resolve in one place; the per-task lookup goes through
# ``agent.auxiliary_client`` at call time so tests and callers keep patching that module's accessor.

def fallback_chain_entry(task: str | None, fb_label: str) -> dict[str, Any] | None:
    """Resolve the stored entry a configured-chain label points at,
    ``fallback_chain[<i>](<provider>)`` or ``fallback_chains[<key>][<i>](<provider>)``, or None
    when the label is not a configured-chain candidate or the index no longer resolves."""
    if not fb_label:
        return None
    match = re.match(r"fallback_chain\[(\d+)\]", fb_label)
    if not match:
        return chain_entry_for_label(fb_label) if fb_label.startswith("fallback_chains[") else None
    if not task:
        return None
    try:
        from agent import auxiliary_client
        chain = auxiliary_client._get_auxiliary_task_config(task).get("fallback_chain")
        entry = chain[int(match.group(1))] if isinstance(chain, list) else None
    except Exception:  # health: allow BLE001 -- config/label resolution is best-effort; a broken config must not break fallback planning
        return None
    return entry if isinstance(entry, dict) else None


def coerce_positive_timeout(raw: Any) -> float | None:
    """Coerce a config ``timeout`` to a positive float, or None (rejects bools, which are ints)."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    return None


def fallback_entry_timeout(task: str | None, fb_label: str) -> float | None:
    """Per-entry ``timeout`` for a configured fallback candidate, or None (keep the task-level
    timeout). Inheriting the primary's deadline used to kill healthy-but-slower fallbacks.

    A fallback candidate previously inherited the exact timeout the primary provider was called with. When
    that deadline was tuned for the primary (or the primary simply consumed its whole budget before failing
    over), the fallback aborted on the same clock even when independently healthy — a 163k-token compression
    that needs ~90s on the fallback died at the primary's 30s deadline every turn (#62452).
    """
    entry = fallback_chain_entry(task, fb_label)
    return coerce_positive_timeout(entry.get("timeout") if entry else None)


def fallback_provider_from_label(label: str) -> str:
    """Recover the provider identifier from a fallback display label."""
    match = re.match(
        r"(?:fallback_chain\[\d+\]|fallback_chains\[[^\]]+\]\[\d+\]|fallback_providers\[\d+\]|main-agent)"
        r"\(([^)]+)\)$", label or "")
    return match.group(1).strip() if match else str(label or "").strip()
