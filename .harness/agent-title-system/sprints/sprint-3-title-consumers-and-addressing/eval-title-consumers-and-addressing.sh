#!/usr/bin/env bash
# Sprint-3 acceptance runner (AS-1..AS-7).
#
# The repository's `scripts/run_tests.sh` wrapper is not usable from this shell: `pwd` yields a
# `C:\…` path, `_activation.sh` is absent, and the `/mnt/c` → `C:\mnt\c` re-exec mangles paths.
# The supported entrypoint here is `scripts/run_tests_parallel.py` under the pinned
# test-environment interpreter, which the runner itself applies (TZ/LANG/PYTHONHASHSEED,
# per-file subprocess isolation).
#
# Usage: bash eval-title-consumers-and-addressing.sh
# Writes one evidence file per acceptance scenario plus a combined log, and exits non-zero when
# any selected file fails.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
EVIDENCE="$HERE/evidence"
mkdir -p "$EVIDENCE"

PYTHON="$REPO/venv/Scripts/python.exe"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$REPO/venv/bin/python"
fi
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || command -v python)"
fi

# Run from the repository root with RELATIVE paths: this bash spells absolute paths as
# `/c/Users/…`, which the Windows interpreter re-maps to `C:\mnt\c\…` (the same reason
# `scripts/run_tests.sh` cannot be used here).
cd "$REPO" || exit 2
RUN=("$PYTHON" "scripts/run_tests_parallel.py")
declare -A FILES=(
  [AS-1]="tests/agent/test_title_generator.py;tests/agent/test_title_drift.py;tests/agent/test_title_input.py"
  [AS-2]="tests/agent/test_task_label.py"
  [AS-3]="tests/agent/test_task_label.py"
  [AS-4]="tests/hermes_state/test_agent_session_title.py"
  [AS-5]="tests/test_session_paths.py;tests/tools/test_agent_tool.py"
  [AS-6]="tests/tools/test_agent_out.py;tests/tools/test_agent_tool.py"
  [AS-7]="tests/tools/test_agent_message.py;tests/tools/test_agent_out.py"
)
declare -A EXPR=(
  [AS-1]="as1 or generator_switch"
  [AS-2]="as2"
  [AS-3]="as3"
  [AS-4]="as4"
  [AS-5]="as5 or DeriveTopic"
  [AS-6]="as6"
  [AS-7]="as7"
)

status=0
for scenario in AS-1 AS-2 AS-3 AS-4 AS-5 AS-6 AS-7; do
  log="$EVIDENCE/$scenario-pytest.txt"
  {
    echo "### $scenario"
    echo "### $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "### runner: scripts/run_tests_parallel.py --paths ${FILES[$scenario]} -q -k '${EXPR[$scenario]}'"
  } > "$log"
  "${RUN[@]}" --paths "${FILES[$scenario]}" -q -k "${EXPR[$scenario]}" >> "$log" 2>&1
  rc=$?
  echo "### exit=$rc" >> "$log"
  echo "$scenario exit=$rc"
  [ "$rc" -ne 0 ] && status=1
done

exit "$status"
