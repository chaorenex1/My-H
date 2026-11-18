"""The agent-definition contract: parse, validate, merge, and the shipped preset pack."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.tools._omp_agent_definitions import REVIEWER_FRONTMATTER, SCOUT_FRONTMATTER
from tools import agent_definitions
from tools.agent_definitions import DefinitionError
from toolsets import validate_toolset

PRESETS = ("init", "scout", "sonic", "reviewer", "security-reviewer", "seance", "task")

#: omp's ``task``/``sonic`` definitions deliberately grant the default built-in toolset.
DEFAULT_SURFACE = {"task", "sonic"}


def _parse(block: str, body: str = "body\n", source: str = "x.md"):
    return agent_definitions.parse_definition(f"---\n{block}---\n{body}", source=source)


def test_frontmatter_fields_are_typed_and_stripped_from_the_body():
    meta, body = _parse(
        "name: scout\n"
        "description: demo\n"
        "toolsets: [file, web]\n"
        "enable_tools: [read_file, web_search]\n"
        "max_iterations: 42\n"
        "model: provider:model\n"
        "reasoning_effort: high\n"
        "worktree: true\n",
        body="You scout.\n")

    assert body == "You scout."
    assert meta == {"name": "scout", "description": "demo", "toolsets": ["file", "web"],
                    "enable_tools": ["read_file", "web_search"], "max_iterations": 42,
                    "model": "provider:model", "reasoning_effort": "high", "worktree": True}


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
    ("model: \"@\"\n", "needs a role name"),
    ("model: \"@smol extra\"\n", "needs a role name"),
    ('model: "@smol:hgih"\n', "unknown level"),
    ("model: [a, b]\n", "one selector per definition"),
    ("max_iterations: 0\n", "positive integer"),
    ("max_iterations: true\n", "positive integer"),
    ("reasoning_effort: hgih\n", "unknown reasoning_effort"),
    ("enable_tools: 3\n", "must be an array"),
])
def test_invalid_fields_fail_closed(block, needle):
    with pytest.raises(DefinitionError, match=needle):
        _parse(block)


def test_a_role_selector_is_valid_syntax_and_checked_at_call_time():
    meta, _ = _parse('model: "@smol"\n')

    assert meta == {"model": "@smol"}, "the role need not exist yet; resolution is per call"


def test_caller_beats_definition_beats_default():
    resolved = agent_definitions.resolve(
        {"worktree": None, "max_iterations": None, "enable_tools": []},
        {"worktree": True, "max_iterations": 42, "enable_tools": ["read_file"]},
        {"worktree": False, "max_iterations": 10, "enable_tools": []})

    assert resolved == {"worktree": True, "max_iterations": 42, "enable_tools": []}


def test_a_knob_omitted_everywhere_keeps_the_default():
    resolved = agent_definitions.resolve({"max_iterations": None}, {}, {"max_iterations": 10})

    assert resolved == {"max_iterations": 10}


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
def test_a_shipped_definition_is_valid_and_has_a_tool_surface(preset):
    text = (_bundled_pack() / f"{preset}.md").read_text(encoding="utf-8")
    meta, body = agent_definitions.parse_definition(text, source=f"{preset}.md")

    assert body.strip(), "the prompt body would be empty"
    if preset not in DEFAULT_SURFACE:
        assert meta.get("toolsets") or meta.get("enable_tools"), (
            "a preset must pin its tool surface")
    if meta.get("toolsets"):
        assert all(validate_toolset(name) for name in meta["toolsets"])
    if "max_iterations" in meta:
        assert isinstance(meta["max_iterations"], int) and meta["max_iterations"] > 0
    if meta.get("enable_tools"):
        assert not {"write_file", "patch"} & set(meta["enable_tools"]), (
            "an explicit allowlist replaces read_only; mutating tools must not sneak in")
        assert "agents" not in meta["enable_tools"], "an allowlisted preset must not fan out"


def test_init_generates_agents_md_in_the_project():
    text = (_bundled_pack() / "init.md").read_text(encoding="utf-8")
    meta, body = agent_definitions.parse_definition(text, source="init.md")

    assert "agents" in meta["toolsets"], "init fans out its survey through the agent tool"
    assert "enable_tools" not in meta, "init keeps its toolsets; no allowlist replaces them"
    assert meta["worktree"] is False, "the document must land in the project, not a branch"
    assert meta["background"] is False, "inline runs can surface the approval prompt"
    assert "AGENTS.md" in body
    assert "agent(tasks=[" in body and "write_file" in body
    assert "approval" in body, "the protected-write refusal must have a documented fallback"


# -- omp parity: output / spawns / blocking -----------------------------------


def test_an_omp_output_schema_normalizes_to_json_schema():
    meta, _ = _parse(
        "output:\n"
        "  properties:\n"
        "    summary:\n"
        "      metadata:\n"
        "        description: What was found\n"
        "      type: string\n"
        "    files:\n"
        "      elements:\n"
        "        properties:\n"
        "          path:\n"
        "            type: string\n"
        "        optionalProperties:\n"
        "          note:\n"
        "            type: string\n"
        "  optionalProperties:\n"
        "    report:\n"
        "      type: string\n")

    schema = meta["output"]
    assert schema["type"] == "object"
    assert schema["required"] == ["summary", "files"], "optionalProperties stays optional"
    assert schema["properties"]["summary"] == {
        "type": "string", "description": "What was found"}
    files = schema["properties"]["files"]
    assert files["type"] == "array"
    assert files["items"]["required"] == ["path"]
    assert set(files["items"]["properties"]) == {"path", "note"}
    assert "report" not in schema.get("required", [])


def test_a_json_schema_output_passes_through_unchanged():
    meta, _ = _parse(
        "output:\n"
        "  type: object\n"
        "  properties:\n"
        "    verdict:\n"
        "      type: string\n"
        "  required: [verdict]\n")

    assert meta["output"] == {"type": "object", "properties": {"verdict": {"type": "string"}},
                              "required": ["verdict"]}


@pytest.mark.parametrize("block, needle", [
    ("output: []\n", "output must be a non-empty mapping"),
    ("output:\n  properties: nope\n", "properties/optionalProperties must be mappings"),
    ("output:\n  type: not-a-type\n", "not a valid JSON Schema"),
    ("spawns: {a: b}\n", "spawns must be"),
    ('blocking: "yes"\n', "blocking must be a boolean"),
])
def test_invalid_output_spawns_and_blocking_fail_closed(block, needle):
    with pytest.raises(DefinitionError, match=needle):
        _parse(block)


@pytest.mark.parametrize("block, expected", [
    ('spawns: "scout"\n', ["scout"]),
    ('spawns: "scout, reviewer"\n', ["scout", "reviewer"]),
    ("spawns: [scout, reviewer]\n", ["scout", "reviewer"]),
    ('spawns: "*"\n', ["*"]),
    ("spawns: [scout, '*']\n", ["*"]),
    ("spawns: []\n", []),
])
def test_spawns_accepts_star_csv_and_arrays(block, expected):
    meta, _ = _parse(block)

    assert meta["spawns"] == expected


def test_spawns_and_blocking_are_optional():
    meta, _ = _parse("model: '@smol'\n")

    assert "spawns" not in meta and "blocking" not in meta and "output" not in meta


# -- omp parity fixtures (the real bundled front matter) ----------------------


def test_the_omp_scout_frontmatter_parses_to_its_role_and_output_schema():
    meta, body = agent_definitions.parse_definition(
        SCOUT_FRONTMATTER + "\nInvestigate.\n", source="scout.md")

    assert meta["model"] == "@smol"
    assert "spawns" not in meta, "a leaf scout derives nothing"
    assert body == "Investigate."

    schema = meta["output"]
    assert schema["required"] == ["summary", "files", "architecture"]
    assert set(schema["properties"]) == {"summary", "files", "architecture", "report"}
    assert schema["properties"]["summary"]["description"].startswith("Brief summary")
    files = schema["properties"]["files"]
    assert files["type"] == "array"
    assert files["items"]["required"] == ["path", "description"]


def test_the_omp_reviewer_frontmatter_parses_to_its_role_spawns_and_schema():
    meta, _ = agent_definitions.parse_definition(
        REVIEWER_FRONTMATTER + "\nReview.\n", source="reviewer.md")

    assert meta["model"] == "@slow"
    assert meta["spawns"] == ["scout"]

    schema = meta["output"]
    assert schema["required"] == ["overall_correctness", "explanation", "confidence"]
    assert set(schema["properties"]) == {
        "overall_correctness", "explanation", "confidence", "findings"}
    assert schema["properties"]["overall_correctness"]["enum"] == ["correct", "incorrect"]
    findings = schema["properties"]["findings"]
    assert findings["type"] == "array"
    assert set(findings["items"]["properties"]) == {
        "title", "body", "priority", "confidence", "file_path", "line_start", "line_end"}
    assert len(findings["items"]["required"]) == 7


def test_the_omp_scout_read_summarize_is_now_a_known_field():
    meta, _ = agent_definitions.parse_definition(
        SCOUT_FRONTMATTER + "\nInvestigate.\n", source="scout.md")

    assert meta["read-summarize"] is False


def test_ported_omp_agent_fields_parse():
    meta, _ = _parse(
        "structured: true\n"
        "autoloadSkills: [team-rubric, 'category/review']\n"
        "advisor: true\n"
        "read-summarize: false\n")

    assert meta["structured"] is True
    assert meta["autoloadSkills"] == ["team-rubric", "category/review"]
    assert meta["advisor"] is True
    assert meta["read-summarize"] is False


def test_autoload_skills_accepts_csv_and_deduplicates():
    meta, _ = _parse("autoloadSkills: a, b, a\n")

    assert meta["autoloadSkills"] == ["a", "b"]


def test_advisor_accepts_a_model_selector():
    meta, _ = _parse('advisor: "@smol:high"\n')

    assert meta["advisor"] == "@smol:high"


def test_read_summarize_camel_case_is_normalized():
    meta, _ = _parse("readSummarize: false\n")

    assert meta == {"read-summarize": False}


@pytest.mark.parametrize("block, needle", [
    ('structured: "yes"\n', "must be a boolean"),
    ("autoloadSkills: 3\n", "CSV string or an array"),
    ("advisor: 3\n", "boolean or a model selector"),
    ('read-summarize: "no"\n', "must be a boolean"),
])
def test_ported_fields_fail_closed(block, needle):
    with pytest.raises(DefinitionError, match=needle):
        _parse(block)


def test_the_omp_definitions_unknown_keys_only_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="tools.agent_definitions"):
        agent_definitions.parse_definition(SCOUT_FRONTMATTER + "\nbody\n", source="scout.md")
        agent_definitions.parse_definition(REVIEWER_FRONTMATTER + "\nbody\n", source="reviewer.md")

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "unknown field" in messages
    assert "tools" in messages and "thinking-level" in messages
    assert "read-summarize" not in messages
