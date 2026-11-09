#!/usr/bin/env python3
"""ADR-4 D14: background eval jobs, streaming and cancellation.

These run real local Python kernels (no Bun needed). The job thread must inherit the
calling turn's context, stream cell output to the tool card, settle its result exactly
once and die with the session.
"""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import patch

import pytest

from agent.tool_progress_relay import bound_tool_events
from tools.code_execution_tool import execute_code
from tools.code_kernel import shutdown_all_kernels
from tools.eval_jobs import (
    cancel_eval_job,
    clear_session_jobs,
    get_eval_job,
    shutdown_all_eval_jobs,
    wait_eval_job,
)

pytestmark = pytest.mark.usefixtures("_local_eval_config")


@pytest.fixture
def _local_eval_config():
    shutdown_all_kernels()
    shutdown_all_eval_jobs()
    with patch("tools.code_execution_tool._load_config",
               return_value={"mode": "strict", "timeout": 30}):
        yield
    shutdown_all_eval_jobs()
    shutdown_all_kernels()


def _events() -> tuple[list[tuple[str, dict]], object]:
    events: list[tuple[str, dict]] = []
    lock = threading.Lock()

    def callback(event_type, name=None, preview=None, args=None, **kwargs):
        with lock:
            events.append((str(event_type), dict(kwargs)))

    return events, callback


def _wait_for(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached before the deadline")


def test_background_job_streams_marks_the_job_and_settles_once():
    events, callback = _events()
    with bound_tool_events(callback, "execute_code", "call-async"):
        body = json.loads(execute_code(
            "print('job-start')\nimport time\ntime.sleep(0.2)\nprint('job-done')",
            task_id="job-test", background=True, tool_call_id="call-async",
        ))
        assert body["status"] == "running"
        job_id = body["async"]["jobId"]
        assert body["async"] == {"state": "running", "jobId": job_id, "type": "eval"}
        snapshot = wait_eval_job(job_id, timeout=30)

    assert snapshot is not None
    assert snapshot["status"] == "completed"
    result = snapshot["result"]
    assert result["status"] == "success", result
    assert "job-start" in result["output"] and "job-done" in result["output"]
    assert result["async"] == {"state": "completed", "jobId": job_id, "type": "eval"}

    outputs = [kwargs for event, kwargs in events if event == "tool.output"]
    assert any("job-start" in item.get("text", "") for item in outputs)
    assert any("job-done" in item.get("text", "") for item in outputs)
    assert {item["cell_index"] for item in outputs} == {0}
    completions = [kwargs for event, kwargs in events if event == "tool.complete"]
    assert len(completions) == 1
    assert completions[0]["job_id"] == job_id
    assert completions[0]["tool_call_id"] == "call-async"


def test_cancel_stops_the_cell_and_settles_as_cancelled():
    events, callback = _events()
    with bound_tool_events(callback, "execute_code", "call-cancel"):
        body = json.loads(execute_code(
            "print('before-sleep')\nimport time\ntime.sleep(30)",
            task_id="job-cancel", background=True, tool_call_id="call-cancel",
        ))
        job_id = body["async"]["jobId"]
        _wait_for(lambda: any("before-sleep" in kw.get("text", "")
                              for event, kw in events if event == "tool.output"))
        assert cancel_eval_job(job_id) is True
        snapshot = wait_eval_job(job_id, timeout=30)

    assert snapshot is not None
    assert snapshot["status"] == "cancelled"
    assert snapshot["result"]["async"]["state"] == "cancelled"
    # Cancellation does not fabricate a successful cell result.
    assert snapshot["result"]["status"] in {"interrupted", "timeout", "error"}


def test_background_timeout_settles_as_failed_not_completed():
    body = json.loads(execute_code(
        "import time\ntime.sleep(30)", task_id="job-timeout", background=True, timeout=0.5,
    ))
    job_id = body["async"]["jobId"]
    snapshot = wait_eval_job(job_id, timeout=30)

    assert snapshot is not None
    assert snapshot["status"] == "failed"
    assert snapshot["result"]["status"] == "timeout"
    assert snapshot["result"]["async"]["state"] == "failed"


def test_clear_session_cancels_and_forgets_the_jobs():
    body = json.loads(execute_code(
        "import time\ntime.sleep(30)", task_id="job-clear", background=True,
    ))
    job_id = body["async"]["jobId"]
    _wait_for(lambda: (get_eval_job(job_id) or {}).get("status") == "running")
    clear_session_jobs("job-clear")
    snapshot = get_eval_job(job_id)
    assert snapshot is None
