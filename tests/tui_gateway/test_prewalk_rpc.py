"""Prewalk RPC surface (ADR-12 D5 / ADR-17 P5).

Declared ``prewalk.arm/restart/off/status`` methods, the ``prewalk.armed`` event, session
scoping, selector errors and the shared state contract.
"""

from __future__ import annotations

import pytest

import hermes_cli.config as hermes_config
import tui_gateway.server as srv
from tui_gateway.contracts.registry import EVENTS, METHODS


class _DB:
    def __init__(self) -> None:
        self.store: dict = {}

    def patch_session_model_config(self, session_id: str, patch: dict) -> None:
        for key, value in patch.items():
            if value is None:
                self.store.pop(key, None)
            else:
                self.store[key] = value

    def get_session_model_config_value(self, session_id: str, key: str, default=None):
        return self.store.get(key, default)


class _Agent:
    def __init__(self) -> None:
        self.model = "primary-model"
        self.provider = "test-provider"
        self.base_url = "https://example.test/v1"
        self.api_key = "test-key"
        self.api_mode = "chat_completions"
        self.reasoning_config = {"enabled": True, "effort": "high"}
        self.session_id = "sess-prewalk-rpc"
        self._session_db = _DB()
        self.valid_tool_names = {"todo_list", "write_file", "patch"}
        self.switches: list[dict] = []

    def switch_model(self, **kwargs):
        self.switches.append(kwargs)
        self.model = kwargs["new_model"]


def _set_prewalk(monkeypatch, **block) -> None:
    monkeypatch.setattr(hermes_config, "load_config_readonly", lambda: {"prewalk": block})


def _live_session(monkeypatch, agent=None, sid: str = "runtime-prewalk") -> str:
    monkeypatch.setitem(srv._sessions, sid, {
        "agent": agent,
        "session_key": "stored-prewalk",
        "profile_home": None,
    })
    return sid


def _call(name: str, params: dict) -> dict:
    return srv._methods[name](1, params)


def test_prewalk_methods_and_events_are_declared():
    assert {"prewalk.arm", "prewalk.restart", "prewalk.off", "prewalk.status"} <= set(METHODS)
    assert {"prewalk.armed", "prewalk.handoff"} <= set(EVENTS)
    # The wire params use ``from`` (a Python keyword), and unknown keys are rejected.
    METHODS["prewalk.arm"].params.model_validate(
        {"session_id": "s", "target": "fast-model", "from": "@default"})
    with pytest.raises(Exception):
        METHODS["prewalk.arm"].params.model_validate(
            {"session_id": "s", "targte": "fast-model"})
    EVENTS["prewalk.armed"].payload.model_validate(
        {"armed": True, "from": "@default", "into": "fast-model"})
    EVENTS["prewalk.handoff"].payload.model_validate(
        {"from_model": "a", "into_model": "b", "provider": "p"})


def test_arm_status_off_contract(monkeypatch):
    _set_prewalk(monkeypatch)
    events: list[tuple] = []
    monkeypatch.setattr(srv, "_emit", lambda event, sid, payload=None: events.append((event, sid, payload)))
    sid = _live_session(monkeypatch, _Agent())

    armed = _call("prewalk.arm", {"session_id": sid, "target": "fast-model"})
    result = armed["result"]
    METHODS["prewalk.arm"].result.model_validate(result)
    assert result["armed"] is True
    assert result["into"] == "fast-model"
    assert result["gate"] == "closed"
    assert events[-1][0] == "prewalk.armed" and events[-1][1] == sid
    assert events[-1][2]["armed"] is True

    status = _call("prewalk.status", {"session_id": sid})["result"]
    assert status["armed"] is True and status["from"] == "@default"

    off = _call("prewalk.off", {"session_id": sid})["result"]
    assert off["armed"] is False
    assert events[-1][0] == "prewalk.armed" and events[-1][2]["armed"] is False


def test_restart_emits_armed_and_returns_planning_model(monkeypatch):
    _set_prewalk(monkeypatch)
    events: list[tuple] = []
    monkeypatch.setattr(srv, "_emit", lambda event, sid, payload=None: events.append((event, sid, payload)))
    agent = _Agent()
    sid = _live_session(monkeypatch, agent)
    _call("prewalk.arm", {"session_id": sid, "target": "fast-model"})
    agent.model = "fast-model"
    result = _call("prewalk.restart", {"session_id": sid})["result"]
    assert result["armed"] is True
    assert agent.model == "primary-model"
    assert events[-1][0] == "prewalk.armed"


def test_invalid_target_is_a_structured_error(monkeypatch):
    _set_prewalk(monkeypatch)
    sid = _live_session(monkeypatch, _Agent())
    envelope = _call("prewalk.arm", {"session_id": sid, "target": "@nope"})
    assert envelope["error"]["code"] == 4002


def test_missing_session_and_unready_agent(monkeypatch):
    _set_prewalk(monkeypatch)
    missing = _call("prewalk.status", {"session_id": "does-not-exist"})
    assert missing["error"]["code"] == 4001
    sid = _live_session(monkeypatch, None)
    not_ready = _call("prewalk.status", {"session_id": sid})
    assert not_ready["error"]["code"] == 4001


def test_handoff_state_round_trips_through_the_session_row(monkeypatch):
    _set_prewalk(monkeypatch)
    db = _DB()
    agent = _Agent()
    agent._session_db = db
    sid = _live_session(monkeypatch, agent)
    _call("prewalk.arm", {"session_id": sid, "target": "fast-model"})
    assert db.store["prewalk"]["armed"] is True
    resumed = _Agent()
    resumed._session_db = db
    resumed_sid = _live_session(monkeypatch, resumed, sid="runtime-prewalk-resumed")
    assert _call("prewalk.status", {"session_id": resumed_sid})["result"]["armed"] is True


def test_rpc_arm_installs_the_live_handoff_event_hook(monkeypatch):
    _set_prewalk(monkeypatch)
    events: list[tuple] = []
    monkeypatch.setattr(srv, "_emit", lambda event, sid, payload=None: events.append((event, sid, payload)))
    agent = _Agent()
    sid = _live_session(monkeypatch, agent)
    _call("prewalk.arm", {"session_id": sid, "target": "fast-model"})

    from agent import prewalk
    from types import SimpleNamespace

    calls = [SimpleNamespace(id="call-0"), SimpleNamespace(id="call-1")]
    outcome = prewalk.advance_after_tool_batch(
        agent, SimpleNamespace(tool_calls=calls),
        [
            {"role": "tool", "name": "todo_list", "tool_call_id": "call-0", "content": "{}"},
            {"role": "tool", "name": "write_file", "tool_call_id": "call-1",
             "content": '{"status": "success"}'},
        ],
    )
    assert outcome["reason"] == "handoff"
    assert events[-1][0] == "prewalk.handoff"
    assert events[-1][2]["into_model"] == "fast-model"
    assert events[-1][2]["from_model"] == "primary-model"
