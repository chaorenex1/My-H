"""Judgment model catalog: kinds, wires, defaults, and chain grammar (ADR-17 D4).

A judgment model is not a chat model picker entry. The catalog keeps only what the
judge chain needs to build a candidate: which backend kind (native System One wire,
on-device llama.cpp, or a prompted chat model), the wire dialect, provider identity,
request model id, and the per-model request limits that decide whether a candidate can
answer a given request.

``judgment.models`` is data; ``judgment.chain`` is an ordered list of references into
it (``native:<id>`` / ``local:<id>``) or role names (``tiny``/``smol``/``default``,
plus the ``memory``/``commit``/``judge`` compatibility aliases).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)

KINDS = ("native", "local", "online")
#: Every native wire dialect this build understands (ADR-11 D4).
KNOWN_APIS = ("typesafe", "openrouter-decisions", "openai-decisions", "workers-ai-systemone")
LOCAL_ALIASES = frozenset({"llamacpp", "llama.cpp", "llama-cpp", "local", "local-inference"})
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

#: Chat roles the judge chain maps onto ``auxiliary.<task>`` (ADR-17 D4).
ROLE_TASKS = {
    "tiny": "tiny",
    "smol": "smol",
    "default": "default",
    "memory": "background_review",
    "commit": "commit_message",
    "judge": "judge",
}
#: Standard prompted tail used by ``mode: text`` and by ``text_after_native``.
TEXT_ROLES = ("tiny", "smol", "default")

DEFAULT_CHAIN = (
    "native:typesafe/jev-1.14",
    "native:openrouter/typesafe-jev-1.14",
    "tiny",
    "smol",
    "default",
)
DEFAULT_MODELS: tuple[dict[str, Any], ...] = (
    {
        "id": "typesafe/jev-1.14", "kind": "native", "api": "typesafe",
        "provider": "typesafe", "model": "jev-1.14.0",
    },
    {
        "id": "openrouter/typesafe-jev-1.14", "kind": "native",
        "api": "openrouter-decisions", "provider": "openrouter",
        "model": "typesafe/jev-1.14",
    },
)


@dataclass(frozen=True)
class JudgmentModelSpec:
    """One ``judgment.models[]`` entry; a chain resolves an id to this."""

    id: str
    kind: str
    api: str = ""
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    request_model_id: str = ""
    max_questions: int = 0
    #: ``None`` inherits the wire's capability; a bool or a bounds mapping overrides it.
    image_input: bool | Mapping[str, Any] | None = None
    boxes: bool | None = None
    timeout_ms: int = 0

    @property
    def image_limits(self) -> Mapping[str, Any] | None:
        """Per-model image bounds when ``image_input`` is a mapping (used from P4 on)."""
        return self.image_input if isinstance(self.image_input, Mapping) else None


def _positive_or_zero_int(raw: Any) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _normalize_kind(raw: Any, api: str, provider: str) -> str:
    kind = str(raw or "").strip().lower()
    if kind in KINDS:
        return kind
    if api in KNOWN_APIS:
        return "native"
    if api in LOCAL_ALIASES or provider in LOCAL_ALIASES:
        return "local"
    return "online"


def normalize_models(raw: Any, problems: list[str]) -> tuple[JudgmentModelSpec, ...]:
    """Parse ``judgment.models``; bad entries are reported and skipped, never fatal."""
    if raw is None:
        raw = DEFAULT_MODELS
    if not isinstance(raw, (list, tuple)):
        problems.append("judgment.models must be a list; using the built-in catalog")
        raw = DEFAULT_MODELS
    models: list[JudgmentModelSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"judgment.models[{index}]"
        if not isinstance(entry, Mapping):
            problems.append(f"{where} must be an object; skipping")
            continue
        api = str(entry.get("api") or "").strip().lower()
        provider = str(entry.get("provider") or "").strip().lower()
        model = str(entry.get("model") or "").strip()
        kind = _normalize_kind(entry.get("kind"), api, provider)
        if kind == "native":
            if api not in KNOWN_APIS:
                problems.append(f"{where}: unknown judgment api {api!r}; skipping")
                continue
            provider = provider or DEFAULT_ROUTE_PROVIDERS[api]
        elif kind == "local":
            provider = provider or "local"
        if not model:
            problems.append(f"{where}: model is required; skipping")
            continue
        model_id = str(entry.get("id") or "").strip() or f"{provider or api}/{model}"
        if model_id in seen:
            problems.append(f"{where}: duplicate model id {model_id!r}; skipping")
            continue
        seen.add(model_id)
        timeout_ms = 0
        if entry.get("timeout_ms") is not None:
            parsed_timeout = _positive_or_zero_int(entry.get("timeout_ms"))
            if parsed_timeout is None:
                problems.append(f"{where}.timeout_ms must be a non-negative integer; using 0")
            else:
                timeout_ms = parsed_timeout
        max_questions = 0
        if entry.get("max_questions") is not None:
            parsed_max = _positive_or_zero_int(entry.get("max_questions"))
            if parsed_max is None:
                problems.append(f"{where}.max_questions must be a non-negative integer; using 0")
            else:
                max_questions = parsed_max
        image_input = entry.get("image_input")
        if image_input is not None and not isinstance(image_input, (bool, Mapping)):
            problems.append(f"{where}.image_input must be a boolean or an object; ignoring it")
            image_input = None
        boxes = entry.get("boxes")
        if boxes is not None and not isinstance(boxes, bool):
            problems.append(f"{where}.boxes must be true or false; ignoring it")
            boxes = None
        models.append(JudgmentModelSpec(
            id=model_id,
            kind=kind,
            api=api,
            provider=provider,
            model=model,
            base_url=str(entry.get("base_url") or "").strip().rstrip("/"),
            api_key_env=str(entry.get("api_key_env") or "").strip(),
            request_model_id=str(entry.get("request_model_id") or "").strip(),
            max_questions=max_questions,
            image_input=image_input,
            boxes=boxes,
            timeout_ms=timeout_ms,
        ))
    return tuple(models)


def normalize_chain(raw: Any, problems: list[str]) -> tuple[str, ...]:
    """Parse ``judgment.chain``; ids are validated against the catalog at resolution."""
    if raw is None:
        return DEFAULT_CHAIN
    if not isinstance(raw, list):
        problems.append("judgment.chain must be a list; using the built-in chain")
        return DEFAULT_CHAIN
    chain: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, str) or not entry.strip():
            problems.append(f"judgment.chain[{index}] must be a non-empty string; skipping")
            continue
        chain.append(entry.strip())
    return tuple(chain)


def model_index(models: tuple[JudgmentModelSpec, ...]) -> dict[str, JudgmentModelSpec]:
    return {spec.id: spec for spec in models}


def parse_chain_entry(raw: str) -> tuple[str, str]:
    """``native:<id>`` → ``("native", id)``; a bare word → ``("role", role)``."""
    prefix, separator, ident = raw.partition(":")
    if separator and prefix in KINDS:
        return prefix, ident.strip()
    return "role", raw.strip().lower()


#: The catalog defaults as resolved specs, so ``JudgmentSettings()`` and
#: ``load_judgment_settings({})`` describe the same chain.
DEFAULT_MODEL_SPECS: tuple[JudgmentModelSpec, ...] = normalize_models(DEFAULT_MODELS, [])
