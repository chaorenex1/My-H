"""Fixtures for the code intelligence subsystem tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_key_cache


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    result = git(root, "init", "-q")
    if result.returncode != 0:
        pytest.skip(f"git unavailable: {result.stderr}")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "user.name", "Tests")
    return root


def commit(root: Path, message: str = "init") -> None:
    git(root, "add", "-A")
    result = git(root, "commit", "-qm", message)
    if result.returncode != 0:
        pytest.skip(f"git commit unavailable: {result.stderr}")


@pytest.fixture
def code_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    reset_hermes_home_key_cache()
    from hermes_graph import GraphClient

    GraphClient.close_all()
    yield home
    GraphClient.close_all()
    reset_hermes_home_key_cache()


@pytest.fixture
def sample_repo(tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "pkg").mkdir()
    (root / "src").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text(
        "import os\n"
        "\n"
        "class A:\n"
        "    def f(self):\n"
        "        return g(1)\n"
        "\n"
        "def g(x):\n"
        "    return x\n"
        "\n"
        "VALUE = 3\n",
        encoding="utf-8",
    )
    (root / "src" / "app.ts").write_text(
        "export class B { m() { return h(1); } }\n"
        "export function h(x: number) { return x; }\n"
        "const link = (a: number) => h(a);\n",
        encoding="utf-8",
    )
    commit(root)
    return root


@pytest.fixture
def ci(code_home, sample_repo):
    pytest.importorskip("ladybug", reason="the [graph] extra is not installed")
    pytest.importorskip("tree_sitter", reason="the [codegraph] extra is not installed")
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    instance = CodeIntelligence.open(
        root=sample_repo, limits=CodeGraphLimits()
    )
    try:
        yield instance
    finally:
        instance.close()
