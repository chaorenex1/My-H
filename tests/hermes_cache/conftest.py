"""Fixtures for the cache subsystem tests."""

from __future__ import annotations

import random
import threading
from pathlib import Path

import pytest

from hermes_cache import (
    EvictionPolicy,
    ProviderEntry,
    ProviderError,
    ProviderStats,
)


class FakeProvider:
    """Minimal in-memory provider used to prove provider independence."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], ProviderEntry] = {}
        self.accessed: dict[tuple[str, str], float] = {}
        self.hits: dict[tuple[str, str], int] = {}
        self.touches: list[tuple[str, str, bool]] = []
        self.purge_event = threading.Event()
        self.closed = False
        self._random = random.Random(0)

    def get(self, namespace: str, key: str) -> ProviderEntry | None:
        if self.closed:
            raise ProviderError("closed")
        return self.data.get((namespace, key))

    def get_many(
        self, namespace: str, keys: list[str]
    ) -> dict[str, ProviderEntry]:
        found: dict[str, ProviderEntry] = {}
        for key in keys:
            entry = self.data.get((namespace, key))
            if entry is not None:
                found[key] = entry
        return found

    def set(self, entry: ProviderEntry) -> None:
        if self.closed:
            raise ProviderError("closed")
        item = (entry.namespace, entry.key)
        self.data[item] = entry
        self.accessed[item] = entry.created_at
        self.hits[item] = 0

    def set_many(self, entries: list[ProviderEntry]) -> None:
        for entry in entries:
            self.set(entry)

    def delete(self, namespace: str, key: str) -> bool:
        item = (namespace, key)
        self.accessed.pop(item, None)
        self.hits.pop(item, None)
        return self.data.pop(item, None) is not None

    def delete_many(self, namespace: str, keys: list[str]) -> int:
        return sum(self.delete(namespace, key) for key in keys)

    def delete_namespace(self, namespace: str) -> int:
        keys = [item for item in self.data if item[0] == namespace]
        for item in keys:
            self.delete(*item)
        return len(keys)

    def clear(self) -> int:
        count = len(self.data)
        self.data.clear()
        self.accessed.clear()
        self.hits.clear()
        return count

    def apply_batch(
        self,
        puts: list[ProviderEntry],
        deletes: list[tuple[str, str]],
    ) -> None:
        for entry in puts:
            self.set(entry)
        for namespace, key in deletes:
            self.delete(namespace, key)

    def purge_expired(self, now: float, limit: int = 1000) -> int:
        self.purge_event.set()
        expired = [
            item
            for item, entry in self.data.items()
            if entry.expires_at is not None and entry.expires_at <= now
        ][:limit]
        for item in expired:
            self.delete(*item)
        return len(expired)

    def touch(
        self,
        namespace: str,
        key: str,
        now: float,
        *,
        count_hit: bool,
    ) -> None:
        item = (namespace, key)
        if item not in self.data:
            return
        self.touches.append((namespace, key, count_hit))
        self.accessed[item] = now
        if count_hit:
            self.hits[item] = self.hits.get(item, 0) + 1

    def touch_many(
        self,
        namespace: str,
        keys: list[str],
        now: float,
        *,
        count_hit: bool,
    ) -> None:
        for key in keys:
            self.touch(namespace, key, now, count_hit=count_hit)

    def trim_to_bytes(
        self,
        max_bytes: int,
        now: float,
        policy: EvictionPolicy = EvictionPolicy.FIFO,
    ) -> int:
        resolved = EvictionPolicy.coerce(policy)
        deleted = self.purge_expired(now)
        total = sum(len(entry.payload) for entry in self.data.values())
        if total <= max_bytes:
            return deleted
        if resolved is EvictionPolicy.RR:
            order = list(self.data)
            self._random.shuffle(order)
        elif resolved is EvictionPolicy.LRU:
            order = sorted(
                self.data,
                key=lambda item: (
                    self.accessed.get(
                        item, self.data[item].created_at
                    ),
                    self.data[item].created_at,
                ),
            )
        elif resolved is EvictionPolicy.LFU:
            order = sorted(
                self.data,
                key=lambda item: (
                    self.hits.get(item, 0),
                    self.data[item].created_at,
                ),
            )
        else:
            order = sorted(
                self.data,
                key=lambda item: self.data[item].created_at,
            )
        for item in order:
            if total <= max_bytes:
                break
            payload_size = len(self.data[item].payload)
            self.delete(*item)
            total -= payload_size
            deleted += 1
        return deleted

    def stats(self) -> ProviderStats:
        return ProviderStats(
            entries=len(self.data),
            size_bytes=sum(len(entry.payload) for entry in self.data.values()),
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def cache_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_constants import reset_hermes_home_key_cache

    from hermes_cache import reset_cache_registry

    reset_hermes_home_key_cache()
    reset_cache_registry()
    yield home
    reset_cache_registry()
    reset_hermes_home_key_cache()


@pytest.fixture
def cache_dir(tmp_path) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    return path
