"""`/prewalk` slash handler (ADR-12 D5) — owner-side so the CLI mixin stays under its line cap."""

from __future__ import annotations

from hermes_cli.cli_commands_session_tools import _accent_line, _command_arg, _cp, _dim_line

LOCAL_SURFACES = frozenset({"cli", "tui", "desktop", "omp-ui", "local"})


def handle_prewalk_command(host, cmd: str):
    """Dispatch `/prewalk [off|restart|status] [target] [from]` for one CLI session."""
    from agent import prewalk

    platform = getattr(host, "platform", "cli") or "cli"
    if platform not in LOCAL_SURFACES:
        return _cp(_dim_line("prewalk is local-only: run /prewalk in the CLI, TUI or desktop app."))
    agent = getattr(host, "agent", None)
    if agent is None:
        return _cp(_dim_line("No live agent yet; send one message first."))
    parts = _command_arg(cmd).split()
    action = parts[0].lower() if parts else "status"
    try:
        if action == "status":
            state = prewalk.status(agent)
        elif action == "off":
            state = prewalk.disarm(agent)
        elif action in {"restart", "arm"}:
            target = parts[1] if len(parts) > 1 else None
            source = parts[2] if len(parts) > 2 else None
            state = (prewalk.restart(agent, into=target, from_=source) if action == "restart"
                     else prewalk.arm(agent, into=target, from_=source))
        else:
            state = prewalk.arm(agent, into=parts[0], from_=parts[1] if len(parts) > 1 else None)
    except ValueError as exc:
        return _cp(_dim_line(str(exc)))
    status = "armed" if state.get("armed") else ("spent" if state.get("consumed") else "off")
    line = f"prewalk: {status}  {state.get('from') or '@default'} -> {state.get('into') or '@smol'}"
    gate = f"  todo gate {state.get('gate', 'closed')}" if state.get("armed") else ""
    auto = f"  auto={state.get('auto_mode', 'off')}" if state.get("auto_mode") else ""
    return _cp(_accent_line(line + gate), _dim_line(f"target resolves on the next edit{auto}"))
