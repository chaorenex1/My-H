"""Read and validate the top-level ``judgment`` config (ADR-17 D4/D8; ADR-11 legacy keys).

Invalid routes never fail agent startup: they are warned about, skipped, and
reported by ``hermes judgment doctor``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from hermes_cli.config import cfg_get
from hermes_cli.judgment_catalog import (
    DEFAULT_CHAIN,
    DEFAULT_MODEL_SPECS,
    DEFAULT_ROUTE_PROVIDERS,
    KNOWN_APIS,
    LOCAL_ALIASES,
    JudgmentModelSpec,
    normalize_chain,
    normalize_models,
    parse_chain_entry,
)

logger = logging.getLogger(__name__)

MODES = ("auto", "native", "text", "off")
TOOL_MODES = ("auto", "on", "off")


@dataclass(frozen=True)
class TextSettings:
    """Chat-model bridge knobs (ADR-17 D3/D8)."""

    parse_retries: int = 2
    max_tokens: int = 4096
    disable_reasoning: bool = True


@dataclass(frozen=True)
class LocalSettings:
    autostart: bool = False
    merge_prompt: bool = True
    parse_retries: int = 0
    answer_max_tokens: int = 16
    reasoning_max_tokens: int = 1024


@dataclass(frozen=True)
class ImageAssetSettings:
    """Local-path image assets (ADR-17 D5/D8); remote URLs are never fetched."""

    allow_local: bool = True
    allow_remote: bool = False
    max_bytes: int = 16 * 1024 * 1024


@dataclass(frozen=True)
class CostGateSettings:
    """Process-local judgment spend caps (ADR-17 D4/D8); ``0`` disables a cap."""

    max_usd_per_call: float = 0.05
    max_usd_per_session: float = 0.50


@dataclass(frozen=True)
class JudgmentSettings:
    mode: str = "auto"
    text_after_native: bool = False
    cache: bool = True
    cache_max_states: int = 50_000
    cache_usage: bool = True
    audit: bool = True
    audit_max_runs: int = 20_000
    eval_debug: bool = False
    eval_max_bytes: int = 52_428_800
    timeout_ms: int = 10_000
    redact_state: bool = True
    images: bool = True
    assets: ImageAssetSettings = field(default_factory=ImageAssetSettings)
    tools_enabled: str = "auto"
    text: TextSettings = field(default_factory=TextSettings)
    local: LocalSettings = field(default_factory=LocalSettings)
    cost_gate: CostGateSettings = field(default_factory=CostGateSettings)
    models: tuple[JudgmentModelSpec, ...] = DEFAULT_MODEL_SPECS
    chain: tuple[str, ...] = DEFAULT_CHAIN
    #: Legacy ``judgment.native`` routes; still parsed, folded into the catalog/chain.
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


def _non_negative_int(raw: Any, default: int, problem: str, problems: list[str]) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        problems.append(f"{problem} must be a non-negative integer (got {raw!r}); using {default}")
        return default
    if value < 0:
        problems.append(f"{problem} must be a non-negative integer (got {value}); using {default}")
        return default
    return value


def _non_negative_float(raw: Any, default: float, problem: str,
                        problems: list[str]) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        problems.append(f"{problem} must be a non-negative number (got {raw!r}); using {default}")
        return default
    if value < 0:
        problems.append(f"{problem} must be a non-negative number (got {value}); using {default}")
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
            "request_model_id": str(entry.get("request_model_id") or "").strip(),
            "max_questions": max_questions,
            "timeout_ms": _positive_int(timeout, default_timeout_ms, f"{where}.timeout_ms", problems),
        }
        routes.append(route)
    return tuple(routes)


def _legacy_native_model(route: Mapping[str, Any]) -> JudgmentModelSpec:
    provider = str(route["provider"])
    model = str(route["model"])
    return JudgmentModelSpec(
        id=f"{provider}/{model}",
        kind="native",
        api=str(route["api"]),
        provider=provider,
        model=model,
        base_url=str(route.get("base_url") or ""),
        api_key_env=str(route.get("api_key_env") or ""),
        request_model_id=str(route.get("request_model_id") or ""),
        max_questions=int(route.get("max_questions") or 0),
        timeout_ms=int(route.get("timeout_ms") or 0),
    )


def _apply_legacy_native(
    models: tuple[JudgmentModelSpec, ...],
    chain: tuple[str, ...],
    legacy: tuple[dict[str, Any], ...],
) -> tuple[tuple[JudgmentModelSpec, ...], tuple[str, ...]]:
    """Fold ``judgment.native`` (ADR-11) into the catalog and front of the chain."""
    if not legacy:
        return models, chain
    merged = list(models)
    positions = {spec.id: index for index, spec in enumerate(merged)}
    legacy_ids: list[str] = []
    for route in legacy:
        spec = _legacy_native_model(route)
        legacy_ids.append(spec.id)
        position = positions.get(spec.id)
        if position is None:
            positions[spec.id] = len(merged)
            merged.append(spec)
        elif merged[position] != spec:
            # User config owns an id a shipped default already uses: replace the
            # default rather than silently running the default's endpoint/model.
            logger.warning(
                "judgment: legacy native route %s overrides the built-in catalog entry",
                spec.id)
            merged[position] = spec
    by_id = {spec.id: spec for spec in merged}
    legacy_identities = {
        (str(route["api"]), str(route["provider"]), str(route["model"]))
        for route in legacy
    }
    folded: list[str] = []
    for model_id in legacy_ids:
        entry = f"native:{model_id}"
        if entry not in folded:
            folded.append(entry)
    if chain == DEFAULT_CHAIN:
        # An ADR-11 config listed its own native routes; the built-in two are only the
        # default for configs that never had any. Keep the role tail.
        chain = tuple(raw for raw in chain if parse_chain_entry(raw)[0] == "role")
    for raw in chain:
        kind, ident = parse_chain_entry(raw)
        spec = by_id.get(ident) if kind != "role" else None
        if spec is not None and (spec.api, spec.provider, spec.model) in legacy_identities:
            continue
        if raw not in folded:
            folded.append(raw)
    return tuple(merged), tuple(folded)


def _runtime_config() -> dict[str, Any]:
    """Live profile config, or the session-frozen one inside ``frozen_runtime_config``."""
    from hermes_cli.runtime_provider import load_config

    return load_config()


def load_judgment_settings(config: Mapping[str, Any] | None = None) -> JudgmentSettings:
    cfg: dict[str, Any] = dict(config) if config is not None else _runtime_config()
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
        parse_retries=_non_negative_int(
            local_raw.get("parse_retries", 0), 0,
            "judgment.local.parse_retries", problems),
        answer_max_tokens=_positive_int(
            local_raw.get("answer_max_tokens", 16), 16,
            "judgment.local.answer_max_tokens", problems),
        reasoning_max_tokens=_positive_int(
            local_raw.get("reasoning_max_tokens", 1024), 1024,
            "judgment.local.reasoning_max_tokens", problems),
    )
    text_section = section.get("text")
    text_raw: Mapping[str, Any] = text_section if isinstance(text_section, Mapping) else {}
    text = TextSettings(
        parse_retries=_non_negative_int(
            text_raw.get("parse_retries", 2), 2,
            "judgment.text.parse_retries", problems),
        max_tokens=_positive_int(
            text_raw.get("max_tokens", 4096), 4096,
            "judgment.text.max_tokens", problems),
        disable_reasoning=_bool(
            text_raw.get("disable_reasoning", True), True,
            "judgment.text.disable_reasoning", problems),
    )
    cost_section = section.get("cost_gate")
    cost_raw: Mapping[str, Any] = cost_section if isinstance(cost_section, Mapping) else {}
    cost_gate = CostGateSettings(
        max_usd_per_call=_non_negative_float(
            cost_raw.get("max_usd_per_call", 0.05), 0.05,
            "judgment.cost_gate.max_usd_per_call", problems),
        max_usd_per_session=_non_negative_float(
            cost_raw.get("max_usd_per_session", 0.50), 0.50,
            "judgment.cost_gate.max_usd_per_session", problems),
    )
    legacy_native = _normalize_routes(
        section.get("native", []), timeout_ms, problems)
    models = normalize_models(section.get("models"), problems)
    chain = normalize_chain(section.get("chain"), problems)
    models, chain = _apply_legacy_native(models, chain, legacy_native)

    audit_section = section.get("audit", True)
    if isinstance(audit_section, Mapping):
        audit_enabled = _bool(
            audit_section.get("enabled", True), True, "judgment.audit.enabled", problems)
        audit_max_runs = _positive_int(
            audit_section.get("max_runs", section.get("audit_max_runs", 20_000)),
            20_000, "judgment.audit.max_runs", problems)
    else:
        audit_enabled = _bool(audit_section, True, "judgment.audit", problems)
        audit_max_runs = _positive_int(
            section.get("audit_max_runs", 20_000), 20_000,
            "judgment.audit_max_runs", problems)

    cache_section = section.get("cache", True)
    if isinstance(cache_section, Mapping):
        cache_enabled = _bool(
            cache_section.get("enabled", True), True, "judgment.cache.enabled", problems)
        cache_max_states = _positive_int(
            cache_section.get("max_states", section.get("cache_max_states", 50_000)),
            50_000, "judgment.cache.max_states", problems)
        cache_usage = _bool(
            cache_section.get("usage", True), True, "judgment.cache.usage", problems)
    else:
        cache_enabled = _bool(cache_section, True, "judgment.cache", problems)
        cache_max_states = _positive_int(
            section.get("cache_max_states", 50_000), 50_000,
            "judgment.cache_max_states", problems)
        cache_usage = True

    images_section = section.get("images", True)
    if isinstance(images_section, Mapping):
        images_enabled = _bool(
            images_section.get("enabled", True), True, "judgment.images.enabled", problems)
        assets_raw = images_section.get("assets")
    else:
        images_enabled = _bool(images_section, True, "judgment.images", problems)
        assets_raw = None
    assets_section: Mapping[str, Any] = assets_raw if isinstance(assets_raw, Mapping) else {}
    if assets_raw is not None and not isinstance(assets_raw, Mapping):
        problems.append("judgment.images.assets must be an object; using defaults")
    assets = ImageAssetSettings(
        allow_local=_bool(
            assets_section.get("allow_local", True), True,
            "judgment.images.assets.allow_local", problems),
        allow_remote=_bool(
            assets_section.get("allow_remote", False), False,
            "judgment.images.assets.allow_remote", problems),
        max_bytes=_positive_int(
            assets_section.get("max_bytes", 16 * 1024 * 1024), 16 * 1024 * 1024,
            "judgment.images.assets.max_bytes", problems),
    )
    if assets.allow_remote:
        problems.append(
            "judgment.images.assets.allow_remote is not supported; remote URLs are never fetched")

    settings = JudgmentSettings(
        mode=mode,
        text_after_native=_bool(section.get("text_after_native", False), False,
                                "judgment.text_after_native", problems),
        cache=cache_enabled,
        cache_max_states=cache_max_states,
        cache_usage=cache_usage,
        audit=audit_enabled,
        audit_max_runs=audit_max_runs,
        eval_debug=_bool(section.get("eval_debug", False), False,
                         "judgment.eval_debug", problems),
        eval_max_bytes=_positive_int(section.get("eval_max_bytes", 52_428_800), 52_428_800,
                                     "judgment.eval_max_bytes", problems),
        timeout_ms=timeout_ms,
        redact_state=_bool(section.get("redact_state", True), True,
                           "judgment.redact_state", problems),
        images=images_enabled,
        assets=assets,
        tools_enabled=tools_enabled,
        text=text,
        local=local,
        cost_gate=cost_gate,
        models=models,
        chain=chain,
        native=legacy_native,
        problems=tuple(problems),
    )
    for problem in settings.problems:
        logger.warning("judgment config: %s", problem)
    return settings


def auxiliary_task_config(
    config: Mapping[str, Any] | None = None, task: str = "judge",
) -> Mapping[str, Any]:
    """One ``auxiliary.<task>`` block; empty when the slot is unconfigured."""
    cfg: dict[str, Any] = dict(config) if config is not None else _runtime_config()
    block = cfg_get(cfg, "auxiliary", task, default={})
    return block if isinstance(block, Mapping) else {}


def auxiliary_judge_config(config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return auxiliary_task_config(config, "judge")


def is_local_provider(provider: str | None) -> bool:
    return str(provider or "").strip().lower() in LOCAL_ALIASES
