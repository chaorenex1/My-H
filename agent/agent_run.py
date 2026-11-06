"""One-shot in-process agent runs: a peer ``AIAgent`` conversation, closed before returning.

The parameter surface is the contract plugins see through ``ctx.agent_loop`` (and the model sees
through the ``agent`` tool): only what a caller can know — the user message, an optional
context block, an optional system message, and the overrides it owns (model, provider, api_mode,
toolsets, iteration budget, history). Everything else is resolved here the way any host surface
resolves a run:

* model          — ``model.default``/``model.model`` from config, else the provider's default;
* credentials    — :func:`hermes_cli.runtime_provider.resolve_runtime_provider` for the requested
                   route (the main route when no provider is pinned), never a borrowed key;
* reasoning      — ``resolve_reasoning_config(config, model)`` for the *final* model;
* the agent      — one ``AIAgent`` per run, ``platform="subagent"`` (a peer is unattended and
                   hidden from human session pickers), closed in ``finally`` unless the caller
                   asked for ``keep_alive`` (a named agent session that outlives its run).

Every run also appends to its native history (:mod:`agent.run_transcript`)::

    <HERMES_HOME>/sessions/<agent>/<session>.jsonl

where ``<agent>`` is ``layer`` (or ``meta["agent"]``) and ``<session>`` is ``history_session``
(the conversation that dispatched the run), falling back to the run's own ``session_id``.
``record_history=False`` opts out; the caller then owns persistence.

The defaults are a peer's posture: no host memory, no project context files, no background review
fork and no operator soul identity. A custom-agent caller flips each one explicitly.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from . import run_transcript

logger = logging.getLogger(__name__)

#: Tool-call budget for one peer run.
MAX_ITERATIONS = 10

#: Runtime keys forwarded from the resolved route to ``AIAgent`` — the same set the gateway
#: forwards from ``_resolve_runtime_agent_kwargs()``, minus ``model`` (resolved separately).
_RUNTIME_KEYS = ("api_key", "base_url", "api_mode", "credential_pool", "request_overrides",
                 "requested_provider", "capabilities", "command", "args")

#: Per-turn counters copied from the host result; names match the turn finalizer.
_USAGE_KEYS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
    "estimated_cost_usd", "cost_status", "cost_source",
)

#: Tools a read-only peer must not hold: it reads and advises, it does not mutate.
READ_ONLY_DROPPED_TOOLS = frozenset({
    "write_file", "patch", "agent", "skill_manage",
})

#: Two runs can land in the same millisecond for the same agent; run ids must still differ.
_SEQUENCE = itertools.count(1)


@dataclass(frozen=True)
class AgentRunResult:
    """One run's answer, how it was produced, and where its history landed."""

    answer: str
    api_calls: int
    model: str
    provider: str
    toolsets: tuple[str, ...]
    #: The one identifier ``agent_out`` and the transcript path are keyed on.
    agent_session_id: str = ""
    #: The conversation loop's messages, tool calls included. Empty when the host returned none.
    messages: tuple[dict[str, Any], ...] = ()
    #: Token/cost counters the host attached to the turn result; empty when none were reported.
    usage: dict[str, Any] = field(default_factory=dict)
    transcript_path: str = ""
    transcript_written: bool = False
    #: Worktree isolation outcome: the inspected payload when it engaged, a ``requested``/
    #: ``created: false`` note when it was asked for and could not be set up. Empty when off.
    worktree: dict[str, Any] = field(default_factory=dict)
    #: The ``@role`` the model selector resolved through; empty when the route was literal.
    model_role: str = ""
    #: ``output`` contract outcome: ``None`` when the definition carried no schema.
    output_schema_valid: Optional[bool] = None
    output_schema_errors: tuple[str, ...] = ()
    #: Structural summaries collected during this run (ADR-16 D4). Carried here because
    #: ``run()`` clears the run-scoped buffer in ``finally`` before any settle callback runs.
    summary_sources: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """The tool/plugin JSON face: ``messages`` stay out (they are history, not an answer)."""
        data = {"answer": self.answer, "agent_session_id": self.agent_session_id,
                "api_calls": self.api_calls, "model": self.model,
                "provider": self.provider, "toolsets": list(self.toolsets),
                "usage": dict(self.usage), "transcript_path": self.transcript_path,
                "transcript_written": self.transcript_written}
        if self.model_role:
            data["model_role"] = self.model_role
        if self.output_schema_valid is not None:
            data["output_schema_valid"] = bool(self.output_schema_valid)
            if self.output_schema_errors:
                data["output_schema_errors"] = list(self.output_schema_errors)
            from tools.delegation_output_schema import validation_available

            if not validation_available():
                data["validated"] = False
        if self.worktree:
            data["worktree"] = dict(self.worktree)
        return data


def _config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly() or {}
    except Exception:  # health: allow BLE001 -- an unreadable config must not break a run
        logger.debug("agent_run: config unreadable", exc_info=True)
        return {}


def _resolve_route(model: str, provider: str, base_url: str = "",
                   api_key: str = "") -> tuple[str, dict[str, Any]]:
    """``(model, runtime)`` for the final route; a pinned provider resolves its own credentials."""
    from hermes_cli.runtime_provider import resolve_runtime_provider

    requested = str(provider or "").strip() or None
    chosen = str(model or "").strip()
    endpoint = str(base_url or "").strip() or None
    runtime = dict(resolve_runtime_provider(requested=requested, target_model=chosen or None,
                                            explicit_api_key=str(api_key or "").strip() or None,
                                            explicit_base_url=endpoint) or {})
    if endpoint and not runtime.get("base_url"):
        # A ``@role`` route's endpoint travels with its provider/model. Every ladder rung emits a
        # ``base_url`` key (possibly None/""), so a `setdefault` would never restore it.
        runtime["base_url"] = endpoint
    if not chosen:
        model_cfg = _config().get("model") or {}
        chosen = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
    if not chosen:
        chosen = str(runtime.get("model") or "").strip()
    if not chosen and runtime.get("provider"):
        from hermes_cli.models import get_default_model_for_provider

        chosen = get_default_model_for_provider(str(runtime["provider"])) or ""
    return chosen, runtime


def _agent_kwargs(runtime: Mapping[str, Any], model: str) -> dict[str, Any]:
    """Credential/provider kwargs for the final route, plus its reasoning config."""
    from hermes_constants import resolve_reasoning_config

    knobs = {key: runtime.get(key) for key in _RUNTIME_KEYS if key in runtime}
    knobs["reasoning_config"] = resolve_reasoning_config(_config(), model)
    return knobs


def _prompt(message: str, context: str = "") -> str:
    return message if not context else f"{message}\n\n--- context ---\n{context}"


def _system_segments(system_message: Any) -> list[str]:
    """Non-empty system segments; a legacy string is one segment."""
    if isinstance(system_message, str):
        raw = [system_message]
    elif isinstance(system_message, Sequence) and not isinstance(system_message, (bytes, bytearray)):
        raw = [str(part) for part in system_message]
    else:
        return []
    return [part.strip() for part in raw if str(part).strip()]


def _usage(result: Mapping[str, Any]) -> dict[str, Any]:
    return {key: result[key] for key in _USAGE_KEYS if result.get(key) is not None}


def _transcript(result: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """The host's messages as plain dicts; anything that is not a message shape is skipped."""
    rows: list[dict[str, Any]] = []
    for item in result.get("messages") or ():
        if isinstance(item, Mapping):
            rows.append(dict(item))
            continue
        to_dict = getattr(item, "to_dict", None)
        if callable(to_dict):
            converted = to_dict()
            if isinstance(converted, Mapping):
                rows.append(dict(converted))
    return tuple(rows)


def _with_reasoning(rows: tuple[dict[str, Any], ...],
                    reasoning: Any) -> tuple[dict[str, Any], ...]:
    """Attach a top-level ``reasoning_content`` to the run's last assistant message."""
    text = str(reasoning or "").strip()
    if not text:
        return rows
    for index in range(len(rows) - 1, -1, -1):
        row = dict(rows[index])
        if str(row.get("role") or "") != "assistant":
            continue
        if not (row.get("reasoning_content") or row.get("reasoning")):
            row["reasoning_content"] = text
        return rows[:index] + (row,) + rows[index + 1:]
    return rows + ({"role": "reasoning", "content": text},)


def apply_read_only(agent: Any) -> list[str]:
    """Strip mutating tools from an already-built agent; returns what was removed."""
    dropped: list[str] = []
    tools = getattr(agent, "tools", None)
    if isinstance(tools, list):
        kept: list[Any] = []
        for tool in tools:
            name = ""
            if isinstance(tool, Mapping):
                name = str((tool.get("function") or {}).get("name") or "")
            if name in READ_ONLY_DROPPED_TOOLS:
                dropped.append(name)
                continue
            kept.append(tool)
        agent.tools = kept
    names = getattr(agent, "valid_tool_names", None)
    if isinstance(names, (set, frozenset, list)):
        agent.valid_tool_names = {name for name in names if name not in READ_ONLY_DROPPED_TOOLS}
    return sorted(set(dropped))


def _configure_tool_guardrails(agent: Any, settings: Mapping[str, Any]) -> None:
    """Give one peer run its own loop guard; a host without the controller leaves the run alone."""
    try:
        from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController

        agent._tool_guardrails = ToolCallGuardrailController(
            ToolCallGuardrailConfig.from_mapping(dict(settings), platform="subagent"))
    except Exception:  # health: allow BLE001 -- the guard is an improvement, never a precondition
        logger.debug("agent_run: tool guardrails not applied", exc_info=True)


def _attach_worktree(project_dir: str, agent_name: str) -> Optional[dict[str, str]]:
    """Opt-in git worktree for one named run; ``None`` when isolation cannot engage.

    Reuses the delegation worktree helper: git-only, local-backend-only, and a failure degrades
    to the shared workspace instead of failing the run.
    """
    try:
        from tools import subagent_worktree

        if not subagent_worktree.local_backend_active():
            logger.debug("agent_run: worktree isolation skipped (non-local terminal backend)")
            return None
        return subagent_worktree.create_subagent_worktree(
            project_dir or os.getcwd(),
            subagent_id=f"agent-{agent_name}-{uuid.uuid4().hex[:8]}")
    except Exception:  # health: allow BLE001 -- isolation is opt-in, never a run precondition
        logger.warning("agent_run: worktree isolation setup failed", exc_info=True)
        return None


def _finalize_worktree(info: dict[str, str]) -> dict[str, Any]:
    """Inspect/prune/archive after the run; an inspection failure is reported, never raised."""
    from tools import subagent_worktree

    try:
        return subagent_worktree.finalize_subagent_worktree(info)
    except Exception as exc:  # health: allow BLE001 -- the answer stands; the tree is kept
        logger.warning("agent_run: worktree finalize failed: %s", exc, exc_info=True)
        return subagent_worktree.unproven_worktree_payload(info, f"finalize raised: {exc}")


def _seed_task_cwd(task_id: str, cwd: str) -> None:
    """Point this run's tool calls at *cwd*: terminal/file tools read the task's cwd record."""
    try:
        from tools.terminal_tool import record_session_cwd

        record_session_cwd(task_id, cwd)
    except Exception:  # health: allow BLE001 -- a host without terminal state still runs
        logger.debug("agent_run: cwd seed failed", exc_info=True)


def _clear_task_cwd(task_id: str) -> None:
    try:
        from tools.terminal_tool import clear_session_cwd

        clear_session_cwd(task_id)
    except Exception:  # health: allow BLE001 -- cleanup is best-effort
        logger.debug("agent_run: cwd clear failed", exc_info=True)


def _account(session_id: str, task: str, result: AgentRunResult) -> None:
    """Bill the run to the conversation its caller scoped, under the caller's task name."""
    if not session_id or not task or not result.usage:
        return
    try:
        from hermes_state_registry import acquire, release_or_close

        db = acquire()
    except Exception:  # health: allow BLE001 -- no ledger available is not a run failure
        logger.debug("agent_run: no session store for accounting", exc_info=True)
        return
    try:
        db.record_auxiliary_usage(
            session_id, task, model=result.model, billing_provider=result.provider,
            input_tokens=int(result.usage.get("input_tokens") or result.usage.get("prompt_tokens") or 0),
            output_tokens=int(result.usage.get("output_tokens") or result.usage.get("completion_tokens") or 0),
            cache_read_tokens=int(result.usage.get("cache_read_tokens") or 0),
            cache_write_tokens=int(result.usage.get("cache_write_tokens") or 0),
            reasoning_tokens=int(result.usage.get("reasoning_tokens") or 0),
            estimated_cost_usd=result.usage.get("estimated_cost_usd"),
            api_call_count=max(1, int(result.api_calls or 1)))
    except Exception:  # health: allow BLE001 -- accounting is best-effort by contract
        logger.debug("agent_run: accounting write failed", exc_info=True)
    finally:
        release_or_close(db)


def _record_history(agent_name: str, agent_session_id: str, messages: tuple[dict[str, Any], ...],
                    *, model: str, provider: str, system: str,
                    usage: Mapping[str, Any],
                    path: Optional[Path] = None) -> tuple[str, bool]:
    """Write this run's history; a write failure is reported, never raised into a finished run."""
    target = Path(path) if path is not None else run_transcript.resolve_write_path(
        agent_name, agent_session_id)
    try:
        info = run_transcript.record(agent_name, agent_session_id, messages,
                                     model=model, provider=provider, system_message=system,
                                     usage=usage, path=target)
        return str(info["path"]), bool(info["written"])
    except Exception:  # health: allow BLE001 -- the answer stands; only the audit trail failed
        logger.warning("agent_run: history write failed for %s/%s", agent_name,
                       agent_session_id, exc_info=True)
        return str(target), False


def _begin_run(agent: Any, on_agent: Optional[Callable[[Any], None]]) -> None:
    """Hand the built agent to the host before its first turn (live-session registration)."""
    if on_agent is not None:
        on_agent(agent)


def _release_run(agent: Any, *, keep_alive: bool,
                 on_settled: Optional[Callable[[], None]]) -> None:
    """Run the settle hook, then close the agent unless the host owns its lifetime."""
    if on_settled is not None:
        try:
            on_settled()
        except Exception:  # health: allow BLE001 -- a settled run must not fail on a hook
            logger.debug("agent_run: on_settled hook failed", exc_info=True)
    # One agent per run in a long-lived host: close() releases tool subprocesses and HTTP
    # clients, otherwise every run leaks them. ``keep_alive`` hands that close to the host.
    if not keep_alive:
        with contextlib.suppress(Exception):
            agent.close()


def _validate_output_turn(agent: Any, result: dict[str, Any], schema: dict[str, Any],
                          task_id: str) -> tuple[bool, tuple[str, ...]]:
    """Validate the answer and, when it misses the contract, run ONE bounded retry turn.

    Mirrors ``delegate_task`` exactly: the errors go back verbatim, the schema is not re-pasted,
    and a second failure returns the raw answer with the errors, never a silent pass.
    """
    from tools.delegation_output_schema import build_retry_message, validate_output

    first_text = str(result.get("final_response") or "")
    valid, errors = validate_output(first_text, schema)
    if valid or not first_text.strip() or result.get("interrupted"):
        return valid, tuple(errors)
    retry: Any = None
    try:
        retry = agent.run_conversation(build_retry_message(errors), task_id=task_id)
    except Exception:  # health: allow BLE001 -- the first answer still stands
        logger.warning("agent_run: output schema retry failed", exc_info=True)
    if not isinstance(retry, Mapping):
        return valid, tuple(errors)
    retry_text = str(retry.get("final_response") or "")
    if retry_text.strip():
        result["final_response"] = retry_text
    result["api_calls"] = (int(result.get("api_calls") or 0)
                           + int(retry.get("api_calls") or 0))
    if isinstance(retry.get("messages"), list):
        result["messages"] = list(result.get("messages") or []) + retry["messages"]
    # The turn result's token/cost keys are the agent's cumulative session counters
    # (``turn_finalizer``): the retry turn's copy already includes the first turn, so replace,
    # never add — otherwise a retried run is billed for the first turn only.
    for key in _USAGE_KEYS:
        if retry.get(key) is not None:
            result[key] = retry[key]
    valid, errors = validate_output(retry_text, schema)
    return valid, tuple(errors)


def _prepare_run_context(context: str, *, task_id: str, agent_name: str,
                         agent_session_id: str,
                         autoload_skills: Optional[Sequence[str]],
                         read_summarize: Optional[bool]) -> str:
    """Apply per-run switches that must exist before the first turn."""
    from tools.read_summary import set_read_summarize

    set_read_summarize(task_id, read_summarize, agent_name=agent_name,
                       agent_session_id=agent_session_id)
    if not autoload_skills:
        return context
    try:
        from agent.skill_commands import build_agent_autoload_skills_prompt

        skill_prompt, _loaded, missing = build_agent_autoload_skills_prompt(
            list(autoload_skills), task_id=task_id)
        if missing:
            logger.warning("agent %s: autoloadSkills not found: %s", agent_name,
                           ", ".join(missing))
        if skill_prompt:
            return "\n\n".join(part for part in (skill_prompt, context) if part)
    except Exception:  # health: allow BLE001 -- a skill load must not fail the run
        logger.warning("agent %s: autoloadSkills injection skipped", agent_name, exc_info=True)
    return context


def _execute_turn(agent: Any, message: str, *, context: str,
                  conversation: Optional[list[dict[str, Any]]], system: str,
                  schema: Optional[dict[str, Any]], task_id: str, agent_name: str,
                  agent_session_id: str, model: str, provider: str,
                  toolsets: Sequence[str], all_tools: bool, history_key: str,
                  account_task: str, record_history: bool,
                  transcript_path: str = "") -> AgentRunResult:
    """Run one conversation turn on an already-built agent and package its answer.

    Shared by :func:`run` (fresh agent per call) and :func:`continue_run` (a resident session's
    wake turn on its kept agent), so both paths produce the same transcript, usage and
    ``output`` validation.
    """
    result = agent.run_conversation(
        _prompt(message, context), system_message=system or None,
        conversation_history=conversation or None, task_id=task_id)
    schema_valid: Optional[bool] = None
    schema_errors: tuple[str, ...] = ()
    if schema is not None:
        schema_valid, schema_errors = _validate_output_turn(agent, result, schema, task_id)
    messages = _with_reasoning(
        _transcript(result), result.get("reasoning_content") or result.get("reasoning"))
    if not messages:
        # The host normally returns the loop's messages; keep at least the prompt and the
        # answer so the history still answers "what did this agent do?".
        messages = ({"role": "user", "content": message},
                    {"role": "assistant",
                     "content": str(result.get("final_response") or "")})
    usage = _usage(result)
    peer = AgentRunResult(
        answer=(result.get("final_response") or "").strip(),
        agent_session_id=agent_session_id,
        api_calls=int(result.get("api_calls") or 0),
        model=model,
        provider=provider,
        toolsets=("*",) if all_tools else tuple(toolsets),
        messages=messages,
        usage=usage,
        output_schema_valid=schema_valid,
        output_schema_errors=schema_errors,
    )
    if record_history:
        path, written = _record_history(agent_name, agent_session_id, messages,
                                        model=model, provider=provider,
                                        system=system, usage=usage,
                                        path=Path(transcript_path) if transcript_path else None)
        peer = replace(peer, transcript_path=path, transcript_written=written)
    if record_history:
        from tools.read_summary import collect_summaries

        peer = replace(peer, summary_sources=tuple(collect_summaries(task_id)))
    _account(history_key, account_task, peer)
    return peer


def continue_run(agent: Any, message: str, *, context: str = "", system_message: str = "",
                 output_schema: Optional[Mapping[str, Any]] = None, task_id: str = "",
                 agent_name: str = "agent", agent_session_id: str = "", model: str = "",
                 provider: str = "", toolsets: Sequence[str] = (), all_tools: bool = False,
                 history_key: str = "", account_task: str = "",
                 model_role: str = "",
                 read_summarize: Optional[bool] = None) -> AgentRunResult:
    """One wake turn on a resident session's kept agent (ADR-15 D5).

    The agent is not built, closed or re-registered here: the caller owns its lifetime. The turn
    appends to the same ``agent_session_id`` transcript (the recorder drops the prefix it already
    holds) and validates the definition's ``output`` exactly like a fresh run.
    """
    from tools.read_summary import set_read_summarize

    set_read_summarize(task_id or agent_session_id, read_summarize,
                       agent_name=agent_name, agent_session_id=agent_session_id)
    schema: Optional[dict[str, Any]] = (
        dict(output_schema) if isinstance(output_schema, Mapping) else None)
    if schema is not None:
        from tools.delegation_output_schema import append_output_contract

        context = append_output_contract(context, schema)
    peer = _execute_turn(
        agent, message, context=context, conversation=None, system=system_message,
        schema=schema, task_id=task_id or agent_session_id, agent_name=agent_name,
        agent_session_id=agent_session_id, model=model, provider=provider,
        toolsets=toolsets, all_tools=all_tools, history_key=history_key,
        account_task=account_task, record_history=True)
    return replace(peer, model_role=str(model_role or ""))


def run(message: str, *, context: str = "", model: str = "", provider: str = "",
        api_mode: str = "", toolsets: Optional[Sequence[str]] = None,
        system_message: str | Sequence[str] = "", max_iterations: int = MAX_ITERATIONS,
        project_dir: Optional[str] = None, all_tools: bool = False, read_only: bool = False,
        history: Optional[Sequence[Mapping[str, Any]]] = None, session_id: str = "",
        layer: str = "", history_session: str = "", account_task: str = "",
        agent_session_id: str = "",
        skip_memory: bool = True, skip_context_files: bool = True,
        skip_background_review: bool = True, load_soul_identity: bool = False,
        extra_tools: Optional[Sequence[Mapping[str, Any]]] = None, cache_scope: str = "",
        tool_guardrails: Optional[Mapping[str, Any]] = None,
        reasoning_effort: Any = "", worktree: bool = False,
        base_url: str = "",
        api_key: str = "",
        fallback_chain: Optional[Sequence[Mapping[str, str]]] = None,
        model_role: str = "",
        output_schema: Optional[Mapping[str, Any]] = None,
        autoload_skills: Optional[Sequence[str]] = None,
        advisor: Any = None,
        read_summarize: Optional[bool] = None,
        on_agent: Optional[Callable[[Any], None]] = None,
        on_settled: Optional[Callable[[], None]] = None,
        keep_alive: bool = False,
        meta: Optional[Mapping[str, Any]] = None, record_history: bool = True,
        transcript_path: str = "") -> AgentRunResult:
    """Run one agent loop on *message* and return its answer.

    The run owns its ``AIAgent`` and closes it in ``finally`` — unless ``keep_alive`` is set, in
    which case the caller owns the close. ``on_agent`` sees the built agent before the first turn,
    so a host can register the live session for steering; ``on_settled`` runs in ``finally``
    before the close and must be idempotent.

    ``history`` is the prior conversation a caller explicitly continues from; system rows are
    dropped (the host owns the system prompt). ``session_id`` keeps this run's host session
    distinct from the caller's; an empty value lets the host mint one. ``agent_session_id`` is the
    identifier the transcript, the ``agent_session`` row and ``agent_out`` are keyed on; an empty
    value mints one. ``layer`` names the agent and ``meta`` carries identifiers only. ``all_tools`` passes every host tool; otherwise
    ``toolsets`` is the exact surface (an empty list is "no tools"). ``reasoning_effort`` pins a
    level when given (it outranks config for this run). ``worktree`` runs in a fresh git worktree
    off HEAD: the run's tools start there, and the tree is inspected, pruned or archived after.
    ``base_url``/``api_key``/``fallback_chain`` come from a resolved ``@role`` selector and travel
    with its provider/model; ``model_role`` is that selector's role name, echoed in the result and
    never sent to the model. ``output_schema`` (a definition's ``output``) appends the same OUTPUT
    CONTRACT the delegated children get and validates the answer with one bounded retry.
    """
    from run_agent import AIAgent  # first: the managed-environment bootstrap replaces sys.path

    final_model, runtime = _resolve_route(model, provider, base_url, api_key)
    knobs = _agent_kwargs(runtime, final_model)
    if str(api_mode or "").strip():
        knobs["api_mode"] = str(api_mode).strip()
    chain = [dict(entry) for entry in (fallback_chain or ()) if isinstance(entry, Mapping)]
    if chain:
        # An ``@role`` route owns its ordered backups (``auxiliary.<task>.fallback_chain`` /
        # ``delegation.fallback_providers``); the host's own chain must not leak onto it.
        knobs["fallback_model"] = chain
    if reasoning_effort is not None and reasoning_effort != "":
        from hermes_constants import parse_reasoning_effort

        parsed_effort = parse_reasoning_effort(reasoning_effort)
        if parsed_effort is None:
            raise ValueError(f"unknown reasoning_effort: {reasoning_effort!r}")
        # After the host resolution on purpose: an explicit level wins over config.yaml, while a
        # caller-pinned model still resolves its own route and credentials.
        knobs["reasoning_config"] = parsed_effort
    final_provider = str(knobs.get("provider") or provider or runtime.get("provider") or "")
    tools = [str(tool) for tool in (toolsets or [])]
    conversation = [dict(row) for row in (history or ())
                    if isinstance(row, Mapping) and str(row.get("role") or "") != "system"]
    segments = _system_segments(system_message)
    system = "\n\n".join(segments)
    agent_name = str(layer or (meta or {}).get("agent") or "agent").strip() or "agent"
    history_key = str(history_session or session_id or "").strip()
    agent_session_id = (str(agent_session_id or "").strip()
                        or f"{agent_name}-{int(time.time() * 1000)}-{next(_SEQUENCE)}")
    task_id = agent_session_id
    context = _prepare_run_context(
        context, task_id=task_id, agent_name=agent_name,
        agent_session_id=agent_session_id,
        autoload_skills=autoload_skills, read_summarize=read_summarize)
    cwd = str(project_dir) if project_dir else None
    worktree_info = _attach_worktree(cwd or "", agent_name) if worktree else None
    if worktree_info is not None:
        cwd = worktree_info["path"]
        _seed_task_cwd(task_id, cwd)
        from tools.subagent_worktree import build_worktree_context_note

        context = (context + build_worktree_context_note(worktree_info)).strip()
    schema: Optional[dict[str, Any]] = (
        dict(output_schema) if isinstance(output_schema, Mapping) else None)
    if schema is not None:
        from tools.delegation_output_schema import append_output_contract

        # Appended last so a stable schema leaves the rest of the context's prefix unchanged.
        context = append_output_contract(context, schema)
    worktree_result: Optional[dict[str, Any]] = None
    if worktree and worktree_info is None:
        worktree_result = {
            "requested": True, "created": False,
            "note": "worktree isolation unavailable (not a git work tree, non-local terminal "
                    "backend, or creation failed); the run used the shared workspace.",
        }
    agent = AIAgent(
        model=final_model,
        **knobs,
        session_id=str(session_id or "").strip() or None,
        enabled_toolsets=None if all_tools else tools,
        max_iterations=max(1, int(max_iterations or MAX_ITERATIONS)),
        quiet_mode=True,
        platform="subagent",
        cwd=cwd,
        skip_context_files=bool(skip_context_files),
        skip_memory=bool(skip_memory),
        skip_background_review=bool(skip_background_review),
        load_soul_identity=bool(load_soul_identity),
        extra_tools=[dict(tool) for tool in (extra_tools or ())
                     if isinstance(tool, Mapping)],
        cache_scope=str(cache_scope or "").strip(),
    )
    finalized = False
    if advisor:
        # Consumed by advisor_session_kind/AdvisorSession; a spawned peer is otherwise
        # deliberately excluded from hosting a reviewer.
        agent._advisor_override = advisor
    try:
        _begin_run(agent, on_agent)
        if tool_guardrails:
            _configure_tool_guardrails(agent, tool_guardrails)
        if read_only:
            apply_read_only(agent)
        peer = _execute_turn(
            agent, message, context=context, conversation=conversation, system=system,
            schema=schema, task_id=task_id, agent_name=agent_name,
            agent_session_id=agent_session_id, model=final_model, provider=final_provider,
            toolsets=tools, all_tools=all_tools, history_key=history_key,
            account_task=account_task, record_history=record_history,
            transcript_path=str(transcript_path or ""))
        peer = replace(peer, model_role=str(model_role or ""))
        if worktree_info is not None:
            worktree_result = _finalize_worktree(worktree_info)
            finalized = True
        if worktree_result is not None:
            peer = replace(peer, worktree=worktree_result)
        return peer
    finally:
        if worktree_info is not None:
            # A failed run still leaves a tree behind: inspect it before the cwd record goes.
            if not finalized:
                _finalize_worktree(worktree_info)
            _clear_task_cwd(task_id)
        _release_run(agent, keep_alive=keep_alive, on_settled=on_settled)
        if not keep_alive:
            from tools.read_summary import clear_read_summarize

            clear_read_summarize(task_id)


async def run_async(message: str, **kwargs: Any) -> AgentRunResult:
    """Await :func:`run` off the event loop (the agent loop is synchronous by design)."""
    return await asyncio.to_thread(run, message, **kwargs)


__all__ = [
    "MAX_ITERATIONS",
    "READ_ONLY_DROPPED_TOOLS",
    "AgentRunResult",
    "apply_read_only",
    "continue_run",
    "run",
    "run_async",
]
