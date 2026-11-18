"""``agent_inbox``: the durable delivery queue for named agent sessions (ADR-15 D4).

A row is written when a message cannot reach a live run (``pending``) and, for the audit trail,
when it was delivered (``delivered``). ``agent_out(mode="inbox")`` reads both; the wake turn
(P4) drains pending rows before it starts. Nothing here decides *how* a message is delivered —
that lives in :mod:`tools.agent_inbox` — so the table stays useful across the later phases.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

#: The queue's life cycle. ``failed`` is terminal: the target never accepted the record.
INBOX_STATUSES = ("pending", "delivered", "failed")
#: What produced a row; peer/broadcast kinds arrive with P5, ``wake_relay`` with P4.
INBOX_KINDS = ("message", "broadcast", "progress", "wake_relay")


def _decode(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["broadcast"] = bool(data.get("broadcast"))
    data["progress"] = bool(data.get("progress"))
    return data


class SessionAgentInboxMixin:
    def add_agent_inbox(
        self, *, agent_session_id: str, sender: str, recipient: str, body: str,
        kind: str = "message", reply_to: str = "", broadcast: bool = False,
        progress: bool = False, status: str = "pending", inbox_id: str = "",
    ) -> str:
        """Insert one delivery record and return its id.

        The id is minted here, outside the write callback, so a lock-retry replays an identical
        INSERT instead of stacking a second row. A caller that must put the id in the delivered
        envelope first passes ``inbox_id``; the replay is still identical.
        """
        inbox_id = str(inbox_id or "") or f"inbox-{uuid.uuid4().hex}"
        now = time.time()
        row = (inbox_id, str(agent_session_id), str(sender or ""), str(recipient or ""),
               str(body), str(kind), str(reply_to or "") or None, int(bool(broadcast)),
               int(bool(progress)), str(status), 0, now,
               now if status != "pending" else None)
        self._execute_write(lambda conn: conn.execute(
            "INSERT INTO agent_inbox (id, agent_session_id, sender, recipient, body, kind, "
            "reply_to, broadcast, progress, status, attempts, created_at, delivered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING", row))
        return inbox_id

    def list_agent_inbox(self, agent_session_id: str, *, status: Optional[str] = None,
                         limit: int = 20) -> list[dict[str, Any]]:
        """Newest-first records for one session, optionally one status."""
        sql = "SELECT * FROM agent_inbox WHERE agent_session_id=?"
        params: list[Any] = [str(agent_session_id)]
        if status:
            sql += " AND status=?"
            params.append(str(status))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [_decode(row) for row in self._read_all(sql, tuple(params))]

    def count_agent_inbox(self, agent_session_id: str, *, status: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM agent_inbox WHERE agent_session_id=?"
        params: list[Any] = [str(agent_session_id)]
        if status:
            sql += " AND status=?"
            params.append(str(status))
        row = self._read_one(sql, tuple(params))
        return int(row["n"]) if row else 0

    def take_pending_agent_inbox(self, agent_session_id: str, *, limit: int = 32
                                 ) -> list[dict[str, Any]]:
        """Atomically read and mark delivered the oldest pending rows (oldest first).

        A wake turn drains with this before it starts, so two wake paths can never deliver the
        same pending message twice.
        """
        sid = str(agent_session_id)
        now = time.time()

        def _write(conn) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM agent_inbox WHERE agent_session_id=? AND status='pending' "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (sid, max(1, int(limit)))).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                conn.executemany(
                    "UPDATE agent_inbox SET status='delivered', delivered_at=?, "
                    "attempts=attempts+1 WHERE id=?",
                    [(now, inbox_id) for inbox_id in ids])
            return [_decode(row) for row in rows]

        return self._execute_write(_write)

    def fail_pending_agent_inbox(self, agent_session_id: str) -> int:
        """Mark every pending row failed (terminal stop); returns how many were dropped."""
        sid = str(agent_session_id)

        def _write(conn) -> int:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_inbox WHERE agent_session_id=? "
                "AND status='pending'", (sid,)).fetchone()
            conn.execute(
                "UPDATE agent_inbox SET status='failed' WHERE agent_session_id=? "
                "AND status='pending'", (sid,))
            return int(row["n"]) if row else 0

        return self._execute_write(_write)
