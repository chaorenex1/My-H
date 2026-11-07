"""Turn-start judgment consumers (ADR-17 P5).

Both classifiers run once per user-initiated turn, before the first API call:
auto-thinking fixes the reasoning level for the whole turn, and auto-prewalk may
arm the one-shot model handoff. They live here so ``conversation_loop`` stays a
facade; every failure is a debug log, never a turn failure.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

logger = logging.getLogger(__name__)


def prepare_turn_consumers(
    agent: Any,
    request: str,
    history: Sequence[dict[str, Any]] | None = None,
) -> None:
    """Resolve auto-thinking and start a prewalk task for one user turn."""
    try:
        from agent.auto_thinking import resolve_auto_effort

        config = resolve_auto_effort(agent, request)
    except Exception:
        logger.debug("auto reasoning classification failed", exc_info=True)
        config = None
    if config:
        agent._auto_reasoning_config = config
        # The overlay only applies to the route it was classified for (a model switch
        # or fallback re-resolve clears it instead of leaking the old level).
        agent._auto_reasoning_route = (
            getattr(agent, "provider", None),
            getattr(agent, "model", None),
            getattr(agent, "api_mode", None),
        )

    # ADR-12 D5: a new top-level task may arm the one-shot prewalk handoff. Forks
    # (background review, side questions) and subagents stay on their own model config.
    try:
        from agent.prewalk import begin_auto_task

        if not getattr(agent, "_turn_origin", None) and not getattr(agent, "_delegate_depth", 0):
            begin_auto_task(agent, request, history)
    except Exception:
        logger.debug("prewalk task start failed", exc_info=True)
