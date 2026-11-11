# omp bundled task agents

Complete content of the agent definitions omp embeds at build time and exposes to the `task`
tool. These are the prompts a spawned subagent actually runs, not summaries.

Hermes does not scan this directory. `scripts/build_bundled_agents.py` translates the snapshots
into `skills/agent-presets/agents/` (omp `tools`/`thinking-level` → `enable_tools`/`reasoning_effort`,
prose tool names → Hermes names); that converted skill is what the `agent` tool discovers. Run the
script after refreshing a snapshot; `--check` fails on stale output.

Source: `packages/coding-agent/src/prompts/agents/` in the vendored oh-my-pi tree
(`C:/Users/zarag/projects/hermes-omp-ui/vendor/oh-my-pi`), consumed by
`packages/coding-agent/src/task/agents.ts` (`EMBEDDED_AGENT_DEFS`).

Snapshot: oh-my-pi `18.12.6`, commit `45c35e4` (2026-10-21), extracted 2026-10-31.
The running harness reports `18.14.0`. Checked against two child transcripts, the agent block is
`§ Role\n` + body + per-spawn `§ Context…`; the body prefix is byte-identical to `scout.md`
(1718 chars) and `task.md` (821 chars) for those two spawns. Only `scout` and `task` were observed —
`reviewer`, `security-reviewer`, and `seance` were not exercised, so their runtime prompts are
unverified against this snapshot.

## Assembly

`buildAgentContent()` renders each definition as:

1. `prompts/agents/frontmatter.md` — a YAML block built from the definition's fields
   (`name`, `description`, optional `spawns`, `model`, `thinking-level`, `blocking`, `prewalk`,
   `advisor`, `autoloadSkills`);
2. the template body.

`parseAgent()` then parses that markdown: frontmatter becomes the agent's fields, and the
remaining body becomes `systemPrompt`. Per-spawn content — the assignment block, `§ Context`,
`§ Coop`/peer list, plan-mode preamble — is appended to the child session later and is **not**
part of these files.

`task` and `sonic` share one body (`task.md`) and differ only in injected frontmatter. The files
here are the rendered result; their body text is byte-identical.

## Agents

| Agent | File | Tools | Model | Thinking | Spawns | Notes |
|---|---|---|---|---|---|---|
| `scout` | `scout.md` (verbatim) | `read, find, grep, glob, web_search` | `@smol` | `medium` | — | `read-summarize: false`; structured output: `summary`, `files`, `architecture`, optional `report` |
| `reviewer` | `reviewer.md` (verbatim) | `read, find, grep, glob, bash, lsp, web_search, ast_grep` | `@slow` | model default | `scout` | structured verdict output |
| `security-reviewer` | `security-reviewer.md` (verbatim) | `read, find, grep, glob, lsp, ast_grep` | model default | model default | — | structured findings output |
| `seance` | `seance.md` (verbatim) | `read, grep, glob` | model default | model default | — | `read-summarize: false`; historical-session consult |
| `task` | `task.md` (rendered) | default built-in toolset | `@task` | `auto` | `*` | general-purpose worker |
| `sonic` | `sonic.md` (rendered) | default built-in toolset | `@smol` | `medium` | not declared | mechanical updates / data collection; same body as `task`; `spawns` derives only if the default toolset includes `task`, which the discovery doc does not state |

An omitted `tools` field selects the default built-in toolset, not "no tools".

## Invocation and precedence

Dispatch is `task` with `tasks[].agent`; an omitted `agent` defaults to `task` (or the first
allowed name under a restricted parent `spawns`). Bundled definitions sit last in discovery:

1. project `.omp/agents/`
2. user `~/.omp/agents/`
3. extension-package `agents/` roots, in configured order
4. Claude marketplace plugin roots (when `claude-plugins` is enabled)
5. bundled (`loadBundledAgents()`)

Dedup is exact-name, first-wins, so any discovered agent may shadow a bundled one.

## Not included here

Subagent sessions may additionally receive: the `§ Role` header that frames the agent body, the
`/skill:`-style autoloaded skills, the plan-mode preamble (tools narrowed to `read`, `grep`, `glob`,
`web_search`), the `yield` tool in structured mode, and the coordinator's
`§ Context`/`§ Coop` assignment and peer blocks. Those are per-spawn inputs, not agent definitions.

## Refresh

```bash
cp "C:/Users/zarag/projects/hermes-omp-ui/vendor/oh-my-pi/packages/coding-agent/src/prompts/agents/"{scout,reviewer,security-reviewer,seance}.md docs/bundled-agents/
```

For `task`/`sonic`, re-render from `task.md` plus the frontmatter in `src/task/agents.ts`
(`sonic` and `task` entries) — do not copy the raw body, which has no frontmatter.
