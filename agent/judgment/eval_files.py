"""Eval judgment files under ``$HERMES_HOME/judgment/eval`` (ADR-11 D16).

``questions/`` and ``batches/`` are user/eval-owned and never rotated;
``logs/<session_id>/<execution_id>.jsonl`` (or ``_standalone/<request_id>.jsonl``)
gets one request-level line per ``purpose="eval"`` judgment. Lines carry hashes and
counts; the bodies are written only when ``judgment.eval_debug`` is on, and then
the state is already redacted. A 50 MiB default budget evicts the oldest log files,
never question or batch manifests.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from agent.judgment.cache import judgment_home

logger = logging.getLogger(__name__)

STANDALONE_DIR = "_standalone"
_MAX_NAME = 96
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def eval_home() -> Path:
    return judgment_home() / "eval"


def questions_dir() -> Path:
    return eval_home() / "questions"


def batches_dir() -> Path:
    return eval_home() / "batches"


def logs_dir() -> Path:
    return eval_home() / "logs"


def ensure_dirs() -> None:
    for directory in (questions_dir(), batches_dir(), logs_dir()):
        directory.mkdir(parents=True, exist_ok=True)


def _safe_name(value: str, fallback: str) -> str:
    cleaned = _UNSAFE.sub("_", value.strip()).strip("._-")[:_MAX_NAME]
    return cleaned or fallback


def log_path(run: Mapping[str, Any]) -> Path:
    """``logs/<session>/<execution>.jsonl``; standalone runs key on the request id."""
    session = _safe_name(str(run.get("session_id") or ""), "")
    if not session:
        session = STANDALONE_DIR
    name = (_safe_name(str(run.get("execution_id") or ""), "")
            or _safe_name(str(run.get("request_id") or ""), "run"))
    return logs_dir() / session / f"{name}.jsonl"


def _timestamp(epoch: Any) -> str:
    try:
        moment = datetime.fromtimestamp(int(epoch), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        moment = datetime.now(UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _line(run: Mapping[str, Any], *, debug_state: Any, debug_answers: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "ts": _timestamp(run.get("created_at")),
        "session_id": str(run.get("session_id") or ""),
        "execution_id": str(run.get("execution_id") or ""),
        "request_id": str(run.get("request_id") or ""),
        "purpose": str(run.get("purpose") or ""),
        "mode": str(run.get("mode") or ""),
        "api": str(run.get("api") or ""),
        "provider": str(run.get("provider") or ""),
        "model": str(run.get("model") or ""),
        "questions": int(run.get("questions") or 0),
        "cached": int(run.get("cached") or 0),
        "usage": {
            "input_tokens": int(run.get("input_tokens") or 0),
            "output_tokens": int(run.get("output_tokens") or 0),
            "cost_usd": float(run.get("cost_usd") or 0.0),
        },
        "latency_ms": int(run.get("latency_ms") or 0),
        "status": str(run.get("status") or ""),
        "error": str(run.get("error") or ""),
        "state_sha": str(run.get("state_sha") or ""),
        "answers_sha": str(run.get("answers_sha") or ""),
    }
    if debug_state is not None:
        line["state"] = debug_state
    if debug_answers is not None:
        line["answers"] = debug_answers
    return line


def record_run(
    run: Mapping[str, Any],
    *,
    max_bytes: int,
    debug_state: Any = None,
    debug_answers: Any = None,
) -> Path | None:
    """Append one eval log line and enforce ``max_bytes``; best-effort, never raises."""
    try:
        path = log_path(run)
        ensure_dirs()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            _line(run, debug_state=debug_state, debug_answers=debug_answers),
            ensure_ascii=False)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        rotate_logs(max_bytes)
        return path
    except (OSError, TypeError, ValueError) as exc:
        logger.debug("judgment eval log write failed (non-fatal): %s", exc)
        return None


def rotate_logs(max_bytes: int) -> None:
    """Evict the oldest files under ``logs/`` until the directory fits ``max_bytes``."""
    root = logs_dir()
    if not root.is_dir():
        return
    entries: list[tuple[int, Path, int]] = []
    total = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime_ns, path, stat.st_size))
        total += stat.st_size
    if total <= max_bytes:
        return
    entries.sort()
    for _mtime, path, size in entries:
        if total <= max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            logger.debug("judgment eval log rotation could not remove %s", path, exc_info=True)
            continue
        total -= size
    for directory in sorted(
            (item for item in root.rglob("*") if item.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
