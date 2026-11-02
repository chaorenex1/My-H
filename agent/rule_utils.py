"""Rule-file discovery shared by the ``rules`` tool and ``hermes rules``.

Rules are Markdown files (``.md`` / ``.mdc``) with optional YAML frontmatter. Discovery mirrors
named-agent definitions (``tools/agent_tool.py::_prompt_scopes``): project (explicit directory,
then cwd) -> profile (``HERMES_HOME``) -> installation, each contributing ``.agents/rules`` and
``rules`` plus skill-local and plugin-local ``rules/`` directories. The first rule for an identity
wins.

Identity is the path relative to its rules root (``python/coding-style``), not the bare filename:
the shipped corpus repeats names like ``coding-style.md`` in every language directory.

Frontmatter is oh-my-pi (omp) rule compatible. ``condition`` / ``astCondition`` / ``question`` /
``scope`` / ``globs`` / ``interruptMode`` / ``alwaysApply`` / ``agents`` are parsed verbatim; the
Hermes corpus's ``paths`` is accepted as an alias for ``globs``, and a rule with neither ``paths``
nor ``globs`` is an always-on candidate.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from ruamel.yaml.error import YAMLError

from agent.skill_utils import EXCLUDED_SKILL_DIRS, parse_frontmatter
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

RULE_SUFFIXES = (".md", ".mdc")
# A README at a rules root documents the directory; it is not a rule. Nested READMEs are
# legitimate rule bodies and stay discoverable.
_IGNORED_ROOT_FILES = frozenset({"readme.md", "readme.mdc"})
INTERRUPT_MODES = ("never", "prose-only", "tool-only", "always")

_LIST_FIELDS = ("condition", "astCondition", "scope", "globs", "paths", "agents")
_BOOL_FIELDS = ("alwaysApply", "enabled")
_STRING_FIELDS = ("description", "notice", "question", "interruptMode")


class RuleError(ValueError):
    """A rule the caller must refuse rather than guess around."""


def _as_list(value: Any, *, split_commas: bool = False) -> list[str]:
    """A scalar-or-list frontmatter value as a stripped string list."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in values:
        text = str(item).strip()
        if not text:
            continue
        if split_commas:
            out.extend(part.strip() for part in text.split(",") if part.strip())
        else:
            out.append(text)
    return out


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "always"}


def _get(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def normalize_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize omp rule frontmatter (and Hermes' ``paths`` alias) to one metadata dict."""
    globs = _as_list(_get(raw, "globs"), split_commas=True)
    paths = _as_list(_get(raw, "paths"), split_commas=True)
    if not globs:
        globs = paths
    return {
        "name": str(_get(raw, "name") or "").strip(),
        "description": str(_get(raw, "description") or "").strip(),
        "condition": _as_list(_get(raw, "condition", "ttsr_trigger", "ttsrTrigger")),
        "ast_condition": _as_list(_get(raw, "astCondition", "ast_condition")),
        "question": str(_get(raw, "question") or "").strip(),
        "scope": _as_list(_get(raw, "scope"), split_commas=True),
        "globs": globs,
        "paths": paths,
        "interrupt_mode": str(_get(raw, "interruptMode", "interrupt_mode") or "").strip(),
        "always_apply": _as_bool(_get(raw, "alwaysApply", "always_apply"), default=False),
        "agents": _as_list(_get(raw, "agents"), split_commas=True),
        "enabled": _as_bool(_get(raw, "enabled"), default=True),
        "raw": dict(raw),
    }


def parse_rule_text(text: str, *, source: str = "<rule>", rule_id: str = "") -> dict[str, Any]:
    """One rule file -> ``{rule_id, name, description, metadata, body, raw_frontmatter}``."""
    frontmatter, body = parse_frontmatter(text)
    metadata = normalize_metadata(frontmatter)
    return {
        "rule_id": rule_id,
        "name": metadata["name"] or rule_id,
        "description": metadata["description"],
        "metadata": metadata,
        "body": body.strip(),
        "source": source,
        "raw_frontmatter": frontmatter,
    }


# -- discovery ---------------------------------------------------------------------------


def _iter_rule_files(root: Path) -> Iterable[Path]:
    """Every rule file under *root*, sorted; VCS/cache directories are pruned by name."""
    if not root.is_dir():
        return
    try:
        paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in RULE_SUFFIXES)
    except OSError:
        return
    for path in paths:
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) == 1 and rel.name.casefold() in _IGNORED_ROOT_FILES:
            continue
        if any(part in EXCLUDED_SKILL_DIRS for part in rel.parts[:-1]):
            continue
        yield path


def _skill_rule_dirs(skills_root: Path, skill_name: str = "") -> list[Path]:
    """``<skills>/<name>/rules`` for the named skill first, then every other skill."""
    try:
        children = sorted(p for p in skills_root.iterdir()
                          if p.is_dir() and not p.name.startswith((".", "_")))
    except OSError:
        return []
    if skill_name:
        children.sort(key=lambda p: p.name != skill_name)
    return [p / "rules" for p in children]


def _plugin_rule_dirs(plugins_root: Path) -> list[Path]:
    try:
        children = sorted(plugins_root.iterdir())
    except OSError:
        return []
    return [p / "rules" for p in children if p.is_dir() and not p.name.startswith((".", "_"))]


def _base_rule_dirs(base: Path, skill_name: str, plugins_rel: str) -> list[Path]:
    return ([base / ".agents" / "rules", base / "rules"]
            + _skill_rule_dirs(base / "skills", skill_name)
            + _plugin_rule_dirs(base / plugins_rel))


def _install_roots() -> tuple[Path, Path, Path]:
    from hermes_cli.plugins import get_bundled_plugins_dir
    from hermes_constants import get_bundled_skills_dir
    from pm.paths import install_root

    root = install_root()
    return root, get_bundled_skills_dir(root / "skills"), get_bundled_plugins_dir()


def rule_search_roots(project_dir: Optional[str | Path] = None,
                      *, skill_name: str = "") -> list[tuple[str, Path]]:
    """``(trust scope, directory)`` in scan order, deduplicated (project -> profile -> install)."""
    project = Path(project_dir).expanduser() if project_dir else None
    scopes: list[tuple[str, Path]] = []
    for base in [base for base in (project, Path.cwd()) if base is not None]:
        scopes += [("project", d) for d in _base_rule_dirs(base, skill_name, ".hermes/plugins")]
    home = get_hermes_home()
    scopes += [("profile", d) for d in _base_rule_dirs(home, skill_name, "plugins")]
    root, bundled_skills, bundled_plugins = _install_roots()
    scopes += [("installation", root / ".agents" / "rules"), ("installation", root / "rules")]
    scopes += [("installation", d) for d in _skill_rule_dirs(bundled_skills, skill_name)]
    scopes += [("installation", d) for d in _plugin_rule_dirs(bundled_plugins)]

    ordered: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for scope, directory in scopes:
        key = str(directory).casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append((scope, directory))
    return ordered


def _agent_matches(agent_name: str, patterns: list[str]) -> bool:
    """Case-insensitive glob match of an agent name against a rule's ``agents`` gate."""
    if not patterns:
        return True
    from fnmatch import fnmatch

    name = agent_name.casefold()
    return any(fnmatch(name, pattern.casefold()) for pattern in patterns)


def discover_rules(project_dir: Optional[str | Path] = None, *,
                   skill_name: str = "", agent_name: str = "") -> list[dict[str, Any]]:
    """Every visible rule in scope order; the first rule for an identity wins."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scope, root in rule_search_roots(project_dir, skill_name=skill_name):
        for path in _iter_rule_files(root):
            try:
                rule_id = path.relative_to(root).with_suffix("").as_posix()
            except ValueError:
                continue
            if not rule_id or rule_id in seen:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                logger.warning("rule: cannot read %s: %s", path, exc)
                continue
            row = parse_rule_text(text, source=str(path), rule_id=rule_id)
            if not row["metadata"]["enabled"]:
                continue
            if agent_name and not _agent_matches(agent_name, row["metadata"]["agents"]):
                continue
            row["scope"] = scope
            row["root"] = str(root)
            seen.add(rule_id)
            found.append(row)
    return found


def rule_summary(rule: dict[str, Any], *, include_body: bool = False) -> dict[str, Any]:
    """The one row shape ``rules_list`` / ``hermes rules list`` return."""
    meta = rule["metadata"]
    row = {
        "rule_id": rule["rule_id"],
        "name": rule["name"],
        "description": rule["description"],
        "scope": rule.get("scope", ""),
        "rule_scope": meta["scope"],
        "source": rule["source"],
        "globs": meta["globs"],
        "always_apply": meta["always_apply"],
        "interrupt_mode": meta["interrupt_mode"],
        "condition": meta["condition"],
        "ast_condition": meta["ast_condition"],
        "question": meta["question"],
        "agents": meta["agents"],
    }
    if include_body:
        row["body"] = rule["body"]
        row["frontmatter"] = rule["raw_frontmatter"]
    return row


def find_rule(identifier: str, *, rules: Optional[list[dict[str, Any]]] = None,
              project_dir: Optional[str | Path] = None) -> Optional[dict[str, Any]]:
    """Resolve an exact id first, then a unique frontmatter ``name``; ambiguous names raise."""
    catalog = rules if rules is not None else discover_rules(project_dir)
    wanted = str(identifier or "").strip()
    if not wanted:
        return None
    exact = [r for r in catalog
             if r["rule_id"] == wanted or r["rule_id"].casefold() == wanted.casefold()]
    if len(exact) == 1:
        return exact[0]
    named = [r for r in catalog if r["name"].casefold() == wanted.casefold()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        ids = ", ".join(sorted(r["rule_id"] for r in named))
        raise RuleError(f"Ambiguous rule name {wanted!r}; use one of: {ids}")
    return None


# -- validation / writing ----------------------------------------------------------------


def validate_rule_id(value: str) -> str:
    """A relative, suffix-free rule id (``python/coding-style``); refuses escapes and drive paths."""
    text = str(value or "").strip().replace("\\", "/")
    for suffix in RULE_SUFFIXES:
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)]
    if not text or text.startswith("/") or ":" in text or text.endswith("/"):
        raise RuleError(f"Invalid rule id {value!r}: use a relative path without a drive or leading slash")
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise RuleError(f"Invalid rule id {value!r}: empty or traversal path segment")
    if any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts):
        raise RuleError(f"Invalid rule id {value!r}: only letters, digits, '.', '_', '-' and '/' are allowed")
    return text


def managed_rule_root() -> Path:
    """The profile-owned writable root: ``HERMES_HOME/.agents/rules``."""
    return get_hermes_home() / ".agents" / "rules"


def rule_file_path(rule_id: str) -> Path:
    root = managed_rule_root().resolve()
    path = (root / f"{validate_rule_id(rule_id)}.md").resolve()
    if not path.is_relative_to(root):
        raise RuleError(f"Rule id {rule_id!r} escapes {root}")
    return path


def _yaml_dump(data: dict[str, Any]) -> str:
    from hermes_yaml import safe_dump

    return safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True).rstrip()


def _fallback_dump(data: dict[str, Any]) -> str:
    """Minimal omp-shaped emitter when no YAML emitter is importable."""
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (list, tuple)):
            if not value:
                continue
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        elif value is None or value == "":
            continue
        else:
            text = str(value)
            if any(ch in text for ch in ":#{}[]&*!|>'\"%@`") or text != text.strip():
                text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
            lines.append(f"{key}: {text}")
    return "\n".join(lines)


def format_rule(metadata: dict[str, Any], body: str) -> str:
    """Serialize normalized metadata + body as an omp-compatible rule file."""
    meta = normalize_metadata(metadata)
    data: dict[str, Any] = {}
    if meta["description"]:
        data["description"] = meta["description"]
    if meta["condition"]:
        data["condition"] = meta["condition"][0] if len(meta["condition"]) == 1 else list(meta["condition"])
    if meta["ast_condition"]:
        data["astCondition"] = (meta["ast_condition"][0] if len(meta["ast_condition"]) == 1
                                else list(meta["ast_condition"]))
    if meta["question"]:
        data["question"] = meta["question"]
    if meta["scope"]:
        data["scope"] = list(meta["scope"])
    if meta["globs"]:
        data["globs"] = list(meta["globs"])
    if meta["interrupt_mode"]:
        data["interruptMode"] = meta["interrupt_mode"]
    if meta["always_apply"]:
        data["alwaysApply"] = True
    if meta["agents"]:
        data["agents"] = list(meta["agents"])
    if not meta["enabled"]:
        data["enabled"] = False
    try:
        dumped = _yaml_dump(data)
    except (ImportError, YAMLError, TypeError, ValueError) as exc:
        logger.warning("rule: YAML formatter unavailable (%s); using fallback frontmatter", exc)
        dumped = _fallback_dump(data)
    head = f"---\n{dumped}\n---\n\n" if dumped else ""
    return head + body.strip() + "\n"


def write_rule(rule_id: str, text: str, *, overwrite: bool = False) -> Path:
    from utils import atomic_write_text

    path = rule_file_path(rule_id)
    if path.exists() and not overwrite:
        raise RuleError(f"Rule already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)
    return path


def delete_rule(rule_id: str) -> bool:
    path = rule_file_path(rule_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


def check_rules(rules: Optional[list[dict[str, Any]]] = None) -> list[dict[str, str]]:
    """Static issues worth surfacing. ``severity`` is ``error`` (unusable) or ``warning``.

    A missing description is a warning: omp treats it as recommended, not required, and the
    shipped language corpus predates it.
    """
    problems: list[dict[str, str]] = []
    for rule in rules if rules is not None else discover_rules():
        rule_id = rule["rule_id"]
        meta = rule["metadata"]
        if not meta["description"]:
            problems.append({"rule_id": rule_id, "severity": "warning",
                             "issue": "missing description"})
        if not rule["body"]:
            problems.append({"rule_id": rule_id, "severity": "error", "issue": "empty body"})
        for pattern in meta["condition"]:
            try:
                re.compile(pattern)
            except re.error as exc:
                problems.append({"rule_id": rule_id, "severity": "error",
                                 "issue": f"invalid condition regex: {exc}"})
        if meta["interrupt_mode"] and meta["interrupt_mode"] not in INTERRUPT_MODES:
            problems.append({"rule_id": rule_id, "severity": "warning",
                             "issue": f"unknown interruptMode {meta['interrupt_mode']!r}"})
    return problems
