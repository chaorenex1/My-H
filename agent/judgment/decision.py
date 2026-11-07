"""Decision table for ``judgrg`` (ADR-11 D14), loaded from ``prompts/find.json``.

The table mirrors omp's ``prompts/judge`` JSON: one question with fixed criteria.
It is data, not code, so the intent vocabulary and the tool preference per intent
can be reviewed and changed without touching the engine. A broken table raises
``JudgmentError`` at call time; it never crashes import or a session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from agent.judgment.types import JudgmentError

PROMPT_DIR = Path(__file__).parent / "prompts"
MODES = frozenset({"content", "ast", "hybrid"})


@dataclass(frozen=True)
class IntentPlan:
    label: str
    tool: str
    mode: str


@dataclass(frozen=True)
class DecisionTable:
    instructions: str
    criteria: Mapping[str, str]
    plans: Mapping[str, IntentPlan]

    def plan_for(self, label: str) -> IntentPlan:
        return self.plans.get(label) or self.plans["unknown_implementation"]


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JudgmentError(f"judgment decision table: {what} must be a non-empty string")
    return value


def parse_decision(data: Any) -> DecisionTable:
    """Validate one decision document; shared by the loader and tests."""
    if not isinstance(data, dict):
        raise JudgmentError("judgment decision table must be a JSON object")
    intent = data.get("intent")
    plans = data.get("plans")
    if not isinstance(intent, dict) or not isinstance(plans, dict):
        raise JudgmentError("judgment decision table needs `intent` and `plans` objects")
    instructions = _text(intent.get("instructions"), "intent.instructions")
    raw_criteria = intent.get("criteria")
    if not isinstance(raw_criteria, dict) or not raw_criteria:
        raise JudgmentError("judgment decision table: intent.criteria must be a non-empty object")
    criteria = {str(label): _text(text, f"criteria.{label}") for label, text in raw_criteria.items()}
    parsed: dict[str, IntentPlan] = {}
    for label, entry in plans.items():
        if label not in criteria:
            raise JudgmentError(f"judgment decision table: plan {label!r} has no criterion")
        if not isinstance(entry, dict):
            raise JudgmentError(f"judgment decision table: plan {label!r} must be an object")
        mode = _text(entry.get("mode"), f"plans.{label}.mode")
        if mode not in MODES:
            raise JudgmentError(
                f"judgment decision table: plan {label!r} mode {mode!r} is not one of {sorted(MODES)}")
        parsed[str(label)] = IntentPlan(str(label), _text(entry.get("tool"), f"plans.{label}.tool"), mode)
    missing = sorted(set(criteria) - set(parsed))
    if missing:
        raise JudgmentError(
            f"judgment decision table: criteria without a plan: {', '.join(missing)}")
    if "unknown_implementation" not in parsed:
        raise JudgmentError("judgment decision table: plan 'unknown_implementation' is required")
    return DecisionTable(instructions, criteria, parsed)


@lru_cache(maxsize=4)
def load_decision(name: str = "find") -> DecisionTable:
    """The packaged decision table; a missing file is an explicit JudgmentError."""
    path = PROMPT_DIR / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise JudgmentError(f"judgment decision table {name}.json is unreadable: {exc}") from None
    return parse_decision(data)
