"""Registry dispatch for rules_list / rule_view / rule_manage."""

from __future__ import annotations

import json

import pytest

from agent import rule_utils


@pytest.fixture
def rules_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(project)
    monkeypatch.setattr(rule_utils, "_install_roots", lambda: (
        tmp_path / "install", tmp_path / "bundled-skills", tmp_path / "bundled-plugins"))
    return home


def _call(name: str, args: dict | None = None) -> dict:
    import model_tools

    return json.loads(model_tools.handle_function_call(name, args or {}))


def test_create_list_view_update_delete_roundtrip(rules_env):
    created = _call("rule_manage", {
        "action": "create",
        "rule_id": "python/style",
        "content": "---\ndescription: Python style\ncondition: 'print\\('\n---\nUse logging.\n",
    })
    assert created["success"] is True
    assert (rules_env / ".agents/rules/python/style.md").is_file()

    listing = _call("rules_list")
    ids = [row["rule_id"] for row in listing["rules"]]
    assert listing["success"] is True and "python/style" in ids

    viewed = _call("rule_view", {"rule_id": "python/style"})
    assert viewed["rule"]["body"] == "Use logging."
    assert viewed["rule"]["condition"] == ["print\\("]

    updated = _call("rule_manage", {
        "action": "update",
        "rule_id": "python/style",
        "metadata": {"globs": ["**/*.py"], "interruptMode": "never"},
        "body": "Use structlog.",
    })
    assert updated["success"] is True
    text = (rules_env / ".agents/rules/python/style.md").read_text(encoding="utf-8")
    assert "globs:" in text and "interruptMode: never" in text
    assert "condition:" in text  # merged from the existing frontmatter

    refused = _call("rule_manage", {"action": "delete", "rule_id": "python/style"})
    assert refused["success"] is False
    deleted = _call("rule_manage", {"action": "delete", "rule_id": "python/style", "confirm": True})
    assert deleted["success"] is True
    assert not (rules_env / ".agents/rules/python/style.md").exists()


def test_rule_manage_refuses_path_traversal(rules_env):
    result = _call("rule_manage", {
        "action": "create", "rule_id": "../../escape", "content": "---\ndescription: x\n---\n",
    })
    assert result["success"] is False
    assert not (rules_env.parent / "escape.md").exists()
