"""Optional-extra degradation: missing ladybug vs missing networkx."""

from __future__ import annotations

import sys

import pytest

from hermes_graph import (
    AlgorithmUnavailableError,
    GraphClient,
    GraphUnavailableError,
    NodeType,
)


def test_import_still_works_without_backends(graph_home, monkeypatch):
    monkeypatch.setitem(sys.modules, "ladybug", None)
    with pytest.raises(GraphUnavailableError):
        GraphClient.open()


def test_missing_networkx_degrades_algorithms_only(graph, monkeypatch):
    graph.schema.register_node(NodeType(name="Memory", scope="knowledge"))
    graph.nodes.create(type="Memory", properties={"content": "x"})
    monkeypatch.setitem(sys.modules, "networkx", None)
    capabilities = graph.capabilities()
    assert capabilities.algorithms == ()
    assert "property_graph" in capabilities.storage
    assert "snapshot" in capabilities.features
    snapshot = graph.index.snapshot("knowledge")
    assert len(snapshot.nodes) == 1
    with pytest.raises(AlgorithmUnavailableError):
        graph.algorithms.degree("knowledge")
