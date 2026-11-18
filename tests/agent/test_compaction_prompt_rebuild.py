"""Compaction rebuild boundary: invalidate clears the cache and the plugin freeze."""

from types import SimpleNamespace

from agent.system_prompt import invalidate_system_prompt


def _agent(**over):
    base = dict(
        _cached_system_prompt="OLD PROMPT",
        _cached_system_prompt_static="OLD",
        _memory_store=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_invalidate_stashes_and_clears_plugin_snapshot():
    agent = _agent()
    agent._plugin_system_prompt_sections_snapshot = ("frozen-section",)
    invalidate_system_prompt(agent)
    assert not hasattr(agent, "_plugin_system_prompt_sections_snapshot")
    assert agent._plugin_system_prompt_sections_previous == ("frozen-section",)
    assert agent._cached_system_prompt is None
    assert agent._cached_system_prompt_static is None


def test_invalidate_reloads_memory_from_disk():
    calls = []
    store = SimpleNamespace(load_from_disk=lambda: calls.append("reload"))
    invalidate_system_prompt(_agent(_memory_store=store))
    assert calls == ["reload"]
