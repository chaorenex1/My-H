"""``connectors.list`` is the one connector verb the in-process legacy adapter cannot serve.

The account-owned branch (the Connectors page) is profile-scoped and already runs through
``dispatch_legacy``; the session-owned branch asks the session's ``manage_connections`` tool for its
state, and the sidecar resolves that session from its own ``_sessions`` table (never an authority
session). This module serves both branches so the method has one wire and one precedence: a live
session's state, or the profile's managed connections.
"""

from __future__ import annotations

import contextlib
from functools import partial

from gateway.session_connections import ConnectorRefusal
from gateway.session_promoted import require, scoped
from tui_gateway.contracts.connectors import (
    ConnectorErrorReason,
    ConnectorsListParams,
)


def handlers(connection):
    return {"connectors.list": partial(list_connectors, connection)}


async def list_connectors(connection, ref, params):
    owner = params.get("owner") if isinstance(params.get("owner"), dict) else None
    if owner is None or owner.get("type") != "session":
        require(connection, "session:read")
        return await scoped(connection, lambda: _account_list(params))
    return await _session_list(connection, ref, params)


# ── account-owned: the Connectors page ────────────────────────────────────────────────────────


@contextlib.contextmanager
def _desktop_page_vars():
    """The session vars the desktop Connectors page binds (the sidecar's ``_desktop_page_vars``)."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_constants import get_hermes_home, profile_name_for_home

    tokens = set_session_vars(source="desktop", profile=profile_name_for_home(get_hermes_home()) or "default",
                              cron_session="")
    try:
        yield
    finally:
        clear_session_vars(tokens)


def _account_list(params):
    from pydantic import ValidationError

    from tui_gateway.connector_payload import connector_ui_payload
    from tools.connectors import connectors_available
    from tools.connectors.gateway.errors import GatewayAuthError
    from tools.connectors.gateway.wire import ConnectorListItem
    from tools.connectors.managed import managed_client
    from tools.managed_tool_gateway import read_nous_access_token
    from tui_gateway.connector_refusals import auth_refusal

    with _desktop_page_vars():
        if not connectors_available():
            # ``status`` never refuses a page without connectors: it says there are none.
            return {"available": False, "connectors": []}
        try:
            ConnectorsListParams.model_validate(params)
        except ValidationError as exc:
            raise ConnectorRefusal(
                4000, ConnectorErrorReason.invalid_params, "Connector parameters are invalid.") from exc
        try:
            rows = managed_client(creates_guest=False).list_connectors() if read_nous_access_token() else []
        except GatewayAuthError as exc:
            raise ConnectorRefusal(*auth_refusal(exc)) from exc
        except Exception as exc:  # health: allow BLE001 -- the sidecar's one named fallback
            raise ConnectorRefusal(
                5034, ConnectorErrorReason.invalid_connector_response,
                "Connector service returned an invalid response.") from exc
        from hermes_cli.free_tier_offer import note_connected_apps_seen

        note_connected_apps_seen(rows)
        validated = [ConnectorListItem.model_validate(row).model_dump(mode="json", by_alias=False)
                     for row in rows]
        return {"available": True, "connectors": connector_ui_payload(validated)}


# ── session-owned: that session's ``manage_connections`` state ────────────────────────────────


async def _session_list(connection, ref, params):
    from gateway.session_connections import _require_connectors, _session_context, _session_toolsets
    from gateway.session_contract import SessionRef

    sid = params["owner"].get("session_id")
    if not isinstance(sid, str) or not sid or sid not in connection.authority.sessions:
        raise ConnectorRefusal(4001, ConnectorErrorReason.not_owner,
                               "session not found or not owned by this transport")
    session_ref = SessionRef(connection.authority.profile_id, sid)
    connection.authority.authorize(connection.actor, session_ref, "session:read")
    try:
        _require_connectors(connection.authority, session_ref)
    except ConnectorRefusal:
        return {"available": False, "connectors": []}
    enabled, disabled = _session_toolsets(connection.authority, session_ref)
    live = connection.authority.sessions[sid]

    def read():
        import json
        import uuid

        import model_tools
        from tui_gateway.connector_payload import connector_ui_payload
        from tools.connectors.gateway.wire import ConnectorListItem

        with _session_context(connection.authority, session_ref):
            raw = model_tools.handle_function_call(
                "manage_connections", {"action": "status"},
                task_id=live.route,
                session_id=getattr(connection.authority.agent(session_ref), "session_id", None) or live.route,
                tool_call_id=f"connector-ui-{uuid.uuid4().hex}",
                enabled_toolsets=enabled, disabled_toolsets=disabled)
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict) or "error" in data:
            raise ConnectorRefusal(5034, ConnectorErrorReason.connector_request_failed,
                                   "Connector request failed. Try again explicitly.")
        rows = data.get("connectors")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ConnectorRefusal(5034, ConnectorErrorReason.invalid_connector_response,
                                   "Connector service returned an invalid response.")
        validated = [ConnectorListItem.model_validate(row).model_dump(mode="json", by_alias=False)
                     for row in rows]
        return {"available": True, "connectors": connector_ui_payload(validated)}

    return await scoped(connection, read)

