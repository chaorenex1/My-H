"""Immediate Semgrep scan for a write (ADR-9 D8).

Semgrep is a diagnostic source beside LSP: ``write_file``/``patch`` start it on the bytes they just
wrote, and it shares their latency budget.  A scan that answers inside the budget goes into the tool
result; a slower one is queued in ``agent.lsp.deferred`` under the same ``(owner, path)`` as the LSP
verdict and reaches the model/UI at the next delivery point.

The scan is ``hermes_codegraph.analysis.semgrep_scan``: registry or profile rules, ``--metrics=off``,
no repository code executed, no files written.  It runs on a daemon thread because the write path
must never block on it; ``code_intel.semgrep.enabled`` stays the master switch and
``lsp.display.semgrep.mode`` only decides whether the write path scans at all.

Findings become LSP-shaped diagnostics (1-based ``range`` in, 1-based line/column out) so the one
reporter renders both sources in the same ``details.files`` line format.  ``source`` is fixed to
``semgrep`` and ``code`` carries the rule id.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from hermes_codegraph.analysis import IMMEDIATE_TIMEOUT_S as SEMGREP_TIMEOUT_S

logger = logging.getLogger("agent.lsp.semgrep")

# A cold ``--config auto`` scan may fetch rules; the write only waits for ``inline_wait`` of it and
# the rest is late, so SEMGREP_TIMEOUT_S bounds how long a daemon thread may live.
SEVERITY_IDS = {"error": 1, "warning": 2, "info": 3, "hint": 4}

# One scan per write, bounded here: a fast batch of edits must not spawn a dozen concurrent Semgrep
# processes. A queued scan still resolves its own future, so the verdict is merely later; a stale
# token drops it at post time (ADR-9 D8).
_MAX_CONCURRENT_SCANS = 2
_SCAN_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_SCANS)


def limits():
    """The code-intel limits, or None when the master switch or the Semgrep layer is off."""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_codegraph.config import CodeGraphLimits

        resolved = CodeGraphLimits.from_config(load_config_readonly())
    except Exception:  # health: allow BLE001 -- optional subsystem; config errors never fail a write
        logger.debug("code_intel limits unavailable for the immediate Semgrep scan", exc_info=True)
        return None
    if not resolved.enabled or not resolved.semgrep_enabled:
        return None
    return resolved


def start_scan(abs_path: str, digest: Optional[str] = None) -> Optional[concurrent.futures.Future]:
    """Scan ``abs_path`` (the bytes just written) on a daemon thread; None when Semgrep is off.

    The scan root is the file's directory and the argument is the bare file name, so findings come
    back relative to that root and resolve to the exact absolute path ``files[].path`` carries.
    ``digest`` is the sha256 of the bytes this write persisted: the scan (and the verdict) is only
    about those bytes, so a file another session rewrote in between is never reported as this edit's
    finding (ADR-9 D8).
    """
    resolved = limits()
    if resolved is None:
        return None
    return _run(Path(abs_path).parent, Path(abs_path).name, resolved.semgrep_mode, digest)


def bytes_match(abs_path: str, digest: Optional[str]) -> bool:
    """True when ``abs_path`` still holds the bytes ``digest`` (no digest = no identity to check)."""
    if not digest:
        return True
    hasher = hashlib.sha256()
    try:
        with open(abs_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError:
        return False
    return hasher.hexdigest() == digest


def _run(root: Path, name: str, mode: str, digest: Optional[str]) -> concurrent.futures.Future:
    future: concurrent.futures.Future = concurrent.futures.Future()

    def _scan() -> None:
        if not future.set_running_or_notify_cancel():
            return
        with _SCAN_SLOTS:
            if not bytes_match(str(root / name), digest):
                result: dict[str, Any] = {"status": "stale", "findings": []}
            else:
                try:
                    from hermes_constants import get_hermes_home
                    from hermes_codegraph.analysis import semgrep_scan

                    result = semgrep_scan(root, [name], mode=mode, home=get_hermes_home(),
                                          timeout=SEMGREP_TIMEOUT_S)
                except Exception:  # health: allow BLE001 -- Semgrep must never fail a write
                    logger.debug("immediate Semgrep scan failed for %s", root / name, exc_info=True)
                    result = {"status": "failed", "findings": []}
        try:
            future.set_result(result)
        except Exception:  # the waiter cancelled; the verdict is simply not delivered
            logger.debug("immediate Semgrep verdict was cancelled", exc_info=True)

    from hermes_threading import spawn_context_thread

    spawn_context_thread(_scan, name="hermes-semgrep").start()
    return future


def diagnostics_from_scan(abs_path: str, result: Any, *,
                           severities: frozenset = frozenset({1, 2})) -> list[dict[str, Any]]:
    """The findings of ``result`` for ``abs_path``, as LSP-shaped diagnostics (empty on any failure).

    A finding for another path (a directory argument that expanded, a stale cwd) is dropped rather
    than rendered under this file: the display line's path must equal ``files[].path`` verbatim.
    """
    if not isinstance(result, dict) or result.get("status") != "ok":
        return []
    root = os.path.dirname(abs_path)
    diagnostics: list[dict[str, Any]] = []
    for finding in result.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        severity = SEVERITY_IDS.get(str(finding.get("severity") or "").lower())
        if severity is None or severity not in severities:
            continue
        if not _same_file(root, str(finding.get("path") or ""), abs_path):
            continue
        line = _positive_int(finding.get("start_line"))
        column = _positive_int(finding.get("start_column"))
        end_line = _positive_int(finding.get("end_line")) or line
        end_column = _positive_int(finding.get("end_column")) or column
        diagnostics.append({
            # LSP's range is 0-based; reporter adds 1 back, so Semgrep's 1-based values round-trip.
            "range": {"start": {"line": line - 1, "character": column - 1},
                      "end": {"line": end_line - 1, "character": end_column - 1}},
            "severity": severity,
            "message": finding.get("message") or "",
            "source": "semgrep",
            "code": finding.get("rule_id") or "",
        })
    return diagnostics


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _same_file(root: str, reported: str, abs_path: str) -> bool:
    if not reported:
        return False
    candidate = reported if os.path.isabs(reported) else os.path.join(root, reported)
    return os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(abs_path)


__all__ = ["SEMGREP_TIMEOUT_S", "SEVERITY_IDS", "diagnostics_from_scan", "limits", "start_scan"]
