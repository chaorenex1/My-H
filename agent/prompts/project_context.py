"""``<project-context>`` tail block: workstation identity, project instruction files and the
workspace tree.

The block is rendered from ``agent/prompts/system/project-prompt.md``. Context files follow
``docs/omp-prompt-system.md`` §5: one project file type wins (candidate priority order), every
directory depth keeps its own file, files inject far ancestor → near ancestor, and a file whose
normalized paragraph sequence is fully contained in a nearer file is dropped.

``<critical>`` and the append text are not part of this block: Hermes already owns them as
``12-critical.md`` and the session append slot, and repeating them in the context tier would
move the closing contract into the middle of the prompt.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

from agent.prompts.prompt_sections import render_section

logger = logging.getLogger(__name__)

# ``<workspace-tree>`` bounds: three levels orient a session, and the entry cap keeps a huge
# checkout from spending prompt bytes on a listing ``rg --files`` answers better.
_TREE_DEPTH = 3
_TREE_MAX_ENTRIES = 200
# Directories that are never prompt material: VCS internals, dependency trees and tool caches
# would otherwise win the mtime sort and hide the files the session actually edits.
_TREE_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", "dist", "build",
})
# Profile-scoped redirect target for a session whose cwd IS the user's home (never mkdtemp:
# a random path would miss the cache every session and litter the disk).
_EMPTY_WORKSPACE_DIRNAME = "empty-workspace"
# Cross-session reuse only: the in-process freeze below owns the bytes of a live session.
_TREE_CACHE_NAMESPACE = "prompt_workspace_tree"
_TREE_CACHE_TTL_S = 3600
_TREE_CACHE_MAX_ENTRIES = 32
_TREE_FREEZE_MAX = 64

# Node's ``process.arch`` spellings, mapped from ``hermes_platform``'s canonical names
# (``amd64``/``x86``), so a prompt produced on Windows reports ``x64`` rather than ``AMD64``.
_MACHINE_ARCH = {
    "amd64": "x64",
    "x64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86": "ia32",
    "ia32": "ia32",
    "arm": "arm",
    "armv7l": "arm",
    "armv6l": "arm",
    "unknown": "",
}


@dataclass(frozen=True)
class ContextFile:
    """One project instruction file selected for the ``<repo-rules>`` block."""

    kind: str
    label: str
    path: Path
    content: str



def workstation_environment() -> list[str]:
    """``<workstation>`` rows: OS/Arch plus the host or backend state the tools actually see."""
    return runtime_environment_rows()[0]


def runtime_environment_rows() -> tuple[list[str], list[str]]:
    """``(rows, notes)`` for ``<workstation>``.

    A local backend gets the host rows (home, cwd, scratch) and the platform notes; a
    remote/sandbox backend gets only its own state — the agent's tools cannot touch the host
    that runs Hermes, so its paths are noise.
    """
    from agent import prompt_builder as pb
    from hermes_platform.host import facts as host_facts

    arch = _MACHINE_ARCH.get(host_facts.native_arch(), host_facts.native_arch())
    os_name = f"{host_facts.os_family()} {host_facts.os_release()}".strip()
    rows = [f"OS: {os_name}"] + ([f"Arch: {arch}"] if arch else [])
    backend = (pb._tenv_read("TERMINAL_ENV") or "local").strip().lower()
    if backend in pb._REMOTE_TERMINAL_BACKENDS or pb._plugin_backend_is_remote(backend):
        return rows, [pb._remote_backend_hint(backend)]
    hints = pb._local_host_hints()
    notes = list(hints[1:])
    if hints:
        rows += hints[0].splitlines()
    if screen := pb._bot_screen_hint():
        notes.append(screen)
    if pb.is_wsl():
        notes.append(pb.WSL_ENVIRONMENT_HINT)
    if embedder := pb._embedder_environment_hint():
        notes.append(embedder)
    return rows, notes


def model_label(provider: str = "", model: str = "") -> str:
    """``provider/model`` for the workstation line; a model already prefixed by its provider wins."""
    provider, model = (provider or "").strip(), (model or "").strip()
    if not provider or not model or model.startswith(provider + "/"):
        return model
    return f"{provider}/{model}"


def effective_project_dir(project_dir: str | Path | None = None) -> Path:
    """The directory a session may inspect for project context.

    A session whose working directory IS the user's home is redirected to a stable,
    profile-scoped empty directory: scanning home would advertise the user's private file
    names and make the cached prompt prefix depend on unrelated files. The target is fixed
    (never a ``mkdtemp`` path), so the workspace-tree cache keeps hitting across sessions.
    """
    path = Path(project_dir) if project_dir is not None else Path.cwd()
    try:
        resolved, home = path.resolve(), Path.home().resolve()
    except OSError:
        return path
    if resolved != home:
        return resolved
    from hermes_constants import apply_secure_dir_policy, get_hermes_home, mkdir_under_hermes_home

    empty = get_hermes_home() / "tmp" / _EMPTY_WORKSPACE_DIRNAME
    try:
        if not empty.is_dir():
            mkdir_under_hermes_home(empty)
            apply_secure_dir_policy(empty)
    except OSError:
        # A missing redirect target still beats scanning the user's home: an unreadable
        # directory resolves to an empty tree.
        logger.debug("empty workspace redirect unavailable", exc_info=True)
    return empty


def _newest_first(entry: os.DirEntry[str]) -> tuple[float, str]:
    try:
        mtime = entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        mtime = 0.0
    return (-mtime, entry.name.lower())


def _scan_workspace_tree(root: Path, depth: int) -> tuple[str, bool]:
    """Bounded listing: per level newest mtime first, dirs suffixed ``/``, two-space indent."""
    lines: list[str] = []
    truncated = False

    def walk(directory: Path, level: int) -> None:
        nonlocal truncated
        if level >= depth or truncated:
            return
        try:
            with os.scandir(directory) as entries:
                ordered = sorted(entries, key=_newest_first)
        except OSError:
            return
        for entry in ordered:
            if len(lines) >= _TREE_MAX_ENTRIES:
                truncated = True
                return
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir and entry.name in _TREE_SKIP_DIRS:
                continue
            lines.append(f"{'  ' * level}{entry.name}{'/' if is_dir else ''}")
            if is_dir:
                walk(Path(entry.path), level + 1)

    walk(root, 0)
    return "\n".join(lines), truncated


def _profile_key() -> str:
    """Cache scope: the active profile, so a multiplexed process never shares trees."""
    try:
        from hermes_constants import get_hermes_home, profile_name_for_home

        return profile_name_for_home(get_hermes_home())
    except Exception:  # health: allow BLE001 -- profile identity is a cache scope, not prompt content
        return "default"


def _workspace_tree_cache():
    """Cross-session namespace; ``get_cache()`` resolves the active profile per call."""
    from hermes_cache import EvictionPolicy, get_cache

    return get_cache().namespace(
        _TREE_CACHE_NAMESPACE,
        default_ttl=_TREE_CACHE_TTL_S,
        max_entries=_TREE_CACHE_MAX_ENTRIES,
        eviction=EvictionPolicy.LRU,
    )


@lru_cache(maxsize=_TREE_FREEZE_MAX)
def _frozen_workspace_tree(profile: str, session_id: str, path: str, depth: int) -> tuple[str, bool]:
    """First resolve for ``(profile, session, path, depth)``, frozen for the process.

    Compression rebuilds call the renderer again; a TTL that expired mid-session must not
    change bytes already cached in the conversation prefix, so the freeze outranks the
    cross-session cache for the life of the process.
    """
    key = f"{path}|d={depth}"
    try:
        cached = _workspace_tree_cache().get(key)
    except Exception:  # health: allow BLE001 -- the cache is an optimization, never a dependency
        cached = None
    if (
        isinstance(cached, dict)
        and isinstance(cached.get("tree"), str)
        and isinstance(cached.get("truncated"), bool)
    ):
        return cached["tree"], cached["truncated"]
    tree, truncated = _scan_workspace_tree(Path(path), depth)
    try:
        _workspace_tree_cache().set(key, {"tree": tree, "truncated": truncated})
    except Exception:  # health: allow BLE001 -- a cache write failure must not block the prompt
        logger.debug("workspace tree cache write skipped", exc_info=True)
    return tree, truncated


def workspace_tree(
    project_dir: str | Path | None = None, *, depth: int = _TREE_DEPTH, session_id: str = ""
) -> tuple[str, bool]:
    """``(tree, truncated)`` for ``<workspace-tree>``; ``("", False)`` for an empty directory.

    The caller passes only a path: the tree is resolved here, frozen for the session and
    reused across sessions through ``hermes_cache`` (1 h TTL, LRU).
    """
    root = effective_project_dir(project_dir)
    bounded = min(max(int(depth), 1), _TREE_DEPTH)
    return _frozen_workspace_tree(_profile_key(), str(session_id or ""), str(root), bounded)


def clear_workspace_tree_cache() -> None:
    """Drop the in-process session freeze (tests, profile churn)."""
    _frozen_workspace_tree.cache_clear()


def split_comparable_blocks(content: str) -> list[str]:
    """Paragraphs outside fenced code, whitespace-normalized (empty paragraphs dropped).

    Comparison only: fenced code is skipped so two files that differ only in examples do not
    count as one containing the other.
    """
    blocks: list[str] = []
    current: list[str] = []
    fenced = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if stripped:
            current.append(stripped)
        elif current:
            blocks.append(" ".join(" ".join(current).split()))
            current = []
    if current:
        blocks.append(" ".join(" ".join(current).split()))
    return blocks


def blocks_contain(container: Sequence[str], target: Sequence[str]) -> bool:
    """Whether *target* is an in-order subsequence of *container* (not necessarily contiguous)."""
    remaining = iter(container)
    return all(any(block == candidate for candidate in remaining) for block in target)


def dedupe_contained_context_files(files: Sequence[ContextFile]) -> list[ContextFile]:
    """Drop a file whose paragraphs are fully repeated by a nearer file (§5 rule 4).

    Order is far ancestor → near ancestor, so a later entry is the same depth or closer to the
    working directory. Equal depths keep discovery order.
    """
    ordered = sorted(files, key=lambda file: len(file.path.parts))
    blocks = [split_comparable_blocks(file.content) for file in ordered]
    return [
        file
        for index, file in enumerate(ordered)
        if not (blocks[index] and any(
            blocks_contain(blocks[other], blocks[index])
            for other in range(index + 1, len(ordered))
        ))
    ]


def select_context_files(
    cwd_path: Path,
    discovered: Iterable[tuple[str, str, Path, str]] | None = None,
) -> list[ContextFile]:
    """Project files the tail block injects: one type wins, then containment dedupe.

    *discovered* is ``discover_context_files`` output already in hand (``/context`` reuses its
    single walk); omitted, the walk runs here. ``.hermes.md`` frontmatter is dropped exactly as
    the legacy loader does, so the YAML header never reaches the prompt.
    """
    from agent import prompt_builder as pb

    by_kind: dict[str, list[ContextFile]] = {}
    for kind, label, path, content in (discovered if discovered is not None
                                       else pb.discover_context_files(cwd_path)):
        if kind == "hermes_md":
            content = pb._strip_yaml_frontmatter(content)
        if content.strip():
            by_kind.setdefault(kind, []).append(ContextFile(kind, label, path, content))
    for kind in pb.CONTEXT_FILE_PRIORITY:
        files = by_kind.get(kind)
        if files:
            return dedupe_contained_context_files(files)
    return []


def format_context_file(file: ContextFile, *, context_length: int | None = None) -> str:
    """One ``<file path="...">`` row: threat-scanned and capped like the legacy block."""
    from agent import prompt_builder as pb

    body = pb._scan_context_content(file.content, file.label)
    body = pb._truncate_content(
        body, file.label, context_length=context_length, read_path=str(file.path)
    )
    path_attr = (file.label or str(file.path)).replace('"', "&quot;")
    return f'<file path="{path_attr}">\n{body}\n</file>'


def _cap_merged_rows(
    rows: Sequence[str],
    files: Sequence[ContextFile],
    *,
    cwd_path: Path | None,
    context_length: int | None,
) -> list[str]:
    """Apply the legacy merged-chain cap: a deep monorepo cannot multiply the file budget.

    Per-file truncation already ran; the AGENTS.md chain and the concatenated ``.cursorrules``
    set each additionally share one budget across the merged result, exactly as
    ``_load_agents_md`` / ``_load_cursorrules`` do.
    """
    if len(rows) <= 1 or not files:
        return list(rows)
    from agent import prompt_builder as pb

    kind = files[0].kind
    warn_name = {"agents_md": "AGENTS.md (directory chain)", "cursorrules": ".cursorrules"}.get(kind)
    if warn_name is None:
        return list(rows)
    default_name = "AGENTS.md" if kind == "agents_md" else ".cursorrules"
    read_path = str(cwd_path / default_name) if cwd_path is not None else default_name
    merged = "\n".join(rows)
    return [pb._truncate_content(merged, warn_name, context_length=context_length, read_path=read_path)]


def render_project_context(
    *,
    provider: str = "",
    model: str = "",
    cwd: str | Path | None = None,
    context_length: int | None = None,
    platform: str = "",
    context_files: Sequence[ContextFile] | None = None,
    dir_context_files: Sequence[str] = (),
    workspace_roots: Sequence[str] = (),
    active_repo_context: str = "",
    subagent: bool = False,
    append_prompt: str = "",
    environment_probe: bool = True,
    skip_context_files: bool = False,
    session_id: str = "",
) -> str:
    """Render ``project-prompt.md``: workstation, ``<repo-rules>``, tail blocks and `<critical>`.

    ``context_files=None`` discovers from *cwd* through the same walk as the legacy
    ``build_context_files_prompt``, with the same install-tree and safe-worker guards;
    explicit files skip discovery (advisor/renderer callers). ``platform`` decides whether
    the launch dir may fall back to the install tree (``cli``/``tui`` only); ``subagent``
    selects the yield rule in ``<critical>``; ``append_prompt`` is the caller's literal
    tail text; ``skip_context_files`` drops the whole block for sessions told to ignore
    project instructions.

    The workspace tree is resolved here from *cwd* — never passed in — and only for a coding
    repository (the same detection ``render_runtime`` uses); a non-repo session scans nothing.
    *session_id* keys the in-process freeze so a compression rebuild keeps the same bytes.
    """
    if skip_context_files:
        return ""
    cwd_path = effective_project_dir(cwd)
    if context_files is None:
        from agent import prompt_builder as pb
        from agent.safe_worker_policy import safe_worker_enabled

        raw_cwd = str(cwd) if cwd is not None else None
        if safe_worker_enabled() or pb._project_context_suppressed(
            raw_cwd, cwd_path, platform in ("cli", "tui")
        ):
            files: list[ContextFile] = []
        else:
            files = select_context_files(cwd_path)
    else:
        files = list(context_files)
    rows = [format_context_file(file, context_length=context_length) for file in files]
    rows = _cap_merged_rows(rows, files, cwd_path=cwd_path, context_length=context_length)
    environment, environment_notes = runtime_environment_rows()
    if environment_probe:
        try:
            from tools.env_probe import get_environment_probe_line

            probe = get_environment_probe_line()
        except Exception:  # health: allow BLE001 -- the probe is optional; a failure must not block the prompt
            probe = ""
        if probe:
            environment_notes.append(probe)
    from agent.prompts.rules import project_languages

    tree, tree_truncated = (
        workspace_tree(cwd_path, session_id=session_id)
        if project_languages(cwd_path) else ("", False)
    )
    return render_section(
        "project_context",
        {
            "environment": environment,
            "environment_notes": environment_notes,
            "model": str(model or ""),
            "provider": str(provider or ""),
            "platform": str(platform or ""),
            "context_files": rows,
            "dir_context_files": [str(path) for path in dir_context_files],
            "include_workspace_tree": bool(tree),
            "workspace_tree": tree,
            "workspace_tree_truncated": bool(tree_truncated),
            "workspace_roots": [str(root) for root in workspace_roots],
            "active_repo_context": str(active_repo_context or ""),
            "subagent": bool(subagent),
            "append_prompt": str(append_prompt or ""),
        },
    ).strip()


__all__ = [
    "ContextFile",
    "blocks_contain",
    "clear_workspace_tree_cache",
    "dedupe_contained_context_files",
    "effective_project_dir",
    "format_context_file",
    "model_label",
    "render_project_context",
    "runtime_environment_rows",
    "select_context_files",
    "split_comparable_blocks",
    "workspace_tree",
    "workstation_environment",
]
