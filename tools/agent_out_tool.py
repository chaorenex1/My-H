"""``agent_out``: read a named agent run back by its identity (``agent_session_id`` or subject).

The ``agent`` tool returns only identity — an agent session id and its unique subject — because
a background run's answer arrives later and a blocking run's payload is not a durable record.
This tool is the durable read side (ADR-16 D5):

* ``history`` — the run's JSONL transcript, character-capped;
* ``artifact`` — the files written at settle: the verbatim report (``<topic>.md``), one tool
  call's archived output (``<topic>/<step>.<tool>.log`` plus ``-original.log`` when truncated),
  or the bundle's file list;
* ``ast_summary`` — ``<topic>_ast.json``, always present after settle (``sources: []`` and a
  ``reason`` is a valid result, not a missing artifact);
* ``inbox`` — the messages queued for a resident session and the most recent deliveries.

``artifact`` serves named agent sessions only. A main conversation has no bundle and is refused
explicitly rather than answered with an empty payload. Resolution is read-only; every path comes
from the row's recorded ``artifact_manifest`` (the authoritative mapping), never from guessing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import hermes_session_paths as session_paths
from agent import run_artifacts, run_transcript
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

#: The transcript is returned as text; past this budget the payload points at the file instead.
MAX_HISTORY_CHARS = 40_000
MODES = ("history", "artifact", "ast_summary", "inbox")
ARTIFACT_KINDS = ("report", "tool", "all")


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


def _is_conversation(session_id: str) -> bool:
    """True when *session_id* names a main conversation rather than a named agent session."""
    if not session_id:
        return False
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        return db.get_session_bundle_meta(session_id) is not None
    finally:
        release_or_close(db)


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    """The row's public identity plus the bundle paths recorded at settle (ADR-16 D5)."""
    identity = {
        "agent_session_id": row.get("agent_session_id"),
        "subject": row.get("subject"),
        "agent_name": row.get("agent_name"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    identity.update(run_artifacts.identity_for_row(row))
    return identity


def _history_text(row: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, int]:
    """Transcript rows without the internal fingerprint fields, plus truncation bookkeeping."""
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


def _read_text(path: Optional[str]) -> Optional[str]:
    """A file's text, or ``None`` when the path is unset, missing or unreadable."""
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _identity_error() -> str:
    return "pass exactly one of agent_session_id or subject"


def _inbox_payload(row: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    from tools.agent_inbox import INBOX_PAGE, count_inbox, read_inbox, session_lifecycle

    session = str(row.get("agent_session_id") or "")
    pending = read_inbox(session, status="pending", limit=INBOX_PAGE)
    delivered = read_inbox(session, status="delivered", limit=INBOX_PAGE)
    lifecycle = session_lifecycle(row)
    payload = {**identity, "mode": "inbox", "pending": pending, "delivered": delivered,
               "pending_count": count_inbox(session, status="pending"),
               "delivered_count": count_inbox(session, status="delivered"),
               **lifecycle}
    if not pending and not delivered:
        payload["note"] = "this agent session has no inbox records"
    elif pending:
        payload["note"] = (
            "pending records wait for the next run/revive; a live resident session drains "
            "them with a wake turn, and a parked session is revived by a message."
            if lifecycle["live"] or lifecycle["parked"] else
            "pending records have not reached a live run; they are delivered when the "
            "session runs again.")
    elif lifecycle["parked"]:
        payload["note"] = ("this session is parked (idle TTL); send it a message to revive "
                           "it, or stop it to cancel.")
    return payload


def _tool_artifact(identity: dict[str, Any], step: Any,
                   tool_name: str) -> tuple[Optional[Path], Optional[Path]]:
    """``(<log>, <-original.log>)`` for one step; either may not exist."""
    try:
        number = int(step)
    except (TypeError, ValueError):
        return None, None
    if number < 1:
        return None, None
    directory = identity.get("tool_artifacts_dir")
    if not directory:
        return None, None
    return session_paths.tool_artifact_paths(directory, number, tool_name)


def _artifact_files(identity: dict[str, Any]) -> list[dict[str, Any]]:
    """Every artifact file that actually exists for this session (kind=all)."""
    entries: list[dict[str, Any]] = []
    for kind, key in (("report", "report_path"), ("ast_summary", "ast_summary_path"),
                      ("structured", "structured_output_path"),
                      ("transcript", "transcript_path")):
        raw = identity.get(key)
        if not raw:
            continue
        path = Path(str(raw))
        try:
            if path.is_file():
                entries.append({"kind": kind, "path": str(path),
                                "bytes": path.stat().st_size})
        except OSError:
            continue
    tool_dir = identity.get("tool_artifacts_dir")
    if tool_dir:
        try:
            children = sorted(Path(str(tool_dir)).iterdir())
        except OSError:
            children = []
        for child in children:
            try:
                if child.is_file():
                    entries.append({"kind": "tool", "path": str(child),
                                    "bytes": child.stat().st_size})
            except OSError:
                continue
    return entries


def _artifact_payload(identity: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    kind = str(args.get("kind") or "report").strip() or "report"
    payload: dict[str, Any] = {**identity, "mode": "artifact", "kind": kind}
    if kind == "report":
        content = _read_text(identity.get("report_path"))
        payload["content"] = content
        if content is None:
            payload["note"] = ("this run recorded no report (an errored run has an ast summary "
                               "but no report); report_path is where it would live")
        return payload
    if kind == "tool":
        tool_name = str(args.get("tool_name") or "").strip()
        if not tool_name:
            return {"error": "artifact kind=tool requires tool_name"}
        log_path, original_path = _tool_artifact(identity, args.get("step"), tool_name)
        if log_path is None:
            return {"error": "artifact kind=tool requires a step >= 1"}
        log_text = _read_text(str(log_path))
        original_text = _read_text(str(original_path)) if original_path else None
        payload.update({
            "tool_name": tool_name,
            "step": int(args.get("step") or 0),
            "path": str(log_path),
            "content": log_text,
            "original_path": str(original_path) if original_path else None,
            "original_content": original_text,
            "truncated": original_text is not None,
        })
        if log_text is None:
            payload["note"] = "no archived output exists for that step and tool name"
        return payload
    payload["files"] = _artifact_files(identity)
    if not payload["files"]:
        payload["note"] = "this bundle has no artifact files (a pre-P2 session has none)"
    return payload


def _ast_payload(identity: dict[str, Any]) -> dict[str, Any]:
    path = identity.get("ast_summary_path")
    payload: dict[str, Any] = {**identity, "mode": "ast_summary", "ast_summary": None,
                               "path": path}
    text = _read_text(path)
    if text is None:
        payload["note"] = ("this session has no ast summary (a pre-P2 session has none); "
                           "ast_summary_path is where settle writes it")
        return payload
    try:
        payload["ast_summary"] = json.loads(text)
    except (TypeError, ValueError):
        payload["note"] = "the ast summary file is not valid JSON"
    return payload


def agent_out_tool(args: dict[str, Any], *, session_id: str = "",
                   task_id: Optional[str] = None, **_: Any) -> str:
    """Read one agent session's transcript, artifacts, ast summary or inbox."""
    mode = str(args.get("mode") or "history").strip() or "history"
    if mode not in MODES:
        return tool_error(f"mode must be one of {', '.join(MODES)}")
    if mode == "artifact" and str(args.get("kind") or "report") not in ARTIFACT_KINDS:
        return tool_error(f"artifact kind must be one of {', '.join(ARTIFACT_KINDS)}")
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
        if mode == "artifact" and row_id and _is_conversation(row_id):
            return tool_error(
                f"artifact mode reads named agent sessions only; {row_id!r} is a conversation "
                "session with no agent bundle")
        which = f"agent_session_id={row_id}" if row_id else f"subject={subject!r}"
        return tool_error(f"no agent session for {which}")
    identity = _identity(row)
    if mode == "inbox":
        return json.dumps(_inbox_payload(row, identity), ensure_ascii=False)
    if mode == "artifact":
        return json.dumps(_artifact_payload(identity, args), ensure_ascii=False)
    if mode == "ast_summary":
        return json.dumps(_ast_payload(identity), ensure_ascii=False)
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
        "Read a named agent run back by agent_session_id or subject. mode=history (default) "
        "returns the session's JSONL transcript; artifact returns the settle-time files — "
        "kind=report (the verbatim final report), kind=tool (step + tool_name, the archived "
        "output and its pre-truncation original when it existed) or kind=all (the file list); "
        "ast_summary returns the structural summary written at settle; inbox returns the "
        "messages queued for the session plus the most recent deliveries. Use the identity the "
        "agent tool returned. artifact serves named agent sessions only; a conversation session "
        "is refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(MODES),
                "description": "What to read; defaults to history.",
            },
            "agent_session_id": {
                "type": "string",
                "description": "The agent session id the agent tool returned.",
            },
            "subject": {
                "type": "string",
                "description": "The agent session's unique subject; alternative to the id.",
            },
            "kind": {
                "type": "string",
                "enum": list(ARTIFACT_KINDS),
                "description": "mode=artifact: report (default), tool, or all.",
            },
            "step": {
                "type": "integer",
                "minimum": 1,
                "description": "mode=artifact kind=tool: the tool call's per-session step.",
            },
            "tool_name": {
                "type": "string",
                "description": "mode=artifact kind=tool: the tool whose output to read.",
            },
        },
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
