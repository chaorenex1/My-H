"""Provider contract tests shared by fake and SQLite providers."""

from __future__ import annotations

import pytest

from hermes_cache import ProviderEntry, ProviderError, SQLiteProvider


@pytest.fixture(params=["fake", "sqlite"])
def provider(request, fake_provider, cache_dir):
    if request.param == "fake":
        yield fake_provider
        return
    sqlite = SQLiteProvider(cache_dir / "cache.db")
    try:
        yield sqlite
    finally:
        sqlite.close()


def _entry(namespace, key, payload=b"v", *, created=0.0, expires=None):
    return ProviderEntry(namespace, key, payload, "raw.v1", created, expires)


def test_crud(provider):
    assert provider.get("ns", "k") is None
    provider.set(_entry("ns", "k", b"value"))
    assert provider.get("ns", "k").payload == b"value"
    assert provider.get_many("ns", ["k", "missing"])["k"].payload == b"value"
    assert provider.delete("ns", "k") is True
    assert provider.get("ns", "k") is None


def test_bulk_and_batch(provider):
    provider.set_many([_entry("ns", "a"), _entry("ns", "b")])
    assert set(provider.get_many("ns", ["a", "b"])) == {"a", "b"}
    assert provider.delete_many("ns", ["a", "b"]) == 2
    provider.apply_batch(
        [_entry("ns", "c"), _entry("ns", "d")],
        [("ns", "c")],
    )
    assert provider.get("ns", "c") is None
    assert provider.get("ns", "d") is not None


def test_namespace_and_clear(provider):
    provider.set_many([_entry("a", "1"), _entry("b", "1")])
    assert provider.delete_namespace("a") == 1
    assert provider.get("b", "1") is not None
    assert provider.clear() == 1
    assert provider.stats().entries == 0


def test_expiry_and_trim(provider):
    provider.set_many(
        [
            _entry("ns", "expired", b"12345", expires=5.0),
            _entry("ns", "one", b"12345", created=1.0),
            _entry("ns", "two", b"12345", created=2.0),
            _entry("ns", "three", b"12345", created=3.0),
        ]
    )
    assert provider.purge_expired(10.0) == 1
    deleted = provider.trim_to_bytes(5, 10.0)
    assert deleted >= 2
    assert provider.stats().size_bytes <= 5


def test_close_rejects_use(provider):
    provider.close()
    with pytest.raises(ProviderError):
        provider.get("ns", "k")
