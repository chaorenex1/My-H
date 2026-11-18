"""``apply_retry_restarts`` bounds the refunding restarts (#106108).

The redirect and rebuilt-for-fallback paths refund the iteration budget and re-issue the
iteration; nothing else in the turn loop counts them, so a flag that keeps re-arming
(a request cancelled on every attempt) refunded forever and held the turn lease.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.turn_iteration_prep import apply_retry_restarts, begin_iteration
from agent.turn_retry_state import TurnRetryState

RESTART_FLAGS = ["restart_with_redirected_messages", "restart_with_rebuilt_messages"]
MAX_RETRIES = 3


def _apply(agent, flag: str | None, restart_count: int, response=None):
    """Run one iteration's restart handling; ``flag=None`` with a response means the model answered."""
    _retry = TurnRetryState()
    if flag:
        setattr(_retry, flag, True)
    return apply_retry_restarts(
        agent, _retry=_retry, response=response, interrupted=False, messages=[],
        conversation_history=[], user_message="hi", api_kwargs={}, current_turn_user_idx=0,
        final_response=None, retry_count=0, max_retries=MAX_RETRIES, api_call_count=1,
        restart_count=restart_count, length_continue_retries=0,
        _preflight_compression_blocked=True, _turn_exit_reason="unknown",
    )


def _agent():
    budget = SimpleNamespace(refunds=0)
    budget.refund = lambda: setattr(budget, "refunds", budget.refunds + 1)
    agent = SimpleNamespace(iteration_budget=budget, steered=[])
    agent._drain_pending_redirect = lambda: "last correction"
    agent.steer = agent.steered.append
    return agent


@pytest.mark.parametrize("flag", RESTART_FLAGS)
def test_single_restart_still_reissues_the_iteration(flag):
    """A lone correction / fallback activation keeps its refund-and-continue contract."""
    agent = _agent()
    verdict = _apply(agent, flag, restart_count=0)
    assert verdict.action == "continue"
    assert (agent.iteration_budget.refunds, verdict.api_call_count) == (1, 0)
    if flag == "restart_with_rebuilt_messages":
        assert verdict._preflight_compression_blocked is False  # still the single consumer


@pytest.mark.parametrize("flag", RESTART_FLAGS)
def test_back_to_back_restart_refunds_are_bounded(flag):
    """Re-arming the flag every iteration breaks after ``max_retries`` refunds instead of
    refunding forever (the turn ends, so the session turn lease is released)."""
    agent = _agent()
    restart_count, verdicts = 0, []
    while len(verdicts) < MAX_RETRIES + 5 and (not verdicts or verdicts[-1].action != "break"):
        verdicts.append(_apply(agent, flag, restart_count))
        restart_count = verdicts[-1].restart_count
    assert [v.action for v in verdicts] == ["continue"] * MAX_RETRIES + ["break"]
    assert agent.iteration_budget.refunds == MAX_RETRIES
    assert verdicts[-1]._turn_exit_reason.endswith("restart_limit_exceeded")
    # The correction that tripped the redirect cap is handed back as the next user turn.
    assert agent.steered == (["last correction"] if flag == "restart_with_redirected_messages" else [])


def _interrupted_agent(tool_interrupt_reason):
    agent = SimpleNamespace(
        _interrupt_requested=True, _tool_interrupt_reason=tool_interrupt_reason, quiet_mode=True,
        _drain_pending_redirect=lambda: None, _checkpoint_mgr=SimpleNamespace(new_turn=lambda: None),
    )
    return begin_iteration(
        agent, messages=[], conversation_history=[], original_user_message="hi",
        api_call_count=0, interrupted=False, _turn_exit_reason="unknown",
    )


@pytest.mark.parametrize("tool_interrupt_reason, expected", [
    # Human stops keep the historical reason (every ``interrupt()`` category, and a bare flag write).
    ("explicit stop requested", "interrupted_by_user"),
    ("user sent a new message", "interrupted_by_user"),
    (None, "interrupted_by_user"),
    # A producer that named itself via ``tool_reason`` is booked as the issuer (#112647).
    ("cron inactivity watchdog", "interrupted_by_system(cron_inactivity_watchdog)"),
    ("turn liveness watchdog", "interrupted_by_system(turn_liveness_watchdog)"),
])
def test_interrupt_exit_reason_names_the_system_issuer(tool_interrupt_reason, expected):
    """A watchdog abort must not be recorded as a user stop: the exit reason carries the issuer."""
    verdict = _interrupted_agent(tool_interrupt_reason)
    assert (verdict.action, verdict.interrupted, verdict._turn_exit_reason) == ("break", True, expected)


def test_begin_iteration_delivers_a_pending_ttsr_reminder_before_the_retry():
    class _Coordinator:
        def __init__(self) -> None:
            self.delivered = 0

        def has_pending_rewind(self) -> bool:
            return True

        def deliver_rewind(self, _agent, messages) -> bool:
            self.delivered += 1
            messages.append({"role": "custom", "custom_type": "ttsr-injection"})
            return True

        def drop_pending_rewind(self) -> None:
            raise AssertionError("a live retry must not drop its reminder")

    budget = SimpleNamespace(consume=lambda: True)
    agent = SimpleNamespace(
        _drain_pending_redirect=lambda: None, _checkpoint_mgr=SimpleNamespace(new_turn=lambda: None),
        _interrupt_requested=False, iteration_budget=budget, _budget_grace_call=False,
        _touch_activity=lambda *_a: None, _api_call_count=0, _current_turn_id="t",
        ttsr_coordinator=_Coordinator(), persisted=0,
    )
    agent._persist_session = lambda *_a, **_k: setattr(agent, "persisted", agent.persisted + 1)
    messages: list[dict] = []
    verdict = begin_iteration(
        agent, messages=messages, conversation_history=[], original_user_message="hi",
        api_call_count=0, interrupted=False, _turn_exit_reason="unknown",
    )
    assert verdict.action == "fallthrough"
    assert agent.ttsr_coordinator.delivered == 1
    assert messages == [{"role": "custom", "custom_type": "ttsr-injection"}]
    assert agent.persisted == 1


def test_ttsr_restart_refunds_and_delivers_the_reminder_at_the_limit():
    """ADR-6 D11: a stream rewind refunds the iteration; the cap delivers the pending reminder
    (the published ``ttsr.triggered`` must not lack its injection) and ends the turn."""
    class _Coordinator:
        def __init__(self) -> None:
            self.delivered = 0

        def deliver_rewind(self, _agent, _messages) -> bool:
            self.delivered += 1
            return True

    agent = _agent()
    agent.ttsr_coordinator = _Coordinator()
    agent._persist_session = lambda *_a, **_k: None

    restart_count, verdicts = 0, []
    while len(verdicts) < MAX_RETRIES + 3 and (not verdicts or verdicts[-1].action != "break"):
        verdicts.append(_apply(agent, "restart_with_ttsr_messages", restart_count))
        restart_count = verdicts[-1].restart_count

    assert [v.action for v in verdicts] == ["continue"] * MAX_RETRIES + ["break"]
    assert agent.iteration_budget.refunds == MAX_RETRIES
    assert verdicts[-1]._turn_exit_reason == "ttsr_restart_limit_exceeded"
    assert agent.ttsr_coordinator.delivered == 1


@pytest.mark.parametrize("flag", RESTART_FLAGS)
def test_response_between_restarts_resets_the_bound(flag):
    """#128000: the user answering clarify cards / sending follow-ups while the model works
    redirects once per request, with a response in between. The bound is for requests that
    are cancelled over and over, so a response starts it over instead of the whole turn
    sharing ``max_retries`` restarts."""
    agent = _agent()
    restart_count, actions = 0, []
    for _ in range(3):  # three rounds of max_retries restarts, each followed by a response
        for _ in range(MAX_RETRIES):
            verdict = _apply(agent, flag, restart_count)
            actions.append(verdict.action)
            restart_count = verdict.restart_count
        verdict = _apply(agent, None, restart_count, response=SimpleNamespace())
        assert (verdict.action, verdict.restart_count) == ("fallthrough", 0)
        restart_count = verdict.restart_count
    assert actions == ["continue"] * (3 * MAX_RETRIES)
    assert agent.steered == []
