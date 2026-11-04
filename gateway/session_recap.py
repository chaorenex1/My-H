"""``recap.generate``: the idle recap the client's timer asks for (ADR-5 D8).

The client owns the idle timer, the composer draft and the current-view check — inputs only it has.
The owner owns what it can prove: the session is idle (no queued/running admission, no pending
prompt), the tail is still a generated assistant turn, and that tail has not already been recapped.
A refusal is a normal reply (``skipped``), never an error, and never a model call.

The turn itself is a cache-parity side turn of the session's resident agent
(``agent/recap.py``): same system prompt and ``tools[]``, no persistence, no transcript row, no
event. The reply is plain text for the client's ``RecapNotice``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from functools import partial
from typing import Any

from gateway.session_busy_controls import authorize
from gateway.session_llm_oneshot import DeferredReply
from hermes_state_runtime import RuntimeStoreError

logger = logging.getLogger(__name__)

_FIELDS = frozenset({"session_id", "profile"})


def handlers(connection):
    return {"recap.generate": partial(generate, connection)}


async def generate(connection, ref, params):
    if (set(params) - _FIELDS or not isinstance(ref.session_id, str) or not ref.session_id):
        raise RuntimeStoreError("invalid_params")
    authority = connection.authority
    if ref.session_id not in authority.sessions:
        raise RuntimeStoreError("not_found")
    authorize(connection, ref, params, "session:read")
    # The model call runs on the owner's one-shot pool: a slow or hanging recap must not hold up
    # this connection's serial dispatch, and the pool's existing bounds refuse over-cap by name.
    return DeferredReply(partial(_recap, authority, ref))


def _busy(authority, session_id: str) -> bool:
    """A queued/running/unknown admission, or an unanswered approval/clarify prompt.

    ``_pending_rows`` is the FIFO projection (queued, started, unknown) that also derives
    ``execution_state``; a pending prompt would be stranded by a turn that is not running.
    """
    from gateway.session_contract import SessionRef

    ref = SessionRef(authority.profile_id, session_id)
    if authority._pending_rows(ref):
        return True
    live = authority.sessions.get(session_id)
    if live is None:
        return True
    with live.event_stream.lock:
        return bool(live.controls.pending)


def _tail_uid(message: dict[str, Any]) -> str:
    """Identity of the conversation tail: its durable row when persisted, else a content digest."""
    row_id = message.get("_row_id")
    if isinstance(row_id, int) and not isinstance(row_id, bool):
        return f"row:{row_id}"
    blob = json.dumps(
        {key: message.get(key) for key in
         ("role", "content", "tool_calls", "timestamp", "reasoning_content", "reasoning")},
        sort_keys=True, default=str)
    return "sha1:" + hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()


def _generated_tail(messages: list[dict[str, Any]]) -> bool:
    """omp's ``priorMessageWasGenerated``: the last message is an assistant turn with output."""
    if not messages:
        return False
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return False
    if last.get("tool_calls"):
        return True
    content = last.get("content")
    if isinstance(content, list):
        if any(isinstance(block, dict) and str(block.get("text") or "").strip() for block in content):
            return True
        return any(isinstance(block, dict) and str(block.get("thinking") or "").strip()
                   for block in content)
    if isinstance(content, str) and content.strip():
        return True
    return bool(str(last.get("reasoning_content") or last.get("reasoning") or "").strip())


def _goal_text(authority, session_id: str) -> str:
    """The goal-mode objective, else the session title (omp's ``#goalText``)."""
    try:
        from hermes_cli.goals import load_goal
        state = load_goal(session_id)
        if state is not None and state.status != "cleared" and (state.goal or "").strip():
            return state.goal.strip()
    except Exception:
        logger.debug("idle recap: goal read failed", exc_info=True)
    row = authority.db.get_session(session_id) or {}
    return str(row.get("title") or "").strip()


def _active_task(agent: Any) -> str:
    """The first actionable todo item, in plan order (omp's ``nextActionableTask``)."""
    store = getattr(agent, "_todo_store", None)
    read = getattr(store, "read", None)
    if not callable(read):
        return ""
    try:
        items = read()
    except Exception:
        logger.debug("idle recap: todo read failed", exc_info=True)
        return ""
    for item in items if isinstance(items, list) else ():
        if (isinstance(item, dict) and item.get("status") in ("pending", "in_progress")
                and str(item.get("content") or "").strip()):
            return str(item["content"]).strip()
    return ""


def _skipped(reason: str, generated_at: float) -> dict[str, Any]:
    return {"text": "", "generated_at": generated_at, "skipped": reason}


def _recap(authority, ref) -> dict[str, Any]:
    """The pool-thread body: guards, anchors, one side turn. Never raises for a skip."""
    from gateway.session_authorities import owner_scope

    generated_at = time.time()
    with owner_scope(authority):
        if _busy(authority, ref.session_id):
            return _skipped("busy", generated_at)
        agent = authority.agent(ref)
        if agent is None:  # evicted, or no turn has run here yet: no warm prefix to replay
            return _skipped("no_agent", generated_at)
        messages = list(getattr(agent, "_session_messages", None) or [])
        if not _generated_tail(messages):
            return _skipped("no_new_turn", generated_at)
        uid = _tail_uid(messages[-1])
        if getattr(agent, "_last_recap_tail_uid", None) == uid:
            return _skipped("no_new_turn", generated_at)
        # Re-check right before spending the model call: a turn may have been admitted while the
        # work waited in the pool queue.
        if _busy(authority, ref.session_id):
            return _skipped("busy", generated_at)

        from agent.recap import generate_recap

        text = generate_recap(agent, goal=_goal_text(authority, ref.session_id),
                              task=_active_task(agent))
        if not text:
            return _skipped("failed", generated_at)
        agent._last_recap_tail_uid = uid
        return {"text": text, "generated_at": time.time()}
