"""Per-user-turn reasoning classifier for ``agent.reasoning_auto`` (ADR-17 P5).

omp's ``auto-thinking`` picks a reasoning level from how open-ended a request is
and fixes it for the turn so the prompt prefix stays cached. Hermes mirrors that
with one side-channel judgment per user turn: ``agent.reasoning_auto`` selects
the mode (``off`` / ``auto`` / ``on``), the bundled ``auto-thinking`` questions
supply the ladder, and the answer is clamped onto the route's own effort
surface. Nothing here appends transcript rows or edits the prompt, and every
failure returns ``None`` (debug log only) so the configured effort stands.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agent.reasoning_effort import clamp_effort, route_supported_efforts

logger = logging.getLogger(__name__)

#: Ladder the bundled ``level`` question offers, low → high. ``max`` is opt-in via
#: ``agent.reasoning_auto_max_effort``.
LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_TIMEOUT_MS = 4000

#: ``with_max -> {"level": ChoiceQuestion, "solution_space": ChoiceQuestion}``;
#: packaged data is read once per ceiling variant.
_QUESTION_SET_CACHE: dict[bool, dict[str, Any]] = {}


def _agent_settings() -> Mapping[str, Any]:
    """The live ``agent`` config block (read-only; never mutated)."""
    from hermes_cli.config import load_config_readonly

    section = (load_config_readonly() or {}).get("agent")
    return section if isinstance(section, Mapping) else {}


def _has_native_judge() -> bool:
    from agent.judgment import has_native_judge

    return bool(has_native_judge())


def _question_set(with_max: bool) -> dict[str, Any]:
    """Bundled auto-thinking questions with the ``solutionSpace`` labels filled.

    ``solutionSpace`` ships ``criteria: {}``; its labels are the ladder levels with
    the ``level`` question's rubrics, matching omp's ``buildQuestionSet``. Both
    question kinds share one rendered ladder, so the judge sees the same choices
    whichever input kind it classifies.
    """
    cached = _QUESTION_SET_CACHE.get(with_max)
    if cached is not None:
        return cached
    from agent.judgment import ChoiceQuestion, load_questions

    levels = LEVELS if with_max else LEVELS[:-1]
    loaded = load_questions(
        "auto-thinking",
        contexts={"level": {"withMax": with_max}, "solutionSpace": {"withMax": with_max}},
        labels={"solutionSpace": list(levels)},
    )
    criteria = {label: loaded["level"].criteria.get(label) for label in levels}
    questions = {
        "level": ChoiceQuestion(loaded["level"].instructions, criteria),
        "solution_space": ChoiceQuestion(loaded["solutionSpace"].instructions, criteria),
    }
    _QUESTION_SET_CACHE[with_max] = questions
    return questions


def _judgment_limits(settings: Mapping[str, Any]) -> tuple[int, float | None]:
    """This turn's ``(timeout_ms, budget_usd)``: the auto timeout capped by the
    judgment timeout, and the per-call cost-gate budget (``None`` when disabled)."""
    timeout_ms = DEFAULT_TIMEOUT_MS
    raw = settings.get("reasoning_auto_timeout_ms")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        timeout_ms = int(raw)
    try:
        from agent.judgment.config import load_judgment_settings

        judgment = load_judgment_settings()
        cap = int(judgment.timeout_ms or 0)
        if cap > 0:
            timeout_ms = min(timeout_ms, cap)
        cap_usd = float(judgment.cost_gate.max_usd_per_call or 0.0)
    except Exception:  # health: allow BLE001 -- limits are advisory, not a gate
        logger.debug("auto-thinking: judgment limits unavailable", exc_info=True)
        return timeout_ms, None
    return timeout_ms, (cap_usd if cap_usd > 0 else None)


def _run_judgment(request: Any, *, timeout_ms: int, budget_usd: float | None) -> Any:
    from agent.judgment import judge

    return judge(
        request, purpose="auto-thinking", timeout_ms=timeout_ms, budget_usd=budget_usd)


def _reasoning_disabled(agent: Any) -> bool:
    """The session's configured reasoning is off; auto must not switch it back on."""
    config = getattr(agent, "reasoning_config", None)
    if not isinstance(config, Mapping):
        return False
    effort = str(config.get("effort") or "").strip().lower()
    return config.get("enabled") is False or effort == "none"


def resolve_auto_effort(
    agent: Any, request: str, *, solution_space: str = "",
) -> dict[str, Any] | None:
    """The reasoning config auto-thinking picks for this turn.

    Returns ``{"enabled": True, "effort": <level>}`` or ``None`` to keep the
    configured effort (mode off, no judge, no effort surface, disabled reasoning,
    blank input, or any failure — all silent).
    """
    try:
        settings = _agent_settings()
        mode = str(settings.get("reasoning_auto") or "off").strip().lower()
        if mode not in ("auto", "on"):
            return None
        # omp lets a delegator's solutionSpace replace the request as the sole input.
        space = solution_space.strip() if isinstance(solution_space, str) else ""
        text = space or (request.strip() if isinstance(request, str) else "")
        if not text:
            return None
        if getattr(agent, "api_mode", None) == "codex_app_server":
            # That runtime takes its level from codex's own harness mode, not from
            # the reasoning config this overlay feeds.
            return None
        if getattr(agent, "_voice_turn_pending", False):
            # A voice turn binds its own route/effort (``auxiliary.voice_chat``);
            # auto must not mask that explicit setting.
            return None
        supported = route_supported_efforts(
            getattr(agent, "provider", None),
            getattr(agent, "model", None),
            getattr(agent, "api_mode", None),
        )
        if not supported:
            return None
        if _reasoning_disabled(agent):
            return None
        if mode == "auto" and not _has_native_judge():
            logger.debug("auto-thinking: no native judge; reasoning_auto=auto keeps the config")
            return None
        ceiling = str(settings.get("reasoning_auto_max_effort") or "xhigh").strip().lower()
        with_max = ceiling == "max"
        question = _question_set(with_max)["solution_space" if space else "level"]
        from agent.judgment import JudgmentRequest

        state = {"solution_space": text} if space else {"request": text}
        timeout_ms, budget_usd = _judgment_limits(settings)
        result = _run_judgment(
            JudgmentRequest(state=state, questions={"level": question}),
            timeout_ms=timeout_ms,
            budget_usd=budget_usd,
        )
        answer = result.answers.get("level")
        level = str(getattr(answer, "choice", "") or "").strip().lower()
        if level not in question.criteria:
            logger.debug("auto-thinking: unusable answer %r", level)
            return None
        effort = str(clamp_effort(level, supported) or "").strip().lower()
        if effort not in {str(value).strip().lower() for value in supported}:
            logger.debug("auto-thinking: no supported effort near %r on this route", level)
            return None
        return {"enabled": True, "effort": effort}
    except Exception:  # health: allow BLE001 -- an advisory classifier must never fail a turn
        logger.debug("auto-thinking: classification failed", exc_info=True)
        return None
