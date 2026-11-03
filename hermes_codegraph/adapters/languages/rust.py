"""Rust Tree-sitter extractor; the descriptor lives in ``languages.yaml``."""

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

_CONTAINER_TYPES = ("struct_item", "enum_item", "trait_item", "mod_item", "union_item")


def extract(source: bytes, descriptor: LanguageDescriptor, path: str) -> ExtractedFile:
    module_name = module_name_for(path, descriptor) or _stem(path)
    tree = parse_source(source, descriptor, path)
    root = tree.root_node
    symbols: list[ExtractedSymbol] = [_module_symbol(source, root, module_name)]
    imports: list[ExtractedImport] = []
    type_relations: list[ExtractedTypeRelation] = []
    tests: list[ExtractedTest] = []

    _collect_declarations(
        source, root, module_name, [], [], descriptor, symbols, imports,
        type_relations, tests,
    )
    references = _collect_references(source, root, module_name, symbols)

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
        language="rust",
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


def _stem(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0]


def _module_symbol(source: bytes, root: Any, module_name: str) -> ExtractedSymbol:
    start_line, start_col, end_line, end_col = span(root)
    return ExtractedSymbol(
        name=module_name.rsplit("::", 1)[-1] or module_name,
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
) -> None:
    for child in node.children:
        handler = _DECLARATION_HANDLERS.get(child.type)
        if handler is not None:
            handler(
                source, child, module_name, containers, container_kinds,
                descriptor, symbols, imports, type_relations, tests,
            )


_DECLARATION_HANDLERS = {
    "use_declaration": lambda source, node, module_name, containers, kinds,
        descriptor, symbols, imports, relations, tests: _collect_use(
            source, node, imports
        ),
    "mod_item": lambda source, node, module_name, containers, kinds, descriptor,
        symbols, imports, relations, tests: _collect_mod(
            source, node, module_name, containers, kinds, descriptor, symbols,
            imports, relations, tests,
        ),
    "struct_item": lambda source, node, module_name, containers, kinds, descriptor,
        symbols, imports, relations, tests: _collect_struct(
            source, node, module_name, containers, descriptor, symbols, relations
        ),
    "union_item": lambda source, node, module_name, containers, kinds, descriptor,
        symbols, imports, relations, tests: _collect_struct(
            source, node, module_name, containers, descriptor, symbols, relations
        ),
    "enum_item": lambda source, node, module_name, containers, kinds, descriptor,
        symbols, imports, relations, tests: _collect_enum(
            source, node, module_name, containers, descriptor, symbols, relations
        ),
    "trait_item": lambda source, node, module_name, containers, kinds, descriptor,
        symbols, imports, relations, tests: _collect_trait(
            source, node, module_name, containers, descriptor, symbols,
            relations, tests,
        ),
    "impl_item": lambda source, node, module_name, containers, kinds, descriptor,
        symbols, imports, relations, tests: _collect_impl(
            source, node, module_name, containers, descriptor, symbols,
            relations, tests,
        ),
    "function_item": lambda source, node, module_name, containers, kinds,
        descriptor, symbols, imports, relations, tests: _collect_function(
            source, node, module_name, containers, kinds, descriptor, symbols,
            relations, tests,
        ),
    "function_signature_item": lambda source, node, module_name, containers, kinds,
        descriptor, symbols, imports, relations, tests: _collect_function(
            source, node, module_name, containers, kinds, descriptor, symbols,
            relations, tests,
        ),
    "const_item": lambda source, node, module_name, containers, kinds, descriptor,
        symbols, imports, relations, tests: _collect_value(
            source, node, module_name, containers, descriptor, symbols
        ),
    "static_item": lambda source, node, module_name, containers, kinds, descriptor,
        symbols, imports, relations, tests: _collect_value(
            source, node, module_name, containers, descriptor, symbols
        ),
    "type_item": lambda source, node, module_name, containers, kinds, descriptor,
        symbols, imports, relations, tests: _collect_type_alias(
            source, node, module_name, containers, descriptor, symbols, relations
        ),
}


def _collect_use(source: bytes, node: Any, imports: list[ExtractedImport]) -> None:
    argument = next(
        (child for child in node.children if child.type not in ("use", ";", "visibility_modifier")),
        None,
    )
    if argument is None:
        return
    text = node_text(source, argument)
    for module, names, alias in _parse_use(text):
        start_line, start_col, end_line, end_col = span(node)
        imports.append(
            ExtractedImport(
                module=module,
                imported_names=tuple(names),
                alias=alias,
                is_relative=module.startswith(("crate", "self", "super")),
                is_reexport=_is_public(source, node),
                start_line=start_line,
                end_line=end_line,
                start_column=start_col,
                end_column=end_col,
                excerpt_hash=excerpt_hash(source, node),
            )
        )


def _parse_use(text: str) -> list[tuple[str, list[str], str | None]]:
    cleaned = text.strip().rstrip(";").strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        items: list[tuple[str, list[str], str | None]] = []
        for part in _split_top_level(cleaned[1:-1]):
            items.extend(_parse_use(part))
        return items
    if " as " in cleaned:
        base, _, alias = cleaned.partition(" as ")
        base = base.strip()
        if "{" in base:
            return _parse_use(base)
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


def _collect_mod(
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
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(source, name_node)
    _append_symbol(
        source, node, name, "module", node.type, module_name, containers,
        descriptor, symbols, exported=_is_public(source, node),
    )
    body = node.child_by_field_name("body")
    if body is not None:
        _collect_declarations(
            source, body, module_name, [*containers, name], [*container_kinds, "module"],
            descriptor, symbols, imports, type_relations, tests,
        )


def _collect_struct(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
    type_relations: list[ExtractedTypeRelation],
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(source, name_node)
    kind = "struct" if node.type == "struct_item" else "union"
    _append_symbol(
        source, node, name, kind, node.type, module_name, containers, descriptor,
        symbols, exported=_is_public(source, node),
    )
    owner = qualified(module_name, containers, name)
    body = node.child_by_field_name("body")
    if body is None:
        return
    for field in _descendants(body, "field_declaration"):
        field_name = field.child_by_field_name("name")
        type_node = field.child_by_field_name("type")
        if field_name is None or type_node is None:
            continue
        field_text = node_text(source, field_name)
        _append_symbol(
            source, field, field_text, "field", "field_declaration", module_name,
            [*containers, name], descriptor, symbols,
            exported=_is_public(source, field),
        )
        target = _type_name(source, type_node)
        if target:
            _append_type_relation(
                source, field, owner, target, "has_type", type_relations
            )


def _collect_enum(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
    type_relations: list[ExtractedTypeRelation],
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(source, name_node)
    _append_symbol(
        source, node, name, "enum", node.type, module_name, containers, descriptor,
        symbols, exported=_is_public(source, node),
    )
    body = node.child_by_field_name("body")
    if body is None:
        return
    for variant in _descendants(body, "enum_variant"):
        variant_name = variant.child_by_field_name("name")
        if variant_name is None:
            continue
        _append_symbol(
            source, variant, node_text(source, variant_name), "variant",
            "enum_variant", module_name, [*containers, name], descriptor, symbols,
            exported=_is_public(source, variant),
        )


def _collect_trait(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
    type_relations: list[ExtractedTypeRelation],
    tests: list[ExtractedTest],
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(source, name_node)
    _append_symbol(
        source, node, name, "trait", node.type, module_name, containers, descriptor,
        symbols, exported=_is_public(source, node),
    )
    owner = qualified(module_name, containers, name)
    bounds = node.child_by_field_name("bounds") or _child_of_type(node, "trait_bounds")
    if bounds is not None:
        for child in bounds.children:
            target = _type_name(source, child)
            if target:
                _append_type_relation(
                    source, child, owner, target, "inherits", type_relations
                )
    body = node.child_by_field_name("body")
    if body is None:
        return
    for child in body.children:
        if child.type in ("function_item", "function_signature_item"):
            _collect_function(
                source, child, module_name, [*containers, name], ["trait"],
                descriptor, symbols, type_relations, tests,
            )


def _collect_impl(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
    type_relations: list[ExtractedTypeRelation],
    tests: list[ExtractedTest],
) -> None:
    type_node = node.child_by_field_name("type")
    if type_node is None:
        return
    type_name = _type_name(source, type_node)
    if not type_name:
        return
    trait_node = node.child_by_field_name("trait")
    trait_name = _type_name(source, trait_node) if trait_node is not None else None
    owner = qualified(module_name, containers, type_name)
    if trait_name:
        _append_type_relation(
            source, trait_node, owner, trait_name, "implements", type_relations
        )
    body = node.child_by_field_name("body")
    if body is None:
        return
    for child in body.children:
        if child.type in ("function_item", "function_signature_item"):
            before = len(symbols)
            _collect_function(
                source, child, module_name, [*containers, type_name], ["impl"],
                descriptor, symbols, type_relations, tests,
            )
            if trait_name and len(symbols) > before:
                method = symbols[before]
                _append_type_relation(
                    source, child, method.qualified_name,
                    f"{trait_name}.{method.name}", "overrides", type_relations,
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
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(source, name_node)
    kind = "method" if container_kinds and container_kinds[-1] in ("impl", "trait") else "function"
    _append_symbol(
        source, node, name, kind, node.type, module_name, containers, descriptor,
        symbols, exported=_is_public(source, node),
    )
    qualified_name = qualified(module_name, containers, name)
    if _has_test_attribute(node):
        targets = _called_names(source, node)
        start_line, start_col, end_line, end_col = span(node)
        tests.append(
            ExtractedTest(
                qualified_name=qualified_name,
                name=name,
                framework="cargo-test",
                target_names=tuple(sorted(set(targets))),
                start_line=start_line,
                end_line=end_line,
                start_column=start_col,
                end_column=end_col,
                excerpt_hash=excerpt_hash(source, node),
            )
        )
    result = node.child_by_field_name("return_type") or _return_type(node)
    if result is not None:
        target = _type_name(source, result)
        if target:
            _append_type_relation(
                source, result, qualified_name, target, "returns_type",
                type_relations,
            )
    for parameter in _descendants(node.child_by_field_name("parameters") or node, "parameter"):
        type_node = parameter.child_by_field_name("type")
        if type_node is None:
            continue
        target = _type_name(source, type_node)
        if target:
            _append_type_relation(
                source, parameter, qualified_name, target, "has_type",
                type_relations,
            )


def _collect_value(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    kind = "constant" if node.type == "const_item" else "variable"
    _append_symbol(
        source, node, node_text(source, name_node), kind, node.type, module_name,
        containers, descriptor, symbols, exported=_is_public(source, node),
    )


def _collect_type_alias(
    source: bytes,
    node: Any,
    module_name: str,
    containers: list[str],
    descriptor: LanguageDescriptor,
    symbols: list[ExtractedSymbol],
    type_relations: list[ExtractedTypeRelation],
) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = node_text(source, name_node)
    _append_symbol(
        source, node, name, "type_alias", node.type, module_name, containers,
        descriptor, symbols, exported=_is_public(source, node),
    )
    type_node = node.child_by_field_name("type")
    if type_node is not None:
        target = _type_name(source, type_node)
        if target:
            _append_type_relation(
                source, type_node, qualified(module_name, containers, name),
                target, "has_type", type_relations,
            )


def _collect_references(
    source: bytes,
    root: Any,
    module_name: str,
    symbols: list[ExtractedSymbol],
) -> list[ExtractedReference]:
    references: list[ExtractedReference] = []
    stack: list[tuple[Any, str, list[str]]] = [(root, module_name, [])]
    while stack:
        node, current, containers = stack.pop()
        if node.type == "impl_item":
            type_name = _type_name(source, node.child_by_field_name("type"))
            if type_name:
                containers = [*containers, type_name]
                current = qualified(module_name, containers, "")
        if node.type in ("function_item", "function_signature_item"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                current = qualified(module_name, containers, node_text(source, name_node))
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            target = _call_target(source, function)
            if target:
                references.append(_reference(source, node, current, target, "calls", False))
        elif node.type == "struct_expression":
            type_node = node.child_by_field_name("name") or node.child_by_field_name("type")
            target = _type_name(source, type_node) if type_node is not None else None
            if target:
                references.append(
                    _reference(source, node, current, target, "instantiates", True)
                )
        for child in reversed(node.children):
            stack.append((child, current, containers))
    references.sort(key=lambda ref: (ref.start_line, ref.start_column))
    return references


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
    if function.type == "field_expression":
        field = function.child_by_field_name("field")
        return node_text(source, field) or None
    if function.type in ("scoped_identifier", "generic_function"):
        name_node = function.child_by_field_name("name")
        if name_node is not None:
            return node_text(source, name_node) or None
        return _call_target(source, function.children[-1] if function.children else None)
    return None


def _called_names(source: bytes, node: Any) -> list[str]:
    names: list[str] = []
    for call in _descendants(node, "call_expression"):
        function = call.child_by_field_name("function")
        target = _call_target(source, function)
        if target:
            names.append(target)
    return names


def _return_type(node: Any) -> Any | None:
    for child in node.children:
        if child.type in ("type_identifier", "scoped_type_identifier", "generic_type", "primitive_type", "reference_type", "tuple_type"):
            return child
    return None


def _has_test_attribute(node: Any) -> bool:
    sibling = node.prev_named_sibling
    while sibling is not None and sibling.type == "attribute_item":
        if "test" in sibling.text.decode("utf-8", errors="replace"):
            return True
        sibling = sibling.prev_named_sibling
    return False


def _is_public(source: bytes, node: Any) -> bool:
    return any(child.type == "visibility_modifier" for child in node.children)


def _type_name(source: bytes, node: Any | None) -> str | None:
    if node is None:
        return None
    if node.type in ("type_identifier", "identifier"):
        return node_text(source, node) or None
    if node.type in ("scoped_type_identifier", "scoped_identifier", "generic_type"):
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            return node_text(source, name_node) or None
        return _type_name(source, node.children[-1] if node.children else None)
    if node.type in ("reference_type", "pointer_type", "mutable_specifier"):
        for child in node.children:
            name = _type_name(source, child)
            if name:
                return name
    return None


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
