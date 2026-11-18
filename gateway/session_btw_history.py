"""``btw.history`` and the durable side of ``/btw`` answers.

A ``/btw`` side question is detached from the transcript by design, and its answer used to exist
only as a ``btw.complete`` event. The panel needs the answers after a reconnect, so each completed
answer is appended to ``<profile>/session/btw/<session>.jsonl`` (one JSON object per line). The store
is per profile and per session; it never enters the model's context or the prompt cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from gateway.session_busy_controls import authorize
from gateway.session_promoted import scoped
from hermes_state_runtime import RuntimeStoreError

logger = logging.getLogger(__name__)

_MAX_BYTES = 1_000_000
_KEEP_LINES = 500
_MAX_LIMIT = 200
_lock = threading.Lock()


def handlers(connection):
    return {"btw.history": partial(history, connection)}


def _store_path(session_id: str) -> Path:
    from hermes_constants import get_hermes_home

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:80] or "session"
    if safe == session_id and session_id not in {".", ".."}:
        name = session_id  # the wire id is already a safe filename
    else:
        name = f"{safe}-{hashlib.sha256(session_id.encode()).hexdigest()[:12]}"
    return Path(get_hermes_home()) / "session" / "btw" / f"{name}.jsonl"


def record(session_id: str, task_id: str, question: str, answer: str) -> None:
    """Append one answer; best-effort (a storage failure must not fail the side question)."""
    entry = {
        "id": hashlib.sha256(f"{session_id}:{task_id}:{answer}".encode()).hexdigest()[:16],
        "question": question,
        "answer": answer,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "task_id": task_id,
    }
    path = _store_path(session_id)
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if path.stat().st_size > _MAX_BYTES:
                lines = path.read_text(encoding="utf-8-sig").splitlines()[-_KEEP_LINES:]
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            logger.warning("btw.history: could not store an answer for %s", session_id, exc_info=True)


def _read(session_id: str, limit: int) -> list[dict]:
    path = _store_path(session_id)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    items = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            items.append(entry)
    return items


def _epoch_millis(created_at) -> int | None:
    """Epoch millis for the stored ISO ``created_at``; None when it does not parse."""
    if not isinstance(created_at, str) or not created_at:
        return None
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:  # the store writes UTC; a bare timestamp is read the same way
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _records(items: list[dict]) -> list[dict]:
    """The panel's rows, newest first (the store is oldest-first). Every stored line is a completed
    answer: ``record`` only appends after the side question settled, so ``status`` is fixed."""
    return [{
        "id": entry["id"],
        "question": str(entry.get("question") or ""),
        "answer": str(entry.get("answer") or ""),
        "created_at": entry.get("created_at") if isinstance(entry.get("created_at"), str) else None,
        "created_at_ms": _epoch_millis(entry.get("created_at")),
        "status": "completed",
        "task_id": entry.get("task_id") if isinstance(entry.get("task_id"), str) else None,
    } for entry in reversed(items)]


async def history(connection, ref, params):
    if not isinstance(ref.session_id, str) or not ref.session_id:
        raise RuntimeStoreError("invalid_params", field="session_id")
    limit = params.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise RuntimeStoreError("invalid_params", field="limit")
    limit = min(limit or 50, _MAX_LIMIT)
    if ref.session_id not in connection.authority.sessions:
        raise RuntimeStoreError("not_found")
    authorize(connection, ref, params, "session:read")
    items = await scoped(connection, lambda: _read(ref.session_id, limit))
    return {"items": items, "records": _records(items)}
