"""The graph package must not import either optional backend at import time."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_import_hermes_graph_does_not_import_backends():
    result = run_python(
        "import sys, hermes_graph; "
        "assert 'ladybug' not in sys.modules, 'ladybug leaked'; "
        "assert 'networkx' not in sys.modules, 'networkx leaked'; "
        "print('ok')"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_cli_and_model_tools_do_not_import_graph():
    result = run_python(
        "import sys, cli, model_tools; "
        "assert 'hermes_graph' not in sys.modules, 'hermes_graph leaked'; "
        "assert 'ladybug' not in sys.modules, 'ladybug leaked'; "
        "assert 'networkx' not in sys.modules, 'networkx leaked'; "
        "print('ok')"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_public_signatures_do_not_expose_backend_types(knowledge):
    from hermes_graph import GraphClient

    signature = inspect.signature(GraphClient.open)
    rendered = str(signature)
    assert "ladybug" not in rendered
    assert "networkx" not in rendered
    for namespace in (
        "nodes", "edges", "query", "traverse", "algorithms",
        "index", "transaction", "schema", "admin",
    ):
        service = getattr(knowledge, namespace)
        for name in dir(service):
            if name.startswith("_"):
                continue
            attr = getattr(service, name)
            if not callable(attr):
                continue
            rendered = str(inspect.signature(attr))
            assert "ladybug" not in rendered
            assert "networkx" not in rendered


def test_returned_objects_are_public_models(knowledge):
    from hermes_graph.models import Edge, Node, QueryResult

    node = knowledge.nodes.create(
        type="Memory", properties={"content": "public boundary"}
    )
    edge = knowledge.edges.create(
        source_id=node.id,
        target_id=knowledge.nodes.upsert(
            type="Entity", key="entity:test", properties={"name": "Test"}
        ).id,
        type="MENTIONS",
    )
    result = knowledge.query.find("Memory")
    assert type(node) is Node
    assert type(edge) is Edge
    assert type(result) is QueryResult
    assert type(knowledge.index.snapshot("knowledge")).__name__ == "GraphSnapshot"
    assert type(knowledge.capabilities()).__name__ == "Capabilities"
    assert type(knowledge.health()).__name__ == "Health"
