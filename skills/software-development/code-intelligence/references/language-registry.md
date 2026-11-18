# Language Registry and persistent artifacts

This reference defines where agent-created artifacts live and the YAML format
used for the agent-managed tree-sitter profile store. The descriptor vocabulary
follows ADR-8 D6; the packaged
`hermes_codegraph/adapters/languages/languages.yaml` remains the code-graph
registry loaded by `LanguageRegistry`.

## Artifact root

`CI_ROOT` is `HERMES_HOME/code-intelligence`:

```text
$CI_ROOT/
├── codeql/
│   ├── <database-name>/
│   └── results/<database-name>.sarif
├── semgrep/
│   ├── <language>/<rule-pack>.yml
│   ├── shared/<rule-pack>.yml
│   └── results/<language>/<rule-pack>.json
└── tree-sitter/
    ├── languages.yaml
    ├── queries/<language>/<purpose>.scm
    └── parsers/<language>/parser.{so,dll,dylib,wasm}
```

Resolve `HERMES_HOME` with:

```bash
python -c "from pathlib import Path; from hermes_constants import get_hermes_home; print(Path(get_hermes_home()) / 'code-intelligence')"
```

Use that path as `CI_ROOT`. Names are stable and lower-case; create a
subdirectory only when the task needs it. Semgrep rule packs and tree-sitter
query/parser files are language-scoped: `semgrep/<language>/` and
`tree-sitter/queries/<language>/`; Semgrep rules that span languages live in
`semgrep/shared/`.

## `tree-sitter/languages.yaml`

One file holds every reusable tree-sitter language profile. Like the ADR-8
registry, it is one file rather than one file per language, and it is validated
as a whole.

```yaml
schema_version: 1
languages:
  - language: python
    aliases: [py]
    extensions: [.py, .pyi]
    filenames: []
    grammar_package: tree-sitter-python
    grammar_version: 0.25.0
    grammar_install: builtin
    parser: parsers/python/parser.so
    tree_sitter_queries:
      symbols: queries/python/symbols.scm
      imports: queries/python/imports.scm
      calls: queries/python/calls.scm
      types: queries/python/types.scm
      tests: queries/python/tests.scm
    kind_map:
      function_definition: function
      class_definition: class
    capabilities:
      symbols: true
      references: true
      calls: true
      types: true
      inheritance: true
      tests: true
      change: true
    parser_version: "0.25.0+1"
```

### Top level

| Key | Type | Required | Rule |
|---|---|---|---|
| `schema_version` | integer | yes | Must be `1`. |
| `languages` | list | yes | Non-empty; one entry per language. |

Unknown top-level keys are invalid. Validation is all-or-nothing: one bad entry
rejects the whole file.

### Language descriptor

| Key | Type | Required | Meaning |
|---|---|---|---|
| `language` | string | yes | Unique id, `[a-z0-9][a-z0-9-]*` (for example `python`, `typescript`). |
| `aliases` | list[str] | yes | Short names (`py`); unique across the registry. |
| `extensions` | list[str] | yes | Extensions including the dot; unique across the registry. |
| `filenames` | list[str] | yes | Exact filenames such as `Dockerfile`; unique across the registry; use `[]` when none. |
| `grammar_package` | string | yes | Python grammar package, for example `tree-sitter-python`. |
| `grammar_version` | string | yes | Exact grammar version used by the queries. |
| `grammar_install` | string | yes | `builtin` or `on_demand:<extra>` (ADR-8 install policy). |
| `parser` | string | no | Relative path to a built parser (`.so`, `.dylib`, `.dll`, or `.wasm`). |
| `tree_sitter_queries` | mapping | yes | Purpose → relative `.scm` path; at least one entry. |
| `kind_map` | mapping | no | Grammar node kind → ACIE kind. |
| `container_rules` | mapping | no | ADR-8 nested-scope rules. |
| `visibility_rules` | mapping | no | ADR-8 visibility rules. |
| `signature_rules` | mapping | no | ADR-8 signature rules. |
| `module_naming` | mapping | no | ADR-8 module naming rules. |
| `import_resolution` | mapping | no | ADR-8 import resolution rules. |
| `test_detection` | mapping | no | ADR-8 test detection rules. |
| `extraction` | mapping | no | Grammar profile consumed by the single code-graph extractor (node tables, field names, modes). Optional here because this store is CLI-only; the code graph requires it to index a language. |
| `semantic_provider` | string/null | no | `agent/lsp` server id for semantic enrichment. |
| `capabilities` | mapping | yes | Boolean capability flags; see below. |
| `parser_version` | string | yes | Grammar + adapter revision; bump when either changes. |

`tree_sitter_queries` keys are `symbols`, `imports`, `calls`, `types`, and
`tests`. Values are paths relative to `tree-sitter/`; absolute paths and `..`
are rejected. A referenced query must exist and be non-empty.

`capabilities` accepts `symbols`, `references`, `calls`, `types`, `inheritance`,
`tests`, and `change`. Missing keys mean `false`; do not infer support.

`parser_version` is the cache key. Reuse the stored queries/parser only while
the grammar version and `parser_version` match; bump it when a query or grammar
changes.

### Distinguishing languages

`language` is the primary key for an entry, and one language has exactly one
descriptor:

- `language` ids are unique and lower-case (`python`, `typescript`, `go`,
  `rust`). Do not add a generic `default` language or merge two languages into
  one entry.
- `aliases`, `extensions`, and `filenames` are lookup shortcuts, but they are
  globally unique: `.ts` or `Dockerfile` cannot point at two languages.
- Per-language files are namespaced by the same id:
  `queries/<language>/...` and `parsers/<language>/...`.
- Duplicate `language`, alias, extension, or filename values reject the whole
  file; the validator does not pick a winner.

A second language is a second list item, not a second file:

```yaml
languages:
  - language: python
    aliases: [py]
    extensions: [.py, .pyi]
    # ...
  - language: typescript
    aliases: [ts, tsx]
    extensions: [.ts, .tsx]
    # ...
```

### Relationship to ADR-8

The packaged `hermes_codegraph/adapters/languages/languages.yaml` is
authoritative for the code graph and is loaded by `LanguageRegistry`. The
`HERMES_HOME/code-intelligence/tree-sitter/languages.yaml` file is the agent's
reusable tree-sitter parse-profile store: it is not that registry, not a copy of
it, and not a bootstrap template copied on first use. It deliberately reuses
ADR-8 descriptor vocabulary so profile facts can be reconciled by `language`;
when both exist, prefer the packaged registry's grammar facts and keep this file
limited to the tree-sitter queries, parser, kind map, and capabilities the skill
uses.

## Query and parser files

```text
tree-sitter/
├── languages.yaml
├── queries/python/symbols.scm
├── queries/python/imports.scm
├── queries/python/calls.scm
├── queries/python/types.scm
├── queries/python/tests.scm
└── parsers/python/parser.so
```

Each `.scm` file is a complete tree-sitter query for that purpose, with its own
captures (`@name`, `@definition.function`, and so on). Keep queries small and
purpose-specific; do not share a generic fallback between languages. Build a
reusable parser with:

```bash
tree-sitter build -o "$CI_ROOT/tree-sitter/parsers/python/parser.so" path/to/tree-sitter-python
```

Use the platform suffix (`.dll` on Windows, `.dylib` on macOS). Use it with:

```bash
tree-sitter parse --lib-path "$CI_ROOT/tree-sitter/parsers/python/parser.so" src/file.py
tree-sitter query --lib-path "$CI_ROOT/tree-sitter/parsers/python/parser.so" "$CI_ROOT/tree-sitter/queries/python/calls.scm" src/
```

Without a stored parser, use `-p/--grammar-path` to the grammar checkout; the
registry still records which grammar version the queries were written for.

## Validate

```bash
python SKILL_DIR/scripts/validate_language_registry.py "$CI_ROOT/tree-sitter/languages.yaml"
```

The validator is the contract: duplicate ids/aliases/extensions, unknown keys,
missing required keys, wrong types, escaping paths, and missing query/parser
files all fail the whole file. It prints `OK` with the language and file count.
A copy is available at `templates/language-registry.yaml`.

## Create, update, delete, reuse

**Create**

1. Resolve `CI_ROOT` and check for an existing entry by `language`, alias, or
   extension.
2. Create `tree-sitter/queries/<language>/` and, if building, `parsers/`.
3. Write the queries, build the parser, then add one descriptor to
   `languages.yaml`.
4. Validate; use the artifacts only after the validator exits 0.

**Update**

1. Read the whole file and change the target descriptor.
2. Bump `parser_version` when the grammar, parser, or a query changes.
3. Write to a temporary file and replace `languages.yaml` atomically.
4. Validate again; never leave a partially written registry.

**Delete**

1. Remove the descriptor, keeping `schema_version` and a valid `languages` list.
2. Validate.
3. Delete the language's query/parser files only if no remaining descriptor
   references them; otherwise leave them.
4. Never delete the `tree-sitter/` root or another tool's artifacts.

**Reuse**

Look up by `language`, alias, or extension before creating anything. Reuse a
descriptor and its files when `grammar_version` and `parser_version` still
match the current task; otherwise regenerate and bump the version. Keep the
registry in place between sessions so later work pays the parsing cost once.
