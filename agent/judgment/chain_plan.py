"""Resolve ``judgment.chain`` into attempt-ordered entries, cached per profile (ADR-17 D4).

Resolution is pure data: it maps catalog ids and role names onto entries; credentials are
resolved per candidate, so a login or rotation applies on the next judgment. The plan is
reused for :data:`CHAIN_TTL_S` so a bulk caller does not rebuild it per item.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from hermes_constants import hermes_home_key
from hermes_cli.judgment_catalog import (
    ROLE_TASKS,
    JudgmentModelSpec,
    model_index,
    parse_chain_entry,
)

from agent.judgment.cache import canonical_json, sha256_text
from agent.judgment.config import JudgmentSettings

logger = logging.getLogger(__name__)

#: Resolved chain plans are reusable for five minutes (ADR-17 D4).
CHAIN_TTL_S = 300.0
_PLANS: dict[tuple[str, str], tuple[float, tuple[_ChainEntry, ...]]] = {}
_PLANS_LOCK = threading.Lock()


@dataclass(frozen=True)
class _ChainEntry:
    """One resolved ``judgment.chain`` reference."""

    kind: str  # native | local | online | role
    spec: JudgmentModelSpec | None = None
    role: str = ""


def reset_chain_cache() -> None:
    """Test hook: forget every process-local chain plan."""
    with _PLANS_LOCK:
        _PLANS.clear()


def _plan_fingerprint(settings: JudgmentSettings) -> str:
    return sha256_text(canonical_json({
        "chain": list(settings.chain),
        "models": [
            [spec.id, spec.kind, spec.api, spec.provider, spec.model, spec.base_url,
             spec.api_key_env, spec.request_model_id, spec.max_questions,
             repr(spec.image_input), spec.boxes, spec.timeout_ms]
            for spec in settings.models
        ],
    }))


def _build_chain_entries(settings: JudgmentSettings) -> tuple[_ChainEntry, ...]:
    index = model_index(settings.models)
    entries: list[_ChainEntry] = []
    for raw in settings.chain:
        kind, ident = parse_chain_entry(raw)
        if kind == "role":
            if ident not in ROLE_TASKS:
                logger.warning("judgment chain: unknown role %r; skipping", raw)
                continue
            entries.append(_ChainEntry("role", role=ident))
            continue
        spec = index.get(ident)
        if spec is None:
            logger.warning("judgment chain: unknown model id %r; skipping", raw)
            continue
        if spec.kind != kind:
            logger.warning(
                "judgment chain: %r names a %s model; skipping", raw, spec.kind)
            continue
        entries.append(_ChainEntry(spec.kind, spec=spec))
    return tuple(entries)


def chain_plan(settings: JudgmentSettings) -> tuple[_ChainEntry, ...]:
    key = (hermes_home_key(), _plan_fingerprint(settings))
    now = time.monotonic()
    with _PLANS_LOCK:
        cached = _PLANS.get(key)
        if cached is not None and now < cached[0]:
            return cached[1]
    entries = _build_chain_entries(settings)
    with _PLANS_LOCK:
        _PLANS[key] = (now + CHAIN_TTL_S, entries)
    return entries
