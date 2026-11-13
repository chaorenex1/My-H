"""``<project-context>`` tail block: workstation, discovery, dedupe and injection."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("jinja2")

from agent.prompts import project_context as pc


def _file(kind: str, label: str, path: Path, content: str) -> pc.ContextFile:
    return pc.ContextFile(kind=kind, label=label, path=path, content=content)


def test_workstation_reports_the_os_architecture_and_runtime_rows():
    rows = pc.workstation_environment()
    labels = [row.split(":", 1)[0] for row in rows]
    assert labels[:2] == ["OS", "Arch"] or labels[:2] == ["OS"]
    assert any(label == "User home directory" for label in labels)
    assert any(label == "Current working directory" for label in labels)


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openrouter", "deepseek-chat", "openrouter/deepseek-chat"),
        ("", "deepseek-chat", "deepseek-chat"),
        ("anthropic", "anthropic/claude-sonnet-4.5", "anthropic/claude-sonnet-4.5"),
        ("openrouter", "vendor/model", "openrouter/vendor/model"),
        ("openrouter", "", ""),
    ],
)
def test_model_label_does_not_double_the_provider(provider, model, expected):
    assert pc.model_label(provider, model) == expected


def test_split_comparable_blocks_normalizes_paragraphs_and_skips_fenced_code():
    content = "Use Ruff.\n\n```\nnot a paragraph\n```\n\nRun tests."
    assert pc.split_comparable_blocks(content) == ["Use Ruff.", "Run tests."]


def test_blocks_contain_accepts_a_subsequence_not_only_a_prefix():
    assert pc.blocks_contain(["a", "b", "c"], ["a", "c"])
    assert not pc.blocks_contain(["a", "c"], ["a", "b", "c"])


def test_dedupe_drops_a_far_file_fully_repeated_in_a_near_file(tmp_path):
    root = _file("agents_md", "..\\AGENTS.md", tmp_path / "AGENTS.md",
                 "Always run tests.\n\nUse Ruff.")
    near = _file("agents_md", "AGENTS.md", tmp_path / "sub" / "AGENTS.md",
                 "Always run tests.\n\nUse Ruff.\n\nWebapp: React only.")
    kept = pc.dedupe_contained_context_files([root, near])
    assert [file.label for file in kept] == ["AGENTS.md"]


def test_dedupe_keeps_a_rewritten_paragraph(tmp_path):
    root = _file("agents_md", "..\\AGENTS.md", tmp_path / "AGENTS.md", "Always run tests.")
    near = _file("agents_md", "AGENTS.md", tmp_path / "sub" / "AGENTS.md",
                 "Always run the full test suite before pushing.")
    assert len(pc.dedupe_contained_context_files([root, near])) == 2


def test_dedupe_ignores_differences_inside_fenced_code(tmp_path):
    root = _file("agents_md", "..\\AGENTS.md", tmp_path / "AGENTS.md",
                 "Intro.\n\n```\nmake lint\n```")
    near = _file("agents_md", "AGENTS.md", tmp_path / "sub" / "AGENTS.md",
                 "Intro.\n\n```\nmake check\n```")
    assert len(pc.dedupe_contained_context_files([root, near])) == 1


def test_select_context_files_loads_only_the_highest_priority_type(tmp_path):
    (tmp_path / ".hermes.md").write_text("Hermes-first context.")
    (tmp_path / "AGENTS.md").write_text("Agent context.")
    files = pc.select_context_files(tmp_path)
    assert [file.kind for file in files] == ["hermes_md"]
    assert files[0].content == "Hermes-first context."


def test_select_context_files_injects_agents_chain_far_to_near(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("Root rule.")
    sub = tmp_path / "packages"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("Packages rule.")
    files = pc.select_context_files(sub)
    assert [file.content for file in files] == ["Root rule.", "Packages rule."]


def test_render_project_context_renders_workstation_and_repo_rules():
    file = _file("agents_md", "AGENTS.md", Path("/repo/AGENTS.md"), "Use pytest.")
    block = pc.render_project_context(provider="openrouter", model="deepseek-chat",
                                      context_files=[file], dir_context_files=["src/AGENTS.md"])
    assert block.startswith("<project-context>\n<workstation>\n- OS: ")
    assert "- Model: deepseek-chat" in block
    assert "- Provider: openrouter" in block
    assert "- Platform: " not in block
    assert "<repo-rules>\nMUST follow these context files for all tasks:" in block
    assert f'<file path="{file.label}">\nUse pytest.\n</file>' in block
    assert "Before changes in these directories, MUST read:\n- src/AGENTS.md" in block
    assert block.count("<project-context>") == 1
    assert "</project-context>" in block


def test_hermes_md_frontmatter_is_not_injected(tmp_path):
    (tmp_path / ".hermes.md").write_text("---\ntitle: atlas\n---\nBody rule.")
    files = pc.select_context_files(tmp_path)
    assert files[0].content.strip() == "Body rule."


def test_agents_chain_shares_one_merged_cap(tmp_path, monkeypatch):
    from agent import prompt_builder as pb

    monkeypatch.setattr(pb, "_get_context_file_max_chars", lambda *_args: 120)
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("R" * 100)
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("S" * 100)
    block = pc.render_project_context(provider="p", model="m", cwd=str(sub))
    assert "AGENTS.md (directory chain)" in block
    assert block.count("<file path=") == 1


def test_cursorrules_share_one_merged_cap(tmp_path, monkeypatch):
    from agent import prompt_builder as pb

    monkeypatch.setattr(pb, "_get_context_file_max_chars", lambda *_args: 120)
    (tmp_path / ".cursorrules").write_text("C" * 100)
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "a.mdc").write_text("M" * 100)
    block = pc.render_project_context(provider="p", model="m", cwd=str(tmp_path))
    assert ".cursorrules" in block
    assert block.count("<file path=") == 1


def test_safe_worker_suppresses_project_files_but_keeps_the_workstation(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("rule")
    from agent import safe_worker_policy

    monkeypatch.setattr(safe_worker_policy, "safe_worker_enabled", lambda: True)
    block = pc.render_project_context(provider="p", model="m", cwd=str(tmp_path))
    assert "<repo-rules>" not in block
    assert "<workstation>" in block


def test_render_project_context_renders_workspace_and_active_repo_blocks():
    block = pc.render_project_context(
        provider="p", model="m", context_files=[],
        include_workspace_tree=True, workspace_tree="src/\n  main.py", workspace_tree_truncated=True,
        workspace_roots=["/other"], active_repo_context="<attention>one repo</attention>",
    )
    assert "<workspace-tree>\nWorking-directory layout: newest mtime first; depth ≤ 3.\nsrc/\n  main.py" in block
    assert "Some entries elided to shorten tree" in block
    assert "<workspace-roots>" in block and "- /other" in block
    assert "Manage with `/add-dir` and `/remove-dir`; `/dirs` lists them." in block
    assert "<attention>one repo</attention>" in block


def test_render_project_context_without_files_keeps_the_workstation_block():
    block = pc.render_project_context(provider="p", model="m", context_files=[])
    assert "<repo-rules>" not in block
    assert "Context files above auto-loaded" not in block
    assert "</project-context>" in block
    assert block.rstrip().endswith("</critical>")


def test_critical_tail_switches_on_the_subagent_rule():
    main = pc.render_project_context(provider="p", model="m", context_files=[])
    sub = pc.render_project_context(provider="p", model="m", context_files=[], subagent=True)
    assert "Before yielding, MUST verify" in main
    assert "verification is main agent's job" not in main
    assert "verification is main agent's job" in sub
    assert "Before yielding, MUST verify" not in sub


def test_append_prompt_lands_after_the_critical_tail():
    block = pc.render_project_context(provider="p", model="m", context_files=[],
                                      append_prompt="Extra rule.")
    assert block.rstrip().endswith("Extra rule.")


def test_assembled_prompt_carries_the_project_context_tail(tmp_path, monkeypatch):
    from agent import system_prompt
    from agent.prompts import renderer

    (tmp_path / "AGENTS.md").write_text("Use pytest.")
    agent = SimpleNamespace(skip_context_files=False, platform="cli", provider="openrouter",
                            model="deepseek-chat", valid_tool_names=set(),
                            _bot_mode_protocol=True, _session_title_hint="", _session_db=None,
                            _memory_store=None, _memory_manager=None, _user_profile_enabled=True,
                            _auto_load_skills_resolved=True, _auto_load_skills_result=("", [], []),
                            _agent_definition_metadata=None, enabled_toolsets=(), is_subagent=False)
    monkeypatch.setattr(system_prompt, "resolve_context_cwd", lambda **_kwargs: tmp_path)
    monkeypatch.setattr(renderer, "render_project_context",
                        lambda **kwargs: "<project-context>Use pytest.</project-context>")
    stable = system_prompt.build_system_prompt_parts(agent)["stable"]
    assert stable.rstrip().endswith("</project-context>")
    assert stable.index("<project-context>") > stable.index("§ Critical")


def test_project_context_is_skipped_when_context_files_are_skipped(monkeypatch):
    from agent import system_prompt
    from agent.prompts import renderer

    agent = SimpleNamespace(skip_context_files=True, platform="cli", provider="p", model="m",
                            valid_tool_names=set(), _bot_mode_protocol=True,
                            _session_title_hint="", _session_db=None, _memory_store=None,
                            _memory_manager=None, _user_profile_enabled=True,
                            _auto_load_skills_resolved=True, _auto_load_skills_result=("", [], []),
                            _agent_definition_metadata=None, enabled_toolsets=(), is_subagent=False)
    monkeypatch.setattr(system_prompt, "resolve_context_cwd", lambda **_kwargs: Path("/nowhere"))
    monkeypatch.setattr(renderer, "render_project_context", lambda **kwargs: "")
    assert "<project-context>" not in system_prompt.build_system_prompt_parts(agent)["stable"]
