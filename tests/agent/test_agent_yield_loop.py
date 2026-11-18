"""``yield`` through the real conversation loop: terminal break and per-turn reset."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from agent.agent_init import _merge_extra_tools
from tools.agent_yield_tool import YieldState, build_yield_tool


def _tool_call(name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


def _response(content: str = "", finish_reason: str = "stop", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="test/model", usage=None)


@pytest.fixture()
def agent(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda: None)
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        instance = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance.client = MagicMock()
    instance._cached_system_prompt = "You are helpful."
    instance._use_prompt_caching = False
    instance.compression_enabled = False
    instance.save_trajectories = False
    state = YieldState()
    _merge_extra_tools(instance, [
        build_yield_tool(state),
        {"name": "noop", "description": "noop",
         "parameters": {"type": "object", "properties": {}},
         "handler": lambda _args: "ok"},
    ])
    instance._yield_state = state
    return instance


def _run(agent, message: str) -> dict:
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(message)


def test_a_yield_tool_call_ends_the_turn_with_its_data(agent):
    agent.client.chat.completions.create.return_value = _response(
        finish_reason="tool_calls",
        tool_calls=[_tool_call("yield", {"data": {"ok": True}, "final": True})])

    result = _run(agent, "hello")

    assert json.loads(result["final_response"]) == {"ok": True}
    assert agent.client.chat.completions.create.call_count == 1, (
        "a terminal yield must not ask the model for another message")
    assert result["messages"][-1]["content"] == result["final_response"]


def test_the_next_turn_does_not_break_on_the_previous_terminal_yield(agent):
    agent.client.chat.completions.create.return_value = _response(
        finish_reason="tool_calls",
        tool_calls=[_tool_call("yield", {"data": {"first": True}, "final": True})])
    assert json.loads(_run(agent, "hello")["final_response"]) == {"first": True}

    agent.client.chat.completions.create.side_effect = [
        _response(finish_reason="tool_calls", tool_calls=[_tool_call("noop", {})]),
        _response(content="second answer"),
    ]
    before = agent.client.chat.completions.create.call_count
    result = _run(agent, "next")

    assert result["final_response"] == "second answer", (
        "a stale terminal yield would have broken the second turn after the noop batch")
    assert agent.client.chat.completions.create.call_count == before + 2


def test_an_agent_without_yield_keeps_the_legacy_handoff(agent):
    agent._yield_state = None
    agent._extra_tool_handlers.pop("yield", None)
    agent.client.chat.completions.create.return_value = _response(content="legacy answer")

    result = _run(agent, "hello")

    assert result["final_response"] == "legacy answer"
