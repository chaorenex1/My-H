"""``hermes omp``: launch the omp-rendered TUI (``omp-ui/``, a Bun project).

``omp-ui`` is a client of the profile gateway, exactly like ``ui-tui``: it attaches to the
canonical WebSocket and never opens ``state.db`` itself. This module only prepares the Bun
workspace and the environment; the UI itself is omp's ``InteractiveMode`` in
``omp-ui/coding-agent/hermes/``, driven over the gateway by ``omp-ui/coding-agent/hermes/main.ts``.

Why Bun: the ``@oh-my-pi/pi-tui`` packages publish TypeScript sources (``exports`` point at
``src/*.ts``), so no build step exists and Node cannot load them.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Marker under node_modules; its content is the digest of the files that decide the install.
_INSTALL_MARKER = ".hermes-omp-ui"


def project_root() -> Path:
    from hermes_cli._startup_fast import project_root_str

    return Path(project_root_str())


def omp_ui_dir() -> Path:
    return project_root() / "omp-ui"


def _bun_bin() -> str | None:
    """$HERMES_BUN, then PATH, then the default installer location (not always on PATH)."""
    override = os.environ.get("HERMES_BUN", "").strip()
    if override:
        return override if Path(override).is_file() else None
    found = shutil.which("bun")
    if found:
        return found
    home = Path.home()
    for candidate in (home / ".bun" / "bin" / "bun.exe", home / ".bun" / "bin" / "bun"):
        if candidate.is_file():
            return str(candidate)
    return None


def _workspace_digest(ui_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in ("package.json", "bun.lock"):
        digest.update(name.encode("utf-8"))
        path = ui_dir / name
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()


def _ensure_dependencies(ui_dir: Path, bun: str) -> None:
    """``bun install`` once per lockfile, not once per launch (it costs seconds and the network)."""
    digest = _workspace_digest(ui_dir)
    marker = ui_dir / "node_modules" / _INSTALL_MARKER
    try:
        current = marker.read_text(encoding="utf-8-sig").strip() if marker.is_file() else ""
    except OSError:
        current = ""
    if current == digest:
        return
    print("omp-ui: installing Bun dependencies…", file=sys.stderr)
    completed = subprocess.run([bun, "install", "--ignore-scripts"], cwd=str(ui_dir), check=False)
    if completed.returncode != 0:
        sys.exit("omp-ui: `bun install` failed; fix the error above and retry `hermes omp`.")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(digest + "\n", encoding="utf-8")


def _require_terminal() -> None:
    if os.environ.get("HERMES_OMP_ALLOW_NO_TTY") == "1":
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        return
    print(
        "hermes omp needs an interactive terminal: it is a full-screen UI and cannot run "
        "through a pipe. Use `hermes chat` for non-interactive runs.",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_omp(args) -> int:
    """Replace the CLI with ``bun omp-ui/coding-agent/hermes/main.ts`` in the caller's cwd."""
    ui_dir = omp_ui_dir()
    entry = ui_dir / "coding-agent" / "hermes" / "main.ts"
    if not entry.is_file():
        print(
            f"omp-ui workspace missing: {entry}\n"
            "`hermes omp` ships with a source checkout (`omp-ui/` next to `ui-tui/`), not with "
            "prebuilt installs — use `hermes --tui` there.\n"
            "In a checkout, recover it with `git restore -- omp-ui` or `hermes update --force`.",
            file=sys.stderr,
        )
        return 1

    _require_terminal()
    bun = _bun_bin()
    if not bun:
        print(
            "Bun is required for `hermes omp` but `bun` was not found.\n"
            "Install it from https://bun.sh (for example `curl -fsSL https://bun.sh/install | bash` "
            "or `powershell -c \"irm bun.sh/install.ps1 | iex\"`), or point HERMES_BUN at the binary.",
            file=sys.stderr,
        )
        return 1

    _ensure_dependencies(ui_dir, bun)

    env = os.environ.copy()
    # The shell runs the checkout's own bootstrap helper, with the interpreter this CLI runs on:
    # a profile installed with a different venv must not be bootstrapped by a global python.
    env["HERMES_AGENT_ROOT"] = str(project_root())
    env["HERMES_PYTHON"] = sys.executable
    # Labels the omp welcome lockup with this install's version instead of the renderer's.
    from hermes_cli import __version__ as hermes_version

    env["HERMES_VERSION"] = hermes_version
    env.pop("HERMES_OMP_RESUME", None)
    if getattr(args, "resume", None):
        env["HERMES_OMP_RESUME"] = args.resume
    if getattr(args, "no_start", False):
        env.pop("HERMES_OMP_START", None)
    else:
        env["HERMES_OMP_START"] = "1"

    argv = [bun, str(entry)]
    if getattr(args, "resume", None):
        argv += ["--resume", args.resume]
    try:
        return subprocess.call(argv, cwd=os.getcwd(), env=env)
    except KeyboardInterrupt:
        return 130
