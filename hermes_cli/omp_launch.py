"""``hermes omp``: launch the omp-rendered TUI (``omp-ui/``, a Bun project).

The UI is omp's own ``InteractiveMode`` from the vendored shell in ``omp-ui/src``; this module
only prepares the Bun workspace and the environment, then replaces the CLI with
``omp-ui/src/main.ts`` (the Hermes entry: argv → gateway session → InteractiveMode, without omp's
own CLI command layer or local agent). The shell pairs with this install's profile gateway and
talks JSON-RPC to it. Its **agent directory** (``config.yml``, ``keybindings.yml``, the session /
model / history DBs) is redirected under the Hermes home; omp's machine-global runtime artifacts
(``logs/``, ``stats.db``, ``install-id``, the native cache) still resolve under ``~/.omp`` because
``PI_CONFIG_DIR`` is a directory *name* joined to the user's home, not a path.

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


def _pm_bun(*, install: bool) -> str | None:
    """Bun from PM's store. ``install=False`` only reads the installed state."""
    try:
        import pm

        if not install:
            installed = pm.installed_package("bun")
            return str(installed.binary) if installed is not None and installed.binary is not None else None
        runner = pm.ensure("bun", explicit=True)
    except Exception:
        # Offline, an unsupported target, or a refused lazy install all mean the
        # caller gets the actionable "Bun is required" message instead of a traceback.
        return None
    return shutil.which("bun", path=runner.env.get("PATH", ""))


def _bun_bin() -> str | None:
    """$HERMES_BUN, then PM's installed copy, then PATH, then the default installer
    location; a Bun nobody installed is provisioned through PM on first use."""
    override = os.environ.get("HERMES_BUN", "").strip()
    if override:
        return override if Path(override).is_file() else None
    if pm_bun := _pm_bun(install=False):
        return pm_bun
    found = shutil.which("bun")
    if found:
        return found
    home = Path.home()
    for candidate in (home / ".bun" / "bin" / "bun.exe", home / ".bun" / "bin" / "bun"):
        if candidate.is_file():
            return str(candidate)
    print("omp-ui: preparing Bun through PM (`hermes pm install bun`)…", file=sys.stderr)
    return _pm_bun(install=True)


def _workspace_digest(ui_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in ("package.json", "bun.lock"):
        digest.update(name.encode("utf-8"))
        path = ui_dir / name
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()


def _ensure_dependencies(ui_dir: Path, bun: str) -> None:
    """``bun install`` once per lockfile, not once per launch (it costs seconds and the network).

    ``--production`` keeps the launcher to runtime dependencies (``@oh-my-pi/pi-tui``); the
    typecheck-only dev dependencies are installed by ``bun install`` when running ``check``.
    """
    digest = _workspace_digest(ui_dir)
    marker = ui_dir / "node_modules" / _INSTALL_MARKER
    try:
        current = marker.read_text(encoding="utf-8-sig").strip() if marker.is_file() else ""
    except OSError:
        current = ""
    if current == digest:
        return
    print("omp-ui: installing Bun dependencies…", file=sys.stderr)
    completed = subprocess.run([bun, "install", "--ignore-scripts", "--production"], cwd=str(ui_dir), check=False)
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


def _hermes_scoped_agent_dir() -> Path:
    """omp's agent dir (config, sessions, auth) under the active Hermes home."""
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "omp-ui" / "agent"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cmd_omp(args) -> int:
    """Replace the CLI with ``bun omp-ui/src/main.ts`` in the caller's cwd."""
    ui_dir = omp_ui_dir()
    entry = ui_dir / "src" / "main.ts"
    if not entry.is_file():
        print(
            f"omp-ui shell missing: {entry}\n"
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
            "Run `hermes pm install bun`, install it from https://bun.sh (for example "
            "`curl -fsSL https://bun.sh/install | bash` or "
            "`powershell -c \"irm bun.sh/install.ps1 | iex\"`), or point HERMES_BUN at the binary.",
            file=sys.stderr,
        )
        return 1

    _ensure_dependencies(ui_dir, bun)

    env = os.environ.copy()
    # The shell pairs with this install's gateway: the launcher names the interpreter and root.
    env["HERMES_AGENT_ROOT"] = str(project_root())
    env["HERMES_PYTHON"] = sys.executable
    # omp ignores PI_CODING_AGENT_DIR when a profile is active, so a profile env var inherited
    # from the caller's shell must not drag the shell back to ~/.omp/profiles/<p>/agent.
    env.pop("OMP_PROFILE", None)
    env.pop("PI_PROFILE", None)
    env["PI_CODING_AGENT_DIR"] = str(_hermes_scoped_agent_dir())
    # Labels the welcome lockup with this install's version instead of the renderer's.
    from hermes_cli import __version__ as hermes_version

    env["HERMES_VERSION"] = hermes_version

    argv = [bun, str(entry)]
    if getattr(args, "resume", None):
        argv.append(f"--resume={args.resume}")
    try:
        return subprocess.call(argv, cwd=os.getcwd(), env=env)
    except KeyboardInterrupt:
        return 130
