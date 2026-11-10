"""Regression tests for plugin toolset cache profile scope."""

from __future__ import annotations

import pytest

from hermes_cache import get_cache, reset_cache_registry


class FakePluginManager:
    _discovered = False

    def discover_and_load(self) -> None:
        self._discovered = True

    def get_portable_mcp_servers(self) -> dict[str, dict]:
        return {"mcp_one": {"command": "echo"}}


@pytest.fixture
def plugin_profiles(tmp_path, monkeypatch):
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / "config.yaml").write_text("", encoding="utf-8")
    profiles = launch / "profiles"
    profiles.mkdir()
    profile = profiles / "p1"
    profile.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(launch))
    from hermes_constants import reset_hermes_home_key_cache

    reset_hermes_home_key_cache()
    reset_cache_registry()
    yield launch, profile
    reset_cache_registry()
    reset_hermes_home_key_cache()


def _patch_plugins(monkeypatch, manager):
    from agent import safe_worker_policy
    from hermes_cli import plugins

    monkeypatch.setattr(plugins, "_background_discovery_thread", None)
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        plugins,
        "get_plugin_toolsets",
        lambda: [("toolset_a", "Toolset A", "desc")],
    )
    monkeypatch.setattr(safe_worker_policy, "safe_worker_enabled", lambda: False)
    return plugins


def test_background_discovery_writes_caller_profile(plugin_profiles, monkeypatch):
    launch, profile = plugin_profiles
    manager = FakePluginManager()
    plugins = _patch_plugins(monkeypatch, manager)

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(profile))
    try:
        plugins.start_background_plugin_discovery()
        thread = plugins._background_discovery_thread
        assert thread is not None
        thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        reset_hermes_home_override(token)

    assert (profile / "cache" / "cache.db").exists()
    assert not (launch / "cache" / "cache.db").exists()
    namespace = get_cache(home=profile).namespace("cli.plugins")
    assert namespace.get("toolset_keys") == ["toolset_a"]
    assert namespace.get("portable_mcp") == ["mcp_one"]


def test_persist_uses_explicit_home(plugin_profiles, monkeypatch):
    launch, profile = plugin_profiles
    manager = FakePluginManager()
    plugins = _patch_plugins(monkeypatch, manager)

    plugins._persist_plugin_toolset_keys(profile)

    assert (profile / "cache" / "cache.db").exists()
    assert not (launch / "cache" / "cache.db").exists()
    namespace = get_cache(home=profile).namespace("cli.plugins")
    assert namespace.get("toolset_keys") == ["toolset_a"]


class _AliveThread:
    def is_alive(self) -> bool:
        return True


def test_nowait_uses_cached_sets_without_blocking(plugin_profiles, monkeypatch):
    from hermes_cli import plugins

    class Manager:
        _discovered = False

    get_cache().namespace("cli.plugins").set("toolset_keys", ["cached_key"])
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: Manager())
    monkeypatch.setattr(plugins, "_background_discovery_thread", _AliveThread())
    monkeypatch.setattr(
        plugins,
        "discover_plugins",
        lambda: pytest.fail("cache hit must not block on discovery"),
    )

    assert plugins.get_plugin_toolset_keys_nowait() == {"cached_key"}


def test_nowait_falls_back_when_cache_misses(plugin_profiles, monkeypatch):
    from hermes_cli import plugins

    class Manager:
        _discovered = False

    called = []
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: Manager())
    monkeypatch.setattr(plugins, "_background_discovery_thread", _AliveThread())
    monkeypatch.setattr(plugins, "discover_plugins", lambda: called.append(True))
    monkeypatch.setattr(
        plugins,
        "get_plugin_toolsets",
        lambda: [("live_key", "Live", "desc")],
    )

    assert plugins.get_plugin_toolset_keys_nowait() == {"live_key"}
    assert called == [True]
