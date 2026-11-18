"""Read-only speculation for streamed ``execute_code`` arguments (ADR-4 D11.3).

While the model streams an ``execute_code`` call, Hermes can see the cell source
before the call is complete. For the only idempotent, policy-free operations the
prelude commonly starts with — ``read_file(path=...)`` — this module prefetches
the file and parks a *claim*. The authoritative cell later asks the claim store
first: a hit whose path fingerprint (mtime/size) still matches returns the
prefetched result; a miss falls through to the normal dispatch, byte-identical
to a run without speculation. Side effects therefore come only from the
committed call (prefetches are reads), a restarted/rewritten argument stream
drops its claims by generation, and an explicit ``reset``/session clear
invalidates the owner.

Kernel snapshot safety: claims are owner-scoped and bound to the argument-stream
generation; a reset (the only way the kernel namespace changes under a live
stream) bumps it. The prefetch itself never runs cell code, so there is no
shadow kernel whose state could disagree.
"""

from __future__ import annotations

import atexit
import contextvars
import json
import logging
import os
import re
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_PARTIAL_CHARS = 64_000
_MAX_CLAIMS = 16
_CLAIM_TTL = 300.0

_READ_CALL_RE = re.compile(
    r"""read_file\s*\(\s*(?:path\s*=\s*)?(["'])(?P<path>(?:\\.|(?!\1).)*)\1""",
    re.DOTALL,
)


class _OwnerClaims:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.generation = 0
        self.calls: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, dict[str, Any]] = {}
        self.pending: set[str] = set()


_OWNERS: dict[str, _OwnerClaims] = {}
_OWNERS_LOCK = threading.Lock()


def _owner(task_id: str = "") -> str:
    try:
        from tools.code_kernel import _resolve_owner
        return _resolve_owner(task_id) or ""
    except Exception:  # health: allow BLE001 -- speculation is opportunistic
        return ""


def _state(owner: str) -> _OwnerClaims:
    with _OWNERS_LOCK:
        state = _OWNERS.get(owner)
        if state is None:
            state = _OWNERS[owner] = _OwnerClaims()
        return state


def _claim_key(name: str, args: Any) -> str:
    try:
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        canonical = str(args)
    return f"{name}\x00{canonical}"


def _canonical_path(path: Any) -> Optional[str]:
    """Absolute path in a comparable form; relative paths are not speculated (their
    resolution depends on the cell's session cwd, which the host does not share)."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return None
    return os.path.normcase(os.path.abspath(path))


def _fingerprint(path: str) -> Optional[list[int]]:
    try:
        stat = os.stat(path)
        return [stat.st_mtime_ns, stat.st_size]
    except OSError:
        return None


def observe_partial(name: Optional[str], chunk: str, slot: int, call_id: str = "") -> None:
    """Observe one streamed argument fragment (called from the tool-call accumulator).

    Cheap for every non-``execute_code`` call: the name check precedes all state.
    """
    if name != "execute_code" or not isinstance(chunk, str) or not chunk:
        return
    owner = _owner()
    if not owner:
        return
    state = _state(owner)
    with state.lock:
        call = state.calls.get(call_id or f"slot-{slot}")
        if call is None or call.get("slot") != slot:
            binding = call_id or f"slot-{slot}"
            # A new streamed execute_code call drops the previous call's claims.
            state.generation += 1
            if call_id:
                for key in [key for key, value in state.calls.items()
                            if value.get("slot") == slot and key != call_id]:
                    state.calls.pop(key, None)
            call = state.calls[binding] = {"slot": slot, "text": "", "scanned": 0,
                                           "generation": state.generation}
        text = str(call.get("text") or "") + chunk
        if len(text) > _MAX_PARTIAL_CHARS:
            text = text[-_MAX_PARTIAL_CHARS:]
            call["scanned"] = 0
        call["text"] = text
        scanned = int(call.get("scanned") or 0)
        window = text[scanned:]
        generation = int(call.get("generation") or 0)
        found: list[str] = []
        for match in _READ_CALL_RE.finditer(window):
            if scanned + match.end() <= len(text):
                path = _unescape(match.group("path"))
                if path and _canonical_path(path) is not None:
                    found.append(path)
        call["scanned"] = len(text)
        for path in found:
            key = _claim_key("read_file", {"path": _canonical_path(path)})
            if key in state.claims or key in state.pending or len(state.claims) >= _MAX_CLAIMS:
                continue
            state.pending.add(key)
    for path in found:
        _spawn_prefetch(owner, generation, path)


def _unescape(path: str) -> str:
    try:
        return json.loads('"' + path.replace('"', '\\"') + '"') if "\\" in path else path
    except ValueError:
        return path


def _spawn_prefetch(owner: str, generation: int, path: str) -> None:
    from hermes_threading import spawn_context_thread

    def run() -> None:
        try:
            # Handler execution only: no pre/post hooks or middleware run for a call the model has
            # not committed. The claim is consumed in ``_execute_tool`` AFTER guards/middleware, so
            # blocked reads never reach the model and side effects stay on the committed call.
            from tools.registry import registry
            result = registry.dispatch("read_file", {"path": path})
            canonical = _canonical_path(path)
            fingerprint = _fingerprint(canonical) if canonical else None
            if fingerprint is None:
                return
        except Exception:  # health: allow BLE001 -- a failed prefetch is simply a miss
            logger.debug("eval speculation prefetch failed for %s", path, exc_info=True)
            result, fingerprint, canonical = None, None, None
        state = _state(owner)
        with state.lock:
            key = _claim_key("read_file", {"path": canonical})
            state.pending.discard(_claim_key("read_file", {"path": _canonical_path(path)}))
            if result is None or canonical is None or generation != state.generation:
                return
            state.claims[key] = {"result": result, "fingerprint": fingerprint,
                                 "created": time.monotonic(), "generation": generation}

    spawn_context_thread(run, name="hermes-eval-speculation").start()


def take_claim(task_id: str, name: str, args: Any) -> Optional[str]:
    """Return a valid prefetched result for this exact call, else ``None``.

    A claim is valid only when its argument-stream generation still matches and
    the file fingerprint is unchanged; either mismatch drops it.
    """
    if name != "read_file" or not isinstance(args, dict):
        return None
    canonical = _canonical_path(args.get("path"))
    if canonical is None:
        return None
    owner = _owner(task_id)
    if not owner:
        return None
    state = _state(owner)
    key = _claim_key(name, {"path": canonical})
    with state.lock:
        claim = state.claims.get(key)
        if claim is None:
            return None
        if claim["generation"] != state.generation or time.monotonic() - claim["created"] > _CLAIM_TTL:
            state.claims.pop(key, None)
            return None
    now = _fingerprint(canonical)
    if now is None or now != claim["fingerprint"]:
        with state.lock:
            state.claims.pop(key, None)
        return None
    with state.lock:
        state.claims.pop(key, None)
    return claim["result"]


def invalidate_owner(task_id: str = "") -> None:
    """Drop every in-flight call and claim for one owner (reset/session clear)."""
    owner = _owner(task_id)
    if not owner:
        return
    with _OWNERS_LOCK:
        state = _OWNERS.get(owner)
    if state is None:
        return
    with state.lock:
        state.generation += 1
        state.calls.clear()
        state.claims.clear()
        state.pending.clear()


def clear_all() -> None:
    """Forget every owner's speculation state (atexit and tests)."""
    with _OWNERS_LOCK:
        states = list(_OWNERS.values())
        _OWNERS.clear()
    for state in states:
        with state.lock:
            state.generation += 1
            state.calls.clear()
            state.claims.clear()
            state.pending.clear()


atexit.register(clear_all)
