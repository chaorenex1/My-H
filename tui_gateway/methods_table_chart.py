"""Local auto-graph chart planning (ADR-12 D6, ADR-17 P5).

``table_chart.plan`` is the local viewer's proxy onto :mod:`agent.auto_graph`:
the planner owns the judge questions and the answer-to-``ChartPlan`` mapping, so
this handler only scopes the payload and keeps failures structured. It is never
exposed to remote messaging platforms, and it persists nothing.
"""

from __future__ import annotations

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

# JSON-RPC error code 5100 = judgment failure, shared with the judgment methods.
# Kept as a literal inside handler bodies: handlers are rebound onto server.py's globals.


@method("table_chart.plan")
@_profile_scoped
def _(rid, params: dict) -> dict:
    """Plan one table's chart, falling back to the caller's local guess."""
    try:
        from agent.auto_graph import plan_table_chart

        payload = {k: v for k, v in params.items() if k != "profile"}
        return _ok(rid, plan_table_chart(payload))
    except Exception as e:  # health: allow BLE001 -- the RPC answer must stay structured
        return _err(rid, 5100, str(e))


def register(server) -> None:
    """Bind this module's handlers onto ``server``'s globals and registry."""
    _registry.install(server)
