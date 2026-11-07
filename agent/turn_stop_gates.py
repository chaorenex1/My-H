"""Text-response stop gates for the conversation turn loop.

When the model stops with a text answer, these gates may instead append the answer as an
interim row plus a user-role nudge and continue the turn: late language-server verdicts on this
turn's edits (a durable row, see ``_late_lsp_verdicts``), the smart unexpected-stop retry
(ADR-17 P5), verify-on-stop (#65919), the ``pre_verify`` plugin hook after code edits, the
kanban worker terminal-tool guard, and the todo reminder for an unfinished plan (ADR-2 D11).
Each keeps the candidate answer as a budget-exhaustion fallback
(``pending_verification_response``) and clears ``final_response`` so the finalizer can tell
this gate from error exits (#61631). Nothing here imports ``agent.conversation_loop`` at
module level (cycle).
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.message_metadata import append_message
from agent.plugin_events import notify_plugin_message

logger = logging.getLogger("agent.conversation_loop")


@dataclass
class StopGateVerdict:
    """``continue_turn`` True → a nudge was appended; re-enter the turn loop with
    ``final_response=None`` and the pending-verification fields updated."""

    continue_turn: bool
    final_response: Any
    pending_verification_response: Any
    pending_verification_response_previewed: Any


_UNEXPECTED_STOP_MODES = ("none", "mechanical", "smart")
_UNEXPECTED_STOP_DEFAULT_MODE = "mechanical"
# One judged retry per user turn: a stop nudge demands the next tool call or the final
# answer, so judging the answer to a nudge only chains no-op "nothing more to add" turns.
_UNEXPECTED_STOP_MAX_NUDGES = 1
# Gateway-routed judges can take ~12s; matches omp's judgment-adjacent 15s budget.
_UNEXPECTED_STOP_JUDGE_TIMEOUT_MS = 15_000
# Leading characters of the request shown to the judge: enough for the ask and its
# constraints without letting a pasted log crowd a small judge model's context.
_UNEXPECTED_STOP_REQUEST_CHARS = 2000
_UNEXPECTED_STOP_THRESHOLD = 0.5
_UNEXPECTED_STOP_PROMPT = Path(__file__).resolve().parent / "prompts" / "unexpected-stop-retry.md"


def _unexpected_stop_mode() -> str:
    """``agent.unexpected_stop_detection``: ``none`` | ``mechanical`` | ``smart``."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        agent_cfg = config.get("agent") if isinstance(config, dict) else None
        raw = agent_cfg.get("unexpected_stop_detection") if isinstance(agent_cfg, dict) else None
    except Exception:
        logger.debug("unexpected-stop mode lookup failed", exc_info=True)
        return _UNEXPECTED_STOP_DEFAULT_MODE
    token = raw.strip().lower() if isinstance(raw, str) else ""
    return token if token in _UNEXPECTED_STOP_MODES else _UNEXPECTED_STOP_DEFAULT_MODE


def _is_synthetic_user_row(message: dict) -> bool:
    """Nudge/scaffolding rows reuse the user role; none of them is the operator request."""
    if message.get("flag") or message.get("display_kind"):
        return True
    return any(
        value for key, value in message.items()
        if key.endswith(("_synthetic", "_nudge"))
    )


def _latest_user_request(messages: Any) -> str:
    """The operator's latest real request (synthetic nudge rows excluded), truncated."""
    for message in reversed(list(messages or ())):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if _is_synthetic_user_row(message):
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        text = content.strip() if isinstance(content, str) else ""
        if text:
            return text[:_UNEXPECTED_STOP_REQUEST_CHARS]
    return ""


def _unexpected_stop_nudge_text(attempt: int) -> str:
    """The retry nudge, rendered from the bundled prompt file (no transcript row)."""
    template = _UNEXPECTED_STOP_PROMPT.read_text(encoding="utf-8-sig")
    return (template.replace("{{retryCount}}", str(attempt))
            .replace("{{maxRetries}}", str(_UNEXPECTED_STOP_MAX_NUDGES)))


def _unexpected_stop_judged(final_response: str, messages: Any) -> bool:
    """``True`` when the judge scores the text stop as unexpected; any failure is silence."""
    from agent.judgment import JudgmentError, JudgmentRequest, judge, load_questions

    state: dict[str, Any] = {"message": final_response}
    request = _latest_user_request(messages)
    if request:
        state["request"] = request
    try:
        question = load_questions("unexpected-stop")["stopped"]
        result = judge(
            JudgmentRequest(state=state, questions={"stopped": question}),
            purpose="unexpected-stop",
            timeout_ms=_UNEXPECTED_STOP_JUDGE_TIMEOUT_MS,
        )
    except JudgmentError as exc:
        logger.debug("unexpected-stop: classification failed: %s", exc)
        return False
    except Exception as exc:  # health: allow BLE001 -- a judged gate must not fail the turn
        logger.debug("unexpected-stop: classification failed: %s", exc)
        return False
    probability = getattr(result.answers.get("stopped"), "noul", None)
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        return False
    return probability >= _UNEXPECTED_STOP_THRESHOLD


def _unexpected_stop_nudge(agent, final_msg, final_response, messages) -> Optional[str]:
    """ADR-17 P5: a text answer that promises work it never starts gets one judged retry.

    ``mechanical`` keeps the shape-based recovery only (the stall/ack guards in
    ``finish_text_response``); ``smart`` also asks the judge about visible text. Interrupted
    turns, tool-call turns, empty text, and the answer to an earlier nudge are never judged.
    The counter is reset per user turn with the other stop-gate counters.
    """
    try:
        if getattr(agent, "_interrupt_requested", False):
            return None
        if isinstance(final_msg, dict) and final_msg.get("tool_calls"):
            return None
        if not isinstance(final_response, str) or not final_response.strip():
            return None
        if int(getattr(agent, "_unexpected_stop_nudges", 0) or 0) >= _UNEXPECTED_STOP_MAX_NUDGES:
            return None
        if _unexpected_stop_mode() != "smart":
            return None
        if not _unexpected_stop_judged(final_response, messages):
            return None
        attempt = int(getattr(agent, "_unexpected_stop_nudges", 0) or 0) + 1
        text = _unexpected_stop_nudge_text(attempt)
    except Exception:
        logger.debug("unexpected-stop check failed", exc_info=True)
        return None
    agent._unexpected_stop_nudges = attempt
    return text


def _verify_on_stop_nudge(agent) -> Optional[str]:
    try:
        from agent.verification_stop import (
            build_verify_on_stop_nudge, verify_on_stop_enabled
        )

        if verify_on_stop_enabled():
            return build_verify_on_stop_nudge(
                session_id=getattr(agent, "session_id", None),
                changed_paths=getattr(agent, "_turn_file_mutation_paths", set()),
                attempts=getattr(agent, "_verification_stop_nudges", 0),
            )
    except Exception:
        logger.debug("verification stop-loop check failed", exc_info=True)
    return None


def _pre_verify_nudge(agent, final_response, attempt: int) -> Optional[str]:
    """After code edits a registered ``pre_verify`` hook may keep the agent going one
    more turn; no default continuation cost."""
    _edited = sorted(getattr(agent, "_turn_file_mutation_paths", set()) or [])
    try:
        from agent.verify_hooks import max_verify_nudges
        from hermes_cli.lifecycle import has_hook
        from hermes_cli.plugins import get_pre_verify_continue_message

        if _edited and has_hook("pre_verify") and attempt < max_verify_nudges():
            # Posture is fixed for the session — resolve once + cache.
            coding = getattr(agent, "_resolved_is_coding", None)
            if coding is None:
                from agent.coding_context import is_coding_context
                coding = bool(is_coding_context(platform=getattr(agent, "platform", "") or ""))
                agent._resolved_is_coding = coding
            return get_pre_verify_continue_message(
                session_id=getattr(agent, "session_id", None) or "",
                platform=getattr(agent, "platform", "") or "",
                model=getattr(agent, "model", "") or "", coding=coding, attempt=attempt,
                final_response=final_response, changed_paths=_edited,
            )
    except Exception:
        logger.debug("pre_verify hook check failed", exc_info=True)
    return None


def _plugin_continuation(agent) -> Optional[dict]:
    """A plugin's stop-gate request for one more round, or ``None``.

    Unlike ``pre_verify`` this is not tied to file edits: it is the only channel a
    plugin has to act on something it noticed while the final model call ran. The
    per-turn budget (``agent.max_plugin_continuations``, default 2) bounds a plugin
    that always asks to continue; a request over budget is discarded so it cannot
    leak into the next turn.
    """
    session = str(getattr(agent, "session_id", "") or "")
    if not session:
        return None
    try:
        from hermes_cli import plugin_runtime

        used = int(getattr(agent, "_plugin_continue_nudges", 0) or 0)
        if used >= plugin_runtime.max_continuations():
            # Dropped, not deferred: the plugin sees `expired` and returns its rows to
            # pending instead of losing them to a delivery that never happened.
            plugin_runtime.discard_continuation(session)
            return None
        return plugin_runtime.take_continuation(session)
    except Exception:
        logger.debug("plugin continuation gate failed", exc_info=True)
        return None


def _plugin_continuation_row(pending: dict) -> dict:
    """The durable user-role row a plugin continuation is delivered as.

    Not flagged synthetic: the advice stays visible in the transcript and is
    replayed on resume, which is the point of the channel. ``display_kind`` lets
    history renderers label it apart from a human prompt.
    """
    from hermes_cli.plugin_runtime import DISPLAY_KIND

    return {
        "role": "user",
        "content": str(pending.get("text") or "").strip(),
        "display_kind": DISPLAY_KIND,
        "plugin_source": str(pending.get("source") or ""),
        "plugin_message_id": uuid.uuid4().hex,
    }


def _advisor_continuation(agent) -> Optional[dict]:
    """A native advisor's stop-gate request for one more round, or ``None``."""
    advisor = getattr(agent, "advisor", None)
    if advisor is None:
        return None
    try:
        return advisor.take_continuation()
    except Exception:
        logger.debug("advisor continuation read failed", exc_info=True)
        return None


def _kanban_stop_nudge(agent, messages) -> Optional[str]:
    """Workers must end with a terminal board tool (kanban_complete / kanban_block /
    kanban_request_review / kanban_request_changes); a narrated stop is recorded
    as protocol_violation, so nudge once or twice first."""
    try:
        from agent.kanban_stop import build_kanban_stop_nudge

        return build_kanban_stop_nudge(
            messages=messages, attempts=getattr(agent, "_kanban_stop_nudges", 0)
        )
    except Exception:
        logger.debug("kanban stop-loop check failed", exc_info=True)
        return None


_MAX_LSP_TURN_END_NOTES = 3


def _late_lsp_verdicts(agent) -> Optional[list]:
    """Language-server verdicts on this turn's edits that no tool result carried yet (the edit was the
    last tool call; a one-shot run has no next turn to carry them): waits briefly for checks still
    running (``agent.lsp.deferred.settle_for_agent``) and returns the queued ``(path, Note)`` pairs."""
    issued = getattr(agent, "_lsp_turn_end_notes", 0)
    if not isinstance(issued, int) or issued >= _MAX_LSP_TURN_END_NOTES:
        return None
    try:
        from agent.lsp.deferred import settle_for_agent

        notes = settle_for_agent(agent)
    except Exception:
        logger.debug("late LSP verdict check failed", exc_info=True)
        return None
    return notes or None


def _append_interim_answer(agent, final_msg, messages, conversation_history, flush_fail_msg: str) -> None:
    """Real content: persist and emit as interim so the user sees the attempted answer;
    only the nudge is flagged synthetic (#65919). A held turn's answer (typed hidden) is not shown
    and flushes with the turn instead, so a budget fallback can still publish it as the rewrite."""
    agent._emit_interim_assistant_message(final_msg)
    append_message(messages, final_msg)
    if final_msg.get("display_kind") == "hidden":
        return
    try:
        agent._flush_messages_to_session_db(messages, conversation_history)
    except Exception:
        logger.debug(flush_fail_msg, exc_info=True)


def apply_stop_gates(
    agent: Any, final_msg: dict[str, Any], *, final_response: Any, messages: list[dict[str, Any]],
    conversation_history: Any, pending_verification_response: Any,
    pending_verification_response_previewed: Any,
) -> StopGateVerdict:
    """Run verify-on-stop → pre_verify hook → kanban → todo reminder, in that order. Nudges
    are user-role rows appended only after the assistant answer row, so role alternation
    holds. Hook lookups are imported lazily from their origin modules (tests patch them
    there)."""

    def _continue(nudge: str, flag: str) -> StopGateVerdict:
        """Append the synthetic nudge row and hand the turn back to the loop."""
        append_message(messages, {"role": "user", "content": nudge, flag: True})
        agent._session_messages = messages
        # Keep the answer only as a budget-exhaustion fallback; clear ``final_response`` so
        # the finalizer can tell this gate from error exits. Mark previewed only if the
        # candidate is reused (#61631).
        return StopGateVerdict(
            continue_turn=True, final_response=None,
            pending_verification_response=final_response,
            # Prefix semantics on purpose: the streamed prefix IS on screen here. The gateway
            # interim verdict uses _interim_content_fully_streamed instead (#88954).
            pending_verification_response_previewed=agent._interim_content_was_streamed(
                final_response or ""
            ),
        )

    from agent.advisor_bridge import terminal_review

    terminal_review(agent, messages, final_response)

    _advisor_pending = _advisor_continuation(agent)
    if _advisor_pending:
        from agent.prompt_builder import advisory_user_row

        agent._advisor_continue_nudges = getattr(agent, "_advisor_continue_nudges", 0) + 1
        final_msg["finish_reason"] = "advisor_continue"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history,
            "advisor continuation interim flush failed",
        )
        advisor_row = advisory_user_row(str(_advisor_pending.get("text") or ""))
        append_message(messages, advisor_row)
        notify_plugin_message(agent, advisor_row)
        agent._session_messages = messages
        logger.debug(
            "advisor continuation nudge issued (attempt %d, source=%s)",
            agent._advisor_continue_nudges, _advisor_pending.get("source") or "",
        )
        return StopGateVerdict(
            continue_turn=True, final_response=None,
            pending_verification_response=final_response,
            pending_verification_response_previewed=agent._interim_content_was_streamed(
                final_response or ""
            ),
        )

    _plugin_pending = _plugin_continuation(agent)
    if _plugin_pending:
        agent._plugin_continue_nudges = getattr(agent, "_plugin_continue_nudges", 0) + 1
        final_msg["finish_reason"] = "plugin_continue"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history,
            "plugin continuation interim flush failed",
        )
        plugin_row = _plugin_continuation_row(_plugin_pending)
        append_message(messages, plugin_row)
        notify_plugin_message(agent, plugin_row)
        agent._session_messages = messages
        logger.debug(
            "plugin continuation nudge issued (attempt %d, source=%s)",
            agent._plugin_continue_nudges, _plugin_pending.get("source") or "",
        )
        return StopGateVerdict(
            continue_turn=True, final_response=None,
            pending_verification_response=final_response,
            pending_verification_response_previewed=agent._interim_content_was_streamed(
                final_response or ""
            ),
        )

    _late_notes = _late_lsp_verdicts(agent)
    if _late_notes:
        agent._lsp_turn_end_notes = getattr(agent, "_lsp_turn_end_notes", 0) + 1  # int: checked above
        final_msg["finish_reason"] = "verification_required"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "late-LSP interim flush failed"
        )
        from agent.lsp import delivery as lsp_delivery

        if lsp_delivery.custom_messages_enabled():
            # ADR-9 D1/D3: a real custom row (developer to the model, a diagnostics card to omp-ui).
            lsp_delivery.deliver(agent, messages, _late_notes, conversation_history)
        else:
            body = "\n\n".join(note.legacy_text for _path, note in _late_notes)
            # A durable row (no scaffolding flag): it is never stripped, so no sent byte changes later.
            source = ("diagnostics reported" if any(note.semgrep_text for _path, note in _late_notes)
                      else "the language server reported")
            append_message(messages, {
                "role": "user",
                "content": (
                    f"[System note: {source} problems in files you edited this turn, "
                    "after those tool results had returned. Fix them if they are real, then give your "
                    "final answer.]\n\n" + body),
            })
        agent._session_messages = messages
        logger.debug("late LSP verdicts delivered before the final answer (%d)", agent._lsp_turn_end_notes)
        return StopGateVerdict(
            continue_turn=True, final_response=None,
            pending_verification_response=final_response,
            pending_verification_response_previewed=agent._interim_content_was_streamed(final_response or ""),
        )

    _unexpected_nudge = _unexpected_stop_nudge(agent, final_msg, final_response, messages)
    if _unexpected_nudge:
        # The gate already bumped ``_unexpected_stop_nudges`` when it flagged the stop.
        final_msg["finish_reason"] = "unexpected_stop"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "unexpected-stop interim flush failed"
        )
        verdict = _continue(_unexpected_nudge, "_unexpected_stop_synthetic")
        # Internal nudge: stay silent on the terminal, debug-log only.
        logger.debug("unexpected-stop retry nudge issued (attempt %d)", agent._unexpected_stop_nudges)
        return verdict

    _verify_nudge = _verify_on_stop_nudge(agent)
    if _verify_nudge:
        agent._verification_stop_nudges = getattr(agent, "_verification_stop_nudges", 0) + 1
        final_msg["finish_reason"] = "verification_required"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "verify-on-stop interim flush failed"
        )
        verdict = _continue(_verify_nudge, "_verification_stop_synthetic")
        # Internal nudge: stay silent on the terminal, debug-log only.
        logger.debug("verification stop-loop nudge issued (attempt %d)", agent._verification_stop_nudges)
        return verdict

    _attempt = getattr(agent, "_pre_verify_nudges", 0)
    _verify_nudge2 = _pre_verify_nudge(agent, final_response, _attempt)
    if _verify_nudge2:
        agent._pre_verify_nudges = _attempt + 1
        final_msg["finish_reason"] = "verify_hook_continue"
        _append_interim_answer(
            agent, final_msg, messages, conversation_history, "pre_verify interim flush failed"
        )
        verdict = _continue(_verify_nudge2, "_pre_verify_synthetic")
        logger.debug("pre_verify nudge issued (attempt %d)", agent._pre_verify_nudges)
        return verdict

    _kanban_nudge = _kanban_stop_nudge(agent, messages)
    if _kanban_nudge:
        agent._kanban_stop_nudges = getattr(agent, "_kanban_stop_nudges", 0) + 1
        final_msg["finish_reason"] = "kanban_terminal_required"
        final_msg["_kanban_stop_synthetic"] = True
        append_message(messages, final_msg)
        verdict = _continue(_kanban_nudge, "_kanban_stop_synthetic")
        logger.info(
            "kanban stop-loop nudge issued (attempt %d) task=%s",
            agent._kanban_stop_nudges,
            os.environ.get("HERMES_KANBAN_TASK", ""),
        )
        agent._emit_diagnostic_status(
            "⚠️ Kanban worker tried to exit without a terminal board call "
            "(kanban_complete/kanban_request_review/kanban_block) — nudging to finish"
        )
        return verdict

    # ADR-2 D11 / ADR-10 D3: the last gate. An unfinished todo plan on an interactive surface
    # appends one durable reminder row (already-persisted rows keep role alternation intact).
    from agent.todo_reminder import stop_gate_reminder

    _todo_verdict = stop_gate_reminder(
        agent, final_msg, final_response=final_response, messages=messages,
        conversation_history=conversation_history,
    )
    if _todo_verdict is not None:
        return _todo_verdict
    return StopGateVerdict(
        continue_turn=False, final_response=final_response,
        pending_verification_response=pending_verification_response,
        pending_verification_response_previewed=pending_verification_response_previewed,
    )
