"""Convert omp's bundled agent snapshots into Hermes agent definitions.

``docs/bundled-agents/`` is the vendored omp source: its front matter speaks omp's vocabulary
(``tools``, ``thinking-level``) and its prose names omp tools (``read``, ``grep``, ``glob``,
``agent://``). The converted definitions ship as the ``agent-presets`` skill
(``skills/agent-presets/agents/``), so this build step translates the front matter to the
enforced contract and rewrites the prose to the Hermes tool names.

Unknown fields and unknown tool names fail closed: a converted file must never silently widen a
model's surface. Run ``python scripts/build_bundled_agents.py`` after refreshing the snapshots;
``--check`` verifies the checked-in output is current.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "docs" / "bundled-agents"
OUTPUT_DIR = ROOT / "skills" / "agent-presets" / "agents"

# Direct execution puts ``scripts/`` on sys.path, not the checkout root; project imports below
# must resolve to this tree rather than an installed copy.
if __package__ in (None, "") and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hermes_yaml

#: omp tool name -> Hermes registry name. A definition that never had ``bash`` must stay
#: read-only: ``grep``/``glob``/``ast_grep`` map to search-only tools, not ``terminal`` (which
#: would silently grant shell write access omp never gave these agents). ``terminal`` appears
#: only when omp itself granted ``bash``.
OMP_TOOL_MAP: dict[str, str] = {
    "read": "read_file",
    "find": "judgrg",
    "grep": "search_files",
    "glob": "search_files",
    "bash": "terminal",
    "lsp": "code_intel",
    "web_search": "web_search",
    "ast_grep": "judgrg",
    "ast_edit": "judgrg",
    "write": "write_file",
    "edit": "patch",
    "eval": "execute_code",
    "proc": "process_manage",
}

#: With a shell, ripgrep/ast-grep are run through `terminal` (the Hermes-supported path).
_SHELL_TOOL_MAP: dict[str, str] = {
    **OMP_TOOL_MAP,
    "grep": "terminal",
    "glob": "terminal",
    "ast_grep": "terminal",
    "ast_edit": "terminal",
}

#: omp front-matter keys this converter understands. ``prewalk`` has no Hermes equivalent yet,
#: so encountering it is a hard failure rather than a silently dropped switch.
_OMP_KEYS = frozenset({
    "name", "description", "tools", "spawns", "model", "thinking-level", "blocking",
    "advisor", "autoloadSkills", "read-summarize", "output",
})

#: Fields emitted in this order so regeneration is byte-stable.
_EMIT_ORDER = (
    "name", "description", "enable_tools", "toolsets", "spawns", "model",
    "reasoning_effort", "blocking", "advisor", "autoloadSkills", "read-summarize",
    "worktree", "skip_memory", "skip_context_files", "skip_background_review",
    "load_soul_identity", "max_iterations", "structured", "output",
)

_DUPLICATE_TOOL = re.compile(r"`([^`]+)`(?:(?:, )?(?:and )?`\1`)+")
_DUPLICATE_SLASH = re.compile(r"`([^`]+)`/`\1`")


class ConversionError(ValueError):
    """A snapshot that cannot be translated without guessing."""


def _tool_names(value: Any, source: str) -> list[str]:
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, (list, tuple)):
        raise ConversionError(f"{source}: tools must be a CSV string or an array")
    parsed: list[str] = []
    for raw in names:
        for part in str(raw).split(","):
            name = part.strip()
            if name and name not in parsed:
                parsed.append(name)
    return parsed


def _tools(value: Any, source: str, *, shell: bool) -> list[str]:
    mapping = _SHELL_TOOL_MAP if shell else OMP_TOOL_MAP
    converted: list[str] = []
    for name in _tool_names(value, source):
        mapped = mapping.get(name)
        if mapped is None:
            raise ConversionError(f"{source}: unknown omp tool {name!r}")
        if mapped not in converted:
            converted.append(mapped)
    if not converted:
        raise ConversionError(f"{source}: tools must not be empty")
    return converted


def _thinking_level(value: Any, source: str) -> str:
    level = str(value or "").strip().lower()
    if not level or level == "auto":
        return ""
    from hermes_constants import parse_reasoning_effort

    if parse_reasoning_effort(level) is None:
        raise ConversionError(f"{source}: unknown thinking-level {value!r}")
    return level


def _emit_ordered(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: values[key] for key in _EMIT_ORDER if key in values}


def convert_frontmatter(raw: Mapping[str, Any], *, source: str,
                        shell: bool = False) -> dict[str, Any]:
    """Translate omp front matter into the Hermes definition contract.

    *shell* is whether the same definition granted ``bash``; it decides whether ripgrep and
    ast-grep searches map to ``terminal`` or to the read-only search tools.
    """
    unknown = sorted(set(raw) - _OMP_KEYS)
    if unknown:
        raise ConversionError(f"{source}: unsupported omp field(s): {', '.join(unknown)}")

    from tools import agent_definitions

    converted: dict[str, Any] = {}
    for key in ("name", "description"):
        if key in raw:
            converted[key] = str(raw[key] or "").strip()
    if "tools" in raw:
        converted["enable_tools"] = _tools(raw["tools"], source, shell=shell)
    if "spawns" in raw:
        spawns = agent_definitions._spawns(raw["spawns"], source)
        if spawns is not None:
            converted["spawns"] = "*" if spawns == ["*"] else spawns
    if "model" in raw:
        converted["model"] = agent_definitions._model(raw["model"], source)
    if "thinking-level" in raw:
        effort = _thinking_level(raw["thinking-level"], source)
        if effort:
            converted["reasoning_effort"] = effort
    for key in ("blocking", "advisor", "read-summarize", "worktree"):
        if key in raw:
            converted[key] = raw[key]
    if "autoloadSkills" in raw:
        converted["autoloadSkills"] = agent_definitions._skill_names(
            raw["autoloadSkills"], source)
    if "output" in raw:
        converted["output"] = agent_definitions.normalize_output(raw["output"], source)
    return _emit_ordered(converted)


def _rewrite_body(body: str, *, source: str, shell: bool = False) -> str:
    """Rewrite omp tool/URI references in prose; unknown hangovers fail closed."""
    text = body
    for omp_name, read_only_target in OMP_TOOL_MAP.items():
        # Prose names the tool the model can actually call; ast-grep stays a binary only when
        # the definition has a shell.
        if shell and omp_name in {"grep", "glob", "ast_grep", "ast_edit"}:
            target = "ast-grep" if omp_name.startswith("ast_") else "terminal"
        else:
            target = read_only_target
        text = text.replace(f"`{omp_name}`", f"`{target}`")
    text = text.replace(
        "Tools: FULL access (edit, write, bash, grep, read, etc.);",
        "Tools: FULL access (patch, write_file, terminal, read_file, etc.);")
    text = text.replace("`task` delegation:", "`agent` delegation:")
    text = text.replace("`agent://all`", "`agent_message` (broadcast)")
    text = re.sub(r"`agent://([^`/]+)`", lambda m: f"`agent_message` (to `{m.group(1)}`)", text)
    text = text.replace("`proc://", "`process_manage` (`")
    text = re.sub(r"`history://([^`/]+)`", lambda m: f"`agent_out` (`mode=\"history\"`, id `{m.group(1)}`)", text)
    text = text.replace("`history://`", "`agent_out` (`mode=\"history\"`)")
    text = re.sub(r"`artifact://([^`/]+)`", lambda m: f"`agent_out` (`mode=\"artifact\"`, id `{m.group(1)}`)", text)
    text = text.replace("`artifact://`", "`agent_out` (`mode=\"artifact\"`)")
    text = _DUPLICATE_SLASH.sub(lambda m: f"`{m.group(1)}`", text)
    text = _DUPLICATE_TOOL.sub(lambda m: f"`{m.group(1)}`", text)
    for marker in ("agent://", "proc://", "history://", "artifact://"):
        if marker in text:
            raise ConversionError(f"{source}: body still references {marker}")
    return text.strip()


def _frontmatter_and_body(text: str, *, source: str) -> tuple[dict[str, Any], str]:
    """Strict front-matter split: a broken YAML block is an error, never a line-split guess.

    ``agent.skill_utils.parse_frontmatter`` falls back to ``key: value`` line splitting, which
    turned a nested ``output:`` schema into bogus top-level fields. The converter must see the
    real load error instead.
    """
    clean = text.removeprefix("\ufeff")
    match = re.search(r"\n---\s*\n", clean[3:]) if clean.startswith("---") else None
    if match is None:
        raise ConversionError(f"{source}: missing front matter")
    block = clean[3:match.start() + 3]
    try:
        raw = hermes_yaml.safe_load(block)
    except Exception as exc:  # health: allow BLE001 -- report the real parse error
        raise ConversionError(f"{source}: front matter is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConversionError(f"{source}: front matter must be a mapping")
    return dict(raw), clean[match.end() + 3:]


def convert_text(text: str, *, source: str) -> str:
    """One omp definition (front matter + body) as a Hermes definition file."""
    from tools import agent_definitions

    raw, body = _frontmatter_and_body(text, source=source)
    if not raw:
        raise ConversionError(f"{source}: missing front matter")
    raw_tools = raw.get("tools")
    shell = "bash" in _tool_names(raw_tools, source) if raw_tools is not None else False
    metadata = convert_frontmatter(raw, source=source, shell=shell)
    converted_body = _rewrite_body(body, source=source, shell=shell)
    if not converted_body:
        raise ConversionError(f"{source}: empty definition body")

    # Refuse to write a file the runtime would not be able to parse.
    payload = "---\n" + hermes_yaml.safe_dump(
        metadata, sort_keys=False, allow_unicode=True, default_flow_style=False,
        width=4096) + "---\n" + converted_body + "\n"
    _parsed, parsed_body = agent_definitions.parse_definition(payload, source=source)
    if not parsed_body:
        raise ConversionError(f"{source}: converted body did not parse")
    if "enable_tools" in metadata:
        from toolsets import TOOLSETS

        known = {tool for definition in TOOLSETS.values()
                 for tool in definition.get("tools", ())}
        missing = [tool for tool in metadata["enable_tools"] if tool not in known]
        if missing:
            raise ConversionError(f"{source}: unknown Hermes tool(s): {', '.join(missing)}")
    return payload


def build(source_dir: Path = SOURCE_DIR, output_dir: Path = OUTPUT_DIR) -> list[Path]:
    """Convert every source snapshot; returns the written (*or compared*) files."""
    written: list[Path] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(source_dir.glob("*.md")):
        if path.name.startswith(".") or path.name.upper() == "README.MD":
            continue
        converted = convert_text(path.read_text(encoding="utf-8-sig"), source=str(path))
        destination = output_dir / path.name
        destination.write_text(converted, encoding="utf-8", newline="\n")
        written.append(destination)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail when skills/agent-presets/agents/ differs from a fresh conversion")
    args = parser.parse_args(argv)
    if not args.check:
        build()
        return 0
    stale: list[Path] = []
    for path in sorted(SOURCE_DIR.glob("*.md")):
        if path.name.startswith(".") or path.name.upper() == "README.MD":
            continue
        expected = convert_text(path.read_text(encoding="utf-8-sig"), source=str(path))
        destination = OUTPUT_DIR / path.name
        actual = destination.read_text(encoding="utf-8-sig") if destination.is_file() else ""
        if actual != expected:
            stale.append(destination)
    for path in stale:
        print(f"stale bundled agent: {path.relative_to(ROOT)}", file=sys.stderr)
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
