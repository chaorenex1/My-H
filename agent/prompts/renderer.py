"""Uniform renderer for the stable head sections 00–03 (ADR-18 D1/D13).

``prompt_sections`` is context-agnostic: it renders whatever the manifest declares and
validates types against it. This module is the typed seam for the sections that accept
caller variables, plus :func:`render_head`, which renders 00–03 through that one
pipeline and joins them in manifest order.

Usage::

    render_rfc2119()
    render_identity(role="Hermes Agent", reactions=False)
    render_safety(rules="- No force-push.", override=True)
    render_communication(personality="- Dry.", platform="- omp-ui.")
    render_role("desktop")  # § Role + the desktop default from system/platforms/desktop.md
    render_head(identity={"role": "Hermes Agent"}, safety={"rules": "- No force-push."})
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.prompts.hermes_help import hermes_agent_help_guidance, is_hermes_agent_help
from agent.prompts.kanban import is_kanban_intent, kanban_guidance
from agent.prompts.platforms import IDENTITY_CAPS, load_platform_hints
from agent.prompts.prompt_sections import render_section
from agent.prompts.rules import project_languages as rule_project_languages
from agent.prompts.rules import rule_rows
from agent.prompts.skills import bundled_skill_rows
from agent.prompts.toolsets import resolve_tool_names, tool_inventory_rows
from agent.prompts.urls import internal_url_rows

HEAD_SECTIONS = ("rfc2119", "identity", "safety", "communication")
HEAD_SEPARATOR = "\n\n"
RUNTIME_HEADING = "§ Runtime"


def render_rfc2119() -> str:
    """00 — fixed RFC 2119 keyword block; takes no variables."""
    return render_section("rfc2119")


def render_identity(
    *,
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
        "role": role,
        "role_desc": role_desc,
        "role_section": role_section,
        "auto_graph": auto_graph,
        "render_mermaid": render_mermaid,
        "render_svg": render_svg,
        "reactions": reactions,
    }
    return render_section("identity", {key: value for key, value in context.items() if value is not None})


def render_safety(*, rules: str = "", override: bool = False) -> str:
    """02 — default policy, default plus *rules*, or *rules* alone when *override*."""
    return render_section("safety", {"safety_rules": rules, "safety_override": override})


def render_runtime(
    *,
    skills: Sequence[str] | None = None,
    project_dir: str | Path | None = None,
    first_input: str | None = None,
    toolsets: Sequence[str] | None = None,
    enable_tools: Sequence[str] | None = None,
) -> str:
    """Runtime block: ``§ Runtime`` is added here, then section 06 renders the three parts.

    *skills* defaults to :func:`agent.prompts.skills.bundled_skill_rows` — the built-in
    tree, each entry prejoined as ``"<name>: <description>"``. An explicit empty list
    renders the fixed guide parts without an ``<available_skills>`` block. Rules are
    discovered internally (built-in + language-matched ``$HERMES_HOME/rules``) from
    *project_dir* (default: cwd); a non-programming project omits the rules section.
    The internal-location list is fixed in code as well — there is no caller parameter
    for it, so extra directories cannot be injected into the prompt. When *first_input*
    asks for help with Hermes Agent itself (judged by ``is_hermes_agent_help``), the
    packaged help guidance is appended to that block.

    *toolsets* may only name default-loaded built-in sets; a non-empty *enable_tools*
    replaces it with an explicit tool allowlist. Skills/guidance/tool-inventory render
    from the resulting tool names, so a section never advertises a missing capability.
    """
    available = resolve_tool_names(toolsets=toolsets, enable_tools=enable_tools)
    project = Path(project_dir) if project_dir is not None else Path.cwd()

    parts: list[str] = []
    skill_map = {tool: tool in available for tool in ("skill_view", "skill_manage", "skills_list")}
    if any(skill_map.values()):
        rows = bundled_skill_rows() if skills is None else list(skills)
        parts.append(render_section("skills", {"skills": rows, **skill_map}))

    rules = rule_rows(project)
    if rules is not None:
        parts.append(render_section("rules", {"rules": rules}))

    help_guidance = ""
    kanban_text = ""
    if first_input:
        if is_hermes_agent_help(first_input):
            help_guidance = hermes_agent_help_guidance(with_skills=skill_map["skill_view"])
        # Cheap gate first: the default toolset has no kanban tools, so only sessions that
        # actually hold them pay for the side-channel judgment.
        if any(name.startswith("kanban_") for name in available) and is_kanban_intent(first_input):
            kanban_text = kanban_guidance()
    parts.append(render_section(
        "internal_urls",
        {
            "internal_urls": internal_url_rows(),
            "hermes_help_guidance": help_guidance,
            "kanban_guidance": kanban_text,
        },
    ))

    coding_project = bool(rule_project_languages(project))
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
) -> str:
    """§ Role block for one platform: 01 identity, 02 safety, 03 communication.

    The ``# Platform`` block takes ``system/platforms/<platform>.md``; an unknown or
    empty platform omits it. Identity starts from the platform's capability gates
    (``IDENTITY_CAPS``); explicit ``identity`` keys win. ``personality``/``style`` reach
    section 03 unchanged.
    """
    hint = load_platform_hints().get(platform, "") if platform else ""
    identity_context = {**IDENTITY_CAPS.get(platform, {}), **dict(identity or {})}
    return HEAD_SEPARATOR.join(
        part.strip()
        for part in (
            render_identity(**identity_context),
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


def render_head(
    *,
    identity: Mapping[str, Any] | None = None,
    safety: Mapping[str, Any] | None = None,
    communication: Mapping[str, Any] | None = None,
) -> str:
    """Render 00–03 through one pipeline, blank-line joined in manifest order.

    ``identity``, ``safety`` and ``communication`` take the same keywords as their
    per-section entry methods, so an unknown key fails here instead of reaching a
    template.
    """
    return HEAD_SEPARATOR.join(
        part.strip()
        for part in (
            render_rfc2119(),
            render_identity(**dict(identity or {})),
            render_safety(**dict(safety or {})),
            render_communication(**dict(communication or {})),
        )
    )


__all__ = [
    "HEAD_SECTIONS",
    "HEAD_SEPARATOR",
    "RUNTIME_HEADING",
    "render_communication",
    "render_head",
    "render_identity",
    "render_rfc2119",
    "render_role",
    "render_runtime",
    "render_safety",
]
