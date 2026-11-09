"""Host ops for ``execute_code`` kernels beyond the judgment seam (ADR-4 D10/D11).

A cell reaches these through the same line-JSON tool RPC as ordinary tools; ``__``
names are reserved, never model tools, and never become transcript rows:

* ``__display__`` / ``__status__`` — structured card data for the current cell
  (views, images, status events). Recorded on the cell's authority and merged
  into the result's additive ``display`` block.
* ``__agent__`` / ``__workpool__`` / ``__wait__`` / ``__cancel__`` — per-session
  handles over the existing delegation/named-agent machinery. Handles outlive
  the cell that created them; the registry is per session (``_resolve_owner`` is
  the same owner the kernels use) and is cleared with the session.
* ``__completion__`` — one-shot auxiliary completion on the session's aux role
  chain, run off the cell thread.
* ``__out__`` — write to the current turn's reply sink. Only valid while a turn
  is live; no transcript row, no second output path.

Every failure is a JSON ``{"error": {type, message}}`` object (matching the
judgment ops) so a cell can branch without parsing transport errors.
"""

from __future__ import annotations

import atexit
import contextvars
import json
import logging
import threading
import time
import uuid
from hermes_threading import spawn_context_thread
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

HOST_OP_NAMES = frozenset({
    "__display__", "__status__", "__wait__", "__cancel__", "__send__", "__agent__",
    "__workpool__", "__completion__", "__out__",
})

#: The cell whose ``display`` block receives ``__display__``/``__status__``.
_CELL_DISPLAY: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "hermes_eval_cell_display", default=None)
_CELL_TASK: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hermes_eval_cell_task", default="")

_COMPLETION_THREADS: set[threading.Thread] = set()
_THREADS_LOCK = threading.Lock()


class _SessionState:
    """Handles/workpools/completions for one kernel owner (session)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.handles: dict[str, dict[str, Any]] = {}
        self.workpools: dict[str, dict[str, Any]] = {}
        self.completions: dict[str, dict[str, Any]] = {}


_STATES: dict[str, _SessionState] = {}
_STATES_LOCK = threading.Lock()


def _session_key(task_id: str = "") -> str:
    from tools.code_kernel import _resolve_owner
    try:
        return _resolve_owner(task_id) or ""
    except Exception:  # health: allow BLE001 -- identity resolution must not break the op
        return task_id or ""


def _state(task_id: str) -> _SessionState:
    key = _session_key(task_id)
    with _STATES_LOCK:
        state = _STATES.get(key)
        if state is None:
            state = _STATES[key] = _SessionState()
        return state


def clear_session_state(owner: str) -> None:
    """Forget every handle/pool/completion a session owns (``approval.clear_session``)."""
    if not owner:
        return
    with _STATES_LOCK:
        doomed = [key for key in _STATES if key == owner or key.startswith(owner + "::child::")]
    for key in doomed:
        with _STATES_LOCK:
            state = _STATES.pop(key, None)
        if state is not None:
            _close_state(state)


def clear_all_session_state() -> None:
    """Dispose every session's eval host state (atexit and tests)."""
    with _STATES_LOCK:
        states = list(_STATES.values())
        _STATES.clear()
    for state in states:
        _close_state(state)


def _close_state(state: _SessionState) -> None:
    with state.lock:
        pools, completions = list(state.workpools.values()), list(state.completions.values())
        state.handles.clear()
        state.workpools.clear()
        state.completions.clear()
    for pool in pools:
        for handle_id in list(pool.get("ids") or []):
            try:
                _cancel_handle(handle_id, state)
            except Exception:  # health: allow BLE001 -- teardown is best effort
                logger.debug("eval host op: workpool cancel failed", exc_info=True)
    for record in completions:
        _cancel_completion(record)


def set_cell_display(display: dict) -> contextvars.Token:
    """Bind the current cell's display sink for the duration of one RPC dispatch."""
    return _CELL_DISPLAY.set(display)


def reset_cell_display(token: contextvars.Token) -> None:
    _CELL_DISPLAY.reset(token)


def _current_display() -> Optional[dict]:
    return _CELL_DISPLAY.get()


def _error(exc: BaseException) -> str:
    return json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}},
                      ensure_ascii=False)


def _ok(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---- display / status -----------------------------------------------------


_MAX_DISPLAY_IMAGES = 2
_MAX_IMAGE_DATA_CHARS = 4_000
_MAX_DISPLAY_IMAGE_CHARS = 8_000
_MAX_VIEW_TEXT_CHARS = 2_000
_MAX_DISPLAY_VIEWS = 4
_MAX_STATUS_EVENTS = 32
_MAX_STATUS_EVENT_CHARS = 2_000


def _normalize_images(items: Any) -> tuple[list[dict[str, Any]], int]:
    """Accept ``{type:"image", data, mimeType}`` (the wire ImageContent) only."""
    images: list[dict[str, Any]] = []
    skipped = 0
    if not isinstance(items, list):
        return images, skipped
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        data = item.get("data")
        mime = item.get("mimeType") or item.get("mime_type")
        if isinstance(data, str) and data and isinstance(mime, str) and mime:
            if len(data) > _MAX_IMAGE_DATA_CHARS:
                skipped += 1
                continue
            image = {"type": "image", "data": data, "mimeType": mime}
            if isinstance(item.get("detail"), str):
                image["detail"] = item["detail"]
            images.append(image)
        else:
            skipped += 1
    return images, skipped


def _display_op(args: Any) -> str:
    try:
        sink = _current_display()
        if sink is None:
            return _error(RuntimeError("display() is only available inside an execute_code cell"))
        params = args if isinstance(args, dict) else {}
        skipped = 0
        views = params.get("views")
        if isinstance(views, list):
            for entry in views:
                if not isinstance(entry, dict) or not isinstance(entry.get("text"), str) or "view" not in entry:
                    continue
                if len(entry["text"]) > _MAX_VIEW_TEXT_CHARS or len(sink["views"]) >= _MAX_DISPLAY_VIEWS:
                    skipped += 1
                    continue
                sink["views"].append({"text": entry["text"], "view": entry["view"]})
        normalized, image_skipped = _normalize_images(params.get("images"))
        skipped += image_skipped
        for image in normalized:
            used = sum(len(item.get("data") or "") for item in sink["images"])
            if len(sink["images"]) >= _MAX_DISPLAY_IMAGES or used + len(image["data"]) > _MAX_DISPLAY_IMAGE_CHARS:
                skipped += 1
                continue
            sink["images"].append(image)
        status_events = params.get("statusEvents")
        if isinstance(status_events, list):
            for event in status_events:
                if not isinstance(event, dict):
                    skipped += 1
                    continue
                if (len(sink["statusEvents"]) >= _MAX_STATUS_EVENTS
                        or len(json.dumps(event, default=str)) > _MAX_STATUS_EVENT_CHARS):
                    skipped += 1
                    continue
                sink["statusEvents"].append(dict(event))
        return _ok(skipped=skipped) if skipped else _ok()
    except Exception as exc:  # health: allow BLE001 -- host ops report, never raise
        return _error(exc)


def _out_op(args: Any) -> str:
    """ADR-4 D11.6: hand text to the current turn's reply sink. No sink means no live turn."""
    try:
        params = args if isinstance(args, dict) else {}
        text = params.get("text", params.get("content"))
        if not isinstance(text, str) or not text:
            return _error(ValueError("out() requires non-empty text"))
        from agent.tool_progress_relay import current_turn_reply
        reply = current_turn_reply()
        if reply is None or reply(text) is False:
            return _error(RuntimeError("out:// is only available during an active turn"))
        return _ok()
    except Exception as exc:  # health: allow BLE001 -- host ops report failures through the result object
        return _error(exc)


def _status_op(args: Any) -> str:
    try:
        params = args if isinstance(args, dict) else {}
        item = params.get("item")
        if isinstance(item, dict):
            return _ok(**_handle_snapshot(item))
        sink = _current_display()
        event = {key: value for key, value in params.items()
                 if key not in {"token", "tool", "args", "item"}}
        op = event.get("op")
        if not isinstance(op, str) or not op:
            return _error(ValueError("status() requires a non-empty 'op'"))
        event["op"] = op
        if sink is not None:
            sink["statusEvents"].append(event)
        return _ok()
    except Exception as exc:  # health: allow BLE001 -- host ops report failures through the result object
        return _error(exc)


# ---- handles: agent / workpool / completion -------------------------------


def _handle_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _register_handle(state: _SessionState, handle_id: str, **fields: Any) -> dict[str, Any]:
    record = {"id": handle_id, "created_at": time.time(), **fields}
    with state.lock:
        state.handles[handle_id] = record
    return record


def _get_handle(state: _SessionState, handle_id: str) -> Optional[dict[str, Any]]:
    with state.lock:
        return state.handles.get(handle_id)


def _dispatch_tool(name: str, args: dict, task_id: str) -> Any:
    """Run one ordinary registry tool under the cell's captured authority."""
    from model_tools import handle_function_call
    parent = _current_parent_agent()
    session_id = str(getattr(parent, "session_id", "") or "")
    return handle_function_call(name, args, task_id=task_id, session_id=session_id)


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if isinstance(raw, str) else {}
    except (TypeError, ValueError):
        return {"error": str(raw)[:300]}
    return value if isinstance(value, dict) else {}


def _joined_text(result: dict[str, Any]) -> str:
    """Model-visible text of a synchronously joined delegation batch."""
    entries = result.get("results")
    if not isinstance(entries, list):
        return str(result.get("error") or "")
    parts = []
    for entry in entries:
        if not isinstance(entry, dict):
            parts.append(str(entry))
            continue
        text = entry.get("summary") or entry.get("result") or entry.get("error") or ""
        if text:
            parts.append(str(text))
    return "\n\n".join(parts)


def _session_grants(tool_name: str) -> bool:
    """Whether the session's resolved surface includes *tool_name*.

    The kernel's host ops bypass the sandbox allow-list on purpose (they are not model
    tools), but delegation must still follow the session grant. An unresolved surface
    (embeds, tests, first call) has no grant to check, so it does not deny.
    """
    try:
        from model_tools import _last_resolved_tool_names
        names = _last_resolved_tool_names
    except Exception:  # health: allow BLE001 -- an unavailable surface cannot be consulted
        return True
    if not names:
        return True
    return tool_name in set(names)


def _agent_op(args: Any) -> str:
    try:
        params = args if isinstance(args, dict) else {}
        prompt = params.get("prompt") or params.get("goal")
        if not isinstance(prompt, str) or not prompt.strip():
            return _error(ValueError("agent() requires a non-empty prompt"))
        task_id = _CELL_TASK.get()
        state = _state(task_id)
        agent_name = params.get("agent")
        if agent_name:
            if not _session_grants("agent"):
                return _error(PermissionError("the 'agent' tool is not enabled in this session"))
            result = _json_object(_dispatch_tool("agent", {
                "agent_name": str(agent_name),
                "user_prompts": prompt,
                "subject": params.get("label"),
                "tool_sets": params.get("tools"),
            }, task_id))
            handle_id = result.get("agent_session_id") or result.get("id")
            if not isinstance(handle_id, str) or not handle_id:
                return _ok(error=result.get("error") or "agent() did not return an agent session")
            _register_handle(state, handle_id, kind="agent", origin="named",
                             agent=str(agent_name), subject=result.get("subject"))
        else:
            if not _session_grants("delegate_task"):
                return _error(PermissionError(
                    "the 'delegate_task' tool is not enabled in this session"))
            parent = _current_parent_agent()
            if parent is None:
                return _error(RuntimeError(
                    "agent() needs a live agent turn; the delegation parent is not bound"))
            result = _json_object(parent._dispatch_delegate_task({
                "goal": prompt,
                "context": params.get("context"),
                "images": params.get("images"),
            }))
            handle_id = (result.get("delegation_id") or result.get("subagent_id")
                         or (result.get("results") or [{}])[0].get("delegation_id"))
            if not isinstance(handle_id, str) or not handle_id:
                if result.get("error"):
                    return _ok(error=result["error"])
                # Gateway inline mode joins the child in this call: it already settled, so the
                # handle is a completed one carrying the joined summary, not a dispatch id.
                handle_id = _handle_id("sync")
                _register_handle(state, handle_id, kind="agent", origin="sync",
                                 agent=None, subject=None, text=_joined_text(result))
                return _ok(id=handle_id, agent=None, handle=f"agent://{handle_id}",
                           completed=True)
            _register_handle(state, handle_id, kind="agent", origin="delegate",
                             agent=None, subject=None)
        return _ok(id=handle_id, agent=agent_name or None, handle=f"agent://{handle_id}")
    except Exception as exc:  # health: allow BLE001 -- host ops report failures through the result object
        return _error(exc)


def _agent_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    handle_id = str(record.get("id") or "")
    if record.get("origin") == "sync":
        return {"status": "completed", "text": record.get("text", "")}
    if record.get("origin") == "delegate":
        from tools.async_delegation import get_durable_delegation
        row = get_durable_delegation(handle_id) or {}
        status = str(row.get("state") or "unknown").lower()
        if status in ("running", "finalizing", "queued", "pending"):
            return {"status": "running", "delegation_id": handle_id}
        if status in ("completed", "complete", "success"):
            return {"status": "completed", "delegation_id": handle_id,
                    "result": row.get("result")}
        return {"status": "failed", "delegation_id": handle_id,
                "error": (row.get("result") or {}).get("error") if isinstance(row.get("result"), dict) else None}
    from tools.agent_inbox import _session_row
    row = _session_row(handle_id) or {}
    status = str(row.get("status") or "unknown").lower()
    live = status in ("running", "queued", "idle", "resident")
    if live:
        return {"status": "running", "agent_session_id": handle_id}
    if status in ("completed", "parked"):
        text = _named_report(handle_id)
        return {"status": "completed", "agent_session_id": handle_id, "text": text}
    return {"status": "failed", "agent_session_id": handle_id,
            "error": row.get("error") or status}


def _named_report(handle_id: str) -> str:
    """Settled named agent's report artifact, else its run status text."""
    try:
        payload = _json_object(_dispatch_tool("agent_out", {
            "agent_session_id": handle_id, "mode": "artifact", "kind": "report",
        }, _CELL_TASK.get()))
        content = payload.get("content")
        if isinstance(content, str):
            return content
        return json.dumps(payload, ensure_ascii=False)[:4000]
    except Exception:  # health: allow BLE001 -- a missing report is not a wait failure
        logger.debug("eval host op: named report read failed", exc_info=True)
        return ""


def _handle_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    kind = str(item.get("kind") or "")
    handle_id = str(item.get("id") or "")
    task_id = _CELL_TASK.get()
    state = _state(task_id)
    if kind == "completion":
        record = _completion_record(state, handle_id)
        if record is None:
            return {"status": "failed", "error": f"unknown completion handle {handle_id!r}"}
        return _completion_snapshot(record)
    record = _get_handle(state, handle_id)
    if record is None:
        return {"status": "failed", "error": f"unknown handle {handle_id!r}"}
    return _agent_snapshot(record)


def _wait_op(args: Any) -> str:
    try:
        params = args if isinstance(args, dict) else {}
        items = params.get("items")
        if not isinstance(items, list) or not items:
            return _error(ValueError("wait() requires a non-empty items list"))
        timeout_ms = params.get("timeoutMs")
        deadline = None
        if isinstance(timeout_ms, (int, float)) and timeout_ms >= 0:
            deadline = time.monotonic() + float(timeout_ms) / 1000.0
        snapshots = []
        for item in items:
            if not isinstance(item, dict):
                snapshots.append({"status": "failed", "error": "invalid handle item"})
                continue
            while True:
                snapshot = _handle_snapshot(item)
                if snapshot.get("status") != "running":
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            snapshots.append(snapshot)
        return _ok(items=snapshots)
    except Exception as exc:  # health: allow BLE001 -- host ops report failures through the result object
        return _error(exc)


def _cancel_handle(handle_id: str, state: _SessionState) -> bool:
    record = _get_handle(state, handle_id)
    if record is None:
        return False
    if record.get("origin") == "sync":
        return False
    if record.get("origin") == "delegate":
        parent = _current_parent_agent()
        if parent is None:
            return False
        result = _json_object(parent._dispatch_delegate_task({
            "action": "stop", "subagent_id": handle_id,
        }))
    else:
        from tools.agent_inbox import stop_named_session
        result = stop_named_session(handle_id, parent_agent=_current_parent_agent()) or {}
    return not result.get("error")


def _cancel_op(args: Any) -> str:
    try:
        params = args if isinstance(args, dict) else {}
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        kind = str(item.get("kind") or params.get("kind") or "")
        handle_id = str(item.get("id") or params.get("id") or "")
        state = _state(_CELL_TASK.get())
        if kind == "completion":
            record = _completion_record(state, handle_id)
            if record is None:
                return _ok(cancelled=False)
            _cancel_completion(record)
            return _ok(cancelled=True)
        return _ok(cancelled=_cancel_handle(handle_id, state))
    except Exception as exc:  # health: allow BLE001 -- host ops report failures through the result object
        return _error(exc)


def _current_parent_agent() -> Any:
    from agent.subagent_lifecycle import get_active_subagent_parent
    return get_active_subagent_parent()


def _send_op(args: Any) -> str:
    try:
        params = args if isinstance(args, dict) else {}
        handle_id = str(params.get("id") or "")
        message = params.get("message")
        if not handle_id or not isinstance(message, str) or not message.strip():
            return _error(ValueError("send() requires a handle id and a non-empty message"))
        state = _state(_CELL_TASK.get())
        record = _get_handle(state, handle_id)
        if record is None:
            return _error(ValueError(f"unknown handle {handle_id!r}"))
        if record.get("origin") == "sync":
            return _error(ValueError(f"handle {handle_id!r} already completed; start a new agent() run"))
        parent = _current_parent_agent()
        if record.get("origin") == "delegate":
            if parent is None:
                return _error(RuntimeError("send() needs a live agent turn"))
            result = _json_object(parent._dispatch_delegate_task({
                "action": "steer", "subagent_id": handle_id, "message": message,
            }))
        else:
            from tools.agent_inbox import deliver_owner_message
            delivered = deliver_owner_message(handle_id, message, parent_agent=parent) or {}
            result = {"action": "steer", **delivered}
        return _ok(**result)
    except Exception as exc:  # health: allow BLE001 -- host ops report failures through the result object
        return _error(exc)


# ---- completion -----------------------------------------------------------


def _completion_record(state: _SessionState, handle_id: str) -> Optional[dict[str, Any]]:
    with state.lock:
        return state.completions.get(handle_id)


def _completion_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    with record["lock"]:
        status = record["status"]
        payload = {"status": status}
        if status == "completed":
            payload["text"] = record.get("text", "")
            if record.get("usage") is not None:
                payload["usage"] = record["usage"]
        elif status == "failed":
            payload["error"] = record.get("error") or "completion failed"
        return payload


def _cancel_completion(record: dict[str, Any]) -> None:
    with record["lock"]:
        if record["status"] == "running":
            record["cancelled"] = True
            record["status"] = "cancelled"
    record["event"].set()


def _completion_op(args: Any) -> str:
    try:
        params = args if isinstance(args, dict) else {}
        op = params.get("op")
        state = _state(_CELL_TASK.get())
        if op == "status":
            record = _completion_record(state, str(params.get("id") or ""))
            return _ok() if record is None else _ok(**_completion_snapshot(record))
        prompt = params.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return _error(ValueError("completion() requires a non-empty prompt"))
        model = params.get("model")
        record = {
            "id": _handle_id("cmp"), "status": "running", "cancelled": False,
            "event": threading.Event(), "lock": threading.Lock(), "text": "", "error": None,
        }
        with state.lock:
            state.completions[record["id"]] = record
        thread = spawn_context_thread(
            _run_completion_worker,
            name="hermes-eval-completion",
            args=(record, prompt, params.get("system"), None if model in (None, "default") else model),
        )
        with _THREADS_LOCK:
            _COMPLETION_THREADS.add(thread)
        thread.start()
        return _ok(id=record["id"])
    except Exception as exc:  # health: allow BLE001 -- host ops report failures through the result object
        return _error(exc)


def _run_completion_worker(record: dict[str, Any], prompt: str, system: Any, model: Any) -> None:
    try:
        from agent.auxiliary_client import call_llm
        messages: list[dict[str, Any]] = []
        if isinstance(system, str) and system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        kwargs: dict[str, Any] = {"messages": messages, "task": "eval"}
        if isinstance(model, str) and model:
            kwargs["model"] = model
        response = call_llm(**kwargs)
        text = ""
        usage = None
        try:
            choice = response.choices[0]
            text = getattr(getattr(choice, "message", None), "content", None) or ""
            raw_usage = getattr(response, "usage", None)
            if raw_usage is not None:
                usage = {
                    key: value for key, value in (
                        ("prompt_tokens", getattr(raw_usage, "prompt_tokens", None)),
                        ("completion_tokens", getattr(raw_usage, "completion_tokens", None)),
                        ("total_tokens", getattr(raw_usage, "total_tokens", None)),
                    ) if value is not None
                }
        except Exception:  # health: allow BLE001 -- non-OpenAI shaped response
            text = str(response)
        with record["lock"]:
            if record["cancelled"]:
                return
            record["text"], record["usage"], record["status"] = text, usage, "completed"
    except Exception as exc:  # health: allow BLE001 -- reported through the handle
        with record["lock"]:
            if not record["cancelled"]:
                record["status"], record["error"] = "failed", f"{type(exc).__name__}: {exc}"
    finally:
        record["event"].set()
        with _THREADS_LOCK:
            _COMPLETION_THREADS.discard(threading.current_thread())


# ---- workpool -------------------------------------------------------------


def _workpool_op(args: Any) -> str:
    try:
        params = args if isinstance(args, dict) else {}
        op = str(params.get("op") or "")
        state = _state(_CELL_TASK.get())
        if op == "create":
            name = str(params.get("name") or _handle_id("pool"))
            with state.lock:
                if name in state.workpools:
                    return _error(ValueError(f"workpool {name!r} already exists"))
                pool = {
                    "name": name, "agent": params.get("agent"), "context": params.get("context"),
                    "tools": params.get("tools"), "limit": int(params.get("limit") or 4),
                    "ids": [], "queue": [], "workers": 0,
                }
                state.workpools[name] = pool
            return _ok(name=name, agent=pool["agent"], limit=pool["limit"])
        pool = state.workpools.get(str(params.get("name") or ""))
        if pool is None:
            return _error(ValueError(f"unknown workpool {params.get('name')!r}"))
        if op == "push":
            items = params.get("items")
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                return _error(ValueError("workpool push requires a list of string items"))
            with state.lock:
                pool["queue"].extend(items)
                _pump_workpool(state, pool)
                ids = list(pool["ids"])
            return _ok(ids=ids)
        if op == "status":
            with state.lock:
                return _ok(name=pool["name"], ids=list(pool["ids"]),
                           queued=len(pool["queue"]), workers=pool["workers"])
        if op == "peek":
            with state.lock:
                ids = list(pool["ids"])
            snapshots = []
            for handle_id in ids:
                record = _get_handle(state, handle_id)
                if record is not None:
                    snapshots.append(_agent_snapshot(record))
            return _ok(items=snapshots)
        if op == "close":
            with state.lock:
                ids = list(pool["ids"])
                state.workpools.pop(pool["name"], None)
            for handle_id in ids:
                _cancel_handle(handle_id, state)
            return _ok(closed=True)
        return _error(ValueError(f"unknown workpool op {op!r}"))
    except Exception as exc:  # health: allow BLE001 -- host ops report failures through the result object
        return _error(exc)


def _pump_workpool(state: _SessionState, pool: dict[str, Any]) -> None:
    """Start workers up to the pool limit (caller holds ``state.lock``)."""
    while pool["queue"] and pool["workers"] < max(1, pool["limit"]):
        item = pool["queue"].pop(0)
        pool["workers"] += 1
        thread = spawn_context_thread(
            _workpool_worker, name=f"hermes-eval-pool-{pool['name']}",
            args=(state, pool, item),
        )
        thread.start()


def _workpool_worker(state: _SessionState, pool: dict[str, Any], item: str) -> None:
    handle_id = ""
    try:
        params: dict[str, Any] = {"prompt": item}
        if pool.get("agent"):
            params["agent"] = pool["agent"]
        if pool.get("context"):
            params["context"] = pool["context"]
        if pool.get("tools"):
            params["tools"] = pool["tools"]
        payload = _json_object(_agent_op(params))
        handle_id = str(payload.get("id") or "")
        if handle_id:
            with state.lock:
                pool["ids"].append(handle_id)
    except Exception:  # health: allow BLE001 -- one item's failure must not wedge the pool
        logger.debug("eval workpool worker failed", exc_info=True)
    finally:
        with state.lock:
            pool["workers"] -= 1
            if pool.get("name") in state.workpools:
                _pump_workpool(state, pool)


# ---- dispatch -------------------------------------------------------------

_OPS: dict[str, Callable[[Any], str]] = {
    "__display__": _display_op,
    "__status__": _status_op,
    "__wait__": _wait_op,
    "__cancel__": _cancel_op,
    "__send__": _send_op,
    "__agent__": _agent_op,
    "__workpool__": _workpool_op,
    "__completion__": _completion_op,
    "__out__": _out_op,
}


def host_op(name: str) -> Optional[Callable[[Any], str]]:
    """The eval host op named ``name``, or ``None`` when it belongs to another seam."""
    if name not in HOST_OP_NAMES:
        return None
    return _OPS[name]


def dispatch_host_op(name: str, args: Any, *, task_id: str = "") -> str:
    """Run one host op with the cell task identity bound; unknown names never dispatch."""
    op = host_op(name)
    if op is None:
        from agent.judgment.rpc import host_op as judgment_op
        judgment = judgment_op(name)
        if judgment is None:
            return _error(ValueError(f"Unknown host op {name!r}"))
        return judgment(args)
    task_token = _CELL_TASK.set(task_id or _CELL_TASK.get())
    try:
        return op(args)
    finally:
        _CELL_TASK.reset(task_token)


atexit.register(clear_all_session_state)
