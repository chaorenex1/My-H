"""Structural summaries for ``read_file`` (omp's ``read.summarize`` / ``read-summarize``).

A summary keeps declarations and drops implementation bodies, replacing each elided span with a
concrete re-read hint. It is deliberately conservative:

* only a whole-file read (no explicit ``offset``/``limit``) is eligible;
* prose is skipped unless ``read.summarize.prose`` is on;
* files below ``read.summarize.min_total_lines`` (default 100) and above the hard byte/line guards
  stay verbatim;
* the result is returned only when it is materially smaller than the file.

The structural engine order is tree-sitter first (the optional codegraph extractor), then a
stdlib ``ast`` path for Python, then a conservative declaration heuristic for other languages.
On an install without the codegraph extra, Python still summarizes via ``ast`` and other
languages fall back to the heuristic; absence of the extra is a normal fallback, never an error.
Persisted copies no longer live on disk: a run's summaries are collected in memory and
flushed by the settle path into the session bundle's ``<topic>_ast.json`` (ADR-16 D4).
A pre-ADR-16 ``.summaries/`` directory is read once for migration and then removed.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: omp's hard guards (``read-summary.ts``): never try to summarize a pathological file.
MAX_SUMMARY_BYTES = 2 * 1024 * 1024
MAX_SUMMARY_LINES = 20_000
DEFAULT_MIN_TOTAL_LINES = 100
PROSE_SUFFIXES = frozenset({".md", ".markdown", ".mdx", ".txt", ".rst"})
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
_MAX_LINE_CHARS = 400

#: Conservative declaration heuristic for languages whose tree-sitter grammar is not installed.
#: It only claims a line at shallow indentation whose first keywords name a declaration, plus
#: module-level imports/package headers; bodies and expressions stay elided.
_DECLARATION_RE = re.compile(
    r"^[ \t]{0,4}(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:public\s+|private\s+|protected\s+|static\s+|abstract\s+|final\s+|sealed\s+|data\s+)*"
    r"(?:function|class|interface|struct|enum|trait|impl|type|func|fn|def|module|namespace|"
    r"record|object)\b")
_IMPORT_RE = re.compile(
    r"^[ \t]*(?:import|from|use|package|#include|require|using)\b")
_CONTAINER_CLOSE_RE = re.compile(r"^[}\])];?[ \t]*$")

_override_lock = threading.Lock()
_overrides: dict[str, bool] = {}
_contexts: dict[str, tuple[str, str]] = {}
#: Run-scoped structural summaries, flushed into ``<topic>_ast.json`` at settle (ADR-16 D4).
_collected: dict[str, dict[str, dict[str, Any]]] = {}


@dataclass(frozen=True)
class ReadSummary:
    """One rendered summary plus the line spans it elided."""

    content: str
    total_lines: int
    elided: tuple[tuple[int, int], ...]
    kept_lines: int


def set_read_summarize(task_id: str, enabled: Optional[bool] = None, *, agent_name: str = "",
                       agent_session_id: str = "") -> None:
    """Register this run's summary switch (``None`` = inherit config) and session directory."""
    key = str(task_id or "").strip()
    if not key:
        return
    with _override_lock:
        if enabled is not None:
            _overrides[key] = bool(enabled)
        if agent_name and agent_session_id:
            _contexts[key] = (str(agent_name), str(agent_session_id))


def clear_read_summarize(task_id: str) -> None:
    key = str(task_id or "").strip()
    if not key:
        return
    with _override_lock:
        _overrides.pop(key, None)
        _contexts.pop(key, None)
        _collected.pop(key, None)


def _read_settings() -> dict[str, Any]:
    """``read.summarize`` from the active profile config.

    No module-level cache: one process serves many profiles, and a process-lifetime slot would
    pin the launch profile's value. ``load_config_readonly`` is already mtime+path cached.
    """
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        read = config.get("read") if isinstance(config, dict) else None
        raw = read.get("summarize") if isinstance(read, dict) else None
        return raw if isinstance(raw, dict) else {}
    except Exception:  # health: allow BLE001 -- summaries are optional, config may be unreadable
        logger.debug("read.summarize settings unavailable", exc_info=True)
        return {}


def _bool(value: Any, default: bool) -> bool:
    return bool(value) if isinstance(value, bool) else default


def _int(value: Any, default: int, *, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def read_summarize_enabled(task_id: str) -> bool:
    """This run's switch: the per-agent override, else ``read.summarize.enabled``."""
    key = str(task_id or "").strip()
    if key:
        with _override_lock:
            if key in _overrides:
                return _overrides[key]
    return _bool(_read_settings().get("enabled"), False)


def persist_summary(task_id: str, source: str, summary: ReadSummary, *,
                    content: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Record one summary for this run; the settle path writes it into ``_ast.json``.

    ADR-16 D4 retired the on-disk ``.summaries/`` directory: a summary is run state, not a
    second read surface. Returns the collected entry (no path — nothing is written yet), or
    ``None`` for a non-agent run with no session context.
    """
    key = str(task_id or "").strip()
    with _override_lock:
        context = _contexts.get(key)
    if not context:
        return None
    source_text = str(source)
    entry = {
        "source": source_text,
        "summary": content if content is not None else summary.content,
        "kept_lines": summary.kept_lines,
        "total_lines": summary.total_lines,
        "elided_ranges": [f"{start}-{end}" for start, end in summary.elided],
    }
    with _override_lock:
        _collected.setdefault(key, {})[source_text] = entry
    return dict(entry)


def collect_summaries(task_id: str) -> list[dict[str, Any]]:
    """Drain this run's summaries (source-sorted); the settle path is the only consumer."""
    key = str(task_id or "").strip()
    if not key:
        return []
    with _override_lock:
        entries = _collected.pop(key, {})
    return [dict(entries[source]) for source in sorted(entries)]


def legacy_summaries(agent_name: str, agent_session_id: str) -> tuple[list[dict[str, Any]], Path]:
    """Read a pre-ADR-16 ``.summaries/`` directory as ``_ast.json`` sources (one-time migration).

    Returns ``(sources, directory)``; an absent or unreadable index is an empty list. Bodies the
    index names but cannot read are skipped — a migration never invents a summary.
    """
    from agent import run_transcript

    directory = run_transcript.summary_dir(agent_name, agent_session_id)
    index_path = directory / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [], directory
    entries = index.get("summaries") if isinstance(index, dict) else None
    sources: list[dict[str, Any]] = []
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "")
        name = Path(str(entry.get("file") or "")).name
        if not source or not name:
            continue
        try:
            body = (directory / name).read_text(encoding="utf-8-sig")
        except OSError:
            continue
        sources.append({
            "source": source,
            "summary": body,
            "kept_lines": entry.get("kept_lines"),
            "total_lines": entry.get("total_lines"),
            "elided_ranges": list(entry.get("elided_ranges") or ()),
        })
    return sources, directory


def remove_legacy_summaries(directory: Path) -> bool:
    """Delete a migrated ``.summaries/`` directory; ``False`` when it could not be removed."""
    import shutil

    try:
        shutil.rmtree(directory)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        logger.warning("read summary: legacy directory removal failed for %s", directory,
                       exc_info=True)
        return False


def summarize_read(text: str, path: str, total_lines: int) -> Optional[ReadSummary]:
    """Summarize *text* when it is eligible; ``None`` means "return the file verbatim"."""
    settings = _read_settings()
    if not text or total_lines <= 0:
        return None
    if len(text.encode("utf-8", errors="replace")) > MAX_SUMMARY_BYTES:
        return None
    if total_lines > MAX_SUMMARY_LINES:
        return None
    min_lines = _int(settings.get("min_total_lines"), DEFAULT_MIN_TOTAL_LINES,
                     low=1, high=MAX_SUMMARY_LINES)
    if total_lines < min_lines:
        return None
    suffix = Path(path).suffix.lower()
    if suffix in PROSE_SUFFIXES and not _bool(settings.get("prose"), False):
        return None
    # tree-sitter is the primary structural engine (omp parity). The stdlib/heuristic paths
    # keep the feature useful on installs without the optional codegraph grammars.
    summary = _summarize_tree_sitter(text, path, total_lines)
    if summary is not None:
        return summary
    if suffix in PYTHON_SUFFIXES:
        return _summarize_python(text, path, total_lines)
    return _summarize_declarations(text, path, total_lines)


def _render(lines: list[str], kept: set[int], path: str, total_lines: int) -> Optional[ReadSummary]:
    """Render kept lines and one marker per elided span; reject a summary that barely shrinks."""
    if not kept:
        return None
    kept = {line for line in kept if 1 <= line <= total_lines}
    rendered: list[str] = [
        "[read summary: structural; "
        f"{len(kept)} of {total_lines} lines shown; re-read a span with "
        "read_file(offset=..., limit=...)]"
    ]
    elided: list[tuple[int, int]] = []
    line_no = 1
    while line_no <= total_lines:
        if line_no in kept:
            rendered.append(f"{line_no}|{_clamp(lines[line_no - 1])}")
            line_no += 1
            continue
        start = line_no
        while line_no <= total_lines and line_no not in kept:
            line_no += 1
        end = line_no - 1
        elided.append((start, end))
        rendered.append(f"{start}-{end}|... ({end - start + 1} line(s) elided; "
                        f"read_file(path={path!r}, offset={start}, "
                        f"limit={end - start + 1}))")
    if len(rendered) >= max(1, int(total_lines * 0.8)):
        return None
    return ReadSummary(content="\n".join(rendered), total_lines=total_lines,
                       elided=tuple(elided), kept_lines=len(kept))


def _clamp(line: str) -> str:
    return line if len(line) <= _MAX_LINE_CHARS else line[:_MAX_LINE_CHARS] + " ..."


def _keep_all(node: ast.AST, kept: set[int], *, limit: Optional[int] = None) -> None:
    start = int(getattr(node, "lineno", 0) or 0)
    end = int(getattr(node, "end_lineno", start) or start)
    if not start or not end:
        return
    if limit is not None and end - start + 1 > limit:
        end = start + limit - 1
    kept.update(range(start, end + 1))


def _keep_header(node: ast.AST, kept: set[int]) -> None:
    """Keep decorators and the signature/header, stopping at the first body statement."""
    for decorator in getattr(node, "decorator_list", ()) or ():
        _keep_all(decorator, kept)
    start = int(getattr(node, "lineno", 0) or 0)
    body = getattr(node, "body", None) or ()
    first_body = int(getattr(body[0], "lineno", start) or start) if body else start
    end = max(start, first_body - 1)
    kept.update(range(start, end + 1))


def _docstring_lines(node: ast.AST) -> set[int]:
    body = getattr(node, "body", None) or ()
    if not body:
        return set()
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
            and isinstance(first.value.value, str):
        start = int(getattr(first, "lineno", 0) or 0)
        end = int(getattr(first, "end_lineno", start) or start)
        return set(range(start, end + 1)) if start else set()
    return set()


def _visit_python(node: ast.AST, kept: set[int], *, top_level: bool = False) -> None:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        _keep_all(node, kept)
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _keep_header(node, kept)
        if int(getattr(node, "end_lineno", 0) or 0) == int(getattr(node, "lineno", 0) or 0):
            _keep_all(node, kept)  # one-line function: nothing to elide
            return
        kept.update(_docstring_lines(node))
        return
    if isinstance(node, ast.ClassDef):
        _keep_header(node, kept)
        kept.update(_docstring_lines(node))
        for child in node.body:
            if isinstance(child, (ast.Assign, ast.AnnAssign)):
                _keep_all(child, kept, limit=3)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                _visit_python(child, kept)
        return
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and top_level:
        _keep_all(node, kept, limit=3)
        return
    if isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.While)):
        _keep_header(node, kept)
        for field in ("body", "orelse", "finalbody"):
            for child in getattr(node, field, ()) or ():
                _visit_python(child, kept)
        for handler in getattr(node, "handlers", ()) or ():
            for child in getattr(handler, "body", ()) or ():
                _visit_python(child, kept)
        return
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
        _keep_all(node, kept)  # module/class-level string literal


def _summarize_python(text: str, path: str, total_lines: int) -> Optional[ReadSummary]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.splitlines()
    kept: set[int] = set()
    for node in tree.body:
        _visit_python(node, kept, top_level=True)
    return _render(lines, kept, path, total_lines)


def _summarize_declarations(text: str, path: str, total_lines: int) -> Optional[ReadSummary]:
    """Language-agnostic outline when no tree-sitter grammar is available.

    The heuristic is intentionally strict: only shallow declarations, imports and container
    closers are kept, so a false positive adds one line rather than leaking a body. It is still
    a heuristic, not a parser; the marker says ``structural`` and the footer points at the
    exact re-read range.
    """
    lines = text.splitlines()
    kept = set(range(1, min(total_lines, 20) + 1))
    found = False
    for number, line in enumerate(lines, start=1):
        if number > total_lines:
            break
        if _IMPORT_RE.match(line) or _DECLARATION_RE.match(line) \
                or _CONTAINER_CLOSE_RE.match(line):
            kept.add(number)
            found = found or number > 20
    if not found:
        return None
    return _render(lines, kept, path, total_lines)


def _render_tree_sitter(parsed: Any, lines: list[str], path: str,
                        total_lines: int) -> Optional[ReadSummary]:
    """Render one ``ExtractedFile`` as an outline without dragging arbitrary header lines in."""
    symbols = sorted((symbol for symbol in getattr(parsed, "symbols", ()) if symbol.start_line),
                     key=lambda symbol: symbol.start_line)
    imports = sorted((item for item in getattr(parsed, "imports", ()) if item.start_line),
                     key=lambda item: item.start_line)
    if not symbols and not imports:
        return None
    kept: set[int] = set()
    first_symbol = symbols[0].start_line if symbols else total_lines + 1
    # Only the pre-symbol header (license/docstring/imports) — never a blanket first N lines.
    kept.update(range(1, max(0, min(first_symbol - 1, 20)) + 1))
    for item in imports:
        kept.update(range(max(1, item.start_line), max(1, item.end_line) + 1))
    containers = ("class", "struct", "impl", "interface", "trait", "enum")
    for symbol in symbols:
        start = max(1, symbol.start_line)
        # Decorators/attributes directly above a declaration belong to it.
        for number in range(max(1, start - 3), start):
            if lines[number - 1].lstrip().startswith(("@", "[")):
                kept.add(number)
        kept.add(start)
        if symbol.end_line > symbol.start_line and symbol.kind in containers:
            kept.add(symbol.end_line)
    return _render(lines, kept, path, total_lines)


def _summarize_tree_sitter(text: str, path: str, total_lines: int) -> Optional[ReadSummary]:
    """Outline from the optional codegraph tree-sitter extractor (primary structural path)."""
    try:
        from hermes_codegraph.adapters.languages.extractor import extract
        from hermes_codegraph.languages import default_registry
    except Exception:  # health: allow BLE001 -- the extra is optional
        return None
    try:
        descriptor = default_registry().by_path(path)
        if descriptor is None:
            return None
        parsed = extract(text.encode("utf-8", errors="replace"), descriptor, path)
    except Exception:  # health: allow BLE001 -- any extractor failure means verbatim
        logger.debug("read summary: tree-sitter extraction failed for %s", path, exc_info=True)
        return None
    return _render_tree_sitter(parsed, text.splitlines(), path, total_lines)


__all__ = ["MAX_SUMMARY_BYTES", "MAX_SUMMARY_LINES", "PROSE_SUFFIXES", "ReadSummary",
           "clear_read_summarize", "collect_summaries", "legacy_summaries", "persist_summary",
           "read_summarize_enabled", "remove_legacy_summaries", "set_read_summarize",
           "summarize_read"]
