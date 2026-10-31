"""``hermes omp`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_omp_parser(subparsers, *, cmd_omp: Callable) -> None:
    """Attach ``omp`` to ``subparsers`` (the omp-rendered TUI in ``omp-ui/``)."""
    omp_parser = subparsers.add_parser(
        "omp",
        help="Open the omp-rendered terminal UI (Bun)",
        description="Open Hermes' terminal UI rendered with omp's components "
            "(@oh-my-pi/pi-tui). It attaches to the profile gateway like `hermes --tui`, "
            "and starts that gateway when none is running. First run installs the Bun "
            "workspace in omp-ui/ (needs https://bun.sh).")
    omp_parser.add_argument(
        "--resume", "-r", metavar="SESSION",
        help="Resume a stored session id instead of creating a new one")
    omp_parser.add_argument(
        "--no-start", action="store_true",
        help="Attach to an already-running gateway; never start one")
    omp_parser.set_defaults(func=cmd_omp)
