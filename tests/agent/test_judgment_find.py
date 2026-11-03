"""Semantic find engine: bounded fan-out, wall budget, and the D14 caps."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent.judgment import find
from agent.judgment.find import FileEntry, Window
from agent.judgment.types import Judge, JudgmentResult, NoulAnswer


class RecordingJudge(Judge):
    api = "typesafe"
    label = "recording"
    supports_images = False

    def __init__(self, probability: float = 0.9) -> None:
        self.probability = probability
        self.requests = []

    def judge(self, request, *, cancel_event=None, purpose=""):
        self.requests.append(request)
        return JudgmentResult(
            api=self.api, provider="typesafe", model="m",
            answers={qid: NoulAnswer(self.probability) for qid in request.questions})


def test_dispatch_stops_every_job_at_an_expired_deadline():
    seen: list[int] = []
    stopped = find._dispatch([1, 2, 3], seen.append, deadline=0.0)
    assert stopped is True
    assert seen == []


def test_dispatch_overlaps_jobs():
    barrier = threading.Barrier(2)

    def worker(_job: int) -> None:
        barrier.wait(timeout=5)

    stopped = find._dispatch([1, 2], worker, deadline=time.monotonic() + 30)
    assert stopped is False


def test_dispatch_runs_every_job_under_a_live_deadline():
    seen: list[int] = []
    stopped = find._dispatch(list(range(20)), seen.append, deadline=time.monotonic() + 30)
    assert stopped is False
    assert sorted(seen) == list(range(20))


def test_sketch_requests_respect_the_card_cap():
    entry = FileEntry(Path("f.py"), "f.py", 1)
    cards = [(entry, Window(1, 1, "x", 0.0), "sketch") for _ in range(100)]
    requests = list(find._sketch_requests("q", cards))
    assert all(len(request.questions) <= find.GREP_SKETCH_CARDS for request, _ in requests)
    assert sum(len(request.questions) for request, _ in requests) == 100


def test_dispatch_propagates_the_accounting_context():
    from agent.aux_accounting import (
        current_context,
        reset_accounting_context,
        set_accounting_context,
    )

    class FakeSessionDB:
        pass

    db = FakeSessionDB()
    seen: list[Any] = []
    token = set_accounting_context(db, "sess-ctx")
    try:
        find._dispatch([1, 2], lambda _job: seen.append(current_context()),
                       deadline=time.monotonic() + 30)
    finally:
        reset_accounting_context(token)
    assert seen == [(db, "sess-ctx"), (db, "sess-ctx")]


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_urlopen(request, timeout=None):
    body = json.loads(request.data.decode("utf-8"))
    answers = {qid: {"type": "noul", "noul": 0.9} for qid in body["questions"]}
    return _FakeResponse({
        "model": "jev-test",
        "answers": answers,
        "usage": {"input_tokens": 3, "output_tokens": 1},
    })


def test_parallel_judggrep_requests_are_billed(tmp_path, monkeypatch):
    from agent.aux_accounting import reset_accounting_context, set_accounting_context
    from agent.judgment import native as native_module
    from agent.judgment.config import JudgmentSettings
    from agent.judgment.native import NativeJudge, NativeRoute

    calls: list[tuple] = []

    class FakeSessionDB:
        def record_auxiliary_usage(self, *args, **kwargs):
            calls.append((args, kwargs))

    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("authenticate a password\n", encoding="utf-8")
    (root / "b.py").write_text("authenticate another password\n", encoding="utf-8")

    monkeypatch.setattr(find, "GREP_WINDOWS", 1)
    monkeypatch.setattr(find, "GREP_SKETCH_CARDS", 1)
    monkeypatch.setattr(native_module.urllib.request, "urlopen", _fake_urlopen)

    route = NativeRoute(
        api="typesafe", provider="typesafe", model="jev-test",
        base_url="http://127.0.0.1:9", api_key="k", timeout_ms=2000)
    judge = NativeJudge(route, JudgmentSettings(cache=False, audit=False))

    token = set_accounting_context(FakeSessionDB(), "sess-1")
    try:
        payload = find.judggrep("authenticate a password", judge=judge, path=str(root))
    finally:
        reset_accounting_context(token)

    assert payload["stats"]["requests"] >= 2
    assert len(calls) == payload["stats"]["requests"]
    assert all(args[:2] == ("sess-1", "judge") for args, _kwargs in calls)


def _wide_file(path: Path, lines: int = 24) -> None:
    body = "".join(f"{'x' * 4980} keyword {index}\n" for index in range(lines))
    path.write_text(body, encoding="utf-8")


def test_verification_stops_at_the_passage_cap(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _wide_file(root / "a.py")
    _wide_file(root / "b.py")

    judge = RecordingJudge()
    payload = find.judggrep("keyword", judge=judge, path=str(root), limit=50)

    assert payload["stats"]["windows"] == 48
    assert payload["stats"]["files"] == 2
    sketch_requests = [request for request in judge.requests if "files" in request.state]
    verify_requests = [request for request in judge.requests if "file" in request.state]
    assert sum(len(request.questions) for request in sketch_requests) == 48
    assert sum(len(request.questions) for request in verify_requests) == find.GREP_FULL_LIMIT
    assert payload["coverage"] == "complete"
    assert payload["hits"]


def test_exhausted_wall_budget_reports_partial(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("keyword\n", encoding="utf-8")
    entry = FileEntry(root / "a.py", "a.py", 8)

    monkeypatch.setattr(find, "GREP_TIMEOUT_S", -1.0)
    monkeypatch.setattr(find, "list_files", lambda *args, **kwargs: [entry])
    monkeypatch.setattr(find, "_rank_content", lambda *args, **kwargs: [entry])
    payload = find.judggrep("keyword", judge=RecordingJudge(), path=str(root))

    assert payload["coverage"] == "partial"
    assert payload["stats"]["requests"] == 0


def test_per_file_and_window_caps_are_applied(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _wide_file(root / "a.py")
    _wide_file(root / "b.py")

    monkeypatch.setattr(find, "GREP_FILES", 1)
    monkeypatch.setattr(find, "GREP_WINDOWS", 2)
    monkeypatch.setattr(find, "_rank_content", lambda entries, terms, deadline: [entries[0]])

    judge = RecordingJudge()
    payload = find.judggrep("keyword", judge=judge, path=str(root))

    assert payload["stats"]["files"] == 1
    assert payload["stats"]["windows"] == 2
