"""ADR-16 P4: ``session.transcript.list`` / ``session.transcript.read``.

The picker must learn which sessions exist as files without reading the host filesystem, and it
must never show a subagent or advisor file as a human session. These pin the wire contract:
listing is grouped by project, metadata comes from the mirror manifest, paging is by message index,
and a client-supplied path is never opened.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ``append_message`` lazily imports ``agent.conversation_loop`` for display identity; preload it
# during collection (see tests/hermes_cli/test_sessions_mirror.py).
import agent.conversation_loop
import hermes_session_mirror as mirror
import hermes_session_paths as session_paths
import tui_gateway.server as srv
from hermes_state import SessionDB


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


@pytest.fixture
def db(tmp_path, home):
    database = SessionDB(home / "state.db")
    try:
        yield database
    finally:
        database.close()


def _seed(db: SessionDB, sid: str, *, cwd: str = "", pairs: int = 1) -> None:
    db.create_session(sid, source="cli", cwd=cwd or None)
    for index in range(pairs):
        db.append_message(sid, "user", f"question {index}")
        db.append_message(sid, "assistant", f"answer {index}")
    db.end_session(sid, "exit")


def _mirror(db: SessionDB, home: Path, sid: str) -> dict:
    rows = db.list_mirror_candidates(session_ids=[sid], include_archived=True)
    report = mirror.mirror_sessions(db, rows, home=home)["reports"][0]
    assert report["ok"], report
    return mirror.read_manifest(home)[sid]


def _call(name: str, params: dict) -> dict:
    return srv.handle_request({"id": "1", "method": name, "params": params})


def test_list_returns_main_bundles_only_grouped_by_project(db, home, tmp_path):
    sid = "20260101_000000_listed"
    _seed(db, sid, cwd=str(tmp_path), pairs=2)
    entry = _mirror(db, home, sid)
    transcript = Path(entry["path"])

    # Neighbours the picker must never treat as sessions: the root routing index, a debug dump,
    # and the bundle's own subagent / advisor namespaces.
    (session_paths.sessions_root(home) / "sessions.json").write_text("{}\n", encoding="utf-8")
    (session_paths.sessions_root(home) / "request_dump_1.json").write_text("{}\n", encoding="utf-8")
    bundle_dir = transcript.with_suffix("")
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "researcher.jsonl").write_text("{}\n", encoding="utf-8")
    (bundle_dir / "_advisor.jsonl").write_text("{}\n", encoding="utf-8")

    envelope = _call("session.transcript.list", {})
    assert "error" not in envelope, envelope
    projects = envelope["result"]["projects"]
    assert [row["session_id"] for project in projects for row in project["sessions"]] == [sid]
    assert projects[0]["project"] == transcript.parent.name
    row = projects[0]["sessions"][0]
    assert row["project"] == transcript.parent.name
    assert row["path"] == str(transcript)
    assert row["message_count"] == 4
    assert row["offloaded"] is False
    assert envelope["result"]["total"] == 1


def test_read_pages_by_message_index(db, home, tmp_path):
    sid = "20260101_000000_paged"
    _seed(db, sid, cwd=str(tmp_path), pairs=2)
    _mirror(db, home, sid)

    first = _call("session.transcript.read", {"session_id": sid, "offset": 0, "limit": 1})
    assert "error" not in first, first
    assert first["result"]["session_id"] == sid
    assert first["result"]["session"]["id"] == sid
    assert [row["content"] for row in first["result"]["messages"]] == ["question 0"]
    assert (first["result"]["total"], first["result"]["offset"]) == (4, 0)
    assert first["result"]["has_more"] is True

    last = _call("session.transcript.read", {"session_id": sid, "offset": 3, "limit": 10})
    assert "error" not in last, last
    assert [row["content"] for row in last["result"]["messages"]] == ["answer 1"]
    assert last["result"]["has_more"] is False


def test_read_refuses_an_unmirrored_session(db, home, tmp_path):
    sid = "20260101_000000_unmirrored"
    _seed(db, sid, cwd=str(tmp_path))
    envelope = _call("session.transcript.read", {"session_id": sid})
    assert envelope["error"]["code"] == 4001
    assert "not mirrored" in envelope["error"]["message"]


def test_read_never_accepts_a_client_path(home):
    envelope = _call("session.transcript.read", {"session_id": "x", "path": "C:/secret.txt"})
    assert envelope["error"]["code"] == 4000


def test_read_falls_back_to_the_rows_recorded_mirror_path(db, home, tmp_path):
    """A lost manifest must not make a mirrored session unreadable: the row keeps the path."""
    sid = "20260101_000000_rowpath"
    _seed(db, sid, cwd=str(tmp_path))
    entry = _mirror(db, home, sid)
    mirror.manifest_path(home).unlink()

    envelope = _call("session.transcript.read", {"session_id": sid})
    assert "error" not in envelope, envelope
    assert envelope["result"]["path"] == entry["path"]
    assert envelope["result"]["total"] == 2


def test_read_never_consults_another_profiles_store(db, home, tmp_path, monkeypatch):
    """A session-bound call must resolve its own home, not the launch store's mirror_path."""
    sid = "20260101_000000_elsewhere"
    _seed(db, sid, cwd=str(tmp_path))
    _mirror(db, home, sid)
    other = tmp_path / "profiles" / "code"
    other.mkdir(parents=True)
    monkeypatch.setattr(srv, "_profile_home", lambda name: other if name == "code" else None)
    monkeypatch.setattr(
        "tui_gateway.launch_profile_policy.activate_multi_profile_hosting", lambda: None
    )

    envelope = _call("session.transcript.read", {"session_id": sid, "profile": "code"})
    assert envelope["error"]["code"] == 4001
    assert "not mirrored" in envelope["error"]["message"]


def test_list_reports_an_offloaded_bundle(db, home, tmp_path):
    sid = "20260101_000000_offloaded"
    _seed(db, sid, cwd=str(tmp_path))
    _mirror(db, home, sid)
    rows = db.list_mirror_candidates(session_ids=[sid], include_archived=True)
    mirror.mirror_sessions(db, rows, home=home, prune_source=True, older_than_days=0)

    envelope = _call("session.transcript.list", {})
    row = envelope["result"]["projects"][0]["sessions"][0]
    assert row["session_id"] == sid
    assert row["offloaded"] is True


def test_list_reads_the_requested_profile_home(db, home, tmp_path, monkeypatch):
    """A dashboard backend serves several homes: ``profile`` must scan THAT profile's sessions/."""
    sid = "20260101_000000_profiled"
    _seed(db, sid, cwd=str(tmp_path))
    _mirror(db, home, sid)
    other = tmp_path / "profiles" / "code"
    other.mkdir(parents=True)
    monkeypatch.setattr(srv, "_profile_home", lambda name: other if name == "code" else None)
    monkeypatch.setattr(
        "tui_gateway.launch_profile_policy.activate_multi_profile_hosting", lambda: None
    )

    assert _call("session.transcript.list", {"profile": "code"})["result"]["projects"] == []
    launch = _call("session.transcript.list", {})
    assert [row["session_id"] for row in launch["result"]["projects"][0]["sessions"]] == [sid]
