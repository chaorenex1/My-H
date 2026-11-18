"""Cache, namespace and batch facade tests."""

from __future__ import annotations

import pytest

from hermes_cache import Cache, CachePolicy, EvictionPolicy, ProviderError


def _cache(fake_provider):
    return Cache(fake_provider, policy=CachePolicy(l1_enabled=False))


def test_namespace_isolation(fake_provider):
    cache = _cache(fake_provider)
    one = cache.namespace("one")
    two = cache.namespace("two")
    one.set("k", 1)
    two.set("k", 2)
    assert one.get("k") == 1
    assert two.get("k") == 2


def test_batch_overlay_commit(fake_provider):
    cache = _cache(fake_provider)
    cache.set("old", "value", namespace="ns")
    with cache.batch(namespace="ns") as batch:
        batch.set("a", 1)
        batch.delete("old")
        assert batch.get("a") == 1
        assert batch.get("old") is None
    assert cache.get("a", namespace="ns") == 1
    assert cache.get("old") is None


def test_batch_rollback_on_exception(fake_provider):
    cache = _cache(fake_provider)
    with pytest.raises(RuntimeError):
        with cache.batch(namespace="ns"):
            cache.set("a", 1, namespace="ns")
            raise RuntimeError("stop")
    assert cache.get("a", namespace="ns") == 1


def test_batch_after_commit_rejects_use(fake_provider):
    cache = _cache(fake_provider)
    batch = cache.batch()
    batch.set("a", 1)
    batch.commit()
    with pytest.raises(RuntimeError):
        batch.set("b", 2)


def test_namespace_batch_and_clear(fake_provider):
    cache = _cache(fake_provider)
    ns = cache.namespace("ns")
    with ns.batch() as batch:
        batch.set("a", 1)
        batch.set("b", 2)
    assert ns.get_many(["a", "b"]) == {"a": 1, "b": 2}
    assert ns.clear() == 2
    assert ns.get("a", "miss") == "miss"


def test_cache_close(fake_provider):
    cache = _cache(fake_provider)
    cache.close()
    assert cache.closed is True


def test_batch_bulk_helpers_and_overlay(fake_provider):
    cache = _cache(fake_provider)
    cache.set_many({"old": 0, "keep": 9})
    batch = cache.batch()
    batch.set_many({"a": 1, "b": 2})
    batch.delete("old")
    batch.delete_many(["ghost"])
    assert batch.get_many(["a", "keep", "old"]) == {"a": 1, "keep": 9}
    assert batch.contains("a") is True
    assert batch.contains("old") is False
    assert batch.pending() == 4
    assert batch.commit() == 4
    assert cache.get("old", "miss") == "miss"
    assert cache.get_many(["a", "b"]) == {"a": 1, "b": 2}


def test_failed_commit_keeps_batch_buffer(fake_provider, monkeypatch):
    cache = _cache(fake_provider)
    batch = cache.batch()
    batch.set("a", 1)

    def fail(puts, deletes):
        raise ProviderError("boom")

    original = fake_provider.apply_batch
    monkeypatch.setattr(fake_provider, "apply_batch", fail)
    assert batch.commit() == 0
    assert batch.pending() == 1
    monkeypatch.setattr(fake_provider, "apply_batch", original)
    assert batch.commit() == 1
    assert cache.get("a") == 1


def test_namespace_cap_evicts_least_recently_used(fake_provider):
    cache = _cache(fake_provider)
    ns = cache.namespace(
        "bounded", max_entries=2, eviction=EvictionPolicy.LRU
    )
    ns.set("a", 1)
    ns.set("b", 2)
    assert ns.get("a") == 1
    ns.set("c", 3)
    assert ns.get("b", "miss") == "miss"
    assert ns.get_many(["a", "c"]) == {"a": 1, "c": 3}
    assert fake_provider.stats().entries == 2


def test_namespace_cap_counts_l1_hits(fake_provider):
    cache = Cache(fake_provider, policy=CachePolicy(background_cleanup=False))
    ns = cache.namespace("bounded", max_entries=2)
    ns.set("a", 1)
    ns.set("b", 2)
    assert ns.get("a") == 1
    ns.set("c", 3)
    assert ns.get("b", "miss") == "miss"
    assert ns.get("a") == 1
    assert ns.get("c") == 3
