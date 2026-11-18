"""Display-only ``stripped-tool-calls`` block: pair an assistant batch's calls with its tool rows.

A call is elided when a closed batch has no projected tool row carrying its persisted
per-occurrence uid (``_tool_call_uids`` / ``_tool_call_uid``). Provider ids repeat, so the uid is
the pairing key; a legacy row without a map falls back to its id variants. The block is inserted
after the batch's last projected row, and a ``running`` session leaves its trailing open batch
unsettled (those calls may still be executing).
"""

from __future__ import annotations

STRIPPED_TOOL_CALLS = "stripped-tool-calls"


def is_stripped_tool_calls(message: dict) -> bool:
    """True for the display-only elided-calls block (text diagnostics skip it)."""
    return message.get("custom_type") == STRIPPED_TOOL_CALLS


def _tool_call_occurrences(assistant: dict) -> list[tuple[str | None, frozenset[str]]]:
    """One ``(uid, id variants)`` pair per ``tool_calls`` entry on an assistant row.

    A folded row stores a list of uids for one provider id: the nth occurrence takes the nth uid.
    A legacy row without ``_tool_call_uids`` yields ``None`` uids and pairs by id variants instead.
    """
    from agent.message_metadata import TOOL_CALL_UIDS
    from agent.message_sanitization import coalesce_tool_call_id, tool_call_id_variants

    uids = assistant.get(TOOL_CALL_UIDS)
    uids = uids if isinstance(uids, dict) else {}
    seen: dict[str, int] = {}
    occurrences: list[tuple[str | None, frozenset[str]]] = []
    for tc in assistant.get("tool_calls") or ():
        call_id = coalesce_tool_call_id(tc)
        uid = uids.get(call_id)
        if isinstance(uid, list):
            nth = seen[call_id] = seen.get(call_id, -1) + 1
            uid = uid[nth] if nth < len(uid) else None
        occurrences.append((uid if isinstance(uid, str) and uid else None,
                            tool_call_id_variants(tc)))
    return occurrences


def _block(count: int) -> dict:
    return {
        "role": "custom", "custom_type": STRIPPED_TOOL_CALLS, "text": "",
        "display": True, "display_only": True, "attribution": "agent",
        "details": {"count": count},
    }


class StrippedToolCallsTracker:
    """Batch-local pairing state for one ``_history_to_messages`` projection."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages
        self._batch: dict | None = None

    def end_batch(self, role: str) -> None:
        """A non-tool row closes the open batch; a tool row belongs to it."""
        if role != "tool":
            self.close()

    def start(self, row: dict) -> None:
        """Open a batch for an assistant row that names tool calls."""
        if row.get("role") != "assistant" or not row.get("tool_calls"):
            return
        occurrences = _tool_call_occurrences(row)
        if occurrences:
            self._batch = {"occurrences": occurrences, "answered_uids": set(),
                           "answered_variants": set(), "anchor": None}

    def note_tool(self, row: dict, message: dict) -> None:
        """Record a projected tool row as answering its uid (or its id variants when legacy)."""
        if self._batch is None:
            return
        from agent.message_metadata import TOOL_CALL_UID
        from agent.message_sanitization import tool_result_id_variants

        uid = row.get(TOOL_CALL_UID)
        if isinstance(uid, str) and uid:
            self._batch["answered_uids"].add(uid)
        else:
            self._batch["answered_variants"].update(tool_result_id_variants(row.get("tool_call_id")))
        self._batch["anchor"] = message

    def anchor(self, message: dict) -> None:
        """The batch's visible assistant row, used when it has no tool rows at all."""
        if self._batch is not None and self._batch["anchor"] is None:
            self._batch["anchor"] = message

    def close(self) -> None:
        batch, self._batch = self._batch, None
        if batch is None:
            return
        missing = 0
        for uid, variants in batch["occurrences"]:
            if uid is not None:
                if uid not in batch["answered_uids"]:
                    missing += 1
            elif not (variants & batch["answered_variants"]):
                missing += 1
        if not missing:
            return
        anchor = batch["anchor"]
        index = (next(i for i, message in enumerate(self._messages) if message is anchor)
                 if anchor is not None else len(self._messages) - 1)
        self._messages.insert(index + 1, _block(missing))

    def finish(self, running: bool) -> None:
        """Settle the trailing batch unless its calls may still be executing."""
        if not running:
            self.close()
