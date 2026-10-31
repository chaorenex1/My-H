"""Plan review panel: newest-plan selection, save/status reset, approve and request validation.

The terminal backend is faked at the two command seams (``run`` / ``run_checked``), so the test is
host-independent while still exercising the exact commands the handler builds.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio

from gateway import session_plan, session_policy
from tests.gateway.session_panel_helpers import panel_authority, policy


class PlanFS:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.status: dict[str, str] = {}
        self.order: list[str] = []
        self.commands: list[str] = []

    def _result(self, output="", code=0):
        return {"output": output, "exit_code": code, "error": None}

    async def run(self, connection, ref, command, **kwargs):
        self.commands.append(command)
        if "ls -1t -- .hermes/plans/*.md" in command:
            return self._result(self.order[-1] if self.order else "")
        if command.startswith("cat -- "):
            path = command.split("cat -- ", 1)[1].strip().strip("'")
            return self._result(self.files.get(path, ""))
        if command.startswith("stat -c %Y"):
            path = command.split("stat -c %Y ", 1)[1].split(" ", 1)[0].strip("'")
            return self._result(f"{100.0 + len(self.files)}\n")
        if command.startswith("printf '%s\\n'"):
            match = re.search(r"cat '([^']+)\.status'", command)
            assert match is not None, command
            path = match.group(1)
            return self._result(f"{100.0 + len(self.files)}\n{self.status.get(path, 'draft')}\n")
        return self._result("")

    async def run_checked(self, connection, ref, command, **kwargs):
        self.commands.append(command)
        if command.startswith("cat -- "):
            path = command.split("cat -- ", 1)[1].strip().strip("'")
            return self.files.get(path, "")
        if command.startswith("mkdir -p .hermes/plans && cat > "):
            match = re.search(r"cat > '([^']+)' <<'([^']+)'\n(.*?)\n\2\n", command, re.DOTALL)
            assert match is not None, command
            path, _delimiter, text = match.groups()
            if path not in self.files:
                self.order.append(path)
            self.files[path] = text
            self.status[path] = "draft"
            return f"{100.0 + len(self.files)}"
        if "printf draft > " in command:
            path = command.split("printf draft > ", 1)[1].split(" &&", 1)[0].strip().strip("'")
            self.status[path.removesuffix(".status")] = "draft"
            return ""
        if "printf approved > " in command:
            path = command.split("printf approved > ", 1)[1].strip().strip("'")
            self.status[path.removesuffix(".status")] = "approved"
            return ""
        return ""


@pytest_asyncio.fixture
async def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async with panel_authority(tmp_path) as conn:
        yield conn


@pytest_asyncio.fixture
async def plan_fs(monkeypatch):
    fs = PlanFS()
    monkeypatch.setattr(session_plan, "run", fs.run)
    monkeypatch.setattr(session_plan, "run_checked", fs.run_checked)
    return fs


@pytest.mark.asyncio
async def test_no_plan_reports_empty_state(connection, plan_fs):
    reply = await connection.dispatch(
        {"id": 1, "method": "session.plan.read", "params": {"session_id": "s"}})
    assert "error" not in reply, reply
    assert reply["result"] == {"path": None, "text": "", "status": "none", "updated_at": None}


@pytest.mark.asyncio
async def test_save_then_read_then_approve(connection, plan_fs):
    saved = await connection.dispatch({"id": 1, "method": "session.plan.save", "params": {
        "session_id": "s", "text": "# Plan\n\n- step", "request_id": "r1"}})
    assert "error" not in saved, saved
    path = saved["result"]["path"]
    assert path.startswith(".hermes/plans/") and path.endswith("-plan.md")
    assert plan_fs.files[path] == "# Plan\n\n- step"
    assert sum("printf draft > " in command for command in plan_fs.commands) == 1
    assert any(command.startswith("mkdir -p .hermes/plans && cat > ") for command in plan_fs.commands)

    read = await connection.dispatch(
        {"id": 2, "method": "session.plan.read", "params": {"session_id": "s"}})
    assert read["result"]["text"] == "# Plan\n\n- step"
    assert read["result"]["status"] == "draft"

    approved = await connection.dispatch({"id": 3, "method": "session.plan.approve", "params": {
        "session_id": "s", "request_id": "r2"}})
    assert approved["result"] == {"status": "approved"}

    again = await connection.dispatch(
        {"id": 4, "method": "session.plan.read", "params": {"session_id": "s"}})
    assert again["result"]["status"] == "approved"


@pytest.mark.asyncio
async def test_save_reuses_the_newest_plan(connection, plan_fs):
    plan_fs.files[".hermes/plans/existing.md"] = "old"
    plan_fs.order.append(".hermes/plans/existing.md")
    reply = await connection.dispatch({"id": 1, "method": "session.plan.save", "params": {
        "session_id": "s", "text": "new", "request_id": "r1"}})
    assert "error" not in reply, reply
    assert reply["result"]["path"] == ".hermes/plans/existing.md"
    assert plan_fs.files[".hermes/plans/existing.md"] == "new"


@pytest.mark.asyncio
async def test_approve_without_a_plan_is_a_domain_refusal(connection, plan_fs):
    reply = await connection.dispatch({"id": 1, "method": "session.plan.approve", "params": {
        "session_id": "s", "request_id": "r1"}})
    assert reply["error"]["message"] == "invalid_params"
    assert reply["error"]["data"]["field"] == "path"


@pytest.mark.asyncio
async def test_write_requires_request_id_and_control(connection, plan_fs, monkeypatch):
    reply = await connection.dispatch({"id": 1, "method": "session.plan.save", "params": {
        "session_id": "s", "text": "x"}})
    assert reply["error"]["message"] == "invalid_params"
    assert "request_id" in reply["error"]["data"]["fields"]

    connection.actor = type(connection.actor)(
        connection.actor.subject, connection.actor.profile_id,
        frozenset({"session:read"}), connection.actor.transport_id)
    reply = await connection.dispatch({"id": 2, "method": "session.plan.save", "params": {
        "session_id": "s", "text": "x", "request_id": "r"}})
    assert reply["error"]["message"] == "permission_denied"


@pytest.mark.platforms("posix")
@pytest.mark.asyncio
async def test_save_round_trips_through_the_real_local_backend(connection, tmp_path, monkeypatch):
    """No fake at the command seam: the generated heredoc/status commands run in bash."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(session_policy, "policy_for_source",
                        lambda runner, source: policy(workspace))

    saved = await connection.dispatch({"id": 1, "method": "session.plan.save", "params": {
        "session_id": "s", "text": "# Plan\n\n- step", "request_id": "r1"}})
    assert "error" not in saved, saved
    path = saved["result"]["path"]
    assert (workspace / path).read_text(encoding="utf-8") == "# Plan\n\n- step\n"

    read = await connection.dispatch(
        {"id": 2, "method": "session.plan.read", "params": {"session_id": "s"}})
    assert read["result"]["path"] == path
    assert read["result"]["text"] == "# Plan\n\n- step"
    assert read["result"]["status"] == "draft"

    approved = await connection.dispatch({"id": 3, "method": "session.plan.approve", "params": {
        "session_id": "s", "request_id": "r2"}})
    assert approved["result"] == {"status": "approved"}
    assert (workspace / f"{path}.status").read_text(encoding="utf-8") == "approved"
