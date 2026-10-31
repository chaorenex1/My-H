"""Extensions / theme / settings panels: hooks allowlist, installed skins and the flat schema."""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.gateway.session_panel_helpers import panel_authority


def _write_config(home, text: str):
    (home / "config.yaml").write_text(text, encoding="utf-8")


@pytest_asyncio.fixture
async def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with panel_authority(tmp_path) as conn:
        yield conn


@pytest.mark.asyncio
async def test_hooks_list_and_toggle_the_allowlist(connection, tmp_path, monkeypatch):
    _write_config(tmp_path, "hooks:\n  pre_tool_call:\n    - command: echo hi\n")
    from agent import shell_hooks

    monkeypatch.setattr(shell_hooks, "sync_from_config", lambda _load: None)

    listed = await connection.dispatch(
        {"id": 1, "method": "hooks.list", "params": {}})
    assert "error" not in listed, listed
    hooks = listed["result"]["hooks"]
    assert [(row["event"], row["name"], row["enabled"]) for row in hooks] == [
        ("pre_tool_call", "pre_tool_call:echo hi", False)]

    enabled = await connection.dispatch({"id": 2, "method": "hooks.toggle", "params": {
        "name": "pre_tool_call:echo hi", "enabled": True, "request_id": "r1"}})
    assert "error" not in enabled, enabled
    assert enabled["result"] == {"name": "pre_tool_call:echo hi", "enabled": True}
    assert shell_hooks.allowlist_entry_for("pre_tool_call", "echo hi") is not None

    again = await connection.dispatch(
        {"id": 3, "method": "hooks.list", "params": {}})
    assert again["result"]["hooks"][0]["enabled"] is True

    disabled = await connection.dispatch({"id": 4, "method": "hooks.toggle", "params": {
        "name": "pre_tool_call:echo hi", "enabled": False, "request_id": "r2"}})
    assert disabled["result"]["enabled"] is False
    assert shell_hooks.allowlist_entry_for("pre_tool_call", "echo hi") is None


@pytest.mark.asyncio
async def test_hooks_toggle_refuses_an_unknown_hook(connection, tmp_path):
    _write_config(tmp_path, "hooks:\n  pre_tool_call:\n    - command: echo hi\n")
    reply = await connection.dispatch({"id": 1, "method": "hooks.toggle", "params": {
        "name": "pre_tool_call:missing", "enabled": True, "request_id": "r1"}})
    assert reply["error"]["message"] == "invalid_params"
    assert reply["error"]["data"]["field"] == "name"


@pytest.mark.asyncio
async def test_skin_list_reports_the_active_palette(connection, tmp_path):
    _write_config(tmp_path, "display:\n  skin: my-h\n")
    reply = await connection.dispatch(
        {"id": 1, "method": "skin.list", "params": {}})
    assert "error" not in reply, reply
    assert reply["result"]["current"] == "my-h"
    skins = {row["name"]: row for row in reply["result"]["skins"]}
    assert {"default", "my-h"} <= set(skins)
    assert skins["default"]["colors"]["banner_title"]
    assert skins["default"]["light"] and skins["default"]["dark"] == {}


@pytest.mark.asyncio
async def test_config_schema_has_typed_settings_and_enums(connection):
    reply = await connection.dispatch(
        {"id": 1, "method": "config.schema", "params": {}})
    assert "error" not in reply, reply
    settings = {row["key"]: row for row in reply["result"]["settings"]}
    assert settings["model"]["type"] == "string"
    assert settings["agent.gateway_timeout"]["type"] == "number"
    backend = settings["terminal.backend"]
    assert backend["type"] == "select"
    assert backend["enum"] and "local" in backend["enum"]
    assert all(row["scope"] == "profile" for row in settings.values())
