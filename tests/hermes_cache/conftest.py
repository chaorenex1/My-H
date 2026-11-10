"""Fixtures for the cache subsystem tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cache import (
    ProviderEntry,
    ProviderError,
    ProviderStats,
)


class FakeProvider:
    """Minimal in-memory provider used to prove provider independence."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], ProviderEntry] = {}
        self.closed = False

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
        self.data[(entry.namespace, entry.key)] = entry

    def set_many(self, entries: list[ProviderEntry]) -> None:
        for entry in entries:
            self.set(entry)

    def delete(self, namespace: str, key: str) -> bool:
        return self.data.pop((namespace, key), None) is not None

    def delete_many(self, namespace: str, keys: list[str]) -> int:
        return sum(self.delete(namespace, key) for key in keys)

    def delete_namespace(self, namespace: str) -> int:
        keys = [item for item in self.data if item[0] == namespace]
        for key in keys:
            del self.data[key]
        return len(keys)

    def clear(self) -> int:
        count = len(self.data)
        self.data.clear()
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
        expired = [
            item
            for item, entry in self.data.items()
            if entry.expires_at is not None and entry.expires_at <= now
        ][:limit]
        for item in expired:
            del self.data[item]
        return len(expired)

    def trim_to_bytes(self, max_bytes: int, now: float) -> int:
        deleted = self.purge_expired(now)
        total = sum(len(entry.payload) for entry in self.data.values())
        for item, entry in sorted(
            self.data.items(), key=lambda pair: pair[1].created_at
        ):
            if total <= max_bytes:
                break
            del self.data[item]
            total -= len(entry.payload)
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
