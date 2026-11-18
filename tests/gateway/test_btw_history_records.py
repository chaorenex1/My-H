"""``btw.history`` records: the panel's newest-first rows over the same stored answers."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
import pytest_asyncio

from gateway import session_btw_history
from tests.gateway.session_panel_helpers import panel_authority
from tui_gateway.contracts.registry import CANONICAL_METHODS


@pytest_asyncio.fixture
async def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with panel_authority(tmp_path) as conn:
        yield conn


def _millis(created_at: str) -> int:
    return int(datetime.fromisoformat(created_at).timestamp() * 1000)


@pytest.mark.asyncio
async def test_records_reverse_the_store_and_carry_epoch_millis(connection):
    session_btw_history.record("s", "btw_1", "first?", "first answer")
    session_btw_history.record("s", "btw_2", "second?", "second answer")
    session_btw_history.record("s", "btw_3", "third?", "third answer")

    reply = await connection.dispatch(
        {"id": 1, "method": "btw.history", "params": {"session_id": "s"}})
    assert "error" not in reply, reply
    CANONICAL_METHODS["btw.history"].result.model_validate(reply["result"])

    items = reply["result"]["items"]
    records = reply["result"]["records"]
    assert [item["question"] for item in items] == ["first?", "second?", "third?"]
    assert [record["question"] for record in records] == ["third?", "second?", "first?"]
    assert [record["task_id"] for record in records] == ["btw_3", "btw_2", "btw_1"]
    assert {record["status"] for record in records} == {"completed"}
    for record in records:
        assert record["created_at_ms"] == _millis(record["created_at"])


@pytest.mark.asyncio
async def test_records_honour_the_same_limit_as_items(connection):
    session_btw_history.record("s", "btw_1", "first?", "first answer")
    session_btw_history.record("s", "btw_2", "second?", "second answer")
    session_btw_history.record("s", "btw_3", "third?", "third answer")

    reply = await connection.dispatch(
        {"id": 1, "method": "btw.history", "params": {"session_id": "s", "limit": 2}})
    assert [item["task_id"] for item in reply["result"]["items"]] == ["btw_2", "btw_3"]
    assert [record["task_id"] for record in reply["result"]["records"]] == ["btw_3", "btw_2"]


@pytest.mark.asyncio
async def test_records_leave_an_unparsable_time_null(connection):
    path = session_btw_history._store_path("s")
    path.parent.mkdir(parents=True, exist_ok=True)
    bad = {"id": "bad", "question": "q", "answer": "a", "created_at": "not-a-date"}
    path.write_text(json.dumps(bad) + "\n", encoding="utf-8")

    reply = await connection.dispatch(
        {"id": 1, "method": "btw.history", "params": {"session_id": "s"}})
    assert "error" not in reply, reply
    record = reply["result"]["records"][0]
    assert record["created_at_ms"] is None
    assert record["created_at"] == "not-a-date"


def test_records_without_a_time_are_null():
    record = session_btw_history._records([{"id": "missing", "question": "q", "answer": "a"}])[0]
    assert record["created_at"] is None
    assert record["created_at_ms"] is None
