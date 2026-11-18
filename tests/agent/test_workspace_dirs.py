"""Additional workspace roots: ``/add-dir``, ``/remove-dir`` and ``/dirs``.

The shared store owns path validation, dedupe and ordering; these drive it directly (the CLI
and gateway handlers are thin wrappers over the same functions).
"""

import os

import pytest

from agent import workspace_dirs as wd


class FakeDB:
    """Minimal ``SessionDB`` surface the store uses (read + persist)."""

    def __init__(self, roots=None, *, fail_write=False):
        self.roots = list(roots or [])
        self.reads = 0
        self.saved = {}
        self.fail_write = fail_write

    def get_session_workspace_roots(self, session_id):
        self.reads += 1
        return list(self.roots)

    def update_session_workspace_roots(self, session_id, roots):
        if self.fail_write:
            raise RuntimeError("store unavailable")
        self.saved[session_id] = list(roots)
        self.roots = list(roots)
        return True


@pytest.fixture(autouse=True)
def _clean_cache():
    wd.clear_cache()
    yield
    wd.clear_cache()


@pytest.fixture()
def work(tmp_path):
    cwd = tmp_path / "repo"
    extra = tmp_path / "shared"
    cwd.mkdir()
    extra.mkdir()
    return cwd, extra


def _same(left, right):
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def test_add_dir_persists_and_lists_every_root(work):
    cwd, extra = work
    db = FakeDB()

    result = wd.add_dir("sess-1", str(extra), cwd=cwd, db=db)

    assert result.changed
    assert f"Added {extra}" in result.text
    assert wd.load_roots("sess-1", db=db) == (str(extra),)
    assert db.saved["sess-1"] == [str(extra)]
    listing = wd.list_dirs("sess-1", cwd=cwd, db=db)
    assert "Workspace directories:" in listing
    assert str(extra) in listing
    assert "(working directory)" in listing


def test_relative_path_resolves_against_the_working_directory(work):
    cwd, _extra = work
    nested = cwd / "nested"
    nested.mkdir()

    result = wd.add_dir("sess-1", "nested", cwd=cwd)

    assert result.changed
    assert _same(wd.load_roots("sess-1")[0], nested)


def test_add_dir_rejects_a_missing_directory(work):
    cwd, _extra = work

    result = wd.add_dir("sess-1", str(cwd / "nope"), cwd=cwd)

    assert not result.changed
    assert "does not exist" in result.text
    assert wd.load_roots("sess-1") == ()


def test_add_dir_rejects_the_working_directory(work):
    cwd, _extra = work

    result = wd.add_dir("sess-1", str(cwd), cwd=cwd)

    assert not result.changed
    assert "already this session's primary workspace root" in result.text


def test_add_dir_is_idempotent(work):
    cwd, extra = work
    db = FakeDB()
    wd.add_dir("sess-1", str(extra), cwd=cwd, db=db)

    result = wd.add_dir("sess-1", str(extra), cwd=cwd, db=db)

    assert not result.changed
    assert "Already in the workspace" in result.text
    assert wd.load_roots("sess-1", db=db) == (str(extra),)


def test_remove_dir_drops_only_the_named_root(work):
    cwd, extra = work
    second = cwd.parent / "second"
    second.mkdir()
    db = FakeDB()
    wd.add_dir("sess-1", str(extra), cwd=cwd, db=db)
    wd.add_dir("sess-1", str(second), cwd=cwd, db=db)

    result = wd.remove_dir("sess-1", str(extra), cwd=cwd, db=db)

    assert result.changed
    assert wd.load_roots("sess-1", db=db) == (str(second),)
    assert db.saved["sess-1"] == [str(second)]


def test_remove_dir_refuses_the_working_directory(work):
    cwd, _extra = work

    result = wd.remove_dir("sess-1", str(cwd), cwd=cwd)

    assert not result.changed
    assert "Cannot remove the working directory" in result.text


def test_remove_dir_reports_an_unknown_root(work):
    cwd, extra = work

    result = wd.remove_dir("sess-1", str(extra), cwd=cwd)

    assert not result.changed
    assert "Not a workspace directory" in result.text


def test_usage_without_an_argument_lists_the_current_roots(work):
    cwd, extra = work
    db = FakeDB()
    wd.add_dir("sess-1", str(extra), cwd=cwd, db=db)

    result = wd.add_dir("sess-1", "", cwd=cwd, db=db)

    assert not result.changed
    assert "Usage: /add-dir <path>" in result.text
    assert str(extra) in result.text


def test_prompt_roots_skip_the_working_directory(work):
    cwd, extra = work
    db = FakeDB([str(cwd), str(extra)])

    assert wd.prompt_roots("sess-1", db=db, cwd=cwd) == [str(extra)]


def test_roots_load_from_the_store_once_per_process(work):
    _cwd, extra = work
    db = FakeDB([str(extra)])

    wd.load_roots("sess-1", db=db)
    wd.load_roots("sess-1", db=db)

    assert db.reads == 1


def test_a_failed_persist_keeps_the_live_session_state(work):
    cwd, extra = work
    db = FakeDB(fail_write=True)

    result = wd.add_dir("sess-1", str(extra), cwd=cwd, db=db)

    assert result.changed
    assert wd.load_roots("sess-1", db=db) == (str(extra),)


def test_remote_backend_keeps_the_declared_container_path(monkeypatch):
    monkeypatch.setattr("tools.file_tools_paths._terminal_env_type_for_task", lambda task_id="default": "docker")

    result = wd.add_dir("sess-1", "/workspace/shared", cwd="/workspace/repo")

    assert result.changed
    assert wd.load_roots("sess-1") == ("/workspace/shared",)
