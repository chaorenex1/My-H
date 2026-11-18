"""Session-persistent Bun kernels for ``execute_code(language="js")`` (ADR-4 D9).

One Bun child per ``(owner, "js", bun binary, cwd)``. Cells arrive on the child's
stdin as JSON lines; state survives across cells because the runner evaluates
each cell through indirect eval in its own global scope and the parser-level
transform publishes top-level bindings to ``globalThis``.

Shared with the Python kernel (``tools.code_kernel``): owner resolution, the
registry/LRU/idle bounds, the tool-RPC server, the frame pump and the cell
result finishing (redaction, truncation, ``--- stderr ---``, timeout text). The
runaway-cell contract is the same: a timed-out/interrupted cell kills the
process and drops its VM state.

Bun is supplied by PM only (``pm.installed_package("bun")``, locked version);
this module never reads ``PATH``/``~/.bun``.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from tools.code_kernel import (
    CellAuthority,
    KernelRegistry,
    SessionKernel,
    _IS_WINDOWS,
    _acquire_kernel,
    _await_cell,
    _bind_rpc_socket,
    _cell_result,
    _ensure_background_reaper,
    _pump_frames,
    _resolve_owner,
    _rpc_forever,
    _stderr_reader,
)

logger = logging.getLogger(__name__)

_RUNNER_PATH = Path(__file__).with_name("eval_js_runner.ts")


class JsKernel(SessionKernel):
    """One live Bun runner plus its RPC server and reader threads.

    The Python kernel's lifecycle (alive/dead/teardown) is identical; only the
    spawn path, wire protocol and stream frames differ.
    """

    def __init__(self, key: tuple):
        super().__init__(key)
        self.stdin_lock = threading.Lock()
        # Stream frames are consumed by D14's tool.output channel; until then they
        # are dropped after parsing (the result frame remains authoritative).
        self.stream_cb: Optional[Callable[[dict], None]] = None


_REGISTRY = KernelRegistry(lambda kernel: kernel.teardown())
_JS_KERNELS: dict[tuple, JsKernel] = _REGISTRY.kernels


def _js_stdout_reader(kernel: JsKernel) -> None:
    """Merge the runner's stdout: result frames settle the cell, stream frames
    feed the optional live channel, everything else is raw fd output."""
    from tools.code_execution_tool import MAX_STDOUT_BYTES
    assert kernel.proc is not None and kernel.proc.stdout is not None

    def raw(data: bytes) -> None:
        kernel.raw.append(data, MAX_STDOUT_BYTES)
        kernel.emit_stream_raw(data)

    def on_frame(payload: dict) -> None:
        kind = payload.get("type") if isinstance(payload, dict) else None
        if kind == "stream":
            if kernel.stream_cb is not None:
                try:
                    kernel.stream_cb(payload)
                except Exception:
                    logger.debug("JS stream callback failed", exc_info=True)
            return
        if kind == "ready":
            return
        kernel.response_q.put(payload)

    _pump_frames(kernel.proc.stdout, ("\n" + kernel.sentinel + " ").encode("utf-8"),
                 raw, on_frame)


def _js_child_env(*, rpc_endpoint: str, rpc_token: str, tmpdir: str) -> dict[str, str]:
    """The shared scrub plus Bun's own knobs: no inherited install root/cache or
    NODE_OPTIONS, HOME and the install cache pinned to the kernel staging dir.

    Built from ``_scrub_child_env`` directly (not ``_build_child_env``): the JS
    child has no PYTHONPATH/interpreter contract, and the Python builder's
    runtime-venv probe reads PM state that the kernel does not need.
    """
    from agent.session_client_env import tool_env, toolchain_environ
    from tools.code_execution_env import _scrub_child_env
    child_env = _scrub_child_env(toolchain_environ(), toolchain=frozenset(tool_env()))
    child_env["HERMES_RPC_SOCKET"] = rpc_endpoint
    child_env["HERMES_RPC_TOKEN"] = rpc_token
    for key in list(child_env):
        if key == "NODE_OPTIONS" or key.startswith("BUN_"):
            child_env.pop(key, None)
    cache = os.path.join(tmpdir, "bun-install-cache")
    child_env["BUN_INSTALL_CACHE_DIR"] = cache
    child_env["HOME"] = tmpdir
    child_env["USERPROFILE"] = tmpdir
    return child_env


def _spawn(kernel: JsKernel, *, bun_path: str, child_cwd: str,
           sandbox_tools: frozenset, max_tool_calls: int, task_id: str = "") -> None:
    if not _RUNNER_PATH.is_file():
        raise RuntimeError(
            "The Bun JS kernel runner is missing from this build "
            f"({_RUNNER_PATH.name}); reinstall Hermes or run from the source checkout.")
    kernel.tmpdir = tempfile.mkdtemp(prefix="hermes_kernel_js_")
    session_cwd = child_cwd or kernel.tmpdir
    kernel.rpc_token = secrets.token_urlsafe(32)
    kernel.sentinel = "@@HERMES-JS-" + secrets.token_urlsafe(16) + "@@"
    rpc_endpoint = _bind_rpc_socket(kernel)
    child_env = _js_child_env(rpc_endpoint=rpc_endpoint, rpc_token=kernel.rpc_token,
                              tmpdir=kernel.tmpdir)
    child_env["HERMES_JS_SENTINEL"] = kernel.sentinel
    # Start in the staging dir so Bun never reads the project's bunfig.toml
    # (--config cannot suppress preload); the runner chdirs to the session cwd
    # on init, so relative cell paths still resolve the way the user expects.
    from hermes_cli.process_identity import spawn_owned
    try:
        kernel.proc = spawn_owned(
            [bun_path, "--no-install", str(_RUNNER_PATH)], purpose="eval-js-kernel",
            cwd=kernel.tmpdir, env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
            close_fds=True,
        )
    except Exception:
        kernel.teardown()
        raise
    for target, args in ((_rpc_forever, (kernel, max_tool_calls, sandbox_tools)),
                         (_js_stdout_reader, (kernel,)), (_stderr_reader, (kernel,))):
        # Raw on purpose (not spawn_context_thread): the RPC thread must not freeze the
        # spawning cell's context — authority is rebound per cell in CellAuthority.
        threading.Thread(target=target, args=args, daemon=True).start()  # health: allow HX012 -- binding the spawning cell's context would freeze authority for the kernel's life
    _ensure_background_reaper(_REGISTRY)
    request = {"type": "init", "cwd": session_cwd, "tools": sorted(sandbox_tools)}
    try:
        from tools.eval_js_packages import managed_dir, managed_key
        request["packages"] = str(managed_dir(managed_key(child_cwd, task_id)))
    except Exception:  # health: allow BLE001 -- resolution falls back to the session cwd
        logger.debug("JS kernel: managed package root unavailable", exc_info=True)
    _write(kernel, request)


def _write(kernel: JsKernel, message: dict) -> None:
    assert kernel.proc is not None and kernel.proc.stdin is not None
    with kernel.stdin_lock:
        kernel.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        kernel.proc.stdin.flush()


def _run_cell(kernel: JsKernel, key: tuple, code: str, *, task_id: str, child_cwd: str,
              bun_path: str, sandbox_tools: frozenset, timeout: float, max_tool_calls: int,
              is_interrupted, exec_start: float, state_reset: bool,
              on_stream: Optional[Callable[[dict], None]] = None, cell_index: int = 0) -> str:
    reused = kernel.proc is not None
    authority = CellAuthority(task_id)
    if state_reset:
        try:
            from tools.eval_speculation import invalidate_owner
            invalidate_owner(task_id)
        except Exception:
            logger.debug("speculation invalidation failed", exc_info=True)
    with kernel.lock:
        try:
            if kernel.proc is None:
                _spawn(kernel, bun_path=bun_path, child_cwd=child_cwd,
                       sandbox_tools=sandbox_tools, max_tool_calls=max_tool_calls,
                       task_id=task_id)
            # Per-cell tool budget: the RPC loop enforces counter < max.
            kernel.tool_call_counter[0] = 0
            kernel.cell_log_start = len(kernel.tool_call_log)
            kernel.raw.drain(), kernel.stderr.drain()
            kernel.cell_authority = authority
            kernel.cell_display = authority.display
            kernel.stream_cb = on_stream
            kernel.stream_cell_index = cell_index
            _write(kernel, {"type": "cell", "id": uuid.uuid4().hex, "code": code,
                            "cell_index": cell_index})
            status, payload = _await_cell(kernel, timeout, is_interrupted)
            result = _cell_result(
                kernel, key, status, payload,
                timeout=timeout, sandbox_tools=sandbox_tools, reused=reused,
                state_reset=state_reset, exec_start=exec_start,
                discard=_REGISTRY.discard,
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            from tools.code_execution_tool import _error_result
            logger.error("JS session kernel failed: %s: %s", type(exc).__name__, exc, exc_info=True)
            _REGISTRY.discard(key, kernel)
            return _error_result(str(exc), tool_calls_made=kernel.tool_call_counter[0],
                                 duration=round(time.monotonic() - exec_start, 2))
        finally:
            kernel.stream_cb = None
            authority.retire()


def run_js_cell(code: str, *, task_id: str, child_cwd: str, sandbox_tools: frozenset,
                timeout: float, max_tool_calls: int, reset: bool, is_interrupted,
                on_stream: Optional[Callable[[dict], None]] = None, cell_index: int = 0) -> str:
    """Run one JS cell in the (owner, bun, cwd) kernel; returns the tool-result JSON."""
    from tools.code_execution_tool import _bun_binary, _error_result
    from agent.delegation_context import is_delegated_child_context
    bun_path = _bun_binary()
    if not bun_path:
        return _error_result("JavaScript needs Bun: hermes pm install bun")
    key = (_resolve_owner(task_id) or "", "js", bun_path, child_cwd)
    exec_start = time.monotonic()
    kernel, state_reset = _acquire_kernel(
        key, reset, pinned=is_delegated_child_context(),
        registry=_REGISTRY, factory=JsKernel,
    )
    try:
        return _run_cell(
            kernel, key, code, task_id=task_id, child_cwd=child_cwd, bun_path=bun_path,
            sandbox_tools=sandbox_tools, timeout=timeout, max_tool_calls=max_tool_calls,
            is_interrupted=is_interrupted, exec_start=exec_start, state_reset=state_reset,
            on_stream=on_stream, cell_index=cell_index,
        )
    finally:
        with _REGISTRY.lock:
            kernel.attached -= 1
            kernel.last_used = time.monotonic()
            orphaned = kernel.attached == 0 and _JS_KERNELS.get(key) is not kernel
        if orphaned:
            kernel.teardown()


def shutdown_js_kernels_for_owner(owner: str) -> None:
    """Dispose every JS kernel a session owns (``tools.approval.clear_session``)."""
    if owner:
        _REGISTRY.shutdown(owner)


def shutdown_js_kernels_where(matcher: Callable[[str], bool]) -> None:
    """Dispose JS kernels whose owner matches (delegated children)."""
    _REGISTRY.shutdown(owner_matches=matcher)


def shutdown_all_js_kernels() -> None:
    """Kill every JS kernel; atexit and tests."""
    _REGISTRY.shutdown()


atexit.register(shutdown_all_js_kernels)
