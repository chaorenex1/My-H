# omp-ui — omp's terminal shell, vendored for Hermes

`omp-ui` is omp's own `InteractiveMode` (upstream coding-agent shell, not a reimplementation) with
its dependency tree vendored into `src/`, ready to be driven by the Hermes profile gateway. The current
plan is [`../docs/omp-ui-panels-gateway-plan.md`](../docs/omp-ui-panels-gateway-plan.md) (the old
`../docs/omp-ui-gap-plan.md` is deprecated); this file is the current state, not the target state.

## Status (2026-11-01)

| Stage | State |
|---|---|
| **G1** snapshot + vendor closure | done — `src/` is the pinned upstream snapshot; non-installed packages live in `src/vendor/` (pi-tui + pi-natives stay installed) |
| **G2** import boundary (R1–R5) | done — `bun run check:imports` clean, `bun run check` passes |
| **G3** `AgentSession`/`SessionManager` → gateway client | **main screen wired** — `hermes omp` creates or resumes a Hermes session over the paired WebSocket; prompts, streaming text/thinking, tool cards, queue/todo state, usage/context and history all come from the gateway. `AgentSession` keeps its class and controllers; the gateway replaces the turn source (`attachGateway`), so there is no local-agent mode and no mode switch. Local JSONL persistence and the local model picker remain, marked below |
| **G4** Hermes entry (`hermes omp` → `src/main.ts`) | done — `main.ts` parses argv, opens the gateway session, seeds history, and boots `InteractiveMode`; omp's `runRootCommand`/CLI command layer is not on this path (`src/cli.ts` stays the worker-host fallback). `--resume` takes a Hermes session id/title |
| **G5** smoke status machine | not started |

The transport (`src/modes/rpc/gateway-client.ts` + `gateway-socket.ts`) and the main-screen projection
(`src/modes/rpc/gateway-session.ts`) are covered by `bun test src/modes/rpc`. The wiring was exercised
against a live gateway: create, resume, streamed reply, a `terminal` tool call, usage/context refresh,
and interrupt.

### Wired now

- session create/resume (`session.create` / `session.resume`), snapshot history, reconnect replay cursor
- prompt submission, steering, follow-ups, queued cancel, `session.interrupt` (with a generation retry)
- `message.start/delta/interim/complete`, reasoning, `tool.start/progress/complete`, `turn.*`,
  `prompt.result`, `session.settled`, `queue.updated`, `todo.updated`, `session.info`, `session.usage`
- Hermes slash commands that omp does not handle locally go through `command.dispatch`
- startup chrome is omp's own composition (plan §2.3.1): welcome lockup + tip, config warnings, and
  What's New from the vendored `CHANGELOG.md` keyed to the shell snapshot version (a resume suppresses
  the intro and skips it); the optional startup splash follows `startup.showSplash` + `startup.quiet`
- status line model (relabelled to the served gateway model), context tokens/window, session cost
- the local `SessionManager` is an in-memory accounting projection (usage/context/`/session`), never a
  second transcript writer

### Still local / next

- **approvals and clarify** answer deny/cancel with a notice (no dialog route yet); a turn that needs
  an approval in ask mode will be refused
- **`/model`, `/resume` pickers** still read omp's local catalog and session list, not
  `model.options` / `session.list`
- **`/tree`, branch/rewind, compact** still drive local `SessionManager` mutations; the gateway owns
  those operations
- **subagent/JOBS/usage dashboards** consume gateway events only where mapped above
- **login/logout, MCP, skills, plugins, cron** panels still use omp's local backends

## Layout

    src/                        pinned snapshot of packages/coding-agent/src (modified in place)
    src/main.ts                 Hermes entry: argv parsing → session → InteractiveMode
    src/cli.ts                  upstream omp CLI; retained only as the worker-host fallback,
                                never the launch entry
    src/session/agent-session.ts         the shell's session; `attachGateway()` makes the gateway
                                         its turn source (local members stay for the worker host)
    src/session/session-manager.ts       in-memory accounting projection on the wired path
    src/modes/rpc/gateway-client.ts      JSON-RPC 2.0 envelopes, event unwrap, seq/replay (done)
    src/modes/rpc/gateway-socket.ts      WebSocket transport + reconnect
    src/modes/rpc/gateway-session.ts     session create/resume, prompt/steer/interrupt, event projection
    src/modes/rpc/gateway-contract.generated.ts   generated from tui_gateway/contracts/*.py
    src/vendor/<pkg>/           import closure of utils/ai/agent/catalog/wire/omptype/stats/
                                snapcompact/mnemopi (see src/VENDORED.md)
    src/assets/                 package.json, CHANGELOG.md, notices the shell imports (R2)
    types/assets/index.d.ts     `*.md`/`*.txt`/… import declarations (from upstream)
    scripts/check-imports.ts    purity gate: R1–R3/R5 + every relative import resolves
    scripts/sync-upstream.py    replay: shell snapshot → vendor closure → rewrite → assets → patches
    tsconfig.json               typecheck only (the runtime is Bun; no build step)

## Why a separate Bun workspace

The `@oh-my-pi/*` packages publish TypeScript sources (`exports` point at `src/*.ts`), so there is no
build step and Node cannot load them: Bun is the runtime. Keeping `omp-ui/` a standalone Bun project —
not an npm workspace — means `npm install` at the repo root stays untouched and the shell's dependency
graph cannot leak into the Ink TUI.

`package.json` `dependencies` is exactly `@oh-my-pi/pi-tui` (renderer) and `@oh-my-pi/pi-natives`
(platform addon). Every other `@oh-my-pi/*` specifier is rewritten to `src/vendor/…` by
`scripts/sync-upstream.py`; the tsconfig `paths` map keeps pi-tui's internal imports on the same
vendored modules, so a `const enum` or class has one identity at typecheck time and at runtime.
`bun.lock` is committed; the launcher's install marker lives in `node_modules/.hermes-omp-ui`.

`devDependencies` mirror the upstream shell's optional/runtime packages (transformers, onnxruntime,
fastembed, OpenTelemetry, `@types/bun`, `typescript`) so the untouched STT/TTS/telemetry files
typecheck; the wired path does not import them.

## Run it

    cd omp-ui
    bun install
    bun run check            # tsc --noEmit (src + scripts)
    bun run check:imports    # R1–R3/R5, unresolved relative modules = 0
    bun test src/modes/rpc   # gateway transport tests
    bun src/main.ts --version # prints the shell's version
    bun src/main.ts           # Hermes entry: gateway session → InteractiveMode
    bun src/main.ts --resume <session-id|title>

Rebuild `src/` from the pinned checkout (`OMP_SRC` overrides the path):

    python scripts/sync-upstream.py && bun run check:imports && bun run check

## Transport: the gateway WebSocket, not stdio

`tui_gateway.entry` (stdio) refuses to start beside a live gateway — it would be a second `state.db`
writer. The shell does what the Ink TUI and Desktop do:

1. `ui-tui/scripts/gateway_bootstrap.py` performs discovery (and, with `--start`, ensures an owner)
   and mints a session ticket: `{url, protocols}`.
2. The client connects with those subprotocols and speaks JSON-RPC, one JSON object per text frame.
3. First frame is `event {type: "gateway.ready"}`; the client then sends
   `client.capabilities {server_requests: true}` once per connection.

`gateway-client.ts` implements all of that, including the error-code normalization and the
`seq`/`replay_epoch` watermark used to dedupe reconnect replay.

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

## Provenance

Renderer, shell and dialog components come from omp (`https://github.com/can1357/oh-my-pi`, MIT).
`src/` is the upstream snapshot at commit `6a86df8539fccf3ad7818081266e554022fc21bc`
(shell `18.14.0`; `@oh-my-pi/*` packages installed at `18.14.1`) and is modified in place from G1
on. Vendored trees, the R2 relocations and the `main.ts` entry patch are listed in
[`src/VENDORED.md`](src/VENDORED.md); `src/LICENSE` carries the upstream MIT license.
