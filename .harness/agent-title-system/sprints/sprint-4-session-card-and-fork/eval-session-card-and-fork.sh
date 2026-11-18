#!/usr/bin/env bash
# Sprint-4 acceptance runner (AS-1..AS-7): session card + fork generator.
#
# The repository's `scripts/run_tests.sh` wrapper is not usable from this shell: `pwd` yields a
# `C:\…` path, `_activation.sh` is absent, and the `/mnt/c` → `C:\mnt\c` re-exec mangles paths.
# The supported entrypoint here is `scripts/run_tests_parallel.py` under the pinned
# test-environment interpreter, which the runner itself applies (TZ/LANG/PYTHONHASHSEED,
# per-file subprocess isolation).
#
# Usage: bash eval-session-card-and-fork.sh
# Writes one evidence file per acceptance scenario, and exits non-zero when any selected file
# fails.
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
  [AS-1]="tests/agent/test_title_card.py"
  [AS-2]="tests/agent/test_prompt_sections.py"
  [AS-3]="tests/agent/test_title_fork.py"
  [AS-4]="tests/agent/test_title_fork.py"
  [AS-5]="tests/agent/test_title_fork.py"
  [AS-6]="tests/agent/test_title_card.py;tests/hermes_state/test_session_title_cards.py;tests/agent/test_title_fork.py"
  [AS-7]="tests/agent/test_title_fork.py;tests/agent/test_title_card.py"
)
declare -A EXPR=(
  [AS-1]="SplitCardTitle or ParseCardReply"
  [AS-2]="title_card or title_fork or card_templates"
  [AS-3]="as3"
  [AS-4]="as4"
  [AS-5]="as5"
  [AS-6]="as6 or KeepTitleCard or carded"
  [AS-7]="as7 or SplitCardTitle"
)
declare -A KEY=(
  [AS-1]="tag attrs / '<icon> <CODE>:' / 'CODE:' parse; 1-6 ASCII code; 1-8 non-ASCII code points (len()); nf dropped; invalid degrades to the unchanged plain title"
  [AS-2]="title_card/title_fork scope=request with the title_system manifest shape; card/plain/language renders under StrictUndefined; French line is the last non-empty before </title-request>; no Go remnants; no Nerd Font guidance; missing keys raise"
  [AS-3]="one side turn on the parent (provider/model/base_url, cached system prompt, tools[]); user_message = title_fork render + opener; history = trimmed snapshot (independent list); empty tool whitelist, max_iterations=1; no transcript row/leaf/list change; usage billed to title_generation; source=llm"
  [AS-4]="timeout/refusal/empty/no-parent fall back to the plan once; exactly one title_card request then; trailing card words ignored; unusable card keeps the plain title; generator=plan runs no fork/card; unset key behaves like fork; <title/> terminates without a write; no plan output keeps the derived title"
  [AS-5]="boring renders title_fork with card=False and no emoji/code guidance; stored value is the plain title; zero title_card requests; drift regeneration does not call keep_title_card"
  [AS-6]="keep_title_card carries the old card; judge state carries the de-carded title; card-only difference is not a rewrite; CAS expected_title is the old full title; resolve_session_by_title finds the carded row after exact/'#N' miss; get_session_by_title stays exact"
  [AS-7]="manual titles start no fork/card/LLM and are never judged; every naming render passes nerd_fonts=False; nf attributes never reach the card"
)

status=0
for scenario in AS-1 AS-2 AS-3 AS-4 AS-5 AS-6 AS-7; do
  log="$EVIDENCE/$scenario.pytest.txt"
  {
    echo "### $scenario"
    echo "### $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "### runner: scripts/run_tests_parallel.py --paths ${FILES[$scenario]} -q -k '${EXPR[$scenario]}'"
    echo "### key assertions: ${KEY[$scenario]}"
  } > "$log"
  "${RUN[@]}" --paths "${FILES[$scenario]}" -q -k "${EXPR[$scenario]}" >> "$log" 2>&1
  rc=$?
  echo "### exit=$rc" >> "$log"
  if [ "$rc" -ne 0 ]; then
    status="$rc"
  fi
done

exit "$status"
