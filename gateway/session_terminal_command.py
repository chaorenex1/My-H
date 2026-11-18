"""Run one command inside a session's own terminal backend.

Panel verbs (Git TUI, plan review) must not touch the gateway host when the session runs in a
remote / sandboxed terminal: they use the same ``terminal_tool`` path the agent's own commands do,
with the session's frozen policy bound and the session folder as the per-command ``workdir``. The
command never writes the session's recorded ``cd`` state (``workdir`` is transient), so opening a
panel can't move the agent's next turn.
"""

from __future__ import annotations

import json

from gateway.session_promoted import scoped
from hermes_state_runtime import RuntimeStoreError


async def run(connection, ref, command: str, *, timeout: int = 30) -> dict:
    """``{"output": str, "exit_code": int, "error": str | None}`` from the session's backend."""
    authority = connection.authority
    live = authority.sessions.get(ref.session_id)
    if live is None:
        raise RuntimeStoreError("not_found")
    from gateway.session_policy import policy_for_source, policy_scope, terminal_uses_host_cwd

    policy = policy_for_source(authority.runner, live.source)
    if policy is None:
        raise RuntimeStoreError("permission_denied")
    try:
        terminal = json.loads(policy.terminal_json)
    except (TypeError, ValueError):
        terminal = {}
    # A host-cwd backend (local, or docker mounting this folder) needs the policy's workspace.
    # SSH/modal/other sandboxes keep their own configured cwd: sending the gateway-host path as a
    # per-command workdir would `cd` to a directory that does not exist there.
    workdir = policy.cwd if terminal_uses_host_cwd(terminal) else None

    def execute() -> str:
        from tools.terminal_tool import terminal_tool

        with policy_scope(policy, authority=authority):
            return terminal_tool(
                command, task_id=live.route, session_id=ref.session_id,
                workdir=workdir, timeout=timeout,
            )

    raw = await scoped(connection, execute)
    try:
        result = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeStoreError("storage_unavailable", detail=str(raw)[:200]) from exc
    if not isinstance(result, dict):
        raise RuntimeStoreError("storage_unavailable", detail="terminal result is not an object")
    return {
        "output": str(result.get("output") or ""),
        "exit_code": int(result.get("exit_code") if result.get("exit_code") is not None else -1),
        "error": result.get("error"),
    }


async def run_checked(connection, ref, command: str, *, timeout: int = 30, field: str = "command") -> str:
    """``run`` requiring exit 0; a failure is ``invalid_params`` naming the command's output."""
    result = await run(connection, ref, command, timeout=timeout)
    if result["exit_code"] != 0:
        detail = (result["error"] or result["output"] or "command failed").strip().replace("\n", " ")
        raise RuntimeStoreError("invalid_params", field=field, detail=detail[:400])
    return result["output"]
