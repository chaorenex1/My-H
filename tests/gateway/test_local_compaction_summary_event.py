"""ADR-5 D4: a committed compaction publishes ``compaction.summary`` once, before status noise rules.

The status line may be suppressed (muted reply, platform noise), but the block is not a status
line: the event must still reach a local viewer, and an aborted attempt's ``compacted`` terminal
edge must publish nothing.
"""
from __future__ import annotations

from types import SimpleNamespace

from gateway.config import Platform
from gateway.run_turn_progress import GatewayTurnProgressMixin


def _turn(platform, agent, published, *, mute=True):
    turn = object.__new__(GatewayTurnProgressMixin)
    turn._ctx = SimpleNamespace(
        agent_holder=[agent],
        mute_notification_reply=mute,
        source=SimpleNamespace(platform=platform),
    )
    turn._approval_owner = (SimpleNamespace(), "s", 1)  # local_viewer_turn reads this
    turn._publish_execution = lambda kind, payload: published.append((kind, payload)) or True
    turn._status_live = lambda: True
    return turn


def _payload():
    return {
        "text": "SUMMARY BODY", "tokens_before": 1200, "tokens_after": 300,
        "token_method": "estimate_rough", "method": "llm_summary", "trigger": "pre_api",
        "timestamp": 99.0, "boundary_row_id": 42,
    }


def test_committed_compaction_publishes_before_status_suppression():
    payload = _payload()
    agent = SimpleNamespace(_last_compaction_display=dict(payload))
    published = []
    turn = _turn(Platform.LOCAL, agent, published, mute=True)

    turn._status_callback_sync("compacted", "Context compaction complete")

    assert published == [("compaction.summary", payload)]
    assert agent._last_compaction_display is None  # consumed: one event per commit


def test_the_abort_terminal_edge_publishes_nothing():
    agent = SimpleNamespace(_last_compaction_display=None)
    published = []
    turn = _turn(Platform.LOCAL, agent, published)

    turn._status_callback_sync("compacted", "Context compaction complete")

    assert published == []


def test_a_platform_turn_publishes_no_compaction_event():
    agent = SimpleNamespace(_last_compaction_display=_payload())
    published = []
    turn = _turn(Platform.TELEGRAM, agent, published)

    turn._status_callback_sync("compacted", "Context compaction complete")

    assert published == []
    assert agent._last_compaction_display is not None  # untouched; nothing consumed it


def test_a_start_failure_does_not_emit_a_compaction_summary():
    agent = SimpleNamespace(_last_compaction_display=_payload())
    published = []
    turn = _turn(Platform.LOCAL, agent, published)

    turn._status_callback_sync("lifecycle", "Compacting context")

    assert published == []
