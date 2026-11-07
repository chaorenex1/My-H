from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.judgment import load_questions, render_question
from agent.judgment.text import render_judgment_prompt, render_retry_prompt
from agent.judgment.types import (
    ChoiceQuestion,
    JudgmentRequest,
    JudgmentValidationError,
    NoulQuestion,
    validate_request,
)

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "tests/fixtures/judgment/omp-18.14.0"


def _auto_graph_contexts() -> dict[str, dict[str, object]]:
    return {
        "order": {"column": "latency"},
        "reference": {"column": "latency", "repeats": True},
        "pair": {"first": "before", "second": "after"},
        "column": {"column": "latency"},
        "name": {"column": "runner", "label": "Model"},
        "group": {"column": "model"},
        "move": {"columns": ["before", "after"], "arrow": True},
        "polarity": {"column": "latency"},
        "rowPolarity": {"row": "cache"},
    }


def _auto_graph_labels() -> dict[str, dict[str, str | None]]:
    return {
        "label": {"baseline": None, "candidate": None},
        "rowPolarity": {
            "higher": "A larger value is better.",
            "lower": "A smaller value is better.",
            "neutral": "Neither: it describes, counts or identifies.",
        },
    }


def test_omp_text_prompt_fixture_is_byte_exact():
    fixture = json.loads((_FIXTURES / "text-rendering.json").read_text(encoding="utf-8"))
    source = fixture["input"]
    questions = load_questions(
        source["questions"], contexts=source["contexts"], labels=source["labels"])
    system, user = render_judgment_prompt(
        JudgmentRequest(state=source["state"], questions=questions))

    assert system == fixture["expected"]["system"]
    assert user == fixture["expected"]["user"]
    assert render_retry_prompt(system, multi=True) == fixture["expected"]["retry"]


def test_bundled_auto_graph_questions_render_and_accept_caller_labels():
    questions = load_questions(
        "auto-graph", contexts=_auto_graph_contexts(), labels=_auto_graph_labels())

    assert isinstance(questions["label"], ChoiceQuestion)
    assert dict(questions["label"].criteria) == {"baseline": None, "candidate": None}
    assert isinstance(questions["rowPolarity"], ChoiceQuestion)
    assert dict(questions["rowPolarity"].criteria)["higher"] == "A larger value is better."
    assert 'measuring "latency"' in questions["order"].instructions
    assert '"before", "after", written as before → after' in questions["move"].instructions
    validate_request(JudgmentRequest(state={"table": "data"}, questions=questions))


def test_question_loading_requires_labels_for_empty_criteria():
    with pytest.raises(JudgmentValidationError, match="labels"):
        load_questions({"row": {"instructions": "Which row?", "criteria": {}}})


def test_question_loading_rejects_unresolved_instruction_context():
    with pytest.raises(JudgmentValidationError, match="context"):
        load_questions({"row": {"instructions": "Choose {{column}}."}})


def test_render_question_uses_supported_handlebars_subset_without_mutating_source():
    source = ChoiceQuestion(
        'Order {{#each columns}}"{{this}}"{{#unless @last}}, {{/unless}}{{/each}}'
        "{{#if arrow}} as before → after{{/if}}",
        {"yes": None},
    )

    rendered = render_question(source, {"columns": ["old", "new"], "arrow": True})

    assert rendered.instructions == 'Order "old", "new" as before → after'
    assert source.instructions != rendered.instructions


def test_render_question_rejects_missing_context_and_unsupported_helpers():
    with pytest.raises(JudgmentValidationError, match="column"):
        render_question(NoulQuestion("Pick {{column}}."), {})
    with pytest.raises(JudgmentValidationError, match="with"):
        render_question(NoulQuestion("{{#with row}}pick{{/with}}"), {"row": {}})


def test_load_questions_reads_an_explicit_user_file(tmp_path: Path):
    path = tmp_path / "custom.json"
    path.write_text(
        json.dumps({"choice": {"instructions": "Pick one", "criteria": {"yes": None}}}),
        encoding="utf-8",
    )

    questions = load_questions(path)

    assert isinstance(questions["choice"], ChoiceQuestion)
    assert dict(questions["choice"].criteria) == {"yes": None}


def test_all_bundled_question_files_load_and_validate():
    cases = (
        (
            "auto-thinking",
            {"level": {"withMax": False}, "solutionSpace": {"withMax": True}},
            {"solutionSpace": {"bounded": None, "open": "Requires open-ended reasoning."}},
        ),
        ("auto-prewalk", {}, None),
        ("title-drift", {}, None),
        ("unexpected-stop", {}, None),
        ("git-stage", {}, None),
    )

    for name, contexts, labels in cases:
        questions = load_questions(name, contexts=contexts, labels=labels)
        validate_request(JudgmentRequest(state={"work": "example"}, questions=questions))


def test_bundled_question_name_is_not_shadowed_by_a_working_directory_file(
    monkeypatch, tmp_path: Path,
):
    (tmp_path / "auto-graph.json").write_text(
        json.dumps({"wrong": {"instructions": "Wrong file"}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    questions = load_questions(
        "auto-graph.json", contexts=_auto_graph_contexts(), labels=_auto_graph_labels())

    assert "kind" in questions and "wrong" not in questions
