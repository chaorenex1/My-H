"""ADR-17 P5 title drift: judge a generated title against the latest conversation and refresh it.

Port of omp ``title-drift-classifier.ts`` / ``buildTitleDriftTurns``. Every check is advisory:
the default config spends no judgment, a user's name is never touched, and any judge or model
failure keeps the current title.
"""

from types import SimpleNamespace

import pytest

import agent.judgment
from agent import title_generator
from agent.title_generator import (
    TITLE_DRIFT_CONTEXT_CHARS,
    TITLE_DRIFT_TURN_CHARS,
    build_title_drift_recent,
    maybe_refresh_title_drift,
    start_title_drift_check,
)
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def _agent(db, session_id):
    return SimpleNamespace(_session_db=db, session_id=session_id, _on_session_title=None)


def _configure(monkeypatch, after_prompts):
    monkeypatch.setattr(
        title_generator, "_title_config", lambda: {"refresh_after_prompts": after_prompts}
    )


def _judge_result(probability):
    return SimpleNamespace(answers={"fits": SimpleNamespace(noul=probability)})


def _messages(*turns, finish="stop"):
    messages = []
    for turn in turns:
        role, text = turn[0], turn[1]
        message = {"role": role, "content": text}
        if role == "assistant":
            message["finish_reason"] = turn[2] if len(turn) > 2 else finish
        messages.append(message)
    return messages


def _seeded_session(db, session_id="s1", *, title="Fix parser crash on empty input",
                    source=SessionDB.TITLE_SOURCE_LLM):
    db.create_session(session_id, source="cli")
    assert db.set_auto_title(session_id, title, source=source)
    return session_id


# ── the recent window ────────────────────────────────────────────────────────────────────────


def test_recent_excludes_aborted_and_errored_assistant_turns():
    messages = _messages(
        ("user", "Fix the parser"),
        ("assistant", "I will look at the parser", "error"),
        ("assistant", "The parser now handles empty input", "stop"),
        ("assistant", "interrupted while testing", "aborted"),
    )

    recent = build_title_drift_recent(messages)

    assert [turn["role"] for turn in recent] == ["user", "assistant"]
    assert recent[1]["text"] == "The parser now handles empty input"


def test_recent_caps_each_turn_and_the_whole_window_oldest_first():
    messages = _messages(
        ("user", "Original ask about the parser"),
        ("assistant", "a" * 5000),
        ("assistant", "b" * 1000),
        ("assistant", "c" * 1000),
    )

    recent = build_title_drift_recent(messages)

    assert recent[0] == {"role": "user", "text": "Original ask about the parser"}
    assert all(len(turn["text"]) <= TITLE_DRIFT_TURN_CHARS for turn in recent)
    assert sum(len(turn["text"]) for turn in recent) <= TITLE_DRIFT_CONTEXT_CHARS
    # Oldest first, ending with the latest turn.
    assert recent[-1]["text"] == "c" * 1000


def test_recent_prepends_the_latest_prompt_when_the_window_holds_only_assistant_notes():
    messages = _messages(
        ("user", "Implement the settings page layout"),
        ("assistant", "a" * 1000),
        ("assistant", "b" * 1000),
        ("assistant", "c" * 1000),
    )

    recent = build_title_drift_recent(messages)

    assert recent[0] == {"role": "user", "text": "Implement the settings page layout"}
    assert recent[-1]["text"] == "c" * 1000
    assert sum(len(turn["text"]) for turn in recent) <= TITLE_DRIFT_CONTEXT_CHARS


# ── guards ───────────────────────────────────────────────────────────────────────────────────


def test_default_off_spends_no_judgment(monkeypatch, db):
    _configure(monkeypatch, 0)
    sid = _seeded_session(db)
    monkeypatch.setattr(
        agent.judgment, "judge", lambda *a, **k: pytest.fail("judge must not run when off")
    )

    result = maybe_refresh_title_drift(
        _agent(db, sid), db, sid,
        _messages(("user", "Now design the settings page"), ("assistant", "Sure")),
        final_status="completed",
    )

    assert result is None
    assert db.get_session_title(sid) == "Fix parser crash on empty input"


def test_interrupted_turn_never_judges(monkeypatch, db):
    _configure(monkeypatch, 1)
    sid = _seeded_session(db)
    monkeypatch.setattr(
        agent.judgment, "judge", lambda *a, **k: pytest.fail("interrupted turns must not judge")
    )

    result = maybe_refresh_title_drift(
        _agent(db, sid), db, sid,
        _messages(("user", "Now design the settings page"), ("assistant", "Sure")),
        final_status="interrupted",
    )

    assert result is None


def test_counter_waits_for_the_configured_number_of_operator_prompts(monkeypatch, db):
    _configure(monkeypatch, 2)
    sid = _seeded_session(db)
    calls = []

    def fake_judge(request, **kwargs):
        calls.append(request)
        return _judge_result(0.9)

    monkeypatch.setattr(agent.judgment, "judge", fake_judge)
    agent_double = _agent(db, sid)
    first = _messages(("user", "First ask"), ("assistant", "ok"))
    second = first + _messages(("user", "Second ask"), ("assistant", "ok"))

    assert maybe_refresh_title_drift(agent_double, db, sid, first, final_status="completed") is None
    assert calls == []
    assert maybe_refresh_title_drift(agent_double, db, sid, second, final_status="completed") is None
    assert len(calls) == 1
    # The check consumed the prompts: the same settled history is not judged again.
    assert maybe_refresh_title_drift(agent_double, db, sid, second, final_status="completed") is None
    assert len(calls) == 1


def test_assistant_rows_appended_after_the_same_prompt_do_not_advance_the_counter(monkeypatch, db):
    _configure(monkeypatch, 2)
    sid = _seeded_session(db)
    calls = []

    def fake_judge(request, **kwargs):
        calls.append(request)
        return _judge_result(0.9)

    monkeypatch.setattr(agent.judgment, "judge", fake_judge)
    agent_double = _agent(db, sid)
    first = _messages(("user", "First ask"), ("assistant", "working"))
    more_assistant = first + _messages(("assistant", "still working"))
    second_prompt = more_assistant + _messages(("user", "Second ask"), ("assistant", "ok"))

    assert maybe_refresh_title_drift(agent_double, db, sid, first, final_status="completed") is None
    assert maybe_refresh_title_drift(agent_double, db, sid, more_assistant, final_status="completed") is None
    assert calls == []
    assert maybe_refresh_title_drift(agent_double, db, sid, second_prompt, final_status="completed") is None
    assert len(calls) == 1


def test_start_check_refreshes_on_a_background_thread(monkeypatch, db):
    _configure(monkeypatch, 1)
    sid = _seeded_session(db)
    monkeypatch.setattr(agent.judgment, "judge", lambda *a, **k: _judge_result(0.1))
    monkeypatch.setattr(
        title_generator, "generate_title", lambda *a, **k: "Design settings page layout"
    )

    thread = start_title_drift_check(
        _agent(db, sid),
        _messages(("user", "Design the settings page"), ("assistant", "Here is a plan")),
        final_status="completed",
    )

    assert thread is not None
    thread.join(10)
    assert not thread.is_alive()
    assert db.get_session_title(sid) == "Design settings page layout"


def test_start_check_spawns_nothing_when_off_or_terminal(monkeypatch, db):
    sid = _seeded_session(db)
    messages = _messages(("user", "First ask"), ("assistant", "ok"))

    _configure(monkeypatch, 0)
    assert start_title_drift_check(_agent(db, sid), messages, final_status="completed") is None

    _configure(monkeypatch, 1)
    assert start_title_drift_check(_agent(db, sid), messages, final_status="interrupted") is None


def test_user_titled_session_is_never_judged_or_rewritten(monkeypatch, db):
    _configure(monkeypatch, 1)
    db.create_session("s1", source="cli")
    assert db.set_session_title("s1", "My own name")
    monkeypatch.setattr(
        agent.judgment, "judge", lambda *a, **k: pytest.fail("user titles must not be judged")
    )

    result = maybe_refresh_title_drift(
        _agent(db, "s1"), db, "s1",
        _messages(("user", "Something completely different"), ("assistant", "ok")),
        final_status="completed",
    )

    assert result is None
    assert db.get_session_title("s1") == "My own name"
    assert db.get_session_title_source("s1") == SessionDB.TITLE_SOURCE_USER


def test_provisional_greeting_title_is_not_judged(monkeypatch, db):
    _configure(monkeypatch, 1)
    sid = _seeded_session(db, title="Friendly greeting", source=SessionDB.TITLE_SOURCE_DERIVED)
    monkeypatch.setattr(
        agent.judgment, "judge", lambda *a, **k: pytest.fail("greeting placeholder must not be judged")
    )

    result = maybe_refresh_title_drift(
        _agent(db, sid), db, sid,
        _messages(("user", "Now benchmark the database"), ("assistant", "On it")),
        final_status="completed",
    )

    assert result is None
    assert db.get_session_title(sid) == "Friendly greeting"


# ── judgment outcomes ───────────────────────────────────────────────────────────────────────


def test_drifted_title_is_regenerated_with_the_recent_context_and_persisted(monkeypatch, db):
    _configure(monkeypatch, 1)
    sid = _seeded_session(db)
    seen = {}

    def fake_judge(request, **kwargs):
        seen["state"] = request.state
        seen["purpose"] = kwargs.get("purpose")
        seen["timeout_ms"] = kwargs.get("timeout_ms")
        return _judge_result(0.1)

    monkeypatch.setattr(agent.judgment, "judge", fake_judge)
    monkeypatch.setattr(
        title_generator, "generate_title",
        lambda context, **kwargs: "Design settings page layout",
    )

    result = maybe_refresh_title_drift(
        _agent(db, sid), db, sid,
        _messages(
            ("user", "Design the settings page layout"),
            ("assistant", "Here is the grid plan"),
        ),
        final_status="completed",
    )

    assert result == "Design settings page layout"
    assert db.get_session_title(sid) == "Design settings page layout"
    assert db.get_session_title_source(sid) == SessionDB.TITLE_SOURCE_LLM
    assert seen["purpose"] == "title-drift"
    assert seen["timeout_ms"] == 15_000
    assert seen["state"]["title"] == "Fix parser crash on empty input"
    assert seen["state"]["recent"][0]["text"] == "Design the settings page layout"


def test_fitting_title_is_kept(monkeypatch, db):
    _configure(monkeypatch, 1)
    sid = _seeded_session(db)
    monkeypatch.setattr(agent.judgment, "judge", lambda *a, **k: _judge_result(0.9))
    monkeypatch.setattr(
        title_generator, "generate_title",
        lambda *a, **k: pytest.fail("a fitting title must not be regenerated"),
    )

    result = maybe_refresh_title_drift(
        _agent(db, sid), db, sid,
        _messages(("user", "Add a regression test for the crash"), ("assistant", "Done")),
        final_status="completed",
    )

    assert result is None
    assert db.get_session_title(sid) == "Fix parser crash on empty input"


def test_judge_failure_keeps_the_title(monkeypatch, db):
    _configure(monkeypatch, 1)
    sid = _seeded_session(db)

    def failing_judge(*args, **kwargs):
        raise RuntimeError("judgment backend down")

    monkeypatch.setattr(agent.judgment, "judge", failing_judge)
    monkeypatch.setattr(
        title_generator, "generate_title",
        lambda *a, **k: pytest.fail("no regeneration after a failed judgment"),
    )

    result = maybe_refresh_title_drift(
        _agent(db, sid), db, sid,
        _messages(("user", "Design the settings page"), ("assistant", "Here is a plan")),
        final_status="completed",
    )

    assert result is None
    assert db.get_session_title(sid) == "Fix parser crash on empty input"


def test_same_source_refresh_never_overwrites_a_newer_user_title(monkeypatch, db):
    _configure(monkeypatch, 1)
    sid = _seeded_session(db)

    def racing_judge(*args, **kwargs):
        # The operator renames the session between the title read and the write.
        assert db.set_session_title(sid, "Renamed by the operator")
        return _judge_result(0.1)

    monkeypatch.setattr(agent.judgment, "judge", racing_judge)
    monkeypatch.setattr(
        title_generator, "generate_title",
        lambda *a, **k: "Design settings page layout",
    )

    result = maybe_refresh_title_drift(
        _agent(db, sid), db, sid,
        _messages(("user", "Design the settings page"), ("assistant", "Here is a plan")),
        final_status="completed",
    )

    assert result is None
    assert db.get_session_title(sid) == "Renamed by the operator"
    assert db.get_session_title_source(sid) == SessionDB.TITLE_SOURCE_USER


# ── the guarded store refresh ───────────────────────────────────────────────────────────────


def test_refresh_auto_title_replaces_only_the_expected_equal_authority_title(db):
    db.create_session("s1", source="cli")
    assert db.set_auto_title("s1", "Old title", source=SessionDB.TITLE_SOURCE_LLM)

    assert db.refresh_auto_title(
        "s1", "New title", source=SessionDB.TITLE_SOURCE_LLM, expected_title="Old title"
    )
    assert db.get_session_title("s1") == "New title"
    # The stale expectation (the title changed) loses instead of clobbering the fresh write.
    assert not db.refresh_auto_title(
        "s1", "Stale refresh", source=SessionDB.TITLE_SOURCE_LLM, expected_title="Old title"
    )
    assert db.get_session_title("s1") == "New title"


def test_refresh_auto_title_never_bumps_a_higher_authority_or_a_user_name(db):
    db.create_session("s1", source="cli")
    assert db.set_session_title("s1", "My own name")

    assert not db.refresh_auto_title(
        "s1", "Auto name", source=SessionDB.TITLE_SOURCE_LLM, expected_title="My own name"
    )
    assert db.get_session_title("s1") == "My own name"

    db.create_session("s2", source="cli")
    assert db.set_auto_title("s2", "Upgraded", source=SessionDB.TITLE_SOURCE_LLM)
    assert not db.refresh_auto_title(
        "s2", "Downgraded", source=SessionDB.TITLE_SOURCE_DERIVED, expected_title="Upgraded"
    )
    assert db.get_session_title("s2") == "Upgraded"


# ── AS-1: drift regeneration reuses the configured generator and the CAS ────────────────────


def test_as1_drift_regeneration_uses_the_configured_generator_and_the_expected_title_cas(
    monkeypatch, tmp_path, db, caplog,
):
    """A drifted ``llm`` title is regenerated through the plan path (drift never starts a fork) and
    replaced with ``refresh_auto_title(expected_title=…)`` — never an unconditional write."""
    import logging
    from types import SimpleNamespace

    from hermes_yaml import safe_dump

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        safe_dump({"auxiliary": {"title_generation": {
            "enabled": True,
            "model_upgrade_enabled": True,
            "generator": "fork",
            "provider": "openrouter",
            "model": "title-model",
            "timeout": 5,
            "refresh_after_prompts": 1,
        }}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    sid = _seeded_session(db)
    calls: list[dict] = []

    def transport(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="<title>Design settings page layout</title>"))])

    monkeypatch.setattr(title_generator, "call_llm", transport)
    monkeypatch.setattr(agent.judgment, "judge", lambda request, **kwargs: _judge_result(0.1))
    refresh_calls: list[dict] = []
    original_refresh = db.refresh_auto_title

    def spy_refresh(*args, **kwargs):
        refresh_calls.append(dict(kwargs))
        return original_refresh(*args, **kwargs)

    monkeypatch.setattr(db, "refresh_auto_title", spy_refresh)
    caplog.set_level(logging.DEBUG, logger="agent.title_generator")

    result = maybe_refresh_title_drift(
        _agent(db, sid), db, sid,
        _messages(
            ("user", "Design the settings page layout"),
            ("assistant", "Here is the grid plan"),
        ),
        final_status="completed",
    )

    assert result == "Design settings page layout"
    assert db.get_session_title(sid) == "Design settings page layout"
    assert db.get_session_title_source(sid) == SessionDB.TITLE_SOURCE_LLM
    assert [(call["task"], call["provider"], call["model"]) for call in calls] == [
        ("title_generation", "openrouter", "title-model")
    ]
    assert refresh_calls == [{"source": "llm", "expected_title": "Fix parser crash on empty input"}]


def test_as1_model_upgrade_off_closes_the_drift_path(monkeypatch, tmp_path, db):
    """``model_upgrade_enabled=false`` keeps the derived title: no judge, no regeneration, no thread.

    ADR-21 D3 closes every stage-1 path on that switch. Drift is one of them — otherwise the
    interval would spend a judgment plus a title call the operator just opted out of.
    """
    from hermes_yaml import safe_dump

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        safe_dump({"auxiliary": {"title_generation": {
            "enabled": True,
            "model_upgrade_enabled": False,
            "provider": "openrouter",
            "model": "title-model",
            "timeout": 5,
            "refresh_after_prompts": 1,
        }}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    sid = _seeded_session(db)
    judged: list[object] = []
    monkeypatch.setattr(
        agent.judgment, "judge",
        lambda request, **kwargs: judged.append(request) or _judge_result(0.1),
    )
    model_calls: list[dict] = []

    def transport(**kwargs):
        model_calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="<title>Should not be used</title>"))])

    monkeypatch.setattr(title_generator, "call_llm", transport)
    messages = _messages(
        ("user", "Design the settings page layout"),
        ("assistant", "Here is the grid plan"),
    )

    assert maybe_refresh_title_drift(_agent(db, sid), db, sid, messages,
                                     final_status="completed") is None
    assert judged == []
    assert model_calls == []
    assert db.get_session_title(sid) == "Fix parser crash on empty input"

    spawned: list[object] = []
    monkeypatch.setattr("hermes_threading.spawn_context_thread",
                        lambda *args, **kwargs: spawned.append(args))
    assert start_title_drift_check(_agent(db, sid), messages, final_status="completed") is None
    assert spawned == []
