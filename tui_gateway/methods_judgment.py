"""Local typed-judgment JSON-RPC handlers (ADR-17 D6).

Thin adapters over ``agent/judgment/service.py``: the service owns schema validation,
budget enforcement, accounting, and error types, so the proxy must not grow its own
routing or caching. Never exposed to remote messaging platforms.
"""

from __future__ import annotations

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

# JSON-RPC error code 5100 = judgment failure (validation errors keep 4000 from the dispatcher).
# Kept as a literal inside handler bodies: handlers are rebound onto server.py's globals.


@method("judgment.judge")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Answer one judgment request with the same contract as the Python/sandbox API."""
    try:
        from agent.judgment.service import judge, result_to_json

        payload = {k: v for k, v in params.items() if k != "profile"}
        return _ok(rid, result_to_json(judge(payload)))
    except Exception as e:  # health: allow BLE001 -- the RPC answer must stay structured
        return _err(rid, 5100, str(e))


@method("judgment.judge_batch")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Run a batch judgment; per-item failures are data, not RPC errors."""
    try:
        from agent.judgment.service import batch_to_json, judge_batch

        payload = {k: v for k, v in params.items() if k != "profile"}
        return _ok(rid, batch_to_json(judge_batch(payload)))
    except Exception as e:  # health: allow BLE001 -- the RPC answer must stay structured
        return _err(rid, 5100, str(e))


def register(server) -> None:
    """Bind this module's handlers onto ``server``'s globals and registry."""
    _registry.install(server)
