from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.judgment import chat as chat_module
from agent.judgment.chat import ChatTextBackend
from agent.judgment.config import JudgmentSettings
from agent.judgment.text import (
    parse_choice_reply,
    parse_noul_reply,
    parse_score_reply,
    parse_text_answers,
    render_judgment_prompt,
    split_answer_lines,
)
from agent.judgment.types import (
    ChoiceAnswer,
    ChoiceQuestion,
    JudgmentParseError,
    JudgmentRequest,
    JudgmentValidationError,
    NoulAnswer,
    NoulQuestion,
    ScoreQuestion,
)


def _response(text: str, model: str = "judge-model") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        model=model,
        usage={"prompt_tokens": 12, "completion_tokens": 3},
    )


def test_render_orders_questions_and_keeps_state_out_of_system():
    request = JudgmentRequest(
        state={"output": "answer", "content": "hello & <world>"},
        questions={
            "q0": NoulQuestion("Does this violate the rule?"),
            "q1": ChoiceQuestion("Pick a level", {"high": "h", "xhigh": "xh"}),
        },
    )
    system, user = render_judgment_prompt(request)
    assert "Question `q0`: Does this violate the rule?" in system
    assert "Question `q1`: Pick a level" in system
    assert "`high`: h" in system and "`xhigh`: xh" in system
    assert "hello" not in system
    assert "<state>" not in user
    assert "<output>answer</output>" in user
    assert "&amp;" in user and "&lt;world&gt;" in user
    assert "Answer one line per question" in user


def test_single_question_prompt_uses_that_question_format():
    request = JudgmentRequest(
        state="hello",
        questions={"q0": ScoreQuestion("Rate it", ("low", "mid", "high"))},
    )
    system, user = render_judgment_prompt(request)
    assert "Question `q0`" not in system
    assert "Levels, lowest to highest:" in system
    assert "`0`: low" in system and "`2`: high" in system
    assert "Answer with exactly one level number: `0`, `1`, `2`." in user


@pytest.mark.parametrize(
    "text,labels,expected",
    [
        ("xhigh", ["high", "xhigh"], "xhigh"),
        ("high and xhigh", ["high", "xhigh"], "high"),
        ("XHIGH", ["high", "xhigh"], "xhigh"),
        ("nothigh", ["high"], None),
        ("not-high", ["high"], "high"),
        ("v2 high", ["high"], "high"),
    ],
)
def test_choice_parsing_uses_whole_words_and_earliest_longest_tie(text, labels, expected):
    assert parse_choice_reply(text, labels) == expected


def test_noul_parsing_uses_first_yes_or_no():
    assert parse_noul_reply("NO, false") is False
    assert parse_noul_reply("yes but no") is True
    assert parse_noul_reply("maybe") is None


def test_score_parsing_rejects_glued_integers_and_decimals():
    assert parse_score_reply("v2 1.5 2.", 3) == 2
    assert parse_score_reply("1.5", 3) is None
    assert parse_score_reply("7", 3) is None


def test_split_answer_lines_accepts_separators_quotes_and_ignores_unknown_ids():
    parsed = split_answer_lines(
        "preamble\nq0: yes\n`q1` = xhigh\nunknown: nope\nq2 - 2", ["q0", "q1", "q2"])
    assert parsed == {"q0": "yes", "q1": "xhigh", "q2": "2"}


def test_parse_text_answers_fails_on_missing_line():
    request = JudgmentRequest(
        state="x",
        questions={"q0": NoulQuestion("a"), "q1": NoulQuestion("b")},
    )
    with pytest.raises(JudgmentParseError, match="q1"):
        parse_text_answers("q0: yes", request)


def test_chat_backend_calls_judge_task_with_temperature_zero(monkeypatch):
    calls: list[dict] = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _response("yes")

    monkeypatch.setattr(chat_module, "call_llm", fake_call_llm)
    backend = ChatTextBackend(JudgmentSettings(parse_retries=0), {
        "auxiliary": {"judge": {"provider": "auto", "model": "", "max_tokens": 4096}},
    })
    result = backend.judge(JudgmentRequest(
        state="x", questions={"q0": NoulQuestion("Is it?")}))

    assert isinstance(result.answers["q0"], NoulAnswer)
    assert result.answers["q0"].noul == 1.0
    assert calls[0]["task"] == "judge"
    assert calls[0]["temperature"] == 0
    assert calls[0]["max_tokens"] == 4096
    assert calls[0]["messages"][0]["role"] == "system"
    assert calls[0]["messages"][1]["role"] == "user"


def test_chat_backend_retries_parse_failures_then_succeeds(monkeypatch):
    replies = iter(["not an answer", "xhigh"])
    calls: list[dict] = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _response(next(replies))

    monkeypatch.setattr(chat_module, "call_llm", fake_call_llm)
    backend = ChatTextBackend(JudgmentSettings(parse_retries=2), {})
    result = backend.judge(JudgmentRequest(
        state="x",
        questions={"q0": ChoiceQuestion("Pick", {"high": "h", "xhigh": "xh"})}))

    assert isinstance(result.answers["q0"], ChoiceAnswer)
    assert result.answers["q0"].choice == "xhigh"
    assert len(calls) == 2
    assert "Classification retry:" in calls[1]["messages"][0]["content"]


def test_chat_backend_raises_after_parse_retry_budget(monkeypatch):
    calls: list[dict] = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _response("garbage")

    monkeypatch.setattr(chat_module, "call_llm", fake_call_llm)
    backend = ChatTextBackend(JudgmentSettings(parse_retries=2), {})
    with pytest.raises(JudgmentParseError):
        backend.judge(JudgmentRequest(
            state="x", questions={"q0": NoulQuestion("Is it?")}))
    assert len(calls) == 3


def test_chat_backend_rejects_images_before_calling(monkeypatch):
    monkeypatch.setattr(chat_module, "call_llm", lambda **kwargs: pytest.fail("must not call"))
    backend = ChatTextBackend(JudgmentSettings(), {})
    from agent.judgment.types import JudgmentImage

    with pytest.raises(JudgmentValidationError, match="not enabled"):
        backend.judge(JudgmentRequest(
            state="x", questions={"q0": NoulQuestion("Is it?")},
            images=(JudgmentImage("AAAA", "image/png"),)))


def test_response_text_falls_back_to_reasoning_when_content_is_empty():
    from agent.judgment.chat import response_text

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content="", reasoning="the answer is yes"))],
        model="thinking-model",
    )
    assert response_text(response) == "the answer is yes"


def test_response_text_strips_inline_think_blocks():
    from agent.judgment.chat import response_text

    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content="<think>no, wait</think>yes"))],
        model="thinking-model",
    )
    assert response_text(response) == "yes"


def test_chat_backend_never_records_usage_itself(monkeypatch):
    monkeypatch.setattr(chat_module, "call_llm", lambda **kwargs: _response("yes"))
    monkeypatch.setattr(
        "agent.aux_accounting.record_aux_usage",
        lambda *args, **kwargs: pytest.fail("text backend must not double-record"))
    result = ChatTextBackend(JudgmentSettings(parse_retries=0), {}).judge(
        JudgmentRequest(state="x", questions={"q0": NoulQuestion("Is it?")}))
    answer = result.answers["q0"]
    assert isinstance(answer, NoulAnswer)
    assert answer.noul == 1.0
