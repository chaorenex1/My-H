"""Session naming through the fork side turn, the plan fallback and the card top-up.

AS-3 is the integration case: a real ``AIAgent`` parent, the real ``build_cache_parity_fork``
lifecycle and a real ``SessionDB``; only the model turn (``run_conversation``) is stubbed. The
other classes stand in a fake fork entry so the orchestration (fallback matrix, card predicate,
``boring`` gate) is exercised without a live agent.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent.judgment
from agent import title_generator
from agent.prompts.renderer import render_request
from agent.title_generator import (
    auto_title_session,
    maybe_auto_title,
    wait_for_title_upgrades,
)
from hermes_state import SessionDB

OPENER = "fix the flaky park tests"
PLAIN = "Fix flaky park tests"
CARDED = "🅿️ PARK: Fix flaky park tests"
FORK_CARD_REPLY = '<title emoji="🅿️" code="PARK">Fix flaky park tests</title>'


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def _configure(monkeypatch, **overrides) -> dict:
    title = {
        "enabled": True,
        "model_upgrade_enabled": True,
        "generator": "fork",
        "card_icons": "emoji",
        "provider": "test",
        "model": "title-model",
        "timeout": 5,
    }
    title.update(overrides)
    config = {"auxiliary": {"title_generation": title}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    return config


def _session(db: SessionDB, session_id: str = "sess-1") -> str:
    db.create_session(session_id, source="cli")
    return session_id


class _Transport:
    """Stand-in for the module's ``call_llm``: records each request and replays scripted replies."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        content = self.replies.pop(0) if self.replies else ""
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _fork_parent(history=None) -> SimpleNamespace:
    return SimpleNamespace(_session_messages=list(history or []))


def _expected_fork_prompt(*, card: bool, language: str = "") -> str:
    return render_request(
        "title_fork", {"card": card, "title_language": language, "nerd_fonts": False}
    ) + f"\n\n{OPENER}"


# ── AS-3: the real fork, its prefix parity and its isolation from the live turn ─────────────


def test_as3_fork_replays_the_parent_prefix_without_touching_the_transcript(tmp_path, monkeypatch, db):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _configure(monkeypatch)
    # Importing run_agent runs hermes_bootstrap's dependency activation, which probes the
    # checkout's parent for a PM manifest (the real home when the checkout lives under it).
    with patch("pm.environments.activate_dependencies"), \
            patch("hermes_cli._early_recovery.recover_if_needed"):
        from run_agent import AIAgent

    captured: dict = {}
    whitelists: list[set] = []

    with patch("model_tools.get_tool_definitions", return_value=[]), \
            patch("model_tools.check_toolset_requirements", return_value={}), \
            patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
            patch("agent.process_bootstrap.OpenAI", MagicMock()):
        parent = AIAgent(
            api_key="k", base_url="https://openrouter.ai/api/v1", model="test/model",
            quiet_mode=True, skip_context_files=True, skip_memory=True, session_id="sess-1",
        )
    parent._cached_system_prompt = "You are helpful."
    parent.tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    parent._session_messages = [
        {"role": "user", "content": OPENER},
        {"role": "assistant", "content": "I will start with the park suite."},
    ]
    parent._session_db = db
    _session(db)
    db.append_message("sess-1", "user", OPENER)
    db.append_message("sess-1", "assistant", "I will start with the park suite.")
    before_messages = [dict(message) for message in parent._session_messages]
    before_rows = [dict(row) for row in db.get_messages("sess-1")]
    appends: list[tuple] = []
    original_append = db.append_message
    monkeypatch.setattr(
        db, "append_message",
        lambda *args, **kwargs: (appends.append(args), original_append(*args, **kwargs))[1],
    )

    def fake_run(self, user_message, conversation_history=None, **kwargs):
        captured.update(
            prompt=user_message,
            history=conversation_history,
            model=self.model,
            provider=self.provider,
            base_url=self.base_url,
            system_prompt=self._cached_system_prompt,
            tools=self.tools,
            max_iterations=self.max_iterations,
            persist_disabled=self._persist_disabled,
            session_db=self._session_db,
        )
        # A real turn would have billed these; the teardown must route them to title_generation.
        self.session_input_tokens = 120
        self.session_output_tokens = 8
        self.session_api_calls = 1
        return {"final_response": FORK_CARD_REPLY}

    with patch.object(AIAgent, "run_conversation", fake_run), \
            patch("agent.process_bootstrap.OpenAI", MagicMock()), \
            patch("hermes_cli.plugins.set_thread_tool_whitelist",
                  side_effect=lambda allowed, **kwargs: whitelists.append(allowed)), \
            patch("hermes_cli.plugins.clear_thread_tool_whitelist"):
        auto_title_session(db, "sess-1", OPENER, agent=parent)

    # One side turn, on the parent's exact runtime and warm prefix.
    assert captured["model"] == parent.model
    assert captured["provider"] == parent.provider
    assert captured["base_url"] == parent.base_url
    assert captured["system_prompt"] == parent._cached_system_prompt
    assert captured["tools"] == parent.tools
    assert captured["tools"] is not parent.tools
    assert captured["max_iterations"] == 1
    assert whitelists == [set()]
    # title_fork render + this turn's opener, and the snapshot replayed verbatim (no digest).
    assert captured["prompt"] == _expected_fork_prompt(card=True)
    assert OPENER in captured["prompt"]
    assert captured["history"] == before_messages
    assert captured["history"] is not parent._session_messages
    assert not any(
        str(message.get("content") or "").startswith("[Earlier conversation digest")
        for message in captured["history"]
    )
    # Persistence detachment: no row, no parent-list mutation, no leaf movement.
    assert captured["persist_disabled"] is True
    assert captured["session_db"] is None
    assert parent._session_messages == before_messages
    assert db.get_messages("sess-1") == before_rows
    assert appends == []
    # The fork's spend is billed to the naming task, and the carded title is stored as llm.
    assert "title_generation" in db.auxiliary_usage_by_task("sess-1")
    assert db.get_session_title("sess-1") == CARDED
    assert db.get_session_title_source("sess-1") == "llm"


# ── AS-4: the fallback matrix, the card predicate and the terminal decline ──────────────────


def _failing_fork(kind: str):
    if kind == "timeout":
        def run(parent, prompt, **kwargs):
            raise TimeoutError("provider timed out")
    elif kind == "refusal":
        def run(parent, prompt, **kwargs):
            raise RuntimeError("refusal")
    else:
        def run(parent, prompt, **kwargs):
            return ""
    return run


@pytest.mark.parametrize("kind", ["timeout", "refusal", "empty", "no_agent"])
def test_as4_every_fork_failure_falls_back_to_the_plan_and_tops_the_card_up(
    monkeypatch, db, kind,
):
    _configure(monkeypatch)
    transport = _Transport(
        f"<title>{PLAIN}</title>", "<title>🅿️ PARK: extra words</title>"
    )
    monkeypatch.setattr(title_generator, "call_llm", transport)
    fork_calls: list[str] = []
    parent = None if kind == "no_agent" else _fork_parent()
    failing = _failing_fork(kind)

    def fork_entry(p, prompt, **kwargs):
        fork_calls.append(prompt)
        return failing(p, prompt)

    monkeypatch.setattr(title_generator, "run_title_fork", fork_entry)
    _session(db)

    auto_title_session(db, "sess-1", OPENER, agent=parent)

    assert len(fork_calls) == (0 if kind == "no_agent" else 1)
    # Plan candidate then exactly one card request, both on the naming task.
    assert [call["task"] for call in transport.calls] == ["title_generation", "title_generation"]
    assert transport.calls[0]["messages"][1]["content"] == OPENER
    assert transport.calls[1]["messages"] == [
        {"role": "system", "content": render_request("title_card", {"nerd_fonts": False})},
        {"role": "user", "content": PLAIN},
    ]
    # Only the icon/code survive from the card reply; trailing words are ignored.
    assert db.get_session_title("sess-1") == CARDED
    assert db.get_session_title_source("sess-1") == "llm"


@pytest.mark.parametrize(
    "card_reply",
    ["", "<title/>", "<title>A PARK: Fix flaky park tests</title>", "<title>PARKING</title>"],
)
def test_as4_an_unusable_card_reply_keeps_the_plain_title(monkeypatch, db, card_reply):
    _configure(monkeypatch)
    transport = _Transport(f"<title>{PLAIN}</title>", card_reply)
    monkeypatch.setattr(title_generator, "call_llm", transport)
    monkeypatch.setattr(title_generator, "run_title_fork", lambda *a, **k: "")
    _session(db)

    auto_title_session(db, "sess-1", OPENER, agent=_fork_parent())

    assert len(transport.calls) == 2  # one attempt, never a retry
    assert db.get_session_title("sess-1") == PLAIN


def test_as4_plan_generator_never_runs_the_fork_or_the_card(monkeypatch, db):
    _configure(monkeypatch, generator="plan")
    transport = _Transport(f"<title>{PLAIN}</title>")
    monkeypatch.setattr(title_generator, "call_llm", transport)
    fork_calls: list[tuple] = []
    monkeypatch.setattr(
        title_generator, "run_title_fork",
        lambda *args, **kwargs: fork_calls.append(args) or "",
    )
    _session(db)

    auto_title_session(db, "sess-1", OPENER, agent=_fork_parent())

    assert fork_calls == []
    assert len(transport.calls) == 1
    assert db.get_session_title("sess-1") == PLAIN


def test_as4_an_unset_generator_is_the_fork(monkeypatch, db):
    config = {"auxiliary": {"title_generation": {
        "enabled": True,
        "model_upgrade_enabled": True,
        "card_icons": "emoji",
        "provider": "test",
        "model": "title-model",
        "timeout": 5,
    }}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    fork_calls: list[str] = []

    def fork_entry(parent, prompt, **kwargs):
        fork_calls.append(prompt)
        return FORK_CARD_REPLY

    monkeypatch.setattr(title_generator, "run_title_fork", fork_entry)
    _session(db)

    auto_title_session(db, "sess-1", OPENER, agent=_fork_parent())

    assert fork_calls == [_expected_fork_prompt(card=True)]
    assert db.get_session_title("sess-1") == CARDED


def test_as4_a_plan_decline_terminates_without_writing_or_topping_up(monkeypatch, db):
    _configure(monkeypatch)
    transport = _Transport("<title/>")
    monkeypatch.setattr(title_generator, "call_llm", transport)
    monkeypatch.setattr(title_generator, "run_title_fork", lambda *a, **k: "")
    _session(db)

    auto_title_session(db, "sess-1", OPENER, agent=_fork_parent())

    assert len(transport.calls) == 1
    assert db.get_session_title("sess-1") is None


def test_as4_a_plan_decline_never_tries_the_next_candidate(monkeypatch, db):
    config = {"auxiliary": {
        "title_generation": {
            "enabled": True,
            "model_upgrade_enabled": True,
            "generator": "fork",
            "card_icons": "emoji",
            "provider": "test",
            "model": "title-model",
            "timeout": 5,
        },
        "tiny": {"provider": "test", "model": "tiny-model", "timeout": 5},
    }}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    transport = _Transport("<title/>", f"<title>{PLAIN}</title>")
    monkeypatch.setattr(title_generator, "call_llm", transport)
    monkeypatch.setattr(title_generator, "run_title_fork", lambda *a, **k: "")
    _session(db)

    auto_title_session(db, "sess-1", OPENER, agent=_fork_parent())

    assert len(transport.calls) == 1  # the decline ends the plan, the second route is not dialled
    assert db.get_session_title("sess-1") is None


def test_as4_a_plan_without_output_keeps_the_derived_title(monkeypatch, db):
    _configure(monkeypatch)
    transport = _Transport("")
    monkeypatch.setattr(title_generator, "call_llm", transport)
    monkeypatch.setattr(title_generator, "run_title_fork", lambda *a, **k: "")
    _session(db)
    db.set_auto_title("sess-1", "instant derived name", source="derived")

    auto_title_session(db, "sess-1", OPENER, agent=_fork_parent())

    assert db.get_session_title("sess-1") == "instant derived name"
    assert db.get_session_title_source("sess-1") == "derived"
    assert len(transport.calls) == 1  # the card request needs a title to decorate


# ── AS-5: ``boring`` closes the card on both sides of the pipeline ─────────────────────────


def test_as5_boring_stores_the_plain_title_and_sends_no_card_request(monkeypatch, db):
    _configure(monkeypatch, card_icons="boring")
    transport = _Transport()
    monkeypatch.setattr(title_generator, "call_llm", transport)
    prompts: list[str] = []
    monkeypatch.setattr(
        title_generator, "run_title_fork",
        lambda parent, prompt, **kwargs: prompts.append(prompt) or FORK_CARD_REPLY,
    )
    _session(db)

    auto_title_session(db, "sess-1", OPENER, agent=_fork_parent())

    assert prompts == [_expected_fork_prompt(card=False)]
    # No card guidance at all: the plain output line, no emoji/code lines.
    assert "<title emoji=" not in prompts[0]
    assert "- emoji:" not in prompts[0]
    assert "- code:" not in prompts[0]
    assert transport.calls == []
    assert db.get_session_title("sess-1") == PLAIN


def test_as5_boring_plan_fallback_sends_no_card_request(monkeypatch, db):
    _configure(monkeypatch, card_icons="boring")
    transport = _Transport(f"<title>{PLAIN}</title>")
    monkeypatch.setattr(title_generator, "call_llm", transport)
    monkeypatch.setattr(title_generator, "run_title_fork", lambda *a, **k: "")
    _session(db)

    auto_title_session(db, "sess-1", OPENER, agent=_fork_parent())

    assert len(transport.calls) == 1
    assert db.get_session_title("sess-1") == PLAIN


def test_as5_boring_drift_regeneration_never_restores_the_old_card(monkeypatch, db):
    monkeypatch.setattr(
        title_generator, "_title_config",
        lambda: {"refresh_after_prompts": 1, "card_icons": "boring"},
    )
    _session(db)
    db.set_auto_title("sess-1", CARDED, source="llm")
    monkeypatch.setattr(agent.judgment, "judge", lambda request, **kwargs: SimpleNamespace(
        answers={"fits": SimpleNamespace(noul=0.1)}
    ))
    monkeypatch.setattr(title_generator, "generate_title", lambda *a, **k: "Fix flaky auth tests")
    keep_calls: list[tuple] = []
    monkeypatch.setattr(
        title_generator, "keep_title_card",
        lambda *args: keep_calls.append(args) or "should not be used",
    )

    from agent.title_generator import maybe_refresh_title_drift

    result = maybe_refresh_title_drift(
        SimpleNamespace(_session_db=db, session_id="sess-1", _on_session_title=None),
        db, "sess-1",
        [
            {"role": "user", "content": "fix the flaky auth tests"},
            {"role": "assistant", "content": "the auth suite passes now", "finish_reason": "stop"},
        ],
        final_status="completed",
    )

    assert keep_calls == []
    assert result == "Fix flaky auth tests"
    assert db.get_session_title("sess-1") == "Fix flaky auth tests"


# ── AS-6/AS-7: card-preserving refresh, manual titles and the Nerd Font gate ───────────────


def test_as6_drift_keeps_the_card_and_compares_on_the_plain_text(monkeypatch, db):
    monkeypatch.setattr(
        title_generator, "_title_config",
        lambda: {"refresh_after_prompts": 1, "card_icons": "emoji"},
    )
    _session(db)
    db.set_auto_title("sess-1", CARDED, source="llm")
    seen: dict = {}

    def fake_judge(request, **kwargs):
        seen["title"] = request.state["title"]
        return SimpleNamespace(answers={"fits": SimpleNamespace(noul=0.1)})

    monkeypatch.setattr(agent.judgment, "judge", fake_judge)
    monkeypatch.setattr(
        title_generator, "generate_title", lambda *a, **k: "Fix flaky auth tests"
    )
    refresh_calls: list[dict] = []
    original_refresh = db.refresh_auto_title

    def spy_refresh(*args, **kwargs):
        refresh_calls.append(dict(kwargs))
        return original_refresh(*args, **kwargs)

    monkeypatch.setattr(db, "refresh_auto_title", spy_refresh)
    from agent.title_generator import maybe_refresh_title_drift

    result = maybe_refresh_title_drift(
        SimpleNamespace(_session_db=db, session_id="sess-1", _on_session_title=None),
        db, "sess-1",
        [
            {"role": "user", "content": "fix the flaky auth tests"},
            {"role": "assistant", "content": "the auth suite passes now", "finish_reason": "stop"},
        ],
        final_status="completed",
    )

    assert seen["title"] == PLAIN  # the judge never sees the card
    assert result == "🅿️ PARK: Fix flaky auth tests"
    assert refresh_calls == [{"source": "llm", "expected_title": CARDED}]
    assert db.get_session_title("sess-1") == "🅿️ PARK: Fix flaky auth tests"


def test_as6_a_card_only_difference_is_not_a_rewrite(monkeypatch, db):
    monkeypatch.setattr(
        title_generator, "_title_config",
        lambda: {"refresh_after_prompts": 1, "card_icons": "emoji"},
    )
    _session(db)
    db.set_auto_title("sess-1", CARDED, source="llm")
    monkeypatch.setattr(agent.judgment, "judge", lambda request, **kwargs: SimpleNamespace(
        answers={"fits": SimpleNamespace(noul=0.1)}
    ))
    monkeypatch.setattr(title_generator, "generate_title", lambda *a, **k: PLAIN)
    monkeypatch.setattr(
        db, "refresh_auto_title",
        lambda *a, **k: pytest.fail("a card-only difference must not write"),
    )
    from agent.title_generator import maybe_refresh_title_drift

    assert maybe_refresh_title_drift(
        SimpleNamespace(_session_db=db, session_id="sess-1", _on_session_title=None),
        db, "sess-1",
        [
            {"role": "user", "content": "fix the flaky park tests again"},
            {"role": "assistant", "content": "park suite still green", "finish_reason": "stop"},
        ],
        final_status="completed",
    ) is None
    assert db.get_session_title("sess-1") == CARDED


def test_as6_a_drift_regeneration_that_declines_keeps_the_title(monkeypatch, db):
    monkeypatch.setattr(
        title_generator, "_title_config",
        lambda: {"refresh_after_prompts": 1, "card_icons": "emoji"},
    )
    _session(db)
    db.set_auto_title("sess-1", CARDED, source="llm")
    monkeypatch.setattr(agent.judgment, "judge", lambda request, **kwargs: SimpleNamespace(
        answers={"fits": SimpleNamespace(noul=0.1)}
    ))
    # `<title/>` from the model is a decision: generate_title returns None and nothing else runs.
    monkeypatch.setattr(title_generator, "generate_title", lambda *a, **k: None)
    monkeypatch.setattr(
        db, "refresh_auto_title", lambda *a, **k: pytest.fail("<title/> must not write")
    )
    monkeypatch.setattr(
        title_generator, "request_title_card", lambda *a, **k: pytest.fail("<title/> needs no card")
    )
    from agent.title_generator import maybe_refresh_title_drift

    assert maybe_refresh_title_drift(
        SimpleNamespace(_session_db=db, session_id="sess-1", _on_session_title=None),
        db, "sess-1",
        [
            {"role": "user", "content": "fix the flaky park tests again"},
            {"role": "assistant", "content": "park suite still green", "finish_reason": "stop"},
        ],
        final_status="completed",
    ) is None
    assert db.get_session_title("sess-1") == CARDED


def test_as7_a_manual_title_never_starts_a_fork_or_a_card_request(monkeypatch, db):
    _configure(monkeypatch)
    transport = _Transport()
    monkeypatch.setattr(title_generator, "call_llm", transport)
    monkeypatch.setattr(
        title_generator, "run_title_fork",
        lambda *a, **k: pytest.fail("a user title must never start a fork"),
    )
    _session(db)
    db.set_session_title("sess-1", "My own name")

    maybe_auto_title(
        db, "sess-1", OPENER, [{"role": "user", "content": OPENER}], agent=_fork_parent()
    )
    auto_title_session(db, "sess-1", OPENER, agent=_fork_parent())
    wait_for_title_upgrades(2)

    assert transport.calls == []
    assert db.get_session_title("sess-1") == "My own name"
    assert db.get_session_title_source("sess-1") == "user"


def test_as7_drift_skips_a_manual_title(monkeypatch, db):
    monkeypatch.setattr(
        title_generator, "_title_config",
        lambda: {"refresh_after_prompts": 1, "card_icons": "emoji"},
    )
    _session(db)
    db.set_session_title("sess-1", "My own name")
    monkeypatch.setattr(agent.judgment, "judge", lambda *a, **k: pytest.fail("user titles are final"))
    monkeypatch.setattr(
        title_generator, "request_title_card", lambda *a, **k: pytest.fail("no card for a user title")
    )
    from agent.title_generator import maybe_refresh_title_drift

    assert maybe_refresh_title_drift(
        SimpleNamespace(_session_db=db, session_id="sess-1", _on_session_title=None),
        db, "sess-1",
        [
            {"role": "user", "content": "fix the flaky park tests"},
            {"role": "assistant", "content": "done", "finish_reason": "stop"},
        ],
        final_status="completed",
    ) is None
    assert db.get_session_title("sess-1") == "My own name"


def test_as7_naming_paths_render_with_nerd_fonts_off(monkeypatch, db):
    _configure(monkeypatch)
    transport = _Transport(f"<title>{PLAIN}</title>", "<title>🅿️ PARK</title>")
    monkeypatch.setattr(title_generator, "call_llm", transport)
    monkeypatch.setattr(title_generator, "run_title_fork", lambda *a, **k: "")
    renders: list[tuple[str, dict]] = []
    real_render = render_request

    def spy(key, context):
        renders.append((key, dict(context)))
        return real_render(key, context)

    monkeypatch.setattr("agent.prompts.renderer.render_request", spy)
    _session(db)

    auto_title_session(db, "sess-1", OPENER, agent=_fork_parent())

    naming_renders = [(key, context) for key, context in renders if key in ("title_fork", "title_card")]
    assert {key for key, _ in naming_renders} == {"title_fork", "title_card"}
    assert all(context["nerd_fonts"] is False for _, context in naming_renders)
    for _, context in naming_renders:
        assert "card_icons" not in context
    assert db.get_session_title("sess-1") == CARDED
