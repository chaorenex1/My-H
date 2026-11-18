"""`hermes graph` CLI: JSON output, read-only query, writes, and fast path."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.subcommands.graph import build_graph_parser, cmd_graph
from hermes_graph import EdgeType, GraphClient, NodeType

REPO_ROOT = Path(__file__).resolve().parents[2]


def make_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_graph_parser(subparsers, cmd_graph=cmd_graph)
    return parser


def invoke(argv):
    args = make_parser().parse_args(argv)
    return args.func(args)


def seed(home) -> None:
    client = GraphClient.open()
    try:
        client.schema.register_node(
            NodeType(
                name="Memory",
                scope="knowledge",
                fields={"content": "STRING", "memory_type": "STRING"},
            )
        )
        client.schema.register_node(
            NodeType(name="Entity", scope="knowledge", fields={"name": "STRING"})
        )
        client.schema.register_edge(
            EdgeType(
                name="MENTIONS",
                source="Memory",
                target="Entity",
                scope="knowledge",
            )
        )
        memory = client.nodes.create(
            type="Memory",
            properties={"content": "cli", "memory_type": "semantic"},
        )
        entity = client.nodes.upsert(
            type="Entity", key="entity:cli", properties={"name": "CLI"}
        )
        client.edges.create(
            source_id=memory.id, target_id=entity.id, type="MENTIONS"
        )
    finally:
        client.close()


def test_help_does_not_import_backends():
    code = (
        "import sys, argparse;"
        "from hermes_cli.subcommands.graph import build_graph_parser, cmd_graph;"
        "p=argparse.ArgumentParser(); s=p.add_subparsers();"
        "build_graph_parser(s, cmd_graph=cmd_graph);"
        "assert 'hermes_graph' not in sys.modules;"
        "assert 'ladybug' not in sys.modules;"
        "assert 'networkx' not in sys.modules;"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_graph_is_a_builtin_subcommand(graph_home):
    code = (
        "import sys; sys.argv=['hermes','graph','--help']; "
        "from hermes_cli.main import _BUILTIN_SUBCOMMANDS, _plugin_cli_discovery_needed; "
        "print('graph' in _BUILTIN_SUBCOMMANDS, _plugin_cli_discovery_needed())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "HERMES_HOME": str(graph_home)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True False"


def test_status_reports_unavailable_without_installing(graph_home, monkeypatch, capsys):
    from pm import extras

    monkeypatch.setattr(extras, "available", lambda name: False)

    def boom(name):
        raise AssertionError("status must not install the extra")

    monkeypatch.setattr(extras, "ensure_import", boom)
    assert invoke(["graph", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unavailable"
    assert payload["extra"] == "graph"
    assert payload["lock"] == "not-probed"


def test_status_before_first_write(graph_home, capsys):
    assert invoke(["graph", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "uninitialized"
    assert payload["lock"] == "not-applicable"
    assert payload["path"].endswith("graph.lbdb")


def test_status_reports_locked_writer(graph_home, capsys):
    env = {**os.environ, "HERMES_HOME": str(graph_home)}
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
        assert child.stdout.readline().strip() == "ready"
        assert invoke(["graph", "status"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert payload["lock"] == "locked"
        assert payload["writable"] is False
    finally:
        if child.poll() is None:
            assert child.stdin is not None
            child.stdin.write("\n")
            child.stdin.flush()
        child.wait(timeout=30)


def test_node_upsert_get_search_and_delete(graph_home, capsys):
    seed(graph_home)
    assert invoke(
        [
            "graph",
            "node",
            "upsert",
            "Entity",
            "entity:cli2",
            "--property",
            "name=Second",
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)["node"]
    assert created["key"] == "entity:cli2"

    assert invoke(["graph", "node", "get", created["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["node"]["id"] == created["id"]

    assert invoke(["graph", "node", "search", "Memory", "--filter", "memory_type=semantic"]) == 0
    items = json.loads(capsys.readouterr().out)["items"]
    assert len(items) == 1

    assert invoke(["graph", "node", "delete", created["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == created["id"]
    assert invoke(["graph", "node", "get", created["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["node"] is None


def test_hard_delete_requires_yes(graph_home, capsys):
    seed(graph_home)
    client = GraphClient.open(read_only=True)
    node_id = client.query.find("Memory").items[0].id
    client.close()
    assert invoke(["graph", "node", "delete", node_id, "--hard"]) == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "validation_error"
    assert invoke(["graph", "node", "delete", node_id, "--hard", "--yes"]) == 0
    assert json.loads(capsys.readouterr().out)["hard"] is True
    assert invoke(["graph", "node", "get", node_id]) == 0
    assert json.loads(capsys.readouterr().out)["node"] is None


def test_query_is_parameterised_and_read_only(graph_home, capsys):
    seed(graph_home)
    statement = "MATCH (n:Memory) WHERE n.memory_type = $kind RETURN n.id AS id"
    assert invoke(["graph", "query", statement, "--param", "kind=semantic"]) == 0
    assert len(json.loads(capsys.readouterr().out)["items"]) == 1

    assert invoke(["graph", "query", "CREATE (n:Memory {content:'x'})"]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "query_error"
    assert invoke(["graph", "query", "MATCH (n:Memory) RETURN n; DELETE n"]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "query_error"

    assert invoke(["graph", "query", "MATCH (n) RETURN n", "--limit", "1"]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "query_limit"


def test_edge_create_list_delete(graph_home, capsys):
    seed(graph_home)
    client = GraphClient.open(read_only=True)
    memory = client.query.find("Memory").items[0]
    entity = client.query.find("Entity").items[0]
    client.close()

    assert invoke(
        [
            "graph",
            "edge",
            "create",
            "--source",
            memory.id,
            "--target",
            entity.id,
            "--type",
            "MENTIONS",
            "--property",
            "confidence=0.9",
        ]
    ) == 0
    edge = json.loads(capsys.readouterr().out)["edge"]
    assert edge["properties"]["confidence"] == 0.9

    assert invoke(["graph", "edge", "list", "--source", memory.id]) == 0
    assert len(json.loads(capsys.readouterr().out)["items"]) == 2

    assert invoke(["graph", "edge", "delete", edge["id"]]) == 0
    json.loads(capsys.readouterr().out)
    assert invoke(["graph", "edge", "list", "--source", memory.id]) == 0
    assert len(json.loads(capsys.readouterr().out)["items"]) == 1


def test_neighbors_and_path(graph_home, capsys):
    seed(graph_home)
    client = GraphClient.open(read_only=True)
    memory = client.query.find("Memory").items[0]
    entity = client.query.find("Entity").items[0]
    client.close()

    assert invoke(["graph", "neighbors", memory.id]) == 0
    neighbours = json.loads(capsys.readouterr().out)["nodes"]
    assert [node["id"] for node in neighbours] == [entity.id]

    assert invoke(["graph", "path", memory.id, entity.id]) == 0
    path = json.loads(capsys.readouterr().out)["path"]
    assert [node["id"] for node in path["nodes"]] == [memory.id, entity.id]


def test_analyze_snapshot_and_backup(graph_home, tmp_path, capsys):
    seed(graph_home)
    assert invoke(["graph", "analyze", "--method", "pagerank", "--scope", "knowledge"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]

    assert invoke(["graph", "snapshot", "--scope", "knowledge"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["version"] >= 1
    assert len(snapshot["nodes"]) == 2

    destination = tmp_path / "backup.lbdb"
    assert invoke(["graph", "backup", "--dest", str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)["backup"] == str(destination)
    assert destination.exists()
    assert invoke(["graph", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_schema_register_from_file(graph_home, tmp_path, capsys):
    schema = {
        "nodes": [
            {"kind": "node", "name": "Doc", "scope": "docs", "fields": {"title": "STRING"}}
        ],
        "edges": [],
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    assert invoke(["graph", "schema", "register", "--file", str(path)]) == 0
    registered = json.loads(capsys.readouterr().out)["registered"]
    assert registered[0]["name"] == "Doc"
    assert invoke(["graph", "schema", "list"]) == 0
    assert [item["name"] for item in json.loads(capsys.readouterr().out)["types"]] == ["Doc"]


def test_doctor_and_status_are_informational(graph_home, capsys):
    seed(graph_home)
    assert invoke(["graph", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert invoke(["graph", "doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["lock"] == "available"
    assert payload["writable"] is True
    assert "capabilities" in payload
