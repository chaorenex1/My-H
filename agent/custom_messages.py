"""ADR-10 custom transcript rows: wire shape, durable storage envelope and provider conversion.

A custom row is a session-layer message (``role:"custom"``) that carries ``custom_type`` and
display ``details``. It is never a provider role: :func:`project_for_api` maps the message-bearing
types to ``developer``/``user`` and drops display-only markers before a request is built.

Durable rows only have the legacy ``display_kind`` / ``display_metadata`` columns, so the custom
fields ride inside ``display_metadata`` under :data:`CUSTOM_STORAGE_KEY`. Producers write them at
the top level; :func:`storage_metadata` and :func:`lift_stored_fields` are the persistence seam.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

CUSTOM_STORAGE_KEY = "_custom_message"

# ADR-10 P5: internal durable markers -> the custom block the projection emits. Producers write
# ``role:"custom"`` rows; this map serves older rows and the marker rows whose model semantics
# still need a real user row (adviser steer). The marker itself never reaches the wire.
DISPLAY_KIND_TO_CUSTOM_TYPE = {
    "adviser": "advisor",
    "skill_invocation": "skill-prompt",
    "todo_reminder": "todo-reminder",
    "model_switch": "model-switch",
    "personality_switch": "personality-switch",
    "process_complete": "process-complete",
    "async_delegation_complete": "async-delegation-complete",
    "internal_notification": "internal-notification",
}

DISPLAY_ONLY_CUSTOM_TYPES = frozenset({
    "model-switch", "personality-switch", "process-complete",
    "async-delegation-complete", "internal-notification",
})
MESSAGE_CUSTOM_TYPES = frozenset({"advisor", "skill-prompt", "todo-reminder", "collab-prompt"})

# Rows that stay legacy user/assistant turns even in a custom-message session (D9/D11).
LEGACY_ROLE_DISPLAY_KINDS = frozenset({"auto_continue", "failed_turn", "steer", "hidden"})

API_ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})
_CUSTOM_FIELDS = ("custom_type", "display", "display_only", "details", "attribution", "legacy_role")

_ADVISER_SEVERITIES = frozenset({"nit", "concern", "blocker"})


def custom_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """The custom fields of *row*; a persisted row carries them in the storage envelope."""
    fields = {key: row[key] for key in _CUSTOM_FIELDS if row.get(key) is not None}
    if fields:
        return fields
    metadata = row.get("display_metadata")
    if isinstance(metadata, Mapping):
        stored = metadata.get(CUSTOM_STORAGE_KEY)
        if isinstance(stored, Mapping):
            return {key: stored[key] for key in _CUSTOM_FIELDS if stored.get(key) is not None}
    return {}


def storage_metadata(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """``display_metadata`` plus the custom envelope a durable write needs (or ``None``)."""
    metadata = row.get("display_metadata")
    envelope = {key: row[key] for key in _CUSTOM_FIELDS if row.get(key) is not None}
    if not envelope:
        return metadata if isinstance(metadata, dict) else None
    base = dict(metadata) if isinstance(metadata, Mapping) else {}
    base[CUSTOM_STORAGE_KEY] = envelope
    return base


def lift_stored_fields(row: dict[str, Any]) -> None:
    """Promote a restored row's envelope onto the row (display_metadata keeps the durable copy)."""
    metadata = row.get("display_metadata")
    if not isinstance(metadata, Mapping):
        return
    stored = metadata.get(CUSTOM_STORAGE_KEY)
    if not isinstance(stored, Mapping):
        return
    for key in _CUSTOM_FIELDS:
        if stored.get(key) is not None and row.get(key) is None:
            row[key] = stored[key]


def adviser_notes(text: str, *, severity: str | None = None,
                  adviser: str | None = None) -> dict[str, Any]:
    """The ``details.notes`` shape for an adviser card, parsed once on the gateway side.

    The wire format is ``- [severity · adviser] note`` between the frame markers; a row without
    parseable notes keeps its body as one note so the card is never empty.
    """
    notes: list[dict[str, Any]] = []
    body: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        match = re.match(r"^-\s*\[([^\]]+)\]\s*(.*)$", stripped)
        if match:
            parts = [part.strip() for part in match[1].split("·")]
            note_severity = parts[0].lower()
            note = {"note": match[2].strip()}
            if note_severity in _ADVISER_SEVERITIES:
                note["severity"] = note_severity
            if len(parts) > 1 and parts[1]:
                note["advisor"] = parts[1]
            notes.append(note)
            continue
        if stripped and not stripped.startswith("[adviser") and not stripped.startswith("[end adviser"):
            body.append(stripped)
    if not notes and body:
        note = {"note": "\n".join(body)}
        if severity and severity.lower() in _ADVISER_SEVERITIES:
            note["severity"] = severity.lower()
        if adviser:
            note["advisor"] = adviser
        notes.append(note)
    return {"notes": notes}


def skill_details(text: str, *, path: str = "", prompt: str | None = None) -> dict[str, Any]:
    """``details`` for a skill-prompt row from its ``/name [args]`` / ``/skill:name [args]`` invocation."""
    match = re.match(r"^/(?:skill:)?([^\s]+)\s*([\s\S]*)$", str(text or "").strip())
    name = match[1] if match else "skill"
    args = (match[2].strip() if match else "") or None
    rendered = prompt if isinstance(prompt, str) and prompt else str(text or "")
    return {"name": name, "path": path, **({"args": args} if args else {}),
            "prompt": rendered, "lineCount": max(1, rendered.count("\n") + 1)}


def todo_details(metadata: Any, *, attempt: int = 0, max_attempts: int = 0) -> dict[str, Any] | None:
    """``details`` for a todo-reminder row from its legacy ``display_metadata`` envelope."""
    source = metadata.get("todo_reminder") if isinstance(metadata, Mapping) else None
    source = source if isinstance(source, Mapping) else (metadata if isinstance(metadata, Mapping) else {})
    todos = source.get("todos") if isinstance(source, Mapping) else None
    if not isinstance(todos, list):
        return None
    rows = []
    for item in todos:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content") if isinstance(item.get("content"), str) else item.get("text")
        raw_status = str(item.get("status") or "")
        status = {"cancelled": "abandoned", "abandoned": "abandoned", "blocked": "blocked",
                  "pending": "pending", "in_progress": "in_progress",
                  "completed": "completed"}.get(raw_status, "pending")
        if isinstance(content, str) and content:
            rows.append({"content": content, "status": status})
    if not rows:
        return None
    return {"todos": rows,
            "attempt": source.get("attempt", attempt) if isinstance(source, Mapping) else attempt,
            "max_attempts": source.get("max_attempts", max_attempts) if isinstance(source, Mapping) else max_attempts}


def legacy_details(row: Mapping[str, Any], *, display_kind: str) -> dict[str, Any] | None:
    """Build ``details`` for a legacy display row so clients never parse its text (D8.1)."""
    text = row.get("content") if isinstance(row.get("content"), str) else ""
    text = str(text)
    metadata = row.get("display_metadata")
    if display_kind == "adviser":
        severity = metadata.get("severity") if isinstance(metadata, Mapping) else None
        notes = adviser_notes(text, severity=severity if isinstance(severity, str) else None).get("notes")
        details = {"notes": notes or []}
        if isinstance(severity, str) and severity:
            details["severity"] = severity
        return details
    if display_kind == "skill_invocation":
        return skill_details(text)
    if display_kind == "todo_reminder":
        return todo_details(metadata)
    return None


def custom_type_of(row: Mapping[str, Any]) -> str | None:
    """The row's ``custom_type``, lifted from the legacy ``display_kind`` when it has none."""
    custom_type = row.get("custom_type")
    if isinstance(custom_type, str) and custom_type:
        return custom_type
    display_kind = row.get("display_kind")
    return DISPLAY_KIND_TO_CUSTOM_TYPE.get(display_kind) if isinstance(display_kind, str) else None


def _split_image_blocks(content: Any) -> tuple[Any, list[Any]]:
    """``(text_content, image_blocks)`` for a block list; anything else stays text-only."""
    if not isinstance(content, list):
        return content, []
    images = [part for part in content if isinstance(part, Mapping) and part.get("type") in {"image", "input_image", "image_url"}]
    if not images:
        return content, []
    text = [part for part in content if part not in images]
    return text, images


def project_for_api(message: dict[str, Any], *, custom_messages: bool) -> list[dict[str, Any]]:
    """ADR-10 D9/D11 projection of one cloned API message.

    Returns the message(s) to send (empty = drop), with only a provider role. ``custom`` rows are
    converted regardless of the session flag (they must never reach a provider); legacy
    ``display_kind`` rows convert only when the session enabled custom messages.
    """
    role = message.get("role")
    custom_type = custom_type_of(message)
    display_only = message.get("display_only") is True or custom_type in DISPLAY_ONLY_CUSTOM_TYPES
    message_bearing = custom_type in MESSAGE_CUSTOM_TYPES if custom_type else False
    # Only a custom row (or a custom-message session's mapped legacy row) is in scope. Any mapped
    # legacy row enters so a display-only notice can be dropped, not replayed as a user turn.
    if role != "custom" and not (custom_messages and custom_type and role in {"user", "assistant"}):
        return [message] if role in API_ROLES else []
    if display_only:
        return []
    if not message_bearing:
        return []
    attribution = message.get("attribution")
    legacy_skill = message.get("display_kind") == "skill_invocation"
    target = "user" if custom_type in {"skill-prompt", "collab-prompt"} and (
        attribution == "user" or legacy_skill) else "developer"
    content = message.get("content")
    text_content, images = _split_image_blocks(content)
    primary = {**message, "role": target, "content": text_content}
    for key in (*_CUSTOM_FIELDS, "display_kind", "display_metadata"):
        primary.pop(key, None)
    if not images:
        return [primary]
    # D9: ordinary custom blocks with images send the text as developer and the images as user.
    image_message = {**primary, "role": "user", "content": images}
    return [primary, image_message]


__all__ = [
    "API_ROLES", "CUSTOM_STORAGE_KEY", "DISPLAY_KIND_TO_CUSTOM_TYPE",
    "DISPLAY_ONLY_CUSTOM_TYPES", "LEGACY_ROLE_DISPLAY_KINDS", "MESSAGE_CUSTOM_TYPES", "adviser_notes",
    "custom_fields", "custom_type_of", "legacy_details", "lift_stored_fields",
    "project_for_api", "skill_details", "storage_metadata", "todo_details",
]
