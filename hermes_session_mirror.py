"""Session cold mirror and DB reduction (ADR-16 P3 / D8).

The authority split is deliberate: ``state.db`` stays canonical for live sessions, and a
mirror is a read-only export that never participates in a turn. A mirrored session can then
be *offloaded* — its ``messages`` rows are deleted and the JSONL bundle becomes the only
message copy until ``restore`` puts the rows back.

Layout (ADR-16 D2)::

    sessions/.mirror/<profile>.jsonl                 manifest (one entry per session)
    sessions/<project>/<timestamp>_<id>.jsonl        main transcript
    sessions/<project>/<timestamp>_<id>/             artifact + subagent namespace

The JSONL is line-oriented: one ``kind: session`` header, then one ``kind: message`` row per
stored message. That keeps a bundle pageable (P4's transcript RPC) without a second format.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import hermes_session_paths as session_paths
from hermes_constants import get_hermes_home
from hermes_dir_acl import make_user_dir
from utils import atomic_write_bytes, atomic_write_text

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
MANIFEST_VERSION = 1
#: D8 default prune age: ended sessions with no user input for this long.
DEFAULT_PRUNE_AGE_DAYS = 180.0


class MirrorError(Exception):
    """A mirror/restore operation refused or failed; the message is user-facing."""


def profile_label(home: str | os.PathLike[str] | None = None) -> str:
    """Profile id for *home* (``default`` for the root home)."""
    from hermes_constants import profile_name_for_home

    return profile_name_for_home(Path(home) if home is not None else Path(get_hermes_home())) or "default"


def manifest_path(home: str | os.PathLike[str] | None = None) -> Path:
    resolved_home = Path(home) if home is not None else get_hermes_home()
    return session_paths.mirror_manifest_path(profile_label(resolved_home), hermes_home=resolved_home)


# ── manifest ────────────────────────────────────────────────────────────

def read_manifest(home: str | os.PathLike[str] | None = None) -> dict[str, dict[str, Any]]:
    """Entries keyed by session id; malformed lines are ignored (a torn write must not brick
    every later mirror)."""
    path = manifest_path(home)
    entries: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            sid = str(entry.get("session_id") or "")
            if sid:
                entries[sid] = entry
    return entries


def write_manifest(entries: Mapping[str, Mapping[str, Any]], home: str | os.PathLike[str] | None = None) -> Path:
    """Atomically rewrite the manifest (idempotent: same content, same bytes)."""
    path = manifest_path(home)
    make_user_dir(path.parent, parents=True, exist_ok=True)
    text = "".join(
        json.dumps(dict(entries[sid]), ensure_ascii=True, sort_keys=True, default=str) + "\n"
        for sid in sorted(entries)
    )
    atomic_write_text(path, text)
    return path


def _updated_entry(entry: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    merged = dict(entry)
    merged.update({key: value for key, value in changes.items() if value is not None})
    return merged


# ── JSONL payload ───────────────────────────────────────────────────────

def _json_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, default=str) + "\n"


def build_payload(db: Any, session_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``(session header, messages)`` from the store's canonical export projection.

    ``include_inactive=True`` keeps rewound/undone rows and their flags: the mirror is a
    cold copy, not a display projection, and restore must reproduce what the row held.
    """
    data = db.export_session(session_id, include_inactive=True)
    if not data:
        raise MirrorError(f"session {session_id!r} not found")
    messages = list(data.pop("messages", []) or [])
    data.pop("timings", None)
    # Mirror bookkeeping changes on every export; including it would make the cold copy
    # differ from itself on the second run (and break byte idempotency).
    for transient in ("mirror_path", "mirrored_at", "offloaded"):
        data.pop(transient, None)
    return data, messages


def render_bundle(session: dict[str, Any], messages: Iterable[dict[str, Any]]) -> str:
    """The exact JSONL bytes for one session; a message row never spans lines."""
    lines = [_json_line({"kind": "session", "version": FORMAT_VERSION, "session": session})]
    lines.extend(_json_line({"kind": "message", "message": msg}) for msg in messages)
    return "".join(lines)


def read_bundle(path: str | os.PathLike[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``(session, messages)`` from a bundle file; raises :class:`MirrorError` when malformed."""
    session: Optional[dict[str, Any]] = None
    messages: list[dict[str, Any]] = []
    try:
        handle = Path(path).open("r", encoding="utf-8")
    except OSError as exc:
        raise MirrorError(f"cannot read mirror {path}: {exc}") from exc
    with handle:
        for lineno, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise MirrorError(f"invalid mirror JSON at {path}:{lineno}: {exc}") from exc
            if not isinstance(row, dict):
                continue
            kind = row.get("kind")
            if kind == "session" and isinstance(row.get("session"), dict):
                session = row["session"]
            elif kind == "message" and isinstance(row.get("message"), dict):
                messages.append(row["message"])
    if session is None:
        raise MirrorError(f"mirror {path} has no session header")
    return session, messages


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_entry(entry: Mapping[str, Any], *, home: str | os.PathLike[str] | None = None) -> tuple[bool, str]:
    """``(ok, reason)`` for one manifest entry against the file on disk."""
    raw_path = str(entry.get("path") or "")
    if raw_path:
        path = Path(raw_path)
    else:
        relative = str(entry.get("relative_path") or "")
        if not relative:
            return False, "manifest entry has no path"
        path = session_paths.sessions_root(home) / relative
    if not path.is_file():
        return False, f"missing mirror file: {path}"
    try:
        size = path.stat().st_size
        digest = _sha256(path)
    except OSError as exc:
        return False, f"cannot read mirror {path}: {exc}"
    if entry.get("bytes") is not None and int(entry["bytes"]) != size:
        return False, f"size mismatch for {path} (manifest {entry['bytes']}, file {size})"
    if entry.get("sha256") and str(entry["sha256"]) != digest:
        return False, f"sha256 mismatch for {path}"
    return True, "ok"


# ── mirror ──────────────────────────────────────────────────────────────

def _bundle_paths(row: Mapping[str, Any], entry: Mapping[str, Any] | None,
                  home: Path) -> tuple[Path, Path]:
    """``(transcript, artifact_dir)``; a recorded manifest path wins over derivation (D2)."""
    if entry:
        recorded = str(entry.get("path") or "")
        if recorded:
            transcript = Path(recorded)
            return transcript, transcript.with_suffix("")
    row_path = str(row.get("mirror_path") or "")
    if row_path:
        transcript = Path(row_path)
        return transcript, transcript.with_suffix("")
    cwd = str(row.get("cwd") or os.getcwd())
    artifact_dir = session_paths.allocate_bundle_dir(
        cwd, row.get("started_at") or time.time(), row.get("id") or "session",
        hermes_home=home,
    )
    return artifact_dir.with_suffix(".jsonl"), artifact_dir


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=True, default=str)
    return str(content)


def _write_tool_artifacts(
    directory: Path, messages: list[dict[str, Any]], *, dry_run: bool
) -> int:
    """Backfill ``<step>.<tool>.log`` from stored (already truncated) result rows.

    ``-original.log`` is never fabricated: it only exists when the live wrapper actually
    truncated. Existing files are never overwritten (live archives win).
    """
    try:
        from tools.tool_output_artifacts import get_artifact_settings, is_excluded_tool
    except Exception:  # health: allow BLE001 -- archival must not fail the mirror
        return 0
    settings = get_artifact_settings()
    step = 0
    written = 0
    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_name = message.get("tool_name") or "tool"
        if not settings.enabled or is_excluded_tool(tool_name, settings):
            continue
        # The live wrapper allocates a step only AFTER the exclusion check
        # (archive_tool_result returns early), so excluded calls must not advance this counter
        # or every later backfilled name drifts from tool_output_seq.
        step += 1
        path, _ = session_paths.tool_artifact_paths(directory, step, tool_name)
        if path.exists():
            continue
        if not dry_run:
            make_user_dir(directory, parents=True, exist_ok=True)
            atomic_write_bytes(path, _content_text(message.get("content")).encode("utf-8", "surrogatepass"))
        written += 1
    return written


def _update_agent_session_paths(db: Any, parent_session_id: str, bundle_dir: Path) -> int:
    """One-time metadata update for subagent transcripts that moved into the bundle (D4)."""
    updated = 0
    try:
        rows = db.list_agent_sessions(parent_session_id=parent_session_id, limit=1000)
    except Exception:  # health: allow BLE001 -- best-effort metadata backfill
        logger.debug("mirror: agent session lookup failed", exc_info=True)
        return 0
    for row in rows:
        candidate = session_paths.transcript_path(bundle_dir, row.get("subject") or "")
        if not candidate.is_file():
            continue
        if str(candidate) == str(row.get("transcript_path") or ""):
            continue
        try:
            db.set_agent_session_transcript_path(row.get("agent_session_id") or "", str(candidate))
            updated += 1
        except Exception:  # health: allow BLE001 -- a row write must not abort the mirror
            logger.debug("mirror: transcript path update failed for %s", row.get("agent_session_id"),
                         exc_info=True)
    return updated


def mirror_session(
    db: Any,
    row: Mapping[str, Any],
    *,
    home: str | os.PathLike[str] | None = None,
    entries: Optional[dict[str, dict[str, Any]]] = None,
    dry_run: bool = False,
    verify: bool = False,
    update_paths: bool = False,
) -> dict[str, Any]:
    """Export one physical session to its bundle; returns a per-session report.

    Offloaded rows are reported, never re-exported: the DB has no messages to write and an
    empty file would silently replace the cold copy.
    """
    resolved_home = Path(home) if home is not None else get_hermes_home()
    sid = str(row.get("id") or "")
    if not sid:
        return {"session_id": "", "ok": False, "status": "error", "reason": "session id missing"}
    if row.get("offloaded"):
        return {"session_id": sid, "ok": True, "status": "offloaded",
                "reason": "already offloaded; run hermes sessions restore <id>"}

    manifest = entries if entries is not None else read_manifest(resolved_home)
    entry = manifest.get(sid)
    transcript, bundle_dir = _bundle_paths(row, entry, resolved_home)
    try:
        session, messages = build_payload(db, sid)
    except MirrorError as exc:
        return {"session_id": sid, "ok": False, "status": "error", "reason": str(exc)}

    text = render_bundle(session, messages)
    encoded = text.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    written = False
    if not dry_run:
        try:
            existing = transcript.read_bytes()
        except OSError:
            existing = None
        if existing != encoded:
            make_user_dir(transcript.parent, parents=True, exist_ok=True)
            atomic_write_bytes(transcript, encoded)
            written = True
    artifacts = _write_tool_artifacts(bundle_dir, messages, dry_run=dry_run)
    paths_updated = 0
    if update_paths and not dry_run:
        paths_updated = _update_agent_session_paths(db, sid, bundle_dir)

    now = time.time()
    if not dry_run:
        try:
            relative = transcript.relative_to(session_paths.sessions_root(resolved_home))
            relative_path = str(relative)
        except ValueError:
            relative_path = ""
        entry = dict(entry or {})
        entry.update(
            version=MANIFEST_VERSION,
            session_id=sid,
            path=str(transcript),
            relative_path=relative_path,
            bundle_dir=str(bundle_dir),
            sha256=digest,
            bytes=len(encoded),
            message_count=len(messages),
            started_at=row.get("started_at"),
            mirrored_at=now,
            offloaded=0,
        )
        manifest[sid] = entry
        if not getattr(db, "read_only", False):
            db.mark_session_mirrored(sid, str(transcript), now)

    report: dict[str, Any] = {
        "session_id": sid,
        "ok": True,
        "status": "written" if written else "unchanged",
        "path": str(transcript),
        "bundle_dir": str(bundle_dir),
        "messages": len(messages),
        "artifacts_written": artifacts,
        "paths_updated": paths_updated,
        "dry_run": dry_run,
    }
    if verify:
        ok, reason = verify_entry(entry or {
            "path": str(transcript), "sha256": digest, "bytes": len(encoded),
        }, home=resolved_home)
        report["verified"] = ok
        if not ok:
            report.update(ok=False, status="verify_failed", reason=reason)
    return report


def mirror_sessions(
    db: Any,
    rows: Iterable[Mapping[str, Any]],
    *,
    home: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
    verify: bool = False,
    update_paths: bool = False,
    prune_source: bool = False,
    older_than_days: Optional[float] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Mirror a selection, then (optionally) offload eligible ones.

    Prune eligibility is evaluated from the row's last *user* message (falling back to
    ``started_at``), so a session that keeps receiving assistant/tool traffic still ages.
    """
    resolved_home = Path(home) if home is not None else get_hermes_home()
    entries = read_manifest(resolved_home)
    reports: list[dict[str, Any]] = []
    pruned: list[dict[str, Any]] = []
    skipped_prune: list[dict[str, Any]] = []
    cutoff_days = DEFAULT_PRUNE_AGE_DAYS if older_than_days is None else float(older_than_days)
    if cutoff_days < 0:
        raise MirrorError("--older-than must be >= 0")
    cutoff = (time.time() if now is None else float(now)) - cutoff_days * 86400.0

    for row in rows:
        report = mirror_session(
            db, row, home=resolved_home, entries=entries, dry_run=dry_run,
            verify=verify, update_paths=update_paths,
        )
        reports.append(report)
        if not prune_source or not report.get("ok") or report.get("status") == "offloaded":
            continue
        sid = report["session_id"]
        eligibility = _prune_eligible(row, cutoff, cutoff_days=cutoff_days)
        has_child = db.has_compression_continuation(sid) if eligibility is True else False
        if dry_run:
            if eligibility is True and not has_child:
                pruned.append({"session_id": sid, "dry_run": True})
            else:
                skipped_prune.append(
                    {"session_id": sid, "reason": "has_continuation" if has_child else eligibility})
            continue
        if eligibility is not True:
            skipped_prune.append({"session_id": sid, "reason": eligibility})
            continue
        if db.has_compression_continuation(sid):
            skipped_prune.append({"session_id": sid, "reason": "has_continuation"})
            continue
        entry = entries.get(sid) or {}
        ok, reason = verify_entry(entry, home=resolved_home)
        if not ok:
            skipped_prune.append({"session_id": sid, "reason": reason})
            continue
        result = db.offload_session_messages(
            sid, mirror_path=str(entry.get("path") or ""),
            mirrored_at=entry.get("mirrored_at"),
            expected_message_count=entry.get("message_count"),
        )
        if result.get("ok"):
            entries[sid] = _updated_entry(entry, offloaded=1)
            pruned.append({**result, "session_id": sid})
        else:
            skipped_prune.append({"session_id": sid, "reason": result.get("error") or "refused",
                                  **{k: v for k, v in result.items() if k not in {"ok", "error"}}})

    if not dry_run and reports:
        write_manifest(entries, resolved_home)
    return {"reports": reports, "pruned": pruned, "skipped_prune": skipped_prune,
            "manifest": str(manifest_path(resolved_home)), "dry_run": dry_run}


def _prune_eligible(row: Mapping[str, Any], cutoff: float, *, cutoff_days: float) -> bool | str:
    """D8: ended, no live lease, and no user input since *cutoff* (``--older-than 0`` opts out)."""
    if row.get("offloaded"):
        return "already_offloaded"
    if row.get("ended_at") is None:
        return "open"
    if cutoff_days <= 0:
        return True
    last_user = row.get("last_user_at")
    reference = float(last_user) if last_user is not None else float(row.get("started_at") or 0)
    return True if reference < cutoff else "too_recent"


# ── restore ─────────────────────────────────────────────────────────────

def _resolve_session(db: Any, target: str, manifest: Mapping[str, dict[str, Any]]) -> Optional[str]:
    if db is not None:
        try:
            resolved = db.resolve_session_id(target)
            if resolved:
                return resolved
        except Exception:  # health: allow BLE001 -- fall through to the manifest
            logger.debug("mirror: session id resolution failed", exc_info=True)
    if target in manifest:
        return target
    matches = [sid for sid in manifest if sid.startswith(target)]
    return matches[0] if len(matches) == 1 else None


def restore_session(
    db: Any,
    target: str,
    *,
    home: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
    allow_missing_row: bool = True,
) -> dict[str, Any]:
    """Restore one offloaded session from its bundle.

    Verification is mandatory: a missing or hash-mismatched mirror refuses rather than
    producing an empty session. A row that no longer exists at all is re-imported from the
    payload (manual recovery into a fresh store); an existing metadata row keeps its routing
    identity and gets only its messages back.
    """
    resolved_home = Path(home) if home is not None else get_hermes_home()
    entries = read_manifest(resolved_home)
    sid = _resolve_session(db, target, entries)
    if not sid:
        raise MirrorError(f"no mirrored session matches {target!r}")
    entry = entries.get(sid) or {}
    row = None
    if db is not None:
        row = db.get_session(sid)
        if row and not entry.get("path") and row.get("mirror_path"):
            entry = {"session_id": sid, "path": row.get("mirror_path"),
                     "mirrored_at": row.get("mirrored_at")}
    transcript = str(entry.get("path") or "")
    if not transcript:
        raise MirrorError(f"session {sid} has no recorded mirror path")
    path = Path(transcript)
    if not path.is_file():
        raise MirrorError(f"mirror file is missing: {path}")
    if entry.get("sha256"):
        try:
            digest = _sha256(path)
        except OSError as exc:
            raise MirrorError(f"cannot read mirror {path}: {exc}") from exc
        if digest != str(entry["sha256"]):
            raise MirrorError(f"mirror checksum mismatch: {path}")
    session, messages = read_bundle(path)
    if dry_run:
        return {"ok": True, "session_id": sid, "path": str(path),
                "messages": len(messages), "dry_run": True}
    if not getattr(db, "read_only", False):
        db.mark_session_mirrored(sid, str(path), entry.get("mirrored_at"))
    if row is None:
        if not allow_missing_row:
            raise MirrorError(f"session row {sid} is missing")
        payload = {**session, "id": sid, "messages": messages}
        result = db.import_sessions([payload])
        if not result.get("ok"):
            raise MirrorError(f"restore import failed: {result.get('errors')}")
        entries[sid] = _updated_entry(entry, offloaded=0)
        write_manifest(entries, resolved_home)
        return {"ok": True, "session_id": sid, "path": str(path),
                "messages": len(messages), "status": "imported"}
    result = db.restore_session_messages(sid, messages)
    if not result.get("ok"):
        reason = result.get("error") or "refused"
        if reason == "not_empty":
            db.clear_session_offloaded(sid)
            return {"ok": True, "session_id": sid, "path": str(path), "status": "already_present"}
        raise MirrorError(f"restore refused for {sid}: {reason}")
    entries[sid] = _updated_entry(entry, offloaded=0)
    write_manifest(entries, resolved_home)
    return {"ok": True, "session_id": sid, "path": str(path),
            "messages": len(messages), "status": "restored", **result}


def ensure_session_restored(
    db: Any, session_id: str, *, home: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Restore *session_id* when it is offloaded; returns the refreshed row.

    This is the read-side entry point for resume: a failure raises :class:`MirrorError`, so
    callers refuse the resume instead of opening an empty conversation.
    """
    row = db.get_session(session_id)
    if not row or not row.get("offloaded"):
        return row or {}
    result = restore_session(db, session_id, home=home)
    refreshed = db.get_session(session_id)
    if not refreshed:
        raise MirrorError(result.get("status") or "restore did not produce a session row")
    return refreshed
