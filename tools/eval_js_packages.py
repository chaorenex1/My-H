"""Managed Bun package environment for ``execute_code(language="js")`` (ADR-4 D11.7).

Only the managed form exists: one cache directory under the Hermes home per session
cwd hash, containing ``package.json``/lock/``node_modules``. ``%bun add`` installs
there with the PM-locked Bun binary; the session's project tree is never touched and
the runner keeps ``--no-install`` (a missing module fails its import, it is not
fetched implicitly). ``%environment project`` is refused as ``unsupported_environment``.

Concurrent installs for one cwd serialize on a lock file; a second process waits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

_INSTALL_TIMEOUT = 180
_PACKAGE_RE = re.compile(r"^(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+(?:@[A-Za-z0-9._^~<>=*| -]+)?$")
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def managed_key(session_cwd: str, task_id: str = "") -> str:
    """Stable key for the managed root: session cwd, else the kernel owner (strict mode)."""
    if session_cwd:
        return str(session_cwd)
    try:
        from tools.code_kernel import _resolve_owner
        return _resolve_owner(task_id) or (task_id or "")
    except Exception:  # health: allow BLE001 -- identity resolution is best effort
        return task_id or ""


def managed_dir(session_cwd: str) -> Path:
    """The managed package root for one session cwd (stable across turns and processes)."""
    from hermes_constants import get_hermes_home
    digest = hashlib.sha256(str(session_cwd or "").encode("utf-8", errors="replace")).hexdigest()[:16]
    return get_hermes_home() / "cache" / "bun-managed" / digest


def parse_magic(code: str) -> Optional[tuple[str, Any]]:
    """``("bun_add", packages)`` / ``("environment", target)``, else ``None``.

    Only a cell whose first non-blank line is the magic is a magic cell; anything
    else is ordinary JavaScript, so a string containing ``%bun add`` is untouched.
    """
    if not isinstance(code, str):
        return None
    first = next((line.strip() for line in code.splitlines() if line.strip()), "")
    if not first.startswith("%"):
        return None
    parts = first.split()
    if parts[:2] == ["%bun", "add"]:
        return "bun_add", parts[2:]
    if parts[:2] == ["%bun", "install"]:
        return "bun_add", []
    if parts[0] == "%environment":
        return "environment", parts[1] if len(parts) > 1 else ""
    return None


def _validate_packages(packages: list[str]) -> Optional[str]:
    for package in packages:
        if not package or package.startswith("-") or not _PACKAGE_RE.match(package):
            return f"invalid package spec {package!r}"
    return None


@contextmanager
def _install_lock(root: Path) -> Iterator[None]:
    """Cross-process install lock (``flock``/``msvcrt``), with a process-local fallback."""
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".install.lock"
    key = str(lock_path)
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    with thread_lock:
        handle = None
        try:
            handle = open(lock_path, "a+b")
        except OSError as exc:
            logger.debug("bun managed: lock file unavailable: %s", exc)
        try:
            if handle is not None:
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except (ImportError, OSError) as exc:
                    logger.debug("bun managed: OS lock failed, using process lock: %s", exc)
            yield
        finally:
            if handle is not None:
                try:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                handle.close()


def _child_env(root: Path) -> dict[str, str]:
    from agent.session_client_env import tool_env, toolchain_environ
    from tools.code_execution_env import _scrub_child_env
    env = _scrub_child_env(toolchain_environ(), toolchain=frozenset(tool_env()))
    for key in list(env):
        if key == "NODE_OPTIONS" or key.startswith("BUN_"):
            env.pop(key, None)
    env["HOME"] = str(root)
    env["USERPROFILE"] = str(root)
    env["BUN_INSTALL_CACHE_DIR"] = str(root / ".bun-cache")
    return env


def _ensure_package_json(root: Path) -> None:
    path = root / "package.json"
    if path.is_file():
        return
    path.write_text(json.dumps({"name": "hermes-eval-managed", "private": True,
                                "version": "0.0.0"}, indent=2) + "\n", encoding="utf-8")


def _install_result(status: str, output: str, exit_code: int, root: Path) -> dict[str, Any]:
    return {
        "status": status,
        "output": output,
        "exit_code": exit_code,
        "managed_dir": str(root),
    }


def install_packages(packages: list[str], *, session_cwd: str, bun_path: str) -> dict[str, Any]:
    """Install *packages* into the managed root; never writes under *session_cwd*."""
    root = managed_dir(session_cwd)
    invalid = _validate_packages(packages)
    if invalid:
        return _install_result("error", invalid, 1, root)
    with _install_lock(root):
        _ensure_package_json(root)
        argv = [bun_path, "add", *packages]
        try:
            from hermes_cli.process_identity import spawn_owned
            proc = spawn_owned(
                argv, purpose="eval-bun-managed-install", cwd=str(root), env=_child_env(root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                close_fds=True,
            )
            try:
                output_bytes, _ = proc.communicate(timeout=_INSTALL_TIMEOUT)
            except subprocess.TimeoutExpired:
                from tools.code_execution_tool import _kill_process_group
                _kill_process_group(proc, escalate=True)
                return _install_result("error", f"bun add timed out after {_INSTALL_TIMEOUT}s", -1, root)
            output = (output_bytes or b"").decode("utf-8", errors="replace")
            return _install_result("success" if proc.returncode == 0 else "error", output,
                                   int(proc.returncode or 0), root)
        except Exception as exc:  # health: allow BLE001 -- reported as the cell result
            logger.error("bun managed install failed: %s", exc, exc_info=True)
            return _install_result("error", f"bun add failed: {type(exc).__name__}: {exc}", 1, root)


def run_magic(kind: str, payload: Any, *, session_cwd: str, bun_path: Optional[str]) -> dict[str, Any]:
    """Execute one ``%`` cell; ``%environment project`` is refused by design."""
    if kind == "environment":
        target = str(payload or "")
        if target in ("", "managed"):
            root = managed_dir(session_cwd)
            return {"status": "success", "output": f"managed environment: {root}", "exit_code": 0,
                    "managed_dir": str(root)}
        return {
            "status": "error",
            "error": "unsupported_environment",
            "output": (f"%environment {target}: project-level installs are not supported; "
                       "the managed environment is the only writable package root. "
                       "Install project dependencies with terminal()/a skill."),
            "exit_code": 1,
        }
    packages = list(payload or [])
    if not bun_path:
        return {"status": "error", "error": "JavaScript needs Bun: hermes pm install bun",
                "output": "JavaScript needs Bun: hermes pm install bun", "exit_code": 1}
    return install_packages(packages, session_cwd=session_cwd, bun_path=bun_path)
