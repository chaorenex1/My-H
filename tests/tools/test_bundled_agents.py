"""Bundled agent definitions: conversion contract, shipped set and discovery precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_constants import get_bundled_agents_dir
from scripts import build_bundled_agents
from tools import agent_definitions, agent_tool
from toolsets import TOOLSETS

ROOT = Path(__file__).resolve().parents[2]
BUNDLED = ROOT / "skills" / "agent-presets" / "agents"
SHIPPED = {"task", "scout", "reviewer", "security-reviewer", "seance", "sonic"}


def _definition(name: str) -> tuple[dict, str]:
    text = (BUNDLED / f"{name}.md").read_text(encoding="utf-8-sig")
    return agent_definitions.parse_definition(text, source=str(BUNDLED / f"{name}.md"))


def _all_bundled() -> list[Path]:
    return sorted(path for path in BUNDLED.glob("*.md") if path.is_file())


def test_the_converted_set_matches_the_shipped_five_plus_task():
    stems = {path.stem for path in _all_bundled()}

    assert SHIPPED <= stems
    assert stems - SHIPPED == {"init"}, "init is a Hermes-only preset, not an omp snapshot"


@pytest.mark.parametrize("path", _all_bundled(), ids=lambda path: path.stem)
def test_every_bundled_definition_parses_without_omp_leftovers(path):
    text = path.read_text(encoding="utf-8-sig")
    metadata, body = agent_definitions.parse_definition(text, source=str(path))

    assert body.strip()
    assert "agent://" not in body and "proc://" not in body
    assert "{{" not in body and "{%" not in body
    assert set(metadata) <= agent_definitions.KNOWN_FIELDS
    if "enable_tools" in metadata:
        known = {tool for definition in TOOLSETS.values()
                 for tool in definition.get("tools", ())}
        assert set(metadata["enable_tools"]) <= known


def test_scout_frontmatter_is_translated_not_dropped():
    metadata, _body = _definition("scout")

    assert metadata["enable_tools"] == ["read_file", "judgrg", "search_files", "web_search"]
    assert metadata["reasoning_effort"] == "medium"
    assert metadata["read-summarize"] is False


def test_read_only_definitions_never_gain_a_shell():
    for name in ("scout", "security-reviewer", "seance"):
        metadata, _body = _definition(name)
        assert "terminal" not in metadata.get("enable_tools", []), name


def test_reviewer_keeps_its_spawn_allowlist_and_shell():
    metadata, _body = _definition("reviewer")

    assert metadata["spawns"] == ["scout"]
    assert metadata["model"] == "@slow"
    assert "terminal" in metadata["enable_tools"]


def test_task_and_sonic_keep_the_default_toolset():
    for name in ("task", "sonic"):
        metadata, _body = _definition(name)
        assert "enable_tools" not in metadata


def test_discovery_reads_the_converted_bundled_presets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_tool, "get_hermes_home", lambda: tmp_path / "home")
    monkeypatch.setattr(agent_tool, "_install_roots", lambda: (
        ROOT, ROOT / "skills", ROOT / "plugins", BUNDLED))

    rows = agent_tool.discover_agent_definitions(tmp_path)
    names = {row["agent_id"] for row in rows}

    assert SHIPPED | {"init"} <= names
    scout = next(row for row in rows if row["agent_id"] == "scout")
    assert Path(scout["source"]).parent == BUNDLED
    init = next(row for row in rows if row["agent_id"] == "init")
    assert Path(init["source"]).parts[-3:] == ("agent-presets", "agents", "init.md")


def test_the_checked_in_output_is_current():
    assert build_bundled_agents.main(["--check"]) == 0


def test_an_unknown_omp_tool_fails_closed():
    text = "---\nname: x\ntools: read, teleport\n---\nBody.\n"

    with pytest.raises(build_bundled_agents.ConversionError, match="teleport"):
        build_bundled_agents.convert_text(text, source="x.md")


def test_an_unsupported_omp_field_fails_closed():
    text = "---\nname: x\nprewalk: true\n---\nBody.\n"

    with pytest.raises(build_bundled_agents.ConversionError, match="prewalk"):
        build_bundled_agents.convert_text(text, source="x.md")


def test_the_env_override_wins_for_bundled_agents(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_BUNDLED_AGENTS", str(tmp_path / "elsewhere"))

    assert get_bundled_agents_dir(Path("ignored")) == tmp_path / "elsewhere"


def test_the_profile_still_shadows_the_bundled_set(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    (home / "agents" / "scout.md").write_text(
        "---\nname: scout\n---\nYou are the profile scout.\n", encoding="utf-8")
    monkeypatch.setattr(agent_tool, "get_hermes_home", lambda: home)
    monkeypatch.setattr(agent_tool, "_install_roots", lambda: (
        ROOT, ROOT / "skills", ROOT / "plugins", BUNDLED))

    rows = agent_tool.discover_agent_definitions(tmp_path)

    scout = next(row for row in rows if row["agent_id"] == "scout")
    assert scout["source"] == str(home / "agents" / "scout.md")
