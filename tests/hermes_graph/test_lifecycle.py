"""Profile-scoped clients, fixed paths, read-only mode, and file locks."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_key_cache
from hermes_graph import (
    GraphClient,
    GraphLockedError,
    GraphReadOnlyError,
    NodeType,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_same_process_open_returns_same_client(graph_home):
    first = GraphClient.open()
    second = GraphClient.open()
    assert first is second
    assert first.read_only is False
    first.close()


def test_writer_can_open_after_read_only_client_closes(graph_home):
    writer = GraphClient.open()
    writer.close()
    reader = GraphClient.open(read_only=True)
    reader.close()
    writer = GraphClient.open()
    try:
        assert writer.read_only is False
    finally:
        writer.close()


def test_open_after_admin_close_creates_a_new_client(graph_home):
    first = GraphClient.open()
    first.admin.close()
    second = GraphClient.open()
    try:
        assert second is not first
        assert second.health().status == "ok"
    finally:
        second.close()


def test_database_path_is_fixed_under_home(graph_home):
    client = GraphClient.open()
    try:
        assert client.path == graph_home / "graph" / "graph.lbdb"
        assert client.path.exists()
    finally:
        client.close()


def test_close_idle_closes_idle_clients(graph_home):
    client = GraphClient.open()
    client.ctx.last_used = 0
    closed = GraphClient.close_idle(ttl_s=1)
    assert closed == [str(client.path)]
    reopened = GraphClient.open()
    try:
        assert reopened is not client
    finally:
        reopened.close()


def test_two_homes_are_isolated(tmp_path, monkeypatch):
    from hermes_graph import EdgeType

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    reset_hermes_home_key_cache()
    a = GraphClient.open()
    a.schema.register_node(NodeType(name="Memory", scope="knowledge"))
    a.schema.register_node(NodeType(name="Entity", scope="knowledge"))
    a.schema.register_edge(
        EdgeType(
            name="MENTIONS", source="Memory", target="Entity", scope="knowledge"
        )
    )
    a.nodes.create(type="Memory", properties={"content": "only in a"})
    a_path = a.path

    monkeypatch.setenv("HERMES_HOME", str(home_b))
    reset_hermes_home_key_cache()
    b = GraphClient.open()
    try:
        assert b.path != a_path
        assert b.schema.list_types() == []
        a.close()
        b.schema.register_node(NodeType(name="Memory", scope="knowledge"))
        assert b.query.find("Memory").items == []
        b.nodes.create(type="Memory", properties={"content": "only in b"})
        assert len(b.query.find("Memory").items) == 1
    finally:
        b.close()
        GraphClient.close_all()
        reset_hermes_home_key_cache()


def test_read_only_client_rejects_writes(graph_home):
    writer = GraphClient.open()
    writer.schema.register_node(NodeType(name="Memory", scope="knowledge"))
    writer.nodes.create(type="Memory", properties={"content": "read me"})
    writer.close()

    reader = GraphClient.open(read_only=True)
    try:
        assert reader.read_only
        assert len(reader.query.find("Memory").items) == 1
        with pytest.raises(GraphReadOnlyError):
            reader.nodes.create(type="Memory", properties={"content": "nope"})
        with pytest.raises(GraphLockedError):
            GraphClient.open()
    finally:
        reader.close()


def test_cross_process_writer_is_locked(graph_home):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(graph_home)
    code = (
        "import sys\n"
        "from hermes_graph import GraphClient\n"
        "client = GraphClient.open()\n"
        "print('ready', flush=True)\n"
        "sys.stdin.readline()\n"
        "client.close()\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert child.stdout is not None
        ready = child.stdout.readline().strip()
        assert ready == "ready", child.stderr.read() if child.stderr else ready
        GraphClient.close_all()
        with pytest.raises(GraphLockedError):
            GraphClient.open()
    finally:
        if child.poll() is None:
            assert child.stdin is not None
            child.stdin.write("\n")
            child.stdin.flush()
        child.wait(timeout=30)
    client = GraphClient.open()
    client.close()
