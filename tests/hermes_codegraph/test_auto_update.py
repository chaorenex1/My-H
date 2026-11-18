"""P6: post-turn reconciler eligibility, budgets and profile binding."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.hermes_codegraph.helpers import commit, make_repo


class _Agent:
    session_cwd = ""
    _turn_origin = None
    _delegate_depth = 0
    _memory_write_origin = "assistant_tool"


def _open(root: Path):
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    return CodeIntelligence.open(root=root, limits=CodeGraphLimits())


def _repo(tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    commit(root)
    return root


def test_eligibility_excludes_children_forks_and_failures():
    from hermes_codegraph.auto_update import _eligible

    agent = _Agent()
    assert _eligible(agent, {"final_response": "ok"}) is True
    assert _eligible(agent, {"final_response": ""}) is False
    assert _eligible(agent, {"final_response": "ok", "failed": True}) is False
    agent._turn_origin = "background_review"
    assert _eligible(agent, {"final_response": "ok"}) is False
    agent._turn_origin = None
    agent._memory_write_origin = "background_review"
    assert _eligible(agent, {"final_response": "ok"}) is False


def test_refresh_updates_changed_file_and_noops_without_changes(code_home, tmp_path):
    from hermes_codegraph.auto_update import _needs_refresh, _run_update

    root = _repo(tmp_path)
    with _open(root) as ci:
        ci.index()
        assert _needs_refresh(ci._client, ci.repository, "mod.py") is False
        (root / "mod.py").write_text(
            "def f():\n    return 2\n", encoding="utf-8"
        )
        assert _needs_refresh(ci._client, ci.repository, "mod.py") is True
    (root / "mod.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8"
    )
    result = _run_update(
        str(code_home), str(root), turn_id="t1", max_files=5,
        budget_seconds=10, synchronous=True,
    )
    assert result["status"] == "no_changes"
    (root / "mod.py").write_text(
        "def f():\n    return 2\n\ndef added():\n    return 3\n", encoding="utf-8"
    )
    result = _run_update(
        str(code_home), str(root), turn_id="t2", max_files=5,
        budget_seconds=10, synchronous=True,
    )
    assert result["status"] == "completed"
    with _open(root) as ci:
        assert ci.find_symbols(name="added")["results"]


def test_schedule_is_zero_io_and_respects_auto_update(code_home, tmp_path, monkeypatch):
    from hermes_codegraph import auto_update

    root = _repo(tmp_path)
    with _open(root) as ci:
        ci.index()
    agent = _Agent()
    agent.session_cwd = str(root)
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        calls.append("io")
        raise AssertionError("scheduler performed I/O")

    monkeypatch.setattr(auto_update, "_run_update", forbidden)
    monkeypatch.setattr(auto_update, "_enqueue", lambda *a, **k: {"status": "queued"})
    result = auto_update.schedule_codegraph_post_turn(
        agent, {"final_response": "done"}, turn_id="t"
    )
    assert result["status"] == "queued"
    assert calls == []

    class _Limits:
        enabled = True
        auto_update = False

    monkeypatch.setattr(
        "hermes_codegraph.config.CodeGraphLimits.from_config",
        classmethod(lambda cls, config=None: _Limits()),
    )
    assert auto_update.schedule_codegraph_post_turn(
        agent, {"final_response": "done"}
    )["status"] == "skipped"


def test_oneshot_uses_synchronous_flush(code_home, tmp_path, monkeypatch):
    from hermes_codegraph import auto_update

    root = _repo(tmp_path)
    with _open(root) as ci:
        ci.index()
    agent = _Agent()
    agent.session_cwd = str(root)
    monkeypatch.setattr(
        "agent.oneshot_footprint.is_single_query_session", lambda: True
    )
    seen: list[str] = []

    def fake_run(home, workspace, **kwargs):
        seen.append(kwargs.get("synchronous") and "sync" or "async")
        return {"status": "completed"}

    monkeypatch.setattr(auto_update, "_run_update", fake_run)
    result = auto_update.schedule_codegraph_post_turn(
        agent, {"final_response": "done"}, turn_id="t"
    )
    assert result["status"] == "completed"
    assert seen == ["sync"]
