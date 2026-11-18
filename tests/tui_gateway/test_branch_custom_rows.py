"""A branch child keeps its parent's custom rows (ADR-9 D6).

``_branch_source_history`` reconciles the stored display projection with live memory and keeps only
rows with visible text; a ``role:"custom"`` late-diagnostics row must survive that filter, and then
``_persist_branch`` must rebuild its ``_custom_message`` envelope from the six copied fields.
"""

from __future__ import annotations

import threading
from pathlib import Path

from agent.custom_messages import CUSTOM_STORAGE_KEY, custom_fields
from tui_gateway.methods_session_branch import _BRANCH_COPY_FIELDS, _branch_source_history


def _custom_envelope() -> dict:
    return {
        "custom_type": "lsp-late-diagnostic",
        "display": True,
        "display_only": False,
        "details": {"files": [{"path": "C:/w/a.py", "summary": "1 error(s)", "errored": True,
                               "messages": ["C:/w/a.py:1:1 [error] [ty] boom"]}]},
        "attribution": "agent",
        "legacy_role": "user",
    }


def test_branch_source_history_and_persist_keep_a_custom_row(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from tui_gateway import server

    branch_source_history = vars(server)["_branch_source_history"]
    persist_branch = vars(server)["_persist_branch"]

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    with SessionDB(home / "state.db") as db:
        db.create_session("parent", source="desktop", model="test-model")
        db.append_messages_batch("parent", [
            {"role": "user", "content": "edit a.py"},
            {
                "role": "custom",
                "content": "<system-notice>\nDiagnostics for the files you edited:\n"
                           "C:/w/a.py — 1 error(s)\nC:/w/a.py:1:1 [error] [ty] boom\n\n"
                           "Fix them if they are real, then continue.\n</system-notice>",
                **_custom_envelope(),
            },
        ])
        session = {"history_lock": threading.RLock(), "history": [], "display_history_prefix": []}
        history = branch_source_history(db, session, "parent")
        assert any(row.get("role") == "custom" for row in history), history
        persist_branch(
            db, "child", "parent", "Branch", history,
            source="desktop", cwd=str(tmp_path), profile_name="default", model="test-model",
            copy_fields=_BRANCH_COPY_FIELDS)

    with SessionDB(home / "state.db") as db:
        rows = db.get_messages("child")
    custom = [row for row in rows if row.get("role") == "custom"]
    assert len(custom) == 1, rows
    fields = custom_fields(custom[0])
    assert fields["custom_type"] == "lsp-late-diagnostic"
    assert fields["details"]["files"][0]["summary"] == "1 error(s)"
    envelope = (custom[0].get("display_metadata") or {}).get(CUSTOM_STORAGE_KEY)
    assert isinstance(envelope, dict) and envelope["attribution"] == "agent"
