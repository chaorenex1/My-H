"""P4.4: worktree auto-apply — merge, dirty parent, conflict, zero-change prune."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tools import subagent_worktree as sw


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=check)


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@test"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / ".gitignore").write_text("", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


@pytest.fixture()
def repo(tmp_path):
    return _make_repo(tmp_path)


def _branch_exists(repo: Path, branch: str) -> bool:
    return bool(_git(["branch", "--list", branch], repo).stdout.strip())


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_auto_apply_commits_dirty_work_and_merges_it_into_the_parent(repo):
    info = sw.create_subagent_worktree(str(repo), "apply1")
    assert info is not None
    (Path(info["path"]) / "child.txt").write_text("from the child\n", encoding="utf-8")

    payload = sw.finalize_subagent_worktree(info, apply="auto")

    assert payload["applied"] is True
    assert payload["pruned"] is True
    assert payload["commits"] == 1
    assert payload["dirty"] is False
    assert payload["merge_commit"]
    assert (repo / "child.txt").read_text(encoding="utf-8") == "from the child\n"
    assert not Path(info["path"]).exists()
    assert not _branch_exists(repo, info["branch"])


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_auto_apply_refuses_a_dirty_parent_and_keeps_the_branch(repo):
    (repo / "user-work.txt").write_text("uncommitted\n", encoding="utf-8")
    info = sw.create_subagent_worktree(str(repo), "dirty-parent")
    assert info is not None and info["parent_clean"] == "false"
    (Path(info["path"]) / "child.txt").write_text("from the child\n", encoding="utf-8")

    payload = sw.finalize_subagent_worktree(info, apply="auto")

    assert payload["applied"] is False
    assert "parent workspace" in payload["apply_error"]
    assert (repo / "user-work.txt").exists()
    assert not (repo / "child.txt").exists()
    assert _branch_exists(repo, info["branch"])


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_auto_apply_conflict_aborts_and_keeps_the_branch(repo):
    info = sw.create_subagent_worktree(str(repo), "conflict1")
    assert info is not None
    # Parent and child edit the same line after the child's base commit.
    (repo / "README.md").write_text("parent\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "parent edit"], repo)
    (Path(info["path"]) / "README.md").write_text("child\n", encoding="utf-8")

    payload = sw.finalize_subagent_worktree(info, apply="auto")

    assert payload["applied"] is False
    assert "merge" in payload["apply_error"]
    assert (repo / "README.md").read_text(encoding="utf-8") == "parent\n"
    status = _git(["status", "--porcelain"], repo).stdout
    assert "UU" not in status, "the failed merge must be aborted, not left in progress"
    assert _branch_exists(repo, info["branch"])


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_auto_apply_with_no_work_prunes_without_a_merge(repo):
    info = sw.create_subagent_worktree(str(repo), "clean1")
    assert info is not None

    payload = sw.finalize_subagent_worktree(info, apply="auto")

    assert payload["applied"] is False
    assert payload["pruned"] is True
    assert payload.get("merge_commit", "") == ""
    assert not Path(info["path"]).exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_a_second_spawn_still_sees_a_clean_parent(repo):
    first = sw.create_subagent_worktree(str(repo), "first")
    assert first is not None
    (Path(first["path"]) / "child.txt").write_text("one\n", encoding="utf-8")
    assert sw.finalize_subagent_worktree(first, apply="auto")["applied"] is True

    second = sw.create_subagent_worktree(str(repo), "second")

    assert second is not None and second["parent_clean"] == "true", (
        "the helper's own .worktrees/ exclusion must not make the parent dirty")


def test_an_unknown_apply_mode_is_refused(repo):
    with pytest.raises(ValueError, match="apply must be"):
        sw.finalize_subagent_worktree({"path": "", "branch": ""}, apply="sometimes")
