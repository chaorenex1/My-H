"""``hermes omp`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_omp_parser(subparsers, *, cmd_omp: Callable) -> None:
    """Attach ``omp`` to ``subparsers`` (the omp shell in ``omp-ui/src``)."""
    omp_parser = subparsers.add_parser(
        "omp",
        help="Open the omp-rendered terminal UI (Bun)",
        description="Open Hermes' terminal UI rendered with omp's components "
            "(@oh-my-pi/pi-tui). The shell runs from omp-ui/src with its agent "
            "directory (config, keybindings, session DBs) scoped under the Hermes home. "
            "First run installs the Bun workspace in "
            "omp-ui/ (Bun comes from PM: `hermes pm install bun`, or https://bun.sh).")
    omp_parser.add_argument(
        "--resume", "-r", metavar="SESSION",
        help="Resume a Hermes session by id or title (the gateway session, not an omp-local one)")
    omp_parser.set_defaults(func=cmd_omp)
