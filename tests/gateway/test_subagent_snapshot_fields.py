"""``subagent.list`` roster identity and ``subagent.tail`` byte ranges.

Both control paths answer the same wire keys: the legacy sidecar
(``tui_gateway/methods_subagents.py``) and the authority twin
(``gateway/session_ancillary.py``). Synthetic registry records / a temp transcript keep the
projection honest without a live child.
"""

from types import SimpleNamespace

import pytest

from tui_gateway.contracts.profiles_vault_complete_foreign_subagents import (
    SubagentSnapshot,
    SubagentTailResult,
)


def _record(**overrides):
    record = {
        "subagent_id": "sa-0-abc12345", "parent_id": None, "depth": 0, "goal": "ship the patch",
        "delegation_id": "del-1", "model": "test", "started_at": 100.0, "status": "running",
        "tool_count": 2, "last_tool": "read_file", "accepting_steer": True,
        "task_index": 0, "agent_role": "leaf", "last_update": 130.0,
    }
    record.update(overrides)
    return record


def _legacy_row(record):
    from tui_gateway.methods_subagents import _snapshot_row

    return _snapshot_row(record)


def _authority_row(record):
    from gateway.session_ancillary import _subagent_snapshot

    return _subagent_snapshot(record)


@pytest.fixture
def rpc(monkeypatch):
    """Legacy dispatch over a closed registry, mirroring a real spawn's record."""
    from tui_gateway import server
    from tools import async_delegation, delegate_tool_registry

    transport = SimpleNamespace(write=lambda frame: True)
    owner = {"session_key": "parent", "history": [], "transport": transport}
    monkeypatch.setattr(server, "_sessions", {"ui-owner": owner})
    # The roster's side reads (agent definitions / failed delegations) are out of scope here: they
    # would open the launch profile's real state.db.
    monkeypatch.setattr(server, "_agent_definitions", lambda roster_owner: [])
    monkeypatch.setattr(server, "_failed_delegations", lambda session_id, roster_owner: [])
    # Empty the shared registries in place: delegate_tool_progress binds ``_active_subagents`` by
    # name at import, so rebinding would fork the view the relay writes into.
    registers = (delegate_tool_registry._active_subagents, delegate_tool_registry._recent_subagents,
                 async_delegation._records)
    saved = [(reg, dict(reg)) for reg in registers]
    for reg in registers:
        reg.clear()

    def call(method, **params):
        return server.dispatch({"id": 1, "method": method,
                                "params": {"session_id": "ui-owner", **params}}, transport=transport)

    yield call, owner, transport
    for reg, snapshot in saved:
        reg.clear()
        reg.update(snapshot)


def test_legacy_and_authority_project_the_same_identity_fields():
    record = _record()
    legacy, authority = _legacy_row(record), _authority_row(record)
    assert legacy == authority
    assert authority == {
        "subagent_id": "sa-0-abc12345", "parent_id": None, "depth": 0, "goal": "ship the patch",
        "delegation_id": "del-1", "model": "test", "started_at": 100.0, "status": "running",
        "tool_count": 2, "last_tool": "read_file", "accepting_steer": True,
        "index": 0, "agent": "leaf", "description": "ship the patch", "task": "ship the patch",
        "assignment": "ship the patch", "session_file": None, "last_update": 130.0,
        "progress": {"tool_count": 2, "last_tool": "read_file", "status": "running"},
        "parent_tool_call_id": None,
    }


def test_every_contract_field_is_projected_and_absent_data_stays_none():
    for row in (_legacy_row({"subagent_id": "child", "goal": "do it"}),
                _authority_row({"subagent_id": "child", "goal": "do it"})):
        assert set(row) == set(SubagentSnapshot.model_fields)
        assert row["index"] is None and row["agent"] is None
        assert row["progress"] is None and row["last_update"] is None
        assert row["session_file"] is None and row["parent_tool_call_id"] is None
        assert row["description"] == row["task"] == row["assignment"] == "do it"


def test_progress_needs_one_live_counter_and_never_the_live_agent_object():
    live = SimpleNamespace()
    row = _legacy_row(_record(status=None, tool_count=None, last_tool="read_file", agent=live))
    assert row["progress"] == {"last_tool": "read_file"}
    # ``agent`` is the derived role; the registry's live agent object never reaches the wire.
    assert row["agent"] == "leaf"
    assert live not in row.values()


def test_list_handler_emits_identity_stamped_by_the_real_emitters(rpc):
    from tools.delegate_tool_child_run import _register_child
    from tools.delegate_tool_progress import _build_child_progress_callback
    from tools.delegate_tool_registry import _unregister_subagent

    call, owner, transport = rpc
    relay = _build_child_progress_callback(
        0, "owned task", SimpleNamespace(tool_progress_callback=lambda *a, **kw: None),
        subagent_id="sa-0-abcdef12")
    child = SimpleNamespace(_subagent_id="sa-0-abcdef12", _delegate_depth=1, model="test",
                            _delegate_role="leaf", _parent_tool_call_id="call-7",
                            tool_progress_callback=relay)
    _register_child(child, None, "owned task", owner_session_id="ui-owner",
                    owner_transport=transport, owner_session_record=owner)
    try:
        relay("tool.started", "read_file")
        rows = call("subagent.list")["result"]["subagents"]
        assert [row["subagent_id"] for row in rows] == ["sa-0-abcdef12"]
        row = rows[0]
        assert row["index"] == 0 and row["agent"] == "leaf"
        assert row["description"] == row["task"] == row["assignment"] == "owned task"
        assert row["parent_tool_call_id"] == "call-7"
        assert row["progress"] == {"tool_count": 1, "last_tool": "read_file", "status": "running"}
        # ``_register_child`` stamps last_update at registration; the relay re-stamps it per tool.
        assert row["started_at"] <= row["last_update"] <= row["started_at"] + 60
    finally:
        _unregister_subagent("sa-0-abcdef12")


def test_tail_reports_the_transcript_and_byte_range_it_read(tmp_path):
    from gateway.session_ancillary import subagent_tail
    from tui_gateway.methods_subagents import _tail_result

    transcript = tmp_path / "child.txt"
    transcript.write_bytes(b"x" * 18000 + b"\nTAIL-MARKER\n")
    records = [{"subagent_id": "child",
                "agent": SimpleNamespace(_live_transcript_path=transcript)}]

    authority = subagent_tail(records, "child")
    legacy = {"subagent_id": "child", **_tail_result(transcript)}
    assert authority == legacy
    size = transcript.stat().st_size
    assert authority["available"] is True and authority["truncated"] is True
    assert authority["session_file"] == str(transcript)
    assert authority["from_byte"] == size - 16384 and authority["next_byte"] == size
    assert len(authority["text"].encode()) == 16384
    assert authority["text"].endswith("TAIL-MARKER\n")
    assert set(authority) == set(SubagentTailResult.model_fields)


def test_tail_without_readable_bytes_keeps_available_false_and_the_real_path(tmp_path):
    from gateway.session_ancillary import subagent_tail
    from tui_gateway.methods_subagents import _tail_result

    assert subagent_tail([], "gone") == {"subagent_id": "gone", **_tail_result(None)}
    assert _tail_result(None) == {"available": False, "text": "", "truncated": False,
                                  "session_file": None, "from_byte": 0, "next_byte": 0}
    # The child still exposes a path the filesystem no longer serves (creation/cleanup race).
    missing = tmp_path / "missing.txt"
    raced = subagent_tail([{"subagent_id": "child",
                            "agent": SimpleNamespace(_live_transcript_path=missing)}], "child")
    assert raced == {"subagent_id": "child", "available": False, "text": "", "truncated": False,
                     "session_file": str(missing), "from_byte": 0, "next_byte": 0}


def test_legacy_tail_handler_writes_the_new_keys(rpc, tmp_path):
    from tools.delegate_tool_child_run import _register_child
    from tools.delegate_tool_registry import _unregister_subagent

    call, owner, transport = rpc
    transcript = tmp_path / "child.txt"
    transcript.write_text("live child output", encoding="utf-8")
    child = SimpleNamespace(_subagent_id="sa-1-fedcba98", _delegate_depth=1, model="test",
                            _live_transcript_path=str(transcript))
    _register_child(child, None, "owned", owner_session_id="ui-owner",
                    owner_transport=transport, owner_session_record=owner)
    try:
        payload = call("subagent.tail", subagent_id="sa-1-fedcba98")["result"]
        assert payload == {"subagent_id": "sa-1-fedcba98", "available": True,
                           "text": "live child output", "truncated": False,
                           "session_file": str(transcript), "from_byte": 0,
                           "next_byte": len("live child output")}
    finally:
        _unregister_subagent("sa-1-fedcba98")
    assert call("subagent.tail", subagent_id="sa-1-fedcba98")["result"] == {
        "subagent_id": "sa-1-fedcba98", "available": False, "text": "", "truncated": False,
        "session_file": None, "from_byte": 0, "next_byte": 0}


def test_interrupt_answers_cancelled_alongside_found(rpc):
    from tools.delegate_tool_child_run import _register_child
    from tools.delegate_tool_registry import _unregister_subagent

    call, owner, transport = rpc
    stopped = []
    child = SimpleNamespace(_subagent_id="child", _delegate_depth=1, model="test",
                            hard_interrupt=lambda message: stopped.append(message))
    _register_child(child, None, "owned", owner_session_id="ui-owner",
                    owner_transport=transport, owner_session_record=owner)
    try:
        accepted = call("subagent.interrupt", subagent_id="child")["result"]
        assert accepted == {"found": True, "subagent_id": "child", "cancelled": True}
        assert stopped  # the interrupt really reached the child
        # An already-finished (or never-owned) child is not a cancellation.
        _unregister_subagent("child")
        assert call("subagent.interrupt", subagent_id="child")["result"] == {
            "found": False, "subagent_id": "child", "cancelled": False}
    finally:
        _unregister_subagent("child")
