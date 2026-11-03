"""Question files: infer a typed question from a JSON shape.

A JSON import widens every string, so the kind follows from ``criteria`` rather than a
``type`` field — the same rule omp implements in
``packages/ai/src/judgment/question-file.ts``:

- absent ``criteria`` → ``noul``
- a list of levels, lowest first (at least two) → ``score``
- an object whose keys are only ``true``/``false`` → ``noul`` with a rubric
- any other object → ``choice`` over its labels (``null`` rubric allowed)
- an ``image`` index (with no ``criteria``) → ``box`` over that ``request.images`` entry
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.judgment.types import (
    BoxQuestion,
    ChoiceQuestion,
    JudgmentValidationError,
    NoulQuestion,
    Question,
    ScoreQuestion,
)


def question_from_spec(question_id: str, spec: Any) -> Question:
    if not isinstance(spec, Mapping):
        raise JudgmentValidationError(f"question {question_id!r} must be an object")
    instructions = spec.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise JudgmentValidationError(f"question {question_id!r} needs non-empty instructions")
    if "image" in spec:
        if "criteria" in spec:
            raise JudgmentValidationError(
                f"box question {question_id!r} takes an image index, not criteria")
        image = spec["image"]
        if isinstance(image, bool) or not isinstance(image, int) or image < 0:
            raise JudgmentValidationError(
                f"box question {question_id!r} image must be a non-negative index")
        return BoxQuestion(instructions, image)
    if "criteria" not in spec:
        return NoulQuestion(instructions)
    criteria = spec["criteria"]
    if isinstance(criteria, list):
        if len(criteria) < 2 or not all(isinstance(level, str) for level in criteria):
            raise JudgmentValidationError(
                f"score question {question_id!r} needs at least two string levels")
        return ScoreQuestion(instructions, tuple(criteria))
    if isinstance(criteria, Mapping):
        if not criteria:
            return ChoiceQuestion(instructions, {})
        labels = set(criteria)
        if labels <= {"true", "false"}:
            rubric: dict[str, str] = {}
            for label, text in criteria.items():
                if text is None:
                    continue
                if not isinstance(text, str):
                    raise JudgmentValidationError(
                        f"question {question_id!r} rubric {label!r} must be a string or null")
                rubric[str(label)] = text
            return NoulQuestion(instructions, rubric)
        choice: dict[str, str | None] = {}
        for label, text in criteria.items():
            if text is not None and not isinstance(text, str):
                raise JudgmentValidationError(
                    f"question {question_id!r} criterion {label!r} must be a string or null")
            choice[str(label)] = text
        return ChoiceQuestion(instructions, choice)
    raise JudgmentValidationError(
        f"question {question_id!r} criteria must be a list or an object")


def questions_from_file(payload: Any) -> dict[str, Question]:
    if not isinstance(payload, Mapping) or not payload:
        raise JudgmentValidationError("questions file must be a non-empty JSON object")
    return {str(qid): question_from_spec(str(qid), spec) for qid, spec in payload.items()}
