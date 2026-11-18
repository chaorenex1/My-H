# omp-ui — the omp TUI as a Hermes gateway client

`omp-ui` is the omp terminal shell driving a **Hermes session**. It renders the conversation and
forwards every turn operation to the Hermes session authority over its JSON-RPC wire; it runs no
agent loop, executes no tools, and persists nothing except the caller's own UI preferences.

The tree was carved on 2026-11-01: only the rendering core, the gateway data-ingress layer and the
RPC protocol remain. omp's own product code — the agent loop, tool implementations, prompt corpus,
providers, compaction, MCP/LSP/IDA/DAP integrations, eval/exec, extensions, skills, subagents/jobs,
memory backends, TTS/STT, collab, plan/goal/vibe modes and the CLI launch graph — is gone. See
`src/VENDORED.md` for the dependency boundary and `../docs/omp-ui-panels-gateway-plan.md` §0.3 for
the decision record.

## Architecture

| Layer | Files | Responsibility |
|---|---|---|
| Entry | `src/main.ts`, `src/modes/rpc/hermes-entry.ts` | argv → pair with the gateway → `session.create`/`session.resume` → `InteractiveMode.init()` → render + `getUserInput()` loop |
| Protocol | `src/modes/rpc/gateway-client.ts`, `gateway-socket.ts`, `gateway-contract.generated.ts`, `gateway-contract.openrpc.json` | JSON-RPC 2.0 envelopes, reconnect/replay (`seq`), server→client requests, the canonical methods/notifications contract generated from `tui_gateway/contracts/*.py` |
| Data ingress | `src/modes/rpc/gateway-session.ts`, `gateway-models.ts`, `gateway-appearance.ts` | `session.create/resume/history/usage/stats`, prompt submit/steer/cancel, `command.dispatch`, `model.options`, `session.mutate`, `session.list`, `session.transcript.list/read` (ADR-16 cold mirror), `omp.appearance.*`; the event projection onto the shell's `AgentSessionEvent` vocabulary |
| Session facade | `src/session/agent-session.ts`, `session-manager.ts`, `agent-session-events.ts`, `agent-session-types.ts` | the renderer-facing session: transcript, model/thinking selectors, queue, notices, usage/context; the in-memory accounting projection |
| UI | `src/modes/interactive-mode.ts`, `controllers/{event,input,command,selector,todo-command}-controller.ts`, `utils/ui-helpers.ts`, `types.ts`, `settings.ts` | transcript/composer/status line/HUD, gateway-fed selectors, local UI commands, notices |
| Config | `src/config/*`, `src/session/settings.ts`, `src/modes/settings.ts` | the UI settings registry and the display-only model catalog |
| Vendored libs | `src/vendor/{ai,agent,catalog,utils,wire,omptype,snapcompact}` | data model + rendering support (see `src/VENDORED.md`) |

The main screen is one `AgentSession` attached with `attachGateway()`: turns, streamed text/thinking,
tool cards, todo HUD, usage/context, queue and title all arrive as gateway events. `EventController`
projects them onto `pi-tui` components; `CommandController` runs the shell's own commands and sends
everything else to `command.dispatch`; `SelectorController` reads the gateway catalog/session list and annotates each row with its
cold-mirror state (`archived` / `not mirrored`).

## Shell commands (local)

`/model`, `/models`, `/thinking`, `/settings` (`/theme`), `/resume`, `/help`, `/hotkeys`, `/quit`,
`/clear`, `/rename`, `/copy`, `/todo`, `/usage`, `/session`, `/tools`, `/context`, `/dequeue`.
Any other slash command is dispatched to the Hermes gateway, which owns the command catalog.

## Models hub (`/models`)

`/models` mounts omp's hub overlay: the provider sidebar and catalog come from `model.options`, and
the Roles view is backed by **Hermes** config over `model.roles.get` / `model.roles.set`
(`tui_gateway/methods_model_roles.py`). Each role is one `auxiliary.<task>` block:

- the `default` role is the live session model and assigns through `session.setModel` — `/model`
  stays the session-scoped picker;
- every other row is either an auxiliary task (`provider: "auto"` = follow the main model) or one of
  the two imported roles: `task` writes `delegation.*` and `advisor` writes `advisor.*`;
- assigning a row writes that route, the thinking column writes its effort
  (`auxiliary.<task>.reasoning_effort`; imported rows accept only `inherit` for `advisor`), and the
  fallback rows write `auxiliary.<task>.fallback_chain` / `delegation.fallback_providers`;
- presets are `model_presets.<name>` snapshots saved and re-applied from the hub (`model.presets.*`);
  the `ctrl+p` role cycle follows `omp.cycle_order` and falls back to the catalog when it is empty;
- a chain keyed by `provider/model` or `provider/*` (the hub's "+ New fallback chain") lands in
  `auxiliary.fallback_chains`, which the auxiliary resolver consults before the per-task chain;
- omp's `plan` / `smol` / `slow` / `widget` rows are ordinary auxiliary slots: assignable and
  visible here, `auto` until pinned, and without a built-in workload behind them yet;
- model-kind roles (`image`, `speech`, `dictation`, `web`) stay unset: their models live in `tts.*`,
  `stt.*` and the image/search provider registries, not in the chat catalog this hub browses.

## Settings / Appearance

The Appearance page's defaults are omp's own registry (`src/modes/settings.ts`), copied into
`../hermes_cli/data/omp_appearance_defaults.json` by `../scripts/gen_omp_appearance_defaults.py`
(regenerate after changing the registry; `src/modes/rpc/gateway-appearance.test.ts` gates the copy).
The profile's overrides live under `omp.appearance` in the Hermes `config.yaml` and are read/written
with `omp.appearance.get` / `omp.appearance.set`.

## Develop & verify

```bash
cd omp-ui
bun install
bun run check          # tsc --noEmit
bun run check:imports  # R1–R3/R5 dependency boundary
bun test src/modes/rpc # protocol + projection tests (58)
bun src/main.ts --version
bun src/main.ts        # pairs with the running Hermes gateway
```

`hermes omp` runs the same entry through the launcher. There is no local-agent fallback: without a
paired gateway the shell exits with `omp: cannot open the Hermes session …`.
