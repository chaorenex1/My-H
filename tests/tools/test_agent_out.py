"""``agent_out``: read a named agent session back by ``agent_session_id`` or subject."""

from __future__ import annotations

import json

import pytest
import run_agent

from agent import agent_loop_service
from tools import agent_out_tool, agent_tool
from tools.registry import registry


class _FakeAgent:
    """Stands in for ``run_agent.AIAgent``: answers deterministically and closes."""

    instances: list[_FakeAgent] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tools: list = []
        self.history = None
        _FakeAgent.instances.append(self)

    def run_conversation(self, message, system_message=None, conversation_history=None,
                         task_id=None):
        self.history = conversation_history
        prompt = message.split("\n\n--- context ---")[0]
        return {
            "final_response": f"answered: {prompt}",
            "api_calls": 1,
            "messages": [{"role": "user", "content": prompt},
                         {"role": "assistant", "content": f"answered: {prompt}"}],
        }

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fake_route_and_agent(monkeypatch):
    from agent import agent_run

    _FakeAgent.instances.clear()
    monkeypatch.setattr(agent_run, "_resolve_route",
                        lambda model, provider: ("fake-model", {"provider": "fake",
                                                                "api_key": "k"}))
    monkeypatch.setattr(run_agent, "AIAgent", _FakeAgent)
    agent_loop_service.reset_service()
    yield
    agent_loop_service.reset_service()


def _run(**args) -> dict:
    raw = registry.dispatch("agent", args, session_id="main")
    return json.loads(raw) if isinstance(raw, str) else raw


def _read(**args) -> dict:
    raw = registry.dispatch("agent_out", args)
    return json.loads(raw) if isinstance(raw, str) else raw


def test_the_answer_is_readable_by_id_and_by_subject():
    run = _run(agent_name="researcher", user_prompts="Find the cause.", background=False,
               subject="cause analysis")

    by_id = _read(mode="final_response", agent_session_id=run["agent_session_id"])
    by_subject = _read(mode="final_response", subject="cause analysis")

    assert by_id["final_response"] == "answered: Find the cause."
    assert by_id["status"] == "completed"
    assert by_subject["agent_session_id"] == run["agent_session_id"]
    assert by_subject["subject"] == "cause analysis"


def test_artifact_mode_reports_no_manifest_without_losing_the_transcript():
    run = _run(agent_name="researcher", user_prompts="go", background=False)

    data = _read(mode="artifact", agent_session_id=run["agent_session_id"])

    assert data["artifact"] is None
    assert data["transcript_path"] == run["transcript_path"]
    assert "no artifact manifest" in data["note"]


def test_history_is_that_agent_session_only():
    first = _run(agent_name="researcher", user_prompts="first", background=False)
    second = _run(agent_name="researcher", user_prompts="second", background=False)

    first_events = _read(mode="history", agent_session_id=first["agent_session_id"])
    second_events = _read(mode="history", agent_session_id=second["agent_session_id"])

    first_text = [event.get("text") for event in first_events["events"]]
    second_text = [event.get("text") for event in second_events["events"]]
    assert "first" in first_text and "second" not in first_text
    assert "second" in second_text and "first" not in second_text
    assert first_events["event_count"] == len(first_events["events"])
    assert first_events["truncated"] is False


def test_identity_must_be_exactly_one_of_id_and_subject():
    both = _read(mode="final_response", agent_session_id="a", subject="b")
    neither = _read(mode="final_response")

    assert "exactly one" in both["error"]
    assert "exactly one" in neither["error"]


def test_an_unknown_identity_is_an_error():
    data = _read(mode="final_response", agent_session_id="researcher-missing")

    assert "no agent session" in data["error"]


def test_a_repeated_subject_is_disambiguated_and_each_row_stays_addressable():
    first = _run(agent_name="researcher", user_prompts="one", background=False,
                 subject="same topic")
    second = _run(agent_name="researcher", user_prompts="two", background=False,
                  subject="same topic")

    assert second["subject"] == "same topic (2)"
    assert _read(mode="final_response", subject="same topic")["agent_session_id"] \
        == first["agent_session_id"]
    assert _read(mode="final_response", subject="same topic (2)")["agent_session_id"] \
        == second["agent_session_id"]


def test_a_running_row_reports_status_instead_of_an_answer():
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        db.begin_agent_session(agent_session_id="researcher-running", agent_name="researcher",
                               subject="still running", transcript_path="nowhere.jsonl")
    finally:
        release_or_close(db)

    running = _read(mode="final_response", agent_session_id="researcher-running")
    assert running["final_response"] is None
    assert running["status"] == "running" and "not settled" in running["note"]

    db = acquire()
    try:
        db.finish_agent_session("researcher-running", status="completed",
                                final_response="done later", transcript_path="nowhere.jsonl")
    finally:
        release_or_close(db)

    settled = _read(mode="final_response", agent_session_id="researcher-running")
    assert settled["final_response"] == "done later" and settled["status"] == "completed"
