"""Sprint-5 acceptance: tool-card titles are a deterministic naming consumer (ADR-4 D13 / ADR-21 D2).

Contract boundary: the real ``card_titles()`` runs against a real ``config.yaml`` written under a
temporary ``HERMES_HOME`` and read by the real config loader. The only fakes are two ``call_llm``
recorders (zero calls is the assertion) and a counting **pass-through** wrapper over the real
``derive_title`` — never a stub, which would bypass the path under test.

Two traps this file closes on purpose:

* ``tools/eval_card_metadata.py`` binds ``derive_title`` at import time, so the wrapper must be
  installed on ``tools.eval_card_metadata.derive_title`` (patching ``agent.title_generator`` is a
  no-op for this path).
* ``_cache`` is module-level and keyed ``(session_id, tool_call_id, index)``. It is cleared before
  every config variant (not once per test), otherwise variants 2..n would replay one cached dict and
  the "identical across variants" assertion would compare a cache with itself.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hermes_yaml import safe_dump
from tools import eval_card_metadata

#: The three inputs every variant titles; the values are ``(name, args)``.
_TOOL_CALLS = {
    "terminal": ("terminal", {"command": "hermes pm install bun\nsecond line"}),
    "read_file": ("read_file", {"path": "docs/adr/ADR-4-eval-execution.md"}),
    "execute_code": ("execute_code", {
        "cells": [
            {"code": "x = 1\nprint(x)"},
            {"code": "const y = 2", "language": "js", "title": "JS cell"},
        ],
    }),
}

#: ``auxiliary`` blocks: the naming route/workload moves, the card titles must not.
_VARIANTS = {
    "default": {},
    "model-upgrade-off": {"title_generation": {"model_upgrade_enabled": False}},
    "generator-fork": {"title_generation": {"generator": "fork"}},
    "card-icons-boring": {"title_generation": {"card_icons": "boring"}},
    "title-route-pinned": {"title_generation": {
        "provider": "openrouter", "model": "nousresearch/hermes-title"}},
    "roles-pinned": {
        "tiny": {"provider": "openrouter", "model": "meta/tiny-pin"},
        "commit_message": {"provider": "nous", "model": "nous/commit-pin"},
        "smol": {"provider": "anthropic", "model": "anthropic/smol-pin"},
    },
}


def _use_auxiliary(monkeypatch, tmp_path: Path, name: str, auxiliary: dict) -> None:
    """Point the real config loader at a fresh home carrying this variant's ``auxiliary`` block."""
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(safe_dump({"auxiliary": auxiliary}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import config as config_module

    config_module._LOAD_CONFIG_CACHE.clear()
    config_module._RAW_CONFIG_CACHE.clear()


def _titles(prefix: str) -> dict[str, dict]:
    """``card_titles`` for every input, under ids unique to this variant."""
    return {
        tool: eval_card_metadata.card_titles(
            f"{prefix}-session", f"{prefix}-{tool}", name, args)
        for tool, (name, args) in _TOOL_CALLS.items()
    }


def _is_upgrade_thread(thread: threading.Thread) -> bool:
    return getattr(getattr(thread, "_target", None), "__module__", "") == "agent.title_generator"


def test_as1_card_titles_are_route_independent_and_never_call_a_model(monkeypatch, tmp_path):
    """AS-1: the same call titles identically in every ``auxiliary.title_generation`` variant,
    with two zeroed ``call_llm`` spies, a monotone ``_UPGRADE_THREADS`` set, and ``derive_title``
    as the single derivation entry point (wrapped, not replaced)."""
    from agent import auxiliary_client, title_generator
    from hermes_cli import config as config_module
    from hermes_cli.config import load_config

    calls = {"title_generator": 0, "auxiliary_client": 0}

    def _title_llm(*_args, **_kwargs):
        calls["title_generator"] += 1
        return ""

    def _aux_llm(*_args, **_kwargs):
        calls["auxiliary_client"] += 1
        return ""

    monkeypatch.setattr(title_generator, "call_llm", _title_llm)
    monkeypatch.setattr(auxiliary_client, "call_llm", _aux_llm)

    real_derive = eval_card_metadata.derive_title
    derived: list[str] = []

    def _counting_derive(text):
        derived.append(text)
        return real_derive(text)

    monkeypatch.setattr(eval_card_metadata, "derive_title", _counting_derive)

    baseline_threads = set(title_generator._UPGRADE_THREADS)
    baseline_targets = {thread for thread in threading.enumerate() if _is_upgrade_thread(thread)}

    baseline: dict[str, dict] = {}
    seen_routes = set()
    for variant, auxiliary in _VARIANTS.items():
        # Per-variant (not per-test) cache clear: variants must not compare a cache with itself.
        eval_card_metadata._cache.clear()
        _use_auxiliary(monkeypatch, tmp_path, variant, auxiliary)

        # The loader really reads this home, so a variant is a live config, not env noise.
        loaded = load_config()["auxiliary"]
        seen_routes.add((loaded["title_generation"]["provider"],
                         loaded["title_generation"]["model"],
                         loaded["tiny"]["provider"], loaded["tiny"]["model"]))
        if "tiny" in auxiliary:
            assert loaded["tiny"]["model"] == auxiliary["tiny"]["model"]

        derived.clear()
        titles = _titles(variant)

        # ``card_titles()`` returns its dict directly (no thread, no await): the value is the result.
        assert isinstance(titles, dict) and set(titles) == set(_TOOL_CALLS)
        assert titles["execute_code"]["cell_titles"] == ["x = 1", "JS cell"]
        # One derivation per title-bearing input; the explicit cell title is reused verbatim.
        assert len(derived) == 3, (variant, derived)

        if not baseline:
            baseline = titles
        else:
            assert titles == baseline, variant

        # Monotone, not equal: ``_UPGRADE_THREADS`` is a WeakSet and may drop a finished thread.
        assert set(title_generator._UPGRADE_THREADS) <= baseline_threads
        assert {thread for thread in threading.enumerate()
                if _is_upgrade_thread(thread)} <= baseline_targets, variant

    # At least one variant pins a live route, so the loop was not six identical reads.
    assert len(seen_routes) >= 2
    assert calls == {"title_generator": 0, "auxiliary_client": 0}
    assert eval_card_metadata.derive_title is _counting_derive  # wrapper stayed in place
    config_module._LOAD_CONFIG_CACHE.clear()
    config_module._RAW_CONFIG_CACHE.clear()


@pytest.fixture(autouse=True)
def _fresh_cache():
    eval_card_metadata._cache.clear()
    yield
    eval_card_metadata._cache.clear()


def test_as3_execute_code_titles_each_cell_and_explicit_title_wins():
    """AS-3 (a)-(c): per-cell titles, explicit title reused verbatim, top-level title first."""
    cells = eval_card_metadata.card_titles("as3-a", "as3-a", "execute_code", {
        "cells": [
            {"code": "x = 1\nprint(x)"},
            {"code": "const y = 2", "language": "js", "title": "JS cell"},
        ],
    })
    assert cells["cell_titles"] == ["x = 1", "JS cell"]
    assert cells["card_title"] == "x = 1"

    titled = eval_card_metadata.card_titles(
        "as3-b", "as3-b", "execute_code", {"code": "print(1)", "title": "One"})
    assert titled["card_title"] == "One"
    assert titled["cell_titles"] == ["One"]

    plain = eval_card_metadata.card_titles("as3-c", "as3-c", "execute_code", {"code": "print(2)"})
    assert plain["card_title"] == "print(2)"


def test_as3_result_cells_supply_titles_when_the_call_args_are_gone():
    """AS-3 (d): a replayed/rounded call with no args titles from the result's ``cells[]``."""
    titles = eval_card_metadata.card_titles("as3-d", "as3-d", "execute_code", None, result={
        "cells": [{"index": 0, "code": "df.head()"}]})
    assert titles["cell_titles"] == ["df.head()"]


def test_as3_ordinary_tools_and_empty_input_have_no_cell_titles():
    """AS-3 (e): ordinary tools only carry ``card_title``; no text yields ``{}``."""
    for index, (name, args) in enumerate((
            ("terminal", {"command": "hermes pm install bun\nsecond line"}),
            ("read_file", {"path": "docs/adr/ADR-4-eval-execution.md"}))):
        titles = eval_card_metadata.card_titles(f"as3-e{index}", f"as3-e{index}", name, args)
        assert set(titles) == {"card_title"}
        assert titles["card_title"]
    assert eval_card_metadata.card_titles("as3-empty", "as3-empty", "terminal", {}) == {}
    assert eval_card_metadata.card_titles("as3-none", "as3-none", "terminal", None) == {}


def test_as3_the_same_identity_reuses_the_cached_title():
    """AS-3 last clause: one ``(session, tool_call_id, index)`` keeps its first title."""
    first = eval_card_metadata.card_titles(
        "as3-cache", "as3-cache", "read_file", {"path": "docs/adr/ADR-4-eval-execution.md"})
    second = eval_card_metadata.card_titles(
        "as3-cache", "as3-cache", "read_file", {"path": "a different path"})
    assert first == second == {"card_title": "docs/adr/ADR-4-eval-execution.md"}
