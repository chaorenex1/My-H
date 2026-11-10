"""L1 cache tests."""

from __future__ import annotations

from hermes_cache.l1 import MemoryL1


def test_lru_eviction_moves_used_entry():
    l1 = MemoryL1(max_entries=2, max_bytes=100, max_age=None)
    l1.put("ns", "a", b"a", "raw.v1", None, 0.0)
    l1.put("ns", "b", b"b", "raw.v1", None, 0.0)
    assert l1.get("ns", "a", 0.0) is not None
    evicted = l1.put("ns", "c", b"c", "raw.v1", None, 0.0)
    assert evicted == 1
    assert l1.get("ns", "b", 0.0) is None
    assert l1.get("ns", "a", 0.0) is not None


def test_ttl_expiry():
    l1 = MemoryL1(max_entries=2, max_bytes=100, max_age=None)
    l1.put("ns", "a", b"a", "raw.v1", 10.0, 0.0)
    assert l1.get("ns", "a", 9.0) is not None
    assert l1.get("ns", "a", 10.0) is None
    assert l1.size == 0


def test_max_age_forces_refresh():
    l1 = MemoryL1(max_entries=2, max_bytes=100, max_age=5.0)
    l1.put("ns", "a", b"a", "raw.v1", None, 0.0)
    assert l1.get("ns", "a", 5.0) is not None
    assert l1.get("ns", "a", 6.0) is None


def test_byte_budget_evicts():
    l1 = MemoryL1(max_entries=10, max_bytes=6, max_age=None)
    l1.put("ns", "a", b"12345", "raw.v1", None, 0.0)
    evicted = l1.put("ns", "b", b"67890", "raw.v1", None, 0.0)
    assert evicted == 1
    assert l1.size == 1
    assert l1.size_bytes == 5


def test_clear_namespace():
    l1 = MemoryL1(max_entries=10, max_bytes=100, max_age=None)
    l1.put("a", "1", b"1", "raw.v1", None, 0.0)
    l1.put("b", "1", b"1", "raw.v1", None, 0.0)
    assert l1.clear_namespace("a") == 1
    assert l1.size == 1
