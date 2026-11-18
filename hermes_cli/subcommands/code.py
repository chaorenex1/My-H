"""``hermes code`` subcommand parser and handlers.

Import-light: ``hermes code --help`` must not import Tree-sitter, Ladybug,
NetworkX, ``hermes_graph`` or ``hermes_codegraph``. Handlers import the code
package only after the extras are known to be available.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WRITE_COMMANDS = frozenset({"index", "language_install", "coverage_import", "prune"})


def build_code_parser(subparsers, cmd_code) -> None:
    parser = subparsers.add_parser(
        "code",
        help="Code intelligence: symbols, references, evidence (ADR-8)",
        description=(
            "Index this repository into the profile's local graph database and query "
            "symbols, definitions and references. Requires the [codegraph] and [graph] "
            "extras. All output is JSON."
        ),
    )
    parser.set_defaults(func=cmd_code, code_command=None)
    commands = parser.add_subparsers(dest="code_command", metavar="<command>")

    index = commands.add_parser("index", help="Incrementally index the repository")
    _add_root(index)
    index.add_argument("--full", action="store_true", help="Ignore content hashes")
    index.add_argument(
        "--semantic", action="store_true", help="Opt into LSP semantic enrichment"
    )
    index.set_defaults(code_command="index")

    status = commands.add_parser("status", help="Repository, revision, counts, freshness")
    _add_root(status)
    status.set_defaults(code_command="status")

    doctor = commands.add_parser("doctor", help="Read-only extras/schema/index diagnostics")
    _add_root(doctor)
    doctor.set_defaults(code_command="doctor")

    language = commands.add_parser("language", help="Inspect or install language grammars")
    language.set_defaults(code_command="language")
    lang_sub = language.add_subparsers(dest="language_command", metavar="<action>")
    listing = lang_sub.add_parser("list", help="Built-in and installable languages")
    listing.set_defaults(code_command="language_list")
    install = lang_sub.add_parser("install", help="Install one on-demand grammar")
    install.add_argument("language", help="language id, e.g. java")
    install.set_defaults(code_command="language_install")

    find = commands.add_parser("find", help="Find symbols by exact name/kind/language")
    find.add_argument("name", nargs="?", default=None)
    find.add_argument("--kind", default=None)
    find.add_argument("--language", default=None)
    find.add_argument("--pattern", default=None, help="ast-grep structural pattern (optional)")
    find.add_argument("--limit", type=int, default=20)
    _add_root(find)
    find.set_defaults(code_command="find")

    symbol = commands.add_parser("symbol", help="Symbol detail plus its definition")
    symbol.add_argument("symbol_id")
    _add_root(symbol)
    symbol.set_defaults(code_command="symbol")

    refs = commands.add_parser("refs", help="References to a symbol")
    refs.add_argument("symbol_id")
    refs.add_argument(
        "--policy", choices=("strict", "balanced", "exploratory"), default="balanced"
    )
    refs.add_argument("--limit", type=int, default=50)
    _add_root(refs)
    refs.set_defaults(code_command="refs")

    for name, help_text in (
        ("callers", "Callers of a symbol"),
        ("callees", "Callees of a symbol"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("symbol_id")
        command.add_argument("--depth", type=int, default=2)
        command.add_argument(
            "--policy", choices=("strict", "balanced", "exploratory"),
            default="strict" if name == "callers" else "balanced",
        )
        command.add_argument("--limit", type=int, default=200)
        _add_root(command)
        command.set_defaults(code_command=name)

    path = commands.add_parser("path", help="Shortest call path between two symbols")
    path.add_argument("source_id")
    path.add_argument("target_id")
    path.add_argument("--max-depth", type=int, default=5)
    path.add_argument(
        "--policy", choices=("strict", "balanced", "exploratory"), default="balanced"
    )
    _add_root(path)
    path.set_defaults(code_command="path")

    types = commands.add_parser("types", help="Inheritance/implementation/annotation relations")
    types.add_argument("symbol_id")
    types.add_argument("--direction", choices=("in", "out", "both"), default="both")
    types.add_argument("--depth", type=int, default=3)
    types.add_argument(
        "--policy", choices=("strict", "balanced", "exploratory"), default="balanced"
    )
    types.add_argument("--limit", type=int, default=200)
    _add_root(types)
    types.set_defaults(code_command="types")

    impact = commands.add_parser("impact", help="Impact radius of a symbol")
    impact.add_argument("symbol_id")
    impact.add_argument("--depth", type=int, default=3)
    impact.add_argument(
        "--policy", choices=("strict", "balanced", "exploratory"), default="strict"
    )
    impact.add_argument("--limit", type=int, default=200)
    _add_root(impact)
    impact.set_defaults(code_command="impact")

    deps = commands.add_parser("deps", help="Module dependencies")
    deps.add_argument("module_id")
    deps.add_argument("--depth", type=int, default=2)
    deps.add_argument("--limit", type=int, default=200)
    _add_root(deps)
    deps.set_defaults(code_command="deps")

    cycles = commands.add_parser("cycles", help="Dependency or type cycles")
    cycles.add_argument("--scope", choices=("modules", "types"), default="modules")
    cycles.add_argument("--limit", type=int, default=100)
    _add_root(cycles)
    cycles.set_defaults(code_command="cycles")

    tests = commands.add_parser("tests", help="Static tests for a symbol")
    tests.add_argument("symbol_id")
    tests.add_argument("--limit", type=int, default=200)
    _add_root(tests)
    tests.set_defaults(code_command="tests")

    context = commands.add_parser("context", help="Agent context for a symbol")
    context.add_argument("symbol_id")
    context.add_argument("--budget-tokens", type=int, default=6000)
    _add_root(context)
    context.set_defaults(code_command="context")

    coverage = commands.add_parser("coverage", help="Import or query coverage")
    coverage.set_defaults(code_command="coverage")
    coverage_sub = coverage.add_subparsers(dest="coverage_command", metavar="<action>")
    coverage_import = coverage_sub.add_parser("import", help="Import per-test coverage JSON")
    coverage_import.add_argument("file")
    coverage_import.add_argument("--revision", required=True)
    coverage_import.add_argument(
        "--format", choices=("auto", "coverage-py-context", "pytest-cov-context"),
        default="auto",
    )
    _add_root(coverage_import)
    coverage_import.set_defaults(code_command="coverage_import")
    coverage_show = coverage_sub.add_parser("show", help="Coverage for a symbol")
    coverage_show.add_argument("symbol_id")
    coverage_show.add_argument("--limit", type=int, default=200)
    _add_root(coverage_show)
    coverage_show.set_defaults(code_command="coverage_show")

    changes = commands.add_parser("changes", help="Change Graph entries for a symbol")
    changes.add_argument("symbol_id")
    changes.add_argument("--from", dest="from_revision", default=None)
    changes.add_argument("--to", dest="to_revision", default=None)
    changes.add_argument("--limit", type=int, default=200)
    _add_root(changes)
    changes.set_defaults(code_command="changes")

    diagnostics = commands.add_parser("diagnostics", help="Parse/Semgrep diagnostics")
    diagnostics.add_argument("--path", default=None)
    diagnostics.add_argument("--rule", default=None)
    diagnostics.add_argument("--severity", default=None)
    diagnostics.add_argument("--limit", type=int, default=50)
    _add_root(diagnostics)
    diagnostics.set_defaults(code_command="diagnostics")

    prune = commands.add_parser("prune", help="Remove old revisions and their change edges")
    prune.add_argument("--keep", type=int, default=50)
    prune.add_argument("--older-than", default=None)
    prune.add_argument("--yes", action="store_true")
    _add_root(prune)
    prune.set_defaults(code_command="prune")


def _add_root(parser) -> None:
    parser.add_argument(
        "--root", default=None, help="Repository path (default: this directory's Git root)"
    )


# ── handler ──────────────────────────────────────────────────────────────
def _action_index(ci, args):
    return ci.index(full=bool(args.full), semantic=bool(args.semantic))


def _action_find(ci, args):
    if args.pattern:
        from hermes_codegraph.analysis import structural_search

        return structural_search(
            ci.repository.root, args.pattern, kind=args.kind, limit=args.limit
        )
    return ci.find_symbols(
        name=args.name,
        kind=args.kind,
        language=args.language,
        limit=args.limit,
    )


def _action_symbol(ci, args):
    return {
        "symbol": ci.get_symbol(args.symbol_id),
        "definition": ci.get_definition(args.symbol_id),
    }


def _action_refs(ci, args):
    return ci.get_references(args.symbol_id, policy=args.policy, limit=args.limit)


def _action_callers(ci, args):
    return ci.get_callers(
        args.symbol_id, depth=args.depth, policy=args.policy, limit=args.limit
    )


def _action_callees(ci, args):
    return ci.get_callees(
        args.symbol_id, depth=args.depth, policy=args.policy, limit=args.limit
    )


def _action_path(ci, args):
    return ci.get_call_path(
        args.source_id, args.target_id, max_depth=args.max_depth, policy=args.policy
    )


def _action_types(ci, args):
    return ci.get_type_hierarchy(
        args.symbol_id, direction=args.direction, depth=args.depth,
        policy=args.policy, limit=args.limit,
    )


def _action_impact(ci, args):
    return ci.get_impact(
        args.symbol_id, depth=args.depth, policy=args.policy, limit=args.limit
    )


def _action_deps(ci, args):
    return ci.get_dependencies(
        args.module_id, depth=args.depth, limit=args.limit
    )


def _action_cycles(ci, args):
    return ci.get_cycles(scope=args.scope, limit=args.limit)


def _action_tests(ci, args):
    return ci.get_tests_for_symbol(args.symbol_id, limit=args.limit)


def _action_context(ci, args):
    return ci.build_context(args.symbol_id, budget_tokens=args.budget_tokens)


def _action_coverage_import(ci, args):
    return ci.import_coverage(
        args.file, revision=args.revision, format=args.format
    )


def _action_coverage_show(ci, args):
    return ci.get_coverage(args.symbol_id, limit=args.limit)


def _action_changes(ci, args):
    return ci.get_changes(
        args.symbol_id, from_revision=args.from_revision,
        to_revision=args.to_revision, limit=args.limit,
    )


def _action_diagnostics(ci, args):
    return ci.get_diagnostics(
        path=args.path, rule=args.rule, severity=args.severity, limit=args.limit
    )


def _action_prune(ci, args):
    from hermes_codegraph.exceptions import CodeGraphError

    if not args.yes:
        raise CodeGraphError("prune requires --yes; nothing was deleted")
    return ci.prune(keep=args.keep, older_than=args.older_than)


_SIMPLE_HANDLERS = {
    "status": lambda args: _status(args),
    "doctor": lambda args: _doctor(args),
    "language_list": lambda args: _language_list(),
    "language_install": lambda args: _language_install(args),
}

_INDEX_ACTIONS = {
    "index": _action_index,
    "find": _action_find,
    "symbol": _action_symbol,
    "refs": _action_refs,
    "callers": _action_callers,
    "callees": _action_callees,
    "path": _action_path,
    "types": _action_types,
    "impact": _action_impact,
    "deps": _action_deps,
    "cycles": _action_cycles,
    "tests": _action_tests,
    "context": _action_context,
    "coverage_import": _action_coverage_import,
    "coverage_show": _action_coverage_show,
    "changes": _action_changes,
    "diagnostics": _action_diagnostics,
    "prune": _action_prune,
}


def cmd_code(args) -> int:
    command = getattr(args, "code_command", None)
    if not command or command == "language":
        _fail("missing_command", "run `hermes code --help`")
        return 2
    simple = _SIMPLE_HANDLERS.get(command)
    if simple is not None:
        return simple(args)
    action = _INDEX_ACTIONS.get(command)
    if action is None:  # pragma: no cover - argparse restricts this
        _fail("unknown_command", f"unknown code command {command!r}")
        return 2
    if not _ensure_extras():
        return 1
    try:
        from hermes_codegraph import CodeIntelligence

        with CodeIntelligence.open(
            root=args.root, read_only=command not in _WRITE_COMMANDS
        ) as ci:
            payload = action(ci, args)
        _emit(payload)
        return 0
    except Exception as exc:
        logger.exception("code command %s failed", command)
        return _handle_error(exc)


def _status(args) -> int:
    from hermes_codegraph.config import CodeGraphLimits
    from hermes_codegraph.languages import default_registry
    from hermes_codegraph.scanner import resolve_repository

    repository = resolve_repository(getattr(args, "root", None))
    graph_available, codegraph_available, target = _extras_state()
    database = _database_path()
    try:
        limits = CodeGraphLimits.from_config()
    except ValueError as exc:
        limits = None
        config_error = str(exc)
    else:
        config_error = None
    registry = default_registry()
    payload: dict[str, Any] = {
        "status": "unavailable" if not graph_available else "unindexed",
        "repository_id": repository.repository_id,
        "repository": repository.to_dict(),
        "path": str(database),
        "graph_available": graph_available,
        "codegraph_available": codegraph_available,
        "platform_target": target,
        "code_intel": None
        if limits is None
        else {
            "enabled": limits.enabled,
            "update": limits.update,
            "auto_update": limits.auto_update,
            "semantic": limits.semantic,
            "semgrep": {
                "enabled": limits.semgrep_enabled,
                "mode": limits.semgrep_mode,
                "source": "live_only",
            },
        },
        "config_error": config_error,
        "language_registry": registry.report.to_dict()
        if registry.report is not None
        else None,
        "install_hint": _install_hint(),
    }
    if not graph_available or not database.exists():
        _emit(payload)
        return 0
    try:
        from hermes_codegraph import CodeIntelligence

        with CodeIntelligence.open(root=args.root, read_only=True) as ci:
            payload.update(ci.status())
    except Exception as exc:
        logger.debug("code status could not read the index", exc_info=True)
        payload["status"] = "error"
        payload["reason"] = f"{type(exc).__name__}: {exc}"
    _emit(payload)
    return 0


def _doctor(args) -> int:
    from hermes_codegraph.languages import default_registry
    from hermes_codegraph.query import doctor_payload
    from hermes_codegraph.scanner import resolve_repository

    repository = resolve_repository(getattr(args, "root", None))
    graph_available, codegraph_available, target = _extras_state()
    registry = default_registry()
    client = None
    try:
        if graph_available and _database_path().exists():
            from hermes_graph import GraphClient

            client = GraphClient.open(read_only=True)
        payload = doctor_payload(
            client,
            repository,
            registry,
            graph_available=graph_available,
            codegraph_available=codegraph_available,
            target=target,
        )
    finally:
        if client is not None:
            client.close()
    payload["platform_target"] = target
    payload["install_hint"] = _install_hint()
    payload["language_registry"] = (
        registry.report.to_dict() if registry.report is not None else None
    )
    _emit(payload)
    return 0 if payload.get("status") == "ok" else 1


def _language_list() -> int:
    from hermes_codegraph.config import CodeGraphLimits
    from hermes_codegraph.languages import default_registry

    codegraph_available = _extras_state()[1]
    try:
        limits = CodeGraphLimits.from_config()
    except ValueError:
        limits = CodeGraphLimits()
    enabled_extra = set(limits.languages)
    registry = default_registry()
    items: list[dict[str, Any]] = []
    for descriptor in registry.descriptors():
        enabled = descriptor.extra is None or descriptor.language in enabled_extra
        items.append(
            {
                **descriptor.to_dict(),
                "builtin": descriptor.extra is None,
                "installed": _module_available(descriptor.grammar_package),
                "enabled": enabled,
                "available": codegraph_available,
            }
        )
    _emit(
        {
            "codegraph_available": codegraph_available,
            "language_registry": registry.report.to_dict()
            if registry.report is not None
            else None,
            "install_hint": _install_hint(),
            "languages": items,
        }
    )
    return 0


def _language_install(args) -> int:
    try:
        from hermes_codegraph.exceptions import LanguageUnsupportedError
        from hermes_codegraph.languages import default_registry, install_target_gap
        from pm import store

        descriptor = default_registry().get(args.language)
        extra = descriptor.extra
        if not extra:
            raise LanguageUnsupportedError(
                f"{descriptor.language!r} is built in; install the [codegraph] extra",
                details={"language": descriptor.language, "extra": "codegraph"},
            )
        target = store.current_target()
        gap = install_target_gap(target)
        if gap:
            _fail(
                "codegraph_unavailable",
                gap,
                details={
                    "language": descriptor.language,
                    "extra": extra,
                    "platform_target": target,
                },
            )
            return 1
        from pm import extras

        extras.ensure_import(extra)
        _emit(
            {
                "language": descriptor.language,
                "extra": extra,
                "installed": _module_available(descriptor.grammar_package),
            }
        )
        return 0
    except Exception as exc:
        logger.debug("code language install failed", exc_info=True)
        return _handle_error(exc)


def _ensure_extras() -> bool:
    try:
        from hermes_codegraph.languages import install_target_gap
        from pm import extras, store

        missing = [
            extra
            for extra in ("graph", "codegraph")
            if not extras.available(extra)
        ]
        if "codegraph" in missing:
            target = store.current_target()
            gap = install_target_gap(target)
            if gap:
                _fail(
                    "codegraph_unavailable",
                    gap,
                    details={"platform_target": target, "install_hint": _install_hint()},
                )
                return False
        for extra in missing:
            extras.ensure_import(extra)
        return True
    except Exception as exc:
        logger.debug("code extras probe failed", exc_info=True)
        hint = _install_hint()
        _fail(
            "codegraph_unavailable",
            f"{exc}; install it with `{hint}`",
            details={"install_hint": hint},
        )
        return False


def _extras_state() -> tuple[bool, bool, str | None]:
    try:
        from pm import extras

        graph_available = extras.available("graph")
        codegraph_available = extras.available("codegraph")
    except Exception:  # health: allow BLE001 -- optional PM extras probe; status must work without PM
        graph_available = _module_available("ladybug")
        codegraph_available = _module_available("tree_sitter")
    try:
        from pm import store

        target = store.current_target()
    except Exception:  # health: allow BLE001 -- optional PM store probe; status must work without PM
        target = None
    return graph_available, codegraph_available, target


def _database_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "graph" / "graph.lbdb"


def _module_available(module: str) -> bool:
    anchor = module.replace("-", "_")
    if anchor in sys.modules:
        return True
    try:
        return importlib.util.find_spec(anchor) is not None
    except (ImportError, ValueError):
        return False


def _install_hint() -> str:
    return "hermes pm install --extra graph && hermes pm install --extra codegraph"


# ── output helpers ───────────────────────────────────────────────────────
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
        json.dumps({"error": "codegraph_error", "message": str(exc)}, ensure_ascii=False),
        file=sys.stderr,
    )
    return 1


__all__ = ["build_code_parser", "cmd_code"]
