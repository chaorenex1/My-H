"""Factory, profile guard and provider-independence tests."""

from __future__ import annotations

import os
import stat

import pytest

from hermes_cache import CachePolicy, default_cache_path, get_cache, reset_cache_registry
from hermes_cache import factory as factory_mod


def test_default_path_does_not_create(cache_home):
    path = default_cache_path()
    assert path == cache_home / "cache" / "cache.db"
    assert not path.exists()


def test_get_cache_roundtrip(cache_home):
    cache = get_cache()
    assert cache.set("k", {"v": 1}) is True
    assert cache.get("k") == {"v": 1}
    assert (cache_home / "cache" / "cache.db").exists()


def test_registry_identity_and_policy_split(cache_home):
    first = get_cache()
    assert get_cache() is first
    policy = CachePolicy(default_ttl=30)
    second = get_cache(policy=policy)
    assert second is not first
    assert get_cache(policy=CachePolicy(default_ttl=30)) is second
    assert get_cache(policy=CachePolicy(default_ttl=60)) is not second


def test_closed_cache_is_recreated(cache_home):
    first = get_cache()
    first.close()
    second = get_cache()
    assert second is not first
    assert second.closed is False


def test_provider_is_constructed_inside_factory(cache_home, fake_provider, monkeypatch):
    monkeypatch.setattr(factory_mod, "SQLiteProvider", lambda path: fake_provider)
    cache = get_cache(policy=CachePolicy(default_ttl=1))
    cache.set("k", "v")
    assert ("default", "k") in fake_provider.data


def test_missing_named_profile_not_materialized(cache_home):
    (cache_home / "config.yaml").write_text("", encoding="utf-8")
    profiles = cache_home / "profiles"
    profiles.mkdir()
    ghost = profiles / "ghost"
    with pytest.raises(FileNotFoundError):
        get_cache(home=ghost)
    assert not (ghost / "cache").exists()


def test_tombstoned_named_profile_not_materialized(cache_home):
    from hermes_constants import mark_named_profile_deleted

    (cache_home / "config.yaml").write_text("", encoding="utf-8")
    profiles = cache_home / "profiles"
    profiles.mkdir()
    ghost = profiles / "ghost"
    ghost.mkdir()
    mark_named_profile_deleted(ghost)
    with pytest.raises(FileNotFoundError):
        get_cache(home=ghost)
    assert not (ghost / "cache").exists()


@pytest.mark.platforms("posix")
def test_cache_dir_permissions(cache_home):
    from hermes_constants import (
        _container_or_chmod_skipped,
        get_managed_system,
    )

    if get_managed_system(cache_home) is not None:
        pytest.skip("managed install controls directory mode")
    if _container_or_chmod_skipped():
        pytest.skip("container/chmod-skip policy controls directory mode")
    if os.environ.get("HERMES_HOME_MODE", "").strip():
        pytest.skip("explicit HERMES_HOME_MODE controls directory mode")
    get_cache()
    mode = stat.S_IMODE(os.stat(cache_home / "cache").st_mode)
    assert mode == 0o700


def test_reset_registry_closes(cache_home):
    cache = get_cache()
    reset_cache_registry()
    assert cache.closed is True
