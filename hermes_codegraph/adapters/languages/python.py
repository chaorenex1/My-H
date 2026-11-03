"""Python Tree-sitter extractor; the descriptor lives in ``languages.yaml``."""

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

_DEFINITION_TYPES = ("class_definition", "function_definition")


def extract(source: bytes, descriptor: LanguageDescriptor, path: str) -> ExtractedFile:
    module_name = module_name_for(path, descriptor) or _stem(path)
    tree = parse_source(source, descriptor, path)
    root = tree.root_node
    symbols: list[ExtractedSymbol] = [_module_symbol(source, root, module_name, descriptor)]
    _walk_definitions(source, root, module_name, [], [], descriptor, symbols)
    references = _extract_references(source, root, module_name, symbols)
    imports = _extract_imports(source, root, path)
    type_relations = _extract_type_relations(source, root, module_name, symbols)
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
        language="python",
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


def _module_symbol(
    source: bytes, root: Any, module_name: str, descriptor: LanguageDescriptor
) -> ExtractedSymbol:
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


def _definition_child(node: Any) -> Any | None:
    for child in node.children:
        if child.type in _DEFINITION_TYPES:
            return child
    return None


def _walk_definitions(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    container_kinds: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    for child in node.children:
        kind = child.type
        if kind == "decorated_definition":
            definition = _definition_child(child)
            if definition is not None:
                _visit_definition(
                    source, definition, module_name, containers, container_kinds,
                    descriptor, symbols,
                )
        elif kind in _DEFINITION_TYPES:
            _visit_definition(
                source, child, module_name, containers, container_kinds,
                descriptor, symbols,
            )
        elif kind == "expression_statement" and not container_kinds.count("function"):
            _visit_assignment(source, child, module_name, containers, descriptor, symbols)
        elif kind in ("if_statement", "try_statement", "with_statement"):
            # Module/class-level conditionals still define importable names.
            if not container_kinds or container_kinds[-1] != "function":
                _walk_definitions(
                    source, child, module_name, containers, container_kinds,
                    descriptor, symbols,
                )


def _visit_definition(
    source: bytes,
    node: Any,
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
    if not name:
        return
    if node.type == "class_definition":
        kind = "class"
    elif container_kinds and container_kinds[-1] == "class":
        kind = "method"
    else:
        kind = "function"
    body = node.child_by_field_name("body")
    start_line, start_col, end_line, end_col = span(node)
    symbols.append(
        ExtractedSymbol(
            name=name,
            qualified_name=qualified(module_name, containers, name),
            kind=kind,
            language_kind=node.type,
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
    if body is not None:
        _walk_definitions(
            source, body, module_name, [*containers, name], [*container_kinds, kind],
            descriptor, symbols,
        )


def _visit_assignment(
    source: bytes,
    statement: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    assignment = next(
        (child for child in statement.children if child.type == "assignment"), None
    )
    if assignment is None:
        return
    left = assignment.child_by_field_name("left")
    if left is None or left.type != "identifier":
        return
    name = node_text(source, left)
    if not name:
        return
    kind = "constant" if name.isupper() else "variable"
    start_line, start_col, end_line, end_col = span(assignment)
    symbols.append(
        ExtractedSymbol(
            name=name,
            qualified_name=qualified(module_name, containers, name),
            kind=kind,
            language_kind="assignment",
            container=qualified(module_name, containers, "") if containers else "",
            start_line=start_line,
            end_line=end_line,
            start_column=start_col,
            end_column=end_col,
            start_byte=assignment.start_byte,
            end_byte=assignment.end_byte,
            signature=node_text(source, assignment)[:500],
            content_hash=excerpt_hash(source, assignment),
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
        if node.type in ("class_definition", "function_definition"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = node_text(source, name_node)
                current = f"{current}.{name}" if current else name
        elif node.type == "call":
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
    if function.type == "attribute":
        attribute = function.child_by_field_name("attribute")
        return node_text(source, attribute) or None
    return None


# ── imports, type relations, tests ──────────────────────────────────────
_IMPORT_TYPES = ("import_statement", "import_from_statement")


def _extract_imports(source: bytes, root: Any, path: str) -> list[ExtractedImport]:
    imports: list[ExtractedImport] = []
    reexport_file = path.replace("\\", "/").endswith("__init__.py")
    for node in _descendants_of(root, _IMPORT_TYPES):
        if node.type == "import_statement":
            _collect_plain_imports(source, node, imports)
        else:
            _collect_from_import(source, node, imports, reexport_file)
    return imports


def _collect_plain_imports(
    source: bytes, node: Any, imports: list[ExtractedImport]
) -> None:
    seen_import = False
    for child in node.children:
        if child.type == "import":
            seen_import = True
            continue
        if not seen_import or child.type not in ("dotted_name", "aliased_import"):
            continue
        alias = None
        name_node = child
        if child.type == "aliased_import":
            name_node = child.child_by_field_name("name") or child
            identifiers = [item for item in child.children if item.type == "identifier"]
            alias = node_text(source, identifiers[-1]) if identifiers else None
        module = node_text(source, name_node)
        if not module:
            continue
        imports.append(_import_record(source, node, module, (), alias, False, False, 0))


def _collect_from_import(
    source: bytes, node: Any, imports: list[ExtractedImport], reexport: bool
) -> None:
    module_node = node.child_by_field_name("module_name")
    module_text = node_text(source, module_node) if module_node is not None else ""
    level = len(module_text) - len(module_text.lstrip("."))
    module = module_text.lstrip(".")
    names: list[str] = []
    alias: str | None = None
    seen_import = False
    for child in node.children:
        if child.type == "import":
            seen_import = True
            continue
        if not seen_import:
            continue
        if child.type == "dotted_name":
            names.append(node_text(source, child))
        elif child.type == "aliased_import":
            name_node = child.child_by_field_name("name") or child
            names.append(node_text(source, name_node))
            identifiers = [item for item in child.children if item.type == "identifier"]
            if len(identifiers) >= 2:
                alias = node_text(source, identifiers[-1])
        elif child.type == "wildcard_import":
            names.append("*")
    imports.append(
        _import_record(
            source, node, module, tuple(names), alias, level > 0, reexport, level
        )
    )


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


def _extract_type_relations(
    source: bytes,
    root: Any,
    module_name: str,
    symbols: list[ExtractedSymbol],
) -> list[ExtractedTypeRelation]:
    relations: list[ExtractedTypeRelation] = []
    stack: list[tuple[Any, list[str]]] = [(root, [])]
    while stack:
        node, containers = stack.pop()
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                class_name = node_text(source, name_node)
                owner = qualified(module_name, containers, class_name)
                bases = node.child_by_field_name("superclasses")
                if bases is not None:
                    for base in bases.children:
                        for target in _type_names(source, base):
                            _append_relation(
                                source, base, owner, target, "inherits", relations
                            )
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, [*containers, class_name]))
                continue
        elif node.type in ("function_definition", "lambda"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                owner = qualified(module_name, containers, node_text(source, name_node))
                return_type = node.child_by_field_name("return_type")
                if return_type is not None:
                    for target in _type_names(source, return_type):
                        _append_relation(
                            source, return_type, owner, target, "returns_type",
                            relations,
                        )
                parameters = node.child_by_field_name("parameters")
                if parameters is not None:
                    for parameter in _descendants_of(
                        parameters, ("typed_parameter", "typed_default_parameter")
                    ):
                        type_node = parameter.child_by_field_name("type")
                        if type_node is None:
                            continue
                        for target in _type_names(source, type_node):
                            _append_relation(
                                source, type_node, owner, target, "has_type",
                                relations,
                            )
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, [*containers, node_text(source, name_node)]))
                continue
        for child in reversed(node.children):
            stack.append((child, containers))
    return relations


def _type_names(source: bytes, node: Any) -> list[str]:
    if node is None:
        return []
    if node.type in ("identifier", "type"):
        text = node_text(source, node).strip()
        return [text] if text and text.isidentifier() else []
    if node.type == "attribute":
        attribute = node.child_by_field_name("attribute")
        if attribute is not None:
            return [node_text(source, attribute)]
        return []
    if node.type in ("subscript", "generic_type"):
        value = node.child_by_field_name("value")
        if value is not None:
            return _type_names(source, value)
        return []
    if node.type == "binary_operator":
        found: list[str] = []
        for child in node.children:
            found.extend(_type_names(source, child))
        return found
    if node.type in ("string", "string_content"):
        return []
    found = []
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
    prefix = detection.get("function_prefix", "test")
    class_prefix = detection.get("class_prefix", "Test")
    frameworks = detection.get("frameworks") or ["pytest"]
    tests: list[ExtractedTest] = []
    stack: list[tuple[Any, list[str], bool]] = [(root, [], False)]
    while stack:
        node, containers, in_test_class = stack.pop()
        if node.type == "class_definition":
            class_node = node.child_by_field_name("name")
            class_name = node_text(source, class_node) if class_node is not None else ""
            body = node.child_by_field_name("body")
            if body is not None:
                stack.append(
                    (
                        body,
                        [*containers, class_name] if class_name else containers,
                        in_test_class or class_name.startswith(class_prefix),
                    )
                )
            continue
        if node.type == "function_definition":
            function_name = node.child_by_field_name("name")
            function_text = (
                node_text(source, function_name) if function_name is not None else ""
            )
            is_test = function_text.startswith(prefix) and (
                not containers or in_test_class or function_text.startswith(prefix)
            )
            if is_test and function_text:
                qualified_name = qualified(module_name, containers, function_text)
                start_line, start_col, end_line, end_col = span(node)
                tests.append(
                    ExtractedTest(
                        qualified_name=qualified_name,
                        name=function_text,
                        framework=str(frameworks[0]),
                        target_names=tuple(sorted(set(_called_names(source, node)))),
                        start_line=start_line,
                        end_line=end_line,
                        start_column=start_col,
                        end_column=end_col,
                        excerpt_hash=excerpt_hash(source, node),
                    )
                )
            body = node.child_by_field_name("body")
            if body is not None:
                stack.append(
                    (
                        body,
                        [*containers, function_text] if function_text else containers,
                        in_test_class,
                    )
                )
            continue
        for child in reversed(node.children):
            stack.append((child, containers, in_test_class))
    return tests


def _called_names(source: bytes, node: Any) -> list[str]:
    names: list[str] = []
    for call in _descendants_of(node, ("call",)):
        function = call.child_by_field_name("function")
        name = _call_name(source, function)
        if name:
            names.append(name)
    return names


def _descendants_of(node: Any, types: tuple[str, ...]) -> list[Any]:
    found: list[Any] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in types:
            found.append(current)
        stack.extend(reversed(current.children))
    return found
