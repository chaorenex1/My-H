# Semgrep — pattern-based static analysis

Official documentation:

- Docs home: https://docs.semgrep.dev/
- Quick start: https://docs.semgrep.dev/getting-started/quickstart
- Local scans / CLI: https://docs.semgrep.dev/getting-started/cli
- CLI reference: https://docs.semgrep.dev/cli-reference
- Rule syntax: https://docs.semgrep.dev/writing-rules/rule-syntax
- Pattern syntax: https://docs.semgrep.dev/writing-rules/pattern-syntax
- Taint mode: https://docs.semgrep.dev/writing-rules/data-flow/taint-mode/overview

Semgrep finds code patterns across many languages without a build. Use it for
bug-class and security sweeps, custom lint rules, and fast CI gates. It is
pattern-based, not a compiler: aliases, wrappers, generated code, and inter-file
flows can produce false negatives.

## Install and check

```bash
hermes pm install semgrep
semgrep --version
```

PM pins the distribution version in an isolated Python environment. Upstream's
preferred manual installs are `pipx install semgrep` or `uv tool install
semgrep`; do not install it into the Hermes application venv. Semgrep publishes
musllinux x86_64/aarch64 wheels but has no Android/bionic wheel.

## Scan commands

Local scan with registry rules (network; see privacy note):

```bash
semgrep scan --config auto --metrics=off .
```

Pinned rule packs and local rules:

```bash
semgrep scan --config p/python --metrics=off .
semgrep scan --config p/security-audit --metrics=off src/
semgrep scan --config "$CI_ROOT/semgrep/python/myrules.yml" --metrics=off .
semgrep scan --config "$CI_ROOT/semgrep/python/myrules.yml" --config p/default --metrics=off src/
```

Outputs and CI behavior:

```bash
semgrep scan --config "$CI_ROOT/semgrep/python/myrules.yml" --json --json-output="$CI_ROOT/semgrep/results/python/myrules.json" --metrics=off .
semgrep scan --config p/default --sarif --sarif-output="$CI_ROOT/semgrep/results/ci.sarif" --error --metrics=off .
```

- `--json`, `--sarif`, `--text`, `--gitlab-sast` select the output format.
- `--json-output=FILE` / `--sarif-output=FILE` also write a file.
- `--error` exits 1 when there are findings; without it a completed scan exits 0.
- `--exclude=PATTERN` and `--include=PATTERN` narrow the file set.
- `-j N` controls parallelism; `--timeout` and `--timeout-threshold` bound
  slow files.
- `--dump-ast` shows how Semgrep parses a file, useful when a pattern misses.

`semgrep ci` is the CI entry point; it fetches organization policy and requires
a Semgrep account. For local or offline work use `semgrep scan`.

Privacy: `--config auto` pulls from the Semgrep registry and, by default, sends
pseudonymous metrics and the project URL. Pass `--metrics=off` and use a pinned
rule file when that is not acceptable.

## Persistent custom rules

Agent-created rules are scoped by language under `$CI_ROOT/semgrep/`:

```text
$CI_ROOT/semgrep/
├── python/
│   └── myrules.yml
├── typescript/
│   └── myrules.yml
├── shared/
│   └── cross-language.yml
└── results/python/myrules.json
```

- The directory name is the Semgrep `languages` key (`python`, `typescript`,
  `go`, and so on). A pack in `semgrep/<language>/` contains only rules for that
  language; do not mix languages in one language directory.
- If any rule in a pack spans multiple languages, put the whole pack under
  `semgrep/shared/`.
- Never put `.yml` files directly at the `semgrep/` root; a root-level file
  defeats language-scoped reuse.
- Reuse a rule pack by listing existing `<language>/*.yml` files before
  creating a new one; one file per logical pack, named for its purpose or
  primary rule id.
- Update the whole file and keep rule ids stable so findings remain
  comparable; validate with `semgrep scan --validate --config <file>`.
- Delete only a rule pack this skill created; do not remove `semgrep/` or files
  authored elsewhere. Remove an empty language directory when its last pack is
  gone.

Run one language's pack explicitly:

```bash
semgrep scan --config "$CI_ROOT/semgrep/python/myrules.yml" --metrics=off .
semgrep scan --config "$CI_ROOT/semgrep/shared/cross-language.yml" --metrics=off .
```

## Write a rule

```yaml
rules:
  - id: python-md5
    languages: [python]
    message: MD5 is weak; use a stronger hash.
    severity: HIGH
    pattern: hashlib.md5(...)
```

Required fields: `id`, `message`, `severity` (`LOW`, `MEDIUM`, `HIGH`,
`CRITICAL`), `languages`, and exactly one top-level pattern operator:

- `pattern`: one expression.
- `patterns`: logical AND (list).
- `pattern-either`: logical OR (list).
- `pattern-regex`: PCRE2 multiline regex.

Compose inside `patterns`:

```yaml
rules:
  - id: unverified-db-query
    languages: [python]
    message: Database query runs without verification.
    severity: HIGH
    patterns:
      - pattern: db_query(...)
      - pattern-not: db_query(..., verify=True, ...)
```

Pattern language essentials:

- `...` matches any sequence of arguments/statements.
- `$X` binds one expression; `$...ARGS` binds argument lists.
- `pattern-inside` restricts matches to a context.
- `metavariable-regex`, `metavariable-pattern`, and
  `metavariable-comparison` constrain bindings.
- `fix:` supplies an autofix; review it before `--autofix`.

Taint rules use `mode: taint` with `pattern-sources`, `pattern-sinks`, and
optional `pattern-sanitizers`. Intraprocedural taint is available in the open
engine; cross-function and inter-file analysis require Semgrep Pro and an
account (`--pro-intrafile`, `--pro`). Use CodeQL when deep cross-file dataflow
is the actual question.

Validate and test rules before a broad scan:

```bash
semgrep scan --validate --config myrules.yml
semgrep scan --test --config myrules.yml
```

Include positive and negative fixtures with `ruleid:` / `ok:` annotations as
described in the testing documentation.

## Reading results

Prefer JSON for exact fields, or SARIF when the output feeds GitHub/another
tool. Both Semgrep and CodeQL produce SARIF; summarize either with:

```bash
python SKILL_DIR/scripts/sarif_summary.py findings.sarif
```

A finding is a lead. Open the file/line with `read_file`, confirm the pattern
instance and the surrounding control flow, and only then report it. Do not
present Semgrep severity as exploitability.

## Pitfalls

- `--config auto` is great for discovery but not reproducible; pin `p/...` packs
  or commit a rule file for repeatable results.
- Semgrep matches syntax, not types or aliases. A wrapper around a dangerous
  call can be missed; combine patterns and review.
- Default file selection honors `.gitignore`/`.semgrepignore`, but vendored
  trees often need explicit `--exclude`.
- `--autofix` can cause data loss when rules are broad or overlapping. Use
  `--dryrun` first and review the diff.
- `semgrep ci` requires login and organization policy; it is not the offline
  equivalent of `semgrep scan`.
- Exit code 0 does not mean "no risk": it means the scan completed. Use
  `--error` when a non-zero gate is intended.

## Verification

- Rule validation and tests pass before scanning the repository.
- Every reported location is confirmed against the source with `read_file`.
- After a fix, re-run the same rule/config and confirm the finding count drops
  as expected; keep the command and rule id in the report.
- For CI, verify both the findings output and the intended exit code.
