"""``session.lease`` reads the cross-process lease registry, never this process's live sessions."""

from __future__ import annotations

import pytest
import pytest_asyncio

from hermes_cli.active_sessions import try_acquire_active_session
from tests.gateway.session_panel_helpers import panel_authority
from tui_gateway.contracts.registry import CANONICAL_METHODS


@pytest_asyncio.fixture
async def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with panel_authority(tmp_path) as conn:
        yield conn


def _acquire(home, session_id):
    lease, refusal = try_acquire_active_session(
        session_id=session_id, surface="test", config=None, registry_home=home)
    assert lease is not None, refusal
    return lease


@pytest.mark.asyncio
async def test_lease_reports_held_ids_in_request_order(connection, tmp_path):
    first = _acquire(tmp_path, "s")
    second = _acquire(tmp_path, "other")
    try:
        reply = await connection.dispatch({
            "id": 1, "method": "session.lease",
            "params": {"session_ids": ["other", "missing", "s"]}})
        assert "error" not in reply, reply
        CANONICAL_METHODS["session.lease"].result.model_validate(reply["result"])
        assert reply["result"] == {"held": ["other", "s"], "unknown": []}
    finally:
        first.release()
        second.release()


@pytest.mark.asyncio
async def test_lease_absent_from_a_healthy_registry_is_not_unknown(connection):
    reply = await connection.dispatch({
        "id": 1, "method": "session.lease", "params": {"session_ids": ["s"]}})
    assert "error" not in reply, reply
    assert reply["result"] == {"held": [], "unknown": []}


@pytest.mark.asyncio
async def test_lease_reports_every_id_unknown_when_the_registry_cannot_be_read(connection, tmp_path):
    path = tmp_path / "runtime" / "active_sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    reply = await connection.dispatch({
        "id": 1, "method": "session.lease", "params": {"session_ids": ["s", "other"]}})
    assert "error" not in reply, reply
    assert reply["result"] == {"held": [], "unknown": ["s", "other"]}


@pytest.mark.asyncio
async def test_lease_rejects_a_non_list_or_empty_id(connection):
    for params, field in (
            ({"session_ids": "s"}, "session_ids"),
            ({"session_ids": [""]}, "session_ids"),
            ({"session_ids": ["s", 7]}, "session_ids")):
        reply = await connection.dispatch({"id": 1, "method": "session.lease", "params": params})
        assert reply["error"]["message"] == "invalid_params", reply
        assert reply["error"]["data"]["field"] == field


@pytest.mark.asyncio
async def test_lease_rejects_an_unknown_param_before_the_handler(connection):
    reply = await connection.dispatch({
        "id": 1, "method": "session.lease", "params": {"session_ids": [], "bogus": 1}})
    assert reply["error"]["message"] == "invalid_params"
    assert "bogus" in reply["error"]["data"]["fields"]


@pytest.mark.asyncio
async def test_lease_is_a_canonical_verb_the_authority_serves(connection):
    assert "session.lease" in CANONICAL_METHODS
    assert "session.lease" in connection.handlers()
