"""System-prompt section registry/renderer (ADR-18 D1/D13)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

pytest.importorskip("jinja2")

from agent.prompts import prompt_sections as ps
from agent.prompts import rules as rules_mod


def test_registry_preserves_manifest_order_and_rejects_duplicates():
    sections = ps.load_sections()
    assert list(sections)[:2] == ["rfc2119", "identity"]
    for section in sections.values():
        assert section.tier in {"stable", "context", "volatile"}
        assert section.gate
        if section.path:
            assert (ps.SYSTEM_PROMPT_DIR / section.path).is_file()


def test_rfc2119_renders_the_fixed_keyword_text():
    out = ps.render_section("rfc2119")
    assert out.startswith("MUST, REQUIRED, SHOULD, RECOMMENDED, MAY, OPTIONAL.")
    assert "NEVER = MUST NOT" in out
    assert len(out) <= 400


def test_safety_defaults_to_the_default_policy():
    out = ps.render_section("safety")
    assert out.startswith("# Safety\n<safety-policy>\n- Act only within what the user asked.")
    assert "- Keep the machine recoverable" in out
    assert out.endswith("\n</safety-policy>\n")
    assert "truncated" not in out


def test_safety_appends_supplied_rules_below_the_defaults():
    out = ps.render_section("safety", {"safety_rules": "- Extra rule."})
    assert out.rstrip().endswith("- Extra rule.\n</safety-policy>")
    assert "- Act only within what the user asked." in out


def test_safety_override_replaces_the_default_rules():
    out = ps.render_section(
        "safety", {"safety_rules": "- Only this.", "safety_override": True}
    )
    assert out == "# Safety\n<safety-policy>\n- Only this.\n</safety-policy>\n"


def test_safety_override_without_rules_falls_back_to_the_defaults():
    assert ps.render_section("safety", {"safety_override": True}) == ps.render_section("safety")


def test_safety_rejects_mistyped_context():
    with pytest.raises(ps.PromptSectionError):
        ps.render_section("safety", {"safety_rules": ["- x"]})
    with pytest.raises(ps.PromptSectionError):
        ps.render_section("safety", {"safety_override": "yes"})


def test_safety_over_budget_policy_fails_instead_of_truncating():
    # A tail-truncated wrapper would drop the closing tag and silently lose policy lines.
    with pytest.raises(ps.PromptBudgetError):
        ps.render_section(
            "safety", {"safety_rules": "x" * 2000, "safety_override": True}
        )


def test_renderer_entry_methods_supply_variables_and_join_in_order():
    from agent.prompts import renderer

    head = renderer.render_head(
        identity={"role": "Hermes Agent", "role_desc": "You are Hermes Agent."},
        safety={"rules": "- No force-push.", "override": True},
    )
    assert head.startswith("MUST, REQUIRED, SHOULD, RECOMMENDED, MAY, OPTIONAL.")
    assert 0 < head.index("§ Role") < head.index("# Safety")
    assert "You are Hermes Agent." in head
    assert "- No force-push." in head
    assert "- Act only within" not in head
    assert "\n\n\n" not in head  # one blank line between sections, no doubled breaks
    assert not head.endswith("\n")
    assert head.index("</safety-policy>") < head.index("# Communication")


def test_renderer_defaults_match_the_registry_defaults():
    from agent.prompts import renderer

    assert renderer.render_rfc2119() == ps.render_section("rfc2119")
    assert renderer.render_identity() == ps.render_section("identity")
    assert renderer.render_safety() == ps.render_section("safety")
    assert renderer.render_communication() == ps.render_section("communication")
    with pytest.raises(TypeError):
        renderer.render_head(identity={"bogus": "x"})


def test_communication_defaults_split_from_personality_and_platform():
    out = ps.render_section("communication")
    assert out.startswith("# Communication\n\n- Reply in the language")
    assert "Write model-facing prompts in English." in out
    assert "Commit messages in a GitHub repository are in English" in out
    assert "# Personality" not in out
    assert "# Platform" not in out


def test_communication_variables_supply_personality_and_platform():
    out = ps.render_section(
        "communication", {"personality": "- Dry.", "platform_hint": "- omp-ui."}
    )
    assert out.startswith("# Personality\n- Dry.\n\n# Communication")
    assert out.index("# Communication") < out.index("# Platform")
    assert "- omp-ui." in out
    assert "- Write model-facing prompts in English." in out

    custom = ps.render_section("communication", {"communication_style": "- Custom style."})
    assert custom.strip() == "# Communication\n\n- Custom style."
    assert "- Reply in the language" not in custom


def test_communication_budget_holds_default_plus_the_longest_platform_hint():
    from agent.prompt_builder import PLATFORM_HINTS

    hint = PLATFORM_HINTS["desktop"]
    out = ps.render_section("communication", {"platform_hint": hint})
    assert hint in out
    assert "truncated" not in out
    assert out.rstrip().endswith(hint.rstrip())


def test_skills_section_has_guide_list_and_search_guide():
    out = ps.render_section("skills")
    assert out.startswith("# Skills\n\nSkills are reusable procedures")
    assert "<available_skills>" not in out
    assert "skills_list(query=" in out

    listed = ps.render_section("skills", {"skills": ["alpha: does a", "beta: does b"]})
    assert "<available_skills>\n- alpha: does a\n- beta: does b\n</available_skills>" in listed
    assert listed.index("Skills are reusable") < listed.index("<available_skills>") < listed.index("Skills demoted")


def test_bundled_skill_rows_discover_the_builtin_tree():
    from agent.prompts.skills import bundled_skill_rows, bundled_skills_dir

    assert (bundled_skills_dir() / "software-development" / "codebase-inspection" / "SKILL.md").is_file()
    rows = bundled_skill_rows()
    assert rows == sorted(rows)
    assert "codebase-inspection: Inspect codebases w/ pygount: LOC, languages, ratios." in rows


def test_runtime_defaults_to_the_builtin_skills_list():
    from agent.prompts import renderer

    out = renderer.render_runtime()
    assert "<available_skills>" in out
    assert "- codebase-inspection: " in out
    empty = renderer.render_runtime(skills=[])
    assert "<available_skills>" not in empty
    assert "Skills demoted to a name-only line" in empty


def _write_rule(root, rel, *, globs="", description="Rule.", always_apply=False):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['---', f'description: "{description}"']
    if globs:
        lines.append(f'globs: "{globs}"')
    if always_apply:
        lines.append("alwaysApply: true")
    lines += ["---", "", "body", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def test_project_languages_count_extension_files(tmp_path):
    if rules_mod._rg_binary() is None:
        pytest.skip("ripgrep unavailable")
    (tmp_path / "a.go").write_text("package a", encoding="utf-8")
    (tmp_path / "b.go").write_text("package b", encoding="utf-8")
    (tmp_path / "c.py").write_text("x = 1", encoding="utf-8")
    assert rules_mod.project_languages(tmp_path) == {"go"}


def test_rules_section_splits_generic_and_domain_blocks():
    out = ps.render_section("rules", {
        "generic_rules": ["Use tabs.\nNever reflow."],
        "domain_rules": ["go-ioutil (**/*.go): Use io"],
        "rule_view": True,
    })
    assert "read the full rule with\n`rule_view`" in out
    assert out.startswith("# Rules\n\nRules are constraints")
    assert "Treat that reminder as authoritative" in out
    assert "Path-only rules apply when you edit a file" in out
    assert "<generic-rules>\nUse tabs.\nNever reflow.\n</generic-rules>" in out
    assert "<domain-rules>\n- go-ioutil (**/*.go): Use io\n</domain-rules>" in out

    empty = ps.render_section("rules", {"generic_rules": [], "domain_rules": [], "rule_view": False})
    assert "\n<generic-rules>\n" not in empty and "\n<domain-rules>\n" not in empty
    assert "rule_view" not in empty


def test_rule_prompt_keeps_builtins_and_gates_profile_rules_by_language(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "rules").mkdir(parents=True)
    _write_rule(home / "rules", "go-custom.md", globs="**/*.go", description="Custom go.")
    _write_rule(home / "rules", "py-custom.md", globs="**/*.py", description="Custom py.")
    _write_rule(home / "rules", "go-ioutil.md", globs="**/*.go", description="Shadowed builtin.")
    monkeypatch.setattr(rules_mod, "get_hermes_home", lambda: home)

    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.go").write_text("package a", encoding="utf-8")
    (project / "b.go").write_text("package b", encoding="utf-8")
    (project / "c.py").write_text("x = 1", encoding="utf-8")  # one .py -> not a language

    prompt = rules_mod.rule_prompt(project)
    assert prompt is not None
    assert any(row.startswith("go-ioutil: Use io and os") for row in prompt.domain)
    assert not any("Shadowed builtin." in row for row in prompt.domain)
    assert "go-custom (**/*.go): Custom go." in prompt.domain
    assert not any(row.startswith("py-custom") for row in prompt.domain)
    assert prompt.generic == ()


def test_rule_prompt_sends_always_apply_to_generic_and_ttsr_rules_to_neither_block(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "rules").mkdir(parents=True)
    _write_rule(home / "rules", "style.md", description="Style.", always_apply=True)
    monkeypatch.setattr(rules_mod, "get_hermes_home", lambda: home)

    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.go").write_text("package a", encoding="utf-8")
    (project / "b.go").write_text("package b", encoding="utf-8")

    prompt = rules_mod.rule_prompt(project)
    assert prompt is not None
    assert prompt.generic == ("body",)

    # An omp-ui viewer registers the condition rules with TTSR: they leave the static prompt.
    monkeypatch.setattr(rules_mod, "ttsr_session_active", lambda: True)
    runtime = rules_mod.rule_prompt(project)
    assert runtime is not None
    assert "go-ioutil" not in " ".join(runtime.domain)
    assert runtime.generic == ("body",)


def test_runtime_includes_rules_only_for_programming_projects(tmp_path, monkeypatch):
    from agent.prompts import renderer

    home = tmp_path / "home"
    (home / "rules").mkdir(parents=True)
    monkeypatch.setattr(rules_mod, "get_hermes_home", lambda: home)

    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.go").write_text("package a", encoding="utf-8")
    (project / "b.go").write_text("package b", encoding="utf-8")
    out = renderer.render_runtime(project_dir=project)
    assert "# Rules" in out and "go-ioutil" in out

    empty = tmp_path / "empty"
    empty.mkdir()
    assert "# Rules" not in renderer.render_runtime(project_dir=empty)


def test_internal_url_rows_cover_the_home_layout_without_host_paths():
    from agent.prompts.urls import internal_url_rows

    rows = internal_url_rows()
    assert rows[0].startswith("skills: $HERMES_HOME/skills")
    assert "HERMES home: $HERMES_HOME" not in rows
    for row in rows:
        assert "$HERMES_HOME" in row and "C:\\" not in row
    assert any(row.startswith("skills: $HERMES_HOME/skills") for row in rows)
    assert any(row.startswith("rules: $HERMES_HOME/rules") for row in rows)
    assert any(row.startswith("sessions: $HERMES_HOME/sessions") for row in rows)
    assert any("cache/scratch" in row for row in rows)


def test_internal_urls_section_names_the_home_token_and_defaults_to_no_rows():
    out = ps.render_section("internal_urls")
    assert out.startswith("# Internal URLs\n\n`$HERMES_HOME` is this profile's home.")
    assert "Exact paths on this host" not in out
    assert "- skills: $HERMES_HOME/skills" not in out


def test_is_hermes_agent_help_uses_the_packaged_question():
    from types import SimpleNamespace

    from agent.judgment.types import NoulAnswer
    from agent.prompts.hermes_help import is_hermes_agent_help

    def fake(request, **_kwargs):
        assert "hermes_agent_help" in request.questions
        return SimpleNamespace(answers={"hermes_agent_help": NoulAnswer(0.9)})

    assert is_hermes_agent_help("how do I configure telegram?", judge_fn=fake)
    assert not is_hermes_agent_help("", judge_fn=fake)

    def low(_request, **_kwargs):
        return SimpleNamespace(answers={"hermes_agent_help": NoulAnswer(0.2)})

    assert not is_hermes_agent_help("fix my app", judge_fn=low)

    def broken(_request, **_kwargs):
        raise RuntimeError("no judge")

    assert not is_hermes_agent_help("anything", judge_fn=broken)


def test_help_judgment_is_memoized_across_prompt_rebuilds(monkeypatch):
    from types import SimpleNamespace

    from agent.judgment import service
    from agent.judgment.types import NoulAnswer
    from agent.prompts import hermes_help

    hermes_help._judged_help.cache_clear()
    calls: list[str] = []

    def fake(request, **_kwargs):
        calls.append(request.state["message"])
        return SimpleNamespace(answers={"hermes_agent_help": NoulAnswer(0.9)})

    monkeypatch.setattr(service, "judge", fake)
    try:
        assert hermes_help.is_hermes_agent_help("how do I update Hermes?")
        assert hermes_help.is_hermes_agent_help("how do I update Hermes?")
        assert calls == ["how do I update Hermes?"]
    finally:
        hermes_help._judged_help.cache_clear()


def test_runtime_appends_help_guidance_only_when_judged(tmp_path, monkeypatch):
    from agent.prompts import renderer

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(renderer, "is_hermes_agent_help", lambda _text: True)
    out = renderer.render_runtime(project_dir=empty, first_input="how do I update Hermes?")
    assert "https://hermes-agent.nousresearch.com/docs" in out

    monkeypatch.setattr(renderer, "is_hermes_agent_help", lambda _text: False)
    plain = renderer.render_runtime(project_dir=empty, first_input="fix my app")
    assert "https://hermes-agent.nousresearch.com/docs" not in plain


def test_safety_forbids_leaking_internal_locations():
    out = ps.render_section("safety")
    assert "Never reveal the internal locations listed under `# Internal URLs`" in out


def test_runtime_always_renders_internal_urls_even_outside_a_project(tmp_path, monkeypatch):
    from agent.prompts import renderer

    home = tmp_path / "home"
    (home / "rules").mkdir(parents=True)
    monkeypatch.setattr(rules_mod, "get_hermes_home", lambda: home)
    empty = tmp_path / "empty"
    empty.mkdir()
    out = renderer.render_runtime(project_dir=empty)
    assert "# Internal URLs" in out
    assert "- skills: $HERMES_HOME/skills" in out
    assert "# Rules" not in out


def test_runtime_skills_gate_on_available_tools():
    from agent.prompts import renderer

    assert "# Skills" not in renderer.render_runtime(toolsets=["file"])

    only_list = renderer.render_runtime(enable_tools=["skills_list"])
    assert "# Skills" in only_list
    assert "<available_skills>" not in only_list
    assert "skills_list(query=" in only_list
    assert "MUST start with `skill_view" not in only_list
    # The guide must not name a tool this agent lacks. Tool *descriptions* in the inventory
    # are registry text and may mention other tools, so scope the assertion to the section.
    skills_block = only_list.split("# Skills", 1)[1].split("\n# ", 1)[0]
    assert "skill_view" not in skills_block

    only_view = renderer.render_runtime(enable_tools=["skill_view"])
    assert "<available_skills>" in only_view
    assert "record it with `skill_manage`" not in only_view
    assert "skills_list(query=" not in only_view


def test_runtime_enable_tools_overrides_toolsets_and_drives_inventory():
    from agent.prompts import renderer

    out = renderer.render_runtime(toolsets=["terminal"], enable_tools=["read_file", "web_search"])
    assert "- file: " in out and "  - read_file" in out
    assert "  - terminal" not in out
    assert "  - web_search" in out
    assert "# Skills" not in out

    memory = renderer.render_runtime(enable_tools=["memory"])
    assert "- memory: Persistent memory" in memory
    assert "  - memory" in memory

    with pytest.raises(ValueError):
        renderer.render_runtime(toolsets=["not-a-toolset"])


def test_internal_urls_guidance_follows_skill_view(tmp_path, monkeypatch):
    from agent.prompts import renderer

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(renderer, "is_hermes_agent_help", lambda _text: True)
    with_skills = renderer.render_runtime(
        project_dir=empty, first_input="how do I configure Hermes?", enable_tools=["skill_view"])
    assert "skill_view(name='hermes-agent')" in with_skills

    without = renderer.render_runtime(
        project_dir=empty, first_input="how do I configure Hermes?", enable_tools=["read_file"])
    assert "https://hermes-agent.nousresearch.com/docs" in without
    assert "skill_view(name='hermes-agent')" not in without


def test_tool_inventory_is_two_levels_and_gates_codegraph_on_the_project(tmp_path):
    from agent.prompts import renderer

    repo = Path(__file__).resolve().parents[2]
    out = renderer.render_runtime(project_dir=repo, toolsets=["codegraph"])
    assert "# Tool Inventory" in out
    assert "- codegraph: " in out
    assert "  - code_intel" in out

    empty = tmp_path / "empty"
    empty.mkdir()
    assert "- codegraph" not in renderer.render_runtime(project_dir=empty, toolsets=["codegraph"])


def test_is_kanban_intent_uses_the_packaged_question():
    from types import SimpleNamespace

    from agent.judgment.types import NoulAnswer
    from agent.prompts.kanban import is_kanban_intent

    def fake(request, **_kwargs):
        assert "kanban_intent" in request.questions
        return SimpleNamespace(answers={"kanban_intent": NoulAnswer(0.9)})

    assert is_kanban_intent("move this card to review", judge_fn=fake)
    assert not is_kanban_intent("", judge_fn=fake)

    def broken(_request, **_kwargs):
        raise RuntimeError("no judge")

    assert not is_kanban_intent("anything", judge_fn=broken)


def test_internal_urls_appends_kanban_guidance_only_with_intent_and_tools(tmp_path, monkeypatch):
    from agent.prompts import renderer

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(renderer, "is_hermes_agent_help", lambda _text: False)
    judged: list[str] = []
    monkeypatch.setattr(
        renderer, "is_kanban_intent", lambda text: judged.append(text) or True)

    with_tools = renderer.render_runtime(
        project_dir=empty, first_input="show me the board", enable_tools=["kanban_show"])
    assert "# Kanban task execution protocol" in with_tools
    assert judged == ["show me the board"]

    judged.clear()
    without_tools = renderer.render_runtime(
        project_dir=empty, first_input="show me the board", enable_tools=["read_file"])
    assert "# Kanban task execution protocol" not in without_tools
    assert judged == [], "no kanban tools -> the judgment must not run"

    monkeypatch.setattr(renderer, "is_kanban_intent", lambda _text: False)
    assert "# Kanban task execution protocol" not in renderer.render_runtime(
        project_dir=empty, first_input="show me the board", enable_tools=["kanban_show"])


def test_runtime_block_prepends_the_heading_once():
    from agent.prompts import renderer

    out = renderer.render_runtime(skills=["alpha: does a"])
    assert out.startswith("§ Runtime\n\n# Skills\n\n")
    assert out.count("§ Runtime") == 1
    assert "- alpha: does a" in out
    assert "skills_list(query=" in out


def test_render_role_emits_each_platform_default_text():
    from agent.prompts import renderer
    from agent.prompts.platforms import load_platform_hints

    hints = load_platform_hints()
    assert "omp-ui" in hints
    for platform, hint in hints.items():
        out = renderer.render_role(platform)
        assert out.startswith("§ Role")
        assert f"# Platform\n{hint}" in out
    assert "omp-ui local viewer" in renderer.render_role("omp-ui")
    assert "# Platform" not in renderer.render_role("unknown-surface")


def test_render_role_applies_platform_capability_gates():
    from agent.prompts import renderer

    cli = renderer.render_role("cli")
    assert "Visuals:" not in cli
    assert "MAY react" in cli  # emoji is plain text; only renderer-specific blocks drop
    api = renderer.render_role("api_server")
    assert "Visuals:" not in api
    cron = renderer.render_role("cron")
    assert "Visuals:" not in cron and "MAY react" not in cron
    desktop = renderer.render_role("desktop")
    assert "Visuals:" in desktop and "MAY react" in desktop
    # An explicit caller value wins over the platform table.
    assert "Visuals:" in renderer.render_role("cli", identity={"render_svg": True})


def test_renderer_communication_entry_and_head_order():
    from agent.prompts import renderer

    head = renderer.render_head(
        identity={"role": "Hermes Agent", "role_desc": "You are Hermes Agent."},
        safety={"rules": "- No force-push.", "override": True},
        communication={"personality": "- Dry.", "platform": "- omp-ui."},
    )
    assert (
        head.index("§ Role")
        < head.index("# Safety")
        < head.index("# Personality")
        < head.index("# Platform")
    )
    assert head.index("# Communication") > head.index("# Personality")


def test_identity_defaults_fall_back_to_the_default_role_block():
    out = ps.render_section("identity")
    assert out.startswith("§ Role\nYou are ")
    assert "\n\n# Engineering\n" in out
    assert "- Otherwise: table or prose." in out
    assert (out == ps.render_section("identity", {"role": "", "role_desc": "", "role_section": ""}))


def test_identity_uses_supplied_role_values():
    out = ps.render_section(
        "identity",
        {"role": "Hermes Agent", "role_desc": "You are Hermes Agent.", "role_section": "- Runs commands."},
    )
    assert out == "§ Role\nYou are Hermes Agent.\n\n# Hermes Agent\n- Runs commands."


def test_identity_capability_gates_drop_the_visuals_block():
    out = ps.render_section(
        "identity",
        {"auto_graph": False, "render_mermaid": False, "render_svg": False, "reactions": False},
    )
    assert "Visuals:" not in out
    assert "MAY react" not in out
    assert "- Otherwise: table or prose." in out


def test_undeclared_or_mistyped_context_is_rejected():
    with pytest.raises(ps.PromptSectionError):
        ps.render_section("identity", {"bogus": "x"})
    with pytest.raises(ps.PromptSectionError):
        ps.render_section("identity", {"role": 1})


def test_unknown_section_and_python_only_section_raise():
    with pytest.raises(ps.PromptSectionError):
        ps.render_section("does-not-exist")
    with pytest.raises(ps.PromptSectionError):
        ps.render_section("additional_communication")


def test_budget_truncation_respects_the_strategy():
    section = ps.PromptSection(
        key="x", path=None, tier="stable", gate="always", budget=40, truncate="tail",
        override=False, renderer=None, context_keys=(), context_types={},
    )
    out = ps.apply_budget("a" * 100, section)
    assert len(out) <= 40 and "truncated" in out
    # A budget smaller than the marker cannot carry it; the hard limit still wins.
    assert len(ps.apply_budget("a" * 100, dataclasses.replace(section, budget=20))) <= 20
    with pytest.raises(ps.PromptBudgetError):
        ps.apply_budget("a" * 100, dataclasses.replace(section, truncate="error"))
    assert ps.apply_budget("short", section) == "short"
    # ADR-18 D3: 0 means unlimited, exactly like an unset budget.
    zero = dataclasses.replace(section, budget=0)
    assert ps.apply_budget("a" * 100, zero) == "a" * 100


def test_int_context_is_supported_and_unknown_types_rejected(tmp_path):
    section = ps.PromptSection(
        key="x", path=None, tier="stable", gate="always", budget=None, truncate="tail",
        override=False, renderer=None, context_keys=("n",), context_types={"n": "int"},
    )
    assert ps.validate_context(section, {"n": 3}) == {"n": 3}
    with pytest.raises(ps.PromptSectionError):
        ps.validate_context(section, {"n": True})
    with pytest.raises(ps.PromptSectionError):
        ps.validate_context(section, {"n": 3.0})

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        '{"sections": [{"key": "x", "path": "00-rfc2119.md", "tier": "stable", '
        '"gate": "always", "context_keys": ["n"], "context_types": {"n": "float"}}]}',
        encoding="utf-8",
    )
    with pytest.raises(ps.PromptSectionError):
        ps.load_sections(manifest)
