"""SQLite provider specifics."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cache import (
    EvictionPolicy,
    ProviderEntry,
    ProviderError,
    SQLiteProvider,
)


def _entry(key, payload=b"value", *, created=0.0, expires=None):
    return ProviderEntry("ns", key, payload, "raw.v1", created, expires)


def test_reopen_persists(cache_dir):
    path = cache_dir / "cache.db"
    first = SQLiteProvider(path)
    first.set(_entry("k"))
    first.close()
    second = SQLiteProvider(path)
    try:
        entry = second.get("ns", "k")
        assert entry is not None
        assert entry.payload == b"value"
    finally:
        second.close()


def test_get_many_chunks(cache_dir):
    provider = SQLiteProvider(cache_dir / "cache.db")
    try:
        keys = [f"k{i}" for i in range(1200)]
        provider.set_many([_entry(key, b"v") for key in keys])
        found = provider.get_many("ns", keys)
        assert len(found) == 1200
        assert provider.delete_many("ns", keys) == 1200
    finally:
        provider.close()


def test_threaded_connections(cache_dir):
    provider = SQLiteProvider(cache_dir / "cache.db")
    try:
        def worker(worker_id):
            for index in range(25):
                key = f"{worker_id}-{index}"
                provider.set(_entry(key))
                entry = provider.get("ns", key)
                assert entry is not None
                assert entry.payload == b"value"

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(4)))
        assert provider.stats().entries == 100
    finally:
        provider.close()


def test_missing_parent_rejected(tmp_path):
    provider = SQLiteProvider(tmp_path / "missing" / "cache.db")
    with pytest.raises(ProviderError):
        provider.get("ns", "k")


def test_schema_version(cache_dir):
    path = cache_dir / "cache.db"
    provider = SQLiteProvider(path)
    try:
        provider.stats()
    finally:
        provider.close()
    conn = sqlite3.connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == 2


def test_v1_schema_migrates_in_place(cache_dir):
    path = cache_dir / "cache.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE cache_entries (
                namespace  TEXT    NOT NULL,
                key        TEXT    NOT NULL,
                payload    BLOB    NOT NULL,
                codec      TEXT    NOT NULL,
                created_at REAL    NOT NULL,
                expires_at REAL,
                size_bytes INTEGER NOT NULL,
                PRIMARY KEY (namespace, key)
            );
            INSERT INTO cache_entries
                (namespace, key, payload, codec, created_at, expires_at, size_bytes)
            VALUES ('ns', 'k', X'76', 'raw.v1', 2.0, NULL, 1);
            """
        )
        conn.commit()
    finally:
        conn.close()
    provider = SQLiteProvider(path)
    try:
        entry = provider.get("ns", "k")
        assert entry is not None
        assert entry.payload == b"v"
        assert provider.stats().entries == 1
    finally:
        provider.close()
    conn = sqlite3.connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(cache_entries)")
        }
        hits, accessed = conn.execute(
            "SELECT hits, accessed_at FROM cache_entries"
        ).fetchone()
    finally:
        conn.close()
    assert version == 2
    assert {"hits", "accessed_at"} <= columns
    assert (hits, accessed) == (0, 2.0)


def test_trim_fifo_removes_oldest(cache_dir):
    provider = SQLiteProvider(cache_dir / "cache.db")
    try:
        provider.set_many(
            [
                _entry("a", b"12345", created=1.0),
                _entry("b", b"12345", created=2.0),
                _entry("c", b"12345", created=3.0),
            ]
        )
        assert provider.trim_to_bytes(5, 100.0, EvictionPolicy.FIFO) == 2
        assert provider.stats().entries == 1
        assert provider.get("ns", "c") is not None
    finally:
        provider.close()


def test_trim_lru_keeps_recently_accessed(cache_dir):
    provider = SQLiteProvider(cache_dir / "cache.db")
    try:
        provider.set_many(
            [
                _entry("a", b"12345", created=1.0),
                _entry("b", b"12345", created=2.0),
                _entry("c", b"12345", created=3.0),
            ]
        )
        provider.touch("ns", "a", 10.0, count_hit=False)
        provider.touch("ns", "c", 11.0, count_hit=False)
        assert provider.trim_to_bytes(5, 100.0, EvictionPolicy.LRU) == 2
        assert provider.get("ns", "c") is not None
    finally:
        provider.close()


def test_trim_lfu_keeps_most_frequent(cache_dir):
    provider = SQLiteProvider(cache_dir / "cache.db")
    try:
        provider.set_many(
            [
                _entry("a", b"12345", created=1.0),
                _entry("b", b"12345", created=2.0),
                _entry("c", b"12345", created=3.0),
            ]
        )
        provider.touch("ns", "b", 10.0, count_hit=True)
        provider.touch("ns", "b", 11.0, count_hit=True)
        provider.touch("ns", "c", 12.0, count_hit=True)
        assert provider.trim_to_bytes(5, 100.0, EvictionPolicy.LFU) == 2
        assert provider.get("ns", "b") is not None
    finally:
        provider.close()


def test_trim_lru_does_not_evict_fresh_write(cache_dir):
    provider = SQLiteProvider(cache_dir / "cache.db")
    try:
        provider.set(_entry("old", b"12345", created=1.0))
        provider.touch("ns", "old", 10.0, count_hit=False)
        provider.set(_entry("fresh", b"12345", created=50.0))
        assert provider.trim_to_bytes(5, 100.0, EvictionPolicy.LRU) == 1
        assert provider.get("ns", "fresh") is not None
    finally:
        provider.close()


def test_trim_rr_removes_to_budget(cache_dir):
    provider = SQLiteProvider(cache_dir / "cache.db")
    try:
        provider.set_many(
            [
                _entry("a", b"12345", created=1.0),
                _entry("b", b"12345", created=2.0),
                _entry("c", b"12345", created=3.0),
            ]
        )
        assert provider.trim_to_bytes(5, 100.0, EvictionPolicy.RR) == 2
        assert provider.stats().entries == 1
    finally:
        provider.close()


def test_reconnects_after_connection_closed(cache_dir):
    provider = SQLiteProvider(cache_dir / "cache.db")
    try:
        provider.set(_entry("k"))
        provider._local.conn.close()
        entry = provider.get("ns", "k")
        assert entry is not None
        assert entry.payload == b"value"
    finally:
        provider.close()


def test_corrupt_database_is_quarantined(cache_dir):
    path = cache_dir / "cache.db"
    path.write_bytes(b"not a sqlite database" * 100)
    provider = SQLiteProvider(path)
    try:
        assert provider.get("ns", "k") is None
        assert provider.stats().entries == 0
    finally:
        provider.close()
    quarantined = [
        item for item in cache_dir.iterdir()
        if item.name.startswith("cache.db.corrupt-")
    ]
    assert len(quarantined) == 1


def test_cross_instance_writes_are_visible(cache_dir):
    path = cache_dir / "cache.db"
    first = SQLiteProvider(path)
    second = SQLiteProvider(path)
    try:
        first.set(_entry("k"))
        entry = second.get("ns", "k")
        assert entry is not None
        assert entry.payload == b"value"
    finally:
        second.close()
        first.close()


@pytest.mark.platforms("posix")
def test_fork_child_can_reopen(cache_dir):
    path = cache_dir / "cache.db"
    provider = SQLiteProvider(path)
    try:
        provider.stats()  # open the parent's connection before forking
        pid = os.fork()
        if pid == 0:
            try:
                provider.set(_entry("child", b"forked"))
                entry = provider.get("ns", "child")
                assert entry is not None
                assert entry.payload == b"forked"
            except BaseException:
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        provider.close()
    reopened = SQLiteProvider(path)
    try:
        entry = reopened.get("ns", "child")
        assert entry is not None
        assert entry.payload == b"forked"
    finally:
        reopened.close()
