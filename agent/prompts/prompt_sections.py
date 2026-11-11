"""System-prompt section registry and renderer (ADR-18 D1/D3/D13).

A section is a manifest entry plus a Jinja2 leaf template rendered with a context dict
the caller builds. Section selection, tier, gating, overrides and assembly stay outside
this module; this module owns exactly: manifest parsing, context validation, rendering,
and the character budget/truncation policy declared per section.

Adding a section means adding its ``.md`` file and manifest entry — no code change here.
Usage::

    render_section("rfc2119")                                  # static section
    render_section("identity", {"role": "Hermes Agent"})       # defaults fill the rest
    render_section("safety")                                   # 02: default policy
    render_section("safety", {"safety_rules": "- No force-push."})   # 02: default + custom
    render_section("safety", {"safety_rules": "- No force-push.", "safety_override": True})
                                                               # 02: custom replaces default
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

SYSTEM_PROMPT_DIR = Path(__file__).resolve().parent / "system"
MANIFEST_PATH = SYSTEM_PROMPT_DIR / "manifest.json"

TRUNCATE_TAIL = "tail"
TRUNCATE_HEAD_TAIL = "head_tail"
TRUNCATE_DROP_OLDEST_LINES = "drop_oldest_lines"
TRUNCATE_ERROR = "error"
_CONTEXT_TYPES = frozenset({"str", "bool", "list", "int"})

_TRUNCATE_STRATEGIES = frozenset(
    {TRUNCATE_TAIL, TRUNCATE_HEAD_TAIL, TRUNCATE_DROP_OLDEST_LINES, TRUNCATE_ERROR}
)


class PromptSectionError(ValueError):
    """Base class for registry/render failures (bad manifest, bad context)."""


class PromptBudgetError(PromptSectionError):
    """A section exceeded a ``truncate: error`` budget."""


@dataclass(frozen=True)
class PromptSection:
    """One manifest entry; the renderer needs nothing else to render a section."""

    key: str
    path: str | None
    tier: str
    gate: str
    budget: int | None
    truncate: str
    override: bool
    renderer: str | None
    context_keys: tuple[str, ...]
    context_types: Mapping[str, str]


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    """Parse the manifest; malformed JSON or a missing file fails fast."""
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def load_sections(path: Path = MANIFEST_PATH) -> dict[str, PromptSection]:
    """Manifest order preserved; keys unique; every template path exists."""
    manifest = load_manifest(path)
    sections: dict[str, PromptSection] = {}
    for raw in manifest.get("sections", ()):
        section = PromptSection(
            key=raw["key"],
            path=raw.get("path"),
            tier=raw["tier"],
            gate=raw["gate"],
            budget=raw.get("budget"),
            truncate=raw.get("truncate", TRUNCATE_TAIL),
            override=bool(raw.get("override", False)),
            renderer=raw.get("renderer"),
            context_keys=tuple(raw.get("context_keys", ())),
            context_types=dict(raw.get("context_types", {})),
        )
        if section.key in sections:
            raise PromptSectionError(f"duplicate prompt section key: {section.key}")
        if section.truncate not in _TRUNCATE_STRATEGIES:
            raise PromptSectionError(f"{section.key}: unknown truncate strategy {section.truncate!r}")
        if section.path and not (SYSTEM_PROMPT_DIR / section.path).is_file():
            raise PromptSectionError(f"{section.key}: template not found: {section.path}")
        unknown_types = set(section.context_types) - set(section.context_keys)
        if unknown_types:
            raise PromptSectionError(
                f"{section.key}: context_types for undeclared keys: {sorted(unknown_types)}"
            )
        bad_types = set(section.context_types.values()) - _CONTEXT_TYPES
        if bad_types:
            raise PromptSectionError(
                f"{section.key}: unknown context type(s): {sorted(bad_types)}"
            )
        sections[section.key] = section
    return sections


def read_prompt_text(path: Path) -> str:
    """UTF-8 decode and LF normalization, so a CRLF checkout renders LF bytes."""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _template_sources() -> dict[str, str]:
    return {
        section.path: read_prompt_text(SYSTEM_PROMPT_DIR / section.path)
        for section in load_sections().values()
        if section.path
    }


def build_environment(sources: Mapping[str, str]):
    """The single Jinja2 environment every section renders through (ADR-18 D13)."""
    from jinja2 import DictLoader, Environment, StrictUndefined

    return Environment(
        loader=DictLoader(dict(sources)),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
        newline_sequence="\n",
        extensions=[],
    )


_ENV_LOCK = threading.Lock()
_ENV = None


def environment():
    """Lazily built, process-wide environment. "No Jinja2" is a clear error, not an ImportError at import."""
    global _ENV
    if _ENV is None:
        with _ENV_LOCK:
            if _ENV is None:
                try:
                    _ENV = build_environment(_template_sources())
                except ImportError as exc:  # pragma: no cover - supported envs pin jinja2==3.1.6
                    raise PromptSectionError(
                        "Jinja2 is required to render prompt sections (ADR-18 D13)"
                    ) from exc
    return _ENV


def validate_context(section: PromptSection, context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Reject undeclared keys and type mismatches; missing keys may fall back inside the template."""
    supplied = dict(context or {})
    undeclared = set(supplied) - set(section.context_keys)
    if undeclared:
        raise PromptSectionError(
            f"{section.key}: undeclared context key(s): {sorted(undeclared)}"
        )
    for key, value in supplied.items():
        expected = section.context_types.get(key)
        if expected == "str" and not isinstance(value, str):
            raise PromptSectionError(f"{section.key}: context[{key!r}] must be str")
        if expected == "bool" and not isinstance(value, bool):
            raise PromptSectionError(f"{section.key}: context[{key!r}] must be bool")
        if expected == "int" and (isinstance(value, bool) or not isinstance(value, int)):
            raise PromptSectionError(f"{section.key}: context[{key!r}] must be int")
        if expected == "list" and not (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        ):
            raise PromptSectionError(f"{section.key}: context[{key!r}] must be list[str]")
        if expected is not None and expected not in _CONTEXT_TYPES:
            raise PromptSectionError(
                f"{section.key}: unknown context type {expected!r} for {key!r}"
            )
    return supplied


def _marker(removed: int) -> str:
    return f"[...truncated {removed} chars...]"


def _truncate_tail(text: str, budget: int) -> str:
    marker = _marker(len(text))
    if len(marker) >= budget:
        return text[:budget]
    keep = max(0, budget - len(marker))
    while keep > 0:
        marker = _marker(len(text) - keep)
        if keep + len(marker) <= budget:
            break
        keep -= 1
    return text[:keep] + marker


def _truncate_head_tail(text: str, budget: int) -> str:
    marker = _marker(len(text))
    if len(marker) >= budget:
        return text[:budget]
    keep = max(0, budget - len(marker))
    head, tail = int(keep * 0.7), int(keep * 0.2)
    while head > 0 or tail > 0:
        marker = _marker(len(text) - head - tail)
        if head + tail + len(marker) <= budget:
            break
        if head >= tail and head > 0:
            head -= 1
        elif tail > 0:
            tail -= 1
    return text[:head] + marker + (text[-tail:] if tail else "")


def _drop_oldest_lines(text: str, budget: int) -> str:
    lines = text.splitlines(keepends=True)
    while len(lines) > 1 and sum(len(line) for line in lines) > budget:
        lines.pop(0)
    return "".join(lines)


def apply_budget(text: str, section: PromptSection) -> str:
    """Apply ``section.budget``/``truncate``; ``None`` budget means unlimited (ADR-18 D3)."""
    if section.budget is None or section.budget <= 0 or len(text) <= section.budget:
        return text
    if section.truncate == TRUNCATE_ERROR:
        raise PromptBudgetError(
            f"{section.key}: {len(text)} chars exceeds budget {section.budget}"
        )
    if section.truncate == TRUNCATE_HEAD_TAIL:
        return _truncate_head_tail(text, section.budget)
    if section.truncate == TRUNCATE_DROP_OLDEST_LINES:
        return _drop_oldest_lines(text, section.budget)
    return _truncate_tail(text, section.budget)


def render_section(key: str, context: Mapping[str, Any] | None = None) -> str:
    """Render one section by manifest key. Unknown keys and Python-only sections raise."""
    section = load_sections().get(key)
    if section is None:
        raise PromptSectionError(f"unknown prompt section: {key}")
    if not section.path:
        raise PromptSectionError(f"{key}: section has no template (renderer={section.renderer!r})")
    text = environment().get_template(section.path).render(**validate_context(section, context))
    return apply_budget(text, section)


def render_sections(keys: Iterable[str], contexts: Mapping[str, Mapping[str, Any]] | None = None) -> list[str]:
    """Render several sections in the given order; ``contexts`` is keyed by section key."""
    contexts = contexts or {}
    return [render_section(key, contexts.get(key)) for key in keys]


__all__ = [
    "MANIFEST_PATH",
    "SYSTEM_PROMPT_DIR",
    "PromptBudgetError",
    "PromptSection",
    "PromptSectionError",
    "apply_budget",
    "build_environment",
    "environment",
    "load_manifest",
    "load_sections",
    "read_prompt_text",
    "render_section",
    "render_sections",
    "validate_context",
]
