"""Prewalk handoff state machine (ADR-12 D5 / ADR-17 P5).

Default off; explicit arm/disarm/restart; the Todo gate; one handoff at the first successful
edit after that gate; the auto classifier's native-only 0.70 route; and resilience to judge and
switch failures.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent.judgment as judgment_pkg
from agent import prewalk
import hermes_cli.config as hermes_config


class _DB:
    def __init__(self) -> None:
        self.store: dict = {}
        self.patches: list = []

    def patch_session_model_config(self, session_id: str, patch: dict) -> None:
        self.patches.append((session_id, patch))
        for key, value in patch.items():
            if value is None:
                self.store.pop(key, None)
            else:
                self.store[key] = value

    def get_session_model_config_value(self, session_id: str, key: str, default=None):
        return self.store.get(key, default)


class _Agent:
    def __init__(self, db: _DB | None = None, tools=("todo_list", "write_file", "patch")) -> None:
        self.model = "primary-model"
        self.provider = "test-provider"
        self.base_url = "https://example.test/v1"
        self.api_key = "test-key"
        self.api_mode = "chat_completions"
        self.reasoning_config = {"enabled": True, "effort": "high"}
        self.session_id = "sess-prewalk"
        self._session_db = db or _DB()
        self.valid_tool_names = set(tools)
        self.switches: list[dict] = []

    def switch_model(self, **kwargs):
        self.switches.append(kwargs)
        self.model = kwargs["new_model"]
        self.provider = kwargs["new_provider"]
        if kwargs.get("base_url"):
            self.base_url = kwargs["base_url"]


def _set_prewalk(monkeypatch, **block) -> None:
    monkeypatch.setattr(hermes_config, "load_config_readonly", lambda: {"prewalk": block})


def _assistant(*names: str):
    calls = [
        SimpleNamespace(id=f"call-{index}", function=SimpleNamespace(name=name))
        for index, name in enumerate(names)
    ]
    return SimpleNamespace(tool_calls=calls)


def _result(name: str, content: str, call_id: str = "call-0") -> dict:
    return {"role": "tool", "name": name, "tool_name": name,
            "tool_call_id": call_id, "content": content}


def _todo_ok(call_id: str = "call-0") -> dict:
    return _result("todo_list", '{"phases": [], "revision": 1}', call_id)


def _write_ok(call_id: str = "call-0") -> dict:
    return _result("write_file", '{"status": "success", "path": "a.py"}', call_id)


def _write_error(call_id: str = "call-0") -> dict:
    return _result("write_file", '{"error": "path required"}', call_id)


def _judge_answer(choice: str, probability: float | None) -> SimpleNamespace:
    probabilities = {} if probability is None else {"prewalk": probability}
    return SimpleNamespace(choice=choice, probabilities=probabilities)


@pytest.fixture
def fake_judge(monkeypatch):
    calls: list[dict] = []
    answers: list[SimpleNamespace] = []

    def _judge(request, **kwargs):
        calls.append({"request": request, **kwargs})
        if not answers:
            raise AssertionError("no fake answer configured")
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(answers={"route": answer})

    monkeypatch.setattr(judgment_pkg, "has_native_judge", lambda **kwargs: True)
    monkeypatch.setattr(judgment_pkg, "judge", _judge)
    monkeypatch.setattr(judgment_pkg, "load_questions", lambda name: {"route": object()})
    return SimpleNamespace(calls=calls, answers=answers)


# ── defaults, explicit arm/disarm/restart ────────────────────────────────────────────────


def test_defaults_are_off_and_advance_never_arms(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent()
    cfg = prewalk.settings()
    assert cfg["enabled"] is False
    assert cfg["auto"] == "off"
    state = prewalk.status(agent)
    assert state["enabled"] is False and state["armed"] is False
    outcome = prewalk.advance_after_tool_batch(agent, _assistant("write_file"), [_write_ok()])
    assert outcome["handoff"] is None
    assert agent.switches == []


def test_explicit_arm_works_while_disabled_and_persists(monkeypatch):
    _set_prewalk(monkeypatch, enabled=False)
    db = _DB()
    agent = _Agent(db)
    state = prewalk.arm(agent, into="fast-model")
    assert state["armed"] is True and state["consumed"] is False
    assert state["planning_model"] == "primary-model"
    assert state["planning_thinking"] == {"enabled": True, "effort": "high"}
    assert agent.switches == []
    assert db.store["prewalk"]["armed"] is True
    assert db.store["prewalk"]["into"] == "fast-model"
    # A fresh agent over the same row inherits the arm.
    assert prewalk.status(_Agent(db))["armed"] is True


def test_unknown_target_is_refused(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent()
    with pytest.raises(ValueError):
        prewalk.arm(agent, into="@no-such-role")
    assert prewalk.status(agent)["armed"] is False


def test_disarm_clears_only_the_arm(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent()
    prewalk.arm(agent, into="fast-model")
    state = prewalk.disarm(agent)
    assert state["armed"] is False
    assert state["consumed"] is False
    assert agent.switches == []


# ── todo gate + one-shot handoff ──────────────────────────────────────────────────────────


def test_todo_gate_closed_until_a_successful_todo_call(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent()
    prewalk.arm(agent, into="fast-model")
    assert prewalk.status(agent)["gate"] == "closed"
    blocked = prewalk.advance_after_tool_batch(agent, _assistant("write_file"), [_write_ok()])
    assert blocked["reason"] == "todo_gate" and blocked["handoff"] is None
    opened = prewalk.advance_after_tool_batch(agent, _assistant("todo_list"), [_todo_ok()])
    assert opened["handoff"] is None
    assert prewalk.status(agent)["gate"] == "open"
    assert agent.switches == []
    fired = prewalk.advance_after_tool_batch(agent, _assistant("write_file"), [_write_ok()])
    assert fired["reason"] == "handoff"
    assert len(agent.switches) == 1
    assert agent.switches[0]["new_model"] == "fast-model"
    state = prewalk.status(agent)
    assert state["consumed"] is True and state["armed"] is False


def test_todo_gate_is_open_without_the_tool(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent(tools=("write_file", "patch"))
    prewalk.arm(agent, into="fast-model")
    assert prewalk.status(agent)["gate"] == "open"
    outcome = prewalk.advance_after_tool_batch(agent, _assistant("write_file"), [_write_ok()])
    assert outcome["handoff"] is not None
    assert len(agent.switches) == 1


def test_no_switch_without_a_successful_edit(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent(tools=("write_file", "patch"))
    prewalk.arm(agent, into="fast-model")
    outcome = prewalk.advance_after_tool_batch(agent, _assistant("write_file"), [_write_error()])
    assert outcome["handoff"] is None and outcome["reason"] == "no_edit"
    assert agent.switches == []
    assert prewalk.status(agent)["armed"] is True


def test_handoff_is_one_shot(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent(tools=("write_file", "patch"))
    prewalk.arm(agent, into="fast-model")
    prewalk.advance_after_tool_batch(agent, _assistant("write_file"), [_write_ok()])
    second = prewalk.advance_after_tool_batch(agent, _assistant("patch"), [_write_ok()])
    assert second["handoff"] is None
    assert len(agent.switches) == 1


def test_patch_also_triggers_handoff(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent(tools=("patch",))
    prewalk.arm(agent, into="fast-model")
    result = _result("patch", '{"status": "success"}')
    outcome = prewalk.advance_after_tool_batch(agent, _assistant("patch"), [result])
    assert outcome["reason"] == "handoff"
    assert len(agent.switches) == 1


def test_todo_and_edit_in_one_batch_hands_off(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent()
    prewalk.arm(agent, into="fast-model")
    outcome = prewalk.advance_after_tool_batch(
        agent, _assistant("todo_list", "write_file"), [_todo_ok(), _write_ok("call-1")])
    assert outcome["reason"] == "handoff"
    assert len(agent.switches) == 1


def test_collect_tool_results_ignores_post_batch_rows(monkeypatch):
    messages = [
        {"role": "assistant", "content": "", "tool_calls": []},
        _write_ok("call-0"),
        # A TTSR / steer row lands after the results; it must not be mistaken for one.
        {"role": "user", "content": "injected reminder"},
        {"role": "tool", "tool_call_id": "other", "name": "read_file", "content": "{}"},
    ]
    results = prewalk.collect_tool_results(messages, _assistant("write_file").tool_calls)
    assert [result["tool_call_id"] for result in results] == ["call-0"]


def test_consumed_arm_is_spent_on_resume(monkeypatch):
    _set_prewalk(monkeypatch)
    db = _DB()
    agent = _Agent(db, tools=("write_file", "patch"))
    prewalk.arm(agent, into="fast-model")
    prewalk.advance_after_tool_batch(agent, _assistant("write_file"), [_write_ok()])
    resumed = _Agent(db, tools=("write_file", "patch"))
    state = prewalk.status(resumed)
    assert state["consumed"] is True and state["armed"] is False
    again = prewalk.advance_after_tool_batch(resumed, _assistant("write_file"), [_write_ok()])
    assert again["handoff"] is None
    assert resumed.switches == []


def test_restart_returns_to_planning_and_rearms(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent(tools=("write_file", "patch"))
    prewalk.arm(agent, into="fast-model")
    prewalk.advance_after_tool_batch(agent, _assistant("write_file"), [_write_ok()])
    assert agent.model == "fast-model"
    state = prewalk.restart(agent)
    assert agent.model == "primary-model"
    assert state["armed"] is True and state["consumed"] is False
    assert state["planning_model"] == "primary-model"


def test_interrupted_batch_does_not_hand_off(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent(tools=("write_file",))
    prewalk.arm(agent, into="fast-model")
    agent._interrupt_requested = True
    outcome = prewalk.advance_after_tool_batch(agent, _assistant("write_file"), [_write_ok()])
    assert outcome["reason"] == "interrupted"
    assert agent.switches == []


def test_switch_failure_does_not_raise(monkeypatch):
    _set_prewalk(monkeypatch)
    agent = _Agent(tools=("write_file",))

    def _boom(**kwargs):
        raise RuntimeError("provider rejected the switch")

    agent.switch_model = _boom
    prewalk.arm(agent, into="fast-model")
    outcome = prewalk.advance_after_tool_batch(agent, _assistant("write_file"), [_write_ok()])
    assert outcome["handoff"] is None and outcome["reason"] == "switch_failed"
    assert prewalk.status(agent)["armed"] is True


# ── auto classifier ─────────────────────────────────────────────────────────────────────


def test_auto_classifier_native_route_and_timeout(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch)
    fake_judge.answers.append(_judge_answer("prewalk", 0.81))
    outcome = prewalk.classify_auto("trace the subsystem before changing it")
    assert outcome["route"] == "prewalk"
    assert outcome["probability"] == pytest.approx(0.81)
    assert fake_judge.calls[0]["purpose"] == "auto-prewalk"
    assert fake_judge.calls[0]["timeout_ms"] == 4000


def test_auto_classifier_requires_native(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch)
    monkeypatch.setattr(judgment_pkg, "has_native_judge", lambda **kwargs: False)
    outcome = prewalk.classify_auto("anything")
    assert outcome["route"] == "" and outcome["reason"] == "no_native_judge"
    assert fake_judge.calls == []


def test_auto_classifier_judge_failure_is_silent(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch)
    fake_judge.answers.append(RuntimeError("judge exploded"))
    outcome = prewalk.classify_auto("anything")
    assert outcome["route"] == "" and outcome["reason"] == "judge_failed"


def test_auto_task_arms_above_threshold_only(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, auto="on", auto_target="fast-model")
    agent = _Agent(tools=("write_file",))
    fake_judge.answers.append(_judge_answer("prewalk", 0.70))
    outcome = prewalk.begin_auto_task(agent, "fix the bug")
    assert outcome["armed"] is True
    state = prewalk.status(agent)
    assert state["armed"] is True and state["auto"] is True
    assert state["into"] == "fast-model"

    below = _Agent(tools=("write_file",))
    fake_judge.answers.append(_judge_answer("prewalk", 0.69))
    assert prewalk.begin_auto_task(below, "fix the bug")["armed"] is False
    assert prewalk.status(below)["armed"] is False


def test_auto_task_never_arms_primary_or_direct(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, auto="on", auto_target="fast-model")
    for route in ("primary", "direct"):
        agent = _Agent(tools=("write_file",))
        fake_judge.answers.append(_judge_answer(route, 0.99))
        outcome = prewalk.begin_auto_task(agent, "do the thing")
        assert outcome["armed"] is False
        assert prewalk.status(agent)["armed"] is False


def test_auto_task_suggest_only_reports(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, auto="suggest", auto_target="fast-model")
    agent = _Agent(tools=("write_file",))
    fake_judge.answers.append(_judge_answer("prewalk", 0.95))
    outcome = prewalk.begin_auto_task(agent, "plan the migration")
    assert outcome["route"] == "prewalk"
    assert outcome["armed"] is False
    assert prewalk.status(agent)["armed"] is False


def test_auto_task_does_not_retarget_an_explicit_arm(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, auto="on", auto_target="fast-model")
    agent = _Agent(tools=("write_file",))
    prewalk.arm(agent, into="explicit-target")
    fake_judge.answers.append(_judge_answer("prewalk", 0.99))
    outcome = prewalk.begin_auto_task(agent, "plan the migration")
    assert outcome["armed"] is False and outcome["reason"] == "explicit_arm"
    assert prewalk.status(agent)["into"] == "explicit-target"


def test_auto_task_judge_failure_does_not_arm(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, auto="on", auto_target="fast-model")
    agent = _Agent(tools=("write_file",))
    fake_judge.answers.append(RuntimeError("judge exploded"))
    outcome = prewalk.begin_auto_task(agent, "plan the migration")
    assert outcome["armed"] is False
    assert prewalk.status(agent)["armed"] is False


def test_auto_task_requires_a_resolvable_target(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, auto="on", auto_target="@smol")
    agent = _Agent(tools=("write_file",))
    fake_judge.answers.append(_judge_answer("prewalk", 0.99))
    outcome = prewalk.begin_auto_task(agent, "plan the migration")
    assert outcome["armed"] is False
    assert outcome["reason"] == "target_unresolved"


def test_auto_task_restores_planning_before_classifying(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, auto="on", auto_target="fast-model")
    db = _DB()
    agent = _Agent(db, tools=("write_file",))
    # Simulate the previous auto task's consumed handoff.
    state = prewalk.arm(agent, into="fast-model", auto=True)
    agent.model = "fast-model"
    state = prewalk._load_state(agent)
    state["consumed"] = True
    state["armed"] = False
    prewalk._persist(agent)
    fake_judge.answers.append(_judge_answer("primary", 0.99))
    outcome = prewalk.begin_auto_task(agent, "keep going on the tricky part")
    assert outcome["armed"] is False
    assert agent.model == "primary-model"
    assert agent.switches[-1]["new_model"] == "primary-model"


# ── config-enabled arming and explicit suppression ───────────────────────────────────────


def test_config_enabled_arms_a_fresh_task_without_judging(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, enabled=True, into="fast-model")
    agent = _Agent(tools=("write_file",))
    outcome = prewalk.begin_auto_task(agent, "implement the plan")
    assert outcome["armed"] is True and outcome["reason"] == "config_arm"
    assert prewalk.status(agent)["armed"] is True
    assert fake_judge.calls == []


def test_explicit_disarm_suppresses_config_rearm(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, enabled=True, into="fast-model")
    agent = _Agent(tools=("write_file",))
    prewalk.arm(agent, into="fast-model")
    prewalk.disarm(agent)
    outcome = prewalk.begin_auto_task(agent, "next task")
    assert outcome["armed"] is False
    assert prewalk.status(agent)["armed"] is False
    assert fake_judge.calls == []


def test_restart_clears_suppression(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, enabled=True, into="fast-model")
    agent = _Agent(tools=("write_file",))
    prewalk.arm(agent, into="fast-model")
    prewalk.disarm(agent)
    assert prewalk.restart(agent)["armed"] is True
    assert getattr(agent, "_prewalk_auto_suppressed", False) is False


def test_off_suppresses_auto_until_restart(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, auto="on", auto_target="fast-model")
    agent = _Agent(tools=("write_file",))
    prewalk.arm(agent, into="fast-model")
    prewalk.disarm(agent)
    fake_judge.answers.append(_judge_answer("prewalk", 0.99))
    outcome = prewalk.begin_auto_task(agent, "plan the migration")
    assert outcome["armed"] is False and outcome["reason"] == "suppressed"
    assert fake_judge.calls == []
    assert prewalk.restart(agent, into="fast-model")["armed"] is True


def test_auto_task_skips_remote_and_headless_surfaces(monkeypatch, fake_judge):
    _set_prewalk(monkeypatch, auto="on", auto_target="fast-model")
    agent = _Agent(tools=("write_file",))
    agent.platform = "telegram"
    assert prewalk.begin_auto_task(agent, "plan")["reason"] == "non_local_surface"

    headless = _Agent(tools=("write_file",))
    headless.quiet_mode = True
    assert prewalk.begin_auto_task(headless, "plan")["reason"] == "non_local_surface"
    assert fake_judge.calls == []
