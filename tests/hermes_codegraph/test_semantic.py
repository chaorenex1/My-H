"""P4: LSP semantic enhancement upgrades/invalidates inferred edges."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.hermes_codegraph.helpers import commit, make_repo


class _FakeLSP:
    def __init__(self, locations):
        self.locations = locations
        self.queries = 0

    def is_active(self):
        return True

    def enabled_for(self, path):
        return True

    def query_definition(self, path, line, column, **kwargs):
        self.queries += 1
        return list(self.locations)


def _open(root: Path):
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    return CodeIntelligence.open(
        root=root, limits=CodeGraphLimits(semantic=True)
    )


def _repo(tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "mod.py").write_text(
        "def helper(x):\n    return x\n\n"
        "def other():\n    return 2\n\n"
        "def caller():\n    return helper(1)\n",
        encoding="utf-8",
    )
    commit(root)
    return root


def test_semantic_upgrade_to_resolved(code_home, tmp_path, monkeypatch):
    root = _repo(tmp_path)
    fake = _FakeLSP(
        [{"path": "mod.py", "start_line": 1, "start_column": 1,
          "end_line": 2, "end_column": 13}]
    )
    monkeypatch.setattr(
        "agent.lsp.manager.LSPService.create_from_config",
        classmethod(lambda cls: fake),
    )
    with _open(root) as ci:
        result = ci.index(semantic=True)
        assert result["semantic"]["upgraded"] >= 1
        caller = ci.find_symbols(name="caller")["results"][0]
        callees = ci.get_callees(caller["symbol_id"], policy="strict")["results"]
        assert [item["qualified_name"] for item in callees] == ["mod.helper"]


def test_semantic_conflict_invalidates_old_target(code_home, tmp_path, monkeypatch):
    root = _repo(tmp_path)
    fake = _FakeLSP(
        [{"path": "mod.py", "start_line": 4, "start_column": 1,
          "end_line": 5, "end_column": 13}]
    )
    monkeypatch.setattr(
        "agent.lsp.manager.LSPService.create_from_config",
        classmethod(lambda cls: fake),
    )
    with _open(root) as ci:
        ci.index(semantic=True)
        caller = ci.find_symbols(name="caller")["results"][0]
        strict = ci.get_callees(caller["symbol_id"], policy="strict")["results"]
        assert [item["qualified_name"] for item in strict] == ["mod.other"]
        exploratory = ci.get_callees(
            caller["symbol_id"], policy="exploratory"
        )["results"]
        assert "mod.helper" not in {item["qualified_name"] for item in exploratory}
