"""Tests for defensive type coercion in todo_tool (issue #14185).

Covers three crash patterns:
1. phases is a JSON string instead of a list
2. a phase's items list contains non-dict items (e.g., bare strings)
3. Well-formed input continues to work unchanged
"""

import json
from types import SimpleNamespace

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from tools.todo_tool import TodoStore, todo_tool


class TestJsonStringCoercion:
    """Guard 1: the inline todo_list dispatch recovers when the LLM sends phases as a JSON string."""

    def test_json_string_is_parsed_into_list(self):
        store = TodoStore()
        phases_str = json.dumps([
            {"phase": "Research", "items": [
                {"id": "t1", "content": "Do A", "status": "pending"},
                {"id": "t2", "content": "Do B", "status": "in_progress"},
            ]},
        ])
        agent = SimpleNamespace(_todo_store=store)
        result = json.loads(INLINE_TOOL_EXECUTORS["todo_list"](
            agent, {"phases": phases_str}, InlineToolContext(effective_task_id="t")))
        assert "error" not in result
        assert result["summary"]["total"] == 2
        # Order-agnostic: TodoStore._normalize_order may lift the in_progress
        # item ahead of earlier pending rows (#42649); this test only pins
        # JSON-string coercion, not ordering.
        by_id = {t["id"]: t for t in result["phases"][0]["items"]}
        assert set(by_id) == {"t1", "t2"}
        assert by_id["t2"]["status"] == "in_progress"

    def test_non_list_non_string_returns_error(self):
        result = json.loads(todo_tool(phases=42, store=TodoStore()))  # type: ignore[arg-type]
        assert "error" in result


class TestNonDictListItems:
    """Guards 2 & 3: _validate and _dedupe_by_id handle non-dict items."""

    def test_string_item_in_list_does_not_crash(self):
        store = TodoStore()
        result = store.write([{"phase": "Research", "items": ["not-a-dict"]}])
        assert len(result) == 1
        assert result[0]["id"] == "1"
        assert result[0]["content"] == "(invalid item)"
        assert result[0]["status"] == "pending"

    def test_non_dict_items_via_todo_tool(self):
        """End-to-end: non-dict items produce valid output, not a crash."""
        store = TodoStore()
        result = json.loads(todo_tool(
            phases=[{"phase": "Research", "items": ["bad", "also bad"]}], store=store))  # type: ignore
        assert "error" not in result
        assert result["summary"]["total"] == 2
        assert result["summary"]["pending"] == 2

    def test_bare_string_phase_list_returns_error(self):
        result = json.loads(todo_tool(phases=["bad"], store=TodoStore()))  # type: ignore[arg-type]
        assert "error" in result


class TestWellFormedInputUnchanged:
    """Regression: normal usage must not be affected by the guards."""

    def test_normal_write_and_read(self):
        store = TodoStore()
        phases = [{"phase": "Research", "items": [
            {"id": "a", "content": "First", "status": "pending"},
            {"id": "b", "content": "Second", "status": "in_progress"},
        ]}]
        result = json.loads(todo_tool(phases=phases, store=store))
        assert result["summary"]["total"] == 2
        assert result["summary"]["pending"] == 1
        assert result["summary"]["in_progress"] == 1

    def test_dedup_still_works(self):
        store = TodoStore()
        result = store.write([{"phase": "Research", "items": [
            {"id": "1", "content": "v1", "status": "pending"},
            {"id": "1", "content": "v2", "status": "in_progress"},
        ]}])
        assert len(result) == 1
        assert result[0]["content"] == "v2"
