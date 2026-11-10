"""Default-loaded built-in toolsets (design: ``docs/prompt-system.md`` § 默认加载内置工具集).

``toolsets`` may only narrow this list; ``enable_tools``, when non-empty, replaces it with an
explicit tool-name allowlist. Section 06 and 08 under ``§ Runtime`` render from the resulting
tool names so the prompt never advertises a capability the agent does not have.
"""

from __future__ import annotations

from typing import Iterable, Sequence

# Order is the inventory's display order. Adding a toolset here is a product decision: these
# are the groups every stock agent starts with, not "all registered toolsets".
DEFAULT_TOOLSETS: tuple[str, ...] = (
    "browser",
    "clarify",
    "code_execution",
    "cronjob",
    "file",
    "judgment",
    "computer_use",
    "session_search",
    "skills",
    "terminal",
    "todo",
    "vision",
    "web",
    "agents",
    "codegraph",
)


def resolve_tool_names(
    *,
    toolsets: Sequence[str] | None = None,
    enable_tools: Iterable[str] | None = None,
) -> set[str]:
    """Tool names enabled for one agent.

    *enable_tools* wins over *toolsets* whenever it has an entry. With neither, the
    default-loaded list applies. A ``toolsets`` entry must be a built-in toolset (plugin and
    MCP names are not default-loadable) and an unknown name fails closed: a typo must never
    widen the surface to every tool.
    """
    explicit = [str(name).strip() for name in (enable_tools or ()) if str(name).strip()]
    if explicit:
        return set(explicit)
    selected = tuple(toolsets) if toolsets is not None else DEFAULT_TOOLSETS
    from toolsets import get_toolset, resolve_toolset

    unknown = [name for name in selected if get_toolset(name, include_registry=False) is None]
    if unknown:
        raise ValueError(f"unknown built-in toolset(s): {', '.join(unknown)}")

    names: set[str] = set()
    for name in selected:
        names.update(resolve_toolset(name))
    return names


def tool_inventory_rows(available: set[str], *, coding_project: bool) -> list[str]:
    """Two-level inventory lines: toolset header, then each available tool with its description.

    Default-loaded groups come first in display order. Tools from outside those groups
    (``enable_tools`` may name any tool) are grouped by their registered toolset afterwards,
    so an explicit allowlist never renders an empty inventory.
    """
    from tools.registry import registry
    from toolsets import get_toolset

    rows: list[str] = []
    covered: set[str] = set()

    def add_group(toolset_name: str, definition: dict, members: list[str]) -> None:
        if not members:
            return
        description = str(definition.get("description") or "tools").strip()
        rows.append(f"- {toolset_name}: {description}".rstrip(": "))
        for tool in members:
            schema = registry.get_schema(tool)
            tool_description = ""
            if isinstance(schema, dict):
                tool_description = str(schema.get("description") or "").split("\n", 1)[0].strip()
            rows.append(f"  - {tool}: {tool_description}".rstrip(": "))

    for toolset_name in DEFAULT_TOOLSETS:
        definition = get_toolset(toolset_name) or {}
        if toolset_name == "codegraph" and not coding_project:
            # Its tools must not leak into the "extra" pass below the coding-project gate.
            covered.update(definition.get("tools", ()))
            continue
        members = sorted(name for name in definition.get("tools", ()) if name in available)
        covered.update(members)
        add_group(toolset_name, definition, members)

    def toolset_for(tool: str) -> str:
        registered = registry.get_toolset_for_tool(tool)
        if registered:
            return registered
        # A caller may render before tool discovery populated the registry; the static
        # TOOLSETS table still knows every built-in tool.
        from toolsets import TOOLSETS

        for name, definition in TOOLSETS.items():
            if tool in definition.get("tools", ()):
                return name
        return "tools"

    extras: dict[str, list[str]] = {}
    for tool in sorted(available - covered):
        extras.setdefault(toolset_for(tool), []).append(tool)
    for toolset_name in sorted(extras):
        add_group(toolset_name, get_toolset(toolset_name) or {}, extras[toolset_name])
    return rows


__all__ = ["DEFAULT_TOOLSETS", "resolve_tool_names", "tool_inventory_rows"]
