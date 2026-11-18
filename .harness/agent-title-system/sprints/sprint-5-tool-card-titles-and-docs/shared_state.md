<!--
  shared_state.md — Sprint-level shared ledger.

  Written by: Orchestrator ONLY (harness-loop).
  Read by:    Planner, Generator, Evaluator (all agents).

  Individual agents write their deliberations to
    sprints/sprint-<N>-<feature>/feedback/{planner,generator,evaluator}-<iter>.md
  to avoid write races and keep this ledger as the canonical single source
  of truth.

  APPEND-ONLY. Never edit prior entries — add a new dated entry instead.
-->

# Shared State — Sprint <N> (<feature-name>)

## Plan
<!--
  Orchestrator summarises Planner's brief here (copied from contract.md goal
  + acceptance_scenarios). Update when Planner issues a re-plan.
-->

- Register tool-card titles as a deterministic naming consumer (sync derivation, zero model calls, display-only) and freeze via contract tests that naming-route/generator changes cannot affect cards.
- AS-1 zero-LLM + monotone thread assert; AS-2 `tool.start`/`tool.title` ordering + closed payloads; AS-3 per-cell titles; AS-4 `display_metadata` persistence/projection; AS-5 docs, sample-config and picker-copy sync.

## Contract
<!--
  Reference to the frozen contract (post-negotiation) with its commit SHA.
  Format: `sprint-<N>-contract.md @ <SHA>`
-->

- sprint-5 contract frozen @ 8c8f2c0b61 (rubric F 1.0 / Craft 0.85 / Consistency 0.9 / Documentation 0.9, max_iterations 8)

## Negotiation
<!--
  Orchestrator-curated summary of negotiation rounds. Raw per-agent messages
  live in feedback/{role}-<iter>.md. Keep this terse: round, outcome.
-->

- [2026-11-18T16:10:22Z] round=1 agent=generator signal=counter delta="F1.0/C0.85/Cons0.9/Doc0.9 max_iter=8; asks Evaluator to rule AS-5 assertion surface" file=feedback/generator-neg-1.md
- [2026-11-18T16:18:16Z] round=1 agent=evaluator signal=counter delta="thresholds accepted; counter on AS-5 .py source read (MUST), AS-2 phantom _lifecycle, AS-1 spy point, slice traps" file=feedback/evaluator-neg-1.md
- [2026-11-18T16:23:44Z] round=2 agent=generator signal=accept delta="accepts thresholds, (a)-(e) and readings 1-5; four freeze clarifications" file=feedback/generator-neg-2.md
- [2026-11-18T16:23:44Z] round=2 agent=evaluator signal=skipped delta="generator accept resolved the round" file=(none)

## WorkLog
<!--
  Timestamped summary of each Generator iteration.
  Format: `[<ISO-8601>] iter=<N> agent=generator action=<summary> commit=<SHA>`
  Detailed per-iteration notes go to feedback/generator-<iter>.md.
-->

- [2026-11-18T17:43:15Z] iter=1 agent=generator action="AS-1..AS-5 contract tests + docs/sample-config/picker-comment sync; zero runtime drift" commit=_pending_

## Evaluation
<!--
  Timestamped summary of each Evaluator verdict.
  Format: `[<ISO-8601>] iter=<N> agent=evaluator verdict=<pass|fail> axes=<...>`
  Evidence paths (screenshots, traces) and rubric raw scores go to
  feedback/evaluator-<iter>.md and evidence/.
-->

- [2026-11-18T17:43:15Z] iter=1 agent=evaluator verdict=pass axes="functionality=1.0 craft=0.9 consistency=0.9 documentation=0.9" (0 critical, 0 improvement, 4 minor) evidence=evidence/iter-1/

## Decisions
<!--
  Orchestrator records irreversible state transitions here:
    - sprint status changes (negotiating → active → done | aborted)
    - Principal Skinner stops (reason, state.json cursor at stop)
    - PR creation (PR number, bundling mode)
    - Human escalations (pending_human set, who approved, when resumed)
-->

- [2026-11-18T17:43:15Z] decision: sprint-5 contract done on iter-1 pass; phase=pr
- [2026-11-18T16:11:45Z] round=1 agent=generator signal=counter delta="F1.0/C0.85/Cons0.9/Doc0.9 max_iter=8; asks Evaluator to rule AS-5 assertion surface" file=feedback/generator-neg-1.md
- [2026-11-18T16:20:16Z] round=1 agent=evaluator signal=counter delta="thresholds accepted (F1.0/C0.85/Cons0.9/Doc0.9, max_iter=8); counter on AS-5 source-read MUST violation, AS-2 phantom _lifecycle, AS-1 derive_title spy point, slice traps" file=feedback/evaluator-neg-1.md
