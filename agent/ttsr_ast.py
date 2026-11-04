"""ast-grep CLI bridge for TTSR ``astCondition`` rules (ADR-6 P2).

The matcher runs against one in-memory tool digest, so the bridge always feeds the source through
``--stdin`` and never writes a temporary file or scans the workspace. ast-grep is an optional PM
package (``hermes pm install ast-grep``); when it is absent the caller advertises the AST-only rules
as unsupported instead of silently skipping them.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: One process per pattern; keep the whole bridge bounded even on a pathological grammar.
AST_GREP_TIMEOUT_S = 2.0

#: Extensions ast-grep can parse, mapped to its language name. Unknown extensions (``.md``,
#: ``.txt``, SFCs, ...) return ``None``: they are not source streams for AST matching, and must
#: not spawn a process just to fail to parse.
_LANG_BY_EXT = {
    "ts": "ts",
    "mts": "ts",
    "cts": "ts",
    "tsx": "tsx",
    "js": "js",
    "mjs": "js",
    "cjs": "js",
    "jsx": "jsx",
    "py": "python",
    "pyi": "python",
    "rs": "rust",
    "go": "go",
    "java": "java",
    "c": "c",
    "h": "c",
    "cc": "cpp",
    "cpp": "cpp",
    "cxx": "cpp",
    "hpp": "cpp",
    "hh": "cpp",
    "hxx": "cpp",
    "cs": "csharp",
    "rb": "ruby",
    "php": "php",
    "kt": "kotlin",
    "kts": "kotlin",
    "swift": "swift",
    "scala": "scala",
    "sc": "scala",
    "lua": "lua",
    "sol": "solidity",
    "ex": "elixir",
    "exs": "elixir",
    "hs": "haskell",
    "lhs": "haskell",
    "sh": "bash",
    "zsh": "bash",
    "bash": "bash",
    "yml": "yaml",
    "yaml": "yaml",
    "json": "json",
    "html": "html",
    "htm": "html",
    "css": "css",
}


def resolve_ast_grep() -> str | None:
    """The PM-managed ``ast-grep`` executable, else one on PATH; never installs anything.

    Resolution goes through ``hermes_platform`` with PM's activation PATH so an optional package
    installed into the Hermes store is visible without mutating this process's environment.
    """
    env_path = None
    try:
        from pm.install import env_for

        env = env_for("ast-grep")
        env_path = env.get("PATH") if isinstance(env, dict) else None
    except Exception:  # health: allow BLE001 -- optional backend must not fail a turn
        logger.debug("ttsr: ast-grep PM lookup failed", exc_info=True)
    try:
        from hermes_platform.resolver import LookupContext, locate_command
    except Exception:  # health: allow BLE001 -- imported lazily for test isolation
        return None
    try:
        resolution = locate_command(
            "ast-grep", LookupContext(path=env_path) if env_path else None)
    except Exception:  # health: allow BLE001 -- optional backend must not fail a turn
        logger.debug("ttsr: ast-grep resolution failed", exc_info=True)
        return None
    return resolution.command[0] if resolution.command else None


def language_for_path(path: str) -> str | None:
    """omp derives the ast-grep language from the tool path's extension (``rule.ts`` -> ``ts``)."""
    suffix = Path(str(path or "")).suffix
    if not suffix or suffix == ".":
        return None
    return _LANG_BY_EXT.get(suffix[1:].lower())


def ast_match(
    executable: str,
    pattern: str,
    source: str,
    lang: str,
    *,
    timeout: float = AST_GREP_TIMEOUT_S,
) -> bool:
    """True when ``pattern`` matches ``source``; backend/parse errors are a non-match.

    omp uses ``AstMatchStrictness.Smart`` and only needs to know whether at least one node matched,
    so the bridge asks only for matching paths and never rewrites or scans files.
    """
    pattern = str(pattern or "").strip()
    if not executable or not pattern or not lang or not source:
        return False
    command = [
        executable, "run",
        "--pattern", pattern,
        "--lang", lang,
        "--strictness", "smart",
        "--stdin",
        "--files-with-matches",
        "--color", "never",
        "--threads", "1",
    ]
    try:
        completed = subprocess.run(
            command,
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("ttsr: ast-grep invocation failed: %s", exc)
        return False
    # 0 = matches, 1 = no match; 8 and other codes are pattern/language/backend errors.
    if completed.returncode not in (0, 1):
        logger.debug(
            "ttsr: ast-grep exit %s (lang=%s): %s",
            completed.returncode, lang, (completed.stderr or "").strip()[-300:])
        return False
    return completed.returncode == 0 and bool((completed.stdout or "").strip())
