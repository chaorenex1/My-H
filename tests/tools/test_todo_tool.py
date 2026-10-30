"""Tests for the two-level todo tool (phases → items)."""

import json

from tools.todo_tool import LEGACY_PHASE_NAME, TodoStore, flatten_phases, todo_tool


def _plan(*phases):
    """``_plan(("Research", item1, item2), ("Build", item3))`` -> phase payload."""
    return [{"phase": name, "items": list(items)} for name, *items in phases]


def _item(i, status="pending", content=None):
    return {"id": i, "content": content or f"task {i}", "status": status}


class TestWriteAndRead:
    def test_write_replaces_plan(self):
        store = TodoStore()
        items = store.write(_plan(
            ("Research", _item("1"), _item("2", "in_progress")),
        ))
        assert [(i["id"], i["status"], i["phase"]) for i in items] == [
            ("2", "in_progress", "Research"), ("1", "pending", "Research")]

    def test_write_preserves_phase_order(self):
        store = TodoStore()
        store.write(_plan(("Research", _item("1")), ("Implement", _item("2")), ("Verify", _item("3"))))
        assert [p["phase"] for p in store.phases()] == ["Research", "Implement", "Verify"]
        assert [items[0]["id"] for items in (p["items"] for p in store.phases())] == ["1", "2", "3"]

    def test_write_deduplicates_duplicate_ids(self):
        store = TodoStore()
        result = store.write(_plan(
            ("Research",
             _item("1", content="First version"),
             _item("2", content="Other task"),
             _item("1", "in_progress", content="Latest version")),
        ))
        assert result == [
            {"id": "1", "content": "Latest version", "status": "in_progress", "phase": "Research"},
            {"id": "2", "content": "Other task", "status": "pending", "phase": "Research"},
        ]

    def test_write_moves_active_item_before_earlier_pending_step(self):
        store = TodoStore()
        result = store.write(_plan(
            ("Implement",
             _item("1", "completed", content="Already done"),
             _item("2", content="Verify freed space"),
             _item("3", "in_progress", content="Move archives to Trash")),
        ))
        assert [i["id"] for i in result] == ["1", "3", "2"]

    def test_phase_lift_is_scoped_to_its_own_phase(self):
        store = TodoStore()
        result = store.write(_plan(
            ("Research", _item("1", "in_progress")),
            ("Build", _item("2"), _item("3")),
        ))
        assert [i["id"] for i in result] == ["1", "2", "3"]

    def test_missing_ids_are_assigned(self):
        store = TodoStore()
        result = store.write(_plan(("Research", {"content": "no id"}, {"content": "also none"})))
        assert [i["id"] for i in result] == ["1", "2"]
        assert result[0]["status"] == "pending"

    def test_legacy_flat_list_still_writes(self):
        """Old flat payloads replay (REST callers / old transcripts)."""
        store = TodoStore()
        result = store.write([_item("1"), _item("2")])
        assert {i["phase"] for i in result} == {LEGACY_PHASE_NAME}


class TestHasItems:
    def test_empty_store(self):
        assert TodoStore().has_items() is False

    def test_non_empty_store(self):
        store = TodoStore()
        store.write(_plan(("Research", _item("1"))))
        assert store.has_items() is True


class TestFormatForInjection:
    def test_empty_returns_none(self):
        assert TodoStore().format_for_injection() is None

    def test_phase_heading_and_active_items_only(self):
        store = TodoStore()
        store.write(_plan(
            ("Research", _item("1", "completed", content="Do thing"), _item("2", content="Next")),
            ("Build", _item("3", "in_progress", content="Working")),
        ))
        text = store.format_for_injection()
        assert text is not None
        assert "## Research" in text
        assert "## Build" in text
        # Completed items are filtered out of injection
        assert "[x]" not in text
        assert "Do thing" not in text
        # Active items are included
        assert "[ ]" in text
        assert "[>]" in text
        assert "Next" in text
        assert "Working" in text

    def test_finished_phase_heading_is_dropped(self):
        store = TodoStore()
        store.write(_plan(("Done", _item("1", "completed")), ("Next", _item("2"))))
        text = store.format_for_injection()
        assert text is not None
        assert "## Done" not in text
        assert "## Next" in text


class TestMergeMode:
    def test_update_existing_by_id(self):
        store = TodoStore()
        store.write(_plan(("Research", _item("1", content="Original"))))
        store.write(_plan(("Research", {"id": "1", "status": "completed"})), merge=True)
        items = store.read()
        assert len(items) == 1
        assert items[0]["status"] == "completed"
        assert items[0]["content"] == "Original"

    def test_merge_appends_new(self):
        store = TodoStore()
        store.write(_plan(("Research", _item("1", content="First"))))
        store.write(_plan(("Build", _item("2", content="Second"))), merge=True)
        items = store.read()
        assert [i["id"] for i in items] == ["1", "2"]
        assert items[1]["phase"] == "Build"

    def test_merge_moves_item_to_another_phase(self):
        store = TodoStore()
        store.write(_plan(("Research", _item("1")), ("Build", _item("2"))))
        store.write(_plan(("Build", {"id": "1", "status": "completed"})), merge=True)
        assert [i["phase"] for i in store.read() if i["id"] == "1"] == ["Build"]

    def test_merge_reorders_active_item_ahead_of_earlier_pending_step(self):
        store = TodoStore()
        store.write(_plan(("Implement",
                           _item("1", "completed", content="Completed"),
                           _item("2", content="Verify freed space"),
                           _item("3", content="Move archives to Trash"))))
        result = store.write(_plan(("Implement", {"id": "3", "status": "in_progress"})), merge=True)
        assert [i["id"] for i in result] == ["1", "3", "2"]


class TestTodoToolFunction:
    def test_read_mode(self):
        store = TodoStore()
        store.write(_plan(("Research", _item("1", content="Task"))))
        result = json.loads(todo_tool(store=store))
        assert result["phases"] == [{"phase": "Research", "items": [
            {"id": "1", "content": "Task", "status": "pending", "phase": "Research"}]}]
        assert result["summary"] == {"total": 1, "phases": 1, "pending": 1, "in_progress": 0,
                                     "completed": 0, "cancelled": 0}
        assert result["revision"] == 1

    def test_write_returns_grouped_phases(self):
        store = TodoStore()
        result = json.loads(todo_tool(phases=_plan(("Research", _item("1")), ("Build", _item("2"))), store=store))
        assert [p["phase"] for p in result["phases"]] == ["Research", "Build"]

    def test_no_store_returns_error(self):
        assert "error" in json.loads(todo_tool())

    def test_non_list_phases_returns_error(self):
        result = json.loads(todo_tool(phases={"phase": "x"}, store=TodoStore()))  # type: ignore[arg-type]
        assert "error" in result


class TestFlattenPhases:
    def test_flattens_current_and_legacy_payloads(self):
        phases = [{"phase": "A", "items": [_item("1")]}, {"phase": "B", "items": [_item("2")]}]
        assert [i["id"] for i in flatten_phases({"phases": phases})] == ["1", "2"]
        assert [i["id"] for i in flatten_phases({"todos": [_item("3")]})] == ["3"]
        assert flatten_phases({"todos": []}) == []


class TestTodoStoreSnapshots:
    def test_revision_only_advances_when_state_changes(self):
        store = TodoStore()
        plan = _plan(("Research", _item("1", content="Task")))

        store.write(plan)
        first = store.snapshot()
        store.write(plan)

        assert first["revision"] == 1
        assert store.snapshot() == first
        assert store.snapshot()["todos"][0]["phase"] == "Research"

    def test_restore_adopts_a_trusted_revision(self):
        store = TodoStore()
        store.restore(_plan(("Research", _item("1", content="Task"))), revision=7)

        assert store.snapshot()["revision"] == 7
        store.write(_plan(("Research", _item("1", "completed", content="Task"))))
        assert store.snapshot()["revision"] == 8

    def test_restore_legacy_flat_todos(self):
        store = TodoStore()
        store.restore([_item("1", content="Task")], revision=3)
        assert store.snapshot() == {
            "todos": [{"id": "1", "content": "Task", "status": "pending", "phase": LEGACY_PHASE_NAME}],
            "revision": 3,
        }

    def test_restore_flat_snapshot_keeps_each_phase(self):
        store = TodoStore()
        store.restore([
            {"id": "1", "content": "Read", "status": "pending", "phase": "Research"},
            {"id": "2", "content": "Write", "status": "pending", "phase": "Build"},
        ], revision=5)
        assert [item["phase"] for item in store.read()] == ["Research", "Build"]


class TestTodoStoreBounds:
    """Bounds on persisted todo state (GHSA-5g4g-6jrg-mw3g hardening)."""

    def test_oversized_content_is_truncated(self):
        from tools.todo_tool import MAX_TODO_CONTENT_CHARS
        store = TodoStore()
        store.write(_plan(("Research", {"id": "1", "content": "A" * 50001, "status": "pending"})))
        item = store.read()[0]
        assert len(item["content"]) <= MAX_TODO_CONTENT_CHARS
        assert item["content"].endswith("… [truncated]")

    def test_injection_block_is_bounded(self):
        from tools.todo_tool import MAX_TODO_CONTENT_CHARS
        store = TodoStore()
        store.write(_plan(("Research", {"id": "1", "content": "A" * 50001, "status": "pending"})))
        inj = store.format_for_injection()
        assert inj is not None
        assert len(inj) < MAX_TODO_CONTENT_CHARS + 200

    def test_item_count_is_bounded(self):
        from tools.todo_tool import MAX_TODO_ITEMS
        store = TodoStore()
        store.write(_plan(("Bulk", *(_item(str(i), content=f"task {i}") for i in range(5000)))))
        assert len(store.read()) == MAX_TODO_ITEMS

    def test_phase_count_is_bounded(self):
        from tools.todo_tool import MAX_TODO_PHASES
        store = TodoStore()
        store.write([{"phase": f"P{i}", "items": [_item(str(i))]} for i in range(100)])
        assert len(store.phases()) == MAX_TODO_PHASES

    def test_normal_plan_is_unchanged(self):
        store = TodoStore()
        store.write(_plan(("Build",
                           {"id": "1", "content": "write the report", "status": "in_progress"},
                           {"id": "2", "content": "review PR", "status": "pending"})))
        items = store.read()
        assert [i["content"] for i in items] == ["write the report", "review PR"]
        assert "[truncated]" not in items[0]["content"]


class TestRejectEmptyContent:
    """Empty or missing content must fail the call instead of substituting
    '(no description)'. See #44496."""

    def test_write_rejects_empty_content(self):
        store = TodoStore()
        try:
            store.write(_plan(("Research", {"id": "1", "content": "", "status": "pending"})))
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "empty or missing content" in str(e)

    def test_write_rejects_missing_content_key(self):
        store = TodoStore()
        try:
            store.write(_plan(("Research", {"id": "2", "status": "pending"})))
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "empty or missing content" in str(e)

    def test_write_rejects_whitespace_content(self):
        store = TodoStore()
        try:
            store.write(_plan(("Research", _item("3", content="   "))))
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "empty or missing content" in str(e)

    def test_phase_name_is_required(self):
        store = TodoStore()
        try:
            store.write([{"phase": "  ", "items": [_item("1")]}])
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "non-empty 'phase' name" in str(e)

    def test_partial_write_does_not_mutate_existing_plan(self):
        store = TodoStore()
        store.write(_plan(("Research", _item("1", content="Keep me"),
                           _item("2", "completed", content="Also keep me"))))
        try:
            store.write(_plan(("Research", _item("1", content="Updated"),
                               {"id": "2", "content": "", "status": "pending"})))
        except ValueError:
            pass
        items = store.read()
        assert [i["content"] for i in items] == ["Keep me", "Also keep me"]
        assert [i["status"] for i in items] == ["pending", "completed"]

    def test_todo_tool_returns_error_on_empty_content(self):
        result = json.loads(todo_tool(
            phases=_plan(("Research", {"id": "1", "content": "", "status": "pending"})),
            store=TodoStore(),
        ))
        assert "error" in result
        assert "empty or missing content" in result["error"]

    def test_merge_new_item_rejects_empty_content(self):
        store = TodoStore()
        store.write(_plan(("Research", _item("1", content="Existing"))))
        try:
            store.write(_plan(("Research", {"id": "2", "content": "", "status": "pending"})), merge=True)
            assert False, "should have raised ValueError"
        except ValueError as e:
            assert "empty or missing content" in str(e)

    def test_merge_existing_item_keeps_content_on_status_only_update(self):
        store = TodoStore()
        store.write(_plan(("Research", _item("1", content="Keep this"))))
        store.write(_plan(("Research", {"id": "1", "status": "completed"})), merge=True)
        items = store.read()
        assert items[0]["content"] == "Keep this"
        assert items[0]["status"] == "completed"

    def test_valid_content_still_works(self):
        store = TodoStore()
        items = store.write(_plan(("Research", _item("1", content="A real task"))))
        assert items[0]["content"] == "A real task"
