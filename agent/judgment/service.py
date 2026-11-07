"""Generic host-side judgment API (ADR-17 D6).

External consumers hand in a state plus questions (inline, a bundled name, or a file) and
get structured answers back. The service is the single implementation behind the Python
wrapper, the sandbox ``__judge__`` host op, and the CLI; it never becomes a model tool and
never appends transcript rows.

Batch runs are owned by the host: bounded fan-out, one retry per item, results in input
order, and per-item failures reported instead of raised.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agent.judgment.questions import load_questions
from agent.judgment.types import (
    JudgmentAborted,
    JudgmentBudgetExceeded,
    JudgmentError,
    JudgmentImage,
    JudgmentRequest,
    JudgmentResult,
    JudgmentState,
    JudgmentTimeout,
    JudgmentValidationError,
    Question,
    answer_to_json,
)
from agent.usage_pricing import CanonicalUsage

logger = logging.getLogger(__name__)

#: Bounded batch fan-out (ADR-17 D6); the request schema caps this at four as well.
MAX_BATCH_CONCURRENCY = 4
DEFAULT_BATCH_CONCURRENCY = 4
_IMAGE_KEYS = frozenset({"data", "path", "mime_type", "detail"})
_REQUEST_KEYS = frozenset({
    "state", "questions", "questions_file", "contexts", "labels", "images",
    "purpose", "budget_usd", "timeout_ms",
})
_BATCH_KEYS = _REQUEST_KEYS | {"items", "min_ok", "max_concurrency"}


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str], what: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise JudgmentValidationError(
            f"{what} has unknown keys: {', '.join(sorted(unknown))}")


@dataclass(frozen=True)
class BatchItem:
    """One state in a batch; questions are shared by the whole run."""

    key: str | int
    state: JudgmentState
    images: tuple[JudgmentImage, ...] = ()


@dataclass(frozen=True)
class BatchItemError:
    type: str
    message: str


@dataclass(frozen=True)
class BatchItemResult:
    key: str | int
    ok: bool
    result: JudgmentResult | None = None
    error: BatchItemError | None = None


@dataclass(frozen=True)
class BatchResult:
    results: tuple[BatchItemResult, ...]
    min_ok_met: bool

    @property
    def ok_count(self) -> int:
        return sum(1 for item in self.results if item.ok)


@dataclass(frozen=True)
class JudgeCall:
    """A request plus the caller-level options that are not part of ``JudgmentRequest``."""

    request: JudgmentRequest
    purpose: str = ""
    budget_usd: float | None = None
    timeout_ms: int | None = None


# ── payload parsing ────────────────────────────────────────────────────────


def _number(value: Any, what: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JudgmentValidationError(f"{what} must be a number")
    if value < minimum:
        raise JudgmentValidationError(f"{what} must be at least {minimum}")
    return float(value)


def _optional_number(payload: Mapping[str, Any], key: str) -> float | None:
    if key not in payload or payload[key] is None:
        return None
    return _number(payload[key], key)


def _optional_timeout(payload: Mapping[str, Any]) -> int | None:
    raw = payload.get("timeout_ms")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise JudgmentValidationError("timeout_ms must be a positive integer")
    return int(raw)


def _optional_purpose(payload: Mapping[str, Any]) -> str:
    raw = payload.get("purpose")
    if raw is None:
        return ""
    if not isinstance(raw, str) or not raw.strip():
        raise JudgmentValidationError("purpose must be a non-empty string")
    return raw.strip()


def image_from_payload(raw: Any) -> JudgmentImage:
    """Build an image from the v1 ``ImageAsset`` shape (base64 or local path)."""
    if isinstance(raw, JudgmentImage):
        return raw
    if not isinstance(raw, Mapping):
        raise JudgmentValidationError("judgment image must be an object")
    unknown = set(raw) - _IMAGE_KEYS
    if unknown:
        raise JudgmentValidationError(
            f"judgment image has unknown keys: {', '.join(sorted(map(str, unknown)))}")
    mime = raw.get("mime_type")
    if not isinstance(mime, str) or not mime.strip():
        raise JudgmentValidationError("judgment image requires mime_type")
    data = raw.get("data") or ""
    path = raw.get("path") or ""
    if not isinstance(data, str) or not isinstance(path, str):
        raise JudgmentValidationError("judgment image data/path must be strings")
    if bool(data.strip()) == bool(path.strip()):
        raise JudgmentValidationError("judgment image takes exactly one of data or path")
    detail = raw.get("detail")
    if detail is not None and not isinstance(detail, str):
        raise JudgmentValidationError("judgment image detail must be a string")
    return JudgmentImage(
        data=data.strip(), mime_type=mime.strip(), path=path.strip(), detail=detail)


def images_from_payload(raw: Any) -> tuple[JudgmentImage, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise JudgmentValidationError("judgment images must be a list")
    return tuple(image_from_payload(item) for item in raw)


def _questions_from_payload(payload: Mapping[str, Any]) -> tuple[Any, Mapping | None, Mapping | None]:
    """``(source, contexts, labels)``; exactly one of questions/questions_file is required."""
    inline = payload.get("questions")
    file = payload.get("questions_file")
    if (inline is None) == (file is None):
        raise JudgmentValidationError(
            "judgment request takes exactly one of questions or questions_file")
    if inline is not None and not isinstance(inline, Mapping):
        raise JudgmentValidationError("questions must be an object keyed by question id")
    if file is not None and (not isinstance(file, str) or not file.strip()):
        raise JudgmentValidationError("questions_file must be a non-empty string")
    contexts = payload.get("contexts")
    labels = payload.get("labels")
    if contexts is not None and not isinstance(contexts, Mapping):
        raise JudgmentValidationError("contexts must be an object keyed by question id")
    if labels is not None and not isinstance(labels, Mapping):
        raise JudgmentValidationError("labels must be an object keyed by question id")
    return inline if inline is not None else file, contexts, labels


def _questions(payload: Mapping[str, Any]) -> dict[str, Question]:
    source, contexts, labels = _questions_from_payload(payload)
    return dict(load_questions(source, contexts=contexts, labels=labels))


def call_from_payload(payload: Mapping[str, Any]) -> JudgeCall:
    """Parse one v1 ``JudgeRequest`` object into a typed call."""
    if not isinstance(payload, Mapping):
        raise JudgmentValidationError("judgment request must be an object")
    _reject_unknown(payload, _REQUEST_KEYS, "judgment request")
    if "state" not in payload:
        raise JudgmentValidationError("judgment request requires state")
    request = JudgmentRequest(
        state=payload["state"],
        questions=_questions(payload),
        images=images_from_payload(payload.get("images")),
    )
    return JudgeCall(
        request=request,
        purpose=_optional_purpose(payload),
        budget_usd=_optional_number(payload, "budget_usd"),
        timeout_ms=_optional_timeout(payload),
    )


def _batch_items(payload: Mapping[str, Any]) -> list[BatchItem]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise JudgmentValidationError("batch request requires a non-empty items list")
    items: list[BatchItem] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            raise JudgmentValidationError(f"batch items[{index}] must be an object")
        if "key" not in raw or "state" not in raw:
            raise JudgmentValidationError(f"batch items[{index}] requires key and state")
        key = raw["key"]
        if isinstance(key, bool) or not isinstance(key, (str, int)):
            raise JudgmentValidationError(f"batch items[{index}].key must be a string or integer")
        if isinstance(key, str) and not key:
            raise JudgmentValidationError(f"batch items[{index}].key must be non-empty")
        items.append(BatchItem(
            key=key, state=raw["state"], images=images_from_payload(raw.get("images"))))
    return items


def _batch_int(payload: Mapping[str, Any] | None, key: str, default: int) -> int:
    if payload is None or payload.get(key) is None:
        return default
    raw = payload[key]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise JudgmentValidationError(f"{key} must be an integer")
    return int(raw)


# ── execution ──────────────────────────────────────────────────────────────


class _Deadline:
    """A cancel event with a caller-supplied deadline (ADR-17 D6 ``timeout_ms``)."""

    def __init__(self, cancel_event: threading.Event | None, timeout_ms: int | None) -> None:
        self._cancel = cancel_event
        self._deadline = (time.monotonic() + timeout_ms / 1000.0) if timeout_ms else None
        self.expired = threading.Event()

    def is_set(self) -> bool:
        if self._cancel is not None and self._cancel.is_set():
            return True
        if self._deadline is not None and time.monotonic() >= self._deadline:
            self.expired.set()
            return True
        return False

    def wait(self, timeout: float) -> bool:
        end = time.monotonic() + max(0.0, timeout)
        while True:
            if self.is_set():
                return True
            remaining = end - time.monotonic()
            if self._deadline is not None:
                remaining = min(remaining, self._deadline - time.monotonic())
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))


def _resolve(purpose: str, config: Mapping[str, Any] | None) -> Any:
    # Resolve through the package attribute so callers/tests can substitute the chain.
    import agent.judgment

    if config is None:
        return agent.judgment.resolve_judge(purpose=purpose)
    return agent.judgment.resolve_judge(purpose=purpose, config=config)


def _run_call(judge: Any, call: JudgeCall, *, cancel_event: threading.Event | None,
              request_id: str = "", execution_id: str = "") -> JudgmentResult:
    deadline = _Deadline(cancel_event, call.timeout_ms)
    kwargs: dict[str, Any] = {}
    if call.budget_usd is not None:
        kwargs["budget_usd"] = call.budget_usd
    if request_id:
        kwargs["request_id"] = request_id
    if execution_id:
        kwargs["execution_id"] = execution_id
    try:
        return judge.judge(
            call.request, cancel_event=deadline, purpose=call.purpose, **kwargs)
    except JudgmentAborted:
        if deadline.expired.is_set():
            raise JudgmentTimeout(
                f"judgment timed out after {call.timeout_ms} ms") from None
        raise


def judge(
    request: JudgmentRequest | Mapping[str, Any],
    *,
    purpose: str | None = None,
    budget_usd: float | None = None,
    timeout_ms: int | None = None,
    cancel_event: threading.Event | None = None,
    config: Mapping[str, Any] | None = None,
    request_id: str = "",
    execution_id: str = "",
) -> JudgmentResult:
    """Answer one judgment request; every failure is a ``JudgmentError`` subclass."""
    if not isinstance(request, JudgmentRequest):
        if not isinstance(request, Mapping):
            raise JudgmentValidationError(
                "judge() takes a JudgmentRequest or a v1 request object")
        call = call_from_payload(dict(request))
        if purpose is None:
            purpose = call.purpose
        if budget_usd is None:
            budget_usd = call.budget_usd
        if timeout_ms is None:
            timeout_ms = call.timeout_ms
        request = call.request
    if budget_usd is not None:
        budget_usd = _number(budget_usd, "budget_usd")
    call = JudgeCall(
        request=request, purpose=str(purpose or "").strip(),
        budget_usd=budget_usd, timeout_ms=timeout_ms)
    resolved = _resolve(call.purpose, config)
    return _run_call(
        resolved, call, cancel_event=cancel_event,
        request_id=request_id, execution_id=execution_id)


def judge_batch(
    items: Sequence[BatchItem] | Mapping[str, Any],
    *,
    questions: Mapping[str, Any] | str | None = None,
    questions_file: str | None = None,
    contexts: Mapping[str, Any] | None = None,
    labels: Mapping[str, Any] | None = None,
    purpose: str = "",
    budget_usd: float | None = None,
    timeout_ms: int | None = None,
    min_ok: int | None = None,
    max_concurrency: int = DEFAULT_BATCH_CONCURRENCY,
    cancel_event: threading.Event | None = None,
    config: Mapping[str, Any] | None = None,
) -> BatchResult:
    """Run one question set over many states with bounded fan-out (ADR-17 D6).

    ``budget_usd`` caps each item's call; the process-local session bucket still applies.
    ``min_ok`` defaults to every item; a failed item is reported, never raised.
    """
    batch_items: list[BatchItem]
    source: Mapping[str, Any] | str
    if isinstance(items, Mapping):
        payload = dict(items)
        _reject_unknown(payload, _BATCH_KEYS, "batch request")
        batch_items = _batch_items(payload)
        source, contexts, labels = _questions_from_payload(payload)
        purpose = _optional_purpose(payload) or purpose
        parsed_budget = _optional_number(payload, "budget_usd")
        if parsed_budget is not None:
            budget_usd = parsed_budget
        parsed_timeout = _optional_timeout(payload)
        if parsed_timeout is not None:
            timeout_ms = parsed_timeout
        min_ok = _batch_int(payload, "min_ok", len(batch_items) if min_ok is None else min_ok)
        max_concurrency = _batch_int(payload, "max_concurrency", max_concurrency)
    else:
        batch_items = list(items)
        if not batch_items:
            raise JudgmentValidationError("judge_batch() takes at least one item")
        if (questions is None) == (questions_file is None):
            raise JudgmentValidationError(
                "judge_batch() takes exactly one of questions or questions_file")
        selected = questions if questions is not None else questions_file
        assert selected is not None
        source = selected
        min_ok = len(batch_items) if min_ok is None else min_ok

    if min_ok is None:
        min_ok = len(batch_items)
    if min_ok < 0 or min_ok > len(batch_items):
        raise JudgmentValidationError(
            f"min_ok must be between 0 and {len(batch_items)} (got {min_ok})")
    if max_concurrency < 1 or max_concurrency > MAX_BATCH_CONCURRENCY:
        raise JudgmentValidationError(
            f"max_concurrency must be between 1 and {MAX_BATCH_CONCURRENCY}")
    if budget_usd is not None:
        budget_usd = _number(budget_usd, "budget_usd")
    if timeout_ms is not None and timeout_ms < 1:
        raise JudgmentValidationError("timeout_ms must be a positive integer")

    shared = dict(load_questions(source, contexts=contexts, labels=labels))
    resolved = _resolve(purpose.strip(), config)

    def run(item: BatchItem) -> BatchItemResult:
        request = JudgmentRequest(state=item.state, questions=shared, images=item.images)
        call = JudgeCall(
            request=request, purpose=purpose.strip(),
            budget_usd=budget_usd, timeout_ms=timeout_ms)
        last: BaseException | None = None
        for attempt in range(2):
            try:
                result = _run_call(resolved, call, cancel_event=cancel_event)
            except (JudgmentValidationError, JudgmentBudgetExceeded, JudgmentAborted,
                    JudgmentTimeout) as exc:
                return BatchItemResult(
                    key=item.key, ok=False, error=BatchItemError(
                        type=type(exc).__name__, message=str(exc)))
            except Exception as exc:  # health: allow BLE001 -- item errors are data, not aborts
                last = exc
                if attempt:
                    break
                continue
            return BatchItemResult(key=item.key, ok=True, result=result)
        error = last if last is not None else RuntimeError("judgment failed")
        return BatchItemResult(
            key=item.key, ok=False,
            error=BatchItemError(type=type(error).__name__, message=str(error)))

    if max_concurrency == 1 or len(batch_items) == 1:
        results = [run(item) for item in batch_items]
    else:
        with ThreadPoolExecutor(
                max_workers=min(max_concurrency, len(batch_items))) as pool:
            results = list(pool.map(run, batch_items))
    ok_count = sum(1 for item in results if item.ok)
    return BatchResult(results=tuple(results), min_ok_met=ok_count >= min_ok)


# ── JSON views (RPC/CLI) ───────────────────────────────────────────────────


def usage_to_json(usage: CanonicalUsage) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "input_tokens": int(usage.input_tokens),
        "output_tokens": int(usage.output_tokens),
        "cache_read_tokens": int(usage.cache_read_tokens),
        "cache_write_tokens": int(usage.cache_write_tokens),
        "reasoning_tokens": int(usage.reasoning_tokens),
        "request_count": int(usage.request_count),
    }
    if usage.raw_usage is not None:
        payload["raw_usage"] = usage.raw_usage
    return payload


def result_to_json(result: JudgmentResult) -> dict[str, Any]:
    return {
        "kind": result.kind,
        "api": result.api,
        "provider": result.provider,
        "model": result.model,
        "answers": {
            question_id: answer_to_json(answer)
            for question_id, answer in result.answers.items()
        },
        "usage": usage_to_json(result.usage),
        "cost_usd": result.cost_usd,
        "cached_questions": int(result.cached_questions),
    }


def batch_to_json(result: BatchResult) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for item in result.results:
        if item.ok and item.result is not None:
            results.append({"key": item.key, "ok": True, "result": result_to_json(item.result)})
            continue
        error = item.error or BatchItemError(type="JudgmentError", message="unknown failure")
        results.append({
            "key": item.key, "ok": False,
            "error": {"type": error.type, "message": error.message},
        })
    return {"results": results, "min_ok_met": bool(result.min_ok_met)}
