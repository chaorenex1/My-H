"""Shared setup for the P2 panel dispatcher tests.

One live ``SessionAuthority`` + one ``AuthorityConnection`` per test, with a local session ``s``
whose source is patched by the test that needs a workspace/terminal policy.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from gateway.session_policy import LocalSessionPolicy
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


def policy(home, *, terminal=None, source="test"):
    return LocalSessionPolicy(
        source=source, platform="local", cwd=str(home), model="test-model", toolsets=(),
        config_json="{}", request_json="{}",
        terminal_json=json.dumps(terminal or {"TERMINAL_ENV": "local"}))


@asynccontextmanager
async def panel_authority(db_dir):
    with SessionDB(db_path=db_dir / "state.db") as db:
        db.create_session("s", source="test")
        epoch = begin_runtime_epoch(db, instance_id="current")
        authority = SessionAuthority(
            SimpleNamespace(_draining=False, session_authorities=()),
            profile_id=str(db_dir), instance_id="current", db=db, epoch=epoch)
        authority.sessions["s"] = LiveSession(None, "route")
        connection = AuthorityConnection(
            authority, SimpleNamespace(write=lambda frame: None), {"user_id": "owner"})
        connection.subscriptions["s"] = "viewer"
        try:
            yield connection
        finally:
            await connection.close()
            authority.db.close()
