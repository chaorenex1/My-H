"""ADR-16 P3: cold mirror, offload/prune, restore, and the read-side markers.

These pin the user-visible contract: a mirror is a byte-stable cold copy, prune removes only
verified copies, restore is a round trip, and an offloaded row is never treated as an empty
session (or as garbage by automatic maintenance).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_session_mirror as mirror
from hermes_session_mirror import MirrorError
from hermes_state import SessionDB

# ``append_message`` lazily imports ``agent.conversation_loop`` for display identity.
# Preload it during collection: its import-time PM activation reads the checkout's real
# home, which the per-test real-home guard (installed in a fixture) would refuse.
import agent.conversation_loop


def _db(tmp_path: Path, home: Path | None = None) -> SessionDB:
    return SessionDB(db_path=(home or tmp_path) / "state.db")


def _seed(db: SessionDB, sid: str, *, cwd: str = "", started_at: float | None = None) -> None:
    db.create_session(sid, source="cli", cwd=cwd or None)
    db.append_message(sid, "user", "hello")
    db.append_message(sid, "assistant", "world")
    db.end_session(sid, "exit")
    if started_at is not None:
        db._write_sql("UPDATE sessions SET started_at = ? WHERE id = ?", (started_at, sid))
        db._write_sql("UPDATE messages SET timestamp = ? WHERE session_id = ?", (started_at, sid))


def _candidates(db: SessionDB, sid: str) -> list[dict]:
    return db.list_mirror_candidates(session_ids=[sid], include_archived=True)


def _session(db: SessionDB, sid: str) -> dict:
    row = db.get_session(sid)
    assert row is not None
    return row


def test_mirror_prune_restore_round_trip(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    sid = "20260101_000000_roundtrip"
    _seed(db, sid, cwd=str(tmp_path))
    before = [(m["role"], m["content"]) for m in db.get_messages(sid, include_inactive=True)]

    mirrored = mirror.mirror_sessions(db, _candidates(db, sid), home=home, verify=True)
    assert mirrored["reports"][0]["ok"]
    entry = mirror.read_manifest(home)[sid]
    transcript = Path(entry["path"])
    assert transcript.is_file()
    assert transcript.parent.parent.name == "sessions"
    assert entry["message_count"] == 2

    pruned = mirror.mirror_sessions(
        db, _candidates(db, sid), home=home, prune_source=True, older_than_days=0
    )
    assert pruned["pruned"] and pruned["pruned"][0]["deleted"] == 2
    assert db.get_messages(sid, include_inactive=True) == []
    assert _session(db, sid)["offloaded"] == 1
    assert mirror.read_manifest(home)[sid]["offloaded"] == 1

    restored = mirror.restore_session(db, sid, home=home)
    assert restored["status"] == "restored"
    assert [(m["role"], m["content"]) for m in db.get_messages(sid, include_inactive=True)] == before
    assert _session(db, sid)["offloaded"] == 0
    assert mirror.read_manifest(home)[sid]["offloaded"] == 0


def test_mirror_is_idempotent_and_dry_run_writes_nothing(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    sid = "20260101_000000_idem"
    _seed(db, sid, cwd=str(tmp_path))

    first = mirror.mirror_sessions(db, _candidates(db, sid), home=home)
    path = Path(first["reports"][0]["path"])
    payload = path.read_bytes()
    second = mirror.mirror_sessions(db, _candidates(db, sid), home=home)
    assert second["reports"][0]["status"] == "unchanged"
    assert path.read_bytes() == payload
    assert len(mirror.read_manifest(home)) == 1

    other = "20260101_000000_dryrun"
    _seed(db, other, cwd=str(tmp_path))
    dry = mirror.mirror_sessions(db, _candidates(db, other), home=home, dry_run=True)
    assert dry["reports"][0]["dry_run"] is True
    assert other not in mirror.read_manifest(home)
    assert not Path(dry["reports"][0]["path"]).exists()


def test_rotate_leaves_parent_bytes_untouched_and_rewrite_replaces_one_bundle(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    parent, child = "20260101_000000_parent", "20260101_010000_child"
    _seed(db, parent, cwd=str(tmp_path))
    db.create_session(child, source="cli", cwd=str(tmp_path), parent_session_id=parent)
    db.append_message(child, "user", "after compression")
    db.end_session(child, "exit")

    result = mirror.mirror_sessions(db, [*_candidates(db, parent), *_candidates(db, child)], home=home)
    paths = {r["session_id"]: Path(r["path"]) for r in result["reports"]}
    parent_bytes = paths[parent].read_bytes()

    db.append_message(child, "assistant", "new reply")
    mirror.mirror_sessions(db, _candidates(db, child), home=home)
    assert paths[parent].read_bytes() == parent_bytes, "a compression child must not rewrite its parent"
    assert paths[parent] != paths[child]

    # /undo-style in-place rewrite: same bundle, no duplicated rows.
    db._write_sql("UPDATE messages SET content = 'edited' WHERE session_id = ? AND role = 'user'", (child,))
    before_lines = paths[child].read_text(encoding="utf-8").splitlines()
    mirror.mirror_sessions(db, _candidates(db, child), home=home)
    _, messages = mirror.read_bundle(paths[child])
    assert len(messages) == len(before_lines) - 1
    assert messages[0]["content"] == "edited"


def test_prune_age_default_is_180_days_and_explicit_id_needs_older_than_zero(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    old, recent = "20250101_000000_old", "20250601_000000_recent"
    now = time.time()
    _seed(db, old, cwd=str(tmp_path), started_at=now - 181 * 86400)
    _seed(db, recent, cwd=str(tmp_path), started_at=now - 179 * 86400)

    rows = db.list_mirror_candidates(session_ids=[old, recent], include_archived=True)
    result = mirror.mirror_sessions(db, rows, home=home, prune_source=True)
    assert [p["session_id"] for p in result["pruned"]] == [old]
    assert _session(db, old)["offloaded"] == 1
    assert _session(db, recent)["offloaded"] == 0
    assert _session(db, recent)["message_count"] == 2

    # An explicit id inside the window is still kept by the age gate.
    explicit = mirror.mirror_sessions(
        db, _candidates(db, recent), home=home, prune_source=True
    )
    assert explicit["pruned"] == []
    assert _session(db, recent)["offloaded"] == 0
    forced = mirror.mirror_sessions(
        db, _candidates(db, recent), home=home, prune_source=True, older_than_days=0
    )
    assert [p["session_id"] for p in forced["pruned"]] == [recent]


def test_open_and_leased_sessions_are_not_pruned(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    open_sid, leased_sid = "20260101_000000_open", "20260101_000000_leased"
    db.create_session(open_sid, source="cli", cwd=str(tmp_path))
    db.append_message(open_sid, "user", "still open")
    _seed(db, leased_sid, cwd=str(tmp_path))
    db.acquire_session_turn_lease(leased_sid, "holder", ttl_seconds=300)

    result = mirror.mirror_sessions(
        db, db.list_mirror_candidates(session_ids=[open_sid, leased_sid], include_archived=True),
        home=home, prune_source=True, older_than_days=0,
    )
    skipped = {item["session_id"]: item["reason"] for item in result["skipped_prune"]}
    assert skipped[open_sid] == "open"
    assert skipped[leased_sid] == "live_lease"
    assert _session(db, open_sid)["offloaded"] == 0
    assert _session(db, leased_sid)["offloaded"] == 0


def test_restore_refuses_missing_or_corrupt_mirror(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    sid = "20260101_000000_corrupt"
    _seed(db, sid, cwd=str(tmp_path))
    mirror.mirror_sessions(db, _candidates(db, sid), home=home, prune_source=True, older_than_days=0)
    path = Path(mirror.read_manifest(home)[sid]["path"])

    path.write_text("not the mirrored bytes\n", encoding="utf-8")
    with pytest.raises(MirrorError, match="checksum"):
        mirror.restore_session(db, sid, home=home)
    assert db.get_messages(sid, include_inactive=True) == [], "a failed restore must not empty or fabricate rows"

    path.unlink()
    with pytest.raises(MirrorError, match="missing"):
        mirror.restore_session(db, sid, home=home)


def test_offloaded_rows_are_exempt_from_automatic_and_empty_deletion(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    sid = "20260101_000000_keepme"
    db.create_session(sid, source="tui", cwd=str(tmp_path))
    db.end_session(sid, "exit")
    db._write_sql("UPDATE sessions SET started_at = ? WHERE id = ?", (time.time() - 200 * 86400, sid))
    mirror.mirror_sessions(db, _candidates(db, sid), home=home, prune_source=True, older_than_days=0)
    assert _session(db, sid)["offloaded"] == 1

    db.maybe_auto_prune_and_vacuum(retention_days=90, min_interval_hours=0, vacuum=False)
    assert db.get_session(sid) is not None
    assert db.delete_empty_sessions() == 0
    assert db.get_session(sid) is not None


def test_session_list_marks_restore_required(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    sid = "20260101_000000_listed"
    _seed(db, sid, cwd=str(tmp_path))
    mirror.mirror_sessions(db, _candidates(db, sid), home=home, prune_source=True, older_than_days=0)

    from tui_gateway.methods_session import _session_row_summary

    row = db.list_sessions_rich(limit=10, include_archived=True)[0]
    summary = _session_row_summary(row)
    assert summary["restore_required"] is True
    assert summary["archived"] is False


def test_resume_restore_helper_refreshes_row_and_refuses_damage(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    sid = "20260101_000000_resume"
    _seed(db, sid, cwd=str(tmp_path))
    mirror.mirror_sessions(db, _candidates(db, sid), home=home, prune_source=True, older_than_days=0)

    from tui_gateway.methods_session import _resume_restore_offloaded

    ctx = SimpleNamespace(found=_session(db, sid), target=sid, db=db, rid="r")
    assert _resume_restore_offloaded(ctx) is None  # type: ignore[arg-type]
    assert ctx.found["offloaded"] == 0
    assert len(db.get_messages(sid)) == 2

    # Damage the mirror of a second session: resume must return an error, not an empty session.
    other = "20260101_000000_resume_bad"
    _seed(db, other, cwd=str(tmp_path))
    mirror.mirror_sessions(db, _candidates(db, other), home=home, prune_source=True, older_than_days=0)
    Path(mirror.read_manifest(home)[other]["path"]).unlink()
    bad = SimpleNamespace(found=_session(db, other), target=other, db=db, rid="r")
    error = _resume_restore_offloaded(bad)  # type: ignore[arg-type]
    assert error and "restore failed" in error


def test_update_paths_points_agent_session_rows_at_the_bundle(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    sid = "20260101_000000_paths"
    _seed(db, sid, cwd=str(tmp_path))
    db.begin_agent_session(
        agent_session_id="agent-1", agent_name="agent", subject="topic one",
        transcript_path=str(tmp_path / "legacy.jsonl"), parent_session_id=sid,
    )
    first = mirror.mirror_sessions(db, _candidates(db, sid), home=home)
    bundle = Path(first["reports"][0]["bundle_dir"])
    bundle.mkdir(parents=True, exist_ok=True)
    moved = bundle / "topic_one.jsonl"
    moved.write_text("{}\n", encoding="utf-8")

    mirror.mirror_sessions(db, _candidates(db, sid), home=home, update_paths=True)
    assert Path(db.get_agent_session("agent-1")["transcript_path"]) == moved


def test_prune_keeps_an_ancestor_whose_continuation_is_still_readable(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    parent, child = "20250101_000000_ancestor", "20260102_000000_continuation"
    now = time.time()
    _seed(db, parent, cwd=str(tmp_path), started_at=now - 200 * 86400)
    db.reopen_session(parent)
    db.end_session(parent, "compression")
    db.create_session(child, source="cli", cwd=str(tmp_path), parent_session_id=parent)
    db.append_message(child, "user", "continued")
    db.end_session(child, "exit")

    result = mirror.mirror_sessions(
        db, db.list_mirror_candidates(session_ids=[parent, child], include_archived=True),
        home=home, prune_source=True,
    )
    reasons = {item["session_id"]: item["reason"] for item in result["skipped_prune"]}
    assert reasons[parent] == "has_continuation"
    assert _session(db, parent)["offloaded"] == 0
    assert len(db.get_messages(parent)) == 2, "resuming the child must still load its ancestor history"

    forced = mirror.mirror_sessions(
        db, _candidates(db, child), home=home, prune_source=True, older_than_days=0
    )
    assert [item["session_id"] for item in forced["pruned"]] == [child]
    assert _session(db, parent)["offloaded"] == 0


def test_tool_artifact_backfill_numbers_only_archived_tools(tmp_path):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    sid = "20260101_000000_steps"
    db.create_session(sid, source="cli", cwd=str(tmp_path))
    db.append_message(sid, "user", "run things")
    db.append_message(sid, "assistant", "calling", tool_calls=[
        {"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        {"id": "t2", "type": "function", "function": {"name": "terminal", "arguments": "{}"}},
    ])
    db.append_message(sid, "tool", "excluded output", tool_name="read_file", tool_call_id="t1")
    db.append_message(sid, "tool", "kept output", tool_name="terminal", tool_call_id="t2")
    db.end_session(sid, "exit")

    report = mirror.mirror_sessions(db, _candidates(db, sid), home=home)
    bundle = Path(report["reports"][0]["bundle_dir"])
    logs = sorted(p.name for p in bundle.glob("*.log"))
    assert logs == ["1.terminal.log"], "an excluded tool must not advance the live-step counter"
    assert (bundle / "1.terminal.log").read_text(encoding="utf-8") == "kept output"


def test_continue_skips_offloaded_sessions(tmp_path, monkeypatch):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    newest, older = "20260102_000000_cold", "20260101_000000_warm"
    _seed(db, newest, cwd=str(tmp_path))
    _seed(db, older, cwd=str(tmp_path))
    db._write_sql("UPDATE sessions SET started_at = started_at + 60 WHERE id = ?", (newest,))
    mirror.mirror_sessions(db, _candidates(db, newest), home=home, prune_source=True, older_than_days=0)
    assert _session(db, newest)["offloaded"] == 1

    import contextlib

    from hermes_cli import main as main_module

    @contextlib.contextmanager
    def _fake_db():
        yield db

    monkeypatch.setattr(main_module, "_session_db", _fake_db)
    monkeypatch.setattr(main_module, "_resolve_workspace_key", lambda: None)
    assert main_module._resolve_last_session() == older


def test_mirror_command_parser_is_registered():
    import argparse

    from hermes_cli.subcommands.sessions import build_sessions_parser

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers()
    build_sessions_parser(subs, cmd_sessions=lambda *a, **k: None)
    ns = parser.parse_args(["sessions", "mirror", "--session-id", "abc", "--prune-source"])
    assert ns.sessions_action == "mirror"
    assert ns.prune_source is True
    ns = parser.parse_args(["sessions", "restore", "abc", "--json"])
    assert ns.sessions_action == "restore"
    assert ns.json is True


def test_mirror_and_restore_handlers_run_end_to_end(tmp_path, monkeypatch):
    home = tmp_path / "home"
    db = _db(tmp_path, home)
    sid = "20260101_000000_handlers"
    _seed(db, sid, cwd=str(tmp_path))
    from hermes_cli import sessions_cmd
    from hermes_cli.sessions_mirror import cmd_mirror, cmd_restore

    monkeypatch.setattr(sessions_cmd, "get_hermes_home", lambda: home)
    args = SimpleNamespace(
        session_id=sid, since=None, include_archived=False, dry_run=False, verify=True,
        update_paths=False, prune_source=True, older_than="0", yes=True, force=False,
    )
    assert cmd_mirror(db, args) == 0
    assert _session(db, sid)["offloaded"] == 1
    assert cmd_restore(db, SimpleNamespace(session_id=sid, dry_run=False, json=False)) == 0
    assert _session(db, sid)["offloaded"] == 0
    assert len(db.get_messages(sid)) == 2
