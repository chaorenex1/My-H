"""The classic CLI's ``/add-dir`` ``/remove-dir`` ``/dirs`` handlers."""

from types import SimpleNamespace

import pytest

from agent import workspace_dirs as wd
from hermes_cli.cli_commands_dirs import CLIWorkspaceDirsMixin


class FakeDB:
    def __init__(self, roots=None):
        self.roots = list(roots or [])
        self.saved = {}

    def get_session_workspace_roots(self, session_id):
        return list(self.roots)

    def update_session_workspace_roots(self, session_id, roots):
        self.saved[session_id] = list(roots)
        self.roots = list(roots)
        return True


class _Stub(CLIWorkspaceDirsMixin):
    def __init__(self, db):
        self.session_id = "sess-cli"
        self._session_db = db
        self.invalidations = []
        self.agent = SimpleNamespace(_invalidate_system_prompt=lambda: self.invalidations.append(1))


@pytest.fixture(autouse=True)
def _clean_cache():
    wd.clear_cache()
    yield
    wd.clear_cache()


@pytest.fixture()
def dirs(tmp_path):
    cwd = tmp_path / "repo"
    extra = tmp_path / "shared"
    cwd.mkdir()
    extra.mkdir()
    return cwd, extra


def test_add_dir_prints_the_listing_and_invalidates_the_prompt(dirs, capsys):
    _cwd, extra = dirs
    stub = _Stub(FakeDB())

    stub._handle_add_dir_command(f"/add-dir {extra}")

    out = capsys.readouterr().out
    assert f"Added {extra}" in out
    assert "Workspace directories:" in out
    assert stub.invalidations == [1]
    assert stub._session_db.saved["sess-cli"] == [str(extra)]


def test_dirs_lists_the_working_directory_without_a_prompt_rebuild(dirs, capsys):
    _cwd, extra = dirs
    stub = _Stub(FakeDB([str(extra)]))

    stub._handle_dirs_command("/dirs")

    out = capsys.readouterr().out
    assert str(extra) in out
    assert "(working directory)" in out
    assert stub.invalidations == []


def test_remove_dir_invalidates_only_when_a_root_was_removed(dirs, capsys):
    _cwd, extra = dirs
    stub = _Stub(FakeDB([str(extra)]))

    stub._handle_remove_dir_command(f"/remove-dir {extra}")
    stub._handle_remove_dir_command(f"/remove-dir {extra}")

    out = capsys.readouterr().out
    assert f"Removed {extra}" in out
    assert "Not a workspace directory" in out
    assert stub.invalidations == [1]


def test_a_rejected_path_changes_nothing(dirs, capsys):
    cwd, _extra = dirs
    stub = _Stub(FakeDB())

    stub._handle_add_dir_command(f"/add-dir {cwd / 'missing'}")

    out = capsys.readouterr().out
    assert "Directory does not exist" in out
    assert stub.invalidations == []
    assert stub._session_db.saved == {}
