"""AS-2/AS-3: write-once task labels for named subagent runs (ADR-21 D7).

Contract boundary: the real ``agent`` tool spawn, a real ``SessionDB`` under the temporary
``HERMES_HOME`` and the real config loader/gates run; only the model transport
(``agent.title_router.call_llm``) is faked, so the async ordering, the daemon thread and the
write-once update are observed exactly as production produces them. A label never reaches the
transcript, the tool result or the model.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
import run_agent

from agent import agent_loop_service, title_router
from agent.task_label import maybe_start_task_label
from hermes_yaml import safe_dump
from tools.registry import registry


class _FakeAgent:
    """Stands in for ``run_agent.AIAgent``: answers deterministically and closes."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tools: list = []

    def run_conversation(self, message, system_message=None, conversation_history=None,
                         task_id=None):
        prompt = message.split("Complete assignment thoroughly:\n\n", 1)[-1]
        prompt = prompt.split("\n\n--- context ---")[0]
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

    monkeypatch.setattr(agent_run, "_resolve_route",
                        lambda model, provider, base_url="", api_key="": (
                            "fake-model", {"provider": "fake", "api_key": "k"}))
    monkeypatch.setattr(run_agent, "AIAgent", _FakeAgent)
    agent_loop_service.reset_service()
    yield
    agent_loop_service.reset_service()


class _LabelTransport:
    """Stand-in for ``agent.title_router.call_llm``: records calls and answers from a script."""

    def __init__(self, reply="<title>Investigate the login regression</title>", block=None):
        self.calls: list[dict] = []
        self.replies = [reply]
        self.block = block
        self.done = threading.Event()
        self.threads: list[threading.Thread] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.threads.append(threading.current_thread())
        if self.block is not None:
            self.block.wait(10)
        content = self.replies.pop(0) if self.replies else ""
        self.done.set()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _configure_title(monkeypatch, tmp_path, **overrides) -> str:
    """A real ``config.yaml`` with the plan pinned to one candidate (and optional gate off)."""
    title = {
        "enabled": True,
        "model_upgrade_enabled": True,
        "provider": "openrouter",
        "model": "label-model",
        "timeout": 5,
    }
    title.update(overrides)
    home = tmp_path / f"home-{len(overrides)}-{time.monotonic_ns()}"
    home.mkdir()
    (home / "config.yaml").write_text(
        safe_dump({"auxiliary": {"title_generation": title}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return str(home)


def _spawn(**args) -> dict:
    raw = registry.dispatch("agent", args, session_id="main")
    return json.loads(raw) if isinstance(raw, str) else raw


def _row(agent_session_id: str) -> dict:
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        row = db.get_agent_session(agent_session_id)
    finally:
        release_or_close(db)
    assert row is not None
    return row


def _begin_row(agent_session_id: str, *, name: str = "alice") -> None:
    from hermes_state_registry import acquire, release_or_close

    db = acquire()
    try:
        db.begin_agent_session(
            agent_session_id=agent_session_id, agent_name="researcher",
            subject=f"row {agent_session_id}", transcript_path="nowhere.jsonl",
            short_name=name,
        )
    finally:
        release_or_close(db)


def _label_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "task-label"]


def _finish_label_threads() -> None:
    """Join any live label thread so a rejected label cannot land after the assertion."""
    for thread in _label_threads():
        thread.join(10)


def _wait(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ── AS-2 ────────────────────────────────────────────────────────────────────────────────────


def test_as2_a_named_run_gets_its_label_asynchronously_and_the_spawn_does_not_wait(
    monkeypatch, tmp_path,
):
    _configure_title(monkeypatch, tmp_path)
    block = threading.Event()
    transport = _LabelTransport(block=block)
    monkeypatch.setattr(title_router, "call_llm", transport)

    run = _spawn(agent_name="researcher",
                 user_prompts="Investigate the flaky login regression",
                 background=False, name="alice")

    # The model call has started and is blocked; the spawn already returned and titled nothing.
    assert _wait(lambda: bool(transport.calls))
    agent_session_id = run["agent_session_id"]
    assert _row(agent_session_id)["title"] == ""
    block.set()
    assert _wait(lambda: _row(agent_session_id)["title"] != "")
    _finish_label_threads()

    assert _row(agent_session_id)["title"] == "Investigate the login regression"
    assert transport.threads and transport.threads[0].daemon is True
    assert transport.threads[0] is not threading.main_thread()
    # The label stays metadata: not in the tool result, not in the transcript.
    assert "Investigate the login regression" not in json.dumps(run)
    transcript = open(run["transcript_path"], encoding="utf-8").read()
    assert "Investigate the login regression" not in transcript


@pytest.mark.parametrize(
    ("name", "reply", "expected"),
    [
        ("alice", "<title>Investigate the login regression</title>",
         "Investigate the login regression"),
        # 9 words are accepted; 10 are refused.
        ("alice", "<title>one two three four five six seven eight nine</title>",
         "one two three four five six seven eight nine"),
        ("alice", "<title>one two three four five six seven eight nine ten</title>", ""),
        # Quotes and the trailing period are stripped, never stored.
        ("alice", '<title>"Fix the login page."</title>', "Fix the login page"),
        # 80 characters pass, 81 are rejected (the shared title guard).
        ("alice", f"<title>{'x' * 80}</title>", "x" * 80),
        ("alice", f"<title>{'x' * 81}</title>", ""),
        # A label must not echo the run's own name or its ``name-2`` spawn handle.
        ("alice", "<title>alice</title>", ""),
        ("alice", "<title>Alice-2</title>", ""),
        # The prompt's declines write nothing.
        ("alice", "<title/>", ""),
        ("alice", "<title>none</title>", ""),
    ],
)
def test_as2_label_guards_and_bounds(monkeypatch, tmp_path, name, reply, expected):
    _configure_title(monkeypatch, tmp_path)
    transport = _LabelTransport(reply=reply)
    monkeypatch.setattr(title_router, "call_llm", transport)

    run = _spawn(agent_name="researcher",
                 user_prompts="Investigate the flaky login regression",
                 background=False, name=name)
    assert transport.done.wait(10)
    _finish_label_threads()

    assert _row(run["agent_session_id"])["title"] == expected


def test_as2_a_second_label_never_replaces_the_first(monkeypatch, tmp_path):
    from hermes_state_registry import acquire, release_or_close

    _configure_title(monkeypatch, tmp_path)
    transport = _LabelTransport()
    monkeypatch.setattr(title_router, "call_llm", transport)
    run = _spawn(agent_name="researcher", user_prompts="Investigate the flaky login regression",
                 background=False, name="alice")
    assert _wait(lambda: _row(run["agent_session_id"])["title"] != "")
    _finish_label_threads()

    db = acquire()
    try:
        assert db.set_agent_session_title(
            run["agent_session_id"], "A completely different label") is False
    finally:
        release_or_close(db)
    assert _row(run["agent_session_id"])["title"] == "Investigate the login regression"


# ── AS-3 ────────────────────────────────────────────────────────────────────────────────────


def test_as3_empty_assignment_starts_no_thread_and_no_call(monkeypatch, tmp_path):
    _configure_title(monkeypatch, tmp_path)
    transport = _LabelTransport()
    monkeypatch.setattr(title_router, "call_llm", transport)
    started: list[threading.Thread] = []
    monkeypatch.setattr("hermes_threading.spawn_context_thread",
                        lambda *args, **kwargs: started.append(args) or SimpleNamespace(start=lambda: None))
    _begin_row("researcher-empty")

    assert maybe_start_task_label(agent_session_id="researcher-empty", assignment="   ",
                                  name="alice") is None

    assert started == []
    assert transport.calls == []
    assert _row("researcher-empty")["title"] == ""


def test_as3_a_dispatcher_owned_worker_gets_no_label(monkeypatch, tmp_path):
    _configure_title(monkeypatch, tmp_path)
    transport = _LabelTransport()
    monkeypatch.setattr(title_router, "call_llm", transport)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_card")
    started: list[threading.Thread] = []
    monkeypatch.setattr("hermes_threading.spawn_context_thread",
                        lambda *args, **kwargs: started.append(args) or SimpleNamespace(start=lambda: None))

    # The whole spawn path: the run is created, no label thread and no model call follow.
    run = _spawn(agent_name="researcher", user_prompts="Investigate the flaky login regression",
                 background=False, name="alice")
    _finish_label_threads()

    assert run["status"] == "completed"
    assert started == []
    assert transport.calls == []
    assert _row(run["agent_session_id"])["title"] == ""


@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled": False},
        {"model_upgrade_enabled": False},
    ],
    ids=["enabled-off", "model-upgrade-off"],
)
def test_as3_a_disabled_switch_starts_no_thread_and_no_call(monkeypatch, tmp_path, overrides):
    _configure_title(monkeypatch, tmp_path, **overrides)
    transport = _LabelTransport()
    monkeypatch.setattr(title_router, "call_llm", transport)
    started: list[threading.Thread] = []
    monkeypatch.setattr("hermes_threading.spawn_context_thread",
                        lambda *args, **kwargs: started.append(args) or SimpleNamespace(start=lambda: None))

    run = _spawn(agent_name="researcher", user_prompts="Investigate the flaky login regression",
                 background=False, name="alice")
    _finish_label_threads()

    assert run["status"] == "completed"
    assert started == []
    assert transport.calls == []
    assert _row(run["agent_session_id"])["title"] == ""


def test_as3_the_gated_spawn_returns_without_waiting(monkeypatch, tmp_path):
    """A blocked model would hang a waiting spawn; with the gate off the call must not happen."""
    _configure_title(monkeypatch, tmp_path, enabled=False)
    never = threading.Event()  # never set: a dialled transport would hang the test file timeout
    transport = _LabelTransport(block=never)
    monkeypatch.setattr(title_router, "call_llm", transport)

    started_at = time.monotonic()
    run = _spawn(agent_name="researcher", user_prompts="Investigate the flaky login regression",
                 background=False, name="alice")
    elapsed = time.monotonic() - started_at

    assert run["status"] == "completed"
    assert elapsed < 30  # generous: the point is "did not wait for a title model at all"
    assert transport.calls == []
    never.set()
