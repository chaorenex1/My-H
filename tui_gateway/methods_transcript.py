"""Mirrored transcript RPC (ADR-16 P4 / D7).

``session.transcript.list`` enumerates the profile's cold-mirror bundles and
``session.transcript.read`` pages one bundle's JSONL. Both are read-only and address a bundle by
session id: the client never supplies a path, because the gateway may be serving a remote profile
whose filesystem the client cannot see.

Listing is a filesystem scan, not a DB query — the point of the mirror is that the file is there
even when the store is locked or pruned. Only a project directory's own ``*.jsonl`` counts as a
session: ``<bundle>/<topic>.jsonl`` (a subagent) and ``<bundle>/_advisor*.jsonl`` live one level
deeper, ``sessions.json`` / ``request_dump_*`` are files at the root and ``.mirror`` is skipped.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Iterator, Optional

from .method_ctx import HandlerRegistry, bind_module

logger = logging.getLogger(__name__)

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

#: Largest page a client may ask for; a bundle larger than a few MB pages in bounds.
MAX_PAGE = 2000


def _home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _bundle_header(path: Path) -> Optional[dict[str, Any]]:
    """The ``kind: session`` header, or None when the file is not a usable bundle."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    return None
                if isinstance(row, dict) and row.get("kind") == "session" and isinstance(row.get("session"), dict):
                    return row["session"]
                return None
    except OSError:
        return None
    return None


def _message_lines(path: Path) -> Iterator[dict[str, Any]]:
    """Streamed ``kind: message`` rows; a malformed line is skipped, never fatal."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("kind") == "message" and isinstance(row.get("message"), dict):
                    yield row["message"]
    except OSError:
        return


def _count_messages(path: Path) -> int:
    return sum(1 for _ in _message_lines(path))


def _manifest(home: Path) -> dict[str, dict[str, Any]]:
    from hermes_session_mirror import read_manifest

    return read_manifest(home)


def _scan_bundles(home: Path) -> dict[str, dict[str, Any]]:
    """``session id -> file row`` for every main bundle under ``sessions/``.

    A manifest entry whose recorded path is this file supplies the authoritative metadata; a file
    without one is still listed (the header carries the identity), just without mirror bookkeeping.
    When two files claim one id the manifest path wins, else the newer file does.
    """
    import hermes_session_paths as session_paths

    root = session_paths.sessions_root(home)
    manifest = _manifest(home)
    rows: dict[str, dict[str, Any]] = {}
    try:
        projects = sorted(entry for entry in root.iterdir() if entry.is_dir())
    except OSError:
        return rows
    for project in projects:
        if project.name.startswith("."):
            continue
        try:
            files = sorted(path for path in project.iterdir()
                           if path.is_file() and path.suffix.lower() == ".jsonl"
                           and not path.name.startswith("_advisor"))
        except OSError:
            continue
        for path in files:
            header = _bundle_header(path)
            if not header:
                continue
            sid = str(header.get("id") or "")
            if not sid:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entry = manifest.get(sid) or {}
            recorded = str(entry.get("path") or "")
            if recorded and Path(recorded) != path:
                entry = {}
            if entry:
                message_count = int(entry.get("message_count") or 0)
                mirrored_at = entry.get("mirrored_at")
                offloaded = bool(entry.get("offloaded"))
            else:
                message_count = _count_messages(path)
                mirrored_at = None
                offloaded = False
            row = {
                "session_id": sid,
                "project": project.name,
                "path": str(path),
                "started_at": _number(header.get("started_at")),
                "cwd": str(header.get("cwd") or ""),
                "title": str(header.get("title") or ""),
                "size_bytes": int(stat.st_size),
                "message_count": message_count,
                "modified_at": stat.st_mtime,
                "mirrored_at": None if mirrored_at is None else _number(mirrored_at),
                "offloaded": offloaded,
            }
            existing = rows.get(sid)
            if existing is None or _preferred(row, existing, recorded_path=recorded):
                rows[sid] = row
    return rows


def _preferred(candidate: dict[str, Any], current: dict[str, Any], *, recorded_path: str) -> bool:
    if recorded_path and candidate["path"] == recorded_path:
        return True
    if recorded_path and current["path"] == recorded_path:
        return False
    return float(candidate["modified_at"]) > float(current["modified_at"])


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _recorded_mirror_path(sid: str, home: Path) -> str:
    """The row's ``mirror_path`` from THIS profile's store, read-only and never bootstrapped.

    ``_profile_db(params)`` resolves only an explicit ``params.profile``: a session-bound call
    already had its profile bound by ``@_profile_scoped``, so asking it again would read the
    launch store (and bootstrap one for a read). ``home`` is the bound profile's home.
    """
    db_path = home / "state.db"
    if not db_path.is_file():
        return ""
    try:
        from hermes_cli.web_server_sessions import _open_session_db_at_path

        db = _open_session_db_at_path(db_path, read_only=True)
    except Exception:  # health: allow BLE001 -- an unreadable store falls back to "not mirrored"
        logger.debug("transcript: cannot open %s", db_path, exc_info=True)
        return ""
    try:
        row = db.get_session(sid)
    except Exception:  # health: allow BLE001 -- a failed row read is reported as "not mirrored"
        logger.debug("transcript: cannot read session %s", sid, exc_info=True)
        return ""
    finally:
        with contextlib.suppress(Exception):
            db.close()
    return str((row or {}).get("mirror_path") or "")


def _resolve_bundle_path(params: dict, home: Path) -> tuple[Optional[str], Optional[str]]:
    """``(path, error)`` for a session id: manifest, then the row's recorded ``mirror_path``."""
    sid = str(params.get("session_id") or "").strip()
    if not sid:
        return None, "session_id is required"
    recorded = str((_manifest(home).get(sid) or {}).get("path") or "")
    if recorded and Path(recorded).is_file():
        return recorded, None
    mirror_path = _recorded_mirror_path(sid, home)
    if mirror_path and Path(mirror_path).is_file():
        return mirror_path, None
    if recorded:
        return None, f"mirror file is missing: {recorded}"
    return None, f"session {sid} is not mirrored; run `hermes sessions mirror --session-id {sid}`"


@method("session.transcript.list")
@_profile_scoped
def _(rid, params: dict) -> dict:
    try:
        limit = int(params.get("limit", 200) or 200)
    except (TypeError, ValueError):
        return _err(rid, 4001, "limit must be an integer")
    limit = max(1, min(limit, MAX_PAGE * 10))
    home = _home()
    rows = sorted(_scan_bundles(home).values(),
                  key=lambda row: (float(row["started_at"]), float(row["modified_at"])), reverse=True)
    kept = rows[:limit]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in kept:
        grouped.setdefault(row["project"], []).append(row)
    return _ok(rid, {"projects": [{"project": name, "sessions": sessions}
                                 for name, sessions in grouped.items()],
                     "total": len(rows)})


@method("session.transcript.read")
@_profile_scoped
def _(rid, params: dict) -> dict:
    try:
        offset = int(params.get("offset", 0) or 0)
        limit = int(params.get("limit", 200) or 200)
    except (TypeError, ValueError):
        return _err(rid, 4001, "offset and limit must be integers")
    if offset < 0 or limit < 1:
        return _err(rid, 4001, "offset must be >= 0 and limit >= 1")
    limit = min(limit, MAX_PAGE)
    home = _home()
    path, problem = _resolve_bundle_path(params, home)
    if problem is not None:
        return _err(rid, 4001, problem)
    bundle = Path(str(path))
    header = _bundle_header(bundle)
    if header is None:
        return _err(rid, 5006, f"not a mirror bundle: {bundle}")
    messages: list[dict[str, Any]] = []
    total = 0
    for message in _message_lines(bundle):
        if offset <= total < offset + limit:
            messages.append(message)
        total += 1
    return _ok(rid, {
        "session_id": str(header.get("id") or params.get("session_id")),
        "path": str(bundle),
        "session": header,
        "messages": messages,
        "offset": offset,
        "total": total,
        "has_more": offset + len(messages) < total,
    })


def register(server):
    bind_module(globals(), server, skip=("_",))
