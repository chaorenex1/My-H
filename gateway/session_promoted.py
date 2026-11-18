"""Session-bound sidecar verbs the session authority serves itself.

Everything else a client panel calls is served in-process by the legacy adapter
(``tui_gateway/ws_legacy_fallback.py``) with the caller's profile bound; re-porting those handlers
would be a second implementation of the same logic. These modules exist only where the sidecar
handler reads its own ``_sessions`` table, which never holds an authority session.

Every module in ``_FAMILIES`` follows one shape::

    def handlers(connection) -> dict[str, Callable[[SessionRef, dict], Awaitable[dict]]]

and answers with the plain result body of the method's declared contract (the authority's
dispatcher adds the JSON-RPC envelope, validates the params and the result). The legacy sidecar
handlers stay registered for older clients; a new client's call never reaches them because these
handlers answer first (``tui_gateway/ws.py`` falls back only on ``-32601``).

Three rules every family follows:

* Capability first — ``require(connection, "session:read" | "session:control" | "session:submit")``.
  These families have no session of their own, so ``SessionAuthority.authorize`` (which would
  require a live session row) is not the gate; the profile match is checked here instead.
* Profile scope — work runs off-loop inside ``scoped(connection, ...)``: this owner's HOME, its
  ``.env`` secret scope and its terminal policy together, exactly like ``session_discovery``.
* Failures are refusals — a ``RuntimeStoreError`` becomes the gateway's error frame. Domain
  failures are ``invalid_params`` (user-correctable) or ``storage_unavailable`` (I/O); billing
  keeps its typed fail-open envelope.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from hermes_state_runtime import RuntimeStoreError

_FAMILIES = (
    "session_promoted_usage",
    "session_promoted_connectors",
    "session_promoted_attachments",
)


def handlers(connection) -> dict[str, Callable[[Any, dict], Any]]:
    """Method → handler for every promoted family (imported lazily: startup budget)."""
    import importlib

    out: dict[str, Callable[[Any, dict], Any]] = {}
    for name in _FAMILIES:
        module = importlib.import_module(f"gateway.{name}")
        for method, handler in module.handlers(connection).items():
            if method in out:  # a family owning a name twice would silently pick one
                raise RuntimeError(f"promoted method declared twice: {method}")
            out[method] = handler
    return out


def require(connection, capability: str) -> None:
    """The session-less gate: this connection's grant covers the verb for THIS owner profile.

    ``SessionAuthority.authorize`` additionally requires a live session row, which panel verbs do
    not have; the two checks that matter here (grant + profile) are the same ones the sidecar's
    ``_profile_scoped`` enforced, now fail-closed against a foreign authority.
    """
    actor = connection.actor
    if capability not in (getattr(actor, "capabilities", ()) or ()):
        raise RuntimeStoreError("permission_denied")
    if getattr(actor, "profile_id", None) != getattr(connection.authority, "profile_id", None):
        raise RuntimeStoreError("permission_denied")


def profile_home(connection) -> Path:
    """The owning profile's home. A symbolic test peer id falls back to the scoped home."""
    home = Path(str(connection.authority.profile_id))
    if home.is_absolute() and home.is_dir():
        return home
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


async def scoped(connection, fn: Callable[[], Any]) -> Any:
    """Run *fn* off the event loop under this owner profile's full runtime scope.

    ``owner_scope`` is the documented seam: a single-profile gateway keeps its ambient scope (a
    ``--safe-mode`` bypass launch must not read the profile's YAML), a multiplexing owner binds
    this authority's HOME + secrets + terminal policy, and a standalone gateway that already hosts
    a second home binds the launch scope. ``profile_home`` stays for path building only.
    """
    authority = connection.authority

    def run():
        from gateway.session_authorities import owner_scope

        with owner_scope(authority, hydrate_secrets=True):
            return fn()

    return await asyncio.to_thread(run)
