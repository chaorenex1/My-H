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
        kind="native",
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
        kind="native",
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



def test_ask_rejects_empty_criteria_before_resolving_a_judge(monkeypatch, tmp_path, capsys):
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps({"row": {"instructions": "Which row?", "criteria": {}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent.judgment.resolve_judge",
        lambda **kwargs: pytest.fail("empty criteria must fail during question loading"),
    )
    args = _parser().parse_args([
        "judgment", "ask", "--state", "hello", "--questions-file", str(questions)])

    assert args.func(args) == 2
    assert "caller-supplied labels" in capsys.readouterr().out


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
    # ADR-17 D8: the default chain names two native models; with no credential they
    # appear as unavailable routes rather than disappearing.
    assert "typesafe:typesafe/jev-1.14.0" in output
    assert "openrouter-decisions:openrouter/typesafe/jev-1.14" in output
    assert "no credential" in output
    assert "images: enabled" in output
    assert "cache:" in output and "audit:" in output


def test_doctor_json(capsys):
    args = _parser().parse_args(["judgment", "doctor", "--json"])
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "auto"
    assert [route["id"] for route in payload["routes"]] == [
        "typesafe/jev-1.14", "openrouter/typesafe-jev-1.14"]
    assert all(not route["available"] for route in payload["routes"])
    assert payload["tools"]["enabled"] == "auto"
    assert payload["images"] is True


def test_auxiliary_judge_slot_and_env_keys_are_registered():
    from typing import Any, cast

    from hermes_cli.aux_slots import AUX_TASK_META, OMP_ROLE_TAGS
    from hermes_cli.config_defaults import DEFAULT_CONFIG, OPTIONAL_ENV_VARS

    aux = cast(dict[str, Any], DEFAULT_CONFIG["auxiliary"])
    judge = cast(dict[str, Any], aux["judge"])
    # ADR-17 D3/D8: the text budget and reasoning override moved to judgment.text;
    # reasoning_effort keeps the shared aux-slot default ("" = unset).
    assert "max_tokens" not in judge
    assert judge["reasoning_effort"] == ""
    assert judge["images"] is False
    assert list(AUX_TASK_META).index("judge") == list(AUX_TASK_META).index("goal_judge") + 1
    assert OMP_ROLE_TAGS["judge"] == "JUDGE"
    # ADR-17 D4: `tiny` is the chain's cheap lane, not a third main-model identity.
    assert aux["tiny"]["prefer_fast_model"] is True
    from agent.auxiliary_client import _FAST_MODEL_TASKS

    assert "tiny" in _FAST_MODEL_TASKS
    assert "default" in AUX_TASK_META
    for key in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL",
                "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        assert key in OPTIONAL_ENV_VARS
    assert DEFAULT_CONFIG["judgment"] == {
        "mode": "auto",
        "text_after_native": False,
        # ADR-17 D4/D8: catalog + chain + per-call/session spend caps.
        "chain": [
            "native:typesafe/jev-1.14",
            "native:openrouter/typesafe-jev-1.14",
            "tiny",
            "smol",
            "default",
        ],
        "models": [
            {"id": "typesafe/jev-1.14", "kind": "native", "api": "typesafe",
             "provider": "typesafe", "model": "jev-1.14.0"},
            {"id": "openrouter/typesafe-jev-1.14", "kind": "native",
             "api": "openrouter-decisions", "provider": "openrouter",
             "model": "typesafe/jev-1.14"},
        ],
        "cost_gate": {"max_usd_per_call": 0.05, "max_usd_per_session": 0.50},
        # ADR-17 D5/P4: native answer cache + billed-attempt usage ledger.
        "cache": {"enabled": True, "max_states": 50_000, "usage": True},
        "audit": {"enabled": True, "max_runs": 20_000},
        "eval_debug": False,
        "eval_max_bytes": 52_428_800,
        "timeout_ms": 10_000,
        "redact_state": True,
        # ADR-17 D5/P4: path assets stay under cwd/$HERMES_HOME; remote URLs are never fetched.
        "images": {
            "enabled": True,
            "assets": {"allow_local": True, "allow_remote": False, "max_bytes": 16_777_216},
        },
        "tools": {"enabled": "auto"},
        "text": {
            "parse_retries": 2,
            "max_tokens": 4096,
            "disable_reasoning": True,
        },
        "local": {
            "autostart": False,
            "merge_prompt": True,
            "parse_retries": 0,
            "answer_max_tokens": 16,
            "reasoning_max_tokens": 1024,
        },
        "native": [],
    }


# ── P4: --questions/--context and `judgment batch` ────────────────────────


def test_ask_accepts_a_bundled_or_path_question_name_and_contexts(monkeypatch, tmp_path, capsys):
    questions = tmp_path / "templated.json"
    questions.write_text(json.dumps({
        "q0": {"instructions": "Which {{column}}?",
               "criteria": {"true": None, "false": None}}}), encoding="utf-8")
    seen: list[str] = []

    class FakeJudge:
        def judge(self, request, *, cancel_event=None, purpose="", **kwargs):
            seen.append(request.questions["q0"].instructions)
            return _result()

    monkeypatch.setattr("agent.judgment.resolve_judge", lambda purpose="", config=None: FakeJudge())
    args = _parser().parse_args([
        "judgment", "ask", "--state", "hello", "--questions", str(questions),
        "--context", "q0.column=status"])
    assert args.func(args) == 0
    assert seen == ["Which status?"]
    assert "Error" not in capsys.readouterr().out


def test_batch_file_runs_items_and_reports_min_ok(monkeypatch, tmp_path, capsys):
    payload = tmp_path / "batch.json"
    payload.write_text(json.dumps({
        "items": [{"key": "a", "state": "ok"}, {"key": "b", "state": "bad"}],
        "questions": {"q0": {"instructions": "Is it?"}},
        "min_ok": 1,
    }), encoding="utf-8")

    class FakeJudge:
        def judge(self, request, *, cancel_event=None, purpose="", **kwargs):
            if request.state == "bad":
                raise JudgmentUnavailable("nope")
            return _result()

    monkeypatch.setattr("agent.judgment.resolve_judge", lambda purpose="", config=None: FakeJudge())
    args = _parser().parse_args(["judgment", "batch", "--file", str(payload)])
    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "a: typesafe jev-test q0=noul 0.82" in output
    assert "b: failed (JudgmentUnavailable: nope)" in output
    assert "min_ok: met (1/2 ok)" in output

    args = _parser().parse_args(["judgment", "batch", "--file", str(payload), "--json"])
    assert args.func(args) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["success"] is True and view["min_ok_met"] is True
    assert [entry["key"] for entry in view["results"]] == ["a", "b"]


def test_batch_file_min_ok_not_met_exits_1(monkeypatch, tmp_path, capsys):
    payload = tmp_path / "batch.json"
    payload.write_text(json.dumps({
        "items": [{"key": "a", "state": "bad"}],
        "questions": {"q0": {"instructions": "Is it?"}},
        "min_ok": 1,
    }), encoding="utf-8")

    class FakeJudge:
        def judge(self, request, **kwargs):
            raise JudgmentUnavailable("nope")

    monkeypatch.setattr("agent.judgment.resolve_judge", lambda purpose="", config=None: FakeJudge())
    args = _parser().parse_args(["judgment", "batch", "--file", str(payload)])
    assert args.func(args) == 1
    assert "min_ok: not met" in capsys.readouterr().out


def test_batch_file_validation_error_exits_2(tmp_path, capsys):
    payload = tmp_path / "batch.json"
    payload.write_text(json.dumps({"items": [], "questions": {}, "min_ok": 0}),
                       encoding="utf-8")
    args = _parser().parse_args(["judgment", "batch", "--file", str(payload)])
    assert args.func(args) == 2
    assert "items" in capsys.readouterr().out
