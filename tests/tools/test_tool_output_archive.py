"""ADR-16 P1: live tool-output archive.

Pins the D3 file contract — one ``<step>.<tool>.log`` per committed result, an
``<step>.<tool>-original.log`` only when the result was truncated, byte-identical for a
tool's own sanitized spill, redacted before disk, and never pointed at a file outside
this profile's cache.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_session_paths as session_paths
from hermes_state import SessionDB
from tools import tool_output_archive as archive
from tools.tool_output_artifacts import ArtifactSettings

SESSION_ID = "sess-archive-1"


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def agent(tmp_path, db):
    cwd = tmp_path / "project"
    cwd.mkdir()
    db.create_session(session_id=SESSION_ID, source="cli", cwd=str(cwd))
    agent = SimpleNamespace(
        session_id=SESSION_ID,
        session_start=None,
        session_cwd=str(cwd),
        _session_db=db,
        _tool_guardrails=SimpleNamespace(record_persisted_result=lambda *a, **k: None),
        _subdirectory_hints=SimpleNamespace(check_tool_call=lambda *a, **k: ""),
        verbose_logging=False,
        quiet_mode=True,
        tool_progress_mode="off",
        tool_progress_callback=None,
        tool_result_metadata_callback=None,
        _todo_reminder_awaiting_progress=True,
        _trim_after_tool_batch=False,
        _current_tool=None,
    )
    # The commit path calls these agent methods; the archive only needs them to be safe.
    agent._touch_activity = lambda *a, **k: None
    agent._tool_result_content_for_active_model = lambda name, result: result
    agent._append_guardrail_observation = lambda *a, **k: a[2]
    agent._record_file_mutation_result = lambda *a, **k: None
    return agent


def _bundle(agent) -> Path:
    directory = archive.bundle_dir_for_agent(agent)
    assert directory is not None
    return directory


def _files(agent) -> list[Path]:
    return sorted(p for p in _bundle(agent).iterdir() if p.is_file())


def test_untruncated_result_writes_only_the_log(agent):
    result = json.dumps({"output": "hello", "exit_code": 0})

    metadata = archive.archive_tool_result(agent, "terminal", result, result)

    assert metadata is not None
    assert metadata["step"] == 1
    assert metadata["truncated"] is False
    log_path, original_path = session_paths.tool_artifact_paths(_bundle(agent), 1, "terminal")
    assert log_path.read_text(encoding="utf-8") == result
    assert not original_path.exists()
    assert _files(agent) == [log_path]


def test_wrapper_truncation_writes_the_preview_and_the_original(agent):
    full = "x" * 200_000
    preview = "<persisted-output>\nPreview: x\n</persisted-output>"

    metadata = archive.archive_tool_result(agent, "mcp_search", full, preview)

    assert metadata is not None and metadata["truncated"] is True
    log_path, original_path = session_paths.tool_artifact_paths(_bundle(agent), 1, "mcp_search")
    assert log_path.read_text(encoding="utf-8") == preview
    assert original_path.read_text(encoding="utf-8") == full


def test_result_writes_are_redacted(agent):
    # Three leak shapes at once: a prefixed token in free text, an opaque secret under a
    # credential key, and the same inside a nested JSON string. All stay valid JSON.
    result = json.dumps({
        "output": "OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012",
        "api_key": "opaque-secret-value",
        "nested": json.dumps({"password": "hunter2"}),
    })

    archive.archive_tool_result(agent, "terminal", result, result)

    log_path, _ = session_paths.tool_artifact_paths(_bundle(agent), 1, "terminal")
    written = log_path.read_text(encoding="utf-8")
    assert "sk-proj-abc123def456ghi789jkl012" not in written
    assert "opaque-secret-value" not in written
    assert "hunter2" not in written
    parsed = json.loads(written)
    assert json.loads(parsed["nested"])["password"] == "***"


def test_terminal_spill_original_is_byte_identical(agent):
    from hermes_constants import get_hermes_home

    spill_dir = get_hermes_home() / "cache" / "terminal-output"
    spill_dir.mkdir(parents=True)
    spill = spill_dir / "out.log"
    # CRLF and a line the terminal redactor already masked: the archive copies bytes.
    spill.write_bytes(b"line one\r\nAPI_KEY=***\r\nline three\r\n")
    result = json.dumps({"output": "line one\r\n... [TRUNCATED] ...\r\nline three",
                         "full_output_path": str(spill)})

    metadata = archive.archive_tool_result(agent, "terminal", result, result)

    assert metadata is not None and metadata["truncated"] is True
    _, original_path = session_paths.tool_artifact_paths(_bundle(agent), 1, "terminal")
    assert original_path.read_bytes() == spill.read_bytes()


def test_execute_code_spill_original_is_redacted(agent):
    from hermes_constants import get_hermes_home

    spill_dir = get_hermes_home() / "cache" / "exec"
    spill_dir.mkdir(parents=True)
    spill = spill_dir / "stdout.txt"
    spill.write_text("token=sk-proj-abc123def456ghi789jkl012\n", encoding="utf-8")
    result = json.dumps({"status": "success", "stdout_spill_path": str(spill)})

    metadata = archive.archive_tool_result(agent, "execute_code", result, result)

    assert metadata is not None and metadata["truncated"] is True
    _, original_path = session_paths.tool_artifact_paths(_bundle(agent), 1, "execute_code")
    written = original_path.read_text(encoding="utf-8")
    assert "sk-proj-abc123def456ghi789jkl012" not in written


def test_spill_path_outside_the_cache_is_not_read(agent, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("do not archive", encoding="utf-8")
    result = json.dumps({"output": "short", "full_output_path": str(secret)})

    metadata = archive.archive_tool_result(agent, "terminal", result, result)

    assert metadata is not None
    assert metadata["truncated"] is False
    _, original_path = session_paths.tool_artifact_paths(_bundle(agent), 1, "terminal")
    assert not original_path.exists()


def test_disabled_archive_writes_nothing(agent, monkeypatch):
    monkeypatch.setattr(
        archive, "get_artifact_settings",
        lambda: ArtifactSettings(enabled=False, redact=True, exclude_tools=()),
    )

    assert archive.archive_tool_result(agent, "terminal", "x", "x") is None
    meta = agent._session_db.get_session_bundle_meta(SESSION_ID)
    assert meta is not None
    expected = session_paths.allocate_bundle_dir(agent.session_cwd, meta["started_at"], SESSION_ID)
    assert not expected.exists()


def test_steps_are_monotonic_across_reopen(agent, db):
    first = archive.archive_tool_result(agent, "search_files", '{"a":1}', '{"a":1}')
    second = archive.archive_tool_result(agent, "glob", '{"b":2}', '{"b":2}')
    assert first is not None and second is not None
    assert (first["step"], second["step"]) == (1, 2)
    assert db.next_tool_output_seq(SESSION_ID) == 3

    db.close()
    reopened = SessionDB(db.db_path)
    try:
        assert reopened.next_tool_output_seq(SESSION_ID) == 4
    finally:
        reopened.close()


def test_no_session_context_skips(monkeypatch):
    assert archive.archive_tool_result(SimpleNamespace(session_id=None), "terminal", "x", "x") is None


@pytest.mark.platforms("windows", "posix")
def test_atomic_write_leaves_no_temp_files(agent):
    archive.archive_tool_result(agent, "terminal", "payload", "payload")
    assert not [p for p in _bundle(agent).iterdir() if p.name.endswith(".tmp")]


def test_commit_tool_result_stamps_the_archive_descriptor(agent, monkeypatch):
    from agent import tool_executor

    monkeypatch.setattr(tool_executor, "_flush_session_db_after_tool_progress", lambda *a, **k: True)
    monkeypatch.setattr(tool_executor, "enforce_turn_budget", lambda *a, **k: None)
    result = json.dumps({"output": "commit path"})
    ref = tool_executor._ToolCallRef(
        name="terminal", args={}, task_id="task", call_id="call-1", trace=[])

    messages: list[dict] = []
    committed = tool_executor._commit_tool_result(
        agent, messages, ref, result, budget=tool_executor.DEFAULT_BUDGET,
        tool_duration=0.1, is_error=False, blocked=False, effect_disposition=None,
    )

    assert committed
    descriptor = messages[-1]["display_metadata"]["tool_output_artifacts"]
    assert descriptor["step"] == 1
    assert descriptor["truncated"] is False
    assert Path(descriptor["artifact_path"]).read_text(encoding="utf-8") == result


def test_blocked_calls_are_not_archived(agent, monkeypatch):
    from agent import tool_executor

    monkeypatch.setattr(tool_executor, "_flush_session_db_after_tool_progress", lambda *a, **k: True)
    ref = tool_executor._ToolCallRef(
        name="terminal", args={}, task_id="task", call_id="call-2", trace=[])

    messages: list[dict] = []
    tool_executor._commit_tool_result(
        agent, messages, ref, "denied", budget=tool_executor.DEFAULT_BUDGET,
        tool_duration=0.0, is_error=True, blocked=True, effect_disposition=None,
    )

    assert "tool_output_artifacts" not in (messages[-1].get("display_metadata") or {})
    assert agent._session_db.next_tool_output_seq(SESSION_ID) == 1  # no step consumed


def test_excluded_tools_are_not_archived(agent):
    assert archive.archive_tool_result(agent, "read_file", "payload", "payload") is None
    assert archive.archive_tool_result(agent, "agent_out", "payload", "payload") is None
    assert _files(agent) == []


def test_a_configured_archive_dir_has_its_own_sequence(tmp_path):
    """ADR-16 D4: a subagent archives under <bundle>/<topic>/ with its own steps."""
    directory = tmp_path / "bundle" / "topic"
    agent = SimpleNamespace(session_id="sub-1")  # no sessions row: the explicit dir is enough

    archive.configure_agent_archive(agent, directory)
    first = archive.archive_tool_result(agent, "terminal", "one", "one")
    second = archive.archive_tool_result(agent, "terminal", "two", "two")

    assert (first["step"], second["step"]) == (1, 2)
    assert sorted(path.name for path in directory.iterdir()) == [
        "1.terminal.log", "2.terminal.log"]

    # A restarted process re-seeds from disk and keeps counting up.
    fresh = SimpleNamespace(session_id="sub-1")
    archive.configure_agent_archive(fresh, directory)
    assert archive.archive_tool_result(fresh, "terminal", "three", "three")["step"] == 3
