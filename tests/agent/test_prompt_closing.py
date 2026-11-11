"""Sections 10–12 — omp-shaped workflow, delivery and critical renders."""

from __future__ import annotations

import pytest

pytest.importorskip("jinja2")

from agent.prompts import renderer

FULL_TOOLS = [
    "agent",
    "agent_out",
    "browser_exec",
    "clarify",
    "code_intel",
    "computer_use",
    "execute_code",
    "judgrg",
    "patch",
    "read_file",
    "search_files",
    "skill_manage",
    "skill_view",
    "skills_list",
    "terminal",
    "todo_list",
    "web_extract",
    "web_search",
    "write_file",
]


def test_workflow_covers_every_phase_on_a_full_coding_surface():
    out = renderer.render_workflow(enable_tools=FULL_TOOLS)

    assert out.startswith("§ Workflow\n# 1. Scope")
    headings = (
        "# 2. Research Before Editing",
        "# 3. Decompose",
        "# 4. Implement",
        "# 5. Verify",
        "# 6. Cleanup",
    )
    assert [out.index(heading) for heading in headings] == sorted(
        out.index(heading) for heading in headings
    )
    assert "`browser_exec`" in out
    assert "`computer_use`" in out
    assert "`todo_list`" in out
    assert "`merge=false`" in out and "`merge=true`" in out
    assert "Ask before destructive commands" in out
    assert "truncated" not in out


def test_workflow_drops_phases_and_tools_the_agent_does_not_have():
    out = renderer.render_workflow(enable_tools=["read_file", "patch"])

    assert "# 3. Decompose" not in out
    assert "todo_list" not in out
    assert "browser_exec" not in out
    assert "computer_use" not in out
    assert "No runtime for the changed surface" in out
    assert "Read relevant rules first." in out
    assert "NEVER run destructive git commands" in out


def test_subagent_closing_hands_verification_to_the_main_agent():
    main = renderer.render_closing(enable_tools=FULL_TOOLS)
    sub = renderer.render_closing(enable_tools=FULL_TOOLS, subagent=True)

    assert "# 5. Verify" in main
    assert "NEVER yield without a smoke run" in main
    assert "# 5. Hand-off" not in main
    assert "# 5. Hand-off" in sub
    assert "# 5. Verify" not in sub
    assert "verification is the main agent's job" in sub
    assert "your slice's callsites" in sub
    assert "Before blocked" not in sub


def test_delivery_keeps_contract_completeness_evidence_and_yielding():
    out = renderer.render_delivery()

    assert out.startswith("§ Delivery\n# Finishing the job")
    tags = ("<contract>", "<completeness>", "<evidence-and-output>", "<yielding>")
    positions = [out.index(tag) for tag in tags]
    assert positions == sorted(positions)
    assert "unobserved = `[INFERENCE]`" in out
    assert "truncated" not in out


def test_critical_names_informed_action_and_the_verify_branch():
    main = renderer.render_critical()
    sub = renderer.render_critical(subagent=True)

    assert "MUST default to informed action" in main
    assert "Before yielding, MUST verify" in main
    assert "Before yielding, MUST verify" not in sub
    assert "verification is the main agent's job" in sub
    assert "truncated" not in main


def test_closing_joins_the_three_sections_in_manifest_order():
    out = renderer.render_closing(enable_tools=FULL_TOOLS)

    assert out.startswith("§ Workflow")
    assert out.index("§ Workflow") < out.index("§ Delivery") < out.index("§ Critical")
    assert "\n\n\n" not in out
    assert not out.endswith("\n")
