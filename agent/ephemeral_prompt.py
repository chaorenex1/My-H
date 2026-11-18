"""One ephemeral side turn over a live parent agent's warm prefix (``/btw``, idle recap).

A cache-parity fork replays the parent's system prompt, ``tools[]`` and history byte-for-byte so the
request hits the parent's prompt cache; a thread-scoped empty tool whitelist denies every tool call
at dispatch while the advertised schema stays identical. The fork never persists, never writes a
session row, and never changes the parent's conversation. Callers own the prompt and the meaning of
the reply; this module owns only the fork lifecycle.
"""

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DENY = "Ephemeral side turn denied tool call: {tool_name}. Tools are disabled here — reply in plain text."


def run_ephemeral_prompt(
    parent: Any, prompt: str, *, task: str, max_iterations: int,
    max_output_tokens: Optional[int] = None,
    history: Optional[list[dict[str, Any]]] = None,
    on_fork: Optional[Callable[[Any], None]] = None,
    deny_message: str = _DEFAULT_DENY,
    task_cfg: Optional[dict[str, Any]] = None,
) -> str:
    """Run *prompt* once against a detached fork of *parent* and return its text.

    ``history`` defaults to the parent's live ``_session_messages``; a caller that captured a
    snapshot earlier passes it so a concurrent turn cannot change the replay. ``on_fork`` publishes
    the built fork (and ``None`` after teardown) so an owner can interrupt it. Usage is attributed
    to ``task`` on the parent session. Raises on a failed turn; an empty reply returns ``""``.
    """
    from agent.background_review import _digest_history, build_cache_parity_fork, release_cache_parity_fork
    from agent.side_question import trim_snapshot_for_fork
    from hermes_cli.plugins import clear_thread_tool_whitelist, set_thread_tool_whitelist

    fork, _rt, routed = build_cache_parity_fork(
        parent, task_cfg or {}, max_iterations=max_iterations, write_origin=task)
    if max_output_tokens is not None:
        fork._ephemeral_max_output_tokens = int(max_output_tokens)
    if on_fork is not None:
        on_fork(fork)
    try:
        set_thread_tool_whitelist(set(), deny_msg_fmt=deny_message)
        snapshot = trim_snapshot_for_fork(
            history if history is not None else getattr(parent, "_session_messages", None))
        result = fork.run_conversation(
            user_message=prompt,
            conversation_history=_digest_history(snapshot) if routed else snapshot)
        answer = (result or {}).get("final_response", "") or ""
        if not answer and result and result.get("error"):
            raise RuntimeError(str(result["error"]))
        return answer.strip()
    finally:
        clear_thread_tool_whitelist()
        release_cache_parity_fork(parent, fork, task=task)
        if on_fork is not None:
            on_fork(None)
