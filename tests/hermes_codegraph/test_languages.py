"""Language Registry: whole-file validation plus packaged/profile merge."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import hermes_yaml
import pytest

from hermes_codegraph.exceptions import LanguageDescriptorError, LanguageUnsupportedError
from hermes_codegraph.languages import (
    BUILTIN_REGISTRY,
    CAPABILITY_KEYS,
    LanguageDescriptor,
    default_registry,
    install_target_gap,
    load_registry_file,
)
from hermes_constants import reset_hermes_home_key_cache

PROFILE_PARTS = ("code-intelligence", "tree-sitter")


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    reset_hermes_home_key_cache()
    yield home
    reset_hermes_home_key_cache()


def _document() -> dict:
    return hermes_yaml.safe_load(BUILTIN_REGISTRY.read_text(encoding="utf-8"))


def _entry(document: dict, language: str) -> dict:
    return next(item for item in document["languages"] if item["language"] == language)


def _profile_path(home: Path) -> Path:
    path = home
    for part in PROFILE_PARTS:
        path = path / part
    return path / "languages.yaml"


def _write_query_files(root: Path, language: str, entry: dict) -> None:
    for relative in entry["tree_sitter_queries"].values():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"; {language} query\n", encoding="utf-8")


def _new_entry(document: dict, language: str = "python-mini") -> dict:
    entry = copy.deepcopy(_entry(document, "python"))
    entry.update(
        language=language,
        aliases=[f"{language}-alias"],
        extensions=[f".{language}"],
        filenames=[],
        grammar_install=f"on_demand:codegraph-{language}",
        capabilities={"symbols": True},
    )
    entry["tree_sitter_queries"] = {
        purpose: f"queries/{language}/{purpose}.scm"
        for purpose in entry["tree_sitter_queries"]
    }
    return entry


def _write_registry(path: Path, languages: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        hermes_yaml.safe_dump({"schema_version": 1, "languages": languages}),
        encoding="utf-8",
    )


def test_builtin_registry_loads_one_file_with_four_complete_descriptors(isolated_home):
    registry = default_registry()
    assert registry.supported_languages() == ["go", "python", "rust", "typescript"]
    for descriptor in registry.descriptors():
        assert descriptor.grammar_package
        assert descriptor.grammar_version
        assert descriptor.parser_version
        assert set(descriptor.tree_sitter_queries) == {
            "symbols", "imports", "calls", "types", "tests"
        }
        assert set(descriptor.capabilities) == set(CAPABILITY_KEYS)
        assert all(descriptor.capabilities.values())
        assert descriptor.semantic_provider
    assert registry.report is not None
    assert registry.report.builtin_languages == 4
    assert registry.report.profile_path is None


def test_aliases_and_extensions_resolve(isolated_home):
    registry = default_registry()
    assert registry.get("py").language == "python"
    assert registry.get("tsx").language == "typescript"
    python = registry.by_path("pkg/mod.py")
    typescript = registry.by_path("src/app.tsx")
    assert python is not None and python.language == "python"
    assert typescript is not None and typescript.language == "typescript"
    assert registry.by_path("README.md") is None


def test_packaged_registry_agrees_with_the_skill_validator():
    script = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "software-development"
        / "code-intelligence"
        / "scripts"
        / "validate_language_registry.py"
    )
    spec = importlib.util.spec_from_file_location("code_intel_registry_validator", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.validate_registry(_document(), BUILTIN_REGISTRY.parent) == []


def test_unknown_language_has_no_fallback(isolated_home):
    with pytest.raises(LanguageUnsupportedError):
        default_registry().get("cobol")


def test_profile_on_demand_language_is_listed_and_installs_through_pm(
    isolated_home, monkeypatch
):
    document = _document()
    entry = _new_entry(document, "java")
    entry.update(
        grammar_package="tree-sitter-java",
        grammar_version="0.23.5",
        grammar_install="on_demand:codegraph-java",
    )
    _write_query_files(
        isolated_home / "code-intelligence" / "tree-sitter", "java", entry
    )
    _write_registry(_profile_path(isolated_home), [entry])

    from hermes_codegraph import CodeIntelligence
    from hermes_codegraph.config import CodeGraphLimits
    from hermes_codegraph.scanner import resolve_repository

    registry = default_registry()
    instance = CodeIntelligence(
        None,
        resolve_repository(isolated_home),
        CodeGraphLimits(languages=("java",)),
        registry,
    )
    java = next(item for item in instance.languages() if item["language"] == "java")
    assert java["installed"] is False
    assert java["enabled"] is True

    calls: list[str] = []
    monkeypatch.setattr(
        "pm.extras.ensure_import", lambda extra: calls.append(extra)
    )
    result = instance.install_language("java")
    assert calls == ["codegraph-java"]
    assert result["extra"] == "codegraph-java"


def test_missing_capability_defaults_to_false():
    document = _document()
    entry = copy.deepcopy(_entry(document, "python"))
    entry["capabilities"] = {"symbols": True}
    descriptor = LanguageDescriptor.from_mapping(entry)
    assert descriptor.capabilities["symbols"] is True
    assert descriptor.capabilities["calls"] is False


def test_builtin_descriptor_must_declare_every_capability():
    document = _document()
    entry = copy.deepcopy(_entry(document, "python"))
    entry["capabilities"] = {"symbols": True}
    with pytest.raises(LanguageDescriptorError, match="must declare capabilities"):
        LanguageDescriptor.from_mapping(entry, require_all_capabilities=True)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda entry: entry.pop("parser_version"),
        lambda entry: entry.update(unknown_field=True),
        lambda entry: entry.update(aliases="py"),
        lambda entry: entry.update(capabilities={"symbols": "yes"}),
        lambda entry: entry.update(tree_sitter_queries={"symbols": "x.scm"}),
    ],
)
def test_bad_descriptor_fields_reject_the_file(mutate):
    document = _document()
    entry = copy.deepcopy(_entry(document, "python"))
    mutate(entry)
    with pytest.raises(LanguageDescriptorError):
        LanguageDescriptor.from_mapping(entry, registry_root=BUILTIN_REGISTRY.parent)


def test_unknown_top_level_keys_are_rejected(tmp_path):
    path = tmp_path / "languages.yaml"
    path.write_text(
        hermes_yaml.safe_dump(
            {"schema_version": 1, "languages": [], "extra": True}
        ),
        encoding="utf-8",
    )
    with pytest.raises(LanguageDescriptorError, match="unknown top-level"):
        load_registry_file(path)


@pytest.mark.parametrize("schema_version", [None, 2, "1"])
def test_schema_version_must_be_one(tmp_path, schema_version):
    path = tmp_path / "languages.yaml"
    path.write_text(
        hermes_yaml.safe_dump(
            {"schema_version": schema_version, "languages": []}
        ),
        encoding="utf-8",
    )
    with pytest.raises(LanguageDescriptorError):
        load_registry_file(path)


def test_empty_languages_list_is_rejected(tmp_path):
    path = tmp_path / "languages.yaml"
    path.write_text(
        hermes_yaml.safe_dump({"schema_version": 1, "languages": []}),
        encoding="utf-8",
    )
    with pytest.raises(LanguageDescriptorError, match="non-empty list"):
        load_registry_file(path)


def test_single_file_duplicate_language_rejects_the_whole_file(tmp_path):
    document = _document()
    python_entry = copy.deepcopy(_entry(document, "python"))
    _write_query_files(tmp_path, "python", python_entry)
    path = tmp_path / "languages.yaml"
    _write_registry(path, [python_entry, copy.deepcopy(python_entry)])
    with pytest.raises(LanguageDescriptorError, match="duplicate language"):
        load_registry_file(path)


def test_single_file_duplicate_alias_and_extension_are_rejected(tmp_path):
    document = _document()
    extra = _new_entry(document)
    _write_query_files(tmp_path, extra["language"], extra)

    alias_dup = copy.deepcopy(extra)
    alias_dup["language"] = "python-alias-dup"
    alias_dup["aliases"] = [extra["aliases"][0]]
    alias_dup["extensions"] = [".pym-alias"]
    alias_dup["tree_sitter_queries"] = {
        purpose: f"queries/{alias_dup['language']}/{purpose}.scm"
        for purpose in alias_dup["tree_sitter_queries"]
    }
    _write_query_files(tmp_path, alias_dup["language"], alias_dup)

    extension_dup = copy.deepcopy(extra)
    extension_dup["language"] = "python-extension-dup"
    extension_dup["aliases"] = ["pym-extension"]
    extension_dup["extensions"] = [extra["extensions"][0]]
    extension_dup["tree_sitter_queries"] = {
        purpose: f"queries/{extension_dup['language']}/{purpose}.scm"
        for purpose in extension_dup["tree_sitter_queries"]
    }
    _write_query_files(tmp_path, extension_dup["language"], extension_dup)

    path = tmp_path / "languages.yaml"
    _write_registry(path, [extra, alias_dup])
    with pytest.raises(LanguageDescriptorError, match="duplicate alias"):
        load_registry_file(path)
    _write_registry(path, [extra, extension_dup])
    with pytest.raises(LanguageDescriptorError, match="duplicate extension"):
        load_registry_file(path)


@pytest.mark.parametrize("query_path", ["/abs/symbols.scm", "../escape.scm"])
def test_query_path_must_stay_inside_registry_root(tmp_path, query_path):
    document = _document()
    entry = _new_entry(document)
    _write_query_files(tmp_path, entry["language"], entry)
    entry["tree_sitter_queries"]["symbols"] = query_path
    path = tmp_path / "languages.yaml"
    _write_registry(path, [entry])
    with pytest.raises(LanguageDescriptorError, match="stay inside|escapes"):
        load_registry_file(path)


def test_missing_and_empty_query_files_reject_the_registry(tmp_path):
    document = _document()
    entry = _new_entry(document)
    path = tmp_path / "languages.yaml"
    _write_registry(path, [entry])
    with pytest.raises(LanguageDescriptorError, match="does not exist"):
        load_registry_file(path)

    _write_query_files(tmp_path, entry["language"], entry)
    empty = tmp_path / entry["tree_sitter_queries"]["symbols"]
    empty.write_text("", encoding="utf-8")
    with pytest.raises(LanguageDescriptorError, match="empty"):
        load_registry_file(path)


def test_profile_registry_adds_languages_and_skips_duplicates(isolated_home):
    document = _document()
    profile_root = _profile_path(isolated_home).parent
    duplicate = copy.deepcopy(_entry(document, "python"))
    duplicate["tree_sitter_queries"] = {
        purpose: f"queries/python/{purpose}.scm"
        for purpose in duplicate["tree_sitter_queries"]
    }
    _write_query_files(profile_root, "python", duplicate)
    extra = _new_entry(document)
    _write_query_files(profile_root, extra["language"], extra)
    _write_registry(_profile_path(isolated_home), [duplicate, extra])

    registry = default_registry()
    assert registry.supported_languages() == [
        "go", "python", "python-mini", "rust", "typescript"
    ]
    assert registry.get("python").tree_sitter_queries["symbols"] == (
        _entry(document, "python")["tree_sitter_queries"]["symbols"]
    )
    assert registry.report is not None
    assert registry.report.profile_languages == 2
    assert registry.report.profile_skipped == 1
    assert registry.report.profile_conflicts == 0
    assert registry.report.profile_error is None


def test_profile_extension_conflict_is_skipped_and_reported(isolated_home):
    document = _document()
    profile_root = _profile_path(isolated_home).parent
    extra = _new_entry(document, "python-extra")
    extra["extensions"] = [".py"]
    _write_query_files(profile_root, extra["language"], extra)
    _write_registry(_profile_path(isolated_home), [extra])

    registry = default_registry()
    assert "python-extra" not in registry.supported_languages()
    assert registry.report is not None
    assert registry.report.profile_conflicts == 1
    assert registry.report.conflicts[0]["language"] == "python-extra"
    assert "extension:.py" in registry.report.conflicts[0]["reasons"]


def test_malformed_profile_file_is_rejected_but_builtin_still_loads(isolated_home):
    _write_registry(
        _profile_path(isolated_home),
        [{"language": "broken"}],
    )
    registry = default_registry()
    assert registry.supported_languages() == ["go", "python", "rust", "typescript"]
    assert registry.report is not None
    assert registry.report.profile_error is not None
    assert registry.report.profile_languages == 0


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("linux-x86_64-musl", None),
        ("linux-arm64-musl", "no tree-sitter core wheel"),
        ("linux-aarch64-musl", "no tree-sitter core wheel"),
        ("linux-x86_64-bionic", "no tree-sitter core wheel"),
        ("win32-x64", None),
        (None, None),
    ],
)
def test_install_target_gap(target, expected):
    gap = install_target_gap(target)
    if expected is None:
        assert gap is None
    else:
        assert gap is not None and expected in gap
