"""Session-bundle artifacts for one named agent run (ADR-16 D4/P2).

A named agent run owns a bundle namespace under its parent session's bundle:

    <bundle>/<topic>.jsonl        transcript (agent.run_transcript)
    <bundle>/<topic>.md           the final report, written verbatim at settle
    <bundle>/<topic>_ast.json     structural summary, always written at settle
    <bundle>/<topic>.json         optional structured output (output schema runs)
    <bundle>/<topic>/             the run's tool-output archive + advisor

The transcript is written live by :mod:`agent.run_transcript`; this module resolves the
bundle, writes the settle-time files, and hands back the ``artifact_manifest`` that
``agent_out`` reads. Every write is best-effort: a bundle failure is logged and the run
still settles.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import hermes_session_paths as session_paths

logger = logging.getLogger(__name__)

#: ``_ast.json`` format version (bump when the source-entry shape changes).
AST_VERSION = 1
#: Tool calls a structural source may be derived from when the run collected none.
SUMMARIZABLE_TOOLS = frozenset({"read_file", "write_file", "patch"})
#: ADR-16 D4: dedupe then take the most recent N files the run touched.
MAX_CANDIDATE_SOURCES = 20
#: Head bytes inspected for a NUL before treating a candidate as binary.
_BINARY_PROBE_BYTES = 8192


def _with_db(fn: Any, default: Any) -> Any:
    """Run *fn* with a session store; DB unavailability degrades to *default*."""
    try:
        from hermes_state_registry import acquire, release_or_close

        db = acquire()
    except Exception:  # health: allow BLE001 -- a run must not depend on the store
        logger.debug("run artifacts: no session store", exc_info=True)
        return default
    try:
        return fn(db)
    except Exception:  # health: allow BLE001 -- path resolution is advisory
        logger.debug("run artifacts: store read failed", exc_info=True)
        return default
    finally:
        release_or_close(db)


def _own_bundle(agent_session_id: str, project_dir: str, started_at: Any) -> Path:
    """The bundle of a run with no parent (cron / layer / standalone)."""
    cwd = str(project_dir or os.getcwd())
    return session_paths.allocate_bundle_dir(cwd, started_at or time.time(), agent_session_id)


def bundle_for_run(*, parent_session_id: str = "", agent_session_id: str = "",
                   project_dir: str = "", started_at: Any = None) -> Path:
    """Resolve the bundle this run's artifacts live in (ADR-16 D2/D4).

    A parented run shares its parent conversation's bundle (derived from that row's own
    ``started_at``/``cwd``); a run without a discoverable parent gets its own bundle keyed by
    its agent session id. The result is allocated, not created.
    """

    def _resolve(db: Any) -> Optional[Path]:
        if parent_session_id:
            meta = db.get_session_bundle_meta(parent_session_id)
            if isinstance(meta, dict) and meta.get("started_at") is not None:
                cwd = str(meta.get("cwd") or project_dir or os.getcwd())
                return session_paths.allocate_bundle_dir(
                    cwd, meta.get("started_at"), parent_session_id)
        row = db.get_agent_session(agent_session_id) if agent_session_id else None
        created = row.get("created_at") if isinstance(row, dict) else None
        return _own_bundle(agent_session_id, project_dir, created or started_at)

    resolved = _with_db(_resolve, None)
    return resolved if resolved is not None else _own_bundle(
        agent_session_id, project_dir, started_at)


def _atomic_text(path: Path, text: str) -> None:
    """Write *text* byte-for-byte (no newline translation) via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _run_value(run: Any, name: str, default: Any = None) -> Any:
    return getattr(run, name, default)


def _merge_sources(collected: list[dict[str, Any]],
                   legacy: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Legacy entries first, this run's collected entries overriding them by source."""
    merged: dict[str, dict[str, Any]] = {}
    for entry in legacy:
        merged[str(entry.get("source") or "")] = entry
    for entry in collected:
        merged[str(entry.get("source") or "")] = entry
    return [merged[source] for source in merged if source]


def _tool_path_arg(raw: Any) -> str:
    """The ``path`` argument of a file tool call, or ``""`` for a V4A patch without one."""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("path", "file_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _candidate_paths(transcript: Path) -> list[str]:
    """Most-recent-first paths the run read or wrote, deduped (ADR-16 D4)."""
    from agent import run_transcript

    seen: dict[str, None] = {}
    for row in run_transcript.read_events("", "", path=transcript):
        for call in row.get("tool_calls") or ():
            if not isinstance(call, dict) or call.get("name") not in SUMMARIZABLE_TOOLS:
                continue
            source = _tool_path_arg(call.get("args"))
            if source:
                seen.pop(source, None)
                seen[source] = None
    return list(reversed(list(seen)))[:MAX_CANDIDATE_SOURCES]


def _resolve_candidate(source: str, cwd: str) -> Path:
    path = Path(source).expanduser()
    return path if path.is_absolute() else Path(cwd) / path


def _read_candidate(path: Path) -> tuple[Optional[str], str]:
    """``(text, reason)``: the file's text, or ``(None, reason)`` when it cannot be summarized."""
    from tools.read_summary import MAX_SUMMARY_BYTES, MAX_SUMMARY_LINES

    try:
        if not path.is_file():
            return None, "file_missing"
        size = path.stat().st_size
        if size > MAX_SUMMARY_BYTES:
            return None, "too_large"
        raw = path.read_bytes()
    except OSError:
        return None, "unreadable"
    if b"\0" in raw[:_BINARY_PROBE_BYTES]:
        return None, "binary"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "binary"
    if len(text.splitlines()) > MAX_SUMMARY_LINES:
        return None, "too_many_lines"
    return text, ""


def _summarize_candidate(source: str, cwd: str) -> dict[str, Any]:
    from tools.read_summary import summarize_read

    path = _resolve_candidate(source, cwd)
    text, reason = _read_candidate(path)
    if text is None:
        return {"source": source, "reason": reason}
    summary = summarize_read(text, source, len(text.splitlines()))
    if summary is None:
        return {"source": source, "reason": "not_summarizable"}
    return {
        "source": source,
        "summary": summary.content,
        "kept_lines": summary.kept_lines,
        "total_lines": summary.total_lines,
        "elided_ranges": [f"{start}-{end}" for start, end in summary.elided],
    }


def _derived_sources(run: Any) -> list[dict[str, Any]]:
    """One local (no-LLM) structural pass over the files the run touched."""
    transcript = Path(str(_run_value(run, "path", "") or ""))
    if not transcript.is_file():
        return []
    cwd = str(_run_value(run, "project_dir", "") or os.getcwd())
    return [_summarize_candidate(source, cwd) for source in _candidate_paths(transcript)]


def _sources(run: Any, carried: Any = ()
            ) -> tuple[list[dict[str, Any]], Optional[Path]]:
    """``(sources, legacy_dir)`` for ``_ast.json``; legacy migration is one-shot.

    *carried* is the run's summary list from :attr:`AgentRunResult.summary_sources` — the
    buffer itself is already cleared by the time settle runs. An empty run with a still-live
    buffer (a direct/keep-alive call) drains it instead.
    """
    from tools import read_summary

    session_id = str(_run_value(run, "agent_session_id", "") or "")
    agent_name = str(_run_value(run, "agent_name", "") or "")
    collected = [dict(entry) for entry in carried
                 if isinstance(entry, dict)] if carried else \
        read_summary.collect_summaries(session_id)
    legacy, legacy_dir = (
        read_summary.legacy_summaries(agent_name, session_id) if agent_name else ([], None))
    sources = _merge_sources(collected, legacy)
    if not sources:
        sources = _derived_sources(run)
    return sources, (legacy_dir if legacy else None)


def _write_report(path: Path, status: str, body: str) -> bool:
    if status != "completed" or not body:
        return False
    try:
        _atomic_text(path, body)
        return True
    except OSError:
        logger.warning("run artifacts: report write failed for %s", path, exc_info=True)
        return False


def _write_structured(run: Any, status: str, body: str, path: Path) -> bool:
    """``<topic>.json`` only when the definition carried an output schema and it parsed."""
    schema = _run_value(run, "output_schema")
    if status != "completed" or not schema or not body:
        return False
    try:
        from tools.delegation_output_schema import extract_json_candidate

        payload = json.loads(extract_json_candidate(body))
        _atomic_json(path, payload)
        return True
    except (OSError, TypeError, ValueError):
        logger.debug("run artifacts: structured output not written for %s", path, exc_info=True)
        return False


def _write_ast(path: Path, run: Any, sources: list[dict[str, Any]], reason: str) -> bool:
    payload = {
        "version": AST_VERSION,
        "agent_session_id": str(_run_value(run, "agent_session_id", "") or ""),
        "subject": str(_run_value(run, "subject", "") or ""),
        "generated_at": _now_iso(),
        "sources": sources,
    }
    if reason:
        payload["reason"] = reason
    try:
        _atomic_json(path, payload)
        return True
    except OSError:
        logger.warning("run artifacts: ast summary write failed for %s", path, exc_info=True)
        return False


def settle_bundle(run: Any, status: str, body: str,
                  sources: Any = ()) -> Optional[dict[str, Any]]:
    """Write ``.md`` / ``_ast.json`` / optional ``.json`` and return the artifact manifest.

    Called from the settle path only, after the run's transcript exists. Returns ``None`` when
    the run has no bundle (pre-P2 caller) or the manifest itself could not be built.
    """
    bundle = _run_value(run, "bundle")
    if bundle is None:
        return None
    bundle = Path(bundle)
    subject = str(_run_value(run, "subject", "") or "")
    topic = str(_run_value(run, "topic", "") or "")
    transcript = Path(str(_run_value(run, "path", "") or ""))
    report = session_paths.report_path(bundle, subject, topic=topic)
    ast = session_paths.ast_summary_path(bundle, subject, topic=topic)
    structured = session_paths.structured_output_path(bundle, subject, topic=topic)
    tool_dir = session_paths.subagent_dir(bundle, subject, topic=topic)

    try:
        resolved, legacy_dir = _sources(run, sources)
    except Exception:  # health: allow BLE001 -- a summary failure must not lose the report
        logger.warning("run artifacts: source collection failed", exc_info=True)
        resolved, legacy_dir = [], None
    reason = "" if resolved else "no_structural_sources"
    if not reason and status != "completed":
        reason = "run_error"
    report_written = _write_report(report, status, body)
    ast_written = _write_ast(ast, run, resolved, reason)
    structured_written = _write_structured(run, status, body, structured)
    if ast_written and legacy_dir is not None:
        from tools.read_summary import remove_legacy_summaries

        remove_legacy_summaries(legacy_dir)
    return {
        "version": 1,
        "status": str(status or ""),
        "agent_session_id": str(_run_value(run, "agent_session_id", "") or ""),
        "subject": subject,
        "bundle_dir": str(bundle),
        "transcript_path": str(transcript),
        "report_path": str(report),
        "report_written": report_written,
        "ast_summary_path": str(ast),
        "ast_summary_written": ast_written,
        "structured_output_path": str(structured),
        "structured_output_written": structured_written,
        "tool_artifacts_dir": str(tool_dir),
        "written_at": _now_iso(),
    }


def _bundle_from_transcript(transcript: Optional[Path], row: dict[str, Any]) -> Optional[Path]:
    """Recover the bundle of a new-layout transcript; legacy paths return ``None``."""
    if transcript is None:
        return None
    parent = transcript.parent
    for key in (row.get("agent_session_id"), row.get("parent_session_id")):
        ident = str(key or "")
        if ident and parent.name.endswith("_" + session_paths.sanitize_component(ident, "session")):
            return parent
    return None


def identity_for_row(row: dict[str, Any]) -> dict[str, Any]:
    """``bundle_dir`` / ``report_path`` / ``ast_summary_path`` for ``agent_out`` (ADR-16 D5).

    The manifest is the authoritative mapping; a pre-P2 row falls back to deriving the bundle
    from its recorded transcript path, and if that is a legacy path the artifact paths are
    ``None`` (the files were never written).
    """
    raw_manifest = row.get("artifact_manifest")
    manifest: dict[str, Any] = raw_manifest if isinstance(raw_manifest, dict) else {}
    subject = str(row.get("subject") or "")
    raw_transcript = str(row.get("transcript_path") or "").strip()
    transcript = Path(raw_transcript) if raw_transcript else None
    bundle: Optional[Path] = None
    if manifest.get("bundle_dir"):
        bundle = Path(str(manifest["bundle_dir"]))
    else:
        bundle = _bundle_from_transcript(transcript, row)
    tool_dir: Optional[Path] = None
    if manifest.get("tool_artifacts_dir"):
        tool_dir = Path(str(manifest["tool_artifacts_dir"]))
    elif bundle is not None:
        tool_dir = session_paths.subagent_dir(bundle, subject)
    report = session_paths.report_path(bundle, subject) if bundle is not None else None
    ast = session_paths.ast_summary_path(bundle, subject) if bundle is not None else None
    structured: Optional[Path] = None
    if manifest.get("structured_output_path"):
        structured = Path(str(manifest["structured_output_path"]))
    elif bundle is not None:
        structured = session_paths.structured_output_path(bundle, subject)
    return {
        "bundle_dir": str(bundle) if bundle is not None else None,
        "transcript_path": raw_transcript or None,
        "report_path": str(report) if report is not None else None,
        "ast_summary_path": str(ast) if ast is not None else None,
        "structured_output_path": str(structured) if structured is not None else None,
        "tool_artifacts_dir": str(tool_dir) if tool_dir is not None else None,
    }


__all__ = ["AST_VERSION", "bundle_for_run", "identity_for_row", "settle_bundle"]
