# Vendored oh-my-pi sources

`omp-ui/src` is a byte-identical snapshot of `packages/coding-agent/src` at
oh-my-pi `6a86df8539fccf3ad7818081266e554022fc21bc` (18.14.0, MIT — see `LICENSE`),
plus `src/vendor/**`: the closure of the non-renderer, non-native packages the shell imports.
`src/assets/**` carries the package-level files the shell imports.

Refresh with:

```bash
python omp-ui/scripts/sync-upstream.py        # OMP_SRC=<checkout> to point elsewhere
cd omp-ui && bun run check:imports && bun run check
```

The script copies the shell snapshot, rebuilds the vendor closure, rewrites every
specifier, copies the assets, and re-applies the patches below. It is idempotent.

## Dependency rule

`package.json` `dependencies` is exactly `@oh-my-pi/pi-tui` (renderer) and
`@oh-my-pi/pi-natives` (native addon). pi-natives publishes JavaScript sources plus a
platform `.node` through its `optionalDependencies`, and its `exports` map covers
`/path`, `/vcs`, `/clipboard`, `/desktop`, and the notices file, so the shell and
pi-tui import it by name. Every other `@oh-my-pi/*` specifier is rewritten to a
relative path under `src/vendor/<pkg>/`. `scripts/check-imports.ts` enforces this
(R1–R3/R5) and that no relative import leaves `src/` or fails to resolve.

pi-natives is pinned to the release that carries the symbols the pinned shell calls
(`FileLock.isHeld`; 18.14.0 does not export it), and pi-tui is bumped with it so both
resolve one package identity.

The tsconfig `paths` map points the vendored specifiers at the vendored files, so
pi-tui's internal `@oh-my-pi/*` imports resolve to the same modules as the shell's
(one identity for `const enum`s and classes) at typecheck time and at runtime under Bun.

## Layout

| Vendor dir | Upstream | Why |
|---|---|---|
| `vendor/utils/` | `packages/utils/src` | dirs/env/logging/chalk/marked/… |
| `vendor/ai/` | `packages/ai/src` | message + provider types, OAuth, usage |
| `vendor/agent/` | `packages/agent/src` | `Agent`, compaction, thinking |
| `vendor/catalog/` | `packages/catalog/src` | model identity/compat (`rules.json` + `rules.d.json.ts`) |
| `vendor/wire/` | `packages/wire/src` | tool wire types |
| `vendor/omptype/` | `packages/omptype/src` | schema/zod/typebox helpers |
| `vendor/stats/` | `packages/stats/src` | local stats store (client panel not vendored) |
| `vendor/snapcompact/` | `packages/snapcompact/src` | compaction |
| `vendor/mnemopi/` | `packages/mnemopi/src` | memory runtime |

Only files reachable from the shell (plus pi-tui's own peer imports, seeded in the
script) are copied; `*.d.json.ts` companions travel with their JSON. `pi-tui` and
`pi-natives` are not copied — they stay installed.

## Local patches

Applied by `scripts/sync-upstream.py` after each refresh:

1. `main.ts` — `main()` delegates to `modes/rpc/hermes-entry.ts` (`runHermesOmp`), which builds
   the Hermes gateway session and boots `InteractiveMode`; omp's `runCli`/`runRootCommand` launch
   graph, CLI command registry, profile flags, and prepaint dispatch are not on the `hermes omp`
   path. The entry module lives outside the snapshot (the script never rewrites it), so a refresh
   only has to restore this delegator. `src/cli.ts` stays in the tree only as the worker-host
   fallback that spawned workers re-enter.
2. R2 relocations — the shell's repo-root imports (`LICENSE`, `package.json`,
   `CHANGELOG.md`, `THIRD-PARTY-NOTICES.txt`) are pointed at `src/LICENSE` and
   `src/assets/`, which the script copies from the coding-agent package.

No other upstream file is edited; `git diff` against the pinned commit is empty for
everything under `src/` except the entry patch and the relocation sites.

## Dev dependencies

`devDependencies` mirror the coding-agent's optional/runtime packages at the versions
the upstream lockfile resolves (transformers, onnxruntime, fastembed, OpenTelemetry,
`@types/bun` 1.4.2, `typescript` 7.0.2). They exist so `tsc` typechecks the untouched
STT/TTS/tiny/telemetry files; they are not `dependencies` and the wired path does not
import them at runtime unless the user invokes those features.
