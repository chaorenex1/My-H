"""Platform hint resolution: built-in texts, plugin adapters, config overlays.

Built-in hint texts are authored as data under ``system/platforms/``. Only core surfaces
live there; a messaging platform's hint ships with its plugin adapter
(``PlatformEntry.platform_hint``), never in this directory.

Resolution order matches the prompt's history: default hint → ``platform_hints.<platform>``
config override → desktop embedded-terminal clarifier → cron delivery-channel hint.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Mapping, Sequence

from agent.prompts.prompt_sections import SYSTEM_PROMPT_DIR, read_prompt_text
from utils import is_truthy_value

logger = logging.getLogger(__name__)

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

_TUI_EMBEDDED_PANE_CLARIFIER = (
    " You're in its embedded terminal pane, beside the GUI chat — the user can "
    "select your output (Option-drag on macOS, Shift-drag elsewhere) and press "
    "Cmd/Ctrl+L to send it to the chat composer."
)


@lru_cache(maxsize=1)
def load_platform_hints() -> dict[str, str]:
    """Platform name (file stem) -> default hint text."""
    return {
        path.stem: read_prompt_text(path).rstrip("\n")
        for path in sorted(PLATFORM_HINTS_DIR.glob("*.md"))
    }


def tui_embedded_pane_clarifier(hint: str) -> str:
    """Append the desktop embedded-terminal clarifier when ``HERMES_DESKTOP_TERMINAL``
    is set (only the desktop's TUI PTY, never the chat backend). Idempotent."""
    if not hint or _TUI_EMBEDDED_PANE_CLARIFIER in hint or not is_truthy_value(os.getenv("HERMES_DESKTOP_TERMINAL")):
        return hint
    return hint + _TUI_EMBEDDED_PANE_CLARIFIER


def apply_override(overrides: Mapping[str, Any] | None, platform_key: str, default_hint: str) -> str:
    """Apply the ``platform_hints.<platform>`` config override: ``replace``
    substitutes the default, ``append`` adds text (a bare string is shorthand
    for append). Malformed entries fall back to the unmodified default so bad
    config can never break prompt assembly or leak across platforms."""
    spec = overrides.get(platform_key) if platform_key and isinstance(overrides, Mapping) else None
    if isinstance(spec, str):
        spec = {"append": spec}
    if not isinstance(spec, Mapping):
        return default_hint
    replace_text, append_text = (v.strip() if isinstance(v, str) else "" for v in (spec.get("replace"), spec.get("append")))
    base = replace_text or default_hint
    return f"{base}\n\n{append_text}".strip() if append_text else base


def _telegram_rich_messages_enabled() -> bool:
    """``rich_messages`` from the Telegram ``extra`` config; same precedence the
    adapter uses (top-level ``platforms.telegram.extra`` overrides
    ``gateway.platforms.telegram.extra`` at the leaf). False on any read failure."""
    try:
        from hermes_cli.config import load_config_readonly
        _cfg = load_config_readonly()
        _gw = (((_cfg.get("gateway") or {}).get("platforms") or {}).get("telegram") or {}).get("extra")
        _top = ((_cfg.get("platforms") or {}).get("telegram") or {}).get("extra")
        merged = {**(_gw if isinstance(_gw, dict) else {}), **(_top if isinstance(_top, dict) else {})}
        return bool(merged.get("rich_messages"))
    except Exception:
        return False


def default_platform_hint(platform_key: str, tool_names: Sequence[str] = ()) -> str:
    """Built-in hint, else the plugin adapter's ``platform_hint`` plus its
    ``platform_hint_tool_notes`` for tools in *tool_names* (the session's toolset), else ``""``."""
    hint = load_platform_hints().get(platform_key, "")
    if not hint and platform_key:
        try:
            from gateway.platform_registry import platform_registry
            entry = platform_registry.get(platform_key)
            if entry is None:
                # A prompt can be built before anything ran plugin discovery (direct builders,
                # one-shot callers); the hint lives on the plugin's entry, so discover (idempotent).
                from hermes_cli.plugins import discover_plugins
                discover_plugins()
                entry = platform_registry.get(platform_key)
            if entry is not None:
                hint = str(entry.platform_hint or "")
                notes = [note for tool, note in (entry.platform_hint_tool_notes or {}).items()
                         if tool in tuple(tool_names or ())]
                if hint and notes:
                    hint = " ".join([hint.rstrip(), *notes])
        except Exception:  # health: allow BLE001 -- plugin discovery/entry lookup is optional; a broken adapter must not break prompt assembly
            logger.debug("platform hint plugin lookup failed", exc_info=True)
    if platform_key == "telegram" and hint and _telegram_rich_messages_enabled():
        from agent.prompt_builder import TELEGRAM_RICH_MESSAGES_HINT

        hint = hint.rstrip() + " " + TELEGRAM_RICH_MESSAGES_HINT
    return hint


def cron_delivery_hint(
    tool_names: Sequence[str] = (),
    overrides: Mapping[str, Any] | None = None,
) -> str:
    """The destination channel's hint for a cron agent (default + its ``platform_hints`` override).

    A cron agent runs as platform ``cron`` but its final response lands on the job's ``deliver``
    channel, so without this the model never learns that MEDIA: tags become Slack/Telegram
    attachments or that tables do not render there. The scheduler publishes the primary
    auto-deliver target into the session ContextVar before the agent runs.
    """
    try:
        from gateway.session_context import get_session_env

        deliver_key = get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").lower().strip()
        if not deliver_key or deliver_key == "cron":
            return ""
        hint = apply_override(
            platform_hint_overrides() if overrides is None else overrides,
            deliver_key,
            default_platform_hint(deliver_key, tool_names),
        )
    except Exception:  # health: allow BLE001 -- no session scope outside cron turns; fall back to no delivery hint
        return ""
    return f"Delivery destination ({deliver_key}): {hint}" if hint else ""


def platform_hint_overrides() -> dict[str, Any]:
    """``platform_hints`` from the active profile's config; ``{}`` when unreadable."""
    try:
        from hermes_cli.config import load_config_readonly

        value = (load_config_readonly() or {}).get("platform_hints")
        return dict(value) if isinstance(value, Mapping) else {}
    except Exception:  # health: allow BLE001 -- unreadable config must not break prompt assembly
        return {}


def resolve_platform_hint(
    platform: str = "",
    *,
    overrides: Mapping[str, Any] | None = None,
    tool_names: Sequence[str] = (),
) -> str:
    """The full hint for *platform*: default → config override → clarifier → cron delivery."""
    platform_key = (platform or "").lower().strip()
    hint = apply_override(
        platform_hint_overrides() if overrides is None else overrides,
        platform_key,
        default_platform_hint(platform_key, tool_names),
    )
    if platform_key == "tui" and hint:
        hint = tui_embedded_pane_clarifier(hint)
    if platform_key == "cron":
        delivery = cron_delivery_hint(tool_names, overrides)
        if delivery:
            hint = f"{hint}\n\n{delivery}".strip()
    return hint


__all__ = [
    "IDENTITY_CAPS",
    "PLATFORM_HINTS_DIR",
    "apply_override",
    "cron_delivery_hint",
    "default_platform_hint",
    "load_platform_hints",
    "platform_hint_overrides",
    "resolve_platform_hint",
    "tui_embedded_pane_clarifier",
]
