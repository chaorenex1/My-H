"""Engine behavior tests using a fake provider."""

from __future__ import annotations

from typing import Any, cast

import pytest

from hermes_cache import (
    CacheClosedError,
    CachePolicy,
    JSONSerializer,
    ProviderEntry,
    ProviderError,
)
from hermes_cache.engine import CacheEngine


def _engine(fake_provider, *, policy=None):
    clock = [0.0]
    engine = CacheEngine(
        fake_provider,
        serializer=JSONSerializer(),
        policy=policy or CachePolicy(l1_enabled=False),
        clock=lambda: clock[0],
    )
    return engine, clock


def test_set_get_delete(fake_provider):
    engine, _ = _engine(fake_provider)
    assert engine.set("k", {"v": 1}) is True
    assert engine.get("k") == {"v": 1}
    assert engine.delete("k") is True
    assert engine.get("k", "miss") == "miss"


def test_ttl_expiry(fake_provider):
    engine, clock = _engine(fake_provider)
    engine.set("k", "v", ttl=10)
    clock[0] = 9
    assert engine.get("k") == "v"
    clock[0] = 10
    assert engine.get("k", "miss") == "miss"
    assert engine.stats().expired == 1
    assert fake_provider.data == {}


def test_zero_ttl_never_expires(fake_provider):
    engine, clock = _engine(fake_provider)
    engine.set("k", "v", ttl=0)
    clock[0] = 10**9
    assert engine.get("k") == "v"


def test_namespace_isolation(fake_provider):
    engine, _ = _engine(fake_provider)
    engine.set("k", "a", namespace="one")
    engine.set("k", "b", namespace="two")
    assert engine.get("k", namespace="one") == "a"
    assert engine.get("k", namespace="two") == "b"


def test_get_or_set_caches_none(fake_provider):
    engine, _ = _engine(fake_provider)
    calls = []
    value = engine.get_or_set("none", lambda: calls.append(1) or None)
    assert value is None
    assert calls == [1]
    assert engine.contains("none") is True
    assert engine.get("none", "miss") is None


def test_get_many_and_set_many(fake_provider):
    engine, _ = _engine(fake_provider)
    assert engine.set_many({"a": 1, "b": 2}) == 2
    assert engine.get_many(["a", "b", "c"]) == {"a": 1, "b": 2}
    assert engine.stats().misses == 1


def test_corrupt_payload_self_heals(fake_provider):
    engine, _ = _engine(fake_provider)
    fake_provider.set(ProviderEntry("ns", "bad", b"{", "json.v1", 0.0, None))
    assert engine.get("bad", "miss", namespace="ns") == "miss"
    assert ("ns", "bad") not in fake_provider.data
    assert engine.stats().errors == 1


def test_strict_provider_error_raises(fake_provider):
    class Failing:
        def get(self, namespace, key):
            raise ProviderError("boom")

    engine = CacheEngine(cast(Any, Failing()), policy=CachePolicy(fail_open=False))
    with pytest.raises(ProviderError):
        engine.get("k")


def test_close_rejects_use(fake_provider):
    engine, _ = _engine(fake_provider)
    engine.close()
    with pytest.raises(CacheClosedError):
        engine.get("k")
