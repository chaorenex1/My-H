"""Default-profile export includes agents, rules, and the shared .agents surface."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from hermes_cli.profiles import export_profile


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def test_export_default_includes_agents_rules_and_dot_agents(profile_env, tmp_path):
    """Named-agent definitions, rule files, and the shared .agents surface are profile data."""
    default_dir = profile_env / ".hermes"
    files = {
        "agents/reviewer.md": "---\nname: reviewer\n---\nReview.",
        "rules/python/coding-style.md": "---\npaths:\n  - '**/*.py'\n---\nPEP 8.",
        ".agents/rules/shared.md": "---\ndescription: shared\n---\nShared.",
        ".agents/skills/demo/SKILL.md": "---\nname: demo\ndescription: demo\n---\nBody.",
    }
    for rel, body in files.items():
        target = default_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    output = tmp_path / "export" / "default.tar.gz"
    output.parent.mkdir(parents=True, exist_ok=True)
    export_profile("default", str(output))

    with tarfile.open(str(output), "r:gz") as tf:
        names = set(tf.getnames())

    assert {f"default/{rel}" for rel in files} <= names
