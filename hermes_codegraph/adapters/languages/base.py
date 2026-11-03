"""Shared parser-independent extraction types and helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ...languages import LanguageDescriptor


@dataclass(frozen=True)
class ExtractedSymbol:
    name: str
    qualified_name: str
    kind: str
    language_kind: str
    container: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    start_byte: int
    end_byte: int
    signature: str
    content_hash: str
    is_exported: bool


@dataclass(frozen=True)
class ExtractedReference:
    source_qualified_name: str
    target_qualified_name: str
    evidence_kind: str
    resolution_method: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    start_byte: int
    end_byte: int
    excerpt_hash: str
    #: ``calls``/``instantiates``/``references``; the resolver writes the
    #: matching edge type. ``target_qualified_name`` may be empty for a
    #: cross-file call whose target is only known by its local name.
    relation: str = "references"
    target_name: str = ""
    #: For ``instantiates``: the constructor target was written as ``new`` /
    #: a composite literal rather than a plain call.
    is_instantiation: bool = False


@dataclass(frozen=True)
class ExtractedImport:
    """One import statement (or one imported name of it)."""

    module: str
    imported_names: tuple[str, ...]
    alias: str | None
    is_relative: bool
    is_reexport: bool
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    excerpt_hash: str
    level: int = 0


@dataclass(frozen=True)
class ExtractedTypeRelation:
    """An inheritance/implementation/annotation relation, resolved later."""

    source_qualified_name: str
    target_name: str
    relation: str  # inherits | implements | overrides | has_type | returns_type
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    excerpt_hash: str


@dataclass(frozen=True)
class ExtractedTest:
    """A statically detected test case and the names it exercises."""

    qualified_name: str
    name: str
    framework: str
    target_names: tuple[str, ...]
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    excerpt_hash: str


@dataclass(frozen=True)
class ExtractedDiagnostic:
    line: int
    column: int
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class ExtractedFile:
    language: str
    module_name: str
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    references: list[ExtractedReference] = field(default_factory=list)
    diagnostics: list[ExtractedDiagnostic] = field(default_factory=list)
    parse_status: str = "success"
    imports: list[ExtractedImport] = field(default_factory=list)
    type_relations: list[ExtractedTypeRelation] = field(default_factory=list)
    tests: list[ExtractedTest] = field(default_factory=list)
    exports: tuple[str, ...] = ()
    reexports: tuple[str, ...] = ()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def span(node: Any) -> tuple[int, int, int, int]:
    """1-based (start_line, start_column, end_line, end_column)."""
    start = node.start_point
    end = node.end_point
    return (int(start[0]) + 1, int(start[1]) + 1, int(end[0]) + 1, int(end[1]) + 1)


def excerpt_hash(source: bytes, node: Any) -> str:
    return sha256_bytes(source[node.start_byte:node.end_byte])


def node_text(source: bytes, node: Any | None) -> str:
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def signature_before(source: bytes, node: Any, body: Any | None) -> str:
    end = body.start_byte if body is not None else node.end_byte
    text = source[node.start_byte:end].strip().decode("utf-8", errors="replace")
    return text[:500]


def module_name_for(path: str, descriptor: LanguageDescriptor) -> str:
    posix = path.replace("\\", "/")
    for suffix in descriptor.module_naming.get("strip_suffixes", ()):
        if posix.endswith(suffix):
            posix = posix[: -len(suffix)]
            break
    parts = [part for part in posix.split("/") if part]
    if parts:
        last = parts[-1]
        for init_name in descriptor.module_naming.get("init_names", ()):
            if last == init_name:
                parts = parts[:-1]
                break
    separator = descriptor.module_naming.get("separator", ".")
    return separator.join(parts)


def qualified(module: str, containers: list[str], name: str) -> str:
    parts = [module, *containers, name] if module else [*containers, name]
    return ".".join(part for part in parts if part)


def exported(name: str, descriptor: LanguageDescriptor) -> bool:
    rules = descriptor.visibility_rules
    if rules.get("exported_when_no_underscore"):
        return not name.startswith("_")
    return True


_GRAMMAR_INSTALL_ATTEMPTED: set[tuple[str, str]] = set()
_GRAMMAR_INSTALL_FAILURES: dict[tuple[str, str], str] = {}


def _auto_install_enabled() -> bool:
    try:
        from ...config import CodeGraphLimits

        return CodeGraphLimits.from_config().auto_install
    except Exception:  # health: allow BLE001 -- config is optional; auto-install defaults on
        return True


def _ensure_grammar(descriptor: LanguageDescriptor) -> None:
    """Install the descriptor's grammar extra before giving up."""
    from ...exceptions import CodeGraphUnavailableError

    extra = descriptor.extra or "codegraph"
    key = (descriptor.language, extra)
    failure = _GRAMMAR_INSTALL_FAILURES.get(key)
    if failure is not None:
        raise CodeGraphUnavailableError(
            f"grammar {descriptor.grammar_package!r} is not installed and "
            f"auto-install failed: {failure}; run `{_install_hint(descriptor)}`"
        )
    if not _auto_install_enabled():
        raise CodeGraphUnavailableError(
            f"grammar {descriptor.grammar_package!r} is not installed; "
            f"run `{_install_hint(descriptor)}` (code_intel.auto_install is false)"
        )
    if key in _GRAMMAR_INSTALL_ATTEMPTED:
        raise CodeGraphUnavailableError(
            f"grammar {descriptor.grammar_package!r} is still unavailable after "
            f"auto-install; run `{_install_hint(descriptor)}` and restart Hermes"
        )
    _GRAMMAR_INSTALL_ATTEMPTED.add(key)
    try:
        from pm import extras

        extras.ensure_import(extra)
    except Exception as exc:  # health: allow BLE001 -- PM install failures surface as structured unavailability
        _GRAMMAR_INSTALL_FAILURES[key] = f"{type(exc).__name__}: {exc}"
        raise CodeGraphUnavailableError(
            f"generic grammar auto-install for {descriptor.language!r} failed: "
            f"{type(exc).__name__}: {exc}; run `{_install_hint(descriptor)}`"
        ) from exc


def _grammar_module(descriptor: LanguageDescriptor) -> Any:
    import importlib

    from ...exceptions import CodeGraphUnavailableError

    package = descriptor.grammar_package.replace("-", "_")
    try:
        return importlib.import_module(package)
    except ImportError:
        _ensure_grammar(descriptor)
        try:
            return importlib.import_module(package)
        except ImportError as retry_exc:
            raise CodeGraphUnavailableError(
                f"grammar {descriptor.grammar_package!r} is still unavailable after "
                f"auto-install; run `{_install_hint(descriptor)}` and restart Hermes"
            ) from retry_exc


def _install_hint(descriptor: LanguageDescriptor) -> str:
    extra = descriptor.extra
    if extra:
        return f"hermes pm install --extra {extra}"
    return "hermes pm install --extra codegraph"


def language_object(descriptor: LanguageDescriptor, path: str) -> Any:
    """Load the grammar's ``Language`` object; imports stay lazy."""
    try:
        from tree_sitter import Language
    except ImportError as exc:  # pragma: no cover - guarded by pm availability
        from ...exceptions import CodeGraphUnavailableError

        raise CodeGraphUnavailableError(
            "tree-sitter is not installed; run `hermes pm install --extra codegraph`"
        ) from exc

    grammar = _grammar_module(descriptor)
    loader = descriptor.extraction.get("grammar_loader", "default")
    if loader == "typescript":
        value = (
            grammar.language_tsx()
            if path.lower().endswith((".tsx", ".jsx"))
            else grammar.language_typescript()
        )
    else:
        value = grammar.language()
    return value if isinstance(value, Language) else Language(value)


def parse_source(source: bytes, descriptor: LanguageDescriptor, path: str) -> Any:
    try:
        from tree_sitter import Parser
    except ImportError as exc:  # pragma: no cover
        from ...exceptions import CodeGraphUnavailableError

        raise CodeGraphUnavailableError(
            "tree-sitter is not installed; run `hermes pm install --extra codegraph`"
        ) from exc

    parser = Parser(language_object(descriptor, path))
    return parser.parse(source)
