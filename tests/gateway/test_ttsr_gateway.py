"""Gateway seams: TTSR event ordering, install gate, and reminder placement."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from gateway import local_viewer_events
from gateway import run_turn_progress
from gateway.run_turn_progress import GatewayTurnProgressMixin
from gateway import session_policy


class _Turn:
    def __init__(self, agent: Any) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._runner: Any = None
        self._ctx = SimpleNamespace(
            agent_holder=[agent],
            _voice_ack_guild=[None],
            _native_slack_task_cards=False,
            progress_mode="all",
            session_id="s1",
        )

    def _publish_execution(self, event_type: str, payload: dict[str, Any]) -> bool:
        self.events.append((event_type, payload))
        return True

    def _publish_api_tool(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Agent:
    def __init__(self) -> None:
        self.ttsr_coordinator = None


def test_tool_start_callback_no_longer_matches_ttsr(monkeypatch):
    """ADR-6 D10: matching moved before staging in ``run_tool_round``; the gateway callback
    runs after persistence/execution and only reports ``tool.start``."""
    monkeypatch.setattr(run_turn_progress, "local_viewer_turn", lambda _turn: True)
    agent = _Agent()

    class _Coordinator:
        def match_tool_start(self, *_args):
            raise AssertionError("the gateway callback must not match TTSR")

    agent.ttsr_coordinator = _Coordinator()
    turn: Any = _Turn(agent)
    GatewayTurnProgressMixin.combined_tool_start_callback(
        turn, "c1", "patch", {"path": "a.ts", "new_string": "x: any"})
    assert [name for name, _payload in turn.events] == ["tool.start"]


def test_no_coordinator_emits_no_ttsr_event(monkeypatch):
    monkeypatch.setattr(run_turn_progress, "local_viewer_turn", lambda _turn: True)
    turn: Any = _Turn(_Agent())
    GatewayTurnProgressMixin.combined_tool_start_callback(
        turn, "c1", "patch", {"path": "a.ts", "new_string": "x"})
    assert [name for name, _payload in turn.events] == ["tool.start"]


def test_publish_helper_uses_the_contract_event():
    turn = _Turn(_Agent())
    assert local_viewer_events.publish_ttsr_triggered(turn, [{"name": "r"}]) is True
    assert turn.events == [("ttsr.triggered", {"rules": [{"name": "r"}]})]


def test_publish_message_rewind_advances_the_segment_cursor():
    from tui_gateway.stream_segments import TurnSegments

    class _RewindTurn(_Turn):
        def __init__(self) -> None:
            super().__init__(_Agent())
            self._approval_owner = ("authority", "s1", "generation")
            self.segments = TurnSegments()
            self.segments.segment = 2

        def _segments_for(self, _generation: str):
            return self.segments

    turn = _RewindTurn()
    assert local_viewer_events.publish_message_rewind(turn) is True
    assert turn.events == [("message.rewind", {
        "reason": "ttsr", "message_id": turn.segments.message_id, "segment": 3})]
    assert turn.segments.offset == 0


def test_install_coordinator_requires_the_omp_ui_client(monkeypatch, tmp_path):
    from agent import ttsr_runtime

    monkeypatch.setattr(run_turn_progress, "local_viewer_turn", lambda _turn: True)
    monkeypatch.setattr(ttsr_runtime, "discover_rules", lambda *_a, **_k: [])
    monkeypatch.setattr(ttsr_runtime, "load_ttsr_settings", lambda: ttsr_runtime.TtsrSettings())
    agent = _Agent()
    turn = _Turn(agent)
    turn._runner = SimpleNamespace(_session_db=SimpleNamespace(_db=None))
    monkeypatch.setattr(session_policy, "active_policy",
                        lambda: SimpleNamespace(client="desktop", cwd=str(tmp_path)))
    assert ttsr_runtime.install_coordinator(turn, agent) is None
    assert agent.ttsr_coordinator is None

    monkeypatch.setattr(session_policy, "active_policy",
                        lambda: SimpleNamespace(client="omp-ui", cwd=str(tmp_path)))
    previous = object()
    agent.ttsr_coordinator = previous
    sinks = ttsr_runtime.install_coordinator(turn, agent)
    assert sinks is not None
    assert isinstance(agent.ttsr_coordinator, ttsr_runtime.TtsrCoordinator)
    ttsr_runtime.restore_coordinator(agent, sinks)
    assert agent.ttsr_coordinator is previous


def test_install_coordinator_shares_undelivered_memory_across_turns(monkeypatch, tmp_path):
    from agent import ttsr_runtime
    from agent.rule_utils import parse_rule_text

    row = parse_rule_text("---\ncondition: 'any'\ninterruptMode: never\n---\nbody\n",
                          source=str(tmp_path / "rules" / "r.md"), rule_id="r")
    monkeypatch.setattr(run_turn_progress, "local_viewer_turn", lambda _turn: True)
    monkeypatch.setattr(ttsr_runtime, "discover_rules", lambda *_a, **_k: [row])
    monkeypatch.setattr(ttsr_runtime, "load_ttsr_settings", lambda: ttsr_runtime.TtsrSettings())
    monkeypatch.setattr(session_policy, "active_policy",
                        lambda: SimpleNamespace(client="omp-ui", cwd=str(tmp_path)))
    agent = _Agent()
    turn = _Turn(agent)
    turn._runner = SimpleNamespace(_session_db=SimpleNamespace(_db=None))

    sinks = ttsr_runtime.install_coordinator(turn, agent)
    assert sinks is not None
    coordinator = agent.ttsr_coordinator
    assert coordinator.match_tool_start("c1", "write_file", {"path": "a.ts", "content": "any"})
    coordinator.after_tool_batch(SimpleNamespace(is_interrupted=True), [
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ], 1)
    ttsr_runtime.restore_coordinator(agent, sinks)

    # The next turn gets a new coordinator but the same session-lifetime memory marks.
    ttsr_runtime.install_coordinator(turn, agent)
    assert agent.ttsr_coordinator.match_tool_start(
        "c2", "write_file", {"path": "b.ts", "content": "any"}) == []


def test_install_publishes_one_unsupported_notice(monkeypatch, tmp_path):
    from agent import ttsr_runtime
    from agent.rule_utils import parse_rule_text

    row = parse_rule_text("---\ncondition: 'x'\ninterruptMode: sometimes\n---\nbody\n",
                          source=str(tmp_path / "rules" / "r.md"), rule_id="r")
    monkeypatch.setattr(run_turn_progress, "local_viewer_turn", lambda _turn: True)
    monkeypatch.setattr(ttsr_runtime, "discover_rules", lambda *_a, **_k: [row])
    monkeypatch.setattr(ttsr_runtime, "load_ttsr_settings", lambda: ttsr_runtime.TtsrSettings())
    monkeypatch.setattr(session_policy, "active_policy",
                        lambda: SimpleNamespace(client="omp-ui", cwd=str(tmp_path)))
    agent = _Agent()
    turn = _Turn(agent)
    turn._runner = SimpleNamespace(_session_db=SimpleNamespace(_db=None))

    sinks = ttsr_runtime.install_coordinator(turn, agent)
    assert [name for name, _payload in turn.events] == ["status.update", "transcript.notice"]
    assert turn.events[0][1]["kind"] == "ttsr_unsupported"
    assert "interruptMode: sometimes" in turn.events[1][1]["text"]
    ttsr_runtime.restore_coordinator(agent, sinks)

    # The same unsupported set is announced once, not on every turn.
    ttsr_runtime.install_coordinator(turn, agent)
    assert len(turn.events) == 2


def test_finalize_tool_batch_calls_the_coordinator_after_steer(monkeypatch):
    from agent import tool_executor

    monkeypatch.setattr(tool_executor, "enforce_turn_budget", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_executor.plugin_live_notes, "keep_after_turn_budget",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(tool_executor, "sync_rewritten_tool_rows", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_executor, "record_tool_batch", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_executor, "get_active_env", lambda *_a, **_k: None)

    observed: list[list[dict[str, Any]]] = []

    class _Coordinator:
        def after_tool_batch(self, _agent, messages, num_tools, *, result_ids=None):
            assert messages[-1]["display_kind"] == "steer"
            assert result_ids == {"c1"}
            observed.append(messages)

    def _steer(messages, num_tools):
        messages.append({"role": "user", "content": "steer", "display_kind": "steer"})

    agent = SimpleNamespace(
        quiet_mode=True,
        ttsr_coordinator=_Coordinator(),
        _apply_pending_steer_to_tool_results=_steer,
    )
    messages = [{"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    tool_executor._finalize_tool_batch(agent, messages, "task", 1, cast(Any, SimpleNamespace()))
    assert observed and len(messages) == 2
