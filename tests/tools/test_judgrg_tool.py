"""``judgrg``: gating, toolset membership, intent routing, and hybrid execution."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent.judgment.config import JudgmentSettings, LocalSettings
from agent.judgment.types import (
    ChoiceAnswer,
    JudgmentResult,
    JudgmentUnavailable,
    NoulAnswer,
)


class IntentJudge:
    api = "typesafe"
    label = "typesafe"
    supports_images = False

    def __init__(self, intent: str = "literal", text_api: bool = False) -> None:
        self.intent = intent
        self.api = "text" if text_api else "typesafe"
        self.requests = 0
        self.purposes: list[str] = []
        self.labels: list[str] = []

    def judge(self, request, *, cancel_event: threading.Event | None = None,
              purpose: str = "") -> JudgmentResult:
        self.requests += 1
        self.purposes.append(purpose)
        answers = {}
        for question_id, question in request.questions.items():
            labels = list(getattr(question, "criteria", {}) or {})
            if labels:
                self.labels = labels
                answers[question_id] = ChoiceAnswer(
                    self.intent,
                    {label: (1.0 if label == self.intent else 0.0) for label in labels},
                    1.0)
            else:
                answers[question_id] = NoulAnswer(0.5)
        kind = "online" if self.api != "typesafe" else "native"
        return JudgmentResult(kind=kind, api=self.api, provider="typesafe", model="m", answers=answers)


@pytest.fixture
def tree(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / "auth").mkdir(parents=True)
    (root / "auth" / "login.py").write_text(
        "def authenticate(user, password):\n    return check(user, password)\n", encoding="utf-8")
    (root / "utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n")
    return root


def _execs(*outputs):
    from agent.judgment.find import _Output

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], Path]] = []
            self.queue = list(outputs)

        def __call__(self, argv, *, cwd, timeout):
            self.calls.append((list(argv), Path(cwd)))
            return self.queue.pop(0) if self.queue else _Output("", "", 0)

    return Recorder()


def _exec(stdout: str = "", stderr: str = "", code: int = 0, timed_out: bool = False):
    from agent.judgment.find import _Output

    return _execs(_Output(stdout, stderr, code, timed_out))


def _rg_match(path: str, line: int, text: str) -> str:
    return json.dumps({"type": "match", "data": {
        "path": {"text": path}, "line_number": line, "lines": {"text": text}}})


def _ast_match(path: str, start: int, end: int, text: str) -> str:
    return json.dumps({
        "text": text, "file": path,
        "range": {"start": {"line": start, "column": 0}, "end": {"line": end, "column": 1}},
    })


def test_registered_in_the_judgment_toolset():
    import tools.judgrg_tool  # registers at import
    from tools.registry import registry
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    assert registry.get_toolset_for_tool("judgrg") == "judgment"
    assert "judgrg" in TOOLSETS["judgment"]["tools"]
    assert "judgrg" in _HERMES_CORE_TOOLS


def test_removed_tools_are_gone():
    from tools.registry import registry

    assert registry.get_entry("judgglob") is None
    assert registry.get_entry("judggrep") is None


def test_check_fn_is_auto_native_then_on(monkeypatch):
    from agent.judgment import find

    monkeypatch.setattr(
        find, "load_judgment_settings", lambda: JudgmentSettings(tools_enabled="auto"))
    monkeypatch.setattr(find, "has_native_judge", lambda: False)
    assert find.judgment_tools_available() is False
    monkeypatch.setattr(find, "has_native_judge", lambda: True)
    assert find.judgment_tools_available() is True

    monkeypatch.setattr(
        find, "load_judgment_settings", lambda: JudgmentSettings(tools_enabled="on"))
    monkeypatch.setattr(find, "has_native_judge", lambda: False)
    assert find.judgment_tools_available() is True

    monkeypatch.setattr(
        find, "load_judgment_settings", lambda: JudgmentSettings(tools_enabled="off"))
    assert find.judgment_tools_available() is False


def test_auto_mode_refuses_to_spend_a_text_model(monkeypatch):
    from agent.judgment import find

    monkeypatch.setattr(
        find, "load_judgment_settings",
        lambda: JudgmentSettings(tools_enabled="auto", local=LocalSettings()))
    monkeypatch.setattr(find, "has_native_judge", lambda: False)
    with pytest.raises(JudgmentUnavailable, match="no native"):
        find.resolve_find_judge("find")

    monkeypatch.setattr(
        find, "load_judgment_settings", lambda: JudgmentSettings(tools_enabled="off"))
    with pytest.raises(JudgmentUnavailable, match="off"):
        find.resolve_find_judge("find")


def test_schema_contract_is_the_model_facing_surface():
    from tools.judgrg_tool import JUDGRG_SCHEMA

    assert JUDGRG_SCHEMA["name"] == "judgrg"
    assert set(JUDGRG_SCHEMA["parameters"]["properties"]) == {"query"}
    assert JUDGRG_SCHEMA["parameters"]["required"] == ["query"]


def test_literal_intent_returns_ranges_and_snippets(tree, monkeypatch):
    from agent.judgment import find
    from tools.judgrg_tool import judgrg_tool

    judge = IntentJudge("literal")
    monkeypatch.setattr("agent.judgment.find.resolve_find_judge", lambda purpose: judge)
    monkeypatch.setattr(find, "resolve_rg", lambda: "/bin/rg")
    record = _exec("\n".join([
        _rg_match("auth/login.py", 2, "    return check(user, password)\n"),
        json.dumps({"type": "begin", "data": {}}),
    ]))
    monkeypatch.setattr(find, "_run", record)

    payload = json.loads(judgrg_tool({"query": "authenticate a password"}))

    assert judge.purposes == ["find"]
    assert judge.labels == [
        "literal", "symbol", "call", "type", "config", "todo", "pattern",
        "unknown_implementation", "cross_language"]
    assert (payload["intent"], payload["mode"], payload["tools"]) == (
        "literal", "content", ["rg"])
    assert payload["coverage"] == "complete"
    hit = payload["hits"][0]
    assert hit["path"].endswith("auth/login.py")
    assert hit["ranges"] == [{"start": 2, "end": 2, "snippet": "return check(user, password)"}]
    argv = record.calls[0][0]
    assert "--json" in argv and "--iglob" in argv and "!.env" in argv
    assert payload["stats"]["requests"] == 1


def test_files_fallback_when_the_query_has_no_terms(tree, monkeypatch):
    from agent.judgment import find
    from tools.judgrg_tool import judgrg_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: IntentJudge("literal"))
    monkeypatch.setattr(find, "resolve_rg", lambda: "/bin/rg")
    record = _exec("auth/login.py\n.env\nlogo.png\nutils.py\n")
    monkeypatch.setattr(find, "_run", record)

    payload = json.loads(judgrg_tool({"query": "the of"}))

    assert (payload["intent"], payload["mode"], payload["tools"]) == (
        "literal", "files", ["rg"])
    assert payload["fallback"]
    names = sorted(Path(hit["path"]).name for hit in payload["hits"])
    assert names == ["login.py", "utils.py"]
    assert "--files" in record.calls[0][0]


def test_path_fallback_uses_the_query_glob(tree, monkeypatch):
    from agent.judgment import find
    from tools.judgrg_tool import judgrg_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: IntentJudge("literal"))
    monkeypatch.setattr(find, "resolve_rg", lambda: "/bin/rg")
    record = _exec("auth/login.py\nutils.py\n")
    monkeypatch.setattr(find, "_run", record)

    payload = json.loads(judgrg_tool({"query": "*.py"}))

    assert (payload["intent"], payload["mode"]) == ("literal", "path")
    argv = record.calls[0][0]
    globs = [argv[index + 1] for index, arg in enumerate(argv) if arg in ("-g", "--iglob")]
    assert "*.py" in globs


def test_symbol_intent_runs_rg_then_ast(tree, monkeypatch):
    from agent.judgment import find
    from agent.judgment.find import _Output
    from tools.judgrg_tool import judgrg_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: IntentJudge("symbol"))
    monkeypatch.setattr(find, "resolve_rg", lambda: "/bin/rg")
    monkeypatch.setattr(find, "resolve_ast_grep", lambda: "/bin/ast-grep")
    record = _execs(
        _Output(_rg_match("auth/login.py", 2, "    print(user)\n"), "", 0),
        _Output(_ast_match("auth/login.py", 1, 1, "print(user)"), "", 0))
    monkeypatch.setattr(find, "_run", record)

    payload = json.loads(judgrg_tool({
        "query": "print($X)"}))

    assert (payload["intent"], payload["mode"], payload["tools"]) == (
        "symbol", "hybrid", ["rg", "ast-grep"])
    assert len(record.calls) == 2
    assert payload["hits"][0]["ranges"] == [
        {"start": 2, "end": 2, "snippet": "print(user)"}]


def test_symbol_intent_refines_a_bare_name_with_ast(tree, monkeypatch):
    from agent.judgment import find
    from agent.judgment.find import _Output
    from tools.judgrg_tool import judgrg_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: IntentJudge("symbol"))
    monkeypatch.setattr(find, "resolve_rg", lambda: "/bin/rg")
    monkeypatch.setattr(find, "resolve_ast_grep", lambda: "/bin/ast-grep")
    record = _execs(
        _Output(_rg_match("auth/login.py", 1, "def authenticate(user):\n"), "", 0),
        _Output(_ast_match("auth/login.py", 0, 0, "authenticate"), "", 0))
    monkeypatch.setattr(find, "_run", record)

    payload = json.loads(judgrg_tool({"query": "authenticate"}))

    assert (payload["intent"], payload["mode"], payload["tools"]) == (
        "symbol", "hybrid", ["rg", "ast-grep"])
    ast_argv = record.calls[1][0]
    assert ast_argv[ast_argv.index("--pattern") + 1] == "authenticate"


def test_call_intent_uses_ast_only(tree, monkeypatch):
    from agent.judgment import find
    from tools.judgrg_tool import judgrg_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: IntentJudge("call"))
    monkeypatch.setattr(find, "resolve_ast_grep", lambda: "/bin/ast-grep")
    record = _exec(_ast_match("auth/login.py", 1, 1, "print(user)"))
    monkeypatch.setattr(find, "_run", record)

    payload = json.loads(judgrg_tool({
        "query": "print($X)"}))

    assert (payload["intent"], payload["mode"], payload["tools"]) == (
        "call", "ast", ["ast-grep"])
    assert payload["hits"][0]["ranges"] == [
        {"start": 2, "end": 2, "snippet": "print(user)"}]
    argv = record.calls[0][0]
    assert "--lang" not in argv  # ast-grep infers the language per file


def test_prose_is_not_sent_to_ast_grep(tree, monkeypatch):
    from agent.judgment import find
    from tools.judgrg_tool import judgrg_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: IntentJudge("call"))
    monkeypatch.setattr(find, "resolve_rg", lambda: "/bin/rg")
    record = _exec(_rg_match("auth/login.py", 1, "def authenticate(user, password):\n"))
    monkeypatch.setattr(find, "_run", record)

    payload = json.loads(judgrg_tool({
        "query": "where users are authenticated"}))

    assert payload["intent"] == "call"
    assert payload["mode"] == "content"
    assert "not a code pattern" in payload["fallback"]
    assert "--pattern" not in record.calls[0][0]


def test_unexpected_answer_falls_back_to_a_guess():
    from agent.judgment import find

    class OddJudge(IntentJudge):
        def judge(self, request, *, cancel_event=None, purpose=""):
            return JudgmentResult(
                kind="native",
                api="typesafe", provider="typesafe", model="m",
                answers={"intent": NoulAnswer(0.9)})

    stats = find._RunStats()
    assert find.choose_intent(OddJudge(), "authenticate a password", stats) == (
        "unknown_implementation")
    assert find.choose_intent(OddJudge(), "print($X)", stats) == "pattern"


def test_handler_marks_text_judgments_uncalibrated(tree, monkeypatch):
    from agent.judgment import find
    from tools.judgrg_tool import judgrg_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge",
        lambda purpose: IntentJudge("literal", text_api=True))
    monkeypatch.setattr(find, "resolve_rg", lambda: "/bin/rg")
    monkeypatch.setattr(find, "_run", _exec("utils.py\n"))

    payload = json.loads(judgrg_tool({"query": "the of"}))
    assert payload["uncalibrated"] is True


def test_empty_query_and_missing_judge_are_tool_errors(monkeypatch):
    from tools.judgrg_tool import judgrg_tool

    assert "non-empty" in judgrg_tool({"query": "  "})

    def unavailable(purpose):
        raise JudgmentUnavailable("no credential")

    monkeypatch.setattr("agent.judgment.find.resolve_find_judge", unavailable)
    error = judgrg_tool({"query": "auth"})
    assert "judgment unavailable" in error and "use search_files" in error


def test_all_requests_failing_is_an_error_not_an_absence_claim(tmp_path, monkeypatch):
    from tools.judgrg_tool import judgrg_tool

    root = tmp_path / "repo"
    root.mkdir()
    (root / "auth.py").write_text("x = 1\n", encoding="utf-8")

    class DownJudge(IntentJudge):
        def judge(self, request, *, cancel_event=None, purpose=""):
            raise JudgmentUnavailable("down")

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: DownJudge())
    error = judgrg_tool({"query": "auth"})
    assert "not evidence" in error


def test_missing_ripgrep_is_actionable(tree, monkeypatch):
    from agent.judgment import find
    from tools.judgrg_tool import judgrg_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: IntentJudge("literal"))
    monkeypatch.setattr(find, "resolve_rg", lambda: None)
    error = judgrg_tool({"query": "authenticate"})
    assert "ripgrep" in error and "pm install" in error


def test_backend_timeout_is_reported_as_partial(tree, monkeypatch):
    from agent.judgment import find
    from tools.judgrg_tool import judgrg_tool

    monkeypatch.setattr(
        "agent.judgment.find.resolve_find_judge", lambda purpose: IntentJudge("literal"))
    monkeypatch.setattr(find, "resolve_rg", lambda: "/bin/rg")
    monkeypatch.setattr(find, "_run", _exec("", code=124, timed_out=True))

    payload = json.loads(judgrg_tool({"query": "the of"}))
    assert payload["coverage"] == "partial"
    assert payload["hits"] == []
