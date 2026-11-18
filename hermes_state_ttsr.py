"""``session_ttsr_injections``: durable ``repeatMode: once`` state for TTSR rules.

The notification itself is live-only (ADR-6 D6); this table remembers which rule names were
already delivered in a session so a resumed session does not inject the same reminder again.
Rows cascade with the session (FK), so no explicit delete path is needed.
"""

from __future__ import annotations

import time
from typing import Any, Iterable


class SessionTtsrMixin:
    def ttsr_injected_names(self, session_id: str) -> set[str]:
        if not session_id:
            return set()
        rows = self._read_all(
            "SELECT rule_name FROM session_ttsr_injections WHERE session_id = ?",
            (str(session_id),),
        )
        return {str(row["rule_name"]) for row in rows}

    def mark_ttsr_injected(
        self, session_id: str, rule_names: Iterable[str], *, injected_at: float | None = None,
    ) -> int:
        """Record delivered rule names; returns the number of rows written."""
        names = sorted({str(name) for name in rule_names if str(name or "").strip()})
        if not session_id or not names:
            return 0
        now = time.time() if injected_at is None else float(injected_at)

        def _do(conn: Any) -> int:
            conn.executemany(
                "INSERT OR IGNORE INTO session_ttsr_injections"
                " (session_id, rule_name, injected_at) VALUES (?, ?, ?)",
                [(str(session_id), name, now) for name in names],
            )
            return len(names)

        return int(self._execute_write(_do) or 0)
