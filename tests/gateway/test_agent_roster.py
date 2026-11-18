"""``gateway.agent_roster``: named-agent definitions merged with dynamic ``agent_config`` rows."""

from __future__ import annotations

from gateway.agent_roster import list_agent_definitions
from hermes_state import SessionDB


def _definition(path, name, body="Prompt.", **frontmatter):
    lines = ["---", f"name: {name}"]
    lines += [f"{key}: {value}" for key, value in frontmatter.items()]
    lines += ["---", "", body, ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def test_roster_merges_named_agent_definitions_and_dynamic_config(tmp_path, monkeypatch):
    home = tmp_path
    workspace = home / "workspace"
    workspace.mkdir()
    elsewhere = home / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("HERMES_HOME", str(home))

    _definition(home / "agents" / "reviewer.md", "reviewer",
                model="user/model", reasoning_effort="high")
    _definition(workspace / "agents" / "planner.md", "planner",
                model="openrouter:foo/bar")

    with SessionDB(db_path=home / "state.db") as db:
        db.upsert_agent_config("reviewer", {
            "prompt_path": str(home / "agents" / "reviewer.md"), "advisor": True,
        }, str(home / "agents" / "reviewer.md"))
        db.upsert_agent_config("planner", {
            "model": "project/dynamic", "pattern": "plan-first",
        }, str(workspace / "agents" / "planner.md"))
        db.upsert_agent_config("solo", {"model": "custom/model"}, "project:custom")

        rows = {row["agent_id"]: row for row in list_agent_definitions(str(workspace), db)}

    assert rows["reviewer"]["scope"] == "profile"
    assert rows["reviewer"]["source"] == str(home / "agents" / "reviewer.md")
    assert rows["reviewer"]["model"] == "user/model"
    assert rows["reviewer"]["thinking"] == "high"
    assert rows["reviewer"]["prompt_path"] == str(home / "agents" / "reviewer.md")
    assert rows["reviewer"]["config"]["advisor"] is True
    assert rows["reviewer"]["config"]["prompt_path"] == str(home / "agents" / "reviewer.md")

    assert rows["planner"]["scope"] == "project"
    assert rows["planner"]["source"] == str(workspace / "agents" / "planner.md")
    assert rows["planner"]["model"] == "foo/bar"
    assert rows["planner"]["provider"] == "openrouter"
    assert rows["planner"]["config"]["model"] == "project/dynamic"
    assert rows["planner"]["config"]["pattern"] == "plan-first"

    assert rows["solo"]["scope"] == "custom"
    assert rows["solo"]["config"]["model"] == "custom/model"


def test_config_only_row_never_hides(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.upsert_agent_config("ghost", {"prompt_path": "ghost.md"}, "")
        rows = list_agent_definitions(str(workspace), db)
    ghosts = [row for row in rows if row["agent_id"] == "ghost"]
    assert [(row["agent_id"], row["scope"]) for row in ghosts] == [("ghost", "custom")]
