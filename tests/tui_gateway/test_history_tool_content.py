"""ADR-10 P2: tool-content depth, tool-row error projection, entries/outline and compaction.

The projection is the contract: ``session.history``/``session.resume`` only forward what
``_history_to_messages`` and ``history_page`` produce.
"""

from __future__ import annotations

import pytest

from tui_gateway import server

_history_to_messages = server._history_to_messages


def _tool_row(**overrides):
    row = {"role": "tool", "tool_call_id": "t1", "tool_name": "read_file", "content": "body",
           "timestamp": 10.0, "_row_id": 7}
    row.update(overrides)
    return row


def test_display_safe_projection_strips_the_untrusted_wrapper_and_elision_notice():
    from agent.tool_dispatch_helpers import (
        _maybe_append_elision_notice,
        _maybe_wrap_untrusted,
        display_tool_result_content,
    )

    raw = "a" * 1200 + " ... 3 more items"
    stored = _maybe_wrap_untrusted("web_search", _maybe_append_elision_notice("web_search", raw))
    assert "untrusted_tool_result" in stored
    assert display_tool_result_content("web_search", stored, images=False) == raw
    # A non-high-risk tool's stored text is already display-safe.
    assert display_tool_result_content("read_file", raw, images=False) == raw


def test_tool_content_modes_drop_or_keep_images():
    from agent.tool_dispatch_helpers import display_tool_result_content

    parts = [
        {"type": "text", "text": "screenshot"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    assert display_tool_result_content("browser_exec", parts, images=False) == [
        {"type": "text", "text": "screenshot"}]
    assert display_tool_result_content("browser_exec", parts, images=True) == [
        {"type": "text", "text": "screenshot"},
        {"type": "image", "data": "AAAA", "mimeType": "image/png"},
    ]


def test_oversized_or_non_data_images_fall_back_to_placeholders(monkeypatch):
    from agent import tool_dispatch_helpers

    monkeypatch.setattr(tool_dispatch_helpers, "_DISPLAY_IMAGE_BASE64_MAX", 3)
    oversized = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAAA"}}]
    assert tool_dispatch_helpers.display_tool_result_content("x", oversized, images=True) == [
        {"type": "text", "text": "[image]"}]
    remote = [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]
    assert tool_dispatch_helpers.display_tool_result_content("x", remote, images=True) == [
        {"type": "text", "text": "[image: https://example.test/a.png]"}]


def test_default_projection_keeps_todays_tool_payload_and_forwards_row_id():
    default = _history_to_messages([_tool_row()])
    explicit_none = _history_to_messages([_tool_row()], include_tool_content="none")
    assert default == explicit_none
    assert "content" not in default[0]
    assert default[0]["row_id"] == 7
    assert default[0]["name"] == "read_file"


def test_edit_rows_keep_their_result_under_the_default_mode():
    row = _tool_row(tool_name="write_file", content="wrote 3 lines")
    assert _history_to_messages([row])[0]["content"] == "wrote 3 lines"


def test_tool_content_text_and_full_projections():
    parts = [
        {"type": "text", "text": "output"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    row = _tool_row(content=parts)
    text = _history_to_messages([row], include_tool_content="text")[0]
    full = _history_to_messages([row], include_tool_content="full")[0]
    assert text["content"] == [{"type": "text", "text": "output"}]
    assert full["content"] == [
        {"type": "text", "text": "output"},
        {"type": "image", "data": "AAAA", "mimeType": "image/png"},
    ]


def test_is_error_is_projected_from_the_durable_metadata():
    row = _tool_row(display_metadata={"tool_result_metadata": {"is_error": True, "inline_diff": "+a"}})
    assert _history_to_messages([row])[0]["is_error"] is True
    # No verdict stored: the key is absent, not a guessed False.
    assert "is_error" not in _history_to_messages([_tool_row()])[0]


def test_stamp_tool_result_metadata_merges_without_rewriting_other_keys():
    from agent.message_metadata import stamp_tool_result_metadata

    message = {"display_metadata": {"reactions": [{"emoji": "x"}]}}
    stamp_tool_result_metadata(message, {"tool_result_metadata": {"inline_diff": "+a"}}, is_error=True)
    assert message["display_metadata"] == {
        "reactions": [{"emoji": "x"}],
        "tool_result_metadata": {"inline_diff": "+a", "is_error": True},
    }


def test_display_projection_forwards_tool_content_only_on_request():
    from gateway.session_controls import display_projection

    rows = [{"row_id": 3, "role": "tool", "tool_name": "read_file", "tool_call_id": "t1",
             "content": [{"type": "text", "text": "body"}]}]
    assert "content" not in display_projection(rows, None)[0]
    assert display_projection(rows, None, include_tool_content="full")[0]["content"] == [
        {"type": "text", "text": "body"}]


def test_history_entries_carry_tool_fields_and_outline_is_opt_in():
    from tui_gateway.history_paging import history_page

    messages = [
        {"role": "user", "text": "first prompt\nmore", "row_id": 1, "timestamp": 1.0},
        {"role": "assistant", "text": "", "row_id": 2},
        {"role": "tool", "name": "read_file", "context": "a.ts", "tool_call_id": "t1",
         "row_id": 3, "content": [{"type": "text", "text": "body"}],
         "args": {"path": "a.ts"}, "is_error": True},
    ]
    page, entries, cursor, outline = history_page(messages, after=None, limit=None, include_outline=True)
    assert page == messages and cursor is None
    assert [entry["entry_id"] for entry in entries] == [1, 2, 3]
    tool = entries[2]
    assert tool["role"] == "tool"
    assert tool["name"] == "read_file"
    assert tool["context"] == "a.ts"
    assert tool["tool_call_id"] == "t1"
    assert tool["args"] == {"path": "a.ts"}
    assert tool["is_error"] is True
    assert tool["content"] == [{"type": "text", "text": "body"}]
    assert [row["row_id"] for row in outline] == [1, 2, 3]
    assert [row["summary"] for row in outline] == ["first prompt", "", "read_file a.ts"]
    assert all(len(row["summary"]) <= 80 for row in outline)
    # Default: no outline key at all.
    assert history_page(messages, after=None, limit=None)[3] is None


def test_history_page_keeps_a_turn_with_its_tool_rows():
    from tui_gateway.history_paging import history_page

    messages = [
        {"role": "user", "text": "q", "row_id": 1},
        {"role": "assistant", "text": "a", "row_id": 2},
        {"role": "tool", "name": "read_file", "row_id": 3},
    ]
    page, entries, cursor, _ = history_page(messages, after=None, limit=2)
    assert [entry["entry_id"] for entry in entries] == [1, 2]
    assert cursor == 2
    assert page == messages[:2]
    page, entries, _, _ = history_page(messages, after=2, limit=2)
    assert page == messages[2:]
    assert [entry["entry_id"] for entry in entries] == [3]


def _compaction_rows():
    from agent.context_compressor import (
        COMPRESSED_SUMMARY_METADATA_KEY,
        SUMMARY_PREFIX,
        _MERGED_SUMMARY_DELIMITER,
    )

    standalone = {"role": "user", "content": f"{SUMMARY_PREFIX}\nsummary body",
                  COMPRESSED_SUMMARY_METADATA_KEY: True, "_row_id": 4, "timestamp": 5.0}
    merged = {"role": "user", "content": f"EARLIER ASK\n{_MERGED_SUMMARY_DELIMITER}\n{SUMMARY_PREFIX}\nbody",
              COMPRESSED_SUMMARY_METADATA_KEY: True, "_row_id": 6}
    return standalone, merged


def test_compaction_summary_is_opt_in_and_partial():
    standalone, merged = _compaction_rows()
    hidden = _history_to_messages([standalone])
    assert hidden == []
    shown = _history_to_messages([standalone], include_compaction=True)
    assert shown[0]["role"] == "compaction_summary"
    assert shown[0]["text"] == "summary body"
    assert "tokens_before" not in shown[0]  # ADR-5 P1 adds the stamp; the client defaults to 0
    assert shown[0]["row_id"] == 4
    # A stamped row carries ADR-5 D2's metadata (counters + the boundary row it replaced).
    stamped = {**standalone, "display_metadata": {"compaction": {
        "tokens_before": 120, "tokens_after": 40, "token_method": "estimate_rough", "method": "llm_summary"}}}
    row = _history_to_messages([stamped], include_compaction=True)[0]
    assert row["details"]["compaction"] == {
        "tokens_before": 120, "tokens_after": 40, "token_method": "estimate_rough",
        "method": "llm_summary", "boundary_row_id": 4}
    assert "display_metadata" not in row
    # A merged handoff keeps its recovered live content instead of becoming the summary block.
    recovered = _history_to_messages([merged], include_compaction=True)
    assert recovered[0]["role"] == "user"
    assert recovered[0]["text"] == "EARLIER ASK"


@pytest.mark.parametrize("mode", ["none", "text", "full"])
def test_default_compaction_flag_leaves_the_projection_unchanged(mode):
    standalone, _ = _compaction_rows()
    assert _history_to_messages([standalone], include_tool_content=mode) == []
