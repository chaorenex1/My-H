"""Shared choice-question reranker for capability search (ADR-18 D9).

``tool_search`` and ``skills_list(query=...)`` use this module: the lexical layer
(BM25 / rg) produces candidates, one or more ``ChoiceQuestion``s are built over
their names + descriptions, ``judge_batch`` answers them, and the picks are merged
in front of the lexical order. Every failure path returns no picks so the caller
falls back to the lexical result; this module never raises into a tool.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Sequence

from agent.judgment.questions import load_bundled_document
from agent.judgment.service import BatchItem, judge_batch
from agent.judgment.types import ChoiceAnswer

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 3
MAX_CANDIDATES_PER_QUESTION = 8
MAX_CANDIDATES = MAX_QUESTIONS * MAX_CANDIDATES_PER_QUESTION
JUDGE_TIMEOUT_MS = 5000
NONE_LABEL = "none"
_NONE_CRITERION = "No candidate in this list is a plausible match."


def judgment_route_available() -> bool:
    """Whether capability-search judging is reachable (config + credentials, no network)."""
    try:
        from agent.judgment.chain import has_native_judge
        from agent.judgment.config import load_judgment_settings

        settings = load_judgment_settings()
        if settings.tools_enabled == "off":
            return False
        if settings.tools_enabled == "on":
            return True
        return has_native_judge()
    except Exception:  # health: allow BLE001 -- unreachable judgment means lexical fallback
        return False


def merge_ranked(*groups: Iterable[str]) -> list[str]:
    """Concatenate name groups, keeping the first occurrence of each name."""
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for name in group:
            if name and name not in seen:
                seen.add(name)
                merged.append(name)
    return merged


def _dedupe_candidates(candidates: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name, description in candidates:
        name = str(name or "").strip()
        if not name or name in seen or name == NONE_LABEL:
            continue
        seen.add(name)
        out.append((name, str(description or "")))
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def _instructions(question_file: str) -> str:
    document = load_bundled_document(question_file)
    spec = document.get("choice")
    if not isinstance(spec, Mapping) or not isinstance(spec.get("instructions"), str):
        raise ValueError(f"bundled question {question_file!r} has no choice.instructions")
    return str(spec["instructions"]).strip()


def _question_specs(question_file: str, candidates: Sequence[tuple[str, str]],
                    max_questions: int) -> dict[str, Any]:
    instructions = _instructions(question_file)
    per_question = MAX_CANDIDATES_PER_QUESTION
    limit = max(1, min(int(max_questions), MAX_QUESTIONS)) * per_question
    groups = [list(candidates[i:i + per_question])
              for i in range(0, min(len(candidates), limit), per_question)]
    specs: dict[str, Any] = {}
    for index, group in enumerate(groups, 1):
        criteria: dict[str, str | None] = {
            name: description or None for name, description in group}
        criteria[NONE_LABEL] = _NONE_CRITERION
        specs[f"choice_{index}"] = {"instructions": instructions, "criteria": criteria}
    return specs


def ask_choices(
    queries: Sequence[str],
    candidates: Iterable[tuple[str, str]],
    *,
    question_file: str,
    purpose: str,
    max_questions: int = MAX_QUESTIONS,
    timeout_ms: int = JUDGE_TIMEOUT_MS,
) -> dict[int, list[str]]:
    """Judge which candidates satisfy each query; ``{query_index: picks}``, ``{}`` on failure."""
    pool = _dedupe_candidates(candidates)
    if not pool or not queries:
        return {}
    try:
        specs = _question_specs(question_file, pool, max_questions)
        if not specs:
            return {}
        items = [BatchItem(key=index, state={"query": str(query), "kind": purpose})
                 for index, query in enumerate(queries)]
        batch = judge_batch(
            items, questions=specs, purpose=purpose, timeout_ms=timeout_ms, min_ok=1)
    except Exception as exc:  # health: allow BLE001 -- capability search falls back lexically
        logger.debug("choice judgment failed (%s); falling back to lexical results", exc)
        return {}
    picks: dict[int, list[str]] = {}
    for item in batch.results:
        if not item.ok or item.result is None:
            continue
        chosen: list[str] = []
        for question_id in specs:
            answer = item.result.answers.get(question_id)
            if isinstance(answer, ChoiceAnswer) and answer.choice != NONE_LABEL:
                chosen.append(answer.choice)
        if chosen:
            picks[int(item.key)] = merge_ranked(chosen)
    return picks


__all__ = [
    "MAX_CANDIDATES",
    "MAX_CANDIDATES_PER_QUESTION",
    "MAX_QUESTIONS",
    "ask_choices",
    "judgment_route_available",
    "merge_ranked",
]
