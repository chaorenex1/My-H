"""Resume helpers split out of ``methods_session.py`` (file-line ratchet).

``method_ctx.bind_module`` rebinds these onto server.py's globals at registration, so the
body still closes over ``logger``/``_err`` exactly as it did in the parent module.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from .method_ctx import HandlerRegistry, bind_module

logger = logging.getLogger(__name__)

_registry = HandlerRegistry()


def _resume_follow_tip(ctx: Any) -> None:
    """Rebind a rotated-out parent id to its compression tip (resuming the original reloads the parent
    transcript and loses the post-compression reply). Skipped for lazy watch windows (exact child); Bot Chat
    follows proven compression edges only."""
    if not ctx.found or ctx.lazy:
        return
    tip = ctx.target
    with contextlib.suppress(Exception):
        from tools.bot_mode_probe import BOT_CHAT_TITLE
        if (ctx.found.get("title") or "").strip() == BOT_CHAT_TITLE:
            tip = ctx.db.get_compression_tip(ctx.target) or ctx.target
        else:
            tip = ctx.db.resolve_resume_session_id(ctx.target)
    if tip and tip != ctx.target:
        ctx.target = tip
        ctx.found = ctx.db.get_session(tip) or ctx.found


def _resume_restore_offloaded(ctx: Any) -> str | None:
    """Restore an offloaded session from its cold mirror before any history read (ADR-16 D8).

    A session whose message rows were pruned by ``sessions mirror --prune-source`` must not
    load as an empty conversation. Verification failure is a refusal, never an empty resume.
    Returns the refusal message, or None after a successful restore.
    """
    if not ctx.found or not ctx.found.get("offloaded"):
        return None
    from hermes_session_mirror import MirrorError, ensure_session_restored

    db_path = getattr(ctx.db, "db_path", None)
    home = Path(db_path).parent if db_path else None
    try:
        refreshed = ensure_session_restored(ctx.db, ctx.target, home=home)
    except MirrorError as exc:
        return f"session {ctx.target} is archived and restore failed: {exc}"
    except Exception as exc:  # health: allow BLE001 -- resume must refuse, not open an empty chat
        logger.warning("resume: restore of offloaded %s failed", ctx.target, exc_info=True)
        return f"session {ctx.target} restore failed: {exc}"
    ctx.found = refreshed or ctx.found
    return None


def register(server) -> None:
    bind_module(globals(), server)
    _registry.install(server)
