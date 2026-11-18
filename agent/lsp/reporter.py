"""Format LSP diagnostics for the model block and for omp's late-diagnostics card.

Two renderings share one filtered verdict (ADR-9 D2): the model reads a compact, severity-filtered,
line-bounded ``<diagnostics>`` block (HTML-escaped, 1-indexed line/column); a client reads
``details.files[]`` lines in pi-tui's ``path:line:col [severity] [source] message (code)`` shape
(plain text, no entities, every line parseable by ``parseDiagnosticMessage``).
"""
from __future__ import annotations

import html
import re
from typing import Any, Optional

from agent.compression_marker import elide

# ERROR only by default — warnings/info/hints would flood the agent.
SEVERITY_NAMES = {1: "ERROR", 2: "WARN", 3: "INFO", 4: "HINT"}
SEVERITY_WORDS = {1: "error", 2: "warning", 3: "info", 4: "hint"}
DEFAULT_SEVERITIES = frozenset({1})

MAX_PER_FILE = 20
MAX_TOTAL_CHARS = 4000

# Display caps (ADR-9 D2): independent from the model block and LARGER — the card shows what the
# compact model text truncates. Every retained line stays parseable; an over-limit line is dropped
# whole, never cut in half, and no synthetic "... N more" line enters ``details.messages``.
MAX_DISPLAY_PER_FILE = 200
MAX_DISPLAY_TOTAL_CHARS = 32000

# Per-field caps bound any single attacker-controlled identifier that can
# ride into the model's tool output via an LSP diagnostic message.
MAX_MESSAGE_CHARS = 300
MAX_CODE_CHARS = 80
MAX_SOURCE_CHARS = 80
MAX_PATH_CHARS = 1024


def _sanitize_field(value: Any, *, limit: int) -> str:
    """Make a language-server field safe to embed in a tool-result block.

    ``message``/``code``/``source`` come from a server that just parsed user-controlled code, so a
    hostile repo can smuggle instruction-shaped text through identifier names.  We collapse CR/LF,
    drop control chars, cap the length, and HTML-escape ``< > &`` so the text can't close
    ``<diagnostics>`` early.  ``None``/empty → ``""`` so callers can omit the part.
    """
    if value is None:
        return ""
    raw = str(value).replace("\r", " ").replace("\n", " ")
    raw = "".join(ch for ch in raw if ch == " " or ch.isprintable())
    return html.escape(raw.strip()[:limit], quote=False)


def format_diagnostic(d: dict[str, Any]) -> str:
    """One-line representation of a single diagnostic (fields sanitized)."""
    sev = SEVERITY_NAMES.get(d.get("severity") or 1, "ERROR")
    start = (d.get("range") or {}).get("start") or {}
    line = int(start.get("line", 0)) + 1
    col = int(start.get("character", 0)) + 1
    msg = _sanitize_field(d.get("message"), limit=MAX_MESSAGE_CHARS)
    code = _sanitize_field(d.get("code"), limit=MAX_CODE_CHARS)
    source = _sanitize_field(d.get("source"), limit=MAX_SOURCE_CHARS)
    return f"{sev} [{line}:{col}] {msg}{f' [{code}]' if code else ''}{f' ({source})' if source else ''}"


def report_for_file(
    file_path: str,
    diagnostics: list[dict[str, Any]],
    *,
    severities: frozenset = DEFAULT_SEVERITIES,
    max_per_file: int = MAX_PER_FILE,
) -> str:
    """Build a ``<diagnostics file=...>`` block; ``""`` when nothing passes the severity filter."""
    filtered = [d for d in diagnostics or [] if (d.get("severity") or 1) in severities]
    if not filtered:
        return ""
    body = "\n".join(format_diagnostic(d) for d in filtered[:max_per_file])
    if len(filtered) > max_per_file:
        body += f"\n... and {len(filtered) - max_per_file} more"
    # quote=True also escapes ``"`` so a crafted file name can't break out of
    # the ``file="..."`` attribute and synthesize new tags.
    safe_path = html.escape(file_path, quote=True)
    return f"<diagnostics file=\"{safe_path}\">\n{body}\n</diagnostics>"


def truncate(s: str, *, limit: int = MAX_TOTAL_CHARS) -> str:
    """Hard-cap a formatted summary string with the non-imitable elision marker."""
    return elide(s, limit)


# ── omp late-diagnostics payload (ADR-9 D2) ──────────────────────────────────────────────────────

def _plain_field(value: Any, *, limit: int, brackets: bool = False) -> str:
    """Plain-text field for a pi-tui diagnostic line: no HTML entities, one line, capped.

    The line must match ``parseDiagnosticMessage``'s regex, so ``source`` may not smuggle the
    closing ``]`` and the whole line may not carry CR/LF. Unlike the model block this text is
    rendered, not interpreted: ``<`` and ``&`` stay as written.
    """
    if value is None:
        return ""
    raw = str(value).replace("\r", " ").replace("\n", " ")
    raw = "".join(ch for ch in raw if ch == " " or ch.isprintable()).strip()
    if brackets:
        raw = raw.replace("[", "(").replace("]", ")")
    return raw[:limit]


def _display_path(path: str) -> str:
    """The file path a line starts with and ``files[].path`` carries: identical, one line."""
    return _plain_field(path, limit=MAX_PATH_CHARS) or str(path)


def display_line(path: str, d: dict[str, Any]) -> str:
    """One pi-tui-parseable line: ``path:line:col [severity] [source] message (code)``."""
    sev = SEVERITY_WORDS.get(d.get("severity") or 1, "error")
    start = (d.get("range") or {}).get("start") or {}
    line = int(start.get("line", 0)) + 1
    col = int(start.get("character", 0)) + 1
    msg = _plain_field(d.get("message"), limit=MAX_MESSAGE_CHARS) or "(no message)"
    code = _plain_field(d.get("code"), limit=MAX_CODE_CHARS, brackets=True)
    source = _plain_field(d.get("source"), limit=MAX_SOURCE_CHARS, brackets=True)
    head = f"{path}:{line}:{col} [{sev}]"
    return f"{head} [{source}] {msg}" + (f" ({code})" if code else "")


def file_entry(
    path: str,
    diagnostics: list[dict[str, Any]],
    *,
    severities: frozenset = DEFAULT_SEVERITIES,
) -> Optional[dict[str, Any]]:
    """One ``details.files[]`` member (uncapped), or ``None`` when nothing passes the filter.

    ``summary`` counts the whole filtered set, exactly like omp (``"2 error(s), 1 warning(s)"``);
    ``errored`` is true only when an error is present. ``messages`` is every retained line.
    """
    filtered = [d for d in diagnostics or [] if (d.get("severity") or 1) in severities]
    if not filtered:
        return None
    shown_path = _display_path(path)
    counts = [(word, sum(1 for d in filtered if (d.get("severity") or 1) == sev))
              for sev, word in SEVERITY_WORDS.items() if sev in severities]
    summary = ", ".join(f"{count} {word}(s)" for word, count in counts if count)
    return {
        "path": shown_path,
        "summary": summary,
        "errored": any((d.get("severity") or 1) == 1 for d in filtered),
        "messages": [display_line(shown_path, d) for d in filtered],
    }


def merge_file_entries(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge each path's entries from several sources (LSP first, then Semgrep) into one entry.

    ``summary``/``errored`` are recomputed over the combined set (ADR-9 D8: a second source always
    recomputes the counts, never overwrites the first).  Messages keep their source order; the
    caller's groups already carry the per-source order (ADR-9 D2: pi-tui sorts inside a file, the
    shell never re-sorts).  Single-source entries are returned as-is.
    """
    order: list[str] = []
    by_path: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        for entry in group or ():
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not isinstance(path, str) or not path:
                continue
            if path not in by_path:
                order.append(path)
                by_path[path] = []
            by_path[path].append(entry)
    merged: list[dict[str, Any]] = []
    for path in order:
        entries = by_path[path]
        if len(entries) == 1:
            merged.append(entries[0])
            continue
        counts: dict[str, int] = {}
        for entry in entries:
            for word, count in _summary_counts(str(entry.get("summary") or "")).items():
                counts[word] = counts.get(word, 0) + count
        summary = ", ".join(f"{counts[word]} {word}(s)" for word in ("error", "warning") if counts.get(word))
        merged.append({
            "path": path,
            "summary": summary,
            "errored": any(bool(entry.get("errored")) for entry in entries),
            "messages": [message for entry in entries for message in entry.get("messages") or []],
        })
    return merged


_SUMMARY_COUNT = re.compile(r"(\d+)\s+(error|warning)\(s\)")


def _summary_counts(summary: str) -> dict[str, int]:
    """Counts back out of our own ``"2 error(s), 1 warning(s)"`` (fixed format, generated above)."""
    return {word: int(count) for count, word in _SUMMARY_COUNT.findall(summary)}


def cap_display_files(
    files: list[dict[str, Any]],
    *,
    per_file: int = MAX_DISPLAY_PER_FILE,
    total_chars: int = MAX_DISPLAY_TOTAL_CHARS,
) -> list[dict[str, Any]]:
    """Bound the client payload: drop whole over-limit lines, keep ``summary``/``errored`` intact."""
    remaining = total_chars
    capped: list[dict[str, Any]] = []
    for entry in files:
        kept: list[str] = []
        for line in entry.get("messages") or []:
            if len(kept) >= per_file or len(line) > remaining:
                break
            kept.append(line)
            remaining -= len(line)
        capped.append({**entry, "messages": kept})
    return capped


def model_text(files: list[dict[str, Any]], *, per_file: int = MAX_PER_FILE,
               total_chars: int = MAX_TOTAL_CHARS) -> str:
    """The source-neutral ``<system-notice>`` a custom late-diagnostics row carries to the model.

    It reads the same filtered ``files`` the client renders (ADR-9 D12: no UI-only filter). Timing
    is not claimed: the row may carry a budget-hit verdict under ``mode=late`` as well as a truly
    late one.
    """
    if not files:
        return ""
    sections: list[str] = []
    for entry in files:
        # The model reads escaped fields (ADR-9 D2: `_sanitize_field` + html.escape only on the
        # model side); `details` keeps the same facts unescaped for the renderer.
        summary = html.escape(str(entry.get("summary") or ""), quote=False)
        path = html.escape(str(entry.get("path") or ""), quote=False)
        messages = (entry.get("messages") or [])[:per_file]
        if not summary and not messages:
            continue  # a path-only entry says nothing; never fabricate a header for it
        lines = [f"{path} — {summary}" if summary else path]
        lines.extend(html.escape(line, quote=False) for line in messages)
        sections.append("\n".join(lines))
    body = "\n\n".join(section for section in sections if section)
    if not body:
        return ""
    text = ("<system-notice>\nDiagnostics for the files you edited:\n"
            f"{body}\n\nFix them if they are real, then continue.\n</system-notice>")
    return truncate(text, limit=total_chars)


__all__ = [
    "DEFAULT_SEVERITIES", "MAX_DISPLAY_PER_FILE", "MAX_DISPLAY_TOTAL_CHARS", "MAX_PER_FILE",
    "MAX_TOTAL_CHARS", "SEVERITY_NAMES", "SEVERITY_WORDS", "cap_display_files", "display_line",
    "file_entry", "format_diagnostic", "merge_file_entries", "model_text", "report_for_file",
    "truncate",
]
