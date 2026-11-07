"""ADR-17 P5: the auto-thinking consumer (per-user-turn reasoning classifier)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

import agent.judgment
from agent import voice_turn_route
from agent import auto_thinking, conversation_loop
from agent.chat_completion_helpers import _reasoning_config_for_wire
from agent.judgment import ChoiceAnswer, JudgmentResult, JudgmentTimeout
from agent.turn_context import TurnContext


def _agent(**overrides):
    values = {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash",
        "api_mode": "chat_completions",
        "reasoning_config": {"enabled": True, "effort": "medium"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _settings(monkeypatch, **overrides):
    values = {
        "reasoning_auto": "auto",
        "reasoning_auto_max_effort": "xhigh",
        "reasoning_auto_timeout_ms": 4000,
    }
    values.update(overrides)
    monkeypatch.setattr(auto_thinking, "_agent_settings", lambda: values)
    return values


def _result(level: str) -> JudgmentResult:
    return JudgmentResult(
        kind="native", api="typesafe", provider="typesafe", model="jev",
        answers={"level": ChoiceAnswer(level, {}, 1.0)},
    )


def _judge(calls=None, *, level="high", error=None):
    def run(request, *, timeout_ms, budget_usd):
        if calls is not None:
            calls.append({"request": request, "timeout_ms": timeout_ms, "budget_usd": budget_usd})
        if error is not None:
            raise error
        return _result(level)

    return run


def _fix_limits(monkeypatch):
    monkeypatch.setattr(auto_thinking, "_judgment_limits", lambda settings: (4000, None))


def _fail(*_args, **_kwargs):
    raise AssertionError("must not be reached")


def test_off_never_judges(monkeypatch):
    _settings(monkeypatch, reasoning_auto="off")
    monkeypatch.setattr(auto_thinking, "_has_native_judge", _fail)
    monkeypatch.setattr(auto_thinking, "_run_judgment", _fail)

    assert auto_thinking.resolve_auto_effort(_agent(), "repair the flaky parser") is None


def test_auto_refuses_without_a_native_judge(monkeypatch):
    _settings(monkeypatch, reasoning_auto="auto")
    monkeypatch.setattr(auto_thinking, "_has_native_judge", lambda: False)
    monkeypatch.setattr(auto_thinking, "_run_judgment", _fail)

    assert auto_thinking.resolve_auto_effort(_agent(), "repair the flaky parser") is None


def test_on_allows_the_text_judge(monkeypatch):
    _settings(monkeypatch, reasoning_auto="on")
    _fix_limits(monkeypatch)
    monkeypatch.setattr(auto_thinking, "_has_native_judge", lambda: False)
    monkeypatch.setattr(auto_thinking, "_run_judgment", _judge(level="high"))

    assert auto_thinking.resolve_auto_effort(_agent(), "pick an API shape") == {
        "enabled": True, "effort": "high",
    }


def test_purpose_timeout_and_budget_reach_the_judgment_service(monkeypatch):
    _settings(monkeypatch, reasoning_auto="on")
    monkeypatch.setattr(auto_thinking, "_judgment_limits", lambda settings: (1500, 0.02))
    monkeypatch.setattr(auto_thinking, "_has_native_judge", lambda: False)
    captured = {}

    def fake_judge(request, *, purpose, timeout_ms, budget_usd):
        captured.update(purpose=purpose, timeout_ms=timeout_ms, budget_usd=budget_usd)
        return _result("medium")

    monkeypatch.setattr(agent.judgment, "judge", fake_judge)

    assert auto_thinking.resolve_auto_effort(_agent(), "rename a constant") == {
        "enabled": True, "effort": "medium",
    }
    assert captured == {"purpose": "auto-thinking", "timeout_ms": 1500, "budget_usd": 0.02}


def test_timeout_follows_the_auto_setting_capped_by_the_judgment_timeout(monkeypatch):
    from agent.judgment import config as judgment_config

    monkeypatch.setattr(
        judgment_config, "load_judgment_settings",
        lambda: SimpleNamespace(
            timeout_ms=1000, cost_gate=SimpleNamespace(max_usd_per_call=0.02)),
    )

    assert auto_thinking._judgment_limits({"reasoning_auto_timeout_ms": 1500}) == (1000, 0.02)
    assert auto_thinking._judgment_limits({"reasoning_auto_timeout_ms": 400}) == (400, 0.02)
    assert auto_thinking._judgment_limits({}) == (1000, 0.02)
    assert auto_thinking._judgment_limits({"reasoning_auto_timeout_ms": "junk"}) == (1000, 0.02)


def test_max_is_dropped_unless_the_ceiling_allows_it(monkeypatch):
    seen = []

    def capture(request, *, timeout_ms, budget_usd):
        seen.append(request)
        return _result("high")

    _settings(monkeypatch, reasoning_auto="on", reasoning_auto_max_effort="xhigh")
    _fix_limits(monkeypatch)
    monkeypatch.setattr(auto_thinking, "_run_judgment", capture)

    auto_thinking.resolve_auto_effort(_agent(), "repair the flaky parser")
    assert "max" not in seen[-1].questions["level"].criteria

    _settings(monkeypatch, reasoning_auto="on", reasoning_auto_max_effort="max")
    auto_thinking.resolve_auto_effort(_agent(), "repair the flaky parser")
    assert "max" in seen[-1].questions["level"].criteria


def test_answer_is_clamped_to_the_route(monkeypatch):
    _settings(monkeypatch, reasoning_auto="on", reasoning_auto_max_effort="max")
    _fix_limits(monkeypatch)
    monkeypatch.setattr(auto_thinking, "_run_judgment", _judge(level="max"))

    monkeypatch.setattr(
        auto_thinking, "route_supported_efforts",
        lambda provider, model, api_mode=None: ("low", "medium", "high", "xhigh"),
    )
    assert auto_thinking.resolve_auto_effort(_agent(), "open-ended deadlock") == {
        "enabled": True, "effort": "xhigh",
    }

    monkeypatch.setattr(
        auto_thinking, "route_supported_efforts",
        lambda provider, model, api_mode=None: ("low", "medium", "high"),
    )
    assert auto_thinking.resolve_auto_effort(_agent(), "open-ended deadlock") == {
        "enabled": True, "effort": "high",
    }


def test_route_without_any_effort_surface_skips_the_judge(monkeypatch):
    _settings(monkeypatch, reasoning_auto="on")
    _fix_limits(monkeypatch)
    monkeypatch.setattr(auto_thinking, "route_supported_efforts", lambda *args: ())
    monkeypatch.setattr(auto_thinking, "_run_judgment", _fail)

    assert auto_thinking.resolve_auto_effort(_agent(), "anything") is None


def test_blank_request_and_disabled_reasoning_skip_the_judge(monkeypatch):
    _settings(monkeypatch, reasoning_auto="on")
    _fix_limits(monkeypatch)
    monkeypatch.setattr(auto_thinking, "_run_judgment", _fail)

    assert auto_thinking.resolve_auto_effort(_agent(), "   ") is None
    assert auto_thinking.resolve_auto_effort(
        _agent(reasoning_config={"enabled": False}), "anything") is None
    assert auto_thinking.resolve_auto_effort(
        _agent(reasoning_config={"effort": "none"}), "anything") is None


def test_voice_and_codex_app_server_turns_keep_their_own_effort(monkeypatch):
    _settings(monkeypatch, reasoning_auto="on")
    _fix_limits(monkeypatch)
    monkeypatch.setattr(auto_thinking, "_run_judgment", _fail)

    assert auto_thinking.resolve_auto_effort(_agent(_voice_turn_pending=True), "hi") is None
    assert auto_thinking.resolve_auto_effort(
        _agent(api_mode="codex_app_server"), "anything") is None


def test_unusable_answer_falls_back_to_none(monkeypatch):
    _settings(monkeypatch, reasoning_auto="on")
    _fix_limits(monkeypatch)
    monkeypatch.setattr(auto_thinking, "_run_judgment", _judge(level="banana"))

    assert auto_thinking.resolve_auto_effort(_agent(), "anything") is None


def test_judge_exception_falls_back_to_none(monkeypatch):
    _settings(monkeypatch, reasoning_auto="on")
    _fix_limits(monkeypatch)
    monkeypatch.setattr(
        auto_thinking, "_run_judgment", _judge(error=JudgmentTimeout("slow judge")))

    assert auto_thinking.resolve_auto_effort(_agent(), "anything") is None


def test_solution_space_replaces_the_request_state(monkeypatch):
    seen = []
    _settings(monkeypatch, reasoning_auto="on")
    _fix_limits(monkeypatch)
    monkeypatch.setattr(auto_thinking, "_run_judgment", _judge(calls=seen, level="medium"))

    auto_thinking.resolve_auto_effort(
        _agent(), "delegated work", solution_space="unknown deadlock across two services")

    question = seen[-1]["request"].questions["level"]
    assert seen[-1]["request"].state == {"solution_space": "unknown deadlock across two services"}
    assert "subagent's problem" in question.instructions
    # criteria: {} in the bundled file is filled from the ladder's levels.
    assert set(question.criteria) == {"low", "medium", "high", "xhigh"}


def _turn_agent():
    agent = SimpleNamespace(
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_mode="chat_completions",
        reasoning_config={"enabled": True, "effort": "medium"},
        max_iterations=0,
        max_compression_attempts=3,
        iteration_budget=SimpleNamespace(remaining=0),
        _budget_grace_call=False,
    )
    agent._try_refresh_env_client_credentials = lambda: None
    return agent


def _turn_context(user_message: str) -> TurnContext:
    return TurnContext(
        user_message=user_message,
        original_user_message=user_message,
        messages=[],
        conversation_history=None,
        active_system_prompt="system",
        effective_task_id="task",
        turn_id="turn",
        current_turn_user_idx=0,
    )


def _stub_turn(monkeypatch):
    monkeypatch.setattr(
        conversation_loop, "build_turn_context",
        lambda agent, message, *a, **k: _turn_context(message),
    )
    monkeypatch.setattr(conversation_loop, "begin_fast_mode_turn", lambda *a, **k: None)
    monkeypatch.setattr(conversation_loop, "held_stream_transforms", list)
    monkeypatch.setattr(conversation_loop, "play_prelude", lambda *a, **k: ("return", {"ok": True}))
    monkeypatch.setattr(voice_turn_route, "begin_voice_turn_route", lambda agent, messages, prompt: prompt)
    return conversation_loop


def test_classifier_runs_once_per_user_turn(monkeypatch):
    calls = []
    _settings(monkeypatch, reasoning_auto="on")
    _fix_limits(monkeypatch)
    monkeypatch.setattr(auto_thinking, "_run_judgment", _judge(calls=calls, level="high"))
    conversation_loop = _stub_turn(monkeypatch)
    agent = _turn_agent()

    assert conversation_loop._run_conversation_turn(agent, "write a parser", moa_config={}) == {"ok": True}
    assert agent._auto_reasoning_config == {"enabled": True, "effort": "high"}
    assert len(calls) == 1

    # A second turn re-classifies (once) instead of reusing the first turn's overlay.
    assert conversation_loop._run_conversation_turn(agent, "rename it", moa_config={}) == {"ok": True}
    assert len(calls) == 2
    assert calls[-1]["request"].state == {"request": "rename it"}


def test_tool_loop_iterations_do_not_reclassify(monkeypatch):
    calls = []
    _settings(monkeypatch, reasoning_auto="on")
    _fix_limits(monkeypatch)
    monkeypatch.setattr(auto_thinking, "_run_judgment", _judge(calls=calls, level="high"))
    _stub_turn(monkeypatch)

    @dataclass
    class _Verdict:
        action: str = "ok"
        result: Any = None

    @dataclass
    class _IntakeVerdict:
        action: str
        assistant_message: Any
        result: Any = None

    monkeypatch.setattr(conversation_loop, "play_prelude", lambda *a, **k: ("fallthrough", None))
    for name in (
        "begin_iteration", "prepare_iteration", "assemble_api_request",
        "run_preflight_gate", "announce_api_call", "apply_retry_restarts",
    ):
        monkeypatch.setattr(conversation_loop, name, lambda agent: _Verdict())
    monkeypatch.setattr(
        conversation_loop, "normalize_model_response",
        lambda agent: _IntakeVerdict("fallthrough", SimpleNamespace(tool_calls=[])),
    )
    outcomes = iter([_Verdict("continue"), _Verdict("return", {"ok": True})])
    iterations = []

    def finish(agent):
        iterations.append(1)
        return next(outcomes)

    monkeypatch.setattr(conversation_loop, "finish_text_response", finish)
    monkeypatch.setattr(conversation_loop, "_run_api_retry_loop", lambda agent, state: None)

    from scheduler import agent_hooks

    monkeypatch.setattr(agent_hooks, "on_step_pre", lambda agent, step_index=0: None)

    agent = _turn_agent()
    agent.max_iterations = 2
    agent.iteration_budget = SimpleNamespace(remaining=5)
    agent._api_max_retries = 1

    assert conversation_loop._run_conversation_turn(agent, "write a parser", moa_config={}) == {"ok": True}
    assert len(iterations) == 2  # the loop really iterated twice
    assert len(calls) == 1


def test_turn_resets_a_stale_overlay_when_classification_is_off(monkeypatch):
    _settings(monkeypatch, reasoning_auto="off")
    monkeypatch.setattr(auto_thinking, "_run_judgment", _fail)
    conversation_loop = _stub_turn(monkeypatch)
    agent = _turn_agent()
    agent._auto_reasoning_config = {"enabled": True, "effort": "xhigh"}
    agent._auto_reasoning_route = ("openrouter", "deepseek/deepseek-v4-flash", "chat_completions")

    assert conversation_loop._run_conversation_turn(agent, "hello", moa_config={}) == {"ok": True}
    assert agent._auto_reasoning_config is None


def test_explicit_config_wins_without_an_overlay():
    agent = _agent()

    assert _reasoning_config_for_wire(agent) == {"enabled": True, "effort": "medium"}

    agent._auto_reasoning_config = {"enabled": True, "effort": "xhigh"}
    assert _reasoning_config_for_wire(agent) == {"enabled": True, "effort": "xhigh"}


def test_overlay_is_dropped_when_the_route_changes():
    agent = _agent(
        _auto_reasoning_config={"enabled": True, "effort": "xhigh"},
        _auto_reasoning_route=("openrouter", "deepseek/deepseek-v4-flash", "chat_completions"),
    )

    assert _reasoning_config_for_wire(agent) == {"enabled": True, "effort": "xhigh"}

    agent.model = "other/model"
    assert _reasoning_config_for_wire(agent) == {"enabled": True, "effort": "medium"}
    assert agent._auto_reasoning_config is None
