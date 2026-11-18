"""ADR-1 pause gate: cooperative freeze semantics for one session's execution tree."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from agent.pause_gate import (
    INTERRUPTED,
    RELEASED,
    AgentPauseGate,
    gate_for,
    park,
    share_gate,
)


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _agent(**attrs):
    values = {"_interrupt_requested": False, "_hard_interrupt_requested": threading.Event()}
    values.update(attrs)
    return SimpleNamespace(**values)


def test_wait_parks_until_resume():
    gate, agent = AgentPauseGate(), _agent()
    gate.pause()
    results = []
    waiter = threading.Thread(target=lambda: results.append(gate.wait(agent, boundary="api request")))
    waiter.start()
    assert _wait(lambda: gate.parked() == 1)
    assert results == []  # still parked: the snapshot is taken before release
    gate.resume()
    waiter.join(5)
    assert results == [RELEASED]
    assert gate.parked() == 0


def test_stop_releases_a_parked_turn_within_a_second():
    gate, agent = AgentPauseGate(), _agent()
    gate.pause()
    results = []
    waiter = threading.Thread(target=lambda: results.append(gate.wait(agent, boundary="iteration")))
    waiter.start()
    assert _wait(lambda: gate.parked() == 1)
    started = time.monotonic()
    agent._interrupt_requested = True
    waiter.join(2)
    assert results == [INTERRUPTED]
    assert time.monotonic() - started < 1.0
    assert gate.frozen  # the freeze itself stays until explicitly released


def test_hard_stop_event_releases_a_parked_turn():
    gate, agent = AgentPauseGate(), _agent()
    gate.pause()
    results = []
    waiter = threading.Thread(target=lambda: results.append(gate.wait(agent)))
    waiter.start()
    assert _wait(lambda: gate.parked() == 1)
    agent._hard_interrupt_requested.set()
    waiter.join(2)
    assert results == [INTERRUPTED]


def test_pause_and_resume_are_idempotent():
    gate = AgentPauseGate()
    assert gate.pause() is True
    frozen_at = gate.frozen_at
    assert gate.pause() is False
    assert gate.frozen_at == frozen_at  # a repeated freeze keeps the original timestamp
    assert gate.resume() is True
    assert gate.frozen_at is None
    assert gate.resume() is False


def test_park_without_a_freeze_is_free():
    agent = _agent()
    assert park(agent, boundary="tools") is False
    gate_for(agent)  # a bound but unfrozen gate never parks
    assert park(agent, boundary="tools") is False


def test_gate_for_and_share_gate_bind_one_object():
    parent, child = _agent(), _agent()
    gate = gate_for(parent)
    assert gate_for(parent) is gate
    assert share_gate(child, parent) is gate
    assert child._pause_gate is gate


def test_parked_turns_stamp_their_liveness(monkeypatch):
    monkeypatch.setattr("agent.pause_gate._HEARTBEAT_S", 0.01)
    beats = []
    gate, agent = AgentPauseGate(), _agent(_touch_activity=beats.append)
    gate.pause()
    waiter = threading.Thread(target=lambda: gate.wait(agent, boundary="iteration"))
    waiter.start()
    assert _wait(lambda: beats)
    gate.resume()
    waiter.join(5)
    assert any("frozen at iteration" in beat for beat in beats)


def test_on_change_listener_fires_and_unsubscribes():
    gate, calls = AgentPauseGate(), []
    off = gate.onChange(lambda: calls.append(1))
    gate.pause()
    gate.resume()
    assert len(calls) >= 2
    off()
    before = len(calls)
    gate.pause()
    gate.resume()
    assert len(calls) == before
