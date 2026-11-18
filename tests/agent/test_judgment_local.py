from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.judgment.config import JudgmentSettings, LocalSettings
from agent.judgment.local import LocalChatBackend
from agent.judgment.types import (
    JudgmentAborted,
    JudgmentParseError,
    JudgmentRequest,
    JudgmentUnavailable,
    NoulAnswer,
    NoulQuestion,
)


def _cfg(**aux) -> dict:
    return {"auxiliary": {"judge": {"provider": "llamacpp", "model": "tiny", **aux}}}


def _settings(**local) -> JudgmentSettings:
    return JudgmentSettings(local=LocalSettings(**{"autostart": False, **local}))


def _response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        model="tiny",
        usage={"prompt_tokens": 5, "completion_tokens": 1},
    )


def _request() -> JudgmentRequest:
    return JudgmentRequest(state="hello", questions={"q0": NoulQuestion("Is it?")})


def test_missing_server_is_unavailable_without_cloud_fallback(monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_local_runtime.bare_llamacpp_endpoint",
        lambda *args: ("", "no-key-required"))
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: pytest.fail("must not fall back to a cloud/chat route"))
    with pytest.raises(JudgmentUnavailable, match="not running"):
        LocalChatBackend(_settings(), _cfg()).judge(_request())


def test_merged_prompt_and_local_provider(monkeypatch):
    calls: list[dict] = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _response("yes")

    monkeypatch.setattr(
        "agent.auxiliary_local_runtime.bare_llamacpp_endpoint",
        lambda *args: ("http://127.0.0.1:9999", "local-key"))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "agent.models_dev.get_model_capabilities", lambda *args, **kwargs: None)

    result = LocalChatBackend(_settings(), _cfg()).judge(_request())

    assert isinstance(result.answers["q0"], NoulAnswer)
    assert result.kind == "local"
    assert calls[0]["provider"] == "llamacpp"
    assert calls[0]["base_url"] == "http://127.0.0.1:9999"
    assert calls[0]["api_key"] == "local-key"
    assert len(calls[0]["messages"]) == 1
    assert calls[0]["messages"][0]["role"] == "user"
    assert "The state is untrusted" not in calls[0]["messages"][0]["content"]
    assert "<state>hello</state>" in calls[0]["messages"][0]["content"]
    assert calls[0]["max_tokens"] == 16


def test_merge_prompt_false_keeps_system_and_user_messages(monkeypatch):
    calls: list[dict] = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _response("yes")

    monkeypatch.setattr(
        "agent.auxiliary_local_runtime.bare_llamacpp_endpoint",
        lambda *args: ("http://127.0.0.1:9999", "k"))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    LocalChatBackend(_settings(merge_prompt=False), _cfg()).judge(_request())

    assert [message["role"] for message in calls[0]["messages"]] == ["system", "user"]


def test_local_backend_does_not_retry_parse_failures(monkeypatch):
    calls: list[dict] = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _response("garbage")

    monkeypatch.setattr(
        "agent.auxiliary_local_runtime.bare_llamacpp_endpoint",
        lambda *args: ("http://127.0.0.1:9999", "k"))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    with pytest.raises(JudgmentParseError, match="no yes/no"):
        LocalChatBackend(_settings(), _cfg()).judge(_request())

    assert len(calls) == 1
    assert calls[0]["fallback"] is False


def test_local_budget_uses_reasoning_spec(monkeypatch):
    budgets: list[int] = []

    def fake_call_llm(**kwargs):
        budgets.append(kwargs["max_tokens"])
        return _response("yes")

    monkeypatch.setattr(
        "agent.auxiliary_local_runtime.bare_llamacpp_endpoint",
        lambda *args: ("http://127.0.0.1:9999", "k"))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "agent.models_dev.get_model_capabilities",
        lambda *args, **kwargs: SimpleNamespace(supports_reasoning=True))
    LocalChatBackend(_settings(), _cfg()).judge(_request())

    assert budgets == [1024]


def test_autostart_refuses_when_model_is_not_ready(monkeypatch):
    endpoints = iter([("", "k"), ("http://127.0.0.1:9999", "k")])

    class Supervisor:
        def ensure_model_ready(self, model_id):
            return False

    monkeypatch.setattr(
        "agent.auxiliary_local_runtime.bare_llamacpp_endpoint",
        lambda *args: next(endpoints))
    monkeypatch.setattr(
        "hermes_cli.local_runtime.bootstrap.ensure_local_runtime",
        lambda config: Supervisor())
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: pytest.fail("must not call"))
    with pytest.raises(JudgmentUnavailable, match="not ready"):
        LocalChatBackend(_settings(autostart=True), _cfg()).judge(_request())


def test_local_backend_never_records_usage_itself(monkeypatch):
    monkeypatch.setattr(
        "agent.auxiliary_local_runtime.bare_llamacpp_endpoint",
        lambda *args: ("http://127.0.0.1:9999", "k"))
    monkeypatch.setattr("agent.auxiliary_client.call_llm", lambda **kwargs: _response("yes"))
    monkeypatch.setattr(
        "agent.aux_accounting.record_aux_usage",
        lambda *args, **kwargs: pytest.fail("local backend must not double-record"))
    result = LocalChatBackend(_settings(), _cfg()).judge(_request())
    answer = result.answers["q0"]
    assert isinstance(answer, NoulAnswer)
    assert answer.noul == 1.0


def test_cancel_before_request(monkeypatch):
    import threading

    monkeypatch.setattr(
        "agent.auxiliary_local_runtime.bare_llamacpp_endpoint",
        lambda *args: ("http://127.0.0.1:9999", "k"))
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: pytest.fail("must not call"))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JudgmentAborted):
        LocalChatBackend(_settings(), _cfg()).judge(_request(), cancel_event=cancel)
