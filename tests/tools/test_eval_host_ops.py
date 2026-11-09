"""ADR-4 P3: eval host ops, display pipeline, managed Bun cells, speculation.

These tests avoid spawning a Bun/Python kernel: they exercise the host-op
contract, the result/display projection, the managed-package guardrails, and the
read-only speculation claim store directly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tools.code_execution_tool import generate_hermes_tools_module
from tools.code_kernel import CellAuthority, SessionKernel, _cell_result


def _sink() -> dict:
    return {"images": [], "views": [], "statusEvents": []}


def test_generated_tools_module_exposes_eval_prelude():
    src = generate_hermes_tools_module(["read_file"])
    compile(src, "<hermes_tools>", "exec")
    for name in ("display", "status", "out", "completion", "agent", "wait", "workpool",
                 "judge", "judge_batch"):
        assert f"def {name}(" in src


def test_display_and_status_are_cell_scoped():
    from tools.eval_host_ops import (
        dispatch_host_op, reset_cell_display, set_cell_display,
    )

    sink = _sink()
    token = set_cell_display(sink)
    try:
        payload = json.loads(dispatch_host_op("__display__", {
            "views": [{"text": "x", "view": {"kind": "json", "value": 1}}],
            "images": [{"data": "AA==", "mimeType": "image/png"}],
        }, task_id="display-task"))
        assert payload == {}
        assert json.loads(dispatch_host_op("__status__", {"op": "phase", "title": "Work"},
                                           task_id="display-task")) == {}
    finally:
        reset_cell_display(token)
    assert sink["views"] == [{"text": "x", "view": {"kind": "json", "value": 1}}]
    assert sink["images"][0]["mimeType"] == "image/png"
    assert sink["statusEvents"] == [{"op": "phase", "title": "Work"}]


def test_display_outside_a_cell_reports_an_error():
    from tools.eval_host_ops import dispatch_host_op

    payload = json.loads(dispatch_host_op("__display__", {"images": []}, task_id="no-cell"))
    assert payload["error"]["type"] == "RuntimeError"


def test_out_writes_the_live_turn_reply_and_fails_without_one():
    from agent.tool_progress_relay import bound_turn_reply
    from tools.eval_host_ops import dispatch_host_op

    seen: list[str] = []
    with bound_turn_reply(seen.append):
        payload = json.loads(dispatch_host_op("__out__", {"text": "visible"}, task_id="out-task"))
    assert payload == {}
    assert seen == ["visible"]

    missing = json.loads(dispatch_host_op("__out__", {"text": "nope"}, task_id="out-task"))
    assert missing["error"]["type"] == "RuntimeError"
    assert "active turn" in missing["error"]["message"]


def test_out_refuses_a_settled_turns_stale_sink():
    from agent.tool_progress_relay import _turn_reply_from_agent, bound_turn_reply
    from tools.eval_host_ops import dispatch_host_op

    seen: list[str] = []
    agent = type("A", (), {})()
    agent.interim_assistant_callback = lambda text, **_: seen.append(text)
    agent._print_fn = None
    with bound_turn_reply(_turn_reply_from_agent(agent)):
        assert json.loads(dispatch_host_op("__out__", {"text": "live"}, task_id="out")) == {}
        agent.interim_assistant_callback = None  # the turn settled; the closure is still bound
        stale = json.loads(dispatch_host_op("__out__", {"text": "late"}, task_id="out"))
    assert seen == ["live"]
    assert stale["error"]["type"] == "RuntimeError"
    assert "active turn" in stale["error"]["message"]


def test_display_rejects_oversized_images_and_views():
    from tools.eval_host_ops import (
        _MAX_IMAGE_DATA_CHARS, _MAX_VIEW_TEXT_CHARS, dispatch_host_op,
        reset_cell_display, set_cell_display,
    )

    sink = _sink()
    token = set_cell_display(sink)
    try:
        payload = json.loads(dispatch_host_op("__display__", {
            "images": [{"data": "A" * (_MAX_IMAGE_DATA_CHARS + 1), "mimeType": "image/png"}],
            "views": [{"text": "V" * (_MAX_VIEW_TEXT_CHARS + 1), "view": {"kind": "markdown", "text": "x"}}],
        }, task_id="cap-task"))
    finally:
        reset_cell_display(token)
    assert payload["skipped"] == 2
    assert sink["images"] == [] and sink["views"] == []


def test_cell_authority_routes_display_to_its_result():
    authority = CellAuthority("authority-task")
    try:
        body = json.loads(authority._invoke("__display__", {
            "views": [{"text": "shown", "view": {"kind": "markdown", "text": "shown"}}],
        }))
        assert body == {}
        status = json.loads(authority._invoke("__status__", {"op": "agent", "state": "waiting"}))
        assert status == {}
    finally:
        authority.retire()
    assert authority.display["views"][0]["text"] == "shown"
    assert authority.display["statusEvents"][0]["op"] == "agent"


def test_cell_result_carries_the_display_block():
    kernel = SessionKernel(("owner", "mode", "python", "cwd", ()))
    kernel.cell_display = {"images": [], "views": [{"text": "v", "view": {"kind": "json", "value": 1}}],
                           "statusEvents": [{"op": "phase", "title": "x"}]}
    result = _cell_result(
        kernel, kernel.key, "success",
        {"status": "ok", "stdout": "hi", "stderr": "", "execution_count": 1},
        timeout=5, sandbox_tools=frozenset(), reused=False, state_reset=False,
        exec_start=time.monotonic(),
    )
    assert result["display"]["views"][0]["text"] == "v"
    assert result["display"]["statusEvents"][0]["title"] == "x"
    assert "images" not in result["display"]


def test_display_block_is_stripped_from_the_model_copy():
    from tools.code_execution_tool import strip_execute_code_display

    raw = json.dumps({"status": "success", "output": "x",
                      "display": {"images": [{"data": "AA==", "mimeType": "image/png"}]}})
    stripped = json.loads(strip_execute_code_display(raw))
    assert "display" not in stripped
    assert stripped["status"] == "success" and stripped["output"] == "x"
    assert strip_execute_code_display("not json") == "not json"


def test_tool_row_projects_the_display_block(monkeypatch):
    from tui_gateway import session_history

    monkeypatch.setattr(session_history, "_tool_ctx", lambda name, args: "", raising=False)
    display = {"views": [{"text": "out", "view": {"kind": "json", "value": 1}}]}
    row = session_history._tool_row_message(
        {"tool_name": "execute_code", "tool_call_id": "c1", "content": "{}",
         "display_metadata": {"display": display}},
        {}, "text",
    )
    assert row["details"] == {"display": display}


def test_managed_environment_refuses_project_installs(tmp_path):
    from tools.eval_js_packages import managed_dir, parse_magic, run_magic

    assert parse_magic('%bun add left-pad') == ("bun_add", ["left-pad"])
    assert parse_magic("const s = '%bun add no'") is None
    result = run_magic("environment", "project", session_cwd=str(tmp_path), bun_path=None)
    assert result["error"] == "unsupported_environment"
    assert result["status"] == "error"
    assert "%environment project" in result["output"]
    assert managed_dir(str(tmp_path)).parent.name == "bun-managed"


def test_bun_missing_and_invalid_specs_fail_before_spawning(tmp_path):
    from tools.eval_js_packages import install_packages, run_magic

    missing = run_magic("bun_add", ["left-pad"], session_cwd=str(tmp_path), bun_path=None)
    assert missing["status"] == "error"
    assert "hermes pm install bun" in missing["error"]
    invalid = install_packages(["-evil"], session_cwd=str(tmp_path), bun_path="bun")
    assert invalid["status"] == "error"
    assert "invalid package spec" in invalid["output"]


def test_speculation_claim_hits_then_fingerprint_change_misses(tmp_path, monkeypatch):
    from tools import eval_speculation as spec

    path = tmp_path / "note.txt"
    path.write_text("payload", encoding="utf-8")
    monkeypatch.setattr(spec, "_owner", lambda task_id="": "spec-owner")

    class _Inline:
        def __init__(self, fn):
            self.fn = fn

        def start(self):
            self.fn()

    monkeypatch.setattr("agent.memory_provider.spawn_context_thread",
                        lambda target, **kw: _Inline(target))
    monkeypatch.setattr("tools.registry.registry.dispatch",
                        lambda name, args: json.dumps({"content": path.read_text(encoding="utf-8")}))
    spec.clear_all()
    try:
        code = f'x = read_file(path="{path.as_posix()}")'
        spec.observe_partial("execute_code", code, 0, "call-1")
        hit = spec.take_claim("task", "read_file", {"path": str(path)})
        assert hit is not None
        assert json.loads(hit)["content"] == "payload"

        # A live claim from call-1 is invalid once a new argument stream starts.
        other = tmp_path / "other.txt"
        other.write_text("other", encoding="utf-8")
        spec.observe_partial("execute_code", f'read_file(path="{other.as_posix()}")', 0, "call-1")
        spec.observe_partial("execute_code", "unrelated", 0, "call-2")
        assert spec.take_claim("task", "read_file", {"path": str(other)}) is None

        # Fingerprint safety: a prefetched read is not reused after the file changes.
        spec.observe_partial("execute_code", code, 0, "call-2")
        path.write_text("changed payload", encoding="utf-8")
        assert spec.take_claim("task", "read_file", {"path": str(path)}) is None
    finally:
        spec.clear_all()


def test_completion_handle_reports_its_result(monkeypatch):
    from tools import eval_host_ops

    class _Inline:
        def __init__(self, fn):
            self.fn = fn

        def start(self):
            self.fn()

    monkeypatch.setattr(eval_host_ops, "spawn_context_thread",
                        lambda target, **kw: _Inline(lambda: target(*kw.get("args", ()))))

    class _Usage:
        prompt_tokens = 1
        completion_tokens = 2
        total_tokens = 3

    class _Message:
        content = "answer"

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]
        usage = _Usage()

    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kw: _Response())
    body = json.loads(eval_host_ops.dispatch_host_op("__completion__", {"prompt": "q"}, task_id="cmp"))
    assert "id" in body
    snap = json.loads(eval_host_ops.dispatch_host_op(
        "__status__", {"item": {"kind": "completion", "id": body["id"]}}, task_id="cmp"))
    assert snap["status"] == "completed"
    assert snap["text"] == "answer"


def test_named_agent_handle_survives_and_reports(monkeypatch):
    from types import SimpleNamespace

    from tools import eval_host_ops

    calls = []

    def fake_handle(name, args, task_id=None, session_id=None):
        calls.append((name, args, session_id))
        if name == "agent":
            return json.dumps({"agent_session_id": "a1", "subject": "topic"})
        if name == "agent_out":
            return json.dumps({"content": "final report"})
        raise AssertionError(name)

    monkeypatch.setattr("model_tools.handle_function_call", fake_handle)
    monkeypatch.setattr(eval_host_ops, "_current_parent_agent",
                        lambda: SimpleNamespace(session_id="parent-session"))
    monkeypatch.setattr("tools.agent_inbox._session_row",
                        lambda agent_session_id: {"agent_session_id": agent_session_id,
                                                  "status": "completed"})
    created = json.loads(eval_host_ops.dispatch_host_op(
        "__agent__", {"prompt": "do it", "agent": "reviewer"}, task_id="agent-task"))
    assert created["id"] == "a1"
    assert calls[0][1]["agent_name"] == "reviewer"
    assert calls[0][2] == "parent-session"

    snapshot = json.loads(eval_host_ops.dispatch_host_op(
        "__status__", {"item": {"kind": "agent", "id": "a1"}}, task_id="agent-task"))
    assert snapshot["status"] == "completed"
    assert snapshot["text"] == "final report"


def test_anonymous_agent_handle_uses_the_bound_delegation_parent(monkeypatch):
    from tools import eval_host_ops

    calls = []

    class _Parent:
        def _dispatch_delegate_task(self, args):
            calls.append(args)
            if args.get("goal"):
                return json.dumps({"delegation_id": "d1"})
            return json.dumps({})

    monkeypatch.setattr(eval_host_ops, "_session_grants", lambda name: True)
    monkeypatch.setattr(eval_host_ops, "_current_parent_agent", lambda: _Parent())
    created = json.loads(eval_host_ops.dispatch_host_op(
        "__agent__", {"prompt": "do it"}, task_id="delegate-task"))
    assert created["id"] == "d1"
    assert calls[0]["goal"] == "do it"

    sent = json.loads(eval_host_ops.dispatch_host_op(
        "__send__", {"id": "d1", "message": "hurry"}, task_id="delegate-task"))
    assert sent == {}
    assert calls[1] == {"action": "steer", "subagent_id": "d1", "message": "hurry"}

    cancelled = json.loads(eval_host_ops.dispatch_host_op(
        "__cancel__", {"item": {"kind": "agent", "id": "d1"}}, task_id="delegate-task"))
    assert cancelled == {"cancelled": True}
    assert calls[2] == {"action": "stop", "subagent_id": "d1"}


def test_execute_code_refuses_project_environment_magic(monkeypatch):
    import tools.code_execution_tool as tool

    monkeypatch.setattr("tools.terminal_tool._get_env_config", lambda: {"env_type": "local"})
    body = json.loads(tool.execute_code(code="%environment project", language="js", task_id="magic-task"))
    assert body["status"] == "error"
    assert body["error"] == "unsupported_environment"
    assert "project-level installs" in body["output"]


def test_managed_key_is_stable_in_strict_mode():
    from tools.eval_js_packages import managed_dir, managed_key

    owner = managed_key("", "strict-task")
    assert owner
    assert managed_key("", "strict-task") == owner
    assert managed_dir(owner) == managed_dir(managed_key("", "strict-task"))


def test_serve_rpc_routes_eval_host_ops_over_tcp():
    import socket
    import threading

    from tools.code_execution_rpc import serve_rpc

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()[:2]
    stop = threading.Event()
    thread = threading.Thread(
        target=serve_rpc,
        args=(server, "rpc-task", [], [0], 5, frozenset(), stop, "tok"),
        daemon=True,
    )
    thread.start()
    client = socket.create_connection((host, port), timeout=5)
    reader = client.makefile("rb")
    try:
        client.sendall((json.dumps({"tool": "__display__", "args": {"images": []},
                                    "token": "tok"}) + "\n").encode())
        response = json.loads(reader.readline())
        assert response["error"]["type"] == "RuntimeError"
        client.sendall((json.dumps({"tool": "__nope__", "args": {}, "token": "tok"}) + "\n").encode())
        assert "Unknown host op" in json.loads(reader.readline())["error"]
    finally:
        reader.close()
        client.close()
        server.close()
        thread.join(timeout=5)


def test_anonymous_agent_handles_inline_joined_results(monkeypatch):
    from tools import eval_host_ops

    class _Parent:
        def _dispatch_delegate_task(self, args):
            return json.dumps({"results": [
                {"task_index": 0, "status": "completed", "summary": "joined answer"},
            ]})

    monkeypatch.setattr(eval_host_ops, "_session_grants", lambda name: True)
    monkeypatch.setattr(eval_host_ops, "_current_parent_agent", lambda: _Parent())
    created = json.loads(eval_host_ops.dispatch_host_op(
        "__agent__", {"prompt": "do it"}, task_id="sync-task"))
    assert created["completed"] is True
    handle_id = created["id"]
    waited = json.loads(eval_host_ops.dispatch_host_op(
        "__wait__", {"items": [{"kind": "agent", "id": handle_id}]}, task_id="sync-task"))
    assert waited["items"][0] == {"status": "completed", "text": "joined answer"}
    cancelled = json.loads(eval_host_ops.dispatch_host_op(
        "__cancel__", {"item": {"kind": "agent", "id": handle_id}}, task_id="sync-task"))
    assert cancelled == {"cancelled": False}
    sent = json.loads(eval_host_ops.dispatch_host_op(
        "__send__", {"id": handle_id, "message": "more"}, task_id="sync-task"))
    assert "already completed" in sent["error"]["message"]


def test_host_op_rpc_dispatch_accepts_display():
    from tools.code_execution_rpc import _default_dispatch

    dispatch = _default_dispatch("rpc-task", allow_host_ops=True)
    body = json.loads(dispatch("__display__", {"images": []}))
    assert body["error"]["type"] == "RuntimeError"  # recognized op, no live cell sink
