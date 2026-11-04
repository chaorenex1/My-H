"""Optional analysis adapters: ast-grep structural search and Semgrep diagnostics.

Neither adapter creates graph facts. ast-grep returns reference expressions for
``code_graph_update``; Semgrep findings become ``CodeDiagnostic`` nodes only.
Both resolve their binaries through PM, never from the application venv.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0
# One-file scans on the immediate paths (a write's edit check, an explicit diagnostics query on a
# named path) share this bound: a cold ``--config auto`` scan may fetch rules, and the caller must
# never wait for it synchronously (ADR-9 D8).
IMMEDIATE_TIMEOUT_S = 15.0
MAX_FINDINGS = 2000


def _which(tool: str) -> str | None:
    try:
        from pm import install

        env = install.env_for(tool)
    except Exception:
        logger.debug("pm env lookup failed for %s", tool, exc_info=True)
        env = {}
    path = env.get("PATH") if isinstance(env, dict) else None
    if path:
        found = shutil.which(tool, path=path)
        if found:
            return found
    return shutil.which(tool)


def _install_hint(tool: str) -> str:
    return f"hermes pm install {tool}"


def structural_search(
    root: Path,
    pattern: str,
    *,
    kind: str | None = None,
    limit: int = 20,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    binary = _which("ast-grep") or _which("sg")
    if binary is None:
        return {
            "status": "unavailable",
            "pattern": pattern,
            "results": [],
            "install_hint": _install_hint("ast-grep"),
        }
    limit = max(1, min(int(limit), 200))
    command = [binary, "run", "--pattern", pattern, "--json=stream", "--color", "never"]
    if kind:
        command.extend(["--kind", kind])
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "pattern": pattern, "results": [], "error": str(exc)}
    results: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        results.append(_ast_grep_result(payload))
        if len(results) >= limit:
            break
    return {
        "status": "ok" if completed.returncode == 0 else "partial",
        "pattern": pattern,
        "results": results,
        "exit_code": completed.returncode,
    }


def _ast_grep_result(payload: dict[str, Any]) -> dict[str, Any]:
    file_path = payload.get("file") or ""
    rng = payload.get("range") or {}
    start = rng.get("start") or {}
    end = rng.get("end") or {}
    return {
        "path": file_path.replace("\\", "/"),
        "start_line": int(start.get("line") or 0) + 1,
        "start_column": int(start.get("column") or 0) + 1,
        "end_line": int(end.get("line") or 0) + 1,
        "end_column": int(end.get("column") or 0) + 1,
        "text": payload.get("text") or "",
        "language": payload.get("language") or "",
        "rule_id": payload.get("ruleId"),
    }


def semgrep_available() -> bool:
    return _which("semgrep") is not None


def semgrep_scan(
    root: Path,
    files: list[str],
    *,
    mode: str,
    home: Path,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Run Semgrep on ``files``; never executes repository code or writes files."""
    binary = _which("semgrep")
    if binary is None:
        return {
            "status": "unavailable",
            "findings": [],
            "install_hint": _install_hint("semgrep"),
        }
    if mode not in ("auto", "custom_dir"):
        return {"status": "skipped", "findings": [], "reason": f"invalid mode {mode!r}"}
    if mode == "custom_dir":
        config_dir = Path(home) / "code-intelligence" / "semgrep"
        if not config_dir.is_dir() or not any(config_dir.rglob("*.y*ml")):
            return {
                "status": "skipped",
                "findings": [],
                "reason": "custom_dir is missing or empty",
                "config_dir": str(config_dir),
            }
        config = str(config_dir)
    else:
        config = "auto"
    selected = [item for item in files if item][:500]
    if not selected:
        return {"status": "skipped", "findings": [], "reason": "no files"}
    command = [
        binary, "scan",
        "--config", config,
        "--json",
        "--metrics=off",
        "--disable-version-check",
        "--quiet",
        "--no-git-ignore",
        *selected,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "findings": [], "error": str(exc)}
    findings: list[dict[str, Any]] = []
    try:
        payload = json.loads(completed.stdout or "{}")
    except ValueError:
        payload = {}
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        start = item.get("start") or {}
        end = item.get("end") or {}
        extra = item.get("extra") or {}
        findings.append(
            {
                "path": str(item.get("path") or "").replace("\\", "/"),
                "rule_id": item.get("check_id") or "",
                "severity": str(extra.get("severity") or "warning").lower(),
                "message": str(extra.get("message") or "")[:2000],
                "start_line": int(start.get("line") or 1),
                "start_column": int(start.get("col") or 1),
                "end_line": int(end.get("line") or 1),
                "end_column": int(end.get("col") or 1),
            }
        )
        if len(findings) >= MAX_FINDINGS:
            break
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "mode": mode,
        "findings": findings,
        "exit_code": completed.returncode,
    }
