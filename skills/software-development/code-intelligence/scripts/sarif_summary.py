#!/usr/bin/env python3
"""Summarize SARIF findings from Semgrep or CodeQL.

Usage:
    python sarif_summary.py results.sarif
    python sarif_summary.py results.sarif --json
    python sarif_summary.py semgrep.sarif codeql.sarif

The Markdown table is meant for a chat reply or report. --json prints a stable
list for further processing. Standard library only; no network access.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

_SEVERITY_ORDER = {"error": 0, "warning": 1, "note": 2, "none": 3}


def _location(result: dict[str, Any]) -> tuple[str, str]:
    """Return (path, line) for the first physical location, or ('?', '?')."""
    locations = result.get("locations") or []
    if not locations:
        return "?", "?"
    physical = locations[0].get("physicalLocation") or {}
    artifact = physical.get("artifactLocation") or {}
    uri = str(artifact.get("uri") or "?")
    if uri.startswith("file:"):
        path = unquote(urlsplit(uri).path)
        # Windows SARIF commonly uses file:///C:/...; drop the leading slash.
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        uri = path
    region = physical.get("region") or {}
    line = region.get("startLine")
    return uri, str(line) if line is not None else "?"


def _level(result: dict[str, Any], rule: dict[str, Any]) -> str:
    level = str(result.get("level") or "").lower()
    if level:
        return level
    default = (rule.get("defaultConfiguration") or {}).get("level")
    return str(default or "warning").lower()


def summarize(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one SARIF document into rows sorted by severity, path, line."""
    rows: list[dict[str, Any]] = []
    for run in document.get("runs") or []:
        driver = ((run.get("tool") or {}).get("driver") or {})
        tool = str(driver.get("name") or "?")
        rules = {str(rule.get("id")): rule for rule in driver.get("rules") or []}
        for result in run.get("results") or []:
            rule_id = str(result.get("ruleId") or "?")
            rule = rules.get(rule_id, {})
            path, line = _location(result)
            message = result.get("message") or {}
            text = str(message.get("text") or message.get("markdown") or "")
            rows.append({
                "tool": tool,
                "severity": _level(result, rule),
                "rule": rule_id,
                "path": path,
                "line": line,
                "message": " ".join(text.split()),
            })
    rows.sort(key=lambda row: (_SEVERITY_ORDER.get(row["severity"], 9),
                               row["path"], _line_number(row["line"]), row["rule"]))
    return rows


def _line_number(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def markdown(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No SARIF findings."
    lines = [
        f"SARIF findings: {len(rows)}",
        "",
        "| Severity | Tool | Rule | Location | Message |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        location = f"{row['path']}:{row['line']}"
        lines.append(
            f"| {_cell(row['severity'])} | {_cell(row['tool'])} | {_cell(row['rule'])} | "
            f"{_cell(location)} | {_cell(row['message'])} |"
        )
    return "\n".join(lines)


def _load(paths: list[Path]) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for path in paths:
        with path.open(encoding="utf-8-sig") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
            raise ValueError(f"{path}: not a SARIF 2.x document")
        runs.extend(data["runs"])
    return {"runs": runs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize SARIF findings.")
    parser.add_argument("sarif", nargs="+", type=Path, help="SARIF file(s) to read")
    parser.add_argument("--json", action="store_true", help="print rows as JSON")
    args = parser.parse_args(argv)
    try:
        rows = summarize(_load(args.sarif))
    except (OSError, ValueError) as exc:
        print(f"sarif_summary: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(markdown(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
