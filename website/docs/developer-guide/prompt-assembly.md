---
sidebar_position: 5
title: "Prompt Assembly"
description: "How Hermes builds the system prompt, preserves cache stability, and injects ephemeral layers"
---

# Prompt Assembly

Hermes deliberately separates:

- **cached system prompt state**
- **ephemeral API-call-time additions**

This is one of the most important design choices in the project because it affects:

- token usage
- prompt caching effectiveness
- session continuity
- memory correctness

Primary files:

- `agent/prompts/assembler.py` (block order + one join)
- `agent/prompts/renderer.py` (block entry points + data)
- `agent/prompts/system/*.md` (prompt text)
- `agent/system_prompt.py` (facts, cache, persistence)
- `tools/memory_tool.py`

## Cached system prompt layers

`agent/prompts/assembler.py` is the single assembly entry. `assemble_main()` calls the renderer's
block entry points **in code order**, drops empty bodies and joins them once with `\n\n`:

1. `render_rfc2119()` — the RFC 2119 keyword contract (00)
2. `render_role()` — 01 identity, 02 safety, 03 communication, including the platform hint
3. `render_runtime()` — 06 skills, 07 rules, 04 internal URLs, 08 tool inventory
4. `render_autoload_skills()` — pinned `skills.auto_load` bodies (zero bytes when unset)
5. `render_tool_policy()` — 09 (zero bytes without tools)
6. `render_memories()` — 05 (zero bytes without a memory store)
7. `render_closing()` — 10 workflow (coding repos only), 11 delivery, 12 critical
8. `render_project_context()` — the last block: `<project-context>` workstation, context files,
   workspace tree/roots, operator append

There are no tiers, no gate table and no manifest-ordered second pass: the manifest is the template
registry (`scope: system|request` plus budget/truncate metadata). A block decides its own contents
and its own section-level gate (`coding_repo`, tool face, `memory`, `subagent`), and a false gate
means zero bytes — never a placeholder or blank line.

`assemble_subagent(ctx, role)` is the child entry: it returns `assemble_main(ctx)` (with
`subagent=True`, so 10/11 use the hand-off wording) followed by `\n\n` and the
`render_subagent_prompt(role)` block for `system/subagent-system-prompt.md`. The base is
byte-identical for every child sharing one context and the role block is always last; an empty
role body is a build error. `agent_run._subagent_prompts()` renders that block as the run's
`system_message` and registers `_subagent_role`; `build_system_prompt_parts` then puts the shared
base in `stable` and the role block in `context`, so the joined prompt is `assemble_subagent`
bytes and `_cached_system_prompt_static` stays the shared base. A run with no definition body
keeps the bare-prompt contract: no role block is built and `context` is appended to the user turn.

Request-level templates (`scope: request` — `irc_incoming`, `eager_task`, `eager_todo`, the
`subagent_*` reminders) render only through `renderer.render_request(key, context)`. It rejects a
`system` key and the assembler's block sequence never calls it, so those bytes ride a user/tool
row. `subagent_system_prompt` is the one request-scope template that is part of the system
prompt: `agent_run` renders it through the same entry, and `system_prompt` places it in the
`context` tier after the shared base.

`system_prompt.build_system_prompt_parts()` keeps its historical return shape for the cache/restore
contract: `stable` is the assembled body, `context` is the caller's `system_message`, and `volatile`
is empty. `_cached_system_prompt_static` is therefore still a literal prefix of the full prompt.

Two carriers deliberately sit outside the assembler (ADR-19 D5-B): the caller's `system_message`
(context tier) and plugin prompt sections (restored from persisted bytes; `plugin-sections.md` is a
placeholder asset and is not registered). A Bot Chat capability epoch is appended by the facade as an
unwired stamp so `hermes tools enable/disable` still triggers one rebuild per capability change.

Timestamp/session lines no longer live in the system prompt. The runtime host lines
(host / home / **current working directory** / scratch), `Model:`, `Provider:` and `Platform:` are
inside the `<workstation>` block of the final `<project-context>` block.

Consequence for stored prompts: `_stored_prompt_matches_runtime()` reads `Model:` / `Provider:` /
`Current working directory:` from `<workstation>` (`agent/surface_switch.py`), falling back to the
legacy `# Hermes runtime environment` / trailer layout so prompts persisted before this cutover still
validate. `Platform:` is not an identity field: a surface switch keeps the stored bytes and delivers
the current surface's guidance as a one-shot note (`agent/surface_switch.py`). `rewrite_prompt_model_identity()`
rewrites `Model:` / `Provider:` only inside `<workstation>`, so decoys in memory or context prose are
never touched. The `<available_skills>` index is compared every turn by `agent/skills_index_delta.py`
and its difference rides the same note channel.

When `skip_context_files` is set (e.g., subagent delegation), the `<project-context>` block is
omitted; identity comes from the `01-identity.md` template, not from `SOUL.md`.

### Concrete example: assembled system prompt

A simplified view of the assembled body (comments show the source template):

```
# 00 — RFC 2119 contract
MUST, REQUIRED, SHOULD, RECOMMENDED, MAY, OPTIONAL. ...
...
§ Role
Active Hermes profile: default.

You are My-H's trusted coding assistant.
# Engineering
...
# Safety
...
# Communication
...
# Platform
You are in a plain terminal (CLI). ...

§ Runtime
# Skills
<available_skills>
- code-review: Structured code review workflow
- test-driven-development: TDD methodology
</available_skills>

# Internal URLs
...
# Tool Inventory
...

[pinned skills.auto_load bodies, when configured]

§ Tool Policy
# General
...

§ Memories
<memories>
<memories-guidance>...</memories-guidance>
<memories-global>
<memories-user>...</memories-user>
<memories-self>...</memories-self>
<memories-work>...</memories-work>
</memories-global>
</memories>

§ Workflow            (coding repos only)
§ Delivery
§ Critical

<project-context>
<workstation>
- OS: linux 6.8
- Arch: x64
- Host: Linux (6.8)
- User home directory: /home/alice
- Current working directory: /home/alice/code/atlas
- Scratch directory: ~/.hermes/cache/scratch (TMPDIR points here; ...)
- Model: anthropic/claude-sonnet-4.5
- Provider: openrouter
- Platform: cli
</workstation>

<repo-rules>
MUST follow these context files for all tasks:
<file path="AGENTS.md">...</file>
</repo-rules>
...
</project-context>

<critical>
- Each response MUST advance the task; ...
</critical>
```

## Customizing platform hints

The platform hint (inside the `§ Role` block) is the per-surface guidance Hermes
injects for Telegram, WhatsApp, Slack, CLI, and other platforms — for
example "you are on a terminal, avoid Markdown." The built-in defaults
live in `agent/prompts/system/platforms/<platform>.md` (loaded by
`agent/prompts/platforms.py::load_platform_hints`); plugin-provided
platforms supply theirs through the platform registry. The hint is resolved inside
`renderer.render_role()`, which owns the overlay chain (config override, desktop TUI clarifier,
cron delivery hint).

An administrator can append to or replace a single platform's hint from
`config.yaml` via the top-level `platform_hints` key, without touching
any other platform:

```yaml
platform_hints:
  whatsapp:
    append: >
      When tabular output would be useful, invoke the table_formatting
      skill instead of emitting a Markdown table.
  slack:
    replace: "You are on Slack. Keep responses tight and avoid wide tables."
  telegram: "Prefer short messages; split long answers."   # shorthand = append
```

- `append` — keep the built-in hint and add the extra text after it.
- `replace` — substitute the built-in hint entirely.
- A bare string — shorthand for `append`.
- `replace` wins over `append` when both are present.
- A malformed entry is ignored defensively and falls back to the
  unmodified default, so a bad config value can never break prompt
  assembly or leak across platforms.

Cron jobs run as platform `cron`, but their final response lands on the
job's `deliver` channel, so a cron agent's prompt also carries that
channel's hint (built-in text plus its `platform_hints.<channel>`
override) under a `Delivery destination (<channel>):` line. A
`platform_hints.slack.append` therefore reaches Slack-delivered scheduled
jobs as well as live Slack chats; `platform_hints.cron` still governs the
cron paragraph itself.

The override is resolved when the system prompt is built (session start,
and again on compaction since that rebuilds the prompt). It produces a
byte-stable hint for a fixed config, so it lives in the **stable** tier
alongside the built-in hint and does not break prompt caching — it is
not a live mid-session mutation of a frozen prompt.

## Identity: the 01 template, not SOUL.md

`agent/prompts/system/01-identity.md` is the identity block. It renders the profile name
(`Active Hermes profile: <name>.`), the role description and the engineering section, with
`role` / `role_desc` / `role_section` as optional overrides from an agent definition.

`SOUL.md` is no longer injected into the system prompt (ADR-19 D5-A): the old
`DEFAULT_AGENT_IDENTITY` fallback and the SOUL loader's prompt slot were deleted. `load_soul_md()`
still exists for callers that read the file, but prompt assembly does not consume it. Configuring an
agent persona goes through the agent definition's `role` fields.

## How context files are injected

`render_project_context()` (`agent/prompts/project_context.py`) renders the
`<project-context>` tail block from `agent/prompts/system/project-prompt.md`. It uses a
**priority system** — only one project context type is loaded (first match wins):

```python
# From agent/prompts/project_context.py (simplified)
def select_context_files(cwd_path):
    by_kind = {
        kind: [ContextFile(kind, label, path, content) ...]
        for kind, label, path, content in discover_context_files(cwd_path)
        if content
    }
    for kind in CONTEXT_FILE_PRIORITY:      # .hermes.md → AGENTS.md → CLAUDE.md → .cursorrules
        if by_kind.get(kind):
            return dedupe_contained_context_files(by_kind[kind])
    return []

def render_project_context(...):
    files = select_context_files(cwd_path)
    rows = [format_context_file(file) for file in files]   # <file path="...">, scanned + capped
    return render_section("project_context", {
        "environment": workstation_environment(),           # OS / Arch rows
        "model": model_label(provider, model),              # provider/model
        "context_files": rows,
        ...
    })
```

Deduplication follows the omp rules (`docs/omp-prompt-system.md` §5): every directory depth
keeps its own AGENTS.md, files inject far ancestor → near ancestor, and a file whose
normalized paragraph sequence is fully contained in a nearer file is dropped. Fenced code
blocks are ignored when comparing, so two files that differ only in examples are not
considered identical.

### Context file discovery details

| Priority | Files | Search scope | Notes |
|----------|-------|-------------|-------|
| 1 | `.hermes.md`, `HERMES.md` | CWD up to git root | Hermes-native project config |
| 2 | `AGENTS.md` | CWD only | Common agent instruction file |
| 3 | `CLAUDE.md` | CWD only | Claude Code compatibility |
| 4 | `.cursorrules`, `.cursor/rules/*.mdc` | CWD only | Cursor compatibility |

All context files are:
- **Security scanned** — checked for prompt injection patterns (invisible unicode, "ignore previous instructions", credential exfiltration attempts). A hit replaces a project file with a `[BLOCKED: …]` marker; the user's own `SOUL.md` in `HERMES_HOME` is warned about and loaded anyway (it is human-approved on write, so it is the same trust class as `config.yaml`)
- **Truncated** — capped at `context_file_max_chars` characters using a 70/20 head/tail split with a truncation marker. The cap scales with the model's context window (20,000-char floor, 500K ceiling); an explicit `context_file_max_chars` in `config.yaml` always wins.
- **YAML frontmatter stripped** — `.hermes.md` frontmatter is removed (reserved for future config overrides)

## API-call-time-only layers

These are intentionally *not* persisted as part of the cached system prompt:

- `ephemeral_system_prompt`
- prefill messages
- gateway-derived session context overlays
- later-turn Honcho/external recall injected into the current-turn user message

`pre_llm_call` plugin context also lands in this API-call-time path: it is appended to the current turn's **user message**, not written into the cached system prompt. When multiple plugins return context, Hermes concatenates those context blocks (see [Hooks → `pre_llm_call`](../user-guide/features/hooks.md#pre_llm_call)).

This separation keeps the stable prefix stable for caching.

## Memory snapshots

Local memory and user profile data are the `§ Memories` block (`05-memories.md`), rendered by
`renderer.render_memories()` from `<home>/memories/USER.md`, the per-agent `AI_SELF.md` and the
agent's `MEMORY.md` (the main agent falls back to `<home>/memories/MEMORY.md`, where the built-in
store writes it). The block is part of the assembled body and is rebuilt only through
`invalidate_system_prompt()` (new session or compression); mid-session writes update disk state but
do not mutate the already-built cached prompt. `memory_enabled` gates the whole block and
`user_profile_enabled` hides only `<memories-user>`.

## Context files

`agent/prompt_builder.py` (as a data helper) scans and sanitizes project context files using a **priority system** — only one type is loaded (first match wins):

1. `.hermes.md` / `HERMES.md` (walks to git root)
2. `AGENTS.md` (CWD at startup; subdirectories discovered progressively during the session via `agent/subdirectory_hints.py`)
3. `CLAUDE.md` (CWD only)
4. `.cursorrules` / `.cursor/rules/*.mdc` (CWD only)

The selected files are rendered into `<repo-rules>` inside `<project-context>` by
`renderer.render_project_context()`, which is the assembler's final block. `SOUL.md` is not part of
this path (and is not injected at all, see above).

Long files are truncated before injection.

## Skills index

The skills system contributes `<available_skills>` inside `§ Runtime` (`06-skills.md`) when skills
tooling is available. Rows come from the session's own `<home>/skills` tree through
`agent/prompts/skills.py::skill_rows()`, which applies the offer-time gates (disabled names,
platform/environment/app requirements, `requires_*`/`fallback_for_*` conditions); an empty tree falls
back to the bundled catalog.

## Supported prompt customization surfaces

Most users should treat `agent/prompts/` as implementation code, not a configuration surface. The supported customization path is to change the prompt inputs Hermes already loads, rather than editing Python templates in place.

### Use these surfaces first

- An agent definition's `role` / `role_desc` / `role_section` front matter — override the identity template's defaults (`01-identity.md`). `~/.hermes/SOUL.md` is no longer injected into the system prompt.
- `~/.hermes/memories/MEMORY.md` and `~/.hermes/memories/USER.md` — provide durable cross-session facts and user profile data that should be snapshotted into new sessions.
- Project context files such as `.hermes.md`, `HERMES.md`, `AGENTS.md`, `CLAUDE.md`, or `.cursorrules` — inject repo-specific working rules.
- Skills — package reusable workflows and references without editing core prompt code.
- Optional system prompt config / API overrides — add deployment-specific instruction text without forking Hermes.
- Ephemeral overlays such as `HERMES_EPHEMERAL_SYSTEM_PROMPT` or prefill messages — add turn-scoped guidance that should not become part of the cached prompt prefix.

### When to edit code instead

Edit `agent/prompts/assembler.py` / `agent/prompts/renderer.py` only if you are intentionally maintaining a fork or contributing upstream behavior changes. Those files own block order and block contents for every session. Direct edits there are global product changes, not per-user prompt customization.

In other words:

- if you want a different assistant identity, set the agent definition's `role` fields
- if you want different repo rules, edit project context files
- if you want reusable operating procedures, add or modify skills
- if you want to change how Hermes assembles prompts for everyone, change Python and treat it as a code contribution

## Reviewing changes to the prompt surface

The **prompt surface** is what a stock install sends before the first user message: the system prompt and the `tools[]` array. A one-line code change can rewrite either without the diff showing it. A guidance constant can move tiers, a toolset can gain a tool, or a schema rewriter can edit a description. So the rendered surface is committed and reviewed like code:

- `scripts/ci/prompt_surface.py render` builds the surface through the production assembly code (`build_system_prompt_parts` and the agent's own `tools[]`) and writes it to `tests/fixtures/prompt_surface/`. It renders nine surfaces: `cli` with four model families (the model name gates guidance and the patch dialect), `cli` inside a code workspace, `desktop`, `telegram`, `discord` and `cron`. The render uses a fresh home. The clock, host OS, network, tool availability and machine paths are pinned, so a laptop and CI produce the same bytes. Every registered tool counts as available, so credential-gated schemas get reviewed too.
- `tests/ci/test_prompt_surface.py` fails when the committed snapshot is stale. A PR that changes the surface carries the regenerated snapshot, and reviewers read the real prompt text and schemas in the Files tab.
- The **Prompt surface diff** CI job posts a per-surface summary to the PR review comment: token deltas, prompt hunks, and added or removed tools and parameters.
- Changes under `tests/fixtures/prompt_surface/` need **an approval from hermes-agent-core** before they can merge.

Regenerate after any change that moves the surface:

```bash
scripts/run-in-hermes-env python scripts/ci/prompt_surface.py render
```

User-side customization (agent `role` fields, context files, skills you install, `platform_hints`, `agent.*` guidance toggles) never touches the snapshot. The render uses stock defaults and only bundled skills.

## Why prompt assembly is split this way

The architecture is intentionally optimized to:

- preserve provider-side prompt caching
- avoid mutating history unnecessarily
- keep memory semantics understandable
- let gateway/ACP/CLI add context without poisoning persistent prompt state

## Related docs

- [Context Compression & Prompt Caching](./context-compression-and-caching.md)
- [Session Storage](./session-storage.md)
- [Gateway Internals](./gateway-internals.md)
