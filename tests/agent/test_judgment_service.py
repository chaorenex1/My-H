"""ADR-17 P4: generic judge/judge_batch API, payload schema, and host RPC aliases."""

from __future__ import annotations

import json
import time

import pytest

from agent.judgment import service
from agent.judgment.types import (
    JudgmentAborted,
    JudgmentImage,
    JudgmentResult,
    JudgmentTimeout,
    JudgmentUnavailable,
    JudgmentValidationError,
    NoulAnswer,
)
from agent.usage_pricing import CanonicalUsage


def _result(model: str = "jev-test") -> JudgmentResult:
    return JudgmentResult(
        kind="native", api="typesafe", provider="typesafe", model=model,
        answers={"q0": NoulAnswer(0.82)},
        usage=CanonicalUsage(input_tokens=9, output_tokens=1, request_count=1),
        cost_usd=0.0002)


class FakeJudge:
    def __init__(self, behavior=None):
        self.behavior = behavior or {}
        self.calls: list[dict] = []

    def judge(self, request, *, cancel_event=None, purpose="", **kwargs):
        self.calls.append({
            "state": request.state, "purpose": purpose,
            "budget_usd": kwargs.get("budget_usd"), "images": request.images,
            "questions": dict(request.questions),
        })
        key = request.state if isinstance(request.state, str) else json.dumps(
            request.state, sort_keys=True)
        action = self.behavior.get(key, _result())
        if callable(action):
            action = action(request, cancel_event)
        if isinstance(action, BaseException):
            raise action
        return action


@pytest.fixture
def fake(monkeypatch):
    judge = FakeJudge()
    monkeypatch.setattr("agent.judgment.resolve_judge", lambda purpose="", config=None: judge)
    return judge


def test_judge_payload_renders_contexts_and_labels(fake):
    payload = {
        "state": {"row": 3},
        "questions": {"q0": {"instructions": "Which {{column}}?", "criteria": {}}},
        "labels": {"q0": ["left", "right"]},
        "contexts": {"q0": {"column": "column"}},
        "purpose": "unit",
        "budget_usd": 0.25,
    }
    result = service.judge(payload)
    assert result.answers["q0"] == NoulAnswer(0.82)
    call = fake.calls[0]
    assert call["purpose"] == "unit"
    assert call["budget_usd"] == 0.25
    # The rendered instruction and caller labels reach the chain, not the template/empty map.
    question = call["questions"]["q0"]
    assert question.instructions == "Which column?"
    assert dict(question.criteria) == {"left": None, "right": None}
    assert service.result_to_json(result) == {
        "kind": "native",
        "api": "typesafe",
        "provider": "typesafe",
        "model": "jev-test",
        "answers": {"q0": {"type": "noul", "noul": 0.82}},
        "usage": {
            "input_tokens": 9, "output_tokens": 1, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "reasoning_tokens": 0, "request_count": 1,
        },
        "cost_usd": 0.0002,
        "cached_questions": 0,
    }


def test_judge_payload_requires_one_question_source():
    with pytest.raises(JudgmentValidationError, match="exactly one"):
        service.call_from_payload({"state": "x", "questions": {}, "questions_file": "f.json"})
    with pytest.raises(JudgmentValidationError, match="exactly one"):
        service.call_from_payload({"state": "x"})


def test_images_accept_base64_and_path_assets_only():
    inline = service.image_from_payload(
        {"data": "AAAA", "mime_type": "image/png", "detail": "high"})
    assert inline == JudgmentImage(data="AAAA", mime_type="image/png", detail="high")
    path = service.image_from_payload({"path": "shot.png", "mime_type": "image/png"})
    assert path.path == "shot.png" and path.data == ""
    with pytest.raises(JudgmentValidationError, match="exactly one"):
        service.image_from_payload({"mime_type": "image/png"})
    with pytest.raises(JudgmentValidationError, match="unknown keys"):
        service.image_from_payload(
            {"data": "AAAA", "mime_type": "image/png", "url": "https://x"})


def test_batch_preserves_order_retries_once_and_reports_failures(fake, monkeypatch):
    attempts: list[str] = []

    def flaky(request, _cancel):
        attempts.append(str(request.state))
        if len(attempts) == 1:
            raise JudgmentUnavailable("transient")
        return _result()

    fake.behavior.update({"bad": JudgmentUnavailable("nope"), "flaky": flaky})
    payload = {
        "items": [
            {"key": "a", "state": "ok"},
            {"key": 7, "state": "bad"},
            {"key": "c", "state": "flaky"},
        ],
        "questions": {"q0": {"instructions": "Is it?"}},
        "min_ok": 2,
        "max_concurrency": 2,
    }
    batch = service.judge_batch(payload)
    assert [item.key for item in batch.results] == ["a", 7, "c"]
    assert [item.ok for item in batch.results] == [True, False, True]
    assert batch.min_ok_met is True
    # The failed item is retried once; the per-item error survives.
    assert sum(1 for call in fake.calls if call["state"] == "bad") == 2
    failure = batch.results[1]
    assert failure.error is not None
    assert failure.error.type == "JudgmentUnavailable"
    assert attempts == ["flaky", "flaky"]  # the transient error is retried once

    json_view = service.batch_to_json(batch)
    assert json_view["min_ok_met"] is True
    assert json_view["results"][1] == {
        "key": 7, "ok": False,
        "error": {"type": "JudgmentUnavailable", "message": "nope"},
    }


def test_batch_min_ok_false_and_concurrency_bounds(fake):
    payload = {
        "items": [{"key": "a", "state": "bad"}],
        "questions": {"q0": {"instructions": "Is it?"}},
        "min_ok": 1,
    }
    fake.behavior["bad"] = JudgmentUnavailable("nope")
    batch = service.judge_batch(payload)
    assert batch.min_ok_met is False
    with pytest.raises(JudgmentValidationError, match="max_concurrency"):
        service.judge_batch({**payload, "max_concurrency": 5})
    with pytest.raises(JudgmentValidationError, match="min_ok"):
        service.judge_batch({**payload, "min_ok": 2})


def test_item_timeout_becomes_judgment_timeout(fake):
    def slow(_request, cancel_event):
        time.sleep(0.15)  # health: allow HX013 -- the delay IS the timeout behavior under test
        if cancel_event is not None and cancel_event.is_set():
            raise JudgmentAborted("aborted")
        return _result()

    fake.behavior["slow"] = slow
    with pytest.raises(JudgmentTimeout, match="timed out"):
        service.judge(
            {"state": "slow", "questions": {"q0": {"instructions": "Is it?"}}},
            timeout_ms=20)


def test_rpc_aliases_return_the_same_contract(fake, tmp_path):
    from agent.judgment import rpc

    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps({"q0": {"instructions": "Is it?"}}), encoding="utf-8")
    payload = {"state": "x", "questions_file": str(questions), "purpose": "eval"}
    single = json.loads(rpc.HOST_OPS["__judge__"](payload))
    assert single["answers"]["q0"] == {"type": "noul", "noul": 0.82}
    assert rpc.HOST_OPS["__judge_batch__"]({
        "items": [{"key": "a", "state": "x"}],
        "questions_file": str(questions),
        "min_ok": 1,
    }) == json.dumps({
        "results": [{"key": "a", "ok": True, "result": single}],
        "min_ok_met": True,
    }, ensure_ascii=False)
    assert rpc.host_op("judge") is None
    assert set(rpc.HOST_OP_NAMES) == set(rpc.HOST_OPS)


def test_rpc_error_is_structured(fake):
    from agent.judgment import rpc

    payload = rpc.HOST_OPS["__judge__"]({"state": "x"})
    assert json.loads(payload)["error"]["type"] == "JudgmentValidationError"


def test_sandbox_accepts_reserved_judgment_ops(monkeypatch):
    from agent.judgment import rpc
    from tools.code_execution_rpc import _handle_rpc_request

    monkeypatch.setitem(rpc.HOST_OPS, "__judge__", lambda args: '{"answers": {}}')
    dispatched: list[tuple[str, dict]] = []
    counter = [0]

    def dispatch(name, args):
        dispatched.append((name, args))
        return '{"answers": {}}'

    response = _handle_rpc_request(
        {"tool": "__judge__", "args": {"state": "x"}},
        allowed_tools=frozenset({"terminal"}), tool_call_counter=counter,
        max_tool_calls=1, dispatch=dispatch, tool_call_log=[], call_start=0.0,
        where="test", allow_host_ops=True)
    assert json.loads(response) == {"answers": {}}
    assert dispatched == [("__judge__", {"state": "x"})]
    assert counter[0] == 0  # host ops spend the judgment budget, not the tool-call budget

    unknown = _handle_rpc_request(
        {"tool": "__nope__", "args": {}},
        allowed_tools=frozenset(), tool_call_counter=counter,
        max_tool_calls=1, dispatch=dispatch, tool_call_log=[], call_start=0.0,
        where="test", allow_host_ops=True)
    assert "Unknown host op" in json.loads(unknown)["error"]

    remote = _handle_rpc_request(
        {"tool": "__judge__", "args": {"state": "x"}},
        allowed_tools=frozenset({"terminal"}), tool_call_counter=counter,
        max_tool_calls=1, dispatch=dispatch, tool_call_log=[], call_start=0.0,
        where="remote sandbox", allow_host_ops=False)
    assert "only available to the local" in json.loads(remote)["error"]
    assert dispatched == [("__judge__", {"state": "x"})]


def test_cell_authority_runs_host_ops_under_the_cell_context(monkeypatch):
    import contextvars

    from agent.judgment import rpc
    from tools.code_kernel import CellAuthority

    scope = contextvars.ContextVar("judgment_test_scope", default="outside")
    token = scope.set("cell")
    try:
        authority = CellAuthority("task")
    finally:
        scope.reset(token)
    monkeypatch.setitem(rpc.HOST_OPS, "__judge__", lambda args: scope.get())
    try:
        assert authority.dispatch("__judge__", {}) == "cell"
    finally:
        authority.retire()
    refused = authority.dispatch("__judge__", {})
    assert "No active execute_code cell" in refused


def test_remote_file_poller_rejects_judgment_ops(tmp_path, monkeypatch):
    """A remote filesystem sandbox is not local authority, even with a valid token."""
    import shlex
    import threading
    import time

    from tools import code_execution_rpc as rpc_module

    rpc_dir = tmp_path / "rpc"
    rpc_dir.mkdir()
    request = rpc_dir / "req_000001"
    request.write_text(json.dumps({
        "seq": 1, "token": "remote-token", "tool": "__judge__", "args": {"state": "x"},
    }), encoding="utf-8")
    responses: list[tuple[str, str]] = []
    monkeypatch.setattr(
        rpc_module, "_remote_write",
        lambda env, path, content, **kwargs: responses.append((path, content)))

    remote_path = "/remote/rpc/req_000001"

    class Env:
        done = False

        def execute(self, command, cwd=None, timeout=None, stdin_data=None):
            if command.startswith("ls -1 "):
                return {"output": "" if self.done else remote_path}
            if command.startswith("cat "):
                return {"output": request.read_text(encoding="utf-8")}
            if command.startswith("rm -f "):
                assert shlex.split(command)[-1] == remote_path
                self.done = True
                return {"output": ""}
            raise AssertionError(f"unexpected remote command: {command}")

    stop = threading.Event()
    poller = threading.Thread(
        target=rpc_module._rpc_poll_loop,
        args=(Env(), str(rpc_dir), "task", [], [0], 5, frozenset({"terminal"}),
              stop, "remote-token"),
        daemon=True)
    poller.start()
    try:
        deadline = time.monotonic() + 5
        while not responses and time.monotonic() < deadline:
            stop.wait(0.02)
    finally:
        stop.set()
        poller.join(timeout=5)
    assert responses, "the poller removed the request without answering it"
    path, content = responses[0]
    assert path.endswith("res_000001")
    assert "only available to the local" in content
