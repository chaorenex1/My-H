"""ADR-4 D13: deterministic, model-free card titles for tool rows and eval cells."""

from __future__ import annotations

from tools.eval_card_metadata import card_titles


def test_ordinary_tool_titles_use_the_first_meaningful_argument():
    titles = card_titles("s1", "call-1", "terminal", {"command": "hermes pm install bun\nsecond line"})
    assert titles["card_title"] == "hermes pm install bun"
    assert "cell_titles" not in titles


def test_execute_code_titles_each_cell_and_prefers_the_explicit_title():
    titles = card_titles("s1", "call-2", "execute_code", {
        "cells": [
            {"code": "x = 1\nprint(x)"},
            {"code": "const y = 2", "language": "js", "title": "JS cell"},
        ]
    })
    assert titles["cell_titles"][0] == "x = 1"
    assert titles["cell_titles"][1] == "JS cell"
    assert titles["card_title"] == "x = 1"


def test_single_code_cell_uses_the_call_title_then_the_code_line():
    titled = card_titles("s1", "call-3", "execute_code", {"code": "print(1)", "title": "One"})
    assert titled["card_title"] == "One" and titled["cell_titles"] == ["One"]
    untitled = card_titles("s1", "call-4", "execute_code", {"code": "print(2)"})
    assert untitled["card_title"] == "print(2)"


def test_result_cells_supply_titles_when_the_call_args_are_gone():
    titles = card_titles("s1", "call-5", "execute_code", None, result={
        "cells": [{"index": 0, "code": "df.head()", "language": "py"}]})
    assert titles["cell_titles"] == ["df.head()"]


def test_titles_are_cached_and_never_raise_on_odd_input():
    first = card_titles("s1", "call-6", "read_file", {"path": "docs/adr/ADR-4-eval-execution.md"})
    second = card_titles("s1", "call-6", "read_file", {"path": "changed"})
    assert first == second == {"card_title": "docs/adr/ADR-4-eval-execution.md"}
    assert card_titles("s1", "call-7", "terminal", {}) == {}
    assert card_titles("s1", "call-8", "terminal", None) == {}
    assert card_titles("s1", "call-9", "execute_code", None) == {}
