"""Local prewalk handoff RPC contracts (ADR-12 D5 / ADR-17 P5).

``prewalk.arm/restart/off/status`` are session-scoped, local-authority verbs over
``agent/prewalk.py``. The handoff is session state persisted in the session row's
``model_config``; it never adds a transcript row. ``prewalk.armed`` announces a state change and
``prewalk.handoff`` the one boundary where the model actually switched.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import JsonValue, Payload, Result
from .common import ProfileParams
from .registry import event, method


class PrewalkSessionParams(ProfileParams):
    session_id: str = Field(min_length=1)


class PrewalkArmParams(PrewalkSessionParams):
    """``/prewalk [target] [from]``: a target/planning selector overrides the config block."""

    target: str | None = None
    from_: str | None = Field(default=None, alias="from")


class PrewalkStatusResult(Result):
    enabled: bool = False
    armed: bool = False
    from_: str = Field(default="", alias="from")
    into: str = ""
    consumed: bool = False
    #: True when this arm came from the auto classifier rather than an explicit command.
    auto: bool = False
    auto_mode: Literal["off", "suggest", "on"] = "off"
    auto_target: str = ""
    planning_model: str = ""
    planning_thinking: JsonValue = None
    #: ``closed`` until a successful todo call (only when the Todo tool is available).
    gate: Literal["open", "closed"] = "open"


class PrewalkArmedPayload(Payload):
    enabled: bool = False
    armed: bool = False
    from_: str = Field(default="", alias="from")
    into: str = ""
    auto: bool = False
    auto_target: str = ""


class PrewalkHandoffPayload(Payload):
    from_model: str = ""
    into_model: str = ""
    provider: str = ""
    consumed: bool = True


method("prewalk.status", params=PrewalkSessionParams, result=PrewalkStatusResult,
       doc="Read the session's one-shot prewalk handoff state; local authority only.")
method("prewalk.arm", params=PrewalkArmParams, result=PrewalkStatusResult,
       doc="Arm the one-shot handoff (works even while prewalk.enabled is false).")
method("prewalk.restart", params=PrewalkArmParams, result=PrewalkStatusResult,
       doc="Return to the planning model/thinking, then arm a fresh handoff.")
method("prewalk.off", params=PrewalkSessionParams, result=PrewalkStatusResult,
       doc="Disarm the pending handoff without changing its planning history.")

event("prewalk.armed", PrewalkArmedPayload,
      doc="The session's prewalk arm state changed (arm/restart/off).")
event("prewalk.handoff", PrewalkHandoffPayload,
      doc="The one-shot handoff switched the session's active model at a tool-batch boundary.")
