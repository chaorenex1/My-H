"""Schema registration, persistence, and additive-only migration."""

from __future__ import annotations

import pytest

from hermes_graph import EdgeType, GraphClient, NodeType, SchemaError, ValidationError


def test_registration_is_idempotent(graph):
    definition = NodeType(name="Memory", scope="knowledge", fields={"content": "STRING"})
    first = graph.schema.register_node(definition)
    second = graph.schema.register_node(definition)
    assert first == second
    assert len(graph.schema.list_types()) == 1


def test_schema_persists_across_reopen(graph_home):
    client = GraphClient.open()
    client.schema.register_node(NodeType(name="Memory", scope="knowledge"))
    client.close()
    reopened = GraphClient.open()
    try:
        assert [item["name"] for item in reopened.schema.list_types()] == ["Memory"]
    finally:
        reopened.close()


def test_additive_migration_adds_nullable_field(graph):
    graph.schema.register_node(
        NodeType(name="Memory", scope="knowledge", fields={"content": "STRING"})
    )
    graph.schema.register_node(
        NodeType(
            name="Memory",
            scope="knowledge",
            fields={"content": "STRING", "score": "DOUBLE"},
        )
    )
    node = graph.nodes.create(
        type="Memory", properties={"content": "x", "score": 1.5}
    )
    assert graph.nodes.get(node.id).properties["score"] == 1.5


def test_additive_edge_migration(graph):
    graph.schema.register_node(NodeType(name="Memory", scope="knowledge"))
    graph.schema.register_node(NodeType(name="Entity", scope="knowledge"))
    graph.schema.register_edge(
        EdgeType(
            name="MENTIONS", source="Memory", target="Entity", scope="knowledge"
        )
    )
    graph.schema.register_edge(
        EdgeType(
            name="MENTIONS",
            source="Memory",
            target="Entity",
            scope="knowledge",
            fields={"confidence": "DOUBLE"},
        )
    )
    memory = graph.nodes.create(type="Memory")
    entity = graph.nodes.create(type="Entity")
    edge = graph.edges.create(
        source_id=memory.id,
        target_id=entity.id,
        type="MENTIONS",
        properties={"confidence": 0.5},
    )
    assert graph.edges.get(edge.id).properties["confidence"] == 0.5


def test_destructive_migration_rejected(graph):
    graph.schema.register_node(
        NodeType(
            name="Memory",
            scope="knowledge",
            fields={"content": "STRING", "score": "DOUBLE"},
        )
    )
    with pytest.raises(SchemaError):
        graph.schema.register_node(
            NodeType(name="Memory", scope="knowledge", fields={"content": "STRING"})
        )
    with pytest.raises(SchemaError):
        graph.schema.register_node(
            NodeType(
                name="Memory",
                scope="knowledge",
                fields={"content": "INT64", "score": "DOUBLE"},
            )
        )
    with pytest.raises(SchemaError):
        graph.schema.register_node(
            NodeType(name="Memory", scope="other", fields={"content": "STRING"})
        )


def test_edge_registration_requires_endpoints(graph):
    with pytest.raises(SchemaError):
        graph.schema.register_edge(
            EdgeType(name="MENTIONS", source="Memory", target="Entity", scope="x")
        )
    graph.schema.register_node(NodeType(name="Memory", scope="x"))
    graph.schema.register_node(NodeType(name="Entity", scope="x"))
    edge = graph.schema.register_edge(
        EdgeType(name="MENTIONS", source="Memory", target="Entity", scope="x")
    )
    assert edge.source == "Memory"
    with pytest.raises(SchemaError):
        graph.schema.register_edge(
            EdgeType(name="MENTIONS", source="Entity", target="Memory", scope="x")
        )


def test_reserved_and_invalid_names_rejected(graph):
    with pytest.raises(ValidationError):
        NodeType(name="GraphNodeRef", scope="knowledge")
    with pytest.raises(ValidationError):
        NodeType(name="memory", scope="knowledge")
    with pytest.raises(ValidationError):
        NodeType(name="Memory", scope="NotAScope")
    with pytest.raises(ValidationError):
        NodeType(name="Memory", scope="knowledge", fields={"key": "STRING"})
    with pytest.raises(ValidationError):
        NodeType(
            name="Memory", scope="knowledge", fields={"content": "BLOB"}
        )


def test_newer_schema_version_is_rejected(graph):
    import json

    graph.ctx.schema_execute(
        "CREATE (s:GraphSchema {name:$name, kind:$kind, definition:$definition, version:$version})",
        {
            "name": "FutureType",
            "kind": "node",
            "definition": json.dumps(
                {"kind": "node", "name": "FutureType", "scope": "future", "fields": {}}
            ),
            "version": 999,
        },
    )
    graph.close()
    with pytest.raises(SchemaError):
        GraphClient.open()


def test_schema_register_payload(graph):
    registered = graph.schema.register(
        {
            "nodes": [
                {"kind": "node", "name": "Memory", "scope": "knowledge",
                 "fields": {"content": "STRING"}},
                {"kind": "node", "name": "Entity", "scope": "knowledge"},
            ],
            "edges": [
                {"kind": "edge", "name": "MENTIONS", "source": "Memory",
                 "target": "Entity", "scope": "knowledge"},
            ],
        }
    )
    assert [item.name for item in registered] == ["Memory", "Entity", "MENTIONS"]
