"""Lease-liveness and credential-account panel contracts.

``session.lease`` answers which requested session ids hold a live cross-process lease (the registry
``hermes_cli.active_sessions`` maintains), and which could not be probed at all. ``auth.accounts``
lists the non-secret identity of a provider's stored credentials: never a token.

Handlers: ``gateway/session_lease.py`` and ``gateway/session_auth_accounts.py``.
"""

from __future__ import annotations

from pydantic import Field

from .base import Params, Result
from .registry import canonical_method


class SessionLeaseParams(Params):
    session_ids: list[str]
    profile: str | None = None


class SessionLeaseResult(Result):
    held: list[str] = Field(default_factory=list)
    # Liveness could not be probed, so "not held" is unknowable — every requested id lands here.
    unknown: list[str] = Field(default_factory=list)


canonical_method("session.lease", params=SessionLeaseParams, result=SessionLeaseResult,
                 doc="Requested session ids that hold a live cross-process lease, and those whose "
                     "liveness could not be probed.")


class AuthAccountsParams(Params):
    provider_id: str
    profile: str | None = None


class AuthAccount(Result):
    credential_id: str
    provider: str
    label: str = ""
    detail: str = ""
    type: str = "api_key"
    active: bool = False


class AuthAccountsResult(Result):
    accounts: list[AuthAccount] = Field(default_factory=list)


canonical_method("auth.accounts", params=AuthAccountsParams, result=AuthAccountsResult,
                 doc="Non-secret identity of the provider's stored credentials (label/source only; never a token).")
