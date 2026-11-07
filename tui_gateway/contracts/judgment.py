"""Local typed-judgment RPC contracts (ADR-17 D6).

``judgment.judge`` / ``judgment.judge_batch`` are the local viewer's proxy onto the one
host service (``agent/judgment/service.py``). They are not exposed to remote platforms;
the backend enforces the schema, budget, accounting, and error types shared with the
Python API and the sandbox ``__judge__`` host ops.
"""

from __future__ import annotations

from typing import Literal

from .base import JsonValue, Result
from .common import ProfileParams
from .registry import method


class JudgmentImageAsset(Result):
    mime_type: str
    data: str | None = None
    path: str | None = None
    detail: str | None = None


class JudgmentParamsBase(ProfileParams):
    questions: dict[str, JsonValue] | None = None
    questions_file: str | None = None
    contexts: dict[str, JsonValue] | None = None
    labels: dict[str, list[str]] | None = None
    purpose: str | None = None
    budget_usd: float | None = None
    timeout_ms: int | None = None


class JudgmentJudgeParams(JudgmentParamsBase):
    state: JsonValue
    images: list[JudgmentImageAsset] | None = None


class JudgmentBatchItem(Result):
    key: str | int
    state: JsonValue
    images: list[JudgmentImageAsset] | None = None


class JudgmentBatchParams(JudgmentParamsBase):
    items: list[JudgmentBatchItem]
    min_ok: int
    max_concurrency: int


class JudgmentUsage(Result):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    request_count: int = 0
    raw_usage: JsonValue = None


class JudgmentJudgeResult(Result):
    kind: Literal["native", "local", "online"]
    api: str
    provider: str
    model: str
    answers: dict[str, JsonValue]
    usage: JudgmentUsage
    cost_usd: float | None
    cached_questions: int = 0


class JudgmentItemError(Result):
    type: str
    message: str


class JudgmentBatchEntry(Result):
    key: str | int
    ok: bool
    result: JudgmentJudgeResult | None = None
    error: JudgmentItemError | None = None


class JudgmentBatchResult(Result):
    results: list[JudgmentBatchEntry]
    min_ok_met: bool


method("judgment.judge", params=JudgmentJudgeParams, result=JudgmentJudgeResult,
       doc="Answer one typed side-channel judgment; local authority only.")
method("judgment.judge_batch", params=JudgmentBatchParams, result=JudgmentBatchResult,
       doc="Run a question set over many states with bounded fan-out; local authority only.")
