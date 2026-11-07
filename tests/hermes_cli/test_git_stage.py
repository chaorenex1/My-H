"""Judge-driven selective staging: real-repo index-only behavior and CLI wiring."""

from __future__ import annotations

import argparse
import json
import os
import subprocess

import pytest

from hermes_cli import git_stage


def git(repo, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout


def write_text(path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


class ScriptedJudge:
    """``belongs`` by state content; ``pick`` by a fixed ``none`` probability."""

    label = "scripted"
    supports_images = False

    def __init__(self, match, none: float = 0.05) -> None:
        self.match = match
        self.none = none
        self.requests = []

    def judge(self, request, **_kwargs):
        from agent.judgment.types import ChoiceAnswer, JudgmentResult, ScoreAnswer

        self.requests.append(request)
        if "belongs" in request.questions:
            hit = bool(self.match(request.state))
            answers = {"belongs": ScoreAnswer(
                2 if hit else 0,
                {"0": 0.0 if hit else 0.9, "1": 0.1, "2": 0.9 if hit else 0.0},
                0.9,
            )}
        else:
            answers = {"pick": ChoiceAnswer(
                "none" if self.none >= 0.5 else "c0", {"none": self.none}, 0.9)}
        return JudgmentResult(
            kind="local", api="text", provider="scripted", model="scripted",
            answers=answers,
        )


class FailingJudge:
    label = "failing"
    supports_images = False

    def judge(self, request, **_kwargs):
        raise RuntimeError("judge backend exploded")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("GIT_") or key in ("EMAIL", "GIT_EDITOR"):
            monkeypatch.delenv(key, raising=False)
    root = tmp_path.resolve() / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    write_text(root / "app.py", "\n".join(f"line {n}" for n in range(1, 41)) + "\n")
    git(root, "add", "app.py")
    git(root, "commit", "-q", "-m", "base")
    return root


def two_hunk_tree(root) -> None:
    lines = [f"line {n}" for n in range(1, 41)]
    lines[2] = "line 3 alpha feature"
    lines[36] = "line 37 beta feature"
    write_text(root / "app.py", "\n".join(lines) + "\n")


def patch_judge(monkeypatch, judge) -> None:
    monkeypatch.setattr("agent.judgment.resolve_judge", lambda **_kwargs: judge)
    monkeypatch.setattr("agent.judgment.find.judgment_tools_available", lambda: True)


def test_stage_selects_one_hunk_and_leaves_the_rest_alone(repo, monkeypatch):
    two_hunk_tree(repo)
    patch_judge(monkeypatch, ScriptedJudge(
        lambda state: "alpha" in str(state.get("change") or "")))
    before = (repo / "app.py").read_bytes()
    commits_before = git(repo, "rev-list", "--count", "HEAD").strip()

    summary = git_stage.stage("stage the alpha change", cwd=str(repo))

    assert summary == {
        "staged": True, "whole_files": 0, "matched_files": 1, "total_files": 1,
        "matched_hunks": 1, "total_hunks": 2, "skipped": [], "dry_run": False,
    }
    staged = git(repo, "diff", "--cached", "--unified=0")
    assert "alpha feature" in staged
    assert "beta feature" not in staged
    unstaged = git(repo, "diff", "--unified=0")
    assert "beta feature" in unstaged
    assert "alpha feature" not in unstaged
    assert (repo / "app.py").read_bytes() == before
    assert git(repo, "rev-list", "--count", "HEAD").strip() == commits_before


def test_dry_run_reports_without_touching_the_index(repo, monkeypatch):
    two_hunk_tree(repo)
    patch_judge(monkeypatch, ScriptedJudge(
        lambda state: "alpha" in str(state.get("change") or "")))

    summary = git_stage.stage("stage the alpha change", cwd=str(repo), dry_run=True)

    assert summary["staged"] is True and summary["dry_run"] is True
    assert summary["matched_hunks"] == 1
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_a_failed_judgment_stages_nothing(repo, monkeypatch):
    two_hunk_tree(repo)
    patch_judge(monkeypatch, FailingJudge())

    with pytest.raises(RuntimeError, match="judgment failed"):
        git_stage.stage("stage the alpha change", cwd=str(repo))

    assert git(repo, "diff", "--cached", "--name-only") == ""
    assert "alpha feature" in git(repo, "diff", "--unified=0")


def test_pick_none_stages_nothing(repo, monkeypatch):
    two_hunk_tree(repo)
    patch_judge(monkeypatch, ScriptedJudge(
        lambda state: "alpha" in str(state.get("change") or ""), none=0.8))

    summary = git_stage.stage("stage the alpha change", cwd=str(repo))

    assert summary["staged"] is False
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_untracked_file_is_staged_whole_and_the_worktree_is_untouched(repo, monkeypatch):
    write_text(repo / "alpha.txt", "alpha notes\nsecond line\n")
    patch_judge(monkeypatch, ScriptedJudge(
        lambda state: state.get("kind") == "new file" and "alpha" in str(state.get("change"))))

    summary = git_stage.stage("stage the alpha notes", cwd=str(repo))

    assert summary == {
        "staged": True, "whole_files": 1, "matched_files": 1, "total_files": 1,
        "matched_hunks": 0, "total_hunks": 0, "skipped": [], "dry_run": False,
    }
    assert git(repo, "diff", "--cached", "--name-only").strip() == "alpha.txt"
    assert (repo / "alpha.txt").read_text(encoding="utf-8") == "alpha notes\nsecond line\n"


def test_path_scopes_the_judged_units(repo, monkeypatch):
    two_hunk_tree(repo)
    write_text(repo / "notes.md", "alpha notes\n")
    patch_judge(monkeypatch, ScriptedJudge(lambda state: True))

    summary = git_stage.stage("stage everything alpha", cwd=str(repo), path="notes.md")

    assert summary["total_files"] == 1 and summary["total_hunks"] == 0
    assert summary["whole_files"] == 1
    assert git(repo, "diff", "--cached", "--name-only").strip() == "notes.md"


def test_nothing_to_filter_is_an_error(repo, monkeypatch):
    patch_judge(monkeypatch, ScriptedJudge(lambda state: True))

    with pytest.raises(RuntimeError, match="no unstaged changes"):
        git_stage.stage("stage anything", cwd=str(repo))


def test_parse_unified_diff_splits_header_and_hunks():
    text = (
        "diff --git a/app.py b/app.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,3 +1,3 @@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
        " three\n"
        "@@ -10,3 +10,3 @@\n"
        " ten\n"
        "-eleven\n"
        "+ELEVEN\n"
        " twelve\n"
    )

    parsed = git_stage.parse_unified_diff(text)

    assert parsed.header.endswith("+++ b/app.py")
    assert len(parsed.hunks) == 2
    assert not parsed.binary and not parsed.new_file and not parsed.deleted_file
    assert parsed.hunks[0].change == "-two\n+TWO"
    assert parsed.hunks[1].raw.startswith("@@ -10,3 +10,3 @@\n ten")
    assert git_stage.bound("x" * 5, 3) == "xxx\n…"


def test_binary_file_is_staged_whole(repo, monkeypatch):
    write_text(repo / "blob.bin", "\x00\x01\x02")
    git(repo, "add", "blob.bin")
    git(repo, "commit", "-q", "-m", "blob")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    patch_judge(monkeypatch, ScriptedJudge(
        lambda state: state.get("path") == "blob.bin" and state.get("kind") == "binary"))

    summary = git_stage.stage("stage the binary blob", cwd=str(repo))

    assert summary == {
        "staged": True, "whole_files": 1, "matched_files": 1, "total_files": 1,
        "matched_hunks": 0, "total_hunks": 0, "skipped": [], "dry_run": False,
    }
    assert git(repo, "diff", "--cached", "--name-only").strip() == "blob.bin"
    assert git(repo, "diff", "--name-only") == ""
    assert (repo / "blob.bin").read_bytes() == b"\x00\x01\x02\x03"


def test_deleted_file_hunk_is_replayed_into_the_index(repo, monkeypatch):
    write_text(repo / "plot.py", "alpha plot\n")
    git(repo, "add", "plot.py")
    git(repo, "commit", "-q", "-m", "plot")
    (repo / "plot.py").unlink()
    patch_judge(monkeypatch, ScriptedJudge(
        lambda state: state.get("kind") == "deleted file"
        and "alpha plot" in str(state.get("change"))))

    summary = git_stage.stage("stage removing the alpha plot", cwd=str(repo))

    assert summary["matched_hunks"] == 1 and summary["total_hunks"] == 1
    assert git(repo, "status", "--porcelain=v2", "--", "plot.py").startswith("1 D.")
    assert git(repo, "diff", "--cached", "--name-status").strip() == "D\tplot.py"
    assert not (repo / "plot.py").exists()


def test_staging_the_second_hunk_alone_keeps_the_first_unstaged(repo, monkeypatch):
    two_hunk_tree(repo)
    patch_judge(monkeypatch, ScriptedJudge(
        lambda state: "beta" in str(state.get("change") or "")))

    summary = git_stage.stage("stage the beta change", cwd=str(repo))

    assert summary["matched_hunks"] == 1 and summary["matched_files"] == 1
    cached = git(repo, "diff", "--cached", "--unified=0")
    assert "beta feature" in cached and "alpha feature" not in cached
    assert "alpha feature" in git(repo, "diff", "--unified=0")


def test_both_hunks_of_one_file_apply_as_one_patch(repo, monkeypatch):
    two_hunk_tree(repo)
    patch_judge(monkeypatch, ScriptedJudge(lambda state: True))

    summary = git_stage.stage("stage the whole app change", cwd=str(repo))

    assert summary == {
        "staged": True, "whole_files": 0, "matched_files": 1, "total_files": 1,
        "matched_hunks": 2, "total_hunks": 2, "skipped": [], "dry_run": False,
    }
    cached = git(repo, "diff", "--cached", "--unified=0")
    assert "alpha feature" in cached and "beta feature" in cached
    assert git(repo, "diff", "--name-only") == ""


def test_cli_parser_exposes_stage(repo, monkeypatch, capsys):
    from hermes_cli.subcommands.git import build_git_parser, cmd_git

    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command")
    build_git_parser(subcommands, cmd_git=cmd_git)
    args = parser.parse_args(["git", "stage", "stage the parser fix", "--dry-run", "--json"])

    assert args.func is cmd_git
    assert args.instruction == "stage the parser fix"
    assert args.dry_run is True and args.json is True and args.path is None

    summary = {
        "staged": True, "whole_files": 0, "matched_files": 1, "total_files": 2,
        "matched_hunks": 1, "total_hunks": 3, "skipped": [], "dry_run": True,
    }
    monkeypatch.setattr("hermes_cli.git_stage.stage", lambda *a, **k: dict(summary))
    assert cmd_git(args) == 0
    assert json.loads(capsys.readouterr().out) == summary


def test_cli_failure_exits_nonzero(monkeypatch, capsys):
    from hermes_cli.subcommands.git import cmd_git

    def boom(*_args, **_kwargs):
        raise RuntimeError("no judge")

    monkeypatch.setattr("hermes_cli.git_stage.stage", boom)
    args = argparse.Namespace(
        instruction="x", path=None, dry_run=False, json=False, git_action="stage")

    assert cmd_git(args) == 1
    captured = capsys.readouterr()
    assert "git stage failed: no judge" in captured.err


def test_stage_refuses_when_judgment_tools_are_off(repo, monkeypatch):
    two_hunk_tree(repo)
    monkeypatch.setattr("agent.judgment.find.judgment_tools_available", lambda: False)
    with pytest.raises(RuntimeError, match="judgment tools are unavailable"):
        git_stage.stage("stage the alpha change", cwd=str(repo))
    assert git(repo, "diff", "--cached").strip() == ""
