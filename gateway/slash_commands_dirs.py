"""``/add-dir``, ``/remove-dir`` and ``/dirs`` on a LOCAL session.

The session's agent lives in the gateway, so the owner applies the change (store + persisted
row) and evicts the cached agent: the next turn rebuilds its system prompt with the new
``<workspace-roots>`` block. Messaging chats have no client workspace and are refused.
"""

from __future__ import annotations

import asyncio

from agent.i18n import t


class GatewayWorkspaceDirsMixin:
    """Slash handlers for a local session's additional workspace roots."""

    async def _handle_add_dir_command(self, event):
        return await self._workspace_dir_reply(event, "add")

    async def _handle_remove_dir_command(self, event):
        return await self._workspace_dir_reply(event, "remove")

    async def _handle_dirs_command(self, event):
        return await self._workspace_dir_reply(event, "list")

    @staticmethod
    def _workspace_dir_session_id(event) -> str | None:
        """The LOCAL session id this command may edit, else None.

        A local session's ``SessionSource.chat_id`` IS its ``sessions.id``: ``local_source``
        (gateway/session_local_recovery.py) builds it from the id and the session entry's
        ``route`` is the separate store key. Reading the same identity the agent's prompt
        builder uses (``agent.session_id``) is what keeps ``/dirs`` and the model's
        ``<workspace-roots>`` block in agreement.
        """
        from gateway.config import Platform
        source = getattr(event, "source", None)
        if source is None or source.platform != Platform.LOCAL:
            return None
        return str(source.chat_id or "") or None

    def _workspace_dir_store(self):
        """The synchronous SessionDB behind the runner's async handle (None when unavailable)."""
        handle = getattr(self, "_session_db", None)
        return getattr(handle, "_db", handle)

    async def _workspace_dir_reply(self, event, action: str) -> str:
        from agent import workspace_dirs

        session_id = self._workspace_dir_session_id(event)
        if session_id is None:
            return t("workspace_dirs.local_only")
        db = self._workspace_dir_store()
        if action == "list":
            return await asyncio.to_thread(workspace_dirs.list_dirs, session_id, db=db)
        arg = (event.get_command_args() or "").strip()
        apply_dir = workspace_dirs.add_dir if action == "add" else workspace_dirs.remove_dir
        result = await asyncio.to_thread(apply_dir, session_id, arg, db=db)
        if result.changed:
            evict = getattr(self, "_evict_cached_agent", None)
            if callable(evict):
                evict(self._session_key_for_source(event.source))
        return result.text
