"""``judgrg`` engine: decision table, query derivation, intent planning, degradation."""

from __future__ import annotations

import threading

import pytest

from agent.judgment import find
from agent.judgment.decision import load_decision, parse_decision
from agent.judgment.types import (
    ChoiceAnswer,
    JudgmentError,
    JudgmentResult,
    JudgmentUnavailable,
)


class ChoiceJudge:
    label = "typesafe"
    supports_images = False
    api = "typesafe"

    def __init__(self, choice: str = "literal") -> None:
        self.choice = choice

    def judge(self, request, *, cancel_event: threading.Event | None = None,
              purpose: str = "") -> JudgmentResult:
        labels = list(request.questions["intent"].criteria)
        return JudgmentResult(
            kind="native",
            api=self.api, provider="typesafe", model="m",
            answers={"intent": ChoiceAnswer(self.choice, {label: 0.25 for label in labels}, 0.5)})


class DownJudge(ChoiceJudge):
    def judge(self, request, *, cancel_event: threading.Event | None = None,
              purpose: str = "") -> JudgmentResult:
        raise JudgmentUnavailable("down")


def test_decision_table_ships_with_every_intent_and_plan():
    table = load_decision()
    assert list(table.criteria) == [
        "literal", "symbol", "call", "type", "config", "todo", "pattern",
        "unknown_implementation", "cross_language"]
    assert set(table.plans) == set(table.criteria)
    assert table.plan_for("call").tool == "ast-grep"
    assert table.plan_for("symbol").mode == "hybrid"
    assert table.plan_for("not-a-label").label == "unknown_implementation"


def test_decision_table_rejects_invalid_documents():
    with pytest.raises(JudgmentError, match="mode"):
        parse_decision({
            "intent": {"instructions": "x", "criteria": {"a": "A"}},
            "plans": {"a": {"tool": "rg", "mode": "nope"},
                      "unknown_implementation": {"tool": "rg", "mode": "content"}},
        })
    with pytest.raises(JudgmentError, match="required"):
        parse_decision({
            "intent": {"instructions": "x", "criteria": {"a": "A"}},
            "plans": {"a": {"tool": "rg", "mode": "content"}},
        })
    with pytest.raises(JudgmentError, match="criterion"):
        parse_decision({
            "intent": {"instructions": "x", "criteria": {"a": "A"}},
            "plans": {"b": {"tool": "rg", "mode": "content"},
                      "unknown_implementation": {"tool": "rg", "mode": "content"}},
        })
    with pytest.raises(JudgmentError, match="without a plan"):
        parse_decision({
            "intent": {"instructions": "x",
                       "criteria": {"a": "A", "unknown_implementation": "U", "b": "B"}},
            "plans": {"a": {"tool": "rg", "mode": "content"},
                      "unknown_implementation": {"tool": "rg", "mode": "content"}},
        })


def test_search_terms_keeps_phrases_and_drops_stopwords():
    terms = find.search_terms('find "retry budget" handling for the HTTP client')
    assert terms[0] == "retry budget"
    assert "handling" in terms and "HTTP" in terms and "client" in terms
    assert "for" not in terms and "the" not in terms


def test_path_glob_builds_a_brace_pattern_and_strips_glob_specials():
    assert find.path_glob("login module", ["login", "module"]) == "**/*{login,module}*"
    assert find.path_glob("*.ts", []) == "*.ts"
    assert find.path_glob("noop", []) is None
    assert find.path_glob("compare a[b] values", ["a[b]"]) == "**/*ab*"


def test_content_pattern_is_case_insensitive_literal_alternation():
    assert find.content_pattern(["a.b", "cfg"]) == "a\\.b|cfg"
    assert find.content_pattern([]) is None


def test_looks_structural_only_accepts_code_shaped_queries():
    assert find.looks_structural("print($X)")
    assert find.looks_structural("Path.cwd()")
    assert not find.looks_structural("where users are authenticated")
    assert not find.looks_structural("")


def test_plan_routes_the_judged_intent_through_the_table(tmp_path, monkeypatch):
    monkeypatch.setattr(find, "resolve_ast_grep", lambda: "/bin/ast-grep")
    assert find.plan_search("literal", "authenticate a password", tmp_path, None).mode == "content"
    assert find.plan_search("symbol", "print($X)", tmp_path, None).mode == "hybrid"
    assert find.plan_search("call", "print($X)", tmp_path, None).mode == "ast"
    assert find.plan_search("pattern", "print($X)", tmp_path, None).mode == "ast"


def test_plan_synthesizes_patterns_for_named_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr(find, "resolve_ast_grep", lambda: "/bin/ast-grep")
    symbol = find.plan_search("symbol", "authenticate", tmp_path, None)
    assert (symbol.mode, symbol.ast_pattern) == ("hybrid", "authenticate")

    call = find.plan_search("call", "authenticate", tmp_path, None)
    assert (call.mode, call.expression) == ("ast", "authenticate($$$)")


def test_plan_prefers_a_raw_glob_over_content(tmp_path):
    assert find.plan_search("literal", "*.py", tmp_path, None).mode == "path"
    assert find.plan_search("literal", "src/**/*.ts", tmp_path, None).mode == "path"


def test_plan_degrades_ast_prose_to_content(tmp_path, monkeypatch):
    monkeypatch.setattr(find, "resolve_ast_grep", lambda: "/bin/ast-grep")
    plan = find.plan_search("call", "where users are authenticated", tmp_path, None)
    assert plan.mode == "content"
    assert "not a code pattern" in plan.fallback
    assert plan.expression


def test_plan_degrades_ast_without_ast_grep(tmp_path, monkeypatch):
    monkeypatch.setattr(find, "resolve_ast_grep", lambda: None)
    plan = find.plan_search("call", "print($X)", tmp_path, None)
    assert plan.mode == "content"
    assert "ast-grep is not installed" in plan.fallback


def test_plan_falls_back_to_files_when_nothing_searchable_remains(tmp_path):
    plan = find.plan_search("literal", "the of", tmp_path, None)
    assert plan.mode == "files"
    assert plan.fallback


def test_merge_ranges_joins_adjacent_windows():
    merged = find._merge_ranges([
        {"start": 5, "end": 6, "snippet": "b"},
        {"start": 1, "end": 2, "snippet": "a"},
        {"start": 10, "end": 11, "snippet": "c"},
    ])
    assert merged == [
        {"start": 1, "end": 2, "snippet": "a"},
        {"start": 5, "end": 6, "snippet": "b"},
        {"start": 10, "end": 11, "snippet": "c"},
    ]


def test_merge_hit_sets_unions_rg_and_ast_per_path():
    merged = find._merge_hit_sets(
        [{"path": "a.py", "ranges": [{"start": 1, "end": 1, "snippet": "x"}]}],
        [{"path": "a.py", "ranges": [{"start": 3, "end": 3, "snippet": "y"}]},
         {"path": "b.py", "ranges": [{"start": 2, "end": 2, "snippet": "z"}]}],
    )
    assert [hit["path"] for hit in merged] == ["a.py", "b.py"]
    assert [item["start"] for item in merged[0]["ranges"]] == [1, 3]


def test_choose_intent_uses_the_choice_and_reports_failure():
    stats = find._RunStats()
    assert find.choose_intent(ChoiceJudge("type"), "class Foo", stats) == "type"
    assert stats.requests == 1

    stats = find._RunStats()
    assert find.choose_intent(DownJudge(), "class Foo", stats) is None
    assert stats.snapshot() == {"requests": 1, "failed": 1}


def test_online_or_local_answers_mark_the_run_uncalibrated():
    class TextJudge(ChoiceJudge):
        def judge(self, request, *, cancel_event=None, purpose=""):
            result = super().judge(request)
            return JudgmentResult(
                kind="online", api="chat_completions", provider=result.provider,
                model=result.model, answers=result.answers)

    stats = find._RunStats()
    assert find.choose_intent(TextJudge(), "class Foo", stats) == "literal"
    assert stats.uncalibrated is True

    stats = find._RunStats()
    assert find.choose_intent(ChoiceJudge(), "class Foo", stats) == "literal"
    assert stats.uncalibrated is False


def test_intent_request_offers_the_json_criteria():
    request = find._intent_request("login")
    question = request.questions["intent"]
    assert isinstance(question, find.ChoiceQuestion)
    assert list(question.criteria) == list(load_decision().criteria)
    assert request.state == {"task": find.TASK, "query": "login"}


def test_judgrg_unavailable_when_the_judge_cannot_answer(monkeypatch, tmp_path):
    payload = find.judgrg("login", judge=DownJudge(), path=str(tmp_path))
    assert payload["coverage"] == "unavailable"
    assert payload["intent"] is None
    assert payload["tools"] == []


def test_judgrg_rejects_a_missing_scope(tmp_path):
    with pytest.raises(ValueError, match="path not found"):
        find.judgrg("login", judge=ChoiceJudge(), path=str(tmp_path / "missing"))


def test_parse_expanded_terms_accepts_json_and_lines_and_caps_five():
    assert find._parse_expanded_terms('["retry", "backoff", "3", ""]') == [
        "retry", "backoff", "3"]
    assert find._parse_expanded_terms("- retry\n- backoff\n") == ["retry", "backoff"]
    assert len(find._parse_expanded_terms('["a","b","c","d","e","f"]')) == 5


def test_expand_terms_falls_back_to_empty_on_failure(monkeypatch):
    def fail(_query):
        raise RuntimeError("no aux model")

    monkeypatch.setattr(find, "_aux_terms", fail)
    assert find._expand_terms("retry handling") == []


def test_query_terms_reserves_room_for_expansions(monkeypatch):
    monkeypatch.setattr(find, "_aux_terms", lambda _query: ["backoff", "jitter"])
    assert find._query_terms("retry handling status endpoint") == [
        "retry", "handling", "status", "backoff", "jitter"]


def test_judgrg_uses_expanded_terms_in_the_plan(monkeypatch, tmp_path):
    seen = {}

    def fake_execute(plan, _query, _scope, **_kwargs):
        seen["plan"] = plan
        return [], False, find._Output("", "", 0), ["rg"]

    monkeypatch.setattr(find, "_aux_terms", lambda _query: ["backoff"])
    monkeypatch.setattr(find, "_execute", fake_execute)
    payload = find.judgrg(
        "retry handling status endpoint", judge=ChoiceJudge("literal"), path=str(tmp_path))

    assert payload["coverage"] == "complete"
    assert set(seen["plan"].expression.split("|")) == {
        "retry", "handling", "status", "backoff", "endpoint"}
