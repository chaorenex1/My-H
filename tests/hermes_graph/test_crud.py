"""CRUD, upsert identity, and soft/hard delete semantics (ADR-7 D3/D4)."""

from __future__ import annotations

import uuid

import pytest

from hermes_graph import PropertySizeError, ValidationError
from hermes_graph.serialization import derived_id


def test_create_uses_uuid7(knowledge):
    node = knowledge.nodes.create(type="Memory", properties={"content": "hello"})
    parsed = uuid.UUID(node.id)
    assert parsed.version == 7
    assert node.key is None
    assert node.created_at == node.updated_at


def test_upsert_is_idempotent_by_type_and_key(knowledge):
    first = knowledge.nodes.upsert(
        type="Entity", key="entity:python", properties={"name": "Python"}
    )
    second = knowledge.nodes.upsert(
        type="Entity", key="entity:python", properties={"name": "Python 3"}
    )
    assert first.id == second.id == derived_id("Entity", "entity:python")
    assert first.created_at == second.created_at
    assert second.properties["name"] == "Python 3"
    assert len(knowledge.query.find("Entity").items) == 1


def test_get_update_and_search(knowledge):
    node = knowledge.nodes.upsert(
        type="Entity",
        key="entity:python",
        properties={"name": "Python", "entity_type": "Language"},
    )
    updated = knowledge.nodes.update(node.id, {"entity_type": "ProgrammingLanguage"})
    assert updated.properties["entity_type"] == "ProgrammingLanguage"
    assert updated.properties["name"] == "Python"

    result = knowledge.query.find(
        "Entity", {"entity_type": "ProgrammingLanguage"}
    )
    assert [item.id for item in result.items] == [node.id]
    assert knowledge.nodes.get(node.id).properties["name"] == "Python"


def test_update_removes_none_properties(knowledge):
    node = knowledge.nodes.create(
        type="Memory", properties={"content": "x", "memory_type": "episodic"}
    )
    updated = knowledge.nodes.update(node.id, {"memory_type": None})
    assert "memory_type" not in updated.properties


def test_soft_delete_filters_reads_and_clears_edges(knowledge):
    memory = knowledge.nodes.create(type="Memory", properties={"content": "m"})
    entity = knowledge.nodes.upsert(
        type="Entity", key="entity:e", properties={"name": "E"}
    )
    edge = knowledge.edges.create(
        source_id=memory.id, target_id=entity.id, type="MENTIONS"
    )

    knowledge.nodes.delete(memory.id, hard=False)
    assert knowledge.nodes.get(memory.id) is None
    assert knowledge.edges.get(edge.id) is None
    assert knowledge.query.find("Memory").items == []
    assert knowledge.edges.list(source_id=memory.id).items == []


def test_soft_deleted_upsert_revives_same_identity(knowledge):
    first = knowledge.nodes.upsert(
        type="Entity", key="entity:revive", properties={"name": "A"}
    )
    knowledge.nodes.delete(first.id)
    revived = knowledge.nodes.upsert(
        type="Entity", key="entity:revive", properties={"name": "B"}
    )
    assert revived.id == first.id
    assert revived.deleted_at is None
    assert revived.properties["name"] == "B"
    assert len(knowledge.query.find("Entity").items) == 1


def test_hard_delete_then_upsert_recreates_one_node(knowledge):
    first = knowledge.nodes.upsert(
        type="Entity", key="entity:hard", properties={"name": "A"}
    )
    knowledge.nodes.delete(first.id, hard=True)
    assert knowledge.nodes.get(first.id) is None
    recreated = knowledge.nodes.upsert(
        type="Entity", key="entity:hard", properties={"name": "B"}
    )
    assert recreated.id == first.id
    assert len(knowledge.query.find("Entity").items) == 1


def test_delete_missing_node_raises(knowledge):
    from hermes_graph import NotFoundError

    with pytest.raises(NotFoundError):
        knowledge.nodes.delete(derived_id("Entity", "entity:missing"))


def test_create_rejects_key_and_upsert_requires_key(knowledge):
    with pytest.raises(ValidationError):
        knowledge.nodes.create(
            type="Entity", key="entity:x", properties={"name": "x"}
        )
    with pytest.raises(ValidationError):
        knowledge.nodes.upsert(type="Entity", key="", properties={})


def test_property_validation(knowledge):
    with pytest.raises(ValidationError):
        knowledge.nodes.create(type="Memory", properties={"content": b"bytes"})
    with pytest.raises(ValidationError):
        knowledge.nodes.create(type="Memory", properties={"content": float("nan")})
    with pytest.raises(ValidationError):
        knowledge.nodes.create(type="Memory", properties={"key": "reserved"})
    with pytest.raises(ValidationError):
        knowledge.nodes.upsert(
            type="Entity", key="entity:bad", properties={"name": 42}
        )


def test_property_size_limit(knowledge):
    oversized = "x" * (knowledge.limits.max_properties_bytes + 1)
    with pytest.raises(PropertySizeError):
        knowledge.nodes.create(type="Memory", properties={"content": oversized})


def test_typed_fields_round_trip(graph):
    from hermes_graph import NodeType

    graph.schema.register_node(
        NodeType(
            name="Score",
            scope="scores",
            fields={"count": "INT64", "ratio": "DOUBLE", "flag": "BOOL"},
        )
    )
    node = graph.nodes.create(
        type="Score", properties={"count": 3, "ratio": 0.25, "flag": True}
    )
    stored = graph.nodes.get(node.id)
    assert stored.properties == {"count": 3, "ratio": 0.25, "flag": True}
    with pytest.raises(ValidationError):
        graph.nodes.create(type="Score", properties={"count": "3"})
    with pytest.raises(ValidationError):
        graph.nodes.create(type="Score", properties={"flag": 1})


def test_search_rejects_unknown_filter(knowledge):
    with pytest.raises(ValidationError):
        knowledge.query.find("Memory", {"not_a_field": "x"})


def test_edges_list_filters_and_type(knowledge):
    memory = knowledge.nodes.create(type="Memory", properties={"content": "m"})
    entity = knowledge.nodes.upsert(
        type="Entity", key="entity:list", properties={"name": "E"}
    )
    first = knowledge.edges.create(
        source_id=memory.id, target_id=entity.id, type="MENTIONS"
    )
    second = knowledge.edges.create(
        source_id=memory.id, target_id=entity.id, type="MENTIONS"
    )
    outgoing = knowledge.edges.list(source_id=memory.id, type="MENTIONS")
    assert {edge.id for edge in outgoing.items} == {first.id, second.id}
    incoming = knowledge.edges.list(target_id=entity.id, type="MENTIONS")
    assert {edge.id for edge in incoming.items} == {first.id, second.id}
    all_edges = knowledge.edges.list(type="MENTIONS")
    assert {edge.id for edge in all_edges.items} == {first.id, second.id}


def test_edge_endpoint_type_mismatch(knowledge):
    from hermes_graph import SchemaError

    memory = knowledge.nodes.create(type="Memory", properties={"content": "m"})
    with pytest.raises(SchemaError):
        knowledge.edges.create(
            source_id=memory.id, target_id=memory.id, type="MENTIONS"
        )
