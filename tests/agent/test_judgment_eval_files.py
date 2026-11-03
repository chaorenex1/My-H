"""Eval judgment files: one JSONL line per ``purpose="eval"`` run (ADR-11 D16)."""

from __future__ import annotations

import json
import os
from typing import Any, cast

import pytest

from agent.judgment import chain as chain_module
from agent.judgment.cache import judgment_home
from agent.judgment.chain import ChainJudge
from agent.judgment.config import load_judgment_settings
from agent.judgment.eval_files import record_run
from agent.judgment.types import (
    Judge,
    JudgmentRequest,
    JudgmentResult,
    NoulAnswer,
    NoulQuestion,
)
from agent.usage_pricing import CanonicalUsage


class FakeJudge:
    api = "typesafe"
    provider = "typesafe"
    model = "jev-test"

    def judge(self, request, *, cancel_event=None, purpose=""):
        return JudgmentResult(
            api="typesafe", provider="typesafe", model="jev-test",
            answers={"q0": NoulAnswer(0.82)},
            usage=CanonicalUsage(input_tokens=5, output_tokens=1))


def _request() -> JudgmentRequest:
    return JudgmentRequest(state={"content": "x"}, questions={"q0": NoulQuestion("Is it?")})


def _cfg(**judgment: Any) -> dict:
    section = {
        "mode": "native", "cache": False, "audit": False,
        "eval_debug": False, "native": [],
    }
    section.update(judgment)
    return {"judgment": section, "auxiliary": {"judge": {"provider": "auto", "model": ""}}}


def _chain(cfg: dict, monkeypatch) -> ChainJudge:
    settings = load_judgment_settings(cfg)
    chain = ChainJudge(settings, cfg, purpose="eval")
    fake = cast(Judge, FakeJudge())
    monkeypatch.setattr(
        chain, "candidates",
        lambda: [chain_module._Candidate("native", fake)])
    return chain


def _lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_eval_purpose_writes_one_standalone_log_line(monkeypatch):
    chain = _chain(_cfg(), monkeypatch)
    chain.judge(_request(), purpose="eval", request_id="r1", execution_id="e1")

    path = judgment_home() / "eval" / "logs" / "_standalone" / "e1.jsonl"
    assert path.is_file()
    (line,) = _lines(path)
    assert line["purpose"] == "eval"
    assert line["request_id"] == "r1"
    assert line["api"] == "typesafe"
    assert line["status"] == "ok"
    assert line["questions"] == 1
    assert line["usage"] == {"input_tokens": 5, "output_tokens": 1, "cost_usd": 0.0}
    assert line["state_sha"] and line["answers_sha"]
    assert "state" not in line and "answers" not in line


def test_eval_log_uses_the_session_directory(monkeypatch):
    from agent.aux_accounting import reset_accounting_context, set_accounting_context

    class FakeSessionDB:
        def record_auxiliary_usage(self, *args, **kwargs):
            pass

    token = set_accounting_context(FakeSessionDB(), "sess-9")
    try:
        chain = _chain(_cfg(), monkeypatch)
        chain.judge(_request(), purpose="eval", request_id="r2", execution_id="exec-2")
    finally:
        reset_accounting_context(token)

    path = judgment_home() / "eval" / "logs" / "sess-9" / "exec-2.jsonl"
    assert path.is_file()
    assert _lines(path)[0]["session_id"] == "sess-9"


def test_eval_debug_writes_redacted_state_and_answers(monkeypatch):
    monkeypatch.setattr(
        chain_module, "redact_state_for_egress",
        lambda state: {"content": "[redacted]"})
    chain = _chain(_cfg(eval_debug=True), monkeypatch)
    chain.judge(_request(), purpose="eval", request_id="r3", execution_id="e3")

    line = _lines(judgment_home() / "eval" / "logs" / "_standalone" / "e3.jsonl")[0]
    assert line["state"] == {"content": "[redacted]"}
    assert line["answers"]["q0"] == {"type": "noul", "noul": pytest.approx(0.82)}


def test_rotation_removes_oldest_logs_but_never_question_or_batch_files():
    questions = judgment_home() / "eval" / "questions"
    questions.mkdir(parents=True, exist_ok=True)
    kept = questions / "audit.json"
    kept.write_text('{"q0": {"instructions": "Is it?"}}', encoding="utf-8")
    batches = judgment_home() / "eval" / "batches"
    batches.mkdir(parents=True, exist_ok=True)
    manifest = batches / "b1.json"
    manifest.write_text('{"batch_id": "b1"}', encoding="utf-8")

    def run(request_id: str) -> dict:
        return {
            "id": request_id, "created_at": 1, "session_id": "", "execution_id": "",
            "request_id": request_id, "purpose": "eval", "mode": "native",
            "api": "typesafe", "provider": "typesafe", "model": "m",
            "questions": 1, "cached": 0, "input_tokens": 1, "output_tokens": 1,
            "cost_usd": 0.0, "latency_ms": 1, "status": "ok", "error": "",
            "state_sha": "s", "answers_sha": "a",
        }

    first = cast(Any, record_run(run("old"), max_bytes=10_000_000))
    second = cast(Any, record_run(run("new"), max_bytes=10_000_000))
    assert first.is_file() and second.is_file()
    os.utime(first, ns=(1, 1))

    record_run(run("newest"), max_bytes=1)

    assert not first.exists()
    assert not second.exists()
    assert kept.is_file()
    assert manifest.is_file()
