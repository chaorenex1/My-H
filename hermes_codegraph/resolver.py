"""Cross-file resolution for the code graph.

Tree-sitter extraction is file-local: it names call targets and imported
modules but cannot know whether a name belongs to another file. This module
turns those local names into typed edges (``CODE_CALLS``, ``CODE_INHERITS``,
``CODE_IMPORTS``, ``CODE_TESTS``, ...) using the import table, the symbol
table and unique-name inference. Ambiguous targets become one ``ambiguous``
edge per candidate; unresolved call expressions stay evidence-only.

All writes are idempotent: edges use ``edges.upsert`` and evidence nodes use
the stable ``(path, span, kind, tool, excerpt_hash)`` key.
"""

from __future__ import annotations

import logging
import posixpath
from datetime import UTC, datetime
from typing import Any

from hermes_graph.serialization import decode_properties, derived_id

from .exceptions import CodeGraphError
from .languages import LanguageDescriptor, LanguageRegistry
from .scanner import RepositoryInfo

logger = logging.getLogger(__name__)

STATUS_CONFIDENCE = {
    "resolved": (0.8, "status_mapping"),
    "inferred": (0.5, "status_mapping"),
    "ambiguous": (0.3, "status_mapping"),
}

RELATION_EDGES = {
    "calls": "CODE_CALLS",
    "instantiates": "CODE_INSTANTIATES",
    "inherits": "CODE_INHERITS",
    "implements": "CODE_IMPLEMENTS",
    "overrides": "CODE_OVERRIDES",
    "has_type": "CODE_HAS_TYPE",
    "returns_type": "CODE_RETURNS_TYPE",
    "references": "CODE_REFERENCES",
}

RESOLVER_EDGES = (
    "CODE_CALLS",
    "CODE_INSTANTIATES",
    "CODE_INHERITS",
    "CODE_IMPLEMENTS",
    "CODE_OVERRIDES",
    "CODE_HAS_TYPE",
    "CODE_RETURNS_TYPE",
    "CODE_IMPORTS",
    "CODE_DEPENDS_ON",
    "CODE_USES_PACKAGE",
    "CODE_EXPORTS",
    "CODE_REEXPORTS",
    "CODE_TESTS",
)

BUILTIN_TYPE_NAMES = frozenset(
    {
        "str", "int", "float", "bool", "bytes", "bytearray", "complex", "object",
        "string", "number", "boolean", "any", "unknown", "never", "void", "null",
        "undefined", "symbol", "bigint", "true", "false", "Self", "self",
        "i8", "i16", "i32", "i64", "i128", "isize", "u8", "u16", "u32", "u64",
        "u128", "usize", "f32", "f64", "char", "String", "Vec", "Option",
        "Result", "error", "rune", "uint", "uintptr", "float32", "float64",
    }
)

_ECOSYSTEMS = {
    "python": "pypi",
    "typescript": "npm",
    "go": "go",
    "rust": "cargo",
}


class Resolver:
    def __init__(
        self,
        client: Any,
        repository: RepositoryInfo,
        registry: LanguageRegistry,
        *,
        run_key: str,
        revision_key: str,
        revision_id: str,
    ):
        self.client = client
        self.repo = repository
        self.registry = registry
        self.run_key = run_key
        self.revision_key = revision_key
        self.revision_id = revision_id
        self.files: list[dict[str, Any]] = []
        self.symbols: list[dict[str, Any]] = []
        self.modules: list[dict[str, Any]] = []
        self.by_qualified: dict[str, list[dict[str, Any]]] = {}
        self.by_name: dict[str, list[dict[str, Any]]] = {}
        self.by_path: dict[str, dict[str, Any]] = {}
        self.symbols_by_path: dict[str, list[dict[str, Any]]] = {}
        self.file_by_module: dict[str, dict[str, Any]] = {}
        self.module_names: set[str] = set()
        self.module_by_path: dict[str, str] = {}
        self.module_languages: dict[str, str] = {}
        self.unresolved: dict[str, int] = {}
        self._observed = datetime.now(UTC).isoformat()

    # ── entry point ──────────────────────────────────────────────────────
    def resolve(self) -> dict[str, int]:
        self._load()
        self._purge_resolver_edges()
        self._purge_test_cases()
        self._purge_stale_modules()
        import_plans = self._plan_imports()
        call_plans = self._plan_calls()
        type_plans = self._plan_types()
        test_plans = self._plan_tests()
        export_plans = self._plan_exports()
        plans = import_plans + call_plans + type_plans + test_plans + export_plans
        self._write(plans)
        self._write_unresolved_counts()
        return {
            "edges_written": len(plans),
            "unresolved_calls": sum(self.unresolved.values()),
        }

    # ── loading ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        self.files = [
            {
                **decode_properties(row.get("props")),
                "path": row.get("path"),
                "language": row.get("language"),
            }
            for row in self._paginate(
                "MATCH (f:CodeFile) WHERE f.repository_id=$repo "
                "AND f.deleted_at IS NULL AND f.path>$after "
                "RETURN f.path AS path, f.language AS language, f.props AS props "
                "ORDER BY f.path",
                "path",
            )
        ]
        for file in self.files:
            file["module"] = file.get("module_name") or ""
        self.symbols = self._paginate(
            "MATCH (s:CodeSymbol) WHERE s.repository_id=$repo "
            "AND (s.symbol_status IS NULL OR s.symbol_status <> 'deleted') "
            "AND s.symbol_id>$after "
            "RETURN s.symbol_id AS symbol_id, s.qualified_name AS qualified_name, "
            "s.name AS name, s.kind AS kind, s.language AS language, "
            "s.file_path AS path, s.container_symbol AS container, "
            "s.is_exported AS is_exported, s.start_line AS start_line, "
            "s.end_line AS end_line, s.content_hash AS content_hash "
            "ORDER BY s.symbol_id",
            "symbol_id",
        )
        self.modules = self._paginate(
            "MATCH (m:CodeModule) WHERE m.repository_id=$repo "
            "AND m.qualified_name>$after "
            "RETURN m.qualified_name AS qualified_name, m.language AS language, "
            "m.path AS path ORDER BY m.qualified_name",
            "qualified_name",
        )
        for symbol in self.symbols:
            if not symbol.get("qualified_name") or not symbol.get("symbol_id"):
                continue
            self.by_qualified.setdefault(symbol["qualified_name"], []).append(symbol)
            self.by_name.setdefault(symbol["name"] or "", []).append(symbol)
            self.symbols_by_path.setdefault(symbol.get("path") or "", []).append(symbol)
        for file in self.files:
            if file.get("path"):
                self.by_path[file["path"]] = file
                if file.get("module"):
                    self.module_names.add(file["module"])
                    self.module_by_path[file["path"]] = file["module"]
                    self.file_by_module[file["module"]] = file
                    self.module_languages[file["module"]] = str(
                        file.get("language") or ""
                    )
        for module in self.modules:
            if module.get("qualified_name"):
                self.module_names.add(module["qualified_name"])

    def _paginate(
        self, statement: str, cursor: str, page: int = 100
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        after = ""
        while True:
            try:
                rows = self.client.query.cypher(
                    statement,
                    {"repo": self.repo.repository_id, "after": after},
                    max_rows=page,
                ).items
            except Exception as exc:  # a broken page must not corrupt the index
                raise CodeGraphError(f"resolver query failed: {exc}") from exc
            if not rows:
                break
            items.extend(row for row in rows if isinstance(row, dict))
            last = str(rows[-1].get(cursor) or "")
            if not last or last == after or len(rows) < page:
                break
            after = last
        return items

    # ── cleanup ──────────────────────────────────────────────────────────
    def _purge_resolver_edges(self) -> None:
        for edge_type in RESOLVER_EDGES:
            after = ""
            while True:
                rows = self.client.query.cypher(
                    f"MATCH (a)-[r:{edge_type}]->(b) "
                    "WHERE a.repository_id=$repo AND r.id>$after "
                    "RETURN r.id AS id ORDER BY r.id",
                    {"repo": self.repo.repository_id, "after": after},
                    max_rows=100,
                ).items
                if not rows:
                    break
                with self.client.transaction.begin() as tx:
                    for row in rows:
                        try:
                            tx.edges.delete(row["id"])
                        except Exception:
                            logger.debug("resolver edge purge failed", exc_info=True)
                last = str(rows[-1].get("id") or "")
                if not last or last == after or len(rows) < 100:
                    break
                after = last

    def _purge_test_cases(self) -> None:
        after = ""
        while True:
            cases = self.client.nodes.search(
                "CodeTestCase",
                {"repository_id": self.repo.repository_id},
                limit=1000,
            ).items
            pending = [
                node for node in cases
                if str(node.properties.get("qualified_name") or "") > after
            ]
            if not pending:
                break
            with self.client.transaction.begin() as tx:
                for node in pending:
                    try:
                        tx.nodes.delete(node.id, hard=True)
                    except Exception:
                        logger.debug("test case purge failed", exc_info=True)
            last = str(pending[-1].properties.get("qualified_name") or "")
            if not last or last == after:
                break
            after = last

    def _purge_stale_modules(self) -> None:
        keep = set(self.module_names)
        stale = [
            module
            for module in self.modules
            if module.get("qualified_name") not in keep
        ]
        if not stale:
            return
        with self.client.transaction.begin() as tx:
            for module in stale:
                try:
                    node_id = derived_id(
                        "CodeModule",
                        f"{self.repo.key}@{module.get('language')}:"
                        f"{module.get('qualified_name')}",
                    )
                    tx.nodes.delete(node_id, hard=True)
                except Exception:
                    logger.debug("stale module purge failed", exc_info=True)

    # ── planning: imports ────────────────────────────────────────────────
    def _plan_imports(self) -> list[dict[str, Any]]:
        plans: list[dict[str, Any]] = []
        self._alias_maps: dict[str, dict[str, tuple[str, str | None]]] = {}
        for file in self.files:
            path = file.get("path") or ""
            source_module = file.get("module") or ""
            aliases: dict[str, tuple[str, str | None]] = {}
            for raw in _as_list(file.get("imports")):
                imp = raw if isinstance(raw, dict) else {}
                module_name = str(imp.get("module") or "")
                target = self._resolve_module(file, imp)
                if target is not None:
                    target_file = self.file_by_module.get(target)
                    plans.append(
                        self._edge_plan(
                            source_key=("CodeFile", self._file_key(path)),
                            target_key=(
                                "CodeModule",
                                self._module_key(target, target_file.get("language") if target_file else file.get("language")),
                            ),
                            edge_type="CODE_IMPORTS",
                            status="resolved",
                            method="import_resolution",
                            evidence=self._evidence(
                                path,
                                "import",
                                imp.get("start_line", 1),
                                imp.get("start_column", 1),
                                imp.get("end_line", 1),
                                imp.get("end_column", 1),
                                str(imp.get("excerpt_hash") or ""),
                                file.get("language") or "",
                                "import",
                            ),
                            props={"module": module_name, "is_reexport": bool(imp.get("is_reexport"))},
                        )
                    )
                    if source_module and target:
                        plans.append(
                            self._edge_plan(
                                source_key=(
                                    "CodeModule",
                                    self._module_key(source_module, file.get("language")),
                                ),
                                target_key=(
                                    "CodeModule",
                                    self._module_key(target, target_file.get("language") if target_file else file.get("language")),
                                ),
                                edge_type="CODE_DEPENDS_ON",
                                status="resolved",
                                method="import_resolution",
                                evidence=self._evidence(
                                    path,
                                    "import",
                                    imp.get("start_line", 1),
                                    imp.get("start_column", 1),
                                    imp.get("end_line", 1),
                                    imp.get("end_column", 1),
                                    str(imp.get("excerpt_hash") or ""),
                                    file.get("language") or "",
                                    "import",
                                ),
                                props={"imported_module": module_name},
                            )
                        )
                    for name in _as_list(imp.get("imported_names")):
                        if name == "*":
                            continue
                        local = str(name)
                        alias = imp.get("alias")
                        if alias:
                            aliases[str(alias)] = (target, local)
                        aliases[local] = (target, local)
                    if imp.get("alias"):
                        aliases[str(imp["alias"])] = (target, None)
                    if file.get("language") == "go" and not imp.get("imported_names"):
                        # Go calls through the package qualifier; the extractor
                        # only records the selector name, so expose the target
                        # package's symbols by name.
                        for symbol in self._module_symbols(target):
                            name = str(symbol.get("name") or "")
                            if name:
                                aliases.setdefault(name, (target, name))
                else:
                    package = self._external_package(file, module_name)
                    if package and source_module:
                        plans.append(self._package_plan(package, module_name))
                        plans.append(
                            self._edge_plan(
                                source_key=(
                                    "CodeModule",
                                    self._module_key(source_module, file.get("language")),
                                ),
                                target_key=("CodePackage", package),
                                edge_type="CODE_USES_PACKAGE",
                                status="inferred",
                                method="import_resolution",
                                evidence=self._evidence(
                                    path,
                                    "import",
                                    imp.get("start_line", 1),
                                    imp.get("start_column", 1),
                                    imp.get("end_line", 1),
                                    imp.get("end_column", 1),
                                    str(imp.get("excerpt_hash") or ""),
                                    file.get("language") or "",
                                    "import",
                                ),
                                props={"module": module_name},
                            )
                        )
            self._alias_maps[path] = aliases
        return plans

    def _resolve_module(
        self, file: dict[str, Any], imp: dict[str, Any]
    ) -> str | None:
        module_name = str(imp.get("module") or "")
        language = file.get("language") or ""
        level = int(imp.get("level") or 0)
        if language == "rust":
            candidate = _rust_module(file, module_name)
        elif imp.get("is_relative"):
            if language == "python":
                candidate = _python_relative(file, module_name, level)
            else:
                candidate = _path_relative(file.get("path") or "", module_name)
        else:
            candidate = module_name
        if not candidate:
            return None
        resolved = self._match_module(candidate, language)
        if resolved is not None:
            return resolved
        if language == "rust" and "::" in candidate:
            parts = candidate.split("::")
            for end in range(len(parts) - 1, 0, -1):
                prefix = "::".join(parts[:end])
                resolved = self._match_module(prefix, language)
                if resolved is not None:
                    return resolved
        return None

    def _match_module(self, candidate: str, language: str) -> str | None:
        normalized = candidate.replace("/", ".").removesuffix(".ts").removesuffix(".tsx")
        names = [
            name for name in sorted(self.module_names)
            if not language
            or self.module_languages.get(name, language) == language
        ]
        if normalized in names:
            return normalized
        if language == "python":
            # `from pkg import mod` imports the package, not a submodule.
            if normalized in names:
                return normalized
        for name in names:
            if name.endswith(f".{normalized}") or name.endswith(f"::{normalized}"):
                return name
        # Go/Rust module paths often carry a repository prefix (`example.com/x`).
        tail = candidate.replace("::", "/").split("/")[-1]
        tail = tail.removesuffix(".ts").removesuffix(".tsx")
        matches = [
            name for name in names
            if name.replace("::", "/").split("/")[-1] == tail
        ]
        return matches[0] if len(matches) == 1 else None

    def _external_package(
        self, file: dict[str, Any], module_name: str
    ) -> str | None:
        if not module_name:
            return None
        language = file.get("language") or ""
        ecosystem = _ECOSYSTEMS.get(language)
        if ecosystem is None:
            return None
        if language == "python":
            name = module_name.split(".")[0]
        elif language == "typescript":
            parts = module_name.split("/")
            name = "/".join(parts[:2]) if module_name.startswith("@") else parts[0]
        elif language == "rust":
            name = module_name.split("::")[0]
            if name in ("crate", "self", "super", "std", "core", "alloc"):
                return f"{ecosystem}:{name}@unknown"
        else:
            name = module_name
        return f"{ecosystem}:{name}@unknown"

    def _package_plan(self, package: str, module_name: str) -> dict[str, Any]:
        ecosystem, rest = package.split(":", 1)
        name, _, version = rest.rpartition("@")
        return {
            "kind": "node",
            "node_type": "CodePackage",
            "key": package,
            "props": {
                "ecosystem": ecosystem,
                "name": name or rest,
                "version": version if version != "unknown" else "",
            },
        }

    # ── planning: calls ──────────────────────────────────────────────────
    def _plan_calls(self) -> list[dict[str, Any]]:
        plans: list[dict[str, Any]] = []
        for file in self.files:
            path = file.get("path") or ""
            aliases = getattr(self, "_alias_maps", {}).get(path, {})
            for raw in _as_list(file.get("call_sites")):
                site = raw if isinstance(raw, dict) else {}
                source_symbols = self.by_qualified.get(
                    str(site.get("source_qualified_name") or ""), []
                )
                if not source_symbols:
                    continue
                source = source_symbols[0]
                relation = str(site.get("relation") or "references")
                if relation not in RELATION_EDGES:
                    relation = "references"
                candidates, status = self._call_candidates(file, site, aliases)
                if not candidates:
                    self.unresolved[path] = self.unresolved.get(path, 0) + 1
                    unresolved_evidence = self._evidence(
                        path,
                        str(site.get("evidence_kind") or "call_expression"),
                        int(site.get("start_line") or 1),
                        int(site.get("start_column") or 1),
                        int(site.get("end_line") or 1),
                        int(site.get("end_column") or 1),
                        str(site.get("excerpt_hash") or ""),
                        file.get("language") or "",
                        "call_expression",
                    )
                    unresolved_evidence["props"].update(
                        {
                            "resolution_status": "unresolved",
                            "target_qualified_name": str(
                                site.get("target_name") or ""
                            ),
                        }
                    )
                    plans.append(
                        {
                            "kind": "node",
                            "node_type": "CodeEvidence",
                            "key": unresolved_evidence["key"],
                            "props": unresolved_evidence["props"],
                        }
                    )
                    continue
                if len(candidates) > 1:
                    status = "ambiguous"
                    candidates = candidates[:3]
                evidence = self._evidence(
                    path,
                    str(site.get("evidence_kind") or "call_expression"),
                    int(site.get("start_line") or 1),
                    int(site.get("start_column") or 1),
                    int(site.get("end_line") or 1),
                    int(site.get("end_column") or 1),
                    str(site.get("excerpt_hash") or ""),
                    file.get("language") or "",
                    "call_expression",
                )
                for rank, target in enumerate(candidates):
                    if target.get("symbol_id") == source.get("symbol_id"):
                        continue
                    props: dict[str, Any] = {
                        "candidate_rank": rank,
                        "candidate_count": len(candidates),
                    }
                    if relation in ("calls", "instantiates", "references"):
                        plans.append(
                            self._edge_plan(
                                source_key=("CodeSymbol", source["symbol_id"]),
                                target_key=("CodeSymbol", target["symbol_id"]),
                                edge_type="CODE_REFERENCES",
                                status=status,
                                method=str(site.get("resolution_method") or "tree_sitter"),
                                evidence=evidence,
                                props=props,
                            )
                        )
                    plans.append(
                        self._edge_plan(
                            source_key=("CodeSymbol", source["symbol_id"]),
                            target_key=("CodeSymbol", target["symbol_id"]),
                            edge_type=RELATION_EDGES[relation],
                            status=status,
                            method=str(site.get("resolution_method") or "tree_sitter"),
                            evidence=evidence,
                            props=props,
                        )
                    )
        return plans

    def _call_candidates(
        self,
        file: dict[str, Any],
        site: dict[str, Any],
        aliases: dict[str, tuple[str, str | None]],
    ) -> tuple[list[dict[str, Any]], str]:
        exact = str(site.get("target_qualified_name") or "")
        if exact and exact in self.by_qualified:
            target = self.by_qualified[exact][0]
            status = (
                "inferred" if target.get("path") == file.get("path") else "resolved"
            )
            return [target], status
        target_name = str(site.get("target_name") or "")
        if not target_name:
            return [], "unresolved"
        same_file = [
            symbol for symbol in self.by_name.get(target_name, [])
            if symbol.get("path") == file.get("path")
        ]
        if len(same_file) == 1:
            return same_file, "inferred"
        if target_name in aliases:
            module, symbol_name = aliases[target_name]
            candidates = self._symbols_in_module(module, symbol_name or target_name)
            if len(candidates) == 1:
                return candidates, "resolved"
            if len(candidates) > 1:
                return candidates, "ambiguous"
        global_matches = [
            symbol for symbol in self.by_name.get(target_name, [])
            if symbol.get("language") == file.get("language")
        ]
        if len(global_matches) == 1:
            return global_matches, "inferred"
        if len(global_matches) > 1:
            return global_matches, "ambiguous"
        return [], "unresolved"

    def _module_symbols(self, module: str) -> list[dict[str, Any]]:
        return [
            symbol for symbol in self.symbols
            if str(symbol.get("qualified_name") or "").startswith(f"{module}.")
        ]

    def _symbols_in_module(
        self, module: str, name: str
    ) -> list[dict[str, Any]]:
        # ``qualified()`` always joins the module path and the symbol name with
        # a dot, including Go ("pkg/util") and Rust ("src::foo") module names.
        exact = f"{module}.{name}"
        return self.by_qualified.get(exact, []) or [
            symbol for symbol in self.by_name.get(name, [])
            if str(symbol.get("qualified_name") or "").startswith(f"{module}.")
        ]

    # ── planning: types ──────────────────────────────────────────────────
    def _plan_types(self) -> list[dict[str, Any]]:
        plans: list[dict[str, Any]] = []
        for file in self.files:
            path = file.get("path") or ""
            aliases = getattr(self, "_alias_maps", {}).get(path, {})
            for raw in _as_list(file.get("type_relations")):
                relation = raw if isinstance(raw, dict) else {}
                source_symbols = self.by_qualified.get(
                    str(relation.get("source_qualified_name") or ""), []
                )
                if not source_symbols:
                    continue
                target_name = str(relation.get("target_name") or "")
                kind = str(relation.get("relation") or "")
                if kind not in RELATION_EDGES or not target_name:
                    continue
                candidates, status = self._type_candidates(
                    file, target_name, kind, aliases
                )
                if not candidates:
                    continue
                evidence = self._evidence(
                    path,
                    "type_reference" if kind in ("has_type", "returns_type") else "inheritance",
                    int(relation.get("start_line") or 1),
                    int(relation.get("start_column") or 1),
                    int(relation.get("end_line") or 1),
                    int(relation.get("end_column") or 1),
                    str(relation.get("excerpt_hash") or ""),
                    file.get("language") or "",
                    "type_reference",
                )
                for target in candidates[:3]:
                    if target.get("symbol_id") == source_symbols[0].get("symbol_id"):
                        continue
                    plans.append(
                        self._edge_plan(
                            source_key=("CodeSymbol", source_symbols[0]["symbol_id"]),
                            target_key=("CodeSymbol", target["symbol_id"]),
                            edge_type=RELATION_EDGES[kind],
                            status=status,
                            method="type_resolution",
                            evidence=evidence,
                            props={"target_name": target_name},
                        )
                    )
        return plans

    def _type_candidates(
        self,
        file: dict[str, Any],
        target_name: str,
        relation: str,
        aliases: dict[str, tuple[str, str | None]],
    ) -> tuple[list[dict[str, Any]], str]:
        if target_name in BUILTIN_TYPE_NAMES:
            return [], "unresolved"
        if relation == "overrides" and "." in target_name:
            container, _, method = target_name.rpartition(".")
            matches = [
                symbol for symbol in self.by_name.get(method, [])
                if str(symbol.get("container") or "").endswith(container)
            ]
            if len(matches) == 1:
                return matches, "resolved"
            return matches, "ambiguous" if matches else "unresolved"
        same_file = [
            symbol for symbol in self.by_name.get(target_name, [])
            if symbol.get("path") == file.get("path")
        ]
        if len(same_file) == 1:
            return same_file, "resolved"
        if target_name in aliases:
            module, symbol_name = aliases[target_name]
            candidates = self._symbols_in_module(module, symbol_name or target_name)
            if candidates:
                return candidates, "resolved" if len(candidates) == 1 else "ambiguous"
        matches = [
            symbol for symbol in self.by_name.get(target_name, [])
            if symbol.get("language") == file.get("language")
        ]
        if len(matches) == 1:
            return matches, "inferred"
        return matches, "ambiguous" if matches else "unresolved"

    # ── planning: tests ──────────────────────────────────────────────────
    def _plan_tests(self) -> list[dict[str, Any]]:
        plans: list[dict[str, Any]] = []
        for file in self.files:
            path = file.get("path") or ""
            aliases = getattr(self, "_alias_maps", {}).get(path, {})
            for raw in _as_list(file.get("tests")):
                test = raw if isinstance(raw, dict) else {}
                qualified_name = str(test.get("qualified_name") or "")
                if not qualified_name:
                    continue
                key = f"{self.repo.key}@{path}::{qualified_name}"
                plans.append(
                    {
                        "kind": "node",
                        "node_type": "CodeTestCase",
                        "key": key,
                        "props": {
                            "repository_id": self.repo.repository_id,
                            "path": path,
                            "qualified_name": qualified_name,
                            "language": file.get("language") or "",
                            "name": str(test.get("name") or qualified_name),
                            "framework": str(test.get("framework") or ""),
                        },
                    }
                )
                targets: list[dict[str, Any]] = []
                for target_name in _as_list(test.get("target_names")):
                    candidates, _status = self._call_candidates(
                        file, {"target_name": str(target_name)}, aliases
                    )
                    targets.extend(candidates)
                for raw_site in _as_list(file.get("call_sites")):
                    site = raw_site if isinstance(raw_site, dict) else {}
                    if site.get("source_qualified_name") != qualified_name:
                        continue
                    candidates, _status = self._call_candidates(file, site, aliases)
                    targets.extend(candidates)
                seen: set[str] = set()
                for target in targets:
                    symbol_id = target.get("symbol_id")
                    if not symbol_id or symbol_id in seen:
                        continue
                    seen.add(symbol_id)
                    plans.append(
                        self._edge_plan(
                            source_key=("CodeTestCase", key),
                            target_key=("CodeSymbol", symbol_id),
                            edge_type="CODE_TESTS",
                            status="inferred",
                            method="test_detection",
                            evidence=self._evidence(
                                path,
                                "test_annotation",
                                int(test.get("start_line") or 1),
                                int(test.get("start_column") or 1),
                                int(test.get("end_line") or 1),
                                int(test.get("end_column") or 1),
                                str(test.get("excerpt_hash") or ""),
                                file.get("language") or "",
                                "test_annotation",
                            ),
                            props={"framework": str(test.get("framework") or "")},
                        )
                    )
        return plans

    # ── planning: exports ────────────────────────────────────────────────
    def _plan_exports(self) -> list[dict[str, Any]]:
        plans: list[dict[str, Any]] = []
        for file in self.files:
            path = file.get("path") or ""
            module = file.get("module") or ""
            if not module:
                continue
            module_key = ("CodeModule", self._module_key(module, file.get("language")))
            for symbol in self.symbols_by_path.get(path, []):
                if not symbol.get("is_exported"):
                    continue
                plans.append(
                    self._edge_plan(
                        source_key=module_key,
                        target_key=("CodeSymbol", symbol["symbol_id"]),
                        edge_type="CODE_EXPORTS",
                        status="resolved",
                        method="visibility_rules",
                        evidence=self._evidence(
                            path,
                            "definition",
                            int(symbol.get("start_line") or 1),
                            1,
                            int(symbol.get("end_line") or 1),
                            1,
                            str(symbol.get("content_hash") or ""),
                            file.get("language") or "",
                            "definition",
                        ),
                        props={},
                    )
                )
            for raw in _as_list(file.get("imports")):
                imp = raw if isinstance(raw, dict) else {}
                if not imp.get("is_reexport"):
                    continue
                target = self._resolve_module(file, imp)
                if target is None:
                    continue
                for name in _as_list(imp.get("imported_names")):
                    if name == "*":
                        continue
                    for symbol in self._symbols_in_module(target, str(name)):
                        plans.append(
                            self._edge_plan(
                                source_key=module_key,
                                target_key=("CodeSymbol", symbol["symbol_id"]),
                                edge_type="CODE_REEXPORTS",
                                status="resolved",
                                method="import_resolution",
                                evidence=None,
                                props={"imported_module": target},
                            )
                        )
        return plans

    # ── writing ──────────────────────────────────────────────────────────
    def _write(self, plans: list[dict[str, Any]]) -> None:
        batch_size = 200
        for start in range(0, len(plans), batch_size):
            chunk = plans[start:start + batch_size]
            with self.client.transaction.begin() as tx:
                for plan in chunk:
                    try:
                        self._apply(tx, plan)
                    except Exception:
                        logger.debug("resolver write skipped", exc_info=True)

    def _apply(self, tx: Any, plan: dict[str, Any]) -> None:
        if plan["kind"] == "node":
            tx.nodes.upsert(
                plan["node_type"], plan["key"], plan["props"]
            )
            return
        source_id = derived_id(*plan["source_key"])
        target_id = derived_id(*plan["target_key"])
        evidence = plan.get("evidence")
        evidence_ids: list[str] = []
        if evidence is not None:
            tx.nodes.upsert("CodeEvidence", evidence["key"], evidence["props"])
            evidence_ids.append(derived_id("CodeEvidence", evidence["key"]))
        status = plan["status"]
        confidence, confidence_method = STATUS_CONFIDENCE[status]
        props = {
            "evidence_ids": evidence_ids,
            "resolution_status": status,
            "confidence": confidence,
            "resolution_method": plan["method"],
            "confidence_method": confidence_method,
            **plan.get("props", {}),
        }
        tx.edges.upsert(source_id, target_id, plan["edge_type"], properties=props)

    def _write_unresolved_counts(self) -> None:
        with self.client.transaction.begin() as tx:
            for path, count in sorted(self.unresolved.items()):
                file_key = self._file_key(path)
                node = tx.nodes.get(derived_id("CodeFile", file_key))
                if node is None:
                    continue
                props = dict(node.properties)
                props["unresolved_count"] = count
                tx.nodes.upsert("CodeFile", file_key, props)

    # ── helpers ──────────────────────────────────────────────────────────
    def _edge_plan(
        self,
        *,
        source_key: tuple[str, str],
        target_key: tuple[str, str],
        edge_type: str,
        status: str,
        method: str,
        evidence: dict[str, Any] | None,
        props: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "kind": "edge",
            "source_key": source_key,
            "target_key": target_key,
            "edge_type": edge_type,
            "status": status,
            "method": method,
            "evidence": evidence,
            "props": props,
        }

    def _evidence(
        self,
        path: str,
        kind: str,
        start_line: int,
        start_column: int,
        end_line: int,
        end_column: int,
        excerpt_hash: str,
        language: str,
        source_kind: str,
    ) -> dict[str, Any]:
        key = (
            f"{self.repo.key}@{path}#{start_line}:{start_column}-"
            f"{end_line}:{end_column}:{kind}:tree-sitter:{excerpt_hash}"
        )
        return {
            "key": key,
            "props": {
                "repository_id": self.repo.repository_id,
                "evidence_kind": kind,
                "source_tool": "tree-sitter",
                "source_tool_version": "",
                "language": language,
                "file_path": path,
                "start_line": start_line,
                "end_line": end_line,
                "start_column": start_column,
                "end_column": end_column,
                "excerpt_hash": excerpt_hash,
                "analysis_run_id": self.run_key,
                "revision_id": self.revision_id,
                "observed_at": self._observed,
                "resolution_kind": source_kind,
            },
        }

    def _file_key(self, path: str) -> str:
        return f"{self.repo.key}@{path}"

    def _module_key(self, module: str, language: str | None) -> str:
        return f"{self.repo.key}@{language or ''}:{module}"


# ── path helpers ─────────────────────────────────────────────────────────
def _python_relative(file: dict[str, Any], module: str, level: int) -> str:
    current = str(file.get("module") or "")
    parts = current.split(".") if current else []
    if not str(file.get("path") or "").endswith("__init__.py"):
        parts = parts[:-1]
    keep = max(0, len(parts) - max(level - 1, 0))
    prefix = ".".join(parts[:keep])
    if module:
        return f"{prefix}.{module}" if prefix else module
    return prefix


def _path_relative(path: str, module: str) -> str:
    directory = posixpath.dirname(path)
    joined = posixpath.normpath(posixpath.join(directory, module)).removeprefix("./")
    for suffix in (".tsx", ".ts", ".mts", ".cts", ".jsx", ".js"):
        if joined.endswith(suffix):
            joined = joined[: -len(suffix)]
            break
    return joined.replace("/", ".")


def _rust_module(file: dict[str, Any], module: str) -> str:
    if module.startswith("crate::"):
        return _rust_candidate(module)
    current = str(file.get("module") or "")
    if module.startswith("self::"):
        suffix = module[len("self::"):]
        return f"{current}::{suffix}" if current else suffix
    if module.startswith("super::"):
        suffix = module[len("super::"):]
        parent = current.rsplit("::", 1)[0] if "::" in current else ""
        return f"{parent}::{suffix}" if parent else suffix
    return module


def _rust_candidate(module: str) -> str:
    if module.startswith("crate::"):
        return "src::" + module[len("crate::"):]
    return module


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []
