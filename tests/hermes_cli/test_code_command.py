"""`hermes code` stays import-light until a handler needs the graph."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_code_subcommand_import_does_not_load_backends_or_parser():
    code = (
        "import sys; import hermes_cli.subcommands.code; "
        "bad=[m for m in ('tree_sitter','ladybug','networkx','hermes_graph',"
        "'hermes_codegraph') if m in sys.modules]; "
        "assert not bad, bad; print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
