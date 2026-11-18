"""``workspace_tree`` caching: session freeze, cross-session reuse, fail open (ADR-19 D10)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.prompts import project_context as pc
from hermes_cache import EvictionPolicy, reset_cache_registry


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    reset_cache_registry()
    pc.clear_workspace_tree_cache()
    yield
    pc.clear_workspace_tree_cache()
    reset_cache_registry()


def _coding_repo(tmp_path):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("x = 1\n")
    return tmp_path


def _counting_scan(monkeypatch):
    calls: list[tuple[str, int]] = []
    real = pc._scan_workspace_tree

    def scan(root, depth):
        calls.append((str(root), depth))
        return real(root, depth)

    monkeypatch.setattr(pc, "_scan_workspace_tree", scan)
    return calls


def test_second_call_in_one_session_reuses_the_frozen_tree(tmp_path, monkeypatch):
    repo = _coding_repo(tmp_path)
    calls = _counting_scan(monkeypatch)
    first = pc.workspace_tree(repo, session_id="s1")
    assert first[0]
    assert len(calls) == 1
    # A TTL expiry mid-session must not change bytes already in the cached prefix.
    pc._workspace_tree_cache().delete(f"{repo.resolve()}|d=3")
    assert pc.workspace_tree(repo, session_id="s1") == first
    assert len(calls) == 1


def test_a_new_session_reuses_the_cross_session_cache(tmp_path, monkeypatch):
    repo = _coding_repo(tmp_path)
    calls = _counting_scan(monkeypatch)
    first = pc.workspace_tree(repo, session_id="s1")
    second = pc.workspace_tree(repo, session_id="s2")
    assert second == first
    assert len(calls) == 1


def test_cache_failure_falls_back_to_a_live_scan(tmp_path, monkeypatch):
    repo = _coding_repo(tmp_path)

    def unavailable():
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(pc, "_workspace_tree_cache", unavailable)
    tree, truncated = pc.workspace_tree(repo, session_id="fail-open")
    assert "a.py" in tree
    assert truncated is False


def test_namespace_is_created_with_the_documented_ttl_and_lru(monkeypatch):
    seen: dict = {}

    class _Namespace:
        def get(self, key, default=None):
            return default

        def set(self, key, value, ttl=None):
            return True

    class _Cache:
        def namespace(self, name, **kwargs):
            seen["name"], seen["kwargs"] = name, kwargs
            return _Namespace()

    monkeypatch.setattr("hermes_cache.get_cache", lambda: _Cache())
    pc._workspace_tree_cache()
    assert seen["name"] == "prompt_workspace_tree"
    assert seen["kwargs"] == {
        "default_ttl": 3600,
        "max_entries": 32,
        "eviction": EvictionPolicy.LRU,
    }


def test_cached_value_is_a_json_serializable_tree(tmp_path, monkeypatch):
    repo = _coding_repo(tmp_path)
    store: dict = {}

    class _Namespace:
        def get(self, key, default=None):
            return store.get(key, default)

        def set(self, key, value, ttl=None):
            store[key] = json.loads(json.dumps(value))
            return True

    class _Cache:
        def namespace(self, name, **kwargs):
            return _Namespace()

    monkeypatch.setattr("hermes_cache.get_cache", lambda: _Cache())
    tree, truncated = pc.workspace_tree(repo, session_id="json")
    assert store[f"{repo.resolve()}|d=3"] == {"tree": tree, "truncated": truncated}


def test_two_builds_in_one_session_are_byte_identical(tmp_path, monkeypatch):
    from agent import system_prompt
    from agent.prompts import renderer

    _coding_repo(tmp_path)
    agent = SimpleNamespace(skip_context_files=False, platform="cli", provider="p", model="m",
                            session_id="same-session", valid_tool_names=set(),
                            _bot_mode_protocol=True, _session_title_hint="", _session_db=None,
                            _memory_store=None, _memory_manager=None, _user_profile_enabled=True,
                            _auto_load_skills_resolved=True, _auto_load_skills_result=("", [], []),
                            _agent_definition_metadata=None, enabled_toolsets=(), is_subagent=False)
    monkeypatch.setattr(system_prompt, "resolve_context_cwd", lambda **_kwargs: tmp_path)
    monkeypatch.setattr("agent.prompts.rules.project_languages", lambda _path: {"py"})
    monkeypatch.setattr(renderer, "rule_project_languages", lambda _path: {"py"})
    calls = _counting_scan(monkeypatch)

    first = system_prompt.build_system_prompt_parts(agent)["stable"]
    pc._workspace_tree_cache().delete(f"{tmp_path.resolve()}|d=3")
    second = system_prompt.build_system_prompt_parts(agent)["stable"]
    assert first == second
    assert "<workspace-tree>" in first
    assert len(calls) == 1
