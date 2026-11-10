"""Provider-based embedded cache for Hermes.

``import hermes_cache`` exposes the public API. Business code uses
:func:`get_cache`; only the factory and tests import ``SQLiteProvider``.
"""

from __future__ import annotations

from .api import Batch, Cache, Namespace
from .errors import (
    CacheClosedError,
    CacheError,
    InvalidKeyError,
    InvalidTTLError,
    ProviderError,
    SerializationError,
)
from .factory import (
    close_all_caches,
    default_cache_path,
    get_cache,
    reset_cache_registry,
)
from .models import CacheStats
from .policy import CachePolicy, EvictionPolicy
from .providers.base import CacheProvider, ProviderEntry, ProviderStats
from .providers.sqlite import SQLiteProvider
from .serializers import (
    JSONSerializer,
    PickleSerializer,
    RawSerializer,
    Serializer,
)

__all__ = [
    "Batch",
    "Cache",
    "CacheClosedError",
    "CacheError",
    "CachePolicy",
    "CacheProvider",
    "CacheStats",
    "EvictionPolicy",
    "InvalidKeyError",
    "InvalidTTLError",
    "JSONSerializer",
    "Namespace",
    "PickleSerializer",
    "ProviderEntry",
    "ProviderError",
    "ProviderStats",
    "RawSerializer",
    "SQLiteProvider",
    "SerializationError",
    "Serializer",
    "close_all_caches",
    "default_cache_path",
    "get_cache",
    "reset_cache_registry",
]
