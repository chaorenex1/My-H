"""Agent-definition front matter: the metadata contract behind the ``agent`` tool.

A named-agent file may open with a YAML block. That block is operator metadata, never prompt
text: :func:`parse_definition` validates it, :func:`resolve` merges it with the caller's
arguments (caller > definition > host default), and the body becomes the system prompt.

Fail closed: an invalid field raises :class:`DefinitionError`, which the tool reports as
``tool_error``. A mistyped ``toolsets`` must never widen the surface to "every tool", and a
mistyped ``model`` must never fall back silently to the host route.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agent.skill_utils import parse_frontmatter

logger = logging.getLogger(__name__)

#: Boolean fields a definition may set; the caller can override each one.
BOOL_FIELDS = frozenset({
    "read_only", "background", "worktree",
    "skip_memory", "skip_context_files", "skip_background_review", "load_soul_identity",
})

#: Plain string fields. ``name``/``description`` document the file and are never sent to the model.
STR_FIELDS = frozenset({"name", "description"})

#: Every key the contract knows; anything else is ignored with one warning (forward compatible).
KNOWN_FIELDS = BOOL_FIELDS | STR_FIELDS | frozenset({"model", "reasoning_effort", "toolsets",
                                                     "max_iterations"})


class DefinitionError(ValueError):
    """A definition the tool must refuse rather than guess around."""


def _bool(value: Any, field: str, source: str) -> bool:
    if not isinstance(value, bool):
        raise DefinitionError(f"{source}: {field} must be a boolean, got {value!r}")
    return value


def _model(value: Any, source: str) -> str:
    """``"provider:model"`` or a bare model name; a half-filled route is refused."""
    text = str(value or "").strip()
    if not text:
        return ""
    provider, sep, model = text.partition(":")
    if sep and (not provider.strip() or not model.strip()):
        raise DefinitionError(
            f"{source}: model must be 'provider:model' or a bare model name, got {text!r}")
    return text


def _toolsets(value: Any, source: str) -> list[str]:
    """A non-empty list of toolset names, each one the registry actually knows."""
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, (list, tuple)) or not names:
        raise DefinitionError(f"{source}: toolsets must be a non-empty list of toolset names")
    from toolsets import validate_toolset  # lazy: toolsets imports the registry at import time

    cleaned: list[str] = []
    for item in names:
        name = str(item).strip()
        if not name or not validate_toolset(name):
            # Fail closed: an unknown name must not degrade to the host's full surface.
            raise DefinitionError(f"{source}: unknown toolset {name!r}")
        cleaned.append(name)
    return cleaned


def _max_iterations(value: Any, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DefinitionError(f"{source}: max_iterations must be a positive integer, got {value!r}")
    return value


def _reasoning_effort(value: Any, source: str) -> str:
    from hermes_constants import parse_reasoning_effort

    text = str(value).strip() if value is not None else ""
    if not text:
        return ""
    if parse_reasoning_effort(text) is None:
        raise DefinitionError(f"{source}: unknown reasoning_effort {text!r}")
    return text


def parse_definition(text: str, *, source: str = "<definition>") -> tuple[dict[str, Any], str]:
    """``(metadata, body)`` for one definition file; unknown keys are dropped with one warning."""
    raw, body = parse_frontmatter(text)
    body = body.strip()
    if not raw:
        return {}, body
    unknown = sorted(str(key) for key in raw if str(key) not in KNOWN_FIELDS)
    if unknown:
        logger.warning("agent definition %s: ignoring unknown field(s): %s",
                       source, ", ".join(unknown))
    metadata: dict[str, Any] = {}
    for key, value in raw.items():
        key = str(key)
        if key in BOOL_FIELDS:
            metadata[key] = _bool(value, key, source)
        elif key in STR_FIELDS:
            metadata[key] = str(value or "").strip()
        elif key == "model":
            metadata[key] = _model(value, source)
        elif key == "reasoning_effort":
            metadata[key] = _reasoning_effort(value, source)
        elif key == "toolsets":
            metadata[key] = _toolsets(value, source)
        elif key == "max_iterations":
            metadata[key] = _max_iterations(value, source)
    return metadata, body


def body_threats(body: str) -> list[str]:
    """Injection-pattern IDs in a definition body (the same context scope context files get)."""
    from tools.threat_patterns import scan_for_threats

    return scan_for_threats(body, scope="context")


def resolve(caller: Mapping[str, Any], definition: Mapping[str, Any],
            defaults: Mapping[str, Any]) -> dict[str, Any]:
    """Per-key precedence for every knob: caller value, then definition, then host default.

    A caller entry is "given" only when it is not ``None``; parsing helpers return ``None`` for an
    omitted argument so an explicit ``false`` from the caller still beats a definition's ``true``.
    """
    resolved: dict[str, Any] = {}
    for key, default in defaults.items():
        value = caller.get(key)
        if value is None:
            value = definition.get(key)
        resolved[key] = default if value is None else value
    return resolved


__all__ = ["KNOWN_FIELDS", "DefinitionError", "body_threats", "parse_definition", "resolve"]
