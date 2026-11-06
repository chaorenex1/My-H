"""Resident named-agent sessions (ADR-15 D5–D7): wake turns, park/revive, and the session pool.

A background ``agent`` run whose definition does not force ``blocking`` and that does not use a
worktree stays *resident*: after it settles the session keeps its live slot, its ``agent_session``
row and its ``agent_inbox``, and the built ``AIAgent`` stays open. While resident:

* a message steers the live turn when one is running (an aside at the next boundary);
* a message to an idle session starts a **wake turn** on the kept agent, appended to the same
  transcript — no new agent, no rewritten prefix;
* a message to a *parked* session (or a session from an earlier process) **revives** it by
  rebuilding a run from the transcript with the definition and ``@role`` re-resolved;
* after ``agent_sessions.idle_ttl_seconds`` with no run, no pending inbox and no worktree the
  session is **parked**: the agent is closed and the live slot released, but the row, transcript
  and pending inbox survive for a later revive.

Resident and wake runs occupy this module's own capacity (``agent_sessions.max_live``), never
``agent_loop``'s worker pool, so a long-lived agent cannot starve ordinary background work.

Every identifier here is the ``agent_session_id`` minted by the ``agent`` tool; parking is per
profile because the row lives in that profile's ``state.db``.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional

import hermes_session_paths as session_paths
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

#: Live state is what a quota slot counts; parked sessions are rows only.
LIVE_STATES = ("running", "idle")

#: Config fallbacks; ``agent_sessions:`` in config.yaml overrides each one.
DEFAULT_LIMITS: dict[str, Any] = {
    "keep_alive": True,
    "idle_ttl_seconds": 1800.0,
    "max_runtime_seconds": 0.0,
    "max_live": 8,
    "max_parked": 64,
    "wake_queue": 32,
    "max_wake_turns": 1,
}

#: Watchdog cadence; a short TTL shortens the scan so a test-sized park does not wait a minute.
SCAN_SECONDS = 15.0

_SEQUENCE = itertools.count(1)


class AgentSessionQueueFull(RuntimeError):
    """The resident pool has no live slot: the caller gets a structured error, not a queue."""


@dataclass
class SessionJob:
    """One resident run's handle; attribute-compatible with ``agent_loop_service.AgentLoopJob``."""

    job_id: str
    label: str
    session_key: str
    dispatched_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    queue_position: int = 0


@dataclass
class _Session:
    """One resident session: identity, rebuild spec, and the live agent when it has one."""

    agent_session_id: str
    profile: str
    agent_name: str
    subject: str
    transcript_path: str
    parent_session_id: str
    spec: dict[str, Any]
    authority: dict[str, Any] = field(default_factory=dict)
    agent: Any = None
    state: str = "running"
    started_at: float = 0.0
    last_activity: float = field(default_factory=time.time)
    pending_bodies: list[str] = field(default_factory=list)
    last_sender: str = ""
    #: Wake turns since the last external delivery. One owner message triggers exactly one wake
    #: turn (its queued bodies are joined), so this stays 1 on the owner path; the relay path
    #: (P5) carries it forward to stop two idle peers ping-ponging past ``max_wake_turns``.
    wake_turns: int = 0
    worker: bool = False
    stopped: bool = False
    timed_out: bool = False
    worktree: bool = False


# -- limits ---------------------------------------------------------------------


def _number(value: Any, default: float, *, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(minimum, float(value))


def session_limits(config: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Resolved ``agent_sessions`` limits; a missing/invalid block leaves the defaults in charge.

    Out-of-range values are clamped here (never raised into a turn); ``hermes doctor`` is where
    an invalid configured value is reported.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly() or {}
        except Exception:  # health: allow BLE001 -- unreadable config must not break a run
            logger.debug("agent_sessions: config unreadable", exc_info=True)
            config = {}
    block = (config or {}).get("agent_sessions")
    block = block if isinstance(block, Mapping) else {}
    keep = block.get("keep_alive", DEFAULT_LIMITS["keep_alive"])
    if isinstance(keep, str):
        keep = keep.strip().lower() not in {"0", "false", "no", "off", ""}
    return {
        "keep_alive": bool(keep),
        "idle_ttl_seconds": _number(block.get("idle_ttl_seconds"),
                                    DEFAULT_LIMITS["idle_ttl_seconds"], minimum=0.0),
        "max_runtime_seconds": _number(block.get("max_runtime_seconds"),
                                       DEFAULT_LIMITS["max_runtime_seconds"], minimum=0.0),
        "max_live": int(_number(block.get("max_live"), DEFAULT_LIMITS["max_live"], minimum=1.0)),
        "max_parked": int(_number(block.get("max_parked"), DEFAULT_LIMITS["max_parked"],
                                  minimum=0.0)),
        "wake_queue": int(_number(block.get("wake_queue"), DEFAULT_LIMITS["wake_queue"],
                                  minimum=1.0)),
        "max_wake_turns": int(_number(block.get("max_wake_turns"),
                                      DEFAULT_LIMITS["max_wake_turns"], minimum=1.0)),
    }


# -- state.db helpers -----------------------------------------------------------


def _with_db(action: Callable[[Any], Any], default: Any, *, profile: str = "") -> Any:
    """Run one store action; a missing or failing store degrades to *default*, never raises.

    ``profile`` pins the state.db a resident session owns. The pool is process-wide while
    profiles are not (HX002): the watchdog and worker threads must never write a session's row
    to whatever profile happened to launch them, so every per-session call passes its home here.
    """
    from hermes_state_registry import acquire, release_or_close

    db_path = (Path(profile) / "state.db") if str(profile or "").strip() else None
    try:
        db = acquire(db_path)
    except Exception:  # health: allow BLE001 -- a missing store is "no persistent rows"
        logger.debug("agent_sessions: no store", exc_info=True)
        return default
    try:
        return action(db)
    except Exception:  # health: allow BLE001 -- bookkeeping must not fail the caller's turn
        logger.warning("agent_sessions: store action failed", exc_info=True)
        return default
    finally:
        release_or_close(db)


def _session_row(agent_session_id: str) -> Optional[dict[str, Any]]:
    return _with_db(lambda db: db.get_agent_session(agent_session_id), None)


def _pending_count(agent_session_id: str, *, profile: str = "") -> int:
    return int(_with_db(
        lambda db: db.count_agent_inbox(agent_session_id, status="pending"), 0,
        profile=profile) or 0)


def _take_pending(agent_session_id: str, limit: int, *, profile: str = ""
                  ) -> list[dict[str, Any]]:
    return list(_with_db(
        lambda db: db.take_pending_agent_inbox(agent_session_id, limit=limit), [],
        profile=profile) or [])


def _finish_row(agent_session_id: str, status: str, body: Optional[str], *,
                profile: str = "") -> None:
    _with_db(lambda db: db.finish_agent_session(
        agent_session_id, status=status, final_response=body), None, profile=profile)


def _park_row(agent_session_id: str, *, profile: str = "") -> None:
    _with_db(lambda db: db.park_agent_session(agent_session_id), None, profile=profile)


def _revive_row(agent_session_id: str, *, profile: str = "") -> None:
    _with_db(lambda db: db.revive_agent_session(agent_session_id), None, profile=profile)


def _cancel_row(agent_session_id: str, reason: str, *, profile: str = "") -> None:
    _with_db(lambda db: db.cancel_agent_session(agent_session_id, reason=reason), None,
             profile=profile)


def _fail_pending(agent_session_id: str, *, profile: str = "") -> int:
    return int(_with_db(
        lambda db: db.fail_pending_agent_inbox(agent_session_id), 0, profile=profile) or 0)


def _parked_rows(limit: int = 200, *, profile: str = "") -> list[dict[str, Any]]:
    return list(_with_db(lambda db: db.list_agent_sessions(
        status="parked", limit=limit), [], profile=profile) or [])


def _relay_row(agent_session_id: str, sender: str, body: str, *, profile: str = "") -> None:
    _with_db(lambda db: db.add_agent_inbox(
        agent_session_id=agent_session_id, sender=sender, recipient=agent_session_id,
        body=body, kind="wake_relay", status="delivered"), None, profile=profile)


def _history(agent_name: str, agent_session_id: str, *, profile: str = "",
             transcript_path: str = "") -> list[dict[str, Any]]:
    from agent import run_transcript

    home = Path(profile) if str(profile or "").strip() else None
    # ADR-16 D4: the row records the bundle transcript; an explicit home would otherwise pin
    # the lookup to the legacy per-agent layout (the read side skips state.db when home is set).
    path = Path(str(transcript_path)) if str(transcript_path or "").strip() else None
    try:
        return run_transcript.read_history(agent_name, agent_session_id, home=home, path=path)
    except Exception:  # health: allow BLE001 -- a missing transcript means a fresh conversation
        logger.warning("agent_sessions: transcript unreadable for %s", agent_session_id,
                       exc_info=True)
        return []


def _configure_tool_archive(session: _Session, agent: Any) -> None:
    """Point a rebuilt agent's tool archive at ``<bundle>/<topic>/`` (ADR-16 D4)."""
    transcript = Path(str(session.transcript_path or ""))
    if not transcript.name:
        return
    from tools.tool_output_archive import configure_agent_archive

    configure_agent_archive(agent, session_paths.subagent_dir(transcript.parent,
                                                               session.subject))


def _write_wake_artifacts(session: _Session, answer: str, value: Any = None) -> None:
    """Refresh the report and ast summary after a wake turn (ADR-16 D4 settle files)."""
    transcript = Path(str(session.transcript_path or ""))
    if not transcript.name:
        return
    from agent import run_artifacts

    run = SimpleNamespace(
        agent_name=session.agent_name, agent_session_id=session.agent_session_id,
        subject=session.subject, topic=transcript.stem, bundle=transcript.parent,
        path=transcript, project_dir="", output_schema=session.spec.get("output_schema"))
    try:
        run_artifacts.settle_bundle(
            run, "completed", answer,
            sources=tuple(getattr(value, "summary_sources", ()) or ()))
    except Exception:  # health: allow BLE001 -- artifacts must not fail a settled wake turn
        logger.warning("[agent_sessions] wake artifacts failed for %s", session.agent_session_id,
                       exc_info=True)


# -- runtime --------------------------------------------------------------------


class AgentSessionRuntime:
    """Process-wide resident sessions for one or more profiles (ADR-15 D5–D7)."""

    def __init__(self, limits: Optional[Mapping[str, Any]] = None) -> None:
        self._limits: dict[str, Any] = dict(limits or session_limits())
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}
        self._watchdog: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    # -- configuration ----------------------------------------------------------

    def limits(self) -> dict[str, Any]:
        return dict(self._limits)

    def keep_alive(self) -> bool:
        return bool(self._limits.get("keep_alive"))

    # -- admission --------------------------------------------------------------

    def _live_count_locked(self, profile: str) -> int:
        return sum(1 for session in self._sessions.values()
                   if session.profile == profile and session.state in LIVE_STATES)

    def _admit_locked(self, profile: str) -> None:
        limit = int(self._limits.get("max_live") or 1)
        if self._live_count_locked(profile) >= limit:
            raise AgentSessionQueueFull(
                f"agent session pool is full ({limit} live session(s) for this profile); "
                "stop a resident agent or raise agent_sessions.max_live")

    def open_session(self, *, agent_session_id: str, agent_name: str, subject: str,
                     transcript_path: str, parent_session_id: str, spec: Mapping[str, Any],
                     authority: Optional[Mapping[str, Any]] = None,
                     worktree: bool = False, profile: Optional[str] = None) -> None:
        """Reserve a live slot and register the session before its first run dispatches."""
        session_id = str(agent_session_id)
        where = str(profile or get_hermes_home())
        with self._lock:
            self._admit_locked(where)
            self._sessions[session_id] = _Session(
                agent_session_id=session_id, profile=where, agent_name=str(agent_name),
                subject=str(subject), transcript_path=str(transcript_path),
                parent_session_id=str(parent_session_id or ""), spec=dict(spec or {}),
                authority=dict(authority or {}), worktree=bool(worktree))
            self._ensure_watchdog_locked()

    def note_agent(self, agent_session_id: str, agent: Any) -> None:
        """Attach the built ``AIAgent`` so a wake turn can reuse it instead of rebuilding."""
        with self._lock:
            session = self._sessions.get(str(agent_session_id))
            if session is not None:
                session.agent = agent
                return
        # The session was stopped/timed out before its run built the agent: ``agent_run`` will
        # not close it (keep_alive), so the pool owns the close here.
        with contextlib.suppress(Exception):
            agent.close()

    # -- initial dispatch -------------------------------------------------------

    def submit(self, agent_session_id: str, task: Callable[[], Any], *, label: str = "",
               session_key: str = "",
               on_settle: Optional[Callable[[Any, Any, Optional[BaseException]], None]] = None
               ) -> SessionJob:
        """Run one fresh session on its own thread; the caller has already opened the slot."""
        from agent.memory_provider import spawn_context_thread

        job = SessionJob(job_id=f"agent-session-{next(_SEQUENCE)}", label=str(label or ""),
                         session_key=str(session_key or ""))
        with self._lock:
            session = self._sessions.get(str(agent_session_id))
            if session is None:
                raise AgentSessionQueueFull(
                    f"agent session {agent_session_id} is not registered for dispatch")
            session.worker = True
            session.started_at = job.started_at = time.time()
        thread = spawn_context_thread(
            lambda: self._initial_work(job, str(agent_session_id), task, on_settle),
            name=f"agent-session-{job.job_id}"[:48])
        try:
            thread.start()
        except RuntimeError as exc:
            logger.error("[agent_sessions] %s could not start: %s", job.job_id, exc)
            self.finish(str(agent_session_id), error=exc)
        return job

    def _initial_work(self, job: SessionJob, agent_session_id: str, task: Callable[[], Any],
                      on_settle: Optional[Callable[..., None]]) -> None:
        error: Optional[BaseException] = None
        value: Any = None
        try:
            value = task()
        except BaseException as exc:  # health: allow BLE001 -- the settle callback owns the error
            error = exc
        if on_settle is not None:
            try:
                on_settle(job, value, error)
            except Exception:  # health: allow BLE001 -- the run already happened
                logger.warning("[agent_sessions] %s settle callback failed", job.job_id,
                               exc_info=True)
        self.finish(agent_session_id, error=error)

    def finish(self, agent_session_id: str, *, error: Optional[BaseException] = None) -> None:
        """Settle a fresh run: keep the agent resident when it succeeded, close it otherwise."""
        session_id = str(agent_session_id)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.worker = False
            drop = error is not None or session.timed_out or session.stopped
            if not drop:
                session.state = "idle"
                session.started_at = 0.0
                session.last_activity = time.time()
            else:
                self._sessions.pop(session_id, None)
        if drop:
            if session.timed_out:
                _finish_row(session_id, "error", self._timeout_note(),
                            profile=session.profile)
            self._close_agent(session)

    # -- delivery ---------------------------------------------------------------

    def state_of(self, agent_session_id: str) -> Optional[str]:
        with self._lock:
            session = self._sessions.get(str(agent_session_id))
            return session.state if session is not None else None

    def is_live(self, agent_session_id: str) -> bool:
        return self.state_of(agent_session_id) in LIVE_STATES

    def deliver(self, agent_session_id: str, body: str, *, sender: str = "",
                row: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
        """Deliver one owner message; ``None`` means "no resident session", so the caller queues.

        A running turn takes it as a steer; an idle session wakes; a parked row revives. Raises
        :class:`AgentSessionQueueFull` when the session's wake queue or the live pool is full.
        """
        session_id = str(agent_session_id)
        text = str(body or "")
        if not text.strip():
            return None
        thread: Optional[threading.Thread] = None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_sender = str(sender or "")
                if session.state == "running" and session.agent is not None:
                    try:
                        if session.agent.steer(text):
                            session.last_activity = time.time()
                            return {"status": "delivered",
                                    "note": "delivered to the live turn; it lands at the next "
                                            "tool boundary."}
                    except Exception:  # health: allow BLE001 -- fall through to the wake queue
                        logger.debug("agent_sessions: live steer failed for %s", session_id,
                                     exc_info=True)
                limit = int(self._limits.get("wake_queue") or 1)
                if len(session.pending_bodies) >= limit:
                    raise AgentSessionQueueFull(
                        f"agent session {session_id} has {limit} message(s) waiting for a wake "
                        "turn; wait for the current turn or raise agent_sessions.wake_queue")
                session.pending_bodies.append(text)
                session.last_activity = time.time()
                session.wake_turns = 0
                thread = self._ensure_worker_locked(session)
        if thread is not None:
            self._start_thread(thread)
        if session is not None:
            return {"status": "woken",
                    "note": "queued for a wake turn; the answer lands in the session transcript "
                            "and its inbox as a wake_relay row."}
        if row is None or str(row.get("status") or "") != "parked":
            return None
        return self._revive(row, text, sender=sender)

    def _revive(self, row: Mapping[str, Any], body: str, *, sender: str = ""
                ) -> dict[str, Any]:
        session_id = str(row.get("agent_session_id") or "")
        where = str(get_hermes_home())
        thread: Optional[threading.Thread] = None
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                # Another delivery revived it between our lookup and this lock: enqueue instead
                # of replacing the live entry.
                existing.pending_bodies.append(body)
                existing.last_sender = str(sender or "")
                existing.last_activity = time.time()
                thread = self._ensure_worker_locked(existing)
                revived = False
            else:
                self._admit_locked(where)
                session = _Session(
                    agent_session_id=session_id, profile=where,
                    agent_name=str(row.get("agent_name") or ""),
                    subject=str(row.get("subject") or ""),
                    transcript_path=str(row.get("transcript_path") or ""),
                    parent_session_id=str(row.get("parent_session_id") or ""),
                    spec=_spec_from_row(row), state="idle", pending_bodies=[body],
                    last_sender=str(sender or ""))
                self._sessions[session_id] = session
                thread = self._ensure_worker_locked(session)
                self._ensure_watchdog_locked()
                revived = True
        if revived:
            _revive_row(session_id, profile=where)
        self._start_thread(thread)
        return {"status": "revived" if revived else "woken",
                "note": "the session is rebuilding from its transcript; the answer will be "
                        "appended there and in its inbox (agent_out)."}

    # -- wake worker ------------------------------------------------------------

    def _ensure_worker_locked(self, session: _Session) -> Optional[threading.Thread]:
        if session.worker:
            return None
        from agent.memory_provider import spawn_context_thread

        session.worker = True
        return spawn_context_thread(
            lambda: self._work(session),
            name=f"agent-wake-{session.agent_session_id}"[:48])

    @staticmethod
    def _start_thread(thread: Optional[threading.Thread]) -> None:
        if thread is None:
            return
        try:
            thread.start()
        except RuntimeError as exc:
            logger.error("[agent_sessions] wake worker could not start: %s", exc)

    def _work(self, session: _Session) -> None:
        session_id = session.agent_session_id
        while True:
            with self._lock:
                if self._sessions.get(session_id) is not session or session.stopped:
                    session.worker = False
                    return
                bodies = list(session.pending_bodies)
                session.pending_bodies.clear()
                session.state = "running"
                session.started_at = time.time()
            rows = _take_pending(session_id, int(self._limits.get("wake_queue") or 1),
                                 profile=session.profile)
            message = "\n\n".join(
                [str(item.get("body") or "") for item in rows] + [body for body in bodies]
            ).strip()
            if not message:
                if self._settle_idle(session):
                    continue
                return
            try:
                value = self._run_turn(session, message)
            except BaseException as exc:  # health: allow BLE001 -- reported on the row
                self._fail(session, exc)
                return
            if not self._after_turn(session, value):
                if session.timed_out and session.agent is not None:
                    self._close_agent(session)
                return
            if self._settle_idle(session):
                continue
            return

    def _settle_idle(self, session: _Session) -> bool:
        """Mark the session idle; True when more work arrived and the loop must run again."""
        session_id = session.agent_session_id
        with self._lock:
            if self._sessions.get(session_id) is not session or session.stopped:
                session.worker = False
                return False
            if session.pending_bodies:
                return True
            session.state = "idle"
            session.started_at = 0.0
            session.last_activity = time.time()
            # Release the worker slot before looking for late rows: a concurrent deliver() then
            # starts its own worker instead of queueing behind this one.
            session.worker = False
        if _pending_count(session_id, profile=session.profile) > 0:
            with self._lock:
                if self._sessions.get(session_id) is not session or session.worker:
                    return False
                session.worker = True
                session.state = "running"
            return True
        return False

    def _run_turn(self, session: _Session, message: str) -> Any:
        from agent import agent_run
        from tools.agent_inbox import register_live_session, unregister_live_session

        if session.agent is not None:
            register_live_session(agent_session_id=session.agent_session_id, agent=session.agent,
                                  owner_session_id=session.parent_session_id,
                                  goal=session.subject, depth=0,
                                  authority=session.authority or None)
            try:
                return agent_run.continue_run(
                    session.agent, message, task_id=session.agent_session_id,
                    agent_name=session.agent_name,
                    agent_session_id=session.agent_session_id,
                    model=str(session.spec.get("model_name") or ""),
                    provider=str(session.spec.get("provider_name") or ""),
                    toolsets=list(session.spec.get("toolsets") or []),
                    all_tools=bool(session.spec.get("all_tools")),
                    history_key=session.parent_session_id,
                    output_schema=session.spec.get("output_schema"),
                    model_role=str(session.spec.get("model_role") or ""),
                    read_summarize=session.spec.get("read_summarize"))
            finally:
                unregister_live_session(session.agent_session_id, session.agent)
        params = _revive_params(session)
        conversation = _history(session.agent_name, session.agent_session_id,
                                profile=session.profile,
                                transcript_path=session.transcript_path)

        def _on_agent(agent: Any) -> None:
            _configure_tool_archive(session, agent)
            self.note_agent(session.agent_session_id, agent)
            register_live_session(agent_session_id=session.agent_session_id, agent=agent,
                                  owner_session_id=session.parent_session_id,
                                  goal=session.subject, depth=0,
                                  authority=session.authority or None)

        try:
            return agent_run.run(message, history=conversation,
                                 agent_session_id=session.agent_session_id,
                                 on_agent=_on_agent,
                                 on_settled=lambda: unregister_live_session(
                                     session.agent_session_id, session.agent),
                                 **params)
        finally:
            unregister_live_session(session.agent_session_id, session.agent)

    def _after_turn(self, session: _Session, value: Any) -> bool:
        answer = str(getattr(value, "answer", "") or "")
        with self._lock:
            if self._sessions.get(session.agent_session_id) is not session or session.stopped:
                return False
            if session.timed_out:
                return False
            session.wake_turns += 1
            session.state = "idle"
            session.started_at = 0.0
            session.last_activity = time.time()
        _write_wake_artifacts(session, answer, value)
        _finish_row(session.agent_session_id, "completed", answer,
                    profile=session.profile)
        _relay_row(session.agent_session_id, session.last_sender, answer,
                   profile=session.profile)
        self._notify_sender(session, answer)
        return True

    def _notify_sender(self, session: _Session, answer: str) -> None:
        """Wake a live *named* sender; an ordinary conversation reads the relay row instead."""
        sender = str(session.last_sender or "")
        if not sender or not self.is_live(sender):
            return
        try:
            from tools.process_registry import process_registry

            process_registry.completion_queue.put({
                "type": "async_delegation", "session_key": sender,
                "agent_session_id": session.agent_session_id,
                "subject": session.subject, "status": "completed",
                "summary": answer, "goal": session.subject, "goals": [session.subject],
                "role": "agent",
            })
        except Exception:  # health: allow BLE001 -- a lost notification must not lose the run
            logger.warning("[agent_sessions] relay notification failed", exc_info=True)

    def _fail(self, session: _Session, exc: BaseException) -> None:
        session_id = session.agent_session_id
        with self._lock:
            self._sessions.pop(session_id, None)
            session.worker = False
            timed_out = session.timed_out
        self._close_agent(session)
        _finish_row(session_id, "error", self._timeout_note() if timed_out else str(exc),
                    profile=session.profile)

    def _timeout_note(self) -> str:
        return (f"timed out after {int(self._limits.get('max_runtime_seconds') or 0)}s "
                "(agent_sessions.max_runtime_seconds)")

    # -- park / stop ------------------------------------------------------------

    def park(self, agent_session_id: str) -> bool:
        """Close a live idle session and keep its row for a later revive."""
        session_id = str(agent_session_id)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.state != "idle" or session.pending_bodies:
                return False
            if _pending_count(session_id, profile=session.profile) > 0:
                return False
            self._sessions.pop(session_id, None)
            session.stopped = True
        self._close_agent(session)
        _park_row(session_id, profile=session.profile)
        self._enforce_parked_cap(session.profile)
        return True

    def stop(self, agent_session_id: str, *, reason: str = "stopped by the owner") -> bool:
        """Terminal stop: interrupt/close a live session or cancel a parked row."""
        session_id = str(agent_session_id)
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                session.stopped = True
                session.worker = False
        if session is not None:
            self._interrupt(session)
            self._close_agent(session)
        profile = session.profile if session is not None else ""
        known = session is not None or _session_row(session_id) is not None
        if known:
            _cancel_row(session_id, reason, profile=profile)
            _fail_pending(session_id, profile=profile)
        return known

    def _cancel_parked(self, agent_session_id: str, profile: str) -> None:
        _cancel_row(agent_session_id, "evicted by agent_sessions.max_parked", profile=profile)
        _fail_pending(agent_session_id, profile=profile)

    def _enforce_parked_cap(self, profile: str) -> None:
        cap = int(self._limits.get("max_parked") or 0)
        rows = _parked_rows(profile=profile)
        if len(rows) <= cap:
            return
        # Newest-first: everything past the cap is the oldest parked session.
        for row in rows[cap:]:
            self._cancel_parked(str(row.get("agent_session_id") or ""), profile)

    # -- watchdog ---------------------------------------------------------------

    def sweep(self) -> dict[str, int]:
        """Park idle sessions and interrupt runs past the hard wall clock; safe to call anytime."""
        now = time.time()
        ttl = float(self._limits.get("idle_ttl_seconds") or 0)
        cap = float(self._limits.get("max_runtime_seconds") or 0)
        with self._lock:
            idle = [session for session in self._sessions.values()
                    if session.state == "idle" and not session.worker]
            running = [session for session in self._sessions.values()
                       if session.state == "running" and session.started_at
                       and cap > 0 and now - session.started_at >= cap]
        parked = 0
        for session in idle:
            if ttl <= 0 or now - session.last_activity < ttl or session.worktree:
                continue
            if self.park(session.agent_session_id):
                parked += 1
        for session in running:
            self._timeout(session)
        return {"parked": parked, "timed_out": len(running)}

    def _timeout(self, session: _Session) -> None:
        with self._lock:
            if self._sessions.get(session.agent_session_id) is not session:
                return
            session.timed_out = True
            session.stopped = True
            # Release the live slot now: the thread may keep running until the interrupt lands,
            # but it must not hold capacity or park.
            self._sessions.pop(session.agent_session_id, None)
        _finish_row(session.agent_session_id, "error", self._timeout_note(),
                    profile=session.profile)
        self._interrupt(session)

    def _interrupt(self, session: _Session) -> None:
        if session.agent is None:
            return
        try:
            from agent.interrupt_compat import request_hard_interrupt

            request_hard_interrupt(session.agent,
                                   f"resident agent session timed out ({session.agent_session_id})")
        except Exception:  # health: allow BLE001 -- an interrupt is best-effort
            logger.debug("agent_sessions: interrupt failed", exc_info=True)

    def _ensure_watchdog_locked(self) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._stopping.clear()
        ttl = float(self._limits.get("idle_ttl_seconds") or 0)
        cap = float(self._limits.get("max_runtime_seconds") or 0)
        if ttl <= 0 and cap <= 0:
            return
        from agent.memory_provider import spawn_context_thread

        # The copied launch context is only a starting point: ``sweep`` rebinds each session's
        # profile (explicit state.db path) before any row/inbox access. ``spawn_context_thread``
        # keeps the thread's ambient state well-defined for HX012 and for any log/telemetry hook.
        self._watchdog = spawn_context_thread(
            self._watch, name="agent-session-watchdog")
        try:
            self._watchdog.start()
        except RuntimeError as exc:
            logger.warning("[agent_sessions] watchdog did not start: %s", exc)
            self._watchdog = None

    def _scan_seconds(self) -> float:
        ttl = float(self._limits.get("idle_ttl_seconds") or 0)
        cap = float(self._limits.get("max_runtime_seconds") or 0)
        windows = [window for window in (ttl, cap) if window > 0]
        if not windows:
            return SCAN_SECONDS
        return max(1.0, min(SCAN_SECONDS, min(windows) / 2))

    def _watch(self) -> None:
        while not self._stopping.wait(self._scan_seconds()):
            try:
                self.sweep()
            except Exception:  # health: allow BLE001 -- a dead watchdog would wedge the pool
                logger.warning("[agent_sessions] sweep failed", exc_info=True)

    def close(self, timeout: float = 5.0) -> None:
        """Stop the watchdog and close every resident agent (process shutdown/tests)."""
        self._stopping.set()
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None and watchdog.is_alive() and watchdog is not threading.current_thread():
            watchdog.join(timeout=max(0.0, float(timeout)))
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stopped = True
            self._close_agent(session)

    @staticmethod
    def _close_agent(session: _Session) -> None:
        agent = session.agent
        from tools.read_summary import clear_read_summarize

        clear_read_summarize(session.agent_session_id)
        session.agent = None
        if agent is None:
            return
        try:
            agent.close()
        except Exception:  # health: allow BLE001 -- closing is best-effort
            logger.debug("agent_sessions: close failed", exc_info=True)

    # -- observation ------------------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        """Every resident session as a listing row (live ones only; parked are DB rows)."""
        with self._lock:
            return [{"agent_session_id": session.agent_session_id,
                     "agent_name": session.agent_name, "subject": session.subject,
                     "parent_session_id": session.parent_session_id,
                     "state": session.state,
                     "model": str(session.spec.get("model_name") or ""),
                     "started_at": session.started_at or None,
                     "last_activity_at": session.last_activity}
                    for session in self._sessions.values()]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_state: dict[str, int] = {}
            for session in self._sessions.values():
                by_state[session.state] = by_state.get(session.state, 0) + 1
        return {"max_live": int(self._limits.get("max_live") or 0), **by_state,
                "live": sum(by_state.get(state, 0) for state in LIVE_STATES)}


def _spec_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The rebuild spec stored on the row; an absent/corrupt block degrades to the row's names."""
    raw = row.get("metadata_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    return {"model_selector": str(row.get("model_role") or ""),
            "system_prompt": "", "toolsets": [], "all_tools": False, "flags": {}}


def _revive_params(session: _Session) -> dict[str, Any]:
    """Re-resolve the definition body and ``@role`` for a rebuild, then map to ``run`` kwargs."""
    spec = session.spec or {}
    body = str(spec.get("system_prompt") or "")
    project = str(spec.get("project_dir") or "")
    try:
        from pathlib import Path

        from tools import agent_tool

        definition = agent_tool._system_prompt(
            Path(project) if project else None, session.agent_name,
            str(spec.get("skill_name") or ""), body)
        body = definition.body or body
    except Exception:  # health: allow BLE001 -- the stored body still runs the session
        logger.warning("agent_sessions: definition re-read failed for %s",
                       session.agent_session_id, exc_info=True)
    flags = dict(spec.get("flags") or {})
    params: dict[str, Any] = {
        "system_message": body,
        "toolsets": list(spec.get("toolsets") or []),
        "all_tools": bool(spec.get("all_tools")),
        "project_dir": project,
        "read_only": bool(spec.get("read_only")),
        "max_iterations": int(spec.get("max_iterations") or 10),
        "reasoning_effort": spec.get("reasoning_effort") or "",
        "output_schema": spec.get("output_schema"),
        "autoload_skills": list(spec.get("autoload_skills") or []),
        "advisor": spec.get("advisor"),
        "read_summarize": spec.get("read_summarize"),
        "layer": session.agent_name,
        "history_session": session.parent_session_id,
        "meta": {"agent": session.agent_name, "depth": 0},
        "keep_alive": True,
        "skip_memory": bool(flags.get("skip_memory", False)),
        "skip_context_files": bool(flags.get("skip_context_files", False)),
        "skip_background_review": bool(flags.get("skip_background_review", True)),
        "load_soul_identity": bool(flags.get("load_soul_identity", True)),
    }
    selector = str(spec.get("model_selector") or "").strip()
    if selector:
        try:
            from hermes_cli.model_roles import resolve_model_selector

            route = resolve_model_selector(selector)
        except Exception:  # health: allow BLE001 -- fall back to the host's current route
            logger.warning("agent_sessions: model selector %r did not resolve", selector,
                           exc_info=True)
        else:
            params.update(model=route.model, provider=route.provider, base_url=route.base_url,
                          api_key=route.api_key, fallback_chain=route.fallback_chain,
                          model_role=route.role)
    return params


_RUNTIME: Optional[AgentSessionRuntime] = None
_RUNTIME_LOCK = threading.Lock()


def runtime() -> AgentSessionRuntime:
    """The process-wide resident pool (one live/idle budget per profile)."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = AgentSessionRuntime()
        return _RUNTIME


def reset_runtime() -> None:
    """Drop the shared pool (tests or a config reload); resident agents are closed."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        old, _RUNTIME = _RUNTIME, None
    if old is not None:
        old.close()


__all__ = [
    "DEFAULT_LIMITS",
    "LIVE_STATES",
    "AgentSessionQueueFull",
    "AgentSessionRuntime",
    "SessionJob",
    "reset_runtime",
    "runtime",
    "session_limits",
]
