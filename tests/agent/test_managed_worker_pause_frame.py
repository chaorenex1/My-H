"""ADR-1 managed workers: the pause control frame drives the worker's own gate."""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

from agent import managed_worker as mw


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _controls(monkeypatch):
    frames = queue.Queue()

    def fake_read(stream):
        item = frames.get(timeout=5)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(mw, "read_frame", fake_read)
    monkeypatch.setattr(mw, "_die_with_owner", lambda: None)
    channel = SimpleNamespace(send=lambda *args, **kwargs: None)
    controls = mw.WorkerControls(channel, "route")
    return controls, frames


def test_pause_and_resume_frames_drive_the_worker_gate(monkeypatch):
    controls, frames = _controls(monkeypatch)
    frames.put({"type": "pause", "paused": True})
    assert _wait(lambda: controls.gate.frozen)

    frames.put({"type": "pause", "paused": False})
    assert _wait(lambda: not controls.gate.frozen)

    frames.put({"type": "finish"})
    controls.reader.join(5)
    assert not controls.reader.is_alive()


def test_parked_worker_agent_releases_on_resume(monkeypatch):
    controls, frames = _controls(monkeypatch)
    agent = SimpleNamespace(_interrupt_requested=False,
                            _hard_interrupt_requested=threading.Event())
    agent._pause_gate = controls.gate  # what managed_worker.execute() binds after construction

    frames.put({"type": "pause", "paused": True})
    assert _wait(lambda: controls.gate.frozen)
    results = []
    waiter = threading.Thread(target=lambda: results.append(controls.gate.wait(agent, boundary="api request")))
    waiter.start()
    assert _wait(lambda: controls.gate.parked() == 1)
    assert results == []

    frames.put({"type": "pause", "paused": False})
    waiter.join(2)
    assert results == ["released"]

    frames.put({"type": "finish"})
    controls.reader.join(5)


def test_malformed_pause_frame_is_a_control_error_not_a_hang(monkeypatch):
    controls, frames = _controls(monkeypatch)
    frames.put({"type": "pause", "paused": "yes"})
    assert _wait(lambda: controls.stopped.is_set())
    controls.reader.join(5)
    assert not controls.gate.frozen
