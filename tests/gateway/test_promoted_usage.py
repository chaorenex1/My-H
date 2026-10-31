"""The session-bound usage verb the authority must serve itself."""
from types import SimpleNamespace

import pytest
import pytest_asyncio

import gateway.run  # preload the scope helper before the per-test HOME guard
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch
from tui_gateway.contracts.registry import CANONICAL_METHODS


@pytest_asyncio.fixture
async def owner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("newer", source="telegram")
        epoch = begin_runtime_epoch(db, instance_id="current")
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(tmp_path),
                                     instance_id="current", db=db, epoch=epoch)
        authority.runner._cached_agent_for = lambda key: None
        authority.sessions["newer"] = LiveSession(None, "route")
        connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None),
                                         {"user_id": "owner"})
        try:
            yield connection
        finally:
            await connection.close()
            authority.db.close()


@pytest.mark.asyncio
async def test_session_usage_zeroes_before_an_agent_exists(owner):
    reply = await owner.dispatch({"id": 1, "method": "session.usage", "params": {"session_id": "newer"}})
    assert "error" not in reply, reply
    CANONICAL_METHODS["session.usage"].result.model_validate(reply["result"])
    assert reply["result"]["input_tokens"] == 0 and reply["result"]["output_tokens"] == 0


@pytest.mark.asyncio
async def test_session_usage_refuses_an_unknown_session(owner):
    reply = await owner.dispatch({"id": 1, "method": "session.usage", "params": {"session_id": "nope"}})
    assert reply["error"]["code"] == 4001
    assert reply["error"]["data"]["reason"] == "not_found"
