"""Optional analysis adapters: ast-grep and Semgrep availability/parsing."""

from __future__ import annotations

from pathlib import Path

from hermes_codegraph import analysis


def test_structural_search_reports_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(analysis, "_which", lambda tool: None)
    result = analysis.structural_search(tmp_path, "$A", limit=5)
    assert result["status"] == "unavailable"
    assert result["install_hint"]


def test_semgrep_missing_is_skipped_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setattr(analysis, "_which", lambda tool: None)
    result = analysis.semgrep_scan(
        tmp_path, ["mod.py"], mode="auto", home=tmp_path
    )
    assert result["status"] == "unavailable"
    assert result["findings"] == []


def test_semgrep_custom_dir_missing_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(analysis, "_which", lambda tool: "/usr/bin/semgrep")
    result = analysis.semgrep_scan(
        tmp_path, ["mod.py"], mode="custom_dir", home=tmp_path
    )
    assert result["status"] == "skipped"
    assert "custom_dir" in result["reason"]
