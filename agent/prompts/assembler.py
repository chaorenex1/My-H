"""System-prompt assembler: code-owned order, one joined body.

The renderer exposes three block entry points — :func:`renderer.render_role`,
:func:`renderer.render_runtime` and :func:`renderer.render_closing` — plus the
single-section ``render_*`` functions. Each block method returns its finished body (it
already decides what belongs in the block), is called **once** per assembly, and this
module joins the bodies in code order.

There is no tier, gate table or per-section dispatch here: the manifest remains the
template registry, while the prompt structure is this function's code.

Usage::

    prompt = assemble_main(RenderContext(inputs=AssemblyInputs(tools=tools), facts=facts))
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.prompts import renderer
from agent.prompts.prompt_sections import PromptSectionError


@dataclass(frozen=True)
class AssemblyInputs:
    """Caller-evaluated switches; assembly derives none of them."""

    tools: frozenset[str] = frozenset()
    subagent: bool = False
    memory: bool = False


@dataclass(frozen=True)
class SessionFacts:
    """Session facts renderers need; evaluated once by the caller."""

    platform: str = ""
    model: str = ""
    provider: str = ""
    session_id: str = ""
    context_length: int | None = None
    context_cwd: str = ""
    cwd_is_launch_artifact: bool = False
    skip_context_files: bool = False
    workspace_roots: tuple[str, ...] = ()
    toolsets: tuple[str, ...] = ()
    memory_provider: str = ""
    user_profile_enabled: bool = True
    auto_load_skills: str = ""
    profile: str = ""
    delegation_policy: Mapping[str, Any] = field(default_factory=dict)
    append_prompt: str = ""
    environment_probe: bool = True
    agent_definition: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RenderContext:
    """Immutable input for one assembly; constructed once per build."""

    inputs: AssemblyInputs
    facts: SessionFacts = SessionFacts()
    agent_name: str = "default"
    home: Path | None = None
    project_dir: Path | None = None
    first_input: str | None = None


@dataclass(frozen=True)
class SubagentRole:
    """Role-block data for ``system/subagent-system-prompt.md`` (ADR-19 D9)."""

    body: str
    context: str = ""
    plan_reference: str = ""
    plan_reference_path: str = ""
    worktree: str = ""
    irc_self_id: str = ""
    irc_peers: tuple[str, ...] = ()
    irc_omitted_count: int = 0
    irc_parked_count: int = 0
    structured: bool = False
    work_pool_yield_items: bool = False
    has_output_schema: bool = False
    yield_schema: str = ""
    output_schema_overrides_agent: bool = False


def _tool_face(ctx: RenderContext) -> dict[str, Any]:
    """Renderer tool arguments: explicit allowlist, or the empty toolset when none."""
    tools = sorted(ctx.inputs.tools)
    return {"enable_tools": tools} if tools else {"toolsets": ()}


def _project_dir(ctx: RenderContext) -> str | Path | None:
    return ctx.project_dir or (ctx.facts.context_cwd or None)


def _identity_overrides(ctx: RenderContext) -> dict[str, str]:
    definition = ctx.facts.agent_definition
    if not isinstance(definition, Mapping):
        return {}
    return {
        key: value.strip()
        for key in ("role", "role_desc", "role_section")
        if isinstance(value := definition.get(key), str) and value.strip()
    }


def _join(parts: tuple[str, ...]) -> str:
    return "\n\n".join(text.strip() for text in parts if text and text.strip())


def assemble_main(ctx: RenderContext) -> str:
    """Assemble the prompt text from the renderer's block entry points.

    Order is this function's code: each block method returns its finished body, so there
    is no tier, gate table or per-section dispatch here. Empty bodies drop out.
    """
    facts, inputs = ctx.facts, ctx.inputs
    tools = sorted(inputs.tools)
    tool_args = _tool_face(ctx)
    delegation = dict(facts.delegation_policy)

    parts = (
        renderer.render_rfc2119(),
        renderer.render_role(
            facts.platform,
            identity=_identity_overrides(ctx),
            tool_names=tuple(tools),
            profile=facts.profile,
        ),
        renderer.render_runtime(
            project_dir=_project_dir(ctx),
            first_input=ctx.first_input,
            platform=facts.platform,
            skills_dir=(ctx.home / "skills") if ctx.home is not None else None,
            **tool_args,
        ),
        renderer.render_autoload_skills(autoload_skills=facts.auto_load_skills),
        renderer.render_tool_policy(**tool_args, **delegation),
        renderer.render_memories(
            agent_name=ctx.agent_name,
            memory_enabled=inputs.memory,
            user_profile_enabled=facts.user_profile_enabled,
            home=ctx.home,
            memories_provider=facts.memory_provider,
        ),
        renderer.render_closing(
            subagent=inputs.subagent, project_dir=_project_dir(ctx), **tool_args),
        renderer.render_project_context(
            provider=facts.provider,
            model=facts.model,
            cwd=_project_dir(ctx),
            context_length=facts.context_length,
            platform=facts.platform,
            workspace_roots=facts.workspace_roots,
            subagent=inputs.subagent,
            append_prompt=facts.append_prompt,
            environment_probe=facts.environment_probe,
            skip_context_files=facts.skip_context_files,
        ),
    )
    return _join(parts)


def assemble_subagent_parts(ctx: RenderContext, role: SubagentRole) -> tuple[str, str]:
    """``(base, role_block)`` for the cache contract; ``assemble_subagent`` joins them.

    ``stable`` stays the shared base (a literal prefix of the full child prompt) while the role
    block rides the context tier, so ``_cached_system_prompt_static`` and every compression
    rebuild reproduce :func:`assemble_subagent` bytes without a second full assembly.
    """
    if not str(role.body or "").strip():
        raise PromptSectionError("subagent role body must not be empty")
    return assemble_main(ctx), renderer.render_subagent_prompt(role).strip()


def assemble_subagent(ctx: RenderContext, role: SubagentRole) -> str:
    """Main-agent bytes followed by the role block (ADR-19 D9).

    The base is byte-identical for every child sharing *ctx*; only the trailing role block
    differs. A child without a definition body is a build error, not a bare prompt.
    """
    base, role_block = assemble_subagent_parts(ctx, role)
    return f"{base}\n\n{role_block}" if base else role_block


__all__ = [
    "AssemblyInputs",
    "RenderContext",
    "SessionFacts",
    "SubagentRole",
    "assemble_main",
    "assemble_subagent",
    "assemble_subagent_parts",
]
