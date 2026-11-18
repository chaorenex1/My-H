"""``auth.accounts`` maps the provider credential pool to non-secret account rows."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from agent import credential_pool
from tests.gateway.session_panel_helpers import panel_authority
from tui_gateway.contracts.registry import CANONICAL_METHODS


class FakePool:
    def __init__(self, entries, active=None):
        self._entries = entries
        self._active = active

    def entries(self):
        return list(self._entries)

    def peek(self):
        return self._active


def _entry(entry_id, *, label, source, auth_type="api_key", provider="openai"):
    # Tokens are present so the test can prove they never reach the result.
    return SimpleNamespace(id=entry_id, provider=provider, label=label, source=source,
                           auth_type=auth_type, access_token="sk-secret", refresh_token="rt-secret")


@pytest_asyncio.fixture
async def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with panel_authority(tmp_path) as conn:
        yield conn


@pytest.fixture
def pool(monkeypatch):
    def install(fake):
        monkeypatch.setattr(credential_pool, "load_pool", lambda provider: fake)
        return fake

    return install


@pytest.mark.asyncio
async def test_accounts_project_the_pool_without_tokens(connection, pool):
    first = _entry("c1", label="work", source="manual")
    second = _entry("c2", label="", source="env", auth_type="oauth")
    pool(FakePool([first, second], active=second))

    reply = await connection.dispatch({
        "id": 1, "method": "auth.accounts", "params": {"provider_id": "openai"}})
    assert "error" not in reply, reply
    CANONICAL_METHODS["auth.accounts"].result.model_validate(reply["result"])
    assert reply["result"]["accounts"] == [
        {"credential_id": "c1", "provider": "openai", "label": "work", "detail": "work",
         "type": "api_key", "active": False},
        {"credential_id": "c2", "provider": "openai", "label": "", "detail": "env",
         "type": "oauth", "active": True},
    ]
    assert "secret" not in json.dumps(reply["result"])


@pytest.mark.asyncio
async def test_accounts_of_an_empty_pool_are_empty(connection, pool):
    pool(FakePool([], active=None))
    reply = await connection.dispatch({
        "id": 1, "method": "auth.accounts", "params": {"provider_id": "openai"}})
    assert "error" not in reply, reply
    assert reply["result"] == {"accounts": []}


@pytest.mark.asyncio
async def test_accounts_requires_a_provider(connection):
    for params, field in (({}, "provider_id"), ({"provider_id": ""}, "provider_id"),
                          ({"provider_id": "   "}, "provider_id")):
        reply = await connection.dispatch({"id": 1, "method": "auth.accounts", "params": params})
        assert reply["error"]["message"] == "invalid_params", reply
        data = reply["error"]["data"]
        assert field in data.get("fields", []) or data.get("field") == field, reply


@pytest.mark.asyncio
async def test_accounts_rejects_an_unknown_param_before_the_handler(connection):
    reply = await connection.dispatch({
        "id": 1, "method": "auth.accounts", "params": {"provider_id": "openai", "bogus": 1}})
    assert reply["error"]["message"] == "invalid_params"
    assert "bogus" in reply["error"]["data"]["fields"]


@pytest.mark.asyncio
async def test_accounts_is_a_canonical_verb_the_authority_serves(connection):
    assert "auth.accounts" in CANONICAL_METHODS
    assert "auth.accounts" in connection.handlers()
