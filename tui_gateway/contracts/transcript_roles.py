"""The transcript role set and its single seat table (ADR-10 D10).

``TRANSCRIPT_ROLES`` is the one source for: the session-history role filter, the role entries of
``gateway/panel_inventory.py::TRANSCRIPT_BLOCKS`` / ``runtime.describe.transcript_blocks``, and the
generated TypeScript role union + client seat subset. Never hand-write a second copy.

Wire roles are snake_case. ``TranscriptRole.system`` is a legacy read-compatibility member only: it
is not part of this table, no adapter emits it, and the generated client union omits it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .base import WireEnum
from .common import TranscriptRole

Status = Literal["produced", "partial", "seated"]


@dataclass(frozen=True)
class TranscriptRoleSpec:
    role: WireEnum
    status: Status
    fields: tuple[str, ...] = ()
    needs: str | None = None
    reason: str | None = None


def _spec(role: WireEnum, status: Status, *fields: str, needs: str | None = None,
          reason: str | None = None) -> TranscriptRoleSpec:
    return TranscriptRoleSpec(role, status, fields, needs, reason)


TRANSCRIPT_ROLES: tuple[TranscriptRoleSpec, ...] = (
    _spec(TranscriptRole.user, "produced", "text", "timestamp", "row_id"),
    _spec(TranscriptRole.developer, "seated", "text", "timestamp",
          needs="a framework-instruction producer; today's mechanism rows project as custom",
          reason="no developer-role row is written"),
    _spec(TranscriptRole.assistant, "produced", "text", "timestamp", "reasoning"),
    _spec(TranscriptRole.tool, "produced", "name", "tool_call_id", "content", "args", "labels", "is_error",
          "details"),
    _spec(TranscriptRole.custom, "produced", "custom_type", "display", "details", "attribution",
          "legacy_role"),
    _spec(TranscriptRole.hook_message, "seated", "text",
          needs="a hook/extension injection API that writes a transcript row",
          reason="plugins mutate model context; they do not append hook_message rows"),
    _spec(TranscriptRole.bash_execution, "seated", "command", "output",
          needs="a composer `!` mode that records shell executions as transcript entries",
          reason="no bash-execution row exists"),
    _spec(TranscriptRole.python_execution, "seated", "code", "output", "language",
          needs="a `$` / REPL cell mode that records evaluations as transcript entries",
          reason="no python-execution row exists"),
    _spec(TranscriptRole.file_mention, "seated", "files",
          needs="an `@file` composer node that keeps mentions as their own row",
          reason="@file expands into the user turn's text"),
    _spec(TranscriptRole.branch_summary, "seated", "text", "from_id",
          needs="branch boundaries (from_id) on the flat transcript rows",
          reason="the flat messages table has no branch edge"),
    _spec(TranscriptRole.compaction_summary, "produced", "text", "details"),
)

# The eleven renderer roles, in table order: the generated client union and the role seat subset.
TRANSCRIPT_ROLE_NAMES: tuple[str, ...] = tuple(spec.role.value for spec in TRANSCRIPT_ROLES)

__all__ = [
    "TRANSCRIPT_ROLES",
    "TRANSCRIPT_ROLE_NAMES",
    "TranscriptRoleSpec",
]
