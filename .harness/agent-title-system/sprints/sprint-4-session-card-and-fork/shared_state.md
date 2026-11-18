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

- Goal: 会话卡片与 fork 生成器——默认走主回复副回合命名，失败回落候选计划并在 `card_icons != boring` 时补一次卡片请求；卡片是 `sessions.title` 的字符串子形态，随重生成保留、随比较/漂移/检索剥离
- AS-1 卡片三形态解析 + code/icon 合法性边界
- AS-2 `title-card.md`/`title-fork.md` Go→Jinja + `scope=request` 注册（`title_card` / `title_fork`）
- AS-3 fork 副回合前缀一致性与 transcript/leaf 隔离；usage 记 `title_generation`
- AS-4 fork 失败矩阵回落 `title_model_plan`；`generator=fork` 且 `card_icons != boring` 时补一次卡片请求
- AS-5 `card_icons=boring` 是命名管线的门（`split_card_title` 仍解析）：不请求卡片、存储去卡文本、重生成不恢复旧卡
- AS-6 卡片保留、`<title/>` 终止、去卡比较/漂移/检索
- AS-7 手工标题不触发模型/卡片；命名路径不消费 Nerd Font

## Contract
<!--
  Reference to the frozen contract (post-negotiation) with its commit SHA.
  Format: `sprint-<N>-contract.md @ <SHA>`
-->

- sprint-4-contract.md @ 747baf507fa794384e042bdc4f1a190eaddb9750
- frozen: Functionality 1.0 / Craft 0.85 / Consistency 0.9 / Documentation 0.85; max_iterations 8

## Negotiation
<!--
  Orchestrator-curated summary of negotiation rounds. Raw per-agent messages
  live in feedback/{role}-<iter>.md. Keep this terse: round, outcome.
-->

- [2026-11-18T13:27:06Z] round=1 agent=generator signal=counter delta="F1.0/C0.85/K0.9/D0.85, iter 8" file=feedback/generator-neg-1.md
- [2026-11-18T13:37:39Z] round=1 agent=evaluator signal=counter delta="thresholds+iter accepted; body AS-2/3/5/6, test set, docs surface counter" file=feedback/evaluator-neg-1.md
- [2026-11-18T13:47:14Z] round=2 agent=generator signal=accept delta="counter adopted verbatim + P1-P6 precision notes" file=feedback/generator-neg-2.md
- [2026-11-18T13:56:39Z] round=2 agent=evaluator signal=skipped delta="round resolved on accept; round-1 proposal adopted"

## WorkLog
<!--
  Timestamped summary of each Generator iteration.
  Format: `[<ISO-8601>] iter=<N> agent=generator action=<summary> commit=<SHA>`
  Detailed per-iteration notes go to feedback/generator-<iter>.md.
-->

- [2026-11-18T15:30:00Z] iter=1 agent=generator action="title_card parser + Jinja card/fork templates + fork-first naming with plan fallback, one-shot card top-up, card-preserving drift, de-carded lookup" files=29

## Evaluation
<!--
  Timestamped summary of each Evaluator verdict.
  Format: `[<ISO-8601>] iter=<N> agent=evaluator verdict=<pass|fail> axes=<...>`
  Evidence paths (screenshots, traces) and rubric raw scores go to
  feedback/evaluator-<iter>.md and evidence/.
-->

- [2026-11-18T15:24:31Z] iter=1 agent=evaluator verdict=pass axes="fn=1.0 craft=0.9 consistency=0.9 documentation=0.9" evidence=evidence/iter-1/

## Decisions
<!--
  Orchestrator records irreversible state transitions here:
    - sprint status changes (negotiating → active → done | aborted)
    - Principal Skinner stops (reason, state.json cursor at stop)
    - PR creation (PR number, bundling mode)
    - Human escalations (pending_human set, who approved, when resumed)
-->

- [2026-11-18T13:56:39Z] decision: sprint-4 contract frozen @ 747baf507fa794384e042bdc4f1a190eaddb9750
- [2026-11-18T13:28:50Z] round=1 agent=generator signal=counter delta="Functionality 1.0 / Craft 0.85 / Consistency 0.9 / Documentation 0.85, max_iterations 8" file=feedback/generator-neg-1.md
- [2026-11-18T13:41:09Z] round=1 agent=evaluator signal=counter delta="thresholds 1.0/0.85/0.9/0.85 + iter 8 accepted; counter on AS-2/3/5/6 body contradictions, manifest test set, docs surface" file=feedback/evaluator-neg-1.md

- [2026-11-18T15:30:00Z] decision: sprint-4 iter-1 verdict pass (all axes >= threshold); contract status done, phase pr
- [2026-11-18T15:30:00Z] decision: PR opened: sprint-4 (pending URL) (bundling=split)
