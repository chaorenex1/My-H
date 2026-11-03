"""P3: Change Graph, coverage contexts and revision pruning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.hermes_codegraph.helpers import commit, make_repo


def _open(root: Path):
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    return CodeIntelligence.open(root=root, limits=CodeGraphLimits())


def test_change_graph_records_modified_and_deleted(code_home, tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "mod.py").write_text(
        "def kept():\n    return 1\n\ndef removed():\n    return 2\n",
        encoding="utf-8",
    )
    commit(root)
    with _open(root) as ci:
        ci.index()
        first = {item["name"]: item for item in ci.find_symbols(limit=50)["results"]}
        (root / "mod.py").write_text(
            "def kept():\n    return 10\n\ndef added():\n    return 3\n",
            encoding="utf-8",
        )
        commit(root, "second")
        ci.index()
        changes = ci.get_changes(first["kept"]["symbol_id"])["results"]
        assert any(item["change_kind"] == "modified" for item in changes)
        deleted = ci.get_changes(first["removed"]["symbol_id"])["results"]
        assert any(item["edge_type"] == "CODE_DELETED_IN" for item in deleted)
        second = {item["name"]: item for item in ci.find_symbols(limit=50)["results"]}
        assert second["added"]["symbol_id"] != first["kept"]["symbol_id"]


def test_cross_file_move_reuses_symbol_id(code_home, tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "one.py").write_text(
        "def moved(value):\n    return value + 1\n", encoding="utf-8"
    )
    (root / "two.py").write_text("", encoding="utf-8")
    commit(root)
    with _open(root) as ci:
        ci.index()
        before = ci.find_symbols(name="moved")["results"][0]
        (root / "one.py").write_text("", encoding="utf-8")
        (root / "two.py").write_text(
            "def moved(value):\n    return value + 1\n", encoding="utf-8"
        )
        commit(root, "move")
        ci.index()
        after = ci.find_symbols(name="moved")["results"][0]
        assert after["symbol_id"] == before["symbol_id"]
        changes = ci.get_changes(after["symbol_id"])["results"]
        assert any(item["change_kind"] == "moved" for item in changes)


def test_dirty_worktree_does_not_write_change_edges(code_home, tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    commit(root)
    with _open(root) as ci:
        ci.index()
        (root / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        ci.index()
        symbol = ci.find_symbols(name="f")["results"][0]
        assert ci.get_changes(symbol["symbol_id"])["results"] == []
        from hermes_codegraph.exceptions import IndexStaleError

        with pytest.raises(IndexStaleError):
            ci.get_callers(symbol["symbol_id"], policy="strict")


def test_coverage_import_creates_covers_and_rejects_aggregate(code_home, tmp_path):
    from hermes_codegraph.exceptions import CoverageContextMissingError

    root = make_repo(tmp_path / "repo")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "b.py").write_text(
        "def helper(x):\n    return x\n", encoding="utf-8"
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_b.py").write_text(
        "from pkg.b import helper\n\ndef test_helper():\n    assert helper(1) == 1\n",
        encoding="utf-8",
    )
    commit(root)
    with _open(root) as ci:
        ci.index()
        helper = ci.find_symbols(name="helper")["results"][0]
        report = tmp_path / "coverage.json"
        report.write_text(
            json.dumps(
                {
                    "files": {
                        "pkg/b.py": {
                            "contexts": {"2": ["tests/test_b.py::test_helper"]}
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = ci.import_coverage(
            str(report), revision=ci.repository.commit_hash
        )
        assert result["covers_edges"] >= 1
        coverage = ci.get_coverage(helper["symbol_id"])
        assert coverage["metadata"]["coverage_available"] is True
        assert coverage["results"]

        aggregate = tmp_path / "aggregate.json"
        aggregate.write_text(
            json.dumps({"files": {"pkg/b.py": {"executed_lines": [2]}}}),
            encoding="utf-8",
        )
        with pytest.raises(CoverageContextMissingError):
            ci.import_coverage(str(aggregate), revision=ci.repository.commit_hash)


def test_prune_keeps_current_and_removes_old_revisions(code_home, tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    commit(root)
    with _open(root) as ci:
        for index in range(3):
            ci.index()
            (root / "mod.py").write_text(
                f"def f():\n    return {index + 2}\n", encoding="utf-8"
            )
            commit(root, f"rev{index}")
        result = ci.prune(keep=2)
        assert result["status"] == "complete"
        assert result["removed"] >= 1
        assert ci.status()["index_status"] in ("complete", "patched")
