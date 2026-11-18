"""Section 09 — capability-gated tool policy."""

from __future__ import annotations

import pytest

pytest.importorskip("jinja2")

from agent.prompts import renderer
from agent.prompts.toolsets import PM_DEFAULT_TOOLSETS

FULL_TOOLS = [
    "read_file",
    "patch",
    "write_file",
    "terminal",
    "code_intel",
    "judgrg",
    "agent",
    "agent_out",
    "agent_message",
    "execute_code",
]


def _policy(**kwargs) -> str:
    return renderer.render_tool_policy(enable_tools=FULL_TOOLS, max_concurrency=10, **kwargs)


def test_pm_default_toolsets_carry_rg_and_astgrep_purposes():
    assert list(PM_DEFAULT_TOOLSETS) == ["rg", "ast-grep"]
    assert "text search" in PM_DEFAULT_TOOLSETS["rg"]
    assert "glob" in PM_DEFAULT_TOOLSETS["rg"]
    assert "structural" in PM_DEFAULT_TOOLSETS["ast-grep"]
    assert all(purpose.strip() for purpose in PM_DEFAULT_TOOLSETS.values())


def test_tool_policy_has_no_tool_inventory_and_prioritizes_specialized_tools():
    out = _policy()

    assert out.startswith("§ Tool Policy\n\n# General")
    assert "A specialized tool comes first" in out
    assert "MUST use the specialized tool over a shell equivalent" in out
    assert "available_tools" not in out
    assert "<tool" not in out


def test_search_policy_allows_rg_but_never_shell_grep_find_or_ls():
    out = _policy()

    assert "`rg` is the preferred specialized search" in out
    assert "`rg --files` is the glob equivalent" in out
    assert "NEVER use shell `grep`, `find`, `ls`, `fd`, or `awk`" in out
    assert "Unknown behavior or location: descriptive `judgrg` FIRST" in out


def test_judgrg_ranges_are_optional_read_offsets():
    out = _policy()

    assert "`judgrg` hits carry `ranges`" in out
    assert "`start` as `read_file.offset`" in out
    assert "`end - start + 1` as `limit`" in out
    assert "this is optional" in out


def test_edit_policy_uses_patch_and_write_file_and_bans_sed_perl_python():
    out = _policy()

    assert "Local change: `patch`" in out
    assert "whole-file replacement: `write_file`" in out
    assert "NEVER use `sed`, `perl`, or `python`" in out


def test_ast_policy_collects_selection_usage_and_safety():
    out = _policy()

    assert "# AST" in out
    assert "`ast-grep` is the only approved structural editor" in out
    assert "belongs to `ast-grep`" in out
    assert "Verify the rewritten syntax" in out


def test_symbols_policy_uses_code_intel_as_authority_not_a_text_search_fallback():
    out = _policy()

    assert "# Symbols and Graph" in out
    assert "questions MUST start with `code_intel`" in out
    assert "MUST consume `code_intel` output" in out
    assert "NOT a fallback when graph data is incomplete or stale" in out


def test_agent_batches_and_delegate_task_is_control_only():
    out = _policy()

    assert "pass `tasks[]`" in out
    assert "`delegate_task` remains only for its live `action=list/steer/stop`" in out
    assert "fully supersedes" not in out


def test_delegation_irc_guidance_renders_with_agent_message():
    out = _policy()

    assert "IRC:" in out
    assert "`agent_message`" in out
    assert "A small missing detail is not a blocker" in out


def test_default_policy_matches_the_registered_config_defaults():
    from agent.prompts.delegation import DEFAULT_POLICY
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    raw = DEFAULT_CONFIG["delegation"]
    assert DEFAULT_POLICY["delegation_bias"] == raw["bias"]
    assert DEFAULT_POLICY["eager_tasks"] == raw["eager_tasks"]
    assert DEFAULT_POLICY["eager_tasks_always"] == raw["eager_tasks_always"]
    assert DEFAULT_POLICY["inline_first"] == raw["inline_first"]


def test_delegation_bias_gated_blocks_subagents_without_an_explicit_request():
    out = _policy(delegation_bias="gated")

    assert "No subagents unless the user or an applicable AGENTS.md/skill explicitly requests" in out
    assert "Proactive multi-agent delegation is active" not in out


def test_delegation_bias_gated_with_eager_tasks_allows_proactive_agents():
    out = _policy(delegation_bias="gated", eager_tasks=True)

    assert "Proactive multi-agent delegation is active" in out
    assert "No subagents unless" not in out


def test_delegation_bias_eager_switches_between_preferred_and_default():
    preferred = _policy(eager_tasks=True)
    default = _policy(eager_tasks=True, eager_tasks_always=True)

    assert "Delegation preferred" in preferred
    assert "Delegation default" not in preferred
    assert "Delegation default" in default
    assert "MUST fan work to `agent`" in default


def test_inline_first_is_the_default_and_can_be_disabled():
    inline = _policy()
    mapped = _policy(inline_first=False)

    assert "Inline first." in inline
    assert "NEVER babysit." in inline
    assert "Inline first." not in mapped
    assert "Map unknown code via `agent`" in mapped


def test_delegation_gates_include_concurrency_only_when_capped():
    capped = _policy()
    uncapped = renderer.render_tool_policy(enable_tools=FULL_TOOLS)

    assert "Max 10 concurrent subagents" in capped
    assert "Max 0 concurrent" not in uncapped
    assert "Shared prerequisites go inline" in uncapped


def test_tool_face_gates_sections_and_pm_shell_tools():
    read_only = renderer.render_tool_policy(enable_tools=["read_file"])
    shell = renderer.render_tool_policy(enable_tools=["terminal", "read_file"])

    assert "# Delegation" not in read_only
    assert "# Symbols and Graph" not in read_only
    assert "# Edit" not in read_only
    assert "execute_code" not in read_only
    assert "`rg`" not in read_only
    assert "`ast-grep`" not in read_only

    assert "`rg`" in shell
    assert "`ast-grep`" in shell
    assert "# Delegation" not in shell


def test_tool_policy_has_no_completion_section():
    assert "# Completion" not in _policy()


@pytest.mark.parametrize("bias", ["always", "", "EAGER"])
def test_unknown_delegation_bias_is_rejected(bias):
    with pytest.raises(ValueError):
        renderer.render_tool_policy(enable_tools=FULL_TOOLS, delegation_bias=bias)


@pytest.mark.parametrize("cap", [-1, True, "3"])
def test_invalid_concurrency_is_rejected(cap):
    with pytest.raises(ValueError):
        renderer.render_tool_policy(enable_tools=FULL_TOOLS, max_concurrency=cap)


def test_delegation_policy_resolves_config_then_definition():
    from agent.prompts.delegation import resolve_delegation_policy

    config = {
        "delegation": {"bias": "gated", "eager_tasks": True, "inline_first": False},
        "agent_loop": {"max_workers": 5},
    }
    policy = resolve_delegation_policy(config)
    assert policy == {
        "delegation_bias": "gated",
        "eager_tasks": True,
        "eager_tasks_always": False,
        "inline_first": False,
        "max_concurrency": 5,
    }

    overridden = resolve_delegation_policy(
        config, definition={"delegation_bias": "eager", "eager_tasks_always": True})
    assert overridden["delegation_bias"] == "eager"
    assert overridden["eager_tasks_always"] is True


def test_render_tool_policy_from_config_uses_the_resolved_switches():
    out = renderer.render_tool_policy_from_config(
        enable_tools=FULL_TOOLS,
        config={
            "delegation": {"bias": "gated", "eager_tasks": False},
            "agent_loop": {"max_workers": 5},
        },
    )

    assert "No subagents unless" in out
    assert "Max 5 concurrent subagents" in out


def test_delegation_policy_rejects_an_unknown_bias():
    from agent.prompts.delegation import resolve_delegation_policy

    with pytest.raises(ValueError):
        resolve_delegation_policy({"delegation": {"bias": "always"}})


def test_workflow_handoff_carries_yield_and_irc():
    out = renderer.render_workflow(
        enable_tools=["agent", "agent_message"], subagent=True)

    assert "Changes complete → yield" in out
    assert "`agent_message`" in out


def test_workflow_and_delivery_carry_yield_and_irc_for_subagents():
    delivery = renderer.render_delivery(subagent=True)
    workflow = renderer.render_workflow(
        enable_tools=["agent", "agent_message"], subagent=True)

    assert "Yield the slice result" in delivery
    assert "`agent_message`" in delivery
    assert "Changes complete → yield" in workflow
    assert "peer waiting on an answer" in workflow
