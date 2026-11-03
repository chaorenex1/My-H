"""Text judgment through ``call_llm(task="judge")``.

``call_llm`` owns provider resolution, fallback, retries, and the single
``record_aux_usage`` billing point; this backend only renders, calls, and parses.
Parse failures get ``parse_retries`` additional completions (each billed), then
surface as :class:`JudgmentParseError`.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Mapping, Sequence

from agent.auxiliary_client import (
    aux_interrupt_protection,
    call_llm,
    extract_content_or_reasoning,
)
from agent.judgment.config import (
    JudgmentSettings,
    auxiliary_judge_config,
)
from agent.judgment.text import (
    parse_text_answers,
    render_judgment_prompt,
    render_retry_prompt,
)
from agent.judgment.types import (
    JudgmentAborted,
    JudgmentError,
    JudgmentImage,
    JudgmentRequest,
    JudgmentResult,
    JudgmentUnavailable,
    JudgmentValidationError,
    image_data_url,
    validate_request,
)
from agent.usage_pricing import CanonicalUsage, normalize_usage

logger = logging.getLogger(__name__)


def _float_or(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _int_or(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def response_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message is not None else None
        if isinstance(content, str) and content.strip():
            # The shared extractor strips inline think blocks and falls back to
            # reasoning fields, which is how a thinking preamble still yields a keyword.
            text = extract_content_or_reasoning(response)
            if text:
                return text
        if isinstance(content, list):
            parts = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, Mapping) and str(block.get("type") or "text") == "text"
            ]
            joined = "\n".join(part for part in parts if part)
            if joined.strip():
                return joined
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    return extract_content_or_reasoning(response)


def _model_accepts_images(provider: str, model: str, config: Mapping[str, Any] | None) -> bool:
    """Whether the configured text model can see images; unknown capability trusts the opt-in.

    Only config overrides and the already-cached models.dev catalog are consulted — a
    judgment (or ``doctor``) must not make a capability network request. The explicit
    ``auxiliary.judge.images: true`` is the caller's assertion for unknown models.
    """
    if not model:
        return True
    provider = "" if provider in ("", "auto") else provider
    cfg = dict(config) if isinstance(config, Mapping) else {}
    try:
        from agent.image_routing import _supports_vision_override

        override = _supports_vision_override(cfg, provider, model)
        if override is not None:
            return override
        from agent.models_dev import get_model_capabilities

        caps = get_model_capabilities(provider, model, allow_network=False)
    except Exception:  # health: allow BLE001 -- capability lookup is advisory here
        return True
    return True if caps is None else bool(caps.supports_vision)


def _image_blocks(images: Sequence[JudgmentImage]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for image in images:
        image_url: dict[str, Any] = {"url": image_data_url(image)}
        if image.detail is not None:
            image_url["detail"] = image.detail
        blocks.append({"type": "image_url", "image_url": image_url})
    return blocks


class ChatTextBackend:
    """Keyword judgment over any chat model; one-hot answers."""

    guard_state = True

    def __init__(self, settings: JudgmentSettings,
                 config: Mapping[str, Any] | None = None) -> None:
        self.settings = settings
        self.aux = auxiliary_judge_config(config)
        self.provider = str(self.aux.get("provider") or "auto")
        self.model = str(self.aux.get("model") or "")
        self.base_url = str(self.aux.get("base_url") or "")
        self.api_key = str(self.aux.get("api_key") or "")
        self.timeout = _float_or(self.aux.get("timeout"), 30.0)
        self.max_tokens = _int_or(self.aux.get("max_tokens"), 4096)
        self.extra_body = self.aux.get("extra_body") if isinstance(
            self.aux.get("extra_body"), Mapping) else None
        # Native-only by default: text vision requires auxiliary.judge.images and a model
        # that is not known to reject image input.
        self.supports_images = bool(
            self.aux.get("images")
            and _model_accepts_images(self.provider, self.model, config))
        self.label = f"text:{self.provider}/{self.model or 'auto'}"

    def judge(
        self,
        request: JudgmentRequest,
        *,
        cancel_event: threading.Event | None = None,
        purpose: str = "",
    ) -> JudgmentResult:
        validate_request(
            request, allow_images=self.supports_images, allow_box=False)
        system, user = render_judgment_prompt(request, guard_state=self.guard_state)
        multi = len(request.questions) > 1
        retries = max(0, int(self.settings.parse_retries))
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise JudgmentAborted("judgment aborted")
            prompt_system = system if attempt == 0 else render_retry_prompt(system, multi=multi)
            response = self._complete(
                prompt_system, user, cancel_event, attempt, len(request.questions),
                request.images)
            text = response_text(response)
            try:
                answers = parse_text_answers(text, request)
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    logger.debug("judgment text parse failed (attempt %d): %s", attempt + 1, exc)
                    continue
                raise
            usage = CanonicalUsage()
            try:
                usage = normalize_usage(
                    getattr(response, "usage", None), provider=self.provider)
            except Exception:
                logger.debug("judgment usage normalization failed", exc_info=True)
            return JudgmentResult(
                api="text",
                provider=self.provider,
                model=str(getattr(response, "model", "") or self.model),
                answers=answers,
                usage=usage,
            )
        raise last_error or JudgmentValidationError("judgment text backend produced no answer")

    def _complete(self, system: str, user: str, cancel_event: threading.Event | None,
                  attempt: int = 0, questions: int = 1,
                  images: Sequence[JudgmentImage] = ()) -> Any:
        user_content: Any = user
        if images:
            user_content = [{"type": "text", "text": user}, *_image_blocks(images)]
        kwargs: dict[str, Any] = {
            "task": "judge",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "timeout": self.timeout,
        }
        if self.provider and self.provider != "auto":
            kwargs["provider"] = self.provider
        if self.model:
            kwargs["model"] = self.model
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.extra_body:
            kwargs["extra_body"] = dict(self.extra_body)
        kwargs["max_tokens"] = self._max_tokens_for(attempt, questions)
        try:
            with aux_interrupt_protection(cancel_event=cancel_event):
                return call_llm(**kwargs)
        except JudgmentError:
            raise
        except Exception as exc:
            logger.debug("judgment text call failed", exc_info=True)
            raise JudgmentUnavailable(f"judgment text call failed: {exc}") from None

    def _max_tokens_for(self, attempt: int, questions: int) -> int:
        return self.max_tokens
