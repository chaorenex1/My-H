# Named Agents (`agent`)

`agent` runs one **named agent** — a peer conversation with its own system prompt, tool
surface and on-disk history — inside the host process. Use it when a task wants a durable,
reusable persona (`researcher`, `reviewer`, `writer`) rather than a fresh anonymous subagent.

Compared with [`delegate_task`](./delegation.md):

| | `agent` | `delegate_task` |
|---|---|---|
| Identity | a named agent definition on disk | an ad-hoc child per call |
| History | one JSONL per (agent, session), read back on the next run | isolated per child |
| Delivery | completion notification (background by default) | completion notification / blocking |
| Default toolsets | every host tool | the parent's toolsets, minus child-blocked ones |
| Recursion | `agent` may call itself up to depth 3 | `delegate_task` per `delegation.*` |
| Worktree isolation | opt-in per call (`worktree: true`) or in the definition | opt-in (`delegation.worktree_isolation`) |

Both are in the default toolset bundle. `agent` belongs to the `agents` toolset.

## Calling the tool

```json
{
  "agent_name": "researcher",
  "user_prompts": "Find why the nightly build fails.",
  "context_files": ["logs/failure.txt"],
  "tool_sets": ["file", "web", "terminal"]
}
```

Arguments:

| Argument | Meaning |
|---|---|
| `agent_name` | Required. A file name (never a path); also the history folder. |
| `user_prompts` | Required. The user message (a list is joined with blank lines). |
| `spawn` | Text shorthand for `agent_name` + `user_prompts`: `"spawn:@<agent_name>\n<task body>"`. Mutually exclusive with `agent_name`, `user_prompts`, `task_file` and `tasks`; the body is required. Authorization (`spawns`, depth 3) runs before any session row is written. Disabled by `delegation.spawn_directive: false`. |
| `system_prompts` | Fallback system prompt, used only when no definition file exists. |
| `skill_name` | Adds `skills/<skill_name>/agents/` to the definition search. |
| `model` | `provider:model`, a bare model name to keep the route's provider, or `@role[:level]` (see [Model roles](#model-roles)). |
| `tool_sets` | Toolsets for the run; omit (or `["*"]`) to use the definition's toolsets, or every host tool when it names none. |
| `enable_tools` / `tools` | Explicit allowlist of **registered** tool names. It replaces `tool_sets`; an unknown name fails the call instead of silently widening the surface. Host-only extra tools (`yield`) cannot be named. Pass one spelling or the other. |
| `context_files` | Up to 20 files (100 KB each, 200 KB total) supplied as **data**. Each path is project-relative (or absolute inside the project/profile home); `local://<path>` is the same reference. |
| `project_dir` | Working directory for the run; defaults to the detected project root. |
| `background` | `true` (default) returns a job; `false` blocks and returns the answer. A blocking `tasks[]` batch runs each task in array order on the calling thread. |
| `subject` | One-line topic for this agent session; unique across the profile, and how `agent_out` finds it. Defaults to the first line of `user_prompts`. |
| `name` | Profile-unique short address (`CamelCase`, ≤32) for `agent_message` and `agent_out`. Generated as `<AgentName><4hex>` when omitted; a duplicate is refused. |
| `continue_from` | A prior `agent_session_id` whose transcript seeds this run's history. The new run still gets its own id, subject and transcript. |
| `worktree` | Run in an isolated git worktree off `HEAD`; the run's edits are committed and merged back automatically. Off by default. |
| `skip_memory`, `skip_context_files`, `skip_background_review`, `load_soul_identity` | The host knobs; a named agent keeps memory, context files and the operator soul by default. |

### The agent definition

The first readable file among these wins (`<agent_name>.md` or `<agent_name>`), in groups —
project, then profile, then the converted bundled set, then the installation:

| Group | Directories |
|---|---|
| Project | `<project>/.agents/`, `<project>/agents/`, `<project>/skills/*/agents/` (the named `skill_name` first), `<project>/.hermes/plugins/*/agents/` |
| Profile | `<HERMES_HOME>/.agents/`, `<HERMES_HOME>/agents/`, `<HERMES_HOME>/skills/*/agents/`, `<HERMES_HOME>/plugins/*/agents/` |
| Bundled | `<install>/skills/agent-presets/agents/` (or `HERMES_BUNDLED_AGENTS`) |
| Installation | `<install>/.agents/`, `<install>/agents/`, `<install>/skills/*/agents/` (bundled skills), `<install>/plugins/*/agents/` (bundled plugins) |

A definition the project owns always beats a shipped one with the same name; the converted bundled
presets are the `agent-presets` skill's `agents/` directory. When a local file shadows a bundled
persona the payload reports `definition_shadowed: true`. `system_prompts` is the last fallback; an
empty prompt leaves the host's own system prompt as the only one.

#### Front matter

A leading YAML block is an enforced contract, not free-form metadata. Unknown keys are ignored
with a warning; an invalid value is a tool error and no agent is constructed.

| Field | Type | Meaning |
|---|---|---|
| `toolsets` | list of toolset names | The run's tool surface. Validated against the toolset registry — an unknown name fails the call, it never widens to every tool. `["*"]` or `["all"]` means every host tool. |
| `enable_tools` / `tools` | list of registered tool names | The exact tool surface; replaces `toolsets`. Unknown names fail the call. It is a tool-surface policy, **not a sandbox**: a definition that also lists `terminal` can still mutate the workspace. The old `read_only` key is no longer supported — list the read/search tools instead. |
| `max_iterations` | positive integer | The run's tool-call budget (default 10). |
| `reasoning_effort` | level | `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`, or `none`/`false` to disable. Applied after the host's own resolution, so it wins for this run. |
| `model` | `provider:model`, a bare model, or `@role[:level]` | Pins the route; a role is resolved against the profile's live config at call time. The optional `:level` sets the run's reasoning effort. One selector, never a list. |
| `output` | JSON Schema or omp's JTD-shaped mapping | The answer contract. `properties`/`optionalProperties`/`elements`/`metadata.description` are normalized to JSON Schema; the same OUTPUT CONTRACT and one bounded retry the delegated children get are applied, and the payload carries `output_schema_valid` (plus errors when invalid). Never enters the system prompt. |
| `structured` | boolean | omp parity switch for the output contract. `false` suppresses `output` validation for this agent; `true` or omitted keeps it. Hermes has no host-wide `task.structured`/parent-session schema, so an agent without `output` has no schema to enforce. |
| `autoloadSkills` | CSV or list of skill names | Skills loaded from the parent session and injected as hidden blocks before the agent's first prompt, like omp's `autoloadSkills`. Unknown or operator-disabled names are skipped with a warning; the agent body still runs. |
| `advisor` | boolean or model selector | Opts this named agent's spawned session into the native advisor. `true` uses the `WATCHDOG.yml` roster/default role, a selector pins the reviewer model. The global `advisor.enabled` gate still applies, and an explicit roster model wins over the selector. |
| `read-summarize` / `readSummarize` | boolean | Per-run override of `read.summarize.enabled`. `true` serves a structural outline for a whole-file `read_file` (tree-sitter is primary; Python falls back to `ast`, other languages to a declaration heuristic), with bodies elided and re-read hints; `false` always returns verbatim. Summaries are kept in memory for the run and written into the session bundle's `<topic>_ast.json` at settle (ADR-16). Explicit `offset`/`limit` reads are never summarized. |
| `spawns` | `"*"`, CSV, or list of agent names | Allow-list for nested `agent` calls (the `agents`/`delegation` toolset). Omitted: `*` only when the definition's own `toolsets` names `agents` or `delegation`, otherwise the agent cannot derive. Caller `tool_sets` does not change the policy. |
| `blocking` | boolean | `true` makes the parent wait even when async execution is available: it forces an inline run and the caller's `background: true` cannot override it. |
| `worktree` | boolean | Run in an isolated git worktree (see below). |
| `background`, `skip_memory`, `skip_context_files`, `skip_background_review`, `load_soul_identity` | boolean | The host knobs. |
| `name`, `description` | string | Documentation only; never sent to the model. |

Precedence per call, highest first: caller argument, definition, host default. `max_iterations`
and `reasoning_effort` are definition-owned and have no model-facing argument, so the tool schema
(and the cached prompt prefix) stays fixed. `enable_tools`/`tools` is accepted from both the
caller and the definition; an explicit allowlist replaces the toolset selection and can only name
registered tools.

#### Model roles

`model` takes exactly one selector: `provider:model`, a bare model name, or `@role[:level]`. A role is
omp's `modelRoles` vocabulary mapped onto the profile's own model settings, and it is resolved at
**call time**, not when the definition is read:

| Selector | Resolves to |
|---|---|
| `@default` | The host's own model route: no provider/model pin. History is unaffected — only `continue_from` carries a prior conversation. |
| `@task` | `delegation:` — the delegated-child route (provider, model, base URL, reasoning effort and `fallback_providers`). |
| `@advisor` | `advisor:` — the review model. |
| `@<auxiliary task>` | `auxiliary.<task>` (for example `@smol`, `@slow`, `@review`, `@plan`, `@vision`, `@judge`); the full list is `hermes_cli.aux_slots.AUX_TASK_KEYS`. |

The role supplies provider, model, base URL, endpoint key and the fallback chain (from
`auxiliary.<task>.api_key`/`fallback_chain` or the imported role's section such as
`delegation.fallback_providers`). A `:level` suffix (for example `@smol:high`) sets the run's
reasoning effort and must name a Hermes effort level. An explicit
`reasoning_effort` in the definition or the call still wins. The resolved role name is echoed as
`model_role` in the payload; it never enters the system prompt, so changing
`auxiliary.<task>` takes effect on the next call without editing any definition.

An unknown role is a tool error before any agent is constructed, and the message lists the roles
that exist — a typo never falls back to the host model. A list of model candidates is refused:
express alternatives as a role route or a single `provider:model`. Caller `model` beats the
definition's role.

```yaml
---
name: scout
model: "@smol"
---
```

The body is scanned with the same context-scope threat patterns a context file gets. A **project**
definition (cloned code) that matches is blocked, and the scan continues to the next candidate; if
nothing else qualifies the call returns an error naming the blocked path, unless the caller passed
`system_prompts`. A **profile** or **installation** definition is operator-owned, so a match only
warns and still loads. This is pattern-based mitigation, not a trust boundary.

#### Completing with `yield`

Every named run is given a per-run `yield` tool; it never enters the global `agents` toolset and
cannot be named in `tools`/`enable_tools` (host-extra tools are not model-registered tools).

```json
{"data": {"findings": []}, "type": ["findings"], "error": "", "final": true}
```

| Field | Meaning |
|---|---|
| `data` | The answer (object or string). With a definition/output schema it is validated first. |
| `type` | Optional section label(s) for an incremental result. |
| `error` | Terminal failure text; already-yielded `data` is kept, not discarded. |
| `final` | `true` (default) ends the turn with `data` as the answer. `false` records one section and keeps working. |

- An invalid `data` for the schema returns a tool error so the model can retry (bounded); the turn
  does not end.
- A run that never calls `yield` keeps the old contract: the final assistant message is the handoff.
- Each wake/revive turn resets the terminal state, so the next turn cannot end on the previous
  turn's `yield`.
- `agent_out` reads the settled structured output (`<topic>.json`) and the final answer from the
  `agent_session` row.
- `agent_sessions.yield_tool: false` suppresses the tool for new runs and revived agents, restoring
  the final-message contract as a rollout switch.

#### Worktree isolation

`worktree: true` (or the call argument) runs the agent in a fresh git worktree off `HEAD` under
`<repo>/.worktrees/`, on branch `hermes-subagent/...`. The run's tools start inside that tree, so
edits never touch the main checkout. Afterwards the run applies the work automatically: uncommitted
child edits are committed, then the branch is merged into the parent with `--no-ff`. Success reports
`applied: true` and the `merge_commit`, then removes the tree and the merged branch. The merge is
refused when the parent workspace was dirty when the agent was dispatched; a merge conflict aborts
the merge and keeps the branch. Those outcomes report `applied: false` with an `apply_error`.
The branch is never lost; the worktree is archived (restorable with `hermes worktree restore`) and
removed when `worktrees.archive_dirty` is on, or kept in place when it is off. A run with no
commits and a clean tree is removed.
Isolation needs a git work tree and a local terminal backend; otherwise the run degrades to the
shared workspace and the payload's `worktree` block says
`{"requested": true, "created": false, ...}`.

```json
{"agent_name": "sonic", "user_prompts": "Rename parse_v1 to parse_v2.", "worktree": true, "background": false}
```

#### Bundled presets

The converted bundled set ships `task`, `scout`, `reviewer`, `security-reviewer`, `seance`, and
`sonic` from omp's snapshots; `scripts/build_bundled_agents.py` regenerates it and maps omp's
`tools`/`thinking-level` front matter to `enable_tools`/`reasoning_effort`. The `agent-presets`
skill contributes `init` and remains the fallback for the five names the bundled set overrides.
`init` surveys a repository through `delegate_task` and writes `AGENTS.md`; that
file is a protected agent-instruction file, so the write needs one-operation human approval and
`init` returns the full document in its reply when approval is unavailable — it never retries
through `patch` or `terminal`.


### Background delivery

With the default `background: true` the tool returns `agent_session_id`, `subject`, `job_id` and
a queue position; the finished run is pushed
onto the host's background-completion queue — the same queue a background process finishes on —
so the calling session is woken with an `[ASYNC DELEGATION COMPLETE]` turn. `background: false`
returns the answer in the payload. A background call needs the calling session id; without one the
tool refuses and asks for `background: false`.

Recursion is capped at depth 3: an agent that holds the tool may start another agent, but not
without bound.

#### Batches (`tasks[]`)

`tasks` takes one object per agent (`agent`, `user_prompts`/`task`, and per-item overrides such as
`name`, `subject`, `tools`, `context_files`, `worktree` and `background`). Each task is one agent
session with its own transcript. **Blocking batches stay serial by design**: with `background:
false` — or when a definition sets `blocking: true` — the tasks execute one at a time, in array
order, on the calling thread, and the payload contains every task's answer. With the default
`background: true` the tasks are dispatched to the shared `agent_loop` pool (`agent_loop.max_workers`,
`agent_loop.max_queued`) and each delivers its own completion; `independent_completions: false`
buffers them into one combined completion. The batch is validated in full before the first spawn,
so a bad later task never leaves half the batch running.

### Resident sessions and wake turns

A background run that is not `blocking: true` and does not use a worktree stays **resident** after
it settles (ADR-15 D5–D7): its `AIAgent`, `agent_session` row, transcript and inbox stay, and the
session keeps a live slot until the idle TTL parks it. A message to a resident session starts a
**wake turn** on the same agent and appends to the same transcript — no new session id, no rewritten
prefix. After `agent_sessions.idle_ttl_seconds` (default 30 minutes) with no run and no pending
inbox the session is **parked**: the agent is closed and the slot released, but the row and inbox
survive. The next message **revives** it by rebuilding from the transcript with the definition and
`@role` re-resolved.

```yaml
agent_sessions:
  keep_alive: true            # false restores the one-shot run (agent closes on settle)
  idle_ttl_seconds: 1800      # 0 = never park
  max_runtime_seconds: 0      # hard wall clock; 0 = unlimited; a hit is an error, never a park
  max_live: 8                 # live (running or idle) sessions per profile
  max_parked: 64              # parked sessions; the oldest is cancelled past the cap
  wake_queue: 32              # messages waiting for a wake turn
  max_wake_turns: 1           # external-message wake turns before a chain stops
  yield_tool: true            # false = final assistant message is the handoff (pre-yield)
```

- The first run of a resident session executes on the session pool, not `agent_loop`; a wake or
  revive run occupies the same `max_live` budget.
- `blocking: true`, `background: false` and worktree runs stay one-shot and never park.
- `delegate_task(action="list")` shows live and parked named sessions with `live`/`parked`/`pending`
  counts; `agent_out(mode="inbox")` reports the same lifecycle for one session.
- `delegate_task(action="stop")` cancels an idle or parked named session (its pending inbox becomes
  `failed`); a running one is interrupted at its next boundary.
- If `max_live` is full a new background call returns `{"status": "queue_full", "code":
  "queue_full", ...}`; a message to a parked agent stays pending instead of being dropped.

### Steering a running named agent

The conversation that started a background run can steer it with the control plane it already
uses for delegated children:

```
delegate_task(action="steer", subagent_id="<agent_session_id>", message="focus on the tests")
```

- A live run receives the text at its next tool boundary; `action="list"` shows it while it runs.
- A resident idle session starts a wake turn; a parked session is revived and the message appended
  to its transcript. Both report `status: "woken"` / `"revived"`.
- With no resident session the message is stored in the session's inbox (`agent_inbox` in
  `state.db`) and reported as `status: "pending"`; it is not lost and is drained by the next
  run/revive.
- Only the owning conversation may steer a session. Another session gets the same
  unknown-target error as an already-finished delegated child.
- Message bodies are escaped (`<` of harness tags becomes `&lt;`), so a steer cannot close or
  forge a harness tag.

`action="stop"` interrupts a live named agent the same way it interrupts a delegated child; for a
parked one it marks the row `cancelled` and fails its pending inbox.

### Messaging a peer (`agent_message`)

Named agents get a model-facing `agent_message` tool for coordination. It is the write half of the
old `agent://` idea; reads stay on `agent_out`.

```json
{"to": "Scout7f3a", "content": "The failing test is in auth/session.", "reply_to": "inbox-..."}
```

- `to` accepts an `agent_session_id`, a profile-unique short `name`, a unique `subject`, or the id
  of the owning conversation (a named child may message its owner). `agent_session_id` / `name` /
  `subject` are also accepted as separate fields.
- Delivery matches `delegate_task(steer)`: a live target receives the text at its next tool
  boundary; an idle resident target gets a wake turn; a parked/absent target gets a pending
  `agent_inbox` row that `agent_out(mode="inbox")` reads.
- The recipient sees an envelope whose `from`, `from_subject` and `reply_to` are server-resolved;
  the model cannot forge them. Answer by passing that `reply_to` value back, and `progress: true`
  marks an interim note.
- Authorized pairs: an owner ↔ its named session, siblings under the same parent, and a parent ↔
  its child. A named child may address the conversation that dispatched it while that surface is
  live; an offline owner is reported `pending`, never a fake `delivered`. Cross-profile and
  unrelated sessions are refused.
- Bodies are escaped, so a message can never close or forge a harness tag. `max_wake_turns` caps
  reply chains; past the cap the reply is stored pending instead of waking again.
- `agent_messages.envelope: false` delivers the escaped body without the envelope attributes (no
  `reply_to` threading) as a rollout switch.

## History on disk

Every call is one agent session with one bundle namespace under its parent conversation's
bundle (a run with no parent session gets its own bundle):

```
<HERMES_HOME>/sessions/<project-dir>/<timestamp>_<parent-session-id>/
  <topic>.jsonl        # transcript, append-only
  <topic>.md           # final report, verbatim (completed runs)
  <topic>_ast.json     # structural summary, always written at settle
  <topic>.json         # structured output, only for output-schema runs
  <topic>/             # this run's tool-output archive and advisor
    1.terminal.log
    1.terminal-original.log
```

- The tool returns `agent_session_id`, `subject` and `name`; all three identify the session, and
  the id is what the `agent_session` row (and its recorded `transcript_path`) and `agent_out` are
  keyed on. A pre-ADR-16 session keeps its `sessions/<agent>/<agent_session_id>.jsonl` path and
  stays readable.
- One JSON object per line, append-only. The `kind: "run"` header carries `agent_session_id`;
  `kind: "msg"` rows carry `role`, `text`, `tool_calls`, `tool_call_id`/`name`/`result` and
  reasoning.
- Different agents never share a file, and different agent sessions never share one.
- There is **no implicit continuation**: a second call with the same `agent_name` starts a fresh
  session with its own transcript. To carry a conversation forward, pass `continue_from` with the
  previous `agent_session_id`; that history seeds the new run, and a replayed prefix is
  de-duplicated by content fingerprint so the new file grows by what happened, not by the prompt
  length.
- The system prompt is stored once per distinct text and is **not** replayed as a conversation
  turn; the host owns it.

## Reading a run back (`agent_out`)

`agent_out` reads an agent session after the fact, by `agent_session_id`, `name`, or `subject`
(resolved in that order):

| Mode | Returns |
|---|---|
| `history` (default) | The full JSONL transcript as `events`, character-capped (`truncated` says when the file is longer). |
| `artifact` | The settle-time files. `kind=report` (default) returns `<topic>.md` verbatim; `kind=tool` with `step` + `tool_name` returns `<topic>/<step>.<tool>.log` and the `-original.log` when the result was truncated; `kind=all` lists every file that exists. A missing file returns `null` with a note — never guessed content. |
| `ast_summary` | `<topic>_ast.json`: `{version, agent_session_id, subject, generated_at, sources, reason?}`. `sources: []` + `reason` is a valid result. |
| `inbox` | `pending` messages that no live run received yet, plus the most recent `delivered` ones, with `pending_count`/`delivered_count`. |
| `context_files` | The files supplied at spawn, as recorded: `raw` spelling, resolved path, byte size and sha256. `include_content: true` also returns their current text, re-resolved through the spawn-time root checks and budgets; a file that moved outside the roots or grew past the budget is reported per entry, never read. |

The identity also carries `bundle_dir`, `report_path` and `ast_summary_path`. `artifact` serves
named agent sessions only: a main conversation has no bundle and is refused explicitly. There is no
`final_response` mode — read the `agent_session` row (or `history`).

`agent_session` rows live in `state.db` (table `agent_session`): the id, the agent name, the
unique subject, the profile-unique `short_name`, the transcript path, the artifact manifest, the
final answer, the status (`running`/`completed`/`error`/`stalled`/`parked`/`cancelled`), the idle
clock and the rebuild spec a wake/revive needs (which includes the spawn-time `context_inputs`). Rows are inserted before dispatch, so the id the tool returned resolves
immediately, and settled from whichever thread finishes the run.

`inbox` also reports the session lifecycle: `live` (a resident running/idle session), `parked`
(the idle TTL closed the agent; a message revives it) and `state`.

## Pool and limits

Two pools with separate budgets:

- **Resident named sessions** (`agent.agent_session_runtime`) run background `agent` calls that
  stay resident, plus their wake/revive turns. They never occupy `agent_loop` workers. Budget:
  `agent_sessions.max_live` / `wake_queue` (see [Resident sessions](#resident-sessions-and-wake-turns)).
- **One-shot runs** — `background: false`, `blocking: true`, worktree runs and plugin
  `ctx.agent_loop` calls — use the shared `agent_loop` pool (`agent.agent_loop_service`):

```yaml
agent_loop:
  max_workers: 8       # simultaneous runs (one thread each)
  max_queued: 64       # calls allowed to wait; beyond this the caller gets an error
  max_run_seconds: 1800  # a run holding its slot this long is reported as stalled
```

A stalled `agent_loop` run's slot is released and the caller is notified; the thread cannot be
killed, so the run may still finish, but a late finish is not reported twice. A resident session
past `agent_sessions.max_runtime_seconds` is settled as `error` (`timed out`) and its live slot is
released immediately; it is never parked.

## Plugins: `ctx.agent_loop`

The same capability is available to plugins from `register(ctx)`:

```python
def register(ctx):
    loop = ctx.agent_loop                     # capability_version: "agent_loop_v1"

    result = loop.run(                        # blocks; returns AgentRunResult
        "Summarise the new issues.",
        system_message="You summarise issue trackers.",
        toolsets=["web", "file"],
        max_iterations=5,
        layer="issue-summarizer",
        history_session=session_id,
    )
    result.answer, result.usage, result.transcript_path

    job = loop.submit("A slower analysis.", layer="analyst")   # fire-and-forget
    job.future.result()                       # AgentRunResult or the failure
    loop.stats()                              # running / queued / capacity
```

Parameters are `agent.agent_run.run`'s keyword surface: `context`, `model`, `provider`,
`api_mode`, `toolsets`, `system_message`, `max_iterations`, `project_dir`, `all_tools`,
`enable_tools`, `reasoning_effort`, `worktree`, `base_url`, `api_key`, `fallback_chain`, `model_role`,
`output_schema`,
`history`, `session_id`, `agent_session_id`,
`layer`, `history_session`, `account_task`, `skip_memory`, `skip_context_files`,
`skip_background_review`, `load_soul_identity`, `extra_tools`, `cache_scope`, `tool_guardrails`,
`meta`, `record_history`. The agent tool also uses the internal seams `on_agent` / `on_settled`
(registration around the built `AIAgent`) and `keep_alive` (leave the agent open for the caller).
The run inherits the plugin's profile scope (home, secrets, config) because the pool copies the
submitter's contextvars onto the worker thread.

Feature-detect with `getattr(ctx, "agent_loop", None)`: a host without the capability (including
out-of-process plugin hosts) raises `AttributeError` as usual.
