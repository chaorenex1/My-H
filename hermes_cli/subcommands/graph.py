"""``hermes graph`` subcommand parser and handlers.

The parser module is import-light: ``hermes graph --help`` must not import
``hermes_graph``, Ladybug, NetworkX, or trigger plugin discovery. Handlers
import the graph package lazily after ``pm.ensure_import("graph")``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WRITE_COMMANDS = frozenset(
    {
        "node_upsert",
        "node_delete",
        "edge_create",
        "edge_delete",
        "schema_register",
        "backup",
    }
)


def build_graph_parser(subparsers, cmd_graph) -> None:
    parser = subparsers.add_parser(
        "graph",
        help="Local property graph (LadybugDB + NetworkX algorithms)",
        description=(
            "Query and maintain this profile's local graph database at "
            "<HERMES_HOME>/graph/graph.lbdb. Requires the optional [graph] extra. "
            "All output is JSON."
        ),
    )
    parser.set_defaults(func=cmd_graph, graph_command=None)
    commands = parser.add_subparsers(dest="graph_command", metavar="<command>")

    _status_parser(commands)
    _schema_parser(commands, cmd_graph)
    _node_parser(commands, cmd_graph)
    _edge_parser(commands, cmd_graph)
    _query_parser(commands, cmd_graph)
    _traverse_parser(commands, cmd_graph)
    _analyze_parser(commands, cmd_graph)
    _snapshot_parser(commands, cmd_graph)
    _backup_parser(commands, cmd_graph)
    _doctor_parser(commands)


def _status_parser(commands) -> None:
    parser = commands.add_parser("status", help="Database path, counts, versions, lock state")
    parser.set_defaults(graph_command="status")


def _schema_parser(commands, cmd_graph) -> None:
    parser = commands.add_parser("schema", help="Inspect or register node/edge types")
    parser.set_defaults(graph_command="schema")
    sub = parser.add_subparsers(dest="schema_command", metavar="<action>")
    sub.add_parser("list", help="List registered types").set_defaults(
        graph_command="schema_list"
    )
    register = sub.add_parser("register", help="Register types from a JSON file")
    register.add_argument("--file", required=True, help="schema.json path")
    register.set_defaults(graph_command="schema_register")


def _node_parser(commands, cmd_graph) -> None:
    parser = commands.add_parser("node", help="Read or write nodes")
    parser.set_defaults(graph_command="node")
    sub = parser.add_subparsers(dest="node_command", metavar="<action>")

    get = sub.add_parser("get", help="Get a node by id")
    get.add_argument("id")
    get.set_defaults(graph_command="node_get")

    search = sub.add_parser("search", help="Search a node type")
    search.add_argument("type")
    _add_filters(search)
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(graph_command="node_search")

    upsert = sub.add_parser("upsert", help="Idempotently write a node by (type, key)")
    upsert.add_argument("type")
    upsert.add_argument("key")
    _add_properties(upsert)
    upsert.set_defaults(graph_command="node_upsert")

    delete = sub.add_parser("delete", help="Soft-delete or hard-delete a node")
    delete.add_argument("id")
    delete.add_argument("--hard", action="store_true")
    delete.add_argument("--yes", action="store_true", help="Required with --hard")
    delete.set_defaults(graph_command="node_delete")


def _edge_parser(commands, cmd_graph) -> None:
    parser = commands.add_parser("edge", help="Read or write relationships")
    parser.set_defaults(graph_command="edge")
    sub = parser.add_subparsers(dest="edge_command", metavar="<action>")

    create = sub.add_parser("create", help="Create a relationship")
    create.add_argument("--source", required=True)
    create.add_argument("--target", required=True)
    create.add_argument("--type", required=True)
    _add_properties(create)
    create.set_defaults(graph_command="edge_create")

    delete = sub.add_parser("delete", help="Delete a relationship")
    delete.add_argument("id")
    delete.set_defaults(graph_command="edge_delete")

    listing = sub.add_parser("list", help="List relationships")
    listing.add_argument("--source")
    listing.add_argument("--target")
    listing.add_argument("--type")
    listing.add_argument("--limit", type=int, default=100)
    listing.set_defaults(graph_command="edge_list")


def _query_parser(commands, cmd_graph) -> None:
    parser = commands.add_parser("query", help="Run read-only parameterised Cypher")
    parser.add_argument("statement")
    parser.add_argument("--param", action="append", default=[], metavar="K=V")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout-ms", type=int, default=None)
    parser.set_defaults(graph_command="query")


def _traverse_parser(commands, cmd_graph) -> None:
    parser = commands.add_parser("neighbors", help="BFS neighbours of a node")
    parser.add_argument("id")
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--direction", choices=("out", "in", "both"), default="both")
    parser.add_argument("--relation", action="append", default=[])
    parser.set_defaults(graph_command="neighbors")

    path = commands.add_parser("path", help="Shortest path between two nodes")
    path.add_argument("source")
    path.add_argument("target")
    path.add_argument("--weight", default=None, help="Edge property to use as weight")
    path.add_argument("--direction", choices=("out", "in", "both"), default="out")
    path.set_defaults(graph_command="path")


def _analyze_parser(commands, cmd_graph) -> None:
    parser = commands.add_parser("analyze", help="Run a NetworkX algorithm on a scope")
    parser.add_argument(
        "--method",
        required=True,
        choices=(
            "pagerank",
            "betweenness",
            "closeness",
            "louvain",
            "degree",
            "components",
            "topological",
        ),
    )
    parser.add_argument("--scope", required=True)
    parser.add_argument("--weight", default=None)
    parser.set_defaults(graph_command="analyze")


def _snapshot_parser(commands, cmd_graph) -> None:
    parser = commands.add_parser("snapshot", help="Build/check a scope snapshot")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--node-type", action="append", default=[])
    parser.add_argument("--edge-type", action="append", default=[])
    parser.set_defaults(graph_command="snapshot")


def _backup_parser(commands, cmd_graph) -> None:
    parser = commands.add_parser("backup", help="Checkpoint and copy the database")
    parser.add_argument("--dest", default=None)
    parser.set_defaults(graph_command="backup")


def _doctor_parser(commands) -> None:
    parser = commands.add_parser("doctor", help="Read-only diagnostics")
    parser.set_defaults(graph_command="doctor")


def _add_filters(parser) -> None:
    parser.add_argument("--filter", action="append", default=[], metavar="K=V")


def _add_properties(parser) -> None:
    parser.add_argument("--file", default=None, help="JSON object of properties")
    parser.add_argument("--property", action="append", default=[], metavar="K=V")


# ── handler ──────────────────────────────────────────────────────────────
def cmd_graph(args) -> int:
    command = getattr(args, "graph_command", None)
    if not command:
        _fail("missing_command", "run `hermes graph --help`")
        return 2
    if command in {"status", "doctor"}:
        return _status(args, doctor=command == "doctor")
    if not _ensure_extra():
        return 1
    try:
        if command in _WRITE_COMMANDS:
            return _run_write(args, command)
        return _run_read(args, command)
    except Exception as exc:
        logger.exception("graph command %s failed", command)
        return _handle_error(exc)


def _ensure_extra() -> bool:
    try:
        from pm import extras

        if not extras.available("graph"):
            extras.ensure_import("graph")
        return True
    except Exception as exc:
        logger.debug("graph extra unavailable", exc_info=True)
        hint = _install_hint()
        _fail(
            "graph_unavailable",
            f"{exc}; install it with `{hint}`",
            details={"extra": "graph", "install_hint": hint},
        )
        return False


def _install_hint() -> str:
    try:
        from pm import extras

        return extras.install_hint("graph")
    except Exception:
        logger.debug("pm install hint unavailable", exc_info=True)
        return "hermes pm install --extra graph"


def _run_read(args, command: str) -> int:
    from hermes_graph import GraphClient

    client = GraphClient.open(read_only=True)
    try:
        payload = _dispatch_read(client, args, command)
        _emit(payload)
        return 0
    finally:
        client.close()


def _run_write(args, command: str) -> int:
    from hermes_graph import GraphClient

    client = GraphClient.open()
    try:
        payload = _dispatch_write(client, args, command)
        _emit(payload)
        return 0
    finally:
        client.close()


def _dispatch_read(client: Any, args, command: str) -> Any:
    if command == "schema_list":
        return {"types": client.schema.list_types()}
    if command == "node_get":
        node = client.nodes.get(args.id)
        return {"node": node.to_dict() if node else None}
    if command == "node_search":
        result = client.query.find(args.type, _parse_pairs(args.filter), limit=args.limit)
        return result.to_dict()
    if command == "edge_list":
        result = client.edges.list(
            source_id=args.source,
            target_id=args.target,
            type=args.type,
            limit=args.limit,
        )
        return result.to_dict()
    if command == "query":
        result = client.query.cypher(
            args.statement,
            _parse_pairs(args.param),
            max_rows=args.limit,
            timeout_ms=args.timeout_ms,
        )
        return result.to_dict()
    if command == "neighbors":
        result = client.query.neighbors(
            args.id,
            direction=args.direction,
            depth=args.depth,
            relation_types=args.relation or None,
        )
        return {"nodes": [node.to_dict() for node in result.items]}
    if command == "path":
        path = client.traverse.shortest_path(
            args.source, args.target, weight=args.weight, direction=args.direction
        )
        return {"path": path.to_dict() if path else None}
    if command == "analyze":
        return {"result": _analyze(client, args)}
    if command == "snapshot":
        snapshot = client.index.snapshot(
            args.scope,
            node_types=args.node_type or None,
            edge_types=args.edge_type or None,
        )
        return snapshot.to_dict()
    raise ValueError(f"unknown read command {command}")


def _dispatch_write(client: Any, args, command: str) -> Any:
    if command == "schema_register":
        payload = _read_json(args.file)
        registered = client.schema.register(payload)
        return {"registered": [item.to_dict() for item in registered]}
    if command == "node_upsert":
        node = client.nodes.upsert(args.type, args.key, _properties(args))
        return {"node": node.to_dict()}
    if command == "node_delete":
        if args.hard and not args.yes:
            from hermes_graph import ValidationError

            raise ValidationError("--hard requires --yes")
        client.nodes.delete(args.id, hard=args.hard)
        return {"deleted": args.id, "hard": bool(args.hard)}
    if command == "edge_create":
        edge = client.edges.create(
            args.source, args.target, args.type, _properties(args)
        )
        return {"edge": edge.to_dict()}
    if command == "edge_delete":
        client.edges.delete(args.id)
        return {"deleted": args.id}
    if command == "backup":
        return {"backup": client.admin.backup(args.dest)}
    raise ValueError(f"unknown write command {command}")


def _analyze(client: Any, args) -> Any:
    if args.method == "degree":
        return client.algorithms.degree(args.scope)
    if args.method == "louvain":
        return client.algorithms.community(args.scope)
    if args.method == "components":
        return client.algorithms.connected_components(args.scope)
    if args.method == "topological":
        return client.algorithms.topological_sort(args.scope)
    return client.algorithms.centrality(
        args.method, scope=args.scope, weight=args.weight
    )


def _status(args, *, doctor: bool) -> int:
    payload = _status_payload(doctor=doctor)
    _emit(payload)
    return 0


def _status_payload(*, doctor: bool) -> dict[str, Any]:
    from hermes_constants import get_hermes_home

    path = Path(get_hermes_home()) / "graph" / "graph.lbdb"
    available = _extra_available()
    payload: dict[str, Any] = {
        "status": "unavailable",
        "path": str(path),
        "extra": "graph",
        "install_hint": _install_hint(),
        "lock": "not-probed",
    }
    if not available:
        return payload
    try:
        from hermes_graph import GraphClient

        client = GraphClient.open(read_only=True)
    except Exception as exc:
        logger.debug("graph status probe failed", exc_info=True)
        exists = path.exists()
        payload["status"] = "uninitialized" if not exists else "error"
        payload["lock"] = "not-applicable" if not exists else "unknown"
        payload["reason"] = str(exc)
        return payload
    try:
        payload.update(client.health().to_dict())
        if doctor:
            payload["capabilities"] = client.capabilities().to_dict()
    finally:
        client.close()
    if payload.get("status") == "ok":
        payload["lock"] = _probe_lock()
        payload["writable"] = payload["lock"] == "available"
    else:
        payload["lock"] = "unknown"
    return payload


def _probe_lock() -> str:
    """Probe whether this process can take the writer lock right now."""
    try:
        from hermes_graph import GraphClient, GraphLockedError
    except Exception:
        logger.debug("graph lock probe import failed", exc_info=True)
        return "unknown"
    try:
        writer = GraphClient.open()
    except GraphLockedError:
        return "locked"
    except Exception:
        logger.debug("graph lock probe failed", exc_info=True)
        return "unknown"
    writer.close()
    return "available"


def _extra_available() -> bool:
    try:
        from pm import extras

        return extras.available("graph")
    except Exception:
        logger.debug("pm availability probe failed", exc_info=True)
        return False


# ── helpers ──────────────────────────────────────────────────────────────
def _parse_pairs(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"expected K=V, got {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError(f"empty key in {item!r}")
        result[key] = _parse_scalar(value)
    return result


def _parse_scalar(value: str) -> Any:
    """K=V values are JSON scalars; bare words stay strings."""
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _properties(args) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    if getattr(args, "file", None):
        properties.update(_read_json(args.file))
    properties.update(_parse_pairs(getattr(args, "property", [])))
    return properties


def _read_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def _fail(code: str, message: str, details: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"error": code, "message": message}
    if details:
        payload["details"] = details
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)


def _handle_error(exc: Exception) -> int:
    to_dict = getattr(exc, "to_dict", None)
    if callable(to_dict):
        print(json.dumps(to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    print(
        json.dumps({"error": "graph_error", "message": str(exc)}, ensure_ascii=False),
        file=sys.stderr,
    )
    return 1


__all__ = ["build_graph_parser", "cmd_graph"]
