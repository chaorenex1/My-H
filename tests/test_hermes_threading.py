"""hermes_threading: background work keeps the submitter's profile scope.

Profile isolation lives in ContextVars. Both entry points must carry the context of whoever
dispatched the work — a pooled worker is reused across profiles, so a context captured at
worker start (or not at all) would leak the launch profile into another profile's job.
"""
from __future__ import annotations

import contextvars
import threading

import pytest

import hermes_threading


@pytest.fixture(autouse=True)
def _fresh_pool():
    yield
    hermes_threading.shutdown_context_pool(wait=True)


def test_spawn_context_thread_is_unstarted_and_inherits_the_callers_context():
    scope = contextvars.ContextVar("scope", default="launch")
    seen: list[str] = []
    token = scope.set("profile-b")
    try:
        thread = hermes_threading.spawn_context_thread(lambda: seen.append(scope.get()), name="probe")
        assert thread.name == "probe"
        assert thread.daemon
        assert not thread.is_alive()  # returns before start() so callers can register it first
        thread.start()
        thread.join(5)
    finally:
        scope.reset(token)

    assert seen == ["profile-b"]


def test_submit_context_copies_context_per_submission_not_per_worker():
    scope = contextvars.ContextVar("scope", default="launch")
    first = scope.set("profile-a")
    try:
        assert hermes_threading.submit_context(scope.get, name="job-a").result(5) == "profile-a"
    finally:
        scope.reset(first)

    second = scope.set("profile-b")
    try:
        # Sequential submissions reuse the same idle pool worker; the snapshot is per submit.
        assert hermes_threading.submit_context(scope.get, name="job-b").result(5) == "profile-b"
    finally:
        scope.reset(second)


def test_submit_context_names_the_worker_for_the_job_then_restores_it():
    observed: dict[str, object] = {}
    done = threading.Event()

    def job() -> None:
        observed["during"] = threading.current_thread().name
        observed["worker"] = threading.current_thread()
        done.set()

    hermes_threading.submit_context(job, name="named-job").result(5)

    worker = observed["worker"]
    assert isinstance(worker, threading.Thread)
    assert done.is_set()
    assert observed["during"] == "named-job"
    assert worker.name.startswith("hermes-ctx")


def test_submit_context_reports_a_failed_job_through_the_future():
    def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        hermes_threading.submit_context(boom, name="boom").result(5)
