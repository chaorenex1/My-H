"""Cooperative freeze of one session's running execution tree (ADR-1, ``session.freeze``).

The gate is deliberately process-local and ownerless-free: a frozen turn parks at safe
boundaries (turn top, before each API request, before a tool batch), keeps touching its
liveness activity so neither the turn watchdog nor the gateway inactivity reaper mistakes
the freeze for a stall, and releases on resume, Stop, or the end of its execution
generation. It never gates ADMISSION — ``agent.estop`` keeps that job — so an idle session
can be "frozen" with no effect on later prompts.

Subagents and the advisor share the parent's gate object (``share_gate``); nothing here
allocates per boundary.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

RELEASED = "released"
INTERRUPTED = "interrupted"

# Stop must release a parked turn in under a second (ADR-1 acceptance); resume is signalled
# through the condition variable, so this slice only bounds interrupt polling.
_POLL_SLICE_S = 0.5
# TurnLivenessWatchdog (600s default) and the gateway inactivity reaper (900s warning) read
# the agent's activity clock; a frozen turn stamps it well inside both windows.
_HEARTBEAT_S = 30.0


def stop_requested(agent) -> bool:
    """Whether a Stop (hard or soft) is pending for *agent*; doubles may carry neither flag."""
    if bool(getattr(agent, "_interrupt_requested", False)):
        return True
    hard = getattr(agent, "_hard_interrupt_requested", None)
    is_set = getattr(hard, "is_set", None)
    try:
        return bool(is_set()) if callable(is_set) else False
    except Exception:  # health: allow BLE001 -- a broken double's event must not wedge the loop
        return False


class AgentPauseGate:
    """One session's freeze switch, shared by every agent in its execution tree."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frozen = False
        self._frozen_at: Optional[float] = None
        self._parked = 0
        self._listeners: list[Callable[[], None]] = []

    @property
    def frozen(self) -> bool:
        with self._cond:
            return self._frozen

    @property
    def frozen_at(self) -> Optional[float]:
        with self._cond:
            return self._frozen_at

    def parked(self) -> int:
        """Agents currently blocked at a boundary (0 before the first one arrives)."""
        with self._cond:
            return self._parked

    def pause(self) -> bool:
        """Engage the freeze. True when this call engaged it, False when already frozen."""
        with self._cond:
            if self._frozen:
                return False
            self._frozen = True
            self._frozen_at = time.time()
        self._notify()
        return True

    def resume(self) -> bool:
        """Release every parker. True when this call released the freeze, False when not frozen."""
        with self._cond:
            if not self._frozen:
                return False
            self._frozen = False
            self._frozen_at = None
            self._cond.notify_all()
        self._notify()
        return True

    def onChange(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Run *listener* after every freeze/park transition; returns an unsubscribe callable."""
        with self._cond:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._cond:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass

        return unsubscribe

    def wait(self, agent=None, *, boundary: str = "execution") -> str:
        """Park until resumed: ``RELEASED``, or ``INTERRUPTED`` when a Stop arrives first."""
        with self._cond:
            if not self._frozen:
                return RELEASED
        if stop_requested(agent):
            return INTERRUPTED
        started = time.monotonic()
        last_beat = started
        with self._cond:
            self._parked += 1
        self._notify()
        try:
            while True:
                with self._cond:
                    if not self._frozen:
                        return RELEASED
                    self._cond.wait(_POLL_SLICE_S)
                    if not self._frozen:
                        return RELEASED
                if stop_requested(agent):
                    return INTERRUPTED
                now = time.monotonic()
                if now - last_beat >= _HEARTBEAT_S:
                    last_beat = now
                    _touch(agent, boundary, int(now - started))
        finally:
            with self._cond:
                self._parked -= 1
            self._notify()

    def _notify(self) -> None:
        with self._cond:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:  # health: allow BLE001 -- an observer must never unwedge or break the gate
                logger.debug("pause gate listener failed", exc_info=True)


def _touch(agent, boundary: str, seconds: int) -> None:
    touch = getattr(agent, "_touch_activity", None)
    if not callable(touch):
        return
    try:
        touch(f"frozen at {boundary} for {seconds}s")
    except Exception:  # health: allow BLE001 -- liveness bookkeeping must never fail the turn
        logger.debug("frozen activity heartbeat failed", exc_info=True)


def gate_for(agent) -> AgentPauseGate:
    """The gate bound to *agent*, creating one on first use."""
    gate = getattr(agent, "_pause_gate", None)
    if isinstance(gate, AgentPauseGate):
        return gate
    gate = AgentPauseGate()
    try:
        agent._pause_gate = gate
    except Exception:  # health: allow BLE001 -- a slotted double still parks on the returned gate
        logger.debug("could not bind a pause gate to %r", type(agent).__name__)
    return gate


def share_gate(child, parent) -> AgentPauseGate:
    """Bind the parent's gate to *child*: one freeze covers the whole execution tree."""
    gate = getattr(parent, "_pause_gate", None)
    if not isinstance(gate, AgentPauseGate):
        gate = gate_for(parent)
    try:
        child._pause_gate = gate
    except Exception:  # health: allow BLE001 -- see gate_for
        logger.debug("could not share a pause gate with %r", type(child).__name__)
    return gate


def park(agent, *, boundary: str = "execution") -> bool:
    """Block at *boundary* while the agent's session is frozen. True when a Stop interrupted it.

    An agent with no gate (CLI, tests) is never frozen by definition, so the call is free and
    every existing interrupt seam keeps deciding — a gate only ever exists because the gateway
    bound this session's freeze capability.
    """
    gate = getattr(agent, "_pause_gate", None)
    if not isinstance(gate, AgentPauseGate):
        return False
    return gate.wait(agent, boundary=boundary) == INTERRUPTED
