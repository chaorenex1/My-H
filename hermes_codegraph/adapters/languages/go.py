"""Go Tree-sitter extractor; the descriptor lives in ``languages.yaml``.

Extraction is syntax-only: symbols, imports, call/composite-literal
references, embedding/field/return type relations and statically detected
tests. Cross-file resolution is the resolver's job, so call targets are
emitted as local names here.
"""

from __future__ import annotations

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
    module_name_for,
    node_text,
    parse_source,
    qualified,
    sha256_bytes,
    signature_before,
    span,
)

_TEST_PREFIX = "Test"


def extract(source: bytes, descriptor: LanguageDescriptor, path: str) -> ExtractedFile:
    module_name = _go_module_name(path, descriptor)
    tree = parse_source(source, descriptor, path)
    root = tree.root_node
    symbols: list[ExtractedSymbol] = [_module_symbol(source, root, module_name)]
    imports: list[ExtractedImport] = []
    type_relations: list[ExtractedTypeRelation] = []
    tests: list[ExtractedTest] = []

    _collect_declarations(
        source, root, module_name, [], [], descriptor, symbols, imports,
        type_relations, tests, path,
    )
    references = _collect_references(source, root, module_name, symbols)
    _add_interface_implementations(symbols, type_relations)

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
        language="go",
        module_name=module_name,
        symbols=symbols,
        references=references,
        diagnostics=diagnostics,
        parse_status=status,
        imports=imports,
        type_relations=type_relations,
        tests=tests,
        exports=tuple(s.name for s in symbols if s.is_exported and s.kind != "module"),
    )


def _go_module_name(path: str, descriptor: LanguageDescriptor) -> str:
    """Go package identity is the directory, not the file stem."""
    posix = path.replace("\\", "/")
    directory = posix.rsplit("/", 1)[0] if "/" in posix else ""
    if directory:
        return directory
    fallback = module_name_for(posix, descriptor)
    return fallback or _stem(posix)


def _stem(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def _module_symbol(source: bytes, root: Any, module_name: str) -> ExtractedSymbol:
    start_line, start_col, end_line, end_col = span(root)
    return ExtractedSymbol(
        name=module_name.rsplit("/", 1)[-1] or module_name,
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


def _collect_declarations(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    container_kinds: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
    imports: list[ExtractedImport],
    type_relations: list[ExtractedTypeRelation],
    tests: list[ExtractedTest],
    path: str,
) -> None:
    for child in node.children:
        handler = _DECLARATION_HANDLERS.get(child.type)
        if handler is not None:
            handler(
                source, child, module_name, containers, container_kinds,
                descriptor, symbols, imports, type_relations, tests, path,
            )


_DECLARATION_HANDLERS = {
    "import_declaration": lambda source, node, module_name, containers, kinds,
        descriptor, symbols, imports, relations, tests, path: _collect_imports(
            source, node, imports
        ),
    "type_declaration": lambda source, node, module_name, containers, kinds,
        descriptor, symbols, imports, relations, tests, path: _collect_type_declaration(
            source, node, module_name, containers, descriptor, symbols, relations
        ),
    "function_declaration": lambda source, node, module_name, containers, kinds,
        descriptor, symbols, imports, relations, tests, path: _collect_function(
            source, node, module_name, containers, kinds, descriptor, symbols,
            relations, tests, path,
        ),
    "method_declaration": lambda source, node, module_name, containers, kinds,
        descriptor, symbols, imports, relations, tests, path: _collect_method(
            source, node, module_name, containers, descriptor, symbols,
            relations, tests, path,
        ),
    "const_declaration": lambda source, node, module_name, containers, kinds,
        descriptor, symbols, imports, relations, tests, path: _collect_values(
            source, node, module_name, containers, descriptor, symbols
        ),
    "var_declaration": lambda source, node, module_name, containers, kinds,
        descriptor, symbols, imports, relations, tests, path: _collect_values(
            source, node, module_name, containers, descriptor, symbols
        ),
    "block": lambda source, node, module_name, containers, kinds, descriptor,
        symbols, imports, relations, tests, path: _collect_declarations(
            source, node, module_name, containers, kinds, descriptor, symbols,
            imports, relations, tests, path,
        ),
    "statement_list": lambda source, node, module_name, containers, kinds,
        descriptor, symbols, imports, relations, tests, path: _collect_declarations(
            source, node, module_name, containers, kinds, descriptor, symbols,
            imports, relations, tests, path,
        ),
}


def _collect_imports(
    source: bytes, node: Any, imports: list[ExtractedImport]
) -> None:
    for spec in _descendants(node, "import_spec"):
        path_node = spec.child_by_field_name("path") or _child_of_type(
            spec, "interpreted_string_literal"
        )
        if path_node is None:
            continue
        module = node_text(source, path_node).strip('"')
        name_node = spec.child_by_field_name("name")
        start_line, start_col, end_line, end_col = span(spec)
        imports.append(
            ExtractedImport(
                module=module,
                imported_names=(),
                alias=node_text(source, name_node) or None,
                is_relative=False,
                is_reexport=False,
                start_line=start_line,
                end_line=end_line,
                start_column=start_col,
                end_column=end_col,
                excerpt_hash=excerpt_hash(source, spec),
            )
        )


def _collect_type_declaration(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
    type_relations: list[ExtractedTypeRelation],
) -> None:
    for spec in node.children:
        if spec.type != "type_spec":
            continue
        name_node = spec.child_by_field_name("name")
        if name_node is None:
            continue
        name = node_text(source, name_node)
        type_node = spec.child_by_field_name("type")
        language_kind = type_node.type if type_node is not None else "type_spec"
        if language_kind == "struct_type":
            kind = "struct"
        elif language_kind == "interface_type":
            kind = "interface"
        else:
            kind = "type_alias"
        _append_symbol(
            source, spec, name, kind, language_kind, module_name, containers,
            descriptor, symbols, exported=_is_exported(name, descriptor),
        )
        qualified_name = qualified(module_name, containers, name)
        if type_node is None:
            continue
        if language_kind == "struct_type":
            _collect_struct_fields(
                source, type_node, module_name, [*containers, name],
                descriptor, symbols, type_relations, qualified_name,
            )
        elif language_kind == "interface_type":
            _collect_interface_methods(
                source, type_node, module_name, [*containers, name],
                descriptor, symbols,
            )


def _collect_struct_fields(
    source: bytes,
    struct_node: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
    type_relations: list[ExtractedTypeRelation],
    owner_qualified: str,
) -> None:
    for field in _descendants(struct_node, "field_declaration"):
        name_node = field.child_by_field_name("name")
        type_node = field.child_by_field_name("type")
        if type_node is None:
            continue
        target = _base_type_name(source, type_node)
        if name_node is None:
            # Embedded field: Go's inheritance-like composition.
            if target:
                _append_type_relation(
                    source, field, owner_qualified, target, "inherits",
                    type_relations,
                )
            continue
        name = node_text(source, name_node)
        _append_symbol(
            source, field, name, "field", "field_declaration", module_name,
            containers, descriptor, symbols,
            exported=_is_exported(name, descriptor),
        )
        if target:
            _append_type_relation(
                source, field, qualified(module_name, containers, name),
                target, "has_type", type_relations,
            )


def _collect_interface_methods(
    source: bytes,
    interface_node: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    for method in _descendants(interface_node, "method_elem"):
        name_node = method.child_by_field_name("name")
        if name_node is None:
            continue
        name = node_text(source, name_node)
        _append_symbol(
            source, method, name, "method", "method_elem", module_name,
            containers, descriptor, symbols, exported=_is_exported(name, descriptor),
        )


def _collect_function(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    container_kinds: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
    type_relations: list[ExtractedTypeRelation],
    tests: list[ExtractedTest],
    path: str,
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(source, name_node)
    _append_symbol(
        source, node, name, "function", node.type, module_name, containers,
        descriptor, symbols, exported=_is_exported(name, descriptor),
    )
    qualified_name = qualified(module_name, containers, name)
    _maybe_test(source, node, qualified_name, name, path, tests)
    result = node.child_by_field_name("result")
    if result is not None:
        target = _base_type_name(source, result)
        if target:
            _append_type_relation(
                source, result, qualified_name, target, "returns_type",
                type_relations,
            )


def _collect_method(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
    type_relations: list[ExtractedTypeRelation],
    tests: list[ExtractedTest],
    path: str,
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(source, name_node)
    receiver = _receiver_type(source, node)
    method_containers = [*containers, *([receiver] if receiver else [])]
    _append_symbol(
        source, node, name, "method", node.type, module_name, method_containers,
        descriptor, symbols, exported=_is_exported(name, descriptor),
    )
    qualified_name = qualified(module_name, method_containers, name)
    _maybe_test(source, node, qualified_name, name, path, tests)
    result = node.child_by_field_name("result")
    if result is not None:
        target = _base_type_name(source, result)
        if target:
            _append_type_relation(
                source, result, qualified_name, target, "returns_type",
                type_relations,
            )
    if receiver:
        # Method receiver: method overrides the same method name in a base
        # struct only when that base exists; the resolver checks the target.
        _append_type_relation(
            source, node, qualified_name, f"{receiver}.{name}", "overrides",
            type_relations,
        )


def _collect_values(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    for spec in node.children:
        if spec.type not in ("const_spec", "var_spec"):
            continue
        for name_node in _children_of_type(spec, "identifier"):
            name = node_text(source, name_node)
            if not name:
                continue
            kind = "constant" if spec.type == "const_spec" else "variable"
            _append_symbol(
                source, spec, name, kind, spec.type, module_name, containers,
                descriptor, symbols, exported=_is_exported(name, descriptor),
            )


def _maybe_test(
    source: bytes,
    node: Any,
    qualified_name: str,
    name: str,
    path: str,
    tests: list[ExtractedTest],
) -> None:
    if not path.replace("\\", "/").endswith("_test.go"):
        return
    if not name.startswith(_TEST_PREFIX):
        return
    targets = _called_names(source, node)
    start_line, start_col, end_line, end_col = span(node)
    tests.append(
        ExtractedTest(
            qualified_name=qualified_name,
            name=name,
            framework="go-test",
            target_names=tuple(sorted(set(targets))),
            start_line=start_line,
            end_line=end_line,
            start_column=start_col,
            end_column=end_col,
            excerpt_hash=excerpt_hash(source, node),
        )
    )


def _collect_references(
    source: bytes,
    root: Any,
    module_name: str,
    symbols: list[ExtractedSymbol],
) -> list[ExtractedReference]:
    references: list[ExtractedReference] = []
    stack: list[tuple[Any, str]] = [(root, module_name)]
    while stack:
        node, current = stack.pop()
        if node.type in ("function_declaration", "method_declaration"):
            current = _current_for_declaration(source, node, module_name, current)
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            target = _call_target(source, function)
            if target:
                references.append(
                    _reference(source, node, current, target, "calls", False)
                )
        elif node.type == "composite_literal":
            type_node = node.child_by_field_name("type")
            target = _base_type_name(source, type_node) if type_node is not None else None
            if target:
                references.append(
                    _reference(source, node, current, target, "instantiates", True)
                )
        for child in reversed(node.children):
            stack.append((child, current))
    references.sort(key=lambda ref: (ref.start_line, ref.start_column))
    return references


def _current_for_declaration(
    source: bytes, node: Any, module_name: str, current: str
) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return current
    name = node_text(source, name_node)
    if node.type == "method_declaration":
        receiver = _receiver_type(source, node)
        if receiver:
            return qualified(module_name, [receiver], name)
    return qualified(module_name, [], name)


def _reference(
    source: bytes,
    node: Any,
    current: str,
    target: str,
    relation: str,
    instantiation: bool,
) -> ExtractedReference:
    start_line, start_col, end_line, end_col = span(node)
    return ExtractedReference(
        source_qualified_name=current,
        target_qualified_name="",
        evidence_kind="call_expression",
        resolution_method="tree_sitter",
        start_line=start_line,
        end_line=end_line,
        start_column=start_col,
        end_column=end_col,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        excerpt_hash=excerpt_hash(source, node),
        relation=relation,
        target_name=target,
        is_instantiation=instantiation,
    )


def _call_target(source: bytes, function: Any | None) -> str | None:
    if function is None:
        return None
    if function.type == "identifier":
        return node_text(source, function) or None
    if function.type == "selector_expression":
        field = function.child_by_field_name("field")
        return node_text(source, field) or None
    if function.type in ("index_expression", "generic_type", "parenthesized_expression"):
        return _call_target(source, function.children[0] if function.children else None)
    return None


def _called_names(source: bytes, node: Any) -> list[str]:
    names: list[str] = []
    for call in _descendants(node, "call_expression"):
        function = call.child_by_field_name("function")
        target = _call_target(source, function)
        if target:
            names.append(target)
    return names


def _receiver_type(source: bytes, node: Any) -> str | None:
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return None
    for child in receiver.children:
        if child.type == "parameter_declaration":
            type_node = child.child_by_field_name("type")
            return _base_type_name(source, type_node) if type_node is not None else None
    return None


def _base_type_name(source: bytes, node: Any | None) -> str | None:
    if node is None:
        return None
    if node.type in ("type_identifier", "identifier", "field_identifier"):
        return node_text(source, node) or None
    if node.type in ("pointer_type", "generic_type", "qualified_type", "parenthesized_type"):
        for child in node.children:
            name = _base_type_name(source, child)
            if name:
                return name
    return None


def _add_interface_implementations(
    symbols: list[ExtractedSymbol], relations: list[ExtractedTypeRelation]
) -> None:
    """Structural Go interfaces: a type implements when it declares every method."""
    interfaces = [s for s in symbols if s.kind == "interface"]
    types = [s for s in symbols if s.kind in ("struct", "type_alias")]
    methods_by_container: dict[str, set[str]] = {}
    for symbol in symbols:
        if symbol.kind == "method" and symbol.container:
            methods_by_container.setdefault(symbol.container, set()).add(symbol.name)
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


def _append_type_relation(
    source: bytes,
    node: Any,
    source_qualified: str,
    target: str,
    relation: str,
    relations: list[ExtractedTypeRelation],
) -> None:
    start_line, start_col, end_line, end_col = span(node)
    relations.append(
        ExtractedTypeRelation(
            source_qualified_name=source_qualified,
            target_name=target,
            relation=relation,
            start_line=start_line,
            end_line=end_line,
            start_column=start_col,
            end_column=end_col,
            excerpt_hash=excerpt_hash(source, node),
        )
    )


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
    *,
    exported: bool,
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
            is_exported=exported,
        )
    )


def _is_exported(name: str, descriptor: LanguageDescriptor) -> bool:
    if descriptor.visibility_rules.get("exported_when_uppercase"):
        return bool(name) and name[0].isupper()
    return True


def _descendants(node: Any, node_type: str) -> list[Any]:
    found: list[Any] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == node_type:
            found.append(current)
        stack.extend(reversed(current.children))
    return found


def _child_of_type(node: Any, node_type: str) -> Any | None:
    return next((child for child in node.children if child.type == node_type), None)


def _children_of_type(node: Any, node_type: str) -> list[Any]:
    return [child for child in node.children if child.type == node_type]
