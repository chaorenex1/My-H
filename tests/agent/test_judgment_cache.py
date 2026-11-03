from __future__ import annotations

from agent.judgment.cache import JudgmentCache, state_identity
from agent.judgment.types import (
    ChoiceAnswer,
    ChoiceQuestion,
    NoulAnswer,
    NoulQuestion,
    ScoreQuestion,
    ScoreAnswer,
)


def _questions() -> dict:
    return {
        "q0": NoulQuestion("Is it?"),
        "q1": ChoiceQuestion("Pick", {"low": None, "high": "more"}),
        "q2": ScoreQuestion("Rate", ("low", "mid", "high")),
    }


def _answers() -> dict:
    return {
        "q0": NoulAnswer(0.75),
        "q1": ChoiceAnswer("high", {"low": 0.0, "high": 1.0}, 1.0),
        "q2": ScoreAnswer(2, {"0": 0.0, "1": 0.0, "2": 1.0}, 1.0),
    }


def test_round_trip_and_question_isolation(tmp_path):
    cache = JudgmentCache(tmp_path / "judgment.db")
    try:
        state = {"content": "hello"}
        cache.record("typesafe:typesafe/m", state, _questions(), _answers())
        assert cache.lookup("typesafe:typesafe/m", state, _questions()) == _answers()

        # Different state, model, instruction, and criteria are all distinct keys.
        assert cache.lookup("typesafe:typesafe/m", {"content": "other"}, _questions()) == {}
        assert cache.lookup("typesafe:other/m", state, _questions()) == {}
        assert cache.lookup(
            "typesafe:typesafe/m", state,
            {"q0": NoulQuestion("Different instruction?")}) == {}
        assert cache.lookup(
            "typesafe:typesafe/m", state,
            {"q1": ChoiceQuestion("Pick", {"low": None, "medium": None})}) == {}
    finally:
        cache.close()


def test_uses_delete_journal_and_foreign_keys(tmp_path):
    cache = JudgmentCache(tmp_path / "judgment.db")
    try:
        db = cache._db
        assert db is not None
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "delete"
    finally:
        cache.close()


def test_oldest_states_are_evicted_with_their_answers(tmp_path):
    cache = JudgmentCache(tmp_path / "judgment.db")
    try:
        cache.record("m", {"n": 1}, {"q0": NoulQuestion("a")}, {"q0": NoulAnswer(1.0)},
                     max_states=1)
        cache.record("m", {"n": 2}, {"q0": NoulQuestion("a")}, {"q0": NoulAnswer(0.0)},
                     max_states=1)
        assert cache.lookup("m", {"n": 1}, {"q0": NoulQuestion("a")}) == {}
        assert cache.lookup("m", {"n": 2}, {"q0": NoulQuestion("a")}) == {"q0": NoulAnswer(0.0)}
    finally:
        cache.close()


def test_corrupt_database_degrades_to_unavailable(tmp_path):
    path = tmp_path / "judgment.db"
    path.write_bytes(b"not a sqlite database")
    cache = JudgmentCache(path)
    assert cache.available is False
    assert cache.lookup("m", "state", {"q0": NoulQuestion("a")}) == {}
    cache.record("m", "state", {"q0": NoulQuestion("a")}, {"q0": NoulAnswer(1.0)})
    assert cache.recent_runs() == []
    cache.close()


def test_run_audit_round_trip_filters_and_eviction(tmp_path):
    cache = JudgmentCache(tmp_path / "judgment.db")
    try:
        base = {
            "session_id": "", "execution_id": "", "request_id": "r",
            "mode": "auto", "api": "text", "provider": "auto", "model": "chat",
            "questions": 2, "cached": 1, "input_tokens": 10, "output_tokens": 2,
            "cost_usd": 0.001, "latency_ms": 12, "status": "ok", "error": "",
            "state_sha": "s", "answers_sha": "a",
        }
        cache.record_run({"id": "1", "created_at": 100, "purpose": "cli", **base}, max_runs=2)
        cache.record_run({"id": "2", "created_at": 200, "purpose": "eval", **base}, max_runs=2)
        cache.record_run({"id": "3", "created_at": 300, "purpose": "cli", **base}, max_runs=2)
        rows = cache.recent_runs()
        assert [row["request_id"] for row in rows] == ["r", "r"]
        assert cache.run_count() == 2
        assert {row["purpose"] for row in cache.recent_runs(purpose="cli")} == {"cli"}
    finally:
        cache.close()


def test_state_identity_is_canonical_across_key_order():
    a = state_identity({"b": 1, "a": 2})
    b = state_identity({"a": 2, "b": 1})
    assert a == b
    assert len(a[0]) == 64


def test_malformed_cached_answer_is_ignored(tmp_path):
    path = tmp_path / "judgment.db"
    cache = JudgmentCache(path)
    try:
        state = {"content": "x"}
        state_id, canonical = state_identity(state)
        db = cache._db
        assert db is not None
        with db:
            db.execute(
                "INSERT INTO states (id, state, created_at) VALUES (?, ?, 1)",
                (state_id, canonical))
            db.execute(
                "INSERT INTO answers (state, model, name, type, instruction, criteria, answer, created_at) "
                "VALUES (?, 'm', 'q0', 'noul', 'Is it?', 'null', 'not json', 1)",
                (state_id,))
        assert cache.lookup("m", state, {"q0": NoulQuestion("Is it?")}) == {}
    finally:
        cache.close()


def test_shared_cache_is_keyed_by_profile_home(tmp_path, monkeypatch):
    import agent.judgment.cache as cache_module

    current = [tmp_path / "profile-a"]
    monkeypatch.setattr(cache_module, "get_hermes_home", lambda: current[0])
    cache_module.reset_shared_cache()
    try:
        first = cache_module.shared_cache()
        assert first is not None
        assert cache_module.shared_cache() is first

        current[0] = tmp_path / "profile-b"
        second = cache_module.shared_cache()
        assert second is not None and second is not first
        assert second.db_path != first.db_path
        assert second.db_path.parent.parent == tmp_path / "profile-b"
    finally:
        cache_module.reset_shared_cache()


def test_run_audit_tolerates_missing_database(tmp_path):
    path = tmp_path / "missing" / "judgment.db"
    cache = JudgmentCache(path)
    assert cache.available
    try:
        assert cache.run_count() == 0
    finally:
        cache.close()
