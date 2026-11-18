"""Review queue: worker drain, retry/drop ladder, quota latch, stale tool eviction."""

from __future__ import annotations

import json
import threading
import time

import pytest

from agent.advisor_config import AdvisorConfig
from agent.advisor_model import AdvisorUsageLimit, AdviseTool
from agent.advisor_recorder import AdvisorRecorder
from agent.advisor_runtime import (
    AdvisorRuntime,
    AdvisorSession,
    AdvisorSettings,
    advisor_session_kind,
    evict_stale_tool_results,
)


def test_a_named_agent_advisor_opts_an_unattended_peer_in():
    agent = type("A", (), {"platform": "subagent", "_delegate_depth": 0,
                           "_advisor_override": True})()

    assert advisor_session_kind(agent) == "sub"


def test_a_plain_peer_without_the_field_hosts_no_advisor():
    agent = type("A", (), {"platform": "subagent", "_delegate_depth": 0})()

    assert advisor_session_kind(agent) is None


def test_the_agent_advisor_selector_fills_the_roster_model():
    agent = type("A", (), {"_advisor_override": {"model": "gpt-5.2",
                                                 "provider": "openai",
                                                 "reasoning_effort": "high"}})()
    session = AdvisorSession(agent, kind="sub")

    config = session._override_config(AdvisorConfig(name="Reviewer", slug="reviewer"))

    assert (config.provider, config.model) == ("openai", "gpt-5.2")
    assert config.reasoning_effort == "high"


def test_an_explicit_roster_model_beats_the_agent_selector():
    agent = type("A", (), {"_advisor_override": {"model": "gpt-5.2"}})()
    session = AdvisorSession(agent, kind="sub")

    config = session._override_config(AdvisorConfig(name="Reviewer", model="claude-sonnet-4"))

    assert config.model == "claude-sonnet-4"


def test_the_advisor_level_reaches_the_auxiliary_call(monkeypatch):
    from agent import auxiliary_client

    seen: dict = {}
    monkeypatch.setattr(auxiliary_client, "call_llm",
                        lambda **kwargs: seen.update(kwargs) or {})
    session = AdvisorSession(type("A", (), {})(), kind="sub")
    config = AdvisorConfig(name="Reviewer", provider="openai", model="gpt-5.2",
                           reasoning_effort="high")

    session._call_model(config)(messages=[], tools=None)

    assert seen["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert (seen["provider"], seen["model"]) == ("openai", "gpt-5.2")


class _FakeModel:
    def __init__(self, failures: int = 0, exc: Exception | None = None):
        self.messages: list[dict] = [{"role": "system", "content": "sys"}]
        self.failures = failures
        self.exc = exc or RuntimeError("boom")
        self.calls = 0
        self.resets = 0
        self.deltas: list[list[str]] = []

    def run(self, deltas, *, wip, covered_turn=None, terminal=False):
        self.calls += 1
        self.deltas.append(list(deltas))
        if self.calls <= self.failures:
            raise self.exc

    def reset(self):
        self.resets += 1
        self.messages = [{"role": "system", "content": "sys"}]


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr("agent.advisor_runtime.RETRY_DELAY_S", 0.0)


def _runtime(tmp_path, model, *, settings=None, config=None):
    return AdvisorRuntime(
        config=config or AdvisorConfig(name="Reviewer", slug="reviewer"),
        settings=settings or AdvisorSettings(),
        model=model,
        recorder=AdvisorRecorder(session_id="s1", slug="reviewer", directory=tmp_path),
        advise=AdviseTool(lambda *args: None),
    )


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_successful_review_drains_the_backlog(tmp_path):
    model = _FakeModel()
    runtime = _runtime(tmp_path, model)

    runtime.capture([{"role": "user", "content": "go"}], wip=False)

    assert _wait(lambda: runtime.backlog == 0)
    assert model.calls == 1
    assert runtime.status == "running"


def test_transient_failures_retry_then_drop(tmp_path):
    model = _FakeModel(failures=3)
    runtime = _runtime(tmp_path, model)

    runtime.capture([{"role": "user", "content": "go"}], wip=False)

    assert _wait(lambda: runtime.backlog == 0)
    assert model.calls == 3
    assert runtime.status == "running"  # one dropped backlog is not a halt


def test_quota_latches_and_stops_draining(tmp_path):
    model = _FakeModel(failures=1, exc=AdvisorUsageLimit("429 usage limit"))
    runtime = _runtime(tmp_path, model)

    runtime.capture([{"role": "user", "content": "go"}], wip=False)

    assert _wait(lambda: runtime.status == "quota_exhausted")
    assert runtime.wait_for_catchup(1.0) is True  # the dropped batch left nothing to wait for
    runtime.capture([{"role": "user", "content": "more"}], wip=False)
    assert runtime.backlog == 0
    assert model.calls == 1  # the latch refuses new reviews until reset


def test_timed_429_waits_out_the_retry_after_instead_of_latching(tmp_path):
    model = _FakeModel(failures=1, exc=AdvisorUsageLimit("429 Rate limit exceeded: retry-after-ms=20"))
    runtime = _runtime(tmp_path, model, settings=AdvisorSettings(max_rate_limit_wait_s=1.0))

    runtime.capture([{"role": "user", "content": "go"}], wip=False)

    assert _wait(lambda: runtime.backlog == 0)
    assert model.calls == 2
    assert runtime.status == "running"


def test_repeated_timed_throttles_latch_after_the_per_batch_budget(tmp_path):
    model = _FakeModel(failures=99, exc=AdvisorUsageLimit("429 retry-after-ms=1"))
    runtime = _runtime(tmp_path, model, settings=AdvisorSettings(max_rate_limit_wait_s=1.0))

    runtime.capture([{"role": "user", "content": "go"}], wip=False)

    assert _wait(lambda: runtime.status == "quota_exhausted")
    # Three honored waits, then the fourth refusal falls through to the latch.
    assert model.calls == 4


def test_rate_limit_wait_beyond_the_cap_still_latches(tmp_path):
    model = _FakeModel(failures=1, exc=AdvisorUsageLimit("429 Rate limit exceeded: retry-after-ms=60000"))
    runtime = _runtime(tmp_path, model, settings=AdvisorSettings(max_rate_limit_wait_s=1.0))

    runtime.capture([{"role": "user", "content": "go"}], wip=False)

    assert _wait(lambda: runtime.status == "quota_exhausted")
    assert model.calls == 1


def test_include_reasoning_false_omits_primary_thinking(tmp_path):
    model = _FakeModel()
    runtime = _runtime(tmp_path, model, config=AdvisorConfig(
        name="Reviewer", slug="reviewer", include_reasoning=False))
    messages = [
        {"role": "user", "content": "verify the patch"},
        {"role": "assistant", "content": "Observable answer.",
         "reasoning": "Internal hypothesis that must not anchor the reviewer."},
    ]

    runtime.capture(messages, wip=False)

    assert _wait(lambda: model.calls == 1)
    rendered = "\n".join(model.deltas[0])
    assert "Observable answer." in rendered
    assert "Internal hypothesis" not in rendered


def test_delta_dispatch_false_is_held_until_flush(tmp_path):
    model = _FakeModel()
    runtime = _runtime(tmp_path, model)

    runtime.capture([{"role": "user", "content": "one"}], wip=False, dispatch=False)
    assert runtime.backlog == 0

    runtime.flush_held()

    assert _wait(lambda: model.calls == 1)


def test_eviction_blanks_only_old_tool_results(tmp_path):
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reading"},
        {"role": "tool", "name": "read_file", "content": "x" * 4000},
        {"role": "user", "content": "latest review"},
    ]

    evicted = evict_stale_tool_results(messages)

    assert evicted == 1
    assert "Stale result elided" in messages[2]["content"]
    assert messages[2]["_advisor_pruned"] is True


class _ScriptedModel(_FakeModel):
    def __init__(self, errors):
        super().__init__()
        self.errors = list(errors)

    def run(self, deltas, *, wip, covered_turn=None, terminal=False):
        self.calls += 1
        self.deltas.append(list(deltas))
        if self.errors:
            raise self.errors.pop(0)


def test_context_overflow_reprime_keeps_the_failure_strikes(tmp_path):
    from agent.advisor_model import AdvisorContextOverflow

    model = _ScriptedModel([
        RuntimeError("boom"),
        RuntimeError("boom"),
        AdvisorContextOverflow("context length exceeded"),
        RuntimeError("boom"),
        RuntimeError("boom"),
        RuntimeError("boom"),
    ])
    runtime = _runtime(tmp_path, model)

    runtime.capture([{"role": "user", "content": "go"}], wip=False)

    assert _wait(lambda: runtime.backlog == 0)
    # Two strikes, one overflow re-prime that keeps them, then the third strike drops the batch.
    assert model.calls == 4
    assert runtime.status == "running"


class _SessionAgent:
    def __init__(self, cwd):
        self.session_id = "session-1"
        self.session_cwd = str(cwd)
        self._model_request_active = threading.Event()
        self._interrupt_requested = False
        self._executing_tools = False
        self._session_messages: list[dict] = []
        self.steered: list[tuple[str, str]] = []
        self.interrupts: list[str] = []
        self.plugin_message_callback = None

    def steer(self, text, *, kind="user"):
        self.steered.append((text, kind))
        return True

    def interrupt(self, *, tool_reason=""):
        self.interrupts.append(tool_reason)

    def _flush_messages_to_session_db(self, messages, history=None):
        return None


def _advise_response(note: str, severity: str):
    from types import SimpleNamespace

    arguments = json.dumps({"note": note, "severity": severity})
    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="advise", arguments=arguments))
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))])


def _final_response():
    from types import SimpleNamespace

    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=None))])


def test_agent_end_mode_skips_mid_turn_reviews(monkeypatch, tmp_path):
    import agent.advisor_runtime as runtime_mod
    from agent.advisor_config import AdvisorConfig, DiscoveredAdvisors
    from agent.advisor_runtime import AdvisorSession, AdvisorSettings

    monkeypatch.setattr(runtime_mod, "load_settings",
                        lambda: AdvisorSettings(enabled=True, review_mode="agent-end", sync_backlog="strict"))
    monkeypatch.setattr(runtime_mod, "discover_advisors",
                        lambda cwd, home="": DiscoveredAdvisors(advisors=[
                            AdvisorConfig(name="Reviewer", slug="reviewer", tools=[])]))
    calls: list[int] = []
    responses = [_advise_response("the test never ran", "blocker")]

    def _call(self, config):
        def call(**kwargs):
            calls.append(len(kwargs.get("messages") or []))
            return responses.pop(0) if responses else _final_response()
        return call

    monkeypatch.setattr(AdvisorSession, "_call_model", _call)
    agent = _SessionAgent(tmp_path)
    session = AdvisorSession(agent)
    session.bind_turn()

    session.on_step([{"role": "user", "content": "go"},
                     {"role": "assistant", "content": "step"}], terminal=False)
    assert calls == []  # agent-end: mid-turn boundaries are held, not reviewed

    session.on_step([{"role": "user", "content": "go"},
                     {"role": "assistant", "content": "done"}], terminal=True)
    assert _wait(lambda: session._continuation is not None)
    assert "the test never ran" in session.take_continuation()["text"]


def test_watchdog_scope_selects_which_session_builds_an_entry(monkeypatch, tmp_path):
    import agent.advisor_runtime as runtime_mod
    from agent.advisor_config import AdvisorConfig, DiscoveredAdvisors
    from agent.advisor_runtime import AdvisorSession, AdvisorSettings

    roster = [
        AdvisorConfig(name="Main", slug="main", scope="main", tools=[]),
        AdvisorConfig(name="Sub", slug="sub", scope="subagent", tools=[]),
        AdvisorConfig(name="Both", slug="both", scope="all", tools=[]),
        AdvisorConfig(name="Unset", slug="unset", tools=[]),
    ]
    monkeypatch.setattr(runtime_mod, "load_settings", lambda: AdvisorSettings(enabled=True))
    monkeypatch.setattr(runtime_mod, "discover_advisors",
                        lambda cwd, home="": DiscoveredAdvisors(advisors=roster))

    main = AdvisorSession(_SessionAgent(tmp_path), kind="main")
    main.bind_turn()
    assert [a["name"] for a in main.status()["advisors"]] == ["Main", "Both", "Unset"]

    sub = AdvisorSession(_SessionAgent(tmp_path), kind="sub")
    sub.bind_turn()
    assert [a["name"] for a in sub.status()["advisors"]] == ["Sub", "Both"]


def test_a_main_only_roster_still_reviews_an_agent_that_opted_in(monkeypatch, tmp_path):
    import agent.advisor_runtime as runtime_mod
    from agent.advisor_config import AdvisorConfig, DiscoveredAdvisors
    from agent.advisor_runtime import AdvisorSession, AdvisorSettings

    monkeypatch.setattr(runtime_mod, "load_settings", lambda: AdvisorSettings(enabled=True))
    monkeypatch.setattr(runtime_mod, "discover_advisors",
                        lambda cwd, home="": DiscoveredAdvisors(advisors=[
                            AdvisorConfig(name="Main", slug="main", scope="main", tools=[])]))
    agent = _SessionAgent(tmp_path)
    agent._advisor_override = True
    session = AdvisorSession(agent, kind="sub")

    session.bind_turn()

    assert [a["name"] for a in session.status()["advisors"]] == ["default"]


def test_nested_sessions_never_bind_or_review(monkeypatch, tmp_path):
    import agent.advisor_runtime as runtime_mod
    from agent.advisor_bridge import ensure_advisor
    from agent.advisor_config import AdvisorConfig, DiscoveredAdvisors
    from agent.advisor_runtime import AdvisorSession, AdvisorSettings

    monkeypatch.setattr(runtime_mod, "load_settings", lambda: AdvisorSettings(enabled=True))
    monkeypatch.setattr(runtime_mod, "discover_advisors",
                        lambda cwd, home="": DiscoveredAdvisors(advisors=[
                            AdvisorConfig(name="Reviewer", slug="reviewer", tools=[])]))
    agent = _SessionAgent(tmp_path)
    agent._delegate_depth = 1

    session = ensure_advisor(agent)
    assert session is not None  # a subagent may host subagent-scoped entries...
    session.bind_turn()
    session.on_step([{"role": "user", "content": "go"}], terminal=True)

    assert session._runtimes == []  # ...but this roster is main-scoped, so none are built
    assert session.status()["advisors"] == []

    cron = _SessionAgent(tmp_path)
    cron.platform = "cron"
    assert ensure_advisor(cron) is None

    peer = _SessionAgent(tmp_path)
    peer.platform = "subagent"  # agent_run.py peer: unattended, never delegated
    assert ensure_advisor(peer) is None
