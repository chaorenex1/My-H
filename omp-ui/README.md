> **方向已重定（2026-10-31）：** 实现位置改为 `src`（就地修改），协议代码从
> `oh-my-pi` vendor 进 `src`，`src` 的直接依赖只允许 `@oh-my-pi/pi-tui`。本文描述的
> `app/` + `coding-agent/hermes/` 桥接是**旧路径，已冻结、待删除（G6）**。
> 唯一权威计划见 [`../docs/omp-ui-gap-plan.md`](../docs/omp-ui-gap-plan.md)。

# omp-ui — Hermes' TUI on omp's renderer

`hermes omp` opens **omp's own `InteractiveMode`** — the upstream coding-agent shell, not a
reimplementation — and runs it as a JSON-RPC client of the Hermes profile gateway. The screen is
the real thing: omp's welcome lockup, band editor, transcript cards, status strip, tool cards,
approval/ask dialogs and slash autocomplete. Hermes owns the agent: sessions, streaming, tools,
approvals and commands all come from the gateway's canonical session API.

    hermes omp              # the product entry point
    hermes omp --resume <session-id>
    hermes omp --no-start   # attach to a running gateway only

`hermes omp` attaches to the profile gateway (starting one when none is running, like
`hermes --tui`); it never opens `state.db` itself. First run installs this workspace with
`bun install`.

## Why a separate workspace

The `@oh-my-pi/*` packages publish TypeScript sources (`exports` point at `src/*.ts`), so there is no
build step and Node cannot load them: Bun is the runtime. Keeping `omp-ui/` a standalone Bun project —
not an npm workspace — means `npm install` at the repo root stays untouched and the shell's dependency
graph cannot leak into the Ink TUI.

The only declared dependency is `@oh-my-pi/pi-tui`, with a floor and no upper bound
(`>=18.14.0`). Every other `@oh-my-pi/*` import **must** be served by code vendored into
`src/vendor/`; until that vendoring runs, those imports resolve through pi-tui's transitive
dependencies (transitional only). When a dependency is removed, its code is copied on demand
(import closure) from the local checkout `C:\Users\zarag\projects\oh-my-pi` at the pinned 18.14.0
commit, never from `node_modules`.
`bun.lock` is committed; `bun install` is run once per lockfile change and its digest is recorded
in `node_modules/.hermes-omp-ui`.

## Layout

> 以下是**旧路径**的布局（冻结，G6 删除）。新布局见
> [`../docs/omp-ui-gap-plan.md` §2](../docs/omp-ui-gap-plan.md)。

    coding-agent/hermes/main.ts       [legacy] entry: gateway session → omp InteractiveMode
    coding-agent/hermes/session.ts    [legacy] HermesAgentSession: the AgentSession-shaped double the UI drives
    coding-agent/hermes/projector.ts  [legacy] gateway events → omp AgentSessionEvents (text/tools/turn)
    coding-agent/hermes/messages.ts   [legacy] Hermes rows → pi-ai messages (history + live)
    coding-agent/hermes/dialogs.ts    [legacy] approval / clarify / sudo / secret on omp's dialogs
    coding-agent/hermes/commands.ts   [legacy] `/` input → gateway command.dispatch, catalog → autocomplete
    coding-agent/hermes/skin.ts       [legacy] Hermes skin → omp theme (`/theme`, `/skin`)
    coding-agent/hermes/settings.ts   [legacy] `/settings` over config.get/set
    coding-agent/hermes/browse.ts     [legacy] `/browse` transcript browser (`/copy` uses omp's picker)
    coding-agent/hermes/manage.ts     [legacy] `/mcp`, `/skills`, `/plugins`, `/cron` panels
    coding-agent/hermes/smoke.ts      [legacy] headless end-to-end run (test terminal, live gateway)
    src/                              [implementation target] the shell; modified in place from G1 on
    app/                              [legacy] the earlier Composer-only shell
    app/gateway.ts, history.ts, hermes.ts, palette.ts   [legacy] shared client + conversions
    tsconfig.json                     typecheck only (the runtime is Bun; no build step)

### Why an `AgentSession` double

`InteractiveMode` is written against omp's `AgentSession` (315 public members, a 15k-line class that
owns omp's model loop and tools). Hermes owns the loop, so `HermesAgentSession` supplies the surface
the UI actually queries — state, model, usage, history, queue, event subscription — and routes the
mutating verbs (`prompt` / `abort` / `steer`) to the gateway. omp ships the same kind of double for
its own galleries (upstream `packages/coding-agent/src/cli/gallery-fixtures/preview-session.ts`), which is
what makes this seam legitimate rather than a fork:

| UI needs | Double answers with |
|---|---|
| `session.subscribe(ev)` | `projector.apply(type, payload)` for every gateway frame |
| `session.prompt(text)` | `prompt.submit`; resolves when the turn seals |
| `session.abort()` | `session.interrupt` |
| `session.state.model/messages`, `getContextUsage()` | gateway `session.info` / `session.usage` |
| `session.sessionManager.buildSessionContext()` | history rows → pi-ai messages (`historyMessages`) |
| `session.slashCommands` | `commands.catalog` (602 commands on a stock profile) |
| `extensionRunner`, `goalRuntime`, `agent` | no-op doubles: Hermes has no in-process extensions |

`message.delta` / `reasoning.delta` become cumulative `message_update`s; `tool.start` seals the
assistant segment and emits `tool_execution_start`; `tool.complete` emits `tool_execution_end`;
`message.complete` seals the turn with `turn_end` + `agent_end`.

## Run it directly

    bun install --ignore-scripts
    HERMES_AGENT_ROOT=<checkout> bun coding-agent/hermes/main.ts
    HERMES_OMP_START=1 bun coding-agent/hermes/smoke.ts     # headless proof

omp keeps its own settings under a Hermes-scoped config root (`PI_CONFIG_DIR`, default
`~/.hermes/omp-ui`; profile-aware via `HERMES_HOME`), so `hermes omp` never reads or writes the
user's `~/.omp` install.

Env: `HERMES_AGENT_ROOT` (checkout owning `ui-tui/scripts/gateway_bootstrap.py`; defaults to this
repo), `HERMES_PYTHON` (bootstrap interpreter), `HERMES_OMP_START=1` (own/start the gateway),
`HERMES_OMP_RESUME` (same as `--resume`), `HERMES_VERSION` (welcome lockup label; `hermes omp` passes
the install's version), `HERMES_BUN` (binary to launch).

Keys are omp's own: `Enter` send · `Ctrl+C` interrupt · `Ctrl+O` expand tool output · `/` command
palette (Hermes catalog) · `Esc` cancel a dialog.

## Transport: the gateway WebSocket, not stdio

`tui_gateway.entry` (stdio) refuses to start beside a live gateway — it would be a second `state.db`
writer. This shell does what the Ink TUI and Desktop do:

1. `ui-tui/scripts/gateway_bootstrap.py` performs discovery (and, with `--start`, ensures an owner)
   and mints a session ticket: `{url, protocols}`.
2. The client connects with those subprotocols and speaks JSON-RPC, one JSON object per text frame.
3. First frame is `event {type: "gateway.ready"}`; the client then sends
   `client.capabilities {server_requests: true}` once per connection.

Wire notes that cost time to discover:

| Call | Constraint |
|---|---|
| `session.create` | `source` must be `cli`/`tui`/`gui`/`acp`/`tool`/`oneshot` |
| `session.resume` | returns raw stored rows + execution generation + open requests/prompts |
| `prompt.submit` | requires `submission_id` (uuid) |
| `session.interrupt` | requires `execution_generation` |
| `approval.respond` | for approvals restored as snapshot `prompts` (no request frame) |

Server→client requests carry string ids (`srq-…`); `request.cancel` must tear the dialog down or the
agent's wait never settles.

## Verification

    bun run check                                       # tsc --noEmit (app + coding-agent/hermes)
    HERMES_OMP_START=1 bun run smoke:native             # live gateway + live turn (writes out/)
    HERMES_OMP_START=1 bun run smoke                    # the earlier shell, unchanged

`smoke:native` boots the real `InteractiveMode` on a test terminal and checks: init renders (welcome,
status line with model/cwd), the Hermes command catalog reaches the autocomplete, a live turn streams
`pong` into omp's transcript, the tool card carries the result/diff/todo details (Hermes' ANSI
`inline_diff` normalized to a unified diff, Ctrl+O expands), the todo HUD mounts from
`todo.updated`, queue/steer map to the gateway, `/model` opens and closes omp's picker, `/resume`
switches the live session and replays history, `/new` starts a fresh session, `/export md` writes the
gateway-rendered file, a notification lands on omp's status row, goal state reaches the footer,
`/agents` and `/jobs` mount pickers, `/mcp` and `/skills` list live registries, `/plugins`, `/cron`
and `/settings` mount their panels, `/btw` and `/background` start side agents, images become
`prompt.submit.attachments`, a server→client approval answers with `once`, a Mermaid fence renders
as a diagram, Hermes' skin repaints omp's theme, and `/browse`/`/copy` read the live transcript.
Evidence: `coding-agent/hermes/out/native.smoke.txt`.

## Known gaps

Hermes skins map onto omp's theme tokens (accent, borders, status line, diffs) rather than replacing
the whole renderer palette; the remaining omp tokens keep the light/dark built-in. Attachment paste
chips, inline images and the transcript tree-rewind (`/tree`, which needs a server-side branch
operation) are still open. Tracked in `../docs/omp-ui-gap-plan.md`.

## Provenance

Renderer, shell and dialog components come from omp (`https://github.com/can1357/oh-my-pi`, MIT).
`src/` started as the upstream snapshot at commit
`6a86df8539fccf3ad7818081266e554022fc21bc` (`@oh-my-pi/pi-coding-agent@18.14.0`); under the
2026-10-31 direction it becomes the **implementation** and is modified in place, so the
byte-identical/read-only claim **no longer applies**. Provenance for the vendored trees moves to
`src/vendor/MANIFEST.json` + `src/VENDORED.md` (G1/G2 in
`../docs/omp-ui-gap-plan.md`). The legacy `coding-agent/hermes/` bridge is frozen for deletion (G6).
