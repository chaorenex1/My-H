"""Agent-definition front matter: the metadata contract behind the ``agent`` tool.

A named-agent file may open with a YAML block. That block is operator metadata, never prompt
text: :func:`parse_definition` validates it, :func:`resolve` merges it with the caller's
arguments (caller > definition > host default), and the body becomes the system prompt.

Fail closed: an invalid field raises :class:`DefinitionError`, which the tool reports as
``tool_error``. A mistyped ``toolsets`` must never widen the surface to "every tool", and a
mistyped ``model`` must never fall back silently to the host route.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from agent.skill_utils import parse_frontmatter

logger = logging.getLogger(__name__)

#: Boolean fields a definition may set; the caller can override each one.
BOOL_FIELDS = frozenset({
    "background", "worktree",
    "skip_memory", "skip_context_files", "skip_background_review", "load_soul_identity",
    "structured", "read-summarize",
    "eager_tasks", "eager_tasks_always", "inline_first",
})

#: Plain string fields. ``name``/``description`` document the file and are never sent to the model.
STR_FIELDS = frozenset({"name", "description", "delegation_bias"})

#: omp spells the read-summary switch as ``readSummarize`` after normalization; accept both
#: spellings and store the front-matter one canonical field.
_FIELD_ALIASES = {"readSummarize": "read-summarize"}

#: Every key the contract knows; anything else is ignored with one warning (forward compatible).
KNOWN_FIELDS = BOOL_FIELDS | STR_FIELDS | frozenset({"model", "reasoning_effort", "toolsets",
                                                     "enable_tools", "max_iterations", "output",
                                                     "spawns", "blocking", "autoloadSkills",
                                                     "advisor"})


class DefinitionError(ValueError):
    """A definition the tool must refuse rather than guess around."""


def _bool(value: Any, field: str, source: str) -> bool:
    if not isinstance(value, bool):
        raise DefinitionError(f"{source}: {field} must be a boolean, got {value!r}")
    return value


def _model(value: Any, source: str) -> str:
    """``"provider:model"``, a bare model name or ``"@role"``; a half-filled route is refused.

    Only syntax is checked here: whether the role exists is decided at call time, against the
    profile's live ``auxiliary``/``delegation``/``advisor`` configuration.
    """
    if isinstance(value, (list, tuple)):
        raise DefinitionError(
            f"{source}: model takes one selector per definition, got {value!r}")
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("@"):
        role, _, level = text[1:].partition(":")
        if not role or any(char.isspace() for char in role) or ":" in role:
            raise DefinitionError(
                f"{source}: model '@role' needs a role name, got {text!r}")
        if level:
            from hermes_constants import parse_reasoning_effort

            if parse_reasoning_effort(level) is None:
                raise DefinitionError(
                    f"{source}: model '@role:level' has an unknown level {level!r}")
        return text
    provider, sep, model = text.partition(":")
    if sep and (not provider.strip() or not model.strip()):
        raise DefinitionError(
            f"{source}: model must be 'provider:model', a bare model name or '@role', "
            f"got {text!r}")
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


def _enable_tools(value: Any, source: str) -> list[str]:
    """An explicit tool-name allowlist; non-empty entries are kept in declaration order.

    Names are not resolved here: the registry is live and plugin tools register during
    discovery, so the runtime intersects this list with the tools actually built. A typo
    therefore narrows the surface instead of widening it.
    """
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, (list, tuple)):
        raise DefinitionError(f"{source}: enable_tools must be an array of tool names")
    cleaned: list[str] = []
    for item in names:
        name = str(item).strip()
        if name and name not in cleaned:
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


#: JSON Schema keywords omp's JTD-shaped ``output`` never uses. One present at a node proves the
#: document is already JSON Schema, which is passed through instead of rebuilt from JTD keywords.
_JSON_SCHEMA_MARKERS = frozenset({
    "$schema", "$id", "$defs", "$ref", "required", "additionalProperties", "items", "anyOf",
    "oneOf", "allOf", "not", "const", "patternProperties", "prefixItems",
})


def _is_json_schema(node: Mapping[str, Any]) -> bool:
    """True when *node* is JSON Schema rather than omp's JTD-shaped front matter."""
    return "type" in node or any(key in node for key in _JSON_SCHEMA_MARKERS)


def _description(node: Mapping[str, Any]) -> dict[str, Any]:
    """``metadata.description`` becomes the node's ``description``; other metadata stays opaque."""
    metadata = node.get("metadata")
    if isinstance(metadata, Mapping):
        text = str(metadata.get("description") or "").strip()
        if text:
            return {"description": text}
    return {}


def _normalize_omp_node(node: Any, source: str) -> dict[str, Any]:
    """One JTD-shaped ``output`` node as JSON Schema (omp's front-matter dialect)."""
    if not isinstance(node, Mapping):
        raise DefinitionError(f"{source}: output schema nodes must be mappings, got {node!r}")
    if "enum" in node:
        values = node["enum"]
        if not isinstance(values, (list, tuple)):
            raise DefinitionError(f"{source}: output enum must be a list, got {values!r}")
        return {"enum": list(values), **_description(node)}
    if "elements" in node:
        return {"type": "array", "items": _normalize_omp_node(node["elements"], source),
                **_description(node)}
    required_keys = node.get("properties") or {}
    optional_keys = node.get("optionalProperties") or {}
    if required_keys or optional_keys:
        if not isinstance(required_keys, Mapping) or not isinstance(optional_keys, Mapping):
            raise DefinitionError(
                f"{source}: output properties/optionalProperties must be mappings")
        properties: dict[str, Any] = {}
        required: list[str] = []
        for key, value in required_keys.items():
            name = str(key)
            properties[name] = _normalize_omp_node(value, source)
            if name not in optional_keys:
                required.append(name)
        for key, value in optional_keys.items():
            name = str(key)
            if name not in properties:
                properties[name] = _normalize_omp_node(value, source)
        schema: dict[str, Any] = {"type": "object", "properties": properties,
                                  "additionalProperties": False}
        if required:
            schema["required"] = required
        return {**schema, **_description(node)}
    if "type" in node:
        return {"type": node["type"], **_description(node)}
    return _description(node)


def normalize_output(value: Any, source: str) -> dict[str, Any]:
    """The definition's ``output`` as JSON Schema; omp's JTD-shaped dialect is normalized.

    omp stores ``properties``/``optionalProperties``/``elements``/``metadata`` in front matter;
    Hermes's canonical form is JSON Schema, so the same file is accepted by running the same
    ``validate_output`` the delegated children use in :mod:`tools.delegation_output_schema`.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise DefinitionError(f"{source}: output must be a mapping or a JSON object")
    if not isinstance(value, Mapping) or not value:
        raise DefinitionError(f"{source}: output must be a non-empty mapping")
    if _is_json_schema(value):
        schema: dict[str, Any] = dict(value)
    else:
        schema = _normalize_omp_node(value, source)
    from tools.delegation_output_schema import coerce_output_schema

    checked, error = coerce_output_schema(schema)
    if error is not None:
        raise DefinitionError(f"{source}: {error}")
    return checked


def _spawns(value: Any, source: str) -> list[str] | None:
    """``"*"``, CSV or an array of agent names; ``None`` means "infer from ``toolsets``"."""
    if value is None:
        return None
    if isinstance(value, str):
        names = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        names = [str(part).strip() for part in value]
    else:
        raise DefinitionError(f"{source}: spawns must be '*', a CSV string or an array")
    cleaned: list[str] = []
    for name in names:
        if not name or name in cleaned:
            continue
        cleaned.append(name)
    if "*" in cleaned:
        return ["*"]
    return cleaned


def _skill_names(value: Any, source: str) -> list[str]:
    """``autoloadSkills``: a CSV string or an array of skill names, in declaration order.

    Names are resolved against the parent session at spawn time; an unknown name is ignored
    (omp's contract), so only the shape is validated here. A slash (``category/skill``) and a
    plugin prefix (``plugin:skill``) are legal identifiers.
    """
    if isinstance(value, str):
        names = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        names = [str(part).strip() for part in value]
    else:
        raise DefinitionError(f"{source}: autoloadSkills must be a CSV string or an array")
    cleaned: list[str] = []
    for name in names:
        if not name:
            continue
        if "\x00" in name:
            raise DefinitionError(f"{source}: autoloadSkills entries must not contain NUL bytes")
        if name not in cleaned:
            cleaned.append(name)
    return cleaned


def _advisor(value: Any, source: str) -> bool | str:
    """``advisor``: ``true`` uses the roster/role, a string pins one model selector."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
        return True
    raise DefinitionError(f"{source}: advisor must be a boolean or a model selector string")


def parse_definition(text: str, *, source: str = "<definition>") -> tuple[dict[str, Any], str]:
    """``(metadata, body)`` for one definition file; unknown keys are dropped with one warning."""
    raw, body = parse_frontmatter(text)
    body = body.strip()
    if not raw:
        return {}, body
    normalized = {_FIELD_ALIASES.get(str(key), str(key)): value for key, value in raw.items()}
    unknown = sorted(key for key in normalized if key not in KNOWN_FIELDS)
    if unknown:
        logger.warning("agent definition %s: ignoring unknown field(s): %s",
                       source, ", ".join(unknown))
    metadata: dict[str, Any] = {}
    for key, value in normalized.items():
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
        elif key == "enable_tools":
            metadata[key] = _enable_tools(value, source)
        elif key == "max_iterations":
            metadata[key] = _max_iterations(value, source)
        elif key == "output":
            metadata[key] = normalize_output(value, source)
        elif key == "spawns":
            metadata[key] = _spawns(value, source)
        elif key == "blocking":
            metadata[key] = _bool(value, key, source)
        elif key == "autoloadSkills":
            metadata[key] = _skill_names(value, source)
        elif key == "advisor":
            metadata[key] = _advisor(value, source)
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


__all__ = ["KNOWN_FIELDS", "DefinitionError", "body_threats", "normalize_output",
           "parse_definition", "resolve"]
