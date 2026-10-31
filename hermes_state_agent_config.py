"""Per-agent (role) custom configuration in the profile's ``state.db``.

The Agent Hub's role editor stores model/provider, adviser/prewalk toggles, reasoning effort and
pattern per agent id. This is profile state, so it lives beside the sessions rather than in
``config.yaml`` (which stays the profile-wide defaults) or ``WATCHDOG.yml`` (the adviser roster).
"""

from __future__ import annotations

import time
from typing import Any, Optional

_COLUMNS = ("model", "provider", "advisor", "prewalk", "reasoning_effort", "pattern", "prompt_path")


class SessionAgentConfigMixin:
    def get_agent_config(self, agent_id: str, source: str = "") -> Optional[dict[str, Any]]:
        row = self._read_one(
            "SELECT * FROM agent_config WHERE agent_id = ? AND source = ?", (agent_id, source))
        return dict(row) if row is not None else None

    def list_agent_configs(self, source: Optional[str] = None) -> list[dict[str, Any]]:
        if source is None:
            rows = self._read_all("SELECT * FROM agent_config ORDER BY agent_id, source")
        else:
            rows = self._read_all(
                "SELECT * FROM agent_config WHERE source = ? ORDER BY agent_id", (source,))
        return [dict(row) for row in rows]

    def upsert_agent_config(
        self, agent_id: str, updates: dict[str, Any], source: str = "",
    ) -> dict[str, Any]:
        """Merge *updates* into one role's row (keys outside the schema are ignored) and return it."""
        fields = {key: updates[key] for key in _COLUMNS if key in updates}
        for key in ("advisor", "prewalk"):
            if fields.get(key) is not None:
                fields[key] = 1 if fields[key] else 0
        now = time.time()

        def _do(conn):
            existing = conn.execute(
                "SELECT * FROM agent_config WHERE agent_id = ? AND source = ?",
                (agent_id, source)).fetchone()
            merged = {**dict(existing), **fields} if existing is not None else dict(fields)
            values = [merged.get("model"), merged.get("provider"), merged.get("advisor"),
                      merged.get("prewalk"), merged.get("reasoning_effort"), merged.get("pattern"),
                      merged.get("prompt_path")]
            conn.execute(
                "INSERT INTO agent_config"
                " (agent_id, source, model, provider, advisor, prewalk, reasoning_effort, pattern,"
                " prompt_path, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(agent_id, source) DO UPDATE SET"
                " model = excluded.model, provider = excluded.provider, advisor = excluded.advisor,"
                " prewalk = excluded.prewalk, reasoning_effort = excluded.reasoning_effort,"
                " pattern = excluded.pattern, prompt_path = excluded.prompt_path,"
                " updated_at = excluded.updated_at",
                (agent_id, source, *values, now),
            )
            return {**merged, "agent_id": agent_id, "source": source, "updated_at": now}

        return self._execute_write(_do)
