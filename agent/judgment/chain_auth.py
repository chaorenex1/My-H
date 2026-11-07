"""Credential refresh/rotation for a rejected native judgment candidate (ADR-17 D4).

401/403 on a pooled credential refreshes it (OAuth grants) or rotates to a sibling API
key, then the chain retries the candidate once. TypeSafe and Workers AI are environment
only, so they have nothing to refresh; the chain cools them down instead.

This module returns a rotated :class:`NativeRoute`; building the judge remains the
chain's job so the candidate class is constructed at exactly one place.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from agent.judgment.native import NativeRoute
from agent.judgment.types import Judge

logger = logging.getLogger(__name__)


def rotate_route_credentials(route: NativeRoute, status: int) -> str | None:
    """Refresh/rotate a pooled credential after 401/403; ``None`` when there is no sibling."""
    pool_key = {
        "openrouter-decisions": "openrouter",
        "openai-decisions": "openai-api",
    }.get(route.api)
    if pool_key is None:
        return None
    try:
        from agent.credential_pool import load_pool

        pool = load_pool(pool_key)
        if not pool.has_credentials():
            return None
        # A 401 on a refreshable OAuth grant is fixed by refreshing it; an API-key
        # pool has nothing to refresh, so rotation is the only move.
        refreshed = pool.try_refresh_matching(api_key_hint=route.api_key)
        if refreshed is not None:
            key = str(getattr(refreshed, "runtime_api_key", "")
                      or refreshed.access_token or "")
            if key and key != route.api_key:
                return key
        entry = pool.mark_exhausted_and_rotate(
            status_code=status, api_key_hint=route.api_key, model=route.model)
        if entry is None:
            return None
        key = str(getattr(entry, "runtime_api_key", "") or entry.access_token or "")
        if not key or key == route.api_key:
            return None
        return key
    except Exception:
        logger.debug("%s credential rotation failed", pool_key, exc_info=True)
        return None


def rotate_route(judge: Judge, status: int) -> NativeRoute | None:
    """``judge``'s route with a refreshed credential, or ``None`` when nothing rotated."""
    route = getattr(judge, "route", None)
    if not isinstance(route, NativeRoute):
        return None
    key = rotate_route_credentials(route, status)
    if not key:
        return None
    return replace(route, api_key=key)
