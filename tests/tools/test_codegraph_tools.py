"""Tool surface for the code graph: membership, gating and read-only queries."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_key_cache


@pytest.fixture
def code_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    reset_hermes_home_key_cache()
    from hermes_graph import GraphClient

    GraphClient.close_all()
    yield home
    GraphClient.close_all()
    reset_hermes_home_key_cache()


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=False)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@example.invalid"],
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "T"], check=False
    )
    (root / "mod.py").write_text(
        "def helper(x):\n    return x\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=False)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "init"], check=False
    )
    return root


def test_codegraph_toolset_membership():
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    assert TOOLSETS["codegraph"]["tools"] == ["code_intel", "code_graph_update"]
    assert "code_intel" in _HERMES_CORE_TOOLS
    assert "code_graph_update" in _HERMES_CORE_TOOLS


def test_check_fns_gate_on_extras_and_config(monkeypatch):
    import tools.code_graph_update_tool as update_tool
    import tools.code_intel_tool as read_tool

    monkeypatch.setattr(
        "pm.extras.available", lambda extra: False
    )
    assert read_tool._codegraph_available() is False
    assert update_tool._update_available() is False

    monkeypatch.setattr("pm.extras.available", lambda extra: True)
    assert read_tool._codegraph_available() is True
    assert update_tool._update_available() is True

    class _Limits:
        enabled = True
        update = False

    monkeypatch.setattr(
        "hermes_codegraph.config.CodeGraphLimits.from_config",
        classmethod(lambda cls, config=None: _Limits()),
    )
    assert read_tool._codegraph_available() is True
    assert update_tool._update_available() is False


def test_code_intel_reports_not_indexed(code_home, tmp_path):
    import tools.code_intel_tool as read_tool

    root = _repo(tmp_path / "repo")
    payload = json.loads(
        read_tool.code_intel_tool({"action": "status", "root": str(root)})
    )
    assert payload["status"] == "not_indexed"
    assert "hermes code index" in payload["hint"]


def test_code_intel_queries_an_indexed_repo(code_home, tmp_path):
    import tools.code_intel_tool as read_tool
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    root = _repo(tmp_path / "repo")
    with CodeIntelligence.open(root=root, limits=CodeGraphLimits()) as ci:
        ci.index()
    payload = json.loads(
        read_tool.code_intel_tool(
            {"action": "find_symbols", "name": "helper", "root": str(root)}
        )
    )
    assert payload["schema_version"] == "1.0"
    assert payload["results"][0]["qualified_name"] == "mod.helper"
    assert payload["metadata"]["index_status"] == "complete"


def test_code_intel_diagnostics_scans_a_named_path_live(code_home, tmp_path, monkeypatch):
    """ADR-9 D8: an explicit ``diagnostics`` query on a named path scans the current bytes; the
    stored graph is only a fallback, and the payload says which one answered."""
    import tools.code_intel_tool as read_tool
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    root = _repo(tmp_path / "repo")
    with CodeIntelligence.open(root=root, limits=CodeGraphLimits()) as ci:
        ci.index()
    monkeypatch.setattr(
        "hermes_codegraph.analysis.semgrep_scan",
        lambda *a, **k: {"status": "ok", "findings": [{
            "path": "mod.py", "rule_id": "python.lang.security.eval", "severity": "error",
            "message": "Unsafe use of eval.", "start_line": 2, "start_column": 5,
            "end_line": 2, "end_column": 9,
        }]},
    )
    monkeypatch.setattr(
        "hermes_codegraph.config.CodeGraphLimits.from_config",
        classmethod(lambda cls, config=None: CodeGraphLimits(semgrep_enabled=True)),
    )

    payload = json.loads(read_tool.code_intel_tool(
        {"action": "diagnostics", "path": "mod.py", "root": str(root)}))

    assert payload["source"] == "live_scan"
    assert payload["results"][0]["code"] == "python.lang.security.eval"
    assert payload["results"][0]["line_number"] == 2


def test_code_intel_diagnostics_without_a_path_reports_stored_graph(code_home, tmp_path):
    import tools.code_intel_tool as read_tool
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    root = _repo(tmp_path / "repo")
    with CodeIntelligence.open(root=root, limits=CodeGraphLimits()) as ci:
        ci.index()

    payload = json.loads(read_tool.code_intel_tool(
        {"action": "diagnostics", "root": str(root)}))

    assert payload["source"] == "stored_graph"


def test_code_graph_update_refresh_file(code_home, tmp_path):
    import tools.code_graph_update_tool as update_tool
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    root = _repo(tmp_path / "repo")
    with CodeIntelligence.open(root=root, limits=CodeGraphLimits()) as ci:
        ci.index()
    (root / "mod.py").write_text(
        "def helper(x):\n    return x\n\ndef added():\n    return 2\n",
        encoding="utf-8",
    )
    payload = json.loads(
        update_tool.code_graph_update_tool(
            {"action": "refresh_file", "path": "mod.py", "root": str(root)}
        )
    )
    assert payload["status"] == "patched"


def test_code_graph_update_refresh_file_returns_semgrep_findings(code_home, tmp_path, monkeypatch):
    """ADR-9 D8: ``refresh_file`` with ``semgrep: true`` hands the model the scan's findings (the
    indexer used to discard them)."""
    import tools.code_graph_update_tool as update_tool
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    root = _repo(tmp_path / "repo")
    with CodeIntelligence.open(root=root, limits=CodeGraphLimits()) as ci:
        ci.index()
    (root / "mod.py").write_text("x = eval('1')\n", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_codegraph.analysis.semgrep_scan",
        lambda *a, **k: {"status": "ok", "findings": [{
            "path": "mod.py", "rule_id": "python.lang.security.eval", "severity": "error",
            "message": "Unsafe use of eval.", "start_line": 1, "start_column": 5,
            "end_line": 1, "end_column": 9,
        }]},
    )
    monkeypatch.setattr(
        "hermes_codegraph.config.CodeGraphLimits.from_config",
        classmethod(lambda cls, config=None: CodeGraphLimits(semgrep_enabled=True)),
    )

    payload = json.loads(update_tool.code_graph_update_tool(
        {"action": "refresh_file", "path": "mod.py", "semgrep": True, "root": str(root)}))

    assert payload["status"] == "patched"
    assert payload["semgrep"]["findings"] == 1
    assert payload["semgrep"]["results"][0]["rule_id"] == "python.lang.security.eval"
