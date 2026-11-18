"""``session_ttsr_injections``: durable once-state and session cascade."""

from __future__ import annotations

from hermes_state import SessionDB


def test_mark_and_read_ttsr_injections(tmp_path):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s1", "cli")
        assert db.ttsr_injected_names("s1") == set()
        assert db.mark_ttsr_injected("s1", ["a", "a", "b"]) == 2
        assert db.mark_ttsr_injected("s1", ["b"]) == 1
        assert db.ttsr_injected_names("s1") == {"a", "b"}
        assert db.mark_ttsr_injected("s1", []) == 0
        assert db.mark_ttsr_injected("", ["a"]) == 0


def test_ttsr_injections_cascade_with_the_session(tmp_path):
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s1", "cli")
        db.mark_ttsr_injected("s1", ["a"])
        db.delete_session("s1")
        assert db.ttsr_injected_names("s1") == set()
