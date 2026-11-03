"""Read-only semantic find: the engine behind ``judgglob`` / ``judggrep`` (ADR-11 D14).

Both tools build *only* the restricted ``noul`` questions this module defines — a
caller cannot pass a questions map or a state, so the judgment engine never becomes
a general classifier. Everything here is read-only: no file writes, no git, no
hidden/credential/binary files, bounded by the caps below.

``judgglob`` ranks paths by name/location. ``judggrep`` runs a small cascade:
keyword prefilter → sketch scoring → verification of the surviving windows.
"""

from __future__ import annotations

import contextvars
import fnmatch
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from agent.judgment.chain import has_native_judge, resolve_judge
from agent.judgment.config import load_judgment_settings
from agent.judgment.types import (
    Judge,
    JudgmentAborted,
    JudgmentError,
    JudgmentRequest,
    JudgmentUnavailable,
    NoulAnswer,
    NoulQuestion,
)
from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

GLOB_NAME_BATCH = 64
GLOB_CANDIDATES = 128
GREP_FILES = 20
GREP_WINDOWS = 24
GREP_WINDOW_BYTES = 8 * 1024
GREP_SKETCH_BYTES = 384
GREP_SKETCH_CARDS = 46
GREP_FULL_LIMIT = 40
GREP_CUTOFF = 0.45
GREP_THRESHOLD = 0.20
GREP_READ_LIMIT = 4 * 1024 * 1024
GREP_PARALLEL = 16
GREP_TIMEOUT_S = 20.0
SKETCH_STATE_BYTES = 18_000
VERIFY_STATE_BYTES = 24 * 1024
SCAN_FILE_LIMIT = 4_000
SNIPPET_CHARS = 200

TASK = (
    "Semantic search over a source tree: decide whether each entry matches a "
    "plain-language description. Entries are judged independently."
)

_IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".bzr", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".tox", ".nox",
    ".idea", ".vscode", ".next", ".nuxt", ".turbo", ".parcel-cache",
})
_CREDENTIAL_NAMES = frozenset({
    ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials", "id_rsa", "id_dsa",
    "id_ecdsa", "id_ed25519", "credentials", "secrets",
})
_CREDENTIAL_PATTERNS = (
    ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "credentials*.json", "secrets.*",
)
_STOPWORDS = frozenset("""
a an and are as at be been being between but by can could did do does for from had has have
how if in into is it its just like made make makes more most new not of off on one only or
other our out over per run runs should so some such than that the their them then there these
they thing things this through to two up use used user using via was way we were what when
where which will with within without would you your
""".split())


# ── keywords and paths ──────────────────────────────────────────────────────


def _stem(token: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def keywords_from_query(query: str, extra: Sequence[str] = ()) -> list[str]:
    """Quoted phrases whole, then tokens minus stopwords; plus caller keywords."""
    found: list[str] = []
    rest = ""
    chars = list(query)
    index = 0
    while index < len(chars):
        char = chars[index]
        if char not in "\"'":
            rest += char
            index += 1
            continue
        index += 1
        phrase = ""
        closed = False
        while index < len(chars):
            if chars[index] == char:
                closed = True
                index += 1
                break
            phrase += chars[index]
            index += 1
        phrase = phrase.strip().lower()
        if closed and len(phrase.encode("utf-8")) >= 3:
            if phrase not in found:
                found.append(phrase)
            rest += " "
        else:
            rest += f"{phrase} "
    for token in re.split(r"[^\w]+", rest, flags=re.UNICODE):
        lower = token.lower()
        if len(lower.encode("utf-8")) < 3 or lower.isdigit() or lower in _STOPWORDS:
            continue
        stemmed = _stem(lower)
        if stemmed not in found:
            found.append(stemmed)
    for raw in extra:
        keyword = str(raw).strip().lower()
        if keyword and keyword not in found:
            found.append(keyword)
    return found


@dataclass(frozen=True)
class FileEntry:
    path: Path
    rel: str
    size: int


def _display_path(path: Path) -> str:
    try:
        return Path(os.path.relpath(path, Path.cwd())).as_posix()
    except ValueError:  # different drive on Windows
        return path.as_posix()


def _gitignore_patterns(root: Path) -> tuple[str, ...]:
    path = root / ".gitignore"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ()
    patterns: list[str] = []
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#") or entry.startswith("!"):
            continue
        patterns.append(entry)
    return tuple(patterns)


def _matches_gitignore(rel: str, name: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        directory_only = pattern.endswith("/")
        candidate = pattern.rstrip("/")
        if "/" in candidate:
            if fnmatch.fnmatchcase(rel, candidate) or rel.startswith(candidate + "/"):
                return True
        elif (fnmatch.fnmatchcase(name, candidate)
              or (directory_only and candidate in rel.split("/"))):
            return True
    return False


def _credentials_file(name: str) -> bool:
    lower = name.lower()
    if lower in _CREDENTIAL_NAMES:
        return True
    return any(fnmatch.fnmatchcase(lower, pattern) for pattern in _CREDENTIAL_PATTERNS)


def _binary_name(name: str) -> bool:
    from tools.binary_extensions import has_binary_extension

    return has_binary_extension(name)


def _eligible(path: Path, name: str, rel: str, size: int,
              patterns: Sequence[str]) -> bool:
    if size <= 0 or _credentials_file(name) or _binary_name(name):
        return False
    return not _matches_gitignore(rel, name, patterns)


def list_files(scope: Path, *, hidden: bool = False,
               deadline: float | None = None) -> list[FileEntry]:
    """Walk ``scope`` in sorted order, skipping VCS/cache dirs, credentials and binaries."""
    if scope.is_file():
        try:
            size = scope.stat().st_size
        except OSError:
            return []
        if _credentials_file(scope.name) or _binary_name(scope.name) or size <= 0:
            return []
        return [FileEntry(scope, _display_path(scope), size)]
    patterns = _gitignore_patterns(scope)
    entries: list[FileEntry] = []
    root = str(scope)
    for dirpath, dirnames, filenames in os.walk(root):
        if deadline is not None and time.monotonic() > deadline:
            break
        current = Path(dirpath)
        rel_dir = current.relative_to(scope).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        kept: list[str] = []
        for name in sorted(dirnames):
            if name in _IGNORED_DIRS:
                continue
            if not hidden and name.startswith("."):
                continue
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            if _matches_gitignore(child_rel, name, patterns):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            if not hidden and name.startswith("."):
                continue
            path = current / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if not _eligible(path, name, rel, size, patterns):
                continue
            entries.append(FileEntry(path, _display_path(path), size))
            if len(entries) >= SCAN_FILE_LIMIT:
                return entries
    return entries


def _scope(raw: str | None) -> Path:
    path = Path(str(raw)).expanduser() if raw else Path.cwd()
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"path not found: {raw or path}") from None


def _check_glob(pattern: str) -> None:
    if pattern.count("[") != pattern.count("]") or pattern.count("{") != pattern.count("}"):
        raise ValueError(f"invalid glob pattern: {pattern}")


def validate_scope(path: str | None, glob_pattern: str | None = None) -> None:
    """Reject a missing path or malformed glob before a judge is resolved."""
    _scope(path)
    if glob_pattern:
        _check_glob(glob_pattern)


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 2] == "**":
                out.append(".*")
                index += 2
                if pattern[index:index + 1] == "/":
                    index += 1
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    out.append("$")
    return re.compile("".join(out))


def _match_glob(rel: str, pattern: str) -> bool:
    candidate = pattern.replace("\\", "/").removeprefix("./")
    return bool(_glob_regex(candidate).match(rel))


# ── judging ─────────────────────────────────────────────────────────────────


def _probability(result: Any, key: str) -> float | None:
    answer = result.answers.get(key)
    if isinstance(answer, NoulAnswer):
        return float(answer.noul)
    return None


class _RunStats:
    """Counters for one find call; dispatch runs requests on worker threads."""

    def __init__(self, **extra: int) -> None:
        self._lock = threading.Lock()
        self._counters = {"requests": 0, "failed": 0, **extra}
        self.timed_out = False
        self.uncalibrated = False

    def record(self, *, api: str = "", failed: bool = False) -> None:
        with self._lock:
            self._counters["requests"] += 1
            if failed:
                self._counters["failed"] += 1
            if api == "text":
                self.uncalibrated = True

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)


def _ask(judge: Judge, request: JudgmentRequest, *, purpose: str,
         stats: _RunStats) -> tuple[Any, str]:
    try:
        result = judge.judge(request, purpose=purpose)
    except JudgmentAborted:
        raise
    except (JudgmentUnavailable, JudgmentError):
        stats.record(failed=True)
        return None, ""
    api = str(getattr(result, "api", ""))
    stats.record(api=api)
    return result, api


def _coverage(stats: _RunStats) -> str:
    counters = stats.snapshot()
    requests = counters["requests"]
    failed = counters["failed"]
    if requests and failed == requests:
        return "unavailable"
    if failed or stats.timed_out:
        return "partial"
    return "complete"


def _dispatch(jobs: Sequence[Any], worker, *, deadline: float) -> bool:
    """Run ``worker(job)`` with at most ``GREP_PARALLEL`` in flight.

    Returns True when ``deadline`` stopped jobs before they ran, so the caller can
    report ``partial`` coverage instead of a complete-looking empty result.
    """
    if not jobs:
        return False
    take_lock = threading.Lock()
    cursor = [0]
    # Judge calls write the session ledger from these threads; a bare pool thread has no
    # accounting ContextVar, so every request would be silently unbilled.
    parent_context = contextvars.copy_context()

    def take() -> Any | None:
        with take_lock:
            if cursor[0] >= len(jobs):
                return None
            job = jobs[cursor[0]]
            cursor[0] += 1
            return job

    def run() -> None:
        while True:
            if time.monotonic() > deadline:
                return
            job = take()
            if job is None:
                return
            parent_context.copy().run(worker, job)

    with ThreadPoolExecutor(max_workers=min(GREP_PARALLEL, len(jobs))) as pool:
        for future in [pool.submit(run) for _ in range(min(GREP_PARALLEL, len(jobs)))]:
            future.result()
    return cursor[0] < len(jobs)


# ── judgglob ────────────────────────────────────────────────────────────────


def _path_score(rel: str, keywords: Sequence[str]) -> float:
    lower = rel.lower()
    return sum(3.0 * lower.count(keyword) for keyword in keywords)


def _glob_request(query: str, entries: Sequence[FileEntry]) -> JudgmentRequest:
    state: dict[str, Any] = {
        "task": TASK,
        "search": query,
        "files": {
            f"e{index:03d}": {"path": entry.rel, "bytes": entry.size}
            for index, entry in enumerate(entries)
        },
    }
    questions = {
        f"e{index:03d}": NoulQuestion(
            "Should this file be returned for the search in the state? "
            f"File: `{entry.rel}` ({entry.size} bytes).")
        for index, entry in enumerate(entries)
    }
    return JudgmentRequest(state=state, questions=questions)


def judgglob(
    query: str,
    *,
    judge: Judge,
    path: str | None = None,
    limit: int = 32,
    hidden: bool = False,
) -> dict[str, Any]:
    entries = list_files(_scope(path), hidden=hidden)
    keywords = keywords_from_query(query)
    if not keywords:
        keywords = [query.strip().lower()]
    ranked = sorted(entries, key=lambda entry: (-_path_score(entry.rel, keywords), entry.rel))
    candidates = ranked[:GLOB_CANDIDATES]
    stats = _RunStats(files=len(entries), candidates=len(candidates))
    hits: list[dict[str, Any]] = []
    for start in range(0, len(candidates), GLOB_NAME_BATCH):
        batch = candidates[start:start + GLOB_NAME_BATCH]
        result, _api = _ask(judge, _glob_request(query, batch), purpose="judgglob", stats=stats)
        if result is None:
            continue
        for index, entry in enumerate(batch):
            probability = _probability(result, f"e{index:03d}")
            if probability is None:
                continue
            hits.append({"path": entry.rel, "score": probability, "confidence": probability})
    hits.sort(key=lambda hit: (-hit["score"], hit["path"]))
    payload: dict[str, Any] = {
        "query": query,
        "hits": hits[:max(1, limit)],
        "coverage": _coverage(stats),
        "stats": stats.snapshot(),
    }
    if stats.uncalibrated:
        payload["uncalibrated"] = True
    return payload


# ── judggrep: windows, sketches, verification ───────────────────────────────


@dataclass(frozen=True)
class Window:
    start: int
    end: int
    text: str
    score: float


def _read_text(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(GREP_READ_LIMIT)
    except OSError:
        return None
    if b"\0" in raw[:4096]:
        return None
    return raw.decode("utf-8", errors="replace")


def _windows(text: str, keywords: Sequence[str]) -> list[Window]:
    lines = text.splitlines()
    windows: list[Window] = []
    index = 0
    while index < len(lines):
        start = index
        parts: list[str] = []
        used = 0
        while index < len(lines):
            line = lines[index]
            encoded = len(line.encode("utf-8")) + 1
            if index > start and used + encoded > GREP_WINDOW_BYTES:
                break
            if index == start and encoded > GREP_WINDOW_BYTES:
                line = line.encode("utf-8")[:GREP_WINDOW_BYTES].decode("utf-8", "ignore")
                encoded = GREP_WINDOW_BYTES
            parts.append(line)
            used += encoded
            index += 1
            if used >= GREP_WINDOW_BYTES:
                break
        body = "\n".join(parts)
        lower = body.lower()
        score = float(sum(lower.count(keyword) for keyword in keywords))
        windows.append(Window(start + 1, index, body, score))
    return windows


def _select_windows(windows: Sequence[Window], limit: int | None = None) -> list[Window]:
    limit = GREP_WINDOWS if limit is None else limit
    if len(windows) <= limit:
        return list(windows)
    if all(window.score == 0 for window in windows):
        step = max(limit - 1, 1)
        keep = {int(index * (len(windows) - 1) / step) for index in range(limit)}
        return [window for index, window in enumerate(windows) if index in keep]
    best = sorted(windows, key=lambda window: (-window.score, window.start))[:limit]
    return sorted(best, key=lambda window: window.start)


def _sketch(window: Window, keywords: Sequence[str]) -> str:
    lines = window.text.splitlines()
    ranked: list[tuple[float, int]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        lower = line.lower()
        score = 0.1 if "(" in line else 0.0
        score += sum(1.0 for keyword in keywords if keyword in lower)
        ranked.append((score, index))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected: list[tuple[int, str]] = []
    used = 0
    for _score, index in ranked:
        text = f"{window.start + index}: {lines[index].strip()[:180]}"
        cost = len(text.encode("utf-8")) + 1
        if used + cost > GREP_SKETCH_BYTES:
            break
        used += cost
        selected.append((index, text))
    selected.sort(key=lambda item: item[0])
    return "\n".join(text for _index, text in selected)


def _rank_content(entries: Sequence[FileEntry], keywords: Sequence[str],
                  deadline: float) -> list[FileEntry]:
    scored: list[tuple[float, FileEntry]] = []
    for entry in entries:
        if time.monotonic() > deadline:
            break
        text = _read_text(entry.path)
        if text is None:
            continue
        lower = text.lower()
        count = sum(lower.count(keyword) for keyword in keywords)
        path_bonus = sum(2.0 for keyword in keywords if keyword in entry.rel.lower())
        scored.append((count + path_bonus, entry))
    scored.sort(key=lambda item: (-item[0], item[1].rel))
    return [entry for _score, entry in scored[:GREP_FILES]]


def _sketch_requests(query: str, cards: Sequence[tuple[FileEntry, Window, str]]
                     ) -> Iterator[tuple[JudgmentRequest, list[tuple[FileEntry, Window]]]]:
    for start in range(0, len(cards), GREP_SKETCH_CARDS):
        batch = cards[start:start + GREP_SKETCH_CARDS]
        files: dict[str, str] = {}
        passages: dict[str, str] = {}
        selected: list[tuple[FileEntry, Window]] = []
        for index, (entry, window, sketch) in enumerate(batch):
            key = f"p{index:02d}"
            files[f"f{index:02d}"] = entry.rel
            passages[key] = sketch
            selected.append((entry, window))
        state_bytes = sum(
            len(key.encode("utf-8")) + len(value.encode("utf-8"))
            for key, value in files.items())
        state_bytes += sum(
            len(key.encode("utf-8")) + len(value.encode("utf-8"))
            for key, value in passages.items())
        if state_bytes > SKETCH_STATE_BYTES:
            half = max(1, len(batch) // 2)
            yield from _sketch_requests(query, batch[:half])
            yield from _sketch_requests(query, batch[half:])
            continue
        state: dict[str, Any] = {"task": TASK, "search": query, "files": files, "passages": passages}
        questions = {
            f"p{index:02d}": NoulQuestion(
                "Does this excerpt contain a substantive implementation, definition, or "
                "explanation matching the search in the state?")
            for index in range(len(batch))
        }
        yield JudgmentRequest(state=state, questions=questions), selected


def _verify_requests(query: str, entries: Sequence[FileEntry],
                     windows: Sequence[Window]) -> Iterator[
                         tuple[JudgmentRequest, list[tuple[FileEntry, Window]]]]:
    batches: list[list[tuple[FileEntry, Window]]] = []
    current: list[tuple[FileEntry, Window]] = []
    used = 0
    for entry, window in zip(entries, windows):
        size = len(window.text.encode("utf-8"))
        if current and used + size > VERIFY_STATE_BYTES:
            batches.append(current)
            current, used = [], 0
        current.append((entry, window))
        used += size
    if current:
        batches.append(current)
    for batch in batches:
        passages = {f"p{index:02d}": window.text for index, (_entry, window) in enumerate(batch)}
        rels = {entry.rel for entry, _window in batch}
        file_label = rels.pop() if len(rels) == 1 else ", ".join(sorted(rels))
        state: dict[str, Any] = {
            "task": TASK, "search": query, "file": file_label, "passages": passages}
        questions = {
            f"p{index:02d}": NoulQuestion(
                "Does this passage contain an implementation, definition, or substantive "
                "explanation matching the search in the state?")
            for index in range(len(batch))
        }
        yield JudgmentRequest(state=state, questions=questions), batch


def _merge_ranges(ranges: Sequence[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    kept = sorted(
        (dict(item) for item in ranges if item["p"] >= threshold and item["p"] > 0),
        key=lambda item: (item["start"], item["end"]))
    merged: list[dict[str, Any]] = []
    for item in kept:
        last = merged[-1] if merged else None
        if last is not None and item["start"] <= last["end"] + 1:
            last["end"] = max(last["end"], item["end"])
            last["p"] = max(last["p"], item["p"])
            continue
        merged.append(item)
    merged.sort(key=lambda item: (-item["p"], item["start"]))
    return merged


def _snippet(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return redact_sensitive_text(stripped[:SNIPPET_CHARS], file_read=True)
    return ""


def judggrep(
    query: str,
    *,
    judge: Judge,
    path: str | None = None,
    glob_pattern: str | None = None,
    keywords: Sequence[str] = (),
    limit: int = 20,
    context_lines: int = 2,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + GREP_TIMEOUT_S
    validate_scope(path, glob_pattern)
    entries = list_files(_scope(path), deadline=deadline)
    if glob_pattern:
        entries = [entry for entry in entries if _match_glob(entry.rel, glob_pattern)]
    terms = keywords_from_query(query, keywords) or [query.strip().lower()]
    ranked = _rank_content(entries, terms, deadline)

    cards: list[tuple[FileEntry, Window, str]] = []
    line_counts: dict[str, int] = {}
    for entry in ranked:
        text = _read_text(entry.path)
        if text is None:
            continue
        line_counts[entry.rel] = len(text.splitlines())
        for window in _select_windows(_windows(text, terms)):
            cards.append((entry, window, _sketch(window, terms)))
    stats = _RunStats(files=len(ranked), windows=len(cards))

    scored: list[tuple[FileEntry, Window, float]] = []
    score_lock = threading.Lock()

    def run_sketch(job: tuple[JudgmentRequest, list[tuple[FileEntry, Window]]]) -> None:
        request, selected = job
        result, _api = _ask(judge, request, purpose="judggrep", stats=stats)
        if result is None:
            return
        found: list[tuple[FileEntry, Window, float]] = []
        for index, (entry, window) in enumerate(selected):
            probability = _probability(result, f"p{index:02d}")
            if probability is not None and probability >= GREP_CUTOFF:
                found.append((entry, window, probability))
        if found:
            with score_lock:
                scored.extend(found)

    stats.timed_out = _dispatch(list(_sketch_requests(query, cards)), run_sketch, deadline=deadline)
    scored.sort(key=lambda item: (-item[2], item[0].rel, item[1].start))
    to_verify = scored[:GREP_FULL_LIMIT]

    verified: list[tuple[FileEntry, Window, float]] = []
    verify_lock = threading.Lock()

    def run_verify(job: tuple[JudgmentRequest, list[tuple[FileEntry, Window]]]) -> None:
        request, batch = job
        result, _api = _ask(judge, request, purpose="judggrep", stats=stats)
        if result is None:
            return
        found: list[tuple[FileEntry, Window, float]] = []
        for index, (entry, window) in enumerate(batch):
            probability = _probability(result, f"p{index:02d}")
            if probability is not None:
                found.append((entry, window, probability))
        if found:
            with verify_lock:
                verified.extend(found)

    if not stats.timed_out:
        verify_jobs = list(_verify_requests(
            query, [item[0] for item in to_verify], [item[1] for item in to_verify]))
        stats.timed_out = _dispatch(verify_jobs, run_verify, deadline=deadline)

    by_file: dict[str, list[dict[str, Any]]] = {}
    for entry, window, probability in verified:
        by_file.setdefault(entry.rel, []).append({
            "start": max(1, window.start - max(0, context_lines)),
            "end": min(line_counts.get(entry.rel, window.end) or window.end,
                       window.end + max(0, context_lines)),
            "p": probability,
            "snippet": _snippet(window.text),
        })
    hits: list[dict[str, Any]] = []
    for rel, ranges in by_file.items():
        merged = _merge_ranges(ranges, GREP_THRESHOLD)
        if not merged:
            continue
        hits.append({
            "path": rel,
            "score": max(item["p"] for item in merged),
            "ranges": [dict(item) for item in merged],
        })
    hits.sort(key=lambda hit: (-hit["score"], hit["path"]))
    payload: dict[str, Any] = {
        "query": query,
        "hits": hits[:max(1, limit)],
        "coverage": _coverage(stats),
        "stats": stats.snapshot(),
    }
    if stats.uncalibrated:
        payload["uncalibrated"] = True
    return payload


# ── tool gating ─────────────────────────────────────────────────────────────


def judgment_tools_available() -> bool:
    """``check_fn``: reachability only — reads config/credentials, never a network call."""
    try:
        settings = load_judgment_settings()
    except Exception:  # health: allow BLE001 -- a broken config hides the tool, never crashes a session
        return False
    if settings.tools_enabled == "off":
        return False
    if settings.tools_enabled == "on":
        return True
    try:
        return has_native_judge()
    except Exception:  # health: allow BLE001 -- credential probes are optional; unreachable hides the tool
        return False


def resolve_find_judge(purpose: str) -> Judge:
    """The chain for a find tool; refuses to spend a text model under ``auto``."""
    settings = load_judgment_settings()
    if settings.tools_enabled == "off":
        raise JudgmentUnavailable("judgment tools are off (judgment.tools.enabled=off)")
    if settings.tools_enabled == "auto" and not has_native_judge():
        raise JudgmentUnavailable(
            "no native judgment route is configured (judgment.tools.enabled=auto)")
    return resolve_judge(purpose=purpose)
