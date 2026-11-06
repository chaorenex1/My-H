"""Session-bundle metadata for the live tool-output archive (ADR-16 D2/D3).

Two small reads/writes the tool executor needs per tool call, kept out of the
``hermes_state_sessions`` facade: the row inputs a bundle path is derived from, and the
monotonic per-session tool-output ordinal used as the archive file name.

ADR-16 P3 adds the cold-mirror side of the same row: candidate listing, the recorded
``mirror_path`` / ``mirrored_at`` / ``offloaded`` state, message pruning and restore.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

from hermes_state_messages import _parse_tool_calls, _tool_calls_count


class SessionBundlesMixin:
    def get_session_bundle_meta(self, session_id: str) -> Optional[dict[str, Any]]:
        """Bundle derivation inputs for *session_id* (ADR-16 D2): the row's own ``started_at``
        and ``cwd``. Not ``get_session``: no prompt JOIN, no read-flush, so the per-tool-call
        archive path can call it without touching the transcript."""
        if not session_id:
            return None
        row = self._read_one("SELECT started_at, cwd FROM sessions WHERE id = ?", (session_id,))
        return dict(row) if row else None

    def next_tool_output_seq(self, session_id: str) -> Optional[int]:
        """Allocate the next 1-based tool-output step for *session_id* (ADR-16 D3).

        One ``UPDATE ... RETURNING`` is the allocation: concurrent tool commits get distinct
        ordinals, and the value survives resume. ``None`` when the session row is gone.
        """
        if not session_id:
            return None

        def _do(conn):
            row = conn.execute(
                "UPDATE sessions SET tool_output_seq = tool_output_seq + 1 "
                "WHERE id = ? RETURNING tool_output_seq",
                (session_id,),
            ).fetchone()
            return int(row[0]) if row else None

        return self._execute_write(_do)

    # ── ADR-16 P3: cold mirror state ─────────────────────────────────────

    def list_mirror_candidates(
        self,
        *,
        session_ids: Optional[Iterable[str]] = None,
        started_after: Optional[float] = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """Physical session rows the mirror can export (ADR-16 D8), oldest first.

        ``offloaded`` rows are returned too: the caller reports them as already mirrored
        instead of silently re-exporting an empty transcript over the cold copy.
        """
        clauses: list[str] = []
        params: list[Any] = []
        ids = [str(sid) for sid in (session_ids or []) if sid]
        if ids:
            clauses.append(f"id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        if started_after is not None:
            clauses.append("started_at >= ?")
            params.append(float(started_after))
        if not include_archived:
            clauses.append("COALESCE(archived, 0) = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._read_all(
            "SELECT id, source, title, model, cwd, started_at, ended_at, message_count, "
            "archived, offloaded, mirror_path, mirrored_at, "
            "(SELECT MAX(m.timestamp) FROM messages m "
            " WHERE m.session_id = sessions.id AND m.role = 'user') AS last_user_at "
            f"FROM sessions {where} "
            "ORDER BY started_at ASC, id ASC",
            params,
        )
        return [dict(row) for row in rows]

    def clear_session_offloaded(self, session_id: str) -> bool:
        """Clear the restore-required flag without touching message rows."""
        if not session_id:
            return False

        def _do(conn):
            cur = conn.execute(
                "UPDATE sessions SET offloaded = 0 WHERE id = ? AND COALESCE(offloaded, 0) <> 0",
                (str(session_id),),
            )
            return cur.rowcount > 0

        return bool(self._execute_write(_do))

    def has_compression_continuation(self, session_id: str) -> bool:
        """Whether a compression rotation continues *session_id* (ADR-16 D8).

        A compression child loads its ancestor transcript, so pruning an ancestor while the
        continuation remains would silently cut the child's pre-compression history. Branch
        children copy their history and do not require the parent.
        """
        if not session_id:
            return False
        from hermes_state_maintenance import _CONTINUATION_EDGE_SQL

        row = self._read_one(
            "SELECT 1 FROM sessions c JOIN sessions p ON p.id = c.parent_session_id "
            f"WHERE c.parent_session_id = ? AND {_CONTINUATION_EDGE_SQL} LIMIT 1",
            (str(session_id),),
        )
        return row is not None

    def mark_session_mirrored(
        self, session_id: str, mirror_path: str, mirrored_at: Optional[float] = None
    ) -> bool:
        """Record the authoritative bundle path after a verified mirror write (ADR-16 D2)."""
        if not session_id or not mirror_path:
            return False
        stamp = time.time() if mirrored_at is None else float(mirrored_at)

        def _do(conn):
            cur = conn.execute(
                "UPDATE sessions SET mirror_path = ?, mirrored_at = ? WHERE id = ?",
                (str(mirror_path), stamp, str(session_id)),
            )
            return cur.rowcount > 0

        return bool(self._execute_write(_do))

    def offload_session_messages(
        self,
        session_id: str,
        *,
        mirror_path: str,
        mirrored_at: Optional[float] = None,
        expected_message_count: Optional[int] = None,
    ) -> dict[str, Any]:
        """Prune a mirrored session's message rows and mark it ``offloaded`` (ADR-16 D8).

        Refuses an open session, a live turn/compression lease, or a row count that no
        longer matches the verified manifest. ``sessions`` metadata survives; the cold
        bundle is the only remaining message copy, so every guard fails closed.
        """
        sid = str(session_id or "")
        if not sid or not mirror_path:
            return {"ok": False, "error": "session_id and mirror_path are required"}
        stamp = time.time() if mirrored_at is None else float(mirrored_at)

        def _do(conn):
            row = conn.execute(
                "SELECT id, ended_at, COALESCE(offloaded, 0) AS offloaded "
                "FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": "not_found"}
            if row["offloaded"]:
                return {"ok": False, "error": "already_offloaded"}
            if row["ended_at"] is None:
                return {"ok": False, "error": "open"}
            if self._write_guards_reject(conn, sid, allow_closed_compression_parent=True):
                return {"ok": False, "error": "live_lease"}
            count = int(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)
            ).fetchone()[0])
            if expected_message_count is not None and count != int(expected_message_count):
                return {
                    "ok": False,
                    "error": "message_count_mismatch",
                    "expected": int(expected_message_count),
                    "actual": count,
                }
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.execute(
                "UPDATE sessions SET offloaded = 1, mirror_path = ?, mirrored_at = ? WHERE id = ?",
                (str(mirror_path), stamp, sid),
            )
            return {"ok": True, "deleted": count}

        return self._execute_write(_do)

    def restore_session_messages(
        self, session_id: str, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Re-insert a cold mirror's rows and clear ``offloaded`` (ADR-16 D8).

        Restores into the existing metadata row, so gateway routing / profile ownership /
        titles survive. Refuses when the row has live message rows already (a half-restore
        is never doubled) or is under a live lease.
        """
        sid = str(session_id or "")
        if not sid:
            return {"ok": False, "error": "session_id is required"}
        if not isinstance(messages, list):
            return {"ok": False, "error": "messages must be a list"}
        try:
            normalized = [self._normalize_import_message(msg, index)
                          for index, msg in enumerate(messages)]
        except ValueError as exc:
            return {"ok": False, "error": "invalid_payload", "detail": str(exc)}

        def _do(conn):
            row = conn.execute("SELECT id FROM sessions WHERE id = ?", (sid,)).fetchone()
            if row is None:
                return {"ok": False, "error": "not_found"}
            if self._write_guards_reject(conn, sid, allow_closed_compression_parent=True):
                return {"ok": False, "error": "live_lease"}
            existing = int(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)
            ).fetchone()[0])
            if existing:
                return {"ok": False, "error": "not_empty", "existing": existing}

            def _flag(msg: dict[str, Any], name: str, default: int) -> int:
                value = msg.get(name)
                if value is None:
                    return default
                try:
                    return 1 if int(value) else 0
                except (TypeError, ValueError):
                    return default

            live = [msg for msg in normalized if _flag(msg, "active", 1)]
            archived = [msg for msg in normalized if not _flag(msg, "active", 1)]
            inserted, _ = self._insert_message_rows(
                conn, sid, normalized, prune_checkpoints=False
            )
            for msg in archived:
                conn.execute(
                    "UPDATE messages SET active = 0, compacted = ? WHERE id = ?",
                    (_flag(msg, "compacted", 0), msg["_row_id"]),
                )
            if archived:
                self._prune_shadowed_checkpoints(conn, sid, live)
            tool_total = sum(_tool_calls_count(_parse_tool_calls(msg.get("tool_calls")))
                             for msg in live)
            conn.execute(
                "UPDATE sessions SET message_count = ?, tool_call_count = ?, offloaded = 0 "
                "WHERE id = ?",
                (len(live), tool_total, sid),
            )
            return {"ok": True, "inserted": inserted, "live": len(live),
                    "archived": len(archived)}

        return self._execute_write(_do)
