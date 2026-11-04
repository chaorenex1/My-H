"""Rule discovery, omp-compatible frontmatter, and managed writes."""

from __future__ import annotations

import pytest

from agent import rule_utils


@pytest.fixture
def rule_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(rule_utils, "_install_roots", lambda: (
        tmp_path / "install", tmp_path / "bundled-skills", tmp_path / "bundled-plugins"))
    return home, project


def _write(root, rel: str, body: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_discovery_mirrors_agent_scopes_and_path_ids(rule_env):
    home, project = rule_env
    _write(project, ".agents/rules/shared.md", "---\ndescription: project dot-agents\n---\nA\n")
    _write(project, "rules/shared.md", "---\ndescription: project rules\n---\nB\n")
    _write(project, "rules/ts/no-any.md", "---\ndescription: TS\nastCondition: '$V as any'\n---\nC\n")
    _write(home, ".agents/rules/profile-only.md", "---\ndescription: profile\n---\nD\n")
    _write(home, "rules/python/coding-style.md", "---\ndescription: py\npaths: ['**/*.py']\n---\nE\n")
    _write(home, "skills/demo/rules/skill-rule.md", "---\ndescription: skill\n---\nF\n")

    rules = rule_utils.discover_rules(project_dir=project)
    by_id = {rule["rule_id"]: rule for rule in rules}

    assert set(by_id) >= {
        "shared", "ts/no-any", "profile-only", "python/coding-style", "skill-rule",
    }
    assert by_id["shared"]["description"] == "project dot-agents"
    assert by_id["shared"]["scope"] == "project"
    assert by_id["profile-only"]["scope"] == "profile"
    assert by_id["python/coding-style"]["metadata"]["globs"] == ["**/*.py"]


def test_frontmatter_is_omp_compatible_and_roundtrips(rule_env):
    parsed = rule_utils.parse_rule_text(
        "---\n"
        "description: Rust guard\n"
        "condition:\n"
        "  - 'Box::leak\\('\n"
        "  - 'unsafe { std::mem::transmute'\n"
        "scope: 'text, tool:edit(*.rs)'\n"
        "globs: ['src/**/*.rs']\n"
        "interruptMode: never\n"
        "alwaysApply: true\n"
        "agents: scout, reviewer\n"
        "---\n"
        "Use Arc instead.\n",
        source="x.md", rule_id="rs/guard")

    meta = parsed["metadata"]
    assert meta["condition"] == ["Box::leak\\(", "unsafe { std::mem::transmute"]
    assert meta["scope"] == ["text", "tool:edit(*.rs)"]
    assert meta["globs"] == ["src/**/*.rs"]
    assert meta["interrupt_mode"] == "never"
    assert meta["always_apply"] is True
    assert meta["agents"] == ["scout", "reviewer"]
    assert parsed["body"] == "Use Arc instead."

    formatted = rule_utils.format_rule(meta, parsed["body"])
    reparsed = rule_utils.parse_rule_text(formatted, rule_id="rs/guard")
    assert reparsed["metadata"]["condition"] == meta["condition"]
    assert reparsed["metadata"]["scope"] == meta["scope"]
    assert reparsed["metadata"]["globs"] == meta["globs"]
    assert "globs:" in formatted and "paths:" not in formatted


def test_root_readme_documents_the_directory_but_nested_readmes_are_rules(rule_env):
    home, project = rule_env
    _write(home, "rules/README.md", "# Rules\n\nDirectory docs.\n")
    _write(home, "rules/python/README.md", "---\ndescription: nested\n---\nBody\n")

    ids = {rule["rule_id"] for rule in rule_utils.discover_rules(project_dir=project)}
    assert "README" not in ids
    assert "python/README" in ids


def test_paths_is_a_globs_alias_and_enabled_false_hides(rule_env):
    home, project = rule_env
    _write(home, "rules/py.md", "---\ndescription: py\npaths: '**/*.py'\n---\nBody\n")
    _write(home, "rules/off.md", "---\ndescription: off\nenabled: false\n---\nBody\n")

    rules = rule_utils.discover_rules(project_dir=project)
    by_id = {rule["rule_id"]: rule for rule in rules}
    assert by_id["py"]["metadata"]["globs"] == ["**/*.py"]
    assert "off" not in by_id


def test_managed_writes_stay_inside_the_profile_root(rule_env):
    home, _project = rule_env

    with pytest.raises(rule_utils.RuleError):
        rule_utils.validate_rule_id("../escape")
    assert rule_utils.validate_rule_id("python/style.md") == "python/style"

    path = rule_utils.write_rule(
        "python/style", "---\ndescription: style\ncondition: 'print\\('\n---\nUse logging.\n")
    assert path == home / ".agents" / "rules" / "python" / "style.md"

    with pytest.raises(rule_utils.RuleError):
        rule_utils.write_rule("python/style", "---\ndescription: dup\n---\nDup\n")
    rule_utils.write_rule(
        "python/style", "---\ndescription: style v2\n---\nUse logging.\n", overwrite=True)

    found = rule_utils.find_rule("python/style")
    assert found is not None and found["description"] == "style v2"
    assert rule_utils.delete_rule("python/style") is True
    assert rule_utils.delete_rule("python/style") is False


def test_duplicate_frontmatter_names_are_ambiguous(rule_env):
    home, project = rule_env
    _write(home, "rules/a/one.md", "---\nname: dup\ndescription: one\n---\nA\n")
    _write(home, "rules/b/two.md", "---\nname: dup\ndescription: two\n---\nB\n")

    rules = rule_utils.discover_rules(project_dir=project)
    assert {"a/one", "b/two"} <= {rule["rule_id"] for rule in rules}
    with pytest.raises(rule_utils.RuleError):
        rule_utils.find_rule("dup", rules=rules)


def test_check_reports_missing_description_and_bad_regex(rule_env):
    home, project = rule_env
    _write(home, "rules/blank.md", "---\ncondition: '(' \n---\n\n")
    _write(home, "rules/good.md",
           "---\ndescription: ok\ncondition: 'Box::leak'\ninterruptMode: never\n---\nBody\n")

    problems = rule_utils.check_rules(rule_utils.discover_rules(project_dir=project))
    issues = {(problem["rule_id"], problem["issue"].split(":")[0]) for problem in problems}
    assert ("blank", "missing description") in issues
    assert ("blank", "invalid condition regex") in issues
    assert any(rule_id == "blank" and issue.startswith("missing interruptMode")
               for rule_id, issue in issues)
    assert not any(rule_id == "good" for rule_id, _issue in issues)
