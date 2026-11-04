"""LSP diagnostics for a write, delivered after the write's tool result instead of inside it.

``write_file`` / ``patch`` return as soon as the bytes are on disk and verified; the language server
check runs on the LSP loop (``agent.lsp.edit_check``).  Its verdict is queued here under the session
that made the edit (bound by ``tools.file_tools`` around the write) and handed to that session's agent
at its next tool result, or its next user turn when no tool runs again
(``agent.plugin_live_notes``: a new row not yet flushed, so no cached byte moves).

A later edit of the same file takes over an undelivered verdict: the newer check diffs against the
older baseline, so an error the first edit introduced is still reported unless the second fixed it.

A verdict must not be lost when the edit was the turn's last tool call (a one-shot ``-q`` run has no
next turn): checks still running are tracked per session (:func:`track_pending`), and before the turn's
final answer is accepted :func:`settle_for_agent` waits for them (bounded) and returns every verdict
still undelivered, which ``agent.turn_stop_gates`` hands to the model as a new message.
"""
from __future__ import annotations

import contextlib
import os
import threading
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Optional

_OWNER: ContextVar[Optional[str]] = ContextVar("hermes_lsp_deferred_owner", default=None)
_MAX_OWNERS = 64  # sessions that never drain (a one-shot caller) must not grow this forever
_MAX_PER_OWNER = 32
_LOCK = threading.Lock()
_SETTLED = threading.Condition(_LOCK)
# A turn-end wait longer than this hands the verdict to the next user turn instead.
TURN_END_WAIT = 5.0


@dataclass(frozen=True)
class Note:
    """One queued late verdict for ``(owner, path)``.

    ``text``/``files`` are the LSP slot: the model block the legacy delivery paths append
    (byte-identical to before) and the structured ``details.files`` entries the custom row renders
    (ADR-9 D2). ``semgrep_text``/``semgrep_files`` are the same fields for the Semgrep source
    (ADR-9 D8): both sources merge per path inside one row, but keep separate slots so a newer
    semantic verdict cannot resurrect stale Semgrep findings (and vice versa).
    """

    text: str
    baseline_text: Optional[str]  # pre-edit content the verdict was diffed against
    files: tuple[dict[str, Any], ...] = ()
    semgrep_text: str = ""
    semgrep_files: tuple[dict[str, Any], ...] = ()
    path: str = ""  # the path the caller wrote, for delivery keys (the queue key is canonical)

    @property
    def legacy_text(self) -> str:
        """The text a non-custom session's tool result / turn-end note carries, both sources."""
        return "\n\n".join(part for part in (self.text, self.semgrep_text) if part)


# owner -> {canonical path (_path_key) -> note}, oldest owner first
_QUEUED: OrderedDict[str, OrderedDict[str, Note]] = OrderedDict()
# owner -> tokens of its checks still running (their verdict lands in _QUEUED or nowhere)
_PENDING: dict[str, set[object]] = {}
# (owner, canonical path) -> token of the newest Semgrep scan: an older scan's verdict is stale bytes.
_SEMGREP_TOKENS: OrderedDict[tuple[str, str], object] = OrderedDict()
_MAX_SEMGREP_TOKENS = 128


def _path_key(path: str) -> str:
    """Identity of one file in the queue: absolute, symlinks resolved, case-folded on Windows.

    A write can name the same file as ``C:\\Repo\\A.py`` and ``c:\\repo\\a.py`` (or through a
    symlink); keying by the literal string would queue two notes for one file and paint two cards.
    The note keeps the caller's path for delivery/display (ADR-9 P3), so the card still matches the
    tool args that produced it.
    """
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except (OSError, ValueError):  # an unrepresentable path: never drop the verdict over identity
        return path


@contextlib.contextmanager
def bind_owner(owner: Optional[str]) -> Iterator[None]:
    """Attribute writes in this block to ``owner`` (the session id, else the task id)."""
    token = _OWNER.set(owner or None)
    try:
        yield
    finally:
        _OWNER.reset(token)


def current_owner() -> Optional[str]:
    return _OWNER.get()


def post(owner: str, path: str, text: str, baseline_text: Optional[str],
         files: Optional[list[dict[str, Any]]] = None) -> None:
    """Queue the LSP verdict for ``owner``.  A note still queued for ``path`` (an older check finished
    while a newer one was starting) is kept in front, with its baseline; its Semgrep slot survives
    untouched.  An empty ``text`` hands the older LSP text back (``restore``)."""
    entries = tuple(files or ())
    key = _path_key(path)
    with _LOCK:
        notes = _QUEUED.pop(owner, None) or OrderedDict()
        older = notes.pop(key, None)
        semgrep_text, semgrep_files = "", ()
        if older is not None:
            semgrep_text, semgrep_files = older.semgrep_text, older.semgrep_files
            if text:
                if older.text and older.text != text:
                    text, baseline_text = f"{older.text}\n\n{text}", older.baseline_text
            else:
                text, baseline_text = older.text, older.baseline_text
            if not entries:  # an empty new set never drops the older structure
                entries = older.files
        if text or entries or semgrep_text or semgrep_files:
            notes[key] = Note(text, baseline_text, entries, semgrep_text, semgrep_files, path)
        _store(owner, notes)


def begin_semgrep(owner: Optional[str], path: str) -> object:
    """Start a Semgrep scan of a freshly written file: invalidate the previous scan's token and drop
    the stale Semgrep slot (its findings describe bytes this write replaced; ADR-9 D8).  The LSP
    slot is left in place — the newer LSP check takes it over through :func:`take`."""
    token = object()
    if not owner:
        return token
    key = _path_key(path)
    with _LOCK:
        _SEMGREP_TOKENS.pop((owner, key), None)
        _SEMGREP_TOKENS[(owner, key)] = token
        while len(_SEMGREP_TOKENS) > _MAX_SEMGREP_TOKENS:
            _SEMGREP_TOKENS.popitem(last=False)
        notes = _QUEUED.get(owner)
        older = notes.get(key) if notes else None
        if older is not None and (older.semgrep_text or older.semgrep_files):
            if older.text or older.files:
                notes[key] = Note(older.text, older.baseline_text, older.files,
                                  path=older.path or path)
            else:
                notes.pop(key)
                if not notes:
                    _QUEUED.pop(owner, None)
    return token


def finish_semgrep(owner: Optional[str], path: str, token: object) -> None:
    """Retire ``token`` without queueing a verdict (the findings went inline, or there were none)."""
    if not owner:
        return
    key = _path_key(path)
    with _LOCK:
        if _SEMGREP_TOKENS.get((owner, key)) is token:
            _SEMGREP_TOKENS.pop((owner, key), None)


def post_semgrep(owner: Optional[str], path: str, token: object, text: str,
                 files: Optional[list[dict[str, Any]]] = None) -> None:
    """Queue the Semgrep verdict for ``owner`` unless a newer write of ``path`` superseded this scan.
    The LSP slot of the same note is preserved, so both sources ride one row."""
    entries = tuple(files or ())
    if not owner:
        return
    key = _path_key(path)
    with _LOCK:
        if _SEMGREP_TOKENS.get((owner, key)) is not token:
            return
        _SEMGREP_TOKENS.pop((owner, key), None)
        notes = _QUEUED.pop(owner, None) or OrderedDict()
        older = notes.pop(key, None)
        lsp_text = older.text if older is not None else ""
        baseline_text = older.baseline_text if older is not None else None
        lsp_files = older.files if older is not None else ()
        if lsp_text or lsp_files or text or entries:
            notes[key] = Note(lsp_text, baseline_text, lsp_files, text or "", entries, path)
        _store(owner, notes)


def _store(owner: str, notes: OrderedDict[str, Note]) -> None:
    if not notes:
        return
    while len(notes) > _MAX_PER_OWNER:
        notes.popitem(last=False)
    _QUEUED[owner] = notes
    while len(_QUEUED) > _MAX_OWNERS:
        _QUEUED.popitem(last=False)


def take(owner: Optional[str], path: str) -> Optional[Note]:
    """Withdraw ``owner``'s undelivered note for ``path``: a newer edit of the file supersedes it and
    diffs against its ``baseline_text``.  The newer check carries it and hands it back to
    :func:`restore` only if it ends with no verdict, so the older errors are not lost."""
    if not owner:
        return None
    key = _path_key(path)
    with _LOCK:
        notes = _QUEUED.get(owner)
        return notes.pop(key, None) if notes else None


def restore(owner: Optional[str], path: str, notes) -> None:
    """Queue ``notes`` (oldest first) withdrawn by :func:`take` again.  Only the LSP slot is restored:
    a Semgrep verdict describes bytes the newer write already replaced (``begin_semgrep`` cleared
    it), so bringing it back would attribute stale findings to the current file."""
    if owner:
        for note in notes:
            post(owner, note.path or path, note.text, note.baseline_text, list(note.files))


def pop_notes(*owners: Optional[str]) -> list[tuple[str, Note]]:
    """Every queued ``(path, note)`` for ``owners``, oldest first, and clear them."""
    out: list[tuple[str, Note]] = []
    with _LOCK:
        for owner in dict.fromkeys(o for o in owners if o):
            notes = _QUEUED.pop(owner, None)
            if notes:
                out.extend((note.path or key, note) for key, note in notes.items())
    return out


def pop_for_agent(agent: Any) -> list[tuple[str, Note]]:
    """Every queued note owned by ``agent`` (session id first, then task id)."""
    return pop_notes(*_owners(agent))


def track_pending(owner: str) -> object:
    """Record a check of ``owner``'s still running; pass the token to :func:`settled` once its verdict
    has been posted (or dropped)."""
    token = object()
    with _LOCK:
        _PENDING.setdefault(owner, set()).add(token)
    return token


def settled(owner: str, token: object) -> None:
    with _SETTLED:
        tokens = _PENDING.get(owner)
        if tokens is not None:
            tokens.discard(token)
            if not tokens:
                del _PENDING[owner]
        _SETTLED.notify_all()


def _owners(agent: Any) -> tuple[Optional[str], ...]:
    owners = (getattr(agent, "session_id", None), getattr(agent, "_current_task_id", None))
    return tuple(o if isinstance(o, str) else None for o in owners)


def settle_for_agent(agent: Any, timeout: float = TURN_END_WAIT) -> list[tuple[str, Note]]:
    """Turn end: wait up to ``timeout`` for ``agent``'s running checks, then pop every undelivered
    ``(path, note)`` verdict.  Returns at once when nothing is running."""
    owners = [o for o in dict.fromkeys(_owners(agent)) if o]
    if not owners:
        return []
    with _SETTLED:
        _SETTLED.wait_for(lambda: not any(_PENDING.get(o) for o in owners), timeout=timeout)
    return pop_notes(*owners)


def forward_to_agent(agent: Any) -> None:
    """Move ``agent``'s finished verdicts onto its live-notes queue, which rides the next tool result
    (or the next user turn).  Called right before that tool result is built."""
    notes = pop_for_agent(agent)
    if not notes:
        return
    from agent.plugin_live_notes import queue_plugin_notice
    for path, note in notes:
        queue_plugin_notice(agent, f"lsp:{path}", note.legacy_text)


def custom_rows_enabled() -> bool:
    """True when this session asked for ADR-10 custom rows (omp-ui)."""
    from agent.lsp.delivery import custom_messages_enabled

    return custom_messages_enabled()


def deliver_notes(agent: Any, messages: list, notes: list[tuple[str, Note]],
                  conversation_history: Any = None) -> Any:
    """Append, flush and announce one late-diagnostics row (see ``agent.lsp.delivery.deliver``)."""
    from agent.lsp.delivery import deliver

    return deliver(agent, messages, notes, conversation_history)


__all__ = [
    "TURN_END_WAIT",
    "Note",
    "begin_semgrep",
    "bind_owner",
    "current_owner",
    "custom_rows_enabled",
    "deliver_notes",
    "finish_semgrep",
    "forward_to_agent",
    "pop_for_agent",
    "pop_notes",
    "post",
    "post_semgrep",
    "restore",
    "settle_for_agent",
    "settled",
    "take",
    "track_pending",
]
