#!/usr/bin/env python3
"""Validate a code-intelligence tree-sitter Language Registry.

Checks the ADR-8-aligned descriptor subset used by the code-intelligence skill:
whole-file schema, unique language/alias/extension, required fields, relative
query/parser paths that stay inside the tree-sitter root, and existing files.
Exit codes: 0 valid, 1 invalid, 2 unreadable/malformed YAML.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
TOP_KEYS = {"schema_version", "languages"}
REQUIRED = {
    "language", "aliases", "extensions", "filenames", "grammar_package",
    "grammar_version", "grammar_install", "tree_sitter_queries", "capabilities",
    "parser_version",
}
OPTIONAL = {
    "parser", "kind_map", "container_rules", "visibility_rules", "signature_rules",
    "module_naming", "import_resolution", "test_detection", "semantic_provider",
    "extraction",
}
QUERY_KEYS = {"symbols", "imports", "calls", "types", "tests"}
CAPABILITY_KEYS = {"symbols", "references", "calls", "types", "inheritance", "tests", "change"}
MAPPING_FIELDS = {
    "kind_map", "container_rules", "visibility_rules", "signature_rules",
    "module_naming", "import_resolution", "test_detection", "extraction",
}
LANGUAGE_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
INSTALL_POLICY = re.compile(r"(?:builtin|on_demand:[a-z0-9][a-z0-9._-]*)\Z")
PARSER_SUFFIXES = {".so", ".dylib", ".dll", ".wasm"}


class RegistryError(ValueError):
    """An unreadable registry (as opposed to an invalid one)."""


def load_registry(path: Path) -> Any:
    try:
        import hermes_yaml as yaml
    except ImportError:
        try:
            import yaml
        except ImportError as exc:
            raise RegistryError(
                "no YAML reader available; run this from the Hermes environment "
                "or install PyYAML"
            ) from exc
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise RegistryError(f"cannot read {path}: {exc}") from exc
    except Exception as exc:  # yaml errors are backend-specific
        raise RegistryError(f"invalid YAML in {path}: {exc}") from exc


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_list(value: Any, *, prefix: str, name: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or any(not _nonempty_string(item) for item in value):
        errors.append(f"{prefix}.{name} must be a list of non-empty strings")
        return []
    if len(set(value)) != len(value):
        errors.append(f"{prefix}.{name} contains duplicates")
    return value


def _artifact_path(value: Any, *, base: Path, prefix: str, name: str,
                   errors: list[str], suffixes: set[str] | None = None) -> None:
    if not _nonempty_string(value):
        errors.append(f"{prefix}.{name} must be a non-empty relative path")
        return
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        errors.append(f"{prefix}.{name} must stay inside the tree-sitter root: {value!r}")
        return
    if suffixes is not None and relative.suffix.lower() not in suffixes:
        errors.append(f"{prefix}.{name} must end with one of {sorted(suffixes)}")
        return
    target = (base / relative).resolve()
    if not target.is_relative_to(base.resolve()):
        errors.append(f"{prefix}.{name} escapes the tree-sitter root: {value!r}")
        return
    if not target.is_file():
        errors.append(f"{prefix}.{name} does not exist: {value!r}")
    elif target.stat().st_size == 0:
        errors.append(f"{prefix}.{name} is empty: {value!r}")


def _validate_top_level(document: Any, errors: list[str]) -> list[Any]:
    if not isinstance(document, dict):
        errors.append("top level must be a mapping")
        return []
    extra = sorted(set(document) - TOP_KEYS)
    missing = sorted(TOP_KEYS - set(document))
    if extra:
        errors.append(f"unknown top-level keys: {extra}")
    if missing:
        errors.append(f"missing top-level keys: {missing}")
    if document.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    languages = document.get("languages")
    if not isinstance(languages, list) or not languages:
        errors.append("languages must be a non-empty list")
        return []
    return languages


def _validate_identity(
    entry: dict[str, Any],
    *,
    prefix: str,
    seen: dict[str, Any],
    errors: list[str],
) -> None:
    language = entry.get("language")
    if not isinstance(language, str) or not _nonempty_string(language) \
            or not LANGUAGE_ID.match(language):
        errors.append(f"{prefix}.language must match [a-z0-9][a-z0-9-]*")
    elif language in seen["languages"]:
        errors.append(f"{prefix}.language duplicates {language!r}")
    else:
        seen["languages"].add(language)

    aliases = _string_list(entry.get("aliases"), prefix=prefix, name="aliases", errors=errors)
    for alias in aliases:
        if alias in seen["aliases"] and seen["aliases"][alias] != language:
            errors.append(f"{prefix}.aliases duplicates alias {alias!r}")
        seen["aliases"][alias] = str(language)

    extensions = _string_list(
        entry.get("extensions"), prefix=prefix, name="extensions", errors=errors
    )
    for extension in extensions:
        if not extension.startswith("."):
            errors.append(f"{prefix}.extensions entries must start with '.': {extension!r}")
        if extension in seen["extensions"] and seen["extensions"][extension] != language:
            errors.append(f"{prefix}.extensions duplicates extension {extension!r}")
        seen["extensions"][extension] = str(language)

    filenames = _string_list(
        entry.get("filenames"), prefix=prefix, name="filenames", errors=errors
    )
    for filename in filenames:
        if filename in seen["filenames"] and seen["filenames"][filename] != language:
            errors.append(f"{prefix}.filenames duplicates filename {filename!r}")
        seen["filenames"][filename] = str(language)


def _validate_sources(entry: dict[str, Any], *, prefix: str, errors: list[str]) -> None:
    for name in ("grammar_package", "grammar_version", "parser_version"):
        if not _nonempty_string(entry.get(name)):
            errors.append(f"{prefix}.{name} must be a non-empty string")
    install = entry.get("grammar_install")
    if not isinstance(install, str) or not _nonempty_string(install) \
            or not INSTALL_POLICY.match(install):
        errors.append(f"{prefix}.grammar_install must be 'builtin' or 'on_demand:<extra>'")


def _validate_queries(
    entry: dict[str, Any], *, prefix: str, base: Path, errors: list[str]
) -> None:
    queries = entry.get("tree_sitter_queries")
    if not isinstance(queries, dict) or not queries:
        errors.append(f"{prefix}.tree_sitter_queries must be a non-empty mapping")
    else:
        unknown = sorted(set(queries) - QUERY_KEYS)
        if unknown:
            errors.append(f"{prefix}.tree_sitter_queries has unknown purposes: {unknown}")
        for purpose, path in queries.items():
            _artifact_path(path, base=base, prefix=prefix,
                           name=f"tree_sitter_queries.{purpose}", errors=errors)
    if "parser" in entry:
        _artifact_path(entry["parser"], base=base, prefix=prefix, name="parser",
                       errors=errors, suffixes=PARSER_SUFFIXES)


def _validate_rules(entry: dict[str, Any], *, prefix: str, errors: list[str]) -> None:
    for name in MAPPING_FIELDS:
        if name in entry and not isinstance(entry[name], dict):
            errors.append(f"{prefix}.{name} must be a mapping")
    provider = entry.get("semantic_provider")
    if provider is not None and not _nonempty_string(provider):
        errors.append(f"{prefix}.semantic_provider must be a string or null")

    capabilities = entry.get("capabilities")
    if not isinstance(capabilities, dict):
        errors.append(f"{prefix}.capabilities must be a mapping")
        return
    unknown = sorted(set(capabilities) - CAPABILITY_KEYS)
    if unknown:
        errors.append(f"{prefix}.capabilities has unknown keys: {unknown}")
    for name, value in capabilities.items():
        if not isinstance(value, bool):
            errors.append(f"{prefix}.capabilities.{name} must be a boolean")


def _validate_entry(
    entry: Any,
    *,
    index: int,
    base: Path,
    seen: dict[str, Any],
    errors: list[str],
) -> None:
    prefix = f"languages[{index}]"
    if not isinstance(entry, dict):
        errors.append(f"{prefix} must be a mapping")
        return
    extra = sorted(set(entry) - REQUIRED - OPTIONAL)
    missing = sorted(REQUIRED - set(entry))
    if extra:
        errors.append(f"{prefix} has unknown keys: {extra}")
    if missing:
        errors.append(f"{prefix} is missing keys: {missing}")
    _validate_identity(entry, prefix=prefix, seen=seen, errors=errors)
    _validate_sources(entry, prefix=prefix, errors=errors)
    _validate_queries(entry, prefix=prefix, base=base, errors=errors)
    _validate_rules(entry, prefix=prefix, errors=errors)


def validate_registry(document: Any, base: Path) -> list[str]:
    """Return a list of schema errors; an empty list means the registry is valid."""
    errors: list[str] = []
    languages = _validate_top_level(document, errors)
    seen: dict[str, Any] = {
        "languages": set(),
        "aliases": {},
        "extensions": {},
        "filenames": {},
    }
    for index, entry in enumerate(languages):
        _validate_entry(entry, index=index, base=base, seen=seen, errors=errors)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a tree-sitter Language Registry.")
    parser.add_argument("registry", type=Path, help="path to tree-sitter/languages.yaml")
    args = parser.parse_args(argv)
    try:
        document = load_registry(args.registry)
    except RegistryError as exc:
        print(f"registry: {exc}", file=sys.stderr)
        return 2
    errors = validate_registry(document, args.registry.parent)
    if errors:
        for error in errors:
            print(f"registry: {error}", file=sys.stderr)
        return 1
    languages = document["languages"]
    query_files = sum(len(entry.get("tree_sitter_queries") or {}) for entry in languages)
    print(f"OK: {len(languages)} languages, {query_files} query files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
