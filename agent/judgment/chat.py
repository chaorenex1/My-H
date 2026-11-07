"""Text judgment through ``call_llm(task="judge")``.

``call_llm`` owns provider resolution and the single ``record_aux_usage`` billing
point; this backend only renders, calls, and parses, with ``fallback=False`` so
the fixed candidate is never swapped underneath the chain. Parse failures get
``judgment.text.parse_retries`` additional completions (each billed) that force a
``submit_judgment`` tool call, then surface as :class:`JudgmentParseError`.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Literal, Mapping, Sequence

from agent.auxiliary_client import (
    aux_interrupt_protection,
    call_llm,
)
from agent.judgment.config import (
    JudgmentSettings,
    auxiliary_task_config,
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
    JudgmentParseError,
    JudgmentRequest,
    JudgmentResult,
    JudgmentUnavailable,
    JudgmentValidationError,
    image_data_url,
    validate_request,
)
from agent.usage_pricing import CanonicalUsage, normalize_usage

logger = logging.getLogger(__name__)

#: Retry-only tool (omp ``SUBMIT_JUDGMENT``): forces the answer into a typed argument
#: when the free-form keyword reply is unparseable.
SUBMIT_JUDGMENT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_judgment",
        "description": (
            "Submit the exact requested answer label, or the requested "
            "question-id lines for a batched judgment."
        ),
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}
SUBMIT_JUDGMENT_CHOICE: dict[str, Any] = {
    "type": "function", "function": {"name": "submit_judgment"}}


def _float_or(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _message_parts(message: Any) -> list[str]:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return [content] if content else []
    if not isinstance(content, (list, tuple)):
        return []
    parts: list[str] = []
    for block in content:
        if isinstance(block, Mapping):
            if str(block.get("type") or "text") == "text":
                parts.append(block.get("text") if isinstance(block.get("text"), str) else "")
        elif isinstance(block, str):
            parts.append(block)
    return parts


def _submit_judgment_answer(response: Any) -> str:
    """The ``answer`` argument of a forced ``submit_judgment`` tool call, or ``""``."""
    choices = getattr(response, "choices", None)
    message = getattr(choices[0], "message", None) if choices else None
    for call in getattr(message, "tool_calls", None) or ():
        function = getattr(call, "function", None)
        if str(getattr(function, "name", "") or "") != "submit_judgment":
            continue
        arguments = getattr(function, "arguments", None)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                continue
        if isinstance(arguments, Mapping):
            answer = arguments.get("answer")
            if isinstance(answer, str):
                return answer
    return ""


def response_text(response: Any, *, retry: bool = False) -> str:
    """Completed reply text: content blocks joined by spaces, plus the retry tool answer.

    Reasoning fields are never read (omp semantics); an uncalibrated preamble simply
    fails parsing and buys a tool-forced retry instead of being guessed at.
    """
    choices = getattr(response, "choices", None)
    message = getattr(choices[0], "message", None) if choices else None
    parts = [part for part in _message_parts(message) if part]
    if retry:
        answer = _submit_judgment_answer(response)
        if answer:
            parts.append(answer)
    return " ".join(parts).strip()


def stop_reason(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    return str(getattr(choices[0], "finish_reason", "") or "").strip().lower()


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
    result_kind: Literal["native", "local", "online"] = "online"

    def __init__(self, settings: JudgmentSettings,
                 config: Mapping[str, Any] | None = None, *,
                 task: str = "judge",
                 provider: str | None = None,
                 model: str | None = None,
                 base_url: str | None = None) -> None:
        self.settings = settings
        self.task = task
        self.aux = auxiliary_task_config(config, task)
        self.provider = str(
            provider if provider is not None else self.aux.get("provider") or "auto")
        self.model = str(
            model if model is not None else self.aux.get("model") or "")
        self.base_url = str(
            base_url if base_url is not None else self.aux.get("base_url") or "")
        self.api_key = str(self.aux.get("api_key") or "")
        self.timeout = _float_or(self.aux.get("timeout"), 30.0)
        self.max_tokens = settings.text.max_tokens
        if self.max_tokens < 1024:
            logger.warning(
                "judgment.text.max_tokens=%d is below 1024; Anthropic-dialect proxies reject "
                "max_tokens <= thinking budget and small models may truncate the answer",
                self.max_tokens)
        self.extra_body = self.aux.get("extra_body") if isinstance(
            self.aux.get("extra_body"), Mapping) else None
        # Native-only by default: text vision requires auxiliary.<task>.images and a model
        # that is not known to reject image input.
        self.supports_images = bool(
            self.aux.get("images")
            and _model_accepts_images(self.provider, self.model, config))
        self.label = f"text:{task}:{self.provider}/{self.model or 'auto'}"

    def _parse_retries(self) -> int:
        return max(0, int(self.settings.text.parse_retries))

    def judge(
        self,
        request: JudgmentRequest,
        *,
        cancel_event: threading.Event | None = None,
        purpose: str = "",
        budget_usd: float | None = None,
    ) -> JudgmentResult:
        validate_request(
            request, allow_images=self.supports_images, allow_box=False,
            image_max_bytes=self.settings.assets.max_bytes,
            allow_local_images=self.settings.assets.allow_local)
        system, user = render_judgment_prompt(request, guard_state=self.guard_state)
        multi = len(request.questions) > 1
        retries = self._parse_retries()
        last_error: Exception | None = None
        total_usage: CanonicalUsage | None = None
        api = "chat_completions"
        for attempt in range(retries + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise JudgmentAborted("judgment aborted")
            retry = attempt > 0
            prompt_system = render_retry_prompt(system, multi=multi) if retry else system
            response, api = self._complete(
                prompt_system, user, cancel_event, retry=retry, images=request.images)
            # Usage for every completed attempt (call_llm already billed each one); the
            # aggregate is reported to the caller and never recorded a second time.
            try:
                usage = normalize_usage(
                    getattr(response, "usage", None), provider=self.provider)
            except Exception:
                logger.debug("judgment usage normalization failed", exc_info=True)
                usage = CanonicalUsage()
            total_usage = usage if total_usage is None else total_usage + usage
            reason = stop_reason(response)
            if reason == "aborted" or (cancel_event is not None and cancel_event.is_set()):
                raise JudgmentAborted("judgment aborted")
            if reason == "error":
                failed = JudgmentUnavailable(
                    "judgment text completion stopped with an error")
                failed.usage = total_usage
                raise failed
            text = response_text(response, retry=retry)
            try:
                answers = parse_text_answers(text, request)
            except JudgmentParseError as exc:
                last_error = exc
                if attempt < retries:
                    logger.debug("judgment text parse failed (attempt %d): %s", attempt + 1, exc)
                    continue
                exc.usage = total_usage
                raise
            return JudgmentResult(
                kind=self.result_kind,
                api=api,
                provider=self.provider,
                model=str(getattr(response, "model", "") or self.model),
                answers=answers,
                usage=total_usage or CanonicalUsage(),
            )
        raise last_error or JudgmentValidationError("judgment text backend produced no answer")

    def _complete(self, system: str, user: str, cancel_event: threading.Event | None,
                  *, retry: bool = False,
                  images: Sequence[JudgmentImage] = ()) -> tuple[Any, str]:
        user_content: Any = user
        if images:
            user_content = [{"type": "text", "text": user}, *_image_blocks(images)]
        kwargs: dict[str, Any] = {
            "task": self.task,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "timeout": self.timeout,
            "max_tokens": self.max_tokens,
            "fallback": False,
        }
        if retry:
            kwargs["tools"] = [SUBMIT_JUDGMENT_TOOL]
            kwargs["tool_choice"] = SUBMIT_JUDGMENT_CHOICE
        if self.settings.text.disable_reasoning:
            kwargs["reasoning_config"] = {"enabled": False}
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
        route_info: dict[str, str] = {}
        kwargs["route_info"] = route_info
        try:
            with aux_interrupt_protection(cancel_event=cancel_event):
                response = call_llm(**kwargs)
        except JudgmentError:
            raise
        except Exception as exc:
            logger.debug("judgment text call failed", exc_info=True)
            raise JudgmentUnavailable(f"judgment text call failed: {exc}") from None
        return response, route_info.get("api_mode") or "chat_completions"
