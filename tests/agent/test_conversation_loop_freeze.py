"""ADR-1: a frozen turn parks before building its next API request; Stop ends it there."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

from agent import conversation_loop
from agent.pause_gate import gate_for


@dataclass
class _Verdict:
    action: str = "ok"
    result: dict | None = None


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _agent():
    return SimpleNamespace(_interrupt_requested=False, _hard_interrupt_requested=threading.Event())


def _drive(monkeypatch, *, calls):
    """Run ``_run_api_retry_loop`` once with only the seams this test observes."""
    state = SimpleNamespace(retry_count=0, max_retries=3)
    performed = []

    def guard(agent):
        return _Verdict()

    def build(agent):
        calls.append("build_api_request")
        return _Verdict()

    def perform(agent):
        performed.append(1)
        state.retry_count += 1
        return _Verdict(action="break")

    def check(agent):
        raise AssertionError("check_api_response must not run after a successful call")

    def interrupted(agent):
        calls.append("handle_api_interrupt")
        return _Verdict(action="break")

    monkeypatch.setattr(conversation_loop, "nous_rate_limit_guard", guard)
    monkeypatch.setattr(conversation_loop, "build_api_request", build)
    monkeypatch.setattr(conversation_loop, "perform_api_call", perform)
    monkeypatch.setattr(conversation_loop, "check_api_response", check)
    monkeypatch.setattr(conversation_loop, "handle_api_interrupt", interrupted)
    return state, performed


def test_freeze_parks_before_the_next_api_request_and_resume_sends_it_once(monkeypatch):
    calls = []
    state, performed = _drive(monkeypatch, calls=calls)
    agent = _agent()
    gate = gate_for(agent)
    gate.pause()

    runner = threading.Thread(target=conversation_loop._run_api_retry_loop, args=(agent, state))
    runner.start()
    assert _wait(lambda: gate.parked() == 1)
    assert calls == []  # frozen: no request was built or announced
    gate.resume()
    runner.join(5)
    assert calls == ["build_api_request"]
    assert performed == [1]


def test_stop_releases_the_parked_request_without_sending_it(monkeypatch):
    calls = []
    state, performed = _drive(monkeypatch, calls=calls)
    agent = _agent()
    gate = gate_for(agent)
    gate.pause()

    runner = threading.Thread(target=conversation_loop._run_api_retry_loop, args=(agent, state))
    runner.start()
    assert _wait(lambda: gate.parked() == 1)
    agent._interrupt_requested = True
    runner.join(2)
    assert not runner.is_alive()
    assert calls == ["handle_api_interrupt"]  # the existing Stop seam, never a phantom request
    assert performed == []


def test_freeze_parks_at_the_iteration_boundary():
    from agent.turn_iteration_prep import begin_iteration

    agent = SimpleNamespace(
        _interrupt_requested=False, _hard_interrupt_requested=threading.Event(), quiet_mode=True,
        _drain_pending_redirect=lambda: None,
        _checkpoint_mgr=SimpleNamespace(new_turn=lambda: None),
    )
    gate = gate_for(agent)
    gate.pause()
    results = []
    runner = threading.Thread(target=lambda: results.append(begin_iteration(
        agent, messages=[], conversation_history=[], original_user_message="u",
        api_call_count=0, interrupted=False, _turn_exit_reason=None)))
    runner.start()
    assert _wait(lambda: gate.parked() == 1)
    assert results == []  # the iteration never assembled or announced a request

    agent._interrupt_requested = True
    runner.join(2)
    assert results and results[0].action == "break"


def test_unfrozen_turn_is_unaffected(monkeypatch):
    calls = []
    state, performed = _drive(monkeypatch, calls=calls)
    conversation_loop._run_api_retry_loop(_agent(), state)
    assert calls == ["build_api_request"]
    assert performed == [1]
