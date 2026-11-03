---
title: Code Intelligence
description: Persistent, evidence-backed code graph for symbols, calls, types, dependencies, tests and changes.
---

# Code Intelligence

Hermes can index a repository into a persistent code graph and answer
cross-file questions without re-reading files on every turn: who calls a
function, what a change would affect, which tests exercise a symbol, how
modules depend on each other, and what changed between revisions.

The graph is local to the active profile (`graph/graph.lbdb`), never uploads
code, and never executes the indexed repository's code during syntax indexing.

## Install

```bash
hermes pm install --extra graph
hermes pm install --extra codegraph
```

`[codegraph]` carries the four built-in grammars (Python, TypeScript/TSX, Go,
Rust) and is atomic: if a platform is missing one wheel, the extra is reported
unavailable instead of partially installing. `hermes code status` and
`hermes code doctor` show the platform target and the exact install hint.

## Index

```bash
hermes code index            # incremental: only changed files are parsed
hermes code index --full     # ignore content hashes
hermes code index --semantic # opt-in LSP enrichment (may start language servers)
```

Parsing failures keep the last successful facts and mark the file `stale`; a
file that did not change since the last commit is not re-parsed. An interrupted
run leaves committed batches valid and the rest to the next run.

## Query

```bash
hermes code find helper
hermes code callers sym_...
hermes code callees sym_... --depth 2
hermes code path sym_a sym_b
hermes code types sym_...
hermes code impact sym_... --policy strict
hermes code deps pkg.module
hermes code cycles
hermes code tests sym_...
hermes code context sym_... --budget-tokens 6000
```

Every command prints JSON. Results carry `revision_id`, `snapshot_hash`,
`index_status`, `stale`, `freshness`, `truncated`, `unresolved_count` and
`coverage_available`. `strict` returns only resolved facts (and verified
coverage); `balanced` also returns inferred edges; `exploratory` also returns
ambiguous candidates and unresolved evidence.

## Model tools

When the extras are installed, the default tool surface includes:

- `code_intel` — read-only queries (same envelope as the CLI). It never writes.
- `code_graph_update` — `refresh_file`, `apply_evidence`, `invalidate`. Facts
  must come from an analysis tool and carry a span plus `excerpt_hash` that
  match the current file; `source_tool="model_observation"` is rejected.

`code_intel.enabled` is the master switch; `code_intel.update` hides only the
write tool. If extras are installed mid-session, the tools appear after an
explicit reload (`/reload-mcp` or restart), not merely at the next turn.

## Tests and coverage

Static test association comes from test files and call sites (`TESTS`).
Runtime coverage needs per-test contexts:

```bash
coverage json --show-contexts -o coverage.json
hermes code coverage import coverage.json --revision <commit>
hermes code coverage show sym_...
```

Aggregated lcov/c8 reports cannot attribute a line to a test case and are
rejected instead of producing fake `COVERS` edges.

## Configuration

```yaml
code_intel:
  enabled: true
  update: true
  auto_update: true
  semantic: false
  semgrep:
    enabled: false
    mode: auto          # auto | custom_dir
  languages: []         # extra language ids; built-ins always enabled
  max_file_bytes: 1048576
  max_files: 20000
  max_total_bytes: 134217728
  max_symbols: 1000000
  exclude: []
```

`auto_update` refreshes files changed by a turn after the final answer, in the
background, single-flight per profile/workspace. It never blocks the answer and
never writes change-history edges for a dirty worktree. One-shot/cron sessions
use a bounded synchronous flush instead, because the process exits immediately.

`semantic: true` reuses `agent/lsp` and can execute project build scripts
through language servers; keep it off for untrusted or offline repositories.
`semgrep.enabled` adds dangerous-pattern diagnostics only; it never creates
symbol or call facts.

## Safety

- Secrets (`.env`, private keys, credentials) are always excluded.
- Binary files, symlinks escaping the repository and non-regular files are
  skipped.
- The graph stores paths, spans and hashes, not source text.
- Repository remotes are sanitized before entering the graph.
- `refresh_file` and `apply_evidence` use one transaction; a conflict or bad
  evidence writes nothing.
