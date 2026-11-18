"""Git TUI on the session authority: command parsing, terminal scope and write validation.

The terminal backend is a fake ``terminal_tool`` so the test can prove the handler hands the
command to the SESSION's backend (``workdir``/``task_id``) instead of running git on the gateway.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from gateway import session_policy
from gateway.session_git import _DIFF_BYTES, _parse_branches, _parse_log, _parse_status
from tests.gateway.session_panel_helpers import panel_authority, policy


@pytest.fixture
def fake_terminal(monkeypatch, tmp_path):
    calls: list[tuple[str, dict]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "branch --show-current" in command:
            output = "main\n"
        elif "status --porcelain" in command:
            output = " M a.txt\x00?? b.txt\x00R  new.txt\x00old.txt\x00"
        elif "rev-list" in command:
            output = "1\t2\n"  # behind=1, ahead=2
        elif "diff --cached --name-only" in command:
            output = "staged.txt\x00"
        elif "rev-parse HEAD" in command:
            output = "abc123\n"
        elif "log -1 --format=%s" in command:
            output = "subject\n"
        else:
            output = ""
        return json.dumps({"output": output, "exit_code": 0, "error": None})

    monkeypatch.setattr("tools.terminal_tool.terminal_tool", run)
    monkeypatch.setattr(session_policy, "policy_for_source",
                        lambda runner, source: policy(tmp_path, terminal={"TERMINAL_ENV": "ssh"}))
    return calls


@pytest_asyncio.fixture
async def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with panel_authority(tmp_path) as conn:
        yield conn


@pytest.mark.asyncio
async def test_status_uses_the_session_backend(connection, fake_terminal):
    reply = await connection.dispatch(
        {"id": 1, "method": "git.status", "params": {"session_id": "s"}})
    assert "error" not in reply, reply
    result = reply["result"]
    assert result["branch"] == "main"
    assert (result["ahead"], result["behind"]) == (2, 1)
    assert [(row["path"], row["xy"]) for row in result["files"]] == [
        ("a.txt", " M"), ("b.txt", "??"), ("new.txt", "R ")]
    assert {command.split(" git ")[0] for command, _ in fake_terminal} == {
        "GIT_TERMINAL_PROMPT=0 GIT_PAGER=cat"}
    # SSH keeps its own configured cwd: a gateway-host workdir would not exist remotely.
    assert all(kwargs["workdir"] is None for _, kwargs in fake_terminal)
    assert all(kwargs["task_id"] == "route" for _, kwargs in fake_terminal)


def test_parse_status_keeps_the_new_rename_path():
    files = _parse_status("R  new.txt\x00old.txt\x00 M staged.txt\x00")
    assert files == [
        {"path": "new.txt", "xy": "R ", "staged": True},
        {"path": "staged.txt", "xy": " M", "staged": False},
    ]


def test_parse_branches_reads_tracking_counts():
    rows = _parse_branches("main\x00origin/main\x00ahead 2, behind 1\nsolo\x00\x00\n")
    assert rows == [
        {"name": "main", "upstream": "origin/main", "ahead": 2, "behind": 1},
        {"name": "solo", "upstream": None, "ahead": 0, "behind": 0},
    ]


def test_parse_log_reads_nul_fields():
    rows = _parse_log("abc\x00subject\x00Alice\x002026-01-01T00:00:00Z\n")
    assert rows == [{"sha": "abc", "subject": "subject", "author": "Alice",
                     "date": "2026-01-01T00:00:00Z"}]


@pytest.mark.asyncio
async def test_stage_quotes_paths_and_answers_the_index(connection, fake_terminal):
    reply = await connection.dispatch({"id": 1, "method": "git.stage", "params": {
        "session_id": "s", "paths": ["a b.txt"]}})
    assert "error" not in reply, reply
    assert reply["result"] == {"staged": ["staged.txt"]}
    assert "add -- 'a b.txt'" in fake_terminal[0][0]


@pytest.mark.asyncio
async def test_commit_refuses_an_empty_message_before_running_git(connection, fake_terminal):
    reply = await connection.dispatch({"id": 1, "method": "git.commit", "params": {
        "session_id": "s", "message": "  "}})
    assert reply["error"]["message"] == "invalid_params"
    assert reply["error"]["data"]["field"] == "message"
    assert fake_terminal == []


@pytest.mark.asyncio
async def test_commit_answers_sha_and_summary(connection, fake_terminal):
    reply = await connection.dispatch({"id": 1, "method": "git.commit", "params": {
        "session_id": "s", "message": "fix: panel"}})
    assert "error" not in reply, reply
    assert reply["result"] == {"sha": "abc123", "summary": "subject"}
    assert "commit -m 'fix: panel'" in fake_terminal[0][0]


@pytest.mark.asyncio
async def test_diff_pipes_with_pipefail_and_reports_truncation(connection, tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return json.dumps({"output": "x" * (_DIFF_BYTES + 1), "exit_code": 0, "error": None})

    monkeypatch.setattr("tools.terminal_tool.terminal_tool", run)
    monkeypatch.setattr(session_policy, "policy_for_source",
                        lambda runner, source: policy(tmp_path, terminal={"TERMINAL_ENV": "ssh"}))

    reply = await connection.dispatch(
        {"id": 1, "method": "git.diff", "params": {"session_id": "s", "path": "a b.txt"}})
    assert "error" not in reply, reply
    assert reply["result"]["truncated"] is True
    assert len(reply["result"]["diff"]) == _DIFF_BYTES
    assert "set -o pipefail;" in calls[0] and "cat >/dev/null" in calls[0]


@pytest.mark.asyncio
async def test_diff_failure_reaches_the_client_as_a_refusal(connection, tmp_path, monkeypatch):
    def run(command, **kwargs):
        return json.dumps({"output": "fatal: not a git repository", "exit_code": 128,
                           "error": None})

    monkeypatch.setattr("tools.terminal_tool.terminal_tool", run)
    monkeypatch.setattr(session_policy, "policy_for_source",
                        lambda runner, source: policy(tmp_path, terminal={"TERMINAL_ENV": "ssh"}))

    reply = await connection.dispatch(
        {"id": 1, "method": "git.diff", "params": {"session_id": "s"}})
    assert reply["error"]["message"] == "invalid_params"
    assert "not a git repository" in reply["error"]["data"]["detail"]


@pytest.mark.asyncio
async def test_local_backend_uses_the_policy_workspace(connection, tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return json.dumps({"output": "", "exit_code": 0, "error": None})

    monkeypatch.setattr("tools.terminal_tool.terminal_tool", run)
    monkeypatch.setattr(session_policy, "policy_for_source",
                        lambda runner, source: policy(tmp_path, terminal={"TERMINAL_ENV": "local"}))
    reply = await connection.dispatch(
        {"id": 1, "method": "git.branches", "params": {"session_id": "s"}})
    assert "error" not in reply, reply
    assert calls[0][1]["workdir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_commit_request_id_is_idempotent(connection, fake_terminal):
    params = {"session_id": "s", "message": "fix: once", "request_id": "r1"}
    first = await connection.dispatch({"id": 1, "method": "git.commit", "params": params})
    second = await connection.dispatch({"id": 2, "method": "git.commit", "params": params})
    assert first["result"] == second["result"] == {"sha": "abc123", "summary": "subject"}
    assert sum("commit -m" in command for command, _ in fake_terminal) == 1
