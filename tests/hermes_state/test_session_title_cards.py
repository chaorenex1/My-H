"""Card-stripped title lookup (ADR-21 D11): ``resolve_session_by_title`` still finds a carded row.

The card lives inside ``sessions.title``, so a resumed session addressed by the plain half of its
title must still resolve. The fallback runs only after the exact and ``<title> #N`` lookups miss,
newest first, and re-checks every LIKE candidate with the card splitter.
"""

from __future__ import annotations

from hermes_state import SessionDB


CARDED = "🅿️ PARK: Fix flaky park tests"
PLAIN = "Fix flaky park tests"


def _db(tmp_path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


def _session(db: SessionDB, session_id: str, title: str, *, started_at: float) -> None:
    db.create_session(session_id, source="cli")
    assert db.set_auto_title(session_id, title, source="llm")
    db._write_sql("UPDATE sessions SET started_at = ? WHERE id = ?", (started_at, session_id))


def test_plain_title_resolves_the_carded_row(tmp_path):
    db = _db(tmp_path)
    _session(db, "s-card", CARDED, started_at=1.0)

    assert db.resolve_session_by_title(PLAIN) == "s-card"
    # The exact lookup stays exact: the card is not invented into the query.
    assert db.get_session_by_title(PLAIN) is None
    db.close()


def test_exact_title_wins_over_a_card_candidate(tmp_path):
    db = _db(tmp_path)
    _session(db, "s-card", CARDED, started_at=2.0)
    _session(db, "s-plain", PLAIN, started_at=1.0)

    assert db.resolve_session_by_title(PLAIN) == "s-plain"
    db.close()


def test_newest_card_candidate_wins(tmp_path):
    db = _db(tmp_path)
    _session(db, "s-old", "PARK: Fix flaky park tests", started_at=1.0)
    _session(db, "s-new", CARDED, started_at=2.0)

    assert db.resolve_session_by_title(PLAIN) == "s-new"
    db.close()


def test_like_wildcards_in_the_query_are_literal(tmp_path):
    db = _db(tmp_path)
    _session(db, "s-percent", "PARK: 100% done", started_at=1.0)

    assert db.resolve_session_by_title("100% done") == "s-percent"
    # ``%`` in the query must not turn into "any suffix": no false hit.
    assert db.resolve_session_by_title("100X done") is None
    db.close()


def test_unnumbered_continuation_probe_still_wins_first(tmp_path):
    db = _db(tmp_path)
    _session(db, "s-card", CARDED, started_at=1.0)
    _session(db, "s-card-2", f"{PLAIN} #2", started_at=2.0)

    assert db.resolve_session_by_title(PLAIN) == "s-card-2"
    db.close()


def test_no_card_candidate_leaves_the_lookup_empty(tmp_path):
    db = _db(tmp_path)
    _session(db, "s-other", "PARK: Fix a different thing", started_at=1.0)

    assert db.resolve_session_by_title(PLAIN) is None
    db.close()
