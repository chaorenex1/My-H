"""The one session-bound usage verb the TUI gateway cannot serve for an authority session.

``session.usage`` reads the sidecar's own ``_sessions`` table (``methods_session._session_method``),
which never holds an authority session; ``session.stats`` already serves the same dict on the
authority. Everything else the usage/billing panels call (``billing.*``, ``subscription.*``,
``usage.bars``) is profile-scoped and is served in-process by the legacy adapter
(``tui_gateway/ws_legacy_fallback.py``) — no second implementation here.
"""

from __future__ import annotations

from functools import partial

from gateway.session_promoted import require, scoped


def handlers(connection):
    return {"session.usage": partial(session_usage, connection)}


async def session_usage(connection, ref, params):
    await connection.authority.resolve(connection.actor, ref)
    require(connection, "session:read")

    def read():
        from agent.account_usage import nous_credits_usage_fields
        from gateway.session_usage import usage_stats

        agent = connection.authority.agent(ref)
        stats = usage_stats(agent)
        stats.update(nous_credits_usage_fields(agent))
        return stats

    return await scoped(connection, read)
