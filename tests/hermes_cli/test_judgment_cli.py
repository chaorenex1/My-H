from __future__ import annotations

import argparse
import json

import pytest

from agent.judgment.cache import JudgmentCache
from agent.judgment.types import (
    JudgmentResult,
    JudgmentUnavailable,
    JudgmentValidationError,
    NoulAnswer,
)
from agent.usage_pricing import CanonicalUsage
from hermes_cli.subcommands.judgment import build_judgment_parser, cmd_judgment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    build_judgment_parser(sub, cmd_judgment=cmd_judgment)
    return parser


def _result() -> JudgmentResult:
    return JudgmentResult(
        api="typesafe", provider="typesafe", model="jev-test",
        answers={"q0": NoulAnswer(0.82)},
        usage=CanonicalUsage(input_tokens=420, output_tokens=2),
        cached_questions=0)


def test_ask_prints_answers_usage_and_cache(monkeypatch, tmp_path, capsys):
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps({"q0": {"instructions": "Is it?"}}), encoding="utf-8")

    class FakeJudge:
        def judge(self, request, *, cancel_event=None, purpose=""):
            assert purpose == "cli"
            assert request.state == "hello"
            return _result()

    monkeypatch.setattr("agent.judgment.resolve_judge", lambda purpose="": FakeJudge())
    monkeypatch.setattr("hermes_cli.subcommands.judgment._usage_cost", lambda result: 0.0001)

    args = _parser().parse_args([
        "judgment", "ask", "--state", "hello", "--questions-file", str(questions)])
    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "api=typesafe" in output
    assert "q0: noul 0.82" in output
    assert "input=420 output=2" in output
    assert "cache: 0/1 hit" in output


def test_ask_json_is_machine_readable(monkeypatch, tmp_path, capsys):
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps({"q0": {"instructions": "Is it?"}}), encoding="utf-8")
    monkeypatch.setattr(
        "agent.judgment.resolve_judge",
        lambda purpose="": type("J", (), {
            "judge": lambda self, request, **kwargs: _result()})())
    monkeypatch.setattr("hermes_cli.subcommands.judgment._usage_cost", lambda result: 0.0001)

    args = _parser().parse_args([
        "judgment", "ask", "--state", "hello", "--questions-file", str(questions), "--json"])
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["answers"]["q0"] == {"type": "noul", "noul": 0.82}
    assert payload["usage"]["input_tokens"] == 420


def test_ask_prints_the_wire_reported_cost(monkeypatch, tmp_path, capsys):
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps({"q0": {"instructions": "Is it?"}}), encoding="utf-8")
    result = JudgmentResult(
        api="typesafe", provider="typesafe", model="jev-test",
        answers={"q0": NoulAnswer(0.82)},
        usage=CanonicalUsage(input_tokens=420, output_tokens=2),
        cost_usd=0.001,
    )
    monkeypatch.setattr(
        "agent.judgment.resolve_judge",
        lambda purpose="": type("J", (), {
            "judge": lambda self, request, **kwargs: result})())
    monkeypatch.setattr(
        "agent.usage_pricing.estimate_usage_cost",
        lambda *args, **kwargs: pytest.fail("wire cost must win"))
    args = _parser().parse_args([
        "judgment", "ask", "--state", "hello", "--questions-file", str(questions)])
    assert args.func(args) == 0
    assert "cost=$0.0010" in capsys.readouterr().out


def test_ask_returns_2_for_invalid_question_file(tmp_path, capsys):
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps({"q0": {"instructions": "Rate", "criteria": ["only-one"]}}),
        encoding="utf-8")
    args = _parser().parse_args([
        "judgment", "ask", "--state", "hello", "--questions-file", str(questions)])
    assert args.func(args) == 2
    assert "at least two" in capsys.readouterr().out


def test_ask_returns_1_when_unavailable(monkeypatch, tmp_path, capsys):
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps({"q0": {"instructions": "Is it?"}}), encoding="utf-8")

    class Fail:
        def judge(self, request, **kwargs):
            raise JudgmentUnavailable("no credential")

    monkeypatch.setattr("agent.judgment.resolve_judge", lambda purpose="": Fail())
    args = _parser().parse_args([
        "judgment", "ask", "--state", "hello", "--questions-file", str(questions)])
    assert args.func(args) == 1
    assert "unavailable" in capsys.readouterr().out.lower()


def test_ask_returns_2_for_validation_errors(monkeypatch, tmp_path, capsys):
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps({"q0": {"instructions": "Is it?"}}), encoding="utf-8")

    class Fail:
        def judge(self, request, **kwargs):
            raise JudgmentValidationError("bad state")

    monkeypatch.setattr("agent.judgment.resolve_judge", lambda purpose="": Fail())
    args = _parser().parse_args([
        "judgment", "ask", "--state", "hello", "--questions-file", str(questions)])
    assert args.func(args) == 2
    assert "bad state" in capsys.readouterr().out


def test_log_reads_runs_and_filters(monkeypatch, tmp_path, capsys):
    cache = JudgmentCache(tmp_path / "judgment.db")
    try:
        cache.record_run({
            "id": "1", "created_at": 100, "purpose": "cli", "mode": "auto",
            "api": "text", "provider": "auto", "model": "chat", "questions": 2,
            "cached": 1, "input_tokens": 10, "output_tokens": 2, "cost_usd": 0.001,
            "latency_ms": 1200, "status": "ok", "error": "", "state_sha": "s",
            "answers_sha": "a", "session_id": "", "execution_id": "", "request_id": "r",
        })
        monkeypatch.setattr("agent.judgment.cache.shared_cache", lambda: cache)

        args = _parser().parse_args(["judgment", "log", "--limit", "5", "--purpose", "cli"])
        assert args.func(args) == 0
        output = capsys.readouterr().out
        assert "cli text:auto/chat ok cached=1/2" in output
        assert "10in/2out" in output
        assert "$0.0010" in output
    finally:
        cache.close()


def test_log_json_and_missing_store(monkeypatch, tmp_path, capsys):
    cache = JudgmentCache(tmp_path / "judgment.db")
    try:
        cache.record_run({
            "id": "1", "created_at": 100, "purpose": "eval", "mode": "auto",
            "api": "text", "provider": "auto", "model": "chat", "questions": 1,
            "cached": 0, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0,
            "latency_ms": 1, "status": "ok", "error": "", "state_sha": "s",
            "answers_sha": "a", "session_id": "", "execution_id": "", "request_id": "r",
        })
        monkeypatch.setattr("agent.judgment.cache.shared_cache", lambda: cache)
        args = _parser().parse_args(["judgment", "log", "--json"])
        assert args.func(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is True
        assert len(payload["runs"]) == 1
    finally:
        cache.close()

    monkeypatch.setattr("agent.judgment.cache.shared_cache", lambda: None)
    args = _parser().parse_args(["judgment", "log"])
    assert args.func(args) == 1


def test_doctor_reports_defaults_without_network(capsys):
    args = _parser().parse_args(["judgment", "doctor"])
    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "mode: auto" in output
    assert "native routes: none configured" in output
    assert "images: enabled" in output
    assert "cache:" in output and "audit:" in output


def test_doctor_json(capsys):
    args = _parser().parse_args(["judgment", "doctor", "--json"])
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "auto"
    assert payload["routes"] == []
    assert payload["tools"]["enabled"] == "auto"
    assert payload["images"] is True


def test_auxiliary_judge_slot_and_env_keys_are_registered():
    from typing import Any, cast

    from hermes_cli.aux_slots import AUX_TASK_META, OMP_ROLE_TAGS
    from hermes_cli.config_defaults import DEFAULT_CONFIG, OPTIONAL_ENV_VARS

    aux = cast(dict[str, Any], DEFAULT_CONFIG["auxiliary"])
    judge = cast(dict[str, Any], aux["judge"])
    assert judge["max_tokens"] == 4096
    assert judge["reasoning_effort"] == "none"
    assert judge["images"] is False
    assert list(AUX_TASK_META).index("judge") == list(AUX_TASK_META).index("goal_judge") + 1
    assert OMP_ROLE_TAGS["judge"] == "JUDGE"
    for key in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL",
                "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        assert key in OPTIONAL_ENV_VARS
    assert DEFAULT_CONFIG["judgment"] == {
        "mode": "auto",
        "text_after_native": False,
        "cache": True,
        "cache_max_states": 50_000,
        "audit": True,
        "audit_max_runs": 20_000,
        "eval_debug": False,
        "eval_max_bytes": 52_428_800,
        "timeout_ms": 10_000,
        "parse_retries": 2,
        "redact_state": True,
        "images": True,
        "tools": {"enabled": "auto"},
        "local": {
            "autostart": False,
            "merge_prompt": True,
            "answer_max_tokens": 16,
            "reasoning_max_tokens": 1024,
        },
        "native": [],
    }
