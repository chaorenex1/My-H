# Code graph reference

The code graph is a persistent projection of Tree-sitter, LSP and analysis
results. It lives in the active profile's `graph/graph.lbdb`, not in the
repository, and is queried through `code_intel` or `hermes code`.

## Tools

| Surface | Use | Writes |
|---|---|---|
| `code_intel` | read-only queries and `status` | never |
| `code_graph_update` | `refresh_file`, `apply_evidence`, `invalidate` | yes, one transaction |
| `hermes code ...` | CLI, JSON output | only `index`, `coverage import`, `prune` |

`code_intel` actions: `status`, `find_symbols`, `get_symbol`,
`get_definition`, `get_references`, `callers`, `callees`, `path`, `types`,
`implementations`, `dependencies`, `cycles`, `impact`, `tests`, `coverage`,
`diagnostics`, `changes`, `context`.

## Envelope fields that change your answer

- `index_status`: `complete` or `patched` are usable; `partial`, `indexing`,
  `stale`, `missing` are not.
- `stale` / `freshness`: `exact` means the indexed commit equals HEAD;
  `head_changed` means the graph describes an older commit; `dirty_unknown`
  means the worktree changed during indexing.
- `snapshot_hash`: absent unless `complete`/`patched`.
- `truncated`: a depth, node or result cap was hit; do not present the partial
  list as complete.
- `unresolved_count`: call/reference expressions that did not resolve to a
  target. They are evidence, not edges.
- `coverage_available`: distinguishes "not imported" from "no coverage for this
  symbol".

## Confidence policy

`strict` returns only `resolved` and `verified` (coverage). `balanced` also
returns `inferred`, with the status visible. `exploratory` also returns
`ambiguous` candidates and unresolved evidence. `invalidated` is never returned.

## Update rules

1. `refresh_file(path)` re-parses one file, rebuilds its graph facts and runs
   the resolver. This is the default after an edit.
2. `apply_evidence` requires `expected_revision_id`, `expected_content_hash`,
   a non-empty `source_tool`, a span inside the file and an `excerpt_hash` that
   matches the current bytes. `model_observation` is always rejected.
3. Priority is `verified > resolved > inferred > ambiguous`. A lower-priority
   write against a higher-priority edge is reported as a conflict and the edge
   becomes `ambiguous`; it is not overwritten.
4. `invalidate(ids)` retires evidence or an edge. An edge with no remaining
   valid evidence is deleted; symbol nodes are never deleted by this action.
5. `patched` files keep their previous `indexed_hash`; the next full `index` or
   `refresh_file` re-parses them and reclaims the patch.

## Failure modes

| Symptom | Meaning | Next step |
|---|---|---|
| `not_indexed` | no `CodeRepository` for this root | run `hermes code index` |
| `GraphLockedError` | another process holds the write lock | retry after it finishes; do not open a second writer |
| `GraphConflictError` | revision/content hash changed | re-read the file and `refresh_file` |
| `EvidenceRejectedError` | bad tool, span, hash or target | fix the observation; nothing was written |
| `CoverageContextMissingError` | aggregate coverage report | use coverage.py contexts or pytest-cov `--cov-context=test` |
| `RevisionMismatchError` | coverage/report targets another revision | re-index the current revision or pass the matching revision |
| `IndexStaleError` (strict policy) | index is not current | re-index before relying on it |
| `unavailable` in metadata | algorithm needs NetworkX | read-only queries still work; install the `graph` extra |

## Safety

The graph never stores source text, only paths, spans and hashes. Secrets
(`.env`, private keys, credentials) are excluded from scanning. `parser` paths
in a language descriptor are metadata only and are never loaded. LSP semantic
indexing is opt-in because servers may run project build scripts.
