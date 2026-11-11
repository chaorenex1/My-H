"""``agent(spawn=...)``: the ``spawn:@<agent_name>`` shorthand.

The directive is only syntax: authorization (``spawns`` allow-list and ``MAX_DEPTH``) and the
definition lookup stay on the normal path, before any agent session row is written.
"""

from __future__ import annotations

import json

import pytest
import run_agent

from agent import agent_loop_service
from tools import agent_tool
from tools.registry import registry


class _FakeAgent:
    """Records the kwargs a spawn produced and answers with a fixed result."""

    instances: list[_FakeAgent] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tools: list = [
            {"type": "function", "function": {"name": name}} for name in ("read_file",)]
        self.valid_tool_names = {"read_file"}
        self.message = ""
        self.system_message = None
        self.history = None
        self.task_id = None
        self.closed = False
        _FakeAgent.instances.append(self)

    def run_conversation(self, message, system_message=None, conversation_history=None,
                         task_id=None):
        self.message, self.system_message, self.history = (message, system_message,
                                                          conversation_history)
        self.task_id = task_id
        return {
            "final_response": "mapped",
            "api_calls": 1,
            "messages": [{"role": "user", "content": message},
                         {"role": "assistant", "content": "mapped"}],
            "input_tokens": 1,
            "output_tokens": 1,
        }

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_runtime(monkeypatch):
    from agent import agent_run

    _FakeAgent.instances.clear()
    monkeypatch.setattr(
        agent_run, "_resolve_route",
        lambda model, provider, base_url="", api_key="": (
            "fake-model", {"provider": "fake", "api_key": "k"}))
    monkeypatch.setattr(run_agent, "AIAgent", _FakeAgent)
    agent_loop_service.reset_service()
    yield
    agent_loop_service.reset_service()


def _dispatch(**args):
    raw = registry.dispatch("agent", args, session_id="s1")
    return json.loads(raw) if isinstance(raw, str) else raw


def test_the_header_and_body_are_split_at_the_first_line():
    assert agent_tool._parse_spawn_directive(
        "spawn:@reviewer\n\nMap the auth flow.") == ("reviewer", "Map the auth flow.")
    assert agent_tool._parse_spawn_directive(
        "spawn:@reviewer\nMap the auth flow.") == ("reviewer", "Map the auth flow.")
    assert agent_tool._parse_spawn_directive(
        "spawn:@scout.v2\n\n\n  Find it.  ") == ("scout.v2", "Find it.")


@pytest.mark.parametrize("value, needle", [
    ("", "requires 'spawn:@<agent_name>'"),
    ("reviewer\nMap the flow.", "must start with"),
    ("spawn:reviewer\nMap the flow.", "must start with"),
    ("spawn:@\nMap the flow.", "must start with"),
    ("spawn:@reviewer extra\nMap the flow.", "must start with"),
    ("spawn:@reviewer", "needs a task body"),
    ("spawn:@reviewer\n\n   ", "needs a task body"),
    ("spawn:@a/b\nMap the flow.", "must start with"),
    ("spawn:@a b\nMap the flow.", "must start with"),
    ("spawn:@-reviewer\nMap the flow.", "must start with"),
    ("spawn:@" + "a" * 65 + "\nMap the flow.", "must start with"),
])
def test_a_malformed_directive_is_refused(value, needle):
    with pytest.raises(ValueError, match=needle):
        agent_tool._parse_spawn_directive(value)


@pytest.mark.parametrize("conflict", [
    {"agent_name": "reviewer"},
    {"user_prompts": "Map it."},
    {"task_file": "task.md"},
])
def test_spawn_is_exclusive_with_the_explicit_arguments(conflict):
    data = _dispatch(spawn="spawn:@reviewer\n\nMap it.", **conflict)

    assert "spawn replaces" in data["error"]
    assert _FakeAgent.instances == []


def test_spawn_cannot_be_combined_with_a_batch():
    data = _dispatch(spawn="spawn:@reviewer\n\nMap it.",
                     tasks=[{"agent": "reviewer", "user_prompts": "Map it."}])

    assert "cannot be combined with tasks" in data["error"]
    assert _FakeAgent.instances == []


def test_spawn_respects_the_parent_allowlist():
    token = agent_tool._SPAWNS.set(frozenset({"scout"}))
    try:
        with pytest.raises(ValueError, match="not allowed"):
            agent_tool._validate({"spawn": "spawn:@reviewer\n\nMap it."})
        call = agent_tool._validate({"spawn": "spawn:@scout\n\nFind it."})
    finally:
        agent_tool._SPAWNS.reset(token)

    assert call.agent == "scout" and call.user_prompt == "Find it."


def test_spawn_respects_the_recursion_limit():
    token = agent_tool._DEPTH.set(agent_tool.MAX_DEPTH)
    try:
        data = _dispatch(spawn="spawn:@scout\n\nFind it.")
    finally:
        agent_tool._DEPTH.reset(token)

    assert "recursion limit" in data["error"]
    assert _FakeAgent.instances == []


def test_spawn_runs_the_named_agent_with_the_body_as_its_prompt():
    data = _dispatch(spawn="spawn:@spawnprobe\n\nMap the flow.", system_prompts="You probe.",
                     background=False)

    assert data["answer"] == "mapped"
    assert data["subject"] == "Map the flow."
    assert data["agent_session_id"].startswith("spawnprobe-")
    agent = _FakeAgent.instances[-1]
    assert agent.message.endswith("Map the flow.")
    assert agent.system_message.startswith("§ Role\nYou probe.")


def test_a_directive_inside_prompt_text_is_not_executed():
    call = agent_tool._validate({
        "agent_name": "spawnprobe",
        "user_prompts": "spawn:@reviewer\n\nMap the flow.",
        "system_prompts": "You probe.",
    })

    assert call.agent == "spawnprobe", "only the spawn argument is a directive"
    assert call.user_prompt == "spawn:@reviewer\n\nMap the flow."


def test_the_spawn_rollout_switch_refuses_the_shorthand(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"delegation": {"spawn_directive": False}})

    with pytest.raises(ValueError, match="spawn directives are disabled"):
        agent_tool._validate({"spawn": "spawn:@reviewer\n\nMap it."})
    assert _FakeAgent.instances == []


def test_the_spawn_switch_defaults_on_when_config_is_unreadable(monkeypatch):
    def boom():
        raise OSError("no config")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)
    assert agent_tool._spawn_directive_enabled() is True
