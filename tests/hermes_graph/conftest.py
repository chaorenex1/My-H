"""Fixtures for the core graph subsystem tests."""

from __future__ import annotations

import pytest

pytest.importorskip("ladybug", reason="the [graph] extra is not installed")

from hermes_constants import reset_hermes_home_key_cache


@pytest.fixture
def graph_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    reset_hermes_home_key_cache()
    from hermes_graph import GraphClient

    GraphClient.close_all()
    yield home
    GraphClient.close_all()
    reset_hermes_home_key_cache()


@pytest.fixture
def graph(graph_home):
    from hermes_graph import GraphClient

    client = GraphClient.open()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def knowledge(graph):
    from hermes_graph import EdgeType, NodeType

    graph.schema.register_node(
        NodeType(
            name="Memory",
            scope="knowledge",
            fields={"content": "STRING", "memory_type": "STRING"},
        )
    )
    graph.schema.register_node(
        NodeType(
            name="Entity",
            scope="knowledge",
            fields={"name": "STRING", "entity_type": "STRING"},
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
    return graph
