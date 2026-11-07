from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, cast

import pytest

from agent.judgment import chain as chain_module
from agent.judgment import chain_auth, chain_plan as chain_plan_module
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
    JudgmentBudgetExceeded,
    JudgmentRequest,
    JudgmentResponseError,
    JudgmentResult,
    JudgmentUnavailable,
    NoulAnswer,
    NoulQuestion,
)
from agent.usage_pricing import CanonicalUsage
from hermes_cli.judgment_catalog import DEFAULT_CHAIN


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
        kind="native",
        api="typesafe", provider="typesafe", model=model,
        answers={"q0": NoulAnswer(0.8)},
        usage=CanonicalUsage(input_tokens=5, output_tokens=1))


def _text_result() -> JudgmentResult:
    return JudgmentResult(
        kind="online",
        api="chat_completions", provider="auto", model="chat",
        answers={"q0": NoulAnswer(1.0)},
        usage=CanonicalUsage(input_tokens=3, output_tokens=1))


class FakeNative:
    calls: list[str] = []
    behavior: dict[str, object] = {}
    caches: list[object] = []
    routes: list[Any] = []

    def __init__(self, route, settings, cache=None):
        self.route = route
        self.label = route.label
        self.supports_images = False
        FakeNative.caches.append(cache)
        FakeNative.routes.append(route)

    def judge(self, request, *, cancel_event=None, purpose=""):
        FakeNative.calls.append(self.route.model)
        behavior = FakeNative.behavior.get(self.route.model)
        if isinstance(behavior, list):
            behavior = behavior.pop(0) if behavior else None
        if isinstance(behavior, BaseException):
            usage = getattr(behavior, "usage", None)
            hook = getattr(self, "on_partial_usage", None)
            if hook is not None and usage is not None:
                hook(usage, None)
            raise behavior
        return behavior or _native_result(self.route.model)


class FakeText:
    calls: int = 0
    tasks: list[str] = []
    behavior: dict[str, object] = {}

    def __init__(self, settings, config=None, *, task="judge", provider=None,
                 model=None, base_url=None):
        self.task = task
        self.provider = provider or "auto"
        self.model = model or ""
        self.label = f"text:{task}:{self.provider}"

    def judge(self, request, *, cancel_event=None, purpose=""):
        FakeText.calls += 1
        FakeText.tasks.append(self.task)
        behavior = FakeText.behavior.get(self.task)
        if isinstance(behavior, BaseException):
            raise behavior
        return _text_result()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reset_rejection_cooldowns()
    chain_plan_module.reset_chain_cache()
    from agent.judgment.cost_gate import reset_cost_gate

    reset_cost_gate()
    FakeNative.calls = []
    FakeNative.behavior = {}
    FakeNative.caches = []
    FakeNative.routes = []
    FakeText.calls = 0
    FakeText.tasks = []
    FakeText.behavior = {}
    monkeypatch.setattr(chain_module, "NativeJudge", FakeNative)
    monkeypatch.setattr(chain_module, "ChatTextBackend", FakeText)
    monkeypatch.setattr(
        chain_module, "get_env_value_prefer_dotenv",
        lambda name: "typesafe-key" if name == "TYPESAFE_API_KEY" else "")
    yield
    reset_rejection_cooldowns()
    chain_plan_module.reset_chain_cache()
    reset_cost_gate()


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
    assert result.kind == "online"


def test_auto_without_credentialed_native_uses_text(monkeypatch):
    monkeypatch.setattr(chain_module, "get_env_value_prefer_dotenv", lambda name: "")
    result = _judge(_cfg(_routes("one"))).judge(_request())
    assert FakeNative.calls == []
    assert FakeText.calls == 1
    assert result.kind == "online"


def test_mode_native_with_empty_routes_is_unavailable():
    with pytest.raises(JudgmentUnavailable):
        _judge(_cfg(mode="native", models=[], chain=[])).judge(_request())
    assert FakeText.calls == 0


def test_mode_text_ignores_native_routes():
    result = _judge(_cfg(_routes("one"), mode="text")).judge(_request())
    assert FakeNative.calls == []
    assert FakeText.calls == 1
    assert result.kind == "online"


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
    candidates = [c for c in ChainJudge(settings, _cfg()).candidates() if c.kind == "native"]
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
    candidates = [c for c in ChainJudge(settings, cfg).candidates() if c.kind == "native"]
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


# ── ADR-17 P3: catalog/chain, slots, TTL, withAuth, cost gate ───────────────


def _models(*specs: dict) -> list[dict]:
    return list(specs)


def test_catalog_chain_orders_native_ids_then_roles():
    FakeNative.behavior = {"one": JudgmentResponseError("boom")}
    cfg = _cfg(models=_models(
        {"id": "t/one", "kind": "native", "api": "typesafe", "provider": "typesafe",
         "model": "one"},
    ), chain=["native:t/one", "tiny", "smol", "default"], text_after_native=True)
    chain = _judge(cfg)
    plan = chain.candidates()
    assert [candidate.kind for candidate in plan] == ["native", "text", "text", "text"]
    assert [getattr(candidate.judge, "task", "") for candidate in plan] == [
        "", "tiny", "smol", "default"]
    result = chain.judge(_request())
    assert FakeNative.calls == ["one"]
    assert FakeText.tasks == ["tiny"]
    assert result.kind == "online"


def test_roles_run_tiny_then_smol_then_default():
    FakeText.behavior = {
        "tiny": JudgmentUnavailable("tiny down"),
        "smol": JudgmentUnavailable("smol down"),
    }
    cfg = _cfg(models=[], chain=["tiny", "smol", "default"])
    result = _judge(cfg).judge(_request())
    assert FakeText.tasks == ["tiny", "smol", "default"]
    assert result.kind == "online"


def test_mode_text_uses_the_role_tail_and_pinned_judge_first():
    cfg = _cfg(
        models=[],
        mode="text",
        auxiliary={"provider": "openrouter", "model": "pinned"},
    )
    chain = _judge(cfg)
    assert [getattr(c.judge, "task", "") for c in chain.candidates()] == [
        "judge", "tiny", "smol", "default"]
    chain.judge(_request())
    assert FakeText.tasks == ["judge"]


def test_chain_plan_is_reused_until_the_ttl_expires(monkeypatch):
    builds = 0
    real = chain_plan_module._build_chain_entries

    def counting(settings):
        nonlocal builds
        builds += 1
        return real(settings)

    monkeypatch.setattr(chain_plan_module, "_build_chain_entries", counting)
    cfg = _cfg(models=[], chain=["tiny"])
    _judge(cfg).candidates()
    _judge(cfg).candidates()
    assert builds == 1
    clock = chain_plan_module.time.monotonic()
    monkeypatch.setattr(chain_module.time, "monotonic", lambda: clock + 301.0)
    _judge(cfg).candidates()
    assert builds == 2


def test_401_rotates_the_credential_and_retries_the_candidate_once(monkeypatch):
    FakeNative.behavior = {
        "one": [JudgmentAccountRejected(401, "expired"), _native_result("one")]}
    monkeypatch.setattr(
        chain_auth, "rotate_route_credentials",
        lambda route, status: "fresh-key")
    result = _judge(_cfg(_routes("one"))).judge(_request())
    assert FakeNative.calls == ["one", "one"]
    assert FakeNative.routes[-1].api_key == "fresh-key"
    assert result.model == "one"


def test_402_cools_down_without_refreshing_or_retrying(monkeypatch):
    rotated: list[int] = []
    FakeNative.behavior = {"one": JudgmentAccountRejected(402, "billing")}
    monkeypatch.setattr(
        chain_auth, "rotate_route_credentials",
        lambda route, status: rotated.append(status))
    with pytest.raises(JudgmentUnavailable):
        _judge(_cfg(_routes("one"))).judge(_request())
    assert FakeNative.calls == ["one"]
    assert rotated == []


def test_prompted_candidate_rejection_never_rotates_a_credential(monkeypatch):
    FakeText.behavior = {"tiny": JudgmentAccountRejected(401, "no")}
    monkeypatch.setattr(
        chain_module, "rotate_route",
        lambda judge, status: pytest.fail("only native candidates rotate"))
    with pytest.raises(JudgmentUnavailable):
        _judge(_cfg(models=[], chain=["tiny"])).judge(_request())
    assert FakeText.tasks == ["tiny"]


def test_cost_gate_refuses_the_next_call_at_the_session_cap():
    FakeNative.behavior = {"one": JudgmentResult(
        kind="native", api="typesafe", provider="typesafe", model="one",
        answers={"q0": NoulAnswer(0.8)}, usage=CanonicalUsage(),
        cost_usd=0.02)}
    cfg = _cfg(_routes("one"), cost_gate={
        "max_usd_per_call": 0, "max_usd_per_session": 0.01})
    chain = _judge(cfg)
    chain.judge(_request())
    with pytest.raises(JudgmentBudgetExceeded):
        chain.judge(_request())
    assert FakeNative.calls == ["one"]


def test_cost_gate_counts_within_one_call():
    from agent.judgment.cost_gate import JudgmentCostGate

    gate = JudgmentCostGate(max_usd_per_call=0.01, max_usd_per_session=0)
    gate.admit()
    gate.record(0.02)
    with pytest.raises(JudgmentBudgetExceeded):
        gate.admit()


def test_failed_prompted_attempt_counts_against_the_call_cap(monkeypatch):
    FakeText.behavior = {"tiny": JudgmentUnavailable("tiny down")}
    monkeypatch.setattr(chain_module, "usage_cost", lambda *args, **kwargs: 0.02)
    cfg = _cfg(models=[], chain=["tiny", "smol"],
               cost_gate={"max_usd_per_call": 0.01, "max_usd_per_session": 0})
    with pytest.raises(JudgmentBudgetExceeded):
        _judge(cfg).judge(_request())
    assert FakeText.tasks == ["tiny"]


def test_refreshed_oauth_token_is_used_before_rotation(monkeypatch):
    class Pool:
        def has_credentials(self):
            return True

        def try_refresh_matching(self, *, api_key_hint=None):
            return type("Entry", (), {"runtime_api_key": "refreshed", "access_token": None})()

        def mark_exhausted_and_rotate(self, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("rotation must not run when refresh produced a new token")

    monkeypatch.setattr("agent.credential_pool.load_pool", lambda key: Pool())
    route = NativeRoute(
        api="openai-decisions", provider="openai-api", model="gpt-6-luna",
        base_url="https://api.openai.com/v1", api_key="stale")
    assert chain_auth.rotate_route_credentials(route, 401) == "refreshed"


def test_max_questions_filters_candidates_that_cannot_shard():
    cfg = _cfg(models=_models(
        {"id": "local/one", "kind": "local", "provider": "local", "model": "one",
         "max_questions": 1},
    ), chain=["local:local/one"])
    request = JudgmentRequest(
        state={"content": "x"},
        questions={"q0": NoulQuestion("Is it?"), "q1": NoulQuestion("Or not?")})
    with pytest.raises(JudgmentUnavailable):
        _judge(cfg).judge(request)
    assert FakeNative.calls == []


def test_local_catalog_entry_builds_a_local_candidate():
    cfg = _cfg(models=_models(
        {"id": "local/qwen", "kind": "local", "provider": "local",
         "model": "qwen3-1.7b"},
    ), chain=["local:local/qwen"])
    candidates = _judge(cfg).candidates()
    assert [candidate.kind for candidate in candidates] == ["local"]
    judge = cast(Any, candidates[0].judge)
    assert judge.result_kind == "local"
    assert judge.supports_images is False


def test_online_catalog_entry_uses_its_own_route():
    cfg = _cfg(models=_models(
        {"id": "chat/small", "kind": "online", "provider": "openrouter",
         "model": "small/chat"},
    ), chain=["online:chat/small"])
    candidates = _judge(cfg).candidates()
    assert [candidate.kind for candidate in candidates] == ["text"]
    judge = cast(Any, candidates[0].judge)
    assert judge.provider == "openrouter"
    assert judge.model == "small/chat"


def test_legacy_route_wins_a_catalog_id_collision():
    settings = load_judgment_settings(_cfg([
        {"api": "typesafe", "provider": "typesafe", "model": "jev-1.14",
         "api_key_env": "MY_KEY"},
    ]))
    spec = {model.id: model for model in settings.models}["typesafe/jev-1.14"]
    assert spec.model == "jev-1.14"
    assert spec.api_key_env == "MY_KEY"


def test_custom_models_need_their_own_chain_ids():
    cfg = _cfg(models=_models(
        {"id": "local/mine", "kind": "local", "provider": "local", "model": "mine"},
    ), chain=["local:local/mine", "tiny"])
    assert [candidate.kind for candidate in _judge(cfg).candidates()] == ["local", "text"]


def test_unknown_chain_ids_are_skipped_not_fatal():
    cfg = _cfg(models=[], chain=["native:missing", "tiny"])
    assert [candidate.kind for candidate in _judge(cfg).candidates()] == ["text"]


def test_doctor_respects_the_images_switch(monkeypatch):
    from agent.judgment.chain import doctor_routes

    monkeypatch.setattr(
        chain_module, "get_env_value_prefer_dotenv",
        lambda name: "typesafe-key" if name == "TYPESAFE_API_KEY" else "")
    rows = doctor_routes(_cfg(images=False))
    assert rows
    assert all(not row["supports_images"] and not row["supports_boxes"] for row in rows)


def test_default_catalog_and_chain_are_present_without_config(monkeypatch):
    settings = load_judgment_settings({})
    assert [spec.id for spec in settings.models] == [
        "typesafe/jev-1.14", "openrouter/typesafe-jev-1.14"]
    assert settings.chain == DEFAULT_CHAIN
    monkeypatch.setattr(chain_module, "get_env_value_prefer_dotenv", lambda name: "")
    assert has_native_judge(_cfg()) is False
    monkeypatch.setattr(
        chain_module, "get_env_value_prefer_dotenv",
        lambda name: "typesafe-key" if name == "TYPESAFE_API_KEY" else "")
    assert has_native_judge(_cfg()) is True


# ── P4: caller budget ─────────────────────────────────────────────────────


def test_zero_budget_refuses_before_any_candidate():
    FakeNative.behavior = {"one": _native_result("one")}
    chain = _judge(_cfg(_routes("one")))
    with pytest.raises(JudgmentBudgetExceeded, match="budget is zero"):
        chain.judge(_request(), budget_usd=0)
    assert FakeNative.calls == []


def test_native_failure_usage_lands_in_the_run_audit(tmp_path):
    error = JudgmentResponseError("boom")
    error.usage = CanonicalUsage(input_tokens=10, output_tokens=1)
    FakeNative.behavior = {"one": error}
    cache = JudgmentCache(tmp_path / "judgment.db")
    cfg = _cfg(_routes("one"), audit=True)
    try:
        with pytest.raises(JudgmentUnavailable):
            ChainJudge(load_judgment_settings(cfg), cfg, cache=cache).judge(_request())
        rows = cache.recent_runs(limit=5)
    finally:
        cache.close()
    assert rows and rows[0]["status"] == "error"
    assert rows[0]["input_tokens"] == 10
    assert rows[0]["output_tokens"] == 1


def test_native_shard_usage_does_not_consume_the_call_budget(monkeypatch):
    error = JudgmentResponseError("boom")
    error.usage = CanonicalUsage(input_tokens=10, output_tokens=1)
    FakeNative.behavior = {"one": error}
    monkeypatch.setattr(chain_module, "usage_cost", lambda *args, **kwargs: 0.02)
    chain = _judge(_cfg(_routes("one", "two")))
    # Native shards bill the session ledger, not the cost gate, so the fallback still runs.
    assert chain.judge(_request(), budget_usd=0.01).model == "two"
    assert FakeNative.calls == ["one", "two"]
