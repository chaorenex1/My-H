"""The ``lsp`` config defaults: language-server diagnostics and their display policy.

Moved out of ``config_defaults.py`` (a facade that may only shrink). The block is the user-facing
config.yaml documentation, so the comments travel with it.
"""

from __future__ import annotations

from typing import Any


def lsp_defaults() -> dict[str, Any]:
    return {
        "enabled": True,  # False disables the whole subsystem: no servers, no event loop, no cost.
        # document = wait up to wait_timeout seconds for the current file's diagnostics; full = also
        # request workspace-wide diagnostics (slower).
        "wait_mode": "document",
        "wait_timeout": 5.0,
        # Budget for the FIRST request against a workspace whose server is not running yet (spawn +
        # initialize + the server's initial program build; tsserver on a large project can need a
        # minute). Once the client is up, wait_timeout applies again. 0 = same as wait_timeout.
        "warmup_timeout": 0.0,
        # After a server fails (spawn error or outer timeout) its (server, workspace root) pair is
        # skipped. 0 = for the process lifetime (until `hermes lsp restart`); N = retried after N
        # seconds, so one transient stall does not silence a workspace forever.
        "broken_retry_seconds": 0.0,
        # Workspace roots (glob patterns, ~ expanded; a bare path also matches everything under
        # it) where no language server runs at all, e.g. one huge monorepo whose server cannot
        # finish in budget, while every other workspace keeps its diagnostics. Must be a list —
        # any other shape logs a warning and skips LSP for every workspace until fixed.
        "exclude_roots": [],
        # Directories (~ expanded; everything under an entry counts) whose projects a language
        # server may load code from: the project's own .venv/venv interpreter, node_modules
        # TypeScript SDK, svelte.config.js, build files (cargo, Gradle, mix, ...). The worktree of
        # the launch dir or the session's workspace (hermes -w, a Desktop project, terminal.cwd) is
        # always trusted; in any other checkout (a clone the agent made) only servers that run no
        # project code start, pinned to Hermes-side tools, and the npx tsc / rustfmt lint fallbacks
        # are skipped.
        "trusted_workspaces": [],
        # ADR-9 D12: WHERE diagnostics are shown, never whether a scanner runs (`enabled` above and
        # `code_intel.semgrep.enabled` remain the subsystem switches).
        #   mode: off | inline | late | both
        #     inline = a verdict inside the write budget goes into the tool result; a timeout is dropped
        #     late   = every verdict becomes a late-diagnostics row (custom sessions) / note (legacy)
        #     both   = the pre-ADR-9 behaviour: inline when it fits, late when it outlives the budget
        #     off    = this source is absent from the diagnostics surface (Semgrep also skips the
        #              write-time scan; `hermes code index` is unaffected)
        #   severities: error | warning (LSP defaults to errors only; Semgrep to both)
        "display": {
            "lsp": {"mode": "both", "severities": ["error"]},
            "semgrep": {"mode": "both", "severities": ["error", "warning"]},
        },
        # Missing server binaries: auto = install via npm/go/pip into <HERMES_HOME>/lsp/bin/ on
        # first use; manual = only binaries on PATH; off = alias for manual.
        "install_strategy": "auto",
        # Node package manager for the npm-recipe servers: npm | pnpm | yarn. Installs still land in
        # <HERMES_HOME>/lsp/node_modules; a configured manager that is not installed, or an unknown
        # value, skips the install (no silent fallback to npm) so a pnpm/yarn supply-chain policy is
        # never bypassed.
        "package_manager": "npm",
        # Idle seconds before a server is shut down (respawned on demand), so long- running
        # processes don't accumulate stale children (hundreds of MB + pipe FDs each) across
        # worktrees. 0 = keep servers for process lifetime.
        "idle_timeout": 600.0,
        # Per-server overrides keyed by registry server_id (pyright, gopls...): disabled: true;
        # command: ["path/to/server", "--stdio"] (bypasses auto- install); env: {...};
        # initialization_options: {...} (merged into LSP initializationOptions).
        # A key that is NOT a built-in id declares a custom server (matched before the built-ins):
        # command: ["my-ls", "--stdio"]; extensions: [".ext"]; optional root_markers: [...],
        # language_id: "..." (didOpen languageId), description: "...". Manual install only.
        "servers": {},
    }


__all__ = ["lsp_defaults"]
