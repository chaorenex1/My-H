"""``hermes sessions mirror`` / ``restore`` handlers (ADR-16 P3 / D8)."""

from __future__ import annotations

import json

from hermes_session_mirror import (
    DEFAULT_PRUNE_AGE_DAYS,
    MirrorError,
    mirror_sessions,
    read_manifest,
    restore_session,
)


def _home():
    from hermes_cli.sessions_cmd import get_hermes_home

    return get_hermes_home()


def _selection(db, args):
    from hermes_cli.session_filters import parse_point_in_time

    if getattr(args, "session_id", None):
        target = args.session_id
        resolved = db.resolve_session_id(target) if db is not None else None
        if not resolved:
            entries = read_manifest(_home())
            matches = [sid for sid in entries if sid.startswith(target)]
            resolved = matches[0] if len(matches) == 1 else None
        if not resolved:
            raise MirrorError(f"no session matches {target!r}")
        return db.list_mirror_candidates(session_ids=[resolved], include_archived=True)
    started_after = None
    if getattr(args, "since", None):
        started_after = parse_point_in_time(args.since, "--since")
    return db.list_mirror_candidates(
        started_after=started_after,
        include_archived=bool(getattr(args, "include_archived", False)),
    )


def _older_than_days(value) -> float | None:
    if value is None:
        return None
    from hermes_cli.session_filters import parse_duration_seconds

    seconds = parse_duration_seconds(str(value))
    if seconds is None:
        raise MirrorError(f"Invalid --older-than value: {value!r} (use days or a duration)")
    return seconds / 86400.0


def _print_mirror_report(result) -> None:
    for report in result["reports"]:
        if report.get("ok"):
            detail = f"{report.get('messages', 0)} message(s)"
            if report.get("artifacts_written"):
                detail += f", {report['artifacts_written']} artifact(s)"
            if report.get("paths_updated"):
                detail += f", {report['paths_updated']} path(s) updated"
            print(f"  {report['session_id']}  {report.get('status')}  {detail}")
        else:
            print(f"  {report['session_id']}  FAILED  {report.get('reason')}")
    pruned = result.get("pruned") or []
    skipped = result.get("skipped_prune") or []
    for item in pruned:
        if item.get("dry_run"):
            print(f"  would offload {item['session_id']} (verified mirror)")
        else:
            print(f"  offloaded {item['session_id']}  deleted {item.get('deleted', 0)} message row(s)")
    for item in skipped:
        print(f"  kept {item['session_id']}  {item.get('reason')}")
    print(f"Manifest: {result.get('manifest')}")


def cmd_mirror(db, args) -> int:
    from hermes_cli.sessions_cmd import _confirm_or_exit_code

    try:
        rows = _selection(db, args)
        older_than = _older_than_days(getattr(args, "older_than", None))
        prune = bool(getattr(args, "prune_source", False))
        dry_run = bool(getattr(args, "dry_run", False))
        if not rows:
            print("No sessions matched.")
            return 0
        if prune and not dry_run and not getattr(args, "yes", False):
            code = _confirm_or_exit_code(
                f"Mirror {len(rows)} session(s) and offload eligible message rows? [y/N] ",
                unattended="--yes",
            )
            if code is not None:
                return code
        result = mirror_sessions(
            db, rows,
            home=_home(),
            dry_run=dry_run,
            verify=bool(getattr(args, "verify", False)),
            update_paths=bool(getattr(args, "update_paths", False)),
            prune_source=prune,
            older_than_days=older_than if older_than is not None else DEFAULT_PRUNE_AGE_DAYS,
        )
        verb = "Would mirror" if dry_run else "Mirrored"
        print(f"{verb} {len(result['reports'])} session(s).")
        _print_mirror_report(result)
        failed = [r for r in result["reports"] if not r.get("ok")]
        verify_failed = [r for r in result["reports"] if r.get("verified") is False]
        if failed:
            return 1
        return 1 if verify_failed else 0
    except MirrorError as exc:
        print(f"Error: {exc}")
        return 1


def cmd_restore(db, args) -> int:
    target = args.session_id
    try:
        result = restore_session(db, target, home=_home(), dry_run=bool(getattr(args, "dry_run", False)))
    except MirrorError as exc:
        print(f"Error: {exc}")
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    if result.get("dry_run"):
        print(f"Verified mirror for {result['session_id']}: {result['path']} "
              f"({result['messages']} message row(s)); nothing written.")
        return 0
    status = result.get("status") or "restored"
    if status == "already_present":
        print(f"Session {result['session_id']} already has its messages; nothing to restore.")
    else:
        print(f"Restored {result['session_id']} from {result['path']} "
              f"({result.get('live', result.get('messages', 0))} live message row(s)).")
    return 0
