---
title: "Porting omp Bundled Agents to Hermes"
description: "Design for shipping omp's bundled agent personas as Hermes named agents"
---

# Porting omp Bundled Agents to Hermes

## Decision

Ship the bundled omp personas as **Hermes named agents** (`agent` tool), not as new
`delegate_task` roles and not as a plugin runtime.

- `scout`, `sonic`, `reviewer`, `security-reviewer`, `seance` become definitions under the
  provisioned bundled-skills tree: `skills/agent-presets/agents/*.md`, documented by
  `skills/agent-presets/SKILL.md`.
- `task` is ported as a **mapping**, not a definition: Hermes `delegate_task` already is the
  generic, anonymous, batch-capable subagent. Adding a second generic surface would duplicate the
  narrow waist.
- `seance` is ported by **composition**: a read-only named agent that locates and reads past
  conversations with `session_search` and `read_file`. It does not fork context or scope source
  artifacts; that fidelity gap is explicit in its prompt and docs.

The existing `agent` tool is the right seam because it already provides named definitions,
per-agent JSONL history, background delivery, model/toolset overrides and a recursion cap.

## Source inventory

omp marks these definitions as `source: "bundled"`:

| omp agent | Role | Port? |
|---|---|---|
| `task` | Generic default worker, may spawn | Yes — mapped to `delegate_task` |
| `scout` | Fast read-only reconnaissance | Yes |
| `sonic` | Low-reasoning mechanical work/data collection | Yes |
| `reviewer` | Code review | Yes |
| `security-reviewer` | Read-only security review | Yes |
| `seance` | Read-only consult of a historical session | Yes — composed from `session_search` + `read_file` |

omp's local `explorer`, `researcher` and `worker` are user-level definitions, not bundled; they
are out of scope. Users can still drop equivalents into `~/.hermes/agents/`.

## Why `agent`, not `delegate_task`

| Need | `agent` | `delegate_task` |
|---|---|---|
| Stable persona name | Definition file on disk | Ad-hoc prompt per call |
| Durable per-persona history | `sessions/<agent>/<session>.jsonl` | Isolated child per call |
| Definition-owned tools/model | Yes, after this design | Route per task via delegation backend |
| Anonymous fan-out | Awkward | Native batch `tasks[]` |
| Worktree isolation | Yes — `worktree: true` at call time or in the definition | Yes (`delegation` worktree config) |

Use `agent` for a named reviewer/scout persona. Use `delegate_task` for one-off parallel work or
structured `output_schema`. Both isolate worktrees now: `agent` builds one off the caller's
project on request and reuses `tools/subagent_worktree.py` (inspect, prune, archive); the
read-only definitions still omit the `agents` toolset, so no persona can fan out from inside its
tree.

## Agent definition contract

Today `tools/agent_tool.py` only stripped a leading YAML block and discarded it. The shipped
contract lives in `tools/agent_definitions.py` (parsing, validation, merge) and is enforced by
`agent_tool._validate` before any run or background dispatch:

```markdown
---
name: scout
description: "Read-only codebase reconnaissance and fact gathering."
toolsets: [file, search, web, session_search]
read_only: true
max_iterations: 100
background: true
skip_memory: true
skip_context_files: false
skip_background_review: true
load_soul_identity: false
model: ""                 # optional, "provider:model" or bare model
reasoning_effort: low     # optional, a Hermes effort level
worktree: false           # optional, isolate edits in a git worktree
---

You are a reconnaissance specialist. ...
```

Rules:

- Reuse `agent.skill_utils.parse_frontmatter`; do not add a second YAML parser.
- Unknown keys are ignored with one warning (forward compatibility).
- Field types are validated. A malformed `toolsets`, `model`, `reasoning_effort` or
  `max_iterations` is a tool error, never a silent fallback.
- `toolsets` must contain only names accepted by `toolsets.validate_toolset`. An unknown name
  fails the run; it must **never** degrade to "all tools".
- `read_only` is enforced by `agent.agent_run.apply_read_only`, which drops
  `write_file`, `patch`, `agent` and `skill_manage`. It is **not a sandbox**: `terminal` can still
  mutate the workspace.
- The body becomes the system prompt exactly as today; the frontmatter is never sent to the model.

### Precedence

Per call, highest first:

1. caller argument (`model`, `tool_sets`, `background`, skip flags)
2. definition frontmatter
3. host default

`read_only`, `max_iterations` and `reasoning_effort` are **definition-owned** in v1 and are not
added to the model-facing `agent` schema. This keeps the tool schema, and therefore the cached
prompt prefix, unchanged.

### Untrusted definitions and shadowing

`_prompt_scopes` is project-first and tags each directory with its trust scope, so a cloned
repository's `skills/agent-presets/agents/security-reviewer.md` silently shadows the shipped
definition and becomes the system prompt. Fixed, predictable names make that a worthwhile target.
Before this change, definition bodies were loaded without the injection scan that context files
receive (`agent/prompt_builder.py::_scan_context_content`).

The same trust model now applies to definition bodies, after frontmatter removal:

- `scan_for_threats(body, scope="context")` (or the builder's `_scan_for_threats` seam).
- **Project** definition with findings: block that file, log the path and findings, and continue
  to the next candidate.
- **Profile** and **installation** definitions with findings: warn and load, matching the
  user-authored `SOUL.md` precedent. Profiles are operator-owned; installation definitions are
  shipped and reviewed.
- If the requested name matched only blocked project files and no later candidate exists, return
  `tool_error` naming the blocked path and findings, unless the caller supplied `system_prompts`.
  Do not silently run a generic host prompt in place of the requested persona.
- When a project or profile file shadows a bundled name, log a warning. The inline payload
  already carries `system_prompt_source`; add the same field to the background completion event
  and a `definition_shadowed: true` marker.

This is pattern-based mitigation, not a sandbox. Project-first precedence stays, but it is now
explicit in the docs and visible in logs/payloads.

## Core changes

### `tools/agent_tool.py`

1. Replace `_front_matter(text) -> str` with `tools/agent_definitions.parse_definition`, which
   returns `(metadata, body)` from `parse_frontmatter` and validates every field.
2. `_system_prompt` returns a `_Definition` (body, source, metadata) and scans each candidate body
   with `scan_for_threats(..., scope="context")` before accepting it, following the trust rules
   above.
3. Make the caller-parsing helpers tri-state:
   - `_split_model`: empty when omitted, so the definition can supply `model`.
   - `_tool_sets`: `None` when omitted, so the definition can supply `toolsets`; `["*"]` still
     means every tool.
   - `_flag`: default `None` for keys the definition may own.
4. Validate metadata once in `_validate`, before any run or background dispatch.
5. Pass `max_iterations` and `read_only` into `_params` and `agent_run.run` (both already exist
   on the runner).
6. Add `reasoning_effort` to `agent_run.run`:

```python
if reasoning_effort:
    parsed = parse_reasoning_effort(reasoning_effort)
    if parsed is None:
        raise ValueError(f"unknown reasoning_effort: {reasoning_effort!r}")
    knobs["reasoning_config"] = parsed
```

`agent_run._agent_kwargs` currently always resolves the host reasoning config. The explicit
level must be applied after that resolution, so a definition's level wins over
`config.yaml`, while a caller-pinned model still resolves its own route and credentials.

7. Keep the `agent` tool schema fixed for the definition-owned knobs. `read_only`,
   `max_iterations` and `reasoning_effort` stay out of the model-facing schema; the model cannot
   silently weaken `read_only` or raise `max_iterations`. The one additive argument is
   `worktree` (below), which a caller may request but cannot use to weaken a definition.

### Worktree isolation (`agent`)

`worktree: true` in a definition, or `worktree: true` at call time (caller wins), runs the agent
in its own checkout:

- `agent_tool._params` forwards `worktree` to `agent_run.run`.
- `run` asks `tools/subagent_worktree.create_subagent_worktree(<project_dir>, subagent_id=
  "agent-<name>-<hex>")`, which returns `None` outside git, on a non-local terminal backend, or on
  failure — a silent downgrade, reported in the payload rather than failing the run.
- On success the worktree path becomes the agent's `cwd` **and** the run's task cwd record
  (`tools.terminal_tool.record_session_cwd(task_id, path)`), because terminal and file tools read
  the task record, not the agent object. `run_conversation(..., task_id=<run_id>)` makes that
  record reachable; the record is cleared in `finally` so it cannot leak across runs.
- The worktree contract note (`build_worktree_context_note`) is appended to the context block, so
  the run knows its branch and that the parent will review it.
- After the run (and on failure, from `finally`) `finalize_subagent_worktree` inspects the tree:
  zero commits and clean → removed; work left → archived to `refs/hermes/archive/<tree>` and
  removed with the branch kept. The result lands in `AgentRunResult.worktree` and therefore in
  the tool payload and the background completion event.


### Discovery and precedence

No discovery code change is needed. `_prompt_dirs` already scans project, profile and
installation roots in that order, and within each root it scans `skills/*/agents/` as well as
`.agents/`/`agents/`. Keep the existing first-wins order:

1. project `.agents/`, `agents/`, `skills/*/agents/`
2. profile `~/.hermes/.agents/`, `~/.hermes/agents/`, `~/.hermes/skills/*/agents/`
3. installation `agents/`, bundled `skills/*/agents/`

The exact bundled skills path matters: `_skill_agent_dirs` scans **one level** under the skills
root, i.e. `skills/<pack>/agents/`, not `skills/<category>/<skill>/agents/`. The shipped layout is
therefore:

```text
skills/agent-presets/
├── SKILL.md                 # documents the pack; makes it a first-class bundled skill
└── agents/
    ├── init.md
    ├── scout.md
    ├── sonic.md
    ├── reviewer.md
    ├── security-reviewer.md
    └── seance.md
```

A caller may pass `skill_name="agent-presets"` to search that pack first; without it, all
`skills/*/agents/` directories are still scanned in sorted order. Add a test that resolves the
pack through `_prompt_dirs`, because the long-form docs' `<skill_name>` shorthand does not spell
out the one-level scan.

### Packaging

Do **not** rely on a repo-root `agents/` directory. Root non-Python assets are not part of the
distributed wheel story, and the Nix wrapper provisions only `skills`, `optional-skills`,
`plugins`, `locales` and `optional-mcps` (`nix/hermes-agent.nix` `resources`). Root placement
would work in a source checkout and the Docker `COPY . .` image, but not reliably in the sealed
Nix runtime.

`skills/` is already the provisioned bundled-asset root (`HERMES_BUNDLED_SKILLS`, Docker skills
sync, Nix `bundledSkills`). Shipping the pack there gives one path for checkout, Docker and Nix.
The packaging test asserts `_install_roots()[1]/agent-presets/agents` resolves with the six
files. No Python package-data entry is required: these are runtime content, not package modules.

## Prompt migration rules

The prompt body is the part that needs real adaptation. Port **behavior**, not omp wire syntax:
every omp-only primitive gets a Hermes tool expression, and a capability Hermes lacks is composed
from tools rather than deleted.

### Runtime primitive mapping

| omp construct | Hermes expression |
|---|---|
| `yield` | Named agents have no hidden completion tool. The final assistant message is the handoff; state that contract in the body. When the source declares a structured `output`, route the call through `delegate_task` with `output_schema` instead of inventing a `yield` call. |
| `agent://<id>` | Named-agent output: `read_file` on its `<HERMES_HOME>/sessions/<agent>/<session>.jsonl`. `delegate_task` output arrives in the completion, not a URI. If only a session id is known, use `session_search(session_id=...)`. A follow-up goes to `agent(agent_name=<same>, user_prompts=...)` in the same parent session; there is no peer-messaging URI. |
| `history://<id>` | Main conversations live in SessionDB: `session_search(session_id=...)`, optionally with `around_message_id`/`window` to scroll. Named `agent` runs have a JSONL transcript; read that with `read_file` only when the path is known. |
| `proc://<id>` | `process_manage(action="poll"\|"log"\|"wait"\|"kill", session_id=...)`; start work with `terminal(background=true, notify_on_complete=true)`. A delegation is not a process: turn interrupts only reach foreground children, and only `/stop` ends background delegations. |
| `local://<name>` | A project-relative path written with `write_file` and read with `read_file`; hand it to a child through `context_files` (or a path in `context`). |
| `artifact://<id>` | Hermes has no artifact URI. Resolve the concrete file and `read_file` it; for session-scoped content use `session_search(session_id=...)`. Do not assume `sessions/<agent>/<session>.jsonl` is the universal path. |
| `task` batch JSON | `delegate_task(tasks=[{goal, context, group?}])`; `solutionSpace` has no field, so fold it into `goal`/`context` text. |
| plan mode | A read-only definition (`read_only: true` plus read-only `toolsets`) and a two-phase prompt: investigate first, report before acting. Enforce with the tool surface, not prose. |
| spawn controls (`spawns`, depth) | Grant the `agents` toolset for `agent(agent_name=...)` or the `delegation` toolset for `delegate_task`; recursion is capped at `MAX_DEPTH=3`. Rewrite omp's `spawns` frontmatter into the granted toolset, not a new frontmatter key. |

Additional rules:

- Tool names: `read` → `read_file`, `write` → `write_file`, `edit` → `patch`,
  `grep`/`glob`/`ls`/`find` → `search_files`, `bash` → `terminal`, `todo_write` → `todo_list`,
  `web_search` stays. `ast_grep` has no direct Hermes tool: express the step as `search_files`
  for locating plus `patch` for the edit, and document that exact AST semantics need an
  extension rather than leaving the omp tool name in prose. `sonic` must not retain omp
  mutation names (`write`, `edit`, `todo_write`).
- Never leave an omp URI or pseudo-tool in a definition. Replace it with the concrete tool call
  (`read_file`, `write_file`, `search_files`, `session_search`, `process_manage`, `delegate_task`).
- State the output contract in the body: concise findings first, evidence as `path:line`,
  explicit uncertainty, no process replay. Background delivery truncates the notification at
  2000 characters and points at the transcript, so lead with the result.
- Read-only definitions must not claim they can edit or run tests. The prompt and the toolset must
  agree.
- Where Hermes has no exact primitive, port the observable behavior by composition and document
  the fidelity gap; do not silently drop the instruction. `seance` is the worked example:
  `session_search` + `read_file` replace transcript forking and artifact scoping.

## Per-agent specifications

| File | Toolsets | `read_only` | `max_iterations` | Spawning | Notes |
|---|---|---|---:|---|---|
| `skills/agent-presets/agents/init.md` | `file, search, terminal, delegation` | no | 200 | yes (`delegation`) | Surveys the repository, fans out four read-only `delegate_task` slices, and writes `AGENTS.md` at the project root. |
| `skills/agent-presets/agents/scout.md` | `file, search, web, session_search` | yes | 100 | no | No terminal. `read_only` removes the mutating file tools; the remaining surface is read/search. |
| `skills/agent-presets/agents/sonic.md` | `file, terminal, search, web` | no | 100 | no | Mechanical edits are allowed; no `agents` toolset, so it cannot fan out. |
| `skills/agent-presets/agents/reviewer.md` | `file, terminal, search, web, session_search` | yes | 150 | no | `terminal` lets it run tests; `read_only` blocks `write_file`/`patch`, but terminal is trusted to follow the no-edit instruction. |
| `skills/agent-presets/agents/security-reviewer.md` | `file, search, web, session_search` | yes | 150 | no | No terminal: findings are static-analysis evidence, never executed exploit code. |
| `skills/agent-presets/agents/seance.md` | `file, search, session_search` | yes | 100 | no | Emulates transcript consultation with `session_search` + `read_file`; no context fork or source-artifact scope. |

`init` is ported from omp's `agents/init.md` command prompt rather than its bundled agent set: it
ships so a fresh repository can bootstrap its own `AGENTS.md`. It holds `delegation` for the
four-slice survey, writes only `AGENTS.md`, and deliberately leaves `worktree` off so the document
lands in the project instead of a branch.

`seance` has no `sourceSession` argument. The caller supplies the source session id (or an
`@session:<profile>/<id>` link) in `user_prompts` or `context`; the definition then calls
`session_search(session_id=..., profile=...)` to read it. Use `read_file` only when the caller
also supplies a concrete JSONL path; never construct `sessions/<agent>/<session>.jsonl` from an
id and assume it exists.

Suggested model policy: leave `model` empty in the shipped files so each profile uses its own
route. Operators can pin a cheaper model for `sonic` or a stronger one for `security-reviewer` by
editing the definition or passing `model` at call time. Do not hardcode a vendor model.

Suggested effort: `scout: low`, `sonic: low`, `reviewer: high`, `security-reviewer: high`,
`seance: medium`. `reasoning_effort` is optional; when absent the host config applies.

## Delivery, limits and concurrency

- Named agents default to `background: true`; results re-enter as an async completion. Callers
  that need the answer in the same turn pass `background: false`.
- The shared `agent_loop` pool bounds concurrency (`agent_loop.max_workers`, default 8;
  `max_queued`, default 64; `max_run_seconds`, default 1800). This port does not add a per-agent
  semaphore. omp's `task.maxConcurrency: 32` is not reproduced; Hermes' global pool is the limit.
- `max_iterations` in the definition replaces `agent_run.MAX_ITERATIONS` (10). Without it, a
  reviewer would stop far too early.
- Recursion is capped by `agent_tool.MAX_DEPTH = 3`. Read-only definitions omit the `agents`
  toolset, so recursion is impossible for them regardless.

## Security boundaries

- `read_only` is a tool-surface policy, not a sandbox. Treat any definition that includes
  `terminal` as able to mutate the workspace. `security-reviewer` and `scout` therefore exclude
  `terminal`.
- `reviewer` includes `terminal` only to run tests; its prompt forbids edits and its file-mutating
  tools are stripped. If a stricter guarantee is required, remove `terminal` and accept static
  review only.
- Definitions run with the caller's profile scope and credentials. They must not read secrets or
  write outside the project; the existing `context_files` root checks remain in force.
- Invalid `toolsets` fail closed. An unknown name must not fall back to all tools.
- A project definition can shadow a shipped persona. Scan its body with `scope="context"` and
  block on findings; log any shadowing of a bundled name and keep `system_prompt_source` visible.
  The scan is pattern-based, so a clean malicious definition can still change behavior. The
  shipped names are conveniences, not a trust boundary: operators who need a pinned persona must
  use a path or config they control, and callers should inspect the reported source when the
  result matters.

## Testing

Add to `tests/tools/`:

1. **Definition parsing and precedence**
   - Frontmatter is stripped from the prompt and applied to the run.
   - Caller `tool_sets`, `model` and `background` override definition values.
   - Omitted caller values inherit the definition; absent definition values keep today's defaults.
   - Invalid `toolsets`/`model`/`reasoning_effort` return `tool_error` and construct no agent.
2. **Behavior through the registry**
   Use the existing `_FakeAgent` fixture and `registry.dispatch("agent", ...)`.
   Assert `system_message`, `enabled_toolsets`, `max_iterations`, `read_only` and skip flags reach
   `agent_run.run`. Do not assert prompt prose.
3. **Definition trust**
   - A project definition containing a context-scope threat pattern is blocked and the next
     candidate wins; if none exists, the call returns `tool_error` naming the blocked file.
   - A profile definition with a finding warns but loads.
   - Shadowing a bundled name logs a warning and reports the winning
     `system_prompt_source`.
4. **Bundled definition validation**
   Discover `_install_roots()[1]/agent-presets/agents/*.md`; for each, parse the frontmatter
   and assert: prompt body non-empty, every toolset validates, `read_only` definitions do not
   include the `agents` toolset, and `max_iterations` is a positive integer.
5. **Packaging**
   Assert `_install_roots()[1]/agent-presets/agents` exposes the six files, and that
   `_prompt_dirs` finds the pack through the one-level `skills/*/agents/` scan.

Run:

```bash
scripts/run_tests.sh tests/tools/test_agent_tool.py tests/tools/test_agent_definitions.py -q
python scripts/check
```

Manual smoke (temp home, one cheap run):

```bash
HERMES_HOME="$(mktemp -d)" hermes -q \
  "Call the agent tool with agent_name=scout, background=false, and ask it to map the files in this repo."
```

## Rollout

1. Add the definition parser, validation and context-scope body scan; keep existing behavior for
   definitions without frontmatter. Land with tests.
2. Add `skills/agent-presets/SKILL.md` (meeting the `skills/AGENTS.md` authoring standards) and
   ship `scout` and `security-reviewer` under `skills/agent-presets/agents/` (no terminal,
   lowest risk). Smoke both.
3. Ship `reviewer` and `sonic`.
4. Add operator docs to `website/docs/user-guide/features/agent-tool.md`: frontmatter fields,
   precedence, shadowing, and the `read_only`/terminal caveat.
5. Ship `seance` with its composed `session_search` + `read_file` workflow and an explicit note
   that transcript forking and source-artifact scoping are not reproduced.

## Non-goals

- No new delegation backend or `task` role.
- No new hidden `yield` tool. The explicit-completion contract is the final assistant message;
  structured payloads use `delegate_task` with `output_schema`.
- No multi-model fallback list in a definition; Hermes host fallback still applies.
- Worktree isolation reuses `tools/subagent_worktree.py`; there is no second worktree
  implementation and no per-agent concurrency of its own.
- No model-facing frontmatter controls; definitions are operator-owned.
- No new transcript-fork primitive; `seance` composes `session_search` + `read_file` and documents
  the fidelity gap.

## Risks

| Risk | Mitigation |
|---|---|
| Prompt still names omp tools | Rewrite every prompt against Hermes tool names; review each definition manually. |
| `read_only` mistaken for a sandbox | Document the terminal caveat; exclude terminal from scout/security-reviewer. |
| Reviewer output truncated in notifications | Lead with findings; use `background: false` when full text is needed; transcript path is always available. |
| Invalid toolset silently broadens access | Fail closed in `_validate`; add a regression test. |
| A cloned repo shadows `security-reviewer` or `reviewer` | Scan definition bodies with `scope="context"`, block project findings, warn on shadowing, expose the winning source. Fixed names remain a convenience, not a trust boundary. |
| Definitions miss the sealed runtime | Ship under `skills/agent-presets/` (provisioned by Nix/Docker/checkout), not repo-root `agents/`; assert the path in packaging tests. |
| `seance` cannot reproduce transcript forking or source-artifact scope | Document the gap in the definition and operator docs; require the caller to supply the session/transcript path through `context_files` when exact history matters. |
| Definition metadata changes prompt cache | Schema stays unchanged; only call-time behavior changes. |
| Global pool contention | Document `agent_loop.max_workers`; no per-agent concurrency in v1. |
