"""Turn-loop bridge to the native advisor.

The conversation loop and the stop gates only need two things: a session handle
(lazily created on the first turn, so an agent whose config has ``advisor.enabled:
false`` pays nothing) and one terminal-boundary capture taken before the stop
gates decide whether the turn ends. Keeping both here leaves the loop phases at
their audited complexity: the call site is one function call, not a branch.

Session kind decides which ``WATCHDOG.yml`` scopes may build: the top-level
session hosts ``main``/``all`` entries, a delegated subagent hosts
``subagent``/``all`` entries, and cron runs or detached forks host none.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def ensure_advisor(agent: Any) -> Optional[Any]:
    """Return (creating once) the agent's advisor session, or None when construction failed."""
    existing = getattr(agent, "advisor", None)
    if existing is not None:
        return existing
    with _LOCK:
        existing = getattr(agent, "advisor", None)
        if existing is not None:
            return existing
        try:
            from agent.advisor_runtime import AdvisorSession, advisor_session_kind

            kind = advisor_session_kind(agent)
            if kind is None:  # cron runs and detached forks never host a reviewer
                return None
            session = AdvisorSession(agent, kind=kind)
        except Exception:  # health: allow BLE001 -- optional feature: a broken advisor must never block a turn
            logger.debug("advisor session init failed", exc_info=True)
            return None
        agent.advisor = session
        return session


def step_review(agent: Any, messages: list[Any]) -> None:
    """Capture a completed tool-round boundary; observational, never blocks the turn."""
    advisor = getattr(agent, "advisor", None)
    if advisor is None:
        return
    try:
        advisor.on_step(messages, terminal=False)
    except Exception:  # health: allow BLE001 -- reviewer capture is observational; the turn must continue
        logger.debug("advisor step capture failed", exc_info=True)


def terminal_review(agent: Any, messages: list[Any], final_response: Any) -> None:
    """Capture the terminal boundary with the candidate answer, before the stop gates.

    The caputed row is a copy: the answer is not appended until a gate accepts it,
    but the reviewer must be able to judge the answer that is about to be shown.
    """
    if not isinstance(final_response, str) or not final_response.strip():
        return
    advisor = ensure_advisor(agent)
    if advisor is None:
        return
    try:
        advisor.on_step([*messages, {"role": "assistant", "content": final_response}], terminal=True)
    except Exception:  # health: allow BLE001 -- reviewer capture is observational; the turn must continue
        logger.debug("advisor terminal capture failed", exc_info=True)


__all__ = ["ensure_advisor", "step_review", "terminal_review"]
