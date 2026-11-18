"""Turn entry helpers that decide whether the generic loop runs at all.

A turn can finish before any inference request (a ``pre_llm_call`` veto) or run in the opt-in
codex app-server subprocess. Both live here so ``conversation_loop`` stays the loop facade.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.turn_context import PRE_LLM_BLOCK_EXIT_REASON


def reset_turn_yield(agent: Any) -> None:
    """Clear the previous turn's terminal ``yield``; a wake turn must not break immediately."""
    state = getattr(agent, "_yield_state", None)
    reset = getattr(state, "reset", None)
    if callable(reset):
        reset()


def turn_without_loop(agent: Any, state: Any, block_message: str) -> Optional[dict[str, Any]]:
    """The turn's result when it never enters the generic loop, else None."""
    if block_message:
        state.final_response, state._turn_exit_reason = block_message, PRE_LLM_BLOCK_EXIT_REASON
        from agent.conversation_loop import _finalize_from_state

        return _finalize_from_state(agent, state)
    return _codex_app_server_turn(agent, state) if agent.api_mode == "codex_app_server" else None


def _codex_app_server_turn(agent: Any, state: Any) -> Optional[dict[str, Any]]:
    """The codex app-server result, or None when its failure activated a fallback.

    A fallback activation rewrote provider/model/api_mode: the generic loop retries the same
    user turn, keeping codex's projected rows and its failed API call in the accounting.
    """
    codex_result = agent._run_codex_app_server_turn(
        user_message=state.user_message, original_user_message=state.original_user_message,
        messages=state.messages, effective_task_id=state.effective_task_id,
        should_review_memory=state._should_review_memory, turn_id=state.turn_id,
    )
    from agent.conversation_loop import _sync_failover_system_message
    from agent.turn_recovery import activate_codex_app_server_fallback

    if not activate_codex_app_server_fallback(agent, codex_result):
        return codex_result
    state.api_call_count = int(codex_result.get("api_calls") or 0)
    state.active_system_prompt = _sync_failover_system_message(
        agent, None, state.active_system_prompt)
    return None


__all__ = ["reset_turn_yield", "turn_without_loop"]
