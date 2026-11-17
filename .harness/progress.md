# Harness Progress Log

Append-only. One event per line: `[<ISO-8601-UTC>] <event>`.
Written by the harness hook and by agents; never rewritten.

[2026-11-17T16:25:00Z] harness-plan: user confirmed epic=agent-title-system scope=P0-P4 rubric=api+custom-script backend=omp
[2026-11-17T16:25:10Z] harness-plan: _config.yml project_type web->api, evaluator_tools->[custom-script, curl]; planner.md rubric preset -> API
[2026-11-17T16:25:20Z] env: WSL bash has no jq/gh; installed jq into ~/.local/bin (WSL) and C:\Users\zarag\bin\jq.exe (Git Bash); run harness scripts with Git Bash on Windows
[2026-11-17T16:40:00Z] tracker: issues enabled on chaorenex1/My-H; bare `gh` resolves to upstream NousResearch/hermes-agent, so every gh call must pass -R chaorenex1/My-H (or GH_REPO=chaorenex1/My-H). Recorded as _state.json.tracker_repo.
[2026-11-17T16:52:00Z] harness-plan: epic issue created chaorenex1/My-H#3
[2026-11-17T17:05:00Z] harness-plan: sprint issues 4,5,6,7,8 created on chaorenex1/My-H (epic #3)
[2026-11-17T17:05:10Z] harness-plan: epic=agent-title-system sprints=5 bundles=0 tracker=github
[2026-11-17T17:12:00Z] harness-plan: contracts corrected — test entry is scripts/run_tests.sh (bare pytest forbidden per AGENTS.md/docs/coding-rules.md); evaluator eval-<feature>.sh must call it; hermes_threading.py path fixed in sprint-3 notes.
[2026-11-17T17:12:10Z] tracker: `gh repo set-default chaorenex1/My-H` written to .git/config (gh-resolved); bare gh now resolves to origin, so harness-loop's `gh issue comment`/`gh pr create` target the fork without -R.
[2026-11-17T17:12:20Z] verified: _state.json.foundation_readiness is the full object from Step 3.5 (not the bare "GREEN" string); C:\Users\zarag\bin\jq.exe exists and `jq --version` works under Git Bash.
