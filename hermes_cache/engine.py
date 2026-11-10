"""Cache engine: TTL, serialization, L1/L2 coordination and stats."""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from hermes_threading import spawn_context_thread

from .errors import (
    CacheClosedError,
    InvalidKeyError,
    InvalidTTLError,
    ProviderError,
    SerializationError,
)
from .l1 import MemoryL1
from .models import DEFAULT_NAMESPACE, MISSING, CacheStats
from .policy import CachePolicy, EvictionPolicy
from .providers.base import CacheProvider, ProviderEntry, ProviderStats
from .serializers import JSONSerializer, Serializer

logger = logging.getLogger(__name__)

_NAMESPACE_MAX_BYTES = 128


@dataclass
class _MutableStats:
    l1_hits: int = 0
    l2_hits: int = 0
    misses: int = 0
    expired: int = 0
    writes: int = 0
    deletes: int = 0
    evictions_l1: int = 0
    errors: int = 0
    batches: int = 0
    purges: int = 0
    evictions_l2: int = 0
    background_cleanups: int = 0

    def snapshot(self) -> CacheStats:
        return CacheStats(
            l1_hits=self.l1_hits,
            l2_hits=self.l2_hits,
            misses=self.misses,
            expired=self.expired,
            writes=self.writes,
            deletes=self.deletes,
            evictions_l1=self.evictions_l1,
            errors=self.errors,
            batches=self.batches,
            purges=self.purges,
            evictions_l2=self.evictions_l2,
            background_cleanups=self.background_cleanups,
        )


@dataclass
class _KeyLock:
    lock: threading.Lock
    refs: int


class _KeyLocks:
    """Per-key locks with reference-counted cleanup."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, str], _KeyLock] = {}

    @contextmanager
    def hold(self, key: tuple[str, str]) -> Iterator[None]:
        with self._guard:
            item = self._locks.get(key)
            if item is None:
                item = _KeyLock(threading.Lock(), 1)
                self._locks[key] = item
            else:
                item.refs += 1
            lock = item.lock
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                item.refs -= 1
                if item.refs == 0:
                    self._locks.pop(key, None)


class _NamespaceLimit:
    """Process-local recency/frequency index enforcing a namespace entry cap."""

    def __init__(
        self, max_entries: int, eviction: EvictionPolicy
    ) -> None:
        self.max_entries = max_entries
        self.eviction = EvictionPolicy.coerce(eviction)
        self.order: OrderedDict[str, int] = OrderedDict()
        self.lock = threading.Lock()
        self._random = random.Random()

    def touch(self, key: str) -> list[str]:
        with self.lock:
            if key in self.order:
                if self.eviction is EvictionPolicy.LRU:
                    self.order.move_to_end(key)
                if self.eviction is EvictionPolicy.LFU:
                    self.order[key] += 1
            else:
                self.order[key] = 1
            victims: list[str] = []
            while len(self.order) > self.max_entries:
                victim = self._victim_locked()
                del self.order[victim]
                victims.append(victim)
            return victims

    def remove(self, key: str) -> None:
        with self.lock:
            self.order.pop(key, None)

    def clear(self) -> None:
        with self.lock:
            self.order.clear()

    def _victim_locked(self) -> str:
        if self.eviction is EvictionPolicy.RR:
            return self._random.choice(tuple(self.order))
        if self.eviction is EvictionPolicy.LFU:
            victim = next(iter(self.order))
            lowest = self.order[victim]
            for key, frequency in self.order.items():
                if frequency < lowest:
                    victim = key
                    lowest = frequency
            return victim
        # LRU moves accessed keys to the end; FIFO keeps insertion order.
        return next(iter(self.order))


class CacheEngine:
    """Coordinates a provider, serializer, policy and optional L1."""

    def __init__(
        self,
        provider: CacheProvider,
        *,
        serializer: Serializer | None = None,
        policy: CachePolicy | None = None,
        clock: Callable[[], float] = time.time,
        l1: MemoryL1 | None = None,
    ) -> None:
        self.provider = provider
        self.serializer = serializer if serializer is not None else JSONSerializer()
        self.policy = policy if policy is not None else CachePolicy()
        self.clock = clock
        if l1 is not None:
            self.l1 = l1
        elif self.policy.l1_enabled and self.policy.max_l1_entries > 0:
            self.l1 = MemoryL1(
                max_entries=self.policy.max_l1_entries,
                max_bytes=self.policy.max_l1_bytes,
                max_age=self.policy.l1_max_age,
                eviction=self.policy.l1_eviction,
            )
        else:
            self.l1 = None
        self._stats = _MutableStats()
        self._stats_lock = threading.Lock()
        self._closed = False
        self._writes_since_maintenance = 0
        self._key_locks = _KeyLocks()
        self._maintenance_lock = threading.RLock()
        self._cleanup_stop = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        self._namespace_limits: dict[str, _NamespaceLimit] = {}
        self._namespace_lock = threading.Lock()
        self._start_background_cleanup()

    @property
    def closed(self) -> bool:
        return self._closed

    def get(
        self,
        key: str,
        default: Any = None,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        bypass_l1: bool = False,
    ) -> Any:
        self._check_open()
        self._validate(namespace, key)
        found, value = self._lookup(
            key, namespace=namespace, bypass_l1=bypass_l1
        )
        return value if found else default

    def contains(
        self, key: str, *, namespace: str = DEFAULT_NAMESPACE
    ) -> bool:
        self._check_open()
        self._validate(namespace, key)
        found, _ = self._lookup(key, namespace=namespace, bypass_l1=False)
        return found

    def set(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl: float | None = None,
    ) -> bool:
        entry = self.build_entry(key, value, namespace=namespace, ttl=ttl)
        try:
            self.provider.set(entry)
        except ProviderError as exc:
            return self._on_provider_error("set", exc, False)
        self._put_l1(entry)
        self._note_namespace_access(namespace, entry.key)
        self._inc("writes")
        self._maybe_maintain()
        return True

    def build_entry(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl: float | None = None,
    ) -> ProviderEntry:
        self._check_open()
        self._validate(namespace, key)
        resolved_ttl = self._resolve_ttl(ttl)
        payload = self.serializer.dumps(value)
        if len(payload) > self.policy.max_value_bytes:
            raise SerializationError(
                "serialized value exceeds max_value_bytes "
                f"({len(payload)} > {self.policy.max_value_bytes})"
            )
        now = self.clock()
        expires_at = now + resolved_ttl if resolved_ttl is not None else None
        return ProviderEntry(
            namespace=namespace,
            key=key,
            payload=payload,
            codec=self.serializer.name,
            created_at=now,
            expires_at=expires_at,
        )

    def get_many(
        self, keys: Sequence[str], *, namespace: str = DEFAULT_NAMESPACE
    ) -> dict[str, Any]:
        self._check_open()
        unique = list(dict.fromkeys(keys))
        for key in unique:
            self._validate(namespace, key)
        result: dict[str, Any] = {}
        pending: list[str] = []
        now = self.clock()
        for key in unique:
            if self.l1 is not None:
                hit = self.l1.get(namespace, key, now)
                if hit is not None:
                    self._inc("l1_hits")
                    try:
                        result[key] = self._decode(hit.payload, hit.codec)
                        continue
                    except SerializationError as exc:
                        self._corrupt(namespace, key, exc)
            pending.append(key)
        if not pending:
            return result
        try:
            entries = self.provider.get_many(namespace, pending)
        except ProviderError as exc:
            self._on_provider_error("get_many", exc, None)
            return result
        for key in pending:
            entry = entries.get(key)
            if entry is None:
                self._inc("misses")
                continue
            if entry.expires_at is not None and entry.expires_at <= now:
                self._inc("expired")
                self._delete_best_effort(namespace, key)
                continue
            try:
                value = self._decode(entry.payload, entry.codec)
            except SerializationError as exc:
                self._corrupt(namespace, key, exc)
                continue
            if self.l1 is not None:
                self._put_l1(entry, now)
            self._inc("l2_hits")
            result[key] = value
        hits = [key for key in pending if key in result]
        if hits:
            self._record_l2_access(namespace, hits, now)
        if result:
            self._note_namespace_access(namespace, list(result))
        return result

    def set_many(
        self,
        values: Mapping[str, Any],
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl: float | None = None,
    ) -> int:
        entries = [
            self.build_entry(key, value, namespace=namespace, ttl=ttl)
            for key, value in values.items()
        ]
        if not entries:
            return 0
        try:
            self.provider.set_many(entries)
        except ProviderError as exc:
            return self._on_provider_error("set_many", exc, 0)
        for entry in entries:
            self._put_l1(entry)
        self._note_namespace_access(
            namespace, [entry.key for entry in entries]
        )
        self._inc("writes", len(entries))
        self._maybe_maintain()
        return len(entries)

    def delete(
        self, key: str, *, namespace: str = DEFAULT_NAMESPACE
    ) -> bool:
        self._check_open()
        self._validate(namespace, key)
        try:
            deleted = self.provider.delete(namespace, key)
        except ProviderError as exc:
            return self._on_provider_error("delete", exc, False)
        if self.l1 is not None:
            self.l1.delete(namespace, key)
        if deleted:
            self._note_namespace_remove(namespace, key)
            self._inc("deletes")
        return deleted

    def delete_many(
        self, keys: Sequence[str], *, namespace: str = DEFAULT_NAMESPACE
    ) -> int:
        self._check_open()
        unique = list(dict.fromkeys(keys))
        for key in unique:
            self._validate(namespace, key)
        if not unique:
            return 0
        try:
            count = self.provider.delete_many(namespace, unique)
        except ProviderError as exc:
            return self._on_provider_error("delete_many", exc, 0)
        if self.l1 is not None:
            for key in unique:
                self.l1.delete(namespace, key)
        self._note_namespace_remove(namespace, unique)
        self._inc("deletes", count)
        return count

    def clear(self, *, namespace: str | None = None) -> int:
        self._check_open()
        if namespace is not None:
            self._validate_namespace(namespace)
        try:
            if namespace is None:
                count = self.provider.clear()
            else:
                count = self.provider.delete_namespace(namespace)
        except ProviderError as exc:
            return self._on_provider_error("clear", exc, 0)
        if self.l1 is not None:
            if namespace is None:
                self.l1.clear()
            else:
                self.l1.clear_namespace(namespace)
        self._forget_namespace(namespace)
        self._inc("deletes", count)
        return count

    def apply_batch(
        self,
        puts: Sequence[ProviderEntry],
        deletes: Sequence[tuple[str, str]],
    ) -> bool:
        self._check_open()
        try:
            self.provider.apply_batch(puts, deletes)
        except ProviderError as exc:
            return self._on_provider_error("apply_batch", exc, False)
        for entry in puts:
            self._put_l1(entry)
        if self.l1 is not None:
            for namespace, key in deletes:
                self.l1.delete(namespace, key)
        for entry in puts:
            self._note_namespace_access(entry.namespace, entry.key)
        for namespace, key in deletes:
            self._note_namespace_remove(namespace, key)
        self._inc("writes", len(puts))
        self._inc("deletes", len(deletes))
        self._inc("batches")
        self._maybe_maintain()
        return True

    def get_or_set(
        self,
        key: str,
        factory: Callable[[], Any],
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl: float | None = None,
    ) -> Any:
        self._check_open()
        self._validate(namespace, key)
        found, value = self._lookup(key, namespace=namespace, bypass_l1=False)
        if found:
            return value
        with self._key_locks.hold((namespace, key)):
            found, value = self._lookup(
                key, namespace=namespace, bypass_l1=False, count_miss=False
            )
            if found:
                return value
            value = factory()
            self.set(key, value, namespace=namespace, ttl=ttl)
            return value

    def purge_expired(self, *, limit: int = 1000) -> int:
        self._check_open()
        try:
            count = self.provider.purge_expired(self.clock(), limit)
        except ProviderError as exc:
            return self._on_provider_error("purge_expired", exc, 0)
        self._inc("purges")
        return count

    def maintenance(self) -> tuple[int, int]:
        """Purge expired rows and enforce ``max_l2_bytes``.

        Returns ``(purged, trimmed)``. Background cleanup calls this on an
        interval; it is also safe to call directly (tests, shutdown hooks).
        """
        self._check_open()
        with self._maintenance_lock:
            if self._closed:
                raise CacheClosedError("cache is closed")
            return self._cleanup_now(strict=True)

    def _cleanup_now(self, *, strict: bool) -> tuple[int, int]:
        now = self.clock()
        purged = 0
        trimmed = 0
        try:
            purged = self.provider.purge_expired(
                now, self.policy.cleanup_batch_limit
            )
            self._inc("purges")
        except ProviderError as exc:
            if strict:
                self._on_provider_error("purge_expired", exc, 0)
            else:
                self._inc("errors")
                logger.debug("cache purge failed", exc_info=True)
        if self.policy.max_l2_bytes is not None:
            try:
                trimmed = self.provider.trim_to_bytes(
                    self.policy.max_l2_bytes,
                    now,
                    self.policy.l2_eviction,
                )
                self._inc("purges")
                self._inc("evictions_l2", trimmed)
            except ProviderError as exc:
                if strict:
                    self._on_provider_error("trim_to_bytes", exc, 0)
                else:
                    self._inc("errors")
                    logger.debug("cache trim failed", exc_info=True)
        return purged, trimmed

    def _start_background_cleanup(self) -> None:
        if not self.policy.background_cleanup:
            return
        try:
            thread = spawn_context_thread(
                self._cleanup_loop, name="hermes-cache-cleanup"
            )
            thread.start()
        except RuntimeError:
            logger.warning("cache background cleanup thread could not start")
            return
        self._cleanup_thread = thread

    def register_namespace(
        self,
        namespace: str,
        *,
        max_entries: int,
        eviction: EvictionPolicy = EvictionPolicy.LRU,
    ) -> None:
        """Bound one namespace to ``max_entries`` with the given eviction order."""
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries < 1
        ):
            raise ValueError("namespace max_entries must be a positive integer")
        resolved = EvictionPolicy.coerce(eviction)
        with self._namespace_lock:
            current = self._namespace_limits.get(namespace)
            if (
                current is not None
                and current.max_entries == max_entries
                and current.eviction is resolved
            ):
                return
            self._namespace_limits[namespace] = _NamespaceLimit(
                max_entries, resolved
            )

    def _note_namespace_access(
        self, namespace: str, keys: str | Sequence[str]
    ) -> None:
        tracker = self._namespace_limits.get(namespace)
        if tracker is None:
            return
        if isinstance(keys, str):
            keys = (keys,)
        victims: list[str] = []
        for key in keys:
            victims.extend(tracker.touch(key))
        for key in victims:
            self._delete_best_effort(namespace, key)

    def _note_namespace_remove(
        self, namespace: str, keys: str | Sequence[str]
    ) -> None:
        tracker = self._namespace_limits.get(namespace)
        if tracker is None:
            return
        if isinstance(keys, str):
            keys = (keys,)
        for key in keys:
            tracker.remove(key)

    def _forget_namespace(self, namespace: str | None) -> None:
        with self._namespace_lock:
            trackers = (
                list(self._namespace_limits.values())
                if namespace is None
                else [self._namespace_limits.get(namespace)]
            )
        for tracker in trackers:
            if tracker is not None:
                tracker.clear()

    def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.wait(self.policy.cleanup_interval):
            try:
                self.maintenance()
            except CacheClosedError:
                return
            except Exception:  # thread boundary: one failure must not stop cleanup
                logger.exception("cache background cleanup failed")
                continue
            self._inc("background_cleanups")

    def stats(self) -> CacheStats:
        with self._stats_lock:
            return self._stats.snapshot()

    def provider_stats(self) -> ProviderStats:
        self._check_open()
        return self.provider.stats()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cleanup_stop.set()
        thread = self._cleanup_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.warning("cache background cleanup did not stop in time")
        with self._maintenance_lock:
            if self.l1 is not None:
                self.l1.clear()
            try:
                self.provider.close()
            except ProviderError:
                logger.debug("cache provider close failed", exc_info=True)

    def _lookup(
        self,
        key: str,
        *,
        namespace: str,
        bypass_l1: bool,
        count_miss: bool = True,
    ) -> tuple[bool, Any]:
        now = self.clock()
        if self.l1 is not None and not bypass_l1:
            hit = self.l1.get(namespace, key, now)
            if hit is not None:
                self._inc("l1_hits")
                try:
                    value = self._decode(hit.payload, hit.codec)
                    self._note_namespace_access(namespace, key)
                    return True, value
                except SerializationError as exc:
                    self._corrupt(namespace, key, exc)
                    return False, MISSING
        try:
            entry = self.provider.get(namespace, key)
        except ProviderError as exc:
            self._on_provider_error("get", exc, False)
            return False, MISSING
        if entry is None:
            if count_miss:
                self._inc("misses")
            return False, MISSING
        if entry.expires_at is not None and entry.expires_at <= now:
            self._inc("expired")
            self._delete_best_effort(namespace, key)
            return False, MISSING
        try:
            value = self._decode(entry.payload, entry.codec)
        except SerializationError as exc:
            self._corrupt(namespace, key, exc)
            return False, MISSING
        if self.l1 is not None:
            self._put_l1(entry, now)
        self._record_l2_access(namespace, key, now)
        self._note_namespace_access(namespace, key)
        self._inc("l2_hits")
        return True, value

    def _decode(self, payload: bytes, codec: str) -> Any:
        if codec != self.serializer.name:
            raise SerializationError(
                f"cache entry codec {codec!r} does not match {self.serializer.name!r}"
            )
        return self.serializer.loads(payload)

    def _put_l1(self, entry: ProviderEntry, now: float | None = None) -> None:
        if self.l1 is None:
            return
        evicted = self.l1.put(
            entry.namespace,
            entry.key,
            entry.payload,
            entry.codec,
            entry.expires_at,
            entry.created_at if now is None else now,
        )
        if evicted:
            self._inc("evictions_l1", evicted)

    def _record_l2_access(
        self,
        namespace: str,
        keys: str | Sequence[str],
        now: float,
    ) -> None:
        policy = self.policy.l2_eviction
        if policy not in (EvictionPolicy.LRU, EvictionPolicy.LFU):
            return
        count_hit = policy is EvictionPolicy.LFU
        try:
            if isinstance(keys, str):
                self.provider.touch(
                    namespace, keys, now, count_hit=count_hit
                )
            else:
                self.provider.touch_many(
                    namespace, keys, now, count_hit=count_hit
                )
        except ProviderError as exc:
            # Access metadata is advisory: a failed touch must not fail the read.
            self._inc("errors")
            logger.debug("cache touch failed: %s", exc)

    def _corrupt(
        self, namespace: str, key: str, exc: SerializationError
    ) -> None:
        self._inc("errors")
        self._delete_best_effort(namespace, key)
        if self.policy.raise_on_corrupt:
            raise exc
        logger.warning(
            "cache entry %s/%s is unusable: %s", namespace, key, exc
        )

    def _delete_best_effort(self, namespace: str, key: str) -> None:
        self._note_namespace_remove(namespace, key)
        if self.l1 is not None:
            self.l1.delete(namespace, key)
        try:
            self.provider.delete(namespace, key)
        except ProviderError:
            logger.debug("cache delete failed for %s/%s", namespace, key, exc_info=True)

    def _on_provider_error(self, operation: str, exc: ProviderError, fallback: Any) -> Any:
        self._inc("errors")
        if not self.policy.fail_open:
            raise exc
        logger.warning("cache %s failed: %s", operation, exc, exc_info=True)
        return fallback

    def _maybe_maintain(self) -> None:
        if self._cleanup_thread is not None:
            return
        every = self.policy.maintenance_every_writes
        if every <= 0:
            return
        self._writes_since_maintenance += 1
        if self._writes_since_maintenance < every:
            return
        self._writes_since_maintenance = 0
        with self._maintenance_lock:
            if self._closed:
                return
            self._cleanup_now(strict=False)

    def _resolve_ttl(self, ttl: float | None) -> float | None:
        if ttl is None:
            ttl = self.policy.default_ttl
        if ttl is None:
            return None
        if (
            not isinstance(ttl, (int, float))
            or isinstance(ttl, bool)
            or not math.isfinite(float(ttl))
            or ttl < 0
        ):
            raise InvalidTTLError("ttl must be finite and >= 0")
        return None if float(ttl) == 0 else float(ttl)

    def _validate(self, namespace: str, key: str) -> None:
        self._validate_namespace(namespace)
        if not isinstance(key, str) or not key or "\x00" in key:
            raise InvalidKeyError("cache key must be a non-empty string without NUL")
        if len(key.encode("utf-8")) > self.policy.max_key_bytes:
            raise InvalidKeyError("cache key exceeds max_key_bytes")

    @staticmethod
    def _validate_namespace(namespace: str) -> None:
        if (
            not isinstance(namespace, str)
            or not namespace
            or "\x00" in namespace
        ):
            raise InvalidKeyError(
                "cache namespace must be a non-empty string without NUL"
            )
        if len(namespace.encode("utf-8")) > _NAMESPACE_MAX_BYTES:
            raise InvalidKeyError("cache namespace is too long")

    def _check_open(self) -> None:
        if self._closed:
            raise CacheClosedError("cache is closed")

    def _inc(self, field: str, amount: int = 1) -> None:
        if amount == 0:
            return
        with self._stats_lock:
            current = getattr(self._stats, field)
            setattr(self._stats, field, current + amount)
