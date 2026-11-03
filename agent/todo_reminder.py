"""Todo stop-gate reminders (ADR-2 D11, ADR-10 D3).

The model may stop with unfinished todo items. On an interactive surface the turn loop then
appends one durable reminder row and continues the turn, so the plan is finished instead of
silently abandoned. The row is a real ``user``-attributed message (it reaches the model); the
event is only a rendering hint and is emitted after the row is durable.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional

from agent.message_metadata import append_message
from agent.plugin_events import notify_plugin_message

logger = logging.getLogger("agent.conversation_loop")

_ACTIVE_STATUSES = frozenset({"pending", "in_progress"})
# Wire status for a Hermes ``cancelled`` item; omp's TodoStatus has ``abandoned``.
_STATUS_MAP = {"pending": "pending", "in_progress": "in_progress",
               "completed": "completed", "cancelled": "abandoned", "blocked": "blocked"}

TODO_REMINDER_HEADER = (
    "[System note: you stopped with unfinished todo items. Complete them, or mark them "
    "cancelled if they are no longer needed, before giving your final answer.]"
)


def _agent_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:  # health: allow BLE001 -- config is external state; a broken file must not block a turn
            config = {}
    section = (config or {}).get("agent") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def todo_reminders_enabled(config: Optional[dict[str, Any]] = None) -> bool:
    """``agent.todo_reminders`` (default on; interactive surfaces only)."""
    value = _agent_config(config).get("todo_reminders", True)
    return value if isinstance(value, bool) else True


def max_todo_reminders(config: Optional[dict[str, Any]] = None) -> int:
    """``agent.todo_reminders_max`` (default 3)."""
    value = _agent_config(config).get("todo_reminders_max", 3)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 3


def active_todos(agent: Any) -> list[dict[str, str]]:
    """The open items of the agent's plan, in wire (``TodoItem``) shape."""
    store = getattr(agent, "_todo_store", None)
    if store is None:
        return []
    try:
        snapshot = store.snapshot()
    except Exception:
        logger.debug("todo reminder snapshot failed", exc_info=True)
        return []
    items = snapshot.get("todos") if isinstance(snapshot, dict) else None
    todos: list[dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        content = item.get("content")
        if status in _ACTIVE_STATUSES and isinstance(content, str) and content:
            todos.append({"content": content, "status": _STATUS_MAP.get(status, "pending")})
    return todos


def _session_key(agent: Any) -> str:
    return str(getattr(agent, "_gateway_session_key", "") or getattr(agent, "session_id", "") or "")


def _awaiting_user_answer(agent: Any, final_response: Any) -> bool:
    """Pending clarify/approval, or a final answer that ends on a direct question."""
    key = _session_key(agent)
    if key:
        try:
            from tools.clarify_gateway import has_pending

            if has_pending(key):
                return True
        except Exception:
            logger.debug("todo reminder clarify probe failed", exc_info=True)
        try:
            from tools.approval import get_pending_gateway_approval

            if get_pending_gateway_approval(key):
                return True
        except Exception:
            logger.debug("todo reminder approval probe failed", exc_info=True)
    return _closing_question(final_response)


def _closing_question(final_response: Any) -> bool:
    """True when the visible answer's last non-empty line is a question."""
    if not isinstance(final_response, str):
        return False
    for line in reversed(final_response.splitlines()):
        stripped = line.strip().strip("*_`\"'")
        if not stripped:
            continue
        return stripped.endswith("?") or stripped.endswith("？")
    return False


def _async_wake_pending() -> bool:
    try:
        from tools.async_delegation import active_count

        return int(active_count() or 0) > 0
    except Exception:
        logger.debug("todo reminder async probe failed", exc_info=True)
        return False


def _is_interactive_surface() -> bool:
    try:
        from gateway.session_context import session_is_messaging_surface

        return not session_is_messaging_surface()
    except Exception:  # health: allow BLE001 -- the gateway package is optional; a missing surface must not block a turn
        return True


def _finite_turn() -> bool:
    try:
        from gateway.session_finite import finite_turn_required

        return finite_turn_required() is True
    except Exception:  # health: allow BLE001 -- finite-turn scope is optional infrastructure; absence means a normal turn
        return False


def _cron_turn() -> bool:
    """True inside a scheduled job (ADR-2 D11 condition 2); a cron surface is not interactive."""
    try:
        from gateway.session_context import get_session_env
        from utils import is_truthy_value

        return bool(is_truthy_value(get_session_env("HERMES_CRON_SESSION", "")))
    except Exception:
        logger.debug("todo reminder cron probe failed", exc_info=True)
        return bool((os.environ.get("HERMES_CRON_SESSION") or "").strip() not in {"", "0", "false"})


def _reminder_text(todos: list[dict[str, str]]) -> str:
    markers = {"in_progress": "[>]", "pending": "[ ]"}
    lines = [TODO_REMINDER_HEADER]
    lines.extend(f"- {markers.get(item['status'], '[ ]')} {item['content']}" for item in todos)
    return "\n".join(lines)


def _reminder_row(agent: Any, todos: list[dict[str, str]], attempt: int, max_attempts: int) -> dict[str, Any]:
    text = _reminder_text(todos)
    from gateway.session_policy import active_policy

    # ADR-10 P5: omp-ui sessions (custom_messages) write the custom row; Desktop/Ink sessions
    # keep the raw user row their hydrators read. The wire projection normalizes both.
    if getattr(active_policy(), "custom_messages", False):
        return {"role": "custom", "custom_type": "todo-reminder", "content": text,
                "display": True, "display_only": False,
                "details": {"todos": todos, "attempt": attempt, "max_attempts": max_attempts},
                "attribution": "agent", "legacy_role": "user",
                "plugin_message_id": uuid.uuid4().hex}
    return {"role": "user", "content": text, "display_kind": "todo_reminder",
            "display_metadata": {"todo_reminder": {"todos": todos, "attempt": attempt,
                                                   "max_attempts": max_attempts}},
            "plugin_message_id": uuid.uuid4().hex}


def stop_gate_reminder(agent: Any, final_msg: dict[str, Any], *, final_response: Any,
                       messages: list[dict[str, Any]], conversation_history: Any):
    """The todo-reminder ``StopGateVerdict`` for this stop, or ``None``.

    Guards follow ADR-2 D11: interactive surface only, not a subagent/finite/kanban run,
    open items, at least one tool result since the last reminder, no pending user question,
    and no pending async wake.
    """
    todos = active_todos(agent)
    if not todos:
        # A finished/empty plan ends the budget (attempts survive across prompts otherwise).
        agent._todo_reminder_attempts = 0
        agent._todo_reminder_awaiting_progress = False
        return None
    if not todo_reminders_enabled():
        return None
    if not _is_interactive_surface() or getattr(agent, "is_subagent", False):
        return None
    if getattr(agent, "_parent_session_id", None) or _finite_turn():
        return None
    if (os.environ.get("HERMES_KANBAN_TASK") or "").strip() or _cron_turn():
        return None
    attempts = int(getattr(agent, "_todo_reminder_attempts", 0) or 0)
    max_attempts = max_todo_reminders()
    if attempts >= max_attempts:
        return None
    if getattr(agent, "_todo_reminder_awaiting_progress", False):
        return None
    if _awaiting_user_answer(agent, final_response) or _async_wake_pending():
        return None

    from agent.turn_stop_gates import StopGateVerdict, _append_interim_answer

    attempt = attempts + 1
    final_msg["finish_reason"] = "todo_reminder"
    _append_interim_answer(
        agent, final_msg, messages, conversation_history, "todo reminder interim flush failed",
    )
    row = _reminder_row(agent, todos, attempt, max_attempts)
    append_message(messages, row)
    try:
        agent._flush_messages_to_session_db(messages, conversation_history)
    except Exception:
        logger.debug("todo reminder row flush failed", exc_info=True)
    agent._todo_reminder_attempts = attempt
    agent._todo_reminder_awaiting_progress = True
    agent._session_messages = messages
    notify_plugin_message(agent, row)
    logger.debug("todo reminder issued (attempt %d/%d, open=%d)", attempt, max_attempts, len(todos))
    streamed = getattr(agent, "_interim_content_was_streamed", None)
    return StopGateVerdict(
        continue_turn=True, final_response=None,
        pending_verification_response=final_response,
        pending_verification_response_previewed=(streamed(final_response or "") if callable(streamed) else False),
    )


__all__ = [
    "TODO_REMINDER_HEADER",
    "active_todos",
    "max_todo_reminders",
    "stop_gate_reminder",
    "todo_reminders_enabled",
]
