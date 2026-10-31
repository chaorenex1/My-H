"""Plan-review and BTW-history panel contracts.

``session.plan.*`` reads and writes the newest markdown plan in the session workspace's
``.hermes/plans``; ``btw.history`` reads the answers the session's ``prompt.btw`` side questions
produced. Handlers: ``gateway/session_plan.py`` and ``gateway/session_btw_history.py``.
"""

from __future__ import annotations

from pydantic import Field

from .base import Result, WireEnum
from .common import SessionParams
from .registry import canonical_method


class BtwHistoryParams(SessionParams):
    limit: int | None = None


class BtwHistoryItem(Result):
    id: str
    question: str = ""
    answer: str = ""
    created_at: str
    task_id: str | None = None


class BtwHistoryRecord(Result):
    """The panel's row: the same stored answer, newest first, with an epoch-millis time."""

    id: str
    question: str = ""
    answer: str = ""
    created_at: str | None = None
    created_at_ms: int | None = None
    status: str = "completed"
    task_id: str | None = None


class BtwHistoryResult(Result):
    items: list[BtwHistoryItem] = Field(default_factory=list)
    records: list[BtwHistoryRecord] = Field(default_factory=list)


canonical_method("btw.history", params=BtwHistoryParams, result=BtwHistoryResult,
                 doc="Answers this session's /btw side questions: ``items`` oldest first, "
                     "``records`` newest first.")


class PlanReadParams(SessionParams):
    pass


class PlanStatus(WireEnum):
    none = "none"
    draft = "draft"
    approved = "approved"


class PlanReadResult(Result):
    path: str | None = None
    text: str = ""
    status: PlanStatus = PlanStatus.none
    updated_at: float | None = None


canonical_method("session.plan.read", params=PlanReadParams, result=PlanReadResult,
                 doc="The newest plan in the session workspace's ``.hermes/plans`` and its approval state.")


class PlanSaveParams(SessionParams):
    text: str
    request_id: str


class PlanSaveResult(Result):
    path: str
    updated_at: float


canonical_method("session.plan.save", params=PlanSaveParams, result=PlanSaveResult,
                 doc="Write the plan markdown in the session workspace and reset it to draft.")


class PlanApproveParams(SessionParams):
    request_id: str


class PlanApproveResult(Result):
    status: PlanStatus


canonical_method("session.plan.approve", params=PlanApproveParams, result=PlanApproveResult,
                 doc="Flip the saved plan to approved; never resumes execution.")
