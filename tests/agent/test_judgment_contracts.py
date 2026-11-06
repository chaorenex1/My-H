from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.judgment.questions import questions_from_file
from agent.judgment.types import (
    BoxQuestion,
    ChoiceQuestion,
    NoulQuestion,
    ScoreQuestion,
)


_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "tests/fixtures/judgment/omp-18.14.0"
_SCHEMA_PATH = _ROOT / "agent/judgment/schemas/judgment-v1.schema.json"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _question_wire(question):
    common = {"instructions": question.instructions}
    if isinstance(question, ChoiceQuestion):
        return {"type": "choice", **common, "criteria": dict(question.criteria)}
    if isinstance(question, NoulQuestion):
        answer = {"type": "noul", **common}
        if question.criteria is not None:
            answer["criteria"] = dict(question.criteria)
        return answer
    if isinstance(question, ScoreQuestion):
        return {"type": "score", **common, "criteria": list(question.criteria)}
    if isinstance(question, BoxQuestion):
        return {"type": "box", **common, "image": question.image}
    raise AssertionError(f"unsupported question: {type(question).__name__}")


def _schema_validator(name: str):
    jsonschema = pytest.importorskip("jsonschema")
    registry = _json(_SCHEMA_PATH)
    schema = {
        "$schema": registry["$schema"],
        "$defs": registry["$defs"],
        "$ref": f"#/$defs/{name}",
    }
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def test_omp_question_file_fixture_replays_question_inference():
    fixture = _json(_FIXTURES / "question-inference.json")
    questions = questions_from_file(fixture["input"])

    assert {key: _question_wire(value) for key, value in questions.items()} == fixture["expected"]


def test_judge_and_batch_contract_fixtures_validate():
    _schema_validator("JudgeRequest").validate(_json(_FIXTURES / "judge-request.json"))
    _schema_validator("JudgeResult").validate(_json(_FIXTURES / "judge-result.json"))
    batch = _json(_FIXTURES / "judge-batch.json")
    _schema_validator("BatchRequest").validate(batch["request"])
    _schema_validator("BatchResult").validate(batch["result"])


def test_judge_contract_rejects_ambiguous_question_source_and_unsupported_batch_width():
    jsonschema = pytest.importorskip("jsonschema")
    request = _json(_FIXTURES / "judge-request.json")
    ambiguous = {**request, "questions_file": "auto-graph"}
    with pytest.raises(jsonschema.ValidationError):
        _schema_validator("JudgeRequest").validate(ambiguous)

    batch = _json(_FIXTURES / "judge-batch.json")["request"]
    over_limit = {**batch, "max_concurrency": 5}
    with pytest.raises(jsonschema.ValidationError):
        _schema_validator("BatchRequest").validate(over_limit)
