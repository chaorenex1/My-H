"""``<project-context>`` tail block: workstation identity and project instruction files.

The block is rendered from ``agent/prompts/system/project-prompt.md``. Context files follow
``docs/omp-prompt-system.md`` §5: one project file type wins (candidate priority order), every
directory depth keeps its own file, files inject far ancestor → near ancestor, and a file whose
normalized paragraph sequence is fully contained in a nearer file is dropped.

``<critical>`` and the append text are not part of this block: Hermes already owns them as
``12-critical.md`` and the session append slot, and repeating them in the context tier would
move the closing contract into the middle of the prompt.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from agent.prompts.prompt_sections import render_section

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


_RUNTIME_LABEL_RE = re.compile(r"^[A-Z][A-Za-z0-9 $./_-]*: ")


def workstation_environment() -> list[str]:
    """``OS``/``Arch`` rows plus the runtime host lines for ``<workstation>``."""
    from hermes_platform.host import facts as host_facts

    arch = _MACHINE_ARCH.get(host_facts.native_arch(), host_facts.native_arch())
    os_name = f"{host_facts.os_family()} {host_facts.os_release()}".strip()
    rows = [f"OS: {os_name}"] + ([f"Arch: {arch}"] if arch else [])
    return rows


def model_label(provider: str = "", model: str = "") -> str:
    """``provider/model`` for the workstation line; a model already prefixed by its provider wins."""
    provider, model = (provider or "").strip(), (model or "").strip()
    if not provider or not model or model.startswith(provider + "/"):
        return model
    return f"{provider}/{model}"


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
    include_workspace_tree: bool = False,
    workspace_tree: str = "",
    workspace_tree_truncated: bool = False,
    workspace_roots: Sequence[str] = (),
    active_repo_context: str = "",
    subagent: bool = False,
    append_prompt: str = "",
    environment_probe: bool = True,
    skip_context_files: bool = False,
) -> str:
    """Render ``project-prompt.md``: workstation, ``<repo-rules>``, tail blocks and `<critical>`.

    ``context_files=None`` discovers from *cwd* through the same walk as the legacy
    ``build_context_files_prompt``, with the same install-tree and safe-worker guards;
    explicit files skip discovery (advisor/renderer callers). ``platform`` decides whether
    the launch dir may fall back to the install tree (``cli``/``tui`` only); ``subagent``
    selects the yield rule in ``<critical>``; ``append_prompt`` is the caller's literal
    tail text; ``skip_context_files`` drops the whole block for sessions told to ignore
    project instructions.
    """
    if skip_context_files:
        return ""
    cwd_path = Path(cwd if cwd is not None else os.getcwd()).resolve()
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
    environment_notes = runtime_environment_rows()[1]
    if environment_probe:
        try:
            from tools.env_probe import get_environment_probe_line

            probe = get_environment_probe_line()
        except Exception:  # health: allow BLE001 -- the probe is optional; a failure must not block the prompt
            probe = ""
        if probe:
            environment_notes.append(probe)
    return render_section(
        "project_context",
        {
            "environment": workstation_environment(),
            "environment_notes": environment_notes,
            "model": str(model or ""),
            "provider": str(provider or ""),
            "platform": str(platform or ""),
            "context_files": rows,
            "dir_context_files": [str(path) for path in dir_context_files],
            "include_workspace_tree": bool(include_workspace_tree),
            "workspace_tree": str(workspace_tree or ""),
            "workspace_tree_truncated": bool(workspace_tree_truncated),
            "workspace_roots": [str(root) for root in workspace_roots],
            "active_repo_context": str(active_repo_context or ""),
            "subagent": bool(subagent),
            "append_prompt": str(append_prompt or ""),
        },
    ).strip()


__all__ = [
    "ContextFile",
    "blocks_contain",
    "dedupe_contained_context_files",
    "format_context_file",
    "model_label",
    "render_project_context",
    "runtime_environment_rows",
    "select_context_files",
    "split_comparable_blocks",
    "workstation_environment",
]
