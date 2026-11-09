"""Background eval jobs for ``execute_code(background=true)`` (ADR-4 D14).

One job runs one validated call spec on a daemon thread while the tool returns
``status="running"`` plus an ``async`` handle. The thread inherits the calling
turn's ContextVars and approval callbacks (``propagate_context_to_thread``), so
sandbox tool calls keep the same authority the synchronous path has.

Lifecycle:

* the job owns its result and settles exactly once (``_finalize`` under the
  record lock); a late final frame cannot overwrite a cancellation;
* ``cancel`` flips the record's interrupt bit, which ``_execute_spec``'s
  ``is_interrupted`` polls; the cell is killed by the same timeout/interrupt
  contract as a foreground call (no fabricated cell output);
* ``clear_session`` (``tools.approval.clear_session``), ``/new``, session close
  and atexit cancel and drop every job the owner holds.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Optional

from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

JOB_ID_PREFIX = "eval_"


def _resolve_owner(task_id: Optional[str]) -> str:
    from tools.code_kernel import _resolve_owner as resolve
    try:
        return resolve(task_id or "") or (task_id or "")
    except Exception:  # health: allow BLE001 -- identity resolution must not block the job
        return task_id or ""


def _running_result(spec: dict, job_id: str) -> dict[str, Any]:
    """The immediate ``execute_code`` result while the job runs (same additive key vocabulary)."""
    payload: dict[str, Any] = {
        "status": "running", "output": "", "tool_calls_made": 0, "duration_seconds": 0.0,
        "stdout_truncated": False, "stdout_bytes_captured": 0, "stdout_bytes_total": 0,
        "stdout_bytes_omitted": 0, "stdout_lines_total": 0, "stdout_lines_shown": 0,
        "async": {"state": "running", "jobId": job_id, "type": "eval"},
    }
    if spec.get("explicit_title") and spec.get("title") is not None:
        payload["title"] = spec["title"]
    if spec.get("explicit_language") and spec.get("single"):
        payload["language"] = spec["cells"][0]["language"]
    return payload


def _final_state(result: dict[str, Any], *, cancelled: bool) -> str:
    if cancelled:
        return "cancelled"
    if result.get("status") in ("timeout", "interrupted"):
        return "failed"
    return "failed" if result.get("status") == "error" and result.get("error") else "completed"


def _finalize(record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Settle the job exactly once and stamp its terminal ``async`` state into the result."""
    with record["lock"]:
        if record["settled"]:
            return record["result"]
        record["settled"] = True
        state = _final_state(result, cancelled=bool(record["cancelled"]))
        record["status"] = state
        result["async"] = {"state": state, "jobId": record["id"], "type": "eval"}
        record["result"] = result
        record["finished"] = time.monotonic()
    return result


def _is_interrupted(record: dict[str, Any]) -> bool:
    if record["cancelled"]:
        return True
    try:
        from tools.interrupt import is_interrupted
        return bool(is_interrupted())
    except Exception:  # health: allow BLE001 -- an unavailable interrupt bit is not an interrupt
        return False


def _run_job(record: dict[str, Any]) -> None:
    from tools.code_execution_tool import _execute_spec
    started = time.monotonic()
    try:
        raw, backend = _execute_spec(
            record["spec"], task_id=record["task_id"], enabled_tools=record["enabled_tools"],
            env_type=record["env_type"], reset=record["reset"],
            max_tool_calls=record["max_tool_calls"],
            is_interrupted=lambda: _is_interrupted(record),
            on_stream=record["on_stream"],
        )
        parsed: Any
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = {"status": "error", "output": str(raw),
                      "error": "execute_code produced an unreadable job result."}
        if not isinstance(parsed, dict):
            parsed = {"status": "error", "output": str(parsed),
                      "error": "execute_code produced an unreadable job result."}
        parsed.setdefault("duration_seconds", round(time.monotonic() - started, 2))
        parsed.setdefault("tool_calls_made", 0)
        parsed["backend"] = backend
        result = _finalize(record, parsed)
        is_error = result.get("status") != "success" or bool(
            (result.get("async") or {}).get("state") == "failed")
    except Exception as exc:  # health: allow BLE001 -- reported through the job record
        logger.exception("eval job %s failed", record["id"])
        result = _finalize(record, {"status": "error", "output": "", "error": str(exc),
                                    "duration_seconds": round(time.monotonic() - started, 2),
                                    "tool_calls_made": 0})
        is_error = True
    sink = record.get("sink")
    if sink is not None:
        sink.complete(result, is_error=is_error, job_id=record["id"],
                      duration_s=round(time.monotonic() - started, 2))
    record["done"].set()


def start_eval_job(*, spec: dict, task_id: Optional[str], enabled_tools: Optional[list[str]],
                   reset: bool, cfg: dict, env_type: str = "local",
                   max_tool_calls: int = 0, tool_call_id: Optional[str] = None,
                   sink: Any = None) -> str:
    """Start one background call and return its ``running`` handle JSON.

    Preflight/approval already ran on the calling thread (``execute_code``), so the
    job thread never has to prompt for a call the user did not see."""

    def _on_stream(frame) -> None:
        if sink is None or not isinstance(frame, dict) or frame.get("type") != "stream":
            return
        sink.stream(int(frame.get("cell_index") or 0), str(frame.get("stream") or "stdout"),
                    str(frame.get("text") or ""))

    from tools.code_execution_tool import DEFAULT_MAX_TOOL_CALLS, _positive_int
    record: dict[str, Any] = {
        "id": JOB_ID_PREFIX + uuid.uuid4().hex[:16],
        "owner": _resolve_owner(task_id),
        "task_id": task_id,
        "spec": spec,
        "enabled_tools": enabled_tools,
        "reset": bool(reset),
        "env_type": env_type,
        "max_tool_calls": max_tool_calls or _positive_int(
            cfg.get("max_tool_calls"), DEFAULT_MAX_TOOL_CALLS),
        "tool_call_id": tool_call_id or "",
        "sink": sink,
        "on_stream": _on_stream,
        "cancelled": False,
        "settled": False,
        "status": "running",
        "result": None,
        "started": time.monotonic(),
        "finished": None,
        "lock": threading.Lock(),
        "done": threading.Event(),
    }
    with _JOBS_LOCK:
        _JOBS[record["id"]] = record
    from agent.memory_provider import spawn_context_thread
    thread = spawn_context_thread(
        propagate_context_to_thread(_run_job), name=f"hermes-eval-job-{record['id'][-8:]}",
        args=(record,),
    )
    record["thread"] = thread
    thread.start()
    return json.dumps(_running_result(spec, record["id"]), ensure_ascii=False)


def get_eval_job(job_id: str) -> Optional[dict[str, Any]]:
    """A job's public snapshot, or ``None`` when unknown."""
    with _JOBS_LOCK:
        record = _JOBS.get(str(job_id or ""))
        if record is None:
            return None
        with record["lock"]:
            return {
                "jobId": record["id"], "status": record["status"],
                "state": record["status"], "result": record["result"],
                "cancelled": record["cancelled"],
                "started": record["started"], "finished": record["finished"],
            }


def cancel_eval_job(job_id: str) -> bool:
    """Ask a running job to stop. The cell's interrupt contract kills its kernel."""
    with _JOBS_LOCK:
        record = _JOBS.get(str(job_id or ""))
    if record is None:
        return False
    with record["lock"]:
        if record["settled"]:
            return False
        record["cancelled"] = True
        record["status"] = "cancelled"
    return True


def wait_eval_job(job_id: str, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Block until the job settles (or *timeout* expires) and return its snapshot."""
    with _JOBS_LOCK:
        record = _JOBS.get(str(job_id or ""))
    if record is None:
        return None
    record["done"].wait(timeout)
    return get_eval_job(job_id)


def clear_session_jobs(owner: str) -> None:
    """Cancel and forget every job a session owns (including delegated children)."""
    if not owner:
        return
    with _JOBS_LOCK:
        doomed = [job for job, record in _JOBS.items()
                  if record["owner"] == owner or record["owner"].startswith(owner + "::child::")]
        records = [_JOBS.pop(job) for job in doomed]
    for record in records:
        with record["lock"]:
            if not record["settled"]:
                record["cancelled"] = True
                record["status"] = "cancelled"


def clear_jobs_where(matcher: Callable[[str], bool]) -> None:
    """Cancel and forget jobs whose owner matches a predicate (delegated children)."""
    with _JOBS_LOCK:
        doomed = [job for job, record in _JOBS.items() if matcher(record["owner"])]
        records = [_JOBS.pop(job) for job in doomed]
    for record in records:
        with record["lock"]:
            if not record["settled"]:
                record["cancelled"] = True
                record["status"] = "cancelled"


def shutdown_all_eval_jobs() -> None:
    """Cancel every job (atexit and tests). The threads are daemons; cancellation kills kernels."""
    with _JOBS_LOCK:
        records = list(_JOBS.values())
        _JOBS.clear()
    for record in records:
        with record["lock"]:
            if not record["settled"]:
                record["cancelled"] = True
                record["status"] = "cancelled"


atexit.register(shutdown_all_eval_jobs)
