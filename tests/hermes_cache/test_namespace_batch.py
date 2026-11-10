"""Cache, namespace and batch facade tests."""

from __future__ import annotations

import pytest

from hermes_cache import Cache, CachePolicy


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
