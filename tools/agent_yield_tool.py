"""``yield``: the explicit completion tool a named agent gets for one run.

The tool is injected as a per-run ``extra_tools`` handler by :mod:`tools.agent_tool` (and the
resident-session rebuild path), so it never enters the process-wide registry or the ``agents``
toolset. A turn ends when the handler records a terminal payload; the tool round checks that
payload after executing the batch and breaks without ``agent.interrupt()`` (which would mark the
turn interrupted and could drop sibling tool results).

``final=false`` records one incremental section and lets the turn continue. When the agent stops
without a terminal ``yield`` (or yields ``final=true`` without ``data``), the sections are
assembled into the output object the definition's ``output`` schema describes: array fields
accumulate, object fields merge, other fields take the last value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Sequence

#: Failed schema validations returned to the model before the payload is accepted as-is; the
#: post-run output validator then reports the contract failure instead of looping forever.
MAX_SCHEMA_RETRIES = 2

_SCHEMA_PROPERTIES = "properties"


@dataclass(frozen=True)
class YieldSnapshot:
    """One immutable view of a turn's yield state."""

    #: The assembled/explicit structured value; ``None`` when nothing was yielded.
    data: Any = None
    error: str = ""
    sections: tuple[dict[str, Any], ...] = ()
    #: True when a terminal ``yield`` (``final=true`` or ``error``) ended the turn.
    terminal: bool = False
    #: The text a caller records as the answer (string data verbatim, everything else JSON).
    text: str = ""

    @property
    def empty(self) -> bool:
        return self.data is None and not self.error and not self.sections


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _type_names(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _is_true(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


class YieldState:
    """Per-run yield bookkeeping: one terminal payload and any incremental sections.

    ``keys`` switches the tool into workpool-item mode: each call carries a 1-based
    ``key``, one item is recorded per call, and the last remaining key makes the
    assembled item map terminal. Without ``keys`` the omp single-result protocol
    (``data``/``type``/``error``/``final``) is unchanged.
    """

    def __init__(self, schema: Optional[dict[str, Any]] = None,
                 keys: Optional[Sequence[str]] = None) -> None:
        self.schema = schema if isinstance(schema, dict) else None
        self.keys: tuple[str, ...] = tuple(str(key) for key in (keys or ()))
        self.payload: Optional[dict[str, Any]] = None
        self.sections: list[dict[str, Any]] = []
        self.schema_failures = 0
        self._completed_keys: set[int] = set()

    # -- turn lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        """Clear the previous turn's terminal payload; a wake turn must not break immediately."""
        self.payload = None
        self.sections.clear()
        self.schema_failures = 0
        self._completed_keys.clear()

    @property
    def remaining_keys(self) -> tuple[int, ...]:
        """1-based keys not yet recorded, in item order."""
        return tuple(index + 1 for index in range(len(self.keys))
                     if index not in self._completed_keys)

    # -- recording --------------------------------------------------------------

    def record(self, args: dict[str, Any]) -> str:
        """Apply one ``yield`` call and return the tool result the model sees."""
        values = args if isinstance(args, dict) else {}
        if self.keys:
            return self._record_keyed(values)
        has_data = "data" in values
        data = values.get("data")
        error = str(values.get("error") or "").strip()
        if error:
            # An error is terminal whatever ``final`` says, and data already produced survives.
            self.payload = {"has_data": has_data, "data": data, "error": error}
            return json.dumps({"status": "yielded", "error": error}, ensure_ascii=False)
        if not _is_true(values.get("final"), default=True):
            if not has_data:
                return json.dumps(
                    {"error": "incremental yield requires data"}, ensure_ascii=False)
            self.sections.append({
                "data": data,
                "type": list(_type_names(values.get("type"))),
            })
            return json.dumps(
                {"status": "recorded", "sections": len(self.sections)}, ensure_ascii=False)

        if self.schema is not None:
            candidate = self._candidate(data, has_data)
            if candidate is not None:
                valid, errors = self._validate(candidate)
                if not valid:
                    self.schema_failures += 1
                    if self.schema_failures <= MAX_SCHEMA_RETRIES:
                        return json.dumps({
                            "error": "yield data does not match the output schema",
                            "errors": list(errors),
                            "retry": True,
                        }, ensure_ascii=False)
        self.payload = {"has_data": has_data, "data": data, "error": ""}
        return json.dumps({"status": "yielded", "final": True}, ensure_ascii=False)

    def _record_keyed(self, values: dict[str, Any]) -> str:
        """Record one workpool item; the last remaining key makes the item map terminal."""
        raw_key = values.get("key")
        if isinstance(raw_key, bool) or not isinstance(raw_key, int):
            return json.dumps({"error": "workpool yield requires an integer key"}, ensure_ascii=False)
        index = raw_key - 1
        if index < 0 or index >= len(self.keys):
            return json.dumps(
                {"error": f"key must be between 1 and {len(self.keys)}"}, ensure_ascii=False)
        if index in self._completed_keys:
            return json.dumps(
                {"error": f"key {raw_key} was already yielded", "remaining": list(self.remaining_keys)},
                ensure_ascii=False)
        has_data = "data" in values
        error = str(values.get("error") or "").strip()
        if not has_data and not error:
            return json.dumps(
                {"error": "workpool yield requires data or error"}, ensure_ascii=False)
        item_id = self.keys[index]
        value = {"error": error} if error else values.get("data")
        section = {"data": value, "type": [item_id]}
        completed = (*self.sections, section)
        remaining = tuple(key for key in range(1, len(self.keys) + 1)
                          if key not in self._completed_keys and key != raw_key)
        if remaining:
            self._completed_keys.add(index)
            self.sections.append(section)
            return json.dumps({"status": "recorded", "remaining": list(remaining)},
                              ensure_ascii=False)
        candidate = self._assemble(completed)
        if self.schema is not None:
            valid, errors = self._validate(candidate)
            if not valid:
                self.schema_failures += 1
                if self.schema_failures <= MAX_SCHEMA_RETRIES:
                    return json.dumps({
                        "error": "yield data does not match the output schema",
                        "errors": list(errors),
                        "retry": True,
                    }, ensure_ascii=False)
        self._completed_keys.add(index)
        self.sections.append(section)
        self.payload = {"has_data": True, "data": candidate, "error": ""}
        return json.dumps({"status": "yielded", "final": True}, ensure_ascii=False)

    def _candidate(self, data: Any, has_data: bool) -> Any:
        """The output a terminal yield would produce, before it is recorded."""
        if has_data:
            if isinstance(data, dict) and self.sections:
                assembled = self._assemble(tuple(self.sections))
                if isinstance(assembled, dict):
                    return {**assembled, **data}
            return data
        return self._assemble(tuple(self.sections)) if self.sections else None

    def _validate(self, data: Any) -> tuple[bool, tuple[str, ...]]:
        try:
            from tools.delegation_output_schema import validate_output

            valid, errors = validate_output(_text(data), self.schema or {})
            return bool(valid), tuple(str(item) for item in errors)
        except Exception:  # health: allow BLE001 -- validation availability must not eat a yield
            return True, ()

    # -- result -----------------------------------------------------------------

    def has_terminal(self) -> bool:
        return self.payload is not None

    def snapshot(self) -> Optional[YieldSnapshot]:
        """The turn's output, or ``None`` when this turn yielded nothing."""
        sections = tuple(dict(section) for section in self.sections)
        if self.payload is None:
            if not sections:
                return None
            data = self._assemble(sections)
            return YieldSnapshot(data=data, sections=sections, text=_text(data))
        payload = self.payload
        error = str(payload.get("error") or "")
        data = self._candidate(payload.get("data"), bool(payload.get("has_data")))
        text = _text(data) if data is not None else (error or "")
        return YieldSnapshot(data=data, error=error, sections=sections, terminal=True,
                             text=text)

    def result_text(self) -> str:
        snapshot = self.snapshot()
        return snapshot.text if snapshot is not None else ""

    # -- assembly ---------------------------------------------------------------

    def _property(self, name: str) -> dict[str, Any]:
        properties = (self.schema or {}).get(_SCHEMA_PROPERTIES)
        value = properties.get(name) if isinstance(properties, dict) else None
        return value if isinstance(value, dict) else {}

    def _is_array_field(self, name: str) -> bool:
        kind = self._property(name).get("type")
        if kind == "array":
            return True
        return isinstance(kind, list) and "array" in kind

    def _assemble(self, sections: tuple[dict[str, Any], ...]) -> Any:
        root: dict[str, Any] = {}
        loose: list[Any] = []
        for section in sections:
            data = section.get("data")
            names = _type_names(section.get("type"))
            if not names:
                if isinstance(data, dict):
                    root.update(data)
                else:
                    loose.append(data)
                continue
            for name in names:
                if self._is_array_field(name):
                    bucket = root.get(name)
                    if not isinstance(bucket, list):
                        bucket = [] if bucket is None else [bucket]
                        root[name] = bucket
                    if isinstance(data, list):
                        bucket.extend(data)
                    else:
                        bucket.append(data)
                elif isinstance(data, dict) and isinstance(root.get(name), dict):
                    root[name] = {**root[name], **data}
                elif name in root and isinstance(root[name], list):
                    root[name].append(data)
                else:
                    root[name] = data
        if loose:
            bucket = root.setdefault("sections", [])
            if isinstance(bucket, list):
                bucket.extend(loose)
        if not root:
            return loose[0] if len(loose) == 1 else (loose or None)
        return root


YIELD_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "data": {
            "description": ("The final output (with final=true) or one incremental section "
                            "(with final=false). Any JSON value."),
        },
        "type": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "description": ("Output-schema field(s) this incremental section belongs to; "
                            "array fields accumulate, others take the last value."),
        },
        "error": {
            "type": "string",
            "description": "Terminal error to record; data already produced is kept.",
        },
        "final": {
            "type": "boolean",
            "default": True,
            "description": "true ends the turn with data; false records a section and continues.",
        },
    },
    "additionalProperties": False,
}


def _yield_parameters(keys: Sequence[str]) -> dict[str, Any]:
    if not keys:
        return YIELD_PARAMETERS
    properties = dict(YIELD_PARAMETERS["properties"])
    properties["key"] = {
        "type": "integer",
        "minimum": 1,
        "maximum": len(keys),
        "description": ("1-based item number from the workpool batch. Yield each item once; "
                        "the tool response names the remaining keys."),
    }
    return {**YIELD_PARAMETERS, "properties": properties}


def build_yield_tool(state: YieldState) -> dict[str, Any]:
    """The ``extra_tools`` entry ``agent_run.run`` merges for one named agent."""
    keyed = bool(state.keys)
    description = (
        "Finish this turn explicitly. final=true (default) ends the turn and its data is "
        "the answer; final=false records an incremental output-schema section and keeps "
        "working. Without any yield call the final assistant message remains the answer."
        if not keyed else
        "Yield one workpool item per call: pass its 1-based key and that item's data (or "
        "error). The tool response names the remaining keys; the last key ends the turn.")
    return {
        "name": "yield",
        "description": description,
        "parameters": _yield_parameters(state.keys),
        "handler": state.record,
    }


__all__ = ["MAX_SCHEMA_RETRIES", "YIELD_PARAMETERS", "YieldSnapshot", "YieldState",
           "build_yield_tool"]
