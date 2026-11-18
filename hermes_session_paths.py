"""Session-bundle path derivation (ADR-16 P0).

Pure and stdlib-only: nothing here opens ``state.db``, creates directories, or depends on the
agent runtime. The layout it names (ADR-16 D2)::

    <HERMES_HOME>/sessions/<project-dir>/<timestamp>_<session-id>.jsonl
    <HERMES_HOME>/sessions/<project-dir>/<timestamp>_<session-id>/
        <step>.<tool>.log
        <step>.<tool>-original.log
        <topic>.jsonl
        <topic>.md
        <topic>_ast.json
        _advisor.jsonl

``project-dir`` follows omp's ``session-paths.ts`` encoding: the temp root wins over home (on
Windows the temp root nests inside home), then a home-relative name, then a double-dash absolute
name. The temp root is the more specific root, so checking it first is what keeps
``%USERPROFILE%\\AppData\\Local\\Temp\\x`` from being encoded as a home path.

This is a *derivation* helper. The authoritative path for an existing bundle is the one recorded
in session metadata (``mirror_path``); readers must not re-derive it. Derivation is for new
bundles and live artifacts only.

Component names are a cross-platform contract:

- only ``[A-Za-z0-9_-]`` survive sanitisation, capped at 64 chars for tool/topic names;
- bundle/project components are capped at 120 chars; an over-long name keeps its prefix and
  appends 8 hex chars of the full name, so the result is deterministic rather than
  filesystem-dependent;
- collision checks compare case-insensitively (Windows can't hold two names that differ only
  by case), and a real collision appends 8 hex chars of the session id.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Optional

#: ``sessions/`` under the active Hermes home (profiles get their own root).
SESSIONS_DIRNAME = "sessions"
#: Windows path-component ceiling (``MAX_PATH`` headroom, omp uses the same bound).
MAX_COMPONENT_CHARS = 120
#: Tool names and agent subjects/topics share one sanitised width.
NAME_COMPONENT_CHARS = 64
#: Tool archive suffix for the text the model actually saw, and for the pre-truncation text.
TOOL_ARTIFACT_SUFFIX = ".log"
TOOL_ORIGINAL_SUFFIX = "-original.log"
#: Reserved advisor stem inside a bundle (``_advisor.jsonl`` / ``_advisor.<slug>.jsonl``).
ADVISOR_STEM = "_advisor"
RESERVED_SESSIONS_ENTRIES = frozenset({"sessions.json", ".mirror"})

_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_-]")
_ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_TOPIC_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
_TOPIC_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_ENCODED_SEPARATORS_RE = re.compile(r"[/\\:]")
_TIMESTAMP_SEPARATOR_RE = re.compile(r"[/\\:.]")


def _digest8(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "surrogatepass")).hexdigest()[:8]


def _resolved(path: str | os.PathLike[str]) -> str:
    """Absolute path, realpath'd only when it exists (symlink/alias equivalence).

    Not-existing paths are common (a project dir whose session starts before the dir is
    created, or a path test fixture), and Windows ``realpath`` mis-parses a colon in a
    non-drive position; ``abspath`` keeps the caller's spelling.
    """
    absolute = os.path.abspath(os.path.expanduser(os.fspath(path)))
    if not os.path.exists(absolute):
        return absolute
    try:
        return os.path.realpath(absolute)
    except OSError:
        return absolute


def _relative_within(target: str, root: str) -> Optional[str]:
    """``target`` relative to ``root`` when it stays inside, else ``None``.

    Comparison is case-insensitive so a differently-cased drive or path still classifies the
    same way on Windows; the returned relative keeps the caller's spelling for the name.
    """
    if os.path.normcase(target) == os.path.normcase(root):
        return ""
    try:
        relative = os.path.relpath(target, root)
    except ValueError:  # different Windows drive
        return None
    if relative in (os.pardir, "") or relative.startswith(os.pardir + os.sep):
        return None
    if os.path.isabs(relative):
        return None
    return relative


def _fit_component(name: str, *, max_chars: int = MAX_COMPONENT_CHARS) -> str:
    """Cap one path component deterministically: prefix + 8 hex of the full name."""
    if len(name) <= max_chars:
        return name
    suffix = f"-{_digest8(name)}"
    return name[: max_chars - len(suffix)] + suffix


def _encode_relative(prefix: str, relative: str) -> str:
    encoded = _ENCODED_SEPARATORS_RE.sub("-", relative)
    if not encoded:
        return prefix
    return prefix + encoded if prefix.endswith("-") else f"{prefix}-{encoded}"


def encode_project_dir(
    cwd: str | os.PathLike[str],
    *,
    os_home: str | os.PathLike[str] | None = None,
    temp_root: str | os.PathLike[str] | None = None,
) -> str:
    """The ``sessions/`` child directory name for ``cwd`` (ADR-16 D2).

    ``os_home`` is the *user's* home (omp's ``os.homedir()``), never ``HERMES_HOME``; the
    sessions root is separate. Both roots are injectable so the encoding is testable without
    touching the real filesystem.
    """
    resolved_cwd = _resolved(cwd)
    home = _resolved(os_home if os_home is not None else Path.home())
    tmp = _resolved(temp_root if temp_root is not None else tempfile.gettempdir())

    temp_relative = _relative_within(resolved_cwd, tmp)
    if temp_relative is not None:
        name = _encode_relative("-tmp", temp_relative)
    else:
        home_relative = _relative_within(resolved_cwd, home)
        if home_relative is not None:
            name = _encode_relative("-", home_relative)
        else:
            absolute = resolved_cwd.lstrip("/\\")
            name = f"--{_ENCODED_SEPARATORS_RE.sub('-', absolute)}--"
    return _fit_component(name)


def sanitize_component(value: object, fallback: str, *, max_len: int = NAME_COMPONENT_CHARS) -> str:
    """One tool/topic path component: ``[^A-Za-z0-9_-]`` -> ``_``, capped, never empty."""
    clean = _SAFE_COMPONENT_RE.sub("_", str(value or "").strip())[:max_len]
    return clean or fallback


def tool_component(tool_name: object) -> str:
    """Archive stem for a tool name (ADR-16 D3): safe chars, 64 max, ``tool`` when empty."""
    return sanitize_component(tool_name, "tool")


def topic_component(subject: object) -> str:
    """Bundle artifact stem for a stored agent subject (ADR-16 D4)."""
    return sanitize_component(subject, "agent")


def _strip_html_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Drop HTML comments from one line; ``in_comment`` carries a block comment across lines."""
    kept: list[str] = []
    index = 0
    while True:
        if in_comment:
            end = line.find("-->", index)
            if end == -1:
                return "".join(kept), True
            index, in_comment = end + 3, False
            continue
        start = line.find("<!--", index)
        if start == -1:
            kept.append(line[index:])
            return "".join(kept), False
        kept.append(line[index:start])
        index, in_comment = start + 4, True


def _topic_first_line(assignment: object) -> str:
    """First meaningful non-structure line: headings, fenced blocks and HTML comments are skipped.

    A subagent assignment is a rendered task document (``# Target`` / ``# Change`` fences), so the
    first raw line is structure, not the work. Comments are removed even mid-line (a ``<!-- note -->``
    before the prose must not hide it) and a fence carries its whole body away.
    """
    in_fence = in_comment = False
    for raw in str(assignment or "").splitlines():
        line, in_comment = _strip_html_comments(raw, in_comment)
        if in_comment and not line.strip():
            continue
        if _TOPIC_FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        text = " ".join(line.split())
        if not text or _TOPIC_HEADING_RE.match(text):
            continue
        return text
    return ""


def derive_topic(assignment: object, *, fallback: object = "") -> str:
    """Path-safe artifact stem for a run, derived from its assignment (ADR-21 D8).

    Composes the existing naming pipeline instead of re-deriving it: ``derive_title`` truncates at
    its 48-char word boundary, ``sanitize_component`` maps to ``[A-Za-z0-9_-]`` and caps at 64.
    A title with no ASCII alphanumeric left after sanitising (CJK/emoji prose becomes a run of
    ``_``) is not discarded: it becomes ``topic-<8 hex of the title>``, stable for the same input.
    *fallback* (the run's agent name) supplies the stem when the assignment is empty.
    """
    from agent.title_generator import derive_title

    text = _topic_first_line(assignment) or _topic_first_line(fallback)
    title = derive_title(text) if text else None
    stem = sanitize_component(title, "")
    if _ASCII_ALNUM_RE.search(stem):
        return stem
    return f"topic-{_digest8(title or text or 'agent')}"


def file_safe_timestamp(started_at: object) -> str:
    """``started_at`` -> filesystem-safe ISO-8601 UTC (``:`` and ``.`` become ``-``).

    ``sessions.started_at`` is a REAL epoch; strings are accepted for mirror imports and
    ISO-shaped metadata. Unparseable strings fall back to the literal replacement, which is
    never worse than failing a bundle write.
    """
    if isinstance(started_at, datetime):
        return _iso_utc(started_at)
    if isinstance(started_at, (int, float)) and not isinstance(started_at, bool):
        return _iso_utc(datetime.fromtimestamp(float(started_at), UTC))
    text = str(started_at)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Never let a malformed value contribute a path separator: the result is a component,
        # and a '/' or '\' here would make bundle_dir escape the project directory.
        return sanitize_component(_TIMESTAMP_SEPARATOR_RE.sub("-", text), "timestamp")
    return _iso_utc(parsed)


def _iso_utc(moment: datetime) -> str:
    # Naive values are treated as UTC (sessions/started_at are UTC) rather than host-local, so
    # the same row produces the same bundle name on every machine.
    if moment.tzinfo is None:
        moment = datetime.combine(moment.date(), moment.time(), UTC)
    else:
        moment = moment.astimezone(UTC)
    # ADR-16 D2 / omp's fileSafeTimestamp: ':' and '.' become '-' in the file name.
    return f"{moment:%Y-%m-%dT%H-%M-%S}-{moment.microsecond // 1000:03d}Z"


def bundle_name(started_at: object, session_id: object) -> str:
    """``<file-safe started_at>_<sanitised session id>`` (no directory, no extension)."""
    return _fit_component(
        f"{file_safe_timestamp(started_at)}_{sanitize_component(session_id, 'session')}"
    )


def sessions_root(hermes_home: str | os.PathLike[str] | None = None) -> Path:
    """``<HERMES_HOME>/sessions``; imported lazily to keep this module import-light."""
    if hermes_home is None:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / SESSIONS_DIRNAME
    return Path(hermes_home) / SESSIONS_DIRNAME


def project_dir(
    cwd: str | os.PathLike[str],
    *,
    hermes_home: str | os.PathLike[str] | None = None,
    os_home: str | os.PathLike[str] | None = None,
    temp_root: str | os.PathLike[str] | None = None,
) -> Path:
    """``sessions/<encoded cwd>`` (no directory is created)."""
    return sessions_root(hermes_home) / encode_project_dir(
        cwd, os_home=os_home, temp_root=temp_root
    )


def bundle_dir(
    cwd: str | os.PathLike[str],
    started_at: object,
    session_id: object,
    *,
    hermes_home: str | os.PathLike[str] | None = None,
    os_home: str | os.PathLike[str] | None = None,
    temp_root: str | os.PathLike[str] | None = None,
) -> Path:
    """The artifact directory for one session (``<project>/<timestamp>_<id>``), not created."""
    return project_dir(
        cwd, hermes_home=hermes_home, os_home=os_home, temp_root=temp_root
    ) / bundle_name(started_at, session_id)


def _child_names(parent: Path) -> set[str]:
    try:
        return {entry.name for entry in parent.iterdir()}
    except OSError:
        return set()


def _disambiguate(name: str, existing: Iterable[str], discriminator: str) -> str:
    """Case-insensitive collision guard: reuse the exact name, else append 8 hex."""
    names = [str(item) for item in existing]
    folded = {item.casefold() for item in names}
    if name.casefold() not in folded:
        return name
    if name in names:
        return name
    return _fit_component(f"{name}-{_digest8(discriminator)}")


def allocate_bundle_dir(
    cwd: str | os.PathLike[str],
    started_at: object,
    session_id: object,
    *,
    hermes_home: str | os.PathLike[str] | None = None,
    os_home: str | os.PathLike[str] | None = None,
    temp_root: str | os.PathLike[str] | None = None,
) -> Path:
    """``bundle_dir`` with the on-disk case-collision guard applied (no directory is created).

    The same session id spelled identically reuses its bundle (mirror is idempotent); a
    differently-cased spelling gets the session id's 8-hex suffix so Windows can hold both.
    """
    parent = project_dir(
        cwd, hermes_home=hermes_home, os_home=os_home, temp_root=temp_root
    )
    return parent / _disambiguate(
        bundle_name(started_at, session_id), _child_names(parent), str(session_id)
    )


def main_transcript_path(
    cwd: str | os.PathLike[str],
    started_at: object,
    session_id: object,
    *,
    hermes_home: str | os.PathLike[str] | None = None,
    os_home: str | os.PathLike[str] | None = None,
    temp_root: str | os.PathLike[str] | None = None,
) -> Path:
    """``<project>/<timestamp>_<id>.jsonl`` — the main session cold mirror (ADR-16 D2)."""
    return bundle_dir(
        cwd, started_at, session_id,
        hermes_home=hermes_home, os_home=os_home, temp_root=temp_root,
    ).with_suffix(".jsonl")


def mirror_manifest_path(
    profile: object, *, hermes_home: str | os.PathLike[str] | None = None
) -> Path:
    """``sessions/.mirror/<profile>.jsonl`` — one mirror manifest per profile (ADR-16 D2)."""
    name = sanitize_component(profile, "default")
    return sessions_root(hermes_home) / ".mirror" / f"{name}.jsonl"


def subagent_dir(bundle: str | os.PathLike[str], subject: object, *,
                 topic: str = "") -> Path:
    """``<bundle>/<topic>`` — the named subagent's own namespace (not created)."""
    return Path(bundle) / (topic or topic_component(subject))


def tool_artifact_paths(
    directory: str | os.PathLike[str], step: int, tool_name: object
) -> tuple[Path, Path]:
    """``(<step>.<tool>.log, <step>.<tool>-original.log)`` for one tool call.

    ``step`` is the per-session 1-based tool-call sequence (ADR-16 D3); the caller writes the
    second path only when the wrapper actually truncated.
    """
    step_number = int(step)
    if step_number < 1:
        raise ValueError(f"tool step must be >= 1, got {step}")
    stem = f"{step_number}.{tool_component(tool_name)}"
    directory_path = Path(directory)
    return (
        directory_path / f"{stem}{TOOL_ARTIFACT_SUFFIX}",
        directory_path / f"{stem}{TOOL_ORIGINAL_SUFFIX}",
    )


def advisor_path(directory: str | os.PathLike[str], slug: object = "") -> Path:
    """``_advisor.jsonl`` (default) or ``_advisor.<slug>.jsonl`` (multi-reviewer)."""
    directory_path = Path(directory)
    if not slug:
        return directory_path / f"{ADVISOR_STEM}.jsonl"
    return directory_path / f"{ADVISOR_STEM}.{sanitize_component(slug, 'advisor')}.jsonl"


def _topic_taken(directory: Path, stem: str) -> bool:
    """True when any ``<stem>[._-]…`` entry already exists (case-insensitive, ADR-16 D4)."""
    folded = stem.casefold()
    for name in _child_names(directory):
        low = name.casefold()
        if low == folded or low.startswith((folded + ".", folded + "_", folded + "-")):
            return True
    return False


def allocate_topic(directory: str | os.PathLike[str], subject: object, *,
                   discriminator: object = "") -> str:
    """The artifact stem for one run, disambiguated against the bundle's existing entries.

    ``topic_component`` alone can collide: ``a b`` and ``a?b`` both become ``a_b``. The second
    run in the same bundle gets an 8-hex suffix derived from the subject (or *discriminator*),
    compared case-insensitively so Windows cannot overwrite the first run's files.
    """
    stem = topic_component(subject)
    if not _topic_taken(Path(directory), stem):
        return stem
    return _fit_component(f"{stem}-{_digest8(discriminator or subject)}",
                          max_chars=NAME_COMPONENT_CHARS)


def transcript_path(directory: str | os.PathLike[str], subject: object, *,
                    topic: str = "") -> Path:
    """``<topic>.jsonl`` — a named agent session's transcript inside its bundle."""
    return Path(directory) / f"{topic or topic_component(subject)}.jsonl"


def report_path(directory: str | os.PathLike[str], subject: object, *,
                topic: str = "") -> Path:
    """``<topic>.md`` — the subagent's final report, written verbatim at settle."""
    return Path(directory) / f"{topic or topic_component(subject)}.md"


def ast_summary_path(directory: str | os.PathLike[str], subject: object, *,
                     topic: str = "") -> Path:
    """``<topic>_ast.json`` — always written at settle, even with ``sources: []``."""
    return Path(directory) / f"{topic or topic_component(subject)}_ast.json"


def structured_output_path(directory: str | os.PathLike[str], subject: object, *,
                           topic: str = "") -> Path:
    """``<topic>.json`` — optional structured result sidecar."""
    return Path(directory) / f"{topic or topic_component(subject)}.json"
