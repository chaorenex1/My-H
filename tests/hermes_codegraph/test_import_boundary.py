"""Import boundary and public shape for hermes_codegraph."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_import_does_not_load_backends_or_parser():
    result = run_python(
        "import sys, hermes_codegraph; "
        "bad = [m for m in ('tree_sitter', 'ladybug', 'networkx') if m in sys.modules]; "
        "assert not bad, bad; "
        "print('ok')"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_public_api_has_no_backend_types():
    from hermes_codegraph import CodeIntelligence

    for name, member in inspect.getmembers(CodeIntelligence):
        if name.startswith("_") or not callable(member):
            continue
        rendered = str(inspect.signature(member))
        for forbidden in ("ladybug", "networkx", "tree_sitter"):
            assert forbidden not in rendered, (name, rendered)
