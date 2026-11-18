"""Typed judgment protocol: questions, answers, results, and validation.

A judgment is a side-channel model call that answers a fixed set of named questions
about a JSON state. It never appends transcript rows, changes the system prompt, or
starts a turn; callers own scheduling and timeouts. Shapes mirror omp's
``packages/ai/src/judgment/types.ts`` so the text/native bridges stay portable.
"""

from __future__ import annotations

import base64
import binascii
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, Sequence, TypeAlias, runtime_checkable

from agent.usage_pricing import CanonicalUsage

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JudgmentState: TypeAlias = str | dict[str, JsonValue] | list[JsonValue]


class JudgmentError(Exception):
    """Base class for every judgment failure.

    ``usage`` carries the billed usage of attempts that already reached a provider when a
    backend fails (a parse retry budget is still billed); the chain records it against the
    cost gate. Native failures account per shard in the session ledger instead.
    """

    usage: CanonicalUsage | None = None


class JudgmentValidationError(JudgmentError):
    """The caller's request is malformed; no candidate is consumed."""


class JudgmentParseError(JudgmentError):
    """A text candidate answered, but its output is not a parseable answer."""

    def __init__(self, question_id: str, output: str, detail: str) -> None:
        super().__init__(f"judgment {question_id!r}: {detail}")
        self.question_id = question_id
        self.output = output
        self.detail = detail


class JudgmentUnavailable(JudgmentError):
    """No candidate could answer: no credential, no server, or every candidate failed."""


class JudgmentBudgetExceeded(JudgmentError):
    """The configured per-call or per-session judgment cap refuses the next attempt."""


class JudgmentAborted(JudgmentError):
    """The caller cancelled the judgment."""


class JudgmentTimeout(JudgmentError):
    """A native attempt exceeded its per-attempt timeout; never demoted to another candidate."""


class JudgmentAPIError(JudgmentError):
    """A native endpoint answered with a non-2xx status."""

    def __init__(self, status: int, body: str, message: str = "") -> None:
        self.status = int(status)
        self.body = body or ""
        super().__init__(message or f"judgment API error (HTTP {self.status})")


class JudgmentAccountRejected(JudgmentAPIError):
    """401/402/403: the account cannot serve this candidate until something changes."""


class JudgmentResponseError(JudgmentError):
    """A native response envelope or one of its answers is malformed."""


class JudgmentTransportError(JudgmentError):
    """A native request failed in transport; the candidate is unavailable, not the caller's fault."""


#: Largest local asset a request may reference (ADR-17 D8 ``images.assets.max_bytes``).
MAX_IMAGE_ASSET_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class JudgmentImage:
    """An image sent alongside the state for image-capable candidates (ADR-17 P4).

    Exactly one of ``data`` (base64, no data: prefix) or ``path`` (a local file under the
    working directory or ``$HERMES_HOME``) identifies the bytes. Remote URLs are never
    fetched: they would turn a judgment request into an SSRF primitive.
    """

    data: str = ""
    mime_type: str = ""
    path: str = ""
    detail: str | None = None


#: RFC 6838 restricted name after ``image/``; ``#`` never survives a data URL.
_IMAGE_MIME = re.compile(r"^image/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$", re.IGNORECASE)
_IMAGE_DETAILS = frozenset({"auto", "low", "high", "original"})
_URL_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def _local_asset_roots() -> tuple[Path, ...]:
    from hermes_constants import get_hermes_home

    roots: list[Path] = []
    for root in (Path.cwd(), get_hermes_home()):
        try:
            roots.append(Path(root).resolve())
        except OSError:
            continue
    return tuple(roots)


def _read_local_asset(raw_path: str, max_bytes: int) -> bytes:
    """Read a path-backed asset, confined to cwd / ``$HERMES_HOME`` and the byte cap."""
    text = str(raw_path or "").strip()
    if not text:
        raise JudgmentValidationError("judgment image path is empty")
    if _URL_LIKE.match(text):
        raise JudgmentValidationError(
            "judgment images never fetch remote URLs; download the file and pass its path")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise JudgmentValidationError(f"cannot read judgment image {text!r}: {exc}") from None
    if not resolved.is_file():
        raise JudgmentValidationError(f"judgment image path is not a file: {text!r}")
    roots = _local_asset_roots()
    if roots and not any(resolved == root or root in resolved.parents for root in roots):
        raise JudgmentValidationError(
            f"judgment image {text!r} is outside the working directory and $HERMES_HOME")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise JudgmentValidationError(f"cannot stat judgment image {text!r}: {exc}") from None
    if max_bytes and size > max_bytes:
        raise JudgmentValidationError(
            f"judgment image {text!r} is {size} bytes; the limit is {max_bytes}")
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise JudgmentValidationError(f"cannot read judgment image {text!r}: {exc}") from None


def normalized_image_mime(mime_type: Any) -> str:
    """Lowercase image MIME, or ``JudgmentValidationError`` when it cannot be sent as a data URL."""
    mime = str(mime_type or "").strip().lower()
    if not _IMAGE_MIME.match(mime) or "#" in mime:
        raise JudgmentValidationError(f"invalid image MIME type: {mime_type!r}")
    return mime


def image_bytes(image: JudgmentImage, *, max_bytes: int = MAX_IMAGE_ASSET_BYTES) -> bytes:
    """Resolve ``image`` to bytes: inline base64, or a local path under cwd / ``$HERMES_HOME``."""
    if not isinstance(image, JudgmentImage):
        raise JudgmentValidationError(
            f"judgment image must be a JudgmentImage (got {type(image).__name__})")
    data = "".join(str(image.data or "").split())
    path = str(image.path or "").strip()
    if data and path:
        raise JudgmentValidationError("judgment image takes either data or path, not both")
    if path:
        return _read_local_asset(path, max_bytes)
    if not data or len(data) % 4 == 1:
        raise JudgmentValidationError("invalid base64 image data")
    try:
        raw = base64.b64decode(data + "=" * (-len(data) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise JudgmentValidationError("invalid base64 image data") from None
    if not raw:
        raise JudgmentValidationError("invalid base64 image data")
    if max_bytes and len(raw) > max_bytes:
        raise JudgmentValidationError(
            f"judgment image is {len(raw)} bytes; the limit is {max_bytes}")
    return raw


def image_data_url(image: JudgmentImage) -> str:
    """``data:<mime>;base64,...`` with a canonical (padded) payload."""
    mime = normalized_image_mime(image.mime_type)
    return f"data:{mime};base64,{base64.b64encode(image_bytes(image)).decode('ascii')}"


def validate_image(image: JudgmentImage, index: int = 0, *,
                   max_bytes: int = MAX_IMAGE_ASSET_BYTES,
                   allow_local: bool = True) -> None:
    """Validate one request image before any candidate sees it."""
    if not isinstance(image, JudgmentImage):
        raise JudgmentValidationError(
            f"judgment image {index} must be a JudgmentImage (got {type(image).__name__})")
    if str(image.path or "").strip() and not allow_local:
        raise JudgmentValidationError(
            "judgment path assets are disabled (judgment.images.assets.allow_local=false)")
    normalized_image_mime(image.mime_type)
    image_bytes(image, max_bytes=max_bytes)
    if image.detail is not None and image.detail not in _IMAGE_DETAILS:
        raise JudgmentValidationError(
            f"judgment image {index} has invalid detail {image.detail!r}; "
            f"use one of {sorted(_IMAGE_DETAILS)}")


@dataclass(frozen=True)
class ChoiceQuestion:
    instructions: str
    criteria: Mapping[str, str | None]

    def __post_init__(self) -> None:
        object.__setattr__(self, "criteria", MappingProxyType(dict(self.criteria)))


@dataclass(frozen=True)
class NoulQuestion:
    instructions: str
    criteria: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.criteria is not None:
            object.__setattr__(self, "criteria", MappingProxyType(dict(self.criteria)))


@dataclass(frozen=True)
class ScoreQuestion:
    instructions: str
    criteria: tuple[str, ...]


@dataclass(frozen=True)
class BoxQuestion:
    instructions: str
    image: int

    def __post_init__(self) -> None:
        if isinstance(self.image, bool) or not isinstance(self.image, int) or self.image < 0:
            raise JudgmentValidationError(
                "box question image must be a non-negative index into request.images")


Question: TypeAlias = ChoiceQuestion | NoulQuestion | ScoreQuestion | BoxQuestion


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float


@dataclass(frozen=True)
class NoulAnswer:
    noul: float


@dataclass(frozen=True)
class ScoreAnswer:
    score: int
    probabilities: Mapping[str, float]
    confidence: float


@dataclass(frozen=True)
class BoxEdges:
    top: float
    bottom: float
    left: float
    right: float


@dataclass(frozen=True)
class BoxAnswer:
    box: BoxEdges
    confidence: float


Answer: TypeAlias = ChoiceAnswer | NoulAnswer | ScoreAnswer | BoxAnswer


@dataclass(frozen=True)
class JudgmentRequest:
    state: JudgmentState
    questions: Mapping[str, Question]
    images: tuple[JudgmentImage, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "questions", MappingProxyType(dict(self.questions)))


@dataclass(frozen=True)
class JudgmentResult:
    kind: Literal["native", "local", "online"]
    api: str
    provider: str
    model: str
    answers: Mapping[str, Answer]
    usage: CanonicalUsage = field(default_factory=CanonicalUsage)
    # Billed amount as reported by the wire (native), or ``None`` when only token counts exist.
    cost_usd: float | None = None
    # Hermes extension: how many answers came from the native cache (CLI/run audit).
    cached_questions: int = 0


@runtime_checkable
class Judge(Protocol):
    label: str
    supports_images: bool

    def judge(
        self,
        request: JudgmentRequest,
        *,
        cancel_event: threading.Event | None = None,
        purpose: str = "",
        budget_usd: float | None = None,
    ) -> JudgmentResult: ...


_QUESTION_TYPES = {
    "choice": ChoiceQuestion,
    "noul": NoulQuestion,
    "score": ScoreQuestion,
    "box": BoxQuestion,
}


def question_type(question: Question) -> str:
    for name, cls in _QUESTION_TYPES.items():
        if isinstance(question, cls):
            return name
    raise JudgmentValidationError(f"unknown question type: {type(question).__name__}")


def _assert_json_value(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise JudgmentValidationError("judgment state is nested too deeply")
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise JudgmentValidationError("judgment state object keys must be strings")
            _assert_json_value(item, depth + 1)
        return
    raise JudgmentValidationError(
        f"judgment state contains a non-JSON value: {type(value).__name__}")


def validate_request(
    request: JudgmentRequest, *, allow_images: bool = False, allow_box: bool = False,
    image_max_bytes: int = MAX_IMAGE_ASSET_BYTES, allow_local_images: bool = True,
) -> None:
    """Reject malformed requests before any candidate is resolved or billed."""
    if not isinstance(request.state, (str, dict, list)):
        raise JudgmentValidationError("judgment state must be a string, object, or array")
    _assert_json_value(request.state)
    if not request.questions:
        raise JudgmentValidationError("judgment request has no questions")
    images = tuple(request.images)
    if images:
        if not allow_images:
            raise JudgmentValidationError(
                "image judgment is not enabled (judgment.images=false or the candidate is text-only)")
        for index, image in enumerate(images):
            validate_image(
                image, index, max_bytes=image_max_bytes, allow_local=allow_local_images)
    for question_id, question in request.questions.items():
        if not isinstance(question_id, str) or not question_id.strip():
            raise JudgmentValidationError("judgment question ids must be non-empty strings")
        if not isinstance(question.instructions, str) or not question.instructions.strip():
            raise JudgmentValidationError(
                f"judgment question {question_id!r} has empty instructions")
        if isinstance(question, BoxQuestion):
            if not allow_images:
                raise JudgmentValidationError(
                    "image judgment is not enabled (judgment.images=false)")
            if not allow_box:
                raise JudgmentValidationError(
                    f"judgment question {question_id!r}: box questions need a native judge "
                    "that forwards box answers")
            if question.image >= len(images):
                raise JudgmentValidationError(
                    f"judgment question {question_id!r} names image {question.image}, "
                    f"but the request has {len(images)} images")
        if isinstance(question, ChoiceQuestion) and not dict(question.criteria):
            raise JudgmentValidationError(
                f"judgment question {question_id!r}: choice criteria must not be empty")
        if isinstance(question, ScoreQuestion) and len(question.criteria) < 2:
            raise JudgmentValidationError(
                f"judgment question {question_id!r}: score criteria needs at least two levels")


def answer_to_json(answer: Answer) -> dict[str, Any]:
    """Wire/cache JSON for one answer."""
    if isinstance(answer, ChoiceAnswer):
        return {
            "type": "choice", "choice": answer.choice,
            "probabilities": dict(answer.probabilities), "confidence": answer.confidence,
        }
    if isinstance(answer, NoulAnswer):
        return {"type": "noul", "noul": answer.noul}
    if isinstance(answer, ScoreAnswer):
        return {
            "type": "score", "score": answer.score,
            "probabilities": dict(answer.probabilities), "confidence": answer.confidence,
        }
    if isinstance(answer, BoxAnswer):
        box = answer.box
        return {
            "type": "box",
            "box": {"top": box.top, "bottom": box.bottom, "left": box.left, "right": box.right},
            "confidence": answer.confidence,
        }
    raise JudgmentValidationError(f"unknown answer type: {type(answer).__name__}")


def answer_from_json(raw: Any) -> Answer:
    """Rebuild an answer from ``answer_to_json`` output (cache lookup)."""
    if not isinstance(raw, Mapping):
        raise JudgmentValidationError("cached answer is not an object")
    kind = str(raw.get("type") or "")
    if kind == "choice":
        return ChoiceAnswer(
            str(raw["choice"]), dict(raw.get("probabilities") or {}), float(raw.get("confidence", 0.0)))
    if kind == "noul":
        return NoulAnswer(float(raw["noul"]))
    if kind == "score":
        return ScoreAnswer(
            int(raw["score"]), dict(raw.get("probabilities") or {}), float(raw.get("confidence", 0.0)))
    if kind == "box":
        box = raw.get("box") or {}
        return BoxAnswer(
            BoxEdges(
                float(box["top"]), float(box["bottom"]), float(box["left"]), float(box["right"])),
            float(raw.get("confidence", 0.0)))
    raise JudgmentValidationError(f"unknown cached answer type: {kind or '<missing>'}")


def _fraction(value: Any, what: str) -> float:
    """A 0–1 coordinate/confidence; box edges and probabilities share this range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise JudgmentResponseError(f"{what} is not a number") from None
    if not 0.0 <= number <= 1.0:
        raise JudgmentResponseError(f"{what} is outside [0, 1]")
    return number


def _probability(value: Any, what: str) -> float:
    return _fraction(value, what)


def _probabilities(raw: Any, labels: Sequence[str], what: str) -> Mapping[str, float]:
    if raw is None:
        return MappingProxyType({label: 0.0 for label in labels})
    if not isinstance(raw, Mapping):
        raise JudgmentResponseError(f"{what} probabilities are not an object")
    out: dict[str, float] = {}
    for label, value in raw.items():
        if label not in labels:
            continue
        out[str(label)] = _probability(value, f"{what} probability {label!r}")
    return MappingProxyType(out)


def coerce_wire_answer(question_id: str, question: Question, raw: Any) -> Answer:
    """Validate one native answer against its question type; any mismatch fails the candidate."""
    if not isinstance(raw, Mapping):
        raise JudgmentResponseError(f"answer for {question_id!r} is not an object")
    kind = question_type(question)
    if raw.get("type") != kind:
        raise JudgmentResponseError(
            f"answer for {question_id!r} is {raw.get('type')!r}, expected {kind!r}")
    if isinstance(question, ChoiceQuestion):
        labels = tuple(question.criteria)
        choice = raw.get("choice")
        if choice not in labels:
            raise JudgmentResponseError(
                f"answer for {question_id!r} chose {choice!r}, not one of {labels}")
        if raw.get("probabilities") is None:
            probabilities = MappingProxyType({
                label: (1.0 if label == choice else 0.0) for label in labels})
        else:
            probabilities = _probabilities(raw.get("probabilities"), labels, question_id)
        return ChoiceAnswer(
            str(choice),
            probabilities,
            _probability(raw.get("confidence", 1.0), f"answer {question_id!r} confidence"),
        )
    if isinstance(question, NoulQuestion):
        return NoulAnswer(_probability(raw.get("noul"), f"answer {question_id!r} noul"))
    if isinstance(question, ScoreQuestion):
        try:
            score = int(raw.get("score"))
        except (TypeError, ValueError):
            raise JudgmentResponseError(f"answer for {question_id!r} has no integer score") from None
        levels = len(question.criteria)
        if not 0 <= score < levels:
            raise JudgmentResponseError(
                f"answer for {question_id!r} score {score} is outside 0..{levels - 1}")
        labels = tuple(str(i) for i in range(levels))
        if raw.get("probabilities") is None:
            probabilities = MappingProxyType({
                label: (1.0 if label == str(score) else 0.0) for label in labels})
        else:
            probabilities = _probabilities(raw.get("probabilities"), labels, question_id)
        return ScoreAnswer(
            score,
            probabilities,
            _probability(raw.get("confidence", 1.0), f"answer {question_id!r} confidence"),
        )
    if isinstance(question, BoxQuestion):
        raw_box = raw.get("box")
        if not isinstance(raw_box, Mapping):
            raise JudgmentResponseError(
                f"answer for {question_id!r} is missing a box object")
        edges = {
            edge: _fraction(raw_box.get(edge), f"answer {question_id!r} box {edge}")
            for edge in ("top", "bottom", "left", "right")
        }
        if not edges["left"] < edges["right"] or not edges["top"] < edges["bottom"]:
            raise JudgmentResponseError(
                f"answer for {question_id!r} has an empty box: {edges}")
        return BoxAnswer(
            BoxEdges(
                top=edges["top"], bottom=edges["bottom"],
                left=edges["left"], right=edges["right"]),
            _fraction(raw.get("confidence", 1.0), f"answer {question_id!r} confidence"),
        )
    raise JudgmentResponseError(f"unknown answer type for {question_id!r}")

