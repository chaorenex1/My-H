"""Live tool-output archive (ADR-16 D3, P1).

Every committed tool result is written into the session bundle as
``<step>.<tool>.log`` (the text the model actually saw). When that result was truncated —
by the wrapper's spillover replacement or by a tool's own bounded capture — the
pre-truncation text is written next to it as ``<step>.<tool>-original.log``.

The archive is host-side only: it never changes the model-visible result and never
replaces the ``cache/spillover`` recovery path (remote sandboxes can't read
``sessions/``). Writes are atomic (temp file + rename), redacted before disk, and
best-effort: an archive failure returns ``None`` and never fails the tool call.

Truncation originals come from one of two places, both real:

- the wrapper replaced the result with a preview (``persisted != result``) — the full
  in-memory result is the original;
- the tool kept a bounded capture and spilled the full stream (terminal's
  ``full_output_path``, execute_code's ``stdout_spill_path``) — the spill file is the
  original. Only spill paths under this profile's ``cache/`` (or the temp result dir)
  are read, so a tool result can't point the archive at an arbitrary host file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

import hermes_session_paths as session_paths
from tools.tool_output_artifacts import get_artifact_settings, is_excluded_tool

logger = logging.getLogger(__name__)

#: Spill files are bounded by their writers (terminal/exec cap at 5M chars); the
#: ceiling only stops a corrupted/hostile path from being slurped whole.
MAX_ORIGINAL_BYTES = 64 * 1024 * 1024
#: Parsing a JSON result only to find its spill handle; huge payloads skip the parse.
MAX_JSON_PARSE_CHARS = 32 * 1024 * 1024
#: Structured fields a tool result may use to expose the full pre-truncation text.
_SPILL_KEYS = ("full_output_path", "stdout_spill_path")
#: ``full_output_path`` is terminal's post-redaction tee; ``stdout_spill_path`` is raw.
_REDACTED_SPILL_KEYS = frozenset({"full_output_path"})

_bundle_lock_attr = "_tool_output_bundle_lock"
#: A subagent run sets these on its own agent: its namespace is ``<bundle>/<topic>/`` and its
#: sequence is its own (ADR-16 D4) — no ``sessions`` row exists to allocate one.
_archive_dir_attr = "_tool_output_archive_dir"
_archive_seq_attr = "_tool_output_archive_seq"
_archive_seq_lock = threading.Lock()


def configure_agent_archive(agent: Any, directory: str | os.PathLike[str]) -> None:
    """Point one agent's tool archive at *directory* and seed its own step counter.

    Used by a named agent run, whose archive lives in ``<parent bundle>/<topic>/``. The
    counter starts at the highest step already on disk so a resident session that outlives
    the process never reuses a file name. Never raises.
    """
    try:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("tool archive: archive dir unavailable (%s)", exc)
        return
    with _archive_seq_lock:
        agent._tool_output_archive_dir = path
        agent._tool_output_archive_seq = _max_step(path)


def _max_step(directory: Path) -> int:
    highest = 0
    try:
        children = list(directory.iterdir())
    except OSError:
        return highest
    for child in children:
        stem = child.name.split(".", 1)[0]
        if stem.isdigit():
            highest = max(highest, int(stem))
    return highest


def _archive_dir(agent: Any) -> Optional[Path]:
    directory = getattr(agent, _archive_dir_attr, None)
    return directory if isinstance(directory, Path) else None


def agent_artifact_dir(agent: Any) -> Optional[Path]:
    """Where this agent's host-side session artifacts belong (ADR-16 D2/D4).

    A named agent run configured through :func:`configure_agent_archive` writes to its own
    ``<bundle>/<topic>/`` namespace; every other agent writes at its session bundle root.
    ``None`` when the bundle cannot be resolved yet (no session row and no start time).
    """
    return _archive_dir(agent) or bundle_dir_for_agent(agent)


def _next_local_step(agent: Any) -> int:
    with _archive_seq_lock:
        step = int(getattr(agent, _archive_seq_attr, 0) or 0) + 1
        agent._tool_output_archive_seq = step
        return step


def _session_id(agent: Any) -> Optional[str]:
    session_id = getattr(agent, "session_id", None)
    return session_id if isinstance(session_id, str) and session_id else None


def bundle_dir_for_agent(agent: Any) -> Optional[Path]:
    """The session's artifact directory, derived once and cached on the agent (ADR-16 D2).

    Uses the session row's own ``started_at``/``cwd`` so a later ``sessions mirror`` derives
    the same bundle. The path is frozen on first use: a session that changes cwd keeps writing
    to the bundle its transcript was started under.
    """
    cached = getattr(agent, "_tool_output_bundle_dir", None)
    if isinstance(cached, Path):
        return cached
    session_id = _session_id(agent)
    if session_id is None:
        return None
    lock = getattr(agent, _bundle_lock_attr, None)
    if not hasattr(lock, "acquire"):
        lock = threading.Lock()
        setattr(agent, _bundle_lock_attr, lock)
    with lock:
        cached = getattr(agent, "_tool_output_bundle_dir", None)
        if isinstance(cached, Path):
            return cached
        started_at: Any = getattr(agent, "session_start", None)
        cwd = getattr(agent, "session_cwd", None) or os.getcwd()
        db = getattr(agent, "_session_db", None)
        if db is not None:
            try:
                meta = db.get_session_bundle_meta(session_id)
            except Exception as exc:  # health: allow BLE001 -- the row is advisory here
                logger.debug("tool archive: session meta lookup failed: %s", exc)
                meta = None
            if isinstance(meta, dict):
                row_started = meta.get("started_at")
                if isinstance(row_started, (int, float, str)) and not isinstance(row_started, bool):
                    started_at = row_started
                row_cwd = meta.get("cwd")
                if isinstance(row_cwd, str) and row_cwd:
                    cwd = row_cwd
        if started_at is None:
            return None
        directory = session_paths.allocate_bundle_dir(cwd, started_at, session_id)
        directory.mkdir(parents=True, exist_ok=True)
        agent._tool_output_bundle_dir = directory
        return directory


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* atomically: temp file in the target directory, then ``os.replace``.

    ``newline=""`` keeps the string byte-for-byte (no CRLF translation on Windows), so an
    ``-original.log`` copied from a sanitized spill stays identical.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _result_text(result: Any) -> Optional[str]:
    """The textual face of a tool result: the string itself, or the text parts of a
    multimodal envelope. ``None`` when nothing textual reached the model."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        parts = result.get("content")
        if isinstance(parts, list):
            texts = [
                part["text"]
                for part in parts
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ]
            if texts:
                return "\n".join(texts)
    return None


def _trusted_spill_path(raw_path: Any) -> Optional[Path]:
    """``raw_path`` when it is a real file under this profile's ``cache/`` (or the temp
    result dir); otherwise ``None``. Resolves symlinks before the containment check."""
    if not isinstance(raw_path, str) or not raw_path:
        return None
    try:
        path = Path(raw_path)
        if not path.is_file():
            return None
        resolved = Path(os.path.realpath(path))
        if resolved.stat().st_size > MAX_ORIGINAL_BYTES:
            logger.warning("tool archive: spill %s exceeds the %d-byte read ceiling", resolved, MAX_ORIGINAL_BYTES)
            return None
        from hermes_constants import get_hermes_home

        # Only the writers that produce a *full* pre-truncation copy; a result naming any
        # other path is not a spill handle and must not be slurped into the archive.
        home = get_hermes_home()
        roots = [
            home / "cache" / "terminal-output",
            home / "cache" / "exec",
            home / "cache" / "spillover",
            Path(tempfile.gettempdir()) / "hermes-results",
        ]
        for root in roots:
            root_path = Path(os.path.realpath(root))
            try:
                resolved.relative_to(root_path)
            except ValueError:
                continue
            return resolved
    except OSError:
        return None
    return None


def _spill_from_result(result: Any) -> tuple[Optional[str], bool]:
    """``(spill text, already_redacted)`` from a tool result's structured spill handle.

    Parsing is limited to JSON object results; anything else (plain text, lists, enormous
    payloads) has no handle. The key, not the path, says whether the writer redacted.
    """
    payload: Any = result
    if isinstance(result, str):
        head = result.lstrip()[:1]
        if head != "{" or len(result) > MAX_JSON_PARSE_CHARS:
            return None, False
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return None, False
    if not isinstance(payload, dict):
        return None, False
    for key in _SPILL_KEYS:
        resolved = _trusted_spill_path(payload.get(key))
        if resolved is None:
            continue
        try:
            # newline="" preserves the file's own line endings for the byte-identical copy.
            with open(resolved, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
                return handle.read(), key in _REDACTED_SPILL_KEYS
        except OSError as exc:
            logger.debug("tool archive: spill read failed for %s: %s", resolved, exc)
            return None, False
    return None, False


def _redact_string(text: str) -> str:
    """Redact one string: nested JSON documents keep their syntax, plain text goes through
    the standard passes (an env dump's ``KEY=opaque`` must still mask)."""
    nested = _redact_json_document(text)
    if nested is not None:
        return nested
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception as exc:  # health: allow BLE001 -- redaction failure must fail closed
        logger.warning("tool archive: redaction failed (%s); archiving a masked placeholder", exc)
        return "[tool archive: redaction failed; content withheld]\n"


def _redact_json_node(value: Any) -> Any:
    from agent.redact import is_secret_field_name

    if isinstance(value, dict):
        return {
            key: "***" if is_secret_field_name(key) and isinstance(item, str) and item
            else _redact_json_node(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_node(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _redact_json_document(text: str) -> Optional[str]:
    """``text`` with every JSON string leaf redacted, or ``None`` when it isn't a JSON
    object/array. Structural redaction is what keeps a tool result valid JSON: the text
    passes alone can consume a closing quote (an env-dump ``KEY=value"`` tail)."""
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")) or len(text) > MAX_JSON_PARSE_CHARS:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, (dict, list)):
        return None
    redacted = _redact_json_node(payload)
    if redacted == payload:
        return text  # unchanged: keep the original formatting byte-for-byte
    return json.dumps(redacted, ensure_ascii=False)


def _redact(text: str) -> str:
    """Mandatory pre-disk redaction (ADR-16 D3)."""
    structured = _redact_json_document(text)
    return structured if structured is not None else _redact_string(text)


def archive_tool_result(
    agent: Any, tool_name: str, result: Any, persisted_result: Any
) -> Optional[dict[str, Any]]:
    """Archive one committed tool result; ``None`` when there is nothing to write.

    *result* is the pre-persist tool result, *persisted_result* what the model sees after
    the wrapper's spillover replacement. Never raises.
    """
    try:
        settings = get_artifact_settings()
        if not settings.enabled or is_excluded_tool(tool_name, settings):
            return None
        text = _result_text(persisted_result)
        if text is None:
            logger.debug("tool archive: skipped %s (no textual result)", tool_name)
            return None

        original_text: Optional[str] = None
        original_already_redacted = False
        if persisted_result is not result and persisted_result != result:
            original_text = _result_text(result)
        else:
            original_text, original_already_redacted = _spill_from_result(result)

        if settings.redact:
            text = _redact(text)
            if original_text is not None and not original_already_redacted:
                original_text = _redact(original_text)

        directory = _archive_dir(agent)
        if directory is not None:
            step = _next_local_step(agent)
        else:
            session_id = _session_id(agent)
            db = getattr(agent, "_session_db", None)
            if session_id is None or db is None:
                logger.debug("tool archive: skipped %s (no session context)", tool_name)
                return None
            try:
                step = db.next_tool_output_seq(session_id)
            except Exception as exc:  # health: allow BLE001 -- archive is best-effort
                logger.debug("tool archive: step allocation failed: %s", exc)
                return None
            if not isinstance(step, int) or isinstance(step, bool) or step < 1:
                logger.debug("tool archive: skipped %s (no step allocated)", tool_name)
                return None
            directory = bundle_dir_for_agent(agent)
        if directory is None:
            logger.debug("tool archive: skipped %s (bundle path unavailable)", tool_name)
            return None

        log_path, original_path = session_paths.tool_artifact_paths(directory, step, tool_name)
        _atomic_write(log_path, text)
        wrote_original = False
        if original_text is not None:
            try:
                _atomic_write(original_path, original_text)
                wrote_original = True
            except OSError as exc:
                logger.warning("tool archive: original write failed for %s: %s", log_path, exc)
        return {
            "step": step,
            "tool": str(tool_name or ""),
            "artifact_path": str(log_path),
            "artifact_original_path": str(original_path) if wrote_original else None,
            "truncated": wrote_original,
        }
    except Exception as exc:  # health: allow BLE001 -- archiving must never fail a tool call
        logger.debug("tool archive: %s archive failed: %s", tool_name, exc, exc_info=True)
        return None
