"""Resident named-agent sessions (ADR-15 P4): wake turns, park/revive, and the session pool."""

from __future__ import annotations

import json
import threading

import pytest
import run_agent

from agent import agent_loop_service, agent_run, agent_session_runtime
from tests.waiting import wait_until
from tools import agent_tool
from tools.delegate_tool import _handle_control_action
from tools.delegate_tool_registry import (
    _active_subagents,
    _active_subagents_lock,
    _recent_subagents,
)
from tools.registry import registry


class _Parent:
    """Stand-in for the calling conversation; ownership resolves on its durable session id."""

    def __init__(self, session_id: str = "owner"):
        self.session_id = session_id


class _FakeAgent:
    """A deterministic run; ``block`` keeps it live until the test releases it."""

    instances: list[_FakeAgent] = []
    block = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tools: list = []
        self.turns: list[str] = []
        self.steered: list[str] = []
        self.interrupted = False
        self.closed = False
        self.release = threading.Event()
        _FakeAgent.instances.append(self)

    def run_conversation(self, message, system_message=None, conversation_history=None,
                         task_id=None):
        if type(self).block:
            self.release.wait(10)
        prompt = message.split("\n\n--- context ---")[0]
        self.history = conversation_history
        self.turns.append(prompt)
        return {
            "final_response": f"answered: {prompt}",
            "api_calls": 1,
            "messages": [{"role": "user", "content": prompt},
                         {"role": "assistant", "content": f"answered: {prompt}"}],
        }

    def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True

    def interrupt(self, message=None) -> None:
        self.interrupted = True

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_route_and_agent(monkeypatch):
    _FakeAgent.instances.clear()
    _FakeAgent.block = False
    with _active_subagents_lock:
        _active_subagents.clear()
        _recent_subagents.clear()
    monkeypatch.setattr(agent_run, "_resolve_route",
                        lambda model, provider, base_url="", api_key="": (
                            "fake-model", {"provider": "fake", "api_key": "k"}))
    monkeypatch.setattr(run_agent, "AIAgent", _FakeAgent)
    agent_loop_service.reset_service()
    agent_session_runtime.reset_runtime()
    yield
    for agent in _FakeAgent.instances:
        agent.release.set()
    _FakeAgent.block = False
    agent_session_runtime.reset_runtime()
    agent_loop_service.reset_service()
    with _active_subagents_lock:
        _active_subagents.clear()
        _recent_subagents.clear()


def _dispatch(**args) -> dict:
    session = args.pop("session_id", "owner")
    raw = registry.dispatch("agent", args, session_id=session)
    return json.loads(raw) if isinstance(raw, str) else raw


def _control(action: str, *, session_id: str = "owner", **args) -> dict:
    raw = _handle_control_action(action, args.get("subagent_id"), args.get("message"),
                                 _Parent(session_id))
    return json.loads(raw)


def _out(**args) -> dict:
    raw = registry.dispatch("agent_out", args)
    return json.loads(raw) if isinstance(raw, str) else raw


def _runtime() -> agent_session_runtime.AgentSessionRuntime:
    return agent_session_runtime.runtime()


def _row(agent_session_id: str) -> dict:
    """The settled ``agent_session`` row (``agent_out`` no longer has a final_response mode)."""
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        return db.get_agent_session(agent_session_id) or {}
    finally:
        release_or_close(db)


def _answer_is(out: dict, expected: str) -> dict | None:
    return out if expected in str(out.get("final_response") or "") else None


def _wait_answer(agent_session_id: str, expected: str) -> dict:
    return wait_until(
        lambda: _answer_is(_row(agent_session_id), expected),
        what=f"the answer {expected!r}")


def _sweep_until(runtime: agent_session_runtime.AgentSessionRuntime, key: str) -> None:
    """Poll ``sweep`` until the TTL/wall clock fires — the delay is the behaviour under test."""
    runtime._limits["idle_ttl_seconds"] = 0.01
    runtime._limits["max_runtime_seconds"] = 0.01
    wait_until(lambda: runtime.sweep().get(key), what=f"sweep reported {key}")


def _park(agent_session_id: str) -> None:
    _sweep_until(_runtime(), "parked")
    wait_until(lambda: _row(agent_session_id).get("status") == "parked",
               what="the idle TTL parked the session")


def test_a_settled_resident_session_parks_on_the_idle_ttl_and_revives_on_a_message():
    run = _dispatch(agent_name="researcher", user_prompts="long task")
    session_id = run["agent_session_id"]
    assert run["resident"] is True
    _wait_answer(session_id, "answered: long task")

    runtime = _runtime()
    wait_until(lambda: runtime.state_of(session_id) == "idle", what="the run went idle")
    agent = _FakeAgent.instances[0]
    assert agent.closed is False, "a resident agent stays open after it settles"

    _sweep_until(runtime, "parked")
    assert agent.closed is True, "park closes the agent and releases its slot"
    assert _row(session_id)["status"] == "parked"
    listed = _control("list")
    assert listed["parked"] == 1 and listed["live"] == 0
    assert any(row["subagent_id"] == session_id and row["status"] == "parked"
               for row in listed["subagents"]), "the listing shows parked sessions"

    # A parked row survives the process that made it: drop the whole pool, then revive.
    agent_session_runtime.reset_runtime()
    reply = _control("steer", subagent_id=session_id, message="next step")
    assert reply["status"] == "revived"

    revived = _wait_answer(session_id, "answered: next step")
    assert revived["agent_session_id"] == session_id
    assert revived["status"] == "completed"
    rebuilt = _FakeAgent.instances[-1]
    contents = [str(row.get("content") or "") for row in (rebuilt.history or [])]
    assert "long task" in contents and "answered: long task" in contents, (
        "a revive rebuilds the conversation from the bundle transcript")
    report = _out(mode="artifact", agent_session_id=session_id)
    assert report["content"] == "answered: next step", (
        "a wake turn refreshes the bundle report")
    history = _out(mode="history", agent_session_id=session_id)
    texts = [str(event.get("text") or "") for event in history["events"]]
    assert "long task" in texts and "next step" in texts, (
        "a revive appends to the same transcript instead of rewriting it")


def test_a_wake_turn_reuses_the_kept_agent_instead_of_rebuilding_it():
    run = _dispatch(agent_name="researcher", user_prompts="first")
    session_id = run["agent_session_id"]
    _wait_answer(session_id, "answered: first")
    wait_until(lambda: _runtime().state_of(session_id) == "idle", what="the run went idle")
    agent = _FakeAgent.instances[0]

    reply = _control("steer", subagent_id=session_id, message="second")
    assert reply["status"] == "woken"

    _wait_answer(session_id, "answered: second")
    assert _FakeAgent.instances == [agent], "the idle session woke on its kept agent"
    assert agent.turns == ["first", "second"]
    relay = wait_until(
        lambda: [row for row in _out(mode="inbox", agent_session_id=session_id)["delivered"]
                 if row["kind"] == "wake_relay"],
        what="the wake relay row")
    assert relay[0]["body"] == "answered: second"


def test_park_writes_each_sessions_row_to_its_own_profile(tmp_path, monkeypatch):
    """The watchdog is process-wide but rows are per profile (HX002): a sweep must never park a
    second profile's session into the launch profile's state.db."""
    import hermes_state
    from hermes_state import SessionDB

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    homes = [tmp_path / "alpha", tmp_path / "beta"]
    ids = ["park-alpha", "park-beta"]
    for home, session_id in zip(homes, ids):
        home.mkdir()
        db = SessionDB(home / "state.db")
        db.begin_agent_session(agent_session_id=session_id, agent_name="scout",
                               subject=session_id, transcript_path=str(home / "t.jsonl"),
                               parent_session_id="owner")
        db.close()

    runtime = agent_session_runtime.AgentSessionRuntime(limits={
        "keep_alive": True, "idle_ttl_seconds": 0.01, "max_runtime_seconds": 0,
        "max_live": 8, "max_parked": 64, "wake_queue": 32, "max_wake_turns": 1})
    for home, session_id in zip(homes, ids):
        runtime.open_session(agent_session_id=session_id, agent_name="scout",
                             subject=session_id, transcript_path=str(home / "t.jsonl"),
                             parent_session_id="owner", spec={}, profile=str(home))
        runtime.finish(session_id)

    wait_until(lambda: runtime.sweep()["parked"] == 2, what="both profiles parked")
    for home, session_id in zip(homes, ids):
        db = SessionDB(home / "state.db")
        try:
            assert db.get_agent_session(session_id)["status"] == "parked", session_id
        finally:
            db.close()
    runtime.close()


def test_a_full_resident_pool_returns_a_structured_queue_full_error():
    runtime = _runtime()
    runtime._limits["max_live"] = 1
    first = _dispatch(agent_name="researcher", user_prompts="one")
    assert first["resident"] is True

    second = _dispatch(agent_name="researcher", user_prompts="two")
    assert second["status"] == "queue_full"
    assert second["code"] == "queue_full" and "max_live" in second["error"]


def test_resident_runs_do_not_take_agent_loop_slots():
    _FakeAgent.block = True
    run = _dispatch(agent_name="researcher", user_prompts="long task")
    assert run["job_id"].startswith("agent-session-")
    assert agent_loop_service.service().stats()["running"] == 0


def test_stop_cancels_a_parked_session_and_fails_its_pending_inbox():
    run = _dispatch(agent_name="researcher", user_prompts="quick")
    session_id = run["agent_session_id"]
    _wait_answer(session_id, "answered: quick")
    wait_until(lambda: _runtime().state_of(session_id) == "idle", what="the run went idle")
    _park(session_id)

    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        db.add_agent_inbox(agent_session_id=session_id, sender="owner", recipient=session_id,
                           body="never delivered", status="pending")
    finally:
        release_or_close(db)

    stopped = _control("stop", subagent_id=session_id)
    assert stopped["status"] == "cancelled"
    row = _row(session_id)
    assert row["status"] == "cancelled"
    inbox = _out(mode="inbox", agent_session_id=session_id)
    assert inbox["pending_count"] == 0


def test_a_foreign_conversation_cannot_wake_or_stop_a_parked_session():
    run = _dispatch(agent_name="researcher", user_prompts="quick")
    session_id = run["agent_session_id"]
    _wait_answer(session_id, "answered: quick")
    wait_until(lambda: _runtime().state_of(session_id) == "idle", what="the run went idle")
    _park(session_id)

    assert "No live subagent" in _control("steer", session_id="intruder",
                                          subagent_id=session_id, message="hijack")["error"]
    assert "No live subagent" in _control("stop", session_id="intruder",
                                          subagent_id=session_id)["error"]
    assert _row(session_id)["status"] == "parked"


def test_keep_alive_false_restores_the_one_shot_run(monkeypatch):
    monkeypatch.setattr(agent_tool, "_resident_enabled", lambda: False)
    run = _dispatch(agent_name="researcher", user_prompts="quick")
    _wait_answer(run["agent_session_id"], "answered: quick")

    wait_until(lambda: _FakeAgent.instances and _FakeAgent.instances[0].closed,
               what="the one-shot agent closed")
    assert _runtime().state_of(run["agent_session_id"]) is None


def test_the_hard_wall_clock_marks_the_session_error_and_does_not_park():
    _FakeAgent.block = True
    run = _dispatch(agent_name="researcher", user_prompts="forever")
    session_id = run["agent_session_id"]
    wait_until(lambda: _FakeAgent.instances, what="the run started")
    wait_until(lambda: _runtime().state_of(session_id) == "running",
               what="the session is running")
    runtime = _runtime()
    _sweep_until(runtime, "timed_out")

    def _errored() -> dict | None:
        out = _row(session_id)
        return out if out.get("status") == "error" else None

    row = wait_until(_errored, what="the timeout row")
    assert "timed out" in str(row["final_response"])
    assert runtime.state_of(session_id) is None


def test_revive_params_carry_the_ported_definition_fields(tmp_path):
    spec = {
        "system_prompt": "You port.",
        "toolsets": [],
        "all_tools": False,
        "flags": {},
        "autoload_skills": ["team-rubric"],
        "advisor": {"model": "gpt-5-mini", "provider": "openai", "selector": "@smol"},
        "read_summarize": True,
    }
    session = agent_session_runtime._Session(
        agent_session_id="revive-1", profile=str(tmp_path), agent_name="ported",
        subject="go", transcript_path="", parent_session_id="owner", spec=spec)

    params = agent_session_runtime._revive_params(session)

    assert params["autoload_skills"] == ["team-rubric"]
    assert params["advisor"] == {"model": "gpt-5-mini", "provider": "openai",
                                 "selector": "@smol"}
    assert params["read_summarize"] is True


def test_resident_spec_never_persists_advisor_credentials():
    definition = agent_tool._Definition(body="You port.", source="ported.md", metadata={
        "advisor": "@smol",
        "autoloadSkills": ["team-rubric"],
        "read-summarize": True,
    })
    call = agent_tool._Call(
        agent="ported", user_prompt="go", definition=definition,
        provider_name="fake", model_name="fake", toolsets=[], all_tools=True,
        background=False, read_only=False, max_iterations=10, reasoning_effort="",
        worktree=False, flags={}, project=None, subject="go",
        autoload_skills=("team-rubric",),
        advisor={"model": "gpt-5-mini", "provider": "openai", "selector": "@smol"},
        read_summarize=True)

    spec = agent_tool._resident_spec(call)

    assert spec["autoload_skills"] == ["team-rubric"]
    assert spec["read_summarize"] is True
    assert "api_key" not in json.dumps(spec)
