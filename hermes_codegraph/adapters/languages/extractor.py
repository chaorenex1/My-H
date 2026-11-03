"""The single Tree-sitter extractor for every code-graph language.

One traversal engine consumes a descriptor's ``tree_sitter_queries`` and its
``extraction`` grammar profile (node tables, field names, mode switches). A new
language adds data to ``languages.yaml`` and its ``.scm`` queries, never a
second traversal implementation.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ...languages import LanguageDescriptor
from .base import (
    ExtractedDiagnostic,
    ExtractedFile,
    ExtractedImport,
    ExtractedReference,
    ExtractedSymbol,
    ExtractedTest,
    ExtractedTypeRelation,
    excerpt_hash,
    exported,
    module_name_for,
    node_text,
    parse_source,
    qualified,
    sha256_bytes,
    signature_before,
    span,
)

_FUNCTION_NODES = frozenset(
    {
        "function_definition",
        "function_declaration",
        "generator_function_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
        "function_item",
        "function_signature_item",
        "method_declaration",
    }
)


@dataclass(frozen=True)
class _Profile:
    """The descriptor's ``extraction`` block, normalized."""

    module_mode: str
    container_fields: dict[str, str]
    container_kinds: dict[str, str]
    method_parents: dict[str, tuple[str, ...]]
    value_defs: dict[str, str]
    value_scope: str
    call_name_fields: dict[str, str]
    instantiation_nodes: tuple[str, ...]
    resolve_same_file: bool
    import_mode: str
    import_nodes: frozenset[str]
    type_name_nodes: tuple[str, ...]
    test_mode: str
    export_mode: str
    skip_defs: frozenset[str]
    self_container_fields: dict[str, str]
    owner_relations: dict[str, str]
    structural_interfaces: bool
    language_kind_from: dict[str, str]


def _profile(descriptor: LanguageDescriptor) -> _Profile:
    data = getattr(descriptor, "extraction", None)
    if not data:
        raise ValueError(
            f"{descriptor.language}: descriptor has no extraction profile"
        )
    return _Profile(
        module_mode=data.get("module_mode", "stem"),
        container_fields=dict(data.get("container_fields", {})),
        container_kinds=dict(data.get("container_kinds", {})),
        method_parents={
            node: tuple(kinds)
            for node, kinds in data.get("method_parents", {}).items()
        },
        value_defs=dict(data.get("value_defs", {})),
        value_scope=data.get("value_scope", "not_function"),
        call_name_fields=dict(data.get("call_name_fields", {})),
        instantiation_nodes=tuple(data.get("instantiation_nodes", ())),
        resolve_same_file=bool(data.get("resolve_same_file", False)),
        import_mode=data.get("import_mode", "python"),
        import_nodes=frozenset(data.get("import_nodes", ())),
        type_name_nodes=tuple(data.get("type_name_nodes", ())),
        test_mode=data.get("test_mode", "python"),
        export_mode=data.get("export_mode", "always"),
        skip_defs=frozenset(data.get("skip_defs", ())),
        self_container_fields=dict(data.get("self_container_fields", {})),
        owner_relations=dict(data.get("owner_relations", {})),
        structural_interfaces=bool(data.get("structural_interfaces", False)),
        language_kind_from=dict(data.get("language_kind_from", {})),
    )


_QUERY_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def extract(source: bytes, descriptor: LanguageDescriptor, path: str) -> ExtractedFile:
    profile = _profile(descriptor)
    module_name = _module_name(profile, descriptor, path)
    tree = parse_source(source, descriptor, path)
    root = tree.root_node
    queries = _queries(descriptor, path)

    def matches(purpose: str) -> list[tuple[int, dict[str, list[Any]]]]:
        from tree_sitter import QueryCursor

        return list(QueryCursor(queries[purpose]).matches(root))

    symbols = _collect_symbols(
        source, root, module_name, descriptor, profile, matches("symbols")
    )
    by_name = _symbols_by_name(symbols)
    references = _collect_references(
        source, profile, symbols, by_name, matches("calls")
    )
    imports = _collect_imports(source, profile, path, matches("imports"))
    relations = _collect_type_relations(
        source, module_name, profile, symbols, matches("types")
    )
    if profile.structural_interfaces:
        relations.extend(_structural_implementations(symbols))
    tests = _collect_tests(
        source, module_name, descriptor, profile, path, matches("tests")
    )

    diagnostics: list[ExtractedDiagnostic] = []
    status = "success"
    if root.has_error:
        status = "partial"
        diagnostics.append(
            ExtractedDiagnostic(
                line=1,
                column=1,
                code="parse_error",
                severity="warning",
                message="tree-sitter reported syntax errors; extracted the recoverable tree",
            )
        )
    return ExtractedFile(
        language=descriptor.language,
        module_name=module_name,
        symbols=symbols,
        references=references,
        diagnostics=diagnostics,
        parse_status=status,
        imports=imports,
        type_relations=relations,
        tests=tests,
        exports=tuple(
            symbol.name
            for symbol in symbols
            if symbol.is_exported and symbol.kind != "module"
        ),
    )


# ── queries ─────────────────────────────────────────────────────────────


def _queries(descriptor: LanguageDescriptor, path: str) -> dict[str, Any]:
    from tree_sitter import Query

    key = (
        descriptor.language,
        descriptor.parser_version,
        path.lower().endswith((".tsx", ".jsx")),
        str(descriptor.query_root),
    )
    cached = _QUERY_CACHE.get(key)
    if cached is not None:
        return cached
    from .base import language_object

    language = language_object(descriptor, path)
    root = descriptor.query_root
    if root is None:
        raise ValueError(f"{descriptor.language}: descriptor has no query root")
    compiled = {
        purpose: Query(language, (Path(root) / relative).read_text(encoding="utf-8"))
        for purpose, relative in descriptor.tree_sitter_queries.items()
    }
    _QUERY_CACHE[key] = compiled
    return compiled


# ── symbols ─────────────────────────────────────────────────────────────


def _collect_symbols(
    source: bytes,
    root: Any,
    module_name: str,
    descriptor: LanguageDescriptor,
    profile: _Profile,
    matches: Iterable[tuple[int, dict[str, list[Any]]]],
) -> list[ExtractedSymbol]:
    symbols = [_module_symbol(source, root, module_name, descriptor)]
    seen: set[tuple[int, str]] = set()
    for _index, captures in matches:
        definition = _first(captures, "symbol.def")
        name_nodes = captures.get("symbol.name") or []
        if not name_nodes:
            continue
        if definition is None:
            definition = name_nodes[0].parent
        if definition is None or definition.type in profile.skip_defs:
            continue
        if not _value_allowed(definition, profile):
            continue
        containers, kinds = _containers(source, definition, profile)
        captured_container = _first(captures, "symbol.container")
        if captured_container is not None:
            container = _base_type_name(source, captured_container, profile)
            if container and (not containers or containers[-1] != container):
                containers = [*containers, container]
                kinds = [*kinds, "container"]
        self_field = profile.self_container_fields.get(definition.type)
        if self_field:
            receiver = _base_type_name(
                source, definition.child_by_field_name(self_field), profile
            )
            if receiver:
                containers = [*containers, receiver]
                kinds = [*kinds, "receiver"]
        kind = _symbol_kind(source, definition, descriptor, profile, kinds)
        if kind is None:
            continue
        for name_node in name_nodes:
            name = node_text(source, name_node)
            if not name:
                continue
            key = (definition.start_byte, qualified(module_name, containers, name))
            if key in seen:
                continue
            seen.add(key)
            symbols.append(
                _symbol(
                    source,
                    definition,
                    name,
                    kind,
                    module_name,
                    containers,
                    descriptor,
                    profile,
                )
            )
    return symbols


def _value_allowed(node: Any, profile: _Profile) -> bool:
    if node.type not in profile.value_defs or profile.value_scope != "not_function":
        return True
    return not any(ancestor.type in _FUNCTION_NODES for ancestor in _ancestors(node))


def _module_name(
    profile: _Profile, descriptor: LanguageDescriptor, path: str
) -> str:
    posix = path.replace("\\", "/")
    if profile.module_mode == "directory":
        directory = posix.rsplit("/", 1)[0] if "/" in posix else ""
        if directory:
            return directory
        return module_name_for(posix, descriptor) or _stem(posix)
    return module_name_for(posix, descriptor) or _stem(posix)


def _stem(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def _module_symbol(
    source: bytes, root: Any, module_name: str, descriptor: LanguageDescriptor
) -> ExtractedSymbol:
    start_line, start_col, end_line, end_col = span(root)
    separator = descriptor.module_naming.get("separator", ".")
    return ExtractedSymbol(
        name=module_name.rsplit(separator, 1)[-1] or module_name,
        qualified_name=module_name,
        kind="module",
        language_kind="module",
        container="",
        start_line=start_line,
        end_line=end_line,
        start_column=start_col,
        end_column=end_col,
        start_byte=root.start_byte,
        end_byte=root.end_byte,
        signature=f"module {module_name}"[:500],
        content_hash=sha256_bytes(source),
        is_exported=True,
    )


def _containers(
    source: bytes, node: Any, profile: _Profile
) -> tuple[list[str], list[str]]:
    containers: list[str] = []
    kinds: list[str] = []
    for ancestor in _ancestors(node):
        field = profile.container_fields.get(ancestor.type)
        if field is None:
            continue
        name = _base_type_name(source, ancestor.child_by_field_name(field), profile)
        if not name and field == "name":
            name = _base_type_name(
                source, ancestor.child_by_field_name("name"), profile
            )
        if not name:
            continue
        if containers and containers[-1] == name:
            continue
        containers.append(name)
        kinds.append(profile.container_kinds.get(ancestor.type, "container"))
    return containers, kinds


def _symbol_kind(
    source: bytes,
    node: Any,
    descriptor: LanguageDescriptor,
    profile: _Profile,
    container_kinds: list[str],
) -> str | None:
    if node.type in profile.value_defs:
        rule = profile.value_defs[node.type]
        if rule == "python_assignment":
            name = node_text(source, node.child_by_field_name("left"))
            return "constant" if name.isupper() else "variable"
        if rule == "ts_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in (
                "arrow_function",
                "function_expression",
                "generator_function",
            ):
                return "function"
            return "constant" if _declaration_keyword(node) == "const" else "variable"
        return rule
    if node.type == "type_spec":
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            if type_node.type == "struct_type":
                return "struct"
            if type_node.type == "interface_type":
                return "interface"
        return "type_alias"
    kind = descriptor.kind_map.get(node.type) or _DEFAULT_KINDS.get(node.type)
    if kind is None:
        return None
    parents = profile.method_parents.get(node.type)
    if parents and container_kinds and container_kinds[-1] in parents:
        return "method"
    return kind


_DEFAULT_KINDS = {
    "class_definition": "class",
    "function_definition": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "method_definition": "method",
    "method_signature": "method",
    "property_signature": "property",
    "public_field_definition": "property",
    "type_alias_declaration": "type_alias",
    "enum_declaration": "enum",
    "function_item": "function",
    "function_signature_item": "method",
    "field_declaration": "field",
    "enum_variant": "variant",
    "const_item": "constant",
    "static_item": "variable",
    "type_item": "type_alias",
    "method_elem": "method",
}


def _symbol(
    source: bytes,
    node: Any,
    name: str,
    kind: str,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    profile: _Profile,
) -> ExtractedSymbol:
    body = node.child_by_field_name("body")
    source_field = profile.language_kind_from.get(node.type)
    language_kind = node.type
    if source_field:
        type_node = node.child_by_field_name(source_field)
        if type_node is not None:
            language_kind = type_node.type
    start_line, start_col, end_line, end_col = span(node)
    return ExtractedSymbol(
        name=name,
        qualified_name=qualified(module_name, containers, name),
        kind=kind,
        language_kind=language_kind,
        container=qualified(module_name, containers, "") if containers else "",
        start_line=start_line,
        end_line=end_line,
        start_column=start_col,
        end_column=end_col,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        signature=signature_before(source, node, body),
        content_hash=excerpt_hash(source, node),
        is_exported=_is_exported(source, node, name, descriptor, profile),
    )


# ── references ──────────────────────────────────────────────────────────


def _symbols_by_name(
    symbols: list[ExtractedSymbol],
) -> dict[str, list[ExtractedSymbol]]:
    by_name: dict[str, list[ExtractedSymbol]] = {}
    for symbol in symbols:
        if symbol.kind in ("module", "variable", "constant"):
            continue
        by_name.setdefault(symbol.name, []).append(symbol)
    return by_name


def _collect_references(
    source: bytes,
    profile: _Profile,
    symbols: list[ExtractedSymbol],
    by_name: dict[str, list[ExtractedSymbol]],
    matches: Iterable[tuple[int, dict[str, list[Any]]]],
) -> list[ExtractedReference]:
    references: list[ExtractedReference] = []
    for _index, captures in matches:
        node = _first(captures, "call.def")
        name_node = _first(captures, "call.name")
        if node is None or name_node is None:
            continue
        target_name = _call_name(source, name_node, profile)
        if not target_name:
            continue
        current = _innermost_symbol(symbols, node)
        if current is None:
            continue
        current_name = current.qualified_name
        instantiation = node.type in profile.instantiation_nodes
        target_qualified = ""
        method = "tree_sitter"
        if not instantiation and profile.resolve_same_file:
            candidates = by_name.get(target_name)
            if candidates is not None and len(candidates) == 1:
                target = candidates[0]
                if target.qualified_name != current_name:
                    target_qualified = target.qualified_name
                    instantiation = target.kind == "class"
                    method = "same_file_unique_name"
            elif candidates is not None:
                continue
        relation = "instantiates" if instantiation else "calls"
        start_line, start_col, end_line, end_col = span(node)
        references.append(
            ExtractedReference(
                source_qualified_name=current_name,
                target_qualified_name=target_qualified,
                evidence_kind="call_expression",
                resolution_method=method,
                start_line=start_line,
                end_line=end_line,
                start_column=start_col,
                end_column=end_col,
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                excerpt_hash=excerpt_hash(source, node),
                relation=relation,
                target_name=target_name,
                is_instantiation=instantiation,
            )
        )
    references.sort(key=lambda ref: (ref.start_line, ref.start_column))
    return references


def _innermost_symbol(
    symbols: list[ExtractedSymbol], node: Any
) -> ExtractedSymbol | None:
    best: ExtractedSymbol | None = None
    for symbol in symbols:
        if symbol.start_byte <= node.start_byte and symbol.end_byte >= node.end_byte:
            if best is None or symbol.start_byte >= best.start_byte:
                best = symbol
    return best


def _call_name(source: bytes, node: Any, profile: _Profile) -> str | None:
    if node.type in (
        "identifier",
        "field_identifier",
        "type_identifier",
        "property_identifier",
        "package_identifier",
    ):
        return node_text(source, node) or None
    field = profile.call_name_fields.get(node.type)
    if field == "__first__":
        return _call_name(source, node.children[0], profile) if node.children else None
    if field:
        return _call_name(source, node.child_by_field_name(field), profile) or None
    return None


# ── imports ─────────────────────────────────────────────────────────────


def _collect_imports(
    source: bytes,
    profile: _Profile,
    path: str,
    matches: Iterable[tuple[int, dict[str, list[Any]]]],
) -> list[ExtractedImport]:
    if profile.import_mode == "rust":
        return _collect_rust_imports(source, profile, matches)
    grouped: dict[int, tuple[Any, dict[str, list[Any]]]] = {}
    for _index, captures in matches:
        node = _first(captures, "import.module") or _first(captures, "import.name")
        if node is None:
            continue
        statement = _import_statement(node, profile)
        if statement is None:
            continue
        entry = grouped.setdefault(statement.start_byte, (statement, {}))
        for name, values in captures.items():
            entry[1].setdefault(name, []).extend(values)
    records: list[ExtractedImport] = []
    reexport_file = path.replace("\\", "/").endswith("__init__.py")
    for statement, captures in grouped.values():
        module_node = _first(captures, "import.module")
        module = node_text(source, module_node) if module_node is not None else ""
        if profile.import_mode == "typescript":
            module = module.strip("\"'`")
        elif profile.import_mode == "go":
            module = module.strip('"')
        names = tuple(
            sorted(
                {
                    node_text(source, node)
                    for node in captures.get("import.name", [])
                    if node_text(source, node)
                }
            )
        )
        aliases = [
            node_text(source, node) for node in captures.get("import.alias", [])
        ]
        alias = aliases[0] if aliases else None
        if profile.import_mode == "python":
            level = len(module) - len(module.lstrip("."))
            module = module.lstrip(".")
            relative = level > 0
            reexport = reexport_file
        elif profile.import_mode == "typescript":
            level = 0
            relative = module.startswith(".")
            reexport = statement.type == "export_statement"
        else:
            level = 0
            relative = False
            reexport = False
        records.append(
            _import_record(
                source, statement, module, names, alias, relative, reexport, level
            )
        )
    records.sort(key=lambda item: (item.start_line, item.start_column))
    return records


def _import_statement(node: Any, profile: _Profile) -> Any | None:
    current = node
    while current is not None:
        if current.type in profile.import_nodes:
            return current
        current = current.parent
    return None


def _collect_rust_imports(
    source: bytes,
    profile: _Profile,
    matches: Iterable[tuple[int, dict[str, list[Any]]]],
) -> list[ExtractedImport]:
    records: list[ExtractedImport] = []
    for _index, captures in matches:
        argument = _first(captures, "import.argument")
        if argument is None:
            continue
        statement = _import_statement(argument, profile)
        if statement is None:
            continue
        text = node_text(source, argument)
        reexport = any(
            child.type == "visibility_modifier" for child in statement.children
        )
        for module, names, alias in _parse_rust_use(text):
            records.append(
                _import_record(
                    source,
                    statement,
                    module,
                    tuple(names),
                    alias,
                    module.startswith(("crate", "self", "super")),
                    reexport,
                    0,
                )
            )
    records.sort(key=lambda item: (item.start_line, item.start_column))
    return records


def _parse_rust_use(text: str) -> list[tuple[str, list[str], str | None]]:
    cleaned = text.strip().rstrip(";").strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        items: list[tuple[str, list[str], str | None]] = []
        for part in _split_top_level(cleaned[1:-1]):
            items.extend(_parse_rust_use(part))
        return items
    if " as " in cleaned:
        base, _, alias = cleaned.partition(" as ")
        base = base.strip()
        if "{" in base:
            return _parse_rust_use(base)
        module, _, name = base.rpartition("::")
        return [(module or base, [name or base], alias.strip())]
    if "::{" in cleaned and cleaned.endswith("}"):
        module, _, body = cleaned.partition("::{")
        return [(module, _split_top_level(body[:-1]), None)]
    if cleaned.endswith("::*"):
        return [(cleaned[:-3], ["*"], None)]
    module, _, name = cleaned.rpartition("::")
    if module:
        return [(module, [name], None)]
    return [(cleaned, [], None)]


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part]


def _import_record(
    source: bytes,
    node: Any,
    module: str,
    names: tuple[str, ...],
    alias: str | None,
    relative: bool,
    reexport: bool,
    level: int,
) -> ExtractedImport:
    start_line, start_col, end_line, end_col = span(node)
    return ExtractedImport(
        module=module,
        imported_names=names,
        alias=alias,
        is_relative=relative,
        is_reexport=reexport,
        start_line=start_line,
        end_line=end_line,
        start_column=start_col,
        end_column=end_col,
        excerpt_hash=excerpt_hash(source, node),
        level=level,
    )


# ── type relations ──────────────────────────────────────────────────────


def _collect_type_relations(
    source: bytes,
    module_name: str,
    profile: _Profile,
    symbols: list[ExtractedSymbol],
    matches: Iterable[tuple[int, dict[str, list[Any]]]],
) -> list[ExtractedTypeRelation]:
    relations: list[ExtractedTypeRelation] = []
    seen: set[tuple[str, str, str, int]] = set()
    rows = list(matches)
    for _index, captures in rows:
        relation = next(
            (
                name.split(".", 1)[1]
                for name in captures
                if name.startswith("type.")
                and name.split(".", 1)[1]
                not in (
                    "owner",
                    "override_name",
                    "override_receiver",
                    "override_method",
                    "implemented_trait",
                )
            ),
            None,
        )
        if relation is None:
            continue
        node = _first(captures, f"type.{relation}")
        if node is None:
            continue
        if relation == "inherits" and _named_field(node):
            continue
        owner_node = _first(captures, "type.owner")
        owner = (
            _qualify_owner(source, module_name, profile, owner_node)
            if owner_node is not None
            else _relation_owner(source, profile, symbols, node)
        )
        if not owner:
            continue
        for target in _type_names(source, node, profile):
            key = (owner, target, relation, node.start_byte)
            if key in seen:
                continue
            seen.add(key)
            relations.append(_relation(source, node, owner, target, relation))
    for _index, captures in rows:
        receiver = _first(captures, "type.override_receiver")
        method_name = _first(captures, "type.override_name")
        if receiver is None or method_name is None:
            continue
        receiver_name = _base_type_name(source, receiver, profile)
        method = _innermost_symbol(symbols, method_name)
        if receiver_name and method is not None:
            target = f"{receiver_name}.{node_text(source, method_name)}"
            key = (method.qualified_name, target, "overrides", receiver.start_byte)
            if key not in seen:
                seen.add(key)
                relations.append(
                    _relation(
                        source, receiver, method.qualified_name, target, "overrides"
                    )
                )
    for _index, captures in rows:
        trait_node = _first(captures, "type.implemented_trait")
        owner_node = _first(captures, "type.owner")
        method_node = _first(captures, "type.override_method")
        if trait_node is None or owner_node is None or method_node is None:
            continue
        owner = _qualify_owner(source, module_name, profile, owner_node)
        trait_name = _base_type_name(source, trait_node, profile)
        method_name = node_text(source, method_node)
        if not owner or not trait_name or not method_name:
            continue
        source_name = f"{owner}.{method_name}"
        target = f"{trait_name}.{method_name}"
        key = (source_name, target, "overrides", method_node.start_byte)
        if key in seen:
            continue
        seen.add(key)
        relations.append(_relation(source, method_node, source_name, target, "overrides"))
    return relations


def _qualify_owner(
    source: bytes | None,
    module_name: str,
    profile: _Profile,
    node: Any,
) -> str | None:
    if source is None:
        return None
    name = _base_type_name(source, node, profile)
    if not name:
        return None
    containers, _ = _containers(source, node, profile)
    if containers and containers[-1] == name:
        containers = containers[:-1]
    return qualified(module_name, containers, name)


def _relation_owner(
    source: bytes,
    profile: _Profile,
    symbols: list[ExtractedSymbol],
    node: Any,
) -> str | None:
    symbol = _innermost_symbol(symbols, node)
    if symbol is None:
        return None
    parent = node.parent
    if (
        parent is not None
        and profile.owner_relations.get(parent.type) == "container"
        and symbol.container
    ):
        return symbol.container
    return symbol.qualified_name


def _relation(
    source: bytes, node: Any, owner: str, target: str, relation: str
) -> ExtractedTypeRelation:
    start_line, start_col, end_line, end_col = span(node)
    return ExtractedTypeRelation(
        source_qualified_name=owner,
        target_name=target,
        relation=relation,
        start_line=start_line,
        end_line=end_line,
        start_column=start_col,
        end_column=end_col,
        excerpt_hash=excerpt_hash(source, node),
    )


def _structural_implementations(
    symbols: list[ExtractedSymbol],
) -> list[ExtractedTypeRelation]:
    interfaces = [s for s in symbols if s.kind == "interface"]
    types = [s for s in symbols if s.kind in ("struct", "type_alias")]
    methods_by_container: dict[str, set[str]] = {}
    for symbol in symbols:
        if symbol.kind == "method" and symbol.container:
            methods_by_container.setdefault(symbol.container, set()).add(symbol.name)
    relations: list[ExtractedTypeRelation] = []
    for interface in interfaces:
        required = methods_by_container.get(interface.qualified_name, set())
        if not required:
            continue
        for candidate in types:
            provided = methods_by_container.get(candidate.qualified_name, set())
            if required <= provided:
                relations.append(
                    ExtractedTypeRelation(
                        source_qualified_name=candidate.qualified_name,
                        target_name=interface.name,
                        relation="implements",
                        start_line=candidate.start_line,
                        end_line=candidate.end_line,
                        start_column=candidate.start_column,
                        end_column=candidate.end_column,
                        excerpt_hash=candidate.content_hash,
                    )
                )
    return relations


def _type_names(source: bytes, node: Any, profile: _Profile) -> list[str]:
    names: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in profile.type_name_nodes:
            text = node_text(source, current).strip()
            if text and text.isidentifier() and text not in names:
                names.append(text)
            continue
        stack.extend(reversed(current.children))
    return names


# ── tests ───────────────────────────────────────────────────────────────


def _collect_tests(
    source: bytes,
    module_name: str,
    descriptor: LanguageDescriptor,
    profile: _Profile,
    path: str,
    matches: Iterable[tuple[int, dict[str, list[Any]]]],
) -> list[ExtractedTest]:
    if profile.test_mode == "typescript":
        return _collect_ts_tests(source, module_name, descriptor, matches)
    detection = descriptor.test_detection
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    patterns = detection.get("file_patterns", [])
    if patterns and not any(
        fnmatch.fnmatchcase(name, pattern) for pattern in patterns
    ):
        return []
    prefix = detection.get("function_prefix", "test")
    frameworks = detection.get("frameworks") or ["test"]
    tests: list[ExtractedTest] = []
    for _index, captures in matches:
        node = _first(captures, "test.def")
        name_node = _first(captures, "test.name")
        if node is None or name_node is None:
            continue
        test_name = node_text(source, name_node)
        if profile.test_mode in ("python", "go") and not test_name.startswith(prefix):
            continue
        containers, _ = _containers(source, node, profile)
        tests.append(
            _test_record(
                source,
                node,
                qualified(module_name, containers, test_name),
                test_name,
                str(frameworks[0]),
                _called_names(source, node, profile),
            )
        )
    return tests


def _collect_ts_tests(
    source: bytes,
    module_name: str,
    descriptor: LanguageDescriptor,
    matches: Iterable[tuple[int, dict[str, list[Any]]]],
) -> list[ExtractedTest]:
    frameworks = descriptor.test_detection.get("frameworks") or ["jest"]
    profile = _profile(descriptor)
    tests: list[ExtractedTest] = []
    for _index, captures in matches:
        node = _first(captures, "test.def")
        name_node = _first(captures, "test.name")
        arguments = _first(captures, "test.args")
        if node is None or name_node is None:
            continue
        call_name = node_text(source, name_node)
        if call_name not in ("test", "it", "describe"):
            continue
        title = ""
        callback = None
        if arguments is not None:
            for child in arguments.children:
                if child.type == "string" and not title:
                    title = node_text(source, child).strip("\"'`")
                elif child.type in (
                    "arrow_function",
                    "function_expression",
                    "function",
                ):
                    callback = child
        if callback is None:
            continue
        start_line = node.start_point[0] + 1
        tests.append(
            _test_record(
                source,
                node,
                qualified(
                    module_name, [], f"{title or call_name}@{start_line}"
                ),
                title or call_name,
                str(frameworks[0]),
                _called_names(source, callback, profile),
            )
        )
    return tests


def _test_record(
    source: bytes,
    node: Any,
    qualified_name: str,
    name: str,
    framework: str,
    targets: list[str],
) -> ExtractedTest:
    start_line, start_col, end_line, end_col = span(node)
    return ExtractedTest(
        qualified_name=qualified_name,
        name=name,
        framework=framework,
        target_names=tuple(sorted(set(targets))),
        start_line=start_line,
        end_line=end_line,
        start_column=start_col,
        end_column=end_col,
        excerpt_hash=excerpt_hash(source, node),
    )


def _called_names(source: bytes, node: Any, profile: _Profile) -> list[str]:
    names: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in ("call", "call_expression"):
            function = current.child_by_field_name("function")
            name = (
                _call_name(source, function, profile)
                if function is not None
                else None
            )
            if name:
                names.append(name)
        stack.extend(reversed(current.children))
    return names


# ── helpers ─────────────────────────────────────────────────────────────


def _ancestors(node: Any) -> list[Any]:
    chain: list[Any] = []
    current = node.parent
    while current is not None:
        chain.append(current)
        current = current.parent
    chain.reverse()
    return chain


def _first(captures: dict[str, list[Any]], name: str) -> Any | None:
    values = captures.get(name)
    return values[0] if values else None


def _named_field(node: Any) -> bool:
    parent = node.parent
    return (
        parent is not None
        and parent.type == "field_declaration"
        and parent.child_by_field_name("name") is not None
    )


def _base_type_name(
    source: bytes, node: Any | None, profile: _Profile
) -> str | None:
    if node is None:
        return None
    if node.type in (
        "type_identifier",
        "identifier",
        "field_identifier",
        "type",
    ):
        text = node_text(source, node)
        return text or None
    if node.type in (
        "pointer_type",
        "reference_type",
        "generic_type",
        "qualified_type",
        "scoped_type_identifier",
        "scoped_identifier",
        "parenthesized_type",
        "generic_function",
        "receiver",
    ):
        for child in node.children:
            name = _base_type_name(source, child, profile)
            if name:
                return name
        return None
    if node.type == "parameter_list":
        for child in node.children:
            name = _base_type_name(source, child, profile)
            if name:
                return name
        return None
    if node.type == "parameter_declaration":
        return _base_type_name(source, node.child_by_field_name("type"), profile)
    return None


def _declaration_keyword(node: Any) -> str:
    current = node.parent
    while current is not None:
        if current.type in ("lexical_declaration", "variable_declaration"):
            for child in current.children:
                if child.type in ("const", "let", "var"):
                    return child.type
        current = current.parent
    return ""


def _is_exported(
    source: bytes,
    node: Any,
    name: str,
    descriptor: LanguageDescriptor,
    profile: _Profile,
) -> bool:
    if profile.export_mode == "underscore":
        return exported(name, descriptor)
    if profile.export_mode == "uppercase":
        return bool(name) and name[0].isupper()
    if profile.export_mode == "rust":
        return any(child.type == "visibility_modifier" for child in node.children)
    return True
