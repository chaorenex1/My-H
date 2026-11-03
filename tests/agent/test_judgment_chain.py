from __future__ import annotations

import threading
from typing import Any, cast

import pytest

from agent.judgment import chain as chain_module
from agent.judgment.cache import JudgmentCache
from agent.judgment.chain import (
    ChainJudge,
    has_native_judge,
    reset_rejection_cooldowns,
)
from agent.judgment.config import JudgmentSettings, load_judgment_settings
from agent.judgment.native import NativeRoute
from agent.judgment.types import (
    JudgmentAccountRejected,
    JudgmentAborted,
    JudgmentRequest,
    JudgmentResponseError,
    JudgmentResult,
    JudgmentUnavailable,
    NoulAnswer,
    NoulQuestion,
)
from agent.usage_pricing import CanonicalUsage


def _cfg(native: list[dict] | None = None, auxiliary: dict | None = None, **judgment) -> dict:
    j = {
        "mode": "auto",
        "text_after_native": False,
        "cache": False,
        "audit": False,
        "native": native or [],
    }
    j.update(judgment)
    return {
        "judgment": j,
        "auxiliary": {"judge": {"provider": "auto", "model": "", **(auxiliary or {})}},
    }


def _request() -> JudgmentRequest:
    return JudgmentRequest(state={"content": "x"}, questions={"q0": NoulQuestion("Is it?")})


def _native_result(model: str) -> JudgmentResult:
    return JudgmentResult(
        api="typesafe", provider="typesafe", model=model,
        answers={"q0": NoulAnswer(0.8)},
        usage=CanonicalUsage(input_tokens=5, output_tokens=1))


def _text_result() -> JudgmentResult:
    return JudgmentResult(
        api="text", provider="auto", model="chat",
        answers={"q0": NoulAnswer(1.0)},
        usage=CanonicalUsage(input_tokens=3, output_tokens=1))


class FakeNative:
    calls: list[str] = []
    behavior: dict[str, object] = {}
    caches: list[object] = []

    def __init__(self, route, settings, cache=None):
        self.route = route
        self.label = route.label
        self.supports_images = False
        FakeNative.caches.append(cache)

    def judge(self, request, *, cancel_event=None, purpose=""):
        FakeNative.calls.append(self.route.model)
        behavior = FakeNative.behavior.get(self.route.model)
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior or _native_result(self.route.model)


class FakeText:
    calls: int = 0

    def __init__(self, settings, config=None):
        self.label = "text:auto"

    def judge(self, request, *, cancel_event=None, purpose=""):
        FakeText.calls += 1
        return _text_result()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reset_rejection_cooldowns()
    FakeNative.calls = []
    FakeNative.behavior = {}
    FakeNative.caches = []
    FakeText.calls = 0
    monkeypatch.setattr(chain_module, "NativeJudge", FakeNative)
    monkeypatch.setattr(chain_module, "ChatTextBackend", FakeText)
    monkeypatch.setattr(
        chain_module, "get_env_value_prefer_dotenv",
        lambda name: "typesafe-key" if name == "TYPESAFE_API_KEY" else "")
    yield
    reset_rejection_cooldowns()


def _routes(*models: str) -> list[dict]:
    return [
        {"api": "typesafe", "provider": "typesafe", "model": model}
        for model in models
    ]


def _judge(cfg: dict) -> ChainJudge:
    return ChainJudge(load_judgment_settings(cfg), cfg)


def test_native_candidates_try_in_order_until_one_succeeds():
    FakeNative.behavior = {"one": JudgmentResponseError("boom"), "two": _native_result("two")}
    result = _judge(_cfg(_routes("one", "two"))).judge(_request())
    assert FakeNative.calls == ["one", "two"]
    assert result.model == "two"
    assert FakeText.calls == 0


def test_native_only_tail_does_not_fall_back_to_text():
    FakeNative.behavior = {
        "one": JudgmentResponseError("boom"), "two": JudgmentResponseError("boom")}
    with pytest.raises(JudgmentUnavailable, match="every judge candidate failed"):
        _judge(_cfg(_routes("one", "two"))).judge(_request())
    assert FakeNative.calls == ["one", "two"]
    assert FakeText.calls == 0


def test_text_after_native_opt_in_allows_text_fallback():
    FakeNative.behavior = {"one": JudgmentResponseError("boom")}
    result = _judge(_cfg(_routes("one"), text_after_native=True)).judge(_request())
    assert FakeNative.calls == ["one"]
    assert FakeText.calls == 1
    assert result.api == "text"


def test_auto_without_credentialed_native_uses_text(monkeypatch):
    monkeypatch.setattr(chain_module, "get_env_value_prefer_dotenv", lambda name: "")
    result = _judge(_cfg(_routes("one"))).judge(_request())
    assert FakeNative.calls == []
    assert FakeText.calls == 1
    assert result.api == "text"


def test_mode_native_with_empty_routes_is_unavailable():
    with pytest.raises(JudgmentUnavailable):
        _judge(_cfg(mode="native")).judge(_request())
    assert FakeText.calls == 0


def test_mode_text_ignores_native_routes():
    result = _judge(_cfg(_routes("one"), mode="text")).judge(_request())
    assert FakeNative.calls == []
    assert FakeText.calls == 1
    assert result.api == "text"


def test_mode_off_is_unavailable():
    with pytest.raises(JudgmentUnavailable):
        _judge(_cfg(_routes("one"), mode="off")).judge(_request())
    assert FakeNative.calls == []
    assert FakeText.calls == 0


def test_has_native_judge_resolves_without_calling_a_candidate():
    assert has_native_judge(_cfg(_routes("one"))) is True
    assert FakeNative.calls == []
    assert has_native_judge(_cfg(mode="text")) is False
    assert has_native_judge(_cfg(_routes("one"), mode="off")) is False


def test_has_native_judge_is_false_without_credentials(monkeypatch):
    monkeypatch.setattr(chain_module, "get_env_value_prefer_dotenv", lambda name: "")
    assert has_native_judge(_cfg(_routes("one"))) is False


def test_callers_cancel_before_any_candidate_runs():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JudgmentAborted):
        _judge(_cfg(_routes("one"))).judge(_request(), cancel_event=cancel)
    assert FakeNative.calls == []


def test_account_rejection_cools_the_candidate_for_five_minutes():
    FakeNative.behavior = {"one": JudgmentAccountRejected(401, "no")}
    chain = _judge(_cfg(_routes("one")))
    with pytest.raises(JudgmentUnavailable):
        chain.judge(_request())
    assert FakeNative.calls == ["one"]

    with pytest.raises(JudgmentUnavailable):
        chain.judge(_request())
    assert FakeNative.calls == ["one"]  # cooled down, not re-requested


def test_timeout_does_not_try_the_next_candidate():
    from agent.judgment.types import JudgmentTimeout

    FakeNative.behavior = {"one": JudgmentTimeout("slow")}
    with pytest.raises(JudgmentTimeout):
        _judge(_cfg(_routes("one", "two"), text_after_native=True)).judge(_request())
    assert FakeNative.calls == ["one"]
    assert FakeText.calls == 0


def test_openrouter_decisions_route_uses_the_alpha_base_url(monkeypatch):
    monkeypatch.setattr(chain_module, "_openrouter_api_key", lambda route: "or-key")
    settings = load_judgment_settings(_cfg([
        {"api": "openrouter-decisions", "provider": "openrouter", "model": "typesafe/jev"},
    ]))
    candidates = ChainJudge(settings, _cfg())._resolve_native()
    assert len(candidates) == 1
    route = cast(Any, candidates[0].judge).route
    assert route.base_url == "https://openrouter.ai/api/alpha"
    assert route.provider == "openrouter"
    assert route.api_key == "or-key"


def test_cache_false_with_audit_true_does_not_pass_a_native_cache(tmp_path):
    cache = JudgmentCache(tmp_path / "judgment.db")
    cfg = _cfg(_routes("one"), audit=True, cache=False)
    settings = load_judgment_settings(cfg)
    try:
        ChainJudge(settings, cfg, cache=cache).judge(_request())
    finally:
        cache.close()
    assert FakeNative.caches == [None]


def test_audit_writes_one_row_without_bodies(tmp_path):
    cache = JudgmentCache(tmp_path / "judgment.db")
    cfg = _cfg(_routes("one"), audit=True)
    settings = load_judgment_settings(cfg)
    try:
        ChainJudge(settings, cfg, cache=cache).judge(_request(), purpose="cli", request_id="r1")
        rows = cache.recent_runs()
    finally:
        cache.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert row["purpose"] == "cli"
    assert row["questions"] == 1
    assert row["input_tokens"] == 5
    assert row["state_sha"] and row["answers_sha"]
    assert row["error"] == ""


def test_audit_writes_an_error_row_when_every_candidate_fails(tmp_path):
    FakeNative.behavior = {"one": JudgmentResponseError("boom")}
    cache = JudgmentCache(tmp_path / "judgment.db")
    cfg = _cfg(_routes("one"), audit=True)
    settings = load_judgment_settings(cfg)
    try:
        with pytest.raises(JudgmentUnavailable):
            ChainJudge(settings, cfg, cache=cache).judge(_request())
        rows = cache.recent_runs()
    finally:
        cache.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert "boom" in rows[0]["error"]
    assert rows[0]["answers_sha"] == ""


def test_invalid_native_route_is_warned_and_skipped():
    settings = load_judgment_settings(_cfg([
        {"api": "nope", "provider": "x", "model": "m"},
        {"api": "typesafe", "provider": "typesafe", "model": ""},
        {"api": "typesafe", "provider": "typesafe", "model": "ok"},
    ]))
    assert [route["model"] for route in settings.native] == ["ok"]
    assert any("unknown api" in problem for problem in settings.problems)
    assert any("model is required" in problem for problem in settings.problems)


def test_default_judgment_settings_make_no_calls():
    settings = load_judgment_settings({})
    assert settings.mode == "auto"
    assert settings.native == ()
    assert settings.local.autostart is False
    assert settings.images is True


def test_openai_and_workers_routes_resolve_their_credentials(monkeypatch):
    env = {
        "OPENAI_API_KEY": "oa-key",
        "CLOUDFLARE_API_TOKEN": "cf-token",
        "CLOUDFLARE_ACCOUNT_ID": "acct-123",
    }
    monkeypatch.setattr(
        chain_module, "get_env_value_prefer_dotenv", lambda name: env.get(name, ""))
    cfg = _cfg([
        {"api": "openai-decisions", "model": "gpt-6-luna"},
        {"api": "workers-ai-systemone", "model": "@cf/cloudflare/clef"},
    ])
    settings = load_judgment_settings(cfg)
    assert settings.problems == ()
    candidates = ChainJudge(settings, cfg)._resolve_native()
    assert [cast(Any, candidate.judge).route.provider for candidate in candidates] == [
        "openai-api", "cloudflare-workers-ai"]

    openai_route = cast(Any, candidates[0].judge).route
    assert openai_route.base_url == "https://api.openai.com/v1"
    assert openai_route.api_key == "oa-key"

    workers_route = cast(Any, candidates[1].judge).route
    assert workers_route.base_url == (
        "https://api.cloudflare.com/client/v4/accounts/acct-123/ai/run")
    assert workers_route.api_key == "cf-token"


def test_doctor_names_the_missing_key_per_wire(monkeypatch):
    from agent.judgment.chain import doctor_routes

    monkeypatch.setattr(chain_module, "get_env_value_prefer_dotenv", lambda name: "")
    monkeypatch.setattr(chain_module, "_pool_api_key", lambda provider: "")
    rows = doctor_routes(_cfg([
        {"api": "typesafe", "model": "jev"},
        {"api": "openrouter-decisions", "model": "typesafe/jev"},
        {"api": "openai-decisions", "model": "gpt-6-luna"},
        {"api": "workers-ai-systemone", "model": "@cf/cloudflare/clef"},
    ]))
    reasons = {row["api"]: row["reason"] for row in rows}
    assert reasons["typesafe"] == "no credential (TYPESAFE_API_KEY)"
    assert reasons["openrouter-decisions"] == "no credential (OPENROUTER_API_KEY)"
    assert reasons["openai-decisions"] == "no credential (OPENAI_API_KEY)"
    assert reasons["workers-ai-systemone"] == (
        "no credential (CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID)")
