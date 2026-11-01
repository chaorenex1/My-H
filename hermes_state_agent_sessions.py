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
    raw = data.get("artifact_manifest")
    if isinstance(raw, str) and raw:
        try:
            data["artifact_manifest"] = json.loads(raw)
        except ValueError:
            pass
    return data


class SessionAgentSessionsMixin:
    def begin_agent_session(
        self, *, agent_session_id: str, agent_name: str, subject: str,
        transcript_path: str, parent_session_id: str = "",
    ) -> str:
        """Insert the row before dispatch; returns the subject actually stored.

        ``subject`` is unique across agent sessions, so a collision is disambiguated here (the
        caller gets the stored value back and reports it) rather than failing the insert.
        """
        candidate = clean_subject(subject) or str(agent_session_id)
        now = time.time()

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
                "subject, transcript_path, artifact_manifest, final_response, status, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, 'running', ?, ?) "
                "ON CONFLICT(agent_session_id) DO UPDATE SET agent_name=excluded.agent_name, "
                "parent_session_id=excluded.parent_session_id, subject=excluded.subject, "
                "transcript_path=excluded.transcript_path, status='running', updated_at=excluded.updated_at",
                (str(agent_session_id), str(agent_name or ""), str(parent_session_id or ""),
                 stored, str(transcript_path or ""), now, now),
            )
            return stored

        return self._execute_write(_write)

    def finish_agent_session(
        self, agent_session_id: str, *, status: str, final_response: Optional[str] = None,
        transcript_path: Optional[str] = None, artifact_manifest: Any = None,
    ) -> None:
        """Record the settled state; called from the settling thread, never mid-run."""
        assigns = ["status=?", "updated_at=?"]
        params: list[Any] = [str(status or "unknown"), time.time()]
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

    def get_agent_session(self, agent_session_id: str) -> Optional[dict[str, Any]]:
        row = self._read_one(
            "SELECT * FROM agent_session WHERE agent_session_id=?", (str(agent_session_id),))
        return _decode(row) if row else None

    def get_agent_session_by_subject(self, subject: str) -> Optional[dict[str, Any]]:
        row = self._read_one("SELECT * FROM agent_session WHERE subject=?", (clean_subject(subject),))
        return _decode(row) if row else None
