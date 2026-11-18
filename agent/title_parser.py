"""``<title>`` marker parser for the naming service's stage-1 replies (ADR-21 D4).

Every naming consumer (session titles today; task labels and cards in later sprints) shares one
output protocol: the model answers with a single line — ``<title>…</title>``, ``<title/>``
("nothing to name"), ``<title>none</title>`` (the same decline in tag form), or plain prose when
a model ignored the marker convention. Parsing is line-first: the first non-empty line of the
reply decides, so preface chatter and trailing notes never reach the title. ``think`` blocks are
stripped before parsing, so a reasoning model's thinking can neither leak into nor hide the title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Titling is a 3-7 word task, so output past these bounds is a model that ignored the task and
# answered the user's message instead — the whole reply must not become the title. Rejected
# (None), never truncated: a truncation is still half an assistant blob the caller would retry
# on the next exchange (port of can1357/oh-my-pi#7306). 12 leaves headroom for legitimate wordy
# titles while excluding full-sentence answers.
MAX_TITLE_CHARS = 80
MAX_TITLE_WORDS = 12

# The decline sentinel the prompt prescribes. ``<title>none</title>`` in tag form terminates the
# naming attempt; a bare prose "none" is rejected the same way but is not a declared decline.
_NONE_SENTINEL = "none"

_QUOTE_CHARS = "\"'"
_SELF_CLOSING_TITLE_RE = re.compile(r"<title\s*/>", re.IGNORECASE)
_CLOSED_TITLE_RE = re.compile(r"^<title>(.*)</title>$", re.IGNORECASE)
# Truncated/mis-nested markers degrade to their text: strip a leading open or self-closing tag
# and a trailing close tag (the same anchored edges the closed-tag match uses), so a token cap
# that cut the reply mid-marker never leaks "<title>" into a session title.
_STRAY_OPEN_TAG_RE = re.compile(r"^(?:<title\s*/>|<title>)\s*", re.IGNORECASE)
_STRAY_CLOSE_TAG_RE = re.compile(r"\s*</title>$", re.IGNORECASE)
# Word characters for the word-count guards: Unicode letters and numbers only (a run of
# punctuation or underscores is not a word), mirroring omp's ``/[\p{L}\p{N}]+/u``.
_TITLE_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class TitleParse:
    """One stage-1 reply: ``title`` is the cleaned title or None; ``terminal`` marks the prompt's
    prescribed decline (``<title/>`` / ``<title>none</title>``), which ends the whole naming
    attempt instead of falling through to the next candidate."""

    title: Optional[str]
    terminal: bool = False


def _first_nonempty_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _strip_title_prefix(text: str) -> str:
    return text[6:].strip() if text.lower().startswith("title:") else text


def clean_title(text: str) -> Optional[str]:
    """Shared naming guard (ADR-21 D4): collapse whitespace, then strip quotes, a ``Title:``
    prefix and trailing sentence punctuation; reject — never truncate — an answer-shaped output:
    more than 80 characters, more than 12 words, or zero words (blank or pure symbols)."""
    title = _strip_title_prefix(" ".join((text or "").split()).strip(_QUOTE_CHARS).strip())
    # The prefix strip can expose the inner half of a quoted title ("Title: \"Fix bug\""),
    # so unquote again before the punctuation pass — quotes would otherwise hide the period.
    title = title.strip(_QUOTE_CHARS).strip().rstrip(".!,;:").strip()
    if not title or title.lower() == _NONE_SENTINEL:
        return None
    words = _TITLE_WORD_RE.findall(title)
    if not words or len(title) > MAX_TITLE_CHARS or len(words) > MAX_TITLE_WORDS:
        return None
    return title


def parse_title_output(raw: str) -> TitleParse:
    """Parse one naming reply under the marker contract.

    The first non-empty line decides everything after ``think`` blocks are stripped: a closed tag
    yields its inner text, a self-closing tag or ``none`` inner is the terminal decline, and any
    other line is the unlabeled-prose fallback (a model that ignored the marker convention still
    titles the session with its first line). Guard failures and empty replies return
    ``TitleParse(None)`` — unusable, but not a decline — while the two tag-form declines return
    ``terminal=True``.
    """
    from agent.agent_runtime_helpers import strip_think_blocks

    line = _first_nonempty_line(strip_think_blocks(None, raw or ""))
    if not line:
        return TitleParse(None)
    unquoted = line.strip(_QUOTE_CHARS).strip()
    if _SELF_CLOSING_TITLE_RE.fullmatch(unquoted):
        return TitleParse(None, terminal=True)
    closed = _CLOSED_TITLE_RE.fullmatch(unquoted)
    if closed and closed.group(1).strip().lower() == _NONE_SENTINEL:
        return TitleParse(None, terminal=True)
    candidate = closed.group(1) if closed else _STRAY_CLOSE_TAG_RE.sub(
        "", _STRAY_OPEN_TAG_RE.sub("", unquoted)
    ).strip()
    return TitleParse(clean_title(candidate))
