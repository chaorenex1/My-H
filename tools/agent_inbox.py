"""Session-owned inbox for named agents (ADR-15 D4, phase P3).

One named agent run is one agent session. While that run is live its ``AIAgent`` is steered at
the next tool boundary — the *aside* queue. When no run is live, the text is persisted in
``agent_inbox`` as ``pending`` and ``agent_out(mode="inbox")`` reads it back; the wake turn (P4)
drains it later. Every body is escaped with :func:`agent.ttsr_prompts.escape_harness_tags`, so a
message can never close or forge a harness tag.

Delivery is owner-scoped: the caller must be the session that started the named agent
(``agent_session.parent_session_id``, matching ``delegate_task``'s durable lineage rule), which is
the same authority ``delegate_task(action="steer")`` already enforces.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Optional

from agent.ttsr_prompts import escape_harness_tags
from tools.delegate_tool_registry import (
    _capture_gateway_steer_authority,
    _register_subagent,
    _resolve_session_lineage,
    _unregister_subagent,
    steer_subagent,
)

logger = logging.getLogger(__name__)

#: How many rows of each state ``agent_out(mode="inbox")`` shows.
INBOX_PAGE = 20


def capture_steer_authority() -> dict[str, Any]:
    """The gateway/TUI authority the roster's ``subagent.steer`` RPC checks at call time.

    Mirrors ``delegate_task``'s child registration: the UI session id comes from the request
    context, and its exact transport/live record is captured as an in-process capability. Hosts
    without that context (CLI, plugins) get empty values, leaving only the durable-owner path.
    """
    try:
        from gateway.session_context import get_session_env

        ui_session_id = get_session_env("HERMES_UI_SESSION_ID", "") or ""
    except Exception:  # health: allow BLE001 -- a non-gateway host has no session context
        ui_session_id = ""
    if not ui_session_id:
        return {"owner_session_id": None, "owner_transport": None,
                "owner_session_record": None}
    transport, record = _capture_gateway_steer_authority(ui_session_id)
    return {"owner_session_id": ui_session_id, "owner_transport": transport,
            "owner_session_record": record}


def register_live_session(*, agent_session_id: str, agent: Any, owner_session_id: str,
                          goal: str = "", model: str = "", depth: int = 0,
                          authority: Optional[dict[str, Any]] = None) -> None:
    """Publish one running named agent so the owner's ``delegate_task(steer)`` can reach it."""
    _register_subagent({
        **dict(authority or {}),
        "subagent_id": str(agent_session_id),
        # Tells the shared control plane this record follows the named-session rules (escaped
        # delivery + durable pending) rather than a delegate child's raw steer.
        "origin": "agent_session",
        "parent_id": None,
        "depth": max(0, int(depth)),
        "goal": str(goal or ""),
        "model": str(model or "") or None,
        "started_at": time.time(),
        "status": "running",
        "tool_count": 0,
        "agent": agent,
        "accepting_steer": True,
        # The durable conversation that dispatched the run; control authority resolves here.
        "owner_agent_session_id": str(owner_session_id or "") or None,
    })


def unregister_live_session(agent_session_id: str, agent: Any) -> None:
    """Drop the live record once the run settles; a late steer then becomes pending inbox."""
    _unregister_subagent(str(agent_session_id), agent=agent)


def owns_agent_session(row: dict[str, Any], parent_agent: Any) -> bool:
    """True when *parent_agent*'s conversation started the named session *row* describes.

    Mirrors ``delegate_task``'s durable-tier ownership: exact session id first, then compression
    lineage on either side. An empty owner (a run dispatched without a session) owns nothing.
    """
    owner = str(row.get("parent_session_id") or "")
    parent = str(getattr(parent_agent, "session_id", "") or "")
    if not owner or not parent:
        return False
    if owner == parent:
        return True
    return _resolve_session_lineage(owner, parent_agent) in {
        parent, _resolve_session_lineage(parent, parent_agent)}


def deliver_owner_message(agent_session_id: str, text: str, *, parent_agent: Any) -> dict[str, Any]:
    """Deliver one message from the owning conversation to a named agent session.

    Live run → ``AIAgent.steer()`` (an aside at its next boundary). A resident idle session
    starts a wake turn; a parked session is revived and appended to its transcript. With no
    session at all the record is queued as ``pending`` for the next run/revive. An unknown,
    foreign or cancelled session returns an empty dict, which the control plane reports as its
    usual unknown-target error.
    """
    body = escape_harness_tags(str(text or "").strip())
    if not body:
        raise ValueError("message must not be empty")
    session_id = str(agent_session_id or "").strip()
    sender = str(getattr(parent_agent, "session_id", "") or "")
    row = _session_row(session_id)
    if row is None or not owns_agent_session(row, parent_agent):
        return {}
    if str(row.get("status") or "") == "cancelled":
        return {}
    if steer_subagent(session_id, body):
        return {"agent_session_id": session_id,
                "inbox_id": _record(session_id, sender, body, status="delivered"),
                "status": "delivered",
                "note": "delivered to the live run; it lands at the next tool boundary."}
    from agent.agent_session_runtime import AgentSessionQueueFull, runtime

    try:
        outcome = runtime().deliver(session_id, body, sender=sender, row=row)
    except AgentSessionQueueFull as exc:
        inbox_id = _record(session_id, sender, body, status="pending")
        if not inbox_id:
            return {}
        return {"agent_session_id": session_id, "inbox_id": inbox_id, "status": "pending",
                "queue_full": True,
                "note": f"{exc}; the message is kept as pending and is delivered on the next "
                        "run or revive."}
    if outcome is not None:
        return {"agent_session_id": session_id,
                "inbox_id": _record(session_id, sender, body, status="delivered"), **outcome}
    inbox_id = _record(session_id, sender, body, status="pending")
    if not inbox_id:
        return {}
    note = ("no live run for this agent session; the message is queued in its inbox as pending. "
            "Read it with agent_out mode=inbox — nothing delivers it until the session runs again.")
    return {"agent_session_id": session_id, "inbox_id": inbox_id, "status": "pending",
            "note": note}


def stop_named_session(agent_session_id: str, *, parent_agent: Any,
                       reason: str = "stopped by the owner") -> Optional[dict[str, Any]]:
    """Cancel an owned named session that has no live roster record (idle/parked/older process).

    ``None`` means the id is not an owned named session, so the control plane reports its usual
    unknown-target error. Pending inbox records are failed, not silently dropped.
    """
    session_id = str(agent_session_id or "").strip()
    row = _session_row(session_id)
    if row is None or not owns_agent_session(row, parent_agent):
        return None
    from agent.agent_session_runtime import runtime

    stopped = runtime().stop(session_id, reason=reason)
    if not stopped:
        return None
    return {"agent_session_id": session_id, "status": "cancelled",
            "note": "the named session was stopped; its transcript is kept and pending inbox "
                    "records were marked failed."}


def read_inbox(agent_session_id: str, *, status: str, limit: int = INBOX_PAGE
               ) -> list[dict[str, Any]]:
    """One page of a session's inbox records, newest first; ``[]`` when the store is unavailable."""
    from hermes_state_registry import acquire, release_or_close

    try:
        db = acquire()
    except Exception:  # health: allow BLE001 -- reading must not fail the caller's turn
        logger.debug("agent inbox: no store to read %s", agent_session_id, exc_info=True)
        return []
    try:
        return db.list_agent_inbox(agent_session_id, status=status, limit=limit)
    except Exception:  # health: allow BLE001 -- reading must not fail the caller's turn
        logger.warning("agent inbox: read failed for %s", agent_session_id, exc_info=True)
        return []
    finally:
        release_or_close(db)


def count_inbox(agent_session_id: str, *, status: str) -> int:
    from hermes_state_registry import acquire, release_or_close

    try:
        db = acquire()
    except Exception:  # health: allow BLE001 -- a missing store is "no records"
        return 0
    try:
        return db.count_agent_inbox(agent_session_id, status=status)
    except Exception:  # health: allow BLE001 -- a missing store is "no records"
        logger.debug("agent inbox: count failed for %s", agent_session_id, exc_info=True)
        return 0
    finally:
        release_or_close(db)


def _session_row(agent_session_id: str) -> Optional[dict[str, Any]]:
    from hermes_state_registry import acquire, release_or_close

    try:
        db = acquire()
    except Exception:  # health: allow BLE001 -- lookup failure rejects the delivery
        logger.debug("agent inbox: no store for %s", agent_session_id, exc_info=True)
        return None
    try:
        return db.get_agent_session(agent_session_id)
    except Exception:  # health: allow BLE001 -- lookup failure rejects the delivery
        logger.warning("agent inbox: session lookup failed for %s", agent_session_id,
                       exc_info=True)
        return None
    finally:
        release_or_close(db)


def live_named_sessions() -> list[dict[str, Any]]:
    """Resident sessions (running or idle) as listing rows; parked ones are DB-only."""
    from agent.agent_session_runtime import runtime

    return runtime().snapshot()


def parked_named_sessions(limit: int = 50) -> list[dict[str, Any]]:
    """Parked sessions from the profile store, newest first (they survive a process restart)."""
    from hermes_state_registry import acquire, release_or_close

    try:
        db = acquire()
    except Exception:  # health: allow BLE001 -- a missing store lists nothing
        return []
    try:
        return db.list_agent_sessions(status="parked", limit=limit)
    except Exception:  # health: allow BLE001 -- a missing store lists nothing
        logger.debug("agent inbox: parked listing failed", exc_info=True)
        return []
    finally:
        release_or_close(db)


def session_lifecycle(row: Mapping[str, Any]) -> dict[str, Any]:
    """``live``/``parked``/``state`` for one session row (observability for P4)."""
    from agent.agent_session_runtime import runtime

    session_id = str(row.get("agent_session_id") or "")
    state = runtime().state_of(session_id)
    return {"live": state in ("running", "idle"), "state": state,
            "parked": str(row.get("status") or "") == "parked"}


def _record(agent_session_id: str, sender: str, body: str, *, status: str) -> str:
    """Persist one delivery record; a failed write is logged, never raised into the turn."""
    from hermes_state_registry import acquire, release_or_close

    try:
        db = acquire()
    except Exception:  # health: allow BLE001 -- the delivery itself already succeeded
        logger.debug("agent inbox: no store to record %s", agent_session_id, exc_info=True)
        return ""
    try:
        return db.add_agent_inbox(agent_session_id=agent_session_id, sender=sender,
                                  recipient=agent_session_id, body=body, kind="message",
                                  status=status)
    except Exception:  # health: allow BLE001 -- a lost audit row must not lose the message
        logger.warning("agent inbox: write failed for %s", agent_session_id, exc_info=True)
        return ""
    finally:
        release_or_close(db)


__all__ = [
    "INBOX_PAGE",
    "capture_steer_authority",
    "count_inbox",
    "deliver_owner_message",
    "live_named_sessions",
    "owns_agent_session",
    "parked_named_sessions",
    "read_inbox",
    "register_live_session",
    "session_lifecycle",
    "stop_named_session",
    "unregister_live_session",
]
