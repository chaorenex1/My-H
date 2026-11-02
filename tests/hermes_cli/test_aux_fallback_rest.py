"""``GET /api/model/auxiliary`` and ``POST /api/model/set`` (scope="auxiliary") over a chain.

The Models page reads one row per slot: built-ins now carry their ``label``/``hint``/``group``
(previously labelled client-side) plus the slot's ``fallback_chain``, and the write path accepts
the same field with omitted / ``[]`` / replace semantics. Entries are validated up front
(provider + model required, cap 8) so a dead route never reaches the runtime chain reader.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

NEW_SLOTS = ("side_question", "commit_message", "kanban_estimator")


@pytest.fixture
def client(monkeypatch):
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    # No pricing lookups on a model assignment (the guard is skipped with confirm_expensive_model,
    # but a stray non-confirmed body must never reach models.dev in a test).
    monkeypatch.setattr("hermes_cli.model_cost_guard.expensive_model_warning", lambda *_a, **_k: None)
    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def _rows(client: TestClient) -> dict[str, dict]:
    resp = client.get("/api/model/auxiliary")
    assert resp.status_code == 200, resp.text
    return {row["task"]: row for row in resp.json()["tasks"]}


def _set(client: TestClient, **body):
    return client.post("/api/model/set", json={"scope": "auxiliary", **body})


def test_read_rows_carry_labels_and_an_empty_chain_for_every_new_slot(client):
    rows = _rows(client)
    assert set(NEW_SLOTS) <= set(rows)
    vision = rows["vision"]
    assert (vision["label"], vision["hint"], vision["group"]) == (
        "Vision", "image/screenshot analysis", "builtin")
    assert vision["fallback_chain"] == []
    assert rows["commit_message"]["label"] == "Commit message"
    for slot in NEW_SLOTS:
        assert (rows[slot]["provider"], rows[slot]["fallback_chain"]) == ("auto", [])


def test_write_persists_a_validated_chain_and_the_read_returns_it(client):
    resp = _set(client, provider="nous", model="hermes-4", task="commit_message",
                fallback_chain=[{"provider": "openrouter", "model": "google/gemini-3.6-flash",
                                 "base_url": ""},
                                {"provider": "zai", "model": "glm-5", "base_url": "https://edge/v1"}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["fallback_chain"] == [
        {"provider": "openrouter", "model": "google/gemini-3.6-flash"},
        {"provider": "zai", "model": "glm-5", "base_url": "https://edge/v1"},
    ]
    assert _rows(client)["commit_message"]["fallback_chain"] == resp.json()["fallback_chain"]

    # Omitted → the stored chain is left alone by an unrelated re-assignment.
    resp = _set(client, provider="nous", model="hermes-4-mini", task="commit_message")
    assert resp.status_code == 200, resp.text
    assert "fallback_chain" not in resp.json()
    assert len(_rows(client)["commit_message"]["fallback_chain"]) == 2


def test_an_entry_without_a_model_is_rejected_with_400_and_nothing_is_persisted(client):
    _set(client, provider="nous", model="hermes-4", task="commit_message",
         fallback_chain=[{"provider": "openrouter", "model": "keep-me"}])

    resp = _set(client, provider="nous", model="hermes-4", task="commit_message",
                fallback_chain=[{"provider": "openrouter", "model": "fine"},
                                {"provider": "zai", "model": "  "}])
    assert resp.status_code == 400, resp.text
    assert "fallback_chain[1]" in resp.json()["detail"]
    assert _rows(client)["commit_message"]["fallback_chain"] == [
        {"provider": "openrouter", "model": "keep-me"}]


def test_an_empty_list_clears_and_reset_clears_too(client):
    chain = [{"provider": "nous", "model": "hermes-4"}]
    _set(client, provider="nous", model="hermes-4", task="commit_message", fallback_chain=chain)
    assert _rows(client)["commit_message"]["fallback_chain"] == chain

    resp = _set(client, provider="nous", model="hermes-4", task="commit_message", fallback_chain=[])
    assert resp.status_code == 200, resp.text
    assert _rows(client)["commit_message"]["fallback_chain"] == []

    _set(client, provider="nous", model="hermes-4", task="commit_message", fallback_chain=chain)
    resp = _set(client, provider="", model="", task="__reset__")
    assert resp.status_code == 200, resp.text
    reset_row = _rows(client)["commit_message"]
    assert (reset_row["provider"], reset_row["fallback_chain"]) == ("auto", [])
