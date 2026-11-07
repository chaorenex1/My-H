"""Small Handlebars-compatible prompt renderer used by judgment prompts and questions."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from agent.judgment.types import JudgmentValidationError

_TAG_RE = re.compile(r"(\{\{\{.*?\}\}\}|\{\{.*?\}\})", re.DOTALL)
_STANDALONE_TAG_RE = re.compile(r"^\{\{[^{}]*\}\}$")
_TABLE_ROW_RE = re.compile(r"\|.*\|")
_TABLE_SEPARATOR_RE = re.compile(r"\|[-:\s|]+\|")
_CLOSING_TAG_RE = re.compile(r"^</[a-z_-]+>$")
_MISSING = object()


def js_number(value: float) -> str:
    """ECMAScript ``String(Number(value))`` for prompt interpolation and state scalars."""
    if isinstance(value, int):
        if abs(value) <= 2**53:
            return str(value)
        try:
            value = float(value)
        except OverflowError:
            return "Infinity" if value > 0 else "-Infinity"
    if not math.isfinite(value):
        return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    absolute = abs(value)
    raw = repr(value).lower()
    if 1e-6 <= absolute < 1e21:
        fixed = format(Decimal(raw), "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    mantissa, exponent = raw.split("e")
    mantissa = mantissa.rstrip("0").rstrip(".") if "." in mantissa else mantissa
    exp = int(exponent)
    return f"{mantissa}e{'+' if exp >= 0 else '-'}{abs(exp)}"


def _tokens(template: str) -> list[tuple[str, str, bool]]:
    tokens: list[tuple[str, str, bool]] = []
    position = 0
    for match in _TAG_RE.finditer(template):
        if match.start() > position:
            tokens.append(("text", template[position:match.start()], False))
        tag = match.group(1)
        is_raw = tag.startswith("{{{")
        expression = tag[3:-3] if is_raw else tag[2:-2]
        tokens.append(("tag", expression.strip(), is_raw))
        position = match.end()
    if position < len(template):
        tokens.append(("text", template[position:], False))
    return tokens


def _without_standalone_lines(template: str) -> str:
    kept: list[str] = []
    for line in template.splitlines(keepends=True):
        stripped = line.strip()
        if _STANDALONE_TAG_RE.fullmatch(stripped):
            expression = stripped[2:-2].strip()
            if expression.startswith(("#", "/")) or expression == "else":
                kept.append(stripped)
                continue
        kept.append(line)
    return "".join(kept)


def _compact_table_row(line: str) -> str:
    return "|".join(cell.strip() for cell in line.split("|"))


def _compact_table_separator(line: str) -> str:
    cells = [cell.strip() for cell in line.split("|") if cell.strip()]
    compacted = []
    for cell in cells:
        left, right = cell.startswith(":"), cell.endswith(":")
        compacted.append(":---:" if left and right else ":---" if left else "---:" if right else "---")
    return "|" + "|".join(compacted) + "|"


def format_prompt(content: str) -> str:
    """Apply omp's post-render whitespace, Markdown-table, and fenced-code formatting."""
    source = content.split("\n")
    result: list[str] = []
    in_code_block = False
    index = 0
    while index < len(source):
        line = source[index].rstrip()
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_code_block = not in_code_block
            result.append(line)
            index += 1
            continue
        if in_code_block:
            result.append(line)
            index += 1
            continue
        if stripped.startswith("|"):
            indent = len(line) - len(stripped)
            if _TABLE_SEPARATOR_RE.fullmatch(stripped):
                line = line[:indent] + _compact_table_separator(stripped)
            elif _TABLE_ROW_RE.fullmatch(stripped):
                line = line[:indent] + _compact_table_row(stripped)
        if not line.strip():
            next_index = index + 1
            if next_index == len(source) or not source[next_index].strip():
                while result and not result[-1]:
                    result.pop()
                while next_index < len(source) and not source[next_index].strip():
                    next_index += 1
                index = next_index
                continue
            if not result or not result[-1]:
                index += 1
                continue
            result.append("")
            index += 1
            continue
        if _CLOSING_TAG_RE.fullmatch(stripped):
            while result and not result[-1]:
                result.pop()
        result.append(line)
        index += 1
    while result and not result[-1]:
        result.pop()
    return "\n".join(result)


def _lookup(
    expression: str,
    context: Mapping[str, Any],
    parents: Sequence[Mapping[str, Any]],
    *,
    strict: bool,
) -> Any:
    if expression == "@last":
        value = context.get("@last", _MISSING)
    else:
        while expression.startswith("../"):
            expression = expression[3:]
            if not parents:
                if strict:
                    raise JudgmentValidationError("template references a missing parent context")
                return _MISSING
            context, parents = parents[0], parents[1:]
        if expression in ("this", "."):
            value = context.get("this", context)
        else:
            name = expression.removeprefix("this.")
            value = context.get(name, _MISSING)
            if value is _MISSING:
                value = next((parent[name] for parent in parents if name in parent), _MISSING)
    if value is _MISSING and strict:
        raise JudgmentValidationError(f"template references unresolved value {expression!r}")
    return value


def _truthy(value: Any) -> bool:
    if value is _MISSING or value is None:
        return False
    if isinstance(value, (list, tuple, str)):
        return bool(value)
    if isinstance(value, Mapping):
        return True
    return bool(value)


def _stringify(value: Any) -> str:
    if value is None or value is _MISSING:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return js_number(value)
    if isinstance(value, (list, tuple)):
        return ",".join(_stringify(item) for item in value)
    if isinstance(value, Mapping):
        return "[object Object]"
    return str(value)


def render_template(template: str, context: Mapping[str, Any] | None = None, *, strict: bool = False) -> str:
    """Render `if`/`unless`/`each`, parent paths, `@last`, and raw or escaped variables.

    Escaping is intentionally disabled, matching omp's judgment prompt renderer. `strict=True`
    rejects unresolved variables and unsupported helpers for question instructions.
    """
    root: Mapping[str, Any] = context or {}
    tokens = _tokens(_without_standalone_lines(template))
    position = 0

    def parse(stop: set[str] | None = None) -> tuple[list[tuple[Any, ...]], int]:
        nonlocal position
        nodes: list[tuple[Any, ...]] = []
        while position < len(tokens):
            kind, value, raw = tokens[position]
            if kind == "text":
                nodes.append(("text", value))
                position += 1
                continue
            if stop and value in stop:
                return nodes, position
            if value.startswith("#"):
                head, separator, expression = value[1:].partition(" ")
                if head not in {"if", "unless", "each"}:
                    raise JudgmentValidationError(f"unsupported template helper {head!r}")
                if not separator or not expression.strip():
                    raise JudgmentValidationError(f"template helper {head!r} needs a value")
                position += 1
                children, close_at = parse({f"/{head}", "else"})
                else_children: list[tuple[Any, ...]] = []
                if close_at < len(tokens) and tokens[close_at][1] == "else":
                    position = close_at + 1
                    else_children, close_at = parse({f"/{head}"})
                if close_at >= len(tokens):
                    raise JudgmentValidationError(f"unclosed template block {head!r}")
                position = close_at + 1
                nodes.append(("block", head, expression.strip(), children, else_children))
            elif value.startswith("/") or value == "else":
                return nodes, position
            elif value.startswith("!"):
                position += 1
            else:
                if strict and (not value or any(char.isspace() for char in value)):
                    raise JudgmentValidationError(f"unsupported template expression {value!r}")
                nodes.append(("var", value, raw))
                position += 1
        return nodes, position

    def render_nodes(
        nodes: list[tuple[Any, ...]],
        current: Mapping[str, Any],
        parents: Sequence[Mapping[str, Any]],
    ) -> str:
        output: list[str] = []
        for node in nodes:
            if node[0] == "text":
                output.append(node[1])
                continue
            if node[0] == "var":
                value = _lookup(node[1], current, parents, strict=strict)
                output.append(_stringify(value))
                continue
            _, helper, expression, children, else_children = node
            value = _lookup(expression, current, parents, strict=strict)
            if helper in {"if", "unless"}:
                condition = _truthy(value) == (helper == "if")
                branch = children if condition else else_children
                output.append(render_nodes(branch, current, parents))
            else:
                if value is _MISSING or value is None:
                    items = ()
                elif isinstance(value, (list, tuple)):
                    items = value
                else:
                    items = ()
                if not items:
                    output.append(render_nodes(else_children, current, parents))
                for index, item in enumerate(items):
                    item_context = dict(item) if isinstance(item, Mapping) else {"this": item}
                    item_context["this"] = item
                    item_context["@last"] = index == len(items) - 1
                    output.append(render_nodes(children, item_context, [current, *parents]))
        return "".join(output)

    nodes, end = parse()
    if end != len(tokens):
        raise JudgmentValidationError("unexpected closing template block")
    return format_prompt(render_nodes(nodes, root, []))
