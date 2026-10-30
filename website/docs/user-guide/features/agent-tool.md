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
| `system_prompts` | Fallback system prompt, used only when no definition file exists. |
| `skill_name` | Adds `skills/<skill_name>/agents/` to the definition search. |
| `model` | `provider:model`, or a bare model name to keep the route's provider. |
| `tool_sets` | Toolsets for the run; omit (or `["*"]`) to use the definition's toolsets, or every host tool when it names none. |
| `context_files` | Up to 20 files (100 KB each) supplied as **data**. Must resolve inside `project_dir`/the working tree or the profile home. |
| `project_dir` | Working directory for the run; defaults to the detected project root. |
| `background` | `true` (default) returns a job; `false` blocks and returns the answer. |
| `worktree` | Run in an isolated git worktree off `HEAD`; the run's edits land on its own branch. Off by default. |
| `skip_memory`, `skip_context_files`, `skip_background_review`, `load_soul_identity` | The host knobs; a named agent keeps memory, context files and the operator soul by default. |

### The agent definition

The first readable file among these wins (`<agent_name>.md` or `<agent_name>`), in groups —
project, then profile, then installation:

| Group | Directories |
|---|---|
| Project | `<project>/.agents/`, `<project>/agents/`, `<project>/skills/*/agents/` (the named `skill_name` first), `<project>/.hermes/plugins/*/agents/` |
| Profile | `<HERMES_HOME>/.agents/`, `<HERMES_HOME>/agents/`, `<HERMES_HOME>/skills/*/agents/`, `<HERMES_HOME>/plugins/*/agents/` |
| Installation | `<install>/.agents/`, `<install>/agents/`, `<install>/skills/*/agents/` (bundled skills), `<install>/plugins/*/agents/` (bundled plugins) |

A definition the project owns always beats a shipped one with the same name; when a local file
shadows a bundled persona the payload reports `definition_shadowed: true`. `system_prompts` is the
last fallback; an empty prompt leaves the host's own system prompt as the only one.

#### Front matter

A leading YAML block is an enforced contract, not free-form metadata. Unknown keys are ignored
with a warning; an invalid value is a tool error and no agent is constructed.

| Field | Type | Meaning |
|---|---|---|
| `toolsets` | list of toolset names | The run's tool surface. Validated against the toolset registry — an unknown name fails the call, it never widens to every tool. `["*"]` or `["all"]` means every host tool. |
| `read_only` | boolean | Strips `write_file`, `patch`, `agent` and `skill_manage`. A tool-surface policy, **not a sandbox**: a definition that also grants `terminal` can still mutate the workspace. |
| `max_iterations` | positive integer | The run's tool-call budget (default 10). |
| `reasoning_effort` | level | `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`, or `none`/`false` to disable. Applied after the host's own resolution, so it wins for this run. |
| `model` | `provider:model` or a bare model | Pins the route. |
| `worktree` | boolean | Run in an isolated git worktree (see below). |
| `background`, `skip_memory`, `skip_context_files`, `skip_background_review`, `load_soul_identity` | boolean | The host knobs. |
| `name`, `description` | string | Documentation only; never sent to the model. |

Precedence per call, highest first: caller argument, definition, host default. `read_only`,
`max_iterations` and `reasoning_effort` are definition-owned and have no model-facing argument, so
the tool schema (and the cached prompt prefix) stays fixed.

The body is scanned with the same context-scope threat patterns a context file gets. A **project**
definition (cloned code) that matches is blocked, and the scan continues to the next candidate; if
nothing else qualifies the call returns an error naming the blocked path, unless the caller passed
`system_prompts`. A **profile** or **installation** definition is operator-owned, so a match only
warns and still loads. This is pattern-based mitigation, not a trust boundary.

#### Worktree isolation

`worktree: true` (or the call argument) runs the agent in a fresh git worktree off `HEAD` under
`<repo>/.worktrees/`, on branch `hermes-subagent/...`. The run's tools start inside that tree, so
edits never touch the main checkout. Afterwards the run inspects it: no commits and a clean tree →
removed; work left behind → archived to `refs/hermes/archive/<tree>` and removed, branch kept.
Isolation needs a git work tree and a local terminal backend; otherwise the run degrades to the
shared workspace and the payload's `worktree` block says
`{"requested": true, "created": false, ...}`.

```json
{"agent_name": "sonic", "user_prompts": "Rename parse_v1 to parse_v2.", "worktree": true, "background": false}
```

#### Bundled presets

The `agent-presets` skill ships six definitions: `init`, `scout`, `sonic`, `reviewer`,
`security-reviewer`, `seance`. Pass `skill_name="agent-presets"` to search the pack first; the
[skill page](../skills/bundled/agent-presets/agent-presets-agent-presets.md) lists each one's
toolset and use. `init` surveys a repository through `delegate_task` and writes `AGENTS.md`; that
file is a protected agent-instruction file, so the write needs one-operation human approval and
`init` returns the full document in its reply when approval is unavailable — it never retries
through `patch` or `terminal`.


### Background delivery

With the default `background: true` the tool returns a job handle and the finished run is pushed
onto the host's background-completion queue — the same queue a background process finishes on —
so the calling session is woken with an `[ASYNC DELEGATION COMPLETE]` turn. `background: false`
returns the answer in the payload. A background call needs the calling session id; without one the
tool refuses and asks for `background: false`.

Recursion is capped at depth 3: an agent that holds the tool may start another agent, but not
without bound.

## History on disk

Every run appends to one file per agent and session:

```
<HERMES_HOME>/sessions/<agent_name>/<session_id>.jsonl
```

- One JSON object per line, append-only. `kind: "run"` headers delimit runs; `kind: "msg"` rows
  carry `role`, `text`, `tool_calls`, `tool_call_id`/`name`/`result` and reasoning.
- Different agents never share a file, and different sessions of one agent never share one.
- The next run of the same agent in the same session reads that file back as
  `conversation_history`, so the agent continues its own thread. A replayed history prefix is
  de-duplicated by content fingerprint, so the file grows by what happened, not by the prompt
  length.
- The system prompt is stored once per distinct text and is **not** replayed as a conversation
  turn; the host owns it.

## Pool and limits

Runs execute on the shared `agent_loop` pool (`agent.agent_loop_service`), also exposed to
plugins as `ctx.agent_loop`. Each accepted call runs on its own daemon thread — N simultaneous
calls use N threads — up to a cap, then a FIFO wait line:

```yaml
agent_loop:
  max_workers: 8       # simultaneous runs (one thread each)
  max_queued: 64       # calls allowed to wait; beyond this the caller gets an error
  max_run_seconds: 1800  # a run holding its slot this long is reported as stalled
```

A stalled run's slot is released and the caller is notified; the thread cannot be killed, so the
run may still finish, but a late finish is not reported twice.

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
`read_only`, `reasoning_effort`, `worktree`, `history`, `session_id`, `layer`,
`history_session`, `account_task`, `skip_memory`, `skip_context_files`, `skip_background_review`,
`load_soul_identity`, `extra_tools`, `cache_scope`, `tool_guardrails`, `meta`,
`record_history`. The run inherits the plugin's profile scope (home, secrets, config) because the
pool copies the submitter's contextvars onto the worker thread.

Feature-detect with `getattr(ctx, "agent_loop", None)`: a host without the capability (including
out-of-process plugin hosts) raises `AttributeError` as usual.
