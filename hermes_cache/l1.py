"""Process-local memory layer with pluggable eviction policies."""

from __future__ import annotations

import random
import threading
from collections import OrderedDict
from dataclasses import dataclass

from .policy import EvictionPolicy


@dataclass(frozen=True, slots=True)
class L1Entry:
    payload: bytes
    codec: str
    expires_at: float | None
    stored_at: float


class MemoryL1:
    """Bounded, thread-safe in-process cache with LRU/LFU/FIFO/RR eviction."""

    def __init__(
        self,
        *,
        max_entries: int,
        max_bytes: int,
        max_age: float | None,
        eviction: EvictionPolicy = EvictionPolicy.LRU,
    ) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_age = max_age
        self.eviction = EvictionPolicy.coerce(eviction)
        self._entries: OrderedDict[tuple[str, str], L1Entry] = OrderedDict()
        self._freq: dict[tuple[str, str], int] = {}
        self._bytes = 0
        self._lock = threading.RLock()
        self._random = random.Random()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def get(self, namespace: str, key: str, now: float) -> L1Entry | None:
        item = (namespace, key)
        with self._lock:
            entry = self._entries.get(item)
            if entry is None:
                return None
            expired = entry.expires_at is not None and entry.expires_at <= now
            stale = (
                self.max_age is not None
                and now - entry.stored_at > self.max_age
            )
            if expired or stale:
                self._remove_locked(item, entry)
                return None
            if self.eviction is EvictionPolicy.LRU:
                self._entries.move_to_end(item)
            elif self.eviction is EvictionPolicy.LFU:
                self._freq[item] = self._freq.get(item, 0) + 1
            return entry

    def put(
        self,
        namespace: str,
        key: str,
        payload: bytes,
        codec: str,
        expires_at: float | None,
        stored_at: float,
    ) -> int:
        size = len(payload)
        if self.max_entries < 1 or self.max_bytes < 1 or size > self.max_bytes:
            return 0
        item = (namespace, key)
        entry = L1Entry(payload, codec, expires_at, stored_at)
        evicted = 0
        with self._lock:
            old = self._entries.pop(item, None)
            if old is not None:
                self._bytes -= len(old.payload)
            self._entries[item] = entry
            self._freq[item] = 1
            self._bytes += size
            while (
                len(self._entries) > self.max_entries
                or self._bytes > self.max_bytes
            ):
                victim = self._victim_locked()
                self._remove_locked(victim, self._entries[victim])
                evicted += 1
        return evicted

    def delete(self, namespace: str, key: str) -> bool:
        item = (namespace, key)
        with self._lock:
            entry = self._entries.get(item)
            if entry is None:
                return False
            self._remove_locked(item, entry)
            return True

    def clear_namespace(self, namespace: str) -> int:
        with self._lock:
            keys = [item for item in self._entries if item[0] == namespace]
            for item in keys:
                entry = self._entries.pop(item)
                self._freq.pop(item, None)
                self._bytes -= len(entry.payload)
            return len(keys)

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self._freq.clear()
            self._bytes = 0
            return count

    def _victim_locked(self) -> tuple[str, str]:
        if self.eviction is EvictionPolicy.RR:
            return self._random.choice(tuple(self._entries))
        if self.eviction is EvictionPolicy.LFU:
            victim = next(iter(self._entries))
            lowest = self._freq.get(victim, 0)
            for item in self._entries:
                frequency = self._freq.get(item, 0)
                if frequency < lowest:
                    victim = item
                    lowest = frequency
            return victim
        # LRU reorders on access, so the front is least recently used.
        # FIFO never reorders, so the front is the oldest insertion.
        return next(iter(self._entries))

    def _remove_locked(
        self, item: tuple[str, str], entry: L1Entry
    ) -> None:
        self._entries.pop(item, None)
        self._freq.pop(item, None)
        self._bytes -= len(entry.payload)
