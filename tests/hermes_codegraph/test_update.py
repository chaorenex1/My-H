"""P5: refresh_file, evidence-constrained writes, conflicts and invalidation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.hermes_codegraph.helpers import commit, make_repo


def _open(root: Path):
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    return CodeIntelligence.open(root=root, limits=CodeGraphLimits())


def _repo(tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "mod.py").write_text(
        "def helper(x):\n    return x\n", encoding="utf-8"
    )
    commit(root)
    return root


def _evidence(path: Path, line: int) -> dict:
    text = path.read_text(encoding="utf-8").splitlines()[line - 1]
    return {
        "source_tool": "ast-grep",
        "span": {
            "start_line": line,
            "start_column": 1,
            "end_line": line,
            "end_column": len(text) + 1,
        },
        "excerpt_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def test_refresh_file_makes_new_facts_visible(code_home, tmp_path):
    root = _repo(tmp_path)
    with _open(root) as ci:
        ci.index()
        (root / "mod.py").write_text(
            "def helper(x):\n    return x + 1\n\ndef added():\n    return 5\n",
            encoding="utf-8",
        )
        result = ci.refresh_file("mod.py")
        assert result["status"] == "patched"
        added = ci.find_symbols(name="added")["results"][0]
        assert ci.get_callees(added["symbol_id"], policy="strict")["metadata"]["index_status"] == "patched"
        assert ci.status()["index_status"] == "patched"


def test_refresh_file_semgrep_returns_the_current_findings(code_home, tmp_path, monkeypatch):
    """ADR-15 D1: ``semgrep=true`` is a live scan; its findings are returned, never stored."""
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    root = _repo(tmp_path)
    finding = {
        "path": "mod.py", "rule_id": "python.lang.security.eval", "severity": "error",
        "message": "Unsafe use of eval.", "start_line": 1, "start_column": 1,
        "end_line": 1, "end_column": 5,
    }
    monkeypatch.setattr(
        "hermes_codegraph.analysis.semgrep_scan",
        lambda *a, **k: {"status": "ok", "findings": [finding]},
    )
    limits = CodeGraphLimits(semgrep_enabled=True, semgrep_mode="auto")
    with CodeIntelligence.open(root=root, limits=limits) as ci:
        ci.index()
        (root / "mod.py").write_text("eval('1')\n", encoding="utf-8")
        result = ci.refresh_file("mod.py", semgrep=True)
        diagnostics = ci.get_diagnostics()

    assert result["semgrep"]["status"] == "ok"
    assert result["semgrep"]["results"][0]["code"] == "python.lang.security.eval"
    assert result["semgrep"]["results"][0]["file_path"] == "mod.py"
    assert result["semgrep"]["results"][0]["line_number"] == 1
    assert diagnostics["results"] == []  # the live scan wrote no CodeDiagnostic rows


def test_refresh_file_semgrep_reports_disabled_without_a_scan(code_home, tmp_path, monkeypatch):
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    root = _repo(tmp_path)
    calls = []
    monkeypatch.setattr("hermes_codegraph.analysis.semgrep_scan",
                        lambda *a, **k: calls.append(1) or {"status": "ok", "findings": []})
    with CodeIntelligence.open(root=root, limits=CodeGraphLimits()) as ci:
        ci.index()
        result = ci.refresh_file("mod.py", semgrep=True)

    assert result["semgrep"] == {"status": "disabled", "findings": 0}
    assert calls == []  # the master switch is off: `refresh_file(semgrep=true)` does not scan


def test_apply_evidence_rejects_bad_input_and_writes_nothing(code_home, tmp_path):
    from hermes_codegraph.exceptions import EvidenceRejectedError, GraphConflictError

    root = _repo(tmp_path)
    with _open(root) as ci:
        ci.index()
        helper = ci.find_symbols(name="helper")["results"][0]
        base = {
            "kind": "call",
            "source_id": helper["symbol_id"],
            "target_id": helper["symbol_id"],
        }
        with pytest.raises(EvidenceRejectedError):
            ci.apply_evidence(
                "mod.py",
                [{**base, "evidence": {**_evidence(root / "mod.py", 2), "source_tool": "model_observation"}}],
                expected_revision_id=ci.repository.revision_key,
                expected_content_hash=helper["file_content_hash"],
            )
        with pytest.raises(GraphConflictError):
            ci.apply_evidence(
                "mod.py",
                [{**base, "evidence": _evidence(root / "mod.py", 2)}],
                expected_revision_id=ci.repository.revision_key,
                expected_content_hash="wrong",
            )
        assert ci.get_callees(helper["symbol_id"], policy="exploratory")["results"] == []


def test_apply_evidence_conflict_does_not_overwrite_resolved(code_home, tmp_path):
    from hermes_codegraph.exceptions import GraphConflictError

    root = make_repo(tmp_path / "repo")
    (root / "mod.py").write_text(
        "def a():\n    return 1\n\ndef b():\n    return 2\n", encoding="utf-8"
    )
    commit(root)
    with _open(root) as ci:
        ci.index()
        a = ci.find_symbols(name="a")["results"][0]
        b = ci.find_symbols(name="b")["results"][0]
        evidence = _evidence(root / "mod.py", 2)
        result = ci.apply_evidence(
            "mod.py",
            [{
                "kind": "call",
                "source_id": a["symbol_id"],
                "target_id": b["symbol_id"],
                "resolution_status": "resolved",
                "evidence": evidence,
            }],
            expected_revision_id=ci.repository.revision_key,
            expected_content_hash=a["file_content_hash"],
        )
        assert result["updated"] == 1
        conflict = ci.apply_evidence(
            "mod.py",
            [{
                "kind": "call",
                "source_id": a["symbol_id"],
                "target_id": b["symbol_id"],
                "resolution_status": "inferred",
                "evidence": {**evidence, "excerpt_hash": evidence["excerpt_hash"]},
            }],
            expected_revision_id=ci.repository.revision_key,
            expected_content_hash=a["file_content_hash"],
        )
        assert conflict["conflicts"]
        callees = ci.get_callees(a["symbol_id"], policy="exploratory")["results"]
        assert callees[0]["edge"]["resolution_status"] == "ambiguous"
        with pytest.raises(GraphConflictError):
            ci.apply_evidence(
                "mod.py",
                [{"kind": "call", "source_id": a["symbol_id"], "target_id": b["symbol_id"],
                  "evidence": evidence}],
                expected_revision_id="other",
                expected_content_hash=a["file_content_hash"],
            )


def test_invalidate_removes_edge_without_valid_evidence(code_home, tmp_path):
    from hermes_graph.serialization import derived_id

    root = make_repo(tmp_path / "repo")
    (root / "mod.py").write_text(
        "def a():\n    return 1\n\ndef b():\n    return 2\n", encoding="utf-8"
    )
    commit(root)
    with _open(root) as ci:
        ci.index()
        a = ci.find_symbols(name="a")["results"][0]
        b = ci.find_symbols(name="b")["results"][0]
        result = ci.apply_evidence(
            "mod.py",
            [{
                "kind": "call",
                "source_id": a["symbol_id"],
                "target_id": b["symbol_id"],
                "evidence": _evidence(root / "mod.py", 2),
            }],
            expected_revision_id=ci.repository.revision_key,
            expected_content_hash=a["file_content_hash"],
        )
        assert result["updated"] == 1
        a_node = ci._client.nodes.get(derived_id("CodeSymbol", a["symbol_id"]))
        evidence_ids = []
        for edge in ci._client.edges.list(
            source_id=a_node.id, type="CODE_CALLS", limit=10
        ).items:
            evidence_ids.extend(edge.properties.get("evidence_ids") or [])
        assert evidence_ids
        invalidated = ci.invalidate(evidence_ids, reason="stale")
        assert invalidated["removed"] >= 1
