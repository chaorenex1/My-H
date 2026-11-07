"""ADR-6 P1 / ADR-11 P3: TTSR rule selection, matching, reminders and judged rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent import ttsr_runtime
from agent.rule_utils import parse_rule_text
from agent.ttsr_runtime import (
    JUDGED_CONTENT_MAX_CHARS,
    JUDGED_CONTENT_MAX_TOKENS,
    TtsrCoordinator,
    TtsrSettings,
    glob_matches,
    load_ttsr_settings,
    parse_scope,
    select_rules,
    split_scope_tokens,
    tool_digests,
)
from agent.judgment import JudgmentResult, NoulAnswer
from agent.usage_pricing import CanonicalUsage


def _row(text: str, *, rule_id: str = "rule", source: str = "/project/rules/rule.md") -> dict[str, Any]:
    return parse_rule_text(text, source=source, rule_id=rule_id)


def _rule(text: str, *, rule_id: str = "rule", source: str = "/project/rules/rule.md") -> Any:
    if "interruptMode" not in text and text.startswith("---"):
        _head, frontmatter, body = text.split("---", 2)
        text = f"---{frontmatter}interruptMode: never\n---{body}"
    rows, unsupported = select_rules([_row(text, rule_id=rule_id, source=source)], TtsrSettings())
    assert not unsupported
    assert len(rows) == 1
    return rows[0]


def _coordinator(
    rules: list[Any], *, settings: TtsrSettings | None = None, judge_factory: Any = None,
    native_available: bool = False, injected: set[str] | None = None,
    session_db: Any = None, publish: Any = None, memory_claimed: set[str] | None = None,
    ast_grep: str | None = None, ast_timeout: float = 2.0,
) -> TtsrCoordinator:
    return TtsrCoordinator(
        session_id="s1",
        cwd="/project",
        settings=settings or TtsrSettings(),
        rules=rules,
        injected_names=injected or set(),
        memory_claimed=memory_claimed if memory_claimed is not None else set(),
        ast_grep=ast_grep,
        ast_timeout=ast_timeout,
        judge_factory=judge_factory,
        native_available=native_available,
        session_db=session_db,
        publish=publish,
    )


class _FakeAgent:
    def __init__(self, interrupted: bool = False) -> None:
        self.is_interrupted = interrupted


class _FakeDB:
    def __init__(self) -> None:
        self.rows: dict[str, set[str]] = {}

    def ttsr_injected_names(self, session_id: str) -> set[str]:
        return set(self.rows.get(session_id, set()))

    def mark_ttsr_injected(self, session_id: str, names: list[str]) -> int:
        self.rows.setdefault(session_id, set()).update(names)
        return len(names)


# -- config / scope / globs ------------------------------------------------------------------


def test_load_ttsr_settings_defaults_and_validation():
    settings = load_ttsr_settings({"ttsr": {"enabled": False, "judge": "bogus",
                                            "disabled_rules": "ts-no-any"}})
    assert settings.enabled is False
    assert settings.judge == "auto"
    assert settings.disabled_rules == ("ts-no-any",)
    assert settings.problems


def test_scope_tokens_survive_braces_quotes_and_commas():
    assert split_scope_tokens('text, tool:edit({ts,tsx}), "tool:write(*.md)"') == [
        "text", "tool:edit({ts,tsx})", '"tool:write(*.md)"']
    assert parse_scope(("toolcall",)).allow_any_tool
    scope = parse_scope(("text", "tool:edit({ts,tsx})", "tool"))
    assert scope.allow_text and scope.allow_any_tool
    assert scope.tool_scopes[0].tool_name == "patch"
    assert scope.tool_scopes[0].path_pattern == "{ts,tsx}"


def test_glob_semantics_do_not_cross_segments_but_try_the_basename():
    assert glob_matches("src/*.ts", "src/a.ts")
    assert not glob_matches("src/*.ts", "src/nested/a.ts")
    assert glob_matches("**/*.ts", "src/nested/a.ts")
    assert glob_matches("*.ts", "src/nested/a.ts")
    assert glob_matches("*.{ts,tsx}", "a.tsx")
    assert not glob_matches("*.ts", "a.md")


# -- digests / matching ----------------------------------------------------------------------


def test_write_and_patch_digests_use_source_text_not_json():
    assert tool_digests("write_file", {"path": "a.ts", "content": "const x: any = 1"}) == [
        ("a.ts", "const x: any = 1")]
    assert tool_digests("patch", {"path": "a.ts", "old_string": "bad: any", "new_string": "ok"}) == [
        ("a.ts", "ok")]
    assert tool_digests("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** End Patch\n"}) == []


def test_v4a_scope_gates_each_file_not_the_whole_batch():
    rule = _rule("---\ncondition: 'any'\nscope: 'tool:edit(*.ts)'\n"
                 "interruptMode: never\n---\nbody\n")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.ts\n@@\n+const x = 1\n"
        "*** Update File: b.md\n@@\n+any\n"
        "*** End Patch\n"
    )
    coordinator = _coordinator([rule])
    assert coordinator.match_tool_start(
        "c1", "patch", {"mode": "patch", "patch": patch}) == []


def test_v4a_digest_keeps_added_lines_per_file_and_skips_deletes():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.ts\n@@\n-old: any\n+new: unknown\n"
        "*** Add File: b.ts\n+const y: any = 2\n"
        "*** Delete File: c.ts\n"
        "*** End Patch\n"
    )
    digests = tool_digests("patch", {"mode": "patch", "patch": patch})
    assert digests == [("a.ts", "new: unknown"), ("b.ts", "const y: any = 2")]


def test_alias_scope_and_regex_match_new_string_only():
    rule = _rule(
        "---\n"
        "description: no any\n"
        'condition: ": any"\n'
        'scope: "tool:edit(*.ts), tool:write(*.ts)"\n'
        "interruptMode: never\n"
        "---\n"
        "Never use any.\n"
    )
    coordinator = _coordinator([rule])
    assert coordinator.match_tool_start("c1", "patch", {
        "path": "a.ts", "old_string": "x: any", "new_string": "x: unknown"}) == []
    matched = coordinator.match_tool_start("c2", "patch", {
        "path": "a.ts", "old_string": "x", "new_string": "y: any"})
    assert [payload["name"] for payload in matched] == ["rule"]
    assert coordinator.match_tool_start("c3", "write_file", {"path": "a.md", "content": "x: any"}) == []


def test_one_rule_matches_once_per_batch_and_not_after_injection():
    rule = _rule("---\ncondition: 'any'\n---\nbody\n")
    coordinator = _coordinator([rule])
    assert coordinator.match_tool_start("c1", "write_file", {"path": "a.ts", "content": "any"})
    assert coordinator.match_tool_start("c2", "write_file", {"path": "b.ts", "content": "any"}) == []
    coordinator.after_tool_batch(_FakeAgent(), [
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ], 1)
    assert coordinator.match_tool_start("c3", "write_file", {"path": "c.ts", "content": "any"}) == []


def test_reminder_is_one_hidden_row_after_tool_results_even_on_tool_error():
    rule = _rule("---\ndescription: d\ncondition: 'any'\n---\nUse unknown.\n")
    db = _FakeDB()
    coordinator = _coordinator([rule], session_db=db)
    coordinator.match_tool_start("c1", "write_file", {"path": "a.ts", "content": "any"})
    messages = [{"role": "assistant", "content": "call"},
                {"role": "tool", "tool_call_id": "c1", "content": "Error: patch failed"}]
    coordinator.after_tool_batch(_FakeAgent(), messages, 1)
    assert len(messages) == 3
    row = messages[-1]
    assert row["role"] == "custom" and row["custom_type"] == "ttsr-injection"
    assert row["display"] is False and row["details"]["rules"] == ["rule"]
    assert "<system-reminder" in row["content"] and "Use unknown." in row["content"]
    assert db.rows["s1"] == {"rule"}


def test_interrupted_batch_delivers_nothing_and_writes_no_repeat_state():
    rule = _rule("---\ncondition: 'any'\n---\nbody\n")
    db = _FakeDB()
    coordinator = _coordinator([rule], session_db=db)
    coordinator.match_tool_start("c1", "write_file", {"path": "a.ts", "content": "any"})
    messages = [{"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    coordinator.after_tool_batch(_FakeAgent(interrupted=True), messages, 1)
    assert len(messages) == 1
    assert db.rows == {}


def test_undelivered_match_stays_claimed_for_the_rest_of_the_turn():
    rule = _rule("---\ncondition: 'any'\n---\nbody\n")
    db = _FakeDB()
    coordinator = _coordinator([rule], session_db=db)
    assert coordinator.match_tool_start("c1", "write_file", {"path": "a.ts", "content": "any"})
    coordinator.after_tool_batch(_FakeAgent(interrupted=True), [
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ], 1)
    # In-memory once suppression without a durable mark: the next batch cannot re-notify.
    assert coordinator.match_tool_start("c2", "write_file", {"path": "b.ts", "content": "any"}) == []
    assert db.rows == {}


def test_regex_rule_without_interrupt_mode_takes_the_global_default():
    rows = [_row("---\ncondition: 'x'\n---\nbody\n", rule_id="r")]
    active, unsupported = select_rules(rows, TtsrSettings())
    assert not unsupported
    assert [rule.interrupt_mode for rule in active] == ["always"]

    active, unsupported = select_rules(rows, TtsrSettings(interrupt_mode="never"))
    assert not unsupported
    assert [rule.interrupt_mode for rule in active] == ["never"]

    bad = [_row("---\ncondition: 'x'\ninterruptMode: sometimes\n---\nbody\n", rule_id="bad")]
    active, unsupported = select_rules(bad, TtsrSettings())
    assert active == []
    assert unsupported == [("bad", "interruptMode: sometimes")]


def test_undelivered_match_is_remembered_across_turns_without_a_durable_row():
    rule = _rule("---\ncondition: 'any'\n---\nbody\n")
    db = _FakeDB()
    memory: set[str] = set()
    first = _coordinator([rule], session_db=db, memory_claimed=memory)
    assert first.match_tool_start("c1", "write_file", {"path": "a.ts", "content": "any"})
    first.after_tool_batch(_FakeAgent(interrupted=True), [
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ], 1)
    assert db.rows == {}

    second = _coordinator([rule], session_db=db, memory_claimed=memory)
    assert second.match_tool_start("c2", "write_file", {"path": "b.ts", "content": "any"}) == []


def test_unsupported_rules_are_reported_not_silently_dropped():
    rows = [
        _row("---\ncondition: 'x'\ninterruptMode: always\n---\nbody\n", rule_id="a"),
        _row("---\nastCondition: 'x'\ninterruptMode: never\n---\nbody\n", rule_id="b"),
        _row("---\nquestion: 'Is it bad?'\n---\nbody\n", rule_id="c"),
    ]
    active, unsupported = select_rules(rows, TtsrSettings())
    assert [rule.name for rule in active] == ["a", "c"]
    assert [name for name, _reason in unsupported] == ["b"]
    assert "ast-grep CLI unavailable" in unsupported[0][1]


def test_ast_only_rule_is_unsupported_without_the_cli():
    row = _row("---\nastCondition: 'const $A: any = $B'\ninterruptMode: never\n---\nbody\n",
               rule_id="ast")
    active, unsupported = select_rules([row], TtsrSettings())
    assert active == []
    assert unsupported == [("ast", "ast-grep CLI unavailable (hermes pm install ast-grep)")]


def test_ast_only_rule_matches_when_the_cli_is_available(monkeypatch):
    row = _row("---\nname: ast\nastCondition: 'const $A: any = $B'\ninterruptMode: never\n"
               "scope: 'tool:write(*.ts)'\n---\nbody\n", rule_id="ast")
    active, unsupported = select_rules([row], TtsrSettings(), ast_available=True)
    assert not unsupported
    assert [rule.name for rule in active] == ["ast"]

    seen: list[tuple[str, str, str, str, float]] = []

    def _fake_match(executable, pattern, source, lang, *, timeout):
        seen.append((executable, pattern, source, lang, timeout))
        return True

    monkeypatch.setattr(ttsr_runtime, "ast_match", _fake_match)
    coordinator = _coordinator(active, ast_grep="/fake/ast-grep")
    assert coordinator.match_tool_start(
        "c1", "write_file", {"path": "a.ts", "content": "const x: any = 1"})
    assert seen and seen[0][0] == "/fake/ast-grep"
    assert seen[0][1] == "const $A: any = $B" and seen[0][3] == "ts"


@pytest.mark.parametrize("path", ["Makefile", "notes.md", "notes.txt"])
def test_ast_rule_skips_a_path_without_a_language(monkeypatch, path):
    row = _row("---\nname: ast\nastCondition: 'const $A: any = $B'\ninterruptMode: never\n"
               "scope: 'tool:write'\n---\nbody\n", rule_id="ast")
    active, _ = select_rules([row], TtsrSettings(), ast_available=True)
    monkeypatch.setattr(ttsr_runtime, "ast_match",
                        lambda *_a, **_kw: pytest.fail("ast-grep must not run without a language"))
    coordinator = _coordinator(active, ast_grep="/fake/ast-grep")
    assert coordinator.match_tool_start(
        "c1", "write_file", {"path": path, "content": "any"}) == []


def test_mixed_regex_and_ast_rule_survives_a_missing_ast_backend():
    row = _row("---\nname: both\ncondition: 'any'\nastCondition: 'const $A = $B'\n"
               "interruptMode: never\n---\nbody\n", rule_id="both")
    active, unsupported = select_rules([row], TtsrSettings())
    assert not unsupported
    assert [rule.name for rule in active] == ["both"]
    coordinator = _coordinator(active, ast_grep=None)
    assert coordinator.match_tool_start(
        "c1", "write_file", {"path": "a.ts", "content": "any"}) == [{
            "name": "both", "description": "", "content": "body"}]


def test_regex_hit_short_circuits_the_ast_bridge(monkeypatch):
    row = _row("---\nname: both\ncondition: 'any'\nastCondition: 'const $A = $B'\n"
               "interruptMode: never\n---\nbody\n", rule_id="both")
    active, _ = select_rules([row], TtsrSettings(), ast_available=True)
    monkeypatch.setattr(ttsr_runtime, "ast_match",
                        lambda *_a, **_kw: pytest.fail("regex hit should not spawn ast-grep"))
    coordinator = _coordinator(active, ast_grep="/fake/ast-grep")
    assert coordinator.match_tool_start(
        "c1", "write_file", {"path": "a.ts", "content": "any"}) == [{
            "name": "both", "description": "", "content": "body"}]


def test_builtin_switch_and_disabled_names_apply(tmp_path):
    shipped = _row("---\ncondition: 'x'\ninterruptMode: never\n---\nbody\n", rule_id="shipped",
                   source=str(Path(__file__).resolve().parents[2] / "rules" / "ts-no-any.md"))
    project = _row("---\ncondition: 'x'\ninterruptMode: never\n---\nbody\n", rule_id="project",
                   source=str(tmp_path / "rules" / "p.md"))
    active, _ = select_rules([shipped, project], TtsrSettings(builtin_rules=False))
    assert [rule.name for rule in active] == ["project"]
    active, _ = select_rules([shipped, project], TtsrSettings(disabled_rules=("project",)))
    assert [rule.name for rule in active] == ["shipped"]


# -- judged rules ----------------------------------------------------------------------------


class _FakeJudge:
    def __init__(self, probabilities: dict[str, float] | list[dict[str, float]]) -> None:
        self._queue = list(probabilities) if isinstance(probabilities, list) else [probabilities]
        self.requests: list[Any] = []

    def judge(self, request: Any, **_kwargs: Any) -> JudgmentResult:
        self.requests.append(request)
        probabilities = self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]
        return JudgmentResult(
            kind="online",
            api="text", provider="p", model="m",
            answers={qid: NoulAnswer(prob) for qid, prob in probabilities.items()},
            usage=CanonicalUsage(input_tokens=1, output_tokens=1),
        )


def _question_row(rule_id: str, question: str = "Does it violate?") -> dict[str, Any]:
    return _row(f"---\nname: {rule_id}\nquestion: '{question}'\n---\nRule body {rule_id}\n",
                rule_id=rule_id)


def test_judged_rule_threshold_claim_and_warning():
    rows = [_question_row("a"), _question_row("b")]
    active, _ = select_rules(rows, TtsrSettings())
    judge = _FakeJudge([{"q0": 0.9, "q1": 0.4}, {"q0": 0.4}])
    published: list[tuple[str, dict[str, Any]]] = []
    coordinator = _coordinator(
        active, settings=TtsrSettings(judge="on"), judge_factory=lambda **_kw: judge,
        publish=lambda event, payload: published.append((event, payload)))
    messages = [{"role": "assistant", "content": "reply"}]
    coordinator.judge_completed_outputs(
        messages, [("reply", "bad reply"), ("reasoning", "bad reasoning")], "stop")

    # One request per output; rule ``a`` is claimed by the first verdict and absent from the second.
    assert len(judge.requests) == 2
    request = judge.requests[0]
    assert request.state == {"output": "reply", "content": "bad reply"}
    assert set(request.questions) == {"q0", "q1"}
    assert set(judge.requests[1].questions) == {"q0"}
    assert published == [("ttsr.triggered", {"rules": [{
        "name": "a", "description": "", "content": "Rule body a"}]})]
    assert messages[-1]["custom_type"] == "ttsr-injection" and messages[-1]["display"] is False
    assert "Rule judge flagged your reply" in messages[-1]["content"]


def test_judge_auto_requires_native_and_off_never_runs():
    rows = [_question_row("a")]
    active, _ = select_rules(rows, TtsrSettings(judge="on"))
    judge = _FakeJudge({"q0": 1.0})
    coordinator = _coordinator(active, settings=TtsrSettings(judge="auto"),
                               judge_factory=lambda **_kw: judge, native_available=False)
    coordinator.judge_completed_outputs([], [("reply", "x")], "stop")
    assert judge.requests == []

    coordinator = _coordinator(active, settings=TtsrSettings(judge="off"),
                               judge_factory=lambda **_kw: judge)
    coordinator.judge_completed_outputs([], [("reply", "x")], "stop")
    assert judge.requests == []


def test_judge_failure_is_silent_and_truncates_content():
    rows = [_question_row("a")]
    active, _ = select_rules(rows, TtsrSettings(judge="on"))

    class _Broken:
        def judge(self, request: Any, **_kwargs: Any) -> JudgmentResult:
            raise RuntimeError("provider down")

    messages: list[dict[str, Any]] = []
    coordinator = _coordinator(active, settings=TtsrSettings(judge="on"),
                               judge_factory=lambda **_kw: _Broken())
    coordinator.judge_completed_outputs(messages, [("reply", "x" * (JUDGED_CONTENT_MAX_CHARS + 10))], "stop")
    assert messages == []

    judge = _FakeJudge({"q0": 0.0})
    coordinator = _coordinator(active, settings=TtsrSettings(judge="on"),
                               judge_factory=lambda **_kw: judge)
    coordinator.judge_completed_outputs([], [("reply", "y" * (JUDGED_CONTENT_MAX_CHARS + 10))], "stop")
    assert len(judge.requests[0].state["content"]) == JUDGED_CONTENT_MAX_CHARS


def test_judged_content_budget_matches_the_omp_token_cap():
    """The 32k-Jev budget is enforced as the ADR-sanctioned 24k-char stand-in."""
    assert JUDGED_CONTENT_MAX_TOKENS == 32_000
    assert JUDGED_CONTENT_MAX_CHARS == 24_000


def test_judge_skips_aborted_and_error_finish_reasons():
    rows = [_question_row("a")]
    active, _ = select_rules(rows, TtsrSettings(judge="on"))
    judge = _FakeJudge({"q0": 1.0})
    coordinator = _coordinator(active, settings=TtsrSettings(judge="on"),
                               judge_factory=lambda **_kw: judge)
    for reason in ("aborted", "error"):
        coordinator.judge_completed_outputs([], [("reply", "x")], reason)
    assert judge.requests == []


def test_question_rule_can_judge_a_tool_digest():
    row = _row("---\nname: tool-q\nquestion: 'Did the write break the rule?'\n"
               "scope: 'tool:write(*.ts)'\n---\nTool rule body\n", rule_id="tool-q")
    active, _ = select_rules([row], TtsrSettings(judge="on"))
    judge = _FakeJudge({"q0": 0.95})
    published: list[tuple[str, dict[str, Any]]] = []
    coordinator = _coordinator(active, settings=TtsrSettings(judge="on"),
                               judge_factory=lambda **_kw: judge,
                               publish=lambda event, payload: published.append((event, payload)))
    assert coordinator.match_tool_start("c1", "write_file", {"path": "a.ts", "content": "x"}) == []
    messages = [{"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    coordinator.after_tool_batch(_FakeAgent(), messages, 1)

    assert judge.requests[0].state["output"] == "`write_file` call on `a.ts`"
    assert judge.requests[0].questions["q0"].instructions == "Did the write break the rule?"
    assert published[0][1]["rules"][0]["name"] == "tool-q"
    assert messages[-1]["custom_type"] == "ttsr-injection" and messages[-1]["display"] is False
    assert "`write_file` call on `a.ts`" in messages[-1]["content"]


def test_brace_scope_survives_the_metadata_splitter():
    row = _row("---\ncondition: 'any'\ninterruptMode: never\n"
               "scope: 'tool:edit(*.{ts,tsx})'\n---\nbody\n")
    active, _ = select_rules([row], TtsrSettings())
    assert active[0].scope.tool_scopes[0].path_pattern == "*.{ts,tsx}"
    coordinator = _coordinator(active)
    assert coordinator.match_tool_start("c1", "patch", {"path": "a.tsx", "new_string": "any"})
    assert coordinator.match_tool_start("c2", "patch", {"path": "a.md", "new_string": "any"}) == []


def test_question_rule_judges_tool_round_text_after_results():
    rows = [_question_row("q")]
    active, _ = select_rules(rows, TtsrSettings(judge="on"))
    judge = _FakeJudge([{"q0": 0.1}, {"q0": 0.9}])
    coordinator = _coordinator(active, settings=TtsrSettings(judge="on"),
                               judge_factory=lambda **_kw: judge)
    coordinator.stash_tool_round_outputs({"content": "narration", "reasoning": "thought"})
    messages = [{"role": "assistant", "content": "narration"},
                {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    coordinator.after_tool_batch(_FakeAgent(), messages, 1)

    assert [r.state["output"] for r in judge.requests] == ["reply", "reasoning"]
    # The warning is appended after the tool result, never between the call and its result.
    assert messages[1]["role"] == "tool"
    assert messages[-1]["custom_type"] == "ttsr-injection" and messages[-1]["display"] is False
    assert "flagged your reasoning" in messages[-1]["content"]
