"""``connectors.list``: the one connector verb the in-process adapter cannot serve.

Account-owned calls read the profile's managed connections; session-owned calls ask that live
session's ``manage_connections`` tool. Both answer the same ``ConnectorsListResult``.
"""
import contextlib
from types import SimpleNamespace

import pytest
import pytest_asyncio

import gateway.run
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch
from tui_gateway.contracts.registry import CANONICAL_METHODS

CONNECTOR_ROW = {
    "connector": "github", "enabled": True, "connected": True,
    "connectionStatus": "active", "statusReason": None, "disabledTools": [],
}


@pytest_asyncio.fixture
async def owner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def scope_check():
        from gateway.session_context import get_session_env
        from hermes_constants import get_hermes_home

        # A single-profile owner keeps the ambient home; only the desktop page vars are bound.
        assert get_hermes_home() == tmp_path
        assert get_session_env("HERMES_SESSION_SOURCE") == "desktop"

    import tools.connectors
    import tools.managed_tool_gateway as managed

    class _Client:
        def list_connectors(self):
            scope_check()
            return [dict(CONNECTOR_ROW)]

    seen = []
    from tools.connectors import managed as connectors_managed
    from hermes_cli import free_tier_offer

    monkeypatch.setattr(tools.connectors, "connectors_available", lambda: True)
    monkeypatch.setattr(managed, "read_nous_access_token", lambda: "token")
    monkeypatch.setattr(connectors_managed, "managed_client", lambda *, creates_guest: _Client())
    monkeypatch.setattr(free_tier_offer, "note_connected_apps_seen", lambda rows: seen.extend(rows))

    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s", source="test")
        epoch = begin_runtime_epoch(db, instance_id="current")
        authority = SessionAuthority(SimpleNamespace(_draining=False, _cached_agent_for=lambda key: None),
                                     profile_id=str(tmp_path), instance_id="current", db=db, epoch=epoch)
        authority.sessions["s"] = LiveSession(None, "route")
        connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None),
                                         {"user_id": "owner"})
        try:
            yield SimpleNamespace(connection=connection, seen=seen)
        finally:
            await connection.close()
            authority.db.close()


async def _call(connection, params):
    reply = await connection.dispatch({"id": 1, "method": "connectors.list", "params": params})
    assert "error" not in reply, reply
    CANONICAL_METHODS["connectors.list"].result.model_validate(reply["result"])
    return reply["result"]


@pytest.mark.asyncio
async def test_account_owned_list_reads_the_managed_page(owner):
    result = await _call(owner.connection, {"owner": {"type": "account"}})
    assert result["available"] is True
    assert [row["connector"] for row in result["connectors"]] == ["github"]
    assert owner.seen and owner.seen[0]["connector"] == "github"


@pytest.mark.asyncio
async def test_session_owned_list_runs_manage_connections(owner, monkeypatch):
    import model_tools
    from gateway import session_connections

    calls = []

    def handle(name, args, **kwargs):
        calls.append((name, args, kwargs))
        return {"connectors": [dict(CONNECTOR_ROW)], "available": True}

    monkeypatch.setattr(model_tools, "handle_function_call", handle)
    monkeypatch.setattr(session_connections, "_require_connectors", lambda authority, ref: None)
    monkeypatch.setattr(session_connections, "_session_toolsets", lambda authority, ref: (None, None))
    monkeypatch.setattr(session_connections, "_session_context",
                        lambda authority, ref: contextlib.nullcontext())

    result = await _call(owner.connection, {"owner": {"type": "session", "session_id": "s"}})
    assert result["available"] is True
    assert result["connectors"][0]["connector"] == "github"
    assert calls[0][0] == "manage_connections" and calls[0][1] == {"action": "status"}


@pytest.mark.asyncio
async def test_closed_gate_answers_status_without_connectors(owner, monkeypatch):
    import tools.connectors

    monkeypatch.setattr(tools.connectors, "connectors_available", lambda: False)
    result = await _call(owner.connection, {"owner": {"type": "account"}})
    assert result == {"available": False, "connectors": []}


@pytest.mark.asyncio
async def test_unknown_key_is_refused_before_the_handler(owner):
    reply = await owner.connection.dispatch({"id": 1, "method": "connectors.list",
                                             "params": {"owner": {"type": "account"}, "bogus": 1}})
    assert reply["error"]["code"] == 4001
    assert reply["error"]["data"]["fields"] == ["bogus"]
