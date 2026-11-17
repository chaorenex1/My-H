# Harness Progress Log

Append-only. One event per line: `[<ISO-8601-UTC>] <event>`.
Written by the harness hook and by agents; never rewritten.

[2026-11-17T16:25:00Z] harness-plan: user confirmed epic=agent-title-system scope=P0-P4 rubric=api+custom-script backend=omp
[2026-11-17T16:25:10Z] harness-plan: _config.yml project_type web->api, evaluator_tools->[custom-script, curl]; planner.md rubric preset -> API
[2026-11-17T16:25:20Z] env: WSL bash has no jq/gh; installed jq into ~/.local/bin (WSL) and C:\Users\zarag\bin\jq.exe (Git Bash); run harness scripts with Git Bash on Windows
