#!/usr/bin/env python3
"""Code Execution Tool -- Programmatic Tool Calling (PTC).

The LLM writes a Python script that calls Hermes tools via RPC, collapsing
multi-step tool chains into one inference turn; only the script's stdout returns
to the LLM. Local backend: a persistent per-conversation session kernel
(tools/code_kernel.py) over a Unix socket (loopback TCP on Windows). Remote
backends: a remote session kernel (tools/code_kernel_remote.py) falling open to a
per-call script ship, tool calls as request files polled via env.execute()
(needs Python 3 on the backend). Siblings: tools/code_execution_env.py (env
scrubbing, interpreter/cwd), tools/code_execution_rpc.py (RPC servers).
"""

import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from tools.thread_context import propagate_context_to_thread
from tools.registry import registry, tool_error

from hermes_time import get_timezone_name
from tools.code_execution_env import _resolve_child_cwd, _resolve_child_python
from tools.code_execution_rpc import (
    _execute_checked, _private_dirs_cmd, _remote_write, _rpc_poll_loop, tool_errors_since,
)
from tools.tool_output_truncate import head_tail_split, truncation_notice

logger = logging.getLogger(__name__)

# Loopback TCP replaces AF_UNIX on Windows, so execute_code runs on every platform Hermes does.
SANDBOX_AVAILABLE = True

# Tools allowed inside the sandbox; ∩ the session's enabled tools decides which stubs are generated.
SANDBOX_ALLOWED_TOOLS = frozenset([
    "web_search", "web_extract", "read_file", "write_file", "search_files", "patch", "terminal",
])

# Resource limit defaults (overridable via config.yaml → code_execution.*)
DEFAULT_TIMEOUT = 300        # 5 minutes
DEFAULT_MAX_TOOL_CALLS = 50
DEFAULT_MAX_CELLS = 16
MAX_CELLS = 256              # hard ceiling: a model-supplied max_cells can't make one call huge
MAX_STDOUT_BYTES = 50_000    # 50 KB
MAX_STDERR_BYTES = 10_000    # 10 KB
# Hard ceiling on the spilled file (as web_tools' MAX_STORED_TEXT_CHARS): a runaway print loop must not fill the disk.
MAX_SPILLED_STDOUT_BYTES = 5_000_000


def _line_count(text: str) -> int:
    """Newline-delimited line count (a final unterminated line counts). ADR-4 D15."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _truncate_stdout_text(stdout_text: str) -> tuple[str, dict[str, Any]]:
    """Cap stdout by bytes (40% head / 60% tail) with explicit truncation metadata: byte counts
    ride alongside the textual marker because a client layer can miss or re-truncate it. The
    omitted middle is spilled to cache/exec and the result carries the path (recover-don't-rerun).
    ``stdout_lines_total``/``stdout_lines_shown`` are additive (ADR-4 D15) so the native card can
    draw an accurate truncation warning; ``stdout_lines_shown`` counts the represented fragments."""
    stdout_bytes = stdout_text.encode("utf-8", errors="replace")
    total = len(stdout_bytes)
    captured = min(total, MAX_STDOUT_BYTES)
    total_lines = _line_count(stdout_text)
    metadata: dict[str, Any] = {"stdout_truncated": total > captured, "stdout_bytes_captured": captured,
                                "stdout_bytes_total": total, "stdout_bytes_omitted": total - captured,
                                "stdout_lines_total": total_lines,
                                "stdout_lines_shown": total_lines}
    if total <= MAX_STDOUT_BYTES:
        return stdout_bytes.decode("utf-8", errors="replace"), metadata
    head_bytes, tail_bytes = head_tail_split(MAX_STDOUT_BYTES)
    head_text = stdout_bytes[:head_bytes].decode("utf-8", errors="replace")
    tail_text = stdout_bytes[-tail_bytes:].decode("utf-8", errors="replace")
    text = head_text + truncation_notice(total - captured, total, unit="bytes") + tail_text
    # Head and tail can be two fragments of the same long line, so clamp the represented count;
    # truncation itself is conveyed by the byte fields (truncatedBy="bytes"), not line arithmetic.
    represented = _line_count(head_text) + _line_count(tail_text)
    metadata["stdout_lines_shown"] = min(represented, total_lines)
    metadata["warning"] = ("execute_code stdout was truncated; the script did run, but only "
                           "the captured head/tail output is included. Re-run only with "
                           "narrower output if the omitted data is required.")
    spill_path = _spill_full_stdout(stdout_text)
    if spill_path:
        metadata["stdout_spill_path"] = spill_path
        metadata["warning"] = ("execute_code stdout was truncated (head/tail shown); the "
                               f"script did run. FULL output saved to {spill_path} — page it "
                               f'with read_file(path="{spill_path}", offset=...) instead of re-running.')
    return text, metadata


def strip_execute_code_display(result: str) -> str:
    """Drop the additive ``display`` block from the MODEL-facing result.

    The live UI reads it from ``tool.complete.result`` and the persisted row carries it in
    ``display_metadata``; leaving base64 images/views in the model copy would spend context
    and can push the result past the tool's result cap. Only ``execute_code`` JSON objects
    with a ``display`` key are rewritten; every other result is returned unchanged.
    """
    if not isinstance(result, str) or not result.startswith("{") or '"display"' not in result:
        return result
    try:
        body = json.loads(result)
    except (TypeError, ValueError):
        return result
    if not isinstance(body, dict) or "display" not in body:
        return result
    body.pop("display", None)
    return json.dumps(body, ensure_ascii=False)


def _spill_full_stdout(stdout_text: str) -> Optional[str]:
    """Write full stdout to cache/exec; return its path (None on failure — best-effort,
    the truncated inline output is still returned). Keyed by content digest so identical
    reruns coalesce; the dir rides the cache/web remote bind-mount list (credential_files)."""
    try:
        import hashlib
        from hermes_constants import get_hermes_dir
        from tools.spill_safety import write_text_exclusive
        if len(stdout_text) > MAX_SPILLED_STDOUT_BYTES:
            stdout_text = (stdout_text[:MAX_SPILLED_STDOUT_BYTES]
                           + f"\n\n[... spill capped at {MAX_SPILLED_STDOUT_BYTES:,} bytes ...]")
        cache_dir = get_hermes_dir("cache/exec", "exec_spill")
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(stdout_text.encode("utf-8", errors="replace")).hexdigest()[:12]
        path = cache_dir / f"stdout-{digest}.txt"
        write_text_exclusive(path, stdout_text, private=False, overwrite=True)
        return str(path)
    except Exception as exc:
        logger.debug("Failed to spill execute_code stdout: %s", exc)
        return None


def check_sandbox_requirements() -> bool:
    """check_fn: available unless the vercel_sandbox backend fails its own checks."""
    if not SANDBOX_AVAILABLE:
        return False
    try:
        from tools.terminal_tool import _get_env_config
        from tools.terminal_tool_backends import _check_vercel_sandbox_requirements
        config = _get_env_config()
    except Exception:
        logger.debug("Could not resolve terminal config for execute_code availability", exc_info=True)
        return False
    return config.get("env_type") != "vercel_sandbox" or _check_vercel_sandbox_requirements(config)


# ---- hermes_tools.py code generator ----

# Per-tool stub templates: (signature, docstring, args_dict_expr — the JSON payload sent over RPC).
_TOOL_STUBS = {
    "web_search": ("query: str, limit: int = 5",
        '"""Search the web. Returns dict with data.web list of {url, title, description}."""',
        '{"query": query, "limit": limit}'),
    "web_extract": ("urls: list, char_limit: int = None",
        '"""Extract content from URLs (no LLM summarization). Returns dict with results list of {url, title, content, error}. Pages over char_limit (default 15000) are head+tail truncated with the full text stored on disk; the content footer gives the path. content is markdown."""',
        '{"urls": urls, "char_limit": char_limit}'),
    "read_file": ("path: str, offset: int = 1, limit: int = 2000",
        '"""Read a file (1-indexed lines). Returns dict with "content" and "total_lines"."""',
        '{"path": path, "offset": offset, "limit": limit}'),
    "write_file": ("path: str, content: str, cross_profile: bool = False",
        '"""Write content to a file (always overwrites). Returns dict with status."""',
        '{"path": path, "content": content, "cross_profile": cross_profile}'),
    "search_files": ('pattern: str, target: str = "content", path: str = ".", file_glob: str = None, limit: int = 50, offset: int = 0, output_mode: str = "content", context: int = 0, order: str = "discovery"',
        '"""Search file contents (target="content") or find files by name (target="files"). Returns dict with "matches"."""',
        '{"pattern": pattern, "target": target, "path": path, "file_glob": file_glob, "limit": limit, "offset": offset, "output_mode": output_mode, "context": context, "order": order}'),
    "patch": ('path: str = None, old_string: str = None, new_string: str = None, replace_all: bool = False, mode: str = "replace", patch: str = None, cross_profile: bool = False',
        '"""Targeted find-and-replace (mode="replace") or V4A multi-file patches (mode="patch"). Returns dict with status."""',
        '{"path": path, "old_string": old_string, "new_string": new_string, "replace_all": replace_all, "mode": mode, "patch": patch, "cross_profile": cross_profile}'),
    "terminal": ("command: str, timeout: int = None, workdir: str = None",
        '"""Run a shell command (foreground only). Returns dict with "output" and "exit_code"."""',
        '{"command": command, "timeout": timeout, "workdir": workdir}'),
}

# Host ops (ADR-17 D6): not model tools, so they are absent from SANDBOX_ALLOWED_TOOLS but
# usable from a local execute_code script. ``__`` names are reserved and never hit the tool
# registry; a remote filesystem sandbox rejects them (local authority only).
_HOST_OP_STUBS = {
    "display": '''def display(value=None, *, kind=None, text=None, view=None, images=None):
    """Show a value on the eval card: prints its text for the model and records a view
    (json by default, markdown/table on request) plus optional images. Returns `value`."""
    if images is None and isinstance(value, dict) and "data" in value and ("mimeType" in value or "mime_type" in value):
        images, value = [value], None
    if view is None and value is not None:
        if kind == "markdown":
            view = {"kind": "markdown", "text": str(value)}
        elif kind == "table" and isinstance(value, list):
            rows = [list(row.values()) if isinstance(row, dict) else list(row) for row in value]
            head = list(value[0]) if value and isinstance(value[0], dict) else list(range(len(rows[0]) if rows else 0))
            view = {"kind": "table", "columns": [{"head": str(col)} for col in head],
                    "rows": [[str(cell) for cell in row] for row in rows]}
        else:
            try:
                safe_value = json.loads(json.dumps(value, default=str))
            except (TypeError, ValueError):
                safe_value = str(value)
            view = {"kind": "json", "value": safe_value}
    if text is None:
        text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str, ensure_ascii=False)
    if text:
        print(text)
    payload = {"images": images or []}
    if view is not None and text:
        payload["views"] = [{"text": text, "view": view}]
    _call("__display__", payload)
    return value
''',
    "status": '''def status(op, **fields):
    """Record one status event on the eval card (`op` names the operation)."""
    return _call("__status__", {"op": str(op), **fields})
''',
    "out": '''def out(text):
    """Send text to the current turn's reply surface (out://). No transcript row; fails
    outside an active turn."""
    return _call("__out__", {"text": str(text)})
''',
    "completion": '''class CompletionHandle:
    """Background one-shot auxiliary completion."""
    kind = "completion"

    def __init__(self, id):
        self.id = id

    def __repr__(self):
        return f"<completion {self.id}>"

    def status(self):
        snap = _call("__status__", {"item": {"kind": self.kind, "id": self.id}})
        return snap.get("status", "failed") if isinstance(snap, dict) else "failed"

    def done(self):
        return self.status() != "running"

    def wait(self, timeout=None):
        args = {"items": [{"kind": self.kind, "id": self.id}]}
        if timeout is not None:
            args["timeoutMs"] = max(0, float(timeout) * 1000)
        snap = (_call("__wait__", args).get("items") or [{}])[0]
        if snap.get("status") == "running":
            raise TimeoutError(f"completion {self.id} is still running")
        if snap.get("status") in ("failed", "cancelled"):
            raise RuntimeError(snap.get("error") or f"completion {self.id} failed")
        return snap.get("text", "")

    def cancel(self):
        result = _call("__cancel__", {"item": {"kind": self.kind, "id": self.id}})
        return bool(result.get("cancelled")) if isinstance(result, dict) else False

    def __await__(self):
        import asyncio
        return asyncio.to_thread(self.wait).__await__()


def completion(prompt, *, model="default", system=None, schema=None):
    """Start a stateless auxiliary completion and return its handle."""
    args = {"prompt": prompt, "model": model}
    if system is not None:
        args["system"] = system
    if schema is not None:
        args["schema"] = schema
    result = _call("__completion__", args)
    if not isinstance(result, dict) or not isinstance(result.get("id"), str):
        raise RuntimeError("completion() did not return a handle")
    return CompletionHandle(result["id"])
''',
    "agent": '''class AgentHandle:
    """Background subagent handle (named agent or delegated child)."""
    kind = "agent"

    def __init__(self, id, agent=None):
        self.id = id
        self.agent = agent
        self.handle = f"agent://{id}"

    def __repr__(self):
        return f"<agent {self.id} ({self.agent})>"

    def status(self):
        snap = _call("__status__", {"item": {"kind": self.kind, "id": self.id}})
        return snap.get("status", "failed") if isinstance(snap, dict) else "failed"

    def done(self):
        return self.status() != "running"

    def wait(self, timeout=None):
        args = {"items": [{"kind": self.kind, "id": self.id}]}
        if timeout is not None:
            args["timeoutMs"] = max(0, float(timeout) * 1000)
        snap = (_call("__wait__", args).get("items") or [{}])[0]
        if snap.get("status") == "running":
            raise TimeoutError(f"agent {self.id} is still running")
        if snap.get("status") in ("failed", "cancelled"):
            raise RuntimeError(snap.get("error") or f"agent {self.id} failed")
        return snap.get("text") if "text" in snap else snap.get("result")

    def send(self, message):
        return _call("__send__", {"id": self.id, "message": str(message)})

    def cancel(self):
        result = _call("__cancel__", {"item": {"kind": self.kind, "id": self.id}})
        return bool(result.get("cancelled")) if isinstance(result, dict) else False

    def __await__(self):
        import asyncio
        return asyncio.to_thread(self.wait).__await__()


def wait(handles, timeout=None, *, raise_errors=True):
    """Wait for agent/completion handles in input order."""
    items = [handles] if isinstance(handles, (AgentHandle, CompletionHandle)) else list(handles)
    args = {"items": [{"kind": h.kind, "id": h.id} for h in items]}
    if timeout is not None:
        args["timeoutMs"] = max(0, float(timeout) * 1000)
    snapshots = _call("__wait__", args).get("items") or []
    results = []
    for handle, snap in zip(items, snapshots):
        status = snap.get("status")
        if status == "running":
            value = TimeoutError(f"{handle.kind} {handle.id} is still running")
        elif status in ("failed", "cancelled"):
            value = RuntimeError(snap.get("error") or f"{handle.kind} {handle.id} failed")
        else:
            value = snap.get("text") if "text" in snap else snap.get("result")
        results.append(value)
    if raise_errors:
        for value in results:
            if isinstance(value, Exception):
                raise value
    return results


def agent(prompt, *, agent=None, label=None, context=None, tools=None):
    """Start a background subagent and return its handle."""
    args = {"prompt": prompt}
    if agent is not None:
        args["agent"] = agent
    if label is not None:
        args["label"] = label
    if context is not None:
        args["context"] = context
    if tools is not None:
        args["tools"] = list(tools)
    result = _call("__agent__", args)
    if not isinstance(result, dict) or not isinstance(result.get("id"), str):
        raise RuntimeError((result or {}).get("error") or "agent() did not return a handle")
    return AgentHandle(result["id"], result.get("agent"))


class WorkPool:
    """Pool of background subagents fed through the host workpool bridge."""
    kind = "workpool"

    def __init__(self, name, agent, limit):
        self.name = name
        self.agent = agent
        self.limit = limit

    def push(self, *items):
        if not all(isinstance(item, str) for item in items):
            raise TypeError("WorkPool.push() expects string items")
        result = _call("__workpool__", {"op": "push", "name": self.name, "items": list(items)})
        return result.get("ids", []) if isinstance(result, dict) else []

    def status(self):
        return _call("__workpool__", {"op": "status", "name": self.name})

    def peek(self):
        return _call("__workpool__", {"op": "peek", "name": self.name})

    def close(self):
        return _call("__workpool__", {"op": "close", "name": self.name})

    def __repr__(self):
        return f"<workpool {self.name} ({self.agent})>"


def workpool(agent=None, *, name=None, context=None, tools=None, limit=None):
    """Create a pool of background subagents."""
    args = {"op": "create"}
    if agent is not None:
        args["agent"] = agent
    if name is not None:
        args["name"] = name
    if context is not None:
        args["context"] = context
    if tools is not None:
        args["tools"] = list(tools)
    if limit is not None:
        args["limit"] = int(limit)
    result = _call("__workpool__", args)
    if not isinstance(result, dict) or not isinstance(result.get("name"), str):
        raise RuntimeError("workpool() did not return a pool")
    return WorkPool(result["name"], result.get("agent"), result.get("limit"))
''',
    "judge": '''def judge(state, *, questions=None, questions_file=None, contexts=None, labels=None,
          images=None, purpose=None, budget_usd=None, timeout_ms=None):
    """Answer one typed question set about `state` (ADR-17). Returns the v1 result object
    {answers, kind, api, provider, model, usage, cost_usd, cached_questions}; a failed
    judgment returns {"error": {"type", "message"}}. Never becomes a transcript row."""
    payload = {"state": state, "questions": questions, "questions_file": questions_file,
               "contexts": contexts, "labels": labels, "images": images,
               "purpose": purpose, "budget_usd": budget_usd, "timeout_ms": timeout_ms}
    return _call("__judge__", {k: v for k, v in payload.items() if v is not None})
''',
    "judge_batch": '''def judge_batch(items, *, questions=None, questions_file=None, contexts=None,
                labels=None, min_ok=None, max_concurrency=None, purpose=None,
                budget_usd=None, timeout_ms=None):
    """Answer one question set over many states with host-owned bounded fan-out (ADR-17).
    Returns {results: [{key, ok, result|error}], min_ok_met}; a failed item is reported,
    never raised. Questions are shared by the whole run."""
    payload = {"items": items, "questions": questions, "questions_file": questions_file,
               "contexts": contexts, "labels": labels, "min_ok": min_ok,
               "max_concurrency": max_concurrency, "purpose": purpose,
               "budget_usd": budget_usd, "timeout_ms": timeout_ms}
    return _call("__judge_batch__", {k: v for k, v in payload.items() if v is not None})
''',
}


def _missing_hermes_tools_import_hint(m, enabled_tools) -> str:
    missing = m.group(1)
    if missing in {"json_parse", "shell_quote", "retry"}:
        return (f"Import helpers with `from hermes_tools import {missing}`. "
                "If that import failed, the generated module may be stale or another "
                "hermes_tools may be first on sys.path. Check hermes_tools.__file__ "
                "and retry with reset=true.")
    available = sorted(SANDBOX_ALLOWED_TOOLS & set(enabled_tools or SANDBOX_ALLOWED_TOOLS))
    return (f"'{missing}' is not available inside the execute_code sandbox. "
            f"Importable tools here: {', '.join(available)}. For anything "
            "else, use the normal tool call instead of execute_code.")


# (regex, formatter(match, enabled_tools)) — first match wins. Production mining (state.db) ranked
# these as the top execute_code failure classes: hermes_tools import misuse, missing helper
# imports, treating tool results as strings, importing third-party packages absent from the sandbox.
_FAILURE_HINT_RULES = (
    (r"cannot import name '(\w+)' from 'hermes_tools'", _missing_hermes_tools_import_hint),
    (r"NameError: name '(json_parse|shell_quote|retry)' is not defined",
     lambda m, _: f"Import {m.group(1)} before calling it: "
                  f"from hermes_tools import {m.group(1)}"),
    (r"ModuleNotFoundError: No module named '([\w.]+)'",
     lambda m, _: f"'{m.group(1)}' is not installed in the sandbox interpreter. "
                  "Use Python stdlib inside execute_code, or run the code via "
                  "terminal() with the project venv's python instead."),
    (r"TypeError: string indices must be integers|AttributeError: 'str' object has no attribute 'get'",
     lambda m, _: "Tool functions in the sandbox return DICTS (already parsed) — "
                  "do not json.loads() them or index them like strings. Example: read_file(path)['content']."),
)


def _sandbox_failure_hint(stderr_text: str, enabled_tools=None) -> Optional[str]:
    """Map well-known sandbox script failures to one actionable recovery hint
    (bounded scan, first match wins, never raises)."""
    if not stderr_text:
        return None
    window = stderr_text[:4000]
    try:
        for pattern, fmt in _FAILURE_HINT_RULES:
            m = re.search(pattern, window)
            if m:
                return fmt(m, enabled_tools)
    except Exception:
        return None
    return None


def generate_hermes_tools_module(enabled_tools: list[str],
                                 transport: str = "uds") -> str:
    """Source of the hermes_tools.py stub module for SANDBOX_ALLOWED_TOOLS ∩ *enabled_tools*.
    ``transport``: ``"uds"`` (local socket client) or ``"file"`` (file RPC, remote backends)."""
    header = _FILE_TRANSPORT_HEADER if transport == "file" else _UDS_TRANSPORT_HEADER
    stubs = "\n".join(
        f"def {name}({sig}):\n    {doc}\n    return _call({name!r}, {args_expr})\n"
        for name, (sig, doc, args_expr) in sorted(_TOOL_STUBS.items()) if name in set(enabled_tools)
    )
    host_ops = "".join(_HOST_OP_STUBS[name] + "\n" for name in sorted(_HOST_OP_STUBS))
    return header + stubs + "\n" + host_ops


# ---- Shared helpers section (embedded in both transport headers) ----------

_COMMON_HELPERS = '''\

# ---------------------------------------------------------------------------
# Convenience helpers (avoid common scripting pitfalls)
# ---------------------------------------------------------------------------

def json_parse(text: str):
    """Parse JSON tolerant of control characters and UTF-8 BOM (strict=False).
    Use this instead of json.loads() when parsing output from terminal()
    or web_extract() that may contain raw tabs/newlines in strings,
    or from tools/files that prepend a UTF-8 BOM (salvage #57870, credit @woxinwuhen713-bit)."""
    if isinstance(text, str) and text.startswith("\ufeff"):
        text = text[1:]
    return json.loads(text, strict=False)


def shell_quote(s: str) -> str:
    """Shell-escape a string for safe interpolation into commands.
    Use this when inserting dynamic content into terminal() commands:
        terminal(f"echo {shell_quote(user_input)}")
    """
    return shlex.quote(s)


def retry(fn, max_attempts=3, delay=2):
    """Retry a function up to max_attempts times with exponential backoff.
    Use for transient failures (network errors, API rate limits):
        result = retry(lambda: terminal("gh issue list ..."))
    """
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(delay * (2 ** attempt))
    raise last_err

'''

# ---- UDS transport (local backend) ---------------------------------------

_UDS_TRANSPORT_HEADER = '''\
"""Auto-generated Hermes tools RPC stubs."""
import json, os, socket, shlex, threading, time

_sock = None
# The RPC server handles a single client connection serially and has no
# request-id in the protocol, so concurrent _call() invocations from multiple
# threads (e.g. ThreadPoolExecutor) would race on the shared socket and get
# each other's responses. Serialize the entire send+recv round-trip.
_call_lock = threading.Lock()
''' + _COMMON_HELPERS + '''\

def _connect():
    """Connect to the parent's RPC server via the transport it picked.

    HERMES_RPC_SOCKET can be either:
      - a filesystem path (POSIX Unix domain socket — the default on
        Linux and macOS)
      - a string of the form ``tcp://127.0.0.1:<port>`` (Windows, where
        AF_UNIX is unreliable — the parent falls back to loopback TCP)
    """
    global _sock
    if _sock is None:
        endpoint = os.environ["HERMES_RPC_SOCKET"]
        if endpoint.startswith("tcp://"):
            # tcp://host:port  (host is always 127.0.0.1 in practice — we
            # only bind loopback server-side)
            _host_port = endpoint[len("tcp://"):]
            _host, _, _port = _host_port.rpartition(":")
            _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _sock.connect((_host or "127.0.0.1", int(_port)))
        else:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            _sock.connect(endpoint)
        _sock.settimeout(300)
    return _sock

def _call(tool_name, args):
    """Send a tool call to the parent process and return the parsed result."""
    request = json.dumps({
        "tool": tool_name,
        "args": args,
        "token": os.environ.get("HERMES_RPC_TOKEN", ""),
    }) + "\\n"
    # Session kernels outlive the RPC server's 300s idle window, so their
    # connection can be legitimately gone by the next cell. The server
    # re-accepts (HERMES_RPC_PERSISTENT=1); retry once on a fresh socket.
    _attempts = 2 if os.environ.get("HERMES_RPC_PERSISTENT") == "1" else 1
    with _call_lock:
        for _attempt in range(_attempts):
            try:
                conn = _connect()
                conn.sendall(request.encode())
                buf = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise RuntimeError("Agent process disconnected")
                    buf += chunk
                    if buf.endswith(b"\\n"):
                        break
                break
            except (OSError, RuntimeError):
                global _sock
                try:
                    if _sock is not None:
                        _sock.close()
                except OSError:
                    pass
                _sock = None
                if _attempt + 1 >= _attempts:
                    raise
    raw = buf.decode().strip()
    result = json.loads(raw)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

'''

# ---- File-based transport (remote backends) -------------------------------

_FILE_TRANSPORT_HEADER = '''\
"""Auto-generated Hermes tools RPC stubs (file-based transport)."""
import json, os, shlex, tempfile, threading, time

_RPC_DIR = os.environ.get("HERMES_RPC_DIR") or os.path.join(tempfile.gettempdir(), "hermes_rpc")
_seq = 0
# `_seq += 1` is not atomic (read-modify-write), so concurrent _call()
# invocations from multiple threads could allocate the same sequence number
# and clobber each other's request files. Guard seq allocation with a lock.
_seq_lock = threading.Lock()
''' + _COMMON_HELPERS + '''\

def _call(tool_name, args):
    """Send a tool call request via file-based RPC and wait for response."""
    global _seq
    with _seq_lock:
        _seq += 1
        seq = _seq
    seq_str = f"{seq:06d}"
    req_file = os.path.join(_RPC_DIR, f"req_{seq_str}")
    res_file = os.path.join(_RPC_DIR, f"res_{seq_str}")

    # Write request atomically (write to .tmp, then rename).
    # encoding="utf-8" is critical: on Windows-hosted remote backends
    # (or any non-UTF-8 locale) the default open() mode would mangle
    # non-ASCII chars in tool args when encoding them as JSON.
    tmp = req_file + ".tmp"
    # The request carries the RPC token and tool args: owner-only even under a
    # permissive process umask.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({
            "tool": tool_name,
            "args": args,
            "seq": seq,
            "token": os.environ.get("HERMES_RPC_TOKEN", ""),
        }, f)
    os.rename(tmp, req_file)

    # Wait for response with adaptive polling
    deadline = time.monotonic() + 300  # 5-minute timeout per tool call
    poll_interval = 0.05  # Start at 50ms
    while not os.path.exists(res_file):
        if time.monotonic() > deadline:
            raise RuntimeError(f"RPC timeout: no response for {tool_name} after 300s")
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.2, 0.25)  # Back off to 250ms

    with open(res_file, encoding="utf-8") as f:
        raw = f.read()

    # Clean up response file
    try:
        os.unlink(res_file)
    except OSError:
        pass

    result = json.loads(raw)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

'''


# ---- Remote execution support (file-based RPC via terminal backend) ----

def _get_or_create_env(task_id: str):
    """``(env, env_type)`` — the environment the terminal/file tools share for *task_id*, created on
    first use (same double-checked per-task lock pattern as file_tools._get_file_ops)."""
    from tools.terminal_tool_backends import (_container_config_from_config, _create_environment,
                                              _local_config_from_config, _ssh_config_from_config)
    from tools.terminal_tool import (
        _active_environments, _env_lock, _get_env_config, _last_activity,
        _start_cleanup_thread, _creation_locks, _creation_locks_lock, _task_env_overrides,
        _resolve_container_task_id, _resolve_task_host_cwd, _is_container_backend, _select_image,
    )
    effective_task_id = _resolve_container_task_id(task_id)
    def _cached():
        with _env_lock:
            env = _active_environments.get(effective_task_id)
            if env is not None:
                _last_activity[effective_task_id] = time.time()
        return env
    env = _cached()
    if env is not None:
        return env, _get_env_config()["env_type"]
    with _creation_locks_lock:
        task_lock = _creation_locks.setdefault(effective_task_id, threading.Lock())
    with task_lock:
        env = _cached()
        if env is not None:
            return env, _get_env_config()["env_type"]
        config = _get_env_config()
        env_type = config["env_type"]
        overrides = _task_env_overrides.get(effective_task_id, {})
        container_config = None
        if _is_container_backend(env_type):
            # Shared shaper: execute_code's own key subset dropped docker_extra_args / docker_forward_env /
            # docker_env, so a sandbox created from this path lost the operator's configured settings.
            container_config = _container_config_from_config(config)
        logger.info("Creating new %s environment for execute_code task %s...",
                     env_type, effective_task_id[:8])
        env = _create_environment(
            env_type=env_type, image=_select_image(env_type, overrides, config),
            cwd=overrides.get("cwd") or config["cwd"], timeout=config["timeout"],
            ssh_config=_ssh_config_from_config(config) if env_type == "ssh" else None,
            container_config=container_config,
            local_config=_local_config_from_config(config) if env_type == "local" else None,
            task_id=effective_task_id, host_cwd=_resolve_task_host_cwd(config, task_id),
        )
        with _env_lock:
            _active_environments[effective_task_id] = env
            _last_activity[effective_task_id] = time.time()
        _start_cleanup_thread()
        logger.info("%s environment ready for execute_code task %s",
                     env_type, effective_task_id[:8])
        return env, env_type


def _ship_file_to_remote(env, remote_path: str, content: str, *, atomic: bool = False) -> None:
    """Write *content* owner-only to *remote_path*; the payload rides
    ``stdin_data``, never an ``echo`` argv (see _remote_write). Raises on write
    failure: the caller ships secrets and code into dirs it believes are locked
    down, so a silent failure would run the next step against a missing or
    half-written file."""
    _remote_write(env, remote_path, content, atomic=atomic, check=True)


def _ship_env_file_and_launch(env, remote_dir: str, env_name: str, launch: str, *,
                              rpc_dir: str, rpc_token: str, **extra_env: str) -> str:
    """Ship the sandbox env (RPC dir + token, PYTHONDONTWRITEBYTECODE, the routed
    profile's TZ, plus *extra_env*) as KEY=value lines to ``remote_dir/env_name``
    and return the complete command that sources it inside a subshell and runs
    *launch* there.

    The subshell is load-bearing: every env.execute() runs inside the backend's
    session wrapper, which re-dumps ``export -p`` into the shared session
    snapshot after each command. A plain ``set -a; . file`` in the outer shell
    would export HERMES_RPC_TOKEN/PYTHONPATH/TZ into that snapshot and re-export
    them into every later command on the backend (the #71296 snapshot-leak
    class). The token also stays off the remote shell's argv, which co-tenant
    users can read via ps for the command's lifetime."""
    env_map = {"HERMES_RPC_DIR": rpc_dir, "HERMES_RPC_TOKEN": rpc_token,
               "PYTHONDONTWRITEBYTECODE": "1", **extra_env}
    tz = get_timezone_name()  # routed profile's timezone, not the bridged default's
    if tz:
        env_map["TZ"] = tz
    lines = "".join(f"{k}={shlex.quote(v)}\n" for k, v in env_map.items())
    _ship_file_to_remote(env, f"{remote_dir}/{env_name}", lines)
    return (f"cd {shlex.quote(remote_dir)} && "
            f"( set -a && . ./{env_name} && set +a && {launch} )")


def _env_temp_dir(env: Any) -> str:
    """Return a writable temp dir for env-backed execute_code sandboxes."""
    temp_dir = None
    get_temp_dir = getattr(env, "get_temp_dir", None)
    if callable(get_temp_dir):
        try:
            temp_dir = get_temp_dir()
        except Exception as exc:
            logger.debug("Could not resolve execute_code env temp dir: %s", exc)
    for candidate in (temp_dir, tempfile.gettempdir()):
        if isinstance(candidate, str) and candidate.startswith("/"):
            return candidate.rstrip("/") or "/"
    return tempfile.gettempdir()


def _format_interrupted_output(stdout_text: str) -> str:
    """Append an interruption marker without guessing who caused it."""
    from tools.interrupt import get_interrupt_reason
    reason = get_interrupt_reason()
    marker = f"[execution interrupted — {reason}]" if reason else "[execution interrupted]"
    return f"{stdout_text}\n{marker}" if stdout_text else marker


def _clean_output(stdout_text: str) -> tuple[str, dict[str, Any]]:
    """Shared output pipeline: byte-cap (with spill), ANSI strip, secret redaction. code_file=True:
    output often echoes source/config — skip ENV/JSON/f-string false positives, still mask credentials."""
    from tools.ansi_strip import strip_ansi
    from agent.redact import redact_sensitive_text
    stdout_text, metadata = _truncate_stdout_text(stdout_text)
    return redact_sensitive_text(strip_ansi(stdout_text), code_file=True), metadata


def _with_timeout_notice(stdout_text: str, timeout_msg: str) -> str:
    """Timeout message goes in the output too — an empty result makes models answer as if
    nothing happened, and the gateway drops empty replies."""
    return stdout_text + f"\n\n⏰ {timeout_msg}" if stdout_text else f"⏰ {timeout_msg}"


def _error_result(error: str, *, tool_calls_made: int = 0, duration: float = 0,
                  user_summary: Optional[str] = None) -> str:
    body = {"status": "error", "error": error, "tool_calls_made": tool_calls_made, "duration_seconds": duration}
    if user_summary:
        body["user_summary"] = user_summary  # one human sentence; surfaces show it before the model text
    return json.dumps(body, ensure_ascii=False)


def _remote_failure(exc: BaseException, exec_start: float, tool_calls_made: int) -> str:
    duration = round(time.monotonic() - exec_start, 2)
    logger.error("execute_code remote failed after %ss with %d tool calls: %s: %s",
                 duration, tool_calls_made, type(exc).__name__, exc, exc_info=True)
    return _error_result(str(exc), tool_calls_made=tool_calls_made, duration=duration)


_REMOTE_EXIT_STATUS = {124: "timeout", 130: "interrupted"}


def _remote_result(status: str, raw_stdout: str, exec_start: float, fields: dict[str, Any],
                   kernel: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Common remote reply shape: status, cleaned output, *fields*, duration, optional kernel
    info, then truncation metadata (key order is part of the result contract)."""
    stdout_text, stdout_metadata = _clean_output(raw_stdout)
    result: dict[str, Any] = {"status": status, "output": stdout_text, **fields,
                              "duration_seconds": round(time.monotonic() - exec_start, 2)}
    if kernel is not None:
        result["kernel"] = kernel
    result.update(stdout_metadata)
    return result


def _apply_timeout(result: dict[str, Any], timeout_msg: str) -> None:
    result["error"] = timeout_msg
    result["output"] = _with_timeout_notice(result["output"], timeout_msg)


def _finish_remote_kernel_result(kernel_result: dict[str, Any], *,
                                 timeout: int, exec_start: float) -> str:
    """Post-process a remote-kernel cell result into the tool's JSON reply. Timeout messaging
    mirrors the local kernel contract (kernel killed, state lost, next call fresh)."""
    stdout_text = kernel_result.get("stdout", "") or ""
    stderr_text = kernel_result.get("stderr", "") or ""
    traceback_text = kernel_result.get("traceback", "") or ""
    if stderr_text or traceback_text:
        # Same joining shape as the local kernel: stderr and traceback ride in
        # the output under one marker so the model sees the failure inline.
        stdout_text = stdout_text + "\n--- stderr ---\n" + stderr_text + traceback_text
    result = _remote_result(kernel_result.get("status", "error"), stdout_text, exec_start,
                            {"tool_calls_made": kernel_result.get("tool_calls_made", 0)},
                            kernel=kernel_result.get("kernel", {"remote": True}))
    if kernel_result.get("tool_errors"):
        result["tool_errors"] = kernel_result["tool_errors"]
    if result["status"] == "timeout":
        _apply_timeout(result, f"Cell timed out after {timeout}s; the remote session kernel was "
                               "killed and its state was lost. The next call starts fresh.")
    elif result["status"] == "error" and kernel_result.get("error"):
        result["error"] = kernel_result["error"]
    return json.dumps(result, ensure_ascii=False)


def _sandbox_tools_for(enabled_tools: Optional[list[str]]) -> frozenset:
    """Enabled ∩ SANDBOX_ALLOWED_TOOLS, or every sandbox tool when the intersection is empty."""
    return frozenset(SANDBOX_ALLOWED_TOOLS & set(enabled_tools or ())) or SANDBOX_ALLOWED_TOOLS


# ---- Cell surface: language, cells, timeout, JS capability (ADR-4 D8) ----

_LANGUAGE_ALIASES = {"py": "py", "python": "py", "js": "js", "javascript": "js"}


def _normalize_language(value: Any, *, default: str = "py") -> Optional[str]:
    """Canonical ``py``/``js`` for a model-supplied language; ``None`` when unsupported."""
    if value is None:
        return default
    if not isinstance(value, str):
        return None
    return _LANGUAGE_ALIASES.get(value.strip().lower())


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _positive_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _max_cells() -> int:
    """``code_execution.max_cells`` (default 16), hard-capped so config can't make a call unbounded."""
    return min(_positive_int(_load_config().get("max_cells"), DEFAULT_MAX_CELLS), MAX_CELLS)


def _bun_binary() -> Optional[str]:
    """PM's locked Bun binary (never PATH/~/.bun); ``None`` when it is not installed."""
    try:
        from pm.install import installed_package
        package = installed_package("bun")
    except Exception:
        logger.debug("PM Bun lookup failed", exc_info=True)
        return None
    if package is None or package.binary is None:
        return None
    try:
        if not package.binary.is_file():
            return None
    except OSError:
        return None
    return str(package.binary)


def _js_kernel_available() -> bool:
    """True when this build ships the Bun kernel module (ADR-4 P2)."""
    import importlib.util
    try:
        return importlib.util.find_spec("tools.eval_js_kernel") is not None
    except (ImportError, ValueError):
        return False


def js_backend_available() -> bool:
    """JS cells need PM's Bun binary and the JS kernel module in this build."""
    return _bun_binary() is not None and _js_kernel_available()


def _facts_signature(path) -> tuple:
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)
    except Exception:
        logger.debug("PM facts stat failed for %s", path, exc_info=True)
        return (str(path), None, None)


def js_capability_fingerprint() -> tuple:
    """Cheap memo-key component for the tool-definitions cache.

    ``hermes pm install bun`` rewrites PM's facts.json without touching config.yaml, so a
    long-lived gateway must still see the ``js`` enum on its next session (ADR-4 D8/acceptance 7).
    Two stats + the static kernel-module bit per key, not the full package lookup; the store
    paths resolve per active home/profile, never through a process-wide cache.
    """
    try:
        from pm import paths
        candidates = (paths.facts_path(), paths.writable_store_root() / "facts.json")
    except Exception:
        logger.debug("PM facts path probe failed", exc_info=True)
        candidates = ()
    return (tuple(_facts_signature(path) for path in candidates), _js_kernel_available())


def _run_js_cell(code: str, *, task_id: str, child_cwd: str, sandbox_tools: frozenset,
                 timeout: float, max_tool_calls: int, reset: bool, is_interrupted,
                 on_stream=None, cell_index: int = 0) -> str:
    """Dispatch one JS cell to the Bun session kernel (ADR-4 P2 owns the runner)."""
    from tools.eval_js_kernel import run_js_cell
    return run_js_cell(code, task_id=task_id, child_cwd=child_cwd, sandbox_tools=sandbox_tools,
                       timeout=timeout, max_tool_calls=max_tool_calls, reset=reset,
                       is_interrupted=is_interrupted, on_stream=on_stream, cell_index=cell_index)


def _run_local_cell(code: str, language: str, *, task_id: str, mode: str, child_python: str,
                    child_cwd: str, sandbox_tools: frozenset, timeout: float, max_tool_calls: int,
                    reset: bool, is_interrupted, on_stream=None, cell_index: int = 0) -> str:
    if language == "js":
        return _run_js_cell(code, task_id=task_id, child_cwd=child_cwd, sandbox_tools=sandbox_tools,
                            timeout=timeout, max_tool_calls=max_tool_calls, reset=reset,
                            is_interrupted=is_interrupted, on_stream=on_stream,
                            cell_index=cell_index)
    from tools.code_kernel import execute_in_session_kernel
    return execute_in_session_kernel(
        code, task_id=task_id, mode=mode, child_python=child_python, child_cwd=child_cwd,
        sandbox_tools=sandbox_tools, timeout=timeout, max_tool_calls=max_tool_calls,
        reset=reset, is_interrupted=is_interrupted, on_stream=on_stream, cell_index=cell_index,
    )


def _cell_entry(index: int, cell: dict, raw: str, cell_start: float) -> dict:
    """One ``cells[]`` item: the cell's identity plus the kernel result's keys unchanged."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = {"status": "error", "error": "execute_code produced an unreadable cell result.",
                  "output": str(raw)}
    if not isinstance(parsed, dict):
        parsed = {"status": "error", "output": str(parsed)}
    parsed.setdefault("duration_seconds", round(time.monotonic() - cell_start, 2))
    entry = {"index": index, "code": cell["code"], "language": cell["language"]}
    if cell.get("title") is not None:
        entry["title"] = cell["title"]
    entry.update(parsed)
    return entry


def _multi_cell_result(cells: list[dict], *, exec_start: float, title: Optional[str] = None) -> str:
    """Aggregate per-cell results: timeout/interrupted outrank error, which outranks success."""
    statuses = [cell.get("status") for cell in cells]
    if "interrupted" in statuses:
        status = "interrupted"
    elif "timeout" in statuses:
        status = "timeout"
    elif any(item != "success" for item in statuses):
        status = "error"
    else:
        status = "success"
    output = "\n\n".join(
        cell["output"] for cell in cells
        if isinstance(cell.get("output"), str) and cell["output"]
    )
    result: dict[str, Any] = {
        "status": status, "output": output, "cells": cells,
        "duration_seconds": round(time.monotonic() - exec_start, 2),
        "tool_calls_made": sum(int(cell.get("tool_calls_made") or 0) for cell in cells),
    }
    byte_fields = ("stdout_bytes_captured", "stdout_bytes_total", "stdout_bytes_omitted",
                   "stdout_lines_total", "stdout_lines_shown")
    if any(field in cell for cell in cells for field in byte_fields):
        for field in byte_fields:
            result[field] = sum(int(cell.get(field) or 0) for cell in cells)
        result["stdout_truncated"] = any(bool(cell.get("stdout_truncated")) for cell in cells)
    if title is not None:
        result["title"] = title
    return json.dumps(result, ensure_ascii=False)



def _run_remote_per_call(env, env_type: str, code: str, effective_task_id: str,
                         sandbox_tools: frozenset, *, timeout: int, max_tool_calls: int,
                         exec_start: float) -> str:
    """Per-call script ship: stage hermes_tools.py + script.py in a fresh remote sandbox dir,
    serve file-RPC from a polling thread, run, clean up."""
    sandbox_dir = f"{_env_temp_dir(env)}/hermes_exec_{uuid.uuid4().hex[:12]}"
    quoted_sandbox_dir = shlex.quote(sandbox_dir)
    tool_call_counter, tool_call_log, stop_event, rpc_thread = [0], [], threading.Event(), None
    try:
        # Private dirs: the sandbox lives under a shared temp dir and carries the
        # RPC token (in req files) and tool results. Fail closed on setup
        # failure rather than ship secrets into a dir that stayed permissive.
        _execute_checked(env, _private_dirs_cmd(sandbox_dir, f"{sandbox_dir}/rpc"),
                         "remote sandbox setup", timeout=10)
        rpc_token = secrets.token_urlsafe(32)
        _ship_file_to_remote(env, f"{sandbox_dir}/hermes_tools.py",
                             generate_hermes_tools_module(list(sandbox_tools), transport="file"))
        _ship_file_to_remote(env, f"{sandbox_dir}/script.py", code)
        # Wrapped so the thread inherits the turn's approval context + callbacks
        # (tools.thread_context) — else sandbox RPC tool calls lose approval routing.
        # See #30882.
        rpc_thread = threading.Thread(
            target=propagate_context_to_thread(_rpc_poll_loop), daemon=True,
            args=(env, f"{sandbox_dir}/rpc", effective_task_id, tool_call_log, tool_call_counter,
                  max_tool_calls, sandbox_tools, stop_event, rpc_token))
        rpc_thread.start()
        # The token travels in a sourced env file, never in argv. No umask on
        # the launch command: the 700 dirs + explicit 0600 writes cover Hermes'
        # files, and user code keeps the remote's default file modes.
        launch_cmd = _ship_env_file_and_launch(
            env, sandbox_dir, "sandbox.env", "exec python3 script.py",
            rpc_dir=f"{sandbox_dir}/rpc", rpc_token=rpc_token)
        logger.info("Executing code on %s backend (task %s)...", env_type, effective_task_id[:8])
        script_result = env.execute(launch_cmd, timeout=timeout)
        stdout_text = script_result.get("output", "") or ""
        exit_code = script_result.get("returncode", -1)
        # Backend exit codes: 124 = timeout wrapper, 130 = SIGINT.
        status = _REMOTE_EXIT_STATUS.get(exit_code, "success")
    except Exception as exc:
        return _remote_failure(exc, exec_start, tool_call_counter[0])
    finally:
        stop_event.set()
        if rpc_thread is not None:
            rpc_thread.join(timeout=5)
        try:
            env.execute(f"rm -rf {quoted_sandbox_dir}", cwd="/", timeout=15)
        except Exception:
            logger.debug("Failed to clean up remote sandbox %s", sandbox_dir)
    result = _remote_result(status, stdout_text, exec_start,
                            {"exit_code": exit_code, "tool_calls_made": tool_call_counter[0]})
    tool_errors = tool_errors_since(tool_call_log)
    if tool_errors:
        result["tool_errors"] = tool_errors
    if status == "timeout":
        _apply_timeout(result, f"Script timed out after {timeout}s and was killed.")
        logger.warning("execute_code (remote) timed out after %ss (limit %ss) with %d tool calls",
                       result["duration_seconds"], timeout, tool_call_counter[0])
    elif status == "interrupted":
        result["output"] = _format_interrupted_output(result["output"])
    elif exit_code != 0:
        result["status"] = "error"
        result["error"] = f"Script exited with code {exit_code}"
    return json.dumps(result, ensure_ascii=False)


def _execute_remote(code: str, task_id: Optional[str], enabled_tools: Optional[list[str]],
                    reset: bool = False, *, timeout: Optional[float] = None) -> str:
    """Run code on the remote terminal backend: the owner's persistent remote session kernel
    (tools/code_kernel_remote.py) first, else the per-call script ship — the fail-open route when
    a kernel cannot be spawned and the only route for hosts that cannot sustain a background process.
    ``timeout`` overrides ``code_execution.timeout`` for this call (capped by it)."""
    from tools.terminal_tool import _resolve_container_task_id
    from tools.terminal_tool_lifecycle import _call_in_flight
    _cfg = _load_config()
    cap_value = _positive_number(_cfg.get("timeout"), float(DEFAULT_TIMEOUT))
    cap = int(cap_value) if float(cap_value).is_integer() else cap_value
    timeout = cap if timeout is None else min(timeout, cap)
    max_tool_calls = _positive_int(_cfg.get("max_tool_calls"), DEFAULT_MAX_TOOL_CALLS)
    sandbox_tools, effective_task_id = _sandbox_tools_for(enabled_tools), task_id or "default"
    env, env_type = _get_or_create_env(effective_task_id)
    with _call_in_flight(_resolve_container_task_id(effective_task_id)):
        exec_start = time.monotonic()
        try:
            py_check = env.execute("command -v python3 >/dev/null 2>&1 && echo OK", cwd="/", timeout=15)
            if "OK" not in py_check.get("output", ""):
                return _error_result(f"Python 3 is not available in the {env_type} terminal "
                                     "environment. Install Python to use execute_code with remote backends.")
            # Session-kernel path: one persistent kernel per owner on the
            # run-to-completion transport. Spawn failure falls OPEN to the per-call
            # path below so a degraded remote host never blocks execution.
            try:
                # --- Session-kernel path (hermes-agent#96873) ------------------- Same always-on model as
                # local: one persistent kernel per owner, rebuilt on the run-to-completion transport (detached
                # runner + file cell protocol).
                from tools.code_kernel_remote import execute_in_remote_kernel
                kernel_result = execute_in_remote_kernel(
                    code, env=env, env_type=env_type, task_env_id=effective_task_id,
                    sandbox_tools=frozenset(sandbox_tools), timeout=timeout,
                    max_tool_calls=max_tool_calls, reset=bool(reset),
                    idle_exit=int(_cfg.get("kernel_idle_timeout", 1800)),
                )
            except Exception:
                logger.warning("remote session-kernel path failed; falling back to per-call", exc_info=True)
                kernel_result = None
            if kernel_result is not None:
                return _finish_remote_kernel_result(kernel_result, timeout=timeout, exec_start=exec_start)
            logger.info("remote session kernel unavailable on %s; using per-call path", env_type)
        except Exception as exc:
            return _remote_failure(exc, exec_start, 0)
        return _run_remote_per_call(env, env_type, code, effective_task_id, sandbox_tools,
                                    timeout=timeout, max_tool_calls=max_tool_calls, exec_start=exec_start)


# ---- Main entry point ----


def _prepare_cell_call(*, code: Any, cells: Any, language: Any, title: Any, timeout: Any,
                       cfg: dict) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Validate the D8 input surface; returns ``(spec, error_json)``. ``spec["cells"]`` is always
    a list (a single ``code`` becomes one cell), so dispatch has one shape."""
    if code is not None and cells is not None:
        return None, tool_error("Provide exactly one of 'code' (a single cell) or 'cells' "
                                "(a list of cells), not both.")
    if title is not None and not isinstance(title, str):
        return None, tool_error("'title' must be a string.")
    if cells is not None:
        if isinstance(cells, (str, bytes)) or not isinstance(cells, list):
            return None, tool_error("'cells' must be a list of {code, language?, title?} objects.")
        if not cells:
            return None, tool_error("'cells' must contain at least one cell.")
        max_cells = min(_positive_int(cfg.get("max_cells"), DEFAULT_MAX_CELLS), MAX_CELLS)
        if len(cells) > max_cells:
            return None, tool_error(f"execute_code accepts at most {max_cells} cells per call "
                                    f"(got {len(cells)}).")
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(cells):
            if not isinstance(raw, dict):
                return None, tool_error(f"cells[{index}] must be an object with a 'code' string.")
            cell_code = raw.get("code")
            if not isinstance(cell_code, str) or not cell_code.strip():
                return None, tool_error(f"cells[{index}].code must be a non-empty string.")
            cell_language = _normalize_language(
                raw.get("language") if raw.get("language") is not None else language
            )
            if cell_language is None:
                return None, tool_error(f"cells[{index}] has unsupported language "
                                        f"{raw.get('language')!r}; use 'py' or 'js'.")
            cell_title = raw.get("title")
            if cell_title is not None and not isinstance(cell_title, str):
                return None, tool_error(f"cells[{index}].title must be a string.")
            normalized.append({"code": cell_code, "language": cell_language, "title": cell_title})
        # `cells` is the multi-cell surface even with one item: its title/language must survive
        # in the aggregate shape (a single `code` is the only single-cell form).
        single = False
    else:
        single_language = _normalize_language(language)
        if single_language is None:
            return None, tool_error(f"Unsupported language {language!r}; use 'py' or 'js'.")
        if code is None:
            hint = "JavaScript" if single_language == "js" else "Python"
            return None, tool_error("No code provided. execute_code requires a non-empty 'code' "
                                    f"parameter containing {hint} source. To run shell commands, "
                                    "use terminal(command=...) instead.")
        if not isinstance(code, str):
            hint = "JavaScript" if single_language == "js" else "Python"
            return None, tool_error(f"execute_code received a {type(code).__name__} in 'code', but it "
                                    f"requires {hint} source as a string. Retry as execute_code(code=\"...\").")
        if not code.strip():
            hint = "JavaScript" if single_language == "js" else "Python"
            return None, tool_error("No code provided. execute_code requires a non-empty 'code' "
                                    f"parameter containing {hint} source. To run shell commands, "
                                    "use terminal(command=...) instead.")
        normalized = [{"code": code, "language": single_language, "title": None}]
        single = True
    cap_value = _positive_number(cfg.get("timeout"), float(DEFAULT_TIMEOUT))
    cap = int(cap_value) if float(cap_value).is_integer() else cap_value
    if timeout is None:
        effective_timeout = cap
    else:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            return None, tool_error("'timeout' must be a number of seconds.")
        if timeout <= 0:
            return None, tool_error("'timeout' must be greater than zero seconds.")
        effective_timeout = min(timeout, cap)
    return {
        "cells": normalized,
        "single": single,
        "languages": {cell["language"] for cell in normalized},
        "title": title,
        "timeout": effective_timeout,
        # Single-cell results only echo these when the model supplied them; generated display
        # titles never enter the model-visible result (ADR-4 D13).
        "explicit_language": language is not None,
        "explicit_title": title is not None,
    }, None


def _preflight_cell_call(spec: dict, *, env_config: dict) -> Optional[str]:
    """JS capability, gateway-lifecycle hard gate, then the approval gate — all before any spawn.
    Every cell is guarded with its own language; nothing runs when one cell is refused."""
    env_type = env_config["env_type"]
    if "js" in spec["languages"]:
        if env_type != "local":
            return tool_error('execute_code(language="js") is only available with the local '
                              "terminal backend; this session uses a remote/container backend.")
        if _bun_binary() is None:
            return tool_error("JavaScript needs Bun: hermes pm install bun")
        if not _js_kernel_available():
            return tool_error("JavaScript execution is unavailable in this build.")
    # Hard-block gateway-lifecycle commands (mirrors the terminal_tool guard — otherwise
    # `os.system("launchctl bootout ...")` or `Bun.spawn([...])` here bypasses it and SIGTERMs
    # the gateway mid-task). Gated on PID-file ownership, not the inherited env marker. The
    # identity probe ends in a kernel process query that has wedged on macOS (#111922); share
    # the call's own deadline and fail CLOSED when it renders no verdict.
    from agent.deadline import run_bounded_sync
    from tools.process_registry import _is_supervised_gateway_process
    from tools.terminal_tool import _PRE_EXEC_GUARD_MIN_TIMEOUT_S
    probe_timeout = max(spec["timeout"], _PRE_EXEC_GUARD_MIN_TIMEOUT_S)
    probe = run_bounded_sync(
        _is_supervised_gateway_process, probe_timeout, label="execute_code.lifecycle-guard",
    )
    if probe.timed_out:
        return tool_error(
            f"execute_code lifecycle guard did not finish within {probe_timeout}s "
            "(process-identity probe wedged); the code was not run. Retry the call."
        )
    if probe.value:
        from cron.lifecycle_guard import (
            HOST_INTERPRETER_KILL_REJECTION,
            contains_gateway_lifecycle_command,
            contains_host_interpreter_kill,
        )
        for cell in spec["cells"]:
            if contains_gateway_lifecycle_command(cell["code"]):
                if contains_host_interpreter_kill(cell["code"]):
                    return tool_error(HOST_INTERPRETER_KILL_REJECTION)
                return tool_error(
                    "Blocked: cannot restart or stop the gateway from inside the "
                    "gateway process. The gateway would kill this script before "
                    "it could complete (SIGTERM propagates to child processes). "
                    "Run the lifecycle command from a shell outside the gateway."
                )
    # Arbitrary code never passes through terminal()/DANGEROUS_PATTERNS, so guard the whole
    # script before either dispatch path spawns it — in this (tool-executor) thread, which holds
    # the session context. A Docker sandbox with host bind mounts gets no container fast-path.
    # See #30882.
    from tools.approval import check_execute_code_guard
    from tools.terminal_tool import _docker_has_host_access
    has_host_access = _docker_has_host_access(env_config)
    user_approved = False
    for cell in spec["cells"]:
        guard = check_execute_code_guard(cell["code"], env_type, has_host_access=has_host_access,
                                         language=cell["language"])
        if not guard.get("approved", False):
            return _error_result(guard.get("message") or "execute_code blocked by approval guard.",
                                 user_summary=guard.get("user_summary"))
        user_approved = user_approved or bool(guard.get("user_approved"))
    # Clear a stale interrupt bit that landed during the blocking approval-wait so it can't
    # kill the just-approved run on the first poll. A genuine post-clear interrupt re-sets it.
    if user_approved:
        from tools.interrupt import clear_current_thread_interrupt
        clear_current_thread_interrupt()
    return None


def _execute_multi_cells(spec: dict, *, task_id: Optional[str], enabled_tools: Optional[list[str]],
                         env_type: str, reset: bool, max_tool_calls: int, is_interrupted,
                         on_stream=None) -> str:
    """Run ``cells``. Same-language cells stay in input order on one kernel; different
    languages may run concurrently (ADR-4 D11.4). ``reset`` clears each involved language's
    kernel on its first cell. Call-level ``timeout`` is the whole-call deadline: a cell that
    starts after it expires is reported as timed out, never run. Results keep input order."""
    exec_start = time.monotonic()
    deadline = exec_start + spec["timeout"]
    mode = _get_execution_mode()
    child_python = _resolve_child_python(mode)
    child_cwd = _resolve_child_cwd(mode, "", task_id=task_id or "")
    sandbox_tools = frozenset(_sandbox_tools_for(enabled_tools))
    cells = spec["cells"]
    entries: list[Optional[dict[str, Any]]] = [None] * len(cells)
    from tools.eval_js_packages import parse_magic

    def run_one(index: int, cell: dict, want_reset: bool, interrupted) -> None:
        if cell["language"] == "js" and parse_magic(cell["code"]) is not None:
            message = "%bun/%environment cells must be a single-cell call."
            entries[index] = {"index": index, "code": cell["code"], "language": "js",
                              "status": "error", "exit_code": 1, "duration_seconds": 0.0,
                              "error": message, "output": message}
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            message = (f"Cell not run: the execute_code deadline ({spec['timeout']:g}s) "
                       "expired before it started.")
            entry = {"index": index, "code": cell["code"], "language": cell["language"]}
            if cell.get("title") is not None:
                entry["title"] = cell["title"]
            entry.update({"status": "timeout", "exit_code": -1, "duration_seconds": 0.0,
                          "error": message, "output": f"⏰ {message}"})
            entries[index] = entry
            return
        cell_start = time.monotonic()
        if env_type != "local":
            raw = _execute_remote(cell["code"], task_id, enabled_tools, reset=want_reset,
                                  timeout=remaining)
        else:
            raw = _run_local_cell(
                cell["code"], cell["language"], task_id=task_id or "", mode=mode,
                child_python=child_python, child_cwd=child_cwd, sandbox_tools=sandbox_tools,
                timeout=remaining, max_tool_calls=max_tool_calls, reset=want_reset,
                is_interrupted=interrupted, on_stream=on_stream, cell_index=index,
            )
        entries[index] = _cell_entry(index, cell, raw, cell_start)

    groups: dict[str, list[int]] = {}
    for index, cell in enumerate(cells):
        groups.setdefault(cell["language"], []).append(index)

    def run_group(indexes: list[int], interrupted) -> None:
        for position, index in enumerate(indexes):
            run_one(index, cells[index], bool(reset) and position == 0, interrupted)

    if env_type != "local" or len(groups) <= 1:
        for indexes in groups.values():
            run_group(indexes, is_interrupted)
    else:
        from concurrent.futures import ThreadPoolExecutor
        from tools.interrupt import is_thread_interrupted
        from tools.thread_context import propagate_context_to_thread
        submitter_tid = threading.current_thread().ident

        def group_interrupted() -> bool:
            # Pool threads are not the tool-call thread, so the per-thread interrupt bit must be
            # read for the submitter (/stop, Ctrl+C) as well as this worker.
            return bool(is_interrupted()) or is_thread_interrupted(submitter_tid)

        with ThreadPoolExecutor(max_workers=len(groups),
                                thread_name_prefix="hermes-eval-cell") as pool:
            # Capture each group's context HERE (on the tool-executor thread): a bare pool worker
            # would lose the turn's approval/session ContextVars and CellAuthority would snapshot none.
            futures = [pool.submit(propagate_context_to_thread(run_group), indexes, group_interrupted)
                       for indexes in groups.values()]
            for future in futures:
                future.result()

    ordered = []
    for index, cell in enumerate(cells):
        entry = entries[index]
        if entry is None:
            entry = {"index": index, "code": cell["code"], "language": cell["language"],
                     "status": "error", "exit_code": -1, "duration_seconds": 0.0,
                     "error": "Cell did not produce a result.", "output": ""}
        ordered.append(entry)
    return _multi_cell_result(ordered, exec_start=exec_start, title=spec.get("title"))


def _execute_spec(spec: dict, *, task_id: Optional[str], enabled_tools: Optional[list[str]],
                  env_type: str, reset: bool, max_tool_calls: int, is_interrupted,
                  on_stream=None) -> tuple[str, str]:
    """Run one validated call spec; returns ``(result_json, backend)``. Shared by the synchronous
    path and the background job manager so both produce one result shape (ADR-4 D14)."""
    if spec["single"]:
        cell = spec["cells"][0]
        if env_type != "local":
            raw = _execute_remote(cell["code"], task_id, enabled_tools, reset=bool(reset),
                                  timeout=spec["timeout"])
            backend = "remote"
        else:
            mode = _get_execution_mode()
            raw = _run_local_cell(
                cell["code"], cell["language"], task_id=task_id or "", mode=mode,
                child_python=_resolve_child_python(mode),
                child_cwd=_resolve_child_cwd(mode, "", task_id=task_id or ""),
                sandbox_tools=frozenset(_sandbox_tools_for(enabled_tools)),
                timeout=spec["timeout"], max_tool_calls=max_tool_calls,
                reset=bool(reset), is_interrupted=is_interrupted,
                on_stream=on_stream, cell_index=0,
            )
            backend = "local"
        if spec.get("explicit_language") or spec.get("explicit_title"):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                if spec.get("explicit_language"):
                    parsed["language"] = cell["language"]
                if spec.get("explicit_title"):
                    parsed["title"] = spec.get("title")
                raw = json.dumps(parsed, ensure_ascii=False)
        return raw, backend
    raw = _execute_multi_cells(
        spec, task_id=task_id, enabled_tools=enabled_tools, env_type=env_type,
        reset=bool(reset), max_tool_calls=max_tool_calls, is_interrupted=is_interrupted,
        on_stream=on_stream,
    )
    return raw, "remote" if env_type != "local" else "local"


def execute_code(
    code: Optional[str] = None,
    task_id: Optional[str] = None,
    enabled_tools: Optional[list[str]] = None,
    reset: bool = False,
    *,
    language: Optional[str] = None,
    cells: Optional[list[dict[str, Any]]] = None,
    title: Optional[str] = None,
    timeout: Optional[float] = None,
    background: bool = False,
    tool_call_id: Optional[str] = None,
) -> str:
    """Run code in the session's persistent kernel (local) or on the remote terminal backend,
    with RPC access to a subset of Hermes tools; returns the JSON result string. "Sandbox" means
    the security envelope (env scrubbing, tool whitelist + call budget, output redaction), not an
    isolation jail: default `project` mode runs in the session's cwd with the project venv.
    ``enabled_tools`` ∩ SANDBOX_ALLOWED_TOOLS; ``reset`` kills the existing kernel first.

    D8 surface: exactly one of ``code`` (single cell) or ``cells`` (ordered, up to
    ``code_execution.max_cells``); ``language`` selects ``py``/``js`` per cell; ``timeout`` is the
    whole-call deadline (capped by ``code_execution.timeout``); ``background`` is handled by the
    eval job manager (ADR-4 D14)."""
    if not SANDBOX_AVAILABLE:
        return tool_error("execute_code sandbox is unavailable in this environment. "
                          "Use normal tool calls (terminal, read_file, write_file, ...) instead.")
    # Fail closed under a terminal-policy refusal scope: the routed profile's terminal
    # policy is unresolved, so refuse rather than inherit the launch process's ambient policy.
    try:
        # See #68559.
        from tools.terminal_scope import enforce_no_refusal
        enforce_no_refusal()
    except Exception as refusal:
        return tool_error(f"execute_code refused: {refusal} "
                          "(profile terminal policy unresolved; fix the profile's config.yaml / .env and retry)")
    if not isinstance(background, bool):
        return tool_error("'background' must be a boolean.")
    cfg = _load_config()
    spec, error = _prepare_cell_call(code=code, cells=cells, language=language, title=title,
                                     timeout=timeout, cfg=cfg)
    if error is not None:
        return error
    from tools.terminal_tool import _get_env_config
    env_config = _get_env_config()
    if spec["single"] and spec["cells"][0]["language"] == "js":
        from tools.eval_js_packages import parse_magic, run_magic
        magic = parse_magic(spec["cells"][0]["code"])
        if magic is not None:
            if background:
                return tool_error("`background` is not supported for %bun/%environment cells.")
            if env_config["env_type"] != "local":
                return tool_error("JavaScript package magic is only available on the local terminal backend.")
            from tools.eval_js_packages import managed_dir, managed_key
            mode = _get_execution_mode()
            session_cwd = managed_key(
                _resolve_child_cwd(mode, "", task_id=task_id or ""), task_id or "")
            body = run_magic(magic[0], magic[1], session_cwd=session_cwd,
                             bun_path=_bun_binary() if magic[0] == "bun_add" else None)
            body.setdefault("tool_calls_made", 0)
            body.setdefault("duration_seconds", 0.0)
            body["managed_dir"] = body.get("managed_dir") or str(managed_dir(session_cwd))
            return json.dumps(body, ensure_ascii=False)
    error = _preflight_cell_call(spec, env_config=env_config)
    if error is not None:
        return error
    try:
        from agent.tool_progress_relay import current_tool_events
        sink = current_tool_events()
    except Exception:  # health: allow BLE001 -- a host without the relay still runs the cell
        sink = None

    def _on_stream(frame) -> None:
        if sink is None or not isinstance(frame, dict) or frame.get("type") != "stream":
            return
        sink.stream(int(frame.get("cell_index") or 0), str(frame.get("stream") or "stdout"),
                    str(frame.get("text") or ""))

    env_type = env_config["env_type"]
    max_tool_calls = _positive_int(cfg.get("max_tool_calls"), DEFAULT_MAX_TOOL_CALLS)
    if background:
        from tools.eval_jobs import start_eval_job
        return start_eval_job(spec=spec, task_id=task_id, enabled_tools=enabled_tools,
                              reset=bool(reset), cfg=cfg, env_type=env_type,
                              max_tool_calls=max_tool_calls, tool_call_id=tool_call_id,
                              sink=sink)
    from hermes_cli.observability.shared_metrics_loop import record_execution_backend
    from tools.interrupt import is_interrupted
    raw, backend = _execute_spec(
        spec, task_id=task_id, enabled_tools=enabled_tools, env_type=env_type,
        reset=bool(reset), max_tool_calls=max_tool_calls, is_interrupted=is_interrupted,
        on_stream=_on_stream,
    )
    return record_execution_backend("code", backend, raw)


def _kill_process_group(proc, escalate: bool = False):
    """Kill the child and its whole process tree via agent.deadline.kill_process_tree: SIGTERM
    (killpg + psutil descendant sweep; ``taskkill /T /F`` on Windows, where sig is ignored); with
    ``escalate=True`` wait 5s then SIGKILL survivors. Never raises — falls back to ``proc.kill()``."""
    import signal as _signal
    def _tree_signal(sig) -> None:
        try:
            from agent.deadline import kill_process_tree
            kill_process_tree(proc.pid, sig=sig)
        except Exception as e:
            logger.debug("Could not terminate process tree: %s", e, exc_info=True)
            try:
                proc.kill()
            except Exception as e2:
                logger.debug("Could not kill process: %s", e2, exc_info=True)
    _tree_signal(getattr(_signal, "SIGTERM", None))
    if escalate:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _tree_signal(getattr(_signal, "SIGKILL", None))


def _load_config() -> dict:
    """Effective ``code_execution`` section (defaults + user file + managed overlay) — runs while the
    module-level schema is built at tool discovery, so it must not import ``cli``."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly().get("code_execution", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


# ---- Execution mode resolution (strict vs project) ----

# Canonical code_execution.mode values (referenced by tests and the config layer). Session
# kernels are the only local execution model; a leftover kernel_mode config key is ignored.
EXECUTION_MODES = ("project", "strict")
DEFAULT_EXECUTION_MODE = "project"


def _get_execution_mode() -> str:
    """``code_execution.mode`` (invalid → default with a warning). ``project``: session cwd + active
    venv python; ``strict``: isolated temp dir + ``sys.executable``. Scrubbing/whitelist apply to both."""
    cfg_value = str(_load_config().get("mode", DEFAULT_EXECUTION_MODE)).strip().lower()
    if cfg_value in EXECUTION_MODES:
        return cfg_value
    logger.warning(
        "Ignoring code_execution.mode=%r (expected one of %s), falling back to %r",
        cfg_value, EXECUTION_MODES, DEFAULT_EXECUTION_MODE,
    )
    return DEFAULT_EXECUTION_MODE


# ---- OpenAI Function-Calling Schema ----

# Per-tool documentation lines for the execute_code description, in canonical display order.
_TOOL_DOC_LINES = [
    ("web_search", "  web_search(query: str, limit: int = 5) -> dict\n"
     "    Returns {\"data\": {\"web\": [{\"url\", \"title\", \"description\"}, ...]}}"),
    ("web_extract", "  web_extract(urls: list[str], char_limit: int = None) -> dict\n"
     "    Returns {\"results\": [{\"url\", \"title\", \"content\", \"error\"}, ...]} where content is markdown.\n"
     "    No LLM summarization. Pages over char_limit (default 15000) are head+tail truncated; full text stored on disk (path in the content footer)."),
    ("read_file", "  read_file(path: str, offset: int = 1, limit: int = 2000) -> dict\n"
     "    Lines are 1-indexed. Returns {\"content\": \"...\", \"total_lines\": N}"),
    ("write_file", "  write_file(path: str, content: str) -> dict\n    Always overwrites the entire file; an existing file must be read_file'd first or the write is refused."),
    ("search_files", "  search_files(pattern: str, target=\"content\", path=\".\", file_glob=None, limit=50, order=\"discovery\") -> dict\n"
     "    target: \"content\" (search inside files) or \"files\" (find files by name). Returns {\"matches\": [...]}"),
    ("patch", "  patch(path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict\n"
     "    Replaces old_string with new_string in the file."),
    ("terminal", "  terminal(command: str, timeout=None, workdir=None) -> dict\n"
     "    Foreground only (no background/pty). Returns {\"output\": \"...\", \"exit_code\": N}"),
]


def build_execute_code_schema(enabled_sandbox_tools: set | None = None,
                              mode: str | None = None,
                              js_available: bool = False) -> dict:
    """execute_code schema listing only *enabled_sandbox_tools* — a disabled tool (e.g. web off)
    must not appear or the model keeps trying it. ``mode`` (None → config) picks the cwd sentence.
    ``js_available`` adds ``js`` to the language enums; it is resolved per session from PM Bun +
    the shipped JS kernel, so registration-time ``EXECUTE_CODE_SCHEMA`` stays Python-only."""
    if enabled_sandbox_tools is None:
        enabled_sandbox_tools = SANDBOX_ALLOWED_TOOLS
    if mode is None:
        mode = _get_execution_mode()
    languages = ["py", "js"] if js_available else ["py"]
    max_cells = _max_cells()
    tool_lines = "\n".join(doc for name, doc in _TOOL_DOC_LINES if name in enabled_sandbox_tools)
    import_examples = [n for n in ("web_search", "terminal") if n in enabled_sandbox_tools]
    import_examples = import_examples or sorted(enabled_sandbox_tools)[:2]
    import_str = ", ".join(import_examples) + ", ..." if import_examples else "..."
    if mode == "strict":
        cwd_note = (
            "Scripts run in their own temp dir, not the session's CWD — pass "
            "absolute paths for any file that lives outside the session's "
            "working directory, or use terminal()/read_file() to reach it."
        )
    else:
        cwd_note = (
            "Scripts run in the session's working directory. Interpreter: "
            "the project's activated venv/conda python when one is active "
            "(VIRTUAL_ENV/CONDA_PREFIX — matches terminal()); otherwise "
            "Hermes's own python (the common case — stdlib plus Hermes's "
            "deps; check `import x` before relying on project packages)."
        )
    runtimes = "Python and JavaScript" if js_available else "Python"
    cell_note = (
        "Provide exactly one of `code` (a single cell) or `cells` (an ordered list of up to "
        f"{max_cells} cells, each `{{code, language?, title?}}`). `language` is "
        + ("`py` (default) or `js`" if js_available else "`py`")
        + "; Python and JS run in separate persistent kernels and do not share state."
    )
    # Remote hosts that fail open to per-call are not worth schema words; the result's
    # `kernel` field tells the truth per call.
    # Session kernels are always on (kernel_mode retired in #96787): persistence is part of the tool's one
    # description, not a bolt-on paragraph behind a dead conditional.
    description = (
        f"Run {runtimes} that calls Hermes tools programmatically. Use when you "
        "need 3+ tool calls with logic between them: filtering/reducing "
        "large outputs before they enter context, branching, or loops "
        "(N pages/files, retry on failure). Use normal tool calls for "
        "single calls, results you must reason over in full, or anything needing user interaction.\n\n"
        "Calls run in a persistent session kernel: variables, imports, and "
        "loaded data survive across execute_code calls, so build on earlier "
        "work instead of re-loading it. A timed-out or interrupted call loses that state.\n\n"
        f"{cell_note}\n\n"
        f"Available via `from hermes_tools import ...`:\n\n"
        f"{tool_lines}\n\n"
        "Limits: 5-minute timeout (or `timeout` seconds, capped by config), max 50 tool calls "
        "per call. Stdout over 50KB shows head/tail inline; the FULL text is auto-saved to a "
        "file whose path rides in the result.\n\n"
        f"{cwd_note}\n\n"
        "Helpers require imports: `from hermes_tools import json_parse, shell_quote, retry`. "
        "json_parse(text) — tolerant "
        "json.loads for terminal() output; shell_quote(s) — shlex.quote for "
        "dynamic shell args; retry(fn, max_attempts=3, delay=2) — exponential backoff."
    )
    return {
        "name": "execute_code",
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": (
                    "Single-cell source. Provide exactly one of `code` or `cells`. Import tools "
                    f"with `from hermes_tools import {import_str}` and print your final result "
                    "to stdout.")},
                "cells": {
                    "type": "array",
                    "description": ("Ordered cells executed against the same per-language kernel "
                                    "(up to the configured max). Provide exactly one of `code` or "
                                    "`cells`."),
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "description": "Cell source."},
                            "language": {"type": "string", "enum": languages,
                                         "description": "Cell language; defaults to the call-level language."},
                            "title": {"type": "string", "description": "Optional cell title for display."},
                        },
                        "required": ["code"],
                    },
                },
                "language": {"type": "string", "enum": languages, "description": (
                    "Language for `code` (and the default for cells). Defaults to `py`.")},
                "title": {"type": "string", "description": (
                    "Optional human title for this call; never changes the execution semantics.")},
                "timeout": {"type": "number", "description": (
                    "Whole-call deadline in seconds; capped by `code_execution.timeout` "
                    "(default 300). A timeout kills the involved kernel(s).")},
                "background": {"type": "boolean", "description": (
                    "Return a running job instead of waiting for the cells to finish.")},
                "reset": {"type": "boolean", "description": (
                    "Discard the involved kernels' persistent state and start fresh before "
                    "running this call.")},
            },
        },
    }


# Registration-time schema (all sandbox tools, configured mode); model_tools.py rebuilds per-session.
EXECUTE_CODE_SCHEMA = build_execute_code_schema()


def _execute_code_handler(args: dict, **kwargs) -> str:
    """Redirect misdirected calls (terminal's ``command`` arg, non-string ``code``) with an
    actionable error before dispatching to ``execute_code``."""
    if "code" not in args and "cells" not in args and "command" in args:
        logger.warning("execute_code received 'command' instead of the required 'code' argument")
        return tool_error("execute_code received a 'command' parameter, but it requires "
                          "source in 'code' or 'cells'. Use terminal(command=...) for shell "
                          "commands; for code, retry as execute_code(code=...).")
    code = args.get("code")
    if code is not None and not isinstance(code, str):
        hint = "JavaScript" if _normalize_language(args.get("language")) == "js" else "Python"
        return tool_error(f"execute_code received a {type(code).__name__} in 'code', but it "
                          f"requires {hint} source as a string. Retry as execute_code(code=\"...\").")
    return execute_code(code=code, task_id=kwargs.get("task_id"),
                        enabled_tools=kwargs.get("enabled_tools"), reset=bool(args.get("reset", False)),
                        language=args.get("language"), cells=args.get("cells"),
                        title=args.get("title"), timeout=args.get("timeout"),
                        background=args.get("background", False),
                        tool_call_id=kwargs.get("tool_call_id"))


registry.register(
    name="execute_code", toolset="code_execution", schema=EXECUTE_CODE_SCHEMA,
    handler=_execute_code_handler, check_fn=check_sandbox_requirements, emoji="🐍",
    max_result_size_chars=100_000,
)
