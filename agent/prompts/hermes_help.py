"""First-input detection for Hermes Agent self-help (``§ Runtime`` → internal URLs).

The question lives in ``agent/prompts/judge/hermes-agent-help.json`` and is answered by
the configured side-channel judge. A True answer appends the packaged guidance from
``system/hermes-agent-help-guidance.md`` to the internal-URLs block, which is also the
text the identity slot uses (one source).

The judgment is advisory: a missing judge, timeout, or malformed answer returns False, so
prompt construction never depends on the side channel succeeding.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from agent.prompts.prompt_sections import SYSTEM_PROMPT_DIR, read_prompt_text

logger = logging.getLogger(__name__)

GUIDANCE_PATH = SYSTEM_PROMPT_DIR / "hermes-agent-help-guidance.md"
NO_SKILLS_GUIDANCE_PATH = SYSTEM_PROMPT_DIR / "hermes-agent-help-guidance-no-skills.md"
QUESTION_FILE = "hermes-agent-help"
QUESTION_ID = "hermes_agent_help"
_MIN_CONFIDENCE = 0.5


@lru_cache(maxsize=1)
def _with_skills() -> str:
    return read_prompt_text(GUIDANCE_PATH).rstrip("\n")


@lru_cache(maxsize=1)
def _without_skills() -> str:
    return read_prompt_text(NO_SKILLS_GUIDANCE_PATH).rstrip("\n")


def hermes_agent_help_guidance(*, with_skills: bool = True) -> str:
    """The packaged guidance text; the no-skills variant never names ``skill_view``."""
    return _with_skills() if with_skills else _without_skills()


def _judge_help(message: str, judge_fn: Callable[..., Any]) -> bool:
    from agent.judgment import JudgmentRequest, load_questions
    from agent.judgment.types import ChoiceAnswer, NoulAnswer

    try:
        questions = load_questions(QUESTION_FILE)
        result = judge_fn(
            JudgmentRequest(state={"message": message}, questions=questions),
            purpose="hermes_help",
        )
    except Exception as exc:  # health: allow BLE001 -- advisory side channel, prompt must build
        logger.debug("hermes-help judgment unavailable: %s", exc)
        return False
    answer = result.answers.get(QUESTION_ID)
    if isinstance(answer, NoulAnswer):
        return answer.noul >= _MIN_CONFIDENCE
    if isinstance(answer, ChoiceAnswer):
        return answer.choice == "true"
    return False


@lru_cache(maxsize=8)
def _judged_help(message: str) -> bool:
    """Memoized default-path judgment.

    The prompt is rebuilt after compression; re-asking the model there could flip the
    answer and change the stable prefix inside one session. One judgment per distinct
    first input per process keeps the bytes (and the bill) fixed.
    """
    from agent.judgment.service import judge

    return _judge_help(message, judge)


def is_hermes_agent_help(
    first_input: str, *, judge_fn: Callable[..., Any] | None = None
) -> bool:
    """True when *first_input* asks for help with Hermes Agent itself.

    The default path is memoized per input (``_judged_help``); *judge_fn* is for tests and
    bypasses that cache. Any failure is False: the guidance is an enhancement, never a
    precondition.
    """
    message = (first_input or "").strip()
    if not message:
        return False
    if judge_fn is None:
        return _judged_help(message)
    return _judge_help(message, judge_fn)


__all__ = [
    "GUIDANCE_PATH",
    "NO_SKILLS_GUIDANCE_PATH",
    "QUESTION_FILE",
    "hermes_agent_help_guidance",
    "is_hermes_agent_help",
]
