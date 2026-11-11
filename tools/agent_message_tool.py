"""``agent_message``: model-visible messaging between named agent sessions.

The sender is the dispatching session (``session_id``): the owning conversation or a named
agent session. Delivery is synchronous inbox/steer work, never a nested run:

- a live target is steered at its next tool boundary;
- a resident idle target gets a wake turn;
- a parked/absent target gets a pending inbox record (`agent_out mode=inbox`).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from tools.registry import registry, tool_error


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
    # ``to`` accepts an id, a short name or a unique subject, in that order. A
    # main-conversation id resolves no row here; delivery then falls back to the owner path.
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
        "Send a short message to another named agent session, by agent_session_id, unique "
        "subject, or the id of the conversation that dispatched you. A live target receives it "
        "at its next tool boundary; a resident idle target wakes; a parked target gets a "
        "pending inbox record (agent_out mode=inbox). The message arrives inside an "
        "<agent_message> envelope whose reply_to attribute is the id to pass back when you "
        "answer. Named targets accept their owner, a sibling under the same parent, or their "
        "own child; a main conversation accepts only a named child it dispatched, and only "
        "while it is live. Use only for coordination among named sessions; the main "
        "conversation receives your final report, not a message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": ("Target agent_session_id, profile-unique short name, a unique "
                                "subject, or the id of the owning conversation (a named child "
                                "may message its owner)."),
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
