"""Native transactions, write serialisation, and scope version bumps."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from hermes_graph import (
    GraphClient,
    GraphLimits,
    NodeType,
    GraphReadOnlyError,
    TransactionBusyError,
    TransactionError,
)


def test_commit_persists_every_write(knowledge):
    with knowledge.transaction.begin() as tx:
        memory = tx.nodes.create(type="Memory", properties={"content": "a"})
        entity = tx.nodes.upsert(
            type="Entity", key="entity:tx", properties={"name": "E"}
        )
        tx.edges.create(source_id=memory.id, target_id=entity.id, type="MENTIONS")
    assert knowledge.nodes.get(memory.id) is not None
    assert len(knowledge.edges.list(source_id=memory.id).items) == 1


def test_exception_rolls_back_everything(knowledge):
    before = len(knowledge.query.find("Memory").items)
    with pytest.raises(TransactionError):
        with knowledge.transaction.begin() as tx:
            tx.nodes.create(type="Memory", properties={"content": "rolled back"})
            raise RuntimeError("boom")
    assert len(knowledge.query.find("Memory").items) == before


def test_nested_transaction_rejected(knowledge):
    with knowledge.transaction.begin() as tx:
        with pytest.raises(TransactionError):
            with knowledge.transaction.begin():
                pass
        tx.nodes.create(type="Memory", properties={"content": "outer"})


def test_read_only_transaction_rejects_writes(knowledge):
    with pytest.raises(GraphReadOnlyError):
        with knowledge.transaction.begin(read_only=True) as tx:
            tx.nodes.create(type="Memory", properties={"content": "nope"})


def test_write_lock_timeout(graph_home):
    limits = replace(GraphLimits(), write_lock_timeout_s=0.2)
    client = GraphClient.open(limits=limits)
    started = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with client.transaction.begin() as tx:
            tx.nodes.create(type="Memory", properties={"content": "holder"})
            started.set()
            release.wait(timeout=5)

    client.schema.register_node(NodeType(name="Memory", scope="knowledge"))
    thread = threading.Thread(target=hold_lock)
    thread.start()
    try:
        assert started.wait(timeout=5)
        with pytest.raises(TransactionBusyError):
            with client.transaction.begin():
                pass
    finally:
        release.set()
        thread.join(timeout=5)
        client.close()


def test_scope_versions_bump_on_commit_only(graph):
    graph.schema.register_node(NodeType(name="Memory", scope="knowledge"))
    graph.schema.register_node(NodeType(name="CodeSymbol", scope="code"))
    before = graph.ctx.scope_versions()

    with graph.transaction.begin() as tx:
        tx.nodes.create(type="Memory", properties={"content": "k"})
        tx.nodes.create(type="CodeSymbol", properties={"name": "f"})
    after = graph.ctx.scope_versions()
    assert after["knowledge"] == before.get("knowledge", 0) + 1
    assert after["code"] == before.get("code", 0) + 1

    with pytest.raises(TransactionError):
        with graph.transaction.begin() as tx:
            tx.nodes.create(type="Memory", properties={"content": "rolled"})
            raise RuntimeError("abort")
    assert graph.ctx.scope_versions() == after


def test_invalidate_does_not_touch_versions(knowledge):
    knowledge.nodes.create(type="Memory", properties={"content": "x"})
    before = knowledge.ctx.scope_versions()
    knowledge.index.invalidate("knowledge")
    assert knowledge.ctx.scope_versions() == before
