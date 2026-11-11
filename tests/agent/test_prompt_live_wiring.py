"""Per-agent front matter must reach the prompt-policy resolver (section 09).

The resolver already accepts a definition; these tests pin the two writers that were missing:
the child ``agent_run`` construction and the resident park/revive rebuild.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from agent import agent_run
from agent import agent_session_runtime as session_runtime
from tools import agent_tool


class _FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run_conversation(self, prompt, **kwargs):
        return {"final_response": "ok", "messages": [], "usage": {}}

    def close(self):
        pass


def test_child_run_attaches_the_definition_for_prompt_policy(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(agent_run, "_resolve_route",
                        lambda *_args, **_kwargs: ("fake-model",
                                                   {"provider": "fake", "api_key": "k"}))

    built = {}
    metadata = {"delegation_bias": "gated", "eager_tasks": True}
    agent_run.run(
        "task",
        agent_name="reviewer",
        agent_session_id="reviewer-test",
        definition_metadata=metadata,
        record_history=False,
        on_agent=lambda child: built.setdefault("agent", child),
    )

    assert built["agent"]._agent_definition_metadata == metadata


def test_resident_revive_preserves_the_definition_policy(monkeypatch):
    metadata = {"delegation_bias": "gated", "eager_tasks": True}

    def _no_definition(*args, **kwargs):
        raise FileNotFoundError("definition re-read unavailable in this test")

    monkeypatch.setattr(agent_tool, "_system_prompt", _no_definition)
    call = SimpleNamespace(
        model_selector="", model_role="", model_name="", provider_name="", toolsets=[],
        all_tools=False, flags={}, enable_tools=[], max_iterations=1, reasoning_effort="",
        project=None, skill_name="", output_schema=None, schema_mode="permissive",
        autoload_skills=(), advisor=None, read_summarize=None, worktree=False, spawns=None,
        definition=SimpleNamespace(body="", source="test", metadata=metadata))
    spec = agent_tool._resident_spec(call, depth=0)
    session = SimpleNamespace(
        spec=spec, agent_name="reviewer", agent_session_id="reviewer-test",
        parent_session_id="parent", transcript_path="")

    params = session_runtime._revive_params(session)

    assert params["definition_metadata"] == metadata
