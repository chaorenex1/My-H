"""P4.1: ``context_inputs`` persistence and ``agent_out(mode="context_files")``."""

from __future__ import annotations

import json

import pytest
import run_agent

from agent import agent_loop_service
from tools import agent_tool
from tools.registry import registry


class _FakeAgent:
    instances: list[_FakeAgent] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tools = []
        self.prompt = ""
        _FakeAgent.instances.append(self)

    def run_conversation(self, message, system_message=None, conversation_history=None,
                         task_id=None):
        self.prompt = message
        return {"final_response": "ok", "api_calls": 0,
                "messages": [{"role": "assistant", "content": "ok"}]}

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


def _run(project, **args) -> dict:
    raw = registry.dispatch("agent", args, session_id="main")
    return json.loads(raw) if isinstance(raw, str) else raw


def _read(**args) -> dict:
    raw = registry.dispatch("agent_out", args)
    return json.loads(raw) if isinstance(raw, str) else raw


def test_context_files_are_recorded_with_path_size_and_hash(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("hello context", encoding="utf-8")

    run = _run(project, agent_name="researcher", user_prompts="go", background=False,
               project_dir=str(project), context_files=["local://notes.md"])

    payload = _read(mode="context_files", agent_session_id=run["agent_session_id"])
    assert payload["count"] == 1
    entry = payload["context_files"][0]
    assert entry["raw"] == "local://notes.md"
    assert entry["bytes"] == len("hello context")
    assert len(entry["sha256"]) == 64
    assert entry["resolved"].endswith("notes.md")
    assert "content" not in entry


def test_include_content_reads_the_records_current_content(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("hello context", encoding="utf-8")
    run = _run(project, agent_name="researcher", user_prompts="go", background=False,
               project_dir=str(project), context_files=["notes.md"])

    payload = _read(mode="context_files", agent_session_id=run["agent_session_id"],
                    include_content=True)

    entry = payload["context_files"][0]
    assert entry["content"] == "hello context"
    assert entry["unchanged"] is True


def test_a_local_reference_cannot_escape_the_roots(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "secret.md").write_text("do not read", encoding="utf-8")

    run = _run(project, agent_name="researcher", user_prompts="go", background=False,
               project_dir=str(project), context_files=["local://../secret.md"])

    assert "outside the project and profile roots" in run["error"]
    assert _FakeAgent.instances == []


def test_context_inputs_survive_park_and_revive(tmp_path):
    from hermes_state_registry import acquire, release_or_close

    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.md").write_text("durable", encoding="utf-8")
    run = _run(project, agent_name="researcher", user_prompts="go", background=False,
               project_dir=str(project), context_files=["notes.md"])

    db = acquire()
    try:
        db.park_agent_session(run["agent_session_id"])
        db.revive_agent_session(run["agent_session_id"])
    finally:
        release_or_close(db)

    payload = _read(mode="context_files", name=run["name"])
    assert payload["context_files"][0]["sha256"]


def test_include_content_reports_a_file_that_grew_past_the_budget(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    notes = project / "notes.md"
    notes.write_text("small", encoding="utf-8")
    run = _run(project, agent_name="researcher", user_prompts="go", background=False,
               project_dir=str(project), context_files=["notes.md"])
    notes.write_text("x" * 100_001, encoding="utf-8")

    payload = _read(mode="context_files", agent_session_id=run["agent_session_id"],
                    include_content=True)

    entry = payload["context_files"][0]
    assert "larger than" in entry["error"]
    assert "content" not in entry


def test_task_file_accepts_a_local_reference(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "brief.md").write_text("from the brief", encoding="utf-8")

    run = _run(project, agent_name="researcher", task_file="local://brief.md",
               background=False, project_dir=str(project))

    assert run["status"] == "completed"
    assert "from the brief" in _FakeAgent.instances[-1].prompt
