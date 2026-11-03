"""Native judgment backend: explicit routes over System One / Decisions wires.

Retry policy matches omp's ``native.ts``: 10s per attempt, at most three
attempts, retries only on 408/429/5xx and transport failures, ``Retry-After``
capped at 5s. 401/402/403 raise :class:`JudgmentAccountRejected` so the chain
can cool the candidate for five minutes instead of paying for the same refusal
on every judgment. Timeouts and cancellation propagate without trying another
candidate.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from agent.judgment.cache import JudgmentCache
from agent.judgment.config import JudgmentSettings
from agent.judgment.images import FittedImage, WHOLE_IMAGE, fit_images, unfit_box
from agent.judgment.types import (
    Answer,
    BoxAnswer,
    BoxQuestion,
    JudgmentAborted,
    JudgmentAccountRejected,
    JudgmentAPIError,
    JudgmentRequest,
    JudgmentResponseError,
    JudgmentResult,
    JudgmentTimeout,
    JudgmentTransportError,
    JudgmentValidationError,
    coerce_wire_answer,
    validate_request,
)
from agent.judgment.wires import WIRES, Wire
from agent.redact import REDACTION_UNAVAILABLE, redact_for_egress
from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 5.0
ERROR_BODY_LIMIT = 1024
#: Concurrent question shards for one ``judge()``; bounded so a config cannot fan out.
MAX_SHARDS = 4


class _AbortSignal:
    """Combine the caller's cancel event with this call's shard-failure flag."""

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))


@dataclass(frozen=True)
class NativeRoute:
    """One configured native candidate, credentials already resolved."""

    api: str
    provider: str
    model: str
    base_url: str
    api_key: str
    timeout_ms: int = 10_000
    max_questions: int = 0

    @property
    def label(self) -> str:
        return f"{self.api}:{self.provider}/{self.model}"

    @property
    def cache_key(self) -> str:
        return f"{self.api}:{self.provider}/{self.model}"


def redact_state_for_egress(state: Any) -> Any:
    """Recursively redact every string in a state; fail closed on redactor errors."""
    if isinstance(state, str):
        redacted = redact_for_egress(state)
        if redacted == REDACTION_UNAVAILABLE:
            raise JudgmentValidationError(
                "judgment state could not be redacted; refusing to send it to a native endpoint")
        return redacted
    if isinstance(state, list):
        return [redact_state_for_egress(item) for item in state]
    if isinstance(state, dict):
        return {key: redact_state_for_egress(value) for key, value in state.items()}
    return state


def _retry_after_seconds(headers: Mapping[str, str] | Any) -> float | None:
    raw = ""
    try:
        raw = str(headers.get("Retry-After") or headers.get("retry-after") or "").strip()
    except (AttributeError, TypeError, ValueError):
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _backoff_seconds(attempt: int, headers: Any) -> float:
    hinted = _retry_after_seconds(headers)
    if hinted is not None:
        return min(hinted, BACKOFF_MAX_S)
    return min(BACKOFF_BASE_S * (2 ** attempt), BACKOFF_MAX_S)


def _sleep_or_abort(seconds: float, cancel_event: threading.Event | None) -> None:
    if cancel_event is not None:
        if cancel_event.wait(seconds):
            raise JudgmentAborted("judgment aborted")
        return
    time.sleep(seconds)


def _cost_usd(route: NativeRoute, usage: CanonicalUsage) -> float | None:
    try:
        cost = estimate_usage_cost(
            route.model, usage, provider=route.provider, base_url=route.base_url)
    except Exception:
        logger.debug("judgment cost estimation failed", exc_info=True)
        return None
    amount: Decimal | None = cost.amount_usd
    return float(amount) if amount is not None else None


def _source_answers(
    questions: Mapping[str, Any], answers: dict[str, Any], fitted: tuple[FittedImage, ...],
) -> dict[str, Any]:
    """Re-measure box answers on the source image after a fit; cached answers are already source-space."""
    if not fitted:
        return answers
    remapped: dict[str, Any] | None = None
    for question_id, question in questions.items():
        answer = answers.get(question_id)
        if not isinstance(question, BoxQuestion) or not isinstance(answer, BoxAnswer):
            continue
        content = fitted[question.image].content
        if content == WHOLE_IMAGE:
            continue
        if remapped is None:
            remapped = dict(answers)
        remapped[question_id] = BoxAnswer(
            box=unfit_box(answer.box, content), confidence=answer.confidence)
    return remapped if remapped is not None else answers


class NativeJudge:
    """One native candidate; stateless apart from its route and shared cache."""

    def __init__(self, route: NativeRoute, settings: JudgmentSettings,
                 cache: JudgmentCache | None = None) -> None:
        self.route = route
        self.settings = settings
        self.cache = cache
        self.wire: Wire = WIRES[route.api]
        self.label = route.label
        self.supports_images = self.wire.images is not None and settings.images
        self.supports_boxes = self.supports_images and self.wire.boxes

    def judge(
        self,
        request: JudgmentRequest,
        *,
        cancel_event: threading.Event | None = None,
        purpose: str = "",
    ) -> JudgmentResult:
        validate_request(
            request, allow_images=self.supports_images, allow_box=self.supports_boxes)
        state = redact_state_for_egress(request.state) if self.settings.redact_state else request.state
        fitted = fit_images(request.images, self.wire.images) if request.images else ()
        images = tuple(item.image for item in fitted)
        cached = (
            self.cache.lookup(
                self.route.cache_key, state, request.questions, images,
                ignores_image_detail=self.wire.ignores_image_detail)
            if self.cache is not None else {}
        )
        pending = {
            question_id: question for question_id, question in request.questions.items()
            if question_id not in cached
        }
        if not pending:
            return JudgmentResult(
                api=self.route.api, provider=self.route.provider, model=self.route.model,
                answers=cached, cached_questions=len(cached), cost_usd=0.0)

        answers, usage, cost, model = self._judge_questions(
            state, pending, images, cancel_event)
        answers = _source_answers(pending, answers, fitted)
        if self.cache is not None:
            self.cache.record(
                self.route.cache_key, state, pending, answers,
                max_states=self.settings.cache_max_states,
                images=images, ignores_image_detail=self.wire.ignores_image_detail)
        merged = dict(cached)
        merged.update(answers)
        return JudgmentResult(
            api=self.route.api,
            provider=self.route.provider,
            model=model or self.route.model,
            answers=merged,
            usage=usage,
            cost_usd=cost,
            cached_questions=len(cached),
        )

    # ── sharding ───────────────────────────────────────────────────────────

    def _shards(self, questions: Mapping[str, Any]) -> list[dict[str, Any]]:
        limit = self.route.max_questions or self.wire.default_max_questions
        items = list(questions.items())
        if limit <= 0 or len(items) <= limit:
            return [dict(items)]
        return [dict(items[start:start + limit]) for start in range(0, len(items), limit)]

    def _judge_questions(
        self, state: Any, questions: Mapping[str, Any], images: tuple[Any, ...],
        cancel_event: threading.Event | None,
    ) -> tuple[dict[str, Any], CanonicalUsage, float | None, str]:
        """Answer every pending question, sharding when the endpoint caps questions.

        Any shard failure stops the remaining shards; shards that already answered stay billed
        (their ledger rows are written per shard) but nothing is cached for a partial result.
        """
        shards = self._shards(questions)
        if len(shards) == 1:
            return self._judge_shard(state, shards[0], images, cancel_event)

        shard_cancel = threading.Event()
        signal = _AbortSignal(cancel_event, shard_cancel) if cancel_event is not None else shard_cancel
        results: list[tuple[dict[str, Any], CanonicalUsage, float | None, str] | None] = [
            None for _ in shards]
        errors: list[BaseException] = []
        errors_lock = threading.Lock()
        # Each shard thread needs the caller's accounting ContextVar (session ledger), which a
        # bare thread does not inherit; copy per job so concurrent entry stays legal.
        parent_context = contextvars.copy_context()

        def run(index: int, context: contextvars.Context) -> None:
            if signal.is_set():
                return
            try:
                results[index] = context.run(
                    self._judge_shard, state, shards[index], images, signal)
            except Exception as exc:  # health: allow BLE001 -- captured to stop sibling shards, then re-raised on the calling thread
                with errors_lock:
                    errors.append(exc)
                shard_cancel.set()

        with ThreadPoolExecutor(max_workers=min(MAX_SHARDS, len(shards))) as pool:
            futures = [
                pool.submit(run, index, parent_context.copy())
                for index in range(len(shards))
            ]
            for future in futures:
                future.result()

        if errors:
            raise errors[0]
        merged_answers: dict[str, Any] = {}
        input_tokens = output_tokens = 0
        costs: list[float] = []
        model = ""
        for result in results:
            if result is None:  # pragma: no cover - every shard either answered or raised
                continue
            shard_answers, shard_usage, shard_cost, shard_model = result
            merged_answers.update(shard_answers)
            input_tokens += shard_usage.input_tokens
            output_tokens += shard_usage.output_tokens
            if shard_cost is not None:
                costs.append(shard_cost)
            model = shard_model or model
        total_cost = sum(costs) if len(costs) == len(shards) else None
        return (
            merged_answers,
            CanonicalUsage(input_tokens=input_tokens, output_tokens=output_tokens),
            total_cost,
            model,
        )

    def _judge_shard(
        self, state: Any, questions: Mapping[str, Any], images: tuple[Any, ...],
        cancel_event: threading.Event | None,
    ) -> tuple[dict[str, Any], CanonicalUsage, float | None, str]:
        fresh_request = JudgmentRequest(state=state, questions=dict(questions), images=images)
        try:
            body = json.dumps(self.wire.encode(fresh_request, self.route.model), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise JudgmentValidationError(
                f"judgment state/questions are not JSON-serializable: {exc}") from None
        decoded = self._post(body, cancel_event, has_images=bool(images))
        usage = CanonicalUsage(
            input_tokens=decoded.input_tokens, output_tokens=decoded.output_tokens)
        cost = decoded.cost_usd
        if cost is None:
            cost = _cost_usd(self.route, usage)
        # A 2xx response is billed even when its answers later fail validation.
        self._record_ledger(usage, cost)
        answers: dict[str, Any] = {}
        for question_id, question in questions.items():
            raw = decoded.answers.get(question_id)
            if raw is None:
                if question_id in decoded.refused:
                    raise JudgmentResponseError(
                        f"{self.label} refused to answer question {question_id!r}")
                raise JudgmentResponseError(
                    f"{self.label} response is missing an answer for question {question_id!r}")
            answers[question_id] = coerce_wire_answer(question_id, question, raw)
        return answers, usage, cost, decoded.model

    def _headers(self, *, has_images: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.route.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if has_images:
            headers.update(self.wire.image_headers)
        return headers

    def _post(self, body: str, cancel_event: threading.Event | None,
              *, has_images: bool = False) -> Any:
        url = self.wire.target(self.route.base_url, self.route.model)
        timeout_s = max(0.1, self.route.timeout_ms / 1000.0)
        last_transport: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            if cancel_event is not None and cancel_event.is_set():
                raise JudgmentAborted("judgment aborted")
            request = urllib.request.Request(
                url, data=body.encode("utf-8"), headers=self._headers(has_images=has_images),
                method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout_s) as response:
                    payload = response.read()
                try:
                    decoded = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise JudgmentResponseError(
                        f"{self.label} returned malformed JSON: {exc}") from None
                return self.wire.decode(decoded)
            except urllib.error.HTTPError as exc:
                text = self._error_body(exc)
                status = int(exc.code)
                if status in (401, 402, 403):
                    raise JudgmentAccountRejected(
                        status, text, f"{self.label} rejected the credential (HTTP {status})")
                transient = status == 408 or status == 429 or status >= 500
                if transient and attempt + 1 < MAX_ATTEMPTS:
                    _sleep_or_abort(_backoff_seconds(attempt, exc.headers), cancel_event)
                    continue
                raise JudgmentAPIError(
                    status, text, f"{self.label} API error (HTTP {status})")
            except TimeoutError as exc:
                last_transport = exc
                if attempt + 1 < MAX_ATTEMPTS:
                    _sleep_or_abort(_backoff_seconds(attempt, None), cancel_event)
                    continue
                raise JudgmentTimeout(f"{self.label} timed out after {MAX_ATTEMPTS} attempts")
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, TimeoutError):
                    last_transport = exc
                    if attempt + 1 < MAX_ATTEMPTS:
                        _sleep_or_abort(_backoff_seconds(attempt, None), cancel_event)
                        continue
                    raise JudgmentTimeout(
                        f"{self.label} timed out after {MAX_ATTEMPTS} attempts")
                last_transport = exc
                if attempt + 1 < MAX_ATTEMPTS:
                    _sleep_or_abort(_backoff_seconds(attempt, None), cancel_event)
                    continue
                raise JudgmentTransportError(f"{self.label} transport error: {reason}")
        raise JudgmentTransportError(f"{self.label} transport error: {last_transport}")

    def _error_body(self, exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read(ERROR_BODY_LIMIT)
        except OSError:
            raw = b""
        return redact_for_egress(raw.decode("utf-8", "replace"))

    def _record_ledger(self, usage: CanonicalUsage, cost: float | None) -> None:
        if not (usage.input_tokens or usage.output_tokens):
            return
        try:
            from agent.aux_accounting import current_context
            from agent.runtime_session_store import call_off_event_loop

            context = current_context()
            if context is None:
                return
            session_db, session_id = context
            call_off_event_loop(
                session_db.record_auxiliary_usage,
                session_id,
                "judge",
                model=self.route.model,
                billing_provider=self.route.provider,
                billing_base_url=self.route.base_url,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                estimated_cost_usd=cost,
            )
        except Exception:
            logger.debug("judgment native usage recording failed (non-fatal)", exc_info=True)
