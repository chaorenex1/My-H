"""Repository helpers shared by codegraph test modules."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    result = git(root, "init", "-q")
    if result.returncode != 0:
        pytest.skip(f"git unavailable: {result.stderr}")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "user.name", "Tests")
    return root


def commit(root: Path, message: str = "init") -> None:
    git(root, "add", "-A")
    result = git(root, "commit", "-qm", message)
    if result.returncode != 0:
        pytest.skip(f"git commit unavailable: {result.stderr}")
