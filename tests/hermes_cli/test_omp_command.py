"""``hermes omp``: parser wiring and the Bun launch contract.

The shell itself is exercised by ``omp-ui/app/smoke.ts`` against a live gateway; these tests pin
the parts the CLI owns — argv, environment, the one-time ``bun install``, and the refusals.
"""

from __future__ import annotations

import argparse
import os
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import omp_launch
from hermes_cli.subcommands.omp import build_omp_parser


def _capture(_args) -> None:
    return None


def test_omp_parser_wires_handler_and_flags() -> None:
    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="command")
    build_omp_parser(subparsers, cmd_omp=_capture)

    plain = root.parse_args(["omp"])
    assert plain.func is _capture
    assert plain.resume is None
    assert plain.no_start is False

    resumed = root.parse_args(["omp", "--resume", "local-abc", "--no-start"])
    assert resumed.resume == "local-abc"
    assert resumed.no_start is True

    assert root.parse_args(["omp", "-r", "local-def"]).resume == "local-def"


@pytest.fixture
def fake_workspace(tmp_path, monkeypatch):
    """An ``omp-ui`` checkout under a fake project root, with Bun and both subprocess seams spied."""
    root = tmp_path / "checkout"
    ui_dir = root / "omp-ui"
    entry = ui_dir / "coding-agent" / "hermes" / "main.ts"
    entry.parent.mkdir(parents=True)
    entry.write_text("// entry\n", encoding="utf-8")
    (ui_dir / "package.json").write_text('{"name":"hermes-omp-ui"}\n', encoding="utf-8")
    (ui_dir / "bun.lock").write_text("lock\n", encoding="utf-8")

    monkeypatch.setattr(omp_launch, "project_root", lambda: root)
    monkeypatch.setattr(omp_launch, "_bun_bin", lambda: "/usr/bin/bun")
    monkeypatch.setattr(omp_launch, "_require_terminal", lambda: None)
    monkeypatch.delenv("HERMES_OMP_ALLOW_NO_TTY", raising=False)

    installs: list[tuple[list[str], str]] = []
    launches: list[tuple[list[str], str, dict]] = []

    def run(argv, cwd=None, check=None):
        installs.append((list(argv), str(cwd)))
        return SimpleNamespace(returncode=0)

    def call(argv, cwd=None, env=None):
        launches.append((list(argv), str(cwd), dict(env or {})))
        return 0

    monkeypatch.setattr(omp_launch.subprocess, "run", run)
    monkeypatch.setattr(omp_launch.subprocess, "call", call)
    return SimpleNamespace(root=root, ui_dir=ui_dir, installs=installs, launches=launches)


def test_cmd_omp_installs_once_then_launches_with_contract_env(fake_workspace) -> None:
    args = SimpleNamespace(resume="local-abc", no_start=False)

    assert omp_launch.cmd_omp(args) == 0
    assert fake_workspace.installs == [
        (["/usr/bin/bun", "install", "--ignore-scripts"], str(fake_workspace.ui_dir))
    ]
    assert (fake_workspace.ui_dir / "node_modules" / ".hermes-omp-ui").is_file()

    argv, cwd, env = fake_workspace.launches[0]
    assert argv == ["/usr/bin/bun", str(fake_workspace.ui_dir / "coding-agent" / "hermes" / "main.ts"), "--resume", "local-abc"]
    assert cwd == str(os.getcwd())
    assert env["HERMES_AGENT_ROOT"] == str(fake_workspace.root)
    assert env["HERMES_PYTHON"] == sys.executable
    assert env["HERMES_OMP_START"] == "1"
    assert env["HERMES_OMP_RESUME"] == "local-abc"

    # A second launch with the same lockfile must not pay for `bun install` again.
    assert omp_launch.cmd_omp(SimpleNamespace(resume=None, no_start=False)) == 0
    assert len(fake_workspace.installs) == 1
    second = fake_workspace.launches[1]
    assert second[0] == ["/usr/bin/bun", str(fake_workspace.ui_dir / "coding-agent" / "hermes" / "main.ts")]
    assert "HERMES_OMP_RESUME" not in second[2]


def test_cmd_omp_no_start_never_sets_the_start_flag(fake_workspace, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_OMP_START", "1")

    assert omp_launch.cmd_omp(SimpleNamespace(resume=None, no_start=True)) == 0
    assert "HERMES_OMP_START" not in fake_workspace.launches[0][2]


def test_cmd_omp_refuses_without_bun(fake_workspace, monkeypatch, capsys) -> None:
    monkeypatch.setattr(omp_launch, "_bun_bin", lambda: None)

    assert omp_launch.cmd_omp(SimpleNamespace(resume=None, no_start=False)) == 1
    assert "https://bun.sh" in capsys.readouterr().err
    assert fake_workspace.launches == []


def test_cmd_omp_refuses_without_the_workspace(fake_workspace) -> None:
    (fake_workspace.ui_dir / "coding-agent" / "hermes" / "main.ts").unlink()

    assert omp_launch.cmd_omp(SimpleNamespace(resume=None, no_start=False)) == 1


def test_cmd_omp_requires_a_tty(monkeypatch, capsys) -> None:
    class _NotATty:
        def isatty(self) -> bool:
            return False

    monkeypatch.delenv("HERMES_OMP_ALLOW_NO_TTY", raising=False)
    monkeypatch.setattr(sys, "stdin", _NotATty())
    monkeypatch.setattr(sys, "stdout", _NotATty())

    with pytest.raises(SystemExit) as excinfo:
        omp_launch._require_terminal()
    assert excinfo.value.code == 1
    assert "interactive terminal" in capsys.readouterr().err
