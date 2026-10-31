"""``session.lease``: which requested session ids hold a live cross-process lease.

The lease is the registry ``hermes_cli.active_sessions`` maintains (one file per profile home,
shared by CLI, desktop/TUI and gateway). The in-memory ``authority.sessions`` is NOT a lease: a
session open in this process may hold none, and a lease may be held by another process. So the
answer always comes from the registry, read ``strict`` so unprovable liveness is reported as
``unknown`` rather than silently guessed free.
"""

from __future__ import annotations

import asyncio
from functools import partial

from gateway.session_promoted import profile_home, require
from hermes_state_runtime import RuntimeStoreError


def handlers(connection):
    return {"session.lease": partial(lease, connection)}


def _requested_ids(params) -> list[str]:
    profile = params.get("profile")
    if profile is not None and not isinstance(profile, str):
        raise RuntimeStoreError("invalid_params", field="profile")
    ids = params.get("session_ids")
    if (not isinstance(ids, list)
            or any(not isinstance(session_id, str) or not session_id for session_id in ids)):
        raise RuntimeStoreError("invalid_params", field="session_ids")
    return ids


async def lease(connection, ref, params):
    ids = _requested_ids(params)
    require(connection, "session:read")
    home = profile_home(connection)
    from hermes_cli.active_sessions import ActiveSessionRegistryError, active_session_registry_snapshot

    try:
        entries = await asyncio.to_thread(
            active_session_registry_snapshot, registry_home=home, strict=True)
    except ActiveSessionRegistryError:
        # The registry could not prove liveness, so no requested id may be reported as free.
        return {"held": [], "unknown": list(ids)}
    held = {str(entry.get("session_id") or "") for entry in entries}
    # A requested id absent from a healthy registry is simply not held: unknown is only about probing.
    return {"held": [session_id for session_id in ids if session_id in held], "unknown": []}
