"""Session-owned inbox for named agents (ADR-15 D4, phase P3).

One named agent run is one agent session. While that run is live its ``AIAgent`` is steered at
the next tool boundary — the *aside* queue. When no run is live, the text is persisted in
``agent_inbox`` as ``pending`` and ``agent_out(mode="inbox")`` reads it back; the wake turn (P4)
drains it later. Every body is escaped with :func:`agent.ttsr_prompts.escape_harness_tags`, so a
message can never close or forge a harness tag.

Delivery is scoped by the durable lineage: a named target accepts its owning conversation, a
sibling under the same parent, or its own child; a main conversation accepts only a named child
it dispatched (and only while a live surface holds its lease, so an offline owner is reported
``pending`` instead of a fake delivery). That is the same authority ``delegate_task`` already
enforces, extended one step in each direction so peer coordination is genuinely bidirectional.
"""

from __future__ import annotations

import html
import logging
import re
import time
import uuid
from pathlib import Path
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

#: The delivered message's own id is the value the recipient passes back as ``reply_to``.
_ENVELOPE_OPEN_RE = re.compile(r"<(?=\s*/?\s*agent_message(?![\w-]))", re.IGNORECASE)


def _attr(value: Any) -> str:
    """One envelope attribute value: quotes escaped, newlines flattened, never a second line."""
    return html.escape(" ".join(str(value or "").split()), quote=True)


def _escape_body(text: str) -> str:
    """Escape harness tags and the envelope's own tag so a body can never close the envelope."""
    return _ENVELOPE_OPEN_RE.sub("&lt;", escape_harness_tags(str(text or "")))


def agent_message_envelope(*, sender: str, sender_subject: str = "", inbox_id: str,
                           body: str, reply_to: str = "", progress: bool = False) -> str:
    """One model-facing peer message: server-resolved identity around escaped text.

    ``reply_to`` is this delivery's own inbox id (what the recipient passes back to answer);
    ``in_reply_to`` names the record this message itself answers, when the sender supplied one.
    """
    attrs = [f'from="{_attr(sender)}"', f'from_subject="{_attr(sender_subject)}"',
             f'reply_to="{_attr(inbox_id)}"', f'progress="{"true" if progress else "false"}"']
    if reply_to:
        attrs.append(f'in_reply_to="{_attr(reply_to)}"')
    return f"<agent_message {' '.join(attrs)}>\n{_escape_body(body)}\n</agent_message>"


def _envelope_enabled() -> bool:
    """``agent_messages.envelope`` rollout switch; unreadable config keeps the envelope on."""
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        value = cfg_get(load_config_readonly(), "agent_messages", "envelope", default=True)
    except Exception:  # health: allow BLE001 -- config must not decide whether delivery works
        logger.debug("agent_message envelope: config unreadable", exc_info=True)
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return value is not False


def _delivery_text(*, sender: str, sender_subject: str, inbox_id: str, body: str,
                   reply_to: str = "", progress: bool = False) -> str:
    """The model-facing text: the envelope, or the escaped body when the switch is off."""
    if not _envelope_enabled():
        # No envelope around it, but the body must still not be able to *forge* one.
        return _escape_body(body)
    return agent_message_envelope(
        sender=sender, sender_subject=sender_subject, inbox_id=inbox_id,
        body=body, reply_to=reply_to, progress=progress)


def envelope_inbox_row(row: Mapping[str, Any], *, profile: str = "") -> str:
    """The envelope for one persisted inbox row (a wake joining raw bodies)."""
    if not _envelope_enabled():
        return _escape_body(str(row.get("body") or ""))
    return agent_message_envelope(
        sender=str(row.get("sender") or ""),
        sender_subject=_sender_subject(str(row.get("sender") or ""), profile=profile),
        inbox_id=str(row.get("id") or ""),
        body=str(row.get("body") or ""),
        reply_to=str(row.get("reply_to") or ""),
        progress=bool(row.get("progress")),
    )


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


def deliver_peer_message(agent_session_id: str, text: str, *, sender_session_id: str,
                         progress: bool = False, reply_to: str = "",
                         profile: str = "") -> dict[str, Any]:
    """Deliver a message between named agent sessions, or from a named child to its owner.

    Named targets accept their owner, a sibling under the same parent, or their own child.
    The owning *conversation* (a ``sessions`` row, not an ``agent_session``) accepts only its
    own child, and only while a live surface holds its lease: its delivery goes through
    ``completion_queue``; an offline owner gets an explicit ``pending`` instead of a fake
    "delivered". An unauthorized or unknown target returns ``{}``.
    """
    body = escape_harness_tags(str(text or "").strip())
    if not body:
        raise ValueError("message must not be empty")
    target_id = str(agent_session_id or "").strip()
    sender = str(sender_session_id or "").strip()
    if not target_id or not sender or sender == target_id:
        return {}
    target = _session_row(target_id, profile=profile)
    sender_row = _session_row(sender, profile=profile)
    if target is not None:
        if str(target.get("status") or "") == "cancelled":
            return {}
        target_parent = str(target.get("parent_session_id") or "")
        is_owner = sender == target_parent
        same_parent = bool(sender_row) and str(
            sender_row.get("parent_session_id") or "") == target_parent
        is_child = bool(sender_row) and str(
            sender_row.get("parent_session_id") or "") == target_id
        if not (is_owner or same_parent or is_child):
            return {}
        return _deliver_to_named(target_id, body, target=target, sender=sender,
                                 sender_row=sender_row, progress=progress, reply_to=reply_to,
                                 profile=profile)
    # A named child may address the conversation that dispatched it. That owner has no
    # agent_session row, so both the target and the authorization resolve through `sessions`.
    owner_row = _conversation_row(target_id, profile=profile)
    if owner_row is None:
        return {}
    if not sender_row or str(sender_row.get("parent_session_id") or "") != target_id:
        return {}
    return _deliver_to_owner(target_id, body, owner_row=owner_row, sender=sender,
                             sender_row=sender_row, progress=progress, reply_to=reply_to,
                             profile=profile)


def _deliver_to_named(target_id: str, body: str, *, target: Mapping[str, Any], sender: str,
                      sender_row: Optional[Mapping[str, Any]], progress: bool,
                      reply_to: str, profile: str = "") -> dict[str, Any]:
    inbox_id = f"inbox-{uuid.uuid4().hex}"
    enveloped = _delivery_text(
        sender=sender, sender_subject=_row_subject(sender_row), inbox_id=inbox_id,
        body=body, reply_to=reply_to, progress=progress)

    def record(status: str) -> None:
        _record(target_id, sender, body, status=status, progress=progress,
                reply_to=reply_to, inbox_id=inbox_id, profile=profile)

    if steer_subagent(target_id, enveloped):
        record("delivered")
        return {"agent_session_id": target_id, "inbox_id": inbox_id, "status": "delivered",
                "note": "delivered to the live run; it lands at the next tool boundary."}
    from agent.agent_session_runtime import AgentSessionQueueFull, runtime

    try:
        outcome = runtime().deliver(target_id, enveloped, sender=sender, row=target)
    except AgentSessionQueueFull as exc:
        record("pending")
        return {"agent_session_id": target_id, "inbox_id": inbox_id, "status": "pending",
                "queue_full": True,
                "note": f"{exc}; the message is kept as pending."}
    if outcome is None:
        record("pending")
        return {"agent_session_id": target_id, "inbox_id": inbox_id, "status": "pending",
                "note": "queued in the target's inbox; read it with agent_out mode=inbox."}
    status = str(outcome.get("status") or "")
    record("pending" if status == "pending" else "delivered")
    return {"agent_session_id": target_id, "inbox_id": inbox_id, **outcome}


def _deliver_to_owner(owner_id: str, body: str, *, owner_row: Mapping[str, Any], sender: str,
                      sender_row: Optional[Mapping[str, Any]], progress: bool,
                      reply_to: str, profile: str = "") -> dict[str, Any]:
    inbox_id = f"inbox-{uuid.uuid4().hex}"
    enveloped = _delivery_text(
        sender=sender, sender_subject=_row_subject(sender_row), inbox_id=inbox_id,
        body=body, reply_to=reply_to, progress=progress)
    # Gateway leases and queue routing use the composited ``agent:...`` session key; CLI/TUI
    # use the conversation id. Queue the key the surface actually polls.
    session_key = str(owner_row.get("session_key") or owner_id)
    if not _owner_is_live(owner_id, session_key, profile=profile):
        return {"agent_session_id": owner_id, "inbox_id": inbox_id, "status": "pending",
                "note": "the owner conversation has no live surface; the message was not queued."}
    try:
        from tools.process_registry import process_registry

        process_registry.completion_queue.put({
            "type": "agent_message", "role": "agent_message",
            "session_key": session_key, "agent_session_id": sender,
            "from": sender, "from_subject": _row_subject(sender_row),
            "inbox_id": inbox_id, "reply_to": inbox_id, "progress": bool(progress),
            "text": enveloped, "content": body,
        })
    except Exception:  # health: allow BLE001 -- a broken queue must not claim delivery
        logger.warning("agent inbox: owner queue failed for %s", owner_id, exc_info=True)
        return {"agent_session_id": owner_id, "inbox_id": inbox_id, "status": "pending",
                "note": "the owner's delivery queue is unavailable; the message was not queued."}
    return {"agent_session_id": owner_id, "inbox_id": inbox_id, "status": "delivered",
            "session_key": session_key,
            "note": "queued for the owner's next turn boundary."}


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


def _row_subject(row: Optional[Mapping[str, Any]]) -> str:
    return str((row or {}).get("subject") or "")


def _sender_subject(sender: str, *, profile: str = "") -> str:
    """The sender's subject for the envelope; an owner conversation has none."""
    return _row_subject(_session_row(sender, profile=profile)) if sender else ""


def _conversation_row(session_id: str, *, profile: str = "") -> Optional[dict[str, Any]]:
    """The main conversation row a child addresses; the row carries its routable session_key."""
    from hermes_state_registry import acquire, release_or_close

    db_path = (Path(profile) / "state.db") if str(profile or "").strip() else None
    try:
        db = acquire(db_path)
    except Exception:  # health: allow BLE001 -- an unreadable store rejects the delivery
        return None
    try:
        return db.get_session(session_id)
    except Exception:  # health: allow BLE001 -- an unreadable store rejects the delivery
        logger.debug("agent inbox: conversation lookup failed for %s", session_id, exc_info=True)
        return None
    finally:
        release_or_close(db)


def _owner_is_live(session_id: str, session_key: str = "", *, profile: str = "") -> bool:
    """True when a live surface holds the conversation's lease; unprovable counts as offline.

    Gateway leases are keyed on the composited ``agent:...`` session key while CLI/TUI leases
    use the conversation id, so either marker proves the owner is reachable.
    """
    wanted = {str(session_id or ""), str(session_key or "")} - {""}
    if not wanted:
        return False
    try:
        from hermes_cli.active_sessions import active_session_registry_snapshot

        registry_home = Path(profile) if str(profile or "").strip() else None
        return any(str(entry.get("session_id") or "") in wanted
                   for entry in active_session_registry_snapshot(registry_home))
    except Exception:  # health: allow BLE001 -- never claim delivery on an unprovable owner
        logger.debug("agent inbox: owner liveness unreadable for %s", session_id, exc_info=True)
        return False


def _session_row(agent_session_id: str, *, profile: str = ""
                 ) -> Optional[dict[str, Any]]:
    from hermes_state_registry import acquire, release_or_close

    db_path = (Path(profile) / "state.db") if str(profile or "").strip() else None
    try:
        db = acquire(db_path)
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


def _record(agent_session_id: str, sender: str, body: str, *, status: str,
            progress: bool = False, reply_to: str = "", inbox_id: str = "",
            profile: str = "") -> str:
    """Persist one delivery record; a failed write is logged, never raised into the turn."""
    from hermes_state_registry import acquire, release_or_close

    db_path = (Path(profile) / "state.db") if str(profile or "").strip() else None
    try:
        db = acquire(db_path)
    except Exception:  # health: allow BLE001 -- the delivery itself already succeeded
        logger.debug("agent inbox: no store to record %s", agent_session_id, exc_info=True)
        return ""
    try:
        return db.add_agent_inbox(agent_session_id=agent_session_id, sender=sender,
                                  recipient=agent_session_id, body=body, kind="message",
                                  reply_to=reply_to, progress=progress, status=status,
                                  inbox_id=inbox_id)
    except Exception:  # health: allow BLE001 -- a lost audit row must not lose the message
        logger.warning("agent inbox: write failed for %s", agent_session_id, exc_info=True)
        return ""
    finally:
        release_or_close(db)


__all__ = [
    "INBOX_PAGE",
    "agent_message_envelope",
    "capture_steer_authority",
    "count_inbox",
    "deliver_owner_message",
    "deliver_peer_message",
    "envelope_inbox_row",
    "live_named_sessions",
    "owns_agent_session",
    "parked_named_sessions",
    "read_inbox",
    "register_live_session",
    "session_lifecycle",
    "stop_named_session",
    "unregister_live_session",
]
