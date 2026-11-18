"""Process-local spend gate for judgment calls (ADR-17 D4/D8).

Every candidate attempt is admitted before it is sent; a successful result records its
cost against both the current judgment call and the ambient session. The session bucket
is keyed by profile home + session id, so one process serving many profiles (gateway) or
many conversations never mixes budgets. Without a session context the profile itself is
the bucket: an out-of-band CLI invocation must still be able to trip its own cap.

The caps are a guard rail, not accounting: real spend is recorded by the session ledger
(and the native ``usage`` table from P4). ``0`` disables a cap.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from hermes_constants import hermes_home_key

from agent.judgment.types import JudgmentBudgetExceeded, JudgmentResult
from agent.usage_pricing import estimate_usage_cost

logger = logging.getLogger(__name__)

#: ``(profile home key, session id or "") -> (USD spent, last touch)``.
_SESSION_SPEND: dict[tuple[str, str], tuple[float, float]] = {}
_LOCK = threading.Lock()
#: A multiplexed gateway must not grow one bucket per conversation forever.
_MAX_BUCKETS = 4096
_IDLE_BUCKET_S = 6 * 60 * 60.0


def _session_key() -> tuple[str, str]:
    session_id = ""
    try:
        from agent.aux_accounting import current_context

        context = current_context()
        if context is not None:
            session_id = str(context[1] or "")
    except Exception:  # health: allow BLE001 -- accounting context is optional
        session_id = ""
    return (hermes_home_key(), session_id)


def reset_cost_gate() -> None:
    """Test hook: forget every process-local judgment spend bucket."""
    with _LOCK:
        _SESSION_SPEND.clear()


@dataclass
class JudgmentCostGate:
    """One judgment call's gate; session spend survives across calls in this process."""

    max_usd_per_call: float = 0.05
    max_usd_per_session: float = 0.50
    key: tuple[str, str] = field(default_factory=_session_key)
    call_spent: float = 0.0

    @property
    def session_spent(self) -> float:
        with _LOCK:
            entry = _SESSION_SPEND.get(self.key)
            return entry[0] if entry is not None else 0.0

    def admit(self) -> None:
        """Raise when the next candidate attempt would cross a configured cap."""
        if self.max_usd_per_call and self.call_spent >= self.max_usd_per_call:
            raise JudgmentBudgetExceeded(
                f"judgment call budget exceeded "
                f"(${self.call_spent:.4f} of ${self.max_usd_per_call:.2f})")
        if self.max_usd_per_session:
            spent = self.session_spent
            if spent >= self.max_usd_per_session:
                raise JudgmentBudgetExceeded(
                    f"judgment session budget exceeded "
                    f"(${spent:.4f} of ${self.max_usd_per_session:.2f})")

    def record(self, cost: float) -> None:
        """Add one attempt's cost to the call and session buckets (billed failures too)."""
        if cost <= 0:
            return
        self.call_spent += cost
        now = time.monotonic()
        with _LOCK:
            spent, _touch = _SESSION_SPEND.get(self.key, (0.0, now))
            _SESSION_SPEND[self.key] = (spent + cost, now)
            _prune_locked(now)


def _prune_locked(now: float) -> None:
    """Drop idle buckets, then the oldest, so a long-lived gateway stays bounded."""
    for key, (_spent, touched) in list(_SESSION_SPEND.items()):
        if now - touched > _IDLE_BUCKET_S:
            _SESSION_SPEND.pop(key, None)
    while len(_SESSION_SPEND) > _MAX_BUCKETS:
        oldest = min(_SESSION_SPEND, key=lambda key: _SESSION_SPEND[key][1])
        _SESSION_SPEND.pop(oldest, None)


def usage_cost(model: str, provider: str, usage: Any, base_url: str = "") -> float:
    """Token-priced USD for one usage record; ``0`` when the model is unpriced."""
    if usage is None:
        return 0.0
    if not (usage.input_tokens or usage.output_tokens or usage.cache_read_tokens
            or usage.cache_write_tokens or usage.reasoning_tokens):
        return 0.0
    try:
        estimate = estimate_usage_cost(model, usage, provider=provider, base_url=base_url)
        return float(estimate.amount_usd) if estimate.amount_usd is not None else 0.0
    except Exception:
        logger.debug("judgment cost estimation failed (non-fatal)", exc_info=True)
        return 0.0


def result_cost(result: JudgmentResult, base_url: str = "") -> float:
    """Billed USD for one result: the wire's amount, else a token-priced estimate."""
    if result.cost_usd is not None:
        return max(0.0, float(result.cost_usd))
    return usage_cost(result.model, result.provider, result.usage, base_url=base_url)
