"""Tests for agent/system_prompt.py — the assembler facade and cache contract."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.system_prompt import build_system_prompt, build_system_prompt_parts
from agent.conversation_loop import _restore_or_build_system_prompt


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=set(),
        _memory_store=None,
        _memory_manager=None,
        _memory_enabled=True,
        _user_profile_enabled=True,
        _context_cwd_is_launch_artifact=False,
        _session_db=None,
        _bot_mode_protocol=False,
        _session_title_hint="",
        _platform_hint_overrides={},
        _auto_load_skills_resolved=True,
        _auto_load_skills_result=("", [], []),
        _agent_definition_metadata=None,
        enabled_toolsets=(),
        is_subagent=False,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
        # build_system_prompt drains pending truncation warnings and forwards each to this.
        _emit_diagnostic_status=lambda *_args, **_kwargs: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _captured_context_cwd(agent):
    """The cwd ``build_system_prompt_parts`` hands to the project-context block."""
    captured = {}

    def fake_project_context(**kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return ""

    with (
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.prompts.renderer.render_project_context", side_effect=fake_project_context),
    ):
        build_system_prompt_parts(agent)
    return captured["cwd"]


def _stable_prompt(agent):
    with (
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.prompts.renderer.render_project_context", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


def _prompt_parts(agent):
    with (
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.prompts.renderer.render_project_context", return_value=""),
    ):
        return build_system_prompt_parts(agent)


class TestContextFileCwd:
    def test_none_when_terminal_cwd_unset(self, monkeypatch):
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert _captured_context_cwd(_make_agent()) is None

    def test_configured_dir_when_terminal_cwd_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        captured_cwd = _captured_context_cwd(_make_agent())
        assert Path(captured_cwd) == tmp_path

    def test_desktop_launch_artifact_does_not_load_bundled_agents_md(
        self, monkeypatch, tmp_path
    ):
        from agent import runtime_cwd

        monkeypatch.setattr(runtime_cwd, "_PACKAGE_ROOT", tmp_path.resolve())
        monkeypatch.chdir(tmp_path)
        (tmp_path / "AGENTS.md").write_text("bundled contributor instructions")

        agent = _make_agent(platform="desktop", _context_cwd_is_launch_artifact=True)
        with patch("agent.system_prompt.resolve_context_cwd", return_value=None):
            stable = _stable_prompt(agent)

        assert "bundled contributor instructions" not in stable

    def test_desktop_launch_artifact_uses_profile_configured_cwd(
        self, monkeypatch, tmp_path
    ):
        from agent import runtime_cwd

        launch = tmp_path / "launch"
        workspace = tmp_path / "workspace"
        launch.mkdir()
        workspace.mkdir()
        monkeypatch.setattr(runtime_cwd, "_PACKAGE_ROOT", launch.resolve())
        monkeypatch.chdir(launch)
        monkeypatch.setenv("TERMINAL_CWD", str(workspace))
        (launch / "AGENTS.md").write_text("bundled contributor instructions")
        (workspace / "AGENTS.md").write_text("operator workspace rules")

        token = runtime_cwd.set_session_cwd(str(launch))
        try:
            agent = _make_agent(platform="desktop", _context_cwd_is_launch_artifact=True)
            with patch("agent.prompt_builder.build_environment_hints", return_value=""):
                stable = build_system_prompt_parts(agent)["stable"]
        finally:
            runtime_cwd.reset_session_cwd(token)

        assert "operator workspace rules" in stable
        assert "bundled contributor instructions" not in stable


def test_project_context_lists_the_sessions_additional_workspace_roots(tmp_path):
    """`/add-dir` roots ride the ``<workspace-roots>`` slot of the project-context block."""
    from agent import workspace_dirs

    extra = tmp_path / "shared"
    extra.mkdir()

    class _DB:
        def get_session_workspace_roots(self, session_id):
            return [str(extra)]

    captured = {}

    def fake_project_context(*, workspace_roots=(), **kwargs):
        captured["roots"] = list(workspace_roots)
        return ""

    workspace_dirs.clear_cache("sess-roots")
    agent = _make_agent(session_id="sess-roots", _session_db=_DB())
    try:
        with (
            patch("agent.prompt_builder.build_environment_hints", return_value=""),
            patch("agent.prompts.renderer.render_project_context", side_effect=fake_project_context),
        ):
            build_system_prompt_parts(agent)
    finally:
        workspace_dirs.clear_cache("sess-roots")

    assert captured["roots"] == [str(extra)]


def test_stored_prompt_cwd_ignores_project_host_decoys(monkeypatch, tmp_path):
    from agent.conversation_loop import _stored_prompt_matches_runtime

    cwd = tmp_path / "worktree"
    cwd.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(cwd))
    decoy = ("Host: Example\nUser home directory: /example\nCurrent working directory: /example\n"
             "- Model: decoy\n- Provider: decoy")
    agent = _make_agent(
        platform="cli", model="test-model", provider="test-provider",
        _memory_store=SimpleNamespace(format_for_system_prompt=lambda _: decoy),
        _memory_manager=SimpleNamespace(build_system_prompt=lambda: decoy),
    )
    full = build_system_prompt(agent)
    assert _stored_prompt_matches_runtime(agent, full)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    assert not _stored_prompt_matches_runtime(agent, full)
    # Legacy prompts keep their original anchor.
    legacy = (f"Host: Example\nUser home directory: {tmp_path}\nCurrent working directory: {cwd}\n\n"
              f"# Project Context\n\n{decoy}\nModel: test-model\nProvider: test-provider\nPlatform: cli")
    assert not _stored_prompt_matches_runtime(agent, legacy)
    monkeypatch.setenv("TERMINAL_CWD", str(cwd))
    assert _stored_prompt_matches_runtime(agent, legacy)


def test_stored_prompt_ignores_a_missing_session_id_line(monkeypatch, tmp_path):
    """Session ID left the prompt with the timestamp line (ADR-19 D5-A); its absence must
    never force a rebuild, and a foreign id in project prose is not identity."""
    from agent.conversation_loop import _stored_prompt_matches_runtime

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    agent = _make_agent(session_id="parent-sid", pass_session_id=True)
    prompt = build_system_prompt(agent).replace("parent-sid", "someone-else")
    assert _stored_prompt_matches_runtime(agent, prompt)


def _root_prompt(*roots):
    lines = "".join(f"- {root}\n" for root in roots)
    return (
        "SYSTEM BODY\n\n<project-context>\n<workspace-roots>\nAdditional workspace directories.\n"
        f"{lines}</workspace-roots>\n</project-context>\n\n"
        "<workstation>\n- Model: test-model\n- Provider: openrouter\n- Platform: cli\n</workstation>"
    )


def _root_agent(db):
    from unittest.mock import MagicMock

    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = "sess-roots"
    agent.model = "test-model"
    agent.provider = "openrouter"
    agent.platform = "cli"
    agent.pass_session_id = False
    agent._session_db = db
    agent._use_prompt_caching = False
    agent._persist_disabled = False
    agent._bot_mode_protocol = False
    agent._session_title_hint = ""
    agent._surface_switch_note = ""
    agent._gateway_turn_context_notes = ""
    agent.enabled_toolsets = []
    agent.disabled_toolsets = None
    agent.tools = []
    agent.valid_tool_names = set()
    agent._build_system_prompt = MagicMock(return_value="NEW_PROMPT")
    return agent


class _RootDB:
    def __init__(self, stored, roots):
        self._stored, self.roots = stored, list(roots)

    def get_session(self, session_id):
        return {"system_prompt": self._stored, "tool_names": None}

    def get_session_workspace_roots(self, session_id):
        return list(self.roots)

    def update_system_prompt(self, *args, **kwargs):
        return True


def test_a_new_workspace_root_is_not_lost_to_a_stored_prompt(tmp_path, monkeypatch):
    """`/add-dir` writes the session row; the next turn must rebuild, not restore bytes that
    omit the root (the stored-prompt predicate is what decides that)."""
    from agent import workspace_dirs

    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    extra = tmp_path / "shared"
    extra.mkdir()
    stored = _root_prompt(tmp_path / "old-root")
    db = _RootDB(stored, [str(extra)])
    agent = _root_agent(db)
    workspace_dirs.clear_cache("sess-roots")
    try:
        _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])
    finally:
        workspace_dirs.clear_cache("sess-roots")
    agent._build_system_prompt.assert_called_once()
    assert agent._cached_system_prompt == "NEW_PROMPT"


def test_a_stored_prompt_listing_the_same_roots_is_reused(tmp_path, monkeypatch):
    """Adding no root must not break the prefix cache: same roots → the stored bytes are kept."""
    from agent import workspace_dirs

    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    extra = tmp_path / "shared"
    extra.mkdir()
    stored = _root_prompt(extra)
    agent = _root_agent(_RootDB(stored, [str(extra)]))
    workspace_dirs.clear_cache("sess-roots")
    try:
        _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])
    finally:
        workspace_dirs.clear_cache("sess-roots")
    agent._build_system_prompt.assert_not_called()
    assert agent._cached_system_prompt == stored


class TestNamedProfileIdentity:
    def test_real_hermes_home_under_profiles_names_the_profile(self, tmp_path, monkeypatch):
        root = tmp_path / ".hermes"
        profile_home = root / "profiles" / "coder"
        profile_home.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        from agent.file_safety import _resolve_active_profile_name

        assert _resolve_active_profile_name() == "coder"
        prompt = _stable_prompt(_make_agent())
        assert "Active Hermes profile: coder." in prompt
        assert f"{profile_home}/profiles/coder" not in prompt

    def test_default_home_names_the_default_profile(self, tmp_path, monkeypatch):
        root = tmp_path / ".hermes"
        root.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(root))
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        assert "Active Hermes profile: default." in _stable_prompt(_make_agent())


def test_stable_body_is_identical_across_default_homes(tmp_path, monkeypatch):
    """The identity block carries only the profile NAME, so two default homes share bytes."""
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    bodies = []
    for name in ("a", "b"):
        root = tmp_path / name / ".hermes"
        root.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda root=root: root.parent)
        monkeypatch.setenv("HERMES_HOME", str(root))
        bodies.append(_stable_prompt(_make_agent()))
    assert bodies[0] == bodies[1]


def test_build_system_prompt_records_stable_prefix():
    agent = _make_agent()
    with (
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.prompts.renderer.render_project_context", return_value="PROJECT"),
    ):
        prompt = build_system_prompt(agent, system_message="CALLER")

    assert prompt.startswith(agent._cached_system_prompt_static)
    assert agent._cached_system_prompt_static.endswith("PROJECT")
    assert prompt[len(agent._cached_system_prompt_static):] == "\n\nCALLER"


class TestTelegramRichMessagesHint:
    def _stable(self, agent):
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {
                "gateway": {"platforms": {"telegram": {"extra": {"rich_messages": True}}}}
            }
            return _stable_prompt(agent)

    def test_base_hint_without_rich_messages(self):
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {
                "gateway": {"platforms": {"telegram": {"extra": {"rich_messages": False}}}}
            }
            stable = _stable_prompt(agent)
        assert "Standard Markdown auto-converts" in stable
        assert "lean into it" not in stable

    def test_rich_hint_with_rich_messages_enabled(self):
        stable = self._stable(_make_agent(platform="telegram"))
        assert "lean into it" in stable
        assert "task lists" in stable

    def test_rich_hint_from_top_level_platforms(self):
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {"platforms": {"telegram": {"extra": {"rich_messages": True}}}}
            stable = _stable_prompt(agent)
        assert "lean into it" in stable

    def test_gateway_rich_messages_integration_via_real_config(self, tmp_path, monkeypatch):
        home = tmp_path / "hermes_home"
        home.mkdir()
        (home / "config.yaml").write_text(
            "gateway:\n  platforms:\n    telegram:\n      extra:\n        rich_messages: true\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        from hermes_cli import config as _cfgmod
        monkeypatch.setattr(_cfgmod, "get_config_path", lambda: home / "config.yaml")

        stable = _stable_prompt(_make_agent(platform="telegram"))
        assert "lean into it" in stable

    def test_malformed_extra_value_falls_back_to_base_hint(self):
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {"gateway": {"platforms": {"telegram": {"extra": "not-a-map"}}}}
            stable = _stable_prompt(agent)
        assert "Standard Markdown auto-converts" in stable


def _write_skill(skills_dir: Path, category: str, name: str, description: str) -> None:
    path = skills_dir / category / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nBody.\n", encoding="utf-8"
    )


def test_skills_index_renders_inside_the_runtime_block(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_skill(home / "skills", "workflow", "demo-skill", "Demo description.")
    monkeypatch.setenv("HERMES_HOME", str(home))
    agent = _make_agent(valid_tool_names={"skill_view", "skills_list"})

    parts = _prompt_parts(agent)
    body = parts["stable"]
    assert "<available_skills>" in body
    assert "demo-skill: Demo description." in body
    assert body.index("§ Runtime") < body.index("<available_skills>")
    # One assembled body: the facade keeps no separate volatile band.
    assert parts["volatile"] == ""
    assert parts["context"] == ""


class TestMemoryProviderSystemPromptGating:
    @staticmethod
    def _provider(block):
        from unittest.mock import MagicMock
        manager = MagicMock()
        manager.build_system_prompt.return_value = block
        return manager

    def _agent(self, *, enabled_toolsets, disabled_toolsets, prompt_block):
        return _make_agent(
            valid_tool_names={"skills_list"},
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            _memory_manager=self._provider(prompt_block),
            _memory_store=SimpleNamespace(format_for_system_prompt=lambda _: ""),
        )

    def test_block_injected_when_memory_toolset_enabled(self):
        block = "PROVIDER_BLOCK_SENTINEL"
        agent = self._agent(enabled_toolsets=["memory"], disabled_toolsets=None, prompt_block=block)
        assert block in _stable_prompt(agent)

    def test_block_dropped_when_memory_toolset_disabled(self):
        block = "PROVIDER_BLOCK_SENTINEL"
        agent = self._agent(enabled_toolsets=None, disabled_toolsets=["memory"], prompt_block=block)
        assert block not in _stable_prompt(agent)

    def test_block_dropped_when_memory_not_in_enabled_toolsets(self):
        block = "PROVIDER_BLOCK_SENTINEL"
        agent = self._agent(enabled_toolsets=["web_search"], disabled_toolsets=None, prompt_block=block)
        assert block not in _stable_prompt(agent)


class TestMemoryBlockGating:
    def test_memory_sections_come_from_files_under_the_agent_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        memories = home / "memories"
        memories.mkdir(parents=True)
        (memories / "USER.md").write_text("User prefers concise replies.", encoding="utf-8")
        (memories / "MEMORY.md").write_text("Repo uses pytest.", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        agent = _make_agent(_memory_store=SimpleNamespace(format_for_system_prompt=lambda _: ""))
        body = _stable_prompt(agent)
        assert "User prefers concise replies." in body
        assert "Repo uses pytest." in body

    def test_memory_disabled_drops_the_whole_section(self, monkeypatch):
        agent = _make_agent(_memory_store=None)
        assert "§ Memories" not in _stable_prompt(agent)

    def test_user_profile_flag_hides_only_the_user_tag(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        memories = home / "memories"
        memories.mkdir(parents=True)
        (memories / "USER.md").write_text("Profile fact.", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        agent = _make_agent(
            _memory_store=SimpleNamespace(format_for_system_prompt=lambda _: ""),
            _user_profile_enabled=False,
        )
        body = _stable_prompt(agent)
        assert "§ Memories" in body
        assert "Profile fact." not in body


def test_assembled_body_orders_role_runtime_tool_policy_and_the_tail():
    agent = _make_agent(valid_tool_names={"read_file"}, platform="cli")
    body = _stable_prompt(agent)
    assert body.index("§ Role") < body.index("§ Runtime")
    assert body.index("§ Runtime") < body.index("§ Workflow")
    assert body.index("§ Delivery") < body.index("§ Critical")


def test_autoload_skills_are_their_own_block_after_runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    agent = _make_agent(
        valid_tool_names={"skill_view"},
        _auto_load_skills_result=("PINNED SKILL BODY", [], []),
    )
    body = _stable_prompt(agent)
    assert "PINNED SKILL BODY" in body
    assert body.index("§ Runtime") < body.index("PINNED SKILL BODY") < body.index("§ Tool Policy")


def test_bot_chat_capability_epoch_rides_outside_the_assembled_body(monkeypatch):
    monkeypatch.setattr("tools.bot_mode_probe.get_bot_mode_protocol_section", lambda home: "protocol")
    monkeypatch.setattr("tools.bot_mode_probe.epoch_line", lambda home: "Capability epoch: deadbeef")
    agent = _make_agent(platform="desktop", _bot_mode_protocol=True, _session_title_hint="Bot Chat")
    body = _stable_prompt(agent)
    assert body.rstrip().endswith("Capability epoch: deadbeef")
    assert "protocol" not in body


def test_other_sessions_carry_no_capability_epoch():
    agent = _make_agent(_bot_mode_protocol=True, _session_title_hint="Regular chat")
    assert "Capability epoch:" not in _stable_prompt(agent)


def test_project_context_is_the_final_assembled_block(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    agent = _make_agent(valid_tool_names={"read_file"}, platform="cli")
    with patch("agent.prompt_builder.build_environment_hints", return_value=""):
        body = build_system_prompt_parts(agent)["stable"]
    tail = body[body.rindex("<project-context>"):]
    assert tail.rstrip().endswith("</critical>")
    assert tail.count("<project-context>") == 1


def test_environment_probe_lands_in_workstation_notes(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    agent = _make_agent(valid_tool_names={"read_file"})
    with (
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("tools.env_probe.get_environment_probe_line", return_value="Python toolchain: 3.14"),
    ):
        body = build_system_prompt_parts(agent)["stable"]
    assert "Python toolchain: 3.14" in body

    with (
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("tools.env_probe.get_environment_probe_line", return_value="Python toolchain: 3.14"),
    ):
        disabled = _make_agent(valid_tool_names={"read_file"}, _environment_probe=False)
        assert "Python toolchain: 3.14" not in build_system_prompt_parts(disabled)["stable"]


def test_coding_instructions_ride_the_project_context_append_tail(monkeypatch, tmp_path):
    from agent.coding_context import RuntimeMode, CODING_PROFILE

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    mode = RuntimeMode(profile=CODING_PROFILE, surface="cli", cwd=tmp_path,
                       instructions="PINNED CODING RULE")
    monkeypatch.setattr("agent.coding_context.resolve_runtime_mode", lambda **_kwargs: mode)
    captured = {}
    with (
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.prompts.renderer.render_project_context",
              side_effect=lambda **kwargs: captured.update(kwargs) or ""),
    ):
        build_system_prompt_parts(_make_agent(valid_tool_names={"read_file"}))
    assert captured["append_prompt"] == "PINNED CODING RULE"


def test_disabled_skills_are_not_advertised(tmp_path, monkeypatch):
    from agent.prompts import skills as skills_mod

    home = tmp_path / "home"
    _write_skill(home / "skills", "workflow", "demo-skill", "Demo description.")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(skills_mod, "get_disabled_skill_names", lambda _platform=None: {"demo-skill"})
    agent = _make_agent(valid_tool_names={"skill_view"})
    body = _stable_prompt(agent)
    assert "demo-skill" not in body


def test_build_stamps_prompt_schema_two_on_the_session_row():
    calls = []

    class _DB:
        def patch_session_model_config(self, session_id, patch):
            calls.append((session_id, patch))

    agent = _make_agent(session_id="s1", _session_db=_DB())
    with (
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.prompts.renderer.render_project_context", return_value=""),
    ):
        build_system_prompt(agent)
    assert calls == [("s1", {"prompt_schema": 2})]


def test_subagent_role_puts_the_shared_base_in_stable_and_the_role_block_in_context():
    from agent.prompts.assembler import SubagentRole, assemble_subagent, assemble_main
    from agent.system_prompt import _render_context as render_context

    agent = _make_agent()
    agent._subagent_role = SubagentRole(body="Review one diff.")
    with (
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.prompts.renderer.render_project_context", return_value=""),
    ):
        ctx = render_context(agent)
        parts = build_system_prompt_parts(agent)
        base = assemble_main(ctx)
        full_expected = assemble_subagent(ctx, agent._subagent_role)

    assert parts["stable"] == base
    assert parts["context"].startswith("§ Subagent Role\nReview one diff.")
    full = "\n\n".join((parts["stable"], parts["context"]))
    assert full == full_expected
    assert full.count("§ Role") == 1
    assert full.count("§ Subagent Role") == 1
    assert ctx.inputs.subagent is True
