"""ADR-10 P1: custom transcript rows, viewer opt-in and provider conversion."""

import pytest

from agent.custom_messages import project_for_api
from hermes_state import SessionDB
from tui_gateway import server

_history_to_messages = server._history_to_messages


def _new_custom_row(**overrides):
    row = {
        "role": "custom",
        "content": "[adviser — advisory only]\n- [concern · reviewer] re-read the failing test\n[end adviser]",
        "custom_type": "advisor",
        "display": True,
        "display_only": False,
        "details": {"notes": [{"note": "re-read the failing test", "severity": "concern",
                               "advisor": "reviewer"}]},
        "attribution": "agent",
        "legacy_role": "user",
        "timestamp": 1.0,
        "_row_id": 7,
    }
    row.update(overrides)
    return row


def test_custom_row_projects_as_custom_always():
    row = _new_custom_row()

    projected = _history_to_messages([row])
    assert projected == [{
        "role": "custom", "text": row["content"], "timestamp": 1.0, "row_id": 7,
        "custom_type": "advisor", "display": True, "display_only": False,
        "attribution": "agent", "legacy_role": "user", "details": row["details"],
    }]
    # ADR-10 P5: the legacy marker pair never reaches the wire.
    assert "display_kind" not in projected[0] and "display_metadata" not in projected[0]


def test_legacy_display_row_normalizes_to_custom():
    row = {
        "role": "user",
        "content": "[adviser — advisory only]\n- [nit] tighten the loop\n[end adviser]",
        "display_kind": "adviser",
        "display_metadata": {"severity": "nit"},
        "timestamp": 1.0,
        "_row_id": 8,
    }
    projected = _history_to_messages([row])
    assert projected[0]["role"] == "custom"
    assert projected[0]["custom_type"] == "advisor"
    assert projected[0]["details"]["notes"] == [{"note": "tighten the loop", "severity": "nit"}]
    assert "display_kind" not in projected[0] and "display_metadata" not in projected[0]


def test_synthetic_markers_are_the_only_legacy_shape_left():
    steer = {"role": "user", "content": "[OUT-OF-BAND] focus on the numbers",
             "display_kind": "steer", "timestamp": 1.0, "_row_id": 9}
    auto = {"role": "user", "content": "[System note: interrupted]", "display_kind": "auto_continue",
            "timestamp": 2.0, "_row_id": 10}
    projected = _history_to_messages([steer, auto])
    assert projected[0]["synthetic_kind"] == "steer"
    assert projected[0]["live_steered"] is True
    assert projected[1]["synthetic_kind"] == "auto_continue"
    assert all("display_kind" not in message and "display_metadata" not in message
               for message in projected)


def test_custom_fields_survive_the_durable_round_trip(tmp_path):
    from types import SimpleNamespace

    from agent.session_persistence import _db_flush_row

    row = _new_custom_row()
    durable = _db_flush_row(SimpleNamespace(), row, False)
    assert durable["custom_type"] == "advisor"
    assert durable["details"] == row["details"]
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("s1", source="tui", model="m")
        db.append_messages_batch("s1", [durable])
        loaded = db.get_messages_as_conversation("s1", include_row_ids=True)[0]
    assert loaded["role"] == "custom"
    assert loaded["custom_type"] == "advisor"
    assert loaded["display"] is True
    assert loaded["details"] == row["details"]
    assert loaded["legacy_role"] == "user"
    # The wire projection through the DB row is the same custom shape.
    projected = _history_to_messages([loaded])
    assert projected[0]["custom_type"] == "advisor"
    assert projected[0]["details"] == row["details"]


@pytest.mark.parametrize("custom_type,attribution,expected", [
    ("advisor", "agent", "developer"),
    ("todo-reminder", "agent", "developer"),
    ("skill-prompt", "user", "user"),
    ("model-switch", "agent", None),
])
def test_custom_rows_convert_to_provider_roles(custom_type, attribution, expected):
    message = {"role": "custom", "content": "body", "custom_type": custom_type,
               "attribution": attribution, "details": {"x": 1}, "display": True}
    projected = project_for_api(message, custom_messages=True)
    if expected is None:
        assert projected == []
    else:
        assert projected[0]["role"] == expected
        assert projected[0]["content"] == "body"
        assert "custom_type" not in projected[0]
        assert "details" not in projected[0]


def test_legacy_display_rows_convert_only_in_a_custom_session():
    row = {"role": "user", "content": "advice", "display_kind": "adviser"}
    assert project_for_api(dict(row), custom_messages=True)[0]["role"] == "developer"
    assert project_for_api(dict(row), custom_messages=False)[0]["role"] == "user"


def test_unknown_and_seated_roles_never_reach_a_provider():
    assert project_for_api({"role": "bash_execution", "content": "ls"}, custom_messages=True) == []
    assert project_for_api({"role": "hook_message", "content": "x"}, custom_messages=False) == []


def test_display_projection_is_always_custom():
    from gateway.session_controls import display_projection

    rows = [{
        "row_id": 5, "role": "custom", "content": "advice", "custom_type": "advisor",
        "display": True, "details": {"notes": [{"note": "advice"}]}, "legacy_role": "user",
    }]
    shown = display_projection(rows, None)
    assert shown[0]["role"] == "custom"
    assert shown[0]["custom_type"] == "advisor"
    assert shown[0]["row_id"] == 5
    assert "display_kind" not in shown[0] and "display_metadata" not in shown[0]


def test_transcript_notice_is_one_line_for_the_status_path():
    from types import SimpleNamespace

    from gateway import local_viewer_events

    published: list[tuple[str, dict]] = []
    turn = SimpleNamespace(_publish_execution=lambda event, payload: published.append((event, payload)),
                           _approval_owner=None)
    agent = SimpleNamespace(log_prefix="", _print_fn=None, thinking_callback=None,
                            reaction_callback=None, reasoning_callback=None,
                            tool_gen_callback=None, transcript_notice_callback=None)
    local_viewer_events.wire(turn, agent)
    agent._in_status_line = True
    agent._print_fn("compacting context")
    agent._in_status_line = False
    agent.transcript_notice_callback("lifecycle", "compacting context", origin="_emit_status")
    notices = [payload for event, payload in published if event == "transcript.notice"]
    assert [payload["text"] for payload in notices] == ["compacting context"]
    assert notices[0]["level"] == "info"
    assert any(event == "agent.print" for event, _ in published)
    # A direct print that never reaches status_callback still produces one notice.
    agent._print_fn("done")
    assert [payload["text"] for event, payload in published if event == "transcript.notice"] == [
        "compacting context", "done"]


def test_adviser_card_writes_the_custom_row_in_a_custom_session(monkeypatch):
    from types import SimpleNamespace

    from agent.adviser_delivery import append_card
    from gateway import session_policy

    monkeypatch.setattr(session_policy, "active_policy",
                        lambda: SimpleNamespace(custom_messages=True))
    agent = SimpleNamespace(messages=[], _session_messages=None,
                            _flush_messages_to_session_db=lambda *_: None)
    text = "[adviser — advisory only]\n- [concern · reviewer] re-read the failing test\n[end adviser]"
    assert append_card(agent, text, severity="concern")
    row = agent.messages[-1]
    assert row["role"] == "custom"
    assert row["custom_type"] == "advisor"
    assert row["display_only"] is False
    assert row["legacy_role"] == "user"
    assert row["details"]["notes"] == [
        {"note": "re-read the failing test", "severity": "concern", "advisor": "reviewer"}]
    assert row["details"]["severity"] == "concern"


def test_openai_chat_downgrades_developer_for_models_without_it():
    from agent.transports.chat_completions import _normalize_developer_roles

    rows = [{"role": "system", "content": "s"}, {"role": "developer", "content": "d"},
            {"role": "user", "content": "u"}]
    assert [m["role"] for m in _normalize_developer_roles(rows, "glm-4.7")] == [
        "system", "system", "user"]
    assert [m["role"] for m in _normalize_developer_roles(rows, "gpt-5.2")] == [
        "system", "developer", "user"]


def test_legacy_display_only_notice_is_dropped_in_a_custom_session():
    row = {"role": "user", "content": "switched to m-2", "display_kind": "model_switch"}
    assert project_for_api(dict(row), custom_messages=True) == []
    # A session without custom messages keeps its legacy replay untouched.
    assert project_for_api(dict(row), custom_messages=False) == [row]


def test_legacy_skill_row_details_read_the_invocation_not_the_body():
    scaffolded = (
        '[IMPORTANT: The user has invoked the "work" skill, indicating they '
        "want you to follow its instructions. The full skill content is "
        "loaded below.]\n\n# /work\n\nSPIN UP A WORKTREE.\n\n"
        "The user has provided the following instruction alongside the skill "
        "invocation: fix the title leak"
    )
    row = {
        "role": "user",
        "content": scaffolded,
        "display_kind": "skill_invocation",
        "timestamp": 1.0,
    }
    projected = _history_to_messages([row])
    assert projected[0]["custom_type"] == "skill-prompt"
    assert projected[0]["details"]["name"] == "work"
    assert projected[0]["details"]["args"] == "fix the title leak"
    assert projected[0]["text"] == "/work fix the title leak"


def test_codex_history_seed_keeps_developer_rows():
    from agent.codex_runtime_history_seed import render_history_seed

    seed = render_history_seed([
        {"role": "user", "content": "hi"},
        {"role": "developer", "content": "advice"},
        {"role": "user", "content": "next"},
    ])
    assert "[DEVELOPER]\nadvice" in seed


def test_custom_row_between_a_tool_call_and_its_result_is_transparent():
    from types import SimpleNamespace

    from agent.agent_runtime_helpers import repair_message_sequence

    messages = [
        {"role": "user", "content": "run"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "custom", "custom_type": "advisor", "content": "advice", "display": True,
         "details": {"notes": [{"note": "advice"}]}},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "body"},
        {"role": "assistant", "content": "done"},
    ]
    repaired = [dict(m) for m in messages]
    assert repair_message_sequence(SimpleNamespace(), repaired) == 0
    assert repaired[1]["tool_calls"]
    assert repaired == messages


def test_role_seat_table_drives_the_history_filter_and_capabilities():
    from gateway.panel_inventory import TRANSCRIPT_BLOCKS, transcript_capabilities
    from tui_gateway.contracts.transcript_roles import TRANSCRIPT_ROLE_NAMES, TRANSCRIPT_ROLES

    assert len(TRANSCRIPT_ROLES) == 11
    assert "system" not in TRANSCRIPT_ROLE_NAMES
    roles = {block.id for block in TRANSCRIPT_BLOCKS if block.id in set(TRANSCRIPT_ROLE_NAMES)}
    assert roles == set(TRANSCRIPT_ROLE_NAMES)
    blocks = transcript_capabilities()
    assert blocks["custom"]["supported"] is True
    assert blocks["bash_execution"]["supported"] is False
    assert blocks["bash_execution"]["needs"]
    assert blocks["todo_reminder"]["events"] == ["todo.reminder"]
    assert blocks["todo_reminder"]["supported"] is True
    assert blocks["cache_miss_marker"]["supported"] is True
    assert blocks["served_model_marker"]["supported"] is True


def test_transcript_capabilities_cover_the_d6_catalog():
    """ADR-10 D6/P4: every named block is advertised, and an unsupported one says what it needs."""
    from gateway.panel_inventory import TRANSCRIPT_BLOCKS, transcript_capabilities

    blocks = transcript_capabilities()
    assert len(blocks) == len(TRANSCRIPT_BLOCKS)
    expected = {
        "adviser", "skill_invocation", "todo_reminder", "compaction_summary", "recap",
        "ttsr_notification", "late_diagnostics", "cache_miss_marker", "served_model_marker",
        "stripped_tool_calls", "reaction", "tool_execution", "notice", "background_tan",
        "collab_prompt", "reply_write",
    }
    assert expected <= set(blocks)
    for block_id, capability in blocks.items():
        if not capability["supported"]:
            assert capability["needs"], block_id
            assert capability["status"] == "unsupported", block_id
        else:
            assert capability["status"] in {"ready", "partial"}, block_id
            assert capability["events"] or capability["fields"], block_id
    # The producers landed by P1-P3 are ready; the ADR-3/5/9 blocks stay unsupported.
    for block_id in ("adviser", "skill_invocation", "tool_execution", "notice", "ttsr_notification"):
        assert blocks[block_id]["status"] == "ready", block_id
        assert blocks[block_id]["fields"], block_id
    assert blocks["tool_execution"]["events"] == ["tool.start", "tool.progress", "tool.complete"]
    assert blocks["notice"]["events"] == ["transcript.notice"]
    assert blocks["compaction_summary"]["status"] == "partial"
    for block_id in ("recap", "late_diagnostics", "background_tan"):
        assert blocks[block_id]["supported"] is False, block_id
        assert blocks[block_id]["needs"], block_id
    assert blocks["ttsr_notification"]["supported"] is True
    assert blocks["ttsr_notification"]["events"] == ["ttsr.triggered"]
    assert blocks["late_diagnostics"]["events"] == ["diagnostics.late"]
    assert blocks["background_tan"]["events"] == [
        "tangent.dispatched", "tangent.complete", "message.start"]
