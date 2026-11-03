# ast-grep — structural search and rewrite

Official documentation:

- Quick start: https://ast-grep.github.io/guide/quick-start.html
- CLI reference: https://ast-grep.github.io/reference/cli.html
- `run` reference: https://ast-grep.github.io/reference/cli/run.html
- Rule essentials: https://ast-grep.github.io/guide/rule-config.html
- Rule object: https://ast-grep.github.io/reference/rule.html
- Configuration reference: https://ast-grep.github.io/reference/yaml.html
- Rule testing: https://ast-grep.github.io/guide/test-rule.html
- Languages: https://ast-grep.github.io/reference/languages.html

`ast-grep` matches syntax trees, not text. Use it when the same code shape
appears with different names, whitespace, formatting, or comments, and when a
regex would be fragile or produce false positives.

## Install and check

PM installs a pinned release:

```bash
hermes pm install ast-grep
ast-grep --version
```

On Linux the shorter alias `sg` can collide with `setgroups`; always call
`ast-grep`. The binary auto-detects language from file extensions, but pass
`-l/--lang` whenever the pattern's language matters.

## Search

The default subcommand is `run`:

```bash
ast-grep -p '$PROP && $PROP()' -l ts src
ast-grep -p 'console.log($$$ARGS)' -l js src
ast-grep -k call_expression src
ast-grep -k 'call_expression > identifier' src
```

Patterns are ordinary code with metavariables:

- `$A` matches one AST node.
- `$$$ARGS` matches zero or more nodes (useful for argument lists).
- `$_` / `$$$` match without binding names.

Check that the pattern parses as intended before scanning a large tree:

```bash
ast-grep -p 'foo($A)' -l python --debug-query=ast
```

Narrow the search with `--globs` (gitignore-style, `!` excludes) and context
flags `-A/-B/-C`. Output options:

```bash
ast-grep -p 'foo($A)' -l python --json=compact src
ast-grep -p 'foo($A)' -l python --json=pretty src
```

`run` exits 0 when at least one line matches and 1 when nothing matches — do
not treat "exit 1" as a tool failure.

## Rewrite

`-r/--rewrite` only takes effect with `-U/--update-all` or `-i/--interactive`:

```bash
ast-grep -p '$PROP && $PROP()' -r '$PROP?.()' -l ts src
ast-grep -p '$PROP && $PROP()' -r '$PROP?.()' -l ts -U src
ast-grep -p 'console.log($$$ARGS)' -r '' -l ts -i src
```

The safe order is:

1. Run with `--json=compact` and inspect the matches.
2. Run the rewrite with `-i`, or with `-U` only after the match set is reviewed.
3. Run the project's tests and re-run the original search to prove it is empty.

Rewrites replace the matched AST node; comments and formatting inside the match
may be lost. Prefer small, reviewable patterns over broad rewrites.

## YAML rules

For persistent linting or cross-file policy, write a rule file:

```yaml
id: no-await-in-promise-all
language: TypeScript
rule:
  pattern: Promise.all($A)
  has:
    pattern: await $_
    stopBy: end
message: Do not await inside Promise.all.
severity: warning
```

Rule fields (see the configuration reference for the complete list):

- `id`, `language`, `rule` are required.
- `message`, `severity` (`hint`, `info`, `warning`, `error`, `off`), `note`.
- `fix`: replacement pattern using metavariables; empty string deletes.
- `constraints`: filter a single metavariable with another rule.
- `files` / `ignores`: globs limiting where the rule applies.
- `utils`: named sub-rules reused via `matches`.

The `rule` object combines:

- Atomic: `pattern`, `kind`, `regex`.
- Relational: `inside`, `has`, `follows`, `precedes` (with `stopBy` and
  optional `field`).
- Composite: `all`, `any`, `not`, `matches`.

For a partial construct that is not valid standalone code, use a pattern object:

```yaml
rule:
  pattern:
    context: 'class { $F }'
    selector: field_definition
```

A rule directory needs a project config — `sgconfig.yml` with `ruleDirs`, which
ast-grep reads recursively:

```yaml
ruleDirs:
  - rules
```

Run rules:

```bash
ast-grep scan --rule no-await-in-promise-all.yml src
ast-grep scan --inline-rules 'id: no-eval
language: Python
rule:
  pattern: eval($A)
message: eval is unsafe.
severity: error' src
ast-grep scan --config sgconfig.yml src --json=compact
ast-grep scan --config sgconfig.yml src --format=sarif
```

`--rule` accepts exactly one rule file and conflicts with `--config`; use
`--config` (or `--inline-rules`) when scanning a rule set. `scan` exits non-zero
when a rule with `severity: error` matches; `--error`/`--warning`/`--info`/`--off`
override severities for a run.

Test rules with the project's rule tests:

```bash
ast-grep test -c sgconfig.yml
```

## Pitfalls

- Double-quoted patterns let the shell expand `$A`; use single quotes.
- `pattern` is parsed as complete code. Use `context`/`selector` for fragments.
- The default matching is `smart`; try `--strictness ast` or `relaxed` when a
  pattern matches too much or too little.
- `kind` names are grammar-specific. Use `--debug-query=ast` or tree-sitter to
  confirm names instead of guessing.
- `run` and `scan` honor `.gitignore`/ignore files by default; use `--no-ignore`
  or `--globs` deliberately.
- `--json` must use `=` for its value: `--json=compact`, not `--json compact`.
- Rewrites are not transactional. Work in a clean tree so the diff can be
  reviewed or reverted.

## Verification

- The search count before the rewrite matches the number of intended edits.
- `--json=compact` output is the evidence for the match set; keep it.
- After the rewrite, the original pattern returns no matches and the targeted
  tests pass.
- For a rule, include a positive and negative fixture and run `ast-grep test`
  after every rule change.
