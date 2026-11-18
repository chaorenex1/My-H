"""ADR-12 D6 / ADR-17 P5: the local ``table_chart.plan`` RPC proxy."""

from __future__ import annotations


def _register() -> None:
    """Bind this slice's contract + handler even before server.py's wiring imports them."""
    from tui_gateway import methods_table_chart, server
    from tui_gateway.contracts import table_chart

    if "table_chart.plan" not in server._methods:  # type: ignore[attr-defined]
        methods_table_chart.register(server)


def _dispatch(request: dict) -> dict:
    from tui_gateway import server

    _register()
    return server.dispatch(request)  # type: ignore[attr-defined]


def _params() -> dict:
    return {
        "markdown": "| Model | Latency |",
        "table": {"columns": [], "measures": [], "rows": []},
        "guess": {"kind": "bar", "label": 0, "series": [1], "transpose": False},
    }


def test_contract_is_registered():
    from tui_gateway.contracts.registry import METHODS

    _register()
    contract = METHODS["table_chart.plan"]
    assert contract.params.__name__ == "TableChartPlanParams"
    assert set(contract.params.model_fields) == {"profile", "markdown", "table", "guess", "mode"}
    assert contract.result.__name__ == "TableChartPlanResult"


def test_table_chart_plan_rpc_returns_the_planner_result(monkeypatch):
    seen: list[dict] = []

    def fake_plan(payload: dict) -> dict:
        seen.append(dict(payload))
        return {"plan": {"kind": "bar", "label": 0, "series": [1], "transpose": False}, "source": "judge"}

    monkeypatch.setattr("agent.auto_graph.plan_table_chart", fake_plan)
    response = _dispatch({
        "jsonrpc": "2.0", "id": 21, "method": "table_chart.plan",
        "params": {"profile": "", **_params()},
    })

    assert response["result"]["source"] == "judge"
    assert response["result"]["plan"]["kind"] == "bar"
    # `profile` is a transport scope, not part of the planner payload.
    assert seen and "profile" not in seen[-1]


def test_table_chart_plan_rpc_returns_local_source(monkeypatch):
    monkeypatch.setattr(
        "agent.auto_graph.plan_table_chart",
        lambda _payload: {"plan": None, "source": "local"})
    response = _dispatch({
        "jsonrpc": "2.0", "id": 22, "method": "table_chart.plan", "params": _params(),
    })

    assert response["result"] == {"plan": None, "source": "local"}


def test_table_chart_plan_rpc_reports_a_structured_error(monkeypatch):
    def fail(_payload: dict) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr("agent.auto_graph.plan_table_chart", fail)
    response = _dispatch({
        "jsonrpc": "2.0", "id": 23, "method": "table_chart.plan", "params": _params(),
    })

    assert response["error"]["code"] == 5100
    assert "boom" in response["error"]["message"]


def test_table_chart_plan_rpc_rejects_unknown_params():
    response = _dispatch({
        "jsonrpc": "2.0", "id": 24, "method": "table_chart.plan",
        "params": {**_params(), "unknown": True},
    })

    assert response["error"]["code"] == 4000
