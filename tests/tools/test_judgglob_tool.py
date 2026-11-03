"""``judgglob``: gating, toolset membership, and the read-only name ranking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.judgment.config import LocalSettings, JudgmentSettings
from agent.judgment.types import (
    JudgmentResult,
    JudgmentUnavailable,
    NoulAnswer,
)


class FakeJudge:
    api = "typesafe"

    def __init__(self, match: str = "auth", text_api: bool = False) -> None:
        self.match = match
        self.api = "text" if text_api else "typesafe"
        self.requests = 0

    def judge(self, request, *, cancel_event=None, purpose=""):
        self.requests += 1
        answers = {}
        for question_id, question in request.questions.items():
            blob = question.instructions
            files = request.state.get("files") if isinstance(request.state, dict) else None
            if isinstance(files, dict) and question_id in files:
                blob += str(files[question_id])
            answers[question_id] = NoulAnswer(0.9 if self.match in blob else 0.05)
        return JudgmentResult(api=self.api, provider="typesafe", model="m", answers=answers)


@pytest.fixture
def tree(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / "auth").mkdir(parents=True)
    (root / "auth" / "login.py").write_text(
        "def authenticate(user, password):\n    return check(user, password)\n", encoding="utf-8")
    (root / "utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / "empty.py").write_text("", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "auth_secret.py").write_text("x = 1\n", encoding="utf-8")
    (root / "generated").mkdir()
    (root / "generated" / "auth_gen.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text("generated/\n", encoding="utf-8")
    return root


def test_registered_in_the_judgment_toolset():
    import tools.judgglob_tool  # registers at import
    from tools.registry import registry
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    assert registry.get_toolset_for_tool("judgglob") == "judgment"
    assert "judgglob" in TOOLSETS["judgment"]["tools"]
    assert "judgglob" in _HERMES_CORE_TOOLS


def test_check_fn_is_auto_native_then_on(monkeypatch):
    from agent.judgment import find

    monkeypatch.setattr(
        find, "load_judgment_settings",
        lambda: JudgmentSettings(tools_enabled="auto"))
    monkeypatch.setattr(find, "has_native_judge", lambda: False)
    assert find.judgment_tools_available() is False
    monkeypatch.setattr(find, "has_native_judge", lambda: True)
    assert find.judgment_tools_available() is True

    monkeypatch.setattr(
        find, "load_judgment_settings",
        lambda: JudgmentSettings(tools_enabled="on"))
    monkeypatch.setattr(find, "has_native_judge", lambda: False)
    assert find.judgment_tools_available() is True

    monkeypatch.setattr(
        find, "load_judgment_settings",
        lambda: JudgmentSettings(tools_enabled="off"))
    monkeypatch.setattr(find, "has_native_judge", lambda: True)
    assert find.judgment_tools_available() is False


def test_auto_mode_refuses_to_spend_a_text_model(monkeypatch):
    from agent.judgment import find

    monkeypatch.setattr(
        find, "load_judgment_settings",
        lambda: JudgmentSettings(tools_enabled="auto", local=LocalSettings()))
    monkeypatch.setattr(find, "has_native_judge", lambda: False)
    with pytest.raises(JudgmentUnavailable, match="no native"):
        find.resolve_find_judge("judgglob")

    monkeypatch.setattr(
        find, "load_judgment_settings",
        lambda: JudgmentSettings(tools_enabled="off"))
    with pytest.raises(JudgmentUnavailable, match="off"):
        find.resolve_find_judge("judgglob")


def test_handler_ranks_matching_paths_and_filters_the_tree(tree, monkeypatch):
    from tools.judgglob_tool import judgglob_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: FakeJudge())
    payload = json.loads(judgglob_tool({
        "query": "code that authenticates users", "path": str(tree), "limit": 5}))

    paths = [hit["path"] for hit in payload["hits"]]
    assert payload["coverage"] == "complete"
    assert paths and paths[0].endswith("auth/login.py")
    assert payload["hits"][0]["score"] == pytest.approx(0.9)
    assert all(".env" not in path and ".hidden" not in path for path in paths)
    assert all("generated" not in path and "empty.py" not in path for path in paths)
    assert payload["stats"]["requests"] == 1


def test_handler_marks_text_judgments_uncalibrated(tree, monkeypatch):
    from tools.judgglob_tool import judgglob_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge",
        lambda purpose: FakeJudge(text_api=True))
    payload = json.loads(judgglob_tool({"query": "auth", "path": str(tree)}))
    assert payload["uncalibrated"] is True


def test_empty_query_and_missing_judge_are_tool_errors(monkeypatch):
    from tools.judgglob_tool import judgglob_tool

    assert "non-empty" in judgglob_tool({"query": "  "})

    def unavailable(purpose):
        raise JudgmentUnavailable("no credential")

    monkeypatch.setattr("agent.judgment.find.resolve_find_judge", unavailable)
    error = judgglob_tool({"query": "auth", "path": "."})
    assert "judgment unavailable" in error and "use glob" in error


def test_all_requests_failing_is_an_error_not_an_absence_claim(tmp_path, monkeypatch):
    from tools.judgglob_tool import judgglob_tool

    root = tmp_path / "repo"
    root.mkdir()
    (root / "auth.py").write_text("x = 1\n", encoding="utf-8")

    class DownJudge(FakeJudge):
        def judge(self, request, *, cancel_event=None, purpose=""):
            raise JudgmentUnavailable("down")

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: DownJudge())
    error = judgglob_tool({"query": "auth", "path": str(root)})
    assert "not evidence" in error
