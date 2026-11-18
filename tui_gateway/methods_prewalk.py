"""Prewalk handoff JSON-RPC handlers (ADR-12 D5 / ADR-17 P5).

Thin session-scoped adapters over ``agent/prewalk.py``: the coordinator owns the state machine,
persistence and the one-shot switch, so this module only resolves the live session, maps the
result to the declared contract, and emits the state-change event. Local authority only: no
messaging platform may arm a session's model handoff.
"""

from __future__ import annotations

import logging

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

logger = logging.getLogger(__name__)

# 4002: a selector the client sent does not resolve; 5103: the coordinator itself failed.
_INVALID_TARGET = 4002
_PREWALK_FAILED = 5103


def _prewalk_agent(rid, params: dict):
    session, err = _sess_nowait(params, rid)
    if err:
        return None, None, err
    wait_err = _wait_agent(session, rid)
    if wait_err:
        return None, None, wait_err
    agent = session.get("agent")
    if agent is None:
        return None, None, _err(rid, 4001, "session agent is not ready")
    return agent, session, None


def _install_publish(agent, sid: str) -> None:
    """Wire the coordinator's event hook to this session's transport.

    The callback closes over this handler's (server-rebound) globals, so ``_emit`` resolves to the
    gateway's live emitter rather than a stale module namespace.
    """
    def _publish(event: str, payload: dict) -> None:
        if event == "prewalk.handoff":
            return _emit("prewalk.handoff", sid, payload)
        return _emit("prewalk.armed", sid, payload)

    try:
        agent.prewalk_publish = _publish
    except Exception:  # health: allow BLE001 -- a read-only agent still answers the RPC
        logger.debug("prewalk: could not install the event hook", exc_info=True)


def _handle(rid, params: dict, action: str) -> dict:
    agent, _session, err = _prewalk_agent(rid, params)
    if err:
        return err
    from agent import prewalk

    sid = str(params.get("session_id") or "")
    _install_publish(agent, sid)
    try:
        if action == "status":
            return _ok(rid, prewalk.status(agent))
        if action == "off":
            return _ok(rid, prewalk.disarm(agent))
        kwargs = {}
        if params.get("target"):
            kwargs["into"] = params["target"]
        if params.get("from"):
            kwargs["from_"] = params["from"]
        state = prewalk.arm(agent, **kwargs) if action == "arm" \
            else prewalk.restart(agent, **kwargs)
        return _ok(rid, state)
    except ValueError as exc:
        return _err(rid, _INVALID_TARGET, str(exc))
    except Exception as exc:  # health: allow BLE001 -- the RPC answer must stay structured
        logger.debug("prewalk.%s failed", action, exc_info=True)
        return _err(rid, _PREWALK_FAILED, str(exc))


@method("prewalk.status")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Read the session's one-shot handoff state."""
    return _handle(rid, params, "status")


@method("prewalk.arm")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Arm one handoff (explicit arming works while the feature is disabled)."""
    return _handle(rid, params, "arm")


@method("prewalk.restart")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Return to the planning model/thinking, then arm a fresh handoff."""
    return _handle(rid, params, "restart")


@method("prewalk.off")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Disarm the pending handoff."""
    return _handle(rid, params, "off")


def register(server) -> None:
    """Bind this module's helpers and handlers onto ``server``'s globals and registry."""
    bind_module(globals(), server, skip=("_",))
