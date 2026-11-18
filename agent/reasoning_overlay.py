"""Per-request reasoning overlays (ADR-17 P5 and the one-shot thinking-off recovery).

Kept beside ``agent.chat_completion_helpers`` rather than inside it: the helper
facade is already over its line cap, and this is request-state logic, not request
assembly.
"""

from __future__ import annotations


def consume_ephemeral_reasoning_off(agent) -> bool:
    """Consume the one-shot "answer without thinking" continuation flag.

    Set by the length-continuation path when a request returned reasoning but NO
    visible content (thinking ate the output cap); continuation turns never replay
    prior reasoning, so thinking ON would re-burn the budget. When True the caller
    overrides the wire reasoning_config with ``{"enabled": False, "effort": "none"}``
    for exactly the next call. Prompt-cache cost is bounded to ONE cold prefix write
    on config-sensitive providers (Anthropic, OpenAI) — far cheaper than four futile
    full-budget continuations.
    """
    consumed = bool(getattr(agent, "_ephemeral_reasoning_off", False))
    if consumed:
        agent._ephemeral_reasoning_off = False
    return consumed


def auto_reasoning_overlay(agent):
    """The per-turn auto-reasoning config, while it still belongs to this route (ADR-17 P5).

    A model switch or fallback re-resolve makes it stale — the new route's effort
    surface may differ — so it is dropped here rather than sent; the freshly
    resolved ``agent.reasoning_config`` wins from then on.
    """
    overlay = getattr(agent, "_auto_reasoning_config", None)
    if not overlay:
        return None
    resolved = getattr(agent, "_auto_reasoning_route", None)
    if resolved is not None and resolved != (
        getattr(agent, "provider", None),
        getattr(agent, "model", None),
        getattr(agent, "api_mode", None),
    ):
        agent._auto_reasoning_config = None
        agent._auto_reasoning_route = None
        return None
    return overlay
