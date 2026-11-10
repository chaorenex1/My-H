"""Built-in platform hint texts, authored as data under ``system/platforms/``.

Only core surfaces live here; ``system/platforms/*.md`` is the authoritative list.
A messaging platform's hint ships with its plugin adapter (``PlatformEntry.platform_hint``,
read by ``agent/system_prompt._default_platform_hint``), never in this directory.

Each file's text is the exact default hint; the trailing newline is file formatting and is
stripped on load so a checkout's newline convention never reaches the prompt.
"""

from __future__ import annotations

from functools import lru_cache

from agent.prompts.prompt_sections import SYSTEM_PROMPT_DIR, read_prompt_text

PLATFORM_HINTS_DIR = SYSTEM_PROMPT_DIR / "platforms"

# Identity capability gates that follow a platform's own hint. A platform absent from
# this table keeps the identity template defaults; a caller-supplied value wins.
# ``cli`` and ``api_server`` state in their hints that markdown does not render, so the
# table/chart, mermaid and inline-SVG instructions would be dead text there; ``cron`` has
# no user present, so none of the conversational/visual bullets apply.
IDENTITY_CAPS: dict[str, dict[str, bool]] = {
    "cli": {"auto_graph": False, "render_mermaid": False, "render_svg": False},
    "api_server": {"auto_graph": False, "render_mermaid": False, "render_svg": False},
    "cron": {"auto_graph": False, "render_mermaid": False, "render_svg": False, "reactions": False},
}


@lru_cache(maxsize=1)
def load_platform_hints() -> dict[str, str]:
    """Platform name (file stem) -> default hint text."""
    return {
        path.stem: read_prompt_text(path).rstrip("\n")
        for path in sorted(PLATFORM_HINTS_DIR.glob("*.md"))
    }


__all__ = ["IDENTITY_CAPS", "PLATFORM_HINTS_DIR", "load_platform_hints"]
