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

import json
import re
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

from agent.judgment.rendering import render_template

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

_QUESTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _read_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JudgmentValidationError(f"could not load judgment questions from {path}: {exc}") from None


def _read_builtin(name: str) -> Any:
    if not _QUESTION_NAME.fullmatch(name) or name in {".", ".."}:
        raise JudgmentValidationError(f"invalid bundled judgment question name: {name!r}")
    resource = files("agent").joinpath("prompts", "judge", f"{name}.json")
    try:
        return json.loads(resource.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JudgmentValidationError(
            f"could not load bundled judgment questions {name!r}: {exc}") from None


def load_bundled_document(name: str) -> Mapping[str, Any]:
    """Raw bundled question document (``agent/prompts/judge/<name>.json``)."""
    payload = _read_builtin(name)
    if not isinstance(payload, Mapping) or not payload:
        raise JudgmentValidationError(
            f"bundled judgment questions {name!r} must be a non-empty object")
    return payload


def _question_payload(source: str | Path | Mapping[str, Any]) -> Any:
    if isinstance(source, Mapping):
        return source
    if isinstance(source, Path):
        return _read_file(source)
    if not isinstance(source, str) or not source.strip():
        raise JudgmentValidationError("questions must be a bundled name, file path, or object")
    path = Path(source)
    if path.is_absolute() or "/" in source or "\\" in source:
        return _read_file(path)
    name = path.stem if path.suffix.lower() == ".json" else source
    return _read_builtin(name)


def render_question(question: Question, context: Mapping[str, Any] | None = None) -> Question:
    """Return a copied question with its instructions rendered from caller context."""
    if context is not None and not isinstance(context, Mapping):
        raise JudgmentValidationError("question template context must be an object")
    instructions = render_template(question.instructions, context, strict=True)
    return replace(question, instructions=instructions)


def _labels_mapping(question_id: str, labels: Any) -> Mapping[str, Any] | None:
    """Normalize the v1 label array (``[label, ...]``) to the loader's label→rubric map."""
    if labels is None:
        return None
    if isinstance(labels, Mapping):
        return labels
    if isinstance(labels, (list, tuple)):
        if not labels or not all(isinstance(label, str) and label for label in labels):
            raise JudgmentValidationError(
                f"question {question_id!r} labels must be non-empty strings")
        if len(set(labels)) != len(labels):
            raise JudgmentValidationError(
                f"question {question_id!r} labels must be unique")
        return {label: None for label in labels}
    raise JudgmentValidationError(
        f"question {question_id!r} labels must be an array or object")


def _labeled_question(question_id: str, question: Question, labels: Any) -> Question:
    if not isinstance(question, ChoiceQuestion) or question.criteria:
        return question
    labels = _labels_mapping(question_id, labels)
    if not labels:
        raise JudgmentValidationError(
            f"question {question_id!r} has criteria: {{}} and requires caller-supplied labels")
    criteria: dict[str, str | None] = {}
    for label, description in labels.items():
        if not isinstance(label, str) or not label:
            raise JudgmentValidationError(
                f"question {question_id!r} labels must have non-empty string keys")
        if description is not None and not isinstance(description, str):
            raise JudgmentValidationError(
                f"question {question_id!r} label {label!r} must be a string or null")
        criteria[label] = description
    return ChoiceQuestion(question.instructions, criteria)


def load_questions(
    source: str | Path | Mapping[str, Any],
    *,
    contexts: Mapping[str, Mapping[str, Any]] | None = None,
    labels: Mapping[str, Mapping[str, str | None]] | None = None,
) -> dict[str, Question]:
    """Load bundled/file/inline questions, apply caller labels, and render instructions."""
    if contexts is not None and not isinstance(contexts, Mapping):
        raise JudgmentValidationError("question contexts must be an object keyed by question id")
    if labels is not None and not isinstance(labels, Mapping):
        raise JudgmentValidationError("question labels must be an object keyed by question id")
    questions = questions_from_file(_question_payload(source))
    rendered: dict[str, Question] = {}
    for question_id, question in questions.items():
        label_values = labels.get(question_id) if labels is not None else None
        question = _labeled_question(question_id, question, label_values)
        context = contexts.get(question_id) if contexts is not None else None
        try:
            rendered[question_id] = render_question(question, context)
        except JudgmentValidationError as exc:
            raise JudgmentValidationError(
                f"question {question_id!r} has an invalid instruction context: {exc}") from None
    return rendered
