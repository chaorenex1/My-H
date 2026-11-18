from __future__ import annotations

from typing import cast

from agent.judgment.rendering import format_prompt, render_template
from agent.judgment.text import render_judgment_state
from agent.judgment.types import JudgmentState


def test_question_template_interpolation_uses_javascript_scalar_strings():
    template = "{{integer_float}} {{negative_zero}} {{small}} {{large}} {{flag}} {{nil}}"

    assert render_template(template, {
        "integer_float": 1.0,
        "negative_zero": -0.0,
        "small": 1e-7,
        "large": 1e21,
        "flag": True,
        "nil": None,
    }) == "1 0 1e-7 1e+21 true"


def test_omp_prompt_format_preserves_one_blank_and_removes_blank_runs_and_closing_gap():
    assert format_prompt("first\n\nsecond") == "first\n\nsecond"
    assert format_prompt("first\n\n\nsecond") == "first\nsecond"
    assert format_prompt("first\n\n</state>") == "first\n</state>"
    assert format_prompt("first\n\n</State>") == "first\n\n</State>"


def test_omp_prompt_format_compacts_tables_but_preserves_fenced_code():
    source = (
        "| name | value |\n| :-- | --: |\n| cache | 1.0 |  \n\n"
        "```md\n| raw | table |\n| --- | --- |  \n```\n"
    )

    assert format_prompt(source) == (
        "|name|value|\n|:---|---:|\n|cache|1.0|\n\n"
        "```md\n| raw | table |\n| --- | --- |\n```"
    )


def test_state_rendering_matches_bun_yaml_for_nested_maps_lists_numbers_and_strings():
    state = {
        "nested": {
            "objects": [{"name": "a", "score": 1}, {"nested": {"v": "x"}}],
            "nestedLists": [[1, 2], [3, [4, 5]], []],
            "emptyObjects": [{}, []],
            "zero": [0, -0.0, 0.0, -0.0],
            "numbers": [1e-6, 1e-7, 1e20, 1e21],
            "strings": ["a#b", "1e", "0b10", "1_000", "12:30", "你好 😀", "a\x01b"],
            "controls": ["\0", "\u0085", "\u00a0", "\u2028", "\u2029"],
        }
    }

    assert render_judgment_state(cast(JudgmentState, state)) == (
        '<nested>\n'
        'objects:\n'
        '  - name: a\n'
        '    score: 1\n'
        '  - nested:\n'
        '      v: x\n'
        'nestedLists:\n'
        '  - - 1\n'
        '    - 2\n'
        '  - - 3\n'
        '    - - 4\n'
        '      - 5\n'
        '  - []\n'
        'emptyObjects:\n'
        '  - {}\n'
        '  - []\n'
        'zero:\n'
        '  - +0\n'
        '  - -0\n'
        '  - +0\n'
        '  - -0\n'
        'numbers:\n'
        '  - 0.000001\n'
        '  - 1e-7\n'
        '  - 100000000000000000000\n'
        '  - 1e+21\n'
        'strings:\n'
        '  - "a#b"\n'
        '  - "1e"\n'
        '  - 0b10\n'
        '  - 1_000\n'
        '  - 12:30\n'
        '  - 你好 😀\n'
        '  - "a\\x01b"\n'
        'controls:\n'
        '  - "\\0"\n'
        '  - "\\N"\n'
        '  - "\\_"\n'
        '  - "\\L"\n'
        '  - "\\P"\n'
        '</nested>'
    )


def test_state_scalar_rendering_uses_javascript_string_and_xml_attribute_rules():
    state = {
        "integer_float": 1.0,
        "negative_zero": -0.0,
        "small_fixed": 1e-6,
        "small_exponent": 1e-7,
        "large_fixed": 1e20,
        "large_exponent": 1e21,
        "nil": None,
        "enabled": True,
        "owner's & key": "safe",
    }

    assert render_judgment_state(cast(JudgmentState, state)) == (
        "<integer_float>1</integer_float>\n"
        "<negative_zero>0</negative_zero>\n"
        "<small_fixed>0.000001</small_fixed>\n"
        "<small_exponent>1e-7</small_exponent>\n"
        "<large_fixed>100000000000000000000</large_fixed>\n"
        "<large_exponent>1e+21</large_exponent>\n"
        "<nil>null</nil>\n"
        "<enabled>true</enabled>\n"
        '<field name="owner\'s &amp; key">safe</field>'
    )
