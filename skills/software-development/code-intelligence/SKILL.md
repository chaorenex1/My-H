---
name: code-intelligence
description: "AST search, codemod, parsing, SAST, and dataflow analysis."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ast-grep, tree-sitter, semgrep, codeql, static-analysis, ast, codemod, parsing, refactoring, security]
    category: software-development
    related_skills: [systematic-debugging, requesting-code-review, codebase-inspection]
---

# Code Intelligence Skill

Four tools answer four different questions about source code: what does this
syntax look like (`tree-sitter`), where does this shape occur and how do I
rewrite it (`ast-grep`), does this risky pattern occur (`semgrep`), and how does
data flow across files (`codeql`). Pick by the question, not by which tool is
familiar. This skill routes, gives safe first commands, and links one detailed
reference per tool.

## When to Use

Trigger this skill when the task is about code structure, not text:

| Signal | Tool | Trigger timing |
|---|---|---|
| Find every call, import, decorator, or syntactic shape; rename/migrate many sites | `ast-grep` | BEFORE editing repeated code or trusting a regex search |
| Need exact node kinds/fields, a parse tree, parse errors, or incremental edits | `tree-sitter` | BEFORE writing an ast-grep rule or tree-sitter query for an unfamiliar language |
| Find a bug class or risky API across a repo; enforce a custom lint; PR diff gate | `semgrep` | BEFORE claiming a pattern is safe or fixing one; in CI on every diff |
| Prove taint/dataflow across functions/files; deep CVE-class analysis; SARIF for GitHub | `codeql` | AFTER semgrep/ast-grep suggests a lead, or when the task is explicitly dataflow/security depth |
| Literal string, log, config value, filename | `search_files` | Never route to these tools |
| Symbol definition, reference, rename inside one repo | LSP tools | Never route to these tools |
| Runtime failure, wrong output, crash | `systematic-debugging` | Code scanning is not a debugger |

Escalation ladder (cheapest first): `search_files` → `ast-grep` (seconds) →
`tree-sitter` (seconds, structural detail) → `semgrep` (seconds-minutes) →
`codeql` (minutes-hours, build + database).

Use `tree-sitter` as a *support* tool: it tells you the grammar's node names so
your `ast-grep`/Semgrep/CodeQL pattern is precise. Do not use it as a semantic
analyzer; it has no types, scopes, or cross-file dataflow.

### Activation

Trigger the skill when the wording points at structure rather than text:
"find every call/import/decorator", "rewrite all uses", "rename this shape",
"show the parse tree", "why doesn't my query match", "add a lint rule",
"scan for this bug class", "track this input to a sink", or "CodeQL/CVE".
An explicit "use code-intelligence" also activates it.

Activate in this order:

1. **Gate.** If the task is literal text, symbol references, or runtime
   debugging, stop and use `search_files`, LSP, or `systematic-debugging`.
2. **Resolve.** Print the `CI_ROOT` path and check which tool binaries exist.
3. **Inventory.** Look in `codeql/*/`, `semgrep/*/*.yml`, and
   `tree-sitter/languages.yaml` for a reusable artifact before creating one.
4. **Read.** If the body is not loaded, call
   `skill_view("code-intelligence")`; then load the routed detail with
   `skill_view("code-intelligence", file_path="references/<tool>.md")`. State
   the chosen tool in one line before acting. Do not improvise rule syntax or
   CLI flags from memory.
5. **Start read-only.** Run the cheapest search/parse/scan first and keep the
   output bounded.

### After activation

- Record the question, chosen tool, command, and artifact path you reused or
  created; later steps should continue from that artifact, not start over.
- Keep every database, rule pack, and language profile under `CI_ROOT` using
  the layout above; do not leave reusable outputs in a temp directory.
- Before changing code, review the match set; after changing it, re-run the same
  query and confirm the old shape/finding is gone.
- Treat Semgrep/CodeQL findings as leads: open the cited line with `read_file`
  and confirm the condition or dataflow before reporting it.
- Delete only artifacts you created and only when they are stale or the user
  agrees; reuse wins over regeneration.

### Why this makes the agent better

The trigger table binds the question to the cheapest capable tool, so the agent
stops forcing regexes or jumping to CodeQL for a syntax lookup. The artifact
layout turns one-off scans into reusable databases, rules, and language
profiles, cutting repeated parsing and false zero-match rules. The read-only
first pass plus source verification keeps the agent from reporting plausible
findings as facts, and the escalation ladder keeps cost proportional to the
question.

## Prerequisites

Install only what the task needs; all four are optional PM packages:

```bash
hermes pm install ast-grep tree-sitter semgrep codeql
```

- `ast-grep`, `tree-sitter`, and `codeql` are pinned binary packages.
- `semgrep` is a Python distribution; PM builds `semgrep==<version>` in an
  isolated environment under `HERMES_HOME/environments/semgrep`.
- Platform gaps: Termux/bionic is a gap for all four. On Linux, ast-grep,
  tree-sitter, and CodeQL are glibc-only; Semgrep also publishes musllinux
  x86_64/aarch64 wheels. Semgrep has no macOS x86_64 or Windows ARM64 wheel;
  CodeQL is x64 on Windows ARM64 and Apple Silicon (host emulation), and its
  license restricts private-repo use.
- Semgrep is host-built: `hermes pm install semgrep` on the target, not a
  cross-target `--target` stage.

If a command is missing, run `hermes pm doctor` and check the tool's gap in
`pm/code_intelligence_packages.py`. Do not fall back to a half-installed binary.

## Artifact Layout

Persist every database, rule, and language parse file under one root so later
sessions can reuse it. `CI_ROOT` below is `HERMES_HOME/code-intelligence`:

```text
$CI_ROOT/
├── codeql/
│   ├── <database-name>/            # one CodeQL database directory
│   └── results/<database-name>.sarif
├── semgrep/
│   ├── <language>/
│   │   └── <rule-pack>.yml         # rules for that Semgrep language
│   ├── shared/
│   │   └── <rule-pack>.yml         # rules spanning multiple languages
│   └── results/<language>/<rule-pack>.json
└── tree-sitter/
    ├── languages.yaml              # ADR-8-aligned Language Registry
    ├── queries/<language>/<purpose>.scm
    └── parsers/<language>/parser.{so,dll,dylib,wasm}
```

Resolve the root once per session; `HERMES_HOME` may be unset:

```bash
python -c "from pathlib import Path; from hermes_constants import get_hermes_home; print(Path(get_hermes_home()) / 'code-intelligence')"
```

Treat that output as `CI_ROOT` and use `$CI_ROOT/...` in commands. Create only
the subdirectories the task needs. Rules for reuse:

- **Look before creating.** Check `languages.yaml`, `semgrep/*/*.yml`, and
  `codeql/*/` for a compatible artifact first; reuse it when the language,
  grammar/parser version, rule id, or source revision still matches.
- **Name for reuse.** Database names `[a-z0-9-]+` (for example
  `myrepo-python-abc1234`); rule packs `[a-z0-9][a-z0-9._-]*`; language ids are
  the registry's `language`.
- **Semgrep rules are language-scoped.** Store a rule pack at
  `$CI_ROOT/semgrep/<language>/<rule-pack>.yml`, where `<language>` is the Semgrep
  `languages` key (for example `python`, `typescript`, `go`). A pack in a
  language directory contains only rules for that language; if any rule spans
  multiple languages, put the pack in `semgrep/shared/`. Never put `.yml` files
  directly at the `semgrep/` root.
- **One language, one descriptor.** In `tree-sitter/languages.yaml`, `language`
  is the unique primary key; `aliases`, `extensions`, and `filenames` are
  globally unique lookup keys, and `queries/<language>/` / `parsers/<language>/`
  use the same id. Never merge languages or add a generic fallback entry.
- **CodeQL databases are immutable.** Never write into an existing database
  directory; update by creating a new directory (and deleting the old one only
  when the user agrees), then write results to `codeql/results/`.
- **Rules and the registry are editable.** Rewrite the whole YAML atomically
  (temporary file, then replace) and validate it before use. Bump
  `parser_version` when a grammar or query changes.
- **Delete narrowly.** Only remove artifacts this skill created under
  `CI_ROOT`; remove a language's query/parser files or rule pack only when no
  registry entry or other rule references them. Never delete the source tree or
  the shared `CI_ROOT` root.

See `references/language-registry.md` for the registry schema, validation, and
create/update/delete procedure. `tree-sitter` itself never reads
`languages.yaml`: the registry is the agent's source of truth, translated into
`-p/--grammar-path`, `--lib-path`, and `.scm` query paths for the CLI.

## How to Run

Run CLI commands through the `terminal` tool. Preflight once per session:

```bash
ast-grep --version
tree-sitter --version
semgrep --version
codeql version
```

Read the matching reference with `skill_view` before the first real invocation:

| Task | Read first |
|---|---|
| Structural search/rewrite, custom lint rules | `references/ast-grep.md` |
| Parse trees, S-expression queries, grammatical node kinds | `references/tree-sitter.md` |
| Pattern-based SAST, custom security/style rules, CI gate | `references/semgrep.md` |
| Databases, QL queries, dataflow, SARIF | `references/codeql.md` |
| Language Registry file, query/parser artifacts, validation | `references/language-registry.md` |

`SKILL_DIR` is the directory containing this `SKILL.md`. Both Semgrep and CodeQL
emit SARIF; summarize either with:

```bash
python SKILL_DIR/scripts/sarif_summary.py results.sarif
python SKILL_DIR/scripts/sarif_summary.py results.sarif --json
```

## Quick Reference

| Goal | First command |
|---|---|
| Search a syntax shape | `ast-grep -p '$A && $A()' -l ts src` |
| Machine-readable search | `ast-grep -p 'console.log($$$A)' -l js --json=compact src` |
| Safe rewrite (review first) | `ast-grep -p '$P && $P()' -l ts --json=compact src`, then add `-r '$P?.()' -U` |
| Print a parse tree | `tree-sitter parse src/file.py --stat` |
| Run an S-expression query | `tree-sitter query "$CI_ROOT/tree-sitter/queries/python/calls.scm" src/` |
| Validate the Language Registry | `python SKILL_DIR/scripts/validate_language_registry.py "$CI_ROOT/tree-sitter/languages.yaml"` |
| Default Semgrep scan | `semgrep scan --config auto --metrics=off .` |
| Custom/local rules | `semgrep scan --config "$CI_ROOT/semgrep/python/myrules.yml" --json-output="$CI_ROOT/semgrep/results/python/myrules.json" .` |
| CI gate on findings | `semgrep scan --config p/default --error --metrics=off .` |
| Create a CodeQL database | `codeql database create "$CI_ROOT/codeql/myrepo-python" --language=python --source-root=.` |
| Standard CodeQL analysis | `codeql database analyze "$CI_ROOT/codeql/myrepo-python" --format=sarif-latest --output="$CI_ROOT/codeql/results/myrepo-python.sarif"` |
| One CodeQL query | `codeql database analyze "$CI_ROOT/codeql/myrepo-python" codeql/python-queries:Functions --format=sarif-latest --output="$CI_ROOT/codeql/results/myrepo-python.sarif"` |

## Procedure

1. **Resolve and inventory.** Resolve `CI_ROOT`; list existing `codeql/`,
   `semgrep/`, and `tree-sitter/` artifacts. Reuse a matching database, rule pack,
   or language entry before creating anything new.
2. **Classify** with the table above and choose the cheapest tool that can
   answer the question. If the question mentions dataflow, cross-file taint, or
   a CVE class, start at Semgrep and escalate to CodeQL.
3. **Establish the grammar.** For an unfamiliar language or an unfamiliar node,
   run `tree-sitter parse` and/or `--debug-query=ast` with ast-grep before
   composing a rule. Record the language and grammar version in
   `tree-sitter/languages.yaml`; guessing node names is the main source of
   silent zero-match rules.
4. **Search before changing.** Always run the read-only form first. Keep output
   bounded: target a directory, use `--globs`/`--exclude`, and prefer
   `--json=compact` for parsing.
5. **Rewrite in two passes.** Review matches and a dry-run diff, then apply with
   `-U` (all) or `-i` (selective). Run the project's tests and a follow-up
   search to prove the old shape is gone.
6. **Verify findings.** A Semgrep/CodeQL result is a lead, not proof. Open the
   cited file and line with `read_file`; confirm the dataflow or condition
   yourself before reporting it.
7. **Close the loop.** Re-run the same query after the fix. For security work,
   re-run Semgrep and the relevant CodeQL query; report the exact command,
   rule/query id, and remaining findings. Keep the reused artifacts in place.

## Pitfalls

- **ast-grep patterns are code, not regex.** Quote them with single quotes so the
  shell keeps `$` metavariables. A pattern must parse as standalone code; use a
  `pattern: {context, selector}` rule for partial constructs. On Linux, `sg` may
  be `setgroups` — always invoke `ast-grep`.
- **ast-grep rewrite is off by default.** `-r` without `-U` or `-i` does not
  write files. Never apply a rewrite without a reviewed JSON/diff pass.
- **tree-sitter is syntax-only.** Query node names and fields come from the
  installed grammar version; the same query can silently match nothing on
  another grammar. `(ERROR)`/`(MISSING)` nodes mean syntax problems, not bugs.
- **Semgrep `--config auto` fetches the registry** and logs the project URL;
  use `--metrics=off` for privacy and a pinned local rule file for
  reproducibility. `semgrep ci` requires a Semgrep account; use
  `semgrep scan` for local work.
- **Semgrep is not a type checker.** Aliases, wrappers, and generated code cause
  false negatives. Combine `pattern-inside`, `metavariable-*`, and human review.
- **CodeQL needs a database and a build.** Interpreted languages need their
  runtime; compiled languages need a working build (`--command`) or a supported
  `--build-mode`. Passing `--command` for Python/JS/TS creates an empty database.
- **CodeQL bundle size/license.** It is ~0.7-1.3 GB, excluded from native
  payload bundles, and free only for public repos unless you hold GitHub Code
  Security. Do not install it for a quick grep.
- **Scan scope.** All tools honor ignore files to different degrees. Exclude
  `node_modules`, vendored trees, build output, and generated code explicitly,
  or you will report noise.
- **Keep artifacts under `CI_ROOT`.** Do not scatter databases, rules, or
  registry files in the repository or a temp directory; later sessions reuse
  only the stable paths in the Artifact Layout.
- **CodeQL databases are not editable.** `database create` refuses an existing
  directory; never delete and recreate one mid-analysis. Create a new database
  for a new revision and keep the old one until the user agrees to remove it.
- **Registry writes are whole-file.** Validate `languages.yaml` before writing
  and replace it atomically; a partial write or a bad entry disables every
  language. Reject paths that escape `tree-sitter/` (no absolute paths, no
  `..`).
- **Rule files are executable policy.** Review a Semgrep rule before scanning
  with it, especially one fetched from elsewhere; keep local rules under
  `semgrep/` and do not embed credentials in them.

## Code graph (`code_intel` / `hermes code`)

The persistent code graph answers cross-file questions that text search cannot:
callers/callees, call paths, type hierarchy, module dependencies, cycles,
impact, static tests, coverage and change history. Prefer it over re-parsing
files by hand when the repository is already indexed.

| Question | Use |
|---|---|
| Who calls this function? What does it call? | `code_intel` action `callers` / `callees` (or `hermes code callers/callees`) |
| What breaks if I change this? | `code_intel` action `impact`, `policy="strict"` |
| Which tests cover this symbol? | `tests` (static) then `coverage` (runtime, per-test contexts only) |
| Why is this module in the build? | `dependencies` / `cycles` |
| What changed between revisions? | `changes` |
| Give me a compact, traceable context block | `context` |

- If the tool returns `not_indexed`, ask the user to run `hermes code index`
  (or run it when you own a terminal). Do not claim graph facts before indexing.
- Read the envelope: `index_status`, `snapshot_hash`, `stale`, `freshness`,
  `truncated`, `unresolved_count`, `coverage_available`. `strict` rejects stale
  or partial indexes; `balanced` marks inferred edges; `exploratory` returns
  ambiguous candidates and unresolved evidence.
- The model is not a fact source. `code_graph_update` only accepts evidence
  from an analysis tool with a matching span and `excerpt_hash`; never invent
  `source_tool="model_observation"`.
- `refresh_file` is the authoritative single-file rebuild. Use it after an edit
  when the user needs the next query to see the new facts.
- The graph stores paths, line spans and hashes, not source text. `context`
  reads the current file only when its hash still matches the index.
- Details and failure modes: `references/code-graph.md`.

## Verification

- `ast-grep`: the pre-change match count matches the post-change diff; the old
  pattern returns zero matches; project tests pass.
- `tree-sitter`: `tree-sitter parse` exits 0 with no `(ERROR)`/`(MISSING)` in the
  queried region; query captures match the nodes you expected.
- `semgrep`: the scan completes, findings are reviewed against source lines, and
  a re-run after the fix reports the expected reduction. Use `--error` only when
  a non-zero exit is the intended gate.
- `codeql`: `codeql database analyze` exits 0, SARIF parses with
  `sarif_summary.py`, every reported location is confirmed with `read_file`, and
  the targeted query is re-run after the fix with a clean database.
- Language Registry and reuse: the registry validator from the Quick Reference
  exits 0, and every database, rule pack, query, and parser you created or
  reused remains under `CI_ROOT` after each create/update/delete.
