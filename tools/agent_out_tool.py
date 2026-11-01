"""``agent_out``: read a named agent run back by its identity (``agent_session_id`` or subject).

The ``agent`` tool returns only identity — an agent session id and its unique subject — because
a background run's answer arrives later and a blocking run's payload is not a durable record.
This tool is the durable read side, in three modes:

* ``final_response`` — the settled answer, or ``null`` with the row's status while it runs;
* ``artifact`` — the run's artifact manifest (``null`` until a run records one; the transcript
  path is always reported so nothing is silently lost);
* ``history`` — the full JSONL transcript of that one agent session, character-capped.

Resolution is read-only against ``state.db``; the transcript is the file
``agent.run_transcript`` wrote. Both are per profile, so a caller served by another profile does
not see this one's runs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

#: The transcript is returned as text; past this budget the payload points at the file instead.
MAX_HISTORY_CHARS = 40_000
MODES = ("final_response", "artifact", "history")


def _resolve(row_id: str, subject: str) -> Optional[dict[str, Any]]:
    """The one row the identity names; both forms are accepted, neither guesses."""
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        if row_id:
            return db.get_agent_session(row_id)
        return db.get_agent_session_by_subject(subject)
    finally:
        release_or_close(db)


def _history_text(row: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, int]:
    """Transcript rows without the internal fingerprint fields, plus truncation bookkeeping."""
    from agent import run_transcript

    rows = run_transcript.read_events(str(row.get("agent_name") or ""),
                                      str(row.get("agent_session_id") or ""))
    kept: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for event in rows:
        item = {key: value for key, value in event.items() if key not in ("fp", "agent_session_id")}
        size = len(json.dumps(item, ensure_ascii=False, default=str))
        if used + size > MAX_HISTORY_CHARS:
            truncated = True
            break
        used += size
        kept.append(item)
    return kept, truncated, len(rows)


def _identity_error() -> str:
    return "pass exactly one of agent_session_id or subject"


def agent_out_tool(args: dict[str, Any], *, session_id: str = "",
                   task_id: Optional[str] = None, **_: Any) -> str:
    """Read one agent session's answer, artifact or transcript."""
    mode = str(args.get("mode") or "final_response").strip()
    if mode not in MODES:
        return tool_error(f"mode must be one of {', '.join(MODES)}")
    row_id = str(args.get("agent_session_id") or "").strip()
    subject = str(args.get("subject") or "").strip()
    if bool(row_id) == bool(subject):
        return tool_error(_identity_error())
    try:
        row = _resolve(row_id, subject)
    except Exception as exc:  # health: allow BLE001 -- the tool reports, it does not crash the turn
        logger.warning("agent_out: lookup failed: %s", exc, exc_info=True)
        return tool_error(f"agent session lookup failed: {exc}")
    if not row:
        which = f"agent_session_id={row_id}" if row_id else f"subject={subject!r}"
        return tool_error(f"no agent session for {which}")
    identity = {"agent_session_id": row.get("agent_session_id"), "subject": row.get("subject"),
                "agent_name": row.get("agent_name"), "status": row.get("status"),
                "transcript_path": row.get("transcript_path"),
                "created_at": row.get("created_at"), "updated_at": row.get("updated_at")}
    if mode == "final_response":
        answer = row.get("final_response")
        payload = {**identity, "mode": mode, "final_response": answer}
        if answer is None:
            payload["note"] = "the run has not settled; its final response is not stored yet"
        return json.dumps(payload, ensure_ascii=False)
    if mode == "artifact":
        manifest = row.get("artifact_manifest")
        payload = {**identity, "mode": mode, "artifact": manifest}
        if manifest is None:
            payload["note"] = ("this run recorded no artifact manifest; its transcript is at "
                               "transcript_path")
        return json.dumps(payload, ensure_ascii=False)
    try:
        kept, truncated, total = _history_text(row)
    except Exception as exc:  # health: allow BLE001 -- an unreadable file is reported, not raised
        logger.warning("agent_out: transcript read failed: %s", exc, exc_info=True)
        return tool_error(f"transcript read failed: {exc}")
    payload = {**identity, "mode": mode, "events": kept, "event_count": total,
               "truncated": truncated}
    if truncated:
        payload["note"] = (f"showing the first {len(kept)} of {total} events; the rest is in "
                           "transcript_path")
    return json.dumps(payload, ensure_ascii=False)


AGENT_OUT_SCHEMA: dict[str, Any] = {
    "name": "agent_out",
    "description": (
        "Read a named agent run back by agent_session_id or subject. mode=final_response returns "
        "the settled answer, artifact returns the run's recorded artifact manifest, history "
        "returns the agent session's full JSONL transcript. Use the identity the agent tool "
        "returned; a run that has not settled reports status instead of an answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(MODES),
                "description": "What to read; defaults to final_response.",
            },
            "agent_session_id": {
                "type": "string",
                "description": "The agent session id the agent tool returned.",
            },
            "subject": {
                "type": "string",
                "description": "The agent session's unique subject; alternative to the id.",
            },
        },
        "required": ["mode"],
    },
}

registry.register(
    name="agent_out",
    toolset="agents",
    schema=AGENT_OUT_SCHEMA,
    handler=agent_out_tool,
    emoji="📤",
)

__all__ = ["AGENT_OUT_SCHEMA", "agent_out_tool"]
