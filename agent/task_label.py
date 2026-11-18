"""Write-once task labels for named subagent runs (ADR-21 D7).

A named run's ``agent_session.title`` is its display name and its ``agent_message.to`` alias. The
label is generated once, asynchronously, from the run's assignment: one plan-driven naming call
(the same ``title_model_plan``/``run_title_plan`` ladder the session titler spends), a tighter
guard (≤9 words, no echo of the run's own handle), and the DB's write-once update. Everything here
is metadata: a failure logs at debug, never reaches the transcript, the tool result or the model,
and never delays the spawn or the turn.

Gates (ADR-21 D7): an empty assignment, a dispatcher-owned kanban worker, either
``auxiliary.title_generation`` switch off, or a plan with no candidate all mean no thread, no
model call and no write.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)


def write_task_label(
    *,
    agent_session_id: str,
    assignment: str,
    name: str = "",
    plan: Sequence[Any] = (),
) -> Optional[str]:
    """Generate one label and persist it write-once; the daemon thread's target. Never raises.

    ``name`` is the run's profile-unique short name (its spawn handle): a label that just repeats
    it — or its ``name-2`` form — is refused, and the run keeps its ``''`` (unlabelled) title.
    """
    try:
        from agent.prompts.renderer import render_request
        from agent.title_parser import clean_label
        from agent.title_router import run_title_plan

        messages = [
            {"role": "system", "content": render_request("task_label", {})},
            {"role": "user", "content": str(assignment or "")},
        ]
        raw = run_title_plan(plan, messages)
        label = clean_label(raw or "", handle=name)
        if not label:
            logger.debug("task label: nothing usable for %s", agent_session_id)
            return None
        return label if _store_label(agent_session_id, label) else None
    except Exception:  # health: allow BLE001 -- naming is metadata, never a run failure
        logger.debug("task label: generation failed for %s", agent_session_id, exc_info=True)
        return None


def _store_label(agent_session_id: str, label: str) -> bool:
    """The write-once update; a store failure logs at debug and leaves the run unlabelled."""
    try:
        from hermes_state_registry import acquire, release_or_close

        db = acquire()
    except Exception:  # health: allow BLE001 -- a run must not depend on the store
        logger.debug("task label: no session store for %s", agent_session_id, exc_info=True)
        return False
    try:
        return bool(db.set_agent_session_title(agent_session_id, label))
    except Exception:  # health: allow BLE001 -- a lost label is not a lost run
        logger.debug("task label: write failed for %s", agent_session_id, exc_info=True)
        return False
    finally:
        release_or_close(db)


def maybe_start_task_label(
    *, agent_session_id: str, assignment: str, name: str = ""
) -> Optional[Any]:
    """Start the daemon label thread for one named run, or return None when a gate says no.

    The plan is resolved before the thread starts so the gates (both switches, and "nothing
    configured to call") cost no thread at all; the model call itself then happens entirely on
    the daemon thread, so the caller's spawn returns first.
    """
    if not str(assignment or "").strip():
        return None
    from agent.delegation_context import owned_kanban_task

    if owned_kanban_task():
        # A dispatcher-owned worker fans runs out in batches; one label call per child would put
        # N auxiliary requests behind a single card (#111166, ADR-21 D7).
        logger.debug("task label: skipped for a dispatcher-owned worker run")
        return None
    from agent.title_router import title_model_plan

    plan = title_model_plan()
    if not plan:
        logger.debug("task label: skipped; no title-model candidate is configured")
        return None
    from hermes_threading import spawn_context_thread

    thread = spawn_context_thread(
        write_task_label,
        name="task-label",
        kwargs={
            "agent_session_id": agent_session_id,
            "assignment": assignment,
            "name": name,
            "plan": tuple(plan),
        },
    )
    thread.start()
    return thread


__all__ = ["maybe_start_task_label", "write_task_label"]
