"""Client-facing projection helpers for model-only compaction carriers."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    ContextCompressor,
    _LEAN_RECOVERY_HEADING,
    _LEAN_USER_MESSAGES_HEADING,
    _MERGED_PRIOR_CONTEXT_HEADER,
    _MERGED_SUMMARY_DELIMITER,
    _SUMMARY_END_MARKER,
    _content_text_for_contains,
    _part_text,
    _with_part_text,
    is_compaction_summary_message,
)
from agent.message_content import flatten_message_text


_COMPACTION_INTERNAL_FIELDS = (
    "tool_calls",
    "finish_reason",
    "reasoning",
    # Provider replay/metadata fields that ride the wire on every request but are invisible to
    # ``msg["content"]``/``msg["tool_calls"]`` accounting. Codex Responses sessions in particular carry
    # ``codex_reasoning_items`` blobs of ``encrypted_content`` that can dominate the serialized session (a
    # measured 214-turn session held ~115K tokens / 27% of its payload there — #55572).
    # ``reasoning_details`` is handled separately (see ``_reasoning_details_text_chars``): its signed/base64
    # envelope is excluded from the budget, mirroring the preflight estimator's exclusion in
    # ``model_metadata._estimate_message_tokens_without_images`` (#73298).
    # An assistant turn may carry only reasoning/thinking content with no visible text (extended-thinking
    # turns, thinking-only recovery responses). Such a turn is persisted with its reasoning fields and is
    # recallable from the transcript, but dropping it here as "empty" makes it vanish from the
    # resumed/reloaded session view while the desktop's reasoning disclosure has nothing to render. Keep it
    # when it carries reasoning so the "Thinking…" block still shows. (#44022)
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
)


def strip_context_summary_handoff_message(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Drop stale handoff data while preserving merged prior-tail content.

    Returns a copy for non-handoff rows, the unwrapped prior-tail content for merged handoffs
    (delimiter form, or legacy end-marker form), and ``None`` for standalone ones. The body lives
    here (display projection); ``ContextCompressor`` keeps a thin forwarder for its callers.
    """
    if not isinstance(message, dict):
        return message
    if not ContextCompressor._is_context_summary_message(message):
        return message.copy()
    content = message.get("content")

    def _unwrapped(new_content: Any) -> dict[str, Any]:
        unwrapped = {**message, "content": new_content}
        unwrapped.pop(COMPRESSED_SUMMARY_METADATA_KEY, None)
        return unwrapped

    if isinstance(content, str):
        if _MERGED_SUMMARY_DELIMITER in content:
            prior = content.split(_MERGED_SUMMARY_DELIMITER, 1)[0].strip()
            if prior.startswith(_MERGED_PRIOR_CONTEXT_HEADER):
                prior = prior[len(_MERGED_PRIOR_CONTEXT_HEADER):].lstrip()
        elif _SUMMARY_END_MARKER in content:
            prior = content.split(_SUMMARY_END_MARKER, 1)[1].lstrip()
        else:
            prior = ""
        return _unwrapped(prior) if prior else None
    if isinstance(content, list):
        prior_blocks: list[Any] = []
        found_delimiter = False
        for item in content:
            text = _part_text(item)
            if isinstance(text, str) and _MERGED_SUMMARY_DELIMITER in text:
                before = text.split(_MERGED_SUMMARY_DELIMITER, 1)[0]
                if before.strip():
                    prior_blocks.append(_with_part_text(item, before))
                found_delimiter = True
                break
            prior_blocks.append(item.copy() if isinstance(item, dict) else item)
        if not found_delimiter:
            # Legacy end-marker form: live content follows the marker inside/after one part.
            for index, item in enumerate(content):
                text = _part_text(item)
                if isinstance(text, str) and _SUMMARY_END_MARKER in text:
                    remainder = text.split(_SUMMARY_END_MARKER, 1)[1].lstrip()
                    legacy_blocks = [_with_part_text(item, remainder)] if remainder else []
                    legacy_blocks += [later.copy() if isinstance(later, dict) else later for later in content[index + 1:]]
                    return _unwrapped(legacy_blocks) if legacy_blocks else None
            return None

        # Strip the PRIOR CONTEXT header from the first block that carries it.
        for index, item in enumerate(prior_blocks):
            text = _part_text(item)
            if isinstance(text, str) and text.lstrip().startswith(_MERGED_PRIOR_CONTEXT_HEADER):
                leading = text.lstrip()[len(_MERGED_PRIOR_CONTEXT_HEADER):].lstrip()
                if leading:
                    prior_blocks[index] = _with_part_text(item, leading)
                else:
                    prior_blocks.pop(index)
                break
        return _unwrapped(prior_blocks) if prior_blocks else None
    return None


def summary_body_for_display(message: Any) -> str:
    """ADR-5 D3: the display body of a compaction carrier, or ``""`` for any other row.

    Reads the one stored copy of the text (standalone handoff or merged carrier) and strips
    ``SUMMARY_PREFIX`` / legacy prefixes / the end marker. Never a second persisted copy.
    """
    if not is_compaction_summary_message(message):
        return ""
    return ContextCompressor._strip_summary_prefix(_content_text_for_contains(message.get("content")))


def stamp_compaction_display(compressor: Any, compressed: list[dict[str, Any]]) -> None:
    """ADR-5 D2: stamp the carrier with this attempt's counters, in place, idempotently.

    Runs at the one producer both commit paths share (``compress()``): the agent's
    ``_commit_compaction`` and canonical ``prepare_compress``'s detached compressor. Only a real
    rewrite gets a stamp; all no-op/abort paths returned before this point. The value is the
    attempt's message-level ``estimate_rough`` (not the later committed shape), so the row and the
    live event agree.
    """
    if not getattr(compressor, "_last_compression_made_progress", False):
        return
    telemetry = getattr(compressor, "_active_compression_telemetry", None)
    if not isinstance(telemetry, dict):
        return
    tokens_before, tokens_after = telemetry.get("tokens_before"), telemetry.get("tokens_after")
    if not isinstance(tokens_before, int) or not isinstance(tokens_after, int):
        return
    carrier = next((m for m in reversed(compressed) if is_compaction_summary_message(m)), None)
    if carrier is None:  # native/plugin compression wrote no Hermes carrier row
        return
    stamp = {
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "token_method": telemetry.get("token_count_method") or "estimate_rough",
        "method": telemetry.get("method") or "llm_summary",
    }
    metadata = carrier.get("display_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    if metadata.get("compaction") == stamp:
        return
    carrier["display_metadata"] = {**metadata, "compaction": stamp}


def record_committed_compaction(agent: Any, compressed: list[dict[str, Any]], trigger: Any) -> None:
    """ADR-5 D4: record the one live event for a committed compaction on ``agent``.

    The durable carrier has its ``_row_id``/timestamp by now; the turn's status callback publishes
    before its own status-suppression gates. A plugin/native engine with no Hermes carrier leaves
    the slot empty, so no event is invented.
    """
    carrier = compaction_carrier(compressed)
    agent._last_compaction_display = (
        compaction_summary_event(carrier, trigger=str(trigger or "auto")) if carrier is not None else None
    )


def project_compaction_message_for_display(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return authentic transcript content, or ``None`` for a pure handoff.

    Model-facing recovery history retains the complete carrier. Display
    projections instead remove the handoff, inherited tool state, and internal
    reasoning while preserving any real prior-tail content or live user ask
    embedded in the carrier.
    """
    if not isinstance(message, dict):
        return None
    if not is_compaction_summary_message(message):
        return message.copy()

    projected = ContextCompressor._strip_context_summary_handoff_message(message)
    if projected is None:
        return None

    projected = projected.copy()
    for key in _COMPACTION_INTERNAL_FIELDS:
        projected.pop(key, None)
    # A safety-refused ask embedded in a composite carrier keeps its ``content_refused`` tag through
    # the unwrap, or a reopened chat would redraw it as an ordinary user bubble with no refused notice
    # (#149708 review): the model-facing handoff is gone, the user's live ask survives as a greyed
    # refused block. Every other carrier drops its internal ``display_kind`` as before.
    from agent.turn_failure_copy import CONTENT_REFUSED_DISPLAY_KIND
    if projected.get("display_kind") != CONTENT_REFUSED_DISPLAY_KIND:
        projected.pop("display_kind", None)
    return projected



# Lean-mode sections written for the model, not for a reader: the compacted turns' user messages
# quoted again, and the footer naming ``session_search`` (``ContextCompressor._augment_summary_lean``).
_MODEL_ONLY_SECTIONS = (_LEAN_USER_MESSAGES_HEADING, _LEAN_RECOVERY_HEADING)
_HISTORICAL_ONLY_LINE = "Historical only; newer protected-tail messages after this summary win."
_COMPACTED_COUNT = re.compile(r"The (\d+) compacted message\(s\) remain fully preserved")


def compaction_summary(message: Any) -> Optional[dict[str, Any]]:
    """What a compaction handoff tells a reader: ``{summary, compacted_messages}``, or ``None`` for any
    other message. ``summary`` is the summary as the compressor wrote it, without the model-facing
    preamble and end marker, prior-tail content or live ask merged into the carrier, the lean
    sections above and the snapshot's ranking line; ``compacted_messages`` is the count the recovery
    footer names (``None`` when it has none)."""
    if not is_compaction_summary_message(message):
        return None
    text = ContextCompressor._strip_summary_prefix(flatten_message_text(message.get("content")))
    sections = {}
    for heading in _MODEL_ONLY_SECTIONS:
        start = text.find(heading)
        if start >= 0:
            end = text.find("\n## ", start + len(heading))
            sections[heading] = text[start:end if end >= 0 else len(text)]
            text = text[:start] + (text[end + 1:] if end >= 0 else "")
    # The footer's count, never one a quoted user message happens to spell out (the quotes are cut first).
    counts = _COMPACTED_COUNT.findall(sections.get(_LEAN_RECOVERY_HEADING, text))
    text = "\n".join(line for line in text.splitlines() if line.strip() != _HISTORICAL_ONLY_LINE)
    return {"summary": re.sub(r"\n{3,}", "\n\n", text).strip(),
            "compacted_messages": int(counts[-1]) if counts else None}


def compaction_summary_stamp(message: Any) -> Optional[dict[str, Any]]:
    """ADR-5 D2: the durable ``display_metadata.compaction`` stamp, or None for a pre-stamp row."""
    metadata = message.get("display_metadata") if isinstance(message, dict) else None
    stamp = metadata.get("compaction") if isinstance(metadata, dict) else None
    return stamp if isinstance(stamp, dict) else None


def compaction_carrier(messages: Any) -> Optional[dict[str, Any]]:
    """The one row a compaction attempt wrote its summary into: a standalone handoff, else a
    merged carrier. Last match wins, matching ``ContextCompressor._stamp_compaction_display``."""
    if not isinstance(messages, list):
        return None
    return next((m for m in reversed(messages) if is_compaction_summary_message(m)), None)


def compaction_summary_event(message: Any, *, trigger: str) -> Optional[dict[str, Any]]:
    """ADR-5 D4: the ``compaction.summary`` payload, from the same carrier row D3 projects.

    Only a stamped carrier yields an event: an unstamped row is a pre-ADR history row and has no
    truthful counters, so the gateway stays silent rather than fabricating them.
    """
    if not isinstance(message, dict):
        return None
    stamp = compaction_summary_stamp(message)
    if stamp is None:
        return None
    text = ContextCompressor.summary_body_for_display(message)
    if not text.strip():
        return None
    row_id = message.get("_row_id")
    return {
        "text": text,
        "tokens_before": int(stamp.get("tokens_before") or 0),
        "tokens_after": stamp.get("tokens_after"),
        "token_method": stamp.get("token_method") or "estimate_rough",
        "method": stamp.get("method") or "llm_summary",
        "trigger": trigger,
        "timestamp": float(message.get("timestamp") or time.time()),
        "boundary_row_id": row_id if isinstance(row_id, int) else None,
    }
