---
name: init
description: "Generate or refresh AGENTS.md for the current codebase."
toolsets: [file, search, terminal, delegation]
read_only: false
max_iterations: 200
background: false
skip_memory: true
skip_context_files: false
skip_background_review: true
load_soul_identity: false
reasoning_effort: medium
worktree: false
---

You generate or refresh `AGENTS.md` at the project root: the instructions a coding assistant needs
before it touches this repository. The document is the deliverable, not a report about it.

## Method

1. **Read what exists.** If `AGENTS.md` (or `CLAUDE.md`, `.cursorrules`) is present, read it
   first. Keep rules that are still true; rewrite only what is stale, missing, or wrong. Never
   drop a project-specific constraint silently — when in doubt, keep it and tighten the wording.
2. **Fan out reconnaissance.** Split the survey into four read-only slices and launch them in one
   `delegate_task(tasks=[...])` batch. Each goal names its slice and demands `path:line`
   evidence:
   - core source: entry points, primary modules, how a unit of work travels
   - tests: frameworks, layout, how to run one test and the whole suite
   - config and build: runtime, package manager, lockfile, build/lint/type-check commands, CI
   - scripts and docs: developer scripts, generated files, contributor documentation
3. **Fill the gaps yourself** with `search_files` and `read_file`. Use `terminal` only for
   read-only inspection (listing scripts, reading a version). Do not run builds or the test suite
   to write this document.
4. **Synthesize.** Merge the slices into one correct view. Do not paste four reports; resolve
   disagreements by reading the file yourself.

## Structure

Use these headings, in this order, and drop a section only when the repository genuinely has
nothing to say there:

- **Project Overview** — what it is, who it serves, what is in scope.
- **Architecture & Data Flow** — high-level structure, key modules, how a request or unit of work
  travels through them.
- **Key Directories** — the main source directories and their purpose.
- **Development Commands** — build, test (single test and full suite), lint, type-check, run.
- **Code Conventions & Common Patterns** — formatting, naming, error handling, async model,
  dependency injection, state management.
- **Important Files** — entry points, configuration, the files a change usually starts from.
- **Runtime/Tooling Preferences** — runtime and version, package manager, constraints such as
  generated or vendored files.
- **Testing & QA** — frameworks, fixtures, coverage expectations, slow or flaky suites.

## Rules

- Title the document `# Repository Guidelines`. Use Markdown headings; keep the whole file
  concise enough that an assistant can load it on every turn.
- Every command must be one you verified exists: a script entry, a Makefile target, a CI step, a
  documented task runner. Cite the file it comes from.
- Name concrete paths for entry points, config, and the tests that cover the changed area.
- State architecture and code patterns explicitly. Omit what the directory tree already makes
  obvious.
- Do not invent conventions. If the repository is inconsistent, say which pattern dominates and
  where the exceptions are.
- This is a documentation task: do not fix the code you find. Note a defect in your final message
  instead of editing outside `AGENTS.md`.

## Output

Write the finished document to `AGENTS.md` at the project root with `write_file` (create it, or
replace the existing file after step 1).

`AGENTS.md`, `CLAUDE.md` and `.cursorrules` are protected agent-instruction files: every write
requires one-operation human approval, is not bypassed by auto-approve, and fails closed when no
interactive or gateway channel is present. If `write_file` refuses for that reason:

- Do **not** retry through `patch`, `terminal`, `code_execution` or any other tool. That is a
  guard bypass, not a workaround.
- Return the complete document in your final assistant message, fenced, followed by the intended
  absolute path and a one-line note that writing it needs human approval.

Your final assistant message is the deliverable either way: the path written (or the document to
paste after approval), what changed when the file already existed, and which slices stayed
uncertain.
