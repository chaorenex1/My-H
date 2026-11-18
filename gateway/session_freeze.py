"""ADR-1: ``session.freeze`` — cooperative freeze of one session's running execution tree.

The switch itself lives on the agents (``agent/pause_gate.py``); this module owns the
per-authority binding, the generation latch and the wire answer. Admission is not touched
(``agent.estop`` owns that), so freezing an idle session is a no-op for later prompts.

One process serves many profiles: the state is keyed by session id on the owning authority,
and the turn's execution generation is mandatory so a late ``frozen:false`` from a changed
connection can never release a newer turn.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from hermes_state_runtime import RuntimeStoreError

logger = logging.getLogger(__name__)

FIELDS = frozenset({"session_id", "profile", "execution_generation", "frozen", "request_id"})
# Receipts kept per session for a retried ``request_id``: a client retries a handful at most.
_RECEIPTS_PER_SESSION = 16


@dataclass
class FreezeBinding:
    """One frozen execution generation: its gate (in-process) or control-frame sender (managed)."""

    generation: int
    frozen_at: float
    gate: Any = None
    notify: Any = None

    def parked_agents(self) -> int | None:
        """In-process: agents parked now. Managed (v1): never reported across the process seam."""
        if self.gate is None:
            return None
        try:
            return self.gate.parked()
        except Exception:  # health: allow BLE001 -- a status read never fails the RPC
            return None

    def state(self) -> dict:
        return {
            "frozen": True,
            "frozen_at": self.frozen_at,
            "execution_generation": self.generation,
            "parked_agents": self.parked_agents(),
        }


def freezes(authority) -> dict:
    table = getattr(authority, "_freezes", None)
    if table is None:
        table = authority._freezes = {}
    return table


def _receipts(authority) -> dict:
    table = getattr(authority, "_freeze_receipts", None)
    if table is None:
        table = authority._freeze_receipts = {}
    return table


def _senders(authority) -> dict:
    """Managed sessions: ``session -> (generation, send)`` for the worker's pause control frames."""
    table = getattr(authority, "_freeze_senders", None)
    if table is None:
        table = authority._freeze_senders = {}
    return table


def freeze_state(authority, session_id: str) -> dict | None:
    """``SessionControlSnapshot.freeze``; ``None`` when this session is not frozen."""
    binding = freezes(authority).get(session_id)
    return binding.state() if binding is not None else None


def _call(notify, paused: bool) -> None:
    try:
        notify(paused)
    except Exception:  # health: allow BLE001 -- a lost worker is settled by its own fence
        logger.debug("managed pause frame failed", exc_info=True)


def _release(authority, session_id: str) -> FreezeBinding | None:
    binding = freezes(authority).pop(session_id, None)
    if binding is None:
        return None
    if binding.gate is not None:
        binding.gate.resume()
    elif binding.notify is not None:
        _call(binding.notify, False)
    return binding


def release_freeze(authority, session_id: str) -> bool:
    """Drop a session's freeze (resume every parker). The execution generation ended or lost its viewer."""
    return _release(authority, session_id) is not None


async def release_and_publish(authority, session_id: str) -> bool:
    """Release a generation's freeze and repaint viewers: an open pause modal closes on this update."""
    if not release_freeze(authority, session_id):
        return False
    from gateway.session_control_actions import publish_async

    try:
        await publish_async(authority, session_id, only_if_changed=True)
    except Exception:  # health: allow BLE001 -- the release itself already landed
        logger.debug("session.control.update after unfreeze failed", exc_info=True)
    return True


def bind_freeze(authority, session_id: str, generation: int, agent) -> None:
    """The in-process turn adopts its agent: attach the gate, and apply a freeze latched meanwhile.

    Deliberately NOT part of ``adopt_agent``: a managed worker is adopted as a ``ManagedWorker``,
    not an agent, and reaches this gate through ``bind_freeze_sender``'s control frames instead.
    """
    from agent.pause_gate import gate_for

    gate = gate_for(agent)
    binding = freezes(authority).get(session_id)
    if binding is None or binding.generation != generation:
        return
    binding.gate = gate
    gate.pause()  # idempotent: keeps the original frozen_at on the binding


def bind_freeze_sender(authority, session_id: str, generation: int, send) -> None:
    """Managed worker: deliver freezes as ``{"type": "pause", "paused": bool}`` frames (ADR-1 v1)."""
    _senders(authority)[session_id] = (generation, send)
    binding = freezes(authority).get(session_id)
    if binding is not None and binding.generation == generation:
        binding.notify = send
        _call(send, True)


def unbind_freeze_sender(authority, session_id: str) -> None:
    """The worker is gone: a later freeze of this session must not target its dead pipe."""
    _senders(authority).pop(session_id, None)


def _reply(binding: FreezeBinding | None, *, generation: int | None, running: bool) -> dict:
    if binding is None:
        return {"frozen": False, "frozen_at": None, "held_seconds": None,
                "execution_generation": generation, "parked_agents": 0, "running": running}
    return {
        "frozen": True,
        "frozen_at": binding.frozen_at,
        "held_seconds": max(0.0, round(time.time() - binding.frozen_at, 3)),
        "execution_generation": binding.generation,
        "parked_agents": binding.parked_agents(),
        "running": running,
    }


def _validate(params: dict) -> tuple[int, bool, str | None]:
    if set(params) - FIELDS or not isinstance(params.get("session_id"), str) or not params["session_id"]:
        raise RuntimeStoreError("invalid_params")
    generation = params.get("execution_generation")
    if type(generation) is not int:
        raise RuntimeStoreError("invalid_params", field="execution_generation")
    frozen = params.get("frozen", True)
    if type(frozen) is not bool:
        raise RuntimeStoreError("invalid_params", field="frozen")
    request_id = params.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 200):
        raise RuntimeStoreError("invalid_params", field="request_id")
    return generation, frozen, request_id


def _remember(authority, session_id: str, generation: int, request_id: str | None, reply: dict) -> None:
    if request_id is None:
        return
    cache = _receipts(authority).setdefault(session_id, {})
    cache[(generation, request_id)] = dict(reply)
    while len(cache) > _RECEIPTS_PER_SESSION:
        cache.pop(next(iter(cache)))


def _gate_for_adopted(authority, session_id: str, generation: int):
    """The adopted agent's gate, when this generation already bound one (managed: none)."""
    from agent.pause_gate import AgentPauseGate

    live = authority.sessions.get(session_id)
    if live is None:
        return None
    with live.event_stream.lock:
        adopted_generation, agent = authority.adopted.get(session_id, (None, None))
    if adopted_generation != generation:
        return None
    gate = getattr(agent, "_pause_gate", None)
    return gate if isinstance(gate, AgentPauseGate) else None


def _publish(authority, session_id: str) -> None:
    from gateway.session_control_actions import publish

    try:
        publish(authority, session_id, only_if_changed=True)
    except Exception:  # health: allow BLE001 -- the reply already carries the new state
        logger.debug("session.control.update after freeze failed", exc_info=True)


def handlers(connection) -> dict:
    from functools import partial

    return {"session.freeze": partial(freeze, connection)}


async def freeze(connection, ref, params: dict) -> dict:
    """``session.freeze``: engage/release the session's cooperative freeze for one generation."""
    generation, frozen, request_id = _validate(params)
    authority = connection.authority
    authority.authorize(connection.actor, ref, "session:control")
    handle = authority._handle(ref)
    if request_id is not None:
        cached = _receipts(authority).get(ref.session_id, {}).get((generation, request_id))
        if cached is not None:
            return {"freeze": dict(cached)}

    if handle.execution_generation != generation:
        if frozen:
            # A reconnect's late freeze must not land on a newer turn; the client re-reads
            # ``session.info`` and retries once with the fresh generation.
            exc = RuntimeStoreError("stale_generation", field="execution_generation",
                                    detail="the running execution generation changed")
            exc.execution_generation = handle.execution_generation
            raise exc
        # The turn whose freeze this released has ended: nothing to undo.
        reply = _reply(None, generation=handle.execution_generation,
                       running=handle.execution_state == "running")
        return {"freeze": reply}

    running = handle.execution_state == "running"
    if not running:
        # Idle session: no state, and no admission side effect (ESTOP owns "nothing may start").
        _release(authority, ref.session_id)
        reply = _reply(None, generation=generation, running=False)
        _remember(authority, ref.session_id, generation, request_id, reply)
        return {"freeze": reply}

    binding = freezes(authority).get(ref.session_id)
    if binding is not None and binding.generation != generation:
        _release(authority, ref.session_id)
        binding = None

    if frozen:
        if binding is None:
            binding = FreezeBinding(generation=generation, frozen_at=time.time())
            gate = _gate_for_adopted(authority, ref.session_id, generation)
            if gate is not None:
                binding.gate = gate
            sender = _senders(authority).get(ref.session_id)
            if sender is not None and sender[0] == generation:
                binding.notify = sender[1]
            freezes(authority)[ref.session_id] = binding
        if binding.gate is not None:
            binding.gate.pause()
        elif binding.notify is not None:
            _call(binding.notify, True)
        # Else: the agent has not adopted yet; bind_freeze / bind_freeze_sender applies the latch.
        reply = _reply(binding, generation=generation, running=True)
    else:
        if binding is not None:
            _release(authority, ref.session_id)
        reply = _reply(None, generation=generation, running=True)

    _remember(authority, ref.session_id, generation, request_id, reply)
    _publish(authority, ref.session_id)
    return {"freeze": reply}
