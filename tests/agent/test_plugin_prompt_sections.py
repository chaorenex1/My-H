"""Plugin prompt sections are restored from persisted bytes but not assembled (ADR-19 D5-B)."""

from __future__ import annotations

from types import SimpleNamespace

from agent.system_prompt import (
    _restore_plugin_prompt_sections,
    build_system_prompt,
    restore_plugin_prompt_sections,
)
from hermes_cli.plugins import (
    PluginContext,
    PluginManager,
    PluginManifest,
    RenderedPluginSystemPromptSection,
    format_system_prompt_sections,
)


def _frame(content: str, *, section_id: str = "example.rules") -> str:
    section = RenderedPluginSystemPromptSection(
        id=section_id, content=content, position="after_memory", plugin="test"
    )
    return format_system_prompt_sections([section])


def test_resume_restores_the_canonical_frozen_frame():
    content = "Always run the linter."
    prompt = f"SYSTEM\n\n{_frame(content)}\n\nConversation started: Thursday, September 24, 2026"
    agent = SimpleNamespace()

    restore_plugin_prompt_sections(agent, prompt)

    restored = agent._plugin_system_prompt_sections_snapshot
    assert len(restored) == 1
    assert restored[0].id == "example.rules"
    assert restored[0].content == content


def test_non_canonical_prose_is_never_restored_as_a_section():
    prompt = (
        "SYSTEM\n\n## Plugin Context: example.rules\n<!-- hermes-plugin-section-chars:5 -->\n\n"
        "hello\n\nConversation started: Thursday, September 24, 2026"
    )
    assert _restore_plugin_prompt_sections(prompt) == ()


def test_a_resumed_prompt_missing_the_boundary_restores_nothing():
    prompt = f"SYSTEM\n\n{_frame('x')}\n\nNo boundary here"
    assert _restore_plugin_prompt_sections(prompt) == ()


def test_new_prompts_do_not_inject_plugin_sections(monkeypatch):
    """Plugin sections are an unwired carrier: the assembler writes none of them."""
    manager = PluginManager()
    manager._discovered = True
    ctx = PluginContext(
        PluginManifest(name="example-plugin", key="example-plugin", source="user"), manager
    )
    ctx.register_system_prompt_section(
        "example.rules", "PLUGIN SECTION SENTINEL", position="after_memory", max_chars=1000
    )
    from hermes_cli import plugins

    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    agent = SimpleNamespace(
        load_soul_identity=False, skip_context_files=True, valid_tool_names=set(),
        _memory_store=None, _memory_manager=None, _memory_enabled=False,
        _user_profile_enabled=False, _context_cwd_is_launch_artifact=False,
        _session_db=None, _bot_mode_protocol=False, _session_title_hint="",
        _platform_hint_overrides={}, _auto_load_skills_resolved=True,
        _auto_load_skills_result=("", [], []), _agent_definition_metadata=None,
        enabled_toolsets=(), is_subagent=False, model="", provider="", platform="cli",
        pass_session_id=False, session_id="plugin-section-test",
        _emit_diagnostic_status=lambda *a, **k: None,
    )
    assert "PLUGIN SECTION SENTINEL" not in build_system_prompt(agent)
