"""Structural read summaries: eligibility, rendering, and the read_file switch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import read_summary
from tools.file_tools import read_file_tool


def _python_source(body_lines: int = 140) -> str:
    body = "\n".join(f"    value_{i} = {i}" for i in range(body_lines))
    return (
        '"""Module docstring."""\n'
        "import os\n"
        "\n"
        "def compute(value):\n"
        '    """Compute things."""\n'
        f"{body}\n"
        "    return value_0\n"
        "\n"
        "class Widget:\n"
        '    """A widget."""\n'
        "\n"
        "    def method(self):\n"
        "        return 1\n"
    )


@pytest.fixture(autouse=True)
def _summary_settings(monkeypatch):
    monkeypatch.setattr(read_summary, "_read_settings",
                        lambda: {"enabled": True, "prose": False, "min_total_lines": 20})


def test_summary_keeps_declarations_and_elides_bodies():
    text = _python_source()
    total = len(text.splitlines())

    summary = read_summary.summarize_read(text, "sample.py", total)

    assert summary is not None
    assert "def compute(value):" in summary.content
    assert "class Widget:" in summary.content
    assert "def method(self):" in summary.content
    assert "value_10 = 10" not in summary.content
    assert "read_file(path=" in summary.content
    assert "offset=" in summary.content
    assert summary.kept_lines < total
    assert summary.elided


def test_non_python_declarations_use_the_stdlib_heuristic(monkeypatch):
    monkeypatch.setattr(read_summary, "_summarize_tree_sitter", lambda *_args: None)
    source = "import { x } from 'y'\n" + "\n".join(f"// body {i}" for i in range(120)) \
        + "\nfunction alpha() {\n  return 1\n}\nclass Beta {\n}\n"

    summary = read_summary.summarize_read(source, "sample.ts", len(source.splitlines()))

    assert summary is not None
    assert "function alpha() {" in summary.content
    assert "class Beta {" in summary.content
    assert "// body 50" not in summary.content


def test_tree_sitter_does_not_keep_arbitrary_body_lines(monkeypatch):
    from types import SimpleNamespace

    lines = ["header", "import x", "junk 3", "junk 4", "class A {"] \
        + [f"  body {i}" for i in range(5, 25)] + ["}"]
    symbol = SimpleNamespace(start_line=5, end_line=26, kind="class")
    parsed = SimpleNamespace(symbols=[symbol],
                             imports=[SimpleNamespace(start_line=2, end_line=2)])

    summary = read_summary._render_tree_sitter(parsed, lines, "sample.ts", len(lines))

    assert summary is not None
    assert "2|import x" in summary.content
    assert "5|class A {" in summary.content
    assert "26|}" in summary.content
    assert "11|  body 10" not in summary.content


def test_tree_sitter_summary_wins_when_available(monkeypatch):
    sentinel = read_summary.ReadSummary(content="TREE", total_lines=200,
                                        elided=((10, 199),), kept_lines=10)
    monkeypatch.setattr(read_summary, "_summarize_tree_sitter", lambda *_args: sentinel)
    monkeypatch.setattr(read_summary, "_summarize_python",
                        lambda *_args: pytest.fail("ast must not run when tree-sitter parsed"))

    assert read_summary.summarize_read(_python_source(), "sample.py", 200) is sentinel


def test_summary_is_collected_for_the_settle_artifact():
    from agent import run_transcript

    task_id = "persist-task"
    read_summary.set_read_summarize(task_id, True, agent_name="researcher",
                                    agent_session_id="session-1")
    try:
        summary = read_summary.summarize_read(
            _python_source(), "src/sample.py", len(_python_source().splitlines()))
        assert summary is not None
        saved = read_summary.persist_summary(
            task_id, "src/sample.py", summary, content="REDACTED SUMMARY")
        assert saved is not None
        assert saved["source"] == "src/sample.py"
        collected = read_summary.collect_summaries(task_id)
        assert collected == [{**saved, "summary": "REDACTED SUMMARY"}]
        assert read_summary.collect_summaries(task_id) == [], "collect drains the run buffer"
        assert not run_transcript.summary_dir("researcher", "session-1").exists(), \
            "ADR-16 D4 retired the .summaries/ directory"
    finally:
        read_summary.clear_read_summarize(task_id)


def test_legacy_summaries_are_read_once_for_migration(tmp_path):
    from agent import run_transcript

    agent, session = "researcher", "legacy-session"
    directory = run_transcript.summary_dir(agent, session)
    directory.mkdir(parents=True)
    (directory / "abc-sample.md").write_text("LEGACY BODY", encoding="utf-8")
    (directory / "index.json").write_text(json.dumps({"summaries": [{
        "source": "src/sample.py", "file": "abc-sample.md", "kept_lines": 3,
        "total_lines": 9, "elided_ranges": ["4-9"]}]}), encoding="utf-8")

    sources, found_dir = read_summary.legacy_summaries(agent, session)

    assert found_dir == directory
    assert sources == [{"source": "src/sample.py", "summary": "LEGACY BODY",
                        "kept_lines": 3, "total_lines": 9, "elided_ranges": ["4-9"]}]
    assert read_summary.remove_legacy_summaries(directory) is True
    assert not directory.exists()


def test_per_agent_false_overrides_the_global_switch(monkeypatch):
    monkeypatch.setattr(read_summary, "_read_settings",
                        lambda: {"enabled": True, "prose": False, "min_total_lines": 20})
    task_id = "opt-out-task"
    read_summary.set_read_summarize(task_id, False)
    try:
        assert read_summary.read_summarize_enabled(task_id) is False
        assert read_summary.read_summarize_enabled("other-task") is True
    finally:
        read_summary.clear_read_summarize(task_id)


def test_short_files_stay_verbatim():
    text = "def tiny():\n    return 1\n"

    assert read_summary.summarize_read(text, "tiny.py", 2) is None


def test_prose_is_skipped_unless_enabled():
    text = "# Heading\n\n" + "\n".join(f"line {i}" for i in range(120))

    assert read_summary.summarize_read(text, "notes.md", 122) is None


def test_prose_can_be_enabled(monkeypatch):
    monkeypatch.setattr(read_summary, "_read_settings",
                        lambda: {"enabled": True, "prose": True, "min_total_lines": 20})
    text = "# Heading\n\n" + "\n".join(f"line {i}" for i in range(120))

    # Markdown has no structural extractor, so it still falls back verbatim.
    assert read_summary.summarize_read(text, "notes.md", 122) is None


def test_syntax_errors_fall_back_to_verbatim():
    text = "def broken(:\n" + "\n".join(f"x{i} = {i}" for i in range(120))

    assert read_summary.summarize_read(text, "broken.py", 121) is None


def test_a_conflicted_file_is_never_summarized(tmp_path: Path, monkeypatch):
    source = "value = 1\n<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> branch\n" \
             + "\n".join(f"line_{i} = {i}" for i in range(120))
    path = tmp_path / "conflicted.py"
    path.write_text(source, encoding="utf-8")
    total = len(source.splitlines())

    from tools.file_operations_common import ReadResult

    class _Ops:
        def read_file(self, _path, offset, limit):
            lines = source.splitlines()
            page = lines[offset - 1:offset - 1 + limit]
            return ReadResult(content="\n".join(f"{offset + i}|{line}"
                                                for i, line in enumerate(page)),
                              total_lines=total, file_size=len(source))

        def read_file_raw(self, _path):
            return ReadResult(content=source, total_lines=total, file_size=len(source))

    monkeypatch.setattr("tools.file_tools._get_file_ops", lambda _task_id: _Ops())
    monkeypatch.setattr("tools.file_tools._file_ops_uses_host_paths", lambda _ops: True)

    task_id = "conflict-task"
    read_summary.set_read_summarize(task_id, True)
    try:
        payload = json.loads(read_file_tool(str(path), task_id=task_id, no_selector=True))
    finally:
        read_summary.clear_read_summarize(task_id)

    assert "summary" not in payload
    assert payload["conflict_blocks"] == 1
    assert "<<<<<<< HEAD" in payload["content"]


def test_a_background_review_fork_never_summarizes(tmp_path: Path, monkeypatch):
    source = _python_source()
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    total = len(source.splitlines())

    from tools.file_operations_common import ReadResult

    class _Ops:
        def read_file(self, _path, offset, limit):
            lines = source.splitlines()
            page = lines[offset - 1:offset - 1 + limit]
            return ReadResult(content="\n".join(f"{offset + i}|{line}"
                                                for i, line in enumerate(page)),
                              total_lines=total, file_size=len(source))

        def read_file_raw(self, _path):
            return ReadResult(content=source, total_lines=total, file_size=len(source))

    monkeypatch.setattr("tools.file_tools._get_file_ops", lambda _task_id: _Ops())
    monkeypatch.setattr("tools.file_tools._file_ops_uses_host_paths", lambda _ops: True)
    monkeypatch.setattr("tools.file_tools.is_background_review", lambda: True)

    task_id = "review-task"
    read_summary.set_read_summarize(task_id, True)
    try:
        payload = json.loads(read_file_tool(str(path), task_id=task_id, no_selector=True))
    finally:
        read_summary.clear_read_summarize(task_id)

    assert "summary" not in payload
    assert "value_10 = 10" in payload["content"]


def test_read_file_tool_serves_a_summary_only_for_a_whole_file_read(tmp_path: Path, monkeypatch):
    source = _python_source()
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    total = len(source.splitlines())

    from tools.file_operations_common import ReadResult

    class _Ops:
        def read_file(self, _path, offset, limit):
            lines = source.splitlines()
            page = lines[offset - 1:offset - 1 + limit]
            return ReadResult(content="\n".join(f"{offset + i}|{line}"
                                                for i, line in enumerate(page)),
                              total_lines=total, file_size=len(source))

        def read_file_raw(self, _path):
            return ReadResult(content=source, total_lines=total, file_size=len(source))

    monkeypatch.setattr("tools.file_tools._get_file_ops", lambda _task_id: _Ops())
    monkeypatch.setattr("tools.file_tools._file_ops_uses_host_paths", lambda _ops: True)

    task_id = "summary-task"
    read_summary.set_read_summarize(task_id, True, agent_name="researcher",
                                    agent_session_id="session-x")
    try:
        summarized = json.loads(read_file_tool(str(path), task_id=task_id, no_selector=True))
        assert summarized["summary"] is True
        assert "def compute(value):" in summarized["content"]
        assert "value_10 = 10" not in summarized["content"]
        assert summarized["elided_ranges"]
        assert "summary_path" not in summarized, "ADR-16 D4 writes summaries at settle only"
        collected = read_summary.collect_summaries(task_id)
        assert [entry["summary"] for entry in collected] == [summarized["content"]]

        verbatim = json.loads(read_file_tool(str(path), task_id=task_id, no_selector=False))
        assert "summary" not in verbatim
        assert "value_10 = 10" in verbatim["content"]
    finally:
        read_summary.clear_read_summarize(task_id)
