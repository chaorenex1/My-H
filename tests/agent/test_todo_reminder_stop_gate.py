"""ADR-2 D11 / ADR-10 D3: the todo stop-gate reminder row and its live event."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent.todo_reminder import stop_gate_reminder
from tools.todo_tool import TodoStore


class _Agent:
    def __init__(self):
        self.session_id = "sid-1"
        self._session_messages: list = []
        self._todo_store = TodoStore()
        self.quiet_mode = True
        self.advisor = None
        self.plugin_message_callback = None
        self.flushed = 0

    def _interim_content_was_streamed(self, text):
        return False

    def _emit_interim_assistant_message(self, message):
        return None

    def _flush_messages_to_session_db(self, messages, history=None):
        self.flushed += 1
        # The durable writer stamps the row id; the live event needs it.
        messages[-1].setdefault("_row_id", 42)


@pytest.fixture
def agent(monkeypatch):
    from gateway import session_context

    monkeypatch.setattr("agent.todo_reminder.todo_reminders_enabled", lambda: True)
    monkeypatch.setattr("agent.todo_reminder.max_todo_reminders", lambda: 3)
    monkeypatch.setattr("agent.todo_reminder._is_interactive_surface", lambda: True)
    monkeypatch.setattr("agent.todo_reminder._finite_turn", lambda: False)
    monkeypatch.setattr("agent.todo_reminder._async_wake_pending", lambda: False)
    monkeypatch.setattr("agent.todo_reminder._awaiting_user_answer", lambda a, r: False)
    monkeypatch.setattr(session_context, "session_is_messaging_surface", lambda: False)
    return _Agent()


def _open_todo(agent):
    agent._todo_store.write([{"phase": "Work", "items": [{"content": "ship it", "status": "pending"}]}])


def test_stop_gate_appends_a_legacy_reminder_row_and_emits_the_event(agent):
    from tui_gateway.agent_callbacks import plugin_row_event

    _open_todo(agent)
    rows: list[Any] = []
    agent.plugin_message_callback = rows.append
    messages = [{"role": "user", "content": "go"}]
    final_msg = {"role": "assistant", "content": "done"}

    verdict = stop_gate_reminder(
        agent, final_msg, final_response="done", messages=messages, conversation_history=[],
    )

    assert verdict is not None and verdict.continue_turn is True
    row: Any = messages[-1]
    metadata: Any = row["display_metadata"]
    assert row["role"] == "user" and row["display_kind"] == "todo_reminder"
    assert metadata["todo_reminder"]["todos"] == [
        {"content": "ship it", "status": "pending"}]
    assert (metadata["todo_reminder"]["attempt"],
            metadata["todo_reminder"]["max_attempts"]) == (1, 3)
    assert final_msg["finish_reason"] == "todo_reminder"
    assert agent._todo_reminder_attempts == 1
    assert agent._todo_reminder_awaiting_progress is True
    assert rows == [row]

    event = plugin_row_event(rows[0])
    assert event is not None
    name, payload = event
    assert name == "todo.reminder"
    assert payload == {"todos": [{"content": "ship it", "status": "pending"}],
                       "attempt": 1, "max_attempts": 3, "row_id": 42}


def test_stop_gate_writes_the_custom_row_in_a_custom_messages_session(agent, monkeypatch):
    from gateway import session_policy

    monkeypatch.setattr(session_policy, "active_policy",
                        lambda: SimpleNamespace(custom_messages=True))
    _open_todo(agent)
    messages = [{"role": "user", "content": "go"}]

    verdict = stop_gate_reminder(
        agent, {"role": "assistant", "content": "done"},
        final_response="done", messages=messages, conversation_history=[],
    )

    assert verdict is not None
    row = messages[-1]
    assert row["role"] == "custom" and row["custom_type"] == "todo-reminder"
    assert row["display_only"] is False and row["legacy_role"] == "user"
    assert row["details"] == {"todos": [{"content": "ship it", "status": "pending"}],
                              "attempt": 1, "max_attempts": 3}


def test_no_reminder_after_a_tool_result_without_new_progress(agent):
    _open_todo(agent)
    agent._todo_reminder_awaiting_progress = True
    messages = [{"role": "user", "content": "go"}]

    assert stop_gate_reminder(
        agent, {"role": "assistant", "content": "done"},
        final_response="done", messages=messages, conversation_history=[],
    ) is None
    assert messages == [{"role": "user", "content": "go"}]


def test_budget_exhaustion_stops_reminding(agent):
    _open_todo(agent)
    agent._todo_reminder_attempts = 3
    messages = [{"role": "user", "content": "go"}]

    assert stop_gate_reminder(
        agent, {"role": "assistant", "content": "done"},
        final_response="done", messages=messages, conversation_history=[],
    ) is None


def test_empty_plan_resets_the_budget(agent):
    agent._todo_reminder_attempts = 2
    agent._todo_reminder_awaiting_progress = True

    assert stop_gate_reminder(
        agent, {"role": "assistant", "content": "done"},
        final_response="done", messages=[], conversation_history=[],
    ) is None
    assert agent._todo_reminder_attempts == 0
    assert agent._todo_reminder_awaiting_progress is False


def test_cron_jobs_never_remind(agent, monkeypatch):
    from agent import todo_reminder

    monkeypatch.setattr(todo_reminder, "_cron_turn", lambda: True)
    _open_todo(agent)
    messages = [{"role": "user", "content": "go"}]

    assert stop_gate_reminder(
        agent, {"role": "assistant", "content": "done"},
        final_response="done", messages=messages, conversation_history=[],
    ) is None
    assert messages == [{"role": "user", "content": "go"}]


def test_a_closing_question_holds_the_reminder(agent, monkeypatch):
    from agent import todo_reminder

    _open_todo(agent)
    assert todo_reminder._closing_question("All done. Want me to open a PR?")
    assert not todo_reminder._closing_question("All done.")
    monkeypatch.setattr(todo_reminder, "_awaiting_user_answer",
                        lambda a, r: todo_reminder._closing_question(r))
    messages = [{"role": "user", "content": "go"}]
    final = "All done. Want me to open a PR?"

    assert stop_gate_reminder(
        agent, {"role": "assistant", "content": final},
        final_response=final, messages=messages, conversation_history=[],
    ) is None
    assert messages == [{"role": "user", "content": "go"}]


def test_apply_stop_gates_runs_the_todo_gate_last(agent, monkeypatch):
    import agent.turn_stop_gates as gates

    for name, fake in {
        "_late_lsp_verdicts": lambda a: None,
        "_verify_on_stop_nudge": lambda a: None,
        "_pre_verify_nudge": lambda a, r, n: None,
        "_kanban_stop_nudge": lambda a, m: None,
    }.items():
        monkeypatch.setattr(gates, name, fake)

    _open_todo(agent)
    messages = [{"role": "user", "content": "go"}]
    verdict = gates.apply_stop_gates(
        agent, {"role": "assistant", "content": "done"}, final_response="done",
        messages=messages, conversation_history=[],
        pending_verification_response=None, pending_verification_response_previewed=None,
    )

    assert verdict.continue_turn is True
    assert messages[-1]["display_kind"] == "todo_reminder"
