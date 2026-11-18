"""``recap.generate`` (ADR-5 P3): owner-side guards, one side turn, nothing persisted.

The client owns the idle timer; the owner proves the session is idle, the tail is a generated
assistant turn and that turn has not already been recapped. A refusal is a normal reply.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio

from gateway import session_recap
from gateway.session_contract import SessionRef
from hermes_state_runtime import admit_session_input
from tests.gateway.session_panel_helpers import panel_authority
from tests.waiting import async_wait_until
from tui_gateway.contracts.registry import CANONICAL_METHODS


class _Transport:
    def __init__(self):
        self.frames: list[dict] = []

    def write(self, frame):
        self.frames.append(frame)

    async def write_async(self, frame):
        self.frames.append(frame)
        return True


class _Agent:
    session_id = "s"

    def __init__(self, messages, todos=()):
        self._session_messages = list(messages)
        self._todo_store = SimpleNamespace(read=lambda: list(todos))


def _assistant(text="done"):
    return {"role": "assistant", "content": text}


@pytest_asyncio.fixture
async def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with panel_authority(tmp_path) as conn:
        conn.transport = _Transport()
        yield conn


def _attach_agent(connection, agent):
    connection.authority.runner._cached_agent_for = lambda key: agent
    return agent


def _ref(connection, session_id="s"):
    return SessionRef(connection.authority.profile_id, session_id)


def _recap(connection):
    return session_recap._recap(connection.authority, _ref(connection))


class TestGeneration:

    def test_returns_the_side_turn_text_and_marks_the_tail(self, connection):
        agent = _attach_agent(connection, _Agent([{"role": "user", "content": "x"}, _assistant()]))
        seen = {}

        def fake_generate(parent, *, goal, task):
            seen.update(parent=parent, goal=goal, task=task)
            return "Fixing the parser; next: run the suite."

        with patch("agent.recap.generate_recap", fake_generate), \
             patch.object(session_recap, "_goal_text", return_value="ship it"):
            result = _recap(connection)

        assert result["text"] == "Fixing the parser; next: run the suite."
        assert isinstance(result["generated_at"], float)
        assert "skipped" not in result
        assert seen == {"parent": agent, "goal": "ship it", "task": ""}
        assert agent._last_recap_tail_uid == session_recap._tail_uid(agent._session_messages[-1])

    def test_same_tail_is_skipped_without_a_second_model_call(self, connection):
        _attach_agent(connection, _Agent([_assistant()]))
        calls = []

        with patch("agent.recap.generate_recap", side_effect=lambda *a, **k: calls.append(1) or "once"):
            assert _recap(connection)["text"] == "once"
            second = _recap(connection)

        assert second["skipped"] == "no_new_turn" and second["text"] == ""
        assert len(calls) == 1

    def test_an_empty_reply_is_a_failed_skip_and_stays_retryable(self, connection):
        agent = _attach_agent(connection, _Agent([_assistant()]))

        with patch("agent.recap.generate_recap", return_value=""):
            result = _recap(connection)

        assert result["skipped"] == "failed"
        assert getattr(agent, "_last_recap_tail_uid", None) is None

    def test_anchors_come_from_the_goal_store_then_the_title(self, connection):
        connection.authority.db.set_session_title("s", "Parser cleanup")
        assert session_recap._goal_text(connection.authority, "s") == "Parser cleanup"

    def test_active_task_is_the_first_actionable_todo(self, connection):
        agent = _Agent([], todos=[
            {"id": "1", "content": "already done", "status": "completed"},
            {"id": "2", "content": "run the suite", "status": "in_progress"},
            {"id": "3", "content": "tag the release", "status": "pending"},
        ])
        assert session_recap._active_task(agent) == "run the suite"


class TestGuards:

    def test_no_resident_agent_skips_without_a_model_call(self, connection):
        connection.authority.runner._cached_agent_for = lambda key: None
        with patch("agent.recap.generate_recap") as generate:
            result = _recap(connection)
        assert result["skipped"] == "no_agent"
        generate.assert_not_called()

    def test_a_user_tail_skips(self, connection):
        _attach_agent(connection, _Agent([_assistant(), {"role": "user", "content": "next"}]))
        with patch("agent.recap.generate_recap") as generate:
            assert _recap(connection)["skipped"] == "no_new_turn"
        generate.assert_not_called()

    def test_an_empty_conversation_skips(self, connection):
        _attach_agent(connection, _Agent([]))
        with patch("agent.recap.generate_recap") as generate:
            assert _recap(connection)["skipped"] == "no_new_turn"
        generate.assert_not_called()

    def test_a_queued_admission_is_busy(self, connection):
        _attach_agent(connection, _Agent([_assistant()]))
        admit_session_input(connection.authority.db, epoch=connection.authority.epoch,
                            principal_id="owner", session_id="s", request_id="r1",
                            payload={"text": "queued turn"})
        with patch("agent.recap.generate_recap") as generate:
            assert _recap(connection)["skipped"] == "busy"
        generate.assert_not_called()

    def test_a_pending_prompt_is_busy(self, connection):
        _attach_agent(connection, _Agent([_assistant()]))
        live = connection.authority.sessions["s"]
        live.controls.pending["p1"] = ("route", {"kind": "approval", "prompt_id": "p1"})
        with patch("agent.recap.generate_recap") as generate:
            assert _recap(connection)["skipped"] == "busy"
        generate.assert_not_called()


class TestDispatch:

    @pytest.mark.asyncio
    async def test_wire_reply_is_the_declared_shape(self, connection):
        _attach_agent(connection, _Agent([_assistant()]))
        with patch("agent.recap.generate_recap", return_value="One line."):
            assert await connection.dispatch(
                {"id": 7, "method": "recap.generate", "params": {"session_id": "s"}}) is None
            await async_wait_until(lambda: connection.transport.frames, what="deferred recap reply")
        frame = connection.transport.frames[0]
        assert frame["id"] == 7
        assert "error" not in frame, frame
        assert frame["result"]["text"] == "One line."
        CANONICAL_METHODS["recap.generate"].result.model_validate(frame["result"])

    @pytest.mark.asyncio
    async def test_unknown_keys_are_refused_before_any_work(self, connection):
        reply = await connection.dispatch(
            {"id": 1, "method": "recap.generate", "params": {"session_id": "s", "nope": 1}})
        assert reply["error"]["code"] == 4001
        assert reply["error"]["data"]["reason"] == "invalid_params"
        assert connection.transport.frames == []
