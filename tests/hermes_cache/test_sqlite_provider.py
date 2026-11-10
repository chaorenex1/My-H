"""SQLite provider specifics."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cache import ProviderEntry, ProviderError, SQLiteProvider


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
    assert version == 1


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
