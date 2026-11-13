"""Named-agent ``yield``: terminal completion, incremental sections, and schema retries."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent import agent_run
from agent import turn_tool_round
from tools.agent_yield_tool import YieldState, build_yield_tool


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": {"type": "object"}},
        "overall_correctness": {"enum": ["correct", "incorrect"]},
        "explanation": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["overall_correctness", "explanation", "confidence"],
}


def _state(**kwargs: Any) -> YieldState:
    return YieldState(schema=kwargs.get("schema"))


def _yield(state: YieldState, **args: Any) -> dict[str, Any]:
    return json.loads(build_yield_tool(state)["handler"](args))


# -- state --------------------------------------------------------------------


def test_final_yield_is_the_answer_text():
    state = _state()
    result = _yield(state, data={"ok": True})

    assert result["status"] == "yielded"
    assert state.payload is not None
    snapshot = state.snapshot()
    assert snapshot is not None and snapshot.terminal is True
    assert snapshot.data == {"ok": True}
    assert json.loads(snapshot.text) == {"ok": True}


def test_a_string_final_payload_is_kept_verbatim():
    state = _state()
    _yield(state, data="the report")

    snapshot = state.snapshot()
    assert snapshot is not None and snapshot.text == "the report"


def test_incremental_yield_records_a_section_and_keeps_working():
    state = _state(schema=SCHEMA)
    result = _yield(state, data={"title": "one"}, type=["findings"], final=False)

    assert result["status"] == "recorded"
    assert state.payload is None
    assert state.sections == [{"data": {"title": "one"}, "type": ["findings"]}]


def test_incremental_yield_requires_data():
    state = _state()
    result = _yield(state, type=["findings"], final=False)

    assert "error" in result
    assert state.payload is None and state.sections == []


def test_error_is_terminal_and_keeps_the_data_already_yielded():
    state = _state()
    result = _yield(state, data={"partial": True}, error="provider died", final=False)

    assert result["status"] == "yielded" and result["error"] == "provider died"
    snapshot = state.snapshot()
    assert snapshot is not None
    assert snapshot.terminal is True and snapshot.error == "provider died"
    assert snapshot.data == {"partial": True}


def test_schema_failures_return_bounded_tool_errors_before_accepting():
    state = _state(schema=SCHEMA)
    for _ in range(2):
        result = _yield(state, data={"findings": []})
        assert result["retry"] is True
        assert state.payload is None

    accepted = _yield(state, data={"findings": []})
    assert accepted["status"] == "yielded"
    snapshot = state.snapshot()
    assert snapshot is not None
    assert snapshot.terminal is True and snapshot.data == {"findings": []}


def test_a_schema_valid_yield_is_accepted_immediately():
    state = _state(schema=SCHEMA)
    result = _yield(state, data={"overall_correctness": "correct",
                                 "explanation": "fine", "confidence": 0.9})

    assert result["status"] == "yielded"
    assert state.schema_failures == 0


def test_final_sections_assemble_into_the_schema_shape():
    state = _state(schema=SCHEMA)
    _yield(state, data={"title": "one"}, type=["findings"], final=False)
    _yield(state, data={"title": "two"}, type=["findings"], final=False)
    _yield(state, data="correct", type=["overall_correctness"], final=False)
    _yield(state, data="looks fine", type=["explanation"], final=False)
    _yield(state, data=0.8, type=["confidence"], final=False)

    snapshot = state.snapshot()
    assert snapshot is not None and snapshot.terminal is False
    assert snapshot.data == {
        "findings": [{"title": "one"}, {"title": "two"}],
        "overall_correctness": "correct",
        "explanation": "looks fine",
        "confidence": 0.8,
    }
    assert json.loads(snapshot.text) == snapshot.data


def test_a_final_payload_merges_the_verdict_into_assembled_findings():
    state = _state(schema=SCHEMA)
    _yield(state, data={"title": "one"}, type=["findings"], final=False)
    _yield(state, data={"overall_correctness": "incorrect",
                        "explanation": "one bug", "confidence": 0.7})

    snapshot = state.snapshot()
    assert snapshot is not None
    assert snapshot.data["findings"] == [{"title": "one"}]
    assert snapshot.data["overall_correctness"] == "incorrect"


def test_reset_clears_a_terminal_yield_for_the_next_wake_turn():
    state = _state()
    _yield(state, data={"done": True})
    assert state.payload is not None

    state.reset()

    assert state.payload is None
    assert state.sections == [] and state.schema_failures == 0
    assert state.snapshot() is None


def test_a_data_less_terminal_validates_the_assembled_sections():
    state = _state(schema=SCHEMA)
    _yield(state, data={"title": "one"}, type=["findings"], final=False)

    rejected = _yield(state)

    assert rejected["retry"] is True
    assert state.payload is None

    _yield(state, data="correct", type=["overall_correctness"], final=False)
    _yield(state, data="looks fine", type=["explanation"], final=False)
    _yield(state, data=0.8, type=["confidence"], final=False)
    accepted = _yield(state)

    assert accepted["status"] == "yielded"
    snapshot = state.snapshot()
    assert snapshot is not None
    assert snapshot.data["findings"] == [{"title": "one"}]


def _prompt_agent():
    """A run agent; the role template renders for real, the base lives in system_prompt."""
    return SimpleNamespace()


def test_subagent_prompt_advertises_yield_when_the_run_has_the_tool():
    from agent.agent_run import _subagent_prompts

    agent = _prompt_agent()
    system, _ = _subagent_prompts(
        agent, "You review.", "", "go", wrap_assignment=True,
        structured=True, output_schema=SCHEMA)

    assert system.startswith("§ Subagent Role\nYou review.")
    assert agent._subagent_role.has_output_schema is True
    assert "No `yield` tool exists" not in system
    assert "Yield protocol" in system
    assert '"findings"' in system, "the output schema shape reaches the prompt"


def test_unstructured_subagent_prompt_keeps_the_legacy_handoff():
    from agent.agent_run import _subagent_prompts

    system, _ = _subagent_prompts(
        _prompt_agent(), "You review.", "", "go", wrap_assignment=True,
        structured=False)

    assert "No `yield` tool exists" in system


# -- turn round ---------------------------------------------------------------


class _RoundAgent:
    quiet_mode = True
    verbose_logging = False
    log_prefix = ""
    stream_delta_callback = None
    _hold_stream = False
    _tool_guardrail_halt_decision = None
    _incremental_persistence_failed = False
    ttsr_coordinator = None
    valid_tool_names = {"yield"}

    def __init__(self, state: YieldState) -> None:
        self._yield_state = state
        self._extra_tool_handlers = {"yield": state.record}
        self.printed: list[str] = []
        self.messages: list[dict[str, Any]] = []

    def _deduplicate_tool_calls(self, calls):
        return calls

    def _cap_delegate_task_calls(self, calls):
        return calls

    def _build_assistant_message(self, message, finish_reason):
        return {"role": "assistant", "content": message.content or ""}

    def _has_content_after_think_block(self, text):
        return False

    def _interim_assistant_visible_text(self, message):
        return ""

    def _emit_interim_assistant_message(self, message):
        pass

    def _flush_messages_to_session_db(self, messages, conversation_history):
        return True

    def _execute_tool_calls(self, assistant_message, messages, task_id, api_call_count):
        for call in assistant_message.tool_calls:
            result = self._extra_tool_handlers[call.function.name](
                json.loads(call.function.arguments))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    def _safe_print(self, text):
        self.printed.append(text)

    def _touch_activity(self, *_args):
        pass


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]):
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name,
                                             arguments=json.dumps(arguments)))


@pytest.fixture(autouse=True)
def _stub_round_side_effects(monkeypatch):
    monkeypatch.setattr(
        turn_tool_round, "validate_tool_calls",
        lambda *_a, **_k: SimpleNamespace(action="ok", result=None,
                                          mixed_invalid_batch=False))
    from agent import tool_call_batches

    monkeypatch.setattr(tool_call_batches, "expand_local_tool_batches",
                        lambda calls, **_kw: calls)
    monkeypatch.setattr(turn_tool_round, "normalize_provider_tool_call_ids",
                        lambda *_a: None)
    monkeypatch.setattr(
        turn_tool_round, "stage_tool_call_message",
        lambda *_a, **_k: ({"role": "assistant", "content": ""}, False))
    from agent import prepared_compaction

    monkeypatch.setattr(prepared_compaction, "maybe_prepare", lambda *_a, **_k: None)
    from hermes_cli.observability import shared_metrics_harness as metrics

    monkeypatch.setattr(metrics, "finish_tool_round", lambda *_a, **_k: None)


def _run_round(agent: _RoundAgent, data: dict[str, Any]):
    assistant = SimpleNamespace(content="", provider_data=None, tool_calls=[
        _tool_call("c1", "yield", data)])
    return turn_tool_round.run_tool_round(
        agent, assistant_message=assistant, finish_reason="tool_calls", messages=[],
        conversation_history=[], api_call_count=1, effective_task_id=None,
        user_message="go", system_message=None, active_system_prompt="",
        compression_attempts=0, max_compression_attempts=3, final_response=None,
        failed=False, _turn_exit_reason=None, truncated_tool_call_retries=0,
        current_turn_user_idx=0)


def test_a_terminal_yield_breaks_the_round_and_appends_the_answer():
    state = _state()
    agent = _RoundAgent(state)

    verdict = _run_round(agent, {"data": {"ok": True}, "final": True})

    assert verdict.action == "break"
    assert verdict._turn_exit_reason == "yielded"
    assert json.loads(verdict.final_response) == {"ok": True}
    assert verdict.messages[-1]["role"] == "assistant"
    assert verdict.messages[-1]["content"] == verdict.final_response


# -- run result ---------------------------------------------------------------


class _RunAgent:
    def __init__(self, state: YieldState) -> None:
        self._yield_state = state

    def run_conversation(self, message, system_message=None, conversation_history=None,
                         task_id=None):
        self._yield_state.reset()
        self._yield_state.record({"data": {"ok": True}, "final": True})
        return {
            "final_response": "ignored prose",
            "api_calls": 1,
            "messages": [{"role": "user", "content": message},
                         {"role": "assistant", "content": "ignored prose"}],
        }


class _SectionsAgent:
    """Stops without a terminal yield; the sections are the completion."""

    def __init__(self, state: YieldState) -> None:
        self._yield_state = state

    def run_conversation(self, message, system_message=None, conversation_history=None,
                         task_id=None):
        self._yield_state.reset()
        for data, type_name in (
            ({"title": "one"}, "findings"),
            ("correct", "overall_correctness"),
            ("looks fine", "explanation"),
            (0.9, "confidence"),
        ):
            self._yield_state.record({
                "data": data, "type": [type_name], "final": False})
        return {
            "final_response": "done",
            "api_calls": 1,
            "messages": [{"role": "user", "content": message},
                         {"role": "assistant", "content": "done"}],
        }


def test_execute_turn_assembles_sections_when_the_model_stops():
    state = _state(schema=SCHEMA)
    agent = _SectionsAgent(state)
    peer = agent_run._execute_turn(
        agent, "go", context="", conversation=None, system="",
        schema=SCHEMA, task_id="task-1", agent_name="reviewer",
        agent_session_id="reviewer-1", model="fake", provider="fake",
        toolsets=(), all_tools=False, history_key="", account_task="",
        record_history=False)

    assert json.loads(peer.answer)["findings"] == [{"title": "one"}]
    assert peer.yield_data == json.loads(peer.answer)
    assert peer.output_schema_valid is True
    assert peer.messages[-1]["content"] == peer.answer


def test_execute_turn_uses_the_yielded_value_as_the_answer():
    state = _state()
    agent = _RunAgent(state)
    peer = agent_run._execute_turn(
        agent, "go", context="", conversation=None, system="",
        schema=None, task_id="task-1", agent_name="reviewer",
        agent_session_id="reviewer-1", model="fake", provider="fake",
        toolsets=(), all_tools=False, history_key="", account_task="",
        record_history=False)

    assert json.loads(peer.answer) == {"ok": True}
    assert peer.yield_data == {"ok": True}
    assert peer.messages[-1]["content"] == peer.answer


def test_enable_tools_never_drops_the_host_owned_yield_tool():
    agent = SimpleNamespace(
        tools=[{"type": "function", "function": {"name": name}}
               for name in ("read_file", "write_file", "yield")],
        valid_tool_names={"read_file", "write_file", "yield"},
        _extra_tool_handlers={"yield": lambda _args: "ok"})

    dropped = agent_run.apply_tool_allowlist(agent, ["read_file"])

    assert [tool["function"]["name"] for tool in agent.tools] == ["read_file", "yield"]
    assert agent.valid_tool_names == {"read_file", "yield"}
    assert dropped == ["write_file"]
