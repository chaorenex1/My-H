"""ADR-12 D6 / ADR-17 P5: the auto-graph planner's questions and answer mapping."""

from __future__ import annotations

import pytest

from agent import auto_graph
from agent.judgment import ChoiceAnswer, NoulAnswer
from agent.judgment.types import JudgmentResult
from agent.usage_pricing import CanonicalUsage


def _cell(text: str, kind: str = "text", value: float | None = None) -> dict:
    cell = {"kind": kind, "text": text}
    if value is not None:
        cell["value"] = value
    return cell


def _table() -> dict:
    return {
        "columns": [
            {"index": 0, "header": "Model", "role": "label",
             "cells": [_cell("A"), _cell("B"), _cell("C"), _cell("D")]},
            {"index": 1, "header": "Harness", "role": "label",
             "cells": [_cell("base"), _cell("base"), _cell("var"), _cell("var")]},
            {"index": 2, "header": "Latency", "role": "measure",
             "dim": "duration", "unit": "ms", "arrowShare": 0,
             "cells": [_cell("10", "number", 10), _cell("12", "number", 12),
                       _cell("8", "number", 8), _cell("20", "number", 20)]},
            {"index": 3, "header": "Cost", "role": "measure",
             "dim": "currency", "unit": "$", "mixed": True, "arrowShare": 0,
             "cells": [_cell("$5", "number", 5), _cell("$6", "number", 6),
                       _cell("$4", "number", 4), _cell("$9", "number", 9)]},
            {"index": 4, "header": "Memory", "role": "measure",
             "dim": "bytes", "unit": "MB", "arrowShare": 0,
             "cells": [_cell("100", "number", 100), _cell("120", "number", 120),
                       _cell("90", "number", 90), _cell("150", "number", 150)]},
            {"index": 5, "header": "Score", "role": "measure",
             "dim": "percent", "unit": "%", "arrowShare": 0,
             "cells": [_cell("80", "number", 80), _cell("85", "number", 85),
                       _cell("70", "number", 70), _cell("90", "number", 90)]},
        ],
        "measures": [2, 3, 4, 5],
        "rows": [0, 1, 2, 3],
    }


def _guess() -> dict:
    return {
        "kind": "heatmap",
        "label": 0,
        "series": [2, 3, 4, 5],
        "transpose": False,
        "reference": {"column": 1, "value": "base"},
        "polarity": {"2": "lower"},
    }


def _choice(choice: str, probability: float = 0.9) -> ChoiceAnswer:
    return ChoiceAnswer(choice, {choice: probability}, probability)


def _answers() -> dict:
    return {
        "kind": _choice("bar"),
        "label": _choice("Harness"),
        "c2": NoulAnswer(0.9),
        "c3": NoulAnswer(0.2),
        "c4": NoulAnswer(0.9),
        "c5": NoulAnswer(0.1),
        "transpose": NoulAnswer(0.9),
        "shading": _choice("series", 0.8),
        "reference": _choice("base", 0.9),
        "order": _choice("descending", 0.7),
        "p2": _choice("higher", 0.8),
        "rivals": _choice("rows", 0.8),
        "focus": _choice("C", 0.8),
        "n0": NoulAnswer(0.9),
        "g0": NoulAnswer(0.8),
    }


def _result(answers: dict) -> JudgmentResult:
    return JudgmentResult(
        kind="native", api="typesafe", provider="typesafe", model="jev-test",
        answers=answers,
        usage=CanonicalUsage(input_tokens=3, output_tokens=1),
    )


def test_judge_request_renders_contexts_labels_and_column_types():
    state, questions = auto_graph.judge_request("| Model | Latency |", _table(), _guess())

    assert state["table"] == "| Model | Latency |"
    assert state["columns"] == [
        {"name": "Model", "type": "text"},
        {"name": "Harness", "type": "text"},
        {"name": "Latency", "type": "number (duration)"},
        {"name": "Cost", "type": "number (mixed units)"},
        {"name": "Memory", "type": "number (bytes)"},
        {"name": "Score", "type": "number (percent)"},
    ]

    assert 'measuring "Latency"' in questions["order"].instructions
    assert "(it may repeat once per group" in questions["reference"].instructions
    assert 'the column "Harness" join that name' in questions["n1"].instructions
    assert 'a "Model" value' in questions["n1"].instructions
    assert dict(questions["label"].criteria) == {"Model": None, "Harness": None}
    assert dict(questions["reference"].criteria) == {"base": None, "var": None, "none": None}
    assert dict(questions["focus"].criteria) == {
        "A": None, "B": None, "C": None, "D": None, "none": "No single row stands out.",
    }
    assert dict(questions["p2"].criteria)["higher"] == "A larger value is better."
    assert questions["kind"].instructions.startswith("The state is a Markdown table")


def test_plan_table_chart_maps_confident_answers_and_passes_timeout(monkeypatch):
    seen: dict = {}

    def fake_judge(request, **kwargs):
        seen["request"] = request
        seen["kwargs"] = kwargs
        return _result(_answers())

    monkeypatch.setattr("agent.judgment.service.judge", fake_judge)
    guess = _guess()
    result = auto_graph.plan_table_chart({
        "markdown": "| Model | Latency |", "table": _table(), "guess": guess,
    })

    assert seen["kwargs"] == {"purpose": "auto-graph", "timeout_ms": 15_000}
    assert seen["request"].state["table"] == "| Model | Latency |"
    # The judge state carries no answer or plan, and the result carries no state at all.
    assert set(result) == {"plan", "source"}
    assert "answers" not in result["plan"] and "markdown" not in result["plan"]

    plan = result["plan"]
    assert result["source"] == "judge"
    assert plan["kind"] == "bar"
    assert plan["label"] == 1
    assert plan["series"] == [2, 4]
    assert plan["transpose"] is True
    assert plan["shading"] == "series"
    assert plan["reference"] == {"column": 1, "value": "base"}
    assert plan["order"] == {"column": 2, "direction": "descending"}
    assert plan["polarity"] == {"2": "higher"}
    assert plan["rivals"] == "rows"
    assert plan["focus"] == [2]
    assert plan["qualifiers"] == [0]
    assert plan["group"] == 0
    assert "title" not in plan
    assert guess == _guess()


def test_none_kind_means_no_chart(monkeypatch):
    monkeypatch.setattr(
        "agent.judgment.service.judge", lambda *_a, **_k: _result({"kind": _choice("none")}))
    result = auto_graph.plan_table_chart({
        "markdown": "| Model | Latency |", "table": _table(), "guess": _guess(),
    })

    assert result == {"plan": None, "source": "judge"}


def test_unsure_or_malformed_answers_keep_the_local_guess():
    guess = _guess()
    answers = {
        "kind": _choice("not-a-kind"),
        "label": _choice("not-a-column"),
        "c2": NoulAnswer(0.1),
        "c3": {"choice": "Cost", "probabilities": {"Cost": 1.0}},
        "transpose": NoulAnswer(0.1),
        "shading": _choice("series", 0.2),
        "reference": _choice("base", 0.2),
        "order": _choice("descending", 0.2),
        "p2": _choice("higher", 0.2),
        "rivals": _choice("rows", 0.2),
        "focus": _choice("C", 0.2),
    }

    plan = auto_graph.plan_from_answers(_table(), guess, answers)

    assert plan == guess
    assert guess["polarity"] == {"2": "lower"}


def test_row_polarity_maps_for_metric_rows():
    table = _table()
    guess = {"kind": "change", "label": 0, "series": [2, 3], "transpose": False}
    plan = auto_graph.plan_from_answers(
        table, guess, {"kind": _choice("change"), "q0": _choice("lower", 0.8)})

    assert plan["rowPolarity"] == {"0": "lower"}
    assert plan["kind"] == "change"


def test_title_maps_when_the_client_offers_takeaways():
    table = _table()
    table["takeaways"] = [{"key": "k1", "text": "A beats B"}]
    _, questions = auto_graph.judge_request("| Model | Latency |", table, _guess())
    assert dict(questions["title"].criteria) == {"k1": "A beats B", "none": "No title: none of these is the table's point."}

    plan = auto_graph.plan_from_answers(
        table, _guess(), {"kind": _choice("bar"), "title": _choice("k1", 0.6)})

    assert plan["title"] == "A beats B"


def test_judge_failure_and_bad_payload_fall_back_to_local(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("judge down")

    monkeypatch.setattr("agent.judgment.service.judge", fail)
    payload = {"markdown": "| Model | Latency |", "table": _table(), "guess": _guess()}

    assert auto_graph.plan_table_chart(payload) == {"plan": None, "source": "local"}
    assert auto_graph.plan_table_chart({"markdown": "x"}) == {"plan": None, "source": "local"}
    assert auto_graph.plan_table_chart({}) == {"plan": None, "source": "local"}


def test_markdown_is_truncated_in_the_judge_state(monkeypatch):
    seen: dict = {}

    def fake_judge(request, **kwargs):
        seen["state"] = request.state
        return _result({"kind": _choice("none")})

    monkeypatch.setattr("agent.judgment.service.judge", fake_judge)
    auto_graph.plan_table_chart({
        "markdown": "x" * 5_000, "table": _table(), "guess": _guess(),
    })

    assert seen["state"]["table"] == "x" * auto_graph.MAX_TABLE_CHARS


def test_empty_table_does_not_ask_the_judge(monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("an empty table has nothing to judge")

    monkeypatch.setattr("agent.judgment.service.judge", unexpected)
    result = auto_graph.plan_table_chart(
        {"markdown": "", "table": {"columns": []}, "guess": {"kind": "bar", "label": None, "series": []}})

    assert result == {"plan": None, "source": "local"}


def test_auto_mode_skips_the_judge_without_a_native_route(monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("auto mode must not spend a text judgment")

    monkeypatch.setattr(auto_graph, "_has_native_judge", lambda: False)
    monkeypatch.setattr("agent.judgment.service.judge", unexpected)
    result = auto_graph.plan_table_chart({
        "markdown": "| Model | Latency |",
        "table": _table(),
        "guess": _guess(),
        "mode": "auto",
    })
    assert result == {"plan": None, "source": "local"}
