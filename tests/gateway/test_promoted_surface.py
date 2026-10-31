"""Every P1-promoted verb is answered by the authority itself, never the legacy fallback.

The wire's compat fallback keys on ``-32601`` (``tui_gateway/ws.py``), so a promoted name that the
authority does not know would silently route a new client to the sidecar. This gate dispatches each
name with an undeclared key: the canonical dispatcher must refuse it as ``4001 invalid_params``
before any handler runs — proof the authority owns the name — and never answer ``-32601``.
"""
from types import SimpleNamespace

import pytest
import pytest_asyncio

import gateway.run  # preload the scope helper before the per-test HOME guard
from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch
from tui_gateway.contracts.canonical_promoted import PROMOTED
from tui_gateway.contracts.registry import CANONICAL_METHODS


@pytest_asyncio.fixture
async def owner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s", source="test")
        epoch = begin_runtime_epoch(db, instance_id="current")
        authority = SessionAuthority(SimpleNamespace(_draining=False, _cached_agent_for=lambda key: None),
                                     profile_id=str(tmp_path), instance_id="current", db=db, epoch=epoch)
        authority.sessions["s"] = LiveSession(None, "route")
        connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None),
                                         {"user_id": "owner"})
        try:
            yield connection
        finally:
            await connection.close()
            authority.db.close()


def test_every_promoted_verb_has_a_canonical_contract_and_handler(owner):
    connection = owner
    served = set(connection.handlers())
    assert [name for name in PROMOTED if name not in CANONICAL_METHODS] == []
    assert [name for name in PROMOTED if name not in served] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", PROMOTED)
async def test_promoted_verb_never_falls_back(owner, method):
    connection = owner
    reply = await connection.dispatch({"id": 1, "method": method, "params": {"__undeclared__": 1}})
    assert reply["error"]["code"] == 4001, (method, reply)
    assert reply["error"]["data"]["reason"] == "invalid_params", (method, reply)
    assert "__undeclared__" in reply["error"]["data"]["fields"], (method, reply)
