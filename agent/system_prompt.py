"""System-prompt facade for :class:`AIAgent`.

The prompt body is produced by :func:`agent.prompts.assembler.assemble_main`: this
module evaluates ``AssemblyInputs`` / ``SessionFacts`` from the live agent, calls the
assembler once, and keeps the persistence/cache contract — three ordered cache tiers
(``stable`` / ``context`` / ``volatile``), ``_cached_system_prompt_static`` and
``reconstruct_static_prefix``. No block list, ordering or prompt text lives here
(ADR-19 D4/D6).

``stable`` is the assembled body; ``context`` is the caller's ``system_message``;
``volatile`` is empty. A Bot Chat session's capability epoch rides outside the
assembled body as a frozen stamp so its capability refresh keeps working after the
bot-mode protocol section left the prompt (ADR-19 D5-A).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from agent.prompt_cache_scope import conversation_id
from agent.prompts.assembler import (
    AssemblyInputs,
    RenderContext,
    SessionFacts,
    assemble_main,
    assemble_subagent_parts,
)
from agent.prompts.platforms import resolve_platform_hint
from agent.prompts.project_context import effective_project_dir
from agent.runtime_cwd import resolve_context_cwd
from hermes_constants import get_default_hermes_root
from utils import is_truthy_value

logger = logging.getLogger(__name__)
PROMPT_SCHEMA = 2
_PROMPT_SCHEMA_KEY = "prompt_schema"
_PLUGIN_SECTION_FRAME_RE = re.compile(
    r"^## Plugin Context: (?P<id>[a-z0-9][a-z0-9._-]{0,127})\n<!-- hermes-plugin-section-chars:(?P<chars>[0-9]{1,4}) -->\n\n",
    re.MULTILINE,
)


def _plugin_session_info(agent: Any) -> dict[str, str]:
    """Return immutable-at-render-time metadata exposed to prompt sections."""
    try:
        cwd = str(resolve_context_cwd() or "")
    except Exception:
        cwd = ""
    info = {k: str(getattr(agent, k, None) or "") for k in ("session_id", "model", "provider", "platform")}
    info.update(profile_name=_active_profile_name(agent, _ambient_plugin_profile_name), cwd=cwd,
                conversation_id=conversation_id(agent))
    # The creating client's self-declared label ("" when none): plugin data only, never prompt text.
    from hermes_cli.plugin_session import session_client
    client = session_client() or {}
    info.update(client=client.get("name", ""), client_version=client.get("version", ""))
    return info


def _ambient_plugin_profile_name() -> str:
    from hermes_cli.profiles import get_active_profile_name
    return str(get_active_profile_name() or "default")


def _active_profile_name(agent: Any, ambient) -> str:
    """Profile name from the agent's OWN home, else *ambient()*; "default" on any
    failure. Ambient resolution misreports on threads that lost the HERMES_HOME
    ContextVar, which is why the agent's home is preferred."""
    try:
        home = _agent_home(agent)
        return _profile_name_for_home(home) if home is not None else ambient()
    except Exception:
        return "default"


def _restore_plugin_prompt_sections(prompt: str) -> tuple:
    """Recover frozen section bytes from the persisted full prompt.  Only the
    exact canonical container emitted by core is accepted — user/project text
    may resemble a frame."""
    from hermes_cli.plugins import (
        MAX_SYSTEM_PROMPT_SECTION_CHARS, PLUGIN_SECTIONS_END, PLUGIN_SECTIONS_START,
        RenderedPluginSystemPromptSection, format_system_prompt_sections,
    )
    start = prompt.rfind(PLUGIN_SECTIONS_START)
    end = prompt.find(PLUGIN_SECTIONS_END, start + len(PLUGIN_SECTIONS_START)) if start >= 0 else -1
    if end < 0:
        return ()
    after_end = end + len(PLUGIN_SECTIONS_END)
    if not prompt[after_end:].startswith("\n\nConversation started:"):
        return ()
    framed = prompt[start:after_end]
    restored = []
    for match in _PLUGIN_SECTION_FRAME_RE.finditer(framed):
        content_len = int(match.group("chars"))
        content = framed[match.end() : match.end() + content_len]
        if content_len > MAX_SYSTEM_PROMPT_SECTION_CHARS or len(content) != content_len:
            continue
        restored.append(RenderedPluginSystemPromptSection(id=match.group("id"), content=content,
                                                          position="after_memory", plugin="persisted-prompt"))
    return tuple(restored) if format_system_prompt_sections(restored) == framed else ()


def restore_plugin_prompt_sections(agent: Any, prompt: str) -> None:
    """Seed a resumed agent's frozen snapshot from persisted prompt bytes."""
    agent._plugin_system_prompt_sections_snapshot = _restore_plugin_prompt_sections(prompt)


def _agent_home(agent: Any) -> Optional[Path]:
    """The agent's OWN profile home, or None to use ambient resolution.

    A bound HERMES_HOME ContextVar override wins (the gateway multiplexes profiles over
    one shared session DB and binds the home per turn); else the parent of
    ``_session_db.db_path`` — ground truth on threads that lost the ContextVar, where
    ambient resolution would leak the launch profile.
    """
    try:
        from hermes_constants import get_hermes_home_override
        override = get_hermes_home_override()
        if override:
            return Path(override)
    except Exception:
        pass
    try:
        db_path = getattr(getattr(agent, "_session_db", None), "db_path", None)
        return Path(db_path).parent if db_path else None
    except Exception:
        return None


def _agent_skills_dir(agent: Any) -> Optional[Path]:
    """The agent's own ``<home>/skills`` dir, or None to use ambient home."""
    home = _agent_home(agent)
    return home / "skills" if home is not None else None


def _profile_name_for_home(home: Path) -> str:
    """``<root>/profiles/X`` -> ``"X"``; anything else -> ``"default"``.
    Uses ``get_default_hermes_root()`` (NOT ``get_hermes_home()``): on a bound
    profile session the ambient home IS the profile dir, so every profile
    would misreport as "default"."""
    try:
        rel = home.resolve().relative_to((get_default_hermes_root() / "profiles").resolve())
        return rel.parts[0] if rel.parts else "default"
    except (ValueError, OSError):
        return "default"


def _ambient_file_safety_profile_name() -> str:
    from agent.file_safety import _resolve_active_profile_name
    return _resolve_active_profile_name()


def _auto_load_skills(agent: Any) -> str:
    """``skills.auto_load`` blocks, resolved once per agent lifecycle (config, skill files and
    HERMES_IGNORE_RULES are read on the first build only) so the prompt stays byte-stable
    across model switches, compression and static-prefix restoration.

    Same gate as the skills block: nothing without the skills toolset, and nothing for agents that skip
    context files (delegate children, curator/review forks, gateway hygiene agents) — pinned skills are
    operator guidance for the user's session, not payload for every internal fork."""
    if getattr(agent, "skip_context_files", False) or not any(
            name in agent.valid_tool_names for name in ("skills_list", "skill_view", "skill_manage")):
        return ""
    if not getattr(agent, "_auto_load_skills_resolved", False):
        result: tuple[str, list[str], list[str]] = ("", [], [])
        try:
            if not is_truthy_value(os.environ.get("HERMES_IGNORE_RULES")):
                from agent.skill_commands import build_auto_load_prompt
                result = build_auto_load_prompt(task_id=getattr(agent, "session_id", None), home_override=_agent_home(agent))
            if result[2]:
                logger.warning("skills.auto_load: skill(s) not found or disabled, skipped: %s", ", ".join(result[2]))
        except Exception:
            logger.debug("skills.auto_load: injection skipped", exc_info=True)  # config errors never block session start
        agent._auto_load_skills_result = result
        agent._auto_load_skills_resolved = True
    return str(agent._auto_load_skills_result[0] or "")


def _bot_chat_epoch_stamp(agent: Any) -> str:
    """The Bot Chat capability epoch, or "" for every other session.

    It is not a prompt section: the bindings never show it to the model as guidance,
    but the restore path compares it so ``hermes tools enable/disable`` still rebuilds a
    canonical Bot Chat prompt once per capability change (ADR-19 D5-A carrier).
    """
    if not getattr(agent, "_bot_mode_protocol", True):
        return ""
    try:
        from tools.bot_mode_probe import BOT_CHAT_TITLE, epoch_line, get_bot_mode_protocol_section
        title = str(getattr(agent, "_session_title_hint", "") or "").strip()
        if not title:
            sdb, sid = getattr(agent, "_session_db", None), getattr(agent, "session_id", None)
            title = str((sdb.get_session_title(sid) if (sdb and sid) else None) or "").strip()
        home = _agent_home(agent)
        if title != BOT_CHAT_TITLE or not get_bot_mode_protocol_section(home):
            return ""
        return epoch_line(home)
    except Exception:  # health: allow BLE001 -- the epoch probe is optional; failure means no stamp
        return ""


def _context_length(agent: Any) -> Optional[int]:
    """Model context window scales the context-file caps; stable per conversation."""
    value = getattr(getattr(agent, "context_compressor", None), "context_length", None)
    return value if isinstance(value, int) and value > 0 else None


def _context_cwd(agent: Any):
    """TERMINAL_CWD when set (gateway); None lets discovery fall back to the launch dir.
    Desktop launch artifacts skip the session cwd but still honor the profile-scoped
    TERMINAL_CWD. A home cwd redirects to the profile's empty workspace (ADR-19 D10), so
    repo detection and context discovery never scan the user's home."""
    launch_artifact = getattr(agent, "_context_cwd_is_launch_artifact", False)
    cwd = resolve_context_cwd(include_session_override=not launch_artifact)
    return effective_project_dir(cwd) if cwd is not None else None


def _workspace_roots(agent: Any, cwd) -> tuple[str, ...]:
    """``/add-dir`` roots for the session, resolved from its store."""
    session_id, db = getattr(agent, "session_id", ""), getattr(agent, "_session_db", None)
    if db is None or not isinstance(session_id, str) or not session_id:
        return ()
    try:
        from agent.workspace_dirs import prompt_roots
        return tuple(str(root) for root in prompt_roots(session_id, db=db, cwd=cwd))
    except Exception:  # health: allow BLE001 -- additional roots must never block prompt build
        logger.debug("workspace roots skipped", exc_info=True)
        return ()


def _memory_provider_text(agent: Any) -> str:
    """External memory provider system prompt block (additive to built-in). Gated on the same check
    ``inject_memory_provider_tools`` uses so we never advertise provider tools the agent's toolset
    configuration has already gated off (#81014)."""
    manager = getattr(agent, "_memory_manager", None)
    if not manager:
        return ""
    try:
        from agent.memory_manager_tools import memory_provider_tools_exposed

        if not memory_provider_tools_exposed(agent):
            return ""
    except ImportError:
        pass
    except Exception:  # health: allow BLE001 -- provider gate probe is optional
        return ""
    try:
        return str(manager.build_system_prompt() or "")
    except Exception:
        logger.debug("memory provider prompt block skipped", exc_info=True)
        return ""


def _coding_instructions(agent: Any, cwd) -> str:
    """``agent.coding_instructions`` for the active coding posture, else ""."""
    if not getattr(agent, "valid_tool_names", None):
        return ""
    try:
        from agent.coding_context import resolve_runtime_mode

        mode = resolve_runtime_mode(
            platform=agent.platform or "",
            cwd=str(cwd) if cwd else None,
            model=agent.model or None,
        )
        return mode.instructions if mode.is_coding else ""
    except Exception:  # health: allow BLE001 -- posture detection must not block prompt assembly
        logger.debug("coding instructions skipped", exc_info=True)
        return ""


def _delegation_policy(agent: Any) -> dict[str, Any]:
    """The profile's delegation switches for section 09; defaults when config is unreadable."""
    try:
        from agent.prompts.delegation import resolve_delegation_policy

        definition = getattr(agent, "_agent_definition_metadata", None)
        return resolve_delegation_policy(definition=definition if isinstance(definition, dict) else None)
    except Exception:
        logger.warning("delegation policy resolution skipped", exc_info=True)
        return {}


def _render_context(agent: Any) -> RenderContext:
    """Evaluate every fact the renderer needs; called once per build.

    A run whose role block is registered (``_subagent_role``) renders 10/11 with the hand-off
    wording, exactly like an agent whose construction declared ``is_subagent``.
    """
    cwd = _context_cwd(agent)
    definition = getattr(agent, "_agent_definition_metadata", None)
    facts = SessionFacts(
        platform=agent.platform or "",
        model=agent.model or "",
        provider=agent.provider or "",
        session_id=str(getattr(agent, "session_id", "") or ""),
        context_length=_context_length(agent),
        context_cwd=str(cwd) if cwd else "",
        cwd_is_launch_artifact=bool(getattr(agent, "_context_cwd_is_launch_artifact", False)),
        skip_context_files=bool(getattr(agent, "skip_context_files", False)),
        workspace_roots=_workspace_roots(agent, cwd),
        toolsets=tuple(str(name) for name in (getattr(agent, "enabled_toolsets", None) or ())),
        memory_provider=_memory_provider_text(agent),
        user_profile_enabled=bool(getattr(agent, "_user_profile_enabled", True)),
        auto_load_skills=_auto_load_skills(agent),
        profile=_active_profile_name(agent, _ambient_file_safety_profile_name),
        delegation_policy=_delegation_policy(agent),
        append_prompt=_coding_instructions(agent, cwd),
        environment_probe=bool(getattr(agent, "_environment_probe", True)),
        agent_definition=definition if isinstance(definition, dict) else None,
    )
    subagent = bool(getattr(agent, "_subagent_role", None)) or bool(
        getattr(agent, "is_subagent", False)
    )
    return RenderContext(
        inputs=AssemblyInputs(
            tools=frozenset(agent.valid_tool_names or ()),
            subagent=subagent,
            memory=bool(getattr(agent, "_memory_store", None)),
        ),
        facts=facts,
        agent_name=str(getattr(agent, "agent_name", "") or "default"),
        home=_agent_home(agent),
        project_dir=Path(cwd) if cwd else None,
    )


def persisted_workspace_roots(prompt: str) -> list[str]:
    """The additional roots listed by a stored ``<workspace-roots>`` block ([] when absent).

    Used to decide whether a persisted prompt still names the session's roots after
    ``/add-dir``/``/remove-dir`` (``conversation_loop._stored_prompt_matches_runtime``).
    """
    marker = "<workspace-roots>"
    start = prompt.find(marker)
    if start < 0:
        return []
    body = prompt[start + len(marker):].split("</workspace-roots>", 1)[0]
    return [line.strip()[2:].strip() for line in body.splitlines() if line.strip().startswith("- ")]


def platform_hint(agent: Any) -> str:
    """The current surface's prompt hint, resolved without a prompt rebuild.

    The system prompt carries its own copy for the surface it was built on; a later
    surface switch delivers this one on the per-turn note channel (``agent.surface_switch``).
    """
    overrides = getattr(agent, "_platform_hint_overrides", None)
    return resolve_platform_hint(
        agent.platform or "",
        overrides=overrides if isinstance(overrides, dict) else {},
        tool_names=tuple(getattr(agent, "valid_tool_names", ()) or ()),
    )


def build_system_prompt_parts(agent: Any, system_message: Optional[str] = None) -> dict[str, str]:
    """Assemble the system prompt through the assembler.

    A run carrying a ``_subagent_role`` puts the shared base in ``stable`` and its
    ``§ Subagent Role`` block in ``context``: joined by the caller, the bytes equal
    :func:`assemble_subagent`, while ``_cached_system_prompt_static`` stays the literal
    prefix every child of a batch shares. Every other agent keeps ``stable`` = the assembled
    body and ``context`` = the caller's ``system_message``. ``volatile`` is always empty.
    Never re-rendered mid-session.
    """
    ctx = _render_context(agent)
    role = getattr(agent, "_subagent_role", None)
    if role is not None:
        body, context = assemble_subagent_parts(ctx, role)
    else:
        body, context = assemble_main(ctx), system_message if system_message is not None else ""
    stamp = _bot_chat_epoch_stamp(agent)
    if stamp:
        body = f"{body}\n\n{stamp}" if body else stamp
    return {"stable": body, "context": context, "volatile": ""}


def _stamp_prompt_schema(agent: Any) -> None:
    """Record ``prompt_schema: 2`` on the session row with the assembler-produced bytes.

    Stored prompts without the marker are legacy bytes and replay verbatim (ADR-19 D7.4); a
    rebuilt prompt is this schema, so the marker is only written on a real build.
    """
    db, session_id = getattr(agent, "_session_db", None), getattr(agent, "session_id", None)
    writer = getattr(db, "patch_session_model_config", None)
    if writer is None or not isinstance(session_id, str) or not session_id:
        return
    try:
        writer(session_id, {_PROMPT_SCHEMA_KEY: PROMPT_SCHEMA})
    except Exception:  # health: allow BLE001 -- a marker write must not break prompt persistence
        logger.debug("prompt_schema stamp failed", exc_info=True)


def build_system_prompt(agent: Any, system_message: Optional[str] = None) -> str:
    """Assemble the full prompt; cached on ``agent._cached_system_prompt`` and
    only rebuilt after compression. The assembled bytes stay one prefix so implicit
    longest-prefix caches keep the unchanged scaffold."""
    parts = build_system_prompt_parts(agent, system_message=system_message)
    agent._cached_system_prompt_static = parts["stable"]
    _stamp_prompt_schema(agent)
    # Surface context-file truncation warnings in chat, not only in logs.
    from agent.prompt_builder import drain_truncation_warnings
    for warning in drain_truncation_warnings():
        agent._emit_diagnostic_status(warning)
    return "\n\n".join(p for p in (parts["stable"], parts["context"], parts["volatile"]) if p)


def invalidate_system_prompt(agent: Any) -> None:
    """Force a rebuild on the next turn (after compression): reload memory from
    disk and clear the frozen plugin snapshot (previous bytes stashed as the
    fail-open fallback) so plugins re-render at the same boundary.

    Called after context compression events. Also reloads memory from disk so the rebuilt prompt captures
    any writes from this session, and clears the frozen plugin-section snapshot so plugins re-render at the
    same boundary (maintainer-directed, #95681 arc): a plugin section is just another prompt block carrying
    state — freezing it while memory, skills, and guidance refresh would recreate the stale-block disease
    inside plugin-land. The previous bytes are stashed so a plugin whose render RAISES falls back to its
    last good section instead of vanishing (fail-open guard, not a freeze).
    """
    agent._cached_system_prompt = None
    agent._cached_system_prompt_static = None
    if hasattr(agent, "_plugin_system_prompt_sections_snapshot"):
        agent._plugin_system_prompt_sections_previous = agent._plugin_system_prompt_sections_snapshot
        del agent._plugin_system_prompt_sections_snapshot
    if agent._memory_store:
        agent._memory_store.load_from_disk()


def reconstruct_static_prefix(agent: Any, system_message: Optional[str] = None, *, log_label: str = "restore") -> None:
    """Reconstruct ``_cached_system_prompt_static`` for a stored prompt.
    Only the full prompt is persisted, so restore / keep-prompt compression /
    mid-turn failover to a cache-on provider must rebuild the stable tier to
    regain the ``[static, volatile]`` layout.  The rebuilt tier is used ONLY
    when the stored prompt literally starts with it; otherwise static stays
    None and the stored bytes are sent untouched.  A failed rebuild is memoized
    per stored prompt so the retry-loop hot path doesn't redo the file I/O."""
    stored = getattr(agent, "_cached_system_prompt", None)
    existing = getattr(agent, "_cached_system_prompt_static", None)
    if (
        not getattr(agent, "_use_prompt_caching", False)
        or not isinstance(stored, str) or not stored
        or (isinstance(existing, str) and existing and stored.startswith(existing))
        or getattr(agent, "_static_rebuild_failed_for", None) == stored
    ):
        return
    try:
        static = build_system_prompt_parts(agent, system_message=system_message)["stable"]
        if static and stored.startswith(static):
            agent._cached_system_prompt_static = static
            agent._static_rebuild_failed_for = None
            return
    except Exception:
        logger.debug("static system-prefix reconstruction failed on %s", log_label, exc_info=True)
    agent._cached_system_prompt_static = None
    agent._static_rebuild_failed_for = stored


def format_tools_for_system_message(agent: Any) -> str:
    """JSON tool definitions in the trajectory format."""
    if not agent.tools:
        return "[]"
    return json.dumps([{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                        "parameters": t["function"].get("parameters", {}),
                        "required": None}  # Match the format in the example
                       for t in agent.tools], ensure_ascii=False)


__all__ = [
    "build_system_prompt",
    "build_system_prompt_parts",
    "format_tools_for_system_message",
    "invalidate_system_prompt",
    "platform_hint",
    "restore_plugin_prompt_sections",
]
