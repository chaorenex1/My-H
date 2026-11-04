"""TTSR runtime: rule selection, tool-argument matching, reminders, judged ``question`` rules.

ADR-6 P1 scopes this to local omp-ui viewers: rules are discovered once per turn, matched against
the final arguments of a tool call before it runs, announced with ``ttsr.triggered``, and delivered
after the batch as one hidden ``role:"custom"`` ``ttsr-injection`` reminder. ADR-11 P3 adds
``question`` rules: after a completed assistant output, every eligible rule's question is judged in
one request (``noul``, threshold 0.7) and a flagged rule delivers a hidden warning once per message.

Nothing here touches the system prompt, tools, transcript rows, or the turn loop when the gateway
did not install a coordinator (CLI, Desktop, messaging, ACP, subagents).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from agent.rule_utils import discover_rules
from agent.ttsr_ast import AST_GREP_TIMEOUT_S, ast_match, language_for_path, resolve_ast_grep
from agent.ttsr_prompts import render_interrupt, render_judged_warning, render_tool_reminder

logger = logging.getLogger(__name__)

#: A single file digest above this size is skipped, never truncated (ADR-6 D4).
MAX_DIGEST_BYTES = 1_048_576
#: Jev's ~33k branch limit leaves room for the request template; ~2 chars/token on dense code.
JUDGED_CONTENT_MAX_CHARS = 56_000
JUDGED_RULE_THRESHOLD = 0.7
JUDGE_MODES = ("auto", "on", "off")
INTERRUPT_MODES = ("always", "prose-only", "tool-only", "never")
REPEAT_MODES = ("once", "after-gap")

#: omp rule scopes name omp's tools; Hermes registers different names (ADR-6 D4).
TOOL_ALIASES = {"edit": "patch", "write": "write_file", "read": "read_file"}

_TOKEN_RE = re.compile(
    r"^(?:(tool)(?::([a-z0-9_-]+))?|([a-z0-9_-]+))(?:\(([^)]+)\))?$", re.IGNORECASE)


def injection_row(
    content: str, rules: Iterable[TtsrRule], *, matched: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """ADR-6 D5/D12 carrier: one hidden ``role:"custom"`` row for a delivered reminder.

    This is the ADR-10 D1 custom shape, not a provider role: ``project_for_api`` converts it to
    ``developer`` at request time and strips the display/custom fields. ``matched`` is only set on
    interrupt rows (rule name → excerpt).
    """
    details: dict[str, Any] = {"rules": [rule.name for rule in rules]}
    if matched:
        details["matched"] = dict(matched)
    return {
        "role": "custom",
        "custom_type": "ttsr-injection",
        "content": content,
        "display": False,
        "display_only": False,
        "details": details,
        "attribution": "agent",
        "legacy_role": "user",
    }


@dataclass(frozen=True)
class TtsrSettings:
    enabled: bool = True
    builtin_rules: bool = True
    disabled_rules: tuple[str, ...] = ()
    judge: str = "auto"
    interrupt_mode: str = "always"
    repeat_mode: str = "once"
    repeat_gap: int = 10
    problems: tuple[str, ...] = ()


def _as_bool(raw: Any, default: bool, problem: str, problems: list[str]) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    problems.append(problem)
    return default


def load_ttsr_settings(config: Mapping[str, Any] | None = None) -> TtsrSettings:
    from hermes_cli.config import cfg_get, load_config

    cfg = dict(config) if config is not None else load_config()
    block = cfg_get(cfg, "ttsr", default={})
    if not isinstance(block, Mapping):
        block = {}
    problems: list[str] = []
    raw_disabled = block.get("disabled_rules", [])
    if isinstance(raw_disabled, str):
        disabled = (raw_disabled.strip(),) if raw_disabled.strip() else ()
    elif isinstance(raw_disabled, (list, tuple)):
        disabled = tuple(str(name).strip() for name in raw_disabled if str(name).strip())
    else:
        disabled = ()
        problems.append("ttsr.disabled_rules must be a list")
    judge = str(block.get("judge", "auto") or "auto").strip().lower()
    if judge not in JUDGE_MODES:
        problems.append(f"ttsr.judge must be one of {JUDGE_MODES}")
        judge = "auto"
    interrupt_mode = str(block.get("interrupt_mode", "always") or "always").strip().lower()
    if interrupt_mode not in INTERRUPT_MODES:
        problems.append(f"ttsr.interrupt_mode must be one of {INTERRUPT_MODES}")
        interrupt_mode = "always"
    repeat_mode = str(block.get("repeat_mode", "once") or "once").strip().lower()
    if repeat_mode not in REPEAT_MODES:
        problems.append(f"ttsr.repeat_mode must be one of {REPEAT_MODES}")
        repeat_mode = "once"
    raw_gap = block.get("repeat_gap", 10)
    if isinstance(raw_gap, bool) or not isinstance(raw_gap, int) or raw_gap < 1:
        problems.append("ttsr.repeat_gap must be a positive integer")
        repeat_gap = 10
    else:
        repeat_gap = raw_gap
    context_mode = block.get("context_mode")
    if context_mode is not None and str(context_mode).strip().lower() != "discard":
        problems.append(
            f"ttsr.context_mode={context_mode} is not implemented; using discard")
    enabled = _as_bool(block.get("enabled"), True, "ttsr.enabled must be a bool", problems)
    builtin_rules = _as_bool(
        block.get("builtin_rules"), True, "ttsr.builtin_rules must be a bool", problems)
    for problem in problems:
        logger.warning("ttsr config: %s", problem)
    return TtsrSettings(
        enabled=enabled,
        builtin_rules=builtin_rules,
        disabled_rules=disabled,
        judge=judge,
        interrupt_mode=interrupt_mode,
        repeat_mode=repeat_mode,
        repeat_gap=repeat_gap,
        problems=tuple(problems),
    )


# -- scope tokens (omp ``capability/rule.ts``) ------------------------------------------------


@dataclass(frozen=True)
class ToolScope:
    tool_name: str = ""
    path_pattern: str = ""


@dataclass(frozen=True)
class TtsrScope:
    allow_text: bool
    allow_thinking: bool
    allow_any_tool: bool
    tool_scopes: tuple[ToolScope, ...]


#: omp's default scope is text + tool (thinking is opt-in).
DEFAULT_SCOPE = TtsrScope(True, False, True, ())


def split_scope_tokens(value: str) -> list[str]:
    """Split a comma list without breaking ``{a,b}``, ``(a,b)``, ``[a,b]`` or quoted commas."""
    tokens: list[str] = []
    current: list[str] = []
    paren = bracket = brace = 0
    quote = ""
    for char in value:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "{":
            brace += 1
        elif char == "}":
            brace = max(0, brace - 1)
        elif char == "," and not (paren or bracket or brace):
            token = "".join(current).strip()
            if token:
                tokens.append(token)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        tokens.append(tail)
    return tokens


def _canonical_tool(name: str) -> str:
    return TOOL_ALIASES.get(str(name or "").strip().lower(), str(name or "").strip().lower())


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1].strip()
    return token


def parse_scope(tokens: Sequence[str]) -> TtsrScope:
    if not tokens:
        return DEFAULT_SCOPE
    allow_text = allow_thinking = allow_any_tool = False
    tool_scopes: list[ToolScope] = []
    for raw in tokens:
        token = _strip_quotes(str(raw).strip())
        if not token:
            continue
        lowered = token.lower()
        if lowered == "text":
            allow_text = True
            continue
        if lowered == "thinking":
            allow_thinking = True
            continue
        if lowered in ("tool", "toolcall"):
            allow_any_tool = True
            continue
        match = _TOKEN_RE.match(token)
        if match is None:
            continue
        has_prefix, scoped_tool, bare_tool, path_pattern = match.groups()
        name = scoped_tool if has_prefix else bare_tool
        canonical = _canonical_tool(name) if name else ""
        pattern = str(path_pattern or "").strip()
        if has_prefix and not canonical and not pattern:
            allow_any_tool = True
            continue
        tool_scopes.append(ToolScope(canonical, pattern))
    return TtsrScope(allow_text, allow_thinking, allow_any_tool, tuple(tool_scopes))


# -- globs -----------------------------------------------------------------------------------


@lru_cache(maxsize=512)
def _segment_regex(segment: str) -> re.Pattern[str]:
    out: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "{":
            end = segment.find("}", index + 1)
            if end == -1:
                out.append(re.escape(char))
            else:
                choices = segment[index + 1:end].split(",")
                out.append("(?:" + "|".join(re.escape(choice) for choice in choices) + ")")
                index = end
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


def _match_segments(pattern: Sequence[str], segments: Sequence[str]) -> bool:
    if not pattern:
        return not segments
    head = pattern[0]
    if head == "**":
        return _match_segments(pattern[1:], segments) or (
            bool(segments) and _match_segments(pattern, segments[1:]))
    return (bool(segments)
            and bool(_segment_regex(head).match(segments[0]))
            and _match_segments(pattern[1:], segments[1:]))


def glob_matches(pattern: str, path: str) -> bool:
    """Bun.Glob-ish path match: ``*`` stays in a segment, ``**`` crosses, ``{a,b}`` expands.

    omp also retries a pattern against the basename, so ``*.ts`` matches ``src/a.ts``.
    """
    pattern = str(pattern or "").replace("\\", "/").removeprefix("./")
    normalized = str(path or "").replace("\\", "/")
    if not pattern or not normalized:
        return False
    segments = normalized.split("/")
    if _match_segments(pattern.split("/"), segments):
        return True
    basename = segments[-1]
    return basename != normalized and _match_segments(pattern.split("/"), [basename])


def _paths_match(patterns: Iterable[str], paths: Iterable[str]) -> bool:
    patterns = tuple(patterns)
    paths = tuple(paths)
    return any(glob_matches(pattern, path) for pattern in patterns for path in paths)


def _display_path(path: str, cwd: str) -> str:
    raw = str(path or "")
    try:
        if cwd and os.path.isabs(raw):
            return os.path.relpath(raw, cwd).replace("\\", "/")
    except (OSError, ValueError):
        pass
    return raw.replace("\\", "/")


# -- tool digests ----------------------------------------------------------------------------


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def tool_digests(tool_name: str, args: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """Per-file source text omp's matcher hooks expose, never ``json.dumps(args)``."""
    if not isinstance(args, Mapping):
        return []
    name = _canonical_tool(tool_name)
    if name == "write_file":
        path, content = _string(args.get("path")), _string(args.get("content"))
        return [(path, content)] if path and content else []
    if name == "patch":
        mode = str(args.get("mode") or "replace").strip().lower()
        if mode == "replace":
            path, new_string = _string(args.get("path")), _string(args.get("new_string"))
            return [(path, new_string)] if path and new_string else []
        patch_text = _string(args.get("patch"))
        if not patch_text:
            return []
        return _v4a_digests(patch_text)
    return []


def _response_outputs(response: Any) -> tuple[str, str]:
    """``(text, reasoning)`` of a non-streaming provider response; empty when unavailable."""
    choices = getattr(response, "choices", None)
    message = getattr(choices[0], "message", None) if choices else None
    content = getattr(message, "content", None)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            str(block.get("text") or "") for block in content if isinstance(block, Mapping))
    else:
        text = ""
    reasoning = (
        getattr(message, "reasoning_content", None)
        or getattr(message, "reasoning", None)
        or ""
    )
    if not isinstance(reasoning, str):
        reasoning = ""
    return text, reasoning


def _v4a_digests(patch_text: str) -> list[tuple[str, str]]:
    from tools.patch_parser import OperationType, parse_v4a_patch

    operations, error = parse_v4a_patch(patch_text)
    if error:
        logger.debug("ttsr: V4A patch not parseable: %s", error)
        return []
    digests: list[tuple[str, str]] = []
    for operation in operations:
        if operation.operation is OperationType.DELETE:
            continue
        added = [line.content for hunk in operation.hunks for line in hunk.lines
                 if line.prefix == "+"]
        if operation.operation is OperationType.MOVE:
            target = operation.new_path or ""
            if target and added:
                digests.append((target, "\n".join(added)))
            continue
        if operation.file_path and added:
            digests.append((operation.file_path, "\n".join(added)))
    return digests


# -- rule selection --------------------------------------------------------------------------


@dataclass(frozen=True)
class TtsrRule:
    name: str
    description: str
    content: str
    source: str
    rule_id: str
    scope: TtsrScope
    globs: tuple[str, ...]
    conditions: tuple[re.Pattern[str], ...]
    ast_conditions: tuple[str, ...]
    question: str
    builtin: bool
    #: Resolved from the rule's own ``interruptMode`` or the global default (ADR-6 D9).
    interrupt_mode: str = "never"
    #: Empty = use the global ``ttsr.repeat_mode`` / ``ttsr.repeat_gap``.
    repeat_mode: str = ""
    repeat_gap: int | None = None


def _is_builtin(source: str) -> bool:
    try:
        from pm.paths import install_root

        return Path(source).resolve().is_relative_to(install_root().resolve())
    except (OSError, ValueError):
        return False


def _compile_conditions(patterns: Sequence[str]) -> tuple[re.Pattern[str], ...] | None:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            logger.warning("ttsr: rule condition does not compile (%s): %s", pattern, exc)
            return None
    return tuple(compiled)


def _raw_tokens(meta: Mapping[str, Any], key: str, fallback: Sequence[str]) -> tuple[str, ...]:
    """Frontmatter list with ``{a,b}``/quote-aware commas; ``normalize_metadata`` splits naively."""
    raw = meta.get("raw") or {}
    value = raw.get(key) if isinstance(raw, Mapping) else None
    if key == "globs" and (not value or value == []) and isinstance(raw, Mapping):
        value = raw.get("paths")
    if isinstance(value, str):
        return tuple(split_scope_tokens(value))
    if isinstance(value, (list, tuple)):
        tokens: list[str] = []
        for item in value:
            tokens.extend(split_scope_tokens(str(item)))
        return tuple(tokens)
    return tuple(fallback)


def _rule_repeat_settings(name: str, meta: Mapping[str, Any]) -> tuple[str, int | None]:
    raw_meta = meta.get("raw") if isinstance(meta.get("raw"), Mapping) else {}
    repeat_mode = str(
        raw_meta.get("repeatMode") or raw_meta.get("repeat_mode") or "").strip().lower()
    if repeat_mode and repeat_mode not in REPEAT_MODES:
        logger.warning("ttsr: rule %s has invalid repeatMode %s; using the global default",
                       name, repeat_mode)
        repeat_mode = ""
    raw_gap = raw_meta.get("repeatGap", raw_meta.get("repeat_gap"))
    if raw_gap is None:
        return repeat_mode, None
    if isinstance(raw_gap, bool) or not isinstance(raw_gap, int) or raw_gap < 1:
        logger.warning("ttsr: rule %s has invalid repeatGap %r; using the global default",
                       name, raw_gap)
        return repeat_mode, None
    return repeat_mode, raw_gap


def _resolve_interrupt_mode(
    meta: Mapping[str, Any], settings: TtsrSettings, *, question: str,
) -> tuple[str | None, str | None]:
    """``(mode, unsupported_reason)``; ``question`` rules never interrupt (ADR-6 D9)."""
    raw = str(meta.get("interrupt_mode") or "").strip().lower()
    if question:
        # Even with conditions, a judged rule has no interrupt path; still reject bad values.
        return ("never", None) if not raw or raw in INTERRUPT_MODES else (
            None, f"interruptMode: {raw}")
    if raw:
        return (raw, None) if raw in INTERRUPT_MODES else (None, f"interruptMode: {raw}")
    return settings.interrupt_mode, None


def select_rules(
    rules: Sequence[Mapping[str, Any]], settings: TtsrSettings, *,
    ast_available: bool = False,
) -> tuple[list[TtsrRule], list[tuple[str, str]]]:
    """Fold by name, apply switches, and split active rules from unsupported ones (ADR-6 D2)."""
    active: list[TtsrRule] = []
    unsupported: list[tuple[str, str]] = []
    seen: set[str] = set()
    disabled = set(settings.disabled_rules)
    for row in rules:
        meta = row.get("metadata") or {}
        name = str(row.get("name") or row.get("rule_id") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if name in disabled:
            continue
        builtin = _is_builtin(str(row.get("source") or ""))
        if builtin and not settings.builtin_rules:
            continue
        conditions_raw = tuple(meta.get("condition") or ())
        ast_conditions = tuple(
            str(pattern).strip() for pattern in (meta.get("ast_condition") or ())
            if str(pattern).strip())
        question = str(meta.get("question") or "").strip()
        if not conditions_raw and not ast_conditions and not question:
            continue
        interrupt_mode, reason = _resolve_interrupt_mode(
            meta, settings, question=question)
        if interrupt_mode is None:
            unsupported.append((name, str(reason)))
            continue
        if ast_conditions and not conditions_raw and not question and not ast_available:
            unsupported.append((name, "ast-grep CLI unavailable (hermes pm install ast-grep)"))
            continue
        compiled = _compile_conditions(conditions_raw)
        if compiled is None:
            continue
        repeat_mode, repeat_gap = _rule_repeat_settings(name, meta)
        active.append(TtsrRule(
            name=name,
            description=str(row.get("description") or ""),
            content=str(row.get("body") or ""),
            source=str(row.get("source") or ""),
            rule_id=str(row.get("rule_id") or ""),
            scope=parse_scope(_raw_tokens(meta, "scope", meta.get("scope") or ())),
            globs=_raw_tokens(meta, "globs", meta.get("globs") or ()),
            conditions=compiled,
            ast_conditions=ast_conditions,
            question=question,
            builtin=builtin,
            interrupt_mode=interrupt_mode,
            repeat_mode=repeat_mode,
            repeat_gap=repeat_gap,
        ))
    return active, unsupported


def _scope_allows_tool(rule: TtsrRule, tool_name: str, paths: Sequence[str]) -> bool:
    scope = rule.scope
    if scope.allow_any_tool:
        return True
    for tool_scope in scope.tool_scopes:
        if tool_scope.tool_name and tool_scope.tool_name != tool_name:
            continue
        if tool_scope.path_pattern and not _paths_match((tool_scope.path_pattern,), paths):
            continue
        return True
    return False


def _scope_allows_text(rule: TtsrRule) -> bool:
    return rule.scope.allow_text


def _scope_allows_thinking(rule: TtsrRule) -> bool:
    return rule.scope.allow_thinking


def rule_payload(rule: TtsrRule) -> dict[str, str]:
    return {"name": rule.name, "description": rule.description, "content": rule.content}


# -- incremental stream matching (omp ``StreamBuffer``/``ConditionTraits``) -------------------

#: A cross-line condition rescans the whole buffer once it grew by this fraction since its last
#: full scan; any condition rescans a long region (an unterminated line) at the same pace.
FULL_RESCAN_GROWTH = 0.25
#: Buffers and regions up to this length are cheap enough to rescan whole on every check.
FULL_RESCAN_ALWAYS_BELOW = 4096
EXCERPT_CONTEXT = 60
EXCERPT_MATCH_MAX = 120

_INLINE_FLAGS_RE = re.compile(r"^\(\?[aiLmsux]+\)")


@dataclass(frozen=True)
class ConditionTraits:
    """How a compiled condition may be scanned incrementally (ADR-6 D10)."""

    slice_safe: bool = True
    line_local: bool = True
    monotonic: bool = True


_OPAQUE_CONDITION = ConditionTraits(False, False, False)


def _scan_escape(source: str, index: int) -> tuple[bool, bool, bool, int]:
    """``(crosses_lines, monotonic, slice_safe, next_index)`` for the ``\\`` at ``index``."""
    if index + 1 >= len(source):
        return True, True, True, index + 1
    escape = source[index + 1]
    crosses = escape in "nsSWD"
    monotonic = escape not in "bBZz"
    slice_safe = escape not in "AZz"
    if escape in "xu":
        digits = 2 if escape == "x" else 4
        try:
            crosses = int(source[index + 2:index + 2 + digits], 16) == 0x0A
        except ValueError:
            pass
        return crosses, monotonic, slice_safe, index + 2 + digits
    return crosses, monotonic, slice_safe, index + 2


def _scan_class(source: str, index: int) -> tuple[bool, int]:
    """``(matches_line_break, next_index)`` for the ``[`` at ``index``."""
    end = index + 1
    crosses = False
    if end < len(source) and source[end] == "^":
        crosses = True  # a negated class matches newline unless it excludes it
        end += 1
    while end < len(source) and source[end] != "]":
        if source[end] == "\\" and end + 1 < len(source):
            if source[end + 1] in "sSWDn":
                crosses = True
            end += 2
            continue
        if source[end] == "\n":
            crosses = True
        end += 1
    return crosses, end + 1


def _scan_group(source: str, index: int) -> tuple[int, bool, bool, bool]:
    """``(next_index, slice_safe, monotonic, opaque)`` for the ``(?`` at ``index``."""
    kind = source[index + 2:index + 3]
    if kind == "<" and source[index + 3:index + 4] in ("=", "!"):
        return index + 4, False, True, False  # lookbehind
    if kind == "!":
        return index + 3, True, False, False  # negative lookahead
    if kind in (":", "=", "<", "P"):
        return index + 3, True, True, False
    return index, True, True, True


def analyze_condition(condition: re.Pattern[str]) -> ConditionTraits:
    """Classify a condition; unmodeled syntax stays opaque and forces a whole-buffer scan."""
    source = condition.pattern
    while True:
        match = _INLINE_FLAGS_RE.match(source)
        if match is None:
            break
        source = source[match.end():]
    slice_safe = True
    crosses_lines = False
    monotonic = True
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\\":
            crosses, mono, safe, index = _scan_escape(source, index)
            crosses_lines |= crosses
            monotonic &= mono
            slice_safe &= safe
            continue
        if char == "[":
            crosses, index = _scan_class(source, index)
            crosses_lines |= crosses
            continue
        if char == "(" and source[index + 1:index + 2] == "?":
            index, safe, mono, opaque = _scan_group(source, index)
            if opaque:
                return _OPAQUE_CONDITION
            slice_safe &= safe
            monotonic &= mono
            continue
        if char == "." and condition.flags & re.DOTALL:
            crosses_lines = True
        elif char == "^" and not condition.flags & re.MULTILINE:
            slice_safe = False
        elif char in "$Zz":
            monotonic = False
        elif char == "\n":
            crosses_lines = True
        index += 1
    return ConditionTraits(slice_safe, slice_safe and not crosses_lines, monotonic)


@dataclass
class _ConditionScan:
    scanned_to: int = -1
    line_start: int = 0
    matched: bool = False
    settled: bool = False
    full_scanned_to: int = -1
    pace_from: int = -1


def _match_excerpt(text: str, index: int, length: int) -> str:
    match_end = index + min(length, EXCERPT_MATCH_MAX)
    start = max(0, index - EXCERPT_CONTEXT)
    end = min(len(text), match_end + EXCERPT_CONTEXT)
    body = re.sub(r"\s+", " ", text[start:end]).strip()
    return f"{'…' if start > 0 else ''}{body}{'…' if end < len(text) else ''}"


class StreamBuffer:
    """One stream's accumulated text and how far each condition has scanned it (ADR-6 D10)."""

    def __init__(self) -> None:
        self._scans: dict[re.Pattern[str], _ConditionScan] = {}
        self.excerpts: dict[str, str | None] = {}
        self._parts: list[str] = []
        self._joined = ""
        self._length = 0
        self._line_start = 0
        self._tail = ""
        self._previous_length = 0
        self._region_start = 0
        self._region = ""

    def append(self, delta: str) -> None:
        if delta:
            self._parts.append(delta)
        self._advance(delta)

    def replace(self, snapshot: str) -> None:
        current = self._text()
        if snapshot == current or snapshot.startswith(current):
            self._advance(snapshot[len(current):])
            self._joined = snapshot
            return
        self.excerpts.clear()
        for scan in self._scans.values():
            scan.scanned_to = -1
            scan.line_start = 0
            scan.matched = False
            scan.settled = False
            scan.full_scanned_to = -1
        self._length = 0
        self._line_start = 0
        self._tail = ""
        self._advance(snapshot)
        self._joined = snapshot

    def matches(self, condition: re.Pattern[str], traits: ConditionTraits, final: bool) -> bool:
        scan = self._scans.get(condition)
        if scan is None:
            scan = self._scans[condition] = _ConditionScan()
        if scan.settled:
            return True
        length = self._length
        if scan.scanned_to != length:
            if final or self._region_rescan_due(scan):
                self._scan_region(condition, traits, scan)
            else:
                scan.matched = False
        if scan.matched or traits.line_local or scan.full_scanned_to == length:
            return scan.matched
        rescan_due = (
            final
            or scan.pace_from < 0
            or length <= FULL_RESCAN_ALWAYS_BELOW
            or length - scan.pace_from >= scan.pace_from * FULL_RESCAN_GROWTH
        )
        if not rescan_due:
            return False
        scan.matched = condition.search(self._text()) is not None
        scan.settled = scan.matched and traits.monotonic
        scan.full_scanned_to = length
        scan.pace_from = length
        return scan.matched

    def excerpt(self, rule_name: str, conditions: Sequence[re.Pattern[str]]) -> str | None:
        if rule_name in self.excerpts:
            return self.excerpts[rule_name]
        text = self._text()
        excerpt: str | None = None
        for condition in conditions:
            match = condition.search(text)
            if match is not None and match.group(0):
                excerpt = _match_excerpt(text, match.start(), len(match.group(0)))
                break
        self.excerpts[rule_name] = excerpt
        return excerpt

    def _region_rescan_due(self, scan: _ConditionScan) -> bool:
        if scan.scanned_to < 0:
            return True
        region = self._length - scan.line_start
        scanned = scan.scanned_to - scan.line_start
        return region <= FULL_RESCAN_ALWAYS_BELOW or (
            region - scanned >= scanned * FULL_RESCAN_GROWTH)

    def _scan_region(
        self, condition: re.Pattern[str], traits: ConditionTraits, scan: _ConditionScan,
    ) -> None:
        scan.matched = False
        if traits.slice_safe:
            in_sync = scan.scanned_to == self._previous_length
            start = self._region_start if in_sync else scan.line_start
            region = self._region if in_sync else self._text()[start:]
            match = condition.search(region)
            if match is not None:
                scan.matched = True
                # A line-local match starting on a line already followed by ``\n`` is final.
                scan.settled = traits.monotonic or (
                    traits.line_local and match.start() < self._line_start - start)
            if start == 0:
                scan.full_scanned_to = self._length
                scan.pace_from = self._length
        scan.scanned_to = self._length
        scan.line_start = self._line_start

    def _advance(self, delta: str) -> None:
        self._previous_length = self._length
        self._region_start = self._line_start
        self._region = self._tail + delta
        last_break = delta.rfind("\n")
        if last_break == -1:
            self._tail = self._region
        else:
            self._line_start = self._length + last_break + 1
            self._tail = delta[last_break + 1:]
        self._length += len(delta)

    def _text(self) -> str:
        if self._parts:
            self._joined += "".join(self._parts)
            self._parts.clear()
        return self._joined


# -- coordinator -----------------------------------------------------------------------------


@dataclass
class TtsrCoordinator:
    """One turn's rule snapshot, match stash, repeat state and judgment consumer."""

    session_id: str
    cwd: str
    settings: TtsrSettings
    rules: list[TtsrRule]
    unsupported: list[tuple[str, str]] = field(default_factory=list)
    injected_names: set[str] = field(default_factory=set)
    # Session-lifetime, in-memory "once" marks for matches that were not delivered (ADR-6 D4):
    # shared across turns by ``install_coordinator`` and never written to the repeat table.
    memory_claimed: set[str] = field(default_factory=set)
    #: Optional ast-grep CLI (ADR-6 P2); ``None`` disables ``astCondition`` matching.
    ast_grep: str | None = None
    ast_timeout: float = AST_GREP_TIMEOUT_S
    ast_budget: float = 3.0
    publish: Callable[[str, dict[str, Any]], Any] | None = None
    #: ADR-6 D13: publishes ``message.rewind`` and advances the turn's segment cursor.
    rewind_publish: Callable[[], Any] | None = None
    judge_factory: Callable[..., Any] | None = None
    native_available: bool = False
    session_db: Any = None
    #: Session-lifetime turn counters shared across turn coordinators (ADR-6 D4).
    completed_turns: int = 0
    last_injected_turn: dict[str, int] = field(default_factory=dict)
    _stash: dict[str, list[TtsrRule]] = field(default_factory=dict)
    _judged_stash: dict[str, list[tuple[str, str, list[TtsrRule]]]] = field(default_factory=dict)
    _pending_text_outputs: list[tuple[str, str]] = field(default_factory=list)
    _ast_cache: dict[tuple[str, str, str], bool] = field(default_factory=dict)
    _batch_claimed: set[str] = field(default_factory=set)
    _injected: set[str] = field(default_factory=set)
    _judged_claimed: set[str] = field(default_factory=set)
    _buffers: dict[str, StreamBuffer] = field(default_factory=dict)
    _traits: dict[re.Pattern[str], ConditionTraits] = field(default_factory=dict)
    _stream_published: set[str] = field(default_factory=set)
    _pending_rewind: list[TtsrRule] = field(default_factory=list)
    _rewind_excerpts: dict[str, str] = field(default_factory=dict)
    _abort_requested: bool = False

    def __post_init__(self) -> None:
        self._injected = set(self.injected_names)

    # -- tool matching ------------------------------------------------------------------

    def _usable_digests(self, name: str, args: Mapping[str, Any] | None) -> list[tuple[str, str]]:
        usable: list[tuple[str, str]] = []
        for path, text in tool_digests(name, args):
            if len(text.encode("utf-8", "ignore")) > MAX_DIGEST_BYTES:
                logger.debug("ttsr: digest too large, skipped: %s", path)
                continue
            usable.append((path, text))
        return usable

    def match_tool_start(
        self, call_id: str, tool_name: str, args: Mapping[str, Any] | None,
    ) -> list[dict[str, str]]:
        """P1-compatible passive match (``interruptMode: never`` only)."""
        if not self.settings.enabled or not self.rules:
            return []
        name = _canonical_tool(tool_name)
        passive, _interrupt, _excerpts = self._match_digests(call_id, name, args)
        for rule in passive:
            self._stash.setdefault(str(call_id), []).append(rule)
        return [rule_payload(rule) for rule in passive]

    def match_tool_calls(self, agent: Any, assistant_message: Any) -> bool:
        """Match every call's final arguments before staging/execution (ADR-6 D10).

        Any interrupt rule discards the whole tool-call response; passive matches stay stashed
        for the post-batch reminder. Returns True when the response must be dropped.
        """
        if not self.settings.enabled or not self.rules:
            return False
        if not self._should_abort(agent):
            return False
        from agent.tool_executor import _parse_tool_arguments

        passive: list[tuple[str, TtsrRule]] = []
        interrupts: list[TtsrRule] = []
        excerpts: dict[str, str] = {}
        for tool_call in getattr(assistant_message, "tool_calls", None) or ():
            function = getattr(tool_call, "function", None)
            name = _canonical_tool(getattr(function, "name", "") or "")
            args, parse_error = _parse_tool_arguments(getattr(function, "arguments", None))
            if parse_error or not isinstance(args, Mapping):
                continue
            call_id = str(getattr(tool_call, "id", "") or "")
            matched_passive, matched_interrupt, matched_excerpts = self._match_digests(
                call_id, name, args)
            passive.extend((call_id, rule) for rule in matched_passive)
            for rule in matched_interrupt:
                if rule.name not in {existing.name for existing in interrupts}:
                    interrupts.append(rule)
            excerpts.update(matched_excerpts)
        if not interrupts:
            for call_id, rule in passive:
                self._stash.setdefault(call_id, []).append(rule)
                self._publish_rules([rule])
            return False
        # A discarded response delivers nothing from its batch (ADR-6 D11).
        self._stash, self._judged_stash, self._pending_text_outputs = {}, {}, []
        self._claim_rewind(agent, interrupts, excerpts, abort=False)
        return True

    def _match_digests(
        self, call_id: str, name: str, args: Mapping[str, Any] | None,
    ) -> tuple[list[TtsrRule], list[TtsrRule], dict[str, str]]:
        usable = self._usable_digests(name, args)
        if not usable:
            return [], [], {}
        self._judged_stash.pop(str(call_id), None)
        judged: list[tuple[str, str, list[TtsrRule]]] = []
        if self.judge_enabled():
            for path, text in usable:
                candidates = [
                    rule for rule in self.rules
                    if rule.question
                    and self._can_trigger(rule)
                    and _scope_allows_tool(rule, name, (path,))
                    and (not rule.globs or _paths_match(rule.globs, (path,)))
                    and (not rule.conditions
                         or any(condition.search(text) for condition in rule.conditions))
                ]
                if candidates:
                    judged.append((f"`{name}` call on `{path}`", text, candidates))
        if judged:
            self._judged_stash[str(call_id)] = judged
        passive: list[TtsrRule] = []
        interrupts: list[TtsrRule] = []
        excerpts: dict[str, str] = {}
        ast_deadline = time.monotonic() + self.ast_budget
        for rule in self.rules:
            if rule.question or not (rule.conditions or rule.ast_conditions):
                continue
            if not self._can_trigger(rule) or rule.name in self._batch_claimed:
                continue
            if not _scope_allows_tool(rule, name, (path for path, _text in usable)):
                continue
            hit_text: str | None = None
            for path, text in usable:
                # Scope/globs gate each file, not the batch: a V4A patch touching a.ts and b.md
                # must not let a ``tool:edit(*.ts)`` rule match b.md's added lines.
                if not _scope_allows_tool(rule, name, (path,)):
                    continue
                if rule.globs and not _paths_match(rule.globs, (path,)):
                    continue
                for condition in rule.conditions:
                    match = condition.search(text)
                    if match is None:
                        continue
                    hit_text = text
                    if match.group(0):
                        excerpts[rule.name] = _match_excerpt(
                            text, match.start(), len(match.group(0)))
                    break
                if hit_text is not None:
                    break
                if rule.ast_conditions and self._ast_matches(rule, path, text, ast_deadline):
                    hit_text = text
                    break
            if hit_text is None:
                continue
            self._batch_claimed.add(rule.name)
            self.memory_claimed.add(rule.name)
            self.last_injected_turn[rule.name] = self.completed_turns
            if rule.interrupt_mode in ("always", "tool-only"):
                interrupts.append(rule)
            else:
                passive.append(rule)
        return passive, interrupts, excerpts

    def _ast_matches(self, rule: TtsrRule, path: str, source: str, deadline: float) -> bool:
        """Run the rule's ast-grep patterns against one digest, bounded by the per-turn budget."""
        if not self.ast_grep or not rule.ast_conditions:
            return False
        lang = language_for_path(path)
        if not lang:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.debug("ttsr: ast-grep budget exhausted before %s", rule.name)
            return False
        digest = hashlib.sha256(source.encode("utf-8", "ignore")).hexdigest()
        for pattern in rule.ast_conditions:
            key = (pattern, lang, digest)
            hit = self._ast_cache.get(key)
            if hit is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.debug("ttsr: ast-grep budget exhausted before %s", rule.name)
                    return False
                hit = ast_match(
                    self.ast_grep, pattern, source, lang,
                    timeout=min(self.ast_timeout, max(0.05, remaining)))
                if len(self._ast_cache) >= 512:
                    self._ast_cache.clear()
                self._ast_cache[key] = hit
            if hit:
                return True
        return False

    # -- repeat / rewind ----------------------------------------------------------------

    def _repeat_mode(self, rule: TtsrRule) -> str:
        return rule.repeat_mode or self.settings.repeat_mode

    def _repeat_gap(self, rule: TtsrRule) -> int:
        return rule.repeat_gap or self.settings.repeat_gap

    def _can_trigger(self, rule: TtsrRule) -> bool:
        """ADR-6 D4: ``once`` is permanent; ``after-gap`` re-arms by completed turns."""
        claimed = rule.name in self._injected or rule.name in self.memory_claimed
        if not claimed:
            return True
        if self._repeat_mode(rule) == "once":
            return False
        last = self.last_injected_turn.get(rule.name, 0)
        return self.completed_turns - last >= self._repeat_gap(rule)

    def _publish_rules(self, rules: Sequence[TtsrRule]) -> None:
        fresh = [rule for rule in rules if rule.name not in self._stream_published]
        if not fresh:
            return
        self._stream_published.update(rule.name for rule in fresh)
        if self.publish is None:
            return
        try:
            self.publish("ttsr.triggered", {"rules": [rule_payload(rule) for rule in fresh]})
        except Exception:  # health: allow BLE001 -- a live notice must not fail the turn
            logger.debug("ttsr: ttsr.triggered publish failed", exc_info=True)

    def _should_abort(self, agent: Any) -> bool:
        """User stop > redirect > TTSR (ADR-6 D11): the first two keep the rewind in memory only."""
        if getattr(agent, "_interrupt_requested", False):
            return False
        pending_redirect = getattr(agent, "_has_pending_redirect", None)
        if callable(pending_redirect):
            try:
                if pending_redirect():
                    return False
            except Exception:  # health: allow BLE001 -- a stub without redirect state
                logger.debug("ttsr: redirect probe failed", exc_info=True)
        return True

    def _claim_rewind(
        self, agent: Any, rules: Sequence[TtsrRule], excerpts: Mapping[str, str], *,
        abort: bool, publish_rewind: bool = True,
    ) -> None:
        if not rules:
            return
        if abort and not self._should_abort(agent):
            # User stop / redirect wins: claim in memory only, never notify or rewind (D11).
            for rule in rules:
                self.memory_claimed.add(rule.name)
                self.last_injected_turn[rule.name] = self.completed_turns
            return
        fresh = [rule for rule in rules if rule.name not in self._pending_rewind]
        for rule in fresh:
            self.memory_claimed.add(rule.name)
            self.last_injected_turn[rule.name] = self.completed_turns
        if fresh:
            self._pending_rewind.extend(fresh)
            self._rewind_excerpts.update(
                {rule.name: excerpts[rule.name] for rule in fresh if rule.name in excerpts})
            self._publish_rules(fresh)
        if publish_rewind and not self._abort_requested and self.rewind_publish is not None:
            try:
                self.rewind_publish()
            except Exception:  # health: allow BLE001 -- a live draft reset must not fail the turn
                logger.debug("ttsr: message.rewind publish failed", exc_info=True)
        if not abort or self._abort_requested:
            return
        self._abort_requested = True
        agent._ttsr_rewind_requested = True
        from agent.interrupt_control import _ic_abort_active_request

        _ic_abort_active_request(
            agent, "ttsr_rewind_abort", "Failed to abort request for TTSR rewind")

    def has_pending_rewind(self) -> bool:
        return bool(self._pending_rewind)

    def drop_pending_rewind(self) -> None:
        """A user stop wins: discard the rewind and the discarded response's stash."""
        self._pending_rewind = []
        self._rewind_excerpts = {}
        self._abort_requested = False
        self._stash, self._judged_stash, self._pending_text_outputs = {}, {}, []

    def deliver_rewind(self, agent: Any, messages: list[dict[str, Any]]) -> bool:
        """Append the hidden ``ttsr-interrupt.md`` reminder for a discarded response (D12)."""
        rules = list(self._pending_rewind)
        if not rules:
            return False
        text = "\n\n".join(
            render_interrupt(
                name=rule.name,
                path=_display_path(rule.source, self.cwd),
                content=rule.content,
                excerpt=self._rewind_excerpts.get(rule.name),
            )
            for rule in rules
        )
        from agent.message_metadata import append_message

        append_message(messages, injection_row(
            text, rules, matched={name: self._rewind_excerpts[name]
                                  for name in self._rewind_excerpts}))
        self._mark_injected(rules)
        self._pending_rewind = []
        self._rewind_excerpts = {}
        self._abort_requested = False
        return True

    def note_turn_end(self) -> None:
        self.completed_turns += 1

    def reset_stream_state(self) -> None:
        """A new provider request starts fresh text/thinking buffers (ADR-6 D10)."""
        self._buffers = {}
        self._stream_published = set()

    # -- stream matching ----------------------------------------------------------------

    def check_stream_delta(
        self, agent: Any, source: str, delta: str, final: bool = False, *,
        publish_rewind: bool = True,
    ) -> list[TtsrRule]:
        """Incrementally match one text/thinking delta (ADR-6 D10); returns newly matched rules."""
        if not self.settings.enabled or not self.rules:
            return []
        if source not in ("text", "thinking"):
            return []
        buffer = self._buffers.setdefault(source, StreamBuffer())
        if delta:
            buffer.append(delta)
        matched: list[TtsrRule] = []
        excerpts: dict[str, str] = {}
        for rule in self.rules:
            if rule.question or not rule.conditions:
                continue
            if not self._can_trigger(rule) or rule.name in self._pending_rewind:
                continue
            if source == "text":
                if not _scope_allows_text(rule):
                    continue
                if rule.interrupt_mode not in ("always", "prose-only"):
                    continue
            else:
                if not _scope_allows_thinking(rule):
                    continue
                if rule.interrupt_mode not in ("always", "prose-only"):
                    continue
            if rule.globs:
                continue  # no file paths on a text/thinking stream
            hit = False
            for condition in rule.conditions:
                traits = self._traits.get(condition)
                if traits is None:
                    traits = self._traits[condition] = analyze_condition(condition)
                if buffer.matches(condition, traits, final):
                    hit = True
                    break
            if not hit:
                continue
            matched.append(rule)
            excerpt = buffer.excerpt(rule.name, rule.conditions)
            if excerpt:
                excerpts[rule.name] = excerpt
        if matched:
            self._claim_rewind(
                agent, matched, excerpts, abort=True, publish_rewind=publish_rewind)
        return matched

    def check_final_response(self, agent: Any, response: Any, *, streaming: bool) -> list[TtsrRule]:
        """Response-level final scan: catches throttled cross-line matches and non-streaming."""
        if not self.settings.enabled or not self.rules:
            return []
        matched: list[TtsrRule] = []
        # Non-streaming, and streaming transports with no delta hook (codex app-server):
        # the assembled response is the only place the text exists.
        if not streaming or not self._buffers:
            text, reasoning = _response_outputs(response)
            if text and "text" not in self._buffers:
                matched.extend(self.check_stream_delta(
                    agent, "text", text, publish_rewind=streaming))
            if reasoning and "thinking" not in self._buffers:
                matched.extend(self.check_stream_delta(
                    agent, "thinking", reasoning, publish_rewind=streaming))
        for source in ("text", "thinking"):
            if source in self._buffers:
                matched.extend(self.check_stream_delta(
                    agent, source, "", final=True, publish_rewind=streaming))
        return matched

    def after_tool_batch(
        self, agent: Any, messages: list[dict[str, Any]], num_tools: int,
        *, result_ids: set[str] | None = None,
    ) -> None:
        stash = self._stash
        judged_stash = self._judged_stash
        pending = self._pending_text_outputs
        # ``_batch_claimed`` survives the batch: a matched rule whose call produced no result is
        # suppressed in memory for the rest of the turn (ADR-6 D4) without being written to the
        # repeat table, so a later turn can still deliver it.
        self._stash, self._judged_stash = {}, {}
        self._pending_text_outputs = []
        if not stash and not judged_stash and not pending:
            return
        if getattr(agent, "is_interrupted", False):
            return
        if result_ids is None:
            result_ids = {
                str(message.get("tool_call_id"))
                for message in messages[-num_tools:]
                if isinstance(message, dict) and message.get("role") == "tool"
            }
        delivered: list[TtsrRule] = []
        seen: set[str] = set()
        for call_id, rules in stash.items():
            if call_id not in result_ids:
                continue
            for rule in rules:
                if rule.name not in seen:
                    seen.add(rule.name)
                    delivered.append(rule)
        if delivered:
            text = "\n\n".join(
                render_tool_reminder(
                    name=rule.name,
                    path=_display_path(rule.source, self.cwd),
                    content=rule.content,
                    builtin=rule.builtin,
                )
                for rule in delivered
            )
            from agent.message_metadata import append_message

            append_message(messages, injection_row(text, delivered))
            self._mark_injected(delivered)
        if self.judge_enabled():
            self._judged_claimed = set()
            outputs = [(subject, content, self._judged_text_candidates(content))
                       for subject, content in pending]
            outputs += [output for call_id, items in judged_stash.items()
                        if call_id in result_ids for output in items]
            if outputs:
                self._judge_outputs(messages, outputs)

    # -- judged ``question`` rules (ADR-11 P3) -------------------------------------------

    def stash_tool_round_outputs(self, message: Mapping[str, Any]) -> None:
        """Remember a completed tool-call message's reply/reasoning for the post-tool verdict.

        Judging here would block tool execution; ``after_tool_batch`` runs it once the results
        exist, and the hidden warning lands after those result rows (valid provider ordering).
        """
        if not self.judge_enabled():
            return
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            self._pending_text_outputs.append(("reply", content))
        reasoning = message.get("reasoning") or message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            self._pending_text_outputs.append(("reasoning", reasoning))

    def judge_enabled(self) -> bool:
        if not self.settings.enabled or self.settings.judge == "off":
            return False
        if self.settings.judge == "auto" and not self.native_available:
            return False
        return any(rule.question for rule in self.rules)

    def judge_completed_outputs(
        self,
        messages: list[dict[str, Any]],
        outputs: Sequence[tuple[str, str]],
        finish_reason: str,
    ) -> None:
        if str(finish_reason or "").lower() in ("aborted", "error"):
            return
        if not self.judge_enabled():
            return
        self._judged_claimed = set()
        self._judge_outputs(
            messages,
            [(subject, content, self._judged_text_candidates(content))
             for subject, content in outputs],
        )

    def _judge_outputs(
        self,
        messages: list[dict[str, Any]],
        outputs: Sequence[tuple[str, str, list[TtsrRule]]],
    ) -> None:
        from agent.judgment import JudgmentError, JudgmentRequest, NoulQuestion, resolve_judge

        for subject, content, candidates in outputs:
            candidates = [rule for rule in candidates if self._can_trigger(rule)]
            if not content or not candidates:
                continue
            questions = {f"q{index}": NoulQuestion(rule.question)
                         for index, rule in enumerate(candidates)}
            request = JudgmentRequest(
                state={"output": subject, "content": content[:JUDGED_CONTENT_MAX_CHARS]},
                questions=questions,
            )
            try:
                factory = self.judge_factory or resolve_judge
                result = factory(purpose="ttsr").judge(request, purpose="ttsr")
            except JudgmentError as exc:
                logger.debug("ttsr: judged rule check failed (%s): %s", subject, exc)
                continue
            except Exception as exc:  # health: allow BLE001 -- judged rules are advisory
                logger.debug("ttsr: judged rule check failed (%s): %s", subject, exc)
                continue
            flagged: list[TtsrRule] = []
            for index, rule in enumerate(candidates):
                answer = result.answers.get(f"q{index}")
                probability = getattr(answer, "noul", None)
                if isinstance(probability, (int, float)) and probability >= JUDGED_RULE_THRESHOLD:
                    flagged.append(rule)
            flagged = [rule for rule in flagged if rule.name not in self._judged_claimed]
            if not flagged:
                continue
            self._judged_claimed.update(rule.name for rule in flagged)
            self.memory_claimed.update(rule.name for rule in flagged)
            if self.publish is not None:
                try:
                    self.publish("ttsr.triggered", {"rules": [rule_payload(r) for r in flagged]})
                except Exception:  # health: allow BLE001 -- a live notice must not fail the turn
                    logger.debug("ttsr: ttsr.triggered publish failed", exc_info=True)
            warning = "\n\n".join(
                render_judged_warning(
                    name=rule.name,
                    path=_display_path(rule.source, self.cwd),
                    subject=subject,
                    content=rule.content,
                )
                for rule in flagged
            )
            from agent.message_metadata import append_message

            append_message(messages, injection_row(warning, flagged))
            self._mark_injected(flagged)

    def _judged_text_candidates(self, content: str) -> list[TtsrRule]:
        candidates: list[TtsrRule] = []
        for rule in self.rules:
            if not rule.question:
                continue
            if (not self._can_trigger(rule) or rule.name in self._judged_claimed):
                continue
            if not _scope_allows_text(rule):
                continue
            if rule.globs:
                continue  # no file paths on completed text output
            if rule.conditions and not any(condition.search(content) for condition in rule.conditions):
                continue
            candidates.append(rule)
        return candidates

    # -- repeat state -------------------------------------------------------------------

    def _mark_injected(self, rules: Sequence[TtsrRule]) -> None:
        names = [rule.name for rule in rules]
        self._injected.update(names)
        for name in names:
            self.last_injected_turn[name] = self.completed_turns
        if self.session_db is None or not self.session_id:
            return
        try:
            self.session_db.mark_ttsr_injected(self.session_id, names)
        except Exception:  # health: allow BLE001 -- repeat state is best effort
            logger.debug("ttsr: repeat-state write failed", exc_info=True)

    def unsupported_text(self) -> str:
        if not self.unsupported:
            return ""
        listed = ", ".join(f"{name} ({reason})" for name, reason in self.unsupported)
        return f"TTSR rules not active: {listed}"


# -- gateway install -------------------------------------------------------------------------


def install_coordinator(turn: Any, agent: Any) -> dict[str, Any] | None:
    """Attach this turn's coordinator when the session opted in (ADR-6 D1); None otherwise."""
    from gateway.run_turn_progress import local_viewer_turn
    from gateway.session_policy import active_policy

    policy = active_policy()
    if not local_viewer_turn(turn) or policy is None:
        return None
    if str(getattr(policy, "client", "") or "") != "omp-ui":
        return None
    settings = load_ttsr_settings()
    if not settings.enabled:
        return None
    cwd = str(getattr(policy, "cwd", "") or "")
    agent_name = str(getattr(agent, "_agent_name", "") or "")
    rows = discover_rules(cwd or None, agent_name=agent_name)
    needs_ast = any(
        (row.get("metadata") or {}).get("ast_condition") for row in rows
        if isinstance(row, Mapping))
    ast_grep = resolve_ast_grep() if needs_ast else None
    rules, unsupported = select_rules(
        rows, settings, ast_available=ast_grep is not None)
    session_id = str(getattr(turn._ctx, "session_id", "") or "")
    memory_map = getattr(agent, "_ttsr_memory_claimed", None)
    if not isinstance(memory_map, dict):
        memory_map = {}
        agent._ttsr_memory_claimed = memory_map
    memory_claimed = memory_map.setdefault(session_id, set())
    turn_map = getattr(agent, "_ttsr_repeat_turns", None)
    if not isinstance(turn_map, dict):
        turn_map = {}
        agent._ttsr_repeat_turns = turn_map
    last_map = getattr(agent, "_ttsr_last_injected_turn", None)
    if not isinstance(last_map, dict):
        last_map = {}
        agent._ttsr_last_injected_turn = last_map
    db = getattr(getattr(turn, "_runner", None), "_session_db", None)
    raw_db = getattr(db, "_db", None)
    injected: set[str] = set()
    if raw_db is not None and session_id:
        try:
            injected = raw_db.ttsr_injected_names(session_id)
        except Exception:  # health: allow BLE001 -- cold DB must not block a turn
            logger.debug("ttsr: repeat-state read failed", exc_info=True)
    native_available = False
    if settings.judge == "auto":
        try:
            from agent.judgment import has_native_judge

            native_available = has_native_judge()
        except Exception:  # health: allow BLE001 -- judgment config must not block a turn
            logger.debug("ttsr: native judge probe failed", exc_info=True)
    coordinator = TtsrCoordinator(
        session_id=session_id,
        cwd=cwd,
        settings=settings,
        rules=rules,
        unsupported=unsupported,
        injected_names=injected,
        memory_claimed=memory_claimed,
        ast_grep=ast_grep,
        publish=lambda _event, payload: _publish_triggered(turn, payload),
        rewind_publish=lambda: _publish_rewind(turn),
        completed_turns=int(turn_map.get(session_id, 0) or 0),
        last_injected_turn=last_map.setdefault(session_id, {}),
        native_available=native_available,
        session_db=raw_db,
    )
    previous = getattr(agent, "ttsr_coordinator", None)
    agent.ttsr_coordinator = coordinator
    _publish_unsupported_once(turn, agent, coordinator)
    return {"ttsr_coordinator": previous}


def install_turn_coordinator(turn: Any, agent: Any) -> dict[str, Any] | None:
    """Install only for an omp-ui local-viewer turn; a passive-rule failure never fails the turn."""
    from gateway.run_turn_progress import local_viewer_turn

    if not local_viewer_turn(turn):
        return None
    try:
        return install_coordinator(turn, agent)
    except Exception:  # health: allow BLE001 -- passive rules must never fail a turn
        logger.debug("TTSR coordinator install failed", exc_info=True)
        return None


def restore_coordinator(agent: Any, previous: dict[str, Any] | None) -> None:
    coordinator = getattr(agent, "ttsr_coordinator", None)
    if coordinator is not None:
        coordinator.note_turn_end()
        turn_map = getattr(agent, "_ttsr_repeat_turns", None)
        if isinstance(turn_map, dict) and coordinator.session_id:
            turn_map[coordinator.session_id] = coordinator.completed_turns
    if not previous:
        return
    agent.ttsr_coordinator = previous.get("ttsr_coordinator")


def judge_final_message(
    agent: Any, messages: list[dict[str, Any]], final_msg: Mapping[str, Any],
    final_response: str | None, finish_reason: Any,
) -> None:
    """ADR-11 P3: judge the completed text response once, after its row is appended."""
    coordinator = getattr(agent, "ttsr_coordinator", None)
    if coordinator is None:
        return
    outputs = [("reply", final_response or "")]
    reasoning = final_msg.get("reasoning") or final_msg.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        outputs.append(("reasoning", reasoning))
    coordinator.judge_completed_outputs(messages, outputs, str(finish_reason or ""))


def stash_tool_round_outputs(agent: Any, assistant_msg: Mapping[str, Any]) -> None:
    """ADR-11 P3: remember a tool-call message's reply/reasoning for the post-tool verdict."""
    coordinator = getattr(agent, "ttsr_coordinator", None)
    if coordinator is not None:
        coordinator.stash_tool_round_outputs(assistant_msg)


def _publish_triggered(turn: Any, payload: Mapping[str, Any]) -> Any:
    from gateway import local_viewer_events

    return local_viewer_events.publish_ttsr_triggered(turn, list(payload.get("rules") or ()))


def _publish_rewind(turn: Any) -> Any:
    from gateway import local_viewer_events

    return local_viewer_events.publish_message_rewind(turn)


def _publish_unsupported_once(turn: Any, agent: Any, coordinator: TtsrCoordinator) -> None:
    text = coordinator.unsupported_text()
    if not text:
        return
    key = tuple(name for name, _reason in coordinator.unsupported)
    notified = getattr(agent, "_ttsr_unsupported_notified", None)
    if notified == key:
        return
    agent._ttsr_unsupported_notified = key
    try:
        turn._publish_execution("status.update", {"kind": "ttsr_unsupported", "text": text})
        # The shell ignores unknown status kinds (they are lifecycle chrome), so the same line
        # also goes through the one-shot notice channel that omp-ui renders.
        from uuid import uuid4

        turn._publish_execution("transcript.notice", {
            "id": uuid4().hex, "level": "warning", "text": text, "source": "ttsr"})
    except Exception:  # health: allow BLE001 -- an advisory status line must not fail a turn
        logger.debug("ttsr: unsupported-rule notice failed", exc_info=True)
