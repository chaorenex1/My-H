# Vendored dependencies of the omp shell

`omp-ui/src` is no longer a byte-identical upstream snapshot. The 2026-11-01 carve removed omp's
product code and kept only the rendering core, the Hermes gateway data-ingress layer and the RPC
protocol; the remaining `src/vendor/**` packages are the libraries those parts import.

## Dependency rule

`package.json` `dependencies` is exactly `@oh-my-pi/pi-tui` (renderer) and `@oh-my-pi/pi-natives`
(native addon). Every other `@oh-my-pi/*` specifier is rewritten to a relative path under
`src/vendor/<pkg>/`; `scripts/check-imports.ts` enforces this (R1–R3/R5) and that no relative import
leaves `src/` or fails to resolve. `tsconfig.json` `paths` map the vendored specifiers to the same
modules so typecheck and runtime share one identity.

There is **no sync script** any more: upstream refreshes would restore the deleted agent, so the
tree is maintained here.

## Vendored packages

| Vendor dir | Upstream | Why it stays |
|---|---|---|
| `vendor/ai/` | `packages/ai/src` | message/model/usage types, `AuthStorage` and the in-memory credential store the display catalog is constructed with |
| `vendor/agent/` | `packages/agent/src` | `AgentMessage`/`AgentEvent` vocabulary and the `Agent` message container the renderer walks (its turn loop is unused) |
| `vendor/catalog/` | `packages/catalog/src` | model identity/compat (`rules.json` + `rules.d.json.ts`), effort levels |
| `vendor/utils/` | `packages/utils/src` | dirs/env/logging/chalk/marked helpers, postmortem |
| `vendor/wire/` | `packages/wire/src` | `INTENT_FIELD` and the tool-wire text types the transcript renders |
| `vendor/omptype/` | `packages/omptype/src` | schema helpers used by `vendor/ai` |
| `vendor/snapcompact/` | `packages/snapcompact/src` | compaction summary helpers still imported by `vendor/agent` |

Removed with the carve: `vendor/mnemopi`, `vendor/stats`, `src/assets/**` (its package-level files were
only read by deleted code), the refresh script, and every upstream module outside the import closure
of the retained shell (telemetry exporters, providers, local backends).

## Brand patch (pi-tui)

The My-H fork replaces omp's visible brand marks in the renderer, which is installed rather than
vendored, so the edits ride a pinned Bun patch: `patches/@oh-my-pi%2Fpi-tui@18.14.1.patch`
(sources under `node_modules/@oh-my-pi/pi-tui`), registered in `package.json`
`patchedDependencies`. `bun install` — including the launcher's `bun install --production` —
applies it. The patch covers:

- `src/prompt/welcome.ts` — the welcome/setup lockup: the ✿ mark, the `My-H` wordmark, the vite
  gradient ladder, and the narrow-width fallback (wordmark before mark).
- `src/setup/scenes/splash.ts` — the compact splash's brand name.
- `src/theme/symbols.ts` — `icon.omp`: `✿` (unicode/nerdfont presets), `myh` (ascii).
- `src/overlays/session-selector.ts` + `dist/types/overlays/session-selector.d.ts` — the optional
  `SessionSelectorEntry.note` (ADR-16 P4): one dim state note beside date/size in the terminal row,
  as a muted badge in the native picker, and in its detail line. It renders product state
  (`archived` / `not mirrored`) without putting it in the user's title or first message. The type
  declaration ships separately because the package runs `src/*.ts` but typechecks
  `dist/types/*.d.ts`, so both files belong in the patch.

Bumping `@oh-my-pi/pi-tui` invalidates the patch (Bun refuses to apply it and the install fails
loudly): regenerate with `bun patch @oh-my-pi/pi-tui@<version>` → edit → `bun patch --commit
node_modules/@oh-my-pi/pi-tui`.

## Dev dependencies

`devDependencies` keeps only what the retained sources typecheck against: `@types/bun`,
`typescript`, and `@opentelemetry/api` (imported by `vendor/agent/telemetry.ts`). The transformers /
ONNX / exporter stack went with the deleted STT/TTS/tiny/telemetry paths.
