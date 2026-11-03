"""Indexing: extraction, evidence, idempotency, incremental reuse."""

from __future__ import annotations

from pathlib import Path

from hermes_graph.serialization import derived_id
from hermes_codegraph import indexer as indexer_module


def _symbols(client, repository_id: str, **filters):
    return client.nodes.search(
        "CodeSymbol", {"repository_id": repository_id, **filters}, limit=100
    ).items


def test_indexes_python_and_typescript_symbols(ci):
    result = ci.index()
    assert result["status"] == "complete"
    assert result["files_indexed"] == 3  # pkg/__init__.py, pkg/mod.py, src/app.ts
    assert result["files_failed"] == 0

    symbols = _symbols(ci._client, ci.repository.repository_id)
    by_qualified = {node.properties["qualified_name"]: node for node in symbols}
    assert by_qualified["pkg.mod.A"].properties["kind"] == "class"
    assert by_qualified["pkg.mod.A.f"].properties["kind"] == "method"
    assert by_qualified["pkg.mod.g"].properties["kind"] == "function"
    assert by_qualified["pkg.mod.VALUE"].properties["kind"] == "constant"
    assert by_qualified["src.app.B"].properties["kind"] == "class"
    assert by_qualified["src.app.h"].properties["kind"] == "function"
    assert by_qualified["src.app.link"].properties["kind"] == "function"

    method = by_qualified["pkg.mod.A.f"]
    assert method.properties["container_symbol"] == "pkg.mod.A"
    assert method.properties["start_line"] == 4
    assert method.properties["symbol_id"].startswith("sym_")


def test_evidence_has_spans_and_hashes_not_source(ci):
    ci.index()
    evidence = ci._client.nodes.search(
        "CodeEvidence", {"repository_id": ci.repository.repository_id}, limit=100
    ).items
    assert evidence
    for node in evidence:
        props = node.properties
        assert props["source_tool"] == "tree-sitter"
        assert props["excerpt_hash"]
        assert props["start_line"] >= 1
        assert props["start_column"] >= 1
        assert "source" not in props
        assert "text" not in props


def test_references_are_inferred_edges_with_evidence(ci):
    ci.index()
    symbol = ci.find_symbols(name="g")["results"][0]
    result = ci.get_references(symbol["symbol_id"])
    assert result["metadata"]["total"] == 1
    reference = result["results"][0]
    assert reference["qualified_name"] == "pkg.mod.A.f"
    edge = reference["edge"]
    assert edge["resolution_status"] == "inferred"
    assert edge["confidence"] == 0.5
    assert edge["evidence_ids"]


def test_reindex_is_idempotent_and_skips_unchanged_parses(ci, monkeypatch):
    ci.index()
    first = ci.status()
    before_ids = sorted(
        node.properties["symbol_id"]
        for node in _symbols(ci._client, ci.repository.repository_id)
    )

    calls = {"count": 0}
    original = indexer_module.extract_file

    def counted(source, descriptor, path):
        calls["count"] += 1
        return original(source, descriptor, path)

    monkeypatch.setattr(indexer_module, "extract_file", counted)
    second = ci.index()

    after = ci.status()
    assert calls["count"] == 0  # unchanged files are not re-parsed
    assert second["files_skipped"] == second["files_total"]
    assert (first["files"], first["symbols"], first["evidence"], first["edges"]) == (
        after["files"], after["symbols"], after["evidence"], after["edges"]
    )
    after_ids = sorted(
        node.properties["symbol_id"]
        for node in _symbols(ci._client, ci.repository.repository_id)
    )
    assert after_ids == before_ids


def test_only_changed_file_is_reparsed(ci, sample_repo: Path):
    ci.index()
    first = ci.status()
    target = sample_repo / "pkg" / "mod.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace("VALUE = 3", "VALUE = 4"),
        encoding="utf-8",
    )
    result = ci.index()
    assert result["files_indexed"] == 1
    assert result["files_skipped"] == result["files_total"] - 1
    after = ci.status()
    assert after["files"] == first["files"]
    value = _symbols(ci._client, ci.repository.repository_id, name="VALUE")[0]
    assert "VALUE = 4" in value.properties["signature"]


def test_file_keys_are_repository_scoped(ci, sample_repo: Path):
    ci.index()
    file_node = ci._client.nodes.get(
        derived_id("CodeFile", f"{ci.repository.key}@pkg/mod.py")
    )
    assert file_node is not None
    assert file_node.properties["indexed_hash"] == file_node.properties["content_hash"]
    assert file_node.properties["parser_version"].startswith("tree-sitter==")


def test_parse_failure_keeps_previous_symbols_and_recovers(ci, sample_repo: Path, monkeypatch):
    ci.index()
    before = {
        node.properties["qualified_name"]: node.properties["symbol_id"]
        for node in _symbols(ci._client, ci.repository.repository_id)
    }
    assert "pkg.mod.g" in before

    target = sample_repo / "pkg" / "mod.py"
    target.write_text(
        target.read_text(encoding="utf-8").replace("VALUE = 3", "VALUE = 5"),
        encoding="utf-8",
    )

    def boom(source, descriptor, path):
        raise RuntimeError("synthetic parser failure")

    original = indexer_module.extract_file
    monkeypatch.setattr(indexer_module, "extract_file", boom)
    result = ci.index()
    assert result["files_failed"] == 1
    assert result["parse_failures"][0]["path"] == "pkg/mod.py"

    after = {
        node.properties["qualified_name"]: node.properties["symbol_id"]
        for node in _symbols(ci._client, ci.repository.repository_id)
    }
    assert after["pkg.mod.g"] == before["pkg.mod.g"]
    file_node = ci._client.nodes.get(
        derived_id("CodeFile", f"{ci.repository.key}@pkg/mod.py")
    )
    assert file_node.properties["parse_status"] == "failed"
    assert file_node.properties["index_status"] == "stale"

    monkeypatch.setattr(indexer_module, "extract_file", original)
    recovered = ci.index()
    assert recovered["files_failed"] == 0
    assert recovered["files_indexed"] == 1
    file_node = ci._client.nodes.get(
        derived_id("CodeFile", f"{ci.repository.key}@pkg/mod.py")
    )
    assert file_node.properties["parse_status"] == "success"
    assert file_node.properties["index_status"] == "complete"


def test_symbol_ceiling_returns_partial_without_oob(code_home, sample_repo):
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    ci = CodeIntelligence.open(
        root=sample_repo, limits=CodeGraphLimits(max_symbols=1)
    )
    try:
        result = ci.index()
        assert result["status"] == "partial"
        assert result["snapshot_hash"] is None
    finally:
        ci.close()


def test_deleted_file_soft_deletes_symbols_and_prune_removes_them(
    code_home, sample_repo
):
    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits

    ci = CodeIntelligence.open(root=sample_repo, limits=CodeGraphLimits())
    try:
        ci.index()
        before = ci.status()["files"]
        target = sample_repo / "pkg" / "mod.py"
        target.unlink()
        result = ci.index()
        assert result["files_deleted"] == 1
        assert ci.status()["files"] == before - 1
        assert ci.find_symbols(name="g")["results"] == []
        assert ci.status()["deleted_symbols"] >= 1
        pruned = ci.prune(keep=50)
        assert pruned["symbols_removed"] >= 1
        assert ci.status()["deleted_symbols"] == 0
    finally:
        ci.close()
