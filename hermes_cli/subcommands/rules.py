"""``hermes rules`` subcommand parser."""

from __future__ import annotations

from typing import Callable

from hermes_cli.subcommands._shared import add_json_flag


def build_rules_parser(subparsers, *, cmd_rules: Callable) -> None:
    """Attach the ``rules`` subcommand to ``subparsers``."""
    rules_parser = subparsers.add_parser(
        "rules",
        help="List, inspect, and check rule files",
        description=(
            "Rule files are Markdown with optional YAML frontmatter. Discovery follows the "
            "named-agent scopes: project (.agents/rules, rules), profile, then installation."),
    )
    rules_subparsers = rules_parser.add_subparsers(dest="rules_action")

    list_parser = rules_subparsers.add_parser("list", help="List visible rules")
    list_parser.add_argument(
        "--scope", default="", choices=["", "project", "profile", "installation"],
        help="Filter by trust scope (default: all)")
    add_json_flag(list_parser, "Emit machine-readable JSON instead of a table")
    list_parser.set_defaults(func=cmd_rules)

    show_parser = rules_subparsers.add_parser("show", help="Show one rule")
    show_parser.add_argument("rule_id", help="Rule id (python/coding-style) or unique name")
    add_json_flag(show_parser, "Emit machine-readable JSON instead of text")
    show_parser.set_defaults(func=cmd_rules)

    check_parser = rules_subparsers.add_parser("check", help="Validate visible rules")
    add_json_flag(check_parser, "Emit machine-readable JSON instead of text")
    check_parser.set_defaults(func=cmd_rules)
