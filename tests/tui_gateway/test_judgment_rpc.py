"""ADR-17 P4: the local viewer proxy dispatches onto the shared judgment service."""

from __future__ import annotations

from agent.judgment.types import JudgmentResult, NoulAnswer
from agent.usage_pricing import CanonicalUsage


def _result() -> JudgmentResult:
    return JudgmentResult(
        kind="native", api="typesafe", provider="typesafe", model="jev-test",
        answers={"q0": NoulAnswer(0.9)},
        usage=CanonicalUsage(input_tokens=3, output_tokens=1))


def _dispatch(request: dict) -> dict:
    from tui_gateway import server

    # ``dispatch`` is bound onto the server namespace by the split-module install.
    return server.dispatch(request)  # type: ignore[attr-defined]


def test_judgment_judge_rpc_uses_the_service(monkeypatch):
    seen: list[dict] = []

    def fake_judge(payload, **kwargs):
        seen.append(dict(payload))
        return _result()

    monkeypatch.setattr("agent.judgment.service.judge", fake_judge)
    response = _dispatch({
        "jsonrpc": "2.0", "id": 7, "method": "judgment.judge",
        "params": {"state": "x", "questions": {"q0": {"instructions": "Is it?"}}},
    })
    assert response["result"]["answers"]["q0"] == {"type": "noul", "noul": 0.9}

    response = _dispatch({
        "jsonrpc": "2.0", "id": 8, "method": "judgment.judge",
        "params": {"profile": "", "state": "x",
                   "questions": {"q0": {"instructions": "Is it?"}}},
    })
    assert response["result"]["model"] == "jev-test"
    # `profile` is a transport scope, not part of the service payload.
    assert seen and "profile" not in seen[-1]


def test_judgment_batch_rpc_returns_per_item_results(monkeypatch):
    from agent.judgment.service import BatchItemResult, BatchResult

    def fake_batch(payload):
        return BatchResult(
            results=(BatchItemResult(key="a", ok=True, result=_result()),),
            min_ok_met=True)

    monkeypatch.setattr("agent.judgment.service.judge_batch", fake_batch)
    response = _dispatch({
        "jsonrpc": "2.0", "id": 9, "method": "judgment.judge_batch",
        "params": {"items": [{"key": "a", "state": "x"}],
                   "questions": {"q0": {"instructions": "Is it?"}},
                   "min_ok": 1, "max_concurrency": 1},
    })
    assert response["result"]["min_ok_met"] is True
    assert response["result"]["results"][0]["key"] == "a"


def test_judgment_rpc_reports_a_structured_error(monkeypatch):
    def fail(_payload, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("agent.judgment.service.judge", fail)
    response = _dispatch({
        "jsonrpc": "2.0", "id": 10, "method": "judgment.judge",
        "params": {"state": "x", "questions": {"q0": {"instructions": "Is it?"}}},
    })
    assert response["error"]["code"] == 5100
    assert "boom" in response["error"]["message"]
