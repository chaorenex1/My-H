"""One late LSP verdict batch as a durable transcript row (ADR-9 D1/D3).

The three pre-existing delivery points — the next tool result, turn end, the next user turn — all
call :func:`deliver` instead of pasting a note into another row's content. ``custom_messages``
sessions (omp-ui) get a real ``role:"custom"`` row; every other session keeps the legacy text paths
(``agent.plugin_live_notes`` / the turn-end user note), byte for byte.

A row is only announced after its flush stamped ``_row_id``: a fabricated id would make resume
paint the same block twice (ADR-9 D10).
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from agent.message_metadata import append_message

logger = logging.getLogger("agent.lsp.delivery")

CUSTOM_TYPE = "lsp-late-diagnostic"
_PENDING_ATTR = "_late_diagnostics_pending_rows"


def custom_messages_enabled() -> bool:
    """True for a session that asked for ADR-10 custom rows (omp-ui's ``client`` gate)."""
    try:
        from gateway.session_policy import active_policy

        return bool(getattr(active_policy(), "custom_messages", False))
    except Exception:  # a standalone agent has no policy; legacy shape is the safe default
        logger.debug("session policy read failed; using legacy late-diagnostics delivery", exc_info=True)
        return False


def live_event_allowed() -> bool:
    """True only for the omp-ui local viewer: the one ``transcript.custom`` consumer (ADR-6 D1)."""
    try:
        from gateway.session_policy import active_policy

        return getattr(active_policy(), "client", None) == "omp-ui"
    except Exception:  # health: allow BLE001 -- a policy read must never break an event emit
        logger.debug("session policy read failed; suppressing transcript.custom", exc_info=True)
        return False


def build_row(notes: Iterable[tuple[str, Any]]) -> Optional[dict[str, Any]]:
    """The custom row for one delivery point's notes, or ``None`` when nothing survives the filter.

    ``notes`` is ``deferred.pop_notes``' ``(path, Note)`` pairs; every note already carries the
    severity-filtered verdict, so the row and the model content are the same facts (ADR-9 D12).
    """
    from agent.lsp.reporter import cap_display_files, merge_file_entries, model_text

    notes = list(notes)
    # One group per queued note, in pop order: ADR-9 D2 keeps ``files`` in that order, while the
    # per-path merge inside a note still puts LSP before Semgrep (D8).
    groups = [list(getattr(note, "files", ()) or ()) + list(getattr(note, "semgrep_files", ()) or ())
              for _path, note in notes]
    files = cap_display_files(merge_file_entries(*groups))
    text = model_text(files)
    if not text:
        return None
    return {
        "role": "custom",
        "custom_type": CUSTOM_TYPE,
        "content": text,
        "display": True,
        "display_only": False,
        "details": {"files": files},
        "attribution": "agent",
        "legacy_role": "user",
    }


def deliver(agent: Any, messages: list[dict[str, Any]], notes: Iterable[tuple[str, Any]],
            conversation_history: Optional[list[dict[str, Any]]] = None) -> Optional[dict[str, Any]]:
    """Append, flush and announce one custom late-diagnostics row; ``None`` when empty."""
    row = build_row(notes)
    if row is None:
        return None
    append_message(messages, row)
    _flush(agent, messages, conversation_history)
    _notify_if_durable(agent, row)
    return row


def insert_row(agent: Any, messages: list[dict[str, Any]], notes: Iterable[tuple[str, Any]],
               index: int) -> Optional[dict[str, Any]]:
    """Insert the row at ``index`` and hold it until the caller's flush makes it durable.

    The turn-start path persists the whole message list in ``_persist_turn_start``; announcing here
    would race that flush, so the row is queued for :func:`notify_ready`.
    """
    row = build_row(notes)
    if row is None:
        return None
    from agent.message_metadata import stamp_message_timestamp

    stamp_message_timestamp(row)
    messages.insert(max(0, index), row)
    pending = getattr(agent, _PENDING_ATTR, None)
    pending = pending if isinstance(pending, list) else []
    pending.append(row)
    setattr(agent, _PENDING_ATTR, pending)
    return row


def notify_ready(agent: Any) -> None:
    """Announce held rows whose flush stamped a durable ``_row_id``; keep the rest for the next flush."""
    pending = getattr(agent, _PENDING_ATTR, None)
    if not isinstance(pending, list) or not pending:
        return
    waiting: list[dict[str, Any]] = []
    for row in pending:
        if not _notify_if_durable(agent, row):
            waiting.append(row)
    setattr(agent, _PENDING_ATTR, waiting)


def _flush(agent: Any, messages: list[dict[str, Any]],
           conversation_history: Optional[list[dict[str, Any]]]) -> None:
    flush = getattr(agent, "_flush_messages_to_session_db", None)
    if not callable(flush):
        return
    try:
        flush(messages, conversation_history)
    except Exception:  # persistence is best-effort; the row stays queued in the transcript
        logger.debug("late-diagnostics row flush failed", exc_info=True)


def _notify_if_durable(agent: Any, row: dict[str, Any]) -> bool:
    """Emit the live event only for a persisted row. Never synthesise ``row_id`` (ADR-9 D10)."""
    if not isinstance(row.get("_row_id"), int):
        return False
    from agent.plugin_events import notify_plugin_message

    notify_plugin_message(agent, row)
    return True


__all__ = ["CUSTOM_TYPE", "build_row", "custom_messages_enabled", "deliver", "insert_row",
           "live_event_allowed", "notify_ready"]
