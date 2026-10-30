"""Todo tool: in-memory, revisioned two-level plan for multi-step work.

State lives on the ``TodoStore`` and is written with ``phases`` — each phase names a
stage of the work and holds its checklist items, so ordering is visible at a glance
instead of encoded in ``parent`` chains. Items are stored flat (``id, content, status,
phase``) so snapshots, the TUI wire format and every transcript-replay consumer keep a
stable shape; ``phases()`` groups them for the tool payload.

Every call returns the full plan. No system-prompt mutation.
"""

import json
from typing import Any, Dict, List, Optional

VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}
# The list is re-read after every compression (format_for_injection), so unbounded
# content/count would defeat the compression it rides through. Caps apply equally to
# model-authored items and caller-replayed API history.
MAX_TODO_CONTENT_CHARS = 4000
MAX_TODO_ITEMS = 256
MAX_TODO_PHASES = 32
# Max single todo tool-result payload accepted during history hydration, so a forged
# oversized result is dropped before parsing (AIAgent._hydrate_todo_store).
MAX_TODO_RESULT_CHARS = 512_000
_TRUNCATION_MARKER = "… [truncated]"
# Adopted by flat item lists written before the two-level schema (replay/hydration).
LEGACY_PHASE_NAME = "Tasks"
# Persisted as ordinary message content; ContextCompressor keys on this stable header to
# tell the synthetic post-compaction row from a real user message.
TODO_INJECTION_HEADER = "[Your active task list was preserved across context compression]"
_STATUS_MARKERS = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]", "cancelled": "[~]"}
_ACTIVE_STATUSES = {"pending", "in_progress"}


def _normalize_phases(raw: Any) -> list[dict[str, Any]]:
    """Validate a ``[{phase, items}]`` list, or wrap a legacy flat item list in one phase.

    Raises ``ValueError`` for a shape that is neither; item validation happens later so
    a rejected write leaves the store untouched.
    """
    if not isinstance(raw, list):
        raise ValueError(f"phases must be a list of {{phase, items}} objects, got {type(raw).__name__}")
    if raw and all(isinstance(entry, dict) and "items" not in entry
                   and ("content" in entry or "status" in entry) for entry in raw):
        return [{"phase": None, "items": raw}]
    phases: list[dict[str, Any]] = []
    for entry in raw[:MAX_TODO_PHASES]:
        if not isinstance(entry, dict):
            raise ValueError("each phase must be an object with 'phase' and 'items'")
        name = str(entry.get("phase", "")).strip()
        if not name:
            raise ValueError("each phase needs a non-empty 'phase' name")
        items = entry.get("items")
        if items is None:
            items = []
        if not isinstance(items, list):
            raise ValueError(f"phase '{name}': 'items' must be a list")
        phases.append({"phase": name, "items": items})
    return phases


def flatten_phases(value: Any) -> list[dict[str, Any]]:
    """Flat item list from a two-level payload (``phases``) or a legacy ``todos`` list.

    Shared by every consumer that reads a Todo tool result (hydration, TUI/ACP, gateway)
    so the tool payload has exactly one shape while old transcripts still replay.
    """
    if isinstance(value, dict):
        phases = value.get("phases")
        if isinstance(phases, list):
            return [item for phase in phases if isinstance(phase, dict)
                    for item in (phase.get("items") or []) if isinstance(item, dict)]
        value = value.get("todos")
    if not isinstance(value, list):
        return []
    if value and all(isinstance(entry, dict) and "items" not in entry for entry in value):
        return [entry for entry in value if isinstance(entry, dict)]
    return [item for phase in value if isinstance(phase, dict)
            for item in (phase.get("items") or []) if isinstance(item, dict)]


class TodoStore:
    """In-memory two-level plan, one per AIAgent: phases hold items.

    Items are ``{id, content, status, phase}``; list position is priority within a
    phase. Phase order is first appearance.
    """

    def __init__(self):
        self._items: list[dict[str, str]] = []
        self._revision = 0

    def _fresh_items(self, phases: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Validate, dedupe and order a whole new plan (replace / restore)."""
        items = [self._validate(item, phase["phase"])
                 for phase in _normalize_phases(phases) for item in phase["items"]]
        items = self._dedupe_by_id(items)[:MAX_TODO_ITEMS]
        self._assign_ids(items)
        return self._normalize_order(items)

    def write(self, phases: list[dict[str, Any]], merge: bool = False) -> list[dict[str, str]]:
        """Replace the plan (default) or merge by id; returns the full item list after writing.
        Raises ValueError (leaving the plan untouched) if any item is invalid."""
        before = self.read()
        try:
            if merge:
                self._merge(phases)
            else:
                self._items = self._fresh_items(phases)
        except ValueError:
            self._items = before  # rejected write must not leave a partial list behind
            raise
        del self._items[MAX_TODO_ITEMS:]  # keep the priority head; replays can't grow unbounded
        if self._items != before:
            self._revision += 1
        return self.read()

    def _merge(self, phases: list[dict[str, Any]]) -> None:
        """Update existing items by id (content/status/phase), append new ones.

        A new item without an id gets a fresh one; an explicit id that matches nothing
        is kept so the model's own short handles survive.
        """
        existing = {item["id"]: item for item in self._items}
        appended: list[dict[str, str]] = []
        for phase in _normalize_phases(phases):
            for raw in phase["items"]:
                item_id = str(raw.get("id", "")).strip() if isinstance(raw, dict) else ""
                cur = existing.get(item_id) if item_id else None
                if cur is None:
                    appended.append(self._validate(raw, phase["phase"]))
                    continue
                # Partial update: absent/empty content keeps the existing text; a valid
                # status wins; the item moves to the phase named by this write.
                if isinstance(raw, dict) and str(raw.get("content", "")).strip():
                    cur["content"] = self._cap_content(str(raw["content"]).strip())
                status = str(raw.get("status", "")).strip().lower() if isinstance(raw, dict) else ""
                if status in VALID_STATUSES:
                    cur["status"] = status
                cur["phase"] = self._phase_for(raw, phase["phase"])
        self._items = list(existing.values()) + self._dedupe_by_id(appended)
        self._assign_ids(self._items)
        self._items = self._normalize_order(self._items)

    def read(self) -> list[dict[str, str]]:
        return [item.copy() for item in self._items]

    def phases(self) -> list[dict[str, Any]]:
        """Items grouped under their phase, in first-appearance order (tool payload shape)."""
        order: list[str] = []
        groups: dict[str, list[dict[str, str]]] = {}
        for item in self._items:
            name = item.get("phase") or LEGACY_PHASE_NAME
            if name not in groups:
                groups[name] = []
                order.append(name)
            groups[name].append(item.copy())
        return [{"phase": name, "items": groups[name]} for name in order]

    def has_items(self) -> bool:
        return bool(self._items)

    def snapshot(self) -> dict[str, Any]:
        """Full state clients can reconcile atomically (flat items carry their ``phase``)."""
        return {"todos": self.read(), "revision": self._revision}

    def restore(self, phases: Any, *, revision: Any = 0) -> list[dict[str, str]]:
        """Restore a trusted snapshot without manufacturing a new revision.

        Accepts both the current phase payload and the flat ``todos`` list that older
        transcripts/replays carry; flat items land in :data:`LEGACY_PHASE_NAME`.
        """
        self._items = self._fresh_items(phases)[:MAX_TODO_ITEMS]
        try:
            self._revision = max(0, int(revision or 0))
        except (TypeError, ValueError):
            self._revision = 0
        return self.read()

    def format_for_injection(self) -> Optional[str]:
        """Render the plan for post-compression injection, or None if nothing active. Only
        pending/in_progress items are injected — finished ones make the model re-do work after
        compression; a phase heading is kept only while it still has active work."""
        if not self._items:
            return None
        lines = [TODO_INJECTION_HEADER]
        for group in self.phases():
            active = [item for item in group["items"] if item["status"] in _ACTIVE_STATUSES]
            if not active:
                continue
            lines.append(f"## {group['phase']}")
            for item in active:
                marker = _STATUS_MARKERS.get(item["status"], "[?]")
                lines.append(f"- {marker} {item['id']}. {item['content']} ({item['status']})")
        return "\n".join(lines) if len(lines) > 1 else None

    @staticmethod
    def _cap_content(content: str) -> str:
        """Truncate to MAX_TODO_CONTENT_CHARS keeping the head (the actionable part) + marker."""
        if len(content) > MAX_TODO_CONTENT_CHARS:
            return content[:MAX_TODO_CONTENT_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
        return content

    @staticmethod
    def _phase_for(item: Any, phase: Optional[str]) -> str:
        """Phase named by the enclosing phase entry, else the item's own, else the legacy default."""
        return phase or (str(item.get("phase") or "").strip() if isinstance(item, dict) else "") or LEGACY_PHASE_NAME

    @staticmethod
    def _validate(item: dict[str, Any], phase: Optional[str]) -> dict[str, str]:
        """Normalize one item to ``{id, content, status, phase}`` (no id when omitted).

        ``phase=None`` marks a legacy flat item list: each item keeps its own phase when
        it carries one, and otherwise lands in :data:`LEGACY_PHASE_NAME`.
        """
        if not isinstance(item, dict):
            return {"id": "", "content": "(invalid item)", "status": "pending",
                    "phase": TodoStore._phase_for(item, phase)}
        item_id = str(item.get("id", "")).strip()
        content = str(item.get("content", "")).strip()
        if not content:
            raise ValueError(
                f"Todo item '{item_id or '(no id)'}' in phase '{TodoStore._phase_for(item, phase)}' has empty or missing content. "
                "Each item must have a non-empty description."
            )
        status = str(item.get("status", "pending")).strip().lower()
        name = TodoStore._phase_for(item, phase)
        return {"id": item_id,
                "content": TodoStore._cap_content(content),
                "status": status if status in VALID_STATUSES else "pending",
                "phase": name}

    @staticmethod
    def _assign_ids(items: list[dict[str, str]]) -> None:
        """Give every id-less item the next free numeric id (in place)."""
        used = {item["id"] for item in items if item.get("id")}
        next_id = 1
        for item in items:
            if item.get("id"):
                continue
            while str(next_id) in used:
                next_id += 1
            item["id"] = str(next_id)
            used.add(item["id"])

    @staticmethod
    def _dedupe_by_id(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse duplicate explicit ids, keeping the last occurrence in its position."""
        seen: set[str] = set()
        kept: list[dict[str, Any]] = []
        for item in reversed(items):
            key = str(item.get("id", "")).strip() if isinstance(item, dict) else ""
            if key:
                if key in seen:
                    continue
                seen.add(key)
            kept.append(item)
        kept.reverse()
        return kept

    @staticmethod
    def _normalize_order(items: list[dict[str, str]]) -> list[dict[str, str]]:
        """Group by phase (first-appearance order) and lift the in_progress item ahead of an
        earlier pending placeholder inside its phase. Item order is otherwise authored order."""
        groups: dict[str, list[dict[str, str]]] = {}
        for item in items:
            groups.setdefault(item.get("phase") or LEGACY_PHASE_NAME, []).append(item)
        ordered: list[dict[str, str]] = []
        for group in groups.values():
            statuses = [item["status"] for item in group]
            if "in_progress" in statuses and "pending" in statuses[:statuses.index("in_progress")]:
                active_index = statuses.index("in_progress")
                group = group.copy()
                group.insert(statuses.index("pending"), group.pop(active_index))
            ordered.extend(group)
        return ordered


def todo_tool(phases: Optional[list[dict[str, Any]]] = None, merge: bool = False,
              store: Optional[TodoStore] = None) -> str:
    """Write ``phases`` (replace, or ``merge`` by item id) or read when None -> plan + summary JSON."""
    if store is None:
        return tool_error("TodoStore not initialized")
    if phases is None:
        items = store.read()
    else:
        if not isinstance(phases, list):
            return tool_error(f"phases must be a list, got {type(phases).__name__}")
        try:
            items = store.write(phases, merge)
        except ValueError as e:
            return tool_error(str(e))
    grouped = store.phases()
    summary = {"total": len(items), "phases": len(grouped)}
    for status in ("pending", "in_progress", "completed", "cancelled"):
        summary[status] = sum(1 for i in items if i["status"] == status)
    return json.dumps({"phases": grouped, "revision": store.snapshot()["revision"],
                       "summary": summary}, ensure_ascii=False)


def check_todo_requirements() -> bool:
    """Todo tool has no external requirements -- always available."""
    return True


# Behavioral guidance is baked into the (static, cached) description; plan shape and merge
# semantics live ONLY in the parameter schema.
TODO_SCHEMA = {
    "name": "todo_list",
    "description": (
        # See #95681.
        "Track a phase-by-phase task plan for multi-step work (3+ steps). "
        "Two levels: ordered phases, each holding ordered checklist items. "
        "Use for complex tasks with 3+ steps or when the user provides multiple "
        "tasks. For 'all N items' tasks, enumerate every instance as its own item "
        "so none are silently dropped. Call with no parameters to read the current plan.\n"
        "Phase order is execution order; item order is priority within its phase. Only ONE "
        "item in_progress at a time across the whole plan. Put the current stage's work in "
        "its own phase instead of inventing subtasks. Mark an item completed only after "
        "the work is verified done, never based on intent. If something fails, cancel it "
        "and add a revised item. Always returns the full current plan."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "phases": {
                "type": "array",
                "description": "Plan to write: ordered phases, each with its ordered items.",
                "items": {
                    "type": "object",
                    "properties": {
                        "phase": {
                            "type": "string",
                            "description": "Stage name, e.g. 'Research', 'Implement', 'Verify'."
                        },
                        "items": {
                            "type": "array",
                            "description": "Checklist items for this phase.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "description": "Stable item id (assigned when omitted). Required to update an item with merge=true."
                                    },
                                    "content": {
                                        "type": "string",
                                        "description": "Task description"
                                    },
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "completed", "cancelled"]
                                    }
                                },
                                "required": ["content"]
                            }
                        }
                    },
                    "required": ["phase", "items"]
                }
            },
            "merge": {
                "type": "boolean",
                "description": (
                    "true: update existing items by id (content/status/phase), add new ones. "
                    "false (default): replace the entire plan with a fresh one."
                ),
                "default": False
            }
        },
        "required": []
    }
}

# Pre-rename names that replay as the Todo tool. model_tools._LEGACY_TOOL_ALIASES derives its todo
# entries from this, so the alias map and the transcript/TUI predicates below cannot drift.
TODO_LEGACY_ALIASES = ("todo",)
TODO_TOOL_NAMES = frozenset((TODO_SCHEMA["name"], *TODO_LEGACY_ALIASES))


def is_todo_tool_name(name: Any) -> bool:
    """True for the Todo tool's current name or a legacy alias (an already-unwrapped dispatch name)."""
    return isinstance(name, str) and name in TODO_TOOL_NAMES


def is_todo_tool_call(tool_call: Any) -> bool:
    """True when a transcript tool_call entry (dict or object) invoked the Todo tool.

    Covers the current name, legacy aliases, and the ``tool_call`` bridge (``todo_list`` is deferred by
    default, and the transcript keeps the bridge name). The bridge is peeled from the recorded arguments
    only, never live tool-search config, and must wrap exactly one call. Keep this module free of model_tools / agent.tool_executor
    imports: TUI resume and run_agent call this without loading either.
    """
    from agent.message_sanitization import _tc_field

    fn = _tc_field(tool_call, "function")
    name, raw_args = _tc_field(fn, "name") or "", _tc_field(fn, "arguments")
    if is_todo_tool_name(name):
        return True
    # Cheap heuristic before the bridge modules load: skip args without a literal "todo". Only a
    # unicode-escaped name slips past, which json.dumps never writes for ASCII.
    if isinstance(raw_args, str) and "todo" not in raw_args:
        return False
    from tools.tool_search_catalog import TOOL_CALL_NAME

    if name != TOOL_CALL_NAME:
        return False
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError:
        return False
    if not isinstance(args, dict):
        return False
    from tools.tool_search_validation import normalize_tool_call_entries

    entries, error = normalize_tool_call_entries(args)
    return not error and len(entries) == 1 and is_todo_tool_name(entries[0]["name"])


from tools.registry import registry, tool_error

registry.register(
    name="todo_list", toolset="todo", schema=TODO_SCHEMA, check_fn=check_todo_requirements,
    handler=lambda args, **kw: todo_tool(
        phases=args.get("phases"), merge=args.get("merge", False), store=kw.get("store")),
    emoji="📋")
