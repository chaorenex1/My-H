"""TTSR runtime: rule selection, tool-argument matching, reminders, judged ``question`` rules.

ADR-6 P1 scopes this to local omp-ui viewers: rules are discovered once per turn, matched against
the final arguments of a tool call before it runs, announced with ``ttsr.triggered``, and delivered
after the batch as one hidden ``role:"user"`` reminder. ADR-11 P3 adds ``question`` rules: after a
completed assistant output, every eligible rule's question is judged in one request (``noul``,
threshold 0.7) and a flagged rule delivers a hidden warning once per message.

Nothing here touches the system prompt, tools, transcript rows, or the turn loop when the gateway
did not install a coordinator (CLI, Desktop, messaging, ACP, subagents).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from agent.rule_utils import discover_rules
from agent.ttsr_prompts import render_judged_warning, render_tool_reminder

logger = logging.getLogger(__name__)

#: A single file digest above this size is skipped, never truncated (ADR-6 D4).
MAX_DIGEST_BYTES = 1_048_576
#: Jev's ~33k branch limit leaves room for the request template; ~2 chars/token on dense code.
JUDGED_CONTENT_MAX_CHARS = 56_000
JUDGED_RULE_THRESHOLD = 0.7
JUDGE_MODES = ("auto", "on", "off")

#: omp rule scopes name omp's tools; Hermes registers different names (ADR-6 D4).
TOOL_ALIASES = {"edit": "patch", "write": "write_file", "read": "read_file"}

_TOKEN_RE = re.compile(
    r"^(?:(tool)(?::([a-z0-9_-]+))?|([a-z0-9_-]+))(?:\(([^)]+)\))?$", re.IGNORECASE)


@dataclass(frozen=True)
class TtsrSettings:
    enabled: bool = True
    builtin_rules: bool = True
    disabled_rules: tuple[str, ...] = ()
    judge: str = "auto"
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
    for problem in problems:
        logger.warning("ttsr config: %s", problem)
    return TtsrSettings(
        enabled=_as_bool(block.get("enabled"), True, "ttsr.enabled must be a bool", problems),
        builtin_rules=_as_bool(block.get("builtin_rules"), True,
                               "ttsr.builtin_rules must be a bool", problems),
        disabled_rules=disabled,
        judge=judge,
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


def select_rules(
    rules: Sequence[Mapping[str, Any]], settings: TtsrSettings,
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
        interrupt_mode = str(meta.get("interrupt_mode") or "").strip().lower()
        raw_meta = meta.get("raw") if isinstance(meta.get("raw"), Mapping) else {}
        repeat_mode = str(raw_meta.get("repeatMode") or raw_meta.get("repeat_mode") or "").strip().lower()
        if repeat_mode and repeat_mode != "once":
            logger.warning("ttsr: rule %s uses repeatMode %s; this build implements once only",
                           name, repeat_mode)
        conditions_raw = tuple(meta.get("condition") or ())
        ast_conditions = tuple(meta.get("ast_condition") or ())
        question = str(meta.get("question") or "").strip()
        if interrupt_mode and interrupt_mode != "never":
            unsupported.append((name, f"interruptMode: {interrupt_mode}"))
            continue
        if not conditions_raw and not ast_conditions and not question:
            continue
        if ast_conditions and not conditions_raw and not question:
            unsupported.append((name, "astCondition"))
            continue
        compiled = _compile_conditions(conditions_raw)
        if compiled is None:
            continue
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


def rule_payload(rule: TtsrRule) -> dict[str, str]:
    return {"name": rule.name, "description": rule.description, "content": rule.content}


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
    publish: Callable[[str, dict[str, Any]], Any] | None = None
    judge_factory: Callable[..., Any] | None = None
    native_available: bool = False
    session_db: Any = None
    _stash: dict[str, list[TtsrRule]] = field(default_factory=dict)
    _judged_stash: dict[str, list[tuple[str, str, list[TtsrRule]]]] = field(default_factory=dict)
    _pending_text_outputs: list[tuple[str, str]] = field(default_factory=list)
    _batch_claimed: set[str] = field(default_factory=set)
    _injected: set[str] = field(default_factory=set)
    _judged_claimed: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._injected = set(self.injected_names)

    # -- tool matching ------------------------------------------------------------------

    def match_tool_start(
        self, call_id: str, tool_name: str, args: Mapping[str, Any] | None,
    ) -> list[dict[str, str]]:
        if not self.settings.enabled or not self.rules:
            return []
        name = _canonical_tool(tool_name)
        digests = tool_digests(name, args)
        if not digests:
            return []
        paths = [path for path, _text in digests]
        usable: list[tuple[str, str]] = []
        for path, text in digests:
            if len(text.encode("utf-8", "ignore")) > MAX_DIGEST_BYTES:
                logger.debug("ttsr: digest too large, skipped: %s", path)
                continue
            usable.append((path, text))
        self._judged_stash.pop(str(call_id), None)
        judged: list[tuple[str, str, list[TtsrRule]]] = []
        if self.judge_enabled():
            for path, text in usable:
                candidates = [
                    rule for rule in self.rules
                    if rule.question
                    and rule.name not in self._injected
                    and _scope_allows_tool(rule, name, (path,))
                    and (not rule.globs or _paths_match(rule.globs, (path,)))
                    and (not rule.conditions
                         or any(condition.search(text) for condition in rule.conditions))
                ]
                if candidates:
                    judged.append((f"`{name}` call on `{path}`", text, candidates))
        if judged:
            self._judged_stash[str(call_id)] = judged
        matched: list[TtsrRule] = []
        for rule in self.rules:
            if rule.question or not rule.conditions:
                continue
            if rule.name in self._injected or rule.name in self._batch_claimed:
                continue
            if not _scope_allows_tool(rule, name, paths):
                continue
            if rule.globs and not _paths_match(rule.globs, paths):
                continue
            for _path, text in usable:
                if any(condition.search(text) for condition in rule.conditions):
                    matched.append(rule)
                    break
        for rule in matched:
            self._batch_claimed.add(rule.name)
            self._stash.setdefault(str(call_id), []).append(rule)
        return [rule_payload(rule) for rule in matched]

    def after_tool_batch(
        self, agent: Any, messages: list[dict[str, Any]], num_tools: int,
        *, result_ids: set[str] | None = None,
    ) -> None:
        stash = self._stash
        judged_stash = self._judged_stash
        pending = self._pending_text_outputs
        self._stash, self._judged_stash, self._batch_claimed = {}, {}, set()
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
        for call_id, rules in stash.items():
            if call_id not in result_ids:
                continue
            delivered.extend(rule for rule in rules if rule.name not in self._injected)
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

            append_message(messages, {"role": "user", "content": text, "display_kind": "hidden"})
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
            candidates = [rule for rule in candidates if rule.name not in self._injected]
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

            append_message(messages, {"role": "user", "content": warning, "display_kind": "hidden"})
            self._mark_injected(flagged)

    def _judged_text_candidates(self, content: str) -> list[TtsrRule]:
        candidates: list[TtsrRule] = []
        for rule in self.rules:
            if not rule.question:
                continue
            if rule.name in self._injected or rule.name in self._judged_claimed:
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
    rules, unsupported = select_rules(discover_rules(cwd or None, agent_name=agent_name), settings)
    session_id = str(getattr(turn._ctx, "session_id", "") or "")
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
        publish=lambda _event, payload: _publish_triggered(turn, payload),
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
