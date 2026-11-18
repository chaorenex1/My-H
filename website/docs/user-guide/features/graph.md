---
title: Graph
description: Local property graph storage and algorithms — the optional [graph] extra and the hermes graph CLI.
---

# Graph

Hermes can keep a local property graph per profile, backed by
[LadybugDB](https://docs.ladybugdb.com/) with [NetworkX](https://networkx.org/)
for analysis. It is **opt-in infrastructure**, not part of the default request
path: no model tool is registered, no hook runs, and nothing is imported at
startup.

## Install

```bash
hermes pm install --extra graph
```

The extra pins `ladybug` and `networkx` exactly. Ladybug ships native wheels for
Windows (x64/arm64), macOS 15+, and Linux (glibc/musl); Android/Termux is not
supported. `hermes graph status` only *probes* the extra — it never installs it. Its
`lock` field is `available`, `locked`, `not-applicable`, or `unknown`; `writable`
says whether a writer could open now, even though the status probe itself is
read-only.
Data commands install on first use through PM, or report the install command.

## Storage and isolation

- One database per profile: `<HERMES_HOME>/graph/graph.lbdb`.
- The path follows the active profile; it is not configurable in v1.
- One **writer** per database file, process-wide. A second writer gets
  `GraphLockedError` with remediation instead of a silent second database.
- Node and edge content stays local. Nothing is uploaded or sent to a model.
- `graph.admin.backup()` checkpoints, closes the database, copies the file, and
  reopens it. It never copies a live file.

## CLI

```bash
hermes graph status      # path, counts, scope versions, writer lock state
hermes graph schema register --file schema.json
hermes graph schema list

hermes graph node upsert Entity entity:python --file props.json
hermes graph node get <id>
hermes graph node search Entity --filter entity_type=Language --limit 20
hermes graph node delete <id>            # soft delete
hermes graph node delete <id> --hard --yes

hermes graph edge create --source <id> --target <id> --type MENTIONS --property confidence=0.9
hermes graph edge list --source <id> --type MENTIONS
hermes graph edge delete <id>

hermes graph query 'MATCH (n:Memory) WHERE n.memory_type = $kind RETURN n.id AS id' --param kind=semantic
hermes graph neighbors <id> --depth 2 --direction both
hermes graph path <source-id> <target-id> --weight confidence
hermes graph analyze --method pagerank --scope knowledge
hermes graph snapshot --scope knowledge
hermes graph backup --dest ~/graph-backup.lbdb
hermes graph doctor
```

`--property`, `--filter`, and `--param` values are parsed as JSON scalars, so
`--property confidence=0.9` writes a number and `--property name=Python` writes a
string. Quote a value that only looks numeric (`--property code='"007"'`).

Every command writes JSON to stdout and errors as JSON to stderr with a
non-zero exit code. `hermes graph query` is read-only: only `MATCH`,
`OPTIONAL MATCH`, `UNWIND`, `WITH`, and `RETURN` statements are accepted, values
must be parameters (`$name`), and write/DDL keywords, comments, and multiple
statements are rejected. Writes go through the typed node/edge commands so the
internal id catalog, scope versions, and schema checks stay consistent.

## Schema

Types must be registered before use. `schema.json`:

```json
{
  "nodes": [
    {"kind": "node", "name": "Memory", "scope": "knowledge",
     "fields": {"content": "STRING", "memory_type": "STRING"}},
    {"kind": "node", "name": "Entity", "scope": "knowledge",
     "fields": {"name": "STRING", "entity_type": "STRING"}}
  ],
  "edges": [
    {"kind": "edge", "name": "MENTIONS", "source": "Memory", "target": "Entity",
     "scope": "knowledge", "fields": {"confidence": "DOUBLE"}}
  ]
}
```

Field types: `STRING`, `INT64`, `DOUBLE`, `BOOL`. Node types are `PascalCase`,
edge types `UPPER_SNAKE_CASE`. v1 indexes the primary key only: exact `id`/`(type, key)`
lookups are indexed, property filters scan that type's table. Only additive migrations are allowed: adding new
nullable fields or new types. Dropping/renaming/retyping a field or changing an
edge's endpoints is rejected.

## Python API

```python
from hermes_graph import GraphClient

with GraphClient.open() as graph:
    node = graph.nodes.upsert(
        type="Entity", key="entity:python",
        properties={"name": "Python", "entity_type": "Language"},
    )
    result = graph.query.find("Entity", {"entity_type": "Language"})
    distances = graph.algorithms.centrality("pagerank", scope="knowledge")
```

Identity rules:

- `create()` generates a UUIDv7 (time-ordered).
- `upsert(type, key)` derives a UUIDv5 from `(type, key)`, so repeating it
  updates the same node.
- `Node.key` is the business identity; `Node.id` is the storage identity.
- Soft delete (`hard=False`) stamps `deleted_at` and removes adjacency edges.
  Re-running `upsert(type, key)` revives that same id. Hard delete removes the
  row, its edges, and its id-catalog entry; `upsert` then recreates the same
  derived id.

Snapshots are derived from a scope version (`GraphScopeVersion`). A committed
write transaction bumps each affected scope exactly once; a rolled-back one
bumps nothing. NetworkX graphs and algorithm results are cache/compute artifacts
and are never written back to Ladybug.

## Limits

Defaults: 256 KiB per property map, 100k nodes / 500k edges / 256 MiB per
snapshot, 100 rows / 1 MiB / 2 s per Cypher query, 5 s write-lock wait. The SDK
accepts a `GraphLimits` instance at `GraphClient.open(limits=...)`; the CLI uses
the defaults.

## Not in v1

Memory integration, code graphs, Advisor, `agent/learning_graph.py`, model tools,
cross-process write proxies, FTS/vector search, and automatic LLM extraction are
separate decisions (see `docs/adr/ADR-7-graph-component.md`).
