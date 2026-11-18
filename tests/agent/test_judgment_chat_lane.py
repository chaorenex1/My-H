"""The judgment text lane's transport contract (ADR-17 D3).

The backend units stub ``call_llm``; these tests drive the real auxiliary client
with a fake provider so the forced ``submit_judgment`` tool, ``fallback=False``
and the reported ``api`` are proven on the wire.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent import auxiliary_client as aux
from agent.judgment.chat import (
    SUBMIT_JUDGMENT_CHOICE,
    SUBMIT_JUDGMENT_TOOL,
    ChatTextBackend,
)
from agent.judgment.config import JudgmentSettings, TextSettings
from agent.judgment.types import JudgmentRequest, JudgmentUnavailable, NoulAnswer, NoulQuestion


class _Provider:
    """One fake OpenAI-compatible endpoint that records every request."""

    def __init__(self, replies):
        self.calls: list[dict] = []
        self.replies = list(replies)
        self.fail = False
        provider = self

        class Completions:
            def create(self, **kwargs):
                provider.calls.append(kwargs)
                if provider.fail:
                    raise RuntimeError("boom")
                return provider.replies.pop(0)

        self.chat = SimpleNamespace(completions=Completions())
        self.base_url = "https://fake.example/v1"
        self.api_key = "k"


def _install(monkeypatch, provider: _Provider) -> None:
    monkeypatch.setattr(
        aux, "_get_cached_client", lambda *a, **k: (provider, "fake-model"))
    monkeypatch.setattr(
        aux, "_effective_provider_for_client", lambda client, name: name)
    monkeypatch.setattr(
        "agent.aux_accounting.record_aux_usage", lambda *a, **k: None)


def _backend(monkeypatch, provider: _Provider, **text) -> ChatTextBackend:
    _install(monkeypatch, provider)
    return ChatTextBackend(
        JudgmentSettings(text=TextSettings(**text)),
        {"auxiliary": {"judge": {
            "provider": "fake", "model": "fake-model",
            "base_url": "https://fake.example/v1", "api_key": "k"}}})


def _reply(text: str, *, tool_answer: str | None = None,
           finish_reason: str = "stop") -> SimpleNamespace:
    tool_calls = None
    if tool_answer is not None:
        tool_calls = [SimpleNamespace(function=SimpleNamespace(
            name="submit_judgment", arguments=json.dumps({"answer": tool_answer})))]
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text, tool_calls=tool_calls),
            finish_reason=finish_reason)],
        model="fake-model",
        usage={"prompt_tokens": 4, "completion_tokens": 1},
    )


def _request() -> JudgmentRequest:
    return JudgmentRequest(state="x", questions={"q0": NoulQuestion("Is it?")})


def test_unparseable_first_reply_retries_with_a_forced_tool_on_the_wire(monkeypatch):
    provider = _Provider([_reply("garbage"), _reply("", tool_answer="yes")])
    result = _backend(monkeypatch, provider, parse_retries=2).judge(_request())

    answer = result.answers["q0"]
    assert isinstance(answer, NoulAnswer) and answer.noul == 1.0
    assert result.api == "chat_completions"
    assert "tools" not in provider.calls[0]
    assert provider.calls[1]["tools"] == [SUBMIT_JUDGMENT_TOOL]
    assert provider.calls[1]["tool_choice"] == SUBMIT_JUDGMENT_CHOICE


def test_text_candidate_never_switches_provider(monkeypatch):
    provider = _Provider([])
    provider.fail = True
    backend = _backend(monkeypatch, provider)

    try:
        backend.judge(_request())
    except JudgmentUnavailable:
        pass
    else:  # pragma: no cover - the assertion below is the contract
        raise AssertionError("a failing fixed candidate must surface as JudgmentUnavailable")

    assert len(provider.calls) == 1
