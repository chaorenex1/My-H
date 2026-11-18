"""Opt-in git worktree isolation for delegated subagents (``delegation.worktree_isolation``).

Git-only (outside a repo children share the parent's cwd); local terminal backend only (on
docker/ssh/modal the host worktree is invisible in the sandbox, so isolation is skipped). One
worktree per child under ``<repo>/.worktrees/subagent-<id>``, branch ``hermes-subagent/<id>``.
On finish: zero commits AND clean (both probes ok) -> removed; work left behind -> archived to
``refs/hermes/archive/<tree>`` then removed (``worktrees.archive_dirty``, branch kept so the parent
can merge it); a failed probe or archive -> kept + flagged.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_cli._subprocess_compat import harden_git_argv, noninteractive_git_env

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30


def _run_git(args, cwd: str, timeout: int = _GIT_TIMEOUT, env=None):
    """Run git capturing output; never raises on non-zero exit.

    :func:`noninteractive_git_env` (GHSA-7x36-8jrh-v4pw): this runs unattended against whatever
    repo the parent sits in and ``worktree add`` runs hooks, so a malicious ``.git/config`` must
    not execute.
    """
    return subprocess.run(["git", *harden_git_argv(args)], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, env=env or noninteractive_git_env(), check=False)


def local_backend_active() -> bool:
    """True when the terminal backend is local (worktrees visible to tools)."""
    try:
        from hermes_cli.config import load_config_readonly

        backend = (load_config_readonly().get("terminal") or {}).get("backend") or "local"
        return str(backend).strip().lower() in ("", "local")
    except Exception:
        # Legacy entry points without the shared loader default to local.
        return True


def resolve_repo_root(path: Optional[str]) -> Optional[str]:
    """Return the git toplevel for *path*, or None when not in a work tree."""
    candidate = os.path.abspath(os.path.expanduser(str(path))) if path else ""
    if not candidate or not os.path.isdir(candidate):
        return None
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], cwd=candidate)
    except Exception as exc:
        logger.debug("subagent worktree: rev-parse failed: %s", exc)
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def _ensure_worktrees_excluded(repo_root: str) -> None:
    """Best-effort: keep ``.worktrees/`` out of git status without touching tracked files.

    ``.git/info/exclude`` is local to the checkout and shared by its linked worktrees. Writing
    the entry into a tracked ``.gitignore`` would leave the parent `` M .gitignore`` forever,
    which makes every later auto-apply see a dirty parent.
    """
    probe = _run_git(["rev-parse", "--git-path", "info/exclude"], cwd=repo_root)
    if probe.returncode != 0 or not probe.stdout.strip():
        return
    exclude = Path(probe.stdout.strip())
    if not exclude.is_absolute():
        exclude = Path(repo_root) / exclude
    try:
        existing = exclude.read_text(encoding="utf-8-sig", errors="replace") if exclude.exists() else ""
        if ".worktrees/" not in existing.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with open(exclude, "a", encoding="utf-8") as f:
                sep = "\n" if existing and not existing.endswith("\n") else ""
                f.write(f"{sep}.worktrees/\n")
    except OSError as exc:
        logger.debug("subagent worktree: could not update info/exclude: %s", exc)


def _parent_clean_state(repo_root: str, env: dict[str, str]) -> str:
    """``"true"``/``"false"``/``"unknown"``: the parent's status at dispatch.

    Two probes, with and without ``core.autocrlf``: the isolated env blanks global config, so on
    Windows a CRLF working tree committed under ``autocrlf=true`` reads as modified under the
    blanked config. A genuine edit is dirty under both settings; an EOL-only difference is clean
    under the setting that matches the checkout, and merge is safe either way.
    """
    verdicts: list[bool] = []
    for value in ("false", "true"):
        probe = _run_git(["-c", f"core.autocrlf={value}", "status", "--porcelain",
                          "--untracked-files=normal"], cwd=repo_root, env=env)
        if probe.returncode != 0:
            return "unknown"
        verdicts.append(not probe.stdout.strip())
    return "true" if any(verdicts) else "false"


def create_subagent_worktree(parent_cwd: Optional[str], subagent_id: Optional[str] = None) -> Optional[dict[str, str]]:
    """Create an isolated worktree for one child; None (silent downgrade) outside git/on failure."""
    repo_root = resolve_repo_root(parent_cwd)
    if not repo_root:
        return None
    wt_name = f"subagent-{(subagent_id or uuid.uuid4().hex[:8]).replace('/', '-')}"
    branch = f"hermes-subagent/{wt_name}"
    wt_path = Path(repo_root) / ".worktrees" / wt_name
    try:
        # The checkout runs the repo-named smudge filter; skip isolation when it cannot be neutralized.
        from hermes_cli._subprocess_compat import noninteractive_repo_git_env
        add_env = noninteractive_repo_git_env(repo_root)
        if add_env is None:
            logger.warning("subagent worktree: filter discovery failed; not creating a worktree")
            return None
        # Captured before anything this helper writes: "dirty parent" for auto-apply means
        # the user's workspace was dirty at dispatch.
        parent_clean = _parent_clean_state(repo_root, add_env)
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_worktrees_excluded(repo_root)
        base = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
        base_commit = base.stdout.strip() if base.returncode == 0 else ""
        result = _run_git(["worktree", "add", str(wt_path), "-b", branch, "HEAD"], cwd=repo_root, env=add_env)
    except Exception as exc:
        logger.warning("subagent worktree: creation failed: %s", exc)
        return None
    if result.returncode != 0:
        # Common on repos with zero commits (unborn HEAD) — degrade silently.
        logger.warning("subagent worktree: git worktree add failed: %s", result.stderr.strip())
        return None
    logger.info("subagent worktree created: %s (branch %s)", wt_path, branch)
    from hermes_cli.worktree_registry import record_worktree_repo
    record_worktree_repo(repo_root)
    return {"path": str(wt_path), "branch": branch, "repo_root": repo_root,
            "base_commit": base_commit, "parent_clean": parent_clean}


def _base_payload(info: dict[str, str]) -> dict[str, Any]:
    """Result-entry schema the parent expects (no creation-side internals)."""
    return {"path": info.get("path", ""), "branch": info.get("branch", ""),
            "commits": 0, "dirty": False, "pruned": False, "applied": False}


def mark_worktree_payload_unproven(payload: dict[str, Any], reason: str, *,
                                   unmeasured: str = "commits/dirty") -> dict[str, Any]:
    """Flag a worktree result payload as un-inspected, in place.

    The parent only sees this dict, so the uncertainty must travel in it or "0 commits, clean"
    reads as "the child produced nothing". *unmeasured* names only the fields actually left
    unproven (one probe can succeed while the other fails).

    See #88113.
    """
    path, branch = payload.get("path", ""), payload.get("branch", "")
    payload["inspection_failed"] = True
    payload["note"] = (f"git inspection failed ({reason}): {unmeasured} UNKNOWN — not proven "
                       f"zero/clean. The worktree and branch were preserved — inspect {path} "
                       f"(branch {branch}) before assuming no work.")
    logger.warning("subagent worktree: git inspection failed (%s) — keeping %s (branch %s) "
                   "for manual review", reason, path, branch)
    return payload


def unproven_worktree_payload(info: dict[str, str], reason: str) -> dict[str, Any]:
    """Complete un-inspected payload for ``delegate_tool`` when finalize raises."""
    return mark_worktree_payload_unproven(_base_payload(info), reason)


def _apply_env(repo_root: str) -> Optional[dict[str, str]]:
    """Isolated git env for commit/merge; identity is explicit because global config is blanked."""
    from hermes_cli._subprocess_compat import noninteractive_repo_git_env

    env = noninteractive_repo_git_env(repo_root)
    if env is None:
        return None
    env = dict(env)
    for key, value in (("GIT_AUTHOR_NAME", "Hermes"),
                       ("GIT_AUTHOR_EMAIL", "hermes@localhost"),
                       ("GIT_COMMITTER_NAME", "Hermes"),
                       ("GIT_COMMITTER_EMAIL", "hermes@localhost")):
        env.setdefault(key, value)
    return env


def _apply_worktree(info: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    """Commit the child's edits and merge its branch into the parent; never raises.

    A dirty parent, an unknown parent state, a failed commit or a merge conflict all leave the
    branch in place with ``applied=false`` and an ``apply_error`` — the parent can inspect and
    merge by hand. Success records the merge commit and lets the caller prune the tree.
    """
    repo_root = info.get("repo_root", "") or info.get("path", "")
    path, branch = info.get("path", ""), info.get("branch", "")
    if info.get("parent_clean") != "true":
        payload["apply_error"] = (
            "parent workspace had uncommitted changes when the agent was dispatched; "
            f"branch {branch} kept for manual merge" if info.get("parent_clean") == "false"
            else f"parent workspace state unproven; branch {branch} kept for manual merge")
        return payload
    env = _apply_env(repo_root)
    if env is None:
        payload["apply_error"] = "filter discovery failed; branch kept for manual merge"
        return payload
    if payload.get("dirty"):
        added = _run_git(["add", "-A"], cwd=path, env=env)
        if added.returncode != 0:
            payload["apply_error"] = f"git add failed: {added.stderr.strip()[:300]}"
            return payload
        committed = _run_git(
            ["-c", "commit.gpgsign=false", "commit", "--no-verify",
             "-m", "hermes subagent: uncommitted work"], cwd=path, env=env)
        if committed.returncode != 0:
            payload["apply_error"] = f"git commit failed: {committed.stderr.strip()[:300]}"
            return payload
        payload["dirty"] = False
        payload["commits"] = int(payload.get("commits") or 0) + 1
    merged = _run_git(["-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-edit", branch],
                      cwd=repo_root, env=env)
    if merged.returncode != 0:
        _run_git(["merge", "--abort"], cwd=repo_root, env=env)
        payload["apply_error"] = (
            f"merge into the parent failed: {(merged.stderr or merged.stdout).strip()[:400]}; "
            f"branch {branch} kept")
        return payload
    head = _run_git(["rev-parse", "HEAD"], cwd=repo_root, env=env)
    payload["applied"] = True
    payload["merge_commit"] = head.stdout.strip() if head.returncode == 0 else ""
    return payload


def _prune_merged_worktree(info: dict[str, str], payload: dict[str, Any]) -> None:
    """Remove the tree and its now-merged branch; a failure leaves both with a note."""
    from hermes_cli.worktree_archive import ArchiveError
    from hermes_cli.worktree_ops import release_lsp_clients, removal_blocked

    path, branch = info.get("path", ""), info.get("branch", "")
    repo_root = info.get("repo_root", "") or path
    try:
        blocked = removal_blocked(path)
        if blocked:
            payload["note"] = f"Worktree kept at {path}: {blocked[1]}."
            return
        release_lsp_clients(path)
        removed = _run_git(["worktree", "remove", "--force", path], cwd=repo_root)
        if removed.returncode != 0:
            payload["note"] = f"Merged, but the worktree could not be removed: {removed.stderr.strip()[:200]}"
            return
        deleted = _run_git(["branch", "-d", branch], cwd=repo_root)
        payload["pruned"] = True
        if deleted.returncode != 0:
            payload["note"] = f"Merged and removed; branch {branch} kept: {deleted.stderr.strip()[:200]}"
        logger.info("subagent worktree merged and pruned: %s", path)
    except (ArchiveError, OSError, subprocess.SubprocessError) as exc:
        logger.warning("subagent worktree: prune after merge failed: %s", exc, exc_info=True)
        payload["note"] = f"Merged, but cleanup failed: {exc}"


def finalize_subagent_worktree(info: dict[str, str], *, prune: bool = True,
                               apply: str = "off") -> dict[str, Any]:
    """Inspect (and possibly apply/prune) a child worktree after the child finishes.

    ``apply="auto"`` commits the child's edits and merges its branch into the parent (see
    :func:`_apply_worktree`); a dirty/unknown parent or a conflict keeps the branch and reports
    ``applied=false``. ``apply="off"`` is the legacy review-by-hand behavior. Prunes only when
    *prune*, commits==0, clean tree AND both git probes succeeded; otherwise keeps it with
    ``inspection_failed`` + ``note`` (``commits``/``dirty`` then are defaults).
    """
    if apply not in {"off", "auto"}:
        raise ValueError("apply must be 'off' or 'auto'")
    path, branch = info.get("path", ""), info.get("branch", "")
    base_commit = info.get("base_commit", "")
    payload = _base_payload(info)
    if not path or not os.path.isdir(path):
        payload["pruned"] = True  # nothing on disk to review
        return payload
    # Without a base commit the count is an unproven default, and the prune
    # condition reads payload["commits"] — so it must not prune either.
    # See #88113.
    if not base_commit:
        return mark_worktree_payload_unproven(
            payload, "no base_commit recorded — commit count unmeasurable", unmeasured="commits")
    # The status probe re-hashes files the child touched, which runs repo-named clean filters.
    from hermes_cli._subprocess_compat import noninteractive_repo_git_env
    probe_env = noninteractive_repo_git_env(path)
    if probe_env is None:
        return mark_worktree_payload_unproven(payload, "filter discovery failed", unmeasured="dirty")
    failed, unmeasured = [], []
    probes = (("commits", "rev-list", ["rev-list", "--count", f"{base_commit}..HEAD"],
               lambda s: int(s or 0)),
              ("dirty", "status", ["status", "--porcelain", "--untracked-files=normal"], bool))
    try:
        for field, label, args, parse in probes:
            res = _run_git(args, cwd=path, env=probe_env)
            if res.returncode == 0:
                payload[field] = parse(res.stdout.strip())
            else:
                failed.append(f"{label} exit {res.returncode}: {res.stderr.strip()[:200]}")
                unmeasured.append(field)
    except Exception as exc:
        # Timeout, OSError or non-numeric rev-list stdout: which probe raised is
        # unknowable, so neither value is trustworthy — keep the worktree.
        return mark_worktree_payload_unproven(payload, f"inspection raised: {exc}")
    if failed:
        # Destructive cleanup requires affirmative proof; defaults prove nothing.
        return mark_worktree_payload_unproven(payload, "; ".join(failed), unmeasured="/".join(unmeasured))
    if not payload["dirty"]:
        # status trusts skip-worktree / assume-unchanged: edits behind them are invisible to it.
        from hermes_cli.worktree_archive import ArchiveError
        from hermes_cli.worktree_retention import hidden_index_edits
        try:
            payload["dirty"] = bool(hidden_index_edits(path))
        except (ArchiveError, OSError, subprocess.SubprocessError) as exc:
            return mark_worktree_payload_unproven(payload, f"index flag probe failed: {exc}", unmeasured="dirty")
    if apply == "auto" and (payload["commits"] or payload["dirty"]):
        payload = _apply_worktree(info, payload)
        if payload.get("applied"):
            if prune:
                _prune_merged_worktree(info, payload)
            return payload
        # The merge was refused or conflicted: fall through to the keep/archive path.
    if prune and (payload["commits"] or payload["dirty"]):
        return _archive_finished_worktree(info, payload)
    if prune and payload["commits"] == 0 and not payload["dirty"]:
        cwd = info.get("repo_root", "") or path
        try:
            from hermes_cli.worktree_ops import release_lsp_clients, removal_blocked
            blocked = removal_blocked(path)  # an embedded repo / private ref dies with the tree
            if blocked:
                payload["note"] = f"Worktree kept at {path}: {blocked[1]}."
                return payload
            release_lsp_clients(path)  # the child ran in-process: its language servers are ours
            removed = _run_git(["worktree", "remove", "--force", path], cwd=cwd)
            if removed.returncode == 0:
                _run_git(["branch", "-D", branch], cwd=cwd)
                payload["pruned"] = True
                logger.info("subagent worktree pruned (no work): %s", path)
            else:
                logger.debug("subagent worktree: prune failed: %s", removed.stderr.strip())
        except Exception as exc:
            logger.debug("subagent worktree: prune failed: %s", exc)
    return payload


def _archive_finished_worktree(info: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    """A finished child left commits or edits: snapshot them, then remove the tree.

    The branch is kept (the parent reviews and merges it); the archive also covers uncommitted
    edits. ``worktrees.archive_dirty: false`` or a failed snapshot keeps the tree as before.
    """
    from hermes_cli.worktree_archive import ArchiveError
    from hermes_cli.worktree_sweep import archive_then_remove, live_process_pids, sweep_settings
    settings = sweep_settings()
    if not settings["archive_dirty"]:
        return payload
    path = info.get("path", "")
    # A server or watcher the child started may still run in the tree: leave it to the sweep.
    if live_process_pids([path], exclude=(os.getpid(),)).get(path):
        logger.info("subagent worktree: a live process works in %s; keeping it", path)
        return payload
    try:
        ref, _kept, removed, why = archive_then_remove(
            info.get("repo_root", "") or path, path, reason="subagent finished",
            max_untracked_mb=settings["archive_max_file_mb"], unlock=False, keep_branch=True,
            exclude_pids=(os.getpid(),))
    except (ArchiveError, OSError, subprocess.SubprocessError) as exc:
        logger.warning("subagent worktree: archive failed, keeping %s: %s", path, exc)
        return payload
    payload["archive"] = ref
    payload["pruned"] = removed
    if removed:
        payload["note"] = (f"Worktree archived to {ref} and removed; branch {info.get('branch')} kept. "
                           f"Restore the full tree (including uncommitted edits) with "
                           f"`hermes worktree restore {ref.rsplit('/', 1)[-1]}`.")
    else:
        payload["note"] = (f"Worktree archived to {ref} but kept at {path} ({why}); "
                           f"branch {info.get('branch')} kept.")
    return payload


def build_worktree_context_note(info: dict[str, str]) -> str:
    """Context block telling the child to work inside its isolated worktree."""
    return (
        "\n\n[WORKTREE ISOLATION] You are working in an isolated git worktree "
        f"at: {info.get('path')}\n"
        f"Your dedicated branch is: {info.get('branch')}\n"
        "All file edits and shell commands must happen inside this worktree directory (your "
        "terminal already starts there). Do NOT cd to the main repository checkout. Commit your "
        "changes to your branch when done; the parent agent will review and merge your branch. If "
        "you make no commits and leave the tree clean, the worktree is discarded automatically; "
        "otherwise it is archived (restorable) and removed when you finish."
    )
