"""``commands.catalog`` carries a flat, deduped ``entries`` view of the display ``pairs``.

Each row keeps the pair's source (builtin/quick/plugin/skill), its aliases (reverse ``canon`` plus
the registry ``CommandDef.aliases``), the registry args hint, the desktop argument mode and the
``sub`` subcommands — without changing any of the legacy catalog fields.
"""
from importlib import import_module
from types import SimpleNamespace

from tui_gateway.command_discovery import command_catalog


def _catalog(*, load_cfg=None, plugin=None, skills=None):
    stubs = {
        "hermes_cli.plugins": SimpleNamespace(get_plugin_commands=lambda: plugin or {}),
        "agent.skill_commands": SimpleNamespace(
            get_interactive_skill_commands=lambda: skills or {}),
    }
    return command_catalog(load_cfg=load_cfg or dict,
                           module_loader=lambda name: stubs.get(name) or import_module(name))


def _entry(result, name):
    return next(e for e in result["entries"] if e["name"] == name)


def test_entries_cover_every_display_pair_once():
    result = _catalog(plugin={"probe": {"description": "Probe", "argument_mode": "options"}},
                      skills={"/myskill": {"description": "Do it", "name": "myskill"}})
    names = [e["name"] for e in result["entries"]]
    assert len(names) == len(result["pairs"])
    assert len(names) == len({n.lower() for n in names})
    assert {e["source"] for e in result["entries"]} == {"builtin", "plugin", "skill"}


def test_registry_entry_carries_aliases_hint_mode_and_subcommands():
    entry = _entry(_catalog(), "context")
    assert entry["source"] == "builtin"
    assert entry["aliases"] == ["ctx"]
    assert entry["hint"] == "[all]"
    assert entry["argument_mode"] == "options"
    assert entry["subcommands"] == ["all"]


def test_quick_plugin_and_skill_entries_have_no_registry_hint():
    result = _catalog(
        load_cfg=lambda: {"quick_commands": {"hello": {"type": "exec", "command": "echo hi"}}},
        plugin={"probe": {"description": "Probe", "args_hint": "<target>",
                          "argument_mode": "options"}},
        skills={"/myskill": {"description": "Do it", "name": "myskill"}})
    quick, plugin, skill = _entry(result, "hello"), _entry(result, "probe"), _entry(result, "myskill")
    assert (quick["source"], quick["hint"]) == ("quick", None)
    assert (plugin["source"], plugin["hint"]) == ("plugin", None)
    assert plugin["argument_mode"] == "options"
    assert (skill["source"], skill["hint"]) == ("skill", None)
