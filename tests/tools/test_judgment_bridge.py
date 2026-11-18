"""ADR-17 P5 eval bridge: generated ``hermes_tools`` helpers route to judgment host ops."""

from __future__ import annotations

from tools.code_execution_tool import generate_hermes_tools_module


def _module(transport: str = "uds"):
    source = generate_hermes_tools_module([], transport=transport)
    namespace: dict = {"__name__": "hermes_tools"}
    exec(compile(source, "hermes_tools.py", "exec"), namespace)
    calls: list[tuple[str, dict]] = []
    namespace["_call"] = lambda name, args: calls.append((name, args)) or "ok"
    return namespace, calls


def test_judge_helper_routes_to_the_host_op_and_drops_unset_keys():
    namespace, calls = _module()
    result = namespace["judge"](
        {"output": "x"},
        questions={"q0": {"instructions": "ok?"}},
        purpose="test",
        timeout_ms=1000,
    )
    assert result == "ok"
    assert calls == [(
        "__judge__",
        {
            "state": {"output": "x"},
            "questions": {"q0": {"instructions": "ok?"}},
            "purpose": "test",
            "timeout_ms": 1000,
        },
    )]


def test_judge_batch_helper_routes_to_the_batch_host_op():
    namespace, calls = _module("file")
    items = [{"key": "a", "state": "x"}]
    namespace["judge_batch"](
        items,
        questions_file="auto-thinking",
        labels={"solutionSpace": ["low", "max"]},
        min_ok=1,
    )
    assert calls == [(
        "__judge_batch__",
        {
            "items": items,
            "questions_file": "auto-thinking",
            "labels": {"solutionSpace": ["low", "max"]},
            "min_ok": 1,
        },
    )]


def test_judgment_helpers_are_not_model_tools():
    from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS

    assert "judge" not in SANDBOX_ALLOWED_TOOLS
    assert "judge_batch" not in SANDBOX_ALLOWED_TOOLS
