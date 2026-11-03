"""Read and validate the top-level ``judgment`` config (ADR-11 D5/D10/D13).

Invalid routes never fail agent startup: they are warned about, skipped, and
reported by ``hermes judgment doctor``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)

MODES = ("auto", "native", "text", "off")
TOOL_MODES = ("auto", "on", "off")
LOCAL_ALIASES = frozenset({"llamacpp", "llama.cpp", "llama-cpp", "local"})
#: Every wire dialect this build understands (ADR-11 D4).
KNOWN_APIS = ("typesafe", "openrouter-decisions", "openai-decisions", "workers-ai-systemone")
DEFAULT_ROUTE_BASE_URLS = {
    "typesafe": "https://api.typesafe.ai",
    "openrouter-decisions": "https://openrouter.ai/api/alpha",
    "openai-decisions": "https://api.openai.com/v1",
    # ``<account>`` is filled from CLOUDFLARE_ACCOUNT_ID when credentials resolve.
    "workers-ai-systemone": (
        "https://api.cloudflare.com/client/v4/accounts/<account>/ai/run"),
}
#: Judgment transports keep their own provider identity so billing/pricing never borrows chat.
DEFAULT_ROUTE_PROVIDERS = {
    "typesafe": "typesafe",
    "openrouter-decisions": "openrouter",
    "openai-decisions": "openai-api",
    "workers-ai-systemone": "cloudflare-workers-ai",
}


@dataclass(frozen=True)
class LocalSettings:
    autostart: bool = False
    merge_prompt: bool = True
    answer_max_tokens: int = 16
    reasoning_max_tokens: int = 1024


@dataclass(frozen=True)
class JudgmentSettings:
    mode: str = "auto"
    text_after_native: bool = False
    cache: bool = True
    cache_max_states: int = 50_000
    audit: bool = True
    audit_max_runs: int = 20_000
    eval_debug: bool = False
    eval_max_bytes: int = 52_428_800
    timeout_ms: int = 10_000
    parse_retries: int = 2
    redact_state: bool = True
    images: bool = True
    tools_enabled: str = "auto"
    local: LocalSettings = field(default_factory=LocalSettings)
    native: tuple[dict[str, Any], ...] = ()
    problems: tuple[str, ...] = ()


def _positive_int(raw: Any, default: int, problem: str, problems: list[str]) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        problems.append(f"{problem} must be a positive integer (got {raw!r}); using {default}")
        return default
    if value <= 0:
        problems.append(f"{problem} must be a positive integer (got {value}); using {default}")
        return default
    return value


def _bool(raw: Any, default: bool, problem: str, problems: list[str]) -> bool:
    if isinstance(raw, bool):
        return raw
    problems.append(f"{problem} must be true or false (got {raw!r}); using {default}")
    return default


def _enum(raw: Any, allowed: tuple[str, ...], default: str, problem: str,
          problems: list[str]) -> str:
    value = str(raw or "").strip().lower()
    if value in allowed:
        return value
    problems.append(f"{problem} must be one of {allowed} (got {raw!r}); using {default}")
    return default


def _normalize_routes(raw: Any, default_timeout_ms: int, problems: list[str]) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append("judgment.native must be a list; ignoring it")
        return ()
    routes: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for index, entry in enumerate(raw):
        where = f"judgment.native[{index}]"
        if not isinstance(entry, Mapping):
            problems.append(f"{where} must be an object; skipping")
            continue
        api = str(entry.get("api") or "").strip().lower()
        model = str(entry.get("model") or "").strip()
        provider = str(entry.get("provider") or DEFAULT_ROUTE_PROVIDERS.get(api, api)).strip().lower()
        base_url = str(entry.get("base_url") or "").strip().rstrip("/")
        if api not in KNOWN_APIS:
            problems.append(f"{where}: unknown api {api!r}; skipping")
            continue
        if not model:
            problems.append(f"{where}: model is required; skipping")
            continue
        dedupe = (api, provider, model, base_url)
        if dedupe in seen:
            problems.append(f"{where}: duplicate route {api}:{provider}/{model}; skipping")
            continue
        seen.add(dedupe)
        timeout = entry.get("timeout_ms", default_timeout_ms)
        raw_max = entry.get("max_questions")
        if raw_max is None:
            max_questions = 0
        else:
            try:
                max_questions = int(raw_max)
            except (TypeError, ValueError):
                max_questions = 0
                problems.append(f"{where}.max_questions must be a non-negative integer; using 0")
            else:
                if max_questions < 0:
                    problems.append(f"{where}.max_questions must be a non-negative integer; using 0")
                    max_questions = 0
        route = {
            "api": api,
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "api_key_env": str(entry.get("api_key_env") or "").strip(),
            "max_questions": max_questions,
            "timeout_ms": _positive_int(timeout, default_timeout_ms, f"{where}.timeout_ms", problems),
        }
        routes.append(route)
    return tuple(routes)


def load_judgment_settings(config: Mapping[str, Any] | None = None) -> JudgmentSettings:
    cfg: dict[str, Any] = dict(config) if config is not None else load_config()
    problems: list[str] = []
    section = cfg_get(cfg, "judgment", default={})
    if not isinstance(section, Mapping):
        problems.append("judgment must be an object; using defaults")
        section = {}
    mode = _enum(section.get("mode", "auto"), MODES, "auto", "judgment.mode", problems)
    tools_section = section.get("tools")
    tools_enabled = _enum(
        tools_section.get("enabled", "auto") if isinstance(tools_section, Mapping) else "auto",
        TOOL_MODES, "auto", "judgment.tools.enabled", problems)
    timeout_ms = _positive_int(
        section.get("timeout_ms", 10_000), 10_000, "judgment.timeout_ms", problems)
    local_section = section.get("local")
    local_raw: Mapping[str, Any] = local_section if isinstance(local_section, Mapping) else {}
    local = LocalSettings(
        autostart=_bool(local_raw.get("autostart", False), False,
                        "judgment.local.autostart", problems),
        merge_prompt=_bool(local_raw.get("merge_prompt", True), True,
                           "judgment.local.merge_prompt", problems),
        answer_max_tokens=_positive_int(
            local_raw.get("answer_max_tokens", 16), 16,
            "judgment.local.answer_max_tokens", problems),
        reasoning_max_tokens=_positive_int(
            local_raw.get("reasoning_max_tokens", 1024), 1024,
            "judgment.local.reasoning_max_tokens", problems),
    )
    settings = JudgmentSettings(
        mode=mode,
        text_after_native=_bool(section.get("text_after_native", False), False,
                                "judgment.text_after_native", problems),
        cache=_bool(section.get("cache", True), True, "judgment.cache", problems),
        cache_max_states=_positive_int(section.get("cache_max_states", 50_000), 50_000,
                                       "judgment.cache_max_states", problems),
        audit=_bool(section.get("audit", True), True, "judgment.audit", problems),
        audit_max_runs=_positive_int(section.get("audit_max_runs", 20_000), 20_000,
                                     "judgment.audit_max_runs", problems),
        eval_debug=_bool(section.get("eval_debug", False), False,
                         "judgment.eval_debug", problems),
        eval_max_bytes=_positive_int(section.get("eval_max_bytes", 52_428_800), 52_428_800,
                                     "judgment.eval_max_bytes", problems),
        timeout_ms=timeout_ms,
        parse_retries=_positive_int(section.get("parse_retries", 2), 2,
                                    "judgment.parse_retries", problems),
        redact_state=_bool(section.get("redact_state", True), True,
                           "judgment.redact_state", problems),
        images=_bool(section.get("images", True), True, "judgment.images", problems),
        tools_enabled=tools_enabled,
        local=local,
        native=_normalize_routes(section.get("native", []), timeout_ms, problems),
        problems=tuple(problems),
    )
    for problem in settings.problems:
        logger.warning("judgment config: %s", problem)
    return settings


def auxiliary_judge_config(config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    cfg: dict[str, Any] = dict(config) if config is not None else load_config()
    block = cfg_get(cfg, "auxiliary", "judge", default={})
    return block if isinstance(block, Mapping) else {}


def is_local_provider(provider: str | None) -> bool:
    return str(provider or "").strip().lower() in LOCAL_ALIASES
