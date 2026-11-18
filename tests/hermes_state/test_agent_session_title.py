"""AS-4: ``agent_session.title`` on legacy stores — reconciler add, non-unique index, write-once.

The store is built from the *old* DDL on purpose: the reconciler's contract is "add every column
``SCHEMA_SQL`` declares", so a store created from today's ``SCHEMA_SQL`` could never prove the
migration ran. Nothing is backfilled: an existing row keeps ``''`` (unlabelled) until the label
writer names it, and the first non-empty label wins forever.
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_state import SessionDB

#: ``agent_session`` exactly as it existed before ADR-21 D7 (no ``title`` column or index).
LEGACY_AGENT_SESSION_DDL = """
CREATE TABLE agent_session (
    agent_session_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    parent_session_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    short_name TEXT NOT NULL DEFAULT '',
    transcript_path TEXT NOT NULL,
    artifact_manifest TEXT,
    final_response TEXT,
    status TEXT NOT NULL,
    last_activity_at REAL NOT NULL DEFAULT 0,
    parked_at REAL,
    model_role TEXT NOT NULL DEFAULT '',
    metadata_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX idx_agent_session_subject ON agent_session(subject);
"""

_ROWS = (
    ("researcher-1", "researcher", "first legacy run"),
    ("researcher-2", "researcher", "second legacy run"),
    ("writer-1", "writer", "third legacy run"),
)


def _legacy_store(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_AGENT_SESSION_DDL)
        for index, (row_id, agent_name, subject) in enumerate(_ROWS):
            conn.execute(
                "INSERT INTO agent_session (agent_session_id, agent_name, subject, "
                "short_name, transcript_path, status, created_at, updated_at) "
                "VALUES (?, ?, ?, '', ?, 'completed', ?, ?)",
                (row_id, agent_name, subject, f"{row_id}.jsonl", float(index), float(index)),
            )
        conn.commit()
    finally:
        conn.close()


def _title_column(db: SessionDB):
    rows = list(db._conn.execute("PRAGMA table_info(agent_session)"))
    return next(row for row in rows if row[1] == "title")


def _title_indexes(db: SessionDB):
    return {
        row[1]: row[2]
        for row in db._conn.execute("PRAGMA index_list(agent_session)")
    }


def _shape(db: SessionDB):
    return (
        [(row[1], row[2], row[3], row[4]) for row in db._conn.execute("PRAGMA table_info(agent_session)")],
        sorted(_title_indexes(db).items()),
    )


def _titles(db: SessionDB) -> dict[str, str]:
    return {
        row[0]: row[1]
        for row in db._conn.execute("SELECT agent_session_id, title FROM agent_session")
    }


@pytest.fixture
def legacy_path(tmp_path):
    path = tmp_path / "state.db"
    _legacy_store(path)
    return path


def test_as4_legacy_store_gains_the_title_column_and_a_non_unique_index(legacy_path):
    db = SessionDB(legacy_path)
    try:
        _cid, name, col_type, notnull, default, _pk = _title_column(db)
        assert (name, col_type, notnull, default) == ("title", "TEXT", 1, "''")
        assert _title_indexes(db)["idx_agent_session_title"] == 0  # unique=0
        # No backfill: every pre-existing row is unlabelled.
        assert set(_titles(db).values()) == {""}
    finally:
        db.close()


def test_as4_reopening_is_idempotent_and_keeps_an_existing_label(legacy_path):
    first = SessionDB(legacy_path)
    try:
        shape = _shape(first)
    finally:
        first.close()

    # The test itself seeds the non-empty value; it is never reconciler output.
    conn = sqlite3.connect(legacy_path)
    try:
        conn.execute(
            "UPDATE agent_session SET title='keep me' WHERE agent_session_id='researcher-1'"
        )
        conn.commit()
    finally:
        conn.close()

    for _ in range(2):
        reopened = SessionDB(legacy_path)
        try:
            assert _shape(reopened) == shape
            assert _titles(reopened)["researcher-1"] == "keep me"
        finally:
            reopened.close()


def test_as4_label_writes_are_write_once(legacy_path):
    db = SessionDB(legacy_path)
    try:
        assert db.set_agent_session_title("researcher-1", "Investigate the pool leak") is True
        assert _titles(db)["researcher-1"] == "Investigate the pool leak"

        # A second, different label cannot replace the first.
        assert db.set_agent_session_title("researcher-1", "Something else entirely") is False
        assert _titles(db)["researcher-1"] == "Investigate the pool leak"

        # An empty label never reaches the store: it is the unlabelled sentinel.
        assert db.set_agent_session_title("researcher-1", "  ") is False
        assert _titles(db)["researcher-1"] == "Investigate the pool leak"

        # An untouched row is still writable once.
        assert db.set_agent_session_title("researcher-2", "Second label") is True
        assert _titles(db)["researcher-2"] == "Second label"
    finally:
        db.close()


def test_as4_live_duplicate_labels_take_a_suffix_in_the_same_transaction(legacy_path):
    db = SessionDB(legacy_path)
    try:
        for row_id in ("researcher-1", "researcher-2", "writer-1"):
            db._execute_write(lambda conn, row_id=row_id: conn.execute(
                "UPDATE agent_session SET status='running' WHERE agent_session_id=?", (row_id,)
            ))
        assert db.set_agent_session_title("researcher-1", "Investigate the pool leak") is True
        assert db.set_agent_session_title("researcher-2", "Investigate the pool leak") is True
        assert db.set_agent_session_title("writer-1", "Investigate the pool leak") is True
        assert _titles(db) == {
            "researcher-1": "Investigate the pool leak",
            "researcher-2": "Investigate the pool leak 2",
            "writer-1": "Investigate the pool leak 3",
        }
    finally:
        db.close()


def test_as4_a_completed_run_does_not_hold_a_label_suffix(legacy_path):
    """Only a *live* duplicate forces the suffix: a settled run keeps its own label."""
    db = SessionDB(legacy_path)
    try:
        db._execute_write(lambda conn: conn.execute(
            "UPDATE agent_session SET status='completed' WHERE agent_session_id='researcher-1'"
        ))
        assert db.set_agent_session_title("researcher-1", "Investigate the pool leak") is True
        assert db.set_agent_session_title("researcher-2", "Investigate the pool leak") is True
        assert _titles(db)["researcher-2"] == "Investigate the pool leak"
    finally:
        db.close()


def test_as4_lookup_is_exact_then_casefolded(legacy_path):
    db = SessionDB(legacy_path)
    try:
        db.set_agent_session_title("researcher-1", "Investigate the pool leak")
        assert [row["agent_session_id"] for row in
                db.get_agent_sessions_by_title("Investigate the pool leak")] == ["researcher-1"]
        assert [row["agent_session_id"] for row in
                db.get_agent_sessions_by_title("  investigate the POOL leak ")] == ["researcher-1"]
        assert db.get_agent_sessions_by_title("pool") == []
        assert db.get_agent_sessions_by_title("") == []
    finally:
        db.close()
