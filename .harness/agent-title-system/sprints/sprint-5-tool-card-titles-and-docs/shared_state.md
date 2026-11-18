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

- _not yet populated_

## Contract
<!--
  Reference to the frozen contract (post-negotiation) with its commit SHA.
  Format: `sprint-<N>-contract.md @ <SHA>`
-->

- _awaiting negotiation completion_

## Negotiation
<!--
  Orchestrator-curated summary of negotiation rounds. Raw per-agent messages
  live in feedback/{role}-<iter>.md. Keep this terse: round, outcome.
-->

- _awaiting round 1_

## WorkLog
<!--
  Timestamped summary of each Generator iteration.
  Format: `[<ISO-8601>] iter=<N> agent=generator action=<summary> commit=<SHA>`
  Detailed per-iteration notes go to feedback/generator-<iter>.md.
-->

- _no iterations yet_

## Evaluation
<!--
  Timestamped summary of each Evaluator verdict.
  Format: `[<ISO-8601>] iter=<N> agent=evaluator verdict=<pass|fail> axes=<...>`
  Evidence paths (screenshots, traces) and rubric raw scores go to
  feedback/evaluator-<iter>.md and evidence/.
-->

- _no evaluations yet_

## Decisions
<!--
  Orchestrator records irreversible state transitions here:
    - sprint status changes (negotiating → active → done | aborted)
    - Principal Skinner stops (reason, state.json cursor at stop)
    - PR creation (PR number, bundling mode)
    - Human escalations (pending_human set, who approved, when resumed)
-->

- _no decisions yet_
- [2026-11-18T16:11:45Z] round=1 agent=generator signal=counter delta="F1.0/C0.85/Cons0.9/Doc0.9 max_iter=8; asks Evaluator to rule AS-5 assertion surface" file=feedback/generator-neg-1.md
- [2026-11-18T16:20:16Z] round=1 agent=evaluator signal=counter delta="thresholds accepted (F1.0/C0.85/Cons0.9/Doc0.9, max_iter=8); counter on AS-5 source-read MUST violation, AS-2 phantom _lifecycle, AS-1 derive_title spy point, slice traps" file=feedback/evaluator-neg-1.md
