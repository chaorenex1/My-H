"""Repository-level snapshot filters (ADR-8 D2/D12)."""

from __future__ import annotations

import pytest

from hermes_graph import EdgeType, NodeType
from hermes_graph.exceptions import ValidationError


@pytest.fixture
def filtered(graph):
    graph.schema.register_node(
        NodeType(name="FNode", scope="filterscope", fields={"repo": "STRING"})
    )
    graph.schema.register_edge(
        EdgeType(
            name="F_REL",
            source="FNode",
            target="FNode",
            scope="filterscope",
        )
    )
    first = graph.nodes.upsert(
        "FNode", "a:1", {"repo": "a"}
    )
    second = graph.nodes.upsert(
        "FNode", "a:2", {"repo": "a"}
    )
    third = graph.nodes.upsert(
        "FNode", "b:1", {"repo": "b"}
    )
    graph.edges.upsert(first.id, second.id, "F_REL")
    graph.edges.upsert(second.id, first.id, "F_REL")
    graph.edges.upsert(third.id, third.id, "F_REL")
    return graph, first, second, third


def test_snapshot_filter_excludes_other_repositories(filtered):
    graph, first, second, _third = filtered
    snapshot = graph.index.snapshot(
        "filterscope", filters={"repo": "a"}
    )
    assert {node.id for node in snapshot.nodes} == {first.id, second.id}
    assert all(
        edge.source_id in {first.id, second.id} for edge in snapshot.edges
    )

    full = graph.index.snapshot("filterscope")
    assert len(full.nodes) == 3


def test_nx_graph_and_algorithms_accept_filters(filtered):
    graph, first, second, _third = filtered
    nx_graph = graph.index.nx_graph("filterscope", filters={"repo": "a"})
    assert set(nx_graph.nodes) == {first.id, second.id}
    cycles = graph.algorithms.cycles("filterscope", filters={"repo": "a"})
    assert len(cycles) == 1
    assert set(cycles[0]) == {first.id, second.id}
    with pytest.raises(ValidationError):
        graph.index.snapshot("filterscope", filters={"missing": "a"})


def test_filter_changes_cache_key(filtered):
    graph, first, second, third = filtered
    a = graph.index.snapshot("filterscope", filters={"repo": "a"})
    b = graph.index.snapshot("filterscope", filters={"repo": "b"})
    assert {node.id for node in a.nodes} == {first.id, second.id}
    assert {node.id for node in b.nodes} == {third.id}
