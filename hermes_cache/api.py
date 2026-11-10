"""Public cache facade: Cache, Namespace and Batch."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .engine import CacheEngine
from .errors import CacheClosedError, SerializationError
from .models import DEFAULT_NAMESPACE, CacheStats
from .policy import CachePolicy
from .providers.base import CacheProvider, ProviderEntry, ProviderStats
from .serializers import Serializer


class Cache:
    """Unified cache API. Business code should not import a concrete provider."""

    def __init__(
        self,
        provider: CacheProvider,
        *,
        serializer: Serializer | None = None,
        policy: CachePolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._engine = CacheEngine(
            provider,
            serializer=serializer,
            policy=policy,
            clock=clock,
        )

    @property
    def closed(self) -> bool:
        return self._engine.closed

    def get(
        self,
        key: str,
        default: Any = None,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        bypass_l1: bool = False,
    ) -> Any:
        return self._engine.get(
            key,
            default,
            namespace=namespace,
            bypass_l1=bypass_l1,
        )

    def set(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl: float | None = None,
    ) -> bool:
        return self._engine.set(key, value, namespace=namespace, ttl=ttl)

    def delete(
        self, key: str, *, namespace: str = DEFAULT_NAMESPACE
    ) -> bool:
        return self._engine.delete(key, namespace=namespace)

    def contains(
        self, key: str, *, namespace: str = DEFAULT_NAMESPACE
    ) -> bool:
        return self._engine.contains(key, namespace=namespace)

    def get_many(
        self, keys: Sequence[str], *, namespace: str = DEFAULT_NAMESPACE
    ) -> dict[str, Any]:
        return self._engine.get_many(keys, namespace=namespace)

    def set_many(
        self,
        values: Mapping[str, Any],
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl: float | None = None,
    ) -> int:
        return self._engine.set_many(
            values, namespace=namespace, ttl=ttl
        )

    def delete_many(
        self, keys: Sequence[str], *, namespace: str = DEFAULT_NAMESPACE
    ) -> int:
        return self._engine.delete_many(keys, namespace=namespace)

    def get_or_set(
        self,
        key: str,
        factory: Callable[[], Any],
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl: float | None = None,
    ) -> Any:
        return self._engine.get_or_set(
            key, factory, namespace=namespace, ttl=ttl
        )

    def clear(self, *, namespace: str | None = None) -> int:
        return self._engine.clear(namespace=namespace)

    def purge_expired(self, *, limit: int = 1000) -> int:
        return self._engine.purge_expired(limit=limit)

    def namespace(
        self, name: str, *, default_ttl: float | None = None
    ) -> Namespace:
        return Namespace(self, name, default_ttl=default_ttl)

    def batch(
        self, *, namespace: str = DEFAULT_NAMESPACE
    ) -> Batch:
        return Batch(self, namespace)

    def stats(self) -> CacheStats:
        return self._engine.stats()

    def provider_stats(self) -> ProviderStats:
        return self._engine.provider_stats()

    def close(self) -> None:
        self._engine.close()

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


class Namespace:
    """A cache view bound to one namespace and optional default TTL."""

    def __init__(
        self,
        cache: Cache,
        name: str,
        *,
        default_ttl: float | None = None,
    ) -> None:
        self.cache = cache
        self.name = name
        self.default_ttl = default_ttl

    def get(
        self,
        key: str,
        default: Any = None,
        *,
        bypass_l1: bool = False,
    ) -> Any:
        return self.cache.get(
            key,
            default,
            namespace=self.name,
            bypass_l1=bypass_l1,
        )

    def set(
        self, key: str, value: Any, *, ttl: float | None = None
    ) -> bool:
        resolved = self.default_ttl if ttl is None else ttl
        return self.cache.set(key, value, namespace=self.name, ttl=resolved)

    def delete(self, key: str) -> bool:
        return self.cache.delete(key, namespace=self.name)

    def contains(self, key: str) -> bool:
        return self.cache.contains(key, namespace=self.name)

    def get_many(self, keys: Sequence[str]) -> dict[str, Any]:
        return self.cache.get_many(keys, namespace=self.name)

    def set_many(
        self, values: Mapping[str, Any], *, ttl: float | None = None
    ) -> int:
        resolved = self.default_ttl if ttl is None else ttl
        return self.cache.set_many(
            values, namespace=self.name, ttl=resolved
        )

    def delete_many(self, keys: Sequence[str]) -> int:
        return self.cache.delete_many(keys, namespace=self.name)

    def clear(self) -> int:
        return self.cache.clear(namespace=self.name)

    def batch(self) -> Batch:
        return Batch(self.cache, self.name, default_ttl=self.default_ttl)

    def stats(self) -> CacheStats:
        return self.cache.stats()


class Batch:
    """Write buffer for one namespace; commit writes puts and deletes together."""

    def __init__(
        self,
        cache: Cache,
        namespace: str,
        *,
        default_ttl: float | None = None,
    ) -> None:
        self._cache = cache
        self._namespace = namespace
        self._default_ttl = default_ttl
        self._puts: dict[str, ProviderEntry] = {}
        self._deletes: set[str] = set()
        self._done = False

    def _check_open(self) -> None:
        if self._done:
            raise RuntimeError("cache batch is already committed or rolled back")
        if self._cache.closed:
            raise CacheClosedError("cache is closed")

    def set(
        self, key: str, value: Any, *, ttl: float | None = None
    ) -> None:
        self._check_open()
        resolved = self._default_ttl if ttl is None else ttl
        entry = self._cache._engine.build_entry(
            key, value, namespace=self._namespace, ttl=resolved
        )
        self._puts[key] = entry
        self._deletes.discard(key)

    def delete(self, key: str) -> None:
        self._check_open()
        self._puts.pop(key, None)
        self._deletes.add(key)

    def get(self, key: str, default: Any = None) -> Any:
        self._check_open()
        if key in self._deletes:
            return default
        entry = self._puts.get(key)
        if entry is not None:
            try:
                return self._cache._engine._decode(
                    entry.payload, entry.codec
                )
            except SerializationError:
                return default
        return self._cache.get(
            key, default, namespace=self._namespace
        )

    def commit(self) -> int:
        self._check_open()
        if not self._puts and not self._deletes:
            self._done = True
            return 0
        puts = list(self._puts.values())
        deletes = [(self._namespace, key) for key in self._deletes]
        ok = self._cache._engine.apply_batch(puts, deletes)
        self._done = True
        count = len(puts) + len(deletes)
        self._puts.clear()
        self._deletes.clear()
        return count if ok else 0

    def rollback(self) -> None:
        if self._done:
            return
        self._puts.clear()
        self._deletes.clear()
        self._done = True

    def __enter__(self) -> Batch:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
