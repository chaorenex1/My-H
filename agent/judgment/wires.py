"""Wire dialects for the native judgment APIs (ADR-11 D4).

``typesafe``, ``openrouter-decisions`` and ``workers-ai-systemone`` share the
System One body (``{state, model, questions}``); they differ by path, default
base URL, provider, credentials, and — for Workers AI — the response being
wrapped in ``result`` and the body carrying only the model's last path segment.

``openai-decisions`` translates the same question kinds into Decisions' ordered
``questions``/``answers`` lists (``noul``→``predicate``, ``choice``→``choices``,
``score``→``levels``); a ``refusal`` entry fails the candidate. Behavior follows
omp's ``systemone.ts`` / ``openai-decisions.ts``.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.judgment.images import (
    JEV_LIMITS,
    OPENAI_DECISIONS_LIMITS,
    WORKERS_AI_LIMITS,
    ImageLimits,
)
from agent.judgment.types import (
    BoxQuestion,
    ChoiceQuestion,
    JudgmentImage,
    JudgmentRequest,
    JudgmentResponseError,
    NoulQuestion,
    Question,
    ScoreQuestion,
    image_data_url,
    image_bytes,
    normalized_image_mime,
)

FAMILY_SYSTEM_ONE = "system-one"
FAMILY_OPENAI_DECISIONS = "openai-decisions"


@dataclass(frozen=True)
class DecodedJudgment:
    answers: Mapping[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    refused: tuple[str, ...] = ()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _usage_fields(payload: Any) -> tuple[int, int, float | None]:
    usage = payload if isinstance(payload, Mapping) else {}
    input_tokens = _finite_number(usage.get("input_tokens")) or 0.0
    output_tokens = _finite_number(usage.get("output_tokens")) or 0.0
    return int(input_tokens), int(output_tokens), _finite_number(usage.get("cost"))


def encode_question(question: Any, *, image_index: bool = False) -> dict[str, Any]:
    if isinstance(question, ChoiceQuestion):
        return {
            "type": "choice",
            "instructions": question.instructions,
            "criteria": dict(question.criteria),
        }
    if isinstance(question, NoulQuestion):
        body: dict[str, Any] = {"type": "noul", "instructions": question.instructions}
        if question.criteria:
            body["criteria"] = dict(question.criteria)
        return body
    if isinstance(question, ScoreQuestion):
        return {
            "type": "score",
            "instructions": question.instructions,
            "criteria": list(question.criteria),
        }
    if isinstance(question, BoxQuestion):
        body = {"type": "box", "instructions": question.instructions}
        if image_index:
            # Only Workers AI forwards questions verbatim; `box` is rejected before encode there.
            body["image"] = question.image
        return body
    raise JudgmentResponseError("unsupported judgment question type")


def _wire_questions(questions: Mapping[str, Question], *, keep_image_index: bool = False
                    ) -> dict[str, Any]:
    return {
        question_id: encode_question(question, image_index=keep_image_index)
        for question_id, question in questions.items()
    }


def _canonical_base64(image: JudgmentImage) -> str:
    return base64.b64encode(image_bytes(image)).decode("ascii")


def _image_format(image: JudgmentImage) -> str:
    return "png" if normalized_image_mime(image.mime_type) == "image/png" else "jpeg"


def _state_with_images(state: Any, items: list[Any]) -> Any:
    """``state`` with ``items`` appended (a string root becomes ``[state, *items]``)."""
    if isinstance(state, list):
        return [*state, *items]
    if not isinstance(state, dict):
        return [state, *items]
    key = "__omp_judgment_images"
    suffix = 1
    while key in state:
        key = f"__omp_judgment_images_{suffix}"
        suffix += 1
    return {**state, key: items}


def encode_system_one(request: JudgmentRequest, model: str) -> dict[str, Any]:
    return {
        "state": request.state,
        "model": model,
        "questions": _wire_questions(request.questions),
    }


def encode_typesafe(request: JudgmentRequest, model: str) -> dict[str, Any]:
    """TypeSafe System One: images become top-level ``assets`` referenced from the state."""
    body: dict[str, Any] = {
        "state": request.state,
        "model": model,
        "questions": _wire_questions(request.questions),
    }
    if not request.images:
        return body
    assets: dict[str, Any] = {}
    references: list[str] = []
    for image in request.images:
        reference = uuid.uuid4().hex
        while reference in assets:
            reference = uuid.uuid4().hex
        assets[reference] = {
            "type": "image",
            "format": _image_format(image),
            "data_base64": _canonical_base64(image),
        }
        references.append(reference)
    body["state"] = _state_with_images(request.state, references)
    body["assets"] = assets
    return body


def encode_openrouter(request: JudgmentRequest, model: str) -> dict[str, Any]:
    """OpenRouter Decisions: image parts join the state; ``original`` degrades to ``auto``."""
    body: dict[str, Any] = {
        "state": request.state,
        "model": model,
        "questions": _wire_questions(request.questions),
    }
    if not request.images:
        return body
    parts = []
    for image in request.images:
        detail = image.detail
        if detail == "original":
            detail = "auto"
        entry: dict[str, Any] = {
            "type": "image_url",
            "image_url": {"url": image_data_url(image)},
        }
        if detail is not None:
            entry["image_url"]["detail"] = detail
        parts.append(entry)
    body["state"] = _state_with_images(request.state, parts)
    return body


def encode_workers(request: JudgmentRequest, model: str) -> dict[str, Any]:
    """Workers AI: a top-level data-URL list; the body model is the last path segment."""
    body: dict[str, Any] = {
        "state": request.state,
        "model": model,
        "questions": _wire_questions(request.questions, keep_image_index=True),
    }
    if request.images:
        body["images"] = [image_data_url(image) for image in request.images]
    return body



def decode_system_one(payload: Any) -> DecodedJudgment:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("answers"), Mapping):
        raise JudgmentResponseError("malformed System One response: missing answers object")
    input_tokens, output_tokens, cost = _usage_fields(payload.get("usage"))
    return DecodedJudgment(
        answers=payload["answers"],
        model=str(payload.get("model") or ""),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
    )


def decode_workers_result(payload: Any) -> DecodedJudgment:
    """Workers AI answers live under ``result``; ``success: false`` fails the candidate."""
    if not isinstance(payload, Mapping):
        raise JudgmentResponseError("malformed Workers AI response: not an object")
    if payload.get("success") is False:
        errors = payload.get("errors")
        detail = json.dumps(errors, ensure_ascii=False) if errors else "no detail"
        raise JudgmentResponseError(f"Workers AI judgment failed: {detail[:512]}")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise JudgmentResponseError("malformed Workers AI response: missing result object")
    return decode_system_one(result)


def _fold_noul_criteria(question: NoulQuestion) -> str:
    """Decisions predicates carry the rubric in the instruction text (omp predicate template)."""
    parts = [question.instructions]
    criteria = question.criteria or {}
    yes = criteria.get("true")
    no = criteria.get("false")
    if yes:
        parts.extend(("", f"True means: {yes}"))
    if no:
        parts.extend(("", f"False means: {no}"))
    return "\n".join(parts)


def encode_openai_question(name: str, question: Any) -> dict[str, Any]:
    if isinstance(question, NoulQuestion):
        return {
            "type": "predicate",
            "name": name,
            "instructions": _fold_noul_criteria(question),
        }
    if isinstance(question, ChoiceQuestion):
        choices: list[dict[str, Any]] = []
        for value, description in question.criteria.items():
            entry: dict[str, Any] = {"value": value}
            if description is not None:
                entry["description"] = description
            choices.append(entry)
        return {
            "type": "choice",
            "name": name,
            "instructions": question.instructions,
            "choices": choices,
        }
    if isinstance(question, ScoreQuestion):
        return {
            "type": "score",
            "name": name,
            "instructions": question.instructions,
            "levels": [{"label": label} for label in question.criteria],
        }
    if isinstance(question, BoxQuestion):
        raise JudgmentResponseError("OpenAI Decisions has no box question type")


def encode_openai_decisions(request: JudgmentRequest, model: str) -> dict[str, Any]:
    state = request.state if isinstance(request.state, str) else json.dumps(
        request.state, ensure_ascii=False)
    user_input: Any = state
    if request.images:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": state}]
        for image in request.images:
            entry: dict[str, Any] = {
                "type": "input_image",
                "image_url": image_data_url(image),
            }
            if image.detail is not None:
                entry["detail"] = image.detail
            content.append(entry)
        user_input = [{"role": "user", "content": content}]
    return {
        "model": model,
        "input": user_input,
        "questions": [
            encode_openai_question(name, question)
            for name, question in request.questions.items()
        ],
    }


def _wire_value_key(value: Any) -> str:
    """JS ``String(value)``: booleans lowercase, integral floats without a trailing ``.0``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _probability_map(value: Any) -> dict[str, float] | None:
    if not isinstance(value, list):
        return None
    found: dict[str, float] = {}
    for entry in value:
        if not isinstance(entry, Mapping):
            return None
        probability = _finite_number(entry.get("probability"))
        raw_value = entry.get("value")
        if probability is None or not isinstance(raw_value, (str, int, float, bool)):
            return None
        found[_wire_value_key(raw_value)] = probability
    return found


def _decode_openai_answer(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = entry.get("type")
    if kind == "predicate":
        probability = _finite_number(entry.get("probability"))
        if probability is None:
            return None
        return {"type": "noul", "noul": probability}
    if kind == "choice":
        probabilities = _probability_map(entry.get("probabilities"))
        confidence = _finite_number(entry.get("confidence"))
        choice = entry.get("choice")
        if probabilities is None or confidence is None or not isinstance(choice, (str, int, float, bool)):
            return None
        return {
            "type": "choice",
            "choice": _wire_value_key(choice),
            "probabilities": probabilities,
            "confidence": confidence,
        }
    if kind == "score":
        probabilities = _probability_map(entry.get("probabilities"))
        confidence = _finite_number(entry.get("confidence"))
        score = _finite_number(entry.get("score"))
        if probabilities is None or confidence is None or score is None:
            return None
        return {
            "type": "score",
            "score": int(score),
            "probabilities": probabilities,
            "confidence": confidence,
        }
    return None


def decode_openai_decisions(payload: Any) -> DecodedJudgment:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("answers"), list):
        raise JudgmentResponseError("malformed OpenAI Decisions response: missing answers list")
    answers: dict[str, Any] = {}
    refused: list[str] = []
    for entry in payload["answers"]:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        if entry.get("type") == "refusal":
            refused.append(name)
            continue
        answer = _decode_openai_answer(entry)
        if answer is not None:
            answers[name] = answer
    input_tokens, output_tokens, cost = _usage_fields(payload.get("usage"))
    return DecodedJudgment(
        answers=answers,
        model=str(payload.get("model") or ""),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        refused=tuple(refused),
    )


@dataclass(frozen=True)
class Wire:
    """One api dialect: where to POST and how to translate the body both ways."""

    api: str
    path: str
    default_base_url: str
    family: str = FAMILY_SYSTEM_ONE
    default_max_questions: int = 0
    unwrap_result: bool = False
    strip_model_prefix: bool = False
    #: Image transport bounds; ``None`` means the wire takes no images.
    images: ImageLimits | None = None
    #: Whether the wire forwards ``box`` questions.
    boxes: bool = False
    #: TypeSafe stores no detail, so the cache identity must not either.
    ignores_image_detail: bool = False
    #: Headers sent only on requests that carry images.
    image_headers: tuple[tuple[str, str], ...] = ()
    encoder: Callable[[JudgmentRequest, str], dict[str, Any]] = encode_system_one

    def target(self, base_url: str, model: str) -> str:
        return f"{base_url.rstrip('/')}{self.path.replace('{model}', model)}"

    def body_model(self, model: str) -> str:
        return model.rsplit("/", 1)[-1] if self.strip_model_prefix else model

    def encode(self, request: JudgmentRequest, model: str) -> dict[str, Any]:
        return self.encoder(request, self.body_model(model))

    def decode(self, payload: Any) -> DecodedJudgment:
        if self.family == FAMILY_OPENAI_DECISIONS:
            return decode_openai_decisions(payload)
        if self.unwrap_result:
            return decode_workers_result(payload)
        return decode_system_one(payload)


WIRES: dict[str, Wire] = {
    "typesafe": Wire(
        "typesafe", "/v1/systemone", "https://api.typesafe.ai",
        images=JEV_LIMITS, boxes=True, ignores_image_detail=True,
        image_headers=(("TypeSafe-Danger-Zone-Images", "true"),),
        encoder=encode_typesafe),
    "openrouter-decisions": Wire(
        "openrouter-decisions", "/decisions", "https://openrouter.ai/api/alpha",
        images=JEV_LIMITS, boxes=True, encoder=encode_openrouter),
    "openai-decisions": Wire(
        "openai-decisions", "/decisions", "https://api.openai.com/v1",
        family=FAMILY_OPENAI_DECISIONS, images=OPENAI_DECISIONS_LIMITS,
        encoder=encode_openai_decisions),
    # Clef accepts at most 64 questions per call; the full model id stays in the path.
    "workers-ai-systemone": Wire(
        "workers-ai-systemone", "/{model}",
        "https://api.cloudflare.com/client/v4/accounts/<account>/ai/run",
        default_max_questions=64, unwrap_result=True, strip_model_prefix=True,
        images=WORKERS_AI_LIMITS, encoder=encode_workers),
}
