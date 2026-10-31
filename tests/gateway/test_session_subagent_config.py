"""Agent Hub role settings: config keyed by (agent_id, source=prompt path) in the profile state.db."""

from __future__ import annotations

import pytest
import pytest_asyncio

from gateway import session_ancillary, session_policy
from tests.gateway.session_panel_helpers import panel_authority, policy


def _definition(path, name, body="Prompt.", **frontmatter):
    lines = ["---", f"name: {name}"]
    lines += [f"{key}: {value}" for key, value in frontmatter.items()]
    lines += ["---", "", body, ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest_asyncio.fixture
async def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    async with panel_authority(tmp_path) as conn:
        yield conn


@pytest.mark.asyncio
async def test_config_round_trips_through_the_state_db(connection, tmp_path):
    prompt = tmp_path / "agents" / "reviewer.md"
    _definition(prompt, "reviewer", model="user/static", reasoning_effort="medium")

    reply = await connection.dispatch({"id": 1, "method": "subagent.config", "params": {
        "subagent_id": "reviewer",
        "model": "google/gemini-3-flash-preview",
        "provider": "openrouter",
        "advisor": True,
        "prewalk": False,
        "thinking": "high",
        "pattern": "review-first",
    }})
    assert "error" not in reply, reply
    result = reply["result"]
    assert result["subagent_id"] == "reviewer"
    assert result["source"] == str(prompt)
    assert result["prompt_path"] == str(prompt)
    assert result["model"] == "google/gemini-3-flash-preview"
    assert result["provider"] == "openrouter"
    assert result["advisor"] is True and result["prewalk"] is False
    assert result["thinking"] == "high" and result["pattern"] == "review-first"
    assert result["updated_at"] and result["updated_at"] > 0

    row = connection.authority.db.get_agent_config("reviewer", str(prompt))
    assert row["model"] == "google/gemini-3-flash-preview"
    assert row["provider"] == "openrouter"
    assert row["advisor"] == 1 and row["prewalk"] == 0
    assert row["reasoning_effort"] == "high" and row["pattern"] == "review-first"
    assert row["prompt_path"] == str(prompt)

    # No YAML layer is written: the source is the prompt path, not a config file.
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "WATCHDOG.yml").exists()


@pytest.mark.asyncio
async def test_running_directory_and_user_scopes_are_distinct(connection, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_prompt = workspace / "agents" / "reviewer.md"
    profile_prompt = tmp_path / "agents" / "reviewer.md"
    _definition(project_prompt, "reviewer", model="project/static")
    _definition(profile_prompt, "reviewer", model="user/static")
    monkeypatch.setattr(session_policy, "policy_for_source",
                        lambda runner, source: policy(workspace))

    project = await connection.dispatch({"id": 1, "method": "subagent.config", "params": {
        "subagent_id": "reviewer", "session_id": "s", "model": "project/dynamic"}})
    assert project["result"]["source"] == str(project_prompt)

    user = await connection.dispatch({"id": 2, "method": "subagent.config", "params": {
        "subagent_id": "reviewer", "model": "user/dynamic"}})
    assert user["result"]["source"] == str(profile_prompt)

    assert connection.authority.db.get_agent_config(
        "reviewer", str(project_prompt))["model"] == "project/dynamic"
    assert connection.authority.db.get_agent_config(
        "reviewer", str(profile_prompt))["model"] == "user/dynamic"


@pytest.mark.asyncio
async def test_explicit_source_pins_the_row(connection, tmp_path):
    reply = await connection.dispatch({"id": 1, "method": "subagent.config", "params": {
        "subagent_id": "reviewer", "source": "project:custom", "advisor": False}})
    assert reply["result"]["source"] == "project:custom"
    assert reply["result"]["prompt_path"] == "project:custom"
    assert connection.authority.db.get_agent_config("reviewer", "project:custom")["advisor"] == 0


@pytest.mark.asyncio
async def test_omitted_fields_keep_their_stored_value(connection, tmp_path):
    _definition(tmp_path / "agents" / "reviewer.md", "reviewer")

    async def call(**fields):
        reply = await connection.dispatch({"id": 1, "method": "subagent.config", "params": {
            "subagent_id": "reviewer", **fields}})
        assert "error" not in reply, reply
        return reply["result"]

    assert (await call(model="a/model", advisor=True))["model"] == "a/model"
    updated = await call(thinking="medium")
    assert updated["model"] == "a/model" and updated["advisor"] is True
    assert updated["thinking"] == "medium"

    cleared = await call(model=None)
    assert cleared["model"] is None and cleared["advisor"] is True


@pytest.mark.asyncio
async def test_reading_an_unset_role_returns_nulls(connection, tmp_path):
    reply = await connection.dispatch({"id": 1, "method": "subagent.config", "params": {
        "subagent_id": "never-configured"}})
    assert "error" not in reply, reply
    assert reply["result"] == {
        "subagent_id": "never-configured",
        "source": str(tmp_path / "agents" / "never-configured.md"),
        "model": None,
        "provider": None,
        "prewalk": None,
        "advisor": None,
        "thinking": None,
        "pattern": None,
        "prompt_path": str(tmp_path / "agents" / "never-configured.md"),
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_subagent_list_includes_scoped_static_definitions(
        connection, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = workspace / "agents" / "reviewer.md"
    _definition(prompt, "reviewer", model="openrouter:static/model", reasoning_effort="medium")
    monkeypatch.setattr(session_policy, "policy_for_source",
                        lambda runner, source: policy(workspace))
    monkeypatch.setattr(session_ancillary, "_require_current_route", lambda authority, ref: None)
    monkeypatch.setattr(session_ancillary, "_owner_objects",
                        lambda authority, ref, **kwargs: (None, []))

    await connection.dispatch({"id": 1, "method": "subagent.config", "params": {
        "subagent_id": "reviewer", "session_id": "s", "model": "project/dynamic"}})

    reply = await connection.dispatch(
        {"id": 2, "method": "subagent.list", "params": {"session_id": "s"}})
    assert "error" not in reply, reply
    agents = {row["agent_id"]: row for row in reply["result"]["agents"]}
    assert agents["reviewer"] == {
        "agent_id": "reviewer",
        "name": "reviewer",
        "source": str(prompt),
        "scope": "project",
        "enabled": None,
        "model": "static/model",
        "provider": "openrouter",
        "thinking": "medium",
        "prompt_path": str(prompt),
        "config": {
            "model": "project/dynamic",
            "provider": None,
            "advisor": None,
            "prewalk": None,
            "thinking": None,
            "pattern": None,
            "prompt_path": str(prompt),
        },
    }


@pytest.mark.asyncio
async def test_toggles_and_thinking_are_validated(connection):
    reply = await connection.dispatch({"id": 1, "method": "subagent.config", "params": {
        "subagent_id": "default", "advisor": "on"}})
    assert reply["error"]["message"] == "invalid_params"
    assert reply["error"]["data"]["field"] == "advisor"

    reply = await connection.dispatch({"id": 2, "method": "subagent.config", "params": {
        "subagent_id": "default", "prewalk": 1}})
    assert reply["error"]["data"]["field"] == "prewalk"

    reply = await connection.dispatch({"id": 3, "method": "subagent.config", "params": {
        "subagent_id": "default", "thinking": "hgih"}})
    assert reply["error"]["message"] == "invalid_params"
    assert reply["error"]["data"]["field"] == "thinking"


@pytest.mark.asyncio
async def test_config_requires_control(connection):
    connection.actor = type(connection.actor)(
        connection.actor.subject, connection.actor.profile_id,
        frozenset({"session:read"}), connection.actor.transport_id)
    reply = await connection.dispatch({"id": 1, "method": "subagent.config", "params": {
        "subagent_id": "default", "model": "a/model"}})
    assert reply["error"]["message"] == "permission_denied"
