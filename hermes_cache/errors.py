"""Cache subsystem exceptions."""

from __future__ import annotations


class CacheError(Exception):
    """Base class for cache failures."""


class CacheClosedError(CacheError):
    """Raised when a closed cache is used."""


class InvalidKeyError(CacheError, ValueError):
    """Raised for an invalid cache key or namespace."""


class InvalidTTLError(CacheError, ValueError):
    """Raised for an invalid TTL."""


class SerializationError(CacheError):
    """Raised when a value cannot be encoded or decoded."""


class ProviderError(CacheError):
    """Raised when a storage provider operation fails."""
