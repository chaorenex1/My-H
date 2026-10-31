"""Paged view of a projected display transcript (``session.history``).

One helper for both emitters: the authority handler (``gateway/session_tui_ops.py``) and the legacy
sidecar handler (``tui_gateway/methods_session.py``). It pages projected messages by durable row id
and derives the flat ``entries`` rows the omp client paints, without re-implementing the projection.
"""

from __future__ import annotations


def history_page(messages: list[dict], *, after: int | None, limit: int | None):
    """The paged slice of one display projection plus its ``HistoryEntry`` rows.

    Entries are the projected messages that carry a durable ``row_id`` (tool rows ride the message
    slice, not the entry list); ``after`` keeps rows with a greater id, *limit* cuts the entries.
    A page's messages run from its first entry to the message before the next entry, so a turn's
    tool rows always travel with the turn they answer; ``next_cursor`` is set only when entries
    were left out.
    """
    indexed = [(index, message["row_id"]) for index, message in enumerate(messages)
               if isinstance(message.get("row_id"), int)]
    selected = [(index, row_id) for index, row_id in indexed if after is None or row_id > after]
    page = selected[:limit] if limit is not None else selected
    if not page:  # no page entry: an exhausted cursor, else rows that carry no durable id at all
        return ([] if after is not None else messages), [], None
    stop = selected[len(page)][0] if len(selected) > len(page) else len(messages)
    entries = [{"entry_id": row_id, "role": messages[index]["role"], "text": messages[index].get("text"),
                "timestamp": messages[index].get("timestamp"),
                "display_kind": messages[index].get("display_kind")}
               for index, row_id in page]
    start = 0 if after is None else page[0][0]
    return messages[start:stop], entries, page[-1][1] if len(selected) > len(page) else None
