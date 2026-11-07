"""``hermes git stage`` — judge-driven selective staging (ADR-17 D7).

The parser stays import-light: ``hermes git --help`` must not resolve a judge
chain or touch a repository. The handler imports ``hermes_cli.git_stage`` lazily.
"""

from __future__ import annotations

import json
import sys
from typing import Callable

from hermes_cli.subcommands._shared import add_json_flag


def build_git_parser(subparsers, *, cmd_git: Callable) -> None:
    """Attach the ``git`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "git",
        help="Judge-driven git helpers (index only, never commits)",
        description=(
            "Stage the changes an instruction describes. The judge scores every "
            "unstaged hunk and whole file; only matching units reach the index."),
    )
    parser.set_defaults(func=cmd_git, git_action=None)
    actions = parser.add_subparsers(dest="git_action", metavar="<action>")

    stage = actions.add_parser("stage", help="Stage the changes an instruction describes")
    stage.add_argument(
        "instruction",
        help="Natural-language description of what to stage")
    stage.add_argument(
        "--path", default=None, metavar="PATH",
        help="Restrict judgment to one file or a directory subtree")
    stage.add_argument(
        "--dry-run", action="store_true",
        help="Report the match without touching the index")
    add_json_flag(stage, "Emit machine-readable JSON")
    stage.set_defaults(func=cmd_git)


def _cmd_stage(args) -> int:
    from agent.judgment import JudgmentError

    from hermes_cli import git_stage

    try:
        summary = git_stage.stage(
            args.instruction,
            path=args.path,
            dry_run=bool(args.dry_run),
        )
    except (RuntimeError, JudgmentError, OSError) as exc:
        print(f"git stage failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    verb = "would stage" if summary["dry_run"] else "staged"
    print(
        f"{verb} {summary['matched_hunks']}/{summary['total_hunks']} hunks and "
        f"{summary['whole_files']} whole files across "
        f"{summary['matched_files']}/{summary['total_files']} changed files")
    if summary["skipped"]:
        print(f"skipped conflicted files: {', '.join(summary['skipped'])}")
    if not summary["staged"]:
        print("nothing matched the instruction")
    return 0


def cmd_git(args) -> int:
    """Dispatch ``hermes git <action>``."""
    action = getattr(args, "git_action", None)
    if action == "stage":
        return _cmd_stage(args)
    print("usage: hermes git stage <instruction> [--path PATH] [--dry-run]", file=sys.stderr)
    return 2


__all__ = ["build_git_parser", "cmd_git"]
