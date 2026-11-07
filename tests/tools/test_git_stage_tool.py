"""``git_stage``: schema, toolset contract, judge gating, and handler behavior."""

from __future__ import annotations

import json

import pytest


def test_registered_in_the_judgment_toolset():
    import tools.git_stage_tool as tool  # registers at import
    from tools.registry import registry
    from toolsets import _HERMES_CORE_TOOLS, TOOLSETS

    assert "git_stage" in TOOLSETS["judgment"]["tools"]
    assert "git_stage" in _HERMES_CORE_TOOLS
    entry = registry.get_entry("git_stage")
    assert entry is not None
    assert entry.toolset == "judgment"
    assert entry.check_fn is tool._available
    assert entry.schema["name"] == "git_stage"


def test_schema_is_the_model_facing_surface():
    from tools.git_stage_tool import GIT_STAGE_SCHEMA

    parameters = GIT_STAGE_SCHEMA["parameters"]
    assert parameters["required"] == ["instruction"]
    assert set(parameters["properties"]) == {"instruction", "path", "dry_run"}
    assert parameters["additionalProperties"] is False


def test_check_fn_requires_a_usable_judge(monkeypatch):
    import tools.git_stage_tool as tool

    monkeypatch.setattr("agent.judgment.find.judgment_tools_available", lambda: True)
    assert tool._available() is True

    monkeypatch.setattr("agent.judgment.find.judgment_tools_available", lambda: False)
    assert tool._available() is False

    def broken():
        raise RuntimeError("no routes")

    monkeypatch.setattr("agent.judgment.find.judgment_tools_available", broken)
    assert tool._available() is False


def test_handler_passes_options_and_returns_the_summary(monkeypatch):
    from tools.git_stage_tool import git_stage_tool

    summary = {
        "staged": True, "whole_files": 0, "matched_files": 1, "total_files": 2,
        "matched_hunks": 1, "total_hunks": 3, "skipped": [], "dry_run": True,
    }
    calls = []

    def fake_stage(instruction, **kwargs):
        calls.append((instruction, kwargs))
        return summary

    monkeypatch.setattr("hermes_cli.git_stage.stage", fake_stage)

    payload = json.loads(git_stage_tool({
        "instruction": "  stage the parser fix  ", "path": "src/", "dry_run": True}))

    assert payload == summary
    assert calls == [("stage the parser fix", {"path": "src/", "dry_run": True})]


def test_blank_instruction_never_reaches_git(monkeypatch):
    from tools.git_stage_tool import git_stage_tool

    def fail(*_args, **_kwargs):
        raise AssertionError("stage must not run")

    monkeypatch.setattr("hermes_cli.git_stage.stage", fail)

    payload = json.loads(git_stage_tool({"instruction": "   "}))

    assert "instruction must be a non-empty description" in payload["error"]


@pytest.mark.parametrize("error", [RuntimeError("no judge"), OSError("git missing")])
def test_failures_are_structured_tool_errors(monkeypatch, error):
    from tools.git_stage_tool import git_stage_tool

    def raise_error(*_args, **_kwargs):
        raise error

    monkeypatch.setattr("hermes_cli.git_stage.stage", raise_error)

    payload = json.loads(git_stage_tool({"instruction": "stage the fix"}))

    assert payload == {"error": f"git stage failed: {error}"}
