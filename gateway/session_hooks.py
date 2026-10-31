"""Extensions panel: ``hooks.list`` / ``hooks.toggle`` over the profile's configured shell hooks.

The data source is the same ``hooks:`` block ``hermes hooks`` reads. A hook's enabled state is its
allowlist approval (plus a global auto-accept opt-in), because that is what actually decides whether
the gateway runs it; toggling approves or revokes exactly one ``(event, command)`` pair.
"""

from __future__ import annotations

import logging
from functools import partial

from gateway.session_promoted import require, scoped
from hermes_state_runtime import RuntimeStoreError

logger = logging.getLogger(__name__)


def handlers(connection):
    return {
        "hooks.list": partial(list_hooks, connection),
        "hooks.toggle": partial(toggle_hook, connection),
    }


def _validate_profile_params(params, allowed):
    if set(params) - allowed:
        raise RuntimeStoreError("invalid_params")
    profile = params.get("profile")
    if profile is not None and not isinstance(profile, str):
        raise RuntimeStoreError("invalid_params", field="profile")
    session_id = params.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise RuntimeStoreError("invalid_params", field="session_id")


def _hook_name(event: str, command: str) -> str:
    return f"{event}:{command}"


def _effective_enabled(shell_hooks, accept, approved, event, command):
    return bool(accept or (event, command) in approved)


def _rows(cfg):
    from agent import shell_hooks

    specs = shell_hooks.iter_configured_hooks(cfg)
    accept = shell_hooks._resolve_effective_accept(cfg, False)
    approved = shell_hooks.allowlisted_pairs()
    rows = []
    seen = set()
    for spec in specs:
        key = (spec.event, spec.command)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "name": _hook_name(spec.event, spec.command),
            "event": spec.event,
            "enabled": _effective_enabled(shell_hooks, accept, approved, spec.event, spec.command),
            "scope": "profile",
            "source": "config.yaml",
            "description": f"Runs {spec.command} on {spec.event}",
        })
    return rows


async def list_hooks(connection, ref, params):
    _validate_profile_params(params, {"profile", "session_id"})
    require(connection, "session:read")

    def read():
        from hermes_cli.config import load_config

        return {"hooks": _rows(load_config())}

    return await scoped(connection, read)


async def toggle_hook(connection, ref, params):
    _validate_profile_params(params, {"profile", "session_id", "name", "enabled", "request_id"})
    name = params.get("name")
    enabled = params.get("enabled")
    request_id = params.get("request_id")
    if not isinstance(name, str) or not name or ":" not in name:
        raise RuntimeStoreError("invalid_params", field="name")
    if not isinstance(enabled, bool):
        raise RuntimeStoreError("invalid_params", field="enabled")
    if not isinstance(request_id, str) or not request_id:
        raise RuntimeStoreError("invalid_params", field="request_id")
    require(connection, "session:control")
    event, _, command = name.partition(":")
    if not event or not command:
        raise RuntimeStoreError("invalid_params", field="name")

    def write():
        from agent import shell_hooks
        from hermes_cli.config import load_config

        cfg = load_config()
        configured = {(spec.event, spec.command) for spec in shell_hooks.iter_configured_hooks(cfg)}
        if (event, command) not in configured:
            raise RuntimeStoreError("invalid_params", field="name", detail="no such configured hook")
        shell_hooks.set_approval(event, command, enabled)
        try:
            shell_hooks.sync_from_config(load_config)
        except Exception:  # a live registration failure must not fail the allowlist write
            logger.warning("hooks.toggle: live re-registration failed", exc_info=True)
        accept = shell_hooks._resolve_effective_accept(cfg, False)
        state = _effective_enabled(shell_hooks, accept, shell_hooks.allowlisted_pairs(), event, command)
        return {"name": name, "enabled": state}

    return await scoped(connection, write)
