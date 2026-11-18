"""Paged view of a projected display transcript (``session.history``).

One helper for both emitters: the authority handler (``gateway/session_tui_ops.py``) and the legacy
sidecar handler (``tui_gateway/methods_session.py``). It pages projected messages by durable row id
and derives the flat ``entries`` rows the omp client paints, without re-implementing the projection.
"""

from __future__ import annotations

# Display-only fields one entry forwards verbatim from its projected message (ADR-10 D4/D5).
_ENTRY_FIELDS = ("content", "args", "labels", "tool_call_id", "name", "context", "is_error", "reasoning",
                 "synthetic_kind", "live_steered")
# Outline summaries are selector rows: one collapsed line, capped at 80 characters (ADR-2 D6).
_OUTLINE_SUMMARY_CHARS = 80


def _outline_summary(entry: dict) -> str:
    """One selector line: the row's first non-empty text line, else ``name context``, else its kind."""
    text = entry.get("text")
    if isinstance(text, str) and text.strip():
        line = text.strip().splitlines()[0]
        return " ".join(line.split())[:_OUTLINE_SUMMARY_CHARS]
    name = entry.get("name")
    if isinstance(name, str) and name:
        return " ".join(f"{name} {entry.get('context') or ''}".split())[:_OUTLINE_SUMMARY_CHARS]
    kind = entry.get("custom_type")
    return kind[:_OUTLINE_SUMMARY_CHARS] if isinstance(kind, str) else ""


def history_page(messages: list[dict], *, after: int | None, limit: int | None,
                 include_outline: bool = False):
    """The paged slice of one display projection plus its ``HistoryEntry`` rows.

    Entries are the projected messages that carry a durable ``row_id`` (tool rows included: their
    ``row_id`` is the durable result row); ``after`` keeps rows with a greater id, *limit* cuts the
    entries. A page's messages run from its first entry to the message before the next entry, so a
    page never splits the gap between two entries (a tool row that is itself an entry opens the next
    page); ``next_cursor`` is set only when entries were left out. ``include_outline`` derives the
    selector rows from this same page.
    """
    indexed = [(index, message["row_id"]) for index, message in enumerate(messages)
               if isinstance(message.get("row_id"), int)]
    selected = [(index, row_id) for index, row_id in indexed if after is None or row_id > after]
    page = selected[:limit] if limit is not None else selected
    if not page:  # no page entry: an exhausted cursor, else rows that carry no durable id at all
        return ([] if after is not None else messages), [], None, ([] if include_outline else None)
    stop = selected[len(page)][0] if len(selected) > len(page) else len(messages)
    from agent.custom_messages import custom_fields
    entries = []
    for index, row_id in page:
        message = messages[index]
        entry = {"entry_id": row_id, "role": message["role"], "text": message.get("text"),
                 "timestamp": message.get("timestamp")}
        entry.update(custom_fields(message))
        entry.update({key: message[key] for key in _ENTRY_FIELDS if message.get(key) is not None})
        entries.append(entry)
    outline = [{"row_id": entry["entry_id"], "role": entry["role"], "summary": _outline_summary(entry)}
               for entry in entries] if include_outline else None
    start = 0 if after is None else page[0][0]
    return messages[start:stop], entries, page[-1][1] if len(selected) > len(page) else None, outline
