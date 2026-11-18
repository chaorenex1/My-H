"""Live progress from inside a running tool call onto the agent's ``tool_progress_callback``.

Two relays ride the same per-call callback:

* ``bound_tool_progress`` / ``current_tool_progress`` — MCP ``notifications/progress`` as throttled
  ``tool.progress`` events; the first update and the one reaching ``total`` always pass.
* ``bound_tool_events`` / ``current_tool_events`` — the ADR-4 D14 eval channel: streamed cell output
  as ``tool.output`` frames and the background job's final ``tool.complete``. This channel is not
  tool-progress chrome: it is not throttled by ``display.tool_progress`` and carries no preview.
* ``bound_turn_reply`` / ``current_turn_reply`` — the ADR-4 D11.6 ``out://`` reply sink. Bound only
  while a tool call runs inside a live turn; absent outside a turn so the host op fails closed.

The executor binds the relays for each dispatched call; a tool reads them on its own thread and may
invoke the result from any thread (the MCP client fires on the MCP event loop).
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
import time
from typing import Callable, Iterator, Optional

logger = logging.getLogger(__name__)

TOOL_PROGRESS_EVENT = "tool.progress"
_MIN_INTERVAL_S = 0.5

ProgressReporter = Callable[[float, Optional[float], Optional[str]], None]
_current: contextvars.ContextVar[Optional[ProgressReporter]] = contextvars.ContextVar("tool_progress_reporter", default=None)

#: ADR-4 D14: one eval cell's streamed text can be arbitrarily long; a single event this size
#: keeps a chatty cell from turning one console.log into a multi-megabyte frame.
TOOL_OUTPUT_CHUNK_CHARS = 8_192
_TOOL_OUTPUT_EVENT = "tool.output"
_TOOL_COMPLETE_EVENT = "tool.complete"

_current_events: contextvars.ContextVar[Optional[ToolEventSink]] = contextvars.ContextVar(
    "tool_event_sink", default=None)
_current_reply: contextvars.ContextVar[Optional[Callable[[str], bool]]] = contextvars.ContextVar(
    "tool_turn_reply", default=None)


class ToolEventSink:
    """The eval tool's additive event channel for one call (ADR-4 D14/D11.6).

    ``stream`` emits ``tool.output`` frames tagged with the cell they belong to; ``complete``
    re-settles the same card after a background job finishes. Both keep the call's stable
    ``tool_call_id`` so a client merges by card identity instead of appending a new row.
    """

    def __init__(self, callback, tool_name: str, tool_call_id: str):
        self._callback = callback
        self.tool_name = str(tool_name or "")
        self.tool_call_id = str(tool_call_id or "")

    def stream(self, cell_index: int, stream: str, text: str) -> None:
        if not isinstance(text, str) or not text:
            return
        name = stream if stream in ("stdout", "stderr") else "stdout"
        for start in range(0, len(text), TOOL_OUTPUT_CHUNK_CHARS):
            chunk = text[start:start + TOOL_OUTPUT_CHUNK_CHARS]
            try:
                self._callback(_TOOL_OUTPUT_EVENT, self.tool_name, None, None,
                               tool_call_id=self.tool_call_id, cell_index=int(cell_index or 0),
                               stream=name, text=chunk)
            except Exception:  # health: allow BLE001 -- a UI sink failing must never fail the cell
                logger.debug("tool.output callback failed for %s", self.tool_name, exc_info=True)

    def complete(self, result: object, *, is_error: bool = False, job_id: str = "",
                 duration_s: Optional[float] = None) -> None:
        try:
            self._callback(_TOOL_COMPLETE_EVENT, self.tool_name, None, None,
                           tool_call_id=self.tool_call_id, result=result,
                           is_error=bool(is_error), job_id=job_id, duration_s=duration_s)
        except Exception:  # health: allow BLE001 -- a UI sink failing must never fail the job
            logger.debug("tool.complete callback failed for %s", self.tool_name, exc_info=True)


@contextlib.contextmanager
def bound_tool_events(callback, tool_name: str, tool_call_id: str) -> Iterator[None]:
    """Bind the eval stream/completion relay for this call (no-op without a progress callback)."""
    sink = ToolEventSink(callback, tool_name, tool_call_id) if callback else None
    token = _current_events.set(sink)
    try:
        yield
    finally:
        _current_events.reset(token)


def current_tool_events() -> Optional[ToolEventSink]:
    """The running call's eval event sink, or None outside a dispatched tool call."""
    return _current_events.get()


@contextlib.contextmanager
def bound_turn_reply(callback) -> Iterator[None]:
    """Bind ``out://``'s reply sink for the call (None when the turn has no reply surface)."""
    token = _current_reply.set(callback if callable(callback) else None)
    try:
        yield
    finally:
        _current_reply.reset(token)


def current_turn_reply() -> Optional[Callable[[str], bool]]:
    """The live turn's reply sink, or None when no user-facing reply path is bound.
    The callable returns False when the turn's own sinks are gone."""
    return _current_reply.get()


def _turn_reply_from_agent(agent) -> Callable[[str], bool]:
    """ADR-4 D11.6: out() lands where mid-turn user-visible text already lands.

    The sinks are re-read on every call, never captured: a background eval job keeps this
    closure after the turn settles, and a stale callback must not emit into a finished turn.
    Returns False when no turn-bound sink is live, which ``__out__`` reports as an error.
    """

    def reply(text: str) -> bool:
        interim = getattr(agent, "interim_assistant_callback", None)
        if callable(interim):
            interim(text, already_streamed=False)
            return True
        printer = getattr(agent, "_print_fn", None)
        if callable(printer):
            printer(text)
            return True
        return False

    return reply


@contextlib.contextmanager
def bound_tool_relay(agent, tool_name: str, tool_call_id: str) -> Iterator[None]:
    """Bind every per-call relay the executor publishes through (progress, eval events, reply)."""
    callback = getattr(agent, "tool_progress_callback", None)
    with bound_tool_progress(callback, tool_name, tool_call_id), \
            bound_tool_events(callback, tool_name, tool_call_id), \
            bound_turn_reply(_turn_reply_from_agent(agent)):
        yield


def _reporter(callback, tool_name: str, tool_call_id: str) -> ProgressReporter:
    lock = threading.Lock()
    last = [0.0]

    def report(progress: float, total: Optional[float] = None, message: Optional[str] = None) -> None:
        done = total is not None and total > 0 and progress >= total
        now = time.monotonic()
        with lock:
            if last[0] and not done and now - last[0] < _MIN_INTERVAL_S:
                return
            last[0] = now
        percent = round(min(max(progress / total, 0.0), 1.0) * 100) if total else None
        try:
            callback(TOOL_PROGRESS_EVENT, tool_name, message or None, None, tool_call_id=tool_call_id,
                     progress=float(progress), total=None if total is None else float(total), percent=percent)
        except Exception:  # health: allow BLE001 -- a UI sink failing must never fail the tool call
            logger.debug("tool.progress callback failed for %s", tool_name, exc_info=True)
    return report


@contextlib.contextmanager
def bound_tool_progress(callback, tool_name: str, tool_call_id: str) -> Iterator[None]:
    """Bind this call's relay for the block (no-op when the agent has no progress callback)."""
    token = _current.set(_reporter(callback, tool_name, tool_call_id or "") if callback else None)
    try:
        yield
    finally:
        _current.reset(token)


def current_tool_progress() -> Optional[ProgressReporter]:
    """The running call's reporter, or None outside a dispatched tool call."""
    return _current.get()


def tool_progress_payload(name: Optional[str], message: Optional[str], kwargs: dict) -> dict:
    """The ``tool.progress`` wire payload every viewer surface publishes (gateway event stream and
    the legacy TUI gateway): ``percent`` is 0-100 when the server sent a ``total``, else None."""
    return {"tool_id": str(kwargs.get("tool_call_id") or ""), "name": str(name or ""),
            "progress": float(kwargs.get("progress") or 0.0), "total": kwargs.get("total"),
            "percent": kwargs.get("percent"), "message": str(message) if message else None}
