"""``agent_config``: per-role custom configuration stored in the profile state.db."""

from __future__ import annotations

from hermes_state import SessionDB


def test_upsert_and_read_back_agent_config(tmp_path):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        assert db.get_agent_config("reviewer") is None
        assert db.list_agent_configs() == []

        row = db.upsert_agent_config("reviewer", {
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
            "advisor": True,
            "prewalk": False,
            "reasoning_effort": "high",
            "pattern": "review-first",
            "prompt_path": "prompts/reviewer.md",
        }, "/work/project/WATCHDOG.yml")
        assert row["agent_id"] == "reviewer"
        assert row["source"] == "/work/project/WATCHDOG.yml"
        assert row["advisor"] == 1 and row["prewalk"] == 0
        assert row["updated_at"] > 0

        stored = db.get_agent_config("reviewer", "/work/project/WATCHDOG.yml")
        assert stored["model"] == "google/gemini-3-flash-preview"
        assert stored["reasoning_effort"] == "high"
        assert stored["pattern"] == "review-first"
        assert stored["prompt_path"] == "prompts/reviewer.md"

        merged = db.upsert_agent_config(
            "reviewer", {"advisor": False}, "/work/project/WATCHDOG.yml")
        assert merged["advisor"] == 0
        assert merged["model"] == "google/gemini-3-flash-preview"

        db.upsert_agent_config("planner", {"model": "a/model"})
        db.upsert_agent_config("reviewer", {"model": "user/model"}, "/home/.hermes/WATCHDOG.yml")
        assert [(row["agent_id"], row["source"]) for row in db.list_agent_configs()] == [
            ("planner", ""), ("reviewer", "/home/.hermes/WATCHDOG.yml"),
            ("reviewer", "/work/project/WATCHDOG.yml")]
