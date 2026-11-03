from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.judgment import native as native_module
from agent.judgment.cache import JudgmentCache
from agent.judgment.config import JudgmentSettings
from agent.judgment.native import NativeJudge, NativeRoute
from agent.judgment.types import (
    JudgmentAborted,
    JudgmentAccountRejected,
    JudgmentAPIError,
    JudgmentImage,
    JudgmentRequest,
    JudgmentResponseError,
    JudgmentTimeout,
    JudgmentValidationError,
    NoulAnswer,
    NoulQuestion,
)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        record = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": json.loads(body),
        }
        # Concurrent shards append here; the responder must see this request's body, not
        # whatever another thread happened to append last.
        self.server.records.append(record)  # type: ignore[attr-defined]
        responder = getattr(self.server, "responder", None)
        if responder is not None:
            status, payload, headers = responder(record)  # type: ignore[misc]
        else:
            responses = self.server.responses  # type: ignore[attr-defined]
            status, payload, headers = responses.pop(0) if responses else (500, {}, {})
        raw = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.records = []  # type: ignore[attr-defined]
    httpd.responses = []  # type: ignore[attr-defined]
    httpd.responder = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _route(server, api: str = "typesafe") -> NativeRoute:
    return NativeRoute(
        api=api, provider="typesafe" if api == "typesafe" else "openrouter",
        model="jev-test", base_url=f"http://127.0.0.1:{server.server_address[1]}",
        api_key="test-key", timeout_ms=1000,
    )


def _response(noul: float = 0.82, cost: float | None = None) -> dict:
    usage: dict = {"input_tokens": 12, "output_tokens": 2}
    if cost is not None:
        usage["cost"] = cost
    return {
        "model": "jev-test",
        "answers": {"q0": {"type": "noul", "noul": noul}},
        "usage": usage,
    }


def _request() -> JudgmentRequest:
    return JudgmentRequest(state={"content": "hello"}, questions={"q0": NoulQuestion("Is it?")})


def test_typesafe_request_line_headers_and_body(server):
    server.responses.append((200, _response(), {}))
    judge = NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False))
    result = judge.judge(_request())

    record = server.records[0]
    assert record["path"] == "/v1/systemone"
    assert record["headers"]["Authorization"] == "Bearer test-key"
    assert record["headers"]["Accept"] == "application/json"
    assert record["body"] == {
        "state": {"content": "hello"},
        "model": "jev-test",
        "questions": {"q0": {"type": "noul", "instructions": "Is it?"}},
    }
    answer = result.answers["q0"]
    assert isinstance(answer, NoulAnswer) and answer.noul == pytest.approx(0.82)
    assert result.api == "typesafe"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 2)


def test_openrouter_decisions_uses_decisions_path(server):
    server.responses.append((200, _response(0.4), {}))
    judge = NativeJudge(
        _route(server, "openrouter-decisions"),
        JudgmentSettings(cache=False, audit=False))
    result = judge.judge(_request())
    assert server.records[0]["path"] == "/decisions"
    assert result.provider == "openrouter"


def test_transient_5xx_retries_then_succeeds(server, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(native_module.time, "sleep", sleeps.append)
    server.responses.extend([(500, {"error": "boom"}, {}), (200, _response(), {})])
    judge = NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False))
    result = judge.judge(_request())
    answer = result.answers["q0"]
    assert isinstance(answer, NoulAnswer)
    assert answer.noul == pytest.approx(0.82)
    assert len(server.records) == 2
    assert sleeps == [0.5]


def test_retry_after_is_honored_and_capped(server, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(native_module.time, "sleep", sleeps.append)
    server.responses.extend([
        (429, {"error": "slow"}, {"Retry-After": "0"}),
        (200, _response(), {}),
    ])
    NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False)).judge(_request())
    assert sleeps == [0.0]


def test_client_errors_are_not_retried(server):
    server.responses.append((400, {"error": "bad"}, {}))
    with pytest.raises(JudgmentAPIError) as info:
        NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False)).judge(_request())
    assert info.value.status == 400
    assert len(server.records) == 1


def test_account_rejection_is_distinct(server):
    server.responses.append((401, {"error": "no"}, {}))
    with pytest.raises(JudgmentAccountRejected) as info:
        NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False)).judge(_request())
    assert info.value.status == 401
    assert len(server.records) == 1


def test_timeout_raises_without_retrying_another_candidate(server, monkeypatch):
    def fake_urlopen(*args, **kwargs):
        raise TimeoutError("slow")

    monkeypatch.setattr(native_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(native_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(JudgmentTimeout):
        NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False)).judge(_request())


@pytest.mark.parametrize("payload", [
    {"answers": {"q0": {"type": "choice", "choice": "yes"}}, "usage": {}},
    {"answers": {}, "usage": {}},
    {"answers": {"q0": {"type": "noul", "noul": 2}}, "usage": {}},
    {"answers": {"q0": "yes"}, "usage": {}},
])
def test_bad_answers_fail_the_candidate(server, payload):
    server.responses.append((200, payload, {}))
    with pytest.raises(JudgmentResponseError):
        NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False)).judge(_request())


def test_malformed_envelope_fails(server):
    server.responses.append((200, [], {}))
    with pytest.raises(JudgmentResponseError):
        NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False)).judge(_request())


def test_native_usage_is_recorded_once_without_double_count(server):
    from agent.aux_accounting import reset_accounting_context, set_accounting_context

    calls: list[tuple] = []

    class FakeSessionDB:
        def record_auxiliary_usage(self, *args, **kwargs):
            calls.append((args, kwargs))

    token = set_accounting_context(FakeSessionDB(), "session-1")
    try:
        server.responses.append((200, _response(), {}))
        NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False)).judge(_request())
    finally:
        reset_accounting_context(token)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("session-1", "judge")
    assert kwargs["model"] == "jev-test"
    assert kwargs["billing_provider"] == "typesafe"
    assert kwargs["input_tokens"] == 12
    assert kwargs["output_tokens"] == 2


def test_cache_hit_makes_no_second_request(server, tmp_path):
    cache = JudgmentCache(tmp_path / "judgment.db")
    try:
        server.responses.append((200, _response(), {}))
        judge = NativeJudge(
            _route(server), JudgmentSettings(cache=True, audit=False), cache=cache)
        first = judge.judge(_request())
        second = judge.judge(_request())
    finally:
        cache.close()
    assert len(server.records) == 1
    assert first.cached_questions == 0
    assert second.cached_questions == 1
    assert second.usage.input_tokens == 0


def test_native_state_is_redacted_before_send_and_cache(server, tmp_path, monkeypatch):
    monkeypatch.setattr(native_module, "redact_for_egress", lambda text: text.replace("SECRET", "[redacted]"))
    cache = JudgmentCache(tmp_path / "judgment.db")
    request = JudgmentRequest(
        state={"content": "SECRET TOKEN"}, questions={"q0": NoulQuestion("Is it?")})
    try:
        server.responses.append((200, _response(), {}))
        judge = NativeJudge(
            _route(server), JudgmentSettings(cache=True, audit=False), cache=cache)
        judge.judge(request)
        judge.judge(request)
    finally:
        cache.close()
    assert server.records[0]["body"]["state"] == {"content": "[redacted] TOKEN"}
    assert len(server.records) == 1


def test_unredactable_state_is_refused_before_network(server, monkeypatch):
    monkeypatch.setattr(native_module, "redact_for_egress",
                        lambda text: native_module.REDACTION_UNAVAILABLE)
    with pytest.raises(JudgmentValidationError, match="redacted"):
        NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False)).judge(_request())
    assert server.records == []


def test_images_are_validated_before_network(server):
    request = JudgmentRequest(
        state="x", questions={"q0": NoulQuestion("Is it?")},
        images=(JudgmentImage(base64.b64encode(b"img").decode(), "image/png"),))
    with pytest.raises(JudgmentValidationError, match="decoded"):
        NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False)).judge(request)
    with pytest.raises(JudgmentValidationError, match="not enabled"):
        NativeJudge(
            _route(server), JudgmentSettings(cache=False, audit=False, images=False)
        ).judge(request)
    assert server.records == []


def test_cancel_before_first_attempt_sends_nothing(server):
    import threading as _threading

    cancel = _threading.Event()
    cancel.set()
    with pytest.raises(JudgmentAborted):
        NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False)).judge(
            _request(), cancel_event=cancel)
    assert server.records == []


def test_billed_response_with_malformed_answers_is_still_recorded(server):
    from agent.aux_accounting import reset_accounting_context, set_accounting_context

    calls: list[tuple] = []

    class FakeSessionDB:
        def record_auxiliary_usage(self, *args, **kwargs):
            calls.append((args, kwargs))

    token = set_accounting_context(FakeSessionDB(), "session-1")
    try:
        server.responses.append((200, {"answers": {}, "usage": {"input_tokens": 7}}, {}))
        with pytest.raises(JudgmentResponseError):
            NativeJudge(_route(server), JudgmentSettings(cache=False, audit=False)).judge(_request())
    finally:
        reset_accounting_context(token)
    assert len(calls) == 1
    assert calls[0][1]["input_tokens"] == 7


def test_cache_hit_does_not_record_usage(server, tmp_path):
    from agent.aux_accounting import reset_accounting_context, set_accounting_context

    cache = JudgmentCache(tmp_path / "judgment.db")
    calls: list[tuple] = []

    class FakeSessionDB:
        def record_auxiliary_usage(self, *args, **kwargs):
            calls.append((args, kwargs))

    token = set_accounting_context(FakeSessionDB(), "session-1")
    try:
        server.responses.append((200, _response(), {}))
        judge = NativeJudge(
            _route(server), JudgmentSettings(cache=True, audit=False), cache=cache)
        judge.judge(_request())
        judge.judge(_request())
    finally:
        reset_accounting_context(token)
        cache.close()
    assert len(server.records) == 1
    assert len(calls) == 1


def test_resolve_judge_native_route_end_to_end_uses_credentials_and_wire(server, monkeypatch):
    from agent.judgment import chain as chain_module
    from agent.judgment import resolve_judge

    monkeypatch.setattr(
        chain_module, "get_env_value_prefer_dotenv",
        lambda name: "typesafe-key" if name == "TYPESAFE_API_KEY" else "")
    config = {
        "judgment": {
            "mode": "native",
            "cache": False,
            "audit": False,
            "native": [{
                "api": "typesafe", "provider": "typesafe", "model": "jev-test",
                "base_url": f"http://127.0.0.1:{server.server_address[1]}",
            }],
        },
        "auxiliary": {"judge": {"provider": "auto", "model": ""}},
    }
    server.responses.append((200, _response(0.73, cost=0.0002), {}))
    result = resolve_judge(purpose="test", config=config).judge(_request())

    assert result.api == "typesafe"
    assert result.answers["q0"] == NoulAnswer(0.73)
    assert result.cost_usd == pytest.approx(0.0002)
    assert server.records[0]["path"] == "/v1/systemone"
    assert server.records[0]["headers"]["Authorization"] == "Bearer typesafe-key"
    assert server.records[0]["body"]["model"] == "jev-test"


# ── P2 wires: OpenAI Decisions / Workers AI Clef, max_questions sharding ────


def _multi_request(count: int) -> JudgmentRequest:
    return JudgmentRequest(
        state={"content": "hello"},
        questions={f"q{i}": NoulQuestion(f"Is {i}?") for i in range(count)},
    )


def _echo_shard(record):
    """Answer every question in the posted body with 0.5 and fixed usage."""
    questions = record["body"]["questions"]
    if isinstance(questions, dict):
        answers = {qid: {"type": "noul", "noul": 0.5} for qid in questions}
    else:
        answers = [
            {"type": "predicate", "name": question["name"], "probability": 0.5}
            for question in questions
        ]
    return 200, {
        "answers": answers,
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }, {}


def _echo_workers_shard(record):
    status, payload, headers = _echo_shard(record)
    return status, {"success": True, "result": payload, "errors": []}, headers


def test_openai_decisions_encodes_and_decodes_every_question_kind(server):
    from agent.judgment.types import ChoiceAnswer, ChoiceQuestion, ScoreAnswer, ScoreQuestion

    server.responses.append((200, {
        "model": "gpt-6-luna",
        "answers": [
            {"type": "predicate", "name": "q0", "probability": 0.82},
            {
                "type": "choice", "name": "q1", "choice": "high",
                "probabilities": [
                    {"value": "low", "probability": 0.1},
                    {"value": "high", "probability": 0.9},
                ],
                "confidence": 0.9,
            },
            {
                "type": "score", "name": "q2", "score": 2,
                "probabilities": [
                    {"value": 0, "probability": 0.05},
                    {"value": 1, "probability": 0.25},
                    {"value": 2, "probability": 0.7},
                ],
                "confidence": 0.7,
            },
        ],
        "usage": {"input_tokens": 21, "output_tokens": 4},
    }, {}))
    request = JudgmentRequest(
        state={"content": "hello"},
        questions={
            "q0": NoulQuestion("Is it bad?", {"true": "bad", "false": "fine"}),
            "q1": ChoiceQuestion("Pick", {"low": None, "high": "more"}),
            "q2": ScoreQuestion("Rate", ("low", "mid", "high")),
        },
    )
    route = NativeRoute(
        api="openai-decisions", provider="openai-api", model="gpt-6-luna",
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        api_key="openai-key", timeout_ms=1000,
    )
    result = NativeJudge(route, JudgmentSettings(cache=False, audit=False)).judge(request)

    record = server.records[0]
    assert record["path"] == "/decisions"
    assert record["headers"]["Authorization"] == "Bearer openai-key"
    assert record["body"]["model"] == "gpt-6-luna"
    assert record["body"]["input"] == json.dumps({"content": "hello"})
    questions = record["body"]["questions"]
    assert [question["type"] for question in questions] == ["predicate", "choice", "score"]
    assert questions[0]["instructions"] == "Is it bad?\n\nTrue means: bad\n\nFalse means: fine"
    assert questions[1]["choices"] == [{"value": "low"}, {"value": "high", "description": "more"}]
    assert questions[2]["levels"] == [{"label": "low"}, {"label": "mid"}, {"label": "high"}]

    assert isinstance(result.answers["q0"], NoulAnswer)
    assert result.answers["q0"].noul == pytest.approx(0.82)
    assert isinstance(result.answers["q1"], ChoiceAnswer)
    assert result.answers["q1"].choice == "high"
    assert result.answers["q1"].probabilities["high"] == pytest.approx(0.9)
    assert isinstance(result.answers["q2"], ScoreAnswer)
    assert result.answers["q2"].score == 2
    assert result.answers["q2"].probabilities["2"] == pytest.approx(0.7)
    assert (result.usage.input_tokens, result.usage.output_tokens) == (21, 4)


def test_openai_decisions_normalizes_boolean_and_float_values(server):
    from agent.judgment.types import ChoiceAnswer, ChoiceQuestion, ScoreAnswer, ScoreQuestion

    server.responses.append((200, {
        "answers": [
            {
                "type": "choice", "name": "q0", "choice": True,
                "probabilities": [
                    {"value": True, "probability": 0.8},
                    {"value": False, "probability": 0.2},
                ],
                "confidence": 0.8,
            },
            {
                "type": "score", "name": "q1", "score": 2.0,
                "probabilities": [
                    {"value": 0.0, "probability": 0.1},
                    {"value": 1.0, "probability": 0.2},
                    {"value": 2.0, "probability": 0.7},
                ],
                "confidence": 0.7,
            },
        ],
        "usage": {"input_tokens": 4},
    }, {}))
    request = JudgmentRequest(
        state="x",
        questions={
            "q0": ChoiceQuestion("Pick", {"true": None, "false": None}),
            "q1": ScoreQuestion("Rate", ("lo", "mid", "hi")),
        },
    )
    route = NativeRoute(
        api="openai-decisions", provider="openai-api", model="gpt-6-luna",
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        api_key="openai-key", timeout_ms=1000,
    )
    result = NativeJudge(route, JudgmentSettings(cache=False, audit=False)).judge(request)

    choice = result.answers["q0"]
    assert isinstance(choice, ChoiceAnswer)
    assert choice.choice == "true"
    assert dict(choice.probabilities) == {"true": pytest.approx(0.8), "false": pytest.approx(0.2)}
    score = result.answers["q1"]
    assert isinstance(score, ScoreAnswer)
    assert score.score == 2
    assert dict(score.probabilities) == {
        "0": pytest.approx(0.1), "1": pytest.approx(0.2), "2": pytest.approx(0.7)}


def test_openai_decisions_refusal_fails_the_candidate(server):
    server.responses.append((200, {
        "answers": [{"type": "refusal", "name": "q0"}],
        "usage": {"input_tokens": 5},
    }, {}))
    route = NativeRoute(
        api="openai-decisions", provider="openai-api", model="gpt-6-luna",
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        api_key="openai-key", timeout_ms=1000,
    )
    with pytest.raises(JudgmentResponseError, match="refused"):
        NativeJudge(route, JudgmentSettings(cache=False, audit=False)).judge(_request())
    assert len(server.records) == 1


def test_workers_ai_uses_full_model_in_path_and_last_segment_in_body(server):
    server.responses.append((200, {
        "success": True,
        "result": {
            "answers": {"q0": {"type": "noul", "noul": 0.63}},
            "usage": {"input_tokens": 9, "output_tokens": 1},
        },
        "errors": [],
    }, {}))
    route = NativeRoute(
        api="workers-ai-systemone", provider="cloudflare-workers-ai",
        model="@cf/cloudflare/clef",
        base_url=f"http://127.0.0.1:{server.server_address[1]}/accounts/acct/ai/run",
        api_key="cf-token", timeout_ms=1000,
    )
    result = NativeJudge(route, JudgmentSettings(cache=False, audit=False)).judge(_request())

    record = server.records[0]
    assert record["path"] == "/accounts/acct/ai/run/@cf/cloudflare/clef"
    assert record["body"]["model"] == "clef"
    assert result.answers["q0"] == NoulAnswer(0.63)
    assert result.usage.input_tokens == 9


def test_workers_ai_failure_envelope_fails_the_candidate(server):
    server.responses.append((200, {"success": False, "errors": [{"code": 500}], "result": {}}, {}))
    route = NativeRoute(
        api="workers-ai-systemone", provider="cloudflare-workers-ai",
        model="@cf/cloudflare/clef",
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        api_key="cf-token", timeout_ms=1000,
    )
    with pytest.raises(JudgmentResponseError, match="Workers AI"):
        NativeJudge(route, JudgmentSettings(cache=False, audit=False)).judge(_request())


def test_max_questions_shards_merge_answers_and_sum_usage(server):
    from agent.aux_accounting import reset_accounting_context, set_accounting_context

    calls: list[tuple] = []

    class FakeSessionDB:
        def record_auxiliary_usage(self, *args, **kwargs):
            calls.append((args, kwargs))

    server.responder = _echo_shard
    route = NativeRoute(
        api="typesafe", provider="typesafe", model="jev-test",
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        api_key="test-key", timeout_ms=1000, max_questions=2,
    )
    token = set_accounting_context(FakeSessionDB(), "session-1")
    try:
        result = NativeJudge(route, JudgmentSettings(cache=False, audit=False)).judge(
            _multi_request(5))
    finally:
        reset_accounting_context(token)

    assert len(server.records) == 3
    assert all(len(record["body"]["questions"]) <= 2 for record in server.records)
    assert set(result.answers) == {f"q{i}" for i in range(5)}
    assert all(isinstance(answer, NoulAnswer) for answer in result.answers.values())
    assert (result.usage.input_tokens, result.usage.output_tokens) == (30, 3)
    assert len(calls) == 3


def test_shard_failure_aborts_queued_shards_and_skips_cache(server, tmp_path, monkeypatch):
    from agent.aux_accounting import reset_accounting_context, set_accounting_context

    cache = JudgmentCache(tmp_path / "judgment.db")
    calls: list[tuple] = []

    class FakeSessionDB:
        def record_auxiliary_usage(self, *args, **kwargs):
            calls.append((args, kwargs))

    failed = threading.Event()

    def responder(record):
        ids = list(record["body"]["questions"])
        if "q0" in ids:
            failed.set()
            return 400, {"error": "bad"}, {}
        failed.wait(timeout=5)
        return _echo_shard(record)

    server.responder = responder
    route = NativeRoute(
        api="typesafe", provider="typesafe", model="jev-test",
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        api_key="test-key", timeout_ms=2000, max_questions=2,
    )
    token = set_accounting_context(FakeSessionDB(), "session-1")
    try:
        with pytest.raises(JudgmentAPIError):
            NativeJudge(route, JudgmentSettings(cache=True, audit=False), cache=cache).judge(
                _multi_request(9))
        cached = cache.lookup(
            "typesafe:typesafe/jev-test", {"content": "hello"}, _multi_request(9).questions)
    finally:
        reset_accounting_context(token)
        cache.close()

    # 9 questions at 2 per shard = 5 shards; four run, the queued fifth never posts.
    assert len(server.records) == 4
    assert len(calls) == 3  # the three successful shards are billed; the failed one is not
    assert cached == {}


def test_workers_endpoint_default_caps_questions_at_64(server):
    server.responder = _echo_workers_shard
    route = NativeRoute(
        api="workers-ai-systemone", provider="cloudflare-workers-ai",
        model="@cf/cloudflare/clef",
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        api_key="cf-token", timeout_ms=1000,
    )
    result = NativeJudge(route, JudgmentSettings(cache=False, audit=False)).judge(
        _multi_request(65))
    sizes = sorted(len(record["body"]["questions"]) for record in server.records)
    assert sizes == [1, 64]
    assert len(result.answers) == 65


def test_explicit_route_max_questions_beats_the_endpoint_default(server):
    server.responder = _echo_workers_shard
    route = NativeRoute(
        api="workers-ai-systemone", provider="cloudflare-workers-ai",
        model="@cf/cloudflare/clef",
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
        api_key="cf-token", timeout_ms=1000, max_questions=3,
    )
    NativeJudge(route, JudgmentSettings(cache=False, audit=False)).judge(_multi_request(5))
    assert len(server.records) == 2
