"""``btw.history`` reads the side-store the gateway appends /btw answers to."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from gateway import session_btw_history
from tests.gateway.session_panel_helpers import panel_authority


@pytest_asyncio.fixture
async def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with panel_authority(tmp_path) as conn:
        yield conn


@pytest.mark.asyncio
async def test_history_returns_oldest_first_and_honours_limit(connection):
    session_btw_history.record("s", "btw_1", "first?", "first answer")
    session_btw_history.record("s", "btw_2", "second?", "second answer")
    session_btw_history.record("s", "btw_3", "third?", "third answer")

    reply = await connection.dispatch(
        {"id": 1, "method": "btw.history", "params": {"session_id": "s", "limit": 2}})
    assert "error" not in reply, reply
    items = reply["result"]["items"]
    assert [(item["question"], item["answer"]) for item in items] == [
        ("second?", "second answer"), ("third?", "third answer")]
    assert items[-1]["task_id"] == "btw_3"
    assert items[-1]["created_at"].endswith("Z")


@pytest.mark.asyncio
async def test_history_skips_malformed_lines(connection, tmp_path):
    path = session_btw_history._store_path("s")
    assert path == tmp_path / "session" / "btw" / "s.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"id": "a1", "question": "q", "answer": "a", "created_at": "2026-01-01T00:00:00Z"}
    path.write_text("not json\n" + json.dumps(entry) + "\n", encoding="utf-8")

    reply = await connection.dispatch(
        {"id": 1, "method": "btw.history", "params": {"session_id": "s"}})
    assert [item["id"] for item in reply["result"]["items"]] == ["a1"]


@pytest.mark.asyncio
async def test_history_rejects_a_bad_limit(connection):
    reply = await connection.dispatch(
        {"id": 1, "method": "btw.history", "params": {"session_id": "s", "limit": 0}})
    assert reply["error"]["message"] == "invalid_params"
    assert reply["error"]["data"]["field"] == "limit"


@pytest.mark.asyncio
async def test_history_requires_session_access(connection):
    session_btw_history.record("other", "btw_1", "secret?", "secret answer")
    reply = await connection.dispatch(
        {"id": 1, "method": "btw.history", "params": {"session_id": "other"}})
    assert reply["error"]["message"] == "not_found"
