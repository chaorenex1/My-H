"""Local tiny-model text judgment over Hermes' managed llama.cpp runtime.

No downloader, no embedded engine: the endpoint is whatever
``agent.auxiliary_local_runtime.bare_llamacpp_endpoint`` resolves for this
profile. A stopped server means ``JudgmentUnavailable`` — never a silent cloud
fallback. Small models get one merged user turn (system + state), no state
guard, no parse retries (ADR-17 D4), and a fixed output budget chosen by whether
the model reasons (16 tokens, or 1024 for a thinking model).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Literal, Mapping, cast

from agent.judgment.chat import ChatTextBackend
from agent.judgment.config import JudgmentSettings, LOCAL_ALIASES
from agent.judgment.types import JudgmentAborted, JudgmentError, JudgmentUnavailable

logger = logging.getLogger(__name__)


class LocalChatBackend(ChatTextBackend):
    """Chat backend for ``auxiliary.judge.provider: llamacpp|llama.cpp|local``."""

    guard_state = False
    result_kind: Literal["native", "local", "online"] = "local"

    def __init__(self, settings: JudgmentSettings,
                 config: Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(settings, config, **kwargs)
        self.supports_images = False  # D13: local text judgment is text-only
        self._endpoint_base = ""
        self._endpoint_key = ""

    @property
    def alias(self) -> str:
        alias = self.provider.strip().lower()
        return "llamacpp" if alias == "local" else alias

    def judge(self, request, **kwargs):
        self._endpoint_base, self._endpoint_key = self._resolve_endpoint()
        return super().judge(request, **kwargs)

    def _parse_retries(self) -> int:
        return max(0, int(self.settings.local.parse_retries))

    def _resolve_endpoint(self) -> tuple[str, str]:
        from agent.auxiliary_local_runtime import bare_llamacpp_endpoint

        if self.provider.strip().lower() not in LOCAL_ALIASES:
            return self.base_url, self.api_key
        endpoint = bare_llamacpp_endpoint(self.alias, self.base_url, self.api_key)
        if endpoint is None:
            # An explicit base_url or a configured provider under the alias resolves
            # through the normal call_llm path.
            return self.base_url, self.api_key
        base, key = endpoint
        if not base and self.settings.local.autostart:
            self._autostart()
            endpoint = bare_llamacpp_endpoint(self.alias, self.base_url, self.api_key)
            base, key = endpoint or ("", "")
        if not base:
            raise JudgmentUnavailable(
                "local llama.cpp server is not running (judgment.local.autostart=false)")
        return str(base), str(key or "")

    def _autostart(self) -> None:
        try:
            from hermes_cli.config import load_config
            from hermes_cli.local_runtime.bootstrap import ensure_local_runtime
        except Exception:  # pragma: no cover - import guard
            logger.debug("local runtime imports unavailable", exc_info=True)
            return
        try:
            supervisor: Any = ensure_local_runtime(load_config())
        except Exception as exc:
            logger.warning("judgment local autostart failed: %s", exc, exc_info=True)
            return
        if supervisor is None:
            return  # another process owns the server; re-resolution decides
        supervisor = cast(Any, supervisor)
        if not self.model:
            return
        try:
            ready = supervisor.ensure_model_ready(self.model)
        except KeyError:
            raise JudgmentUnavailable(
                f"local model {self.model!r} is not installed") from None
        except Exception as exc:
            logger.debug("judgment local model load failed", exc_info=True)
            raise JudgmentUnavailable(
                f"local model {self.model!r} could not be loaded: {exc}") from None
        if not ready:
            raise JudgmentUnavailable(f"local model {self.model!r} is not ready")

    def _complete(self, system: str, user: str, cancel_event: threading.Event | None,
                  *, retry: bool = False,
                  images: Any = ()) -> tuple[Any, str]:
        from agent.auxiliary_client import aux_interrupt_protection, call_llm

        if cancel_event is not None and cancel_event.is_set():
            raise JudgmentAborted("judgment aborted")
        merged = f"{system}\n\n{user}" if self.settings.local.merge_prompt else user
        messages = (
            [{"role": "user", "content": merged}]
            if self.settings.local.merge_prompt
            else [{"role": "system", "content": system},
                  {"role": "user", "content": user}]
        )
        kwargs: dict[str, Any] = {
            "task": "judge",
            "provider": self.alias,
            "messages": messages,
            "temperature": 0,
            "timeout": self.timeout,
            "max_tokens": self._max_tokens_for(),
            "fallback": False,
        }
        if self.model:
            kwargs["model"] = self.model
        if self._endpoint_base:
            kwargs["base_url"] = self._endpoint_base
        if self._endpoint_key:
            kwargs["api_key"] = self._endpoint_key
        route_info: dict[str, str] = {}
        kwargs["route_info"] = route_info
        try:
            with aux_interrupt_protection(cancel_event=cancel_event):
                response = call_llm(**kwargs)
        except JudgmentError:
            raise
        except Exception as exc:
            logger.debug("local judgment call failed", exc_info=True)
            raise JudgmentUnavailable(f"local judgment call failed: {exc}") from None
        return response, route_info.get("api_mode") or "chat_completions"

    def _max_tokens_for(self) -> int:
        """omp parity: a reasoning tiny model needs the thinking preamble budget, else 16."""
        reasoning = False
        if self.model:
            try:
                from agent.models_dev import get_model_capabilities

                caps = get_model_capabilities(self.alias, self.model, allow_network=False)
                reasoning = caps is not None and caps.supports_reasoning is True
            except Exception:  # health: allow BLE001 -- capability lookup is advisory here
                reasoning = False
        return max(1, self.settings.local.reasoning_max_tokens if reasoning
                   else self.settings.local.answer_max_tokens)
