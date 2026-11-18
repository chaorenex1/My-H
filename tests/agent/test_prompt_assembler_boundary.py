"""Assembler import/IO boundary (ADR-19 D1, acceptance 3)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

pytest.importorskip("jinja2")

from agent.prompts import prompt_sections as ps
from agent.prompts.prompt_sections import SYSTEM_PROMPT_DIR

ASSEMBLER = Path(__file__).resolve().parents[2] / "agent" / "prompts" / "assembler.py"
ALLOWED_AGENT_MODULES = (
    "agent.prompts.prompt_sections",
    "agent.prompts.renderer",
)
FORBIDDEN_AGENT_MODULES = (
    "agent.system_prompt",
    "agent.prompt_builder",
    "run_agent",
    "hermes_cli",
)


def _tree() -> ast.Module:
    return ast.parse(ASSEMBLER.read_text(encoding="utf-8"), filename=str(ASSEMBLER))


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "agent.prompts":
                modules.update(f"{node.module}.{alias.name}" for alias in node.names)
            else:
                modules.add(node.module)
    return modules


def test_imports_stay_inside_the_stdlib_and_the_two_prompt_modules():
    modules = _imported_modules(_tree())
    for module in modules:
        if module.startswith("agent"):
            assert module in ALLOWED_AGENT_MODULES or any(
                module == allowed or module.startswith(allowed + ".")
                for allowed in ALLOWED_AGENT_MODULES
            ), f"assembler imports an internal module outside its seam: {module}"
            continue
        root = module.split(".", 1)[0]
        assert root in sys.stdlib_module_names, f"assembler imports a non-stdlib module: {module}"


def test_forbidden_facade_imports_are_absent():
    modules = _imported_modules(_tree())
    for module in modules:
        for forbidden in FORBIDDEN_AGENT_MODULES:
            assert not (module == forbidden or module.startswith(forbidden + ".")), (
                f"assembler must not import {forbidden} (found {module})"
            )


def test_assembler_does_no_direct_file_io():
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            assert func.id not in {"open", "Path"}, f"direct IO call: {func.id}()"
        elif isinstance(func, ast.Attribute):
            assert func.attr not in {
                "read_text", "read_bytes", "write_text", "write_bytes", "open",
            }, f"direct IO call: .{func.attr}()"


def test_manifest_section_files_exist_below_the_system_directory():
    for section in ps.load_sections().values():
        if section.path:
            assert (SYSTEM_PROMPT_DIR / section.path).is_file(), section.key
