"""Rule list for section 07 under ``§ Runtime``.

Loading rules (ADR-18 D2):
* built-in rules (the installation's ``rules/`` tree) load first and in full — they are
  never replaced by a profile rule with the same id;
* ``$HERMES_HOME/rules`` rules load additionally, but only when one of their languages
  (``globs``/``paths`` suffixes, else the ``<lang>-`` id prefix) is a language the
  current project actually uses;
* not a programming project -> no rule list at all, so the section is not rendered.

Project languages are detected with ripgrep: a suffix counts only when more than one file
in the project has it (``rg --files``, so ignore rules still apply).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from agent.rule_utils import parse_rule_text
from hermes_constants import get_hermes_home

_RULE_SUFFIXES = (".md", ".mdc")
_DOC_NAMES = frozenset({"readme.md", "readme.mdc", "license", "license.md", "notice", "notice.md"})
_RG_TIMEOUT_S = 10
_EXTENSION_RE = re.compile(r"\.([A-Za-z0-9_+-]+)")

# Suffix -> language name used by rule ids (``go-``, ``rs-``, ``ts-`` in the built-in corpus).
LANGUAGE_BY_EXTENSION = {
    ".go": "go",
    ".rs": "rs",
    ".ts": "ts", ".tsx": "ts", ".mts": "ts", ".cts": "ts",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".py": "py", ".pyi": "py",
    ".java": "java", ".kt": "kt", ".kts": "kt", ".scala": "scala",
    ".swift": "swift", ".m": "objc", ".mm": "objc",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cs": "cs", ".rb": "rb", ".php": "php", ".sh": "sh", ".bash": "sh", ".zsh": "sh",
    ".sql": "sql", ".lua": "lua", ".dart": "dart", ".ex": "elixir", ".exs": "elixir",
}
_LANGUAGE_ALIASES = {
    "rust": "rs", "typescript": "ts", "javascript": "js", "python": "py", "golang": "go",
}
_LANGUAGES = frozenset(LANGUAGE_BY_EXTENSION.values())


def _rg_binary() -> str | None:
    """PM-managed/path ripgrep, else PATH; never installs anything.

    ``resolve_rg`` asks PM for the pinned ripgrep's environment first, which is where a
    stock install keeps it (the tool store is not on PATH).
    """
    try:
        from agent.judgment.find import resolve_rg

        found = resolve_rg()
        if found:
            return found
    except Exception:  # health: allow BLE001 -- language detection degrades to "not a project"
        pass
    return shutil.which("rg")


def project_languages(project_dir: Path | str) -> set[str]:
    """Languages with more than one file under *project_dir* (``rg --files`` suffixes)."""
    rg = _rg_binary()
    if not rg:
        return set()
    try:
        proc = subprocess.run(
            [rg, "--files", "--no-messages"], cwd=str(project_dir), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=_RG_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode not in (0, 1):
        return set()
    counts = Counter(Path(line).suffix.lower() for line in proc.stdout.splitlines() if line.strip())
    return {
        LANGUAGE_BY_EXTENSION[suffix]
        for suffix, count in counts.items()
        if count > 1 and suffix in LANGUAGE_BY_EXTENSION
    }


def builtin_rules_dir() -> Path:
    from pm.paths import install_root

    return install_root() / "rules"


def profile_rules_dir() -> Path:
    return get_hermes_home() / "rules"


def _iter_rules(root: Path) -> Iterable[dict[str, Any]]:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _RULE_SUFFIXES:
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if relative.name.casefold() in _DOC_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        yield parse_rule_text(text, source=str(path), rule_id=relative.with_suffix("").as_posix())


def _rule_languages(rule: dict[str, Any]) -> set[str]:
    metadata = rule["metadata"]
    languages: set[str] = set()
    for pattern in (*metadata["globs"], *metadata["paths"]):
        for match in _EXTENSION_RE.finditer(pattern):
            language = LANGUAGE_BY_EXTENSION.get("." + match.group(1).lower())
            if language:
                languages.add(language)
    if not languages:
        prefix = rule["rule_id"].split("/", 1)[0].split("-", 1)[0].casefold()
        prefix = _LANGUAGE_ALIASES.get(prefix, prefix)
        if prefix in _LANGUAGES:
            languages.add(prefix)
    return languages


def _row(rule: dict[str, Any], *, builtin: bool) -> str:
    name = rule["name"] or rule["rule_id"]
    description = rule["description"]
    location = f"rules/{rule['rule_id']}.md" if builtin else f"$HERMES_HOME/rules/{rule['rule_id']}.md"
    return f"{name}: {description} — {location}" if description else f"{name} — {location}"


def rule_rows(project_dir: Path | str | None = None) -> list[str] | None:
    """Rendered rule lines, or ``None`` when the project is not a programming project."""
    languages = project_languages(Path(project_dir) if project_dir is not None else Path.cwd())
    if not languages:
        return None
    rows: list[str] = []
    seen: set[str] = set()
    for rule in _iter_rules(builtin_rules_dir()):
        seen.add(rule["rule_id"])
        rows.append(_row(rule, builtin=True))
    for rule in _iter_rules(profile_rules_dir()):
        if rule["rule_id"] in seen:  # built-ins are never overridden
            continue
        rule_languages = _rule_languages(rule)
        if rule_languages & languages or (not rule_languages and rule["metadata"]["always_apply"]):
            rows.append(_row(rule, builtin=False))
    return rows


__all__ = [
    "LANGUAGE_BY_EXTENSION",
    "builtin_rules_dir",
    "profile_rules_dir",
    "project_languages",
    "rule_rows",
]
