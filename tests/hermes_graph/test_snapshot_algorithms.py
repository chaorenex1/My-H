"""Snapshot versioning, MultiDiGraph shape, and deterministic algorithms."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hermes_graph import (
    AlgorithmError,
    EdgeType,
    GraphClient,
    GraphLimits,
    NodeType,
    SnapshotTooLargeError,
)


@pytest.fixture
def linked(knowledge):
    knowledge.schema.register_edge(
        EdgeType(
            name="RELATES_TO",
            source="Entity",
            target="Entity",
            scope="knowledge",
        )
    )
    memory = knowledge.nodes.create(type="Memory", properties={"content": "m"})
    first = knowledge.nodes.upsert(
        type="Entity", key="entity:a", properties={"name": "A"}
    )
    second = knowledge.nodes.upsert(
        type="Entity", key="entity:b", properties={"name": "B"}
    )
    knowledge.edges.create(
        source_id=memory.id, target_id=first.id, type="MENTIONS"
    )
    knowledge.edges.create(
        source_id=first.id, target_id=second.id, type="RELATES_TO"
    )
    return knowledge, memory, first, second


def test_snapshot_version_matches_scope_version(linked):
    knowledge, *_ = linked
    snapshot = knowledge.index.snapshot("knowledge")
    assert snapshot.version == knowledge.ctx.scope_version("knowledge")
    assert snapshot.scope == "knowledge"
    assert {node.type for node in snapshot.nodes} == {"Memory", "Entity"}
    assert all(
        node.type in {"Memory", "Entity"} for node in snapshot.nodes
    )


def test_snapshot_excludes_internal_tables(linked):
    knowledge, *_ = linked
    snapshot = knowledge.index.snapshot("knowledge")
    assert all(not node.type.startswith("Graph") for node in snapshot.nodes)


def test_multidigraph_keeps_parallel_relation_types(linked):
    knowledge, _, first, second = linked
    knowledge.schema.register_edge(
        EdgeType(
            name="SIMILAR_TO",
            source="Entity",
            target="Entity",
            scope="knowledge",
        )
    )
    knowledge.edges.create(
        source_id=first.id, target_id=second.id, type="SIMILAR_TO"
    )
    graph = knowledge.index.nx_graph("knowledge")
    assert graph.is_multigraph()
    relations = {
        data["relation"]
        for data in graph.get_edge_data(first.id, second.id).values()
    }
    assert relations == {"RELATES_TO", "SIMILAR_TO"}


def test_snapshot_cache_tracks_scope_version(linked):
    knowledge, *_ = linked
    first = knowledge.index.snapshot("knowledge")
    cached = knowledge.index.snapshot("knowledge")
    assert cached is first
    knowledge.nodes.create(type="Memory", properties={"content": "new"})
    rebuilt = knowledge.index.snapshot("knowledge")
    assert rebuilt.version == first.version + 1
    assert len(rebuilt.nodes) == len(first.nodes) + 1


def test_snapshot_limit_rejects(graph_home):
    limits = replace(GraphLimits(), max_snapshot_nodes=1)
    client = GraphClient.open(limits=limits)
    try:
        client.schema.register_node(NodeType(name="Memory", scope="knowledge"))
        client.nodes.create(type="Memory", properties={"content": "a"})
        client.nodes.create(type="Memory", properties={"content": "b"})
        with pytest.raises(SnapshotTooLargeError):
            client.index.snapshot("knowledge")
    finally:
        client.close()


def test_algorithms_are_deterministic(linked):
    knowledge, *_ = linked
    first = knowledge.algorithms.centrality("pagerank", scope="knowledge")
    second = knowledge.algorithms.centrality("pagerank", scope="knowledge")
    assert first == second
    assert list(first) == sorted(first)
    assert knowledge.algorithms.centrality("betweenness", scope="knowledge")
    assert knowledge.algorithms.centrality("closeness", scope="knowledge")
    communities_a = knowledge.algorithms.community("knowledge")
    communities_b = knowledge.algorithms.community("knowledge")
    assert communities_a == communities_b


def test_connected_components_sorted(linked):
    knowledge, *_ = linked
    components = knowledge.algorithms.connected_components("knowledge")
    assert components == sorted(components)
    assert components


def test_topological_sort_cycle_raises(knowledge):
    knowledge.schema.register_node(NodeType(name="CodeSymbol", scope="code"))
    knowledge.schema.register_edge(
        EdgeType(
            name="CALLS",
            source="CodeSymbol",
            target="CodeSymbol",
            scope="code",
        )
    )
    first = knowledge.nodes.create(type="CodeSymbol", properties={"name": "a"})
    second = knowledge.nodes.create(type="CodeSymbol", properties={"name": "b"})
    knowledge.edges.create(
        source_id=first.id, target_id=second.id, type="CALLS"
    )
    knowledge.edges.create(
        source_id=second.id, target_id=first.id, type="CALLS"
    )
    with pytest.raises(AlgorithmError):
        knowledge.algorithms.topological_sort("code")


def test_topological_sort_acyclic(knowledge):
    knowledge.schema.register_node(NodeType(name="CodeSymbol", scope="code"))
    knowledge.schema.register_edge(
        EdgeType(
            name="CALLS",
            source="CodeSymbol",
            target="CodeSymbol",
            scope="code",
        )
    )
    first = knowledge.nodes.create(type="CodeSymbol", properties={"name": "a"})
    second = knowledge.nodes.create(type="CodeSymbol", properties={"name": "b"})
    knowledge.edges.create(
        source_id=first.id, target_id=second.id, type="CALLS"
    )
    order = knowledge.algorithms.topological_sort("code")
    assert order.index(first.id) < order.index(second.id)


def test_traverse_shortest_path(linked):
    knowledge, memory, first, second = linked
    path = knowledge.traverse.shortest_path(memory.id, second.id)
    assert path is not None
    assert [node.id for node in path.nodes] == [memory.id, first.id, second.id]
    assert path.length == 2
    assert knowledge.traverse.shortest_path(second.id, memory.id) is None


def test_query_neighbors_excludes_start(linked):
    knowledge, memory, first, _ = linked
    neighbours = knowledge.query.neighbors(memory.id, depth=1)
    assert [node.id for node in neighbours.items] == [first.id]
    assert [node.id for node in knowledge.traverse.bfs(memory.id, 1)] == [
        memory.id,
        first.id,
    ]


def test_traverse_subgraph(linked):
    knowledge, memory, first, second = linked
    subgraph = knowledge.traverse.subgraph([first.id], max_depth=1)
    assert {node.id for node in subgraph.nodes} == {memory.id, first.id, second.id}
    assert len(subgraph.edges) == 2
