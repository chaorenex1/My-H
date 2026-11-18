"""ADR-6 P2: the ast-grep CLI bridge used by TTSR ``astCondition`` matching."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from agent import ttsr_ast


def _completed(returncode=0, stdout="STDIN\n", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_ast_match_feeds_stdin_and_uses_files_with_matches(monkeypatch):
    seen = {}

    def _run(command, **kwargs):
        seen["command"] = command
        seen["input"] = kwargs["input"]
        return _completed()

    monkeypatch.setattr(ttsr_ast.subprocess, "run", _run)
    assert ttsr_ast.ast_match(
        "/fake/ast-grep", "const $A = $B", "const x = 1", "ts", timeout=1.5) is True
    command = seen["command"]
    assert command[0] == "/fake/ast-grep"
    assert command[command.index("--pattern") + 1] == "const $A = $B"
    assert command[command.index("--lang") + 1] == "ts"
    assert command[command.index("--strictness") + 1] == "smart"
    assert "--stdin" in command and "--files-with-matches" in command
    assert seen["input"] == "const x = 1"


def test_ast_match_treats_no_match_as_false(monkeypatch):
    monkeypatch.setattr(
        ttsr_ast.subprocess, "run",
        lambda *_a, **_kw: _completed(returncode=1, stdout=""))
    assert ttsr_ast.ast_match("/fake/ast-grep", "p", "s", "ts") is False


def test_ast_match_treats_backend_errors_as_no_match(monkeypatch):
    monkeypatch.setattr(
        ttsr_ast.subprocess, "run",
        lambda *_a, **_kw: _completed(returncode=8, stdout="", stderr="bad pattern"))
    assert ttsr_ast.ast_match("/fake/ast-grep", "p", "s", "ts") is False

    monkeypatch.setattr(
        ttsr_ast.subprocess, "run",
        lambda *_a, **_kw: _completed(returncode=0, stdout=""))
    assert ttsr_ast.ast_match("/fake/ast-grep", "p", "s", "ts") is False


def test_ast_match_treats_timeout_as_no_match(monkeypatch):
    def _timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 1.0)

    monkeypatch.setattr(ttsr_ast.subprocess, "run", _timeout)
    assert ttsr_ast.ast_match("/fake/ast-grep", "p", "s", "ts", timeout=1.0) is False


def test_ast_match_short_circuits_empty_inputs(monkeypatch):
    monkeypatch.setattr(
        ttsr_ast.subprocess, "run",
        lambda *_a, **_kw: pytest.fail("empty input must not spawn ast-grep"))
    assert ttsr_ast.ast_match("", "p", "s", "ts") is False
    assert ttsr_ast.ast_match("/fake/ast-grep", "", "s", "ts") is False
    assert ttsr_ast.ast_match("/fake/ast-grep", "p", "", "ts") is False


def test_language_for_path_uses_omp_extension_aliases():
    assert ttsr_ast.language_for_path("src/a.ts") == "ts"
    assert ttsr_ast.language_for_path("src/a.mts") == "ts"
    assert ttsr_ast.language_for_path("src/a.tsx") == "tsx"
    assert ttsr_ast.language_for_path("src/a.jsx") == "jsx"
    assert ttsr_ast.language_for_path("src/a.rs") == "rust"
    assert ttsr_ast.language_for_path("src/a.py") == "python"
    assert ttsr_ast.language_for_path("src/a.go") == "go"
    assert ttsr_ast.language_for_path("src/a.hpp") == "cpp"
    assert ttsr_ast.language_for_path("Makefile") is None
    assert ttsr_ast.language_for_path("README.md") is None
    assert ttsr_ast.language_for_path("notes.txt") is None


def test_resolve_ast_grep_uses_the_pm_activation_path(monkeypatch):
    import hermes_platform.resolver
    import pm.install

    seen = {}

    monkeypatch.setattr(pm.install, "env_for", lambda _name: {"PATH": "pm-path"})

    def _locate(name, ctx=None):
        seen["name"] = name
        seen["path"] = ctx.effective_path() if ctx is not None else None
        return SimpleNamespace(command=("C:/tools/ast-grep.exe",))

    monkeypatch.setattr(hermes_platform.resolver, "locate_command", _locate)
    assert ttsr_ast.resolve_ast_grep() == "C:/tools/ast-grep.exe"
    assert seen == {"name": "ast-grep", "path": "pm-path"}


@pytest.fixture(scope="module")
def ast_grep():
    executable = ttsr_ast.resolve_ast_grep()
    if executable is None:
        pytest.skip("ast-grep CLI is not installed")
    return executable


def test_real_ast_grep_matches_a_typescript_pattern(ast_grep):
    assert ttsr_ast.ast_match(
        ast_grep, "const $A: any = $B", "const x: any = 1", "ts")
    assert not ttsr_ast.ast_match(
        ast_grep, "const $A: any = $B", "const x = 1", "ts")
