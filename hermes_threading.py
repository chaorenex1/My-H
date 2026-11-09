"""Context-bound thread helpers: the one place background work captures profile scope.

Profile isolation in Hermes is carried in ContextVars: a ``HERMES_HOME`` override plus the
per-turn secret scope. A bare ``threading.Thread`` or ``ThreadPoolExecutor`` worker starts with
an empty ``contextvars.Context``, so work dispatched from a secondary profile silently lands on
the launch profile's home (or fails closed with ``UnscopedSecretError``). Start every background
job through this module instead.

Two entry points, chosen by lifetime:

* :func:`spawn_context_thread` — a dedicated, unstarted daemon thread. Use it when the caller
  needs the Thread itself: register it in a gate, ``join()`` it at exit, name it in a thread
  registry, or let it block for the life of a session/worker loop.
* :func:`submit_context` — the shared daemon pool, returning a ``Future``. Use it for short,
  self-contained jobs. A bounded pool must never be the home of a job that can block forever
  (model calls, socket readers, provider drains): one wedged task would starve the queue.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ctx_bound",
    "shutdown_context_pool",
    "spawn_context_thread",
    "submit_context",
]

# Short, non-blocking background jobs only: enough concurrency for bursts without letting the
# pool hoard threads. Blocking work belongs on spawn_context_thread (see the module docstring).
_POOL_MAX_WORKERS = 8
_POOL: Optional[ThreadPoolExecutor] = None
_POOL_LOCK = threading.Lock()


def ctx_bound(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Bind ``fn`` to the CALLER's contextvars for another thread/executor. Profile isolation
    is a ContextVar-scoped HERMES_HOME override plus the per-turn secret scope; a worker started
    with an empty context silently lands on the default profile (or fails closed on secrets)."""
    ctx = contextvars.copy_context()
    return lambda *args, **kwargs: ctx.run(fn, *args, **kwargs)


def spawn_context_thread(target: Callable[..., Any], *, name: str, daemon: bool = True,
                         args: tuple = (), kwargs: Optional[dict[str, Any]] = None) -> threading.Thread:
    """Unstarted thread running *target* under the spawner's contextvars (see :func:`ctx_bound`).

    Returns before ``start()`` on purpose: callers register the thread in a gate or a join
    registry first, and a start failure (thread exhaustion) stays the caller's to handle."""
    ctx = contextvars.copy_context()
    return threading.Thread(target=ctx.run, args=(target, *args), kwargs=kwargs or {},
                            name=name, daemon=daemon)


def shutdown_context_pool(wait: bool = False) -> None:
    """Release the shared pool (tests and profile churn); the next submit recreates it."""
    global _POOL
    with _POOL_LOCK:
        executor, _POOL = _POOL, None
    if executor is not None:
        executor.shutdown(wait=wait)


def _context_pool() -> ThreadPoolExecutor:
    """Process-wide pool. ``DaemonThreadPoolExecutor`` copies the submitting context per task,
    so one pool safely serves every profile (the worker's own start context is irrelevant)."""
    global _POOL
    executor = _POOL
    if executor is not None:
        return executor
    with _POOL_LOCK:
        if _POOL is None:
            from tools.daemon_pool import DaemonThreadPoolExecutor
            _POOL = DaemonThreadPoolExecutor(
                max_workers=_POOL_MAX_WORKERS, thread_name_prefix="hermes-ctx")
        return _POOL


def _named_target(target: Callable[..., Any], name: str) -> Callable[..., Any]:
    def _run(*args: Any, **kwargs: Any) -> Any:
        thread = threading.current_thread()
        previous = thread.name
        thread.name = name
        try:
            return target(*args, **kwargs)
        finally:
            thread.name = previous
    return _run


def _log_failure(label: str, future: Future) -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.error("background job %s failed", label, exc_info=exc)


def submit_context(target: Callable[..., Any], *, name: Optional[str] = None,
                   args: tuple = (), kwargs: Optional[dict[str, Any]] = None) -> Future:
    """Submit a short, fire-and-forget job to the shared pool under the caller's contextvars.

    The returned ``Future`` carries the result/exception (exceptions are also logged with
    ``name`` for context). Do not submit blocking or joinable work: use
    :func:`spawn_context_thread` for anything that can outlive a bounded pool slot."""
    label = name or getattr(target, "__name__", "job")
    runnable = _named_target(target, name) if name else target
    future = _context_pool().submit(runnable, *args, **(kwargs or {}))
    future.add_done_callback(lambda done: _log_failure(label, done))
    return future
