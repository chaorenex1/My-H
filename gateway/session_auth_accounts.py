"""``auth.accounts``: the non-secret identity of a provider's stored credentials.

The store is the provider's credential pool (``agent.credential_pool``). Only identity metadata
leaves this module — the pool id, provider, label, source and auth type. An access/refresh token is
never copied into the result, and ``detail`` is a human hint (label, else source) only.
"""

from __future__ import annotations

import asyncio
from functools import partial

from gateway.session_promoted import require
from hermes_state_runtime import RuntimeStoreError


def handlers(connection):
    return {"auth.accounts": partial(accounts, connection)}


def _account(entry, active_id) -> dict:
    auth_type = str(getattr(entry, "auth_type", "") or "").lower()
    label = str(getattr(entry, "label", "") or "")
    return {
        "credential_id": str(entry.id),
        "provider": str(entry.provider),
        "label": label,
        "detail": label or str(getattr(entry, "source", "") or ""),
        "type": "oauth" if "oauth" in auth_type else "api_key",
        "active": entry.id == active_id,
    }


async def accounts(connection, ref, params):
    provider = params.get("provider_id")
    profile = params.get("profile")
    if not isinstance(provider, str) or not provider.strip():
        raise RuntimeStoreError("invalid_params", field="provider_id")
    if profile is not None and not isinstance(profile, str):
        raise RuntimeStoreError("invalid_params", field="profile")
    require(connection, "session:read")

    def read():
        from agent.credential_pool import load_pool

        pool = load_pool(provider.strip())
        active_id = getattr(pool.peek(), "id", None)
        return [_account(entry, active_id) for entry in pool.entries()]

    # ``load_pool`` reads (and may heal) the profile's auth store; the whole read stays off the loop.
    return {"accounts": await asyncio.to_thread(read)}
