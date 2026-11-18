"""Physical schema for the six code graphs (``code`` scope).

Type names are prefixed (``Code*`` / ``CODE_*``) because ``Symbol``, ``File``
and ``CONTAINS`` are generic words other graphs in the same profile are
expected to reuse. Registration is additive and idempotent.
"""

from __future__ import annotations

from typing import Any

from hermes_graph.schema.definitions import EdgeType, NodeType

SCOPE = "code"
CODEGRAPH_SCHEMA_VERSION = "1.0"

NODE_TYPES: tuple[NodeType, ...] = (
    NodeType(
        name="CodeRepository",
        scope=SCOPE,
        fields={
            "repository_id": "STRING",
            "name": "STRING",
            "remote_url": "STRING",
            "schema_version": "STRING",
            "index_status": "STRING",
            "snapshot_hash": "STRING",
            "indexed_at": "STRING",
            "commit_hash": "STRING",
            "is_git": "BOOL",
        },
    ),
    NodeType(
        name="CodeRevision",
        scope=SCOPE,
        fields={
            "repository_id": "STRING",
            "commit_hash": "STRING",
            "branch": "STRING",
            "is_dirty": "BOOL",
            "status": "STRING",
            "snapshot_hash": "STRING",
            "indexed_at": "STRING",
        },
    ),
    NodeType(
        name="CodeFile",
        scope=SCOPE,
        fields={
            "repository_id": "STRING",
            "path": "STRING",
            "language": "STRING",
            "content_hash": "STRING",
            "indexed_hash": "STRING",
            "index_status": "STRING",
            "parse_status": "STRING",
            "parser_version": "STRING",
            "grammar_version": "STRING",
            "semantic_provider_version": "STRING",
            "indexed_revision": "STRING",
            "size_bytes": "INT64",
            "line_count": "INT64",
            "symbol_count": "INT64",
        },
    ),
    NodeType(
        name="CodeModule",
        scope=SCOPE,
        fields={
            "repository_id": "STRING",
            "language": "STRING",
            "qualified_name": "STRING",
            "path": "STRING",
        },
    ),
    NodeType(
        name="CodeSymbol",
        scope=SCOPE,
        fields={
            "repository_id": "STRING",
            "symbol_id": "STRING",
            "kind": "STRING",
            "name": "STRING",
            "qualified_name": "STRING",
            "language": "STRING",
            "file_path": "STRING",
            "start_line": "INT64",
            "end_line": "INT64",
            "start_column": "INT64",
            "end_column": "INT64",
            "content_hash": "STRING",
            "symbol_status": "STRING",
            "container_symbol": "STRING",
            "is_exported": "BOOL",
        },
    ),
    NodeType(
        name="CodePackage",
        scope=SCOPE,
        fields={
            "ecosystem": "STRING",
            "name": "STRING",
            "version": "STRING",
        },
    ),
    NodeType(
        name="CodeTestCase",
        scope=SCOPE,
        fields={
            "repository_id": "STRING",
            "path": "STRING",
            "qualified_name": "STRING",
            "language": "STRING",
            "name": "STRING",
        },
    ),
    NodeType(
        name="CodeAnalysisRun",
        scope=SCOPE,
        fields={
            "repository_id": "STRING",
            "kind": "STRING",
            "status": "STRING",
            "started_at": "STRING",
            "finished_at": "STRING",
            "target_revision": "STRING",
            "files_total": "INT64",
            "files_indexed": "INT64",
            "files_skipped": "INT64",
            "files_failed": "INT64",
            "snapshot_hash": "STRING",
            "error": "STRING",
        },
    ),
    NodeType(
        name="CodeDiagnostic",
        scope=SCOPE,
        fields={
            "repository_id": "STRING",
            "file_path": "STRING",
            "line_number": "INT64",
            "column_number": "INT64",
            "code": "STRING",
            "tool": "STRING",
            "severity": "STRING",
            "message": "STRING",
        },
    ),
    NodeType(
        name="CodeEvidence",
        scope=SCOPE,
        fields={
            "repository_id": "STRING",
            "evidence_kind": "STRING",
            "source_tool": "STRING",
            "source_tool_version": "STRING",
            "language": "STRING",
            "file_path": "STRING",
            "start_line": "INT64",
            "end_line": "INT64",
            "start_column": "INT64",
            "end_column": "INT64",
            "excerpt_hash": "STRING",
            "analysis_run_id": "STRING",
            "revision_id": "STRING",
            "observed_at": "STRING",
        },
    ),
)

_COMMON_EDGE_FIELDS: dict[str, str] = {
    "resolution_status": "STRING",
    "confidence": "DOUBLE",
    "resolution_method": "STRING",
    "confidence_method": "STRING",
    "change_kind": "STRING",
}

_EDGE_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("CODE_CONTAINS", "CodeRepository", "CodeFile"),
    ("CODE_DEFINES", "CodeFile", "CodeSymbol"),
    ("CODE_CONTAINS_SYMBOL", "CodeSymbol", "CodeSymbol"),
    ("CODE_BELONGS_TO", "CodeSymbol", "CodeModule"),
    ("CODE_IN_REVISION", "CodeFile", "CodeRevision"),
    ("CODE_REFERENCES", "CodeSymbol", "CodeSymbol"),
    ("CODE_CALLS", "CodeSymbol", "CodeSymbol"),
    ("CODE_READS", "CodeSymbol", "CodeSymbol"),
    ("CODE_WRITES", "CodeSymbol", "CodeSymbol"),
    ("CODE_INHERITS", "CodeSymbol", "CodeSymbol"),
    ("CODE_IMPLEMENTS", "CodeSymbol", "CodeSymbol"),
    ("CODE_OVERRIDES", "CodeSymbol", "CodeSymbol"),
    ("CODE_INSTANTIATES", "CodeSymbol", "CodeSymbol"),
    ("CODE_RETURNS_TYPE", "CodeSymbol", "CodeSymbol"),
    ("CODE_HAS_TYPE", "CodeSymbol", "CodeSymbol"),
    ("CODE_IMPORTS", "CodeFile", "CodeModule"),
    ("CODE_DEPENDS_ON", "CodeModule", "CodeModule"),
    ("CODE_USES_PACKAGE", "CodeModule", "CodePackage"),
    ("CODE_EXPORTS", "CodeModule", "CodeSymbol"),
    ("CODE_REEXPORTS", "CodeModule", "CodeSymbol"),
    ("CODE_TESTS", "CodeTestCase", "CodeSymbol"),
    ("CODE_COVERS", "CodeTestCase", "CodeSymbol"),
    ("CODE_GENERATED_FROM", "CodeSymbol", "CodeSymbol"),
    ("CODE_ALIASES", "CodeSymbol", "CodeSymbol"),
    ("CODE_DECLARES", "CodeFile", "CodeModule"),
    ("CODE_PARENT_OF", "CodeRevision", "CodeRevision"),
    ("CODE_CHANGED_IN", "CodeSymbol", "CodeRevision"),
    ("CODE_DELETED_IN", "CodeSymbol", "CodeRevision"),
)

EDGE_TYPES: tuple[EdgeType, ...] = tuple(
    EdgeType(
        name=name,
        source=source,
        target=target,
        scope=SCOPE,
        fields=dict(_COMMON_EDGE_FIELDS),
    )
    for name, source, target in _EDGE_ENDPOINTS
)

#: Edges whose source is a File. Rebuild deletes all of them for the file.
FILE_SOURCE_EDGES = tuple(
    edge.name for edge in EDGE_TYPES if edge.source == "CodeFile"
)
#: Edges whose source is a Symbol (used when a symbol disappears).
SYMBOL_SOURCE_EDGES = tuple(
    edge.name for edge in EDGE_TYPES if edge.source == "CodeSymbol"
)
#: Edges whose target is a Symbol (incoming edges must also be removed).
SYMBOL_TARGET_EDGES = tuple(
    edge.name for edge in EDGE_TYPES if edge.target == "CodeSymbol"
)


def register(client: Any) -> dict[str, int]:
    """Register every code type. Idempotent; returns registration counts."""
    for node in NODE_TYPES:
        client.schema.register_node(node)
    for edge in EDGE_TYPES:
        client.schema.register_edge(edge)
    return {"nodes": len(NODE_TYPES), "edges": len(EDGE_TYPES)}


def missing_types(client: Any) -> list[str]:
    """Names of code types not present in the graph's registry."""
    missing: list[str] = []
    for node in NODE_TYPES:
        if client.schema.ctx.schema.maybe_node(node.name) is None:
            missing.append(node.name)
    for edge in EDGE_TYPES:
        if client.schema.ctx.schema.maybe_edge(edge.name) is None:
            missing.append(edge.name)
    return missing
