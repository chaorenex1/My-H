"""Kanban guidance and first-input detection for ``§ Runtime``.

The guidance is packaged under ``system/kanban-guidance.md`` (the same text the identity
slot uses) and is appended to the internal-URLs block only when both hold:

* the first user message asks to use the shared Kanban board (question file
  ``prompts/judge/kanban.json``, answered by the side-channel judge), and
* the agent's allowed tool list actually contains Kanban tools.

The judgment is memoized per first input so a post-compression prompt rebuild keeps the
same bytes and does not pay for a second judge call.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from agent.prompts.prompt_sections import SYSTEM_PROMPT_DIR, read_prompt_text

logger = logging.getLogger(__name__)

GUIDANCE_PATH = SYSTEM_PROMPT_DIR / "kanban-guidance.md"
QUESTION_FILE = "kanban"
QUESTION_ID = "kanban_intent"
_MIN_CONFIDENCE = 0.5


@lru_cache(maxsize=1)
def kanban_guidance() -> str:
    return read_prompt_text(GUIDANCE_PATH).rstrip("\n")


def _judge_kanban(message: str, judge_fn: Callable[..., Any]) -> bool:
    from agent.judgment import JudgmentRequest, load_questions
    from agent.judgment.types import ChoiceAnswer, NoulAnswer

    try:
        questions = load_questions(QUESTION_FILE)
        result = judge_fn(
            JudgmentRequest(state={"message": message}, questions=questions),
            purpose="kanban_intent",
        )
    except Exception as exc:  # health: allow BLE001 -- advisory side channel, prompt must build
        logger.debug("kanban-intent judgment unavailable: %s", exc)
        return False
    answer = result.answers.get(QUESTION_ID)
    if isinstance(answer, NoulAnswer):
        return answer.noul >= _MIN_CONFIDENCE
    if isinstance(answer, ChoiceAnswer):
        return answer.choice == "true"
    return False


@lru_cache(maxsize=8)
def _judged_kanban(message: str) -> bool:
    from agent.judgment.service import judge

    return _judge_kanban(message, judge)


def is_kanban_intent(first_input: str, *, judge_fn: Callable[..., Any] | None = None) -> bool:
    """True when *first_input* asks to use the Kanban board.

    The default path is memoized per input; *judge_fn* is for tests and bypasses that
    cache. Any failure is False: the guidance is an enhancement, never a precondition.
    """
    message = (first_input or "").strip()
    if not message:
        return False
    if judge_fn is None:
        return _judged_kanban(message)
    return _judge_kanban(message, judge_fn)


__all__ = ["GUIDANCE_PATH", "QUESTION_FILE", "is_kanban_intent", "kanban_guidance"]
