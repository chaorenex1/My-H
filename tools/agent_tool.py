"""``agent``: one named in-process agent, with its conversation on disk.

A caller supplies the agent's name, prompts, optional context files and the knobs the host
normally decides. The run uses the host's model route, the requested toolsets (the
default-loaded built-in sets when ``tool_sets`` is omitted), and the host's own one-shot
runner (:func:`agent.agent_run.run`).
A named agent starts as an isolated peer: no host memory, no project context files and no
operator soul, matching omp's blank subagent. Those are the flags the caller can turn back on.

Every run is one agent session with its own native history file::

    <HERMES_HOME>/sessions/<agent_name>/<agent_session_id>.jsonl

The tool mints ``agent_session_id`` and its unique ``subject`` and returns them; the transcript,
the ``agent_session`` row and ``agent_out`` are all keyed on that id. Runs never continue each
other implicitly: a caller that wants the previous conversation passes ``continue_from`` with its
``agent_session_id``.

By default the agent runs in the background: the tool returns a job handle immediately and the
finished run is pushed onto the host's background-completion queue
(``process_registry.completion_queue``), the same queue a background process finishes on, so the
calling session is woken with an ``[ASYNC DELEGATION COMPLETE]`` turn. ``background: false``
keeps the blocking form and returns the answer in the payload. Runs execute on the shared
``agent_loop`` pool (``agent.agent_loop_service``): at most ``agent_loop.max_workers`` threads
run at once and at most ``agent_loop.max_queued`` wait.

The ``agent`` tool owns new delegation work: a named agent (or a ``tasks[]`` batch) carries its
own prompt, tools, model, JSONL history, background/resident lifecycle and ``agent_out``
artifacts, and can be resumed with ``continue_from``. ``delegate_task`` remains for its live
``action=list/steer/stop`` control and legacy batch callers; new work uses ``agent``.

Every named run is also given the host-side ``yield`` completion tool
(:mod:`tools.agent_yield_tool`): ``final=true`` ends the turn with its data, ``final=false``
records an incremental output-schema section, and stopping without ``yield`` keeps the final
assistant message as the handoff. The tool is per-run and never enters the global toolset.

The system prompt is a lookup, not only an argument: the first readable prompt file is the
agent's definition, and ``system_prompts`` is the last fallback. Directories are scanned in this
order (each ``<name>.md`` or ``<name>``):

1. the project — ``.agents/``, ``agents/``, ``skills/*/agents/``,
   ``.hermes/plugins/*/agents/``;
2. the profile (``HERMES_HOME``) — ``.agents/``, ``agents/``, ``skills/*/agents/``,
   ``plugins/*/agents/``;
3. the converted bundled set — ``skills/agent-presets/agents/`` (``HERMES_BUNDLED_AGENTS``);
4. the installation (the tree Hermes runs from) — ``.agents/``, ``agents/``,
   ``skills/*/agents/`` (bundled skills), ``plugins/*/agents/`` (bundled plugins).

A leading YAML front-matter block is an enforced contract (``tools/agent_definitions.py``): it may
pin ``model`` (``provider:model``, a bare name or ``@role``), ``toolsets``, ``enable_tools``,
``max_iterations``, ``reasoning_effort``, ``worktree`` and the skip flags. It may also declare
``output`` (an omp JTD-shaped or JSON Schema output contract, normalized to JSON Schema and
validated with one bounded retry), ``spawns`` (the named agents this definition may derive, ``*``
or a list) and ``blocking`` (the parent waits for this agent even when async execution is
available). Precedence per call is
caller argument, then definition, then host default; the block itself is never sent to the model.
The body is scanned with the same context-scope threat patterns a context file gets: a project
definition that matches is blocked (an operator-owned profile or installation definition only
warns). ``worktree: true`` (or the call argument) runs the agent in its own git worktree, which
is inspected, pruned or archived after the run.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

import hermes_session_paths as session_paths
from agent import agent_loop_service, agent_run, run_artifacts, run_transcript
from agent.skill_utils import find_project_root
from hermes_constants import get_hermes_home
from tools import agent_definitions
from tools.agent_context_inputs import (
    MAX_CONTEXT_FILE_BYTES,
    ContextInput,
    read_context_files,
    resolve_path,
)
from tools.agent_yield_tool import YieldState, build_yield_tool
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

MAX_AGENT_NAME_CHARS = 64
MAX_SHORT_NAME_CHARS = 32
MAX_DEPTH = 3
#: One batch call is one model turn; the pool queues the excess (``agent_loop.max_queued``).
MAX_BATCH_TASKS = 10
#: omp spells coarse effort as lo/med/hi; Hermes reasoning efforts use the full names.
_EFFORT_ALIASES = {"lo": "low", "med": "medium", "hi": "high"}
#: The completion notification carries the answer only up to this many characters; the transcript
#: path is appended above it, because the host renders ``summary`` verbatim.
MAX_NOTIFICATION_CHARS = 2000
#: The rendered goal line (``Original goal: …``) is a label, not the prompt.
MAX_GOAL_CHARS = 120

#: An agent may itself call ``agent`` when it holds the tool. The depth is a ContextVar, and
#: the pool copies the submitter's context, so a nested run sees the parent's depth.
_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("agent_tool_depth", default=0)

#: The running definition's ``spawns`` allow-list (``None`` = no definition, so no policy).
#: Set around the run, so a nested ``agent`` call inside it is checked at *its* validation time.
_SPAWNS: contextvars.ContextVar[Optional[frozenset[str]]] = contextvars.ContextVar(
    "agent_tool_spawns", default=None)

_TRUE_FLAGS = frozenset({"1", "true", "yes", "on"})
_FALSE_FLAGS = frozenset({"0", "false", "no", "off"})

#: Buffered batch completions (``independent_completions=false``): each child reports into
#: its batch; the last one emits one combined event.
_BATCH_LOCK = threading.Lock()
_BATCHES: dict[str, dict[str, Any]] = {}


# -- argument parsing ----------------------------------------------------------


def _text(value: Any) -> str:
    """A string argument; a list is joined with blank lines (models sometimes send lists)."""
    if isinstance(value, (list, tuple)):
        return "\n\n".join(str(part).strip() for part in value if str(part).strip())
    return str(value or "").strip()


def _flag(args: dict[str, Any], name: str, default: Optional[bool] = None) -> Optional[bool]:
    """A boolean argument; omitted stays ``None`` so a definition's value can apply."""
    value = (args or {}).get(name)
    if value is None:
        return default
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_FLAGS:
            return True
        if token in _FALSE_FLAGS:
            return False
        raise ValueError(f"{name} must be a boolean, got {value[:40]!r}")
    return bool(value)


def _segment(value: Any, label: str) -> str:
    """One path-free name: an agent name or a skill folder, never a path."""
    name = _text(value)
    if len(name) > MAX_AGENT_NAME_CHARS:
        raise ValueError(f"{label} must be at most {MAX_AGENT_NAME_CHARS} characters")
    if name in {".", ".."} or any(char in name for char in ("/", "\\", "\x00")):
        raise ValueError(f"{label} must not contain path separators or NUL bytes")
    return name


#: A short name is an addressing token, not a label: one CamelCase word, no separators.
_SHORT_NAME = re.compile(
    rf"[A-Za-z][A-Za-z0-9]{{0,{MAX_SHORT_NAME_CHARS - 1}}}")


def _short_name(value: Any) -> str:
    """Validate and return a caller-supplied short name; empty when omitted."""
    name = _text(value)
    if not name:
        return ""
    if not _SHORT_NAME.fullmatch(name):
        raise ValueError(
            f"name must be CamelCase letters/digits, start with a letter, and be at most "
            f"{MAX_SHORT_NAME_CHARS} characters, got {name[:40]!r}")
    return name


def _generated_short_name(agent_name: str) -> str:
    """``<AgentName><4hex>``: the default address for a run whose caller named none."""
    base = re.sub(r"[^A-Za-z0-9]", "", agent_name) or "Agent"
    if not base[0].isalpha():
        base = f"Agent{base}"
    return f"{base[:MAX_SHORT_NAME_CHARS - 4]}{uuid.uuid4().hex[:4]}"


def _registered_tool_names() -> frozenset[str]:
    """Every currently registered tool name; discovery first, so a lazy import cannot hide one."""
    from tools.registry import discover_builtin_tools

    discover_builtin_tools()
    return frozenset(registry.get_all_tool_names())


def _agent_name(args: dict[str, Any]) -> str:
    name = _segment(args.get("agent_name"), "agent_name")
    if not name:
        raise ValueError("agent_name is required")
    return name


def _skill_name(args: dict[str, Any]) -> str:
    """The optional skill folder the ``skills/<skill_name>/agents`` directories use."""
    return _segment(args.get("skill_name"), "skill_name")


#: ``spawn:@<agent_name>``: one path-free name, so a directive can never address a file.
_SPAWN_HEADER = re.compile(r"spawn:@([A-Za-z0-9][A-Za-z0-9_.-]{0,63})")


def _parse_spawn_directive(value: Any) -> tuple[str, str]:
    """``spawn:@<name>`` + body → ``(agent_name, body)``; the body is required.

    The header is the whole first line; an optional blank line separates it from the assignment.
    Only syntax is parsed here: authorization is ``_check_spawn_allowed`` at validation time.
    """
    raw = _text(value)
    if not raw:
        raise ValueError("spawn requires 'spawn:@<agent_name>' followed by the task body")
    first, _, rest = raw.partition("\n")
    match = _SPAWN_HEADER.fullmatch(first.strip())
    if not match:
        raise ValueError(
            "spawn must start with 'spawn:@<agent_name>' on the first line, then the task body")
    body = rest.strip()
    if not body:
        raise ValueError("spawn needs a task body after the 'spawn:@<agent_name>' line")
    return match.group(1), body


def _model_route(raw: Any) -> Any:
    """Resolve one ``model`` selector (``provider:model``, bare name or ``@role``)."""
    from hermes_cli.model_roles import resolve_model_selector

    return resolve_model_selector(raw)


def _spawn_directive_enabled() -> bool:
    """``delegation.spawn_directive`` rollout switch; unreadable config keeps it on."""
    try:
        from hermes_cli.config import cfg_get, load_config_readonly

        value = cfg_get(load_config_readonly(), "delegation", "spawn_directive", default=True)
    except Exception:  # health: allow BLE001 -- config must not decide whether the tool loads
        logger.debug("spawn directive: config unreadable", exc_info=True)
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return value is not False


def _yield_tool_enabled() -> bool:
    """``agent_sessions.yield_tool`` rollout switch; unreadable config keeps the tool on."""
    try:
        from agent.agent_session_runtime import session_limits

        return bool(session_limits().get("yield_tool", True))
    except Exception:  # health: allow BLE001 -- the per-run tool is an enhancement, not a gate
        logger.debug("yield tool: config unreadable", exc_info=True)
        return True


def _tool_sets(args: dict[str, Any]) -> tuple[Optional[list[str]], bool]:
    """``(None, False)`` when omitted (a definition may supply toolsets); ``([], True)`` means all."""
    raw = args.get("tool_sets")
    if raw is None:
        return None, False
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("tool_sets must be an array of toolset names")
    names = [str(name).strip() for name in raw if str(name).strip()]
    if not names or "*" in names:
        return [], True
    return names, False


def _project(project_dir: Any) -> Optional[Path]:
    raw = str(project_dir or "").strip()
    if raw:
        root = Path(os.path.expanduser(raw))
        if not root.is_dir():
            raise ValueError(f"project_dir is not a usable directory: {raw}")
        return Path(os.path.realpath(root))
    return find_project_root()


# -- prompt and context resolution ---------------------------------------------


def _install_roots() -> tuple[Path, Path, Path, Path]:
    """``(install tree, bundled skills, bundled plugins, bundled agents)``.

    One seam for the four resolvers so a packaged/Nix install (``HERMES_INSTALL_ROOT``,
    ``HERMES_BUNDLED_SKILLS``, ``HERMES_BUNDLED_PLUGINS``, ``HERMES_BUNDLED_AGENTS``) is
    followed rather than inferred from this file's location.
    """
    from hermes_cli.plugins import get_bundled_plugins_dir
    from hermes_constants import get_bundled_agents_dir, get_bundled_skills_dir
    from pm.paths import install_root

    root = install_root()
    skills_root = get_bundled_skills_dir(root / "skills")
    return (root, skills_root, get_bundled_plugins_dir(),
            get_bundled_agents_dir(skills_root / "agent-presets" / "agents"))


def _plugin_agent_dirs(plugins_root: Path) -> list[Path]:
    """``<plugins>/<name>/agents`` for every installed plugin, sorted for a stable scan."""
    try:
        children = sorted(plugins_root.iterdir())
    except OSError:
        return []
    return [child / "agents" for child in children
            if child.is_dir() and not child.name.startswith((".", "_"))]


def _skill_agent_dirs(skills_root: Path, skill_name: str) -> list[Path]:
    """``<skills>/<name>/agents`` for the named skill first, then every other skill."""
    try:
        children = sorted(skills_root.iterdir())
    except OSError:
        return []
    names = [child for child in children
             if child.is_dir() and not child.name.startswith((".", "_"))]
    if skill_name:
        names.sort(key=lambda child: child.name != skill_name)
    return [child / "agents" for child in names]


def _base_agent_dirs(base: Path, skill_name: str, plugins_rel: str) -> list[Path]:
    """The definition directories one root contributes, in scan order."""
    return ([base / ".agents", base / "agents"]
            + _skill_agent_dirs(base / "skills", skill_name)
            + _plugin_agent_dirs(base / plugins_rel))


def _prompt_scopes(project: Optional[Path],
                   skill_name: str) -> list[tuple[Path, str]]:
    """``(directory, trust scope)`` in scan order, deduplicated.

    Project first, then the operator's profile, then the converted bundled presets, the
    installation and its bundled skills and plugins: a definition the project owns always beats a
    shipped one with the same name. The scope decides how a poisoned definition body is handled
    (project blocks, operator warns).
    """
    scopes: list[tuple[Path, str]] = []
    for base in [base for base in (project, Path.cwd()) if base is not None]:
        scopes += [(directory, "project")
                   for directory in _base_agent_dirs(base, skill_name, ".hermes/plugins")]
    home = get_hermes_home()
    scopes += [(directory, "profile")
               for directory in _base_agent_dirs(home, skill_name, "plugins")]
    root, bundled_skills, bundled_plugins, bundled_agents = _install_roots()
    scopes += [(bundled_agents, "installation")]
    scopes += [(root / ".agents", "installation"), (root / "agents", "installation")]
    scopes += [(directory, "installation")
               for directory in _skill_agent_dirs(bundled_skills, skill_name)]
    scopes += [(directory, "installation")
               for directory in _plugin_agent_dirs(bundled_plugins)]
    ordered: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for directory, scope in scopes:
        key = os.path.normcase(str(directory))
        if key in seen:
            continue
        seen.add(key)
        ordered.append((directory, scope))
    return ordered


def _prompt_dirs(project: Optional[Path], skill_name: str) -> list[Path]:
    """Agent-definition directories in scan order (see :func:`_prompt_scopes`)."""
    return [directory for directory, _scope in _prompt_scopes(project, skill_name)]


def discover_agent_definitions(project_dir: Optional[str | Path] = None) -> list[dict[str, Any]]:
    """Every named-agent definition visible to this process, in ``_prompt_scopes`` order.

    Project first (including the process cwd), then the profile (``HERMES_HOME``), then the
    installation. Each row carries the prompt path, the scope label and the frontmatter's
    model/provider/reasoning effort; the first definition for a name wins.
    """
    project = Path(project_dir).expanduser() if project_dir else None
    seen: set[str] = set()
    found: list[dict[str, Any]] = []
    for directory, scope in _prompt_scopes(project, ""):
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for path in entries:
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix not in ("", ".md"):
                continue
            name = path.stem if path.suffix == ".md" else path.name
            if not name or name in seen:
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                continue
            try:
                metadata, body = agent_definitions.parse_definition(text, source=str(path))
            except agent_definitions.DefinitionError as exc:
                logger.warning("agent: definition %s skipped: %s", path, exc)
                continue
            if not body:
                continue
            seen.add(name)
            route = str(metadata.get("model") or "")
            provider, sep, model = route.partition(":")
            if not sep or not provider.strip() or not model.strip():
                provider, model = "", route
            found.append({
                "agent_id": name,
                "name": str(metadata.get("name") or name),
                "description": str(metadata.get("description") or ""),
                "source": str(path),
                "scope": scope,
                "model": model.strip() or None,
                "provider": provider.strip() or None,
                "thinking": str(metadata.get("reasoning_effort") or "") or None,
                "toolsets": list(metadata.get("toolsets") or []),
            })
    return found


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except OSError:
        return False


def _bundled_definition_paths(agent_name: str, skill_name: str) -> list[Path]:
    """Definition files the shipped installation provides under *agent_name*."""
    root, bundled_skills, bundled_plugins, bundled_agents = _install_roots()
    dirs = ([bundled_agents, root / ".agents", root / "agents"]
            + _skill_agent_dirs(bundled_skills, skill_name)
            + _plugin_agent_dirs(bundled_plugins))
    return [path for directory in dirs if (path := _prompt_path(directory, agent_name))]


@dataclass(frozen=True)
class _Definition:
    """The resolved definition: its prompt body, where it came from, and its metadata."""

    body: str
    source: str
    shadowed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)



def _prompt_path(directory: Path, agent_name: str) -> Optional[Path]:
    for candidate in (directory / f"{agent_name}.md", directory / agent_name):
        if candidate.is_file():
            return candidate
    return None


def _system_prompt(project: Optional[Path], agent_name: str, skill_name: str,
                   argument: str) -> _Definition:
    """The first usable definition; the argument is the last fallback.

    Trust follows the directory: a *project* definition is cloned code, so a context-scope
    injection match blocks that file and the scan continues; a *profile* or *installation*
    definition is operator-owned, so a match only warns. A definition that shadows a shipped
    persona is reported (``shadowed``), because the fixed name makes it a worthwhile target.
    """
    blocked: list[str] = []
    bundled = _bundled_definition_paths(agent_name, skill_name)
    for directory, scope in _prompt_scopes(project, skill_name):
        path = _prompt_path(directory, agent_name)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            logger.warning("agent: prompt %s unreadable: %s", path, exc)
            continue
        metadata, body = agent_definitions.parse_definition(text, source=str(path))
        if not body:
            continue
        findings = agent_definitions.body_threats(body)
        if findings:
            detail = ", ".join(findings)
            if scope == "project":
                logger.warning("agent: project definition %s blocked: %s", path, detail)
                blocked.append(f"{path} ({detail})")
                continue
            logger.warning("agent: definition %s matched injection pattern(s) %s; loaded "
                           "anyway because it is operator-owned — review it if you did not "
                           "write it", path, detail)
        shadowed = bool(bundled) and not any(_same_path(path, shipped) for shipped in bundled)
        if shadowed:
            logger.warning("agent: definition %s shadows bundled %s", path, bundled[0])
        return _Definition(body=body, source=str(path), shadowed=shadowed, metadata=metadata)
    if blocked and not argument:
        raise agent_definitions.DefinitionError(
            "agent definition blocked (prompt-injection patterns): " + "; ".join(blocked))
    return _Definition(body=argument, source=("argument" if argument else ""))


def _context_files(args: dict[str, Any],
                   project: Optional[Path]) -> tuple[str, tuple[ContextInput, ...]]:
    """Read requested files; a path outside the project/profile roots is refused, not read."""
    return read_context_files(args.get("context_files"), project=project)


# -- notifications -------------------------------------------------------------


def _task_file_prompt(raw: Any, project: Optional[Path]) -> str:
    """Read a batch/single prompt from a file inside the allowed roots; '' when absent."""
    name = _text(raw)
    if not name:
        return ""
    path = resolve_path(name, project=project)
    size = path.stat().st_size
    if size > MAX_CONTEXT_FILE_BYTES:
        raise ValueError(f"{name}: file is larger than {MAX_CONTEXT_FILE_BYTES} bytes")
    data = path.read_bytes()
    if b"\x00" in data:
        raise ValueError(f"{name}: binary files are not accepted as a task")
    return data.decode("utf-8", "replace").strip()


def _goal(prompt: str) -> str:
    """The first line of the prompt, short enough to sit on one rendered row."""
    line = next((part.strip() for part in (prompt or "").splitlines() if part.strip()), "")
    return line if len(line) <= MAX_GOAL_CHARS else line[:MAX_GOAL_CHARS - 1] + "…"


def _summary(text: str, path: Optional[Path]) -> str:
    """The answer as the notification body; over-long answers point at the transcript instead."""
    body = (text or "").strip()
    if len(body) <= MAX_NOTIFICATION_CHARS:
        return body
    where = f"full answer: {path}" if path is not None else "no transcript was written"
    return f"{body[:MAX_NOTIFICATION_CHARS]}\n\n…({len(body)} characters total; {where})"


def _notification_queue() -> Any:
    """The host's background-completion queue, imported here so a partial host stays importable."""
    from tools.process_registry import process_registry

    return process_registry.completion_queue


@dataclass
class _Run:
    """One accepted run: everything the calling thread resolved, ready for a worker thread."""

    agent: str
    session_key: str
    agent_session_id: str
    subject: str
    #: Profile-unique short name (``agent_message``/``agent_out`` address it); never empty.
    short_name: str
    user_prompt: str
    system_prompt: str
    prompt_source: str
    context: str
    context_count: int
    #: What the supplied context files were, durably described for ``agent_out``.
    context_inputs: tuple[ContextInput, ...]
    provider_name: str
    model_name: str
    toolsets: list[str]
    all_tools: bool
    project_dir: str
    path: Path
    goal: str
    depth: int
    enable_tools: list[str]
    max_iterations: int
    reasoning_effort: Any
    worktree: bool
    shadowed: bool
    flags: dict[str, bool] = field(default_factory=dict)
    model_role: str = ""
    model_selector: str = ""
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    fallback_chain: tuple[dict[str, str], ...] = ()
    output_schema: Optional[dict[str, Any]] = None
    schema_mode: str = "permissive"
    autoload_skills: tuple[str, ...] = ()
    advisor: Any = None
    read_summarize: Optional[bool] = None
    #: Parsed definition front matter; the child's prompt policy reads it back per agent.
    definition_metadata: dict[str, Any] = field(default_factory=dict)
    #: Resident-session rebuild spec (ADR-15 D5/D6); survives a park or a process restart.
    spec: dict[str, Any] = field(default_factory=dict)
    keep_alive: bool = False
    #: Batch coordination (``tasks[]``): id, expected size, and whether each child reports alone.
    batch_id: str = ""
    batch_size: int = 0
    batch_independent: bool = True
    #: The live agent, once built — handed to the resident runtime in ``on_agent``.
    live: dict[str, Any] = field(default_factory=dict)
    #: Session bundle this run's artifacts land in (ADR-16 D4), and its own archive namespace.
    bundle: Optional[Path] = None
    tool_dir: Optional[Path] = None
    #: The collision-free artifact stem chosen at creation (``<topic>`` in every file name).
    topic: str = ""


def _payload_dict(value: Any) -> dict[str, Any]:
    to_dict = getattr(value, "as_dict", None)
    return to_dict() if callable(to_dict) else {}


def _new_agent_session_id(agent_name: str) -> str:
    """One agent session = one run = one transcript file; the id is minted here, never reused."""
    return f"{agent_name}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _claim_short_name(requested: str, agent_name: str) -> tuple[str, str]:
    """``(short_name, error)``: a caller's duplicate is refused; a generated one retries.

    Profile uniqueness is what makes a short name a usable address for ``agent_message`` and
    ``agent_out``. An unreadable store is not a refusal: the run stands and the row insert is
    already best-effort.
    """
    try:
        from hermes_state_registry import acquire, release_or_close

        db = acquire()
    except Exception:  # health: allow BLE001 -- a run must not depend on the store
        logger.debug("agent: no session store for short name", exc_info=True)
        return requested or _generated_short_name(agent_name), ""
    try:
        if requested:
            if db.get_agent_session_by_name(requested) is not None:
                return requested, f"name already in use: {requested}"
            return requested, ""
        for _ in range(8):
            candidate = _generated_short_name(agent_name)
            if db.get_agent_session_by_name(candidate) is None:
                return candidate, ""
        return _generated_short_name(agent_name), "could not allocate a unique name"
    except Exception:  # health: allow BLE001 -- lookup failure leaves the generated name
        logger.debug("agent: short name lookup failed for %s", agent_name, exc_info=True)
        return requested or _generated_short_name(agent_name), ""
    finally:
        release_or_close(db)


def _preflight_short_names(calls: list[tuple[dict[str, Any], _Call]]) -> None:
    """Refuse duplicate or already-used names before the first spawn in a batch."""
    requested = [call.name for _item, call in calls if call.name]
    if not requested:
        return
    if len(set(requested)) != len(requested):
        duplicate = next(name for name in requested if requested.count(name) > 1)
        raise ValueError(f"duplicate name in this call: {duplicate}")
    try:
        from hermes_state_registry import acquire, release_or_close

        db = acquire()
    except Exception:  # health: allow BLE001 -- no store means no row can collide
        logger.debug("agent: no session store for short-name preflight", exc_info=True)
        return
    try:
        for name in requested:
            if db.get_agent_session_by_name(name) is not None:
                raise ValueError(f"name already in use: {name}")
    finally:
        release_or_close(db)


def _begin_session_row(agent_session_id: str, run: _Run) -> str:
    """Insert the row before dispatch so the id the tool returns resolves immediately."""
    try:
        from hermes_state_registry import acquire, release_or_close

        db = acquire()
    except Exception:  # health: allow BLE001 -- a run must not depend on the store
        logger.debug("agent: no session store for agent_session", exc_info=True)
        return run.subject
    try:
        return db.begin_agent_session(
            agent_session_id=agent_session_id, agent_name=run.agent, subject=run.subject,
            transcript_path=str(run.path), parent_session_id=run.session_key,
            short_name=run.short_name,
            model_role=run.model_role, metadata=run.spec or None)
    except Exception:  # health: allow BLE001 -- the run stands; only the lookup row is lost
        logger.warning("agent: agent_session row was not written for %s",
                       agent_session_id, exc_info=True)
        return run.subject
    finally:
        release_or_close(db)


def _record_transcript_path(agent_session_id: str, path: Path) -> None:
    """Persist the actual bundle file on the row (ADR-16 D4 authoritative mapping)."""
    try:
        from hermes_state_registry import acquire, release_or_close

        db = acquire()
    except Exception:  # health: allow BLE001 -- the run stands without the row
        logger.debug("agent: no store for transcript path", exc_info=True)
        return
    try:
        db.set_agent_session_transcript_path(agent_session_id, str(path))
    except Exception:  # health: allow BLE001 -- a missing update only loses the mapping
        logger.warning("agent: transcript path not recorded for %s", agent_session_id,
                       exc_info=True)
    finally:
        release_or_close(db)


def _finalize_run_paths(agent_session_id: str, run: _Run) -> _Run:
    """Name the transcript/archive after the *stored* subject, not the requested one.

    A duplicate subject gains an `` (N)`` suffix inside ``begin_agent_session``; the file names
    must follow the stored subject (ADR-16 D4) or two runs could share a topic.
    """
    if run.bundle is None:
        return run
    topic = session_paths.allocate_topic(run.bundle, run.subject,
                                         discriminator=agent_session_id)
    path = session_paths.transcript_path(run.bundle, run.subject, topic=topic)
    tool_dir = session_paths.subagent_dir(run.bundle, run.subject, topic=topic)
    if str(path) != str(run.path):
        _record_transcript_path(agent_session_id, path)
    return replace(run, path=path, tool_dir=tool_dir, topic=topic)


def _settle(run: _Run, value: Any, error: Optional[BaseException]) -> tuple[str, str]:
    """Record the settled row and return ``(status, body)``. Never raises."""
    payload = _payload_dict(value) if value is not None else {}
    if error is not None:
        status = "stalled" if isinstance(error, TimeoutError) else "error"
        body = str(error)
    else:
        status = "completed"
        body = str(payload.get("answer") or "")
    sources = tuple(getattr(value, "summary_sources", ()) or ()) if value is not None else ()
    manifest: Optional[dict[str, Any]] = None
    try:
        manifest = run_artifacts.settle_bundle(run, status, body, sources=sources)
    except Exception:  # health: allow BLE001 -- the run already happened
        logger.warning("agent: bundle artifacts were not written for %s",
                       run.agent_session_id, exc_info=True)
    try:
        from hermes_state_registry import acquire, release_or_close

        db = acquire()
    except Exception:  # health: allow BLE001 -- a run must not depend on the store
        logger.debug("agent: no session store to settle %s", run.agent_session_id, exc_info=True)
        return status, body
    try:
        db.finish_agent_session(
            run.agent_session_id, status=status, final_response=body,
            transcript_path=str(payload.get("transcript_path") or run.path),
            artifact_manifest=manifest)
    except Exception:  # health: allow BLE001 -- the run already happened
        logger.warning("agent: agent_session row was not settled for %s",
                       run.agent_session_id, exc_info=True)
    finally:
        release_or_close(db)
    return status, body


def _continued_history(args: dict[str, Any], agent_name: str) -> list[dict[str, Any]]:
    """The history an explicit ``continue_from`` names; an unknown id is a refused call."""
    source = _text(args.get("continue_from"))
    if not source:
        return []
    rows = run_transcript.read_history(agent_name, source)
    if not rows:
        raise ValueError(f"continue_from not found: {source}")
    return rows


def _begin_batch(batch_id: str, size: int, session_key: str) -> None:
    """Open a buffered batch; each child's completion decrements it."""
    with _BATCH_LOCK:
        _BATCHES[batch_id] = {"remaining": size, "events": [], "session_key": session_key}


def _batch_completion(batch_id: str, event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Record one child event; return the combined event when the last child lands."""
    with _BATCH_LOCK:
        state = _BATCHES.get(batch_id)
        if state is None:
            return event
        state["events"].append(event)
        state["remaining"] -= 1
        if state["remaining"] > 0:
            return None
        events = state.pop("events")
        session_key = state["session_key"]
    statuses = {str(item.get("status") or "") for item in events}
    status = "completed" if statuses == {"completed"} else (
        "error" if statuses == {"error"} else "partial")
    return {"type": "async_delegation_batch", "session_key": session_key,
            "status": status, "count": len(events), "tasks": events}


def _notify(run: _Run, job: agent_loop_service.AgentLoopJob, value: Any,
            error: Optional[BaseException]) -> None:
    """Hand a finished run to the host's completion queue. Never raises: the run already happened."""
    payload = _payload_dict(value) if value is not None else {}
    if error is None and run.schema_mode == "strict" \
            and payload.get("output_schema_valid") is False:
        errors = "; ".join(str(item) for item in payload.get("output_schema_errors") or ()) \
            or "invalid output"
        error = ValueError(f"output schema validation failed: {errors}")
    status, body = _settle(run, value, error)
    event: dict[str, Any] = {
        "type": "async_delegation",
        "session_key": run.session_key,
        "session_id": job.job_id,
        "delegation_id": job.job_id,
        "agent_session_id": run.agent_session_id,
        "subject": run.subject,
        "status": status,
        "summary": _summary(body, run.path),
        "goal": run.goal,
        "goals": [run.goal],
        "api_calls": int(payload.get("api_calls") or 0),
        "duration_seconds": round(time.time() - (job.started_at or job.dispatched_at), 1),
        "dispatched_at": job.dispatched_at,
        "completed_at": time.time(),
        "model": str(payload.get("model") or run.model_name or ""),
        "role": "agent",
        "toolsets": list(payload.get("toolsets") or run.toolsets),
    }
    if run.shadowed:
        event["definition_shadowed"] = True
    if "output_schema_valid" in payload:
        event["output_schema_valid"] = payload["output_schema_valid"]
        if payload.get("output_schema_errors"):
            event["output_schema_errors"] = payload["output_schema_errors"]
        if payload.get("validated") is False:
            event["validated"] = False
    if payload.get("worktree"):
        event["worktree"] = payload["worktree"]
    if error is not None:
        event["error"] = body
    if run.batch_id and not run.batch_independent:
        combined = _batch_completion(run.batch_id, event)
        if combined is None:
            return
        event = combined
    try:
        _notification_queue().put(event)
    except Exception as exc:  # health: allow BLE001 -- a lost notification must not lose the run
        logger.warning("agent %s finished but the notification failed: %s",
                       run.agent, exc, exc_info=True)


# -- execution -----------------------------------------------------------------


def _live_hooks(run: _Run) -> dict[str, Any]:
    """Publish the built agent for owner steering; drop the record once the run settles.

    The gateway/TUI authority is captured here, in the calling thread, so the roster's
    ``subagent.steer`` check sees the same live transport a delegated child records.
    """
    from tools.agent_inbox import (
        capture_steer_authority,
        register_live_session,
        unregister_live_session,
    )

    authority = capture_steer_authority()
    live: dict[str, Any] = {"authority": authority}
    run.live = live

    def _on_agent(agent: Any) -> None:
        live["agent"] = agent
        if run.tool_dir is not None:
            # ADR-16 D4: the subagent's tool log is its own namespace, with its own steps.
            from tools.tool_output_archive import configure_agent_archive

            configure_agent_archive(agent, run.tool_dir)
        register_live_session(agent_session_id=run.agent_session_id, agent=agent,
                              owner_session_id=run.session_key, goal=run.goal,
                              model=run.model_name, depth=run.depth, authority=authority)
        if run.keep_alive:
            from agent.agent_session_runtime import runtime

            runtime().note_agent(run.agent_session_id, agent)

    def _on_settled() -> None:
        unregister_live_session(run.agent_session_id, live.get("agent"))

    return {"on_agent": _on_agent, "on_settled": _on_settled}


def _params(run: _Run, history: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
    """The ``agent.agent_run.run`` keyword surface for this job."""
    flags = run.flags
    hooks = _live_hooks(run)
    yield_state = YieldState(schema=run.output_schema) if _yield_tool_enabled() else None
    on_agent = hooks["on_agent"]

    def _on_agent(agent: Any) -> None:
        # The turn loop reads this after the tool batch; a wake turn resets it first.
        if yield_state is not None:
            agent._yield_state = yield_state
        on_agent(agent)

    return {
        "context": run.context,
        "system_message": run.system_prompt,
        "model": run.model_name,
        "provider": run.provider_name,
        "base_url": run.base_url,
        "api_key": run.api_key,
        "fallback_chain": [dict(entry) for entry in run.fallback_chain],
        "model_role": run.model_role,
        "toolsets": run.toolsets,
        "all_tools": run.all_tools,
        "project_dir": run.project_dir,
        "history": history,
        "layer": run.agent,
        "agent_session_id": run.agent_session_id,
        "transcript_path": str(run.path) if run.path else "",
        "history_session": run.session_key,
        # The child AIAgent's own session id is the agent_session_id, so its nested ``agent``
        # calls report this session as their parent and named-parent authorization resolves.
        "session_id": run.agent_session_id,
        "parent_session_id": run.session_key,
        "meta": {"agent": run.agent, "parent": run.session_key, "depth": run.depth},
        "skip_memory": flags.get("skip_memory", True),
        "skip_context_files": flags.get("skip_context_files", True),
        "skip_background_review": flags.get("skip_background_review", True),
        "load_soul_identity": flags.get("load_soul_identity", False),
        "enable_tools": list(run.enable_tools),
        "max_iterations": run.max_iterations,
        "reasoning_effort": run.reasoning_effort,
        "worktree": run.worktree,
        "output_schema": run.output_schema,
        "autoload_skills": list(run.autoload_skills),
        "advisor": run.advisor,
        "read_summarize": run.read_summarize,
        "definition_metadata": dict(run.definition_metadata),
        "keep_alive": run.keep_alive,
        "extra_tools": [build_yield_tool(yield_state)] if yield_state is not None else [],
        "on_agent": _on_agent,
        "on_settled": hooks["on_settled"],
    }


def _payload(result: agent_run.AgentRunResult, run: _Run) -> dict[str, Any]:
    payload = dict(result.as_dict())
    payload.update({
        "status": "completed",
        "name": run.short_name,
        "all_tools": run.all_tools,
        "system_prompt_source": run.prompt_source,
        "context_files": run.context_count,
        "transcript_path": str(payload.get("transcript_path") or run.path),
    })
    if run.shadowed:
        payload["definition_shadowed"] = True
    return payload


@dataclass(frozen=True)
class _Call:
    """One validated call: caller arguments merged with the definition and host defaults."""

    agent: str
    user_prompt: str
    definition: _Definition
    provider_name: str
    model_name: str
    toolsets: list[str]
    all_tools: bool
    background: bool
    enable_tools: list[str]
    max_iterations: int
    reasoning_effort: Any
    worktree: bool
    flags: dict[str, bool]
    project: Optional[Path]
    subject: str
    #: Caller-supplied short name; empty means the run generates one.
    name: str = ""
    model_role: str = ""
    model_selector: str = ""
    skill_name: str = ""
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    fallback_chain: tuple[dict[str, str], ...] = ()
    spawns: Optional[frozenset[str]] = None
    output_schema: Optional[dict[str, Any]] = None
    schema_mode: str = "permissive"
    autoload_skills: tuple[str, ...] = ()
    advisor: Any = None
    read_summarize: Optional[bool] = None


#: Host defaults per knob; a definition overrides them, a caller argument overrides both.
_KNOB_DEFAULTS: dict[str, Any] = {
    "background": True,
    # omp's subagents start blank: no host memory, no project context files, no operator soul.
    # A caller or definition can opt back in per run.
    "skip_memory": True,
    "skip_context_files": True,
    "skip_background_review": True,
    "load_soul_identity": False,
    "enable_tools": [],
    "max_iterations": agent_run.MAX_ITERATIONS,
    "reasoning_effort": "",
    "worktree": False,
    "toolsets": None,
    "model": "",
}


def _caller_knobs(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """``(caller values, all_tools)``: ``None`` wherever the caller omitted the argument."""
    toolsets, all_tools = _tool_sets(args)
    if toolsets:
        from toolsets import validate_toolset

        unknown = [name for name in toolsets if not validate_toolset(name)]
        if unknown:
            raise ValueError(f"unknown toolset(s): {', '.join(unknown)}")
    raw_model = args.get("model")
    if isinstance(raw_model, (list, tuple)):
        raise ValueError("model takes one selector, not a list; use '@role' or 'provider:model'")
    model = _text(raw_model)
    if model:
        _model_route(model)  # validate the caller's route/role before it can outrank a definition
    knobs: dict[str, Any] = {
        name: _flag(args, name) for name in (
            "background", "skip_memory", "skip_context_files", "skip_background_review",
            "load_soul_identity", "worktree")}
    knobs["toolsets"] = toolsets
    raw_tools = args.get("tools")
    if raw_tools is not None and args.get("enable_tools") is not None:
        raise ValueError("pass either tools or enable_tools, not both")
    if raw_tools is None:
        raw_tools = args.get("enable_tools")
    if raw_tools is not None:
        if isinstance(raw_tools, str):
            raw_tools = [raw_tools]
        if not isinstance(raw_tools, (list, tuple)):
            raise ValueError("tools must be an array of registered tool names")
        knobs["enable_tools"] = [
            str(name).strip() for name in raw_tools if str(name).strip()]
    else:
        knobs["enable_tools"] = None
    knobs["model"] = model or None
    raw_max_iterations = args.get("max_iterations")
    if raw_max_iterations is None:
        knobs["max_iterations"] = None
    else:
        if isinstance(raw_max_iterations, bool) or not isinstance(raw_max_iterations, int) \
                or raw_max_iterations < 1:
            raise ValueError("max_iterations must be a positive integer")
        knobs["max_iterations"] = raw_max_iterations
    raw_effort = args.get("effort")
    if raw_effort is None:
        knobs["reasoning_effort"] = None
    else:
        token = str(raw_effort).strip().lower()
        if token not in _EFFORT_ALIASES:
            raise ValueError("effort must be one of lo, med, hi")
        knobs["reasoning_effort"] = _EFFORT_ALIASES[token]
    if knobs.get("worktree") is None and args.get("isolated") is not None:
        knobs["worktree"] = _flag(args, "isolated")
    return knobs, all_tools


def _caller_output_schema(args: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The caller's ``output_schema`` (a JSON Schema object), validated before any spawn."""
    raw = args.get("output_schema")
    if raw is None:
        return None
    from tools.delegation_output_schema import coerce_output_schema

    schema, error = coerce_output_schema(raw)
    if error:
        raise ValueError(error)
    return schema


def _spawn_allowlist(definition: _Definition) -> Optional[frozenset[str]]:
    """The definition's ``spawns``: ``None`` without a definition file, else an allow-list.

    Missing ``spawns`` follows omp's compatibility rule: the definition's own ``toolsets``
    naming ``agents``/``delegation`` means ``*``; otherwise the agent cannot derive at all.
    """
    if not definition.source or definition.source == "argument":
        return None
    if "spawns" in definition.metadata:
        return frozenset(definition.metadata.get("spawns") or ())
    toolsets = set(definition.metadata.get("toolsets") or ())
    return frozenset({"*"}) if toolsets & {"agents", "delegation"} else frozenset()


def _resident_enabled() -> bool:
    """Whether a settled background run stays resident (``agent_sessions.keep_alive``)."""
    from agent.agent_session_runtime import session_limits

    return bool(session_limits().get("keep_alive"))


def _resident_spec(call: _Call, *, depth: int,
                   context_inputs: tuple[ContextInput, ...] = ()) -> dict[str, Any]:
    """What a wake/revive needs to rebuild this session; credentials are never included.

    ``spawns``/``spawn_depth`` travel with the spec because a park→revive rebuilds the run
    without the spawning call's ContextVars: without them a revived named agent would lose its
    spawn allow-list and restart the recursion budget at zero. ``context_inputs`` is the durable
    description of the files the run was given, so ``agent_out`` can report them after a revive.
    """
    spawns = None if call.spawns is None else (
        ["*"] if "*" in call.spawns else sorted(call.spawns))
    return {
        "model_selector": call.model_selector,
        "model_role": call.model_role,
        "model_name": call.model_name,
        "provider_name": call.provider_name,
        "toolsets": list(call.toolsets),
        "all_tools": call.all_tools,
        "flags": dict(call.flags),
        "enable_tools": list(call.enable_tools),
        "max_iterations": call.max_iterations,
        "reasoning_effort": call.reasoning_effort or "",
        "project_dir": str(call.project) if call.project else "",
        "skill_name": call.skill_name,
        "system_prompt": call.definition.body,
        "prompt_source": call.definition.source,
        "output_schema": call.output_schema,
        "schema_mode": call.schema_mode,
        "autoload_skills": list(call.autoload_skills),
        "advisor": call.advisor,
        "read_summarize": call.read_summarize,
        "worktree": call.worktree,
        "definition_metadata": dict(call.definition.metadata),
        "spawns": spawns,
        "spawn_depth": int(depth) + 1,
        "context_inputs": [item.as_dict() for item in context_inputs],
    }


@contextlib.contextmanager
def nested_spawn_policy(spawns: Any, depth: int):
    """Bind one resident session's ``spawns``/depth for its wake and revive turns.

    ``spawns=None`` means the session had no definition file (no policy); a list (including
    ``["*"]``) is the allow-list the nested ``agent`` call validates against. The depth is the
    run's own depth, so a revived session resumes the same recursion budget it started with.
    """
    allowed: Optional[frozenset[str]]
    if spawns is None:
        allowed = None
    elif "*" in (spawns or ()):
        allowed = frozenset({"*"})
    else:
        allowed = frozenset(str(name) for name in (spawns or ()) if str(name).strip())
    spawn_token = _SPAWNS.set(allowed)
    depth_token = _DEPTH.set(max(0, int(depth or 0)))
    try:
        yield
    finally:
        _DEPTH.reset(depth_token)
        _SPAWNS.reset(spawn_token)


def _check_spawn_allowed(agent_name: str) -> None:
    """Refuse a nested ``agent`` call the parent definition's ``spawns`` does not name."""
    allowed = _SPAWNS.get()
    if allowed is None or "*" in allowed or agent_name in allowed:
        return
    listed = ", ".join(sorted(allowed)) or "none"
    raise ValueError(
        f"agent {agent_name!r} is not allowed by the parent definition's spawns ({listed})")


def _output_schema(definition: _Definition) -> Optional[dict[str, Any]]:
    """The effective ``output`` contract: ``structured: false`` explicitly opts out."""
    schema = definition.metadata.get("output")
    if schema is None or definition.metadata.get("structured") is False:
        return None
    return schema


def _stored_agent_advisor(agent_name: str, source: str) -> Optional[bool]:
    """The Agent Hub's stored ``advisor`` toggle for this definition row, if any.

    The row is keyed by ``(agent_id, source)`` where ``source`` is the prompt path the hub
    edited. Application code only reads it: the spawn path consumes the value below.
    """
    if not source or source == "argument":
        return None
    try:
        from hermes_state_registry import acquire, release_or_close

        db = acquire()
    except Exception:  # health: allow BLE001 -- no store means no stored override
        logger.debug("agent: no session store for agent_config", exc_info=True)
        return None
    try:
        row = db.get_agent_config(agent_name, source)
    except Exception:  # health: allow BLE001 -- a read must not fail the spawn
        logger.debug("agent: agent_config read failed for %s", agent_name, exc_info=True)
        return None
    finally:
        release_or_close(db)
    if not isinstance(row, dict) or row.get("advisor") is None:
        return None
    raw = row.get("advisor")
    if isinstance(raw, bool):
        return raw
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return None


def _advisor_option(agent_name: str, definition: _Definition) -> Any:
    """The per-agent advisor opt-in: a stored hub toggle, else the definition field.

    ``True`` uses the configured roster/role; a string resolves through the same selector
    grammar as ``model`` (``provider:model``, a bare id or ``@role``).
    """
    stored = _stored_agent_advisor(agent_name, definition.source)
    value = stored if stored is not None else definition.metadata.get("advisor")
    if value is None or value is False:
        return None
    if value is True:
        return True
    selector = str(value)
    level = ""
    from hermes_constants import parse_reasoning_effort

    if selector.startswith("@"):
        role, sep, suffix = selector.partition(":")
        if sep and parse_reasoning_effort(suffix):
            selector, level = role, suffix
    else:
        base, sep, suffix = selector.rpartition(":")
        if sep and parse_reasoning_effort(suffix):
            selector, level = base, suffix
    route = _model_route(selector)
    option: dict[str, Any] = {"model": route.model, "provider": route.provider,
                              "selector": selector}
    if level:
        option["reasoning_effort"] = level
    # Only non-secret routing metadata: this value is persisted in the resident spec.
    return option


def _validate(args: dict[str, Any]) -> _Call:
    """Parse the call once: arguments, then the definition, then the merged knobs.

    ``ValueError`` carries the caller-facing message; it is raised before any run or background
    dispatch, so a refused call constructs no agent.
    """
    spawn_raw = args.get("spawn")
    if spawn_raw is None:
        name = _agent_name(args)
        prompt = _text(args.get("user_prompts"))
    else:
        if not _spawn_directive_enabled():
            raise ValueError(
                "spawn directives are disabled (delegation.spawn_directive=false); pass "
                "agent_name and user_prompts instead")
        if args.get("tasks") is not None:
            raise ValueError("spawn cannot be combined with tasks")
        for key in ("agent_name", "user_prompts", "task_file"):
            if _text(args.get(key)):
                raise ValueError(f"spawn replaces {key}; pass only the spawn directive")
        name, prompt = _parse_spawn_directive(spawn_raw)
    _check_spawn_allowed(name)
    short_name = _short_name(args.get("name"))
    skill_name = _skill_name(args)
    project = _project(args.get("project_dir"))
    if spawn_raw is None:
        task_file = _task_file_prompt(args.get("task_file"), project)
        if task_file and prompt:
            raise ValueError("pass exactly one of user_prompts or task_file")
        prompt = prompt or task_file
        if not prompt:
            raise ValueError("user_prompts is required (or task_file)")
    user_prompt = prompt
    definition = _system_prompt(project, name, skill_name, _text(args.get("system_prompts")))
    caller, all_tools = _caller_knobs(args)
    caller_schema = _caller_output_schema(args)
    schema_mode = str(args.get("schema_mode") or "permissive").strip().lower()
    if schema_mode not in {"permissive", "strict"}:
        raise ValueError("schema_mode must be 'permissive' or 'strict'")
    knobs = agent_definitions.resolve(caller, definition.metadata, _KNOB_DEFAULTS)
    route = _model_route(knobs["model"])
    enable_tools = [str(name) for name in (knobs["enable_tools"] or [])]
    toolsets = [str(name) for name in (knobs["toolsets"] or [])]
    if enable_tools:
        unknown = sorted(set(enable_tools) - _registered_tool_names())
        if unknown:
            raise ValueError(
                "unknown tool(s): " + ", ".join(unknown)
                + " (tools takes registered tool names; host extra_tools are not callable "
                  "by name)")
        # An explicit tool allowlist replaces the toolset selection entirely.
        all_tools, toolsets = False, []
    elif not toolsets and not all_tools:
        from agent.prompts.toolsets import DEFAULT_TOOLSETS

        toolsets = list(DEFAULT_TOOLSETS)
    elif "*" in toolsets or "all" in toolsets:
        all_tools, toolsets = True, []
    # Definition-level blocking outranks async execution: the parent waits even when the host
    # default (or the caller) asked for a background run.
    background = False if definition.metadata.get("blocking") is True else bool(
        knobs["background"])
    return _Call(
        agent=name, user_prompt=user_prompt, definition=definition,
        provider_name=route.provider, model_name=route.model, toolsets=toolsets,
        all_tools=all_tools, background=background,
        enable_tools=enable_tools,
        max_iterations=max(1, int(knobs["max_iterations"])),
        reasoning_effort=knobs["reasoning_effort"] or route.reasoning_effort,
        worktree=bool(knobs["worktree"]),
        model_role=route.role, model_selector=str(knobs["model"] or ""), skill_name=skill_name,
        base_url=route.base_url, api_key=route.api_key,
        fallback_chain=route.fallback_chain,
        spawns=_spawn_allowlist(definition),
        output_schema=caller_schema if caller_schema is not None else _output_schema(definition),
        schema_mode=schema_mode,
        autoload_skills=tuple(definition.metadata.get("autoloadSkills") or ()),
        advisor=_advisor_option(name, definition),
        read_summarize=definition.metadata.get("read-summarize"),
        flags={key: bool(knobs[key]) for key in
               ("skip_memory", "skip_context_files", "skip_background_review",
                "load_soul_identity")},
        project=project, subject=_text(args.get("subject")), name=short_name)



def _execute_call(call: _Call, args: dict[str, Any], *, session_id: str, depth: int,
                  batch: Optional[tuple[str, int, bool]] = None) -> str:
    """Run one validated call (inline or background) and return its JSON payload."""
    try:
        context, context_inputs = _context_files(args, call.project)
        history = _continued_history(args, call.agent)
    except ValueError as exc:
        return tool_error(str(exc))

    name = call.agent
    short_name, name_error = _claim_short_name(call.name, name)
    if name_error:
        return tool_error(name_error)
    parent_session = str(session_id or "").strip()
    agent_session_id = _new_agent_session_id(name)
    project_dir = str(call.project) if call.project else ""
    bundle = run_artifacts.bundle_for_run(
        parent_session_id=parent_session, agent_session_id=agent_session_id,
        project_dir=project_dir)
    path = session_paths.transcript_path(bundle, call.subject or _goal(call.user_prompt))
    keep_alive = bool(call.background and not call.worktree and _resident_enabled())
    run = _Run(agent=name, session_key=parent_session, agent_session_id=agent_session_id,
               subject=call.subject or _goal(call.user_prompt),
               short_name=short_name,
               user_prompt=call.user_prompt,
               system_prompt=call.definition.body, prompt_source=call.definition.source,
               context=context, context_count=len(context_inputs),
               context_inputs=context_inputs,
               provider_name=call.provider_name, model_name=call.model_name,
               base_url=call.base_url, api_key=call.api_key,
               fallback_chain=call.fallback_chain,
               model_role=call.model_role,
               toolsets=call.toolsets, all_tools=call.all_tools,
               project_dir=project_dir, path=path,
               goal=_goal(call.user_prompt), depth=depth + 1, enable_tools=call.enable_tools,
               max_iterations=call.max_iterations, reasoning_effort=call.reasoning_effort,
               worktree=call.worktree, shadowed=call.definition.shadowed, flags=call.flags,
               output_schema=call.output_schema,
               schema_mode=call.schema_mode,
               autoload_skills=call.autoload_skills,
               advisor=call.advisor,
               read_summarize=call.read_summarize,
               definition_metadata=dict(call.definition.metadata),
               spec=_resident_spec(call, depth=depth, context_inputs=context_inputs),
               keep_alive=keep_alive, bundle=bundle,
               batch_id=(batch[0] if batch else ""),
               batch_size=(batch[1] if batch else 0),
               batch_independent=(batch[2] if batch else True))
    subject = _begin_session_row(agent_session_id, run)
    run = _finalize_run_paths(agent_session_id, replace(run, subject=subject))
    params = _params(run, history)

    if not call.background:
        token = _DEPTH.set(depth + 1)
        spawn_token = _SPAWNS.set(call.spawns)
        try:
            result = agent_run.run(call.user_prompt, **params)
        except Exception as exc:  # the tool loop gets a message, not a traceback
            logger.warning("agent %s failed: %s", name, exc, exc_info=True)
            _settle(run, None, exc)
            return tool_error(f"agent run failed: {exc}")
        finally:
            _SPAWNS.reset(spawn_token)
            _DEPTH.reset(token)
        payload = _payload(result, run)
        if call.schema_mode == "strict" and payload.get("output_schema_valid") is False:
            errors = "; ".join(str(item) for item in payload.get("output_schema_errors") or ()) \
                or "invalid output"
            _settle(run, None, ValueError(f"output schema validation failed: {errors}"))
            return tool_error(f"output schema validation failed: {errors}",
                              agent_session_id=agent_session_id,
                              output_schema_valid=False)
        _settle(run, result, None)
        payload.pop("status", None)
        return json.dumps({"agent_session_id": agent_session_id, "subject": subject,
                           "status": "completed", **payload})

    if not parent_session:
        _settle(run, None, ValueError("background run without a calling session"))
        return tool_error("agent needs the calling session for a background run; "
                          "pass background=false to run it inline")
    token = _DEPTH.set(depth + 1)
    spawn_token = _SPAWNS.set(call.spawns)
    try:
        if keep_alive:
            from agent.agent_session_runtime import AgentSessionQueueFull, runtime

            pool = runtime()
            try:
                pool.open_session(
                    agent_session_id=agent_session_id, agent_name=name, subject=subject,
                    transcript_path=str(run.path), parent_session_id=parent_session,
                    spec=run.spec, authority=run.live.get("authority"),
                    worktree=run.worktree)
                job = pool.submit(
                    agent_session_id,
                    lambda: agent_run.run(call.user_prompt, **params),
                    label=name, session_key=parent_session,
                    on_settle=lambda settled, value, error: _notify(
                        run, settled, value, error))
            except AgentSessionQueueFull as exc:
                _settle(run, None, exc)
                return tool_error(str(exc), status="queue_full", code="queue_full")
        else:
            job = agent_loop_service.service().submit(
                lambda: agent_run.run(call.user_prompt, **params),
                label=name, session_key=parent_session,
                on_settle=lambda settled, value, error: _notify(run, settled, value, error))
    except agent_loop_service.AgentLoopQueueFull as exc:
        _settle(run, None, exc)
        return tool_error(str(exc))
    finally:
        _SPAWNS.reset(spawn_token)
        _DEPTH.reset(token)
    return json.dumps({
        "agent_session_id": agent_session_id,
        "subject": subject,
        "name": short_name,
        "job_id": job.job_id,
        "status": "running" if job.queue_position == 0 else "queued",
        "queue_position": job.queue_position,
        "background": True,
        "resident": keep_alive,
        "transcript_path": str(run.path),
    })


def _batch_task_args(args: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    """Per-task argument dicts for ``tasks=[...]``, or ``None`` for the single-call form.

    The batch shares the top-level ``context`` (prepended to every prompt) and every other
    caller knob; an item overrides them. Each item names its own agent, defaulting to the
    top-level ``agent_name``. Everything is validated before the first spawn.
    """
    raw = args.get("tasks")
    if raw is None:
        return None
    if args.get("spawn") is not None:
        raise ValueError("spawn cannot be combined with tasks")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"tasks must be a JSON array of task objects ({exc.msg})") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("tasks must be a non-empty array of task objects")
    if len(raw) > MAX_BATCH_TASKS:
        raise ValueError(f"tasks accepts at most {MAX_BATCH_TASKS} tasks; split the call")
    if _text(args.get("user_prompts")) or _text(args.get("task_file")):
        raise ValueError("with tasks, pass user_prompts/task_file per task, not at the top level")
    shared_context = _text(args.get("context"))
    default_agent = _text(args.get("agent_name"))
    inheritable = {
        key: value for key, value in args.items()
        if key not in {"tasks", "context", "context_files", "continue_from",
                       "agent_name", "user_prompts", "subject", "task_file", "name"}
    }
    prepared: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Task {index} must be an object")
        merged = {**inheritable, **item}
        agent = _text(item.get("agent") or default_agent)
        if not agent:
            raise ValueError(f"Task {index} is missing agent")
        prompt = _text(item.get("user_prompts") if item.get("user_prompts") is not None
                       else item.get("task"))
        item_task_file = _text(item.get("task_file"))
        if item_task_file:
            if prompt:
                raise ValueError(f"Task {index} must pass exactly one of user_prompts or task_file")
            prompt = _task_file_prompt(item_task_file, _project(merged.get("project_dir")))
        if not prompt:
            raise ValueError(f"Task {index} is missing user_prompts or task_file")
        solution_space = _text(item.get("solution_space"))
        if solution_space:
            prompt = f"{prompt}\n\nSolution space: {solution_space}"
        if shared_context:
            prompt = f"{shared_context}\n\n{prompt}"
        merged["agent_name"] = agent
        merged["user_prompts"] = prompt
        for key in ("agent", "task", "solution_space", "task_file"):
            merged.pop(key, None)
        prepared.append(merged)
    return prepared


def agent_tool(args: dict[str, Any], *, session_id: str = "",
               task_id: Optional[str] = None, **_: Any) -> str:
    """Run one named agent, or a concurrent batch, and return identity payload(s)."""
    try:
        batch = _batch_task_args(args)
        prepared = ([(item, _validate(item)) for item in batch]
                    if batch is not None else [(args, _validate(args))])
        _preflight_short_names(prepared)
    except ValueError as exc:
        return tool_error(str(exc))
    depth = _DEPTH.get()
    if depth >= MAX_DEPTH:
        return tool_error(f"agent recursion limit ({MAX_DEPTH}) reached")
    if batch is None:
        return _execute_call(prepared[0][1], prepared[0][0], session_id=session_id, depth=depth)
    independent = bool(args.get("independent_completions", True))
    aggregate = (not independent and bool(str(session_id or "").strip())
                 and all(call.background for _item, call in prepared))
    batch_id = f"batch-{uuid.uuid4().hex[:12]}"
    if aggregate:
        _begin_batch(batch_id, len(prepared), session_id)
    tasks = []
    for item, call in prepared:
        result = json.loads(_execute_call(
            call, item, session_id=session_id, depth=depth,
            batch=(batch_id, len(prepared), independent) if aggregate else None))
        tasks.append(result)
        if aggregate and result.get("status") not in {"running", "queued"}:
            # The child never reached the pool, so it will not notify; count it here.
            combined = _batch_completion(batch_id, {"status": "error", **result})
            if combined is not None:
                try:
                    _notification_queue().put(combined)
                except Exception:  # health: allow BLE001 -- a lost notification is logged below
                    logger.warning("agent batch notification failed", exc_info=True)
    status = "running" if any(task.get("status") in {"running", "queued"} for task in tasks) \
        else "completed"
    return json.dumps({"status": status, "background": status == "running", "tasks": tasks})


AGENT_SCHEMA: dict[str, Any] = {
    "name": "agent",
    "description": (
        "Run one named agent or a concurrent `tasks[]` batch, each with its own prompt, tools "
        "and on-disk history. "
        "The agent definition is <agent_name>.md (or <agent_name>) searched in the project, "
        "profile and installation .agents//agents/ directories, plugin agents/ directories and "
        "skills/<skill_name>/agents/; `agent_name` is required for a single run and may be the "
        "batch default. The definition's "
        "front matter may pin model, toolsets, enable_tools, max_iterations, reasoning_effort, "
        "worktree, output, spawns, blocking and the skip flags; a caller argument overrides it "
        "(blocking: true forces an inline run). `spawn` is a one-call shorthand: "
        "`spawn:@<agent_name>` on the first line, then the task body. By default the "
        "run is background and the answer arrives as a completion; background=false blocks and "
        "returns the answer. A background run that is not blocking/worktree stays resident after "
        "it settles (agent_sessions.keep_alive): it keeps its slot until the idle TTL parks it, a "
        "message wakes it, and a parked one revives on the next message. Every call is a new agent "
        "session with its own transcript at "
        "<parent bundle>/<topic>.jsonl under the profile's sessions/ tree (a run with no parent "
        "session gets its own bundle); the payload returns agent_session_id and subject, which "
        "agent_out reads back. Runs do not continue "
        "each other implicitly: pass continue_from with a prior agent_session_id to carry that "
        "conversation forward."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "The agent's name (a file name, not a path); also its history folder.",
            },
            "spawn": {
                "type": "string",
                "description": (
                    "One-call shorthand: 'spawn:@<agent_name>' on the first line, then the task "
                    "body. Mutually exclusive with agent_name, user_prompts, task_file and tasks; "
                    "the named agent still passes the caller's spawns allow-list and depth limit."
                ),
            },
            "user_prompts": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "The user message for this run (a list is joined with blank lines).",
            },
            "task_file": {
                "type": "string",
                "description": (
                    "Read the prompt from this file instead of user_prompts (absolute or "
                    "project-relative, inside the project or profile home)."
                ),
            },
            "system_prompts": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": (
                    "System prompt used only when no agent definition file is found in the "
                    "search path."
                ),
            },
            "skill_name": {
                "type": "string",
                "description": "Optional skill folder whose skills/<skill_name>/agents/ is searched.",
            },
            "model": {
                "type": "string",
                "description": (
                    "Pin the route as 'provider:model', a bare model name, or '@role' "
                    "(@default, @task, @advisor or an auxiliary.<task> slot such as @smol)."
                ),
            },
            "tool_sets": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Toolsets for this run, limited to the default-loaded built-in sets; "
                    "omit for that default, or ['*'] for every host tool."
                ),
            },
            "enable_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Explicit tool-name allowlist. When non-empty it replaces tool_sets "
                    "and the definition's toolsets; every name must be a registered tool "
                    "(an unknown name fails closed). Host-only extra tools cannot be named."
                ),
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Alias for enable_tools; pass only one of the two.",
            },
            "subject": {
                "type": "string",
                "description": (
                    "One-line topic for this agent session; unique, and how agent_out finds it. "
                    "Defaults to the first line of user_prompts."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Profile-unique short name (CamelCase letters/digits, at most 32) that "
                    "agent_message and agent_out address this session by. Generated from the "
                    "agent name plus four hex characters when omitted; a duplicate is refused."
                ),
            },
            "continue_from": {
                "type": "string",
                "description": (
                    "A prior agent_session_id whose transcript seeds this run's history. The new "
                    "run still gets its own id, subject and transcript."
                ),
            },
            "context_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Files to supply as data (at most 20, 100KB each, 200KB total). A path is "
                    "project-relative (or absolute inside the project/profile home), and "
                    "`local://<path>` is an accepted spelling of the same reference."
                ),
            },
            "project_dir": {
                "type": "string",
                "description": "Working directory for the run; defaults to the project root.",
            },
            "background": {
                "type": "boolean",
                "description": (
                    "true (default) returns jobs and delivers answers later, dispatching a "
                    "batch concurrently; false blocks and runs the call (or every task in a "
                    "batch, in array order) on the calling thread."
                ),
            },
            "worktree": {
                "type": "boolean",
                "description": (
                    "Run in an isolated git worktree off HEAD; the run's edits are committed "
                    "and merged back automatically (apply=auto). A dirty parent workspace, a "
                    "merge conflict or a failed merge keeps the branch and reports why. Off by "
                    "default; degrades to the shared workspace outside git or on a non-local "
                    "terminal backend, and the payload says which happened."
                ),
            },
            "skip_memory": {
                "type": "boolean",
                "description": "Skip host memory context (default true for a named peer).",
            },
            "skip_context_files": {
                "type": "boolean",
                "description": "Skip project context files such as AGENTS.md (default true).",
            },
            "skip_background_review": {
                "type": "boolean",
                "description": "Skip the post-turn background review fork (default true).",
            },
            "load_soul_identity": {
                "type": "boolean",
                "description": "Load the operator soul/system identity (default false).",
            },
            "tasks": {
                "type": "array",
                "description": (
                    "Batch form: one object per agent. With background=false (or a blocking "
                    "definition) tasks run one at a time in array order on the calling thread; "
                    "background tasks are dispatched concurrently. Each item takes "
                    "`agent` (defaults to agent_name), `user_prompts` (or `task`), and may "
                    "override name, subject, model, tool_sets, enable_tools/tools, "
                    "context_files, worktree/isolated, output_schema, continue_from, background."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string", "description": "Agent definition name for this task."},
                        "user_prompts": {
                            "type": ["string", "array"],
                            "items": {"type": "string"},
                            "description": "The task prompt; `task` is accepted as an alias.",
                        },
                        "task": {"type": ["string", "array"], "items": {"type": "string"}},
                        "task_file": {"type": "string"},
                        "name": {"type": "string", "description": "Profile-unique short name."},
                        "subject": {"type": "string"},
                        "solution_space": {
                            "type": "string",
                            "description": "How open-ended the problem is; appended to the prompt.",
                        },
                        "model": {"type": "string"},
                        "tool_sets": {"type": "array", "items": {"type": "string"}},
                        "enable_tools": {"type": "array", "items": {"type": "string"}},
                        "tools": {"type": "array", "items": {"type": "string"}},
                        "context_files": {"type": "array", "items": {"type": "string"}},
                        "worktree": {"type": "boolean"},
                        "isolated": {"type": "boolean", "description": "Alias for worktree."},
                        "output_schema": {"type": "object"},
                        "schema_mode": {"type": "string", "enum": ["permissive", "strict"]},
                        "continue_from": {"type": "string"},
                        "background": {"type": "boolean"},
                    },
                },
            },
            "context": {
                "type": "string",
                "description": (
                    "Shared batch context (`# Goal`, `# Contract` interfaces), prepended to "
                    "every task prompt; NEVER repeat it per task."
                ),
            },
            "independent_completions": {
                "type": "boolean",
                "description": (
                    "Batch background runs: true (default) delivers one completion per task; "
                    "false buffers them into one combined completion."
                ),
            },
            "max_iterations": {
                "type": "integer",
                "minimum": 1,
                "description": "Per-run tool-call budget; overrides the definition and host default.",
            },
            "effort": {
                "type": "string",
                "enum": ["lo", "med", "hi"],
                "description": "Coarse reasoning effort; maps to low/medium/high.",
            },
            "isolated": {
                "type": "boolean",
                "description": "Alias for worktree (isolated git worktree per run).",
            },
            "output_schema": {
                "type": "object",
                "description": (
                    "JSON Schema the final answer must satisfy; overrides the definition's output "
                    "contract for this call."
                ),
            },
            "schema_mode": {
                "type": "string",
                "enum": ["permissive", "strict"],
                "description": (
                    "permissive (default) returns the raw answer after a failed validation; "
                    "strict fails the run instead."
                ),
            },
        },
        "required": [],
    },
}

registry.register(
    name="agent",
    toolset="agents",
    schema=AGENT_SCHEMA,
    handler=agent_tool,
    emoji="🧩",
)

__all__ = ["AGENT_SCHEMA", "agent_tool"]
