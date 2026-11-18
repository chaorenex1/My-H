"""``agent_message``: model-visible messaging between named agent sessions.

The sender is the dispatching session (``session_id``): the owning conversation or a named
agent session. Delivery is synchronous inbox/steer work, never a nested run:

- a live target is steered at its next tool boundary;
- a resident idle target gets a wake turn;
- a parked/absent target gets a pending inbox record (`agent_out mode=inbox`).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


def _label_matches(to: str) -> list[dict[str, Any]]:
    """Every agent session whose write-once task label matches *to* exactly (casefolded).

    The label is the first address ``to`` consults (ADR-21 D7). An unreadable store is not a
    refusal — delivery falls through to id/name/subject — and while labels are still being
    generated this simply returns ``[]``, which is the "not labelled yet" path, never a block.
    """
    label = str(to or "").strip()
    if not label:
        return []
    try:
        from hermes_state_registry import acquire, release_or_close

        db = acquire()
    except Exception:  # health: allow BLE001 -- resolution must not depend on the store
        logger.debug("agent_message: no session store for label lookup", exc_info=True)
        return []
    try:
        return db.get_agent_sessions_by_title(label)
    except Exception:  # health: allow BLE001 -- a failed lookup falls back to id/name/subject
        logger.debug("agent_message: label lookup failed for %r", label, exc_info=True)
        return []
    finally:
        release_or_close(db)


def _candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ambiguity payload: what the caller needs to address one of them explicitly."""
    return [
        {
            "agent_session_id": str(row.get("agent_session_id") or ""),
            "name": str(row.get("short_name") or ""),
            "subject": str(row.get("subject") or ""),
            "status": str(row.get("status") or ""),
        }
        for row in rows
    ]


def _resolve_target(args: dict[str, Any]) -> Optional[dict[str, Any]]:
    from tools.agent_out_tool import _resolve

    row_id = str(args.get("agent_session_id") or "").strip()
    name = str(args.get("name") or "").strip()
    subject = str(args.get("subject") or "").strip()
    to = str(args.get("to") or "").strip()
    if row_id or name or subject:
        row = _resolve(row_id, subject, name)
        return row if row is not None else _unknown_identity(row_id, name, subject)
    if not to:
        return None
    # ``to`` resolves label first (the task label is this run's stable address), then an id, a
    # short name or a unique subject. Two live runs can share a label only in a hand-edited
    # store; the caller gets the candidates instead of a guess.
    labels = _label_matches(to)
    if len(labels) > 1:
        return {"_ambiguity": _candidate_rows(labels)}
    if labels:
        return labels[0]
    # A main-conversation id resolves no row here; delivery then falls back to the owner path.
    row = _resolve(to)
    if row is None:
        row = _resolve("", "", to)
    if row is None:
        row = _resolve("", to, "")
    return row if row is not None else {"agent_session_id": to}


def _unknown_identity(row_id: str, name: str, subject: str) -> Optional[dict[str, Any]]:
    """No row for an explicit identity: keep an id-shaped value for the owner fallback."""
    if row_id and not name and not subject:
        return {"agent_session_id": row_id}
    return None


def agent_message_tool(args: dict[str, Any], *, session_id: str = "",
                       task_id: Optional[str] = None, **_: Any) -> str:
    """Send one message to a named agent session; returns its delivery state."""
    row = _resolve_target(args)
    if not row:
        return tool_error("no agent session for the given id/subject")
    ambiguous = row.get("_ambiguity")
    if ambiguous:
        return tool_error(
            f"ambiguous task label: {len(ambiguous)} sessions share it; address one explicitly "
            "by agent_session_id, name or subject",
            candidates=ambiguous,
        )
    target = str(row.get("agent_session_id") or "")
    if not target:
        return tool_error("the target row has no agent_session_id")
    sender = str(task_id or session_id or "").strip()
    try:
        from tools.agent_inbox import deliver_peer_message

        outcome = deliver_peer_message(
            target,
            str(args.get("content") or args.get("message") or ""),
            sender_session_id=sender,
            progress=bool(args.get("progress")),
            reply_to=str(args.get("reply_to") or ""),
        )
    except ValueError as exc:
        return tool_error(str(exc))
    if not outcome:
        return tool_error(
            "message refused: sender is not the target's owner, a sibling, its child, or a "
            "child of the target conversation")
    return json.dumps(outcome, ensure_ascii=False)


AGENT_MESSAGE_SCHEMA: dict[str, Any] = {
    "name": "agent_message",
    "description": (
        "Send a short message to another named agent session. Address it by its task label "
        "(agent_session.title) first, then by agent_session_id, unique subject, profile-unique "
        "short name, or the id of the conversation that dispatched you; an explicit "
        "agent_session_id/name/subject parameter always wins over the free-text address. A live "
        "target receives it at its next tool boundary; a resident idle target wakes; a parked "
        "target gets a pending inbox record (agent_out mode=inbox). The message arrives inside an "
        "<agent_message> envelope whose reply_to attribute is the id to pass back when you "
        "answer. A label shared by more than one session is refused with its candidate "
        "name/subject/agent_session_id list instead of guessing. Named targets accept their "
        "owner, a sibling under the same parent, or their own child; a main conversation accepts "
        "only a named child it dispatched, and only while it is live. Use only for coordination "
        "among named sessions; the main conversation receives your final report, not a message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": ("Free-text address, resolved label-first: a task label "
                                "(agent_session.title), then an agent_session_id, a "
                                "profile-unique short name, a unique subject, or the id of the "
                                "owning conversation (a named child may message its owner). "
                                "Matched exactly after strip()/casefold(); an ambiguous label is "
                                "refused with its candidates."),
            },
            "agent_session_id": {"type": "string", "description": "Target identity."},
            "name": {"type": "string", "description": "Target's profile-unique short name."},
            "subject": {"type": "string", "description": "Target's unique subject."},
            "content": {
                "type": "string",
                "description": "Message text; brief coordination, not a long report.",
            },
            "reply_to": {
                "type": "string",
                "description": ("The inbound envelope's reply_to value when this message answers "
                                "one; omitted for a new thread."),
            },
            "progress": {
                "type": "boolean",
                "description": "Mark this as a progress note; the final answer follows later.",
            },
        },
        "required": ["content"],
    },
}

registry.register(
    name="agent_message",
    toolset="agents",
    schema=AGENT_MESSAGE_SCHEMA,
    handler=agent_message_tool,
    emoji="✉️",
)

__all__ = ["AGENT_MESSAGE_SCHEMA", "agent_message_tool"]
