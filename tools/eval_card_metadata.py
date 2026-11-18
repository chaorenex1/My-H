"""ADR-4 D13: deterministic card titles for tool rows.

The same instant-title method as a session's first message
(:func:`agent.title_generator.derive_title`) names a tool card: first meaningful line,
word-boundary truncation, no model, never raises. ``execute_code`` gets one title per cell
(its ``cells[]`` array), every other tool gets one.

Titles are display-only. They ride the live ``tool.start``/``tool.title`` events and the
durable ``display_metadata`` (``card_title`` / ``cell_titles``) so a resumed transcript
rebuilds the same card; a generated title never enters the model-visible tool result.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from agent.title_generator import derive_title

#: Tool args whose first non-empty string is the best title source, in display order.
_TITLE_ARG_KEYS = (
    "command", "path", "file_path", "target", "query", "pattern", "url", "urls",
    "prompt", "name", "text",
)
#: Bound on the cache; a session's calls are far below this, and stale ids are cheap.
_MAX_CACHE_ENTRIES = 4096

_lock = threading.Lock()
_cache: dict[tuple[str, str, int], Optional[str]] = {}


def _text(value: Any, depth: int = 0) -> Optional[str]:
    """First non-empty string in ``value`` (bounded recursion for list/dict args)."""
    if isinstance(value, str):
        return value if value.strip() else None
    if depth >= 2:
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _text(item, depth + 1)
            if found:
                return found
    elif isinstance(value, dict):
        for item in value.values():
            found = _text(item, depth + 1)
            if found:
                return found
    return None


def _arg_text(args: Any) -> Optional[str]:
    if not isinstance(args, dict):
        return None
    for key in _TITLE_ARG_KEYS:
        found = _text(args.get(key))
        if found:
            return found
    for value in args.values():
        found = _text(value, 1)
        if found:
            return found
    return None


def _derive(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    try:
        return derive_title(text)
    except Exception:  # health: allow BLE001 -- a title must never fail a tool call or event
        return None


def _cached(session_id: str, tool_call_id: str, index: int, text: Optional[str]) -> Optional[str]:
    key = (str(session_id or ""), str(tool_call_id or ""), int(index))
    with _lock:
        if key in _cache:
            return _cache[key]
    title = _derive(text)
    with _lock:
        if len(_cache) >= _MAX_CACHE_ENTRIES:
            _cache.clear()
        _cache[key] = title
    return title


def _eval_cells(args: Any, result: Any) -> list[dict[str, Any]]:
    """``args.cells`` / ``args.code`` first, then the result's own ``cells`` array."""
    if isinstance(args, dict):
        cells = args.get("cells")
        if isinstance(cells, list) and cells:
            return [cell for cell in cells if isinstance(cell, dict)]
        if isinstance(args.get("code"), str):
            return [{"code": args["code"], "title": args.get("title")}]
    if isinstance(result, dict):
        cells = result.get("cells")
        if isinstance(cells, list) and cells:
            return [cell for cell in cells if isinstance(cell, dict)]
    return []


def _explicit(title: Any) -> Optional[str]:
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def card_titles(session_id: str, tool_call_id: str, name: str, args: Any,
                *, result: Any = None) -> dict[str, Any]:
    """Titles for one tool call: ``{card_title, cell_titles?}`` (both display-only).

    ``execute_code`` answers one entry per cell (explicit ``title`` wins, otherwise the cell
    code's first line); other tools answer a single ``card_title``. Missing/odd input yields
    ``{}`` so callers never have to special-case a title failure.
    """
    if name == "execute_code":
        cells = _eval_cells(args, result)
        if not cells:
            return {}
        cell_titles: list[Optional[str]] = []
        for index, cell in enumerate(cells):
            explicit = _explicit(cell.get("title"))
            cell_titles.append(
                explicit if explicit is not None
                else _cached(session_id, tool_call_id, index, _text(cell.get("code")))
            )
        top = _explicit(args.get("title")) if isinstance(args, dict) else None
        card_title = top or next((title for title in cell_titles if title), None)
        return {"card_title": card_title, "cell_titles": cell_titles}
    title = _cached(session_id, tool_call_id, -1, _arg_text(args))
    return {"card_title": title} if title else {}
