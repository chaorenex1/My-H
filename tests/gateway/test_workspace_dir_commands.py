"""The gateway's ``/add-dir`` ``/remove-dir`` ``/dirs`` handlers (local sessions only)."""

import asyncio
from types import SimpleNamespace

import pytest

from agent import workspace_dirs as wd
from gateway.config import Platform
from gateway.slash_commands_dirs import GatewayWorkspaceDirsMixin


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


class _Handle:
    """Stands in for the runner's AsyncSessionDB wrapper."""

    def __init__(self, db):
        self._db = db


class _Runner(GatewayWorkspaceDirsMixin):
    def __init__(self, db):
        self._session_db = _Handle(db)
        self.evicted = []

    def _session_key_for_source(self, source):
        return "route:1"

    def _evict_cached_agent(self, session_key):
        self.evicted.append(session_key)


def _event(platform, chat_id, args=""):
    return SimpleNamespace(source=SimpleNamespace(platform=platform, chat_id=chat_id),
                           get_command_args=lambda: args)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_cache():
    wd.clear_cache()
    yield
    wd.clear_cache()


@pytest.fixture()
def dirs(tmp_path):
    extra = tmp_path / "shared"
    extra.mkdir()
    return tmp_path, extra


def test_add_dir_evicts_the_cached_agent_so_the_next_turn_lists_the_root(dirs):
    _cwd, extra = dirs
    runner = _Runner(FakeDB())

    reply = _run(runner._handle_add_dir_command(_event(Platform.LOCAL, "sess-gw", str(extra))))

    assert f"Added {extra}" in reply
    assert runner.evicted == ["route:1"]
    assert runner._session_db._db.saved["sess-gw"] == [str(extra)]


def test_dirs_answers_from_the_session_store_without_an_eviction(dirs):
    _cwd, extra = dirs
    runner = _Runner(FakeDB([str(extra)]))

    reply = _run(runner._handle_dirs_command(_event(Platform.LOCAL, "sess-gw")))

    assert str(extra) in reply
    assert runner.evicted == []


def test_remove_dir_evicts_only_on_a_real_change(dirs):
    _cwd, extra = dirs
    runner = _Runner(FakeDB([str(extra)]))

    _run(runner._handle_remove_dir_command(_event(Platform.LOCAL, "sess-gw", str(extra))))
    _run(runner._handle_remove_dir_command(_event(Platform.LOCAL, "sess-gw", str(extra))))

    assert runner.evicted == ["route:1"]


def test_a_messaging_chat_gets_a_local_only_refusal(dirs):
    _cwd, extra = dirs
    db = FakeDB()
    runner = _Runner(db)

    reply = _run(runner._handle_add_dir_command(_event(Platform.TELEGRAM, "chat-1", str(extra))))

    assert "local session's workspace" in reply
    assert db.saved == {}
    assert runner.evicted == []
