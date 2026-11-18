"""Tests for the code-intelligence bundled skill and its SARIF helper."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import hermes_yaml as yaml
import pytest

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "software-development" / "code-intelligence"
ANSWER = SKILL_DIR / "scripts" / "sarif_summary.py"

TOOLS = ("ast-grep", "tree-sitter", "semgrep", "codeql")
REFERENCE_URLS = {
    "ast-grep.md": ("https://ast-grep.github.io/guide/quick-start.html",
                    "https://ast-grep.github.io/reference/cli.html"),
    "tree-sitter.md": ("https://tree-sitter.github.io/tree-sitter/cli/parse.html",
                       "https://tree-sitter.github.io/tree-sitter/cli/query.html"),
    "semgrep.md": ("https://docs.semgrep.dev/getting-started/cli",
                   "https://docs.semgrep.dev/writing-rules/rule-syntax"),
    "codeql.md": ("https://docs.github.com/en/code-security/tutorials/customize-code-scanning/prepare-code-for-analysis",
                  "https://docs.github.com/en/code-security/tutorials/customize-code-scanning/analyze-code"),
}


@pytest.fixture(scope="module")
def skill_text() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sarif_mod():
    spec = importlib.util.spec_from_file_location("code_intel_sarif", ANSWER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frontmatter(text: str) -> dict:
    assert text.startswith("---")
    end = text.index("\n---", 3)
    return yaml.safe_load(text[3:end])


def test_frontmatter_and_description(skill_text):
    fm = _frontmatter(skill_text)
    assert fm["name"] == SKILL_DIR.name
    assert fm["platforms"] == ["linux", "macos", "windows"]
    description = fm["description"]
    assert description.endswith(".")
    assert len(description) <= 60
    hermes = fm["metadata"]["hermes"]
    assert hermes["category"] == "software-development"
    assert {"ast-grep", "tree-sitter", "semgrep", "codeql"} <= set(hermes["tags"])


def test_skill_routes_every_tool_and_reference(skill_text):
    for tool in TOOLS:
        assert f"`{tool}`" in skill_text
        assert f"references/{tool}.md" in skill_text


def test_references_exist_and_cite_official_docs():
    for name, urls in REFERENCE_URLS.items():
        path = SKILL_DIR / "references" / name
        assert path.is_file(), name
        text = path.read_text(encoding="utf-8")
        for url in urls:
            assert url in text, (name, url)


def test_skill_has_activation_guidance(skill_text):
    headings = ["### Activation", "### After activation", "### Why this makes the agent better"]
    positions = [skill_text.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert positions[0] < skill_text.index("## Prerequisites")
    assert 'skill_view("code-intelligence")' in skill_text
    assert 'skill_view("code-intelligence", file_path="references/<tool>.md")' in skill_text


def test_skill_has_required_sections_in_order(skill_text):
    headings = [
        "## When to Use",
        "## Prerequisites",
        "## How to Run",
        "## Quick Reference",
        "## Procedure",
        "## Pitfalls",
        "## Verification",
    ]
    positions = [skill_text.index(heading) for heading in headings]
    assert positions == sorted(positions)


def _sarif() -> dict:
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {
                    "name": "CodeQL",
                    "rules": [
                        {"id": "py/error-rule",
                         "defaultConfiguration": {"level": "error"}},
                    ],
                }},
                "results": [
                    {
                        "ruleId": "py/warning-rule",
                        "level": "warning",
                        "message": {"text": "second finding"},
                        "locations": [{"physicalLocation": {
                            "artifactLocation": {"uri": "src/b.py"},
                            "region": {"startLine": 20},
                        }}],
                    },
                    {
                        "ruleId": "py/error-rule",
                        "message": {"text": "first | finding"},
                        "locations": [{"physicalLocation": {
                            "artifactLocation": {"uri": "file:///C:/repo/a.py"},
                            "region": {"startLine": 3},
                        }}],
                    },
                    {
                        "ruleId": "py/note-rule",
                        "level": "note",
                        "message": {"text": "last"},
                        "locations": [],
                    },
                ],
            }
        ],
    }


def test_summarize_sorts_and_normalizes_paths(sarif_mod):
    rows = sarif_mod.summarize(_sarif())
    assert [row["rule"] for row in rows] == [
        "py/error-rule", "py/warning-rule", "py/note-rule",
    ]
    assert rows[0]["path"] == "C:/repo/a.py"
    assert rows[0]["line"] == "3"
    assert rows[0]["severity"] == "error"
    assert rows[1]["severity"] == "warning"
    assert rows[2]["path"] == "?"


def test_markdown_escapes_table_cells(sarif_mod):
    text = sarif_mod.markdown(sarif_mod.summarize(_sarif()))
    assert "SARIF findings: 3" in text
    assert "C:/repo/a.py:3" in text
    assert "first \\| finding" in text


def test_cli_writes_json_and_table(sarif_mod, tmp_path, capsys):
    path = tmp_path / "results.sarif"
    path.write_text(json.dumps(_sarif()), encoding="utf-8")

    assert sarif_mod.main([str(path), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["rule"] == "py/error-rule"

    assert sarif_mod.main([str(path)]) == 0
    assert "SARIF findings: 3" in capsys.readouterr().out


def test_cli_rejects_non_sarif(sarif_mod, tmp_path, capsys):
    path = tmp_path / "not-sarif.json"
    path.write_text('{"nope": []}', encoding="utf-8")
    assert sarif_mod.main([str(path)]) == 2
    assert "not a SARIF" in capsys.readouterr().err


REGISTRY_SCRIPT = SKILL_DIR / "scripts" / "validate_language_registry.py"


@pytest.fixture(scope="module")
def registry_mod():
    spec = importlib.util.spec_from_file_location("code_intel_registry", REGISTRY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_semgrep_rule_files_are_language_scoped(skill_text):
    assert "$CI_ROOT/semgrep/<language>/<rule-pack>.yml" in skill_text
    reference = (SKILL_DIR / "references" / "semgrep.md").read_text(encoding="utf-8")
    assert "semgrep/python/myrules.yml" in reference
    assert "semgrep/shared/cross-language.yml" in reference
    assert '$CI_ROOT/semgrep/myrules.yml' not in reference


def test_language_registry_assets_and_artifact_paths(skill_text):
    assert "HERMES_HOME/code-intelligence" in skill_text
    for name in ("references/language-registry.md", "templates/language-registry.yaml",
                 "scripts/validate_language_registry.py"):
        assert (SKILL_DIR / name).is_file(), name
    assert "references/language-registry.md" in skill_text
    assert "codeql/" in skill_text and "semgrep/" in skill_text and "tree-sitter/" in skill_text


def _descriptor(language: str = "python") -> dict:
    return {
        "language": language,
        "aliases": ["py"] if language == "python" else ["ts"],
        "extensions": [".py"] if language == "python" else [".ts"],
        "filenames": [],
        "grammar_package": f"tree-sitter-{language}",
        "grammar_version": "0.25.0",
        "grammar_install": "builtin",
        "parser": f"parsers/{language}/parser.so",
        "tree_sitter_queries": {
            purpose: f"queries/{language}/{purpose}.scm"
            for purpose in ("symbols", "imports", "calls", "types", "tests")
        },
        "kind_map": {"function_definition": "function"},
        "capabilities": {
            "symbols": True, "references": True, "calls": True,
            "types": True, "inheritance": True, "tests": True, "change": True,
        },
        "parser_version": "0.25.0+1",
    }


def _registry(tmp_path: Path, document: dict | None = None) -> tuple[Path, dict]:
    root = tmp_path / "tree-sitter"
    for purpose in ("symbols", "imports", "calls", "types", "tests"):
        query = root / "queries" / "python" / f"{purpose}.scm"
        query.parent.mkdir(parents=True, exist_ok=True)
        query.write_text("(identifier) @name\n", encoding="utf-8")
    parser = root / "parsers" / "python" / "parser.so"
    parser.parent.mkdir(parents=True, exist_ok=True)
    parser.write_bytes(b"\x7fELF")
    doc = document or {"schema_version": 1, "languages": [_descriptor()]}
    path = root / "languages.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path, doc


def test_registry_validator_accepts_a_complete_profile(registry_mod, tmp_path):
    path, doc = _registry(tmp_path)
    assert registry_mod.validate_registry(doc, path.parent) == []


def test_registry_validator_rejects_duplicate_language(registry_mod, tmp_path):
    doc = {"schema_version": 1, "languages": [_descriptor(), _descriptor()]}
    path, _ = _registry(tmp_path, doc)
    errors = registry_mod.validate_registry(doc, path.parent)
    assert any("duplicates" in error for error in errors)


@pytest.mark.parametrize("field,value", [
    ("aliases", ["py"]),
    ("extensions", [".py"]),
    ("filenames", ["Dockerfile"]),
])
def test_registry_validator_rejects_cross_language_lookup_collisions(
        registry_mod, tmp_path, field, value):
    first = _descriptor()
    if field == "filenames":
        first["filenames"] = value
    second = _descriptor("typescript")
    second[field] = value
    doc = {"schema_version": 1, "languages": [first, second]}
    path, _ = _registry(tmp_path, doc)
    errors = registry_mod.validate_registry(doc, path.parent)
    assert any("duplicates" in error for error in errors)


def test_registry_validator_rejects_missing_query_file(registry_mod, tmp_path):
    path, doc = _registry(tmp_path)
    (path.parent / "queries/python/calls.scm").unlink()
    errors = registry_mod.validate_registry(doc, path.parent)
    assert any("does not exist" in error for error in errors)


def test_registry_validator_rejects_escaping_paths(registry_mod, tmp_path):
    path, doc = _registry(tmp_path)
    doc["languages"][0]["tree_sitter_queries"]["calls"] = "../outside.scm"
    (tmp_path / "outside.scm").write_text("(identifier) @name\n", encoding="utf-8")
    errors = registry_mod.validate_registry(doc, path.parent)
    assert any("stay inside" in error for error in errors)


def test_registry_validator_rejects_unknown_keys_and_types(registry_mod, tmp_path):
    path, doc = _registry(tmp_path)
    entry = doc["languages"][0]
    entry["unexpected"] = True
    entry["capabilities"]["calls"] = "yes"
    errors = registry_mod.validate_registry(doc, path.parent)
    assert any("unknown keys" in error for error in errors)
    assert any("must be a boolean" in error for error in errors)


def test_registry_cli_exit_codes(registry_mod, tmp_path, capsys):
    path, _ = _registry(tmp_path)
    assert registry_mod.main([str(path)]) == 0
    assert "OK: 1 languages" in capsys.readouterr().out

    (path.parent / "queries/python/imports.scm").unlink()
    assert registry_mod.main([str(path)]) == 1
    assert "does not exist" in capsys.readouterr().err

    path.write_text("languages: [\n", encoding="utf-8")
    assert registry_mod.main([str(path)]) == 2
    assert "invalid YAML" in capsys.readouterr().err

