"""Uniform renderer for system-prompt sections 00–12 (ADR-18 D1/D13).

``prompt_sections`` is context-agnostic: it renders whatever the manifest declares and
validates types against it. This module is the typed seam for the sections that accept
caller variables. Three block entry points return finished bodies:
:func:`render_role` (01–03), :func:`render_runtime` (04/06–08) and
:func:`render_closing` (10–12); :func:`agent.prompts.assembler.assemble_main` joins them
with the single-section renderers in code order.

Usage::

    render_rfc2119()
    render_identity(role="Hermes Agent", reactions=False)
    render_safety(rules="- No force-push.", override=True)
    render_communication(personality="- Dry.", platform="- omp-ui.")
    render_role("desktop")  # § Role + the desktop default from system/platforms/desktop.md
    render_memories(agent_name="default")  # 05, from $HERMES_HOME/memories
    render_closing(subagent=True)  # 10–12: workflow, delivery, critical
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from hermes_constants import get_hermes_home, get_skills_dir

from agent.prompts.hermes_help import hermes_agent_help_guidance, is_hermes_agent_help
from agent.prompts.kanban import is_kanban_intent, kanban_guidance
from agent.prompts.platforms import IDENTITY_CAPS, resolve_platform_hint
from agent.prompts.project_context import effective_project_dir, render_project_context
from agent.prompts.prompt_sections import (
    SYSTEM_PROMPT_DIR,
    read_prompt_text,
    render_section,
)
from agent.prompts.rules import project_languages as rule_project_languages
from agent.prompts.rules import rule_prompt
from agent.prompts.skills import bundled_skill_rows, skill_rows
from agent.prompts.toolsets import PM_DEFAULT_TOOLSETS, resolve_tool_names, tool_inventory_rows
from agent.prompts.urls import internal_url_rows
from agent.ttsr_prompts import escape_harness_tags

if TYPE_CHECKING:  # the assembler owns the dataclass; avoid an import cycle at runtime
    from agent.prompts.assembler import SubagentRole

logger = logging.getLogger(__name__)

HEAD_SEPARATOR = "\n\n"
RUNTIME_HEADING = "§ Runtime"
MEMORIES_GUIDANCE_PATH = SYSTEM_PROMPT_DIR / "memories-guidance.md"
DEFAULT_AGENT_NAME = "default"


def render_rfc2119() -> str:
    """00 — fixed RFC 2119 keyword block; takes no variables."""
    return render_section("rfc2119")


def render_identity(
    *,
    profile: str | None = None,
    role: str | None = None,
    role_desc: str | None = None,
    role_section: str | None = None,
    auto_graph: bool | None = None,
    render_mermaid: bool | None = None,
    render_svg: bool | None = None,
    reactions: bool | None = None,
) -> str:
    """01 — role and personality; ``None`` keeps that template default."""
    context = {
        "profile": profile or "",
        "role": role,
        "role_desc": role_desc,
        "role_section": role_section,
        "auto_graph": auto_graph,
        "render_mermaid": render_mermaid,
        "render_svg": render_svg,
        "reactions": reactions,
    }
    return render_section("identity", {
        key: value for key, value in context.items() if value is not None or key == "profile"
    })


def render_safety(*, rules: str = "", override: bool = False) -> str:
    """02 — default policy, default plus *rules*, or *rules* alone when *override*."""
    return render_section("safety", {"safety_rules": rules, "safety_override": override})


def _memory_agent_name(agent_name: str) -> str:
    """Normalize the peer name used under ``memories/agents/``; reject path escapes."""
    name = str(agent_name or "").strip()
    if not name:
        return DEFAULT_AGENT_NAME
    if name in {".", ".."} or "/" in name or "\\" in name or Path(name).name != name:
        raise ValueError(f"invalid agent name for memories: {agent_name!r}")
    return name


def _read_memory_file(path: Path) -> str:
    """Memory-file text for the prompt, or "" when absent/unreadable.

    Memory text enters the system prompt and persists across sessions, so it gets the
    same strict-scope scan the memory store applies to its frozen snapshot. A hit is
    replaced by a placeholder instead of leaking the payload; the file itself is untouched.
    """
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read memory file %s: %s", path, exc)
        return ""
    if not text:
        return ""
    from tools.threat_patterns import scan_for_threats

    findings = scan_for_threats(text, scope="strict")
    if not findings:
        return text
    logger.warning("Memory file %s blocked at render time: %s", path, ", ".join(findings))
    return (f"[BLOCKED: {path.name} matched threat pattern(s): {', '.join(findings)}. "
            "Removed from the system prompt; edit the file to delete the payload.]")


def _memories_guidance() -> str:
    """The static usage/feedback guide shipped beside the section templates."""
    return read_prompt_text(MEMORIES_GUIDANCE_PATH).strip()


def render_memories(
    *,
    agent_name: str = DEFAULT_AGENT_NAME,
    memory_enabled: bool = True,
    user_profile_enabled: bool = True,
    home: str | Path | None = None,
    memories_provider: str = "",
) -> str:
    """05 — built-in memories plus the external provider block.

    ``memory_enabled`` gates the whole section (including the provider block);
    ``user_profile_enabled`` gates only ``<memories-user>``. Files are read from *home*
    (default: :func:`hermes_constants.get_hermes_home`):

    - ``memories/USER.md`` → ``<memories-user>``
    - ``memories/agents/<agent_name>/AI_SELF.md`` → ``<memories-self>``
    - ``memories/agents/<agent_name>/MEMORY.md`` → ``<memories-work>``

    ``MEMORY.md`` is the recent-session tag; this renderer never queries the session
    database. The main agent's peer name is ``default``; subagents pass their own name.
    A missing file renders an empty tag rather than failing the prompt build.
    """
    if not memory_enabled:
        return ""
    name = _memory_agent_name(agent_name)
    root = Path(home) if home is not None else get_hermes_home()
    memories = root / "memories"
    agent_dir = memories / "agents" / name
    work_path = agent_dir / "MEMORY.md"
    if name == DEFAULT_AGENT_NAME and not work_path.is_file():
        # The built-in store writes the main agent's work memory at the memories root.
        work_path = memories / "MEMORY.md"
    return render_section(
        "memories",
        {
            "memories_guidance": _memories_guidance(),
            "memories_user": _read_memory_file(memories / "USER.md") if user_profile_enabled else "",
            "memories_self": _read_memory_file(agent_dir / "AI_SELF.md"),
            "memories_work": _read_memory_file(work_path),
            "memories_provider": memories_provider,
            "user_profile_enabled": user_profile_enabled,
        },
    )


def _available_tools(
    toolsets: Sequence[str] | None,
    enable_tools: Sequence[str] | None,
) -> set[str]:
    """Resolved tool names plus the shell binaries the PM environment provides.

    ``rg`` and ``ast-grep`` are executables, not registry tools, so resolution cannot
    see them; they exist for a session exactly when its surface includes a shell.
    """
    available = resolve_tool_names(toolsets=toolsets, enable_tools=enable_tools)
    if "terminal" in available:
        available.update(PM_DEFAULT_TOOLSETS)
    return available


def _owned_kanban_task() -> str:
    """The dispatcher-owned board task for this execution, or "" outside a worker."""
    from agent.delegation_context import owned_kanban_task

    return owned_kanban_task()


def render_tool_policy(
    *,
    toolsets: Sequence[str] | None = None,
    enable_tools: Sequence[str] | None = None,
    delegation_bias: str = "eager",
    eager_tasks: bool = False,
    eager_tasks_always: bool = False,
    inline_first: bool = True,
    max_concurrency: int = 0,
) -> str:
    """09 — tool selection and invocation, capability-gated by the session's tool face.

    ``toolsets``/``enable_tools`` resolve exactly as in :func:`render_runtime`.
    :data:`~agent.prompts.toolsets.PM_DEFAULT_TOOLSETS` (``rg``, ``ast-grep``) is merged
    in when ``terminal`` is available: those are shell binaries, so registry-based
    resolution cannot see them and a session without a shell cannot run them.

    ``delegation_bias`` is ``"gated"`` or ``"eager"``; the delegation tree mirrors omp's
    switches (``eager_tasks``, ``eager_tasks_always``, ``inline_first``) with Hermes's
    ``agent``/``agent_out``, which fully supersede ``delegate_task``. ``max_concurrency``
    above zero names the cap. The section deliberately carries no tool inventory and no
    completion/verification rules — those belong to the inventory and Workflow/Delivery.
    """
    if delegation_bias not in {"gated", "eager"}:
        raise ValueError(f"delegation_bias must be 'gated' or 'eager', got {delegation_bias!r}")
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 0:
        raise ValueError(f"max_concurrency must be a non-negative int, got {max_concurrency!r}")

    available = _available_tools(toolsets, enable_tools)
    if not available:
        return ""
    return render_section(
        "tool_policy",
        {
            "tools": sorted(available),
            "delegation_bias": delegation_bias,
            "eager_tasks": eager_tasks,
            "eager_tasks_always": eager_tasks_always,
            "inline_first": inline_first,
            "max_concurrency": max_concurrency,
        },
    )


def render_tool_policy_from_config(
    *,
    toolsets: Sequence[str] | None = None,
    enable_tools: Sequence[str] | None = None,
    definition: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> str:
    """09 with the policy switches resolved from config.yaml and an agent definition.

    Explicit section rendering stays deterministic; the assembler uses this entry so the
    delegation bias/eager/inline/concurrency text follows the profile's configuration.
    """
    from agent.prompts.delegation import resolve_delegation_policy

    policy = resolve_delegation_policy(config, definition=definition)
    return render_tool_policy(
        toolsets=toolsets, enable_tools=enable_tools, **policy)


def render_workflow(
    *,
    toolsets: Sequence[str] | None = None,
    enable_tools: Sequence[str] | None = None,
    subagent: bool = False,
) -> str:
    """10 — workflow phases, capability-gated by the session's tool face.

    ``tools`` reaches the template as resolved tool names, so a phase that needs a
    missing capability drops out instead of naming a tool the agent cannot call.
    ``subagent`` swaps Verify for Hand-off: the main agent runs verification once.
    """
    available = _available_tools(toolsets, enable_tools)
    return render_section("workflow", {"tools": sorted(available), "subagent": subagent})


def render_delivery(*, subagent: bool = False) -> str:
    """11 — delivery contract, completeness, evidence and yielding rules.

    ``subagent`` narrows the yielding line to the slice it owns and hands verification
    to the main agent; the contract itself is identical for both roles.
    """
    return render_section("delivery", {"subagent": subagent})


def render_critical(*, subagent: bool = False) -> str:
    """12 — the closing invariants; the last bullet follows *subagent*."""
    return render_section("critical", {"subagent": subagent})


def render_closing(
    *,
    subagent: bool = False,
    project_dir: str | Path | None = None,
    **workflow: Any,
) -> str:
    """Render 10–12 in one call, blank-line joined in manifest order.

    A non-coding session has no Verify phase to hand off, so its workflow section is
    omitted; delivery and critical always apply. The block judges its own project kind
    (same detection as :func:`render_runtime`); no caller gate decides it.
    """
    project = effective_project_dir(project_dir)
    parts = [
        render_workflow(subagent=subagent, **workflow) if rule_project_languages(project) else "",
        render_delivery(subagent=subagent),
        render_critical(subagent=subagent),
    ]
    return HEAD_SEPARATOR.join(part.strip() for part in parts if part.strip())


def render_runtime(
    *,
    skills: Sequence[str] | None = None,
    skills_dir: str | Path | None = None,
    project_dir: str | Path | None = None,
    platform: str = "",
    first_input: str | None = None,
    toolsets: Sequence[str] | None = None,
    enable_tools: Sequence[str] | None = None,
) -> str:
    """Runtime block: ``§ Runtime`` is added here, then section 06 renders the three parts.

    *skills* defaults to :func:`agent.prompts.skills.bundled_skill_rows` — the built-in
    tree, each entry prejoined as ``"<name>: <description>"``. An explicit empty list
    renders the fixed guide parts without an ``<available_skills>`` block. Rules are
    discovered internally (built-in + language-matched ``$HERMES_HOME/rules``) from
    *project_dir* (default: cwd), then bucketed into ``<generic-rules>`` (always-apply)
    and ``<domain-rules>`` (rulebook); a rule registered with the session's TTSR matcher
    is listed in neither block. The block judges the project kind itself: a
    non-programming project omits the rules section and the codegraph inventory. The
    internal-location list is fixed in code as well — there is no caller parameter for
    it, so extra directories cannot be injected into the prompt. When *first_input* asks
    for help with Hermes Agent itself (judged by ``is_hermes_agent_help``), the packaged
    help guidance is appended to that block; the kanban judgment runs only when the
    session actually holds kanban tools.

    *toolsets* may only name default-loaded built-in sets; a non-empty *enable_tools*
    replaces it with an explicit tool allowlist. Skills/guidance/tool-inventory render
    from the resulting tool names, so a section never advertises a missing capability.
    """
    available = resolve_tool_names(toolsets=toolsets, enable_tools=enable_tools)
    project = effective_project_dir(project_dir)
    coding_project = bool(rule_project_languages(project))

    parts: list[str] = []
    skill_map = {tool: tool in available for tool in ("skill_view", "skill_manage", "skills_list")}
    if any(skill_map.values()):
        if skills is not None:
            rows = list(skills)
        else:
            session_skills = Path(skills_dir) if skills_dir is not None else get_skills_dir()
            try:
                import model_tools
                skill_toolsets = {model_tools.get_toolset_for_tool(name) for name in available} - {None, ""}
            except Exception:  # health: allow BLE001 -- toolset derivation is advisory; the index must still render
                skill_toolsets = None
            rows = (skill_rows(session_skills, available_tools=set(available),
                               available_toolsets=skill_toolsets, platform=platform)
                    if session_skills.is_dir() else [])
            if not rows:
                # A profile that has not synced its skills tree yet still advertises the
                # bundled catalog instead of silently rendering an empty skills block.
                rows = bundled_skill_rows()
        parts.append(render_section("skills", {"skills": rows, **skill_map}))

    if coding_project:
        rules = rule_prompt(project)
        if rules is not None:
            parts.append(render_section("rules", {
                "generic_rules": list(rules.generic),
                "domain_rules": list(rules.domain),
                "rule_view": "rule_view" in available,
            }))

    help_text = (
        hermes_agent_help_guidance(with_skills=skill_map["skill_view"])
        if first_input and is_hermes_agent_help(first_input) else ""
    )
    # Cheap gate first: the default toolset has no kanban tools, so only sessions
    # that actually hold them pay for the side-channel judgment. A dispatcher-owned
    # worker gets the lifecycle block regardless of the first message: it owns a task
    # and every turn needs the protocol.
    holds_kanban = any(name.startswith("kanban_") for name in available)
    if holds_kanban and "kanban_show" in available and _owned_kanban_task():
        kanban_text = kanban_guidance()
    else:
        kanban_text = (
            kanban_guidance()
            if first_input and holds_kanban and is_kanban_intent(first_input)
            else ""
        )
    parts.append(render_section(
        "internal_urls",
        {
            "internal_urls": internal_url_rows(),
            "hermes_help_guidance": help_text,
            "kanban_guidance": kanban_text,
        },
    ))

    inventory = tool_inventory_rows(available, coding_project=coding_project)
    if inventory:
        parts.append(render_section("tool_inventory", {"tool_groups": inventory}))

    body = HEAD_SEPARATOR.join(part.strip() for part in parts)
    return f"{RUNTIME_HEADING}{HEAD_SEPARATOR}{body}"


def render_role(
    platform: str = "",
    *,
    identity: Mapping[str, Any] | None = None,
    safety: Mapping[str, Any] | None = None,
    personality: str = "",
    style: str = "",
    tool_names: Sequence[str] = (),
    profile: str = "",
) -> str:
    """§ Role block for one platform: 01 identity, 02 safety, 03 communication.

    The hint resolves here — built-in/plugin text plus the config overlay, the desktop
    TUI clarifier and a cron agent's delivery channel — never through ``SessionFacts``.
    Identity starts from the platform's capability gates (``IDENTITY_CAPS``); explicit
    ``identity`` keys win. ``personality``/``style`` reach section 03 unchanged.
    """
    hint = resolve_platform_hint(platform, tool_names=tool_names) if platform else ""
    identity_context = {**IDENTITY_CAPS.get(platform, {}), **dict(identity or {})}
    return HEAD_SEPARATOR.join(
        part.strip()
        for part in (
            render_identity(profile=profile or None, **identity_context),
            render_safety(**dict(safety or {})),
            render_communication(personality=personality, style=style, platform=hint),
        )
    )


def render_communication(
    *,
    personality: str = "",
    style: str = "",
    platform: str = "",
) -> str:
    """03 — optional personality, communication style (default overridable), platform hint."""
    return render_section(
        "communication",
        {
            "personality": personality,
            "communication_style": style,
            "platform_hint": platform,
        },
    )


def render_autoload_skills(*, autoload_skills: str) -> str:
    """Pinned ``skills.auto_load`` bodies, rendered as their own block; "" when none."""
    body = str(autoload_skills or "").strip()
    return render_section("autoload_skills", {"autoload_skills": body}) if body else ""


def render_request(key: str, context: Mapping[str, Any]) -> str:
    """Render a ``scope=request`` template; a system section is rejected.

    Request templates ride a user/tool row and never enter the assembled prompt (ADR-19 D10).
    The scope guard is the enforcement point: an accidental system key here would duplicate a
    block the assembler already owns. Interpolated content (an IRC message, § Context, a plan
    body) stays byte-exact, so only outer whitespace is trimmed; blank-line cleanup belongs to
    Jinja whitespace control in the template, never a regex here.
    """
    return render_section(key, context, scope="request").strip()


def render_eager_task(*, task_batch: bool = True) -> str:
    """Per-turn delegation reminder (``delegation.eager_tasks``)."""
    return render_request("eager_task", {"task_batch": bool(task_batch)})


def render_eager_todo(*, forced: bool = False) -> str:
    """Todo reminder, forced before substantive work when the host asks for it."""
    return render_request("eager_todo", {"forced": bool(forced)})


# Request notices are bounded here, not by a manifest budget: a ``tail`` truncation of an
# XML envelope would cut its closing tag and let the model read an unterminated block (ADR-20 D3).
_NOTICE_ROW_CHARS = 320
_NOTICE_MAX_SEARCHES = 3
_NOTICE_MAX_ACTIONS = 8


def _notice_row(value: Any, *, max_chars: int = _NOTICE_ROW_CHARS) -> str:
    """One preformatted notice bullet: single line, clipped, harness tags escaped."""
    text = " ".join(str(value or "").split())
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return escape_harness_tags(text)


def _notice_rows(values: Sequence[Any], *, limit: int) -> list[str]:
    return [row for row in (_notice_row(value) for value in values) if row][:limit]


def render_session_tool_search_notice(*, searches: Sequence[str]) -> str:
    """Latest session tool searches (newest first, at most 3); "" when none.

    ``searches`` are preformatted one-line rows (query and matched tool names) owned by the
    session ledger; this helper bounds and escapes them, never re-parses tool payloads.
    """
    rows = _notice_rows(searches, limit=_NOTICE_MAX_SEARCHES)
    return render_request("session_tool_search_notice", {"searches": rows}) if rows else ""


def render_session_skill_search_notice(*, searches: Sequence[str]) -> str:
    """Latest session skill searches (newest first, at most 3); "" when none."""
    rows = _notice_rows(searches, limit=_NOTICE_MAX_SEARCHES)
    return render_request("session_skill_search_notice", {"searches": rows}) if rows else ""


def render_background_review_skill_reminder(*, actions: Sequence[str]) -> str:
    """Skill writes a background review already landed; "" when the review changed no skills."""
    rows = _notice_rows(actions, limit=_NOTICE_MAX_ACTIONS)
    return render_request("background_review_skill_reminder", {"actions": rows}) if rows else ""


def render_background_review_memory_reminder(*, actions: Sequence[str]) -> str:
    """Memory writes a background review already landed; "" when it changed no memories."""
    rows = _notice_rows(actions, limit=_NOTICE_MAX_ACTIONS)
    return render_request("background_review_memory_reminder", {"actions": rows}) if rows else ""


def render_irc_incoming(
    *,
    sender: str,
    message: str,
    reply_to: str = "",
    progress: bool = False,
    interrupting: bool = False,
    relay_on_stop: bool = True,
) -> str:
    """One inbound peer message envelope; ``sender`` is resolved by the host, never the model."""
    return render_request(
        "irc_incoming",
        {
            "from": str(sender),
            "message": str(message),
            "reply_to": str(reply_to or ""),
            "progress": bool(progress),
            "interrupting": bool(interrupting),
            "relay_on_stop": bool(relay_on_stop),
        },
    )


def render_subagent_role(
    *,
    role: str,
    context: str = "",
    plan_reference: bool = False,
    peers: str = "",
    completion: str = "",
) -> str:
    """Child Role/Context/Peers/Completion block from a definition or caller system message."""
    return render_request(
        "subagent_role",
        {
            "role": str(role),
            "context": str(context or ""),
            "plan_reference": bool(plan_reference),
            "peers": str(peers or ""),
            "completion": str(completion or ""),
        },
    )


def render_subagent_prompt(role: SubagentRole) -> str:
    """The role block alone: ``§ Subagent Role``, context, plan, peers and completion.

    This is the only entry for ``system/subagent-system-prompt.md``; ``assemble_subagent``
    appends its bytes after the shared base. ``body`` is the definition body with front matter
    removed. Peers arrive pre-formatted so the template never interprets host records.
    """
    return render_request(
        "subagent_system_prompt",
        {
            "agent": str(role.body),
            "context": str(role.context or ""),
            "plan_reference": str(role.plan_reference or ""),
            "plan_reference_path": str(role.plan_reference_path or ""),
            "worktree": str(role.worktree or ""),
            "irc_self_id": str(role.irc_self_id or ""),
            "irc_peers": [str(peer) for peer in role.irc_peers],
            "irc_omitted_count": int(role.irc_omitted_count or 0),
            "irc_parked_count": int(role.irc_parked_count or 0),
            "structured": bool(role.structured),
            "work_pool_yield_items": bool(role.work_pool_yield_items),
            "has_output_schema": bool(role.has_output_schema),
            "yield_schema": str(role.yield_schema or ""),
            "output_schema_overrides_agent": bool(role.output_schema_overrides_agent),
        },
    )


def render_subagent_user_prompt(*, assignment: str) -> str:
    """Wrap one child assignment as the user turn."""
    return render_request("subagent_user_prompt", {"assignment": str(assignment)})


def render_subagent_async_pending(
    *, count: int, jobs: str, multiple: bool | None = None
) -> str:
    """Remind a child that its yield landed while owned background jobs still run."""
    jobs_count = int(count)
    return render_request(
        "subagent_async_pending",
        {
            "count": jobs_count,
            "jobs": str(jobs),
            "multiple": (jobs_count > 1) if multiple is None else bool(multiple),
        },
    )


def render_subagent_completion_probe(
    *,
    inflight_lines: Sequence[str] = (),
    has_previous: bool = False,
    previous_ago: str = "",
    previous_percent: int = 0,
    previous_stage: str = "",
) -> str:
    """One orchestrator progress probe; ``inflight_lines`` are pre-formatted tool summaries."""
    return render_request(
        "subagent_completion_probe",
        {
            "inflight_lines": [str(line) for line in inflight_lines],
            "has_previous": bool(has_previous),
            "previous_ago": str(previous_ago or ""),
            "previous_percent": int(previous_percent),
            "previous_stage": str(previous_stage or ""),
        },
    )


def render_subagent_yield_reminder(
    *, budget_stop: bool = False, structured: bool = False,
    retry_count: int = 1, max_retries: int = 3,
) -> str:
    """Forced wrap-up at the budget wall, or the idle-session no-tool-call reminder."""
    return render_request(
        "subagent_yield_reminder",
        {
            "budget_stop": bool(budget_stop),
            "structured": bool(structured),
            "retry_count": int(retry_count),
            "max_retries": int(max_retries),
        },
    )


def render_workpool_batch(*, pool: str, batch: str, items: Sequence[str]) -> str:
    """One workpool batch turn; ``items`` are raw item texts, numbered 1-based here."""
    rows = [{"index": index + 1, "text": str(text)}
            for index, text in enumerate(items)]
    return render_request("workpool_batch", {
        "pool": str(pool), "batch": str(batch), "items": rows})


__all__ = [
    "HEAD_SEPARATOR",
    "MEMORIES_GUIDANCE_PATH",
    "RUNTIME_HEADING",
    "render_autoload_skills",
    "render_background_review_memory_reminder",
    "render_background_review_skill_reminder",
    "render_closing",
    "render_communication",
    "render_critical",
    "render_delivery",
    "render_eager_task",
    "render_eager_todo",
    "render_identity",
    "render_irc_incoming",
    "render_memories",
    "render_project_context",
    "render_request",
    "render_rfc2119",
    "render_role",
    "render_runtime",
    "render_safety",
    "render_session_skill_search_notice",
    "render_session_tool_search_notice",
    "render_subagent_async_pending",
    "render_subagent_completion_probe",
    "render_subagent_prompt",
    "render_subagent_role",
    "render_subagent_user_prompt",
    "render_subagent_yield_reminder",
    "render_tool_policy",
    "render_tool_policy_from_config",
    "render_workflow",
    "render_workpool_batch",
]
