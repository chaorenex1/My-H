"""Model-picked charts for assistant tables (ADR-12 D6, ADR-17 P5).

This is the host half of omp's ``tui.autoGraph: smart`` planner
(``packages/coding-agent/src/auto-graph/planner.ts``). The client has already
read the table and built a local :class:`ChartPlan` guess; this module asks the
``auto-graph`` question set what only meaning can decide — kind, label column,
plotted columns, transpose, shading, ordering, reference row, polarity, rivals,
focus row and title — and folds each confident answer into a copy of the guess.

The judge state and answers never leave the request: no markdown, table or
answer is persisted, and nothing is appended to the transcript. A missing
judge, an expired 15 s timeout, or any malformed answer falls back to the local
guess (``{"plan": None, "source": "local"}``) instead of failing the turn.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from functools import lru_cache
from importlib.resources import files
from typing import Any, Mapping

from agent.judgment import (
    ChoiceAnswer,
    ChoiceQuestion,
    JudgmentRequest,
    NoulAnswer,
    Question,
    render_question,
)
from agent.judgment.questions import question_from_spec

logger = logging.getLogger(__name__)


def _has_native_judge() -> bool:
    from agent.judgment import has_native_judge

    return bool(has_native_judge())


#: A pick slower than this gives way to the client's local guess (omp's PICK_TIMEOUT_MS).
PICK_TIMEOUT_MS = 15_000
#: Longest table source embedded in the judge state (omp's MAX_TABLE_CHARS).
MAX_TABLE_CHARS = 4_000
#: Most questions one table is asked; semantic questions fill what the structural ones leave.
MAX_QUESTIONS = 25
#: Most parts a share bar holds; past them the local guess draws the parts instead.
MAX_SHARE_SEGMENTS = 10
SHADING_CONFIDENCE = 0.7
REFERENCE_CONFIDENCE = 0.8
ORDER_CONFIDENCE = 0.6
POLARITY_CONFIDENCE = 0.7
RIVALS_CONFIDENCE = 0.7
SURE_FOCUS = 0.75
AGREED_FOCUS = 0.4
TITLE_CONFIDENCE = 0.4
#: Fewest same-unit columns that can draw as a heatmap.
MIN_HEAT_COLUMNS = 4
#: Fewest rows a ranking can reorder usefully.
MIN_RANKED_ROWS = 4
#: Fewest rows for which a contest or focus changes the chart.
MIN_CONTEST_ROWS = 3
#: Most rows offered as the table's focus.
MAX_FOCUS_ROWS = 12
#: Most measures asked for their polarity.
MAX_POLARITY_QUESTIONS = 6
#: Most distinct naming values offered as reference rows.
MAX_REFERENCE_CHOICES = 8
MAX_REFERENCE_CHARS = 48
NONE = "none"
OWN_FRAME = frozenset({
    "line", "scatter", "change", "stacked", "timeline", "waterfall", "slope",
    "bullet", "histogram",
})
FLOWS = frozenset({"timeline", "waterfall"})
REFERENCED = frozenset({"bar", "multiples", "heatmap"})
LABEL_ROLES = frozenset({"label", "temporal"})


@lru_cache(maxsize=1)
def _question_specs() -> dict[str, Any]:
    """The bundled ``auto-graph`` question specs, as shipped in the wheel."""
    resource = files("agent").joinpath("prompts", "judge", "auto-graph.json")
    return json.loads(resource.read_text(encoding="utf-8-sig"))


def _question(
    question_id: str,
    context: Mapping[str, Any] | None = None,
    criteria: Mapping[str, str | None] | None = None,
) -> Question:
    question = question_from_spec(question_id, _question_specs()[question_id])
    if context is not None:
        question = render_question(question, context)
    if criteria is not None and isinstance(question, ChoiceQuestion):
        question = replace(question, criteria=criteria)
    return question


# ── table / guess normalization (the wire carries omp's TableAnalysis shape) ──


def _role_from_type(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith("number"):
        return "measure"
    if value == "row index":
        return "index"
    if value == "date/time":
        return "temporal"
    if value == "ordered step":
        return "sequence"
    return "label" if value == "text" else ""


def _columns(table: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = table.get("columns")
    if not isinstance(raw, (list, tuple)):
        return []
    columns: list[dict[str, Any]] = []
    for position, item in enumerate(raw):
        if not isinstance(item, Mapping):
            continue
        index = item.get("index", position)
        if isinstance(index, bool) or not isinstance(index, int):
            index = position
        role = item.get("role")
        if not isinstance(role, str) or not role:
            role = _role_from_type(item.get("type"))
        header = item.get("header")
        if not isinstance(header, str) or not header.strip():
            header = item.get("name")
        if not isinstance(header, str) or not header.strip():
            header = f"column {index + 1}"
        hint = item.get("type") if isinstance(item.get("type"), str) else ""
        mixed = item.get("mixed")
        cells = item.get("cells")
        try:
            arrow_share = float(item.get("arrowShare") or 0.0)
        except (TypeError, ValueError):
            arrow_share = 0.0
        columns.append({
            "index": index,
            "header": header.strip(),
            "role": role,
            "dim": item.get("dim") if isinstance(item.get("dim"), str) else None,
            "unit": item.get("unit") if isinstance(item.get("unit"), str) else "",
            "mixed": bool(mixed) if isinstance(mixed, bool) else "mixed units" in hint,
            "scores": bool(item.get("scores")) or "score out of a total" in hint,
            "arrowShare": arrow_share,
            "cells": list(cells) if isinstance(cells, (list, tuple)) else [],
            "constant": bool(item.get("constant")),
            "type": hint or None,
        })
    return columns


def _measure_indices(table: Mapping[str, Any], columns: list[dict[str, Any]]) -> list[int]:
    raw = table.get("measures")
    indices: list[int] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            index = item.get("index") if isinstance(item, Mapping) else item
            if isinstance(index, int) and not isinstance(index, bool):
                indices.append(index)
    if not indices:
        indices = [c["index"] for c in columns if c["role"] == "measure" and not c["constant"]]
    return indices


def _row_indices(table: Mapping[str, Any], columns: list[dict[str, Any]]) -> list[int]:
    raw = table.get("rows")
    if isinstance(raw, (list, tuple)):
        rows = [row for row in raw if isinstance(row, int) and not isinstance(row, bool)]
        if rows:
            return rows
    width = max((len(c["cells"]) for c in columns), default=0)
    return list(range(width))


def _cell_text(cell: Any) -> str:
    if isinstance(cell, str):
        return cell
    if isinstance(cell, Mapping):
        text = cell.get("text")
        if isinstance(text, str) and text:
            return text
        value = cell.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _cell_number(cell: Any) -> float | None:
    if isinstance(cell, Mapping) and cell.get("kind") == "number":
        value = cell.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _column_at(columns: list[dict[str, Any]], index: Any) -> dict[str, Any] | None:
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    for column in columns:
        if column["index"] == index:
            return column
    return None


def _column_type(column: Mapping[str, Any]) -> str:
    role = column.get("role")
    hint = column.get("type") if isinstance(column.get("type"), str) else ""
    if role == "measure":
        if column.get("mixed") or "mixed units" in hint:
            return "number (mixed units)"
        if column.get("scores") or "score out of a total" in hint:
            return "number (score out of a total)"
        dim = column.get("dim")
        if not dim and hint.startswith("number (") and hint.endswith(")"):
            dim = hint[len("number ("):-1]
        return f"number ({dim or 'count'})"
    if role == "sequence":
        return "ordered step"
    if role == "index":
        return "row index"
    if role == "temporal":
        return "date/time"
    return "text"


# ── local helpers mirroring chart-plan's predicates ─────────────────────────


def _is_monotonic(values: list[float]) -> bool:
    if len(values) < 2:
        return False
    up = values[1] > values[0]
    for at in range(1, len(values)):
        step = values[at] - values[at - 1]
        if step == 0 or (step > 0) != up:
            return False
    return True


def _leading_measures(
    columns: list[dict[str, Any]], measures: list[int], guess: Mapping[str, Any],
) -> list[dict[str, Any]]:
    label = _column_at(columns, guess.get("label"))
    if label is None or label["role"] != "label":
        return []
    return [c for c in columns if c["index"] in measures and c["index"] < label["index"]]


def _label_options(
    columns: list[dict[str, Any]], measures: list[int], guess: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Column options for the label question, keyed like omp: header, or ``column N``."""
    candidates = [
        *_leading_measures(columns, measures, guess),
        *[c for c in columns if c["role"] in LABEL_ROLES],
    ]
    candidates.sort(key=lambda c: c["index"])
    options: dict[str, dict[str, Any]] = {}
    if len(candidates) < 2:
        return options
    for column in candidates:
        name = column["header"]
        if not name or name in options:
            name = f"column {column['index'] + 1}"
        options[name] = column
    return options


def _heat_capable(
    table: Mapping[str, Any], guess: Mapping[str, Any], candidates: list[dict[str, Any]],
) -> bool:
    composition = table.get("composition")
    if isinstance(composition, Mapping) and composition.get("exact"):
        return False
    if guess.get("kind") == "heatmap":
        return True
    sizes: dict[tuple[Any, Any], int] = {}
    for column in candidates:
        key = (column["dim"], column["unit"])
        sizes[key] = sizes.get(key, 0) + 1
    return bool(sizes) and max(sizes.values()) >= MIN_HEAT_COLUMNS


def _rankable(
    columns: list[dict[str, Any]], guess: Mapping[str, Any], rows: list[int],
) -> dict[str, Any] | None:
    label = _column_at(columns, guess.get("label"))
    series = guess.get("series")
    lead_index = series[0] if isinstance(series, list) and series else None
    lead = _column_at(columns, lead_index)
    if lead is None or label is None or label["role"] != "label":
        return None
    if guess.get("kind") in OWN_FRAME or guess.get("transpose") or guess.get("baselines"):
        return None
    if len(rows) < MIN_RANKED_ROWS:
        return None
    values = [
        number for number in (_cell_number(lead["cells"][row]) for row in rows if row < len(lead["cells"]))
        if number is not None
    ]
    return None if _is_monotonic(values) else lead


def _reference_candidates(
    table: Mapping[str, Any], columns: list[dict[str, Any]], guess: Mapping[str, Any],
    rows: list[int],
) -> dict[str, Any] | None:
    if guess.get("kind") not in REFERENCED or guess.get("baselines"):
        return None
    composition = table.get("composition")
    if isinstance(composition, Mapping) and composition.get("exact"):
        return None
    reference = guess.get("reference")
    qualifiers = guess.get("qualifiers")
    index = (
        reference.get("column") if isinstance(reference, Mapping)
        else None
    )
    if index is None and isinstance(qualifiers, list) and qualifiers:
        index = qualifiers[-1]
    if index is None:
        index = guess.get("label")
    column = _column_at(columns, index)
    if column is None or column["role"] != "label":
        return None
    cells = [
        _cell_text(column["cells"][row]) for row in rows
        if row < len(column["cells"]) and _cell_text(column["cells"][row])
    ]
    values = list(dict.fromkeys(cells))
    if len(values) < 2 or len(values) > MAX_REFERENCE_CHOICES:
        return None
    if any(len(value) > MAX_REFERENCE_CHARS or value == NONE for value in values):
        return None
    return {"column": column, "values": values, "repeats": len(values) < len(cells)}


def _pair_candidates(
    table: Mapping[str, Any], guess: Mapping[str, Any], candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    composition = table.get("composition")
    if (isinstance(composition, Mapping) and composition.get("exact")) or guess.get("baselines"):
        return None
    for at, first in enumerate(candidates):
        for second in candidates[at + 1:]:
            if first["dim"] != second["dim"] or first["unit"] != second["unit"]:
                continue
            if first["arrowShare"] >= 0.6 or second["arrowShare"] >= 0.6:
                continue
            return first, second
    return None


def _rows_are_metrics(columns: list[dict[str, Any]], guess: Mapping[str, Any]) -> bool:
    if guess.get("kind") == "change" or guess.get("transpose"):
        return True
    series = guess.get("series")
    if not isinstance(series, list):
        return False
    return any(
        column is not None and column["mixed"]
        for column in (_column_at(columns, index) for index in series)
    )


def _row_names(
    columns: list[dict[str, Any]], rows: list[int], guess: Mapping[str, Any],
) -> dict[str, int]:
    label = _column_at(columns, guess.get("label"))
    if label is None:
        return {}
    qualifiers = guess.get("qualifiers")
    indices = [label["index"]]
    if isinstance(qualifiers, list):
        indices.extend(index for index in qualifiers if isinstance(index, int))
    namers = [c for c in (_column_at(columns, index) for index in indices) if c is not None]
    names: dict[str, int] = {}
    for row in rows:
        parts = []
        for column in namers:
            if row >= len(column["cells"]):
                continue
            text = _cell_text(column["cells"][row])
            if text:
                parts.append(text)
        base = " · ".join(parts) or f"row {row + 1}"
        name = base
        for at in range(2, len(rows) + 2):
            if name not in names and name != NONE:
                break
            name = f"{base} ({at})"
        names[name] = row
    return names


def _move_columns(
    columns: list[dict[str, Any]], guess: Mapping[str, Any],
) -> list[dict[str, Any]]:
    series = guess.get("series")
    if not isinstance(series, list):
        return []
    plotted = [c for c in (_column_at(columns, index) for index in series) if c is not None]
    if guess.get("kind") == "slope":
        return plotted
    if guess.get("kind") == "paired" and len(plotted) == 1 and plotted[0]["arrowShare"] >= 0.6:
        return plotted
    return []


def _kind_criteria(guess: Mapping[str, Any]) -> dict[str, str | None]:
    """Kind options the table can plausibly draw: a run of steps or a stat only where guessed."""
    criteria = dict(_question_specs()["kind"]["criteria"])
    for kind in ("timeline", "waterfall", "stat"):
        if guess.get("kind") != kind:
            criteria.pop(kind, None)
    return criteria


# ── judge request ──────────────────────────────────────────────────────────


def judge_request(
    markdown: str,
    table: Mapping[str, Any],
    guess: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Question]]:
    """The judge state and questions for one table; each answer only where it can change the chart."""
    columns = _columns(table)
    rows = _row_indices(table, columns)
    measures = _measure_indices(table, columns)
    label = _column_at(columns, guess.get("label"))
    candidates = [
        column for column in (_column_at(columns, index) for index in measures)
        if column is not None and column is not label
    ]
    questions: dict[str, Question] = {
        "kind": _question("kind", criteria=_kind_criteria(guess)),
        "transpose": _question("transpose"),
    }

    def add(question_id: str, question: Question) -> None:
        if len(questions) < MAX_QUESTIONS or question_id in {"kind", "transpose"}:
            questions[question_id] = question

    if _heat_capable(table, guess, candidates):
        add("shading", _question("shading"))

    lead = _rankable(columns, guess, rows)
    if lead is not None:
        add("order", _question("order", {"column": lead["header"]}))

    reference = _reference_candidates(table, columns, guess, rows)
    if reference is not None:
        rows_labels = {value: None for value in reference["values"]}
        rows_labels.update(_question_specs()["reference"]["criteria"])
        add("reference", _question(
            "reference",
            {"column": reference["column"]["header"], "repeats": reference["repeats"]},
            rows_labels,
        ))

    options = _label_options(columns, measures, guess)
    if options:
        add("label", _question("label", criteria={name: None for name in options}))

    pair = _pair_candidates(table, guess, candidates)
    if pair is not None:
        add("pair", _question(
            "pair", {"first": pair[0]["header"], "second": pair[1]["header"]}))

    for column in candidates:
        add(f"c{column['index']}", _question("column", {"column": column["header"]}))

    namers = [
        c for c in columns
        if c["role"] == "label" and label is not None and c["index"] != label["index"]
    ]
    if label is not None and namers:
        for column in namers:
            add(f"n{column['index']}", _question(
                "name", {"column": column["header"], "label": label["header"]}))
        for column in [label, *namers]:
            add(f"g{column['index']}", _question("group", {"column": column["header"]}))

    _add_semantic_questions(questions, table, columns, rows, candidates, guess, label)

    state = {
        "table": markdown[:MAX_TABLE_CHARS],
        "columns": [{"name": column["header"], "type": _column_type(column)} for column in columns],
    }
    return state, questions


def _add_semantic_questions(
    questions: dict[str, Question],
    table: Mapping[str, Any],
    columns: list[dict[str, Any]],
    rows: list[int],
    candidates: list[dict[str, Any]],
    guess: Mapping[str, Any],
    label: dict[str, Any] | None,
) -> None:
    """Meaning questions, in omp's priority: title, move, rivals, polarity, focus, row polarity."""

    def add(question_id: str, question: Question) -> None:
        if len(questions) < MAX_QUESTIONS or question_id in {"kind", "transpose"}:
            questions[question_id] = question

    takeaways = table.get("takeaways")
    if isinstance(takeaways, list) and takeaways:
        criteria: dict[str, str | None] = {}
        for fact in takeaways:
            if isinstance(fact, Mapping) and isinstance(fact.get("key"), str):
                text = fact.get("text")
                criteria[fact["key"]] = text if isinstance(text, str) else None
        if criteria:
            criteria.update(_question_specs()["title"]["criteria"])
            add("title", _question("title", criteria=criteria))

    move = _move_columns(columns, guess)
    if move:
        add("move", _question("move", {
            "columns": [column["header"] for column in move],
            "arrow": len(move) == 1,
        }))

    if len(rows) >= MIN_CONTEST_ROWS:
        add("rivals", _question("rivals"))

    leading = [
        column for column in (_column_at(columns, index) for index in guess.get("series") or [])
        if column is not None
    ]
    plotted = leading + [column for column in candidates if column not in leading]
    for column in plotted[:MAX_POLARITY_QUESTIONS]:
        if column["role"] == "measure":
            add(f"p{column['index']}", _question("polarity", {"column": column["header"]}))

    names = _row_names(columns, rows, guess)
    if MIN_CONTEST_ROWS <= len(names) <= MAX_FOCUS_ROWS:
        criteria = {name: None for name in names}
        criteria.update(_question_specs()["focus"]["criteria"])
        add("focus", _question("focus", criteria=criteria))

    if label is not None and _rows_are_metrics(columns, guess):
        for row in rows:
            if row >= len(label["cells"]):
                continue
            add(f"q{row}", _question(
                "rowPolarity",
                {"row": _cell_text(label["cells"][row])},
                _question_specs()["polarity"]["criteria"],
            ))


# ── answer mapping ─────────────────────────────────────────────────────────


def _odds(answer: ChoiceAnswer, choice: str) -> float:
    probabilities = answer.probabilities if isinstance(answer.probabilities, Mapping) else {}
    if choice in probabilities:
        try:
            return float(probabilities[choice])
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(answer.confidence)
    except (TypeError, ValueError):
        return 0.0


def _choice(
    answer: Any, gate: float, criteria: Mapping[str, Any],
) -> str | None:
    if not isinstance(answer, ChoiceAnswer):
        return None
    if answer.choice not in criteria:
        return None
    return answer.choice if _odds(answer, answer.choice) >= gate else None


def _noul(answer: Any) -> float:
    if not isinstance(answer, NoulAnswer):
        return 0.0
    try:
        return float(answer.noul)
    except (TypeError, ValueError):
        return 0.0


def _normalize_polarity(mapping: Any) -> dict[str, str]:
    if not isinstance(mapping, Mapping):
        return {}
    return {
        str(index): value
        for index, value in mapping.items()
        if value in {"higher", "lower"} and isinstance(index, (int, str))
    }


def _plan_structure(
    table: Mapping[str, Any],
    guess: Mapping[str, Any],
    answers: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[int], list[int],
           dict[str, Any] | None, list[int]] | None:
    """The chart's skeleton (kind, label, series, transpose) or ``None`` for no chart."""
    columns = _columns(table)
    rows = _row_indices(table, columns)
    measures = _measure_indices(table, columns)
    kind_criteria = _question_specs()["kind"]["criteria"]
    kind = _choice(answers.get("kind"), 0.0, kind_criteria) or guess.get("kind")
    if kind is None or kind == NONE:
        return None
    if kind == "share" and len(rows) > MAX_SHARE_SEGMENTS:
        kind = guess.get("kind")

    options = _label_options(columns, measures, guess)
    picked_label = answers.get("label")
    named = (
        options.get(picked_label.choice)
        if isinstance(picked_label, ChoiceAnswer) and picked_label.choice in options
        else None
    )
    label = named if named is not None else _column_at(columns, guess.get("label"))
    label_index = label["index"] if label is not None else None

    candidates = [c for c in columns if c["index"] in measures and c is not label]
    picked = [c["index"] for c in candidates if _noul(answers.get(f"c{c['index']}")) >= 0.5]
    series = picked or [
        index for index in (guess.get("series") or [])
        if isinstance(index, int) and index != label_index
    ]
    if not series:
        return None
    if kind == "scatter":
        series = series[:2]

    plan = dict(guess)
    plan["kind"] = kind
    if label_index is None:
        plan.pop("label", None)
    else:
        plan["label"] = label_index
    plan["series"] = series

    # The judge confirms (noul) whether a dumbbell's two series are one quantity.
    pair = _pair_candidates(table, guess, candidates)
    if kind == "paired" and len(series) == 2 and pair is not None:
        if answers.get("pair") is not None and _noul(answers.get("pair")) < 0.5:
            if guess.get("kind") != "paired":
                plan["kind"] = "grouped"

    transpose = bool(guess.get("transpose"))
    if (
        kind not in {"stat", "change", "stacked", "bullet"}
        and kind not in FLOWS
        and _noul(answers.get("transpose")) >= 0.5
        and any(c["mixed"] for c in candidates)
    ):
        transpose = True
    plan["transpose"] = transpose
    return plan, columns, rows, label, series


def _apply_naming(
    plan: dict[str, Any],
    columns: list[dict[str, Any]],
    answers: Mapping[str, Any],
    guess: Mapping[str, Any],
    label: dict[str, Any] | None,
    label_index: int | None,
) -> None:
    """Extra naming columns and the group column the judge accepted."""
    namers = [
        c for c in columns
        if c["role"] == "label" and label is not None and c["index"] != label["index"]
    ]
    if label is None or not namers:
        return
    label_changed = label_index != guess.get("label")
    accepted = [
        column["index"] for column in namers
        if _noul(answers.get(f"n{column['index']}")) >= 0.5
    ]
    if accepted or label_changed:
        plan["qualifiers"] = accepted
    group_at = guess.get("group")
    odds = 0.0
    for index in [label["index"], *accepted]:
        answer = _noul(answers.get(f"g{index}"))
        if answer >= 0.5 and answer > odds:
            group_at = index
            odds = answer
    if odds > 0:
        plan["group"] = group_at
    elif label_changed:
        plan.pop("group", None)


def _apply_layout(
    plan: dict[str, Any],
    table: Mapping[str, Any],
    columns: list[dict[str, Any]],
    rows: list[int],
    answers: Mapping[str, Any],
    guess: Mapping[str, Any],
    kind: str,
) -> None:
    """Shading, reference row and row order."""
    shading = _choice(
        answers.get("shading"), SHADING_CONFIDENCE, _question_specs()["shading"]["criteria"])
    if shading:
        plan["shading"] = shading

    reference = _reference_candidates(table, columns, guess, rows)
    if reference is not None:
        criteria = {value: None for value in reference["values"]}
        criteria.update(_question_specs()["reference"]["criteria"])
        picked_reference = _choice(answers.get("reference"), REFERENCE_CONFIDENCE, criteria)
        if picked_reference == NONE:
            plan.pop("reference", None)
        elif picked_reference:
            plan["reference"] = {"column": reference["column"]["index"], "value": picked_reference}

    if _rankable(columns, guess, rows) is None:
        return
    direction = _choice(
        answers.get("order"), ORDER_CONFIDENCE, _question_specs()["order"]["criteria"])
    if direction in {"descending", "ascending"} and kind not in OWN_FRAME:
        column = (guess.get("series") or [None])[0]
        series = plan.get("series") or []
        if column not in series:
            column = series[0]
        plan["order"] = {"column": column, "direction": direction}
    elif direction == "table" and kind not in OWN_FRAME:
        plan.pop("order", None)


def _apply_polarity(
    plan: dict[str, Any],
    columns: list[dict[str, Any]],
    answers: Mapping[str, Any],
    guess: Mapping[str, Any],
    series: list[int],
) -> None:
    """Per-series polarity plus the multi-stage move direction."""
    polarity_criteria = _question_specs()["polarity"]["criteria"]
    polarity = _normalize_polarity(guess.get("polarity"))
    for index in series:
        answer = _choice(answers.get(f"p{index}"), POLARITY_CONFIDENCE, polarity_criteria)
        key = str(index)
        if answer == "neutral":
            polarity.pop(key, None)
        elif answer in {"higher", "lower"}:
            polarity[key] = answer
    move = _choice(answers.get("move"), POLARITY_CONFIDENCE, _question_specs()["move"]["criteria"])
    if move:
        for column in _move_columns(columns, guess):
            if column["index"] not in series:
                continue
            key = str(column["index"])
            if move == "neutral":
                polarity.pop(key, None)
            elif move in {"higher", "lower"}:
                polarity[key] = move
    if polarity:
        plan["polarity"] = polarity
    else:
        plan.pop("polarity", None)


def _apply_rows_rivals_focus(
    plan: dict[str, Any],
    columns: list[dict[str, Any]],
    rows: list[int],
    answers: Mapping[str, Any],
    guess: Mapping[str, Any],
) -> None:
    """Metric-row polarity, the rival axis, and the focused row."""
    polarity_criteria = _question_specs()["polarity"]["criteria"]
    row_polarity = _normalize_polarity(guess.get("rowPolarity"))
    for row in rows:
        answer = _choice(answers.get(f"q{row}"), POLARITY_CONFIDENCE, polarity_criteria)
        key = str(row)
        if answer == "neutral":
            row_polarity.pop(key, None)
        elif answer in {"higher", "lower"}:
            row_polarity[key] = answer
    if row_polarity:
        plan["rowPolarity"] = row_polarity
    else:
        plan.pop("rowPolarity", None)

    rivals = _choice(
        answers.get("rivals"), RIVALS_CONFIDENCE, _question_specs()["rivals"]["criteria"])
    if rivals == "neither":
        plan.pop("rivals", None)
    elif rivals:
        plan["rivals"] = rivals

    names = _row_names(columns, rows, guess)
    focus_answer = answers.get("focus")
    if not isinstance(focus_answer, ChoiceAnswer) or not names:
        return
    odds = _odds(focus_answer, focus_answer.choice)
    local = [row for row in (guess.get("focus") or []) if isinstance(row, int)]
    if focus_answer.choice == NONE:
        plan["focus"] = []
    elif focus_answer.choice in names:
        row = names[focus_answer.choice]
        if odds >= SURE_FOCUS or (row in local and odds >= AGREED_FOCUS):
            plan["focus"] = [row]


def _apply_title(plan: dict[str, Any], table: Mapping[str, Any], answers: Mapping[str, Any]) -> None:
    """The takeaway the client offered, when the judge is confident it is the table's point."""
    takeaways = table.get("takeaways")
    title_answer = answers.get("title")
    if not isinstance(takeaways, list) or not isinstance(title_answer, ChoiceAnswer):
        return
    odds = _odds(title_answer, title_answer.choice)
    by_key = {
        fact["key"]: fact.get("text") for fact in takeaways
        if isinstance(fact, Mapping) and isinstance(fact.get("key"), str)
    }
    if title_answer.choice == NONE and odds >= TITLE_CONFIDENCE:
        plan.pop("title", None)
    elif title_answer.choice in by_key and odds >= TITLE_CONFIDENCE:
        title = by_key[title_answer.choice]
        if isinstance(title, str) and title.strip():
            plan["title"] = title


def plan_from_answers(
    table: Mapping[str, Any],
    guess: Mapping[str, Any],
    answers: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Fold confident judge answers into a copy of ``guess``; ``None`` when the judge sees no chart."""
    structured = _plan_structure(table, guess, answers)
    if structured is None:
        return None
    plan, columns, rows, label, series = structured
    label_index = label["index"] if label is not None else None
    _apply_naming(plan, columns, answers, guess, label, label_index)
    _apply_layout(plan, table, columns, rows, answers, guess, plan["kind"])
    _apply_polarity(plan, columns, answers, guess, series)
    _apply_rows_rivals_focus(plan, columns, rows, answers, guess)
    _apply_title(plan, table, answers)
    return plan

def plan_table_chart(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Plan one table's chart through the judge, falling back to the client's local guess.

    Returns ``{"plan": ChartPlan | None, "source": "judge" | "local"}``. A judge that
    answers ``none`` yields ``{"plan": None, "source": "judge"}``; any failure yields
    ``{"plan": None, "source": "local"}`` so the caller draws its own guess.
    """
    try:
        if not isinstance(payload, Mapping):
            raise ValueError("table chart request must be an object")
        markdown = payload.get("markdown")
        table = payload.get("table")
        guess = payload.get("guess")
        if not isinstance(markdown, str) or not isinstance(table, Mapping) or not isinstance(guess, Mapping):
            raise ValueError("table chart request needs markdown, table and guess")
        # ``auto`` trusts only a native (calibrated) judge; without one the client
        # keeps its local guess and no text model sees the table. ``smart`` may judge.
        if str(payload.get("mode") or "smart").strip().lower() == "auto" and not _has_native_judge():
            return {"plan": None, "source": "local"}
        state, questions = judge_request(markdown, table, guess)
        if len(questions) <= 2:
            return {"plan": None, "source": "local"}
        from agent.judgment.service import judge

        result = judge(
            JudgmentRequest(state=state, questions=questions),
            purpose="auto-graph",
            timeout_ms=PICK_TIMEOUT_MS,
        )
        plan = plan_from_answers(table, guess, result.answers)
        return {"plan": plan, "source": "judge"}
    except Exception as exc:  # health: allow BLE001 -- a table's chart must never fail the turn
        logger.debug("auto-graph planning fell back to the local guess: %s", exc)
        return {"plan": None, "source": "local"}


__all__ = ["PICK_TIMEOUT_MS", "judge_request", "plan_from_answers", "plan_table_chart"]
