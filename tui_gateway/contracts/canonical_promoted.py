"""Canonical declarations for the sidecar methods the authority must serve itself.

Most sidecar panel verbs are served in-process by the legacy adapter
(``tui_gateway/ws_legacy_fallback.py``) with the ticket's profile bound; the gateway does not
re-implement them. The handful below are different: their sidecar handler resolves a session from
its own ``_sessions`` table, which never holds an authority session, so the authority owns them.

Their wire is **unchanged**: each canonical entry reuses the ``Params``/``Result`` models the
sidecar declared in ``METHODS``. The canonical table adds the closed key-set check before the
handler runs, without widening the legacy ``RpcMethods`` map.

Add a name here only when ``gateway/session_promoted.handlers`` answers it; the wire-contract test
holds both directions.
"""

from __future__ import annotations

from .registry import METHODS, canonical_method

#: Session-bound sidecar verbs the authority serves itself.
PROMOTED = (
    "session.usage",
    "connectors.list",  # the session-owned branch
    "image.attach",
    "pdf.attach",
)


def _declare() -> None:
    for name in PROMOTED:
        legacy = METHODS.get(name)
        if legacy is None:
            raise RuntimeError(f"promoted method has no legacy declaration: {name}")
        canonical_method(name, params=legacy.params, result=legacy.result, doc=legacy.doc)


_declare()
