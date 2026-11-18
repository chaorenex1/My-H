# CodeQL — databases, queries, and dataflow

Official documentation:

- CodeQL docs: https://codeql.github.com/docs/
- CodeQL CLI overview: https://docs.github.com/en/code-security/concepts/code-scanning/codeql/codeql-cli
- Preparing code / `database create`: https://docs.github.com/en/code-security/tutorials/customize-code-scanning/prepare-code-for-analysis
- Analyzing code / `database analyze`: https://docs.github.com/en/code-security/tutorials/customize-code-scanning/analyze-code
- Query packs: https://docs.github.com/en/code-security/tutorials/customize-code-scanning/customize-analysis
- Writing custom queries: https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/scan-from-the-command-line/write-custom-queries
- License: https://github.com/github/codeql-cli-binaries/blob/main/LICENSE.md

CodeQL extracts a codebase into a relational database, then runs QL queries over
it. It is the tool for semantic, cross-file, interprocedural dataflow and
CVE-class analysis. It is slow and resource-heavy; do not use it for syntax
search or a quick grep.

## Install and check

```bash
hermes pm install codeql
codeql version
codeql resolve languages
codeql resolve queries
```

PM pins the CLI bundle from `github/codeql-action` (tag
`codeql-bundle-v<version>`). The bundle carries the CLI and the standard query
packs. It is excluded from native payload bundles because of its size, and it
does not run on musl/Alpine. Windows ARM64 and Apple Silicon use the x64 bundle
through host emulation.

License: the CLI is free for public repositories; private repositories require
GitHub Code Security / GitHub Advanced Security licensing. Confirm before
running it against private code.

## Create a database

```bash
codeql database create "$CI_ROOT/codeql/myrepo-python" --language=python --source-root=.
codeql database create "$CI_ROOT/codeql/myrepo-js" --language=javascript-typescript --source-root=.
codeql database create "$CI_ROOT/codeql/myrepo-cpp" --language=c-cpp --command='make' --source-root=.
```

Language identifiers from the official docs: `c-cpp`, `csharp`, `go`,
`java-kotlin`, `javascript-typescript`, `python`, `ruby`, `rust`, `swift`,
plus `actions` for GitHub Actions workflows.

Rules that prevent wasted runs:

- The database path must not already exist.
- Python, JavaScript/TypeScript, and Ruby need their runtime, but must NOT be
  given `--command`; that overrides the extractor and can create an empty
  database.
- Compiled languages need a working build. Pass `--command` once per command, or
  use `--build-mode=autobuild` / `--build-mode=none` where supported.
- Multi-language repositories: `--db-cluster --language=python,java-kotlin
  --command='...' --no-run-unnecessary-builds` creates one database per language
  under the parent directory.
- JavaScript/TypeScript extraction needs `node` on `PATH`.
- Clean the build first for compiled languages so extraction sees every
  translation unit.

Databases live at `$CI_ROOT/codeql/<database-name>/`; results go to
`$CI_ROOT/codeql/results/<database-name>.sarif`. Look for an existing database
for the same language and source revision before creating one. Databases are
immutable: create a new directory when the revision changes, analyze it, and
delete the old directory only when the user agrees. Never delete the
`codeql/` root.

## Analyze a database

Standard code-scanning queries (omit the query specifier):

```bash
codeql database analyze "$CI_ROOT/codeql/myrepo-python" --format=sarif-latest --output="$CI_ROOT/codeql/results/myrepo-python.sarif"
```

Named suite or a single query:

```bash
codeql database analyze "$CI_ROOT/codeql/myrepo-python" javascript-code-scanning.qls --format=sarif-latest --output="$CI_ROOT/codeql/results/myrepo-python.sarif"
codeql database analyze "$CI_ROOT/codeql/myrepo-python" codeql/python-queries:Functions --format=sarif-latest --output="$CI_ROOT/codeql/results/myrepo-python-functions.sarif"
codeql database analyze "$CI_ROOT/codeql/myrepo-cpp" 'codeql/cpp-queries:codeql-suites/cpp-security-and-quality.qls' --format=sarif-latest --output="$CI_ROOT/codeql/results/myrepo-cpp-security.sarif" --download
```

Useful options:

- `--sarif-category=<name>` is required when analyzing multiple databases for
  one commit; without it, results overwrite each other on upload.
- `--threads=0` uses all logical processors.
- `--download` fetches query packs that are not already in the bundle.
- `--format=csv` is convenient for a single targeted query.
- `codeql resolve queries` lists suites shipped with the bundle.

Analyze one language at a time from its database directory (or the parent with
`--db-cluster`). Standard suites are slower than a single query; start with a
single query when investigating one lead.

## Read results

SARIF is verbose. Summarize it:

```bash
python SKILL_DIR/scripts/sarif_summary.py results.sarif
python SKILL_DIR/scripts/sarif_summary.py results.sarif --json
```

Each result is a lead. Open the referenced file and line with `read_file` and
confirm the dataflow path yourself before reporting a vulnerability. SARIF
"level" is severity metadata, not proof of exploitability.

## Custom queries and packs

CodeQL queries are QL files organized into packs. A typical local loop:

```bash
codeql pack init my-org/my-queries
codeql query compile my-queries/MyQuery.ql
codeql database analyze "$CI_ROOT/codeql/myrepo-python" --search-path=. my-org/my-queries --format=sarif-latest --output="$CI_ROOT/codeql/results/myrepo-python-custom.sarif"
```

`pack init` creates a child directory named for the pack (`my-queries/`);
`--search-path=.` lets `database analyze` resolve that working-tree pack by its
`my-org/my-queries` name. Omit `--search-path` only for a pack that is already
installed or published; alternatively pass the query file directly
(`./my-queries/MyQuery.ql`).

The query's `@kind`, `@id`, `@name`, `@description`, `@problem.severity`, and
`@security-severity` metadata drive how results are interpreted. Pack
dependencies go in `qlpack.yml`; `codeql pack install` resolves them. See the
official "Writing custom queries" and "Customizing analysis" pages for the full
metadata and pack contract.

## Pitfalls

- **License/size/platform.** Not a general-purpose dev dependency: glibc-only,
  large, and licensed for public repos unless GitHub Code Security applies.
- **Empty database.** Passing `--command` for Python/JS/TS is the classic way to
  get a database with no code. Omit it for interpreted languages.
- **Build must succeed.** A failed compile usually yields a partial or empty
  database; fix the build before analyzing.
- **Database path exists.** `database create` refuses an existing directory; use
  a fresh path or remove a database you created.
- **Suite names vary by language.** Prefer `codeql resolve queries` over guessing
  `*-code-scanning.qls`; a wrong name fails after extraction.
- **Generated/vendored code.** Exclude it with `--codescanning-config` or a
  scoped `--source-root`; otherwise findings and runtime balloon.
- **No musl/Alpine.** Use a glibc container or host for CodeQL.

## Verification

- `codeql database create` reports success and the database directory contains
  source archives and relational data.
- `codeql database analyze` exits 0; SARIF parses and every finding location is
  confirmed in source.
- Re-run the specific query (not always the full suite) after the fix with a
  fresh database; keep the query id, command, and remaining findings in the
  report.
