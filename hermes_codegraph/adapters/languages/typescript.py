"""TypeScript/TSX Tree-sitter extractor; the descriptor lives in ``languages.yaml``."""

from __future__ import annotations

import fnmatch
from typing import Any

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

_CLASS_TYPES = ("class_declaration", "abstract_class_declaration")
_FUNCTION_TYPES = ("function_declaration", "generator_function_declaration")


def extract(source: bytes, descriptor: LanguageDescriptor, path: str) -> ExtractedFile:
    module_name = module_name_for(path, descriptor) or _stem(path)
    tree = parse_source(source, descriptor, path)
    root = tree.root_node
    symbols: list[ExtractedSymbol] = [_module_symbol(source, root, module_name)]
    _walk(source, root, module_name, [], [], descriptor, symbols)
    references = _extract_references(source, root, module_name, symbols)
    imports = _extract_imports(source, root)
    type_relations = _extract_type_relations(source, root, module_name)
    tests = _extract_tests(source, root, module_name, path, descriptor)
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
        language="typescript",
        module_name=module_name,
        symbols=symbols,
        references=references,
        diagnostics=diagnostics,
        parse_status=status,
        imports=imports,
        type_relations=type_relations,
        tests=tests,
        exports=tuple(
            symbol.name for symbol in symbols
            if symbol.is_exported and symbol.kind != "module"
        ),
    )


def _stem(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def _module_symbol(source: bytes, root: Any, module_name: str) -> ExtractedSymbol:
    start_line, start_col, end_line, end_col = span(root)
    return ExtractedSymbol(
        name=module_name.rsplit(".", 1)[-1] or module_name,
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


def _walk(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    container_kinds: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    for child in node.children:
        _handle(
            source, child, module_name, containers, container_kinds,
            descriptor, symbols,
        )


def _handle(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    container_kinds: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    kind = node.type
    if kind in ("export_statement", "ambient_declaration"):
        declaration = node.child_by_field_name("declaration") or _declaration_child(node)
        if declaration is not None:
            _handle(
                source, declaration, module_name, containers, container_kinds,
                descriptor, symbols,
            )
        return
    if kind in _CLASS_TYPES:
        _visit_container(source, node, "class", module_name, containers,
                         container_kinds, descriptor, symbols)
    elif kind == "interface_declaration":
        _visit_container(source, node, "interface", module_name, containers,
                         container_kinds, descriptor, symbols)
    elif kind in _FUNCTION_TYPES:
        _visit_function(source, node, "function", module_name, containers,
                        container_kinds, descriptor, symbols)
    elif kind == "method_definition":
        _visit_function(source, node, "method", module_name, containers,
                        container_kinds, descriptor, symbols)
    elif kind == "type_alias_declaration":
        _visit_name_only(source, node, "type_alias", module_name, containers,
                         descriptor, symbols)
    elif kind == "enum_declaration":
        _visit_name_only(source, node, "enum", module_name, containers,
                         descriptor, symbols)
    elif kind in ("public_field_definition", "property_signature"):
        _visit_name_only(source, node, "property", module_name, containers,
                         descriptor, symbols)
    elif kind == "method_signature":
        _visit_name_only(source, node, "method", module_name, containers,
                         descriptor, symbols)
    elif kind in ("lexical_declaration", "variable_declaration"):
        if not container_kinds or container_kinds[-1] != "function":
            _visit_variables(source, node, module_name, containers, descriptor,
                             symbols)


def _declaration_child(node: Any) -> Any | None:
    for child in node.children:
        if child.type not in ("export", "default", "declare", "comment"):
            return child
    return None


def _visit_container(
    source: bytes,
    node: Any,
    kind: str,
    module_name: str,
    containers: list[str],
    container_kinds: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(source, name_node)
    body = node.child_by_field_name("body")
    _append_symbol(source, node, name, kind, node.type, module_name, containers,
                   descriptor, symbols)
    if body is None:
        return
    if kind == "interface":
        _walk_interface_members(source, body, module_name, [*containers, name],
                                descriptor, symbols)
        return
    _walk(source, body, module_name, [*containers, name], [*container_kinds, kind],
          descriptor, symbols)


def _walk_interface_members(
    source: bytes,
    body: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    for child in body.children:
        if child.type in ("method_signature", "property_signature"):
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            kind = "method" if child.type == "method_signature" else "property"
            _append_symbol(source, child, node_text(source, name_node), kind,
                           child.type, module_name, containers, descriptor, symbols)


def _visit_function(
    source: bytes,
    node: Any,
    kind: str,
    module_name: str,
    containers: list[str],
    container_kinds: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(source, name_node)
    _append_symbol(source, node, name, kind, node.type, module_name, containers,
                   descriptor, symbols)
    body = node.child_by_field_name("body")
    if body is not None:
        _walk(source, body, module_name, [*containers, name],
              [*container_kinds, "function"], descriptor, symbols)


def _visit_name_only(
    source: bytes,
    node: Any,
    kind: str,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    _append_symbol(source, node, node_text(source, name_node), kind, node.type,
                   module_name, containers, descriptor, symbols)


def _visit_variables(
    source: bytes,
    declaration: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    keyword = ""
    for child in declaration.children:
        if child.type in ("const", "let", "var"):
            keyword = child.type
            break
    for declarator in declaration.children:
        if declarator.type != "variable_declarator":
            continue
        name_node = declarator.child_by_field_name("name")
        if name_node is None or name_node.type != "identifier":
            continue
        value = declarator.child_by_field_name("value")
        is_function = value is not None and value.type in (
            "arrow_function",
            "function_expression",
            "generator_function",
        )
        kind = "function" if is_function else ("constant" if keyword == "const" else "variable")
        _append_symbol(source, declarator, node_text(source, name_node), kind,
                       value.type if is_function and value is not None else declarator.type,
                       module_name, containers, descriptor, symbols)


def _append_symbol(
    source: bytes,
    node: Any,
    name: str,
    kind: str,
    language_kind: str,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    if not name:
        return
    body = node.child_by_field_name("body")
    start_line, start_col, end_line, end_col = span(node)
    symbols.append(
        ExtractedSymbol(
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
            is_exported=exported(name, descriptor),
        )
    )


def _extract_references(
    source: bytes,
    root: Any,
    module_name: str,
    symbols: list[ExtractedSymbol],
) -> list[ExtractedReference]:
    by_name: dict[str, list[ExtractedSymbol]] = {}
    for symbol in symbols:
        if symbol.kind in ("module", "variable", "constant"):
            continue
        by_name.setdefault(symbol.name, []).append(symbol)
    references: list[ExtractedReference] = []
    stack: list[tuple[Any, str]] = [(root, module_name)]
    while stack:
        node, current = stack.pop()
        if (
            node.type in _CLASS_TYPES
            or node.type in _FUNCTION_TYPES
            or node.type in ("method_definition",)
        ):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = node_text(source, name_node)
                current = f"{current}.{name}" if current else name
        elif node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if (
                name_node is not None
                and value is not None
                and value.type in ("arrow_function", "function_expression")
            ):
                name = node_text(source, name_node)
                current = f"{current}.{name}" if current else name
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            target_name = _call_name(source, function)
            if target_name:
                candidates = by_name.get(target_name)
                if candidates is not None and len(candidates) == 1:
                    target = candidates[0]
                    if target.qualified_name != current:
                        references.append(
                            _call_reference(
                                source, node, current, target.name,
                                target.qualified_name,
                                "instantiates" if target.kind == "class" else "calls",
                                target.kind == "class",
                                "same_file_unique_name",
                            )
                        )
                elif candidates is None:
                    references.append(
                        _call_reference(
                            source, node, current, target_name, "", "calls", False,
                            "tree_sitter",
                        )
                    )
        for child in reversed(node.children):
            stack.append((child, current))
    references.sort(key=lambda ref: (ref.start_line, ref.start_column))
    return references


def _call_reference(
    source: bytes,
    node: Any,
    current: str,
    target_name: str,
    target_qualified: str,
    relation: str,
    instantiation: bool,
    method: str,
) -> ExtractedReference:
    sl, sc, el, ec = span(node)
    return ExtractedReference(
        source_qualified_name=current,
        target_qualified_name=target_qualified,
        evidence_kind="call_expression",
        resolution_method=method,
        start_line=sl,
        end_line=el,
        start_column=sc,
        end_column=ec,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        excerpt_hash=excerpt_hash(source, node),
        relation=relation,
        target_name=target_name,
        is_instantiation=instantiation,
    )


def _call_name(source: bytes, function: Any | None) -> str | None:
    if function is None:
        return None
    if function.type == "identifier":
        return node_text(source, function) or None
    if function.type == "member_expression":
        prop = function.child_by_field_name("property")
        return node_text(source, prop) or None
    return None


# ── imports, type relations, tests ──────────────────────────────────────
def _string_value(source: bytes, node: Any | None) -> str:
    if node is None:
        return ""
    return node_text(source, node).strip("\"'`")


def _extract_imports(source: bytes, root: Any) -> list[ExtractedImport]:
    imports: list[ExtractedImport] = []
    for node in root.children:
        handler = _IMPORT_HANDLERS.get(node.type)
        if handler is not None:
            handler(source, node, imports)
    return imports


def _collect_import_statement(
    source: bytes, node: Any, imports: list[ExtractedImport]
) -> None:
    module = _string_value(source, node.child_by_field_name("source"))
    if not module:
        return
    clause = next(
        (child for child in node.children if child.type == "import_clause"), None
    )
    names: list[str] = []
    alias: str | None = None
    if clause is not None:
        for child in clause.children:
            if child.type == "identifier":
                names.append(node_text(source, child))
            elif child.type == "named_imports":
                for specifier in child.children:
                    if specifier.type != "import_specifier":
                        continue
                    name_node = specifier.child_by_field_name("name")
                    if name_node is not None:
                        names.append(node_text(source, name_node))
                    identifiers = [
                        item for item in specifier.children
                        if item.type == "identifier"
                    ]
                    if len(identifiers) >= 2:
                        alias = node_text(source, identifiers[-1])
            elif child.type == "namespace_import":
                identifiers = [
                    item for item in child.children if item.type == "identifier"
                ]
                if identifiers:
                    alias = node_text(source, identifiers[-1])
    imports.append(
        _import_record(
            source, node, module, tuple(names), alias,
            module.startswith("."), False,
        )
    )


def _collect_export_statement(
    source: bytes, node: Any, imports: list[ExtractedImport]
) -> None:
    source_node = node.child_by_field_name("source")
    if source_node is None:
        return
    module = _string_value(source, source_node)
    if not module:
        return
    names: list[str] = []
    for child in node.children:
        if child.type == "export_clause":
            for specifier in child.children:
                if specifier.type != "export_specifier":
                    continue
                name_node = specifier.child_by_field_name("name")
                if name_node is not None:
                    names.append(node_text(source, name_node))
        elif child.type == "*":
            names.append("*")
    imports.append(
        _import_record(
            source, node, module, tuple(names), None,
            module.startswith("."), True,
        )
    )


_IMPORT_HANDLERS = {
    "import_statement": _collect_import_statement,
    "export_statement": _collect_export_statement,
}


def _import_record(
    source: bytes,
    node: Any,
    module: str,
    names: tuple[str, ...],
    alias: str | None,
    relative: bool,
    reexport: bool,
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
    )


def _extract_type_relations(
    source: bytes, root: Any, module_name: str
) -> list[ExtractedTypeRelation]:
    relations: list[ExtractedTypeRelation] = []
    stack: list[tuple[Any, list[str]]] = [(root, [])]
    while stack:
        node, containers = stack.pop()
        handler = _TYPE_HANDLERS.get(node.type)
        if handler is not None and handler(
            source, node, containers, module_name, relations, stack
        ):
            continue
        for child in reversed(node.children):
            stack.append((child, containers))
    return relations


def _visit_type_container(
    source: bytes,
    node: Any,
    containers: list[str],
    module_name: str,
    relations: list[ExtractedTypeRelation],
    stack: list[tuple[Any, list[str]]],
) -> bool:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return True
    name = node_text(source, name_node)
    owner = qualified(module_name, containers, name)
    heritage = next(
        (child for child in node.children if child.type == "class_heritage"),
        None,
    )
    if heritage is not None:
        for clause in heritage.children:
            relation = {
                "extends_clause": "inherits",
                "implements_clause": "implements",
            }.get(clause.type)
            if relation is None:
                continue
            for target in _type_names(source, clause):
                _append_relation(
                    source, clause, owner, target, relation, relations
                )
    body = node.child_by_field_name("body")
    if body is not None:
        stack.append((body, [*containers, name]))
    return True


def _visit_type_function(
    source: bytes,
    node: Any,
    containers: list[str],
    module_name: str,
    relations: list[ExtractedTypeRelation],
    stack: list[tuple[Any, list[str]]],
) -> bool:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return True
    owner = qualified(module_name, containers, node_text(source, name_node))
    return_type = node.child_by_field_name("return_type")
    if return_type is not None:
        for target in _type_names(source, return_type):
            _append_relation(
                source, return_type, owner, target, "returns_type", relations
            )
    parameters = node.child_by_field_name("parameters")
    if parameters is not None:
        for parameter in _descendants(
            parameters, ("required_parameter", "optional_parameter")
        ):
            type_node = parameter.child_by_field_name("type")
            if type_node is None:
                continue
            for target in _type_names(source, type_node):
                _append_relation(
                    source, type_node, owner, target, "has_type", relations
                )
    body = node.child_by_field_name("body")
    if body is not None:
        stack.append((body, [*containers, node_text(source, name_node)]))
    return True


def _visit_type_field(
    source: bytes,
    node: Any,
    containers: list[str],
    module_name: str,
    relations: list[ExtractedTypeRelation],
    stack: list[tuple[Any, list[str]]],
) -> bool:
    name_node = node.child_by_field_name("name")
    type_node = node.child_by_field_name("type")
    if name_node is not None and type_node is not None:
        owner = qualified(module_name, containers, node_text(source, name_node))
        for target in _type_names(source, type_node):
            _append_relation(source, type_node, owner, target, "has_type", relations)
    return True


_TYPE_HANDLERS = {
    **{name: _visit_type_container for name in (*_CLASS_TYPES, "interface_declaration")},
    **{name: _visit_type_function for name in (*_FUNCTION_TYPES, "method_definition", "method_signature")},
    "public_field_definition": _visit_type_field,
    "property_signature": _visit_type_field,
}


def _type_names(source: bytes, node: Any) -> list[str]:
    if node is None:
        return []
    if node.type in ("type_identifier", "identifier"):
        text = node_text(source, node).strip()
        return [text] if text and text.isidentifier() else []
    if node.type in ("predefined_type", "literal_type", "string", "number"):
        return []
    found: list[str] = []
    for child in node.children:
        found.extend(_type_names(source, child))
    return found


def _append_relation(
    source: bytes,
    node: Any,
    owner: str,
    target: str,
    relation: str,
    relations: list[ExtractedTypeRelation],
) -> None:
    start_line, start_col, end_line, end_col = span(node)
    relations.append(
        ExtractedTypeRelation(
            source_qualified_name=owner,
            target_name=target,
            relation=relation,
            start_line=start_line,
            end_line=end_line,
            start_column=start_col,
            end_column=end_col,
            excerpt_hash=excerpt_hash(source, node),
        )
    )


def _extract_tests(
    source: bytes,
    root: Any,
    module_name: str,
    path: str,
    descriptor: LanguageDescriptor,
) -> list[ExtractedTest]:
    detection = descriptor.test_detection
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    patterns = detection.get("file_patterns", [])
    if not any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
        return []
    frameworks = detection.get("frameworks") or ["jest"]
    tests: list[ExtractedTest] = []
    for node in _descendants(root, ("call_expression",)):
        function = node.child_by_field_name("function")
        if function is None or function.type != "identifier":
            continue
        call_name = node_text(source, function)
        if call_name not in ("test", "it", "describe"):
            continue
        arguments = node.child_by_field_name("arguments")
        if arguments is None:
            continue
        title = ""
        callback = None
        for child in arguments.children:
            if child.type == "string" and not title:
                title = _string_value(source, child)
            elif child.type in ("arrow_function", "function_expression", "function"):
                callback = child
        if callback is None:
            continue
        start_line, start_col, end_line, end_col = span(node)
        tests.append(
            ExtractedTest(
                qualified_name=qualified(
                    module_name, [], f"{title or call_name}@{start_line}"
                ),
                name=title or call_name,
                framework=str(frameworks[0]),
                target_names=tuple(sorted(set(_called_names(source, callback)))),
                start_line=start_line,
                end_line=end_line,
                start_column=start_col,
                end_column=end_col,
                excerpt_hash=excerpt_hash(source, node),
            )
        )
    return tests


def _called_names(source: bytes, node: Any) -> list[str]:
    names: list[str] = []
    for call in _descendants(node, ("call_expression",)):
        function = call.child_by_field_name("function")
        target = _call_name(source, function)
        if target:
            names.append(target)
    return names


def _descendants(node: Any, types: tuple[str, ...]) -> list[Any]:
    found: list[Any] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in types:
            found.append(current)
        stack.extend(reversed(current.children))
    return found
