"""Assembler: code-owned order/tiers, one call per block, no gate table."""

from __future__ import annotations

import pytest

pytest.importorskip("jinja2")

from agent.prompts import assembler
from agent.prompts import prompt_sections as ps
from agent.prompts import renderer

ALL_INPUTS = assembler.AssemblyInputs(
    tools=frozenset({"read_file"}),
    subagent=True,
    memory=True,
)
BLOCKS = (
    "render_rfc2119",
    "render_role",
    "render_runtime",
    "render_autoload_skills",
    "render_tool_policy",
    "render_memories",
    "render_closing",
    "render_project_context",
)


def _install(monkeypatch, bodies: dict[str, str] | None = None, calls: dict[str, int] | None = None):
    """Replace every block entry point with a recorded constant-return stub."""
    bodies = {**{name: name for name in BLOCKS}, **(bodies or {})}
    calls = calls if calls is not None else {}

    def stub(name):
        def render(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return bodies.get(name, "")
        return render

    for name in BLOCKS:
        monkeypatch.setattr(renderer, name, stub(name))
    return calls


def _ctx(inputs: assembler.AssemblyInputs | None = None) -> assembler.RenderContext:
    return assembler.RenderContext(inputs=inputs or assembler.AssemblyInputs())


def test_blocks_run_once_and_their_bodies_land_in_code_order(monkeypatch):
    calls = _install(monkeypatch)
    out = assembler.assemble_main(_ctx(ALL_INPUTS))

    assert calls == {name: 1 for name in BLOCKS}
    assert out == (
        "render_rfc2119\n\nrender_role\n\nrender_runtime\n\nrender_autoload_skills"
        "\n\nrender_tool_policy\n\nrender_memories\n\nrender_closing"
        "\n\nrender_project_context"
    )


def test_blocks_receive_the_caller_facts(monkeypatch):
    seen: dict[str, dict] = {}

    def capture(name, body):
        def render(*args, **kwargs):
            seen[name] = {"args": args, "kwargs": kwargs}
            return body
        return render

    for name in BLOCKS:
        monkeypatch.setattr(renderer, name, capture(name, name))

    ctx = assembler.RenderContext(
        inputs=ALL_INPUTS,
        facts=assembler.SessionFacts(
            platform="cli",
            provider="openai",
            model="gpt-5",
            context_cwd="/repo",
            agent_definition={"role": "Hermes"},
            auto_load_skills="PINNED SKILL BODY",
            profile="coder",
            user_profile_enabled=False,
        ),
        agent_name="peer",
        first_input="how do I update Hermes?",
    )
    assembler.assemble_main(ctx)

    assert seen["render_role"]["args"] == ("cli",)
    assert seen["render_role"]["kwargs"]["identity"] == {"role": "Hermes"}
    assert seen["render_role"]["kwargs"]["tool_names"] == ("read_file",)
    assert seen["render_closing"]["kwargs"]["subagent"] is True
    assert seen["render_closing"]["kwargs"]["project_dir"] == "/repo"
    assert "coding_repo" not in seen["render_closing"]["kwargs"]
    assert seen["render_closing"]["kwargs"]["enable_tools"] == ["read_file"]
    assert seen["render_runtime"]["kwargs"]["project_dir"] == "/repo"
    assert seen["render_runtime"]["kwargs"]["first_input"] == "how do I update Hermes?"
    assert "coding_repo" not in seen["render_runtime"]["kwargs"]
    assert seen["render_role"]["kwargs"]["profile"] == "coder"
    assert seen["render_autoload_skills"]["kwargs"]["autoload_skills"] == "PINNED SKILL BODY"
    assert seen["render_memories"]["kwargs"]["memory_enabled"] is True
    assert seen["render_memories"]["kwargs"]["user_profile_enabled"] is False
    assert seen["render_memories"]["kwargs"]["agent_name"] == "peer"
    assert seen["render_tool_policy"]["kwargs"]["enable_tools"] == ["read_file"]


def test_empty_block_bodies_drop_out(monkeypatch):
    _install(monkeypatch, bodies={
        "render_role": "  \n",
        "render_tool_policy": "",
        "render_project_context": "",
        "render_memories": "",
    })
    out = assembler.assemble_main(_ctx(ALL_INPUTS))

    assert out == "render_rfc2119\n\nrender_runtime\n\nrender_autoload_skills\n\nrender_closing"
    assert "\n\n\n" not in out


def test_assemble_subagent_appends_the_role_block_after_the_base(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(
        renderer, "render_subagent_prompt",
        lambda role: f"§ Subagent Role\n{role.body}",
    )
    out = assembler.assemble_subagent(
        _ctx(ALL_INPUTS), assembler.SubagentRole(body="Inspect the repo.")
    )
    assert out.startswith("render_rfc2119\n\nrender_role")
    assert out.endswith("\n\n§ Subagent Role\nInspect the repo.")


def test_assemble_subagent_rejects_an_empty_role_body(monkeypatch):
    _install(monkeypatch)
    with pytest.raises(ps.PromptSectionError, match="role body"):
        assembler.assemble_subagent(_ctx(), assembler.SubagentRole(body="  "))


def test_subagent_siblings_share_the_base_and_keep_the_role_block_last(monkeypatch):
    _install(monkeypatch)
    ctx = _ctx(ALL_INPUTS)
    base = assembler.assemble_main(ctx)
    first = assembler.assemble_subagent(ctx, assembler.SubagentRole(body="Review the diff."))
    second = assembler.assemble_subagent(ctx, assembler.SubagentRole(body="Fix the test."))

    assert first.startswith(base + "\n\n")
    assert second.startswith(base + "\n\n")
    assert first[len(base) + 2:].startswith("§ Subagent Role\nReview the diff.")
    assert first.count("§ Subagent Role") == 1
    assert second[len(base) + 2:].startswith("§ Subagent Role\nFix the test.")


def test_request_templates_never_enter_the_assembled_prompt(monkeypatch):
    _install(monkeypatch)
    out = assembler.assemble_main(_ctx(ALL_INPUTS))

    assert "§ Subagent Role" not in out
    assert "<system-reminder>" not in out
    assert "<irc>" not in out
