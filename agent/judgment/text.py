"""Text bridge: render typed questions into a keyword prompt and parse the reply.

The three templates and every parsing rule are ported from omp's
``packages/ai/src/judgment/text.ts``. Answers are one-hot (probability 1/0,
confidence 1) because a text completion carries no distribution.
"""

from __future__ import annotations

import math
import re
from importlib.resources import files
from typing import Any, Mapping, Sequence

from agent.judgment.rendering import js_number, render_template
from agent.judgment.types import (
    Answer,
    ChoiceAnswer,
    ChoiceQuestion,
    JudgmentParseError,
    JudgmentRequest,
    JudgmentState,
    JudgmentValidationError,
    NoulAnswer,
    NoulQuestion,
    Question,
    ScoreAnswer,
    ScoreQuestion,
)


def _read_prompt_template(name: str) -> str:
    return files("agent.judgment").joinpath("prompts", name).read_text(encoding="utf-8-sig")


TEXT_JUDGE = _read_prompt_template("text-judge.md")
TEXT_JUDGE_STATE = _read_prompt_template("text-judge-state.md")
TEXT_JUDGE_RETRY = _read_prompt_template("text-judge-retry.md")


_XML_TAG_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def escape_xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_xml_attribute(value: str) -> str:
    return escape_xml_text(value).replace('"', "&quot;")


def _scalar_text(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return js_number(value)
    return str(value)

_YAML_NUMBER = re.compile(
    r"^[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]*)?|0[xX][0-9a-fA-F]+|0[oO][0-7]+)$"
)
_YAML_RESERVED = frozenset({
    "null", "true", "false", "yes", "no", "y", "n", "on", "off", "~",
    ".nan", ".inf", "+.inf", "-.inf",
})


def _yaml_quote(value: str) -> str:
    escapes = {
        0: r"\0", 7: r"\a", 8: r"\b", 9: r"\t", 10: r"\n", 11: r"\v",
        12: r"\f", 13: r"\r", 27: r"\e", 0x85: r"\N", 0xA0: r"\_",
        0x2028: r"\L", 0x2029: r"\P", 34: r'\"', 92: r"\\",
    }
    parts: list[str] = []
    for char in value:
        codepoint = ord(char)
        if 0x80 <= codepoint <= 0x84 or 0x86 <= codepoint <= 0x9F:
            continue
        if codepoint in escapes:
            parts.append(escapes[codepoint])
        elif codepoint < 0x20 or codepoint == 0x7F:
            parts.append(f"\\x{codepoint:02x}")
        else:
            parts.append(char)
    return '"' + "".join(parts) + '"'


def _yaml_string(value: str) -> str:
    value = "".join(
        char for char in value
        if not (0x80 <= ord(char) <= 0x84 or 0x86 <= ord(char) <= 0x9F)
    )
    unsafe_start = "-?:,[]{}#&*!|>'\"%@`<"
    unsafe = (
        not value
        or value != value.strip()
        or value[0] in unsafe_start
        or any(ord(char) < 0x20 or ord(char) in {0x7F, 0x85, 0xA0, 0x2028, 0x2029} for char in value)
        or any(char in value for char in "#'\"`[]{},")
        or re.search(r":(?:\s|$)", value) is not None
        or value.lower() in _YAML_RESERVED
        or _YAML_NUMBER.fullmatch(value) is not None
    )
    return _yaml_quote(value) if unsafe else value


def _yaml_number(value: float) -> str:
    if value == 0:
        return "-0" if isinstance(value, float) and math.copysign(1.0, value) < 0 else "+0"
    if isinstance(value, float) and not math.isfinite(value):
        return ".nan" if math.isnan(value) else ".inf" if value > 0 else "-.inf"
    return js_number(value)


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, str):
        return _yaml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return _yaml_number(value)
    if isinstance(value, Mapping):
        return "{}"
    if isinstance(value, list):
        return "[]"
    raise JudgmentValidationError(f"judgment state contains a non-JSON value: {type(value).__name__}")


def _yaml_dump(value: Any, indent: int = 0) -> str:
    padding = " " * indent
    if isinstance(value, Mapping):
        if not value:
            return padding + "{}"
        lines: list[str] = []
        for key, child in value.items():
            prefix = padding + _yaml_string(str(key)) + ":"
            if isinstance(child, (Mapping, list)) and child:
                lines.extend((prefix, _yaml_dump(child, indent + 2)))
            else:
                lines.append(prefix + " " + _yaml_scalar(child))
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return padding + "[]"
        lines = []
        for child in value:
            marker = padding + "-"
            if isinstance(child, (Mapping, list)) and child:
                nested = _yaml_dump(child, indent + 2).splitlines()
                lines.append(marker + " " + nested[0].lstrip())
                lines.extend(nested[1:])
            else:
                lines.append(marker + " " + _yaml_scalar(child))
        return "\n".join(lines)
    return padding + _yaml_scalar(value)


def render_state_field(key: str, value: Any) -> str:
    key = str(key)
    if _XML_TAG_NAME.match(key):
        open_tag, close_tag = f"<{key}>", f"</{key}>"
    else:
        open_tag = f'<field name="{escape_xml_attribute(key)}">'
        close_tag = "</field>"
    if not isinstance(value, (dict, list)):
        return f"{open_tag}{escape_xml_text(_scalar_text(value))}{close_tag}"
    return f"{open_tag}\n{escape_xml_text(_yaml_dump(value))}\n{close_tag}"


def render_judgment_state(state: JudgmentState) -> str:
    """Top-level XML fields; nested values use a YAML block (never JSON)."""
    if isinstance(state, list) or not isinstance(state, dict):
        return render_state_field("state", state)
    fields = [render_state_field(str(key), value) for key, value in state.items()]
    return "\n".join(fields) if fields else "<state>{}</state>"



def _rendered_questions(request: JudgmentRequest) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for question_id, question in request.questions.items():
        item: dict[str, Any] = {"id": question_id, "instructions": question.instructions}
        if isinstance(question, ChoiceQuestion):
            item["options"] = [
                {"label": label, "description": description}
                for label, description in question.criteria.items()
            ]
        elif isinstance(question, ScoreQuestion):
            item["levels"] = [
                {"index": index, "description": description}
                for index, description in enumerate(question.criteria)
            ]
        elif isinstance(question, NoulQuestion):
            item["yesno"] = True
            criteria = question.criteria or {}
            item["yes"] = criteria.get("true")
            item["no"] = criteria.get("false")
        rendered.append(item)
    return rendered


def render_judgment_prompt(
    request: JudgmentRequest, *, guard_state: bool = True,
) -> tuple[str, str]:
    """``(system, user)``; the system block is constant across states for prompt caching."""
    questions = _rendered_questions(request)
    if not questions:
        raise JudgmentValidationError("judgment request has no questions")
    multi = len(questions) > 1
    system = render_template(TEXT_JUDGE, {
        "questions": questions, "multi": multi, "guardState": guard_state,
    })
    first = dict(questions[0])
    first.update({
        "multi": multi, "guardState": guard_state, "state": render_judgment_state(request.state),
    })
    user = render_template(TEXT_JUDGE_STATE, first)
    return system, user


def render_retry_prompt(system: str, *, multi: bool) -> str:
    return render_template(TEXT_JUDGE_RETRY, {"system": system, "multi": multi})


# ── Parsing ─────────────────────────────────────────────────────────────────────

_WORD = re.compile(r"\w", re.UNICODE)


def index_of_word(text: str, needle: str) -> int:
    """Earliest whole-word, case-insensitive occurrence of ``needle`` in ``text``."""
    lower = text.lower()
    target = needle.lower()
    if not target:
        return -1
    from_at = 0
    while from_at <= len(lower) - len(target):
        at = lower.find(target, from_at)
        if at < 0:
            return -1
        before = lower[at - 1] if at > 0 else ""
        after = lower[at + len(target)] if at + len(target) < len(lower) else ""
        bounded_before = not before or not _WORD.match(before) or not _WORD.match(target[0])
        bounded_after = not after or not _WORD.match(after) or not _WORD.match(target[-1])
        if bounded_before and bounded_after:
            return at
        from_at = at + 1
    return -1


def parse_choice_reply(text: str, labels: Sequence[str]) -> str | None:
    best: str | None = None
    best_at = float("inf")
    for label in labels:
        at = index_of_word(text, label)
        if at < 0:
            continue
        if at < best_at or (at == best_at and best is not None and len(label) > len(best)):
            best, best_at = label, at
    return best


def parse_noul_reply(text: str) -> bool | None:
    yes = [at for at in (index_of_word(text, "yes"), index_of_word(text, "true")) if at >= 0]
    no = [at for at in (index_of_word(text, "no"), index_of_word(text, "false")) if at >= 0]
    yes_at = min(yes) if yes else -1
    no_at = min(no) if no else -1
    if yes_at < 0 and no_at < 0:
        return None
    if no_at < 0:
        return True
    if yes_at < 0:
        return False
    return yes_at < no_at


_INTEGER = re.compile(r"(?<!\w)(?<!\d\.)(\d+)(?!\w)(?!\.\d)", re.UNICODE)


def parse_score_reply(text: str, levels: int) -> int | None:
    for match in _INTEGER.finditer(text):
        value = int(match.group(1))
        if value < levels:
            return value
    return None


_LINE_PREFIX = re.compile(r"^[\s\-*\u2022]+")
_LINE_SEPARATOR = re.compile(r"\s*[:=]\s*|\s+-\s+")
_QUOTES = str.maketrans("", "", "`\"'")
_ANSWER_PREFIX = re.compile(r"^\s*[:=-]\s*")


def split_answer_lines(text: str, ids: Sequence[str]) -> dict[str, str]:
    by_id: dict[str, str] = {}
    wanted = set(ids)
    for raw_line in text.split("\n"):
        line = _LINE_PREFIX.sub("", raw_line).strip()
        match = _LINE_SEPARATOR.search(line)
        if match is None or match.start() <= 0:
            continue
        question_id = line[: match.start()].translate(_QUOTES)
        if question_id not in wanted or question_id in by_id:
            continue
        by_id[question_id] = _ANSWER_PREFIX.sub("", line[match.start():])
    return by_id


def _one_hot(labels: Sequence[str], chosen: str) -> dict[str, float]:
    return {label: (1.0 if label == chosen else 0.0) for label in labels}


def parse_answer(question_id: str, question: Question, reply: str) -> Answer:
    if isinstance(question, ChoiceQuestion):
        labels = tuple(question.criteria)
        choice = parse_choice_reply(reply, labels)
        if choice is None:
            raise JudgmentParseError(question_id, reply, "no option label in reply")
        return ChoiceAnswer(choice, _one_hot(labels, choice), 1.0)
    if isinstance(question, NoulQuestion):
        verdict = parse_noul_reply(reply)
        if verdict is None:
            raise JudgmentParseError(question_id, reply, "no yes/no in reply")
        return NoulAnswer(1.0 if verdict else 0.0)
    if isinstance(question, ScoreQuestion):
        level = parse_score_reply(reply, len(question.criteria))
        if level is None:
            raise JudgmentParseError(question_id, reply, "no level number in reply")
        labels = tuple(str(index) for index in range(len(question.criteria)))
        return ScoreAnswer(level, _one_hot(labels, str(level)), 1.0)
    raise JudgmentValidationError(
        f"judgment {question_id!r}: text judges cannot answer box questions")


def parse_text_answers(output: str, request: JudgmentRequest) -> dict[str, Answer]:
    """Parse one completion for every question; any missing/unparseable id fails the attempt."""
    ids = list(request.questions)
    replies = (
        {ids[0]: output}
        if len(ids) == 1
        else split_answer_lines(output, ids)
    )
    answers: dict[str, Answer] = {}
    for question_id in ids:
        reply = replies.get(question_id)
        if reply is None:
            raise JudgmentParseError(question_id, output, "no answer line for question")
        answers[question_id] = parse_answer(question_id, request.questions[question_id], reply)
    return answers
