"""A malformed nested ``.git`` must not cost a project its checkpoints.

git aborts the whole ``git add -A`` when a work-tree directory has a ``.git``
that is not a usable repository (an empty dir, a dead gitfile). Without
recovery every snapshot of that project fails silently, so ``/rollback`` has
nothing to offer.
"""
import os
import subprocess

import pytest

from tools import checkpoint_manager as cm
from utils import rmtree_readonly


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", tmp_path / "checkpoints")
    # process-wide cache: keep one test's skipped path out of the next one
    monkeypatch.setattr(cm, "_MALFORMED_NESTED_SKIPS", {})
    work = tmp_path / "workspace"
    work.mkdir()
    (work / "notes.txt").write_text("before\n")
    tool = work / "tool"
    tool.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (["init", "-q"], ["config", "user.name", "Test"],
                 ["config", "user.email", "test@example.invalid"]):
        subprocess.run(["git", *args], cwd=tool, env=env, check=True, capture_output=True)
    (tool / "main.py").write_text("nested\n")
    subprocess.run(["git", "add", "."], cwd=tool, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tool, env=env, check=True,
                   capture_output=True)
    mgr = cm.CheckpointManager(enabled=True)
    assert mgr.ensure_checkpoint(str(work), "baseline") is True
    # the nested repo was captured as a gitlink; now its .git is debris
    rmtree_readonly(str(tool / ".git"))
    (tool / ".git").mkdir()
    mgr.new_turn()
    return mgr, work, tool


def tree_paths(work, commit):
    ok, out, err = cm._run_git(["ls-tree", "-r", "-z", commit], cm._store_path(), str(work))
    assert ok, err
    return [record.split("\t", 1)[1] for record in out.split("\x00") if "\t" in record]


def test_broken_nested_git_does_not_discard_the_snapshot(manager):
    mgr, work, _ = manager
    (work / "notes.txt").write_text("after\n")
    assert mgr.ensure_checkpoint(str(work), "edit") is True
    checkpoints = mgr.list_checkpoints(str(work))
    assert len(checkpoints) == 2
    assert "notes.txt" in tree_paths(work, checkpoints[0]["hash"])


def test_later_snapshot_still_detects_changes(manager):
    mgr, work, _ = manager
    (work / "notes.txt").write_text("after\n")
    assert mgr.ensure_checkpoint(str(work), "edit") is True
    checkpoints = mgr.list_checkpoints(str(work))
    result = mgr.diff(str(work), checkpoints[-1]["hash"])
    assert result["success"] is True, result
    assert "notes.txt" in result["diff"]


@pytest.mark.parametrize("stderr, expected", [
    ("fatal: 'broken/.git' not recognized as a git repository\n", "broken"),
    ("fatal: 'a/b/.git' not recognized as a git repository\n", "a/b"),
    ("fatal: 'a\\b\\.git' not recognized as a git repository\n", "a/b"),
    ("fatal: 'C:/x/.git' not recognized as a git repository\n", None),
    ("fatal: '../x/.git' not recognized as a git repository\n", None),
    ("fatal: something else entirely\n", None),
    ("", None),
])
def test_malformed_nested_git_path(stderr, expected):
    assert cm._malformed_nested_git_path(stderr) == expected


def test_recovered_path_is_remembered(manager):
    """One failing add per work tree per process, not one per snapshot."""
    mgr, work, _ = manager
    (work / "notes.txt").write_text("after\n")
    assert mgr.ensure_checkpoint(str(work), "edit") is True
    skipped = cm._MALFORMED_NESTED_SKIPS.get(str(cm._normalize_path(str(work))))
    assert skipped == {"tool"}
    store = cm._store_path()
    index = cm._index_path(store, cm._project_hash(str(work)))
    ok, _, err = cm._stage_all(store, str(work), index)
    assert ok, err
