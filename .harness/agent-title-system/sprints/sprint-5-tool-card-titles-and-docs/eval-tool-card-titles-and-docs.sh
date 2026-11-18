#!/usr/bin/env bash
# Sprint-5 acceptance runner (AS-1..AS-5): tool-card titles registered + docs reconciled.
#
# The repository's `scripts/run_tests.sh` wrapper is not usable from this shell: `pwd` yields a
# `C:\…` path, `_activation.sh` is absent, and the `/mnt/c` → `C:\mnt\c` re-exec mangles paths.
# The accepted substitute (sprint-3/4, recorded in the contract) is `scripts/run_tests_parallel.py`
# under the checkout's pinned test environment (`pm.testenv`), with the runner's TZ/LANG/
# PYTHONHASHSEED. The interpreter is resolved through the checkout's own resolver, never guessed.
#
# Usage: bash eval-tool-card-titles-and-docs.sh
# Writes one evidence file per acceptance scenario plus a regression run, and exits non-zero when
# any selected run fails.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# `git rev-parse --show-toplevel` yields the forward-slash Windows path this bash can execute;
# the dirname-based fallback keeps the script runnable outside a worktree.
REPO="$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null | tr -d '\r')"
if [ -z "${REPO:-}" ]; then
  REPO="$(cd "$HERE/../../../.." && pwd)"
fi
EVIDENCE="$HERE/evidence"
mkdir -p "$EVIDENCE"

# Run from the repository root with RELATIVE paths: this bash spells absolute paths as
# `/c/Users/…`, which the Windows interpreter re-maps to `C:\mnt\c\…`.
cd "$REPO" || exit 2

# Pinned test interpreter: ask the checkout's resolver (pm.testenv), fall back to the venv python
# that sprints 3/4 used when resolution is unavailable. The resolver prints a Windows path, so the
# backslashes are normalized before bash tests the executable bit.
PYTHON=""
# This bash reports `/mnt/c/...` paths but cannot exec a bare `C:/...` child from that cwd, so a
# resolver answer is normalized to the same `/mnt/<drive>/...` form the runner itself uses.
_to_exec_path() {
  case "$1" in
    [A-Za-z]:/*) printf '/mnt/%s/%s' "$(printf '%s' "${1%%:*}" | tr 'A-Z' 'a-z')" "${1#*:/}" ;;
    *) printf '%s' "$1" ;;
  esac
}
for resolver in "$REPO/venv/Scripts/python.exe" "$REPO/venv/bin/python"; do
  if [ -x "$resolver" ]; then
    PYTHON="$("$resolver" -c 'from pathlib import Path; from pm.testenv import testenv_python; print(str(testenv_python(Path.cwd())).replace("\\", "/"))' 2>/dev/null | tr -d '\r')"
    [ -n "${PYTHON:-}" ] && PYTHON="$(_to_exec_path "$PYTHON")"
    if [ -n "${PYTHON:-}" ] && "$PYTHON" -c 'import pytest, pytest_asyncio' 2>/dev/null; then
      break
    fi
    PYTHON=""
  fi
done
if [ -z "${PYTHON:-}" ]; then
  PYTHON="$REPO/venv/Scripts/python.exe"
fi
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || command -v python)"
fi

export TZ=UTC LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONHASHSEED=0

status=0

# run_case <log> <label> <paths> [pytest args...]
run_case() {
  local log="$1" label="$2" paths="$3"
  shift 3
  {
    echo "### $label"
    echo "### $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "### interpreter: $PYTHON"
    echo "### runner: TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 $PYTHON scripts/run_tests_parallel.py --paths $paths -q $*"
  } >> "$log"
  "$PYTHON" scripts/run_tests_parallel.py --paths "$paths" -q "$@" >> "$log" 2>&1
  local rc=$?
  echo "### exit=$rc" >> "$log"
  if [ "$rc" -ne 0 ]; then status="$rc"; fi
}

declare -A AS=(
  [AS-1]="tests/tools/test_eval_card_metadata_contract.py"
  [AS-2]="tests/gateway/test_tool_card_title_contract.py"
  [AS-3]="tests/tools/test_eval_card_metadata_contract.py"
  [AS-4]="tests/gateway/test_tool_card_title_contract.py"
)
declare -A EXPR=(
  [AS-1]="as1"
  [AS-2]="as2"
  [AS-3]="as3"
  [AS-4]="as4"
)
declare -A KEY=(
  [AS-1]="identical card_title/cell_titles across default, model_upgrade_enabled=false, generator=fork, card_icons=boring and pinned tiny/commit_message/smol/title routes; both call_llm spies 0; _UPGRADE_THREADS monotone (subset) and no new agent.title_generator thread target; card_titles returns a dict synchronously; derive_title counted through a pass-through wrapper (3 derivations, never a stub)"
  [AS-2]="tool.start carries title + non-empty cell_titles; per-cell tool.title with cell_index immediately after start; no tool.complete/tool.error between them; ToolStartPayload/ToolTitlePayload accept the declared fields and reject an unknown one; a late tool.title with cell_index is valid; result text never carries a title"
  [AS-3]="execute_code per-cell titles (x = 1 / JS cell) with card_title = first cell; top-level title wins (One); plain code first line (print(2)); result cells fill in (df.head()); ordinary tools card_title-only; empty args {}; repeated identity reuses the cached title"
  [AS-4]="prepare_fields and complete_fields share display/card_title/cell_titles at the top level; _history_to_messages details equal the durable row (top-level and legacy tool_result_metadata sidecar); model-facing content byte-identical with/without titles; stamp_tool_result_metadata merges without rewriting a concurrent key"
)

for scenario in AS-1 AS-2 AS-3 AS-4; do
  log="$EVIDENCE/$scenario.pytest.txt"
  {
    echo "### $scenario"
    echo "### key assertions: ${KEY[$scenario]}"
  } > "$log"
  run_case "$log" "$scenario" "${AS[$scenario]}" -k "${EXPR[$scenario]}"
done

as5_log="$EVIDENCE/AS-5.pytest.txt"
{
  echo "### AS-5"
  echo "### key assertions: title chapter names the workload switch + route override and is not the"
  echo "### only title source; default fork and the tiny -> commit_message -> smol -> session-model"
  echo "### plan with a pinned route heading it only when pinned; the <title> marker is the current"
  echo "### request format; sample-config title block stays commented and states the override; the"
  echo "### imported picker hint names workload + override (no 'backup'); the four named prompts are"
  echo "### scope=request and title-marker-instruction.md stays unwired; prompt_sections and the"
  echo "### prompt-surface summary guard stay green (the committed snapshot is a pre-existing red,"
  echo "### documented with base-SHA evidence in evidence/iter-1/)."
} > "$as5_log"
run_case "$as5_log" "AS-5 docs contract" "tests/ci/test_title_docs_contract.py" -k "as5"
run_case "$as5_log" "AS-5 picker registry" "tests/hermes_cli/test_aux_slots.py"
run_case "$as5_log" "AS-5 prompt sections" "tests/agent/test_prompt_sections.py"
run_case "$as5_log" "AS-5 prompt-surface summary guard" "tests/ci/test_prompt_surface.py" -k "test_summary_names_the_surface_and_tool_that_changed"

: > "$EVIDENCE/existing-suites.pytest.txt"
{
  echo "### existing suites (regression, unchanged behaviour)"
  echo "### the same fixture/assertion surfaces the contract told us to reuse"
} >> "$EVIDENCE/existing-suites.pytest.txt"
run_case "$EVIDENCE/existing-suites.pytest.txt" "existing card/metadata suites" \
  "tests/tools/test_eval_card_metadata.py;tests/tui_gateway/test_history_tool_content.py;tests/tui_gateway/test_show_reasoning_display_gate.py"
run_case "$EVIDENCE/existing-suites.pytest.txt" "existing local-viewer AS-2/AS-4 selection" \
  "tests/gateway/test_local_viewer_events_turn.py" \
  -k "tool_start_carries_the_requesting_steps_usage or tool_start_without_a_step_usage_omits_the_field or prepared_eval_card_fields_are_top_level_and_reused_by_the_live_frame or prepared_edit_diff_is_reused_by_the_completion_event"

exit "$status"
