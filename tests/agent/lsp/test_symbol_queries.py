"""Narrow LSP symbol query surface (ADR-8 D6)."""

from __future__ import annotations

from agent.lsp.client import _CLIENT_CAPABILITIES
from agent.lsp.manager import _normalise_locations, _normalise_symbols


def test_implementation_capability_is_advertised():
    assert "implementation" in _CLIENT_CAPABILITIES["textDocument"]


def test_normalise_locations_converts_to_one_based():
    result = _normalise_locations(
        [
            {
                "uri": "file:///tmp/project/mod.py",
                "range": {
                    "start": {"line": 9, "character": 4},
                    "end": {"line": 9, "character": 10},
                },
            },
            {
                "targetUri": "file:///tmp/project/other.py",
                "targetSelectionRange": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 2, "character": 1},
                },
            },
        ]
    )
    assert result[0]["start_line"] == 10
    assert result[0]["start_column"] == 5
    assert result[1]["path"].endswith("other.py")
    assert result[1]["end_line"] == 3


def test_normalise_symbols_flattens_children():
    result = _normalise_symbols(
        [
            {
                "name": "Outer",
                "kind": 5,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 10, "character": 0},
                },
                "selectionRange": {
                    "start": {"line": 0, "character": 6},
                    "end": {"line": 0, "character": 11},
                },
                "children": [
                    {
                        "name": "inner",
                        "kind": 6,
                        "range": {
                            "start": {"line": 2, "character": 4},
                            "end": {"line": 3, "character": 0},
                        },
                    }
                ],
            }
        ]
    )
    assert [item["name"] for item in result] == ["Outer", "inner"]
    assert result[1]["container_name"] == "Outer"
    assert result[1]["start_line"] == 3
