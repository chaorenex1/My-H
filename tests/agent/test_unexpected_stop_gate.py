"""Smart unexpected-stop detection (ADR-17 P5).

A text-only answer that announces work it never starts earns one judged retry; the
mechanical/none modes never pay for a judge, and every failure ends the turn silently.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.judgment as judgment_pkg
import agent.turn_stop_gates as gates
import hermes_cli.config as hermes_config
from agent.judgment import JudgmentError, NoulAnswer

_PROMPT = Path(gates.__file__).resolve().parent / "prompts" / "unexpected-stop-retry.md"


class _FakeJudge:
    def __init__(self, probability: float = 0.0, exc: BaseException | None = None) -> None:
        self.probability = probability
        self.exc = exc
        self.requests: list = []
        self.calls: list[dict] = []

    def judge(self, request, **kwargs):
        self.requests.append(request)
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(answers={"stopped": NoulAnswer(self.probability)})


class _NullAdvisor:
    def on_step(self, messages, *, terminal):
        return None

    def take_continuation(self):
        return None


class _Agent:
    def __init__(self) -> None:
        self._turn_file_mutation_paths: set = set()
        self._session_messages: list = []
        self.session_id = "sess-unexpected-stop"
        self.plugin_message_callback = None
        self.quiet_mode = True
        self.advisor = _NullAdvisor()

    def _interim_content_was_streamed(self, text):
        return False

    def _emit_interim_assistant_message(self, message):
        return None

    def _flush_messages_to_session_db(self, messages, history=None):
        return None


def _set_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        hermes_config,
        "load_config_readonly",
        lambda: {"agent": {"unexpected_stop_detection": mode}},
    )


def _use_judge(monkeypatch, fake: _FakeJudge) -> _FakeJudge:
    monkeypatch.setattr(judgment_pkg, "resolve_judge", lambda **kwargs: fake)
    return fake


def _stopped_answer(text: str = "I'll run the tests now.") -> str:
    return text


@pytest.mark.parametrize("mode", ["mechanical", "none"])
def test_mechanical_and_none_modes_never_call_the_judge(monkeypatch, mode):
    fake = _use_judge(monkeypatch, _FakeJudge(0.99))
    _set_mode(monkeypatch, mode)
    agent = _Agent()

    nudge = gates._unexpected_stop_nudge(
        agent,
        {"role": "assistant", "content": _stopped_answer()},
        _stopped_answer(),
        [{"role": "user", "content": "run the tests"}],
    )

    assert nudge is None
    assert fake.requests == []
    assert getattr(agent, "_unexpected_stop_nudges", 0) == 0


def test_smart_mode_flags_and_returns_the_prompt_file_text(monkeypatch):
    fake = _use_judge(monkeypatch, _FakeJudge(0.75))
    _set_mode(monkeypatch, "smart")
    agent = _Agent()
    messages = [{"role": "user", "content": "fix the failing test"}]

    nudge = gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": _stopped_answer()}, _stopped_answer(), messages,
    )

    expected = (_PROMPT.read_text(encoding="utf-8")
                .replace("{{retryCount}}", "1").replace("{{maxRetries}}", "1"))
    assert nudge == expected
    assert "{{" not in nudge
    assert agent._unexpected_stop_nudges == 1
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.state == {
        "message": _stopped_answer(), "request": "fix the failing test",
    }
    assert fake.calls[0]["purpose"] == "unexpected-stop"


def test_threshold_is_inclusive_at_one_half(monkeypatch):
    _use_judge(monkeypatch, _FakeJudge(0.5))
    _set_mode(monkeypatch, "smart")
    agent = _Agent()

    nudge = gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": "I'll fix it now."}, "I'll fix it now.",
        [{"role": "user", "content": "fix it"}],
    )

    assert nudge is not None
    assert agent._unexpected_stop_nudges == 1


def test_below_threshold_ends_the_turn(monkeypatch):
    _use_judge(monkeypatch, _FakeJudge(0.49))
    _set_mode(monkeypatch, "smart")
    agent = _Agent()

    nudge = gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": "Done and verified."}, "Done and verified.",
        [{"role": "user", "content": "fix it"}],
    )

    assert nudge is None
    assert getattr(agent, "_unexpected_stop_nudges", 0) == 0


@pytest.mark.parametrize("exc", [JudgmentError("judge unavailable"), RuntimeError("boom")])
def test_judge_failure_ends_silently(monkeypatch, exc):
    _use_judge(monkeypatch, _FakeJudge(exc=exc))
    _set_mode(monkeypatch, "smart")
    agent = _Agent()

    nudge = gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": "I'll do it now."}, "I'll do it now.",
        [{"role": "user", "content": "do it"}],
    )

    assert nudge is None
    assert getattr(agent, "_unexpected_stop_nudges", 0) == 0


def test_interrupted_turn_is_not_judged(monkeypatch):
    fake = _use_judge(monkeypatch, _FakeJudge(0.99))
    _set_mode(monkeypatch, "smart")
    agent = _Agent()
    agent._interrupt_requested = True

    nudge = gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": "I'll do it now."}, "I'll do it now.",
        [{"role": "user", "content": "do it"}],
    )

    assert nudge is None
    assert fake.requests == []


def test_tool_call_turn_is_not_judged(monkeypatch):
    fake = _use_judge(monkeypatch, _FakeJudge(0.99))
    _set_mode(monkeypatch, "smart")
    agent = _Agent()

    nudge = gates._unexpected_stop_nudge(
        agent,
        {"role": "assistant", "content": "I'll do it now.", "tool_calls": [{"id": "call-1"}]},
        "I'll do it now.",
        [{"role": "user", "content": "do it"}],
    )

    assert nudge is None
    assert fake.requests == []


@pytest.mark.parametrize("text", ["", "   \n\t "])
def test_empty_text_is_not_judged(monkeypatch, text):
    fake = _use_judge(monkeypatch, _FakeJudge(0.99))
    _set_mode(monkeypatch, "smart")
    agent = _Agent()

    nudge = gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": text}, text,
        [{"role": "user", "content": "do it"}],
    )

    assert nudge is None
    assert fake.requests == []


def test_one_judged_retry_per_turn(monkeypatch):
    fake = _use_judge(monkeypatch, _FakeJudge(0.99))
    _set_mode(monkeypatch, "smart")
    agent = _Agent()
    messages = [{"role": "user", "content": "do it"}]

    first = gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": "I'll do it now."}, "I'll do it now.", messages,
    )
    second = gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": "I'll do it now."}, "I'll do it now.", messages,
    )

    assert first is not None
    assert second is None
    assert len(fake.requests) == 1
    assert agent._unexpected_stop_nudges == 1


def test_second_turn_starts_with_a_fresh_counter(monkeypatch):
    fake = _use_judge(monkeypatch, _FakeJudge(0.99))
    _set_mode(monkeypatch, "smart")
    agent = _Agent()
    messages = [{"role": "user", "content": "do it"}]

    assert gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": "I'll do it now."}, "I'll do it now.", messages,
    ) is not None
    assert agent._unexpected_stop_nudges == 1

    from agent.turn_context import _PER_TURN_RESET_STATE

    reset = dict(_PER_TURN_RESET_STATE)
    assert reset["_unexpected_stop_nudges"] == 0
    for name, value in _PER_TURN_RESET_STATE:
        setattr(agent, name, value)

    nudge = gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": "I'll do it now."}, "I'll do it now.", messages,
    )

    assert nudge is not None
    assert len(fake.requests) == 2
    assert agent._unexpected_stop_nudges == 1


def test_request_is_truncated_to_2000_chars(monkeypatch):
    fake = _use_judge(monkeypatch, _FakeJudge(0.9))
    _set_mode(monkeypatch, "smart")
    agent = _Agent()
    long_request = "x" * 2500

    gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": "I'll do it now."}, "I'll do it now.",
        [{"role": "user", "content": long_request}],
    )

    assert fake.requests[0].state["request"] == long_request[:2000]


def test_synthetic_rows_are_not_the_operator_request(monkeypatch):
    fake = _use_judge(monkeypatch, _FakeJudge(0.9))
    _set_mode(monkeypatch, "smart")
    agent = _Agent()
    messages = [
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "I'll start now."},
        {"role": "user", "content": "previous nudge", "_unexpected_stop_synthetic": True},
    ]

    gates._unexpected_stop_nudge(
        agent, {"role": "assistant", "content": "I'll start now."}, "I'll start now.", messages,
    )

    assert fake.requests[0].state["request"] == "do the thing"


def _run_stop_gate(monkeypatch):
    _use_judge(monkeypatch, _FakeJudge(0.9))
    _set_mode(monkeypatch, "smart")
    for name, fake in {
        "_late_lsp_verdicts": lambda agent: None,
        "_plugin_continuation": lambda agent: None,
        "_verify_on_stop_nudge": lambda agent: None,
        "_pre_verify_nudge": lambda agent, response, attempt: None,
        "_kanban_stop_nudge": lambda agent, messages: None,
    }.items():
        monkeypatch.setattr(gates, name, fake)

    agent = _Agent()
    messages = [{"role": "user", "content": "fix the bug"}]
    final_msg = {"role": "assistant", "content": "I'll fix it now."}
    verdict = gates.apply_stop_gates(
        agent, final_msg, final_response="I'll fix it now.", messages=messages,
        conversation_history=[], pending_verification_response=None,
        pending_verification_response_previewed=None,
    )
    return agent, messages, verdict


def test_stop_gate_continues_with_interim_answer_and_synthetic_nudge(monkeypatch):
    agent, messages, verdict = _run_stop_gate(monkeypatch)

    expected = (_PROMPT.read_text(encoding="utf-8")
                .replace("{{retryCount}}", "1").replace("{{maxRetries}}", "1"))
    assert verdict.continue_turn is True
    assert verdict.final_response is None
    assert verdict.pending_verification_response == "I'll fix it now."
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["content"] == "I'll fix it now."
    assert messages[-2]["finish_reason"] == "unexpected_stop"
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == expected
    assert messages[-1]["_unexpected_stop_synthetic"] is True
    assert agent._session_messages is messages
    assert agent._unexpected_stop_nudges == 1


def test_nudge_row_never_replays_as_a_durable_user_row(monkeypatch):
    """Registered with every other stop-gate nudge: persistence and compression skip it."""
    from agent.conversation_compression import _is_real_user_message
    from agent.session_persistence import _is_ephemeral_scaffolding
    from agent.turn_final_response import _EPHEMERAL_SCAFFOLDING_FLAGS

    _agent, messages, _verdict = _run_stop_gate(monkeypatch)
    row = messages[-1]

    assert row["_unexpected_stop_synthetic"] is True
    assert _is_ephemeral_scaffolding(row) is True
    assert _is_real_user_message(row) is False
    assert "_unexpected_stop_synthetic" in _EPHEMERAL_SCAFFOLDING_FLAGS
