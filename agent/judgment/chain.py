"""Judge candidate chain: native first, explicit text fallback, rejection cooldown.

Resolution follows ADR-11 D5: configured native routes (credentialed only, in
order), then text only when there is no native at all — or when
``text_after_native`` is explicitly on. A text model never stands in for a
failed native judgment by default because it cannot produce calibrated
probabilities. Every ``judge()`` also writes one best-effort ``runs`` audit row.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from agent.judgment import eval_files
from hermes_constants import hermes_home_key
from hermes_cli.config import get_env_value_prefer_dotenv

from agent.judgment.cache import (
    JudgmentCache,
    canonical_json,
    sha256_text,
    shared_cache,
    state_identity,
)
from agent.judgment.chat import ChatTextBackend
from agent.judgment.config import (
    DEFAULT_ROUTE_BASE_URLS,
    JudgmentSettings,
    auxiliary_judge_config,
    is_local_provider,
    load_judgment_settings,
)
from agent.judgment.images import ImageLimits, images_acceptable
from agent.judgment.local import LocalChatBackend
from agent.judgment.native import NativeJudge, NativeRoute, redact_state_for_egress
from agent.judgment.wires import WIRES
from agent.judgment.types import (
    BoxQuestion,
    JudgmentAborted,
    JudgmentAccountRejected,
    JudgmentError,
    JudgmentRequest,
    JudgmentResult,
    JudgmentTimeout,
    JudgmentUnavailable,
    JudgmentValidationError,
    Judge,
    answer_to_json,
    validate_request,
)
from agent.redact import redact_for_egress
from agent.usage_pricing import estimate_usage_cost

logger = logging.getLogger(__name__)

CANDIDATE_REJECTION_COOLDOWN_S = 300.0
#: ``(profile home key, candidate identity) -> monotonic expiry``.
_REJECTIONS: dict[tuple[str, str], float] = {}
_REJECTIONS_LOCK = threading.Lock()


def _cooldown_identity(route: Mapping[str, Any]) -> str:
    return (f"{route['api']}:{route['provider']}/{route['model']}"
            f"@{route.get('base_url') or ''}")


def _cooled_down(route: Mapping[str, Any]) -> bool:
    key = (hermes_home_key(), _cooldown_identity(route))
    with _REJECTIONS_LOCK:
        expiry = _REJECTIONS.get(key)
        if expiry is None:
            return False
        if expiry <= time.monotonic():
            _REJECTIONS.pop(key, None)
            return False
        return True


def _mark_rejected(route: Mapping[str, Any]) -> None:
    key = (hermes_home_key(), _cooldown_identity(route))
    with _REJECTIONS_LOCK:
        _REJECTIONS[key] = time.monotonic() + CANDIDATE_REJECTION_COOLDOWN_S


def reset_rejection_cooldowns() -> None:
    """Test hook: forget every process-local account cooldown."""
    with _REJECTIONS_LOCK:
        _REJECTIONS.clear()


def _typesafe_credentials(route: Mapping[str, Any]) -> tuple[str, str, str] | None:
    env_name = str(route.get("api_key_env") or "TYPESAFE_API_KEY")
    key = get_env_value_prefer_dotenv(env_name)
    if not key:
        return None
    api_key = str(key)
    base_url = (
        str(route.get("base_url") or "")
        or str(get_env_value_prefer_dotenv("TYPESAFE_BASE_URL") or "")
        or DEFAULT_ROUTE_BASE_URLS["typesafe"]
    )
    return api_key, base_url.rstrip("/"), str(route.get("provider") or "typesafe")


def _pool_api_key(provider: str) -> str:
    try:
        from agent.credential_pool import load_pool

        pool = load_pool(provider)
        if pool.has_credentials():
            entry = pool.select()
            if entry is not None:
                return str(getattr(entry, "runtime_api_key", "") or entry.access_token or "")
    except Exception:
        logger.debug("%s credential pool unavailable", provider, exc_info=True)
    return ""


def _openrouter_api_key(route: Mapping[str, Any]) -> str:
    env_name = str(route.get("api_key_env") or "OPENROUTER_API_KEY")
    key = get_env_value_prefer_dotenv(env_name)
    if key:
        return str(key)
    if env_name == "OPENROUTER_API_KEY":
        legacy = get_env_value_prefer_dotenv("OPENAI_API_KEY")
        if legacy and str(legacy).startswith("sk-or-"):
            return str(legacy)
    return _pool_api_key("openrouter")


def _openrouter_credentials(route: Mapping[str, Any]) -> tuple[str, str, str] | None:
    key = _openrouter_api_key(route)
    if not key:
        return None
    base_url = (
        str(route.get("base_url") or "")
        or DEFAULT_ROUTE_BASE_URLS["openrouter-decisions"]
    )
    return key, base_url.rstrip("/"), str(route.get("provider") or "openrouter")


def _openai_credentials(route: Mapping[str, Any]) -> tuple[str, str, str] | None:
    env_name = str(route.get("api_key_env") or "OPENAI_API_KEY")
    key = str(get_env_value_prefer_dotenv(env_name) or "")
    if not key and env_name == "OPENAI_API_KEY":
        key = _pool_api_key("openai-api")
    if not key:
        return None
    base_url = (
        str(route.get("base_url") or "")
        or str(get_env_value_prefer_dotenv("OPENAI_BASE_URL") or "")
        or DEFAULT_ROUTE_BASE_URLS["openai-decisions"]
    )
    return key, base_url.rstrip("/"), str(route.get("provider") or "openai-api")


def _workers_ai_credentials(route: Mapping[str, Any]) -> tuple[str, str, str] | None:
    env_name = str(route.get("api_key_env") or "CLOUDFLARE_API_TOKEN")
    key = str(get_env_value_prefer_dotenv(env_name) or "")
    account = str(get_env_value_prefer_dotenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    if not key or not account:
        return None
    base_url = (
        str(route.get("base_url") or "")
        or DEFAULT_ROUTE_BASE_URLS["workers-ai-systemone"]
    ).replace("<account>", account)
    return (
        key,
        base_url.rstrip("/"),
        str(route.get("provider") or "cloudflare-workers-ai"),
    )


#: One credential resolver per wire; a judgment transport is never a chat provider.
_CREDENTIAL_RESOLVERS = {
    "typesafe": _typesafe_credentials,
    "openrouter-decisions": _openrouter_credentials,
    "openai-decisions": _openai_credentials,
    "workers-ai-systemone": _workers_ai_credentials,
}

#: ``doctor`` names the missing key(s) instead of the route's provider id.
_ROUTE_CREDENTIAL_HINTS = {
    "typesafe": "TYPESAFE_API_KEY",
    "openrouter-decisions": "OPENROUTER_API_KEY",
    "openai-decisions": "OPENAI_API_KEY",
    "workers-ai-systemone": "CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID",
}


def _credential_hint(route: Mapping[str, Any]) -> str:
    return str(route.get("api_key_env") or _ROUTE_CREDENTIAL_HINTS.get(
        str(route.get("api") or ""), "credential"))


def _native_route(settings: JudgmentSettings, route: Mapping[str, Any]
                  ) -> NativeRoute | None:
    api = str(route["api"])
    resolver = _CREDENTIAL_RESOLVERS.get(api)
    if resolver is None:
        return None
    credentials = resolver(route)
    if credentials is None:
        return None
    api_key, base_url, provider = credentials
    return NativeRoute(
        api=api,
        provider=provider,
        model=str(route["model"]),
        base_url=base_url,
        api_key=api_key,
        timeout_ms=int(route.get("timeout_ms") or settings.timeout_ms),
        max_questions=int(route.get("max_questions") or 0),
    )


@dataclass(frozen=True)
class _Candidate:
    kind: str
    judge: Judge
    cooled: bool = False
    supports_images: bool = False
    supports_boxes: bool = False
    image_limits: ImageLimits | None = None


class ChainJudge:
    """A lightweight chain resolved on each ``judge()`` call."""

    label = "judge role chain"

    def __init__(self, settings: JudgmentSettings, config: Mapping[str, Any],
                 purpose: str = "", cache: JudgmentCache | None = None) -> None:
        self.settings = settings
        self.config = config
        self.purpose = purpose
        self._cache = cache if (settings.cache or settings.audit) else None

    @property
    def supports_images(self) -> bool:
        """Whether any configured candidate can take image input (no network)."""
        try:
            return any(candidate.supports_images for candidate in self.candidates())
        except JudgmentError:
            return False

    # ── resolution ─────────────────────────────────────────────────────────

    def _resolve_native(self) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for raw in self.settings.native:
            route = _native_route(self.settings, raw)
            if route is None:
                logger.debug("judgment candidate %s has no credential", _cooldown_identity(raw))
                continue
            identity = {
                "api": route.api, "provider": route.provider,
                "model": route.model, "base_url": route.base_url,
            }
            cooled = _cooled_down(identity)
            if cooled:
                logger.debug("judgment candidate %s is cooling down", _cooldown_identity(identity))
            judge = NativeJudge(
                route, self.settings,
                self._cache if self.settings.cache else None)
            candidates.append(_Candidate(
                "native", judge, cooled=cooled,
                supports_images=bool(getattr(judge, "supports_images", False)),
                supports_boxes=bool(getattr(judge, "supports_boxes", False)),
                image_limits=getattr(getattr(judge, "wire", None), "images", None)))
        return candidates

    def _text_candidate(self) -> _Candidate:
        aux = auxiliary_judge_config(self.config)
        provider = str(aux.get("provider") or "auto")
        if is_local_provider(provider):
            judge: Judge = LocalChatBackend(self.settings, self.config)
        else:
            judge = ChatTextBackend(self.settings, self.config)
        return _Candidate(
            "text", judge,
            supports_images=bool(getattr(judge, "supports_images", False)),
            supports_boxes=False)

    def candidates(self) -> list[_Candidate]:
        mode = self.settings.mode
        if mode == "off":
            return []
        if mode == "text":
            return [self._text_candidate()]
        native = self._resolve_native()
        if mode == "native":
            return native
        if native:
            return native + ([self._text_candidate()] if self.settings.text_after_native else [])
        return [self._text_candidate()]

    def _image_compatible(self, request: JudgmentRequest) -> list[_Candidate] | None:
        """Candidates that can take this request's images/boxes; ``None`` when none can."""
        candidates = self.candidates()
        needs_images = bool(request.images)
        needs_boxes = any(
            isinstance(question, BoxQuestion) for question in request.questions.values())
        if not needs_images and not needs_boxes:
            return candidates
        if not candidates:
            return candidates
        compatible = [
            candidate for candidate in candidates
            if candidate.supports_images
            and (not needs_boxes or candidate.supports_boxes)
            and (candidate.image_limits is None
                 or images_acceptable(request.images, candidate.image_limits))
        ]
        return compatible or None

    # ── judging ────────────────────────────────────────────────────────────

    def judge(
        self,
        request: JudgmentRequest,
        *,
        cancel_event: threading.Event | None = None,
        purpose: str = "",
        request_id: str = "",
        execution_id: str = "",
    ) -> JudgmentResult:
        validate_request(
            request, allow_images=self.settings.images, allow_box=self.settings.images)
        started = time.monotonic()
        purpose = purpose or self.purpose
        candidates = self._image_compatible(request)
        if candidates is None:
            error = "no compatible judge candidate supports images/box"
            self._audit(
                request=request, started=started, status="error", purpose=purpose,
                request_id=request_id, execution_id=execution_id, error=error)
            raise JudgmentUnavailable(error)
        if not candidates:
            self._audit(
                request=request, started=started, status="error", purpose=purpose,
                request_id=request_id, execution_id=execution_id,
                error="judgment is off or no candidate is configured",
            )
            raise JudgmentUnavailable("judgment is off or no candidate is configured")

        last_failure = ""
        last_api = last_provider = last_model = ""
        for candidate in candidates:
            if cancel_event is not None and cancel_event.is_set():
                self._audit(
                    request=request, started=started, status="aborted", purpose=purpose,
                    request_id=request_id, execution_id=execution_id,
                    api=last_api, provider=last_provider, model=last_model,
                    error="judgment aborted",
                )
                raise JudgmentAborted("judgment aborted")
            judge = candidate.judge
            route = getattr(judge, "route", None)
            last_api = route.api if route is not None else ("text" if candidate.kind == "text" else "")
            last_provider = route.provider if route is not None else getattr(judge, "provider", "")
            last_model = route.model if route is not None else getattr(judge, "model", "")
            if candidate.cooled:
                last_failure = f"{getattr(judge, 'label', candidate.kind)} rejected the account recently"
                continue
            try:
                result = judge.judge(
                    request, cancel_event=cancel_event, purpose=purpose)
            except JudgmentAborted:
                self._audit(
                    request=request, started=started, status="aborted", purpose=purpose,
                    request_id=request_id, execution_id=execution_id,
                    api=last_api, provider=last_provider, model=last_model,
                    error="judgment aborted",
                )
                raise
            except JudgmentValidationError:
                raise
            except JudgmentTimeout as exc:
                self._audit(
                    request=request, started=started, status="error", purpose=purpose,
                    request_id=request_id, execution_id=execution_id,
                    api=last_api, provider=last_provider, model=last_model,
                    error=str(exc),
                )
                raise
            except JudgmentAccountRejected as exc:
                route = getattr(judge, "route", None)
                if route is not None:
                    _mark_rejected({
                        "api": route.api, "provider": route.provider,
                        "model": route.model, "base_url": route.base_url,
                    })
                last_failure = str(exc)
                logger.warning("judgment candidate %s rejected; cooling down 5m",
                               getattr(judge, "label", candidate.kind))
            except (JudgmentError, OSError) as exc:
                last_failure = str(exc)
                logger.warning("judgment candidate %s failed: %s",
                               getattr(judge, "label", candidate.kind), exc)
            else:
                self._audit(
                    request=request, started=started, status="ok", purpose=purpose,
                    request_id=request_id, execution_id=execution_id,
                    result=result,
                )
                return result

        error = f"every judge candidate failed: {last_failure or 'no credential'}"
        self._audit(
            request=request, started=started, status="error", purpose=purpose,
            request_id=request_id, execution_id=execution_id,
            api=last_api, provider=last_provider, model=last_model, error=error,
        )
        raise JudgmentUnavailable(error)

    # ── audit ──────────────────────────────────────────────────────────────

    def _audit(
        self,
        *,
        request: JudgmentRequest,
        started: float,
        status: str,
        purpose: str,
        request_id: str,
        execution_id: str,
        result: JudgmentResult | None = None,
        api: str = "",
        provider: str = "",
        model: str = "",
        error: str = "",
    ) -> None:
        if not self.settings.audit and purpose != "eval":
            return
        try:
            session_id = ""
            try:
                from agent.aux_accounting import current_context

                context = current_context()
                if context is not None:
                    session_id = str(context[1] or "")
            except Exception:
                logger.debug("judgment accounting context unavailable", exc_info=True)
            state_sha, _canonical = state_identity(request.state, request.images)
            safe_error = redact_for_egress(error)[:1024] if error else ""
            answers: Mapping[str, Any] = result.answers if result is not None else {}
            usage = result.usage if result is not None else None
            cost = 0.0
            if result is not None and result.cost_usd is not None:
                cost = float(result.cost_usd)
            elif usage is not None and (usage.input_tokens or usage.output_tokens):
                try:
                    estimate = estimate_usage_cost(
                        result.model if result else model,
                        usage,
                        provider=result.provider if result else provider,
                        base_url=getattr(getattr(result, "route", None), "base_url", "") or "",
                    )
                    cost = float(estimate.amount_usd) if estimate.amount_usd is not None else 0.0
                except Exception:
                    logger.debug("judgment audit cost estimation failed", exc_info=True)
                    cost = 0.0
            run = {
                "id": uuid.uuid4().hex,
                "created_at": int(time.time()),
                "session_id": session_id,
                "execution_id": execution_id,
                "request_id": request_id,
                "purpose": purpose,
                "mode": self.settings.mode,
                "api": api or (result.api if result else ""),
                "provider": provider or (result.provider if result else ""),
                "model": model or (result.model if result else ""),
                "questions": len(request.questions),
                "cached": result.cached_questions if result else 0,
                "input_tokens": usage.input_tokens if usage else 0,
                "output_tokens": usage.output_tokens if usage else 0,
                "cost_usd": cost,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "status": status,
                "error": safe_error,
                "state_sha": state_sha,
                "answers_sha": sha256_text(canonical_json({
                    question_id: answer_to_json(answer)
                    for question_id, answer in answers.items()
                })) if answers else "",
            }
            if self.settings.audit and self._cache is not None:
                self._cache.record_run(run, max_runs=self.settings.audit_max_runs)
            if purpose == "eval":
                debug_answers = None
                debug_state = None
                if self.settings.eval_debug:
                    debug_answers = {
                        question_id: answer_to_json(answer)
                        for question_id, answer in answers.items()
                    }
                    debug_state = redact_state_for_egress(request.state)
                eval_files.record_run(
                    run, max_bytes=self.settings.eval_max_bytes,
                    debug_state=debug_state, debug_answers=debug_answers)
        except Exception:
            logger.debug("judgment run audit failed (non-fatal)", exc_info=True)


def resolve_judge(*, purpose: str = "", config: Mapping[str, Any] | None = None) -> ChainJudge:
    """Resolve the configured candidate chain; candidates resolve on the first judge()."""
    from hermes_cli.config import load_config

    cfg = dict(config) if config is not None else load_config()
    settings = load_judgment_settings(cfg)
    cache = shared_cache() if (settings.cache or settings.audit) else None
    return ChainJudge(settings, cfg, purpose=purpose, cache=cache)


def has_native_judge(config: Mapping[str, Any] | None = None) -> bool:
    """Whether the first resolvable candidate is native; never makes a network request."""
    from hermes_cli.config import load_config

    cfg = dict(config) if config is not None else load_config()
    settings = load_judgment_settings(cfg)
    if settings.mode in ("off", "text"):
        return False
    chain = ChainJudge(settings, cfg)
    candidates = chain._resolve_native()
    return bool(candidates) and candidates[0].kind == "native"


def doctor_routes(config: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Per-route availability for ``hermes judgment doctor`` (no network, no secrets)."""
    from hermes_cli.config import load_config

    cfg = dict(config) if config is not None else load_config()
    settings = load_judgment_settings(cfg)
    rows: list[dict[str, Any]] = []
    for raw in settings.native:
        wire = WIRES.get(raw["api"])
        row = {
            "api": raw["api"],
            "provider": raw["provider"],
            "model": raw["model"],
            "base_url": raw.get("base_url") or "",
            "available": False,
            "reason": "",
            "supports_images": bool(wire and wire.images),
            "supports_boxes": bool(wire and wire.boxes),
        }
        route = _native_route(settings, raw)
        if route is None:
            row["reason"] = f"no credential ({_credential_hint(raw)})"
        elif _cooled_down({
                "api": route.api, "provider": route.provider, "model": route.model,
                "base_url": route.base_url,
        }):
            row["reason"] = "rejected by the account recently (5m cooldown)"
        else:
            row["available"] = True
            row["base_url"] = route.base_url
        rows.append(row)
    return rows
