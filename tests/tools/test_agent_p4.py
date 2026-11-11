"""P4 agent surfaces: short names, registered ``tools``, and serial blocking batches."""

from __future__ import annotations

import json
import re

import pytest
import run_agent

from agent import agent_loop_service
from tools import agent_tool
from tools.registry import registry


class _FakeAgent:
    """Stands in for ``run_agent.AIAgent``: records the prompt and answers deterministically."""

    instances: list[_FakeAgent] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tools = [
            {"type": "function", "function": {"name": name}}
            for name in ("read_file", "write_file", "patch")
        ]
        self.valid_tool_names = {"read_file", "write_file", "patch"}
        self.prompt = ""
        _FakeAgent.instances.append(self)

    def run_conversation(self, message, system_message=None, conversation_history=None,
                         task_id=None):
        self.prompt = message
        return {
            "final_response": "answered",
            "api_calls": 0,
            "messages": [{"role": "user", "content": message},
                         {"role": "assistant", "content": "answered"}],
        }

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fake_route_and_agent(monkeypatch):
    from agent import agent_run

    _FakeAgent.instances.clear()
    monkeypatch.setattr(agent_run, "_resolve_route",
                        lambda model, provider, base_url="", api_key="": (
                            "fake-model", {"provider": "fake", "api_key": "k"}))
    monkeypatch.setattr(run_agent, "AIAgent", _FakeAgent)
    agent_loop_service.reset_service()
    yield
    agent_loop_service.reset_service()


def _call(**args) -> dict:
    raw = registry.dispatch("agent", args, session_id="main")
    return json.loads(raw) if isinstance(raw, str) else raw


def _read(**args) -> dict:
    raw = registry.dispatch("agent_out", args)
    return json.loads(raw) if isinstance(raw, str) else raw


def test_tools_alias_maps_to_a_registered_allowlist_and_filters_the_surface():
    data = _call(agent_name="researcher", user_prompts="go", background=False,
                 tools=["read_file"])

    assert data["all_tools"] is False
    agent = _FakeAgent.instances[-1]
    assert [str((tool.get("function") or {}).get("name"))
            for tool in agent.tools if isinstance(tool, dict)] == ["read_file"]


def test_an_unknown_tool_name_fails_closed_before_any_spawn():
    data = _call(agent_name="researcher", user_prompts="go", background=False,
                 tools=["read_file", "read_file_typo"])

    assert "unknown tool(s): read_file_typo" in data["error"]
    assert _FakeAgent.instances == []


def test_tools_and_enable_tools_cannot_be_combined():
    data = _call(agent_name="researcher", user_prompts="go", background=False,
                 tools=["read_file"], enable_tools=["read_file"])

    assert "either tools or enable_tools" in data["error"]
    assert _FakeAgent.instances == []


def test_short_name_is_unique_and_addresses_the_session():
    first = _call(agent_name="researcher", user_prompts="first", background=False,
                  name="WorkerOne")

    assert first["name"] == "WorkerOne"
    read = _read(name="WorkerOne")
    assert read["agent_session_id"] == first["agent_session_id"]
    assert read["name"] == "WorkerOne"

    duplicate = _call(agent_name="researcher", user_prompts="second", background=False,
                      name="WorkerOne")
    assert "name already in use: WorkerOne" in duplicate["error"]
    assert len(_FakeAgent.instances) == 1


def test_an_omitted_name_is_generated_from_the_agent_name():
    data = _call(agent_name="researcher", user_prompts="go", background=False)

    assert re.fullmatch(r"researcher[0-9a-f]{4}", data["name"]), data["name"]


@pytest.mark.parametrize("name", ["bad-name", "1leading", "x" * 33])
def test_an_invalid_short_name_is_refused(name):
    data = _call(agent_name="researcher", user_prompts="go", background=False, name=name)

    assert "name must be CamelCase" in data["error"]
    assert _FakeAgent.instances == []


def test_a_batch_assigns_per_task_short_names():
    data = _call(background=False, tasks=[
        {"agent": "researcher", "user_prompts": "first", "name": "One"},
        {"agent": "writer", "user_prompts": "second"},
    ])

    assert next(task["name"] for task in data["tasks"]) == "One"
    assert re.fullmatch(r"writer[0-9a-f]{4}", data["tasks"][1]["name"])
    assert {task["subject"] for task in data["tasks"]} == {"first", "second"}


def test_a_batch_with_duplicate_short_names_is_refused_before_any_spawn():
    data = _call(background=False, tasks=[
        {"agent": "researcher", "user_prompts": "first", "name": "Same"},
        {"agent": "writer", "user_prompts": "second", "name": "Same"},
    ])

    assert "duplicate name in this call: Same" in data["error"]
    assert _FakeAgent.instances == []


def test_a_blocking_batch_runs_in_array_order_on_the_calling_thread():
    data = _call(background=False, tasks=[
        {"agent": "researcher", "user_prompts": "first"},
        {"agent": "writer", "user_prompts": "second"},
        {"agent": "researcher", "user_prompts": "third"},
    ])

    assert [task["status"] for task in data["tasks"]] == ["completed"] * 3
    prompts = [agent.prompt for agent in _FakeAgent.instances]
    assert prompts[:3] == ["first", "second", "third"]
