"""Choice reranker shared by capability search (ADR-18 D9)."""

from __future__ import annotations

from typing import Any

from agent.judgment import choice
from agent.judgment.service import BatchItemResult, BatchResult
from agent.judgment.types import ChoiceAnswer, JudgmentResult


def _result(answers: dict[str, str]) -> JudgmentResult:
    return JudgmentResult(
        kind="native", api="native", provider="p", model="m",
        answers={
            qid: ChoiceAnswer(label, {}, 0.9) for qid, label in answers.items()
        },
    )


def test_question_specs_chunk_candidates_and_add_none():
    candidates = [(f"tool_{index}", f"description {index}") for index in range(20)]
    specs = choice._question_specs("tool-choice", candidates, max_questions=2)

    assert list(specs) == ["choice_1", "choice_2"]
    first = specs["choice_1"]["criteria"]
    assert list(first)[:8] == [f"tool_{index}" for index in range(8)]
    assert first[choice.NONE_LABEL]
    assert list(specs["choice_2"]["criteria"])[:8] == [
        f"tool_{index}" for index in range(8, 16)]


def test_ask_choices_batches_queries_and_merges_picks(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_judge_batch(items, *, questions, purpose, timeout_ms, min_ok):
        seen["items"] = items
        seen["questions"] = questions
        seen["purpose"] = purpose
        seen["timeout_ms"] = timeout_ms
        seen["min_ok"] = min_ok
        return BatchResult(
            results=(
                BatchItemResult(key=0, ok=True, result=_result(
                    {"choice_1": "tool_b", "choice_2": choice.NONE_LABEL})),
                BatchItemResult(key=1, ok=True, result=_result(
                    {"choice_1": "tool_a", "choice_2": "tool_c"})),
            ),
            min_ok_met=True,
        )

    monkeypatch.setattr(choice, "judge_batch", fake_judge_batch)
    picks = choice.ask_choices(
        ["first", "second"],
        [("tool_a", "a"), ("tool_b", "b")],
        question_file="tool-choice",
        purpose="tool_choice",
        max_questions=1,
    )

    assert picks == {0: ["tool_b"], 1: ["tool_a"]}
    assert [item.state["query"] for item in seen["items"]] == ["first", "second"]
    assert seen["purpose"] == "tool_choice"
    assert seen["min_ok"] == 1
    assert seen["timeout_ms"] == choice.JUDGE_TIMEOUT_MS
    assert "none" in seen["questions"]["choice_1"]["criteria"]


def test_ask_choices_returns_empty_on_judgment_failure(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("judge down")

    monkeypatch.setattr(choice, "judge_batch", fail)
    assert choice.ask_choices(
        ["q"], [("tool_a", "a")], question_file="tool-choice", purpose="tool_choice") == {}


def test_merge_ranked_keeps_first_occurrence_order():
    assert choice.merge_ranked(["b", "a"], ["a", "c"], ["b"]) == ["b", "a", "c"]
