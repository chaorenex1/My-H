"""ADR-1 ``session.freeze``: generation latch, idempotency, managed frames and snapshot wiring."""

from __future__ import annotations

import contextlib
import threading
import time
from types import SimpleNamespace

import pytest

from gateway import session_freeze as freeze_mod
from gateway.session_contract import SessionRef
from gateway.session_freeze import (
    bind_freeze,
    bind_freeze_sender,
    freeze,
    freeze_state,
    release_freeze,
)
from agent.pause_gate import AgentPauseGate, gate_for

REF = SessionRef("fixture", "s1")


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _agent():
    return SimpleNamespace(_interrupt_requested=False, _hard_interrupt_requested=threading.Event())


def _authority(*, generation=7, running=True):
    live = SimpleNamespace(event_stream=SimpleNamespace(lock=threading.Lock()), source=None)
    authority = SimpleNamespace(
        profile_id="fixture", instance_id="i", epoch=1,
        sessions={"s1": live}, adopted={}, _freezes={}, _freeze_receipts={},
    )
    authority.authorize = lambda actor, ref, capability: None
    authority._handle = lambda ref: SimpleNamespace(
        execution_generation=generation,
        execution_state="running" if running else "idle",
    )
    return authority


def _connection(authority):
    return SimpleNamespace(authority=authority,
                           actor=SimpleNamespace(profile_id="fixture",
                                                 capabilities=frozenset({"session:control"})))


def _params(**overrides):
    return {"session_id": "s1", "execution_generation": 7, "frozen": True, **overrides}


@pytest.fixture(autouse=True)
def _no_publish(monkeypatch):
    published = []
    monkeypatch.setattr(freeze_mod, "_publish", lambda authority, sid: published.append(sid))
    return published


@pytest.mark.asyncio
async def test_freeze_parks_the_adopted_agent_and_is_idempotent(_no_publish):
    authority = _authority()
    agent = _agent()
    authority.adopted["s1"] = (7, agent)
    bind_freeze(authority, "s1", 7, agent)
    gate = gate_for(agent)

    first = await freeze(_connection(authority), REF, _params())
    assert first["freeze"]["frozen"] is True
    assert first["freeze"]["running"] is True
    assert first["freeze"]["execution_generation"] == 7
    assert first["freeze"]["held_seconds"] is not None
    assert gate.frozen

    second = await freeze(_connection(authority), REF, _params())
    assert second["freeze"]["frozen_at"] == first["freeze"]["frozen_at"]

    released = await freeze(_connection(authority), REF, _params(frozen=False))
    assert released["freeze"]["frozen"] is False
    assert not gate.frozen
    assert freeze_state(authority, "s1") is None
    assert _no_publish == ["s1", "s1", "s1"]  # engage, repeat, release each repaint the card


@pytest.mark.asyncio
async def test_one_sessions_freeze_never_touches_another():
    authority = _authority()
    first, second = _agent(), _agent()
    authority.adopted.update({"s1": (7, first), "s2": (7, second)})
    bind_freeze(authority, "s1", 7, first)
    bind_freeze(authority, "s2", 7, second)
    authority.sessions["s2"] = SimpleNamespace(event_stream=SimpleNamespace(lock=threading.Lock()),
                                               source=None)

    await freeze(_connection(authority), REF, _params())
    assert gate_for(first).frozen
    assert not gate_for(second).frozen
    assert freeze_state(authority, "s2") is None


@pytest.mark.asyncio
async def test_freeze_requires_the_session_control_capability():
    from hermes_state_runtime import RuntimeStoreError

    authority = _authority()
    capabilities = []

    def authorize(actor, ref, capability):
        capabilities.append(capability)
        raise RuntimeStoreError("permission_denied")

    authority.authorize = authorize
    with pytest.raises(RuntimeStoreError) as denied:
        await freeze(_connection(authority), REF, _params())
    assert denied.value.reason == "permission_denied"
    assert capabilities == ["session:control"]


@pytest.mark.asyncio
async def test_idle_session_returns_running_false_without_state():
    authority = _authority(running=False)
    reply = await freeze(_connection(authority), REF, _params())
    assert reply["freeze"]["running"] is False
    assert reply["freeze"]["frozen"] is False
    assert freeze_state(authority, "s1") is None


@pytest.mark.asyncio
async def test_stale_generation_refuses_freeze_but_a_stale_release_is_a_no_op():
    authority = _authority(generation=9)
    with pytest.raises(Exception) as refused:
        await freeze(_connection(authority), REF, _params(execution_generation=7))
    assert getattr(refused.value, "reason", None) == "stale_generation"
    assert getattr(refused.value, "execution_generation", None) == 9

    released = await freeze(_connection(authority), REF,
                            {"session_id": "s1", "execution_generation": 7, "frozen": False})
    assert released["freeze"]["frozen"] is False
    assert released["freeze"]["execution_generation"] == 9


@pytest.mark.asyncio
async def test_repeated_request_id_answers_the_first_receipt():
    authority = _authority()
    agent = _agent()
    authority.adopted["s1"] = (7, agent)
    bind_freeze(authority, "s1", 7, agent)

    first = await freeze(_connection(authority), REF, _params(request_id="req-1"))
    await freeze(_connection(authority), REF, _params(frozen=False, request_id="req-2"))
    replay = await freeze(_connection(authority), REF, _params(request_id="req-1"))
    assert replay == first


def test_release_resumes_a_parked_agent():
    authority = _authority()
    agent = _agent()
    authority.adopted["s1"] = (7, agent)
    bind_freeze(authority, "s1", 7, agent)
    gate = gate_for(agent)
    freeze_mod.freezes(authority)["s1"] = freeze_mod.FreezeBinding(7, time.time(), gate=gate)
    gate.pause()
    results = []
    waiter = threading.Thread(target=lambda: results.append(gate.wait(agent)))
    waiter.start()
    assert _wait(lambda: gate.parked() == 1)

    assert release_freeze(authority, "s1") is True
    waiter.join(2)
    assert results == ["released"]
    assert release_freeze(authority, "s1") is False


@pytest.mark.asyncio
async def test_managed_session_freezes_through_control_frames():
    authority = _authority()
    sent = []
    bind_freeze_sender(authority, "s1", 7, sent.append)

    engaged = await freeze(_connection(authority), REF, _params())
    assert engaged["freeze"]["frozen"] is True
    assert engaged["freeze"]["parked_agents"] is None  # v1 never crosses the process seam
    assert sent == [True]

    await freeze(_connection(authority), REF, _params(frozen=False))
    assert sent == [True, False]


@pytest.mark.asyncio
async def test_freeze_latched_before_the_agent_is_bound():
    authority = _authority()
    engaged = await freeze(_connection(authority), REF, _params())
    assert engaged["freeze"]["frozen"] is True

    agent = _agent()
    bind_freeze(authority, "s1", 7, agent)
    assert gate_for(agent).frozen  # bind applied the latch


def test_control_snapshot_carries_freeze_and_estop(monkeypatch):
    from gateway import session_ancillary

    class _DB:
        def get_compression_tip(self, sid):
            return None

        def get_meta(self, key):
            return None

    authority = SimpleNamespace(db=_DB(), profile_id="fixture", _freezes={},
                                sessions={"s1": SimpleNamespace(source=None)})
    session_freeze = freeze_mod.FreezeBinding(generation=7, frozen_at=100.0, gate=AgentPauseGate())
    authority._freezes["s1"] = session_freeze
    in_owner_scope = []

    @contextlib.contextmanager
    def scope(_authority):
        in_owner_scope.append(True)
        yield

    monkeypatch.setattr("gateway.session_authorities.owner_scope", scope)

    def estop_state():
        # The sentinel is read for the OWNING profile, never the launch profile's ambient files.
        assert in_owner_scope
        return {"reason": "maintenance", "engaged_at": "2026-11-01T10:00:00+00:00"}

    monkeypatch.setattr("agent.estop.get_state", estop_state)

    control = session_ancillary.control_snapshot(authority, REF)
    assert control["freeze"] == {
        "frozen": True, "frozen_at": 100.0, "execution_generation": 7, "parked_agents": 0,
    }
    assert control["estop"] == {
        "engaged": True, "reason": "maintenance", "engaged_at": "2026-11-01T10:00:00+00:00",
    }
    assert control["revision"]

    monkeypatch.setattr("agent.estop.get_state", lambda: None)
    assert session_ancillary.control_snapshot(authority, REF)["revision"] != control["revision"]
