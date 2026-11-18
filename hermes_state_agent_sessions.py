"""``agent_session``: one row per named in-process agent run (the ``agent`` tool).

The row is the identity ``agent_out`` resolves — the agent session id, its unique subject, the
JSONL transcript the session owns, and the run's final answer. It is deliberately not a second
conversation store: :mod:`agent.run_transcript` holds the history, ``final_response`` is only the
snapshot a caller can read without re-walking the file.

Write order follows the run: ``begin_agent_session`` inserts before dispatch (so the id the tool
returns is resolvable immediately) and ``finish_agent_session`` updates in the settle path, from
whichever thread settles the run.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

#: Subject width the listing surfaces can print on one row.
MAX_SUBJECT_CHARS = 120
#: Collision suffixes tried before falling back to the id (a runaway loop is worse than a suffix).
_MAX_SUBJECT_ATTEMPTS = 50


def clean_subject(value: Any) -> str:
    """One printable subject line: whitespace collapsed, length capped."""
    text = " ".join(str(value or "").split())
    return text[:MAX_SUBJECT_CHARS].strip()


def _decode(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("artifact_manifest", "metadata_json"):
        raw = data.get(key)
        if isinstance(raw, str) and raw:
            try:
                data[key] = json.loads(raw)
            except ValueError:
                pass
    return data


class SessionAgentSessionsMixin:
    def begin_agent_session(
        self, *, agent_session_id: str, agent_name: str, subject: str,
        transcript_path: str, parent_session_id: str = "", model_role: str = "",
        metadata: Any = None, short_name: str = "",
    ) -> str:
        """Insert the row before dispatch; returns the subject actually stored.

        ``subject`` is unique across agent sessions, so a collision is disambiguated here (the
        caller gets the stored value back and reports it) rather than failing the insert.
        ``short_name`` is the profile-unique address (unique index, ``''`` = unaddressed);
        the caller checks it first so a duplicate is a refused call, not a lost row.
        ``metadata`` is the rebuild spec a wake/revive needs (definition source, toolsets, flags,
        model selector); it never carries credentials.
        """
        candidate = clean_subject(subject) or str(agent_session_id)
        now = time.time()
        spec = json.dumps(metadata, ensure_ascii=False, default=str) if metadata else None

        def _write(conn) -> str:
            stored = candidate
            for attempt in range(1, _MAX_SUBJECT_ATTEMPTS + 1):
                taken = conn.execute(
                    "SELECT 1 FROM agent_session WHERE subject=?", (stored,)).fetchone()
                if taken is None:
                    break
                stored = f"{candidate} ({attempt + 1})"
            # Conflict target is the id only: a subject race must raise rather than let
            # REPLACE delete the row that legitimately owns that subject.
            conn.execute(
                "INSERT INTO agent_session (agent_session_id, agent_name, parent_session_id, "
                "subject, short_name, transcript_path, artifact_manifest, final_response, status, "
                "last_activity_at, parked_at, model_role, metadata_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 'running', ?, NULL, ?, ?, ?, ?) "
                "ON CONFLICT(agent_session_id) DO UPDATE SET agent_name=excluded.agent_name, "
                "parent_session_id=excluded.parent_session_id, subject=excluded.subject, "
                "short_name=excluded.short_name, "
                "transcript_path=excluded.transcript_path, status='running', "
                "last_activity_at=excluded.last_activity_at, parked_at=NULL, "
                "model_role=excluded.model_role, metadata_json=excluded.metadata_json, "
                "updated_at=excluded.updated_at",
                (str(agent_session_id), str(agent_name or ""), str(parent_session_id or ""),
                 stored, str(short_name or ""), str(transcript_path or ""), now,
                 str(model_role or ""), spec, now, now),
            )
            return stored

        return self._execute_write(_write)

    def set_agent_session_transcript_path(self, agent_session_id: str, path: str) -> None:
        """Record the bundle transcript the session actually writes (ADR-16 D4).

        Called once, right after the row is inserted and the unique (possibly suffixed)
        subject is known — the topic in the file name derives from that stored subject.
        """
        self._execute_write(lambda conn: conn.execute(
            "UPDATE agent_session SET transcript_path=?, updated_at=? WHERE agent_session_id=?",
            (str(path or ""), time.time(), str(agent_session_id)),
        ))

    def set_agent_session_title(self, agent_session_id: str, title: str) -> bool:
        """Write the task label once (ADR-21 D7): ``''`` -> *title*, never *title* -> *title'*.

        The single write path is ``UPDATE … SET title=? WHERE agent_session_id=? AND title=''``;
        ``rowcount == 0`` means the row was already labelled (or vanished) and the label is kept.
        A live run already holding the same label (case-insensitive) makes this write take the
        next `` 2`` / `` 3`` suffix — decided and applied inside this one write transaction, so
        two concurrent labellers can never both settle on the same address. An empty label is
        refused before SQL: ``title=''`` is the unlabelled sentinel, not a name.
        """
        label = " ".join(str(title or "").split())
        if not label:
            return False

        def _write(conn) -> bool:
            row = conn.execute(
                "SELECT title FROM agent_session WHERE agent_session_id=?",
                (str(agent_session_id),),
            ).fetchone()
            if row is None or str(row[0] or "").strip():
                return False
            candidate, suffix = label, 1
            while conn.execute(
                "SELECT 1 FROM agent_session WHERE agent_session_id<>? AND title<>'' "
                "AND lower(title)=lower(?) AND status IN ('running', 'parked') LIMIT 1",
                (str(agent_session_id), candidate),
            ).fetchone() is not None:
                suffix += 1
                candidate = f"{label} {suffix}"
            cursor = conn.execute(
                "UPDATE agent_session SET title=? WHERE agent_session_id=? AND title=''",
                (candidate, str(agent_session_id)),
            )
            return cursor.rowcount > 0

        return bool(self._execute_write(_write))

    def get_agent_sessions_by_title(self, title: str) -> list[dict[str, Any]]:
        """Every session addressed by *title*, exact match first, then a case-insensitive pass.

        ``agent_message.to`` resolves labels by exact ``title=?`` (the index), and only when that
        misses does it compare non-empty labels casefolded in Python — the run count is bounded
        by the profile, so no second index is worth keeping (ADR-21 D7). More than one row is
        the caller's ambiguity to report, never a reason to guess the first.
        """
        label = " ".join(str(title or "").split())
        if not label:
            return []
        rows = self._read_all("SELECT * FROM agent_session WHERE title=?", (label,))
        if rows:
            return [_decode(row) for row in rows]
        folded = label.casefold()
        return [
            _decode(row)
            for row in self._read_all("SELECT * FROM agent_session WHERE title<>''")
            if str(row["title"] or "").casefold() == folded
        ]

    def finish_agent_session(
        self, agent_session_id: str, *, status: str, final_response: Optional[str] = None,
        transcript_path: Optional[str] = None, artifact_manifest: Any = None,
    ) -> None:
        """Record the settled state; called from the settling thread, never mid-run."""
        now = time.time()
        assigns = ["status=?", "updated_at=?", "last_activity_at=?", "parked_at=NULL"]
        params: list[Any] = [str(status or "unknown"), now, now]
        if final_response is not None:
            assigns.append("final_response=?")
            params.append(str(final_response))
        if transcript_path is not None:
            assigns.append("transcript_path=?")
            params.append(str(transcript_path))
        if artifact_manifest is not None:
            assigns.append("artifact_manifest=?")
            params.append(json.dumps(artifact_manifest, ensure_ascii=False, default=str))
        params.append(str(agent_session_id))

        def _write(conn) -> None:
            conn.execute(
                f"UPDATE agent_session SET {', '.join(assigns)} WHERE agent_session_id=?",
                tuple(params),
            )

        self._execute_write(_write)

    def park_agent_session(self, agent_session_id: str, *, parked_at: Optional[float] = None
                           ) -> None:
        """Mark a session parked: the live agent was closed, the row/inbox/transcript stay."""
        now = time.time()

        def _write(conn) -> None:
            conn.execute(
                "UPDATE agent_session SET status='parked', parked_at=?, last_activity_at=?, "
                "updated_at=? WHERE agent_session_id=?",
                (float(parked_at or now), now, now, str(agent_session_id)),
            )

        self._execute_write(_write)

    def revive_agent_session(self, agent_session_id: str) -> None:
        """Clear the parked marker when a message/resume starts a fresh run for the session."""
        now = time.time()

        def _write(conn) -> None:
            conn.execute(
                "UPDATE agent_session SET status='running', parked_at=NULL, "
                "last_activity_at=?, updated_at=? WHERE agent_session_id=?",
                (now, now, str(agent_session_id)),
            )

        self._execute_write(_write)

    def touch_agent_session(self, agent_session_id: str, *, status: Optional[str] = None) -> None:
        """Refresh the idle clock (and optionally the status) without touching the answer."""
        now = time.time()
        assigns = ["last_activity_at=?", "updated_at=?"]
        params: list[Any] = [now, now]
        if status:
            assigns.append("status=?")
            params.append(str(status))
        params.append(str(agent_session_id))

        def _write(conn) -> None:
            conn.execute(
                f"UPDATE agent_session SET {', '.join(assigns)} WHERE agent_session_id=?",
                tuple(params),
            )

        self._execute_write(_write)

    def cancel_agent_session(self, agent_session_id: str, *, reason: str = "") -> None:
        """Terminal stop: a live or parked session the operator ended."""
        now = time.time()
        note = f"cancelled: {reason}" if reason else "cancelled"

        def _write(conn) -> None:
            conn.execute(
                "UPDATE agent_session SET status='cancelled', final_response=COALESCE("
                "final_response, ?), last_activity_at=?, updated_at=? WHERE agent_session_id=?",
                (note, now, now, str(agent_session_id)),
            )

        self._execute_write(_write)

    def list_agent_sessions(self, *, status: Optional[str] = None,
                            parent_session_id: Optional[str] = None,
                            limit: int = 50) -> list[dict[str, Any]]:
        """Newest-first sessions, optionally one status and/or one dispatching conversation."""
        sql = "SELECT * FROM agent_session WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(str(status))
        if parent_session_id is not None:
            sql += " AND parent_session_id=?"
            params.append(str(parent_session_id))
        sql += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [_decode(row) for row in self._read_all(sql, tuple(params))]

    def count_agent_sessions(self, *, status: Optional[str] = None,
                             parent_session_id: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM agent_session WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(str(status))
        if parent_session_id is not None:
            sql += " AND parent_session_id=?"
            params.append(str(parent_session_id))
        row = self._read_one(sql, tuple(params))
        return int(row["n"]) if row else 0

    def get_agent_session(self, agent_session_id: str) -> Optional[dict[str, Any]]:
        row = self._read_one(
            "SELECT * FROM agent_session WHERE agent_session_id=?", (str(agent_session_id),))
        return _decode(row) if row else None

    def get_agent_session_by_subject(self, subject: str) -> Optional[dict[str, Any]]:
        row = self._read_one("SELECT * FROM agent_session WHERE subject=?", (clean_subject(subject),))
        return _decode(row) if row else None

    def get_agent_session_by_name(self, name: str) -> Optional[dict[str, Any]]:
        """The session addressed by its profile-unique short name; ``''`` never matches."""
        short_name = str(name or "").strip()
        if not short_name:
            return None
        row = self._read_one(
            "SELECT * FROM agent_session WHERE short_name=?", (short_name,))
        return _decode(row) if row else None
