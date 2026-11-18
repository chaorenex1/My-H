#!/usr/bin/env bash
# Sprint-local evaluator for sprint-1-title-protocol (ADR-21 D4/D5).
#
# Acceptance evidence: runs the marker-contract pytest suites through the canonical runner
# (scripts/run_tests.sh — never bare pytest) and an independent AST/source scan for the four
# retired JSON-contract symbols. Each AS section is tee'd to evidence/AS-<id>.txt; any failure
# exits non-zero while preserving all output.
#
# Selection note (pre-existing environment failure, NOT a contract defect):
#   tests/agent/test_title_generator.py::TestModelSwitchMarkerNotTitleable::test_marker_prefix_matches_gateway_constant
#   is deselected: importing tui_gateway.server runs the agent bootstrap chain, whose
#   import-time dependency activation probes <checkout>/../manifest.json — outside the checkout
#   on this machine's default-install layout, which tests/home_io_guard.py refuses before the
#   (absent) file is even read. Unrelated to AS-1..AS-5; recorded in feedback/generator-1.md.
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

# AS-1: title_system / task_label registered at scope=request; StrictUndefined rejects the
# missing declared key; unknown keys and the archived marker-instruction file stay out.
run_as 1 scripts/run_tests.sh tests/agent/test_prompt_sections.py -- -q \
    -k "title_system or task_label or title_marker_instruction or manifest_scope"

# AS-2: the {% if title_language %} line is byte-identical when empty, verbatim when set.
run_as 2 scripts/run_tests.sh tests/agent/test_prompt_sections.py -- -q -k "language_line"

# AS-3: the single <title> marker contract (closed / <title/> / <title>none</title> / prose).
run_as 3 scripts/run_tests.sh tests/agent/test_title_parser.py -- -q -k "MarkerParsing"

# AS-4: shared guard strips quotes/Title:/punctuation, rejects instead of truncating, think first.
run_as 4 scripts/run_tests.sh tests/agent/test_title_parser.py -- -q -k "SharedGuard"

# AS-5 (pytest part): the naming path sends no response_format, parses via the marker parser,
# and the auxiliary structured-output retry (still exercised by plugin completions) stays green.
run_as 5 scripts/run_tests.sh tests/agent/test_title_generator.py -- -q \
    --deselect tests/agent/test_title_generator.py::TestModelSwitchMarkerNotTitleable::test_marker_prefix_matches_gateway_constant
run_as 5 scripts/run_tests.sh tests/agent/test_structured_output_rejection_retry.py -- -q

# AS-5 (independent scan): the retired JSON-contract symbols have no definitions and no call
# sites anywhere in the Python tree, the naming path renders the registered request templates,
# generate_title's call_llm carries no extra_body/response_format, and the retitle-skills
# command still routes through generate_title (one parser, no separate JSON request).
run_as 5 python - <<'PYEOF'
import ast
from pathlib import Path

REPO = Path(".").resolve()
SKIP_DIRS = {".git", ".harness", ".omp", ".specs", "website", "node_modules", ".venv", "venv"}
RETIRED = (
    "_TITLE_RESPONSE_FORMAT", "_extract_json_title", "_title_from_reasoning",
    "_is_prompt_example_echo", "_EXAMPLE_ECHO_REJECT", "_extract_title_text",
)

py_files = [
    p for p in REPO.rglob("*.py")
    if not (set(p.parts) & SKIP_DIRS)
]

def defined_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, ast.Tuple):
                    names.update(e.id for e in t.elts if isinstance(e, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names

problems = []
definitions = 0
for path in py_files:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        problems.append(f"unreadable: {path} ({exc})")
        continue
    names = defined_names(tree)
    definitions += len(names & set(RETIRED))
    for node in ast.walk(tree):
        hit = None
        if isinstance(node, ast.Name) and node.id in RETIRED:
            hit = f"name {node.id!r}"
        elif isinstance(node, ast.Attribute) and node.attr in RETIRED:
            hit = f"attribute {node.attr!r}"
        if hit:
            problems.append(f"{path}:{node.lineno}: {hit}")

print(f"scanned {len(py_files)} files under {REPO.name}/ (excluding {sorted(SKIP_DIRS)})")
print(f"retired symbol definitions found: {definitions}")
if problems:
    print("PROBLEMS:")
    for line in problems:
        print(" ", line)
    raise SystemExit(1)

# Positive bindings: the one marker parser, its consumers, and a JSON-free naming request.
title_parser = (REPO / "agent" / "title_parser.py").read_text(encoding="utf-8-sig")
title_generator = ast.parse(
    (REPO / "agent" / "title_generator.py").read_text(encoding="utf-8-sig"))
sessions_cmd = (REPO / "hermes_cli" / "sessions_cmd.py").read_text(encoding="utf-8-sig")
assert "def parse_title_output" in title_parser and "def clean_title" in title_parser
gen_names = defined_names(title_generator)
assert "parse_title_output" not in gen_names  # consumed, not re-defined
assert "generate_title" in gen_names
for node in ast.walk(title_generator):
    if isinstance(node, ast.Call):
        func = node.func
        if (isinstance(func, ast.Name) and func.id == "call_llm") or (
                isinstance(func, ast.Attribute) and func.attr == "call_llm"):
            kw_names = [k.arg for k in node.keywords]
            assert "extra_body" not in kw_names and "response_format" not in kw_names, (
                f"generate_title's call_llm sends a structured-output field: {kw_names}")
assert "from agent.title_generator import generate_title" in sessions_cmd
for token in RETIRED[:4] + ("response_format", "extra_body"):
    assert token not in sessions_cmd, f"retitle-skills path mentions {token}"
print("bindings ok: parse_title_output defined once; title_generator consumes it;")
print("generate_title.call_llm sends no extra_body/response_format;")
print("hermes sessions retitle-skills imports generate_title and builds no JSON request")
PYEOF

echo
if [ "$status" -ne 0 ]; then
    echo "=== eval-title-protocol: FAILED ==="
else
    echo "=== eval-title-protocol: ALL AS SECTIONS PASSED ==="
fi
exit "$status"
