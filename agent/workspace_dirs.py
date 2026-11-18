"""Session-scoped additional workspace roots (``/add-dir``, ``/remove-dir``, ``/dirs``).

A session has ONE working directory (``agent.runtime_cwd``) and zero or more additional workspace
roots. A root does not move the session's cwd, ``TERMINAL_CWD`` or its terminal: it widens where
the agent may work, and the system prompt lists every root under ``<workspace-roots>``
(``agent/prompts/system/project-prompt.md``) so the model reaches them through absolute paths.

The list is persisted per session (``sessions.workspace_roots``, a JSON array) and cached here
under the same profile-qualified session key the terminal/file tools use. Slash handlers write
the store and invalidate the session's prompt; the prompt build only reads it.
"""

from __future__ import annotations

import logging
import os
import posixpath
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agent.i18n import t

logger = logging.getLogger("hermes.workspace_dirs")

# session key -> ordered roots; ``_loaded`` marks keys whose store has already been read, so a
# session with no roots costs one read per process, not one per prompt build.
_roots: dict[str, tuple[str, ...]] = {}
_loaded: set[str] = set()
_lock = threading.Lock()

_REMOTE_BACKENDS = frozenset({"ssh", "docker", "modal", "daytona", "vercel_sandbox"})


@dataclass(frozen=True)
class DirResult:
    """A slash command's reply plus whether the stored workspace actually changed."""

    text: str
    changed: bool = False


def _key(session_id: Optional[str]) -> str:
    from tools.terminal_tool import _qualify_task_key
    return _qualify_task_key(str(session_id or "default"))


def _norm(path: str | Path) -> str:
    """Comparison form: case-folded on Windows, trailing separators dropped."""
    return os.path.normcase(os.path.normpath(str(path)))


def _resolved_cwd(cwd: str | Path | None) -> str:
    if cwd is not None:
        return str(cwd)
    from agent.runtime_cwd import resolve_agent_cwd
    return str(resolve_agent_cwd())


def _is_remote(task_id: str) -> bool:
    from tools.file_tools_paths import _terminal_env_type_for_task
    return _terminal_env_type_for_task(task_id or "default") in _REMOTE_BACKENDS


def resolve_root(raw: str, *, cwd: str | Path | None, session_id: Optional[str]) -> str:
    """Absolute form of *raw* for this session's backend.

    A host backend resolves against *cwd* and requires a real directory (the same check omp
    makes). A remote/container backend keeps the declared path: it names a directory inside the
    sandbox or on the peer, which this process must not stat.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError(t("workspace_dirs.usage_add"))
    if _is_remote(str(session_id or "default")):
        if text.startswith("~") or posixpath.isabs(text):
            return posixpath.normpath(text)
        return posixpath.normpath(posixpath.join(str(cwd or "/"), text))
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        path = Path(_resolved_cwd(cwd)) / path
    try:
        path = path.resolve()
    except OSError:
        pass
    if not path.is_dir():
        raise ValueError(t("workspace_dirs.not_found", path=str(path)))
    return str(path)


def load_roots(session_id: Optional[str], *, db: Any = None) -> tuple[str, ...]:
    """Stored roots for *session_id*, read from the session store on the first call."""
    key = _key(session_id)
    with _lock:
        if key in _loaded:
            return _roots.get(key, ())
    roots: tuple[str, ...] = ()
    if db is not None and session_id:
        try:
            roots = tuple(str(root) for root in (db.get_session_workspace_roots(session_id) or ()) if root)
        except Exception:  # health: allow BLE001 -- an unreadable store reads as "no roots"
            logger.debug("workspace roots: session row read failed (session=%s)", session_id, exc_info=True)
    with _lock:
        _roots[key] = roots
        _loaded.add(key)
    return roots


def _store(session_id: Optional[str], roots: tuple[str, ...], *, db: Any = None) -> None:
    key = _key(session_id)
    with _lock:
        _roots[key] = roots
        _loaded.add(key)
    if db is None or not session_id:
        return
    try:
        db.update_session_workspace_roots(session_id, list(roots))
    except Exception:  # health: allow BLE001 -- the live session still sees the roots in memory
        logger.warning("workspace roots: could not persist session %s", session_id, exc_info=True)


def clear_cache(session_id: Optional[str] = None) -> None:
    """Drop the process cache (all sessions, or one) — session teardown and tests."""
    with _lock:
        if session_id is None:
            _roots.clear()
            _loaded.clear()
            return
        key = _key(session_id)
        _roots.pop(key, None)
        _loaded.discard(key)


def prompt_roots(session_id: Optional[str], *, db: Any = None, cwd: str | Path | None = None) -> list[str]:
    """Roots the ``<workspace-roots>`` block lists: stored order minus the working directory."""
    base = ""
    if cwd is not None:
        base = _norm(Path(str(cwd)).resolve())
    return [root for root in load_roots(session_id, db=db) if not base or _norm(root) != base]


def format_listing(cwd: str | Path | None, roots: tuple[str, ...] | list[str]) -> str:
    """``/dirs`` body: the working directory first, then every additional root."""
    lines = [t("workspace_dirs.listing_header")]
    if cwd:
        lines.append(t("workspace_dirs.listing_cwd", path=str(cwd)))
    lines.extend(t("workspace_dirs.listing_root", path=str(root)) for root in roots)
    return "\n".join(lines)


def add_dir(
    session_id: Optional[str], arg: str, *, cwd: str | Path | None = None, db: Any = None,
) -> DirResult:
    """``/add-dir <path>``: validate, dedupe, persist; the reply lists the session's roots."""
    roots = load_roots(session_id, db=db)
    resolved_cwd = _resolved_cwd(cwd)
    if not str(arg or "").strip():
        return DirResult(f"{t('workspace_dirs.usage_add')}\n{format_listing(resolved_cwd, roots)}")
    try:
        path = resolve_root(arg, cwd=resolved_cwd, session_id=session_id)
    except ValueError as exc:
        return DirResult(str(exc))
    if _norm(path) == _norm(resolved_cwd):
        return DirResult(t("workspace_dirs.already_cwd"))
    if any(_norm(root) == _norm(path) for root in roots):
        return DirResult(
            f"{t('workspace_dirs.already', path=path)}\n{format_listing(resolved_cwd, roots)}"
        )
    updated = (*roots, path)
    _store(session_id, updated, db=db)
    return DirResult(
        f"{t('workspace_dirs.added', path=path)}\n{format_listing(resolved_cwd, updated)}",
        changed=True,
    )


def remove_dir(
    session_id: Optional[str], arg: str, *, cwd: str | Path | None = None, db: Any = None,
) -> DirResult:
    """``/remove-dir <path>``: drop a root (never the working directory)."""
    roots = load_roots(session_id, db=db)
    resolved_cwd = _resolved_cwd(cwd)
    if not str(arg or "").strip():
        return DirResult(f"{t('workspace_dirs.usage_remove')}\n{format_listing(resolved_cwd, roots)}")
    try:
        path = resolve_root(arg, cwd=resolved_cwd, session_id=session_id)
    except ValueError as exc:
        return DirResult(str(exc))
    if _norm(path) == _norm(resolved_cwd):
        return DirResult(t("workspace_dirs.cwd_removal"))
    remaining = tuple(root for root in roots if _norm(root) != _norm(path))
    if len(remaining) == len(roots):
        return DirResult(
            f"{t('workspace_dirs.missing', path=path)}\n{format_listing(resolved_cwd, roots)}"
        )
    _store(session_id, remaining, db=db)
    return DirResult(
        f"{t('workspace_dirs.removed', path=path)}\n{format_listing(resolved_cwd, remaining)}",
        changed=True,
    )


def list_dirs(
    session_id: Optional[str], *, cwd: str | Path | None = None, db: Any = None,
) -> str:
    """``/dirs``: the working directory plus every additional root, in stored order."""
    return format_listing(_resolved_cwd(cwd), load_roots(session_id, db=db))


__all__ = [
    "DirResult",
    "add_dir",
    "clear_cache",
    "format_listing",
    "list_dirs",
    "load_roots",
    "prompt_roots",
    "remove_dir",
    "resolve_root",
]
