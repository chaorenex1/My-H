"""``judggrep``: gating, bounded cascade, and honest partial/unavailable coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.judgment.types import JudgmentResult, JudgmentUnavailable, NoulAnswer


class FakeJudge:
    api = "typesafe"

    def __init__(self, match: str = "authenticate", fail_from: int = 0) -> None:
        self.match = match
        self.fail_from = fail_from
        self.requests = 0

    def judge(self, request, *, cancel_event=None, purpose=""):
        self.requests += 1
        if self.fail_from and self.requests >= self.fail_from:
            raise JudgmentUnavailable("down")
        answers = {}
        for question_id in request.questions:
            blob = ""
            passages = request.state.get("passages") if isinstance(request.state, dict) else None
            if isinstance(passages, dict):
                blob = passages.get(question_id, "")
            answers[question_id] = NoulAnswer(0.9 if self.match in blob else 0.02)
        return JudgmentResult(api=self.api, provider="typesafe", model="m", answers=answers)


@pytest.fixture
def tree(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "auth.py").write_text(
        "def authenticate(user, password):\n"
        "    token = check_credentials(user, password)\n"
        "    return token\n",
        encoding="utf-8",
    )
    (root / "maths.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "notes.md").write_text("# Auth notes\n\nauthenticate talks to the password store\n",
                                   encoding="utf-8")
    (root / ".env").write_text("PASSWORD=nope\n", encoding="utf-8")
    return root


def test_registered_in_the_judgment_toolset():
    import tools.judggrep_tool  # registers at import
    from tools.registry import registry
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    assert registry.get_toolset_for_tool("judggrep") == "judgment"
    assert "judggrep" in TOOLSETS["judgment"]["tools"]
    assert "judggrep" in _HERMES_CORE_TOOLS


def test_returns_ranges_probabilities_and_snippets(tree, monkeypatch):
    from tools.judggrep_tool import judggrep_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: FakeJudge())
    payload = json.loads(judggrep_tool({
        "query": "authenticate a user password", "path": str(tree), "limit": 5}))

    assert payload["coverage"] == "complete"
    assert payload["hits"], payload
    hit = payload["hits"][0]
    assert hit["path"].endswith("auth.py")
    assert hit["score"] == pytest.approx(0.9)
    range_ = hit["ranges"][0]
    assert range_["p"] == pytest.approx(0.9)
    assert 1 <= range_["start"] <= range_["end"] <= 3
    assert "authenticate" in range_["snippet"]
    assert payload["stats"]["requests"] >= 1


def test_glob_filter_and_keywords_narrow_the_scan(tree, monkeypatch):
    from tools.judggrep_tool import judggrep_tool

    judge = FakeJudge(match="password")
    monkeypatch.setattr("agent.judgment.find.resolve_find_judge", lambda purpose: judge)
    payload = json.loads(judggrep_tool({
        "query": "authenticate a user",
        "path": str(tree),
        "glob": "**/*.md",
        "keywords": ["password"],
        "limit": 5,
    }))
    paths = [hit["path"] for hit in payload["hits"]]
    assert paths and all(path.endswith(".md") for path in paths)


def test_verification_failure_reports_partial_without_claiming_absence(tree, monkeypatch):
    from tools.judggrep_tool import judggrep_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge",
        lambda purpose: FakeJudge(fail_from=2))
    payload = json.loads(judggrep_tool({
        "query": "authenticate a user password", "path": str(tree)}))
    assert payload["coverage"] == "partial"
    assert payload["stats"]["failed"] >= 1
    assert payload["hits"] == []


def test_all_requests_failing_is_a_tool_error(tree, monkeypatch):
    from tools.judggrep_tool import judggrep_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge",
        lambda purpose: FakeJudge(fail_from=1))
    error = judggrep_tool({"query": "authenticate", "path": str(tree)})
    assert "not evidence" in error and "search_files/grep" in error


def test_bad_arguments_and_missing_judge_are_tool_errors(monkeypatch):
    from tools.judggrep_tool import judggrep_tool

    assert "non-empty" in judggrep_tool({"query": ""})
    assert "keywords must be" in judggrep_tool({"query": "x", "keywords": "nope"})
    assert "path not found" in judggrep_tool({"query": "x", "path": "does/not/exist"})
    assert "invalid glob" in judggrep_tool({
        "query": "x", "path": ".", "glob": "src/[unclosed*.py"})

    def unavailable(purpose):
        raise JudgmentUnavailable("no credential")

    monkeypatch.setattr("agent.judgment.find.resolve_find_judge", unavailable)
    error = judggrep_tool({"query": "x", "path": "."})
    assert "judgment unavailable" in error and "search_files/grep" in error
