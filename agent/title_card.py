"""Session-title cards as a string sub-form of ``sessions.title`` (ADR-21 D11).

A card is an optional icon plus a short code that head the stored title:
``🅿️ PARK: Fix flaky park tests``. It lives inside the ``sessions.title`` string itself — no
column, no payload field, no render field — and every consumer that compares, ranks or searches
titles strips it first (``split_card_title``), so the card is decoration and never identity.

Three input forms are accepted, all emitted by the naming prompts:
``<title emoji="🅿️" code="PARK">Fix flaky park tests</title>`` (the fork side turn),
``🅿️ PARK: Fix flaky park tests`` and the icon-less ``PARK: Fix flaky park tests``. A malformed
or missing card is not an error: the text is returned unchanged as the plain title, so a model's
card mistake never costs the session its name. ``nf`` is a retired Nerd Font attribute — read and
discarded here, never returned.
"""

from __future__ import annotations

import re
from typing import Optional

MAX_CARD_CODE_CHARS = 6
MAX_CARD_ICON_CHARS = 8

_CARD_CODE_RE = re.compile(r"[A-Z0-9]{1,6}")
# The whole-line tag the fork prompt prescribes. Only the attribute form is a card here; an
# attribute-less ``<title>…</title>`` is a plain title.
_TITLE_TAG_RE = re.compile(
    r"^<title(?P<attrs>[^>]*)>(?P<body>.*)</title>$", re.IGNORECASE | re.DOTALL
)
_ATTRIBUTE_RE = re.compile(r"""([A-Za-z_][\w-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
# ``<icon> <CODE>: title`` / ``CODE: title``: the stored string form.
_CARD_LINE_RE = re.compile(r"^(?:(?P<icon>\S+)\s+)?(?P<code>[A-Z0-9]{1,6}):\s*(?P<title>.+)$")
# A card request's reply: the card alone, optionally followed by words.
_CARD_REPLY_RE = re.compile(r"^(?:(?P<icon>\S+)\s+)?(?P<code>[A-Z0-9]{1,6})(?::.*)?$")


def _first_line(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


def _attributes(raw: str) -> dict[str, str]:
    return {
        match.group(1).lower(): match.group(2) if match.group(2) is not None else match.group(3)
        for match in _ATTRIBUTE_RE.finditer(raw or "")
    }


def _valid_code(code: Optional[str]) -> Optional[str]:
    return code if code and _CARD_CODE_RE.fullmatch(code) else None


def _valid_icon(icon: Optional[str]) -> Optional[str]:
    """An icon is 1–8 non-ASCII code points.

    ``len()`` counts code points, so a variation selector counts: ``🅿️`` is U+1F17F + U+FE0F,
    two code points. A letter or digit is never an icon, which is what keeps a title like
    ``A PARK: Fix …`` from turning prose into a card.
    """
    if not icon or len(icon) > MAX_CARD_ICON_CHARS:
        return None
    return icon if all(ord(char) > 127 for char in icon) else None


def _tag_card(attributes: dict[str, str]) -> Optional[str]:
    """The card of a ``<title emoji=… code=…>`` tag: both attributes must be valid."""
    icon = _valid_icon(attributes.get("emoji"))
    code = _valid_code(attributes.get("code"))
    return f"{icon} {code}" if icon and code else None


def _prefixed_card(line: str) -> tuple[Optional[str], str]:
    """``(card, plain)`` for the string forms; ``(None, line)`` when there is no valid card."""
    match = _CARD_LINE_RE.fullmatch(line)
    if not match:
        return None, line
    code = _valid_code(match.group("code"))
    icon = match.group("icon")
    if icon is not None:
        icon = _valid_icon(icon)
        if not icon or not code:
            return None, line
        return f"{icon} {code}", match.group("title").strip()
    return (code, match.group("title").strip()) if code else (None, line)


def split_card_title(text: str) -> tuple[Optional[str], str]:
    """Split one stored title or model reply into ``(card, plain_title)``.

    The first non-empty line decides, mirroring the ``<title>`` marker parser. A card is only
    returned when icon and code are both valid; otherwise the text comes back verbatim as the
    plain title (no exception, no swallowed words), which is what lets comparison, drift and
    search run on ``plain_title`` without special-casing broken cards.
    """
    line = _first_line(text)
    if not line:
        return None, ""
    tag = _TITLE_TAG_RE.fullmatch(line)
    if tag:
        return _tag_card(_attributes(tag.group("attrs"))), tag.group("body").strip()
    return _prefixed_card(line)


def keep_title_card(new_title: str, current_title: str) -> str:
    """Carry *current_title*'s card onto *new_title* when the new title has none.

    A regeneration changes the words; the card is the stable half of the address, so a plain new
    title keeps the old card (``Fix flaky auth tests`` → ``🅿️ PARK: Fix flaky auth tests``). A new
    title that already carries its own card wins as-is, and a plain current title leaves the new
    title untouched.
    """
    new_card, new_plain = split_card_title(new_title)
    if new_card is not None:
        return f"{new_card}: {new_plain}"
    current_card, _ = split_card_title(current_title)
    return f"{current_card}: {new_plain}" if current_card else new_plain


def parse_card_reply(text: str) -> Optional[str]:
    """The card from a ``title-card`` reply, or ``None`` when it carries no usable card.

    The request asks for the card alone — ``<title>🅿️ PARK</title>`` or ``🅿️ PARK`` — so only the
    icon and code are kept and everything after the code is ignored. An invalid icon or code
    yields ``None``: the caller keeps the plain title and never retries the request.
    """
    line = _first_line(text)
    tag = _TITLE_TAG_RE.fullmatch(line)
    if tag:
        card = _tag_card(_attributes(tag.group("attrs")))
        if card is not None:
            return card
        line = tag.group("body").strip()
    match = _CARD_REPLY_RE.fullmatch(line)
    if not match:
        return None
    code = _valid_code(match.group("code"))
    if code is None:
        return None
    icon = match.group("icon")
    if icon is None:
        return code
    icon = _valid_icon(icon)
    return f"{icon} {code}" if icon else None
