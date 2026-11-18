"""Default cache factory and process-local registry."""

from __future__ import annotations

import atexit
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .api import Cache
from .policy import CachePolicy
from .providers.sqlite import SQLiteProvider
from .serializers import JSONSerializer, Serializer

logger = logging.getLogger(__name__)

_REGISTRY: dict[tuple[Any, ...], Cache] = {}
_REGISTRY_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


def default_cache_path(home: Path | None = None) -> Path:
    """Return the profile's cache database path without creating it."""
    from hermes_constants import get_hermes_home

    root = Path(home) if home is not None else get_hermes_home()
    return root / "cache" / "cache.db"


def _ensure_atexit() -> None:
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    atexit.register(close_all_caches)
    _ATEXIT_REGISTERED = True


def get_cache(
    *,
    home: Path | None = None,
    policy: CachePolicy | None = None,
    serializer: Serializer | None = None,
) -> Cache:
    """Return the process-local cache for the active profile.

    The default provider is constructed here, so business code never imports
    ``SQLiteProvider``. Different policy/serializer pairs get distinct
    instances that share the same database.
    """
    from hermes_constants import (
        apply_secure_dir_policy,
        assert_named_profile_home_live,
        get_hermes_home,
        hermes_home_key,
        mkdir_under_hermes_home,
    )

    root = Path(home) if home is not None else get_hermes_home()
    resolved_policy = policy if policy is not None else CachePolicy()
    resolved_serializer = serializer if serializer is not None else JSONSerializer()
    try:
        hash(resolved_policy)
    except TypeError as exc:
        raise TypeError("CachePolicy must be hashable") from exc
    cache_dir = root / "cache"
    assert_named_profile_home_live(cache_dir)
    path = cache_dir / "cache.db"
    key = (
        hermes_home_key(root),
        os.path.normcase(os.path.abspath(str(path))),
        resolved_policy,
        type(resolved_serializer),
        resolved_serializer.name,
    )
    _ensure_atexit()
    with _REGISTRY_LOCK:
        cache = _REGISTRY.get(key)
        if cache is None or cache.closed:
            mkdir_under_hermes_home(cache_dir)
            apply_secure_dir_policy(cache_dir, home=root)
            cache = Cache(
                SQLiteProvider(
                    path,
                    retries=resolved_policy.provider_retries,
                    retry_delay=resolved_policy.provider_retry_delay,
                ),
                policy=resolved_policy,
                serializer=resolved_serializer,
            )
            _REGISTRY[key] = cache
        return cache


def close_all_caches() -> None:
    """Close every cache owned by this process."""
    with _REGISTRY_LOCK:
        caches = list(_REGISTRY.values())
        _REGISTRY.clear()
    for cache in caches:
        try:
            cache.close()
        except Exception:  # shutdown boundary: never fail interpreter exit
            logger.debug("cache close failed during shutdown", exc_info=True)


def reset_cache_registry() -> None:
    """Close and forget all caches (tests and profile churn)."""
    close_all_caches()
