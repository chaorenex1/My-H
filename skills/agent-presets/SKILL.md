---
name: agent-presets
description: "Bundled named-agent definitions for the agent tool."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [agents, delegation, review, reconnaissance, security]
    related_skills: []
---

# Agent Presets Skill

This pack ships six named-agent definitions — `init`, `scout`, `sonic`, `reviewer`,
`security-reviewer`, `seance` — in `agents/` beside this file. The `agent` tool discovers them
through its normal `skills/*/agents/` scan, so they work without installation: pass
`skill_name="agent-presets"` to search this pack first. Copy any file into
`<project>/.agents/` or `<HERMES_HOME>/agents/` to override it with your own version.

## When to Use

- A repository has no `AGENTS.md`, or its instructions have drifted from the code; `init` surveys
  the tree and writes the document.
- A task needs a reusable persona with a fixed tool surface, not an anonymous one-off child.
- You want a read-only reviewer that cannot edit the workspace by construction.
- You need a second opinion on a diff with the operator's own model route and effort level.
- You want to consult an earlier conversation that this session no longer holds.

Use `delegate_task` instead for parallel fan-out, structured `output_schema`, or a one-off
goal that needs no durable history.

## Prerequisites

- The `agents` toolset (the `agent` tool itself). `scout`, `sonic` and `reviewer` also need the
  toolsets their definitions request: `file`, `search`, `web`, `terminal`, `session_search`.
- A project or profile whose `.agents/`, `agents/` or `skills/*/agents/` directory does not
  already define the same names; a local file shadows this pack.

## How to Run

Ask for the agent by name; the definition supplies the prompt, toolsets, iteration budget and
effort:

```json
{"agent_name": "scout", "user_prompts": "Where does the nightly build fail?", "background": false}
```

Override per call when needed — a caller argument always beats the definition:

```json
{"agent_name": "reviewer", "user_prompts": "Review HEAD against main.", "model": "provider:model", "background": false}
```

## Quick Reference

| Agent | Toolsets | Read-only | Effort | Use for |
|---|---|---|---|---|
| `init` | `file, search, terminal, delegation` | no | medium | Survey a repo and write `AGENTS.md` |
| `scout` | `file, search, web, session_search` | yes | low | Mapping code, tracing a path, gathering facts |
| `sonic` | `file, terminal, search, web` | no | low | Small mechanical edits and data collection |
| `reviewer` | `file, terminal, search, web, session_search` | yes | high | Defect-focused review; runs existing tests |
| `security-reviewer` | `file, search, web, session_search` | yes | high | Input-to-sink security analysis, no execution |
| `seance` | `file, search, session_search` | yes | medium | Reading a past conversation by session id |

## Procedure

1. To onboard a repository, run `init` inline (`background: false`) with the absolute project path
   in `project_dir`; it fans out four read-only `delegate_task` slices, synthesizes them, and
   writes `AGENTS.md` at the root.
2. Pick the narrowest agent for the job. A question that needs no edits belongs to `scout` or
   `security-reviewer`, never to `sonic`.
3. Run inline (`background: false`) when you need the answer in this turn; the default
   background form delivers a completion whose body is truncated, so the preset prompts lead
   with the result.
4. Pass paths through `context_files` (they resolve inside the project or profile roots) rather
   than pasting large files into `user_prompts`.
5. For a past conversation, give `seance` the session id in `user_prompts`; it resolves the
   record with `session_search`, and it only uses `read_file` on a path you supplied.
6. Ask for `worktree: true` when the agent may edit (that is `sonic`) and you want its changes
   isolated on a `hermes-subagent/...` branch instead of the working tree.

## Pitfalls

- `init` writes `<project>/AGENTS.md`, so leave `worktree` off for it — an isolated worktree would
  strand the document on a branch. It holds `delegation` to fan out its survey; recursion stays
  capped at depth 3.
- `AGENTS.md` is a protected agent-instruction file: each write needs one-operation human approval,
  is not bypassed by auto-approve, and fails closed on an unattended run. `init` therefore defaults
  to inline (`background: false`) and returns the complete document in its final message when the
  write is refused — approve it, then paste. It must not retry through `patch` or `terminal`.
- `read_only` is a tool-surface policy, not a sandbox: `write_file`, `patch`, `agent` and
  `skill_manage` are stripped, but a definition that also grants `terminal` can still mutate the
  workspace. `scout`, `security-reviewer` and `seance` deliberately omit `terminal`; `reviewer`
  keeps it for tests and relies on its prompt.
- A project definition shadows a shipped one with the same name. The call payload reports
  `system_prompt_source` (and `definition_shadowed: true`) — check it when the answer matters.
- `max_iterations` comes from the definition; without it a run stops at the host default of 10
  and a reviewer quits before it finishes.
- Worktree isolation degrades silently outside a git work tree or on a non-local terminal
  backend; the payload's `worktree` block says whether it engaged.

## Verification

- `agent_name="init", background=false, project_dir=<repo>` on a repository without `AGENTS.md`:
  either the file exists with a `# Repository Guidelines` title and the run names its path, or the
  run returns the full document plus the path because approval was unavailable.
- `agent_name="scout", background=false` on a known repository: the answer cites `path:line`
  for every claim and the payload's `system_prompt_source` ends in
  `agent-presets/agents/scout.md`.
- `agent_name="sonic"` with a one-line edit and `worktree: true`: the payload's `worktree` block
  names a branch, and the main checkout is unchanged.
