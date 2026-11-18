#!/usr/bin/env bash
# Sprint-local evaluator for sprint-2-title-model-plan (ADR-21 D3 stage 1).
#
# Acceptance evidence: one pytest selection per AS through the canonical runner
# (scripts/run_tests.sh — never bare pytest), plus an independent scan of agent/title_router.py
# for the two things the contract forbids at plan time: extra route resolution
# (_get_aux_model_for_provider / _get_cached_client / _resolve_fallback_entry) and a second
# marker parser. Each AS section is appended to evidence/AS-<id>.txt; any non-zero exit fails the
# script while every section still runs.
set -uo pipefail

SPRINT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SPRINT_DIR/../../../.." && pwd)"
EVIDENCE="$SPRINT_DIR/evidence"
mkdir -p "$EVIDENCE"
cd "$REPO_ROOT" || exit 1
# Harness tooling path (jq et al.) on this workstation; no-op elsewhere.
[ -d "/c/Users/zarag/bin" ] && export PATH="/c/Users/zarag/bin:$PATH"

status=0

run_as() {
    local id="$1"; shift
    local ev="$EVIDENCE/AS-$id.txt"
    local out rc append=0
    [ -f "$ev" ] && append=1
    out="$(mktemp)"
    "$@" >"$out" 2>&1
    rc=$?
    {
        if [ "$append" = 0 ]; then
            echo "### AS-$id: $*"
        else
            echo
            echo "### AS-$id (continued): $*"
        fi
        echo
        cat "$out"
        echo "exit: $rc"
    } >>"$ev"
    if [ "$rc" -ne 0 ]; then
        echo "!!! AS-$id FAILED (exit $rc) — see $ev"
        status=1
    else
        echo "--- AS-$id ok ($ev)"
    fi
    rm -f "$out"
}

TEST_FILE=tests/agent/test_title_router.py

# AS-1: default title route → exactly the session model, one call with fallback=False.
run_as 1 scripts/run_tests.sh "$TEST_FILE" -- -q -k "as1"

# AS-2: pinned roles plan tiny → commit_message → smol → current, per-task timeouts, auto skipped.
run_as 2 scripts/run_tests.sh "$TEST_FILE" -- -q -k "as2"

# AS-3: a pinned title_generation route is candidate 0; the default route adds none.
run_as 3 scripts/run_tests.sh "$TEST_FILE" -- -q -k "as3"

# AS-4: depth-first chain expansion (exact key → provider wildcard → per-task), dedupe, cycle cut.
run_as 4 scripts/run_tests.sh "$TEST_FILE" -- -q -k "as4"

# AS-5: either gate off, or an empty plan, produces zero model calls.
run_as 5 scripts/run_tests.sh "$TEST_FILE" -- -q -k "as5"

# AS-6: one call per candidate, failure advances, first success stops, tag declines terminate.
run_as 6 scripts/run_tests.sh "$TEST_FILE" -- -q -k "as6"

# AS-4/AS-6 independent scan: the plan layer resolves no routes and owns no parser.
run_as 4 python - <<'PYEOF'
import ast
from pathlib import Path

PLAN_FORBIDDEN = ("_get_aux_model_for_provider", "_get_cached_client", "_resolve_fallback_entry")
PARSERS = ("parse_title_output", "clean_title")
source = Path("agent/title_router.py").read_text(encoding="utf-8")
tree = ast.parse(source, filename="agent/title_router.py")

called = set()
defined = set()
imported = {}
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        defined.add(node.name)
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            imported[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        called.add(node.func.id)
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        called.add(node.func.attr)
    elif isinstance(node, ast.Attribute):
        called.add(node.attr)

problems = [f"plan-time route resolution: {name}" for name in PLAN_FORBIDDEN if name in called]
problems += [f"local parser copy: {name}" for name in PARSERS if name in defined]
problems += [f"local parser copy: {name}()" for name in PARSERS if name in called and name not in imported]
if imported.get("parse_title_output") != "agent.title_parser.parse_title_output":
    problems.append("marker parsing is not the sprint-1 agent.title_parser entry")

print("agent/title_router.py imports:", sorted(imported.values()))
print("plan-time resolution calls:", sorted(called & set(PLAN_FORBIDDEN)))
for problem in problems:
    print("FAIL:", problem)
raise SystemExit(1 if problems else 0)
PYEOF

exit "$status"
