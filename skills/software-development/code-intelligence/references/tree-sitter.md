# tree-sitter — parsing and structural queries

Official documentation:

- Book: https://tree-sitter.github.io/tree-sitter/
- CLI overview: https://tree-sitter.github.io/tree-sitter/cli/index.html
- `parse` command: https://tree-sitter.github.io/tree-sitter/cli/parse.html
- `query` command: https://tree-sitter.github.io/tree-sitter/cli/query.html
- Query syntax: https://tree-sitter.github.io/tree-sitter/using-parsers/queries/1-syntax.html
- `build` command: https://tree-sitter.github.io/tree-sitter/cli/build.html
- Parser tests: https://tree-sitter.github.io/tree-sitter/cli/test.html

The `tree-sitter` CLI parses source with a language grammar and lets you query
the resulting syntax tree. It is the tool for *structure discovery*: exact node
kinds, field names, and parse errors. It has no type system, scope resolution, or
cross-file dataflow.

## Install and grammar setup

```bash
hermes pm install tree-sitter
tree-sitter --version
```

A parser must be available for the target language. Tree-sitter discovers
grammars through the parser directories in its config:

```bash
tree-sitter init-config
```

Point the generated `config.json` at cloned grammar repositories, or pass
`-p/--grammar-path` to a grammar directory. Inside a grammar repository:

```bash
tree-sitter generate
tree-sitter build
tree-sitter parse test/fixtures/example.py
```

## Reusable parse profiles

Keep language profiles under `$CI_ROOT/tree-sitter` so later sessions do not
rediscover grammars and node names (see `references/language-registry.md`):

```text
$CI_ROOT/tree-sitter/
├── languages.yaml          # ADR-8-aligned Language Registry
├── queries/python/*.scm
└── parsers/python/parser.so
```

Look up an existing profile by `language`, alias, or extension before parsing.
Reuse its query/parser files while `grammar_version` and `parser_version` match;
otherwise regenerate and bump `parser_version`. Use a built parser directly:

```bash
tree-sitter parse --lib-path "$CI_ROOT/tree-sitter/parsers/python/parser.so" src/file.py
tree-sitter query --lib-path "$CI_ROOT/tree-sitter/parsers/python/parser.so" "$CI_ROOT/tree-sitter/queries/python/calls.scm" src/
```

Validate the registry after every create/update/delete:

```bash
python SKILL_DIR/scripts/validate_language_registry.py "$CI_ROOT/tree-sitter/languages.yaml"
```

`languages.yaml` records the grammar package/version, query paths, kind map, and
capabilities; do not keep a second copy of the same language elsewhere.

## Parse

```bash
tree-sitter parse src/file.py
tree-sitter parse -s src/file.py
tree-sitter parse -q -s src/file.py
tree-sitter parse --json-summary src/file.py
```

Useful flags:

- `-s/--stat`: print parse statistics.
- `-q/--quiet`: suppress the tree but keep the exit status.
- `-x/--xml` or `--dot`: alternate tree output.
- `-t/--time`: parse timing.
- `-c/--cst`: pretty-printed concrete syntax tree.
- `--no-ranges`: omit byte ranges, useful when copying S-expressions.
- `--edits`: apply textual edits after parsing (advanced incremental checks).

`parse` exits non-zero when the file contains parse errors. In the tree,
`(ERROR)` marks unrecognized text and `(MISSING ...)` marks zero-width tokens
the parser inserted during recovery. Both are syntax signals, not semantic bugs.

The printed tree uses grammar-specific node names and field labels; these are
exactly the names you put in an ast-grep rule or a tree-sitter query.

## Query

A query is one or more S-expression patterns with captures:

```scheme
(call
  function: (identifier) @fn
  arguments: (argument_list) @args)
```

Run it against files or a directory:

```bash
tree-sitter query queries/calls.scm src/
tree-sitter query -c queries/calls.scm src/file.py
```

`-c/--captures` orders output by capture instead of match. `--row-range` and
`--containing-row-range` scope a query to a region; `--byte-range` does the same
by byte offsets. `-p/--grammar-path` selects the grammar when it is not
discoverable.

Query language essentials (full syntax in the official page):

```scheme
; node type plus children
(binary_expression (number_literal) (number_literal))

; field-constrained children
(assignment_expression
  left: (member_expression
    object: (call_expression)))

; anonymous operator token
(binary_expression
  operator: "!="
  right: (null))

; wildcards: (_) any named node, _ any node
(call (_) @call.inner)

; supertype/subtype
(expression/binary_expression) @binary

; error and missing nodes
(ERROR) @error-node
(MISSING) @missing-node
```

Captures can be filtered with predicates such as `#eq?`, `#match?`, and
`#any-of?`; examples live in the query-syntax page. Predicates are evaluated by
the CLI, so keep them in the `.scm` file rather than post-processing capture
text.

Useful workflow: run `tree-sitter parse` on a small representative file, copy
the exact node/field spelling, then write the query. Version skew matters —
node names come from the installed grammar, so re-check after a grammar bump.

## Working in a parser project

```bash
tree-sitter generate
tree-sitter build
tree-sitter test
tree-sitter parse -p . test/fixtures/*.py
tree-sitter query -p . queries/locals.scm test/fixtures/example.py
```

`tree-sitter build` produces a dynamically loadable parser (`.so`, `.dylib`,
`.dll`, or Wasm). The CLI builds a parser automatically when needed unless a
prebuilt `--lib-path` is supplied.

## Pitfalls

- `tree-sitter parse` needs a grammar; without one it fails rather than guessing
  from the extension. Install/clone the grammar and configure its directory.
- Node kinds and field names are grammar-version-specific. A query that worked
  in one grammar version can be silently empty in another.
- `(ERROR)` and `(MISSING)` report syntax only. A file with no parse errors can
  still be semantically wrong.
- The query CLI prints S-expression matches, not semantic results. Do not claim
  "no callers" from a syntax query; use LSP for symbol semantics.
- `-q` suppresses output, not errors; rely on the exit status for CI syntax
  gates.
- Parsing a huge generated/vendor tree is wasteful. Pass explicit paths or
  restrict with the range flags.

## Verification

- `tree-sitter parse -q -s <file>` exits 0 for the file you care about.
- The query's captures match the nodes you inspected manually in the parse tree.
- For a grammar change, `tree-sitter test` passes with positive and negative
  fixtures, and `parse` shows no new `(ERROR)`/`(MISSING)` nodes.
