"""Session-level prewalk handoff (ADR-12 D5 / ADR-17 P5).

Prewalk lets the strong planning model investigate and commit a todo plan, then hands the
mechanical part of the task to a cheaper model at the first successful ``write_file``/``patch``
batch. The handoff is session state, not a prompt instruction: it is armed explicitly or by the
auto classifier, fires once, persists in the session's ``model_config`` and never switches in the
middle of a tool batch.

Everything here fails soft. A consumer of this module (turn loop, gateway RPC, auto task start)
gets a structured status or a debug log, never a raised exception in the middle of a turn.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

PREWALK_KEY = "prewalk"
DEFAULT_FROM = "@default"
DEFAULT_INTO = "@smol"
AUTO_MODES = ("off", "suggest", "on")
AUTO_PREWALK_THRESHOLD = 0.70
AUTO_PREWALK_TIMEOUT_MS = 4_000
AUTO_PREWALK_PURPOSE = "auto-prewalk"
# The Todo tool's current name plus its legacy alias (``tools/todo_tool.py``).
TODO_TOOL_NAMES = frozenset({"todo_list", "todo"})
# Tools whose successful result is the handoff boundary (omp's edit/write).
EDIT_TOOL_NAMES = frozenset({"write_file", "patch"})
_STATE_KEYS = (
    "armed", "from", "into", "consumed", "auto", "auto_target",
    "planning_model", "planning_thinking",
    # Runtime needed to return to the planning model on the next task / restart.
    "planning_provider", "planning_base_url", "planning_api_mode",
)
_FAILURE_STATUSES = frozenset({"error", "failed", "failure"})


def _empty_state() -> dict[str, Any]:
    return {
        "armed": False, "from": "", "into": "", "consumed": False, "auto": False,
        "auto_target": "", "planning_model": "", "planning_thinking": None,
        "planning_provider": "", "planning_base_url": "", "planning_api_mode": "",
    }


def settings() -> dict[str, Any]:
    """The ``prewalk`` config block with defaults merged in (default: off)."""
    merged: dict[str, Any] = {
        "enabled": False, "from": DEFAULT_FROM, "into": DEFAULT_INTO,
        "auto": "off", "auto_target": DEFAULT_INTO,
    }
    try:
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        block = DEFAULT_CONFIG.get(PREWALK_KEY)
        if isinstance(block, Mapping):
            merged.update({k: v for k, v in block.items() if v is not None})
    except Exception:  # health: allow BLE001 -- defaults are static; a broken import must not fail
        logger.debug("prewalk: default config unavailable", exc_info=True)
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        block = cfg.get(PREWALK_KEY) if isinstance(cfg, Mapping) else None
        if isinstance(block, Mapping):
            merged.update({k: v for k, v in block.items() if v is not None})
    except Exception:  # health: allow BLE001 -- an unreadable config means the defaults
        logger.debug("prewalk: config lookup failed", exc_info=True)
    auto = str(merged.get("auto") or "off").strip().lower()
    merged["auto"] = auto if auto in AUTO_MODES else "off"
    merged["enabled"] = bool(merged.get("enabled"))
    for key in ("from", "into", "auto_target"):
        merged[key] = str(merged.get(key) or "").strip()
    return merged


# ── state + persistence ────────────────────────────────────────────────────────────────────


def _session_db(agent: Any) -> Any:
    return getattr(agent, "_session_db", None) or getattr(agent, "session_db", None)


def _jsonable(value: Any) -> Any:
    """Best-effort JSON value for ``planning_thinking``; unknown objects are stringified."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _load_state(agent: Any) -> dict[str, Any]:
    """The live state, lazily hydrated from the session row on first use."""
    cached = getattr(agent, "_prewalk_state", None)
    if isinstance(cached, dict):
        return cached
    state = _empty_state()
    raw: Any = None
    db = _session_db(agent)
    sid = str(getattr(agent, "session_id", "") or "")
    if db is not None and sid:
        getter = getattr(db, "get_session_model_config_value", None)
        if callable(getter):
            try:
                raw = getter(sid, PREWALK_KEY, None)
            except Exception:  # health: allow BLE001 -- a cold/legacy row means "unarmed"
                logger.debug("prewalk: state read failed for %s", sid, exc_info=True)
    if isinstance(raw, Mapping):
        for key in _STATE_KEYS:
            if key in raw:
                state[key] = raw[key]
    # A persisted row (even one that only records "disarmed") means the session already
    # made its prewalk decision; only a first hydration may apply ``prewalk.enabled``.
    try:
        agent._prewalk_state_from_row = isinstance(raw, Mapping)
    except Exception:  # health: allow BLE001 -- a slotted agent keeps the state only per call
        logger.debug("prewalk: could not record row provenance", exc_info=True)
    state["armed"] = bool(state.get("armed"))
    state["consumed"] = bool(state.get("consumed"))
    # A consumed arm is spent: resume/revive must not hand off a second time.
    if state["consumed"]:
        state["armed"] = False
    try:
        agent._prewalk_state = state
    except Exception:  # health: allow BLE001 -- a slotted agent keeps the state only per call
        logger.debug("prewalk: could not cache state on agent", exc_info=True)
    return state


def _persist(agent: Any, state: dict[str, Any] | None = None) -> None:
    state = state if isinstance(state, dict) else getattr(agent, "_prewalk_state", None)
    if not isinstance(state, dict):
        return
    db = _session_db(agent)
    sid = str(getattr(agent, "session_id", "") or "")
    if db is None or not sid:
        return
    patcher = getattr(db, "patch_session_model_config", None)
    if not callable(patcher):
        return
    try:
        patcher(sid, {PREWALK_KEY: {k: _jsonable(v) for k, v in state.items()}})
        agent._prewalk_state_from_row = True
    except Exception:  # health: allow BLE001 -- prewalk state must never break a turn
        logger.debug("prewalk: state persist failed for %s", sid, exc_info=True)


def _publish(agent: Any, event: str, payload: dict[str, Any]) -> None:
    for attr in ("prewalk_publish", "_prewalk_publish"):
        callback = getattr(agent, attr, None)
        if callable(callback):
            try:
                callback(event, payload)
            except Exception:  # health: allow BLE001 -- a viewer must not fail the turn
                logger.debug("prewalk: %s publish failed", event, exc_info=True)
            return


# ── model resolution + switching ──────────────────────────────────────────────────────────


def _resolve_target(selector: Any) -> dict[str, Any] | None:
    """Resolve one selector through ``resolve_model_selector``; ``None`` when it names no model.

    A role that inherits the main route (``@default`` or an auto auxiliary slot with no model)
    has no handoff destination: switching to it would be a no-op.
    """
    text = str(selector or "").strip()
    if not text:
        return None
    from hermes_cli.model_roles import resolve_model_selector

    route = resolve_model_selector(text)
    model = str(getattr(route, "model", "") or "").strip()
    if not model:
        return None
    return {
        "selector": text,
        "provider": str(getattr(route, "provider", "") or "").strip(),
        "model": model,
        "base_url": str(getattr(route, "base_url", "") or "").strip(),
        "api_key": str(getattr(route, "api_key", "") or ""),
        "reasoning_effort": getattr(route, "reasoning_effort", ""),
        "role": str(getattr(route, "role", "") or ""),
    }


def _resolve_runtime(provider: str, api_key: str, base_url: str, model: str) -> dict[str, Any]:
    """Credentials/endpoint for a cross-provider handoff (the same ladder ``/model`` uses)."""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        return resolve_runtime_provider(
            requested=provider,
            explicit_api_key=api_key or None,
            explicit_base_url=base_url or None,
            target_model=model,
        ) or {}
    except Exception:  # health: allow BLE001 -- the route keeps its own values
        logger.debug("prewalk: runtime resolution failed for %s", provider, exc_info=True)
        return {}


def _apply_effort(agent: Any, effort: Any) -> None:
    if effort in (None, ""):
        return
    try:
        from hermes_constants import parse_reasoning_effort

        parsed = parse_reasoning_effort(effort)
    except Exception:  # health: allow BLE001 -- an unparsable effort keeps the current one
        logger.debug("prewalk: could not parse reasoning effort %r", effort, exc_info=True)
        return
    if parsed is not None:
        agent.reasoning_config = parsed


def _switch_to_route(agent: Any, route: Mapping[str, Any]) -> None:
    switch = getattr(agent, "switch_model", None)
    if not callable(switch):
        raise RuntimeError("this agent has no switch_model; prewalk cannot hand off")
    model = str(route.get("model") or "")
    current_provider = str(getattr(agent, "provider", "") or "")
    provider = str(route.get("provider") or current_provider or "")
    base_url = str(route.get("base_url") or "")
    api_key = str(route.get("api_key") or "")
    api_mode = str(route.get("api_mode") or "")
    if provider and provider.lower() != current_provider.lower():
        runtime = _resolve_runtime(provider, api_key, base_url, model)
        base_url = base_url or str(runtime.get("base_url") or "")
        api_key = api_key or str(runtime.get("api_key") or "")
        api_mode = api_mode or str(runtime.get("api_mode") or "")
    else:
        # Same provider: keep the live endpoint/credential (a bare model name). The API mode is
        # left empty so the switch re-resolves it from the destination model (dual-wire providers).
        base_url = base_url or str(getattr(agent, "base_url", "") or "")
        api_key = api_key or str(getattr(agent, "api_key", "") or "")
    switch(
        new_model=model,
        new_provider=provider,
        api_key=api_key,
        base_url=base_url,
        api_mode=api_mode,
        capabilities=None,
    )
    _apply_effort(agent, route.get("reasoning_effort"))


def _switch_to_model(agent: Any, model: str, *, provider: str = "", base_url: str = "",
                     api_mode: str = "") -> None:
    _switch_to_route(agent, {
        "model": model, "provider": provider or getattr(agent, "provider", ""),
        "base_url": base_url, "api_key": "", "api_mode": api_mode,
    })


def _snapshot_planning(agent: Any, state: dict[str, Any]) -> None:
    state["planning_model"] = str(getattr(agent, "model", "") or "")
    state["planning_provider"] = str(getattr(agent, "provider", "") or "")
    state["planning_base_url"] = str(getattr(agent, "base_url", "") or "")
    state["planning_api_mode"] = str(getattr(agent, "api_mode", "") or "")
    state["planning_thinking"] = _jsonable(getattr(agent, "reasoning_config", None))


def _restore_planning(agent: Any, state: Mapping[str, Any]) -> bool:
    """Return to the model/thinking the current task was planned on (no-op when already there)."""
    model = str(state.get("planning_model") or "")
    if not model:
        return False
    current = str(getattr(agent, "model", "") or "")
    provider = str(state.get("planning_provider") or "")
    current_provider = str(getattr(agent, "provider", "") or "")
    if model != current or (provider and provider.lower() != current_provider.lower()):
        _switch_to_model(
            agent, model, provider=provider,
            base_url=str(state.get("planning_base_url") or ""),
            api_mode=str(state.get("planning_api_mode") or ""),
        )
    thinking = state.get("planning_thinking")
    if thinking is not None:
        agent.reasoning_config = copy.deepcopy(thinking)
    return True


# ── public state machine ─────────────────────────────────────────────────────────────────


def todo_available(agent: Any) -> bool:
    """Whether the Todo tool is on this agent's tool surface (unknown means "no gate")."""
    names = getattr(agent, "valid_tool_names", None)
    if names is None:
        names = getattr(agent, "tool_names", None)
    if not names:
        return False
    try:
        available = {str(name) for name in names}
    except TypeError:
        return False
    return bool(available & TODO_TOOL_NAMES)


def _gate_open(agent: Any) -> bool:
    if not todo_available(agent):
        return True
    return bool(getattr(agent, "_prewalk_todo_gate", False))


def _reset_gate(agent: Any) -> None:
    try:
        agent._prewalk_todo_gate = False
    except Exception:  # health: allow BLE001 -- gate is advisory state
        logger.debug("prewalk: could not reset the todo gate", exc_info=True)


_LOCAL_SURFACES = frozenset({"cli", "tui", "desktop", "omp-ui", "local"})


def _local_interactive(agent: Any) -> bool:
    """Prewalk is a local, user-driven handoff: never a messaging platform or a headless run."""
    platform = str(getattr(agent, "platform", "cli") or "cli").strip().lower()
    return platform in _LOCAL_SURFACES and not getattr(agent, "quiet_mode", False)


def status(agent: Any) -> dict[str, Any]:
    """The one status shape the gateway, CLI and tests read."""
    cfg = settings()
    try:
        state = _load_state(agent)
    except Exception:  # health: allow BLE001 -- status must answer even on a broken row
        logger.debug("prewalk: status load failed", exc_info=True)
        state = _empty_state()
    return {
        "enabled": bool(cfg["enabled"]),
        "armed": bool(state.get("armed")),
        "from": str(state.get("from") or cfg["from"]),
        "into": str(state.get("into") or cfg["into"]),
        "consumed": bool(state.get("consumed")),
        "auto": bool(state.get("auto")),
        "auto_mode": cfg["auto"],
        "auto_target": str(state.get("auto_target") or cfg["auto_target"]),
        "planning_model": str(state.get("planning_model") or ""),
        "planning_thinking": state.get("planning_thinking"),
        "gate": "open" if _gate_open(agent) else "closed",
    }


def _armed_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    return {k: result[k] for k in ("enabled", "armed", "from", "into", "auto", "auto_target")}


def arm(agent: Any, *, into: Any = None, from_: Any = None, auto: bool = False,
        auto_target: Any = None) -> dict[str, Any]:
    """Arm one handoff. Explicit arming works even when ``prewalk.enabled`` is false."""
    cfg = settings()
    into_sel = str(into or cfg["into"] or DEFAULT_INTO).strip()
    from_sel = str(from_ or cfg["from"] or DEFAULT_FROM).strip()
    # Validate both selectors up front: a typo must fail here, not at the edit boundary.
    if _resolve_target(into_sel) is None:
        raise ValueError(f"prewalk target {into_sel!r} does not resolve to a model")
    from hermes_cli.model_roles import resolve_model_selector

    resolve_model_selector(from_sel)  # raises on an unknown role; @default is the inherit marker
    state = _load_state(agent)
    state.update({
        "armed": True, "from": from_sel, "into": into_sel, "consumed": False,
        "auto": bool(auto),
        "auto_target": str(auto_target or cfg["auto_target"] or ""),
    })
    agent._prewalk_auto_suppressed = False
    _snapshot_planning(agent, state)
    _reset_gate(agent)
    _persist(agent, state)
    result = status(agent)
    _publish(agent, "prewalk.armed", _armed_payload(result))
    return result


def disarm(agent: Any) -> dict[str, Any]:
    """Turn the one-shot handoff off without changing the consumed/planning history."""
    state = _load_state(agent)
    state["armed"] = False
    # An explicit /prewalk off outlives the turn: neither ``prewalk.enabled`` nor
    # ``prewalk.auto`` may re-arm this live session. /prewalk or /prewalk restart clears it.
    agent._prewalk_auto_suppressed = True
    _reset_gate(agent)
    _persist(agent, state)
    result = status(agent)
    _publish(agent, "prewalk.armed", _armed_payload(result))
    return result


def restart(agent: Any, *, into: Any = None, from_: Any = None) -> dict[str, Any]:
    """Return to the planning model/thinking, then arm a fresh handoff."""
    state = _load_state(agent)
    cfg = settings()
    target = str(into or state.get("into") or cfg["into"] or DEFAULT_INTO).strip()
    from_sel = str(from_ or state.get("from") or cfg["from"] or DEFAULT_FROM).strip()
    if _resolve_target(target) is None:
        raise ValueError(f"prewalk target {target!r} does not resolve to a model")
    try:
        _restore_planning(agent, state)
    except Exception:  # health: allow BLE001 -- a failed restore must not kill /prewalk restart
        logger.debug("prewalk: planning restore failed during restart", exc_info=True)
    return arm(agent, into=target, from_=from_sel)


# ── auto classifier ──────────────────────────────────────────────────────────────────────


def _history_context(history: Sequence[Mapping[str, Any]] | None) -> dict[str, str]:
    """Prior request/reply, only to resolve a referential new request."""
    previous_request = previous_response = ""
    for message in reversed(list(history or ())):
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role == "assistant" and not previous_response:
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                previous_response = content.strip()[:2_000]
        elif role == "user" and not previous_request:
            if message.get("flag") or message.get("display_kind"):
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                previous_request = content.strip()[:2_000]
        if previous_request and previous_response:
            break
    return {"previous_request": previous_request, "previous_response": previous_response}


def classify_auto(request: Any, history: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Classify a new task as ``prewalk``/``primary``/``direct``; failures are data, not raises."""
    outcome: dict[str, Any] = {"route": "", "probability": None, "reason": ""}
    text = str(request or "").strip()
    if not text:
        outcome["reason"] = "empty_request"
        return outcome
    try:
        from agent.judgment import (
            JudgmentError, JudgmentRequest, has_native_judge, judge, load_questions,
        )

        if not has_native_judge():
            outcome["reason"] = "no_native_judge"
            return outcome
        question = load_questions(AUTO_PREWALK_PURPOSE)["route"]
        state: dict[str, Any] = {"request": text[:4_000]}
        state.update({k: v for k, v in _history_context(history).items() if v})
        result = judge(
            JudgmentRequest(state=state, questions={"route": question}),
            purpose=AUTO_PREWALK_PURPOSE,
            timeout_ms=AUTO_PREWALK_TIMEOUT_MS,
        )
    except JudgmentError as exc:
        logger.debug("prewalk: auto classification failed: %s", exc)
        outcome["reason"] = "judge_failed"
        return outcome
    except Exception as exc:  # health: allow BLE001 -- a judged route must not fail the turn
        logger.debug("prewalk: auto classification failed: %s", exc)
        outcome["reason"] = "judge_failed"
        return outcome
    answer = result.answers.get("route")
    choice = str(getattr(answer, "choice", "") or "")
    probabilities = getattr(answer, "probabilities", None)
    probability: float | None = None
    if isinstance(probabilities, Mapping):
        raw = probabilities.get("prewalk")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            probability = float(raw)
    if probability is None and choice == "prewalk":
        probability = 1.0
    outcome.update({"route": choice, "probability": probability})
    return outcome


def begin_auto_task(agent: Any, request: Any,
                    history: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Start-of-task hook: restore planning, classify, and arm only on a confident ``prewalk``."""
    cfg = settings()
    mode = cfg["auto"]
    outcome: dict[str, Any] = {
        "mode": mode, "route": "", "probability": None, "armed": False, "reason": "",
    }
    if not _local_interactive(agent):
        outcome["reason"] = "non_local_surface"
        return outcome
    try:
        state = _load_state(agent)
        # ``prewalk.enabled`` arms a fresh session once, on first hydration; a persisted
        # row (an explicit /prewalk off, or a spent one-shot) never re-arms.
        if (cfg["enabled"] and not state.get("armed") and not state.get("consumed")
                and not getattr(agent, "_prewalk_state_from_row", False)):
            try:
                result = arm(agent, into=cfg["into"], from_=cfg["from"], auto=False)
                outcome.update({"armed": bool(result.get("armed")), "reason": "config_arm",
                                "target": result.get("into")})
            except Exception:  # health: allow BLE001 -- a bad configured target must not fail a turn
                logger.debug("prewalk: config arm failed", exc_info=True)
                outcome["reason"] = "config_arm_failed"
        if mode not in ("suggest", "on"):
            outcome["reason"] = outcome.get("reason") or "auto_off"
            return outcome
        if getattr(agent, "_prewalk_auto_suppressed", False) or outcome.get("reason") == "config_arm":
            outcome["reason"] = outcome.get("reason") or "suppressed"
            return outcome
        # A new task re-evaluates this task on its own; an unconsumed auto arm does not carry over.
        if state.get("auto") and state.get("armed"):
            state["armed"] = False
            _persist(agent, state)
        if state.get("consumed") and state.get("planning_model"):
            try:
                _restore_planning(agent, state)
            except Exception:  # health: allow BLE001 -- classification can still run
                logger.debug("prewalk: auto restore failed", exc_info=True)
        classification = classify_auto(request, history)
        outcome.update(classification)
        if mode != "on":
            outcome["reason"] = outcome.get("reason") or "suggest_only"
            return outcome
        probability = classification.get("probability")
        if classification.get("route") != "prewalk" or probability is None \
                or probability < AUTO_PREWALK_THRESHOLD:
            outcome["reason"] = outcome.get("reason") or "below_threshold"
            return outcome
        if state.get("armed") and not state.get("auto"):
            # The user's explicit arm is already pending; auto must not retarget it.
            outcome["reason"] = "explicit_arm"
            return outcome
        target = str(cfg.get("auto_target") or state.get("auto_target") or DEFAULT_INTO).strip()
        if _resolve_target(target) is None:
            outcome["reason"] = "target_unresolved"
            return outcome
        armed = arm(
            agent, into=target, from_=state.get("from") or cfg["from"],
            auto=True, auto_target=target,
        )
        outcome["armed"] = bool(armed.get("armed"))
        outcome["target"] = target
        return outcome
    except Exception as exc:  # health: allow BLE001 -- the classifier must not fail a turn
        logger.debug("prewalk: auto task start failed: %s", exc)
        outcome["reason"] = "error"
        return outcome


# ── tool-batch hook ──────────────────────────────────────────────────────────────────────


def _call_field(tool_call: Any, name: str, default: Any = "") -> Any:
    if isinstance(tool_call, Mapping):
        value = tool_call.get(name)
        function = tool_call.get("function")
    else:
        value = getattr(tool_call, name, None)
        function = getattr(tool_call, "function", None)
    if value is not None:
        return value
    if name == "name" and function is not None:
        if isinstance(function, Mapping):
            return function.get("name", default)
        return getattr(function, "name", default)
    return default


def collect_tool_results(messages: Sequence[Mapping[str, Any]], tool_calls: Any) -> list[Mapping[str, Any]]:
    """The result rows belonging to one assistant message's calls (post-batch rows included)."""
    call_ids: set[str] = set()
    for tool_call in tool_calls or ():
        call_id = _call_field(tool_call, "id", "")
        if call_id:
            call_ids.add(str(call_id))
    if not call_ids:
        return []
    found: dict[str, Mapping[str, Any]] = {}
    for message in reversed(list(messages or ())):
        if not isinstance(message, Mapping) or message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        if call_id in call_ids:
            found.setdefault(call_id, message)
            if len(found) == len(call_ids):
                break
    return [found[call_id] for call_id in call_ids if call_id in found]


def _succeeded(content: Any) -> bool:
    """A tool result is a failure only when it says so; plain text results are successes."""
    if content is None:
        return True
    if isinstance(content, Mapping):
        data: Any = content
    elif isinstance(content, str):
        text = content.strip()
        if not text:
            return True
        if text.startswith("[Tool execution cancelled") or text.startswith("[Tool execution skipped"):
            return False
        if not text.startswith("{"):
            return True
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return False
    else:
        return True
    if not isinstance(data, Mapping):
        return True
    if data.get("success") is False:
        return False
    if data.get("error"):
        return False
    status = str(data.get("status") or "").strip().lower()
    return status not in _FAILURE_STATUSES


def _result_name(result: Mapping[str, Any], id_to_name: Mapping[str, str]) -> str:
    name = result.get("tool_name") or result.get("name") or ""
    if not name:
        name = id_to_name.get(str(result.get("tool_call_id") or ""), "")
    return str(name)


def _todo_succeeded(result: Mapping[str, Any]) -> bool:
    return _succeeded(result.get("content")) if "content" in result else _succeeded(result)


def advance_after_tool_batch(agent: Any, assistant_message: Any,
                             tool_results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One-shot handoff at the first successful edit after the todo gate opened.

    Called once per completed tool batch, never between calls. A failure is logged and
    returned as data; it never aborts the turn.
    """
    outcome: dict[str, Any] = {"handoff": None, "reason": ""}
    try:
        state = _load_state(agent)
        if not state.get("armed") or state.get("consumed"):
            outcome["reason"] = "not_armed"
            return outcome
        if getattr(agent, "_interrupt_requested", False) or getattr(agent, "is_interrupted", False):
            outcome["reason"] = "interrupted"
            return outcome
        raw_calls = (assistant_message.get("tool_calls")
                     if isinstance(assistant_message, Mapping)
                     else getattr(assistant_message, "tool_calls", None))
        tool_calls = list(raw_calls or ())
        id_to_name = {
            str(_call_field(call, "id", "")): str(_call_field(call, "name", ""))
            for call in tool_calls
        }
        results = [r for r in (tool_results or ()) if isinstance(r, Mapping)]
        # A successful Todo call (including the read-only view) opens the gate.
        for result in results:
            if _result_name(result, id_to_name) in TODO_TOOL_NAMES and _todo_succeeded(result):
                try:
                    agent._prewalk_todo_gate = True
                except Exception:  # health: allow BLE001 -- gate is advisory state
                    logger.debug("prewalk: could not open the todo gate", exc_info=True)
                break
        if not _gate_open(agent):
            outcome["reason"] = "todo_gate"
            return outcome
        edit = next(
            (result for result in results
             if _result_name(result, id_to_name) in EDIT_TOOL_NAMES
             and _succeeded(result.get("content"))),
            None,
        )
        if edit is None:
            outcome["reason"] = "no_edit"
            return outcome
        route = _resolve_target(state.get("into"))
        if route is None:
            outcome["reason"] = "target_unresolved"
            logger.debug("prewalk: target %r no longer resolves; staying armed", state.get("into"))
            return outcome
        from_model = str(getattr(agent, "model", "") or "")
        try:
            _switch_to_route(agent, route)
        except Exception as exc:  # health: allow BLE001 -- a failed handoff is not a turn failure
            logger.debug("prewalk: handoff switch failed: %s", exc)
            outcome["reason"] = "switch_failed"
            return outcome
        state["consumed"] = True
        state["armed"] = False
        _reset_gate(agent)
        _persist(agent, state)
        payload = {
            "from_model": from_model,
            "into_model": str(route.get("model") or ""),
            "provider": str(route.get("provider") or getattr(agent, "provider", "") or ""),
            "consumed": True,
        }
        _publish(agent, "prewalk.handoff", payload)
        outcome.update({"handoff": payload, "reason": "handoff"})
        return outcome
    except Exception as exc:  # health: allow BLE001 -- prewalk must never break a turn
        logger.debug("prewalk: tool-batch advance failed: %s", exc)
        outcome["reason"] = "error"
        return outcome


__all__ = [
    "AUTO_MODES",
    "AUTO_PREWALK_THRESHOLD",
    "AUTO_PREWALK_TIMEOUT_MS",
    "PREWALK_KEY",
    "advance_after_tool_batch",
    "arm",
    "begin_auto_task",
    "classify_auto",
    "collect_tool_results",
    "disarm",
    "restart",
    "settings",
    "status",
    "todo_available",
]
