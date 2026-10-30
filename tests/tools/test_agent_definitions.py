"""The agent-definition contract: parse, validate, merge, and the shipped preset pack."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tools import agent_definitions
from tools.agent_definitions import DefinitionError
from toolsets import validate_toolset

PRESETS = ("init", "scout", "sonic", "reviewer", "security-reviewer", "seance")


def _parse(block: str, body: str = "body\n", source: str = "x.md"):
    return agent_definitions.parse_definition(f"---\n{block}---\n{body}", source=source)


def test_frontmatter_fields_are_typed_and_stripped_from_the_body():
    meta, body = _parse(
        "name: scout\n"
        "description: demo\n"
        "toolsets: [file, web]\n"
        "read_only: true\n"
        "max_iterations: 42\n"
        "model: provider:model\n"
        "reasoning_effort: high\n"
        "worktree: true\n",
        body="You scout.\n")

    assert body == "You scout."
    assert meta == {"name": "scout", "description": "demo", "toolsets": ["file", "web"],
                    "read_only": True, "max_iterations": 42, "model": "provider:model",
                    "reasoning_effort": "high", "worktree": True}


def test_a_body_without_frontmatter_has_no_metadata():
    meta, body = agent_definitions.parse_definition("You scout.\n", source="scout.md")

    assert meta == {} and body == "You scout."


def test_unknown_fields_are_ignored_with_one_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="tools.agent_definitions"):
        meta, _ = _parse("name: x\ntools: [file]\nkeep_going: yes\n")

    assert meta == {"name": "x"}
    assert sum("unknown field" in record.getMessage() for record in caplog.records) == 1


@pytest.mark.parametrize("block, needle", [
    ("toolsets: [nope]\n", "unknown toolset"),
    ("toolsets: []\n", "non-empty list"),
    ("model: provider:\n", "provider:model"),
    ("max_iterations: 0\n", "positive integer"),
    ("max_iterations: true\n", "positive integer"),
    ("reasoning_effort: hgih\n", "unknown reasoning_effort"),
    ('read_only: "sometimes"\n', "must be a boolean"),
])
def test_invalid_fields_fail_closed(block, needle):
    with pytest.raises(DefinitionError, match=needle):
        _parse(block)


def test_caller_beats_definition_beats_default():
    resolved = agent_definitions.resolve(
        {"read_only": False, "worktree": None, "max_iterations": None},
        {"read_only": True, "worktree": True, "max_iterations": 42},
        {"read_only": False, "worktree": False, "max_iterations": 10})

    assert resolved == {"read_only": False, "worktree": True, "max_iterations": 42}


def test_a_knob_omitted_everywhere_keeps_the_default():
    resolved = agent_definitions.resolve({"read_only": None}, {}, {"read_only": False})

    assert resolved == {"read_only": False}


def _bundled_pack() -> Path:
    from tools.agent_tool import _install_roots

    return _install_roots()[1] / "agent-presets" / "agents"


def test_the_bundled_pack_exposes_every_definition():
    pack = _bundled_pack()

    assert pack.is_dir(), f"bundled preset pack missing: {pack}"
    assert {path.stem for path in pack.glob("*.md")} == set(PRESETS)


def test_the_pack_is_found_by_the_one_level_skill_scan():
    from tools.agent_tool import _prompt_dirs

    assert _bundled_pack() in _prompt_dirs(None, "agent-presets")


@pytest.mark.parametrize("preset", PRESETS)
def test_a_shipped_definition_is_valid_and_read_only_safe(preset):
    text = (_bundled_pack() / f"{preset}.md").read_text(encoding="utf-8")
    meta, body = agent_definitions.parse_definition(text, source=f"{preset}.md")

    assert body.strip(), "the prompt body would be empty"
    assert meta.get("toolsets"), "a preset must pin its tool surface"
    assert all(validate_toolset(name) for name in meta["toolsets"])
    assert isinstance(meta.get("max_iterations"), int) and meta["max_iterations"] > 0
    if meta.get("read_only"):
        assert "agents" not in meta["toolsets"], "a read-only preset must not fan out"


def test_init_generates_agents_md_in_the_project():
    text = (_bundled_pack() / "init.md").read_text(encoding="utf-8")
    meta, body = agent_definitions.parse_definition(text, source="init.md")

    assert "delegation" in meta["toolsets"], "init fans out its survey through delegate_task"
    assert meta["read_only"] is False
    assert meta["worktree"] is False, "the document must land in the project, not a branch"
    assert meta["background"] is False, "inline runs can surface the approval prompt"
    assert "AGENTS.md" in body
    assert "delegate_task" in body and "write_file" in body
    assert "approval" in body, "the protected-write refusal must have a documented fallback"
