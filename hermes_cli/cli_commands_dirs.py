"""``/add-dir``, ``/remove-dir`` and ``/dirs``: a local session's additional workspace roots.

The store, path rules and reply text live in :mod:`agent.workspace_dirs`; this mixin only wires
the classic CLI (`HermesCLI`) to it. A gateway-backed session routes the same commands to the
owner's handler (``hermes_cli.gateway_setting_commands``), where the agent whose prompt lists
the roots actually lives.
"""

from __future__ import annotations


class CLIWorkspaceDirsMixin:
    """Slash handlers for the session's extra workspace directories."""

    def _handle_add_dir_command(self, cmd_original: str) -> None:
        self._apply_workspace_dirs("add", cmd_original)

    def _handle_remove_dir_command(self, cmd_original: str) -> None:
        self._apply_workspace_dirs("remove", cmd_original)

    def _handle_dirs_command(self, cmd_original: str) -> None:
        self._apply_workspace_dirs("list", cmd_original)

    def _apply_workspace_dirs(self, action: str, cmd_original: str) -> None:
        from agent import workspace_dirs

        session_id = getattr(self, "session_id", None)
        db = getattr(self, "_session_db", None)
        if action == "list":
            print(workspace_dirs.list_dirs(session_id, db=db))
            return
        parts = (cmd_original or "").split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        apply_dir = workspace_dirs.add_dir if action == "add" else workspace_dirs.remove_dir
        result = apply_dir(session_id, arg, db=db)
        print(result.text)
        if not result.changed:
            return
        # The next turn's prompt must list the new roots: an explicit workspace change is one of
        # the few commands that may take the one-time prefix-cache break (like a model switch).
        agent = getattr(self, "agent", None)
        invalidate = getattr(agent, "_invalidate_system_prompt", None)
        if callable(invalidate):
            invalidate()
