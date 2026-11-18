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

- **Goal**: 交付 `title_model_plan()` + `run_title_plan()`：按固定顺序（显式 `title_generation` route → `tiny` → `commit_message` → `smol` → 当前会话模型）产出有序、去重的显式候选路由；执行器逐条 `fallback=False` 单次调用，按拥有该候选的 task 计时/计费；门控关闭或空计划时零模型调用。
- **Acceptance scenarios**: AS-1 无 pin 只调当前模型一次；AS-2 pin 排序 tiny→commit_message→smol→current；AS-3 显式 route 仅非默认时作为候选 0；AS-4 链 DFS 展开 + 三元组去重 + 防环；AS-5 门控/空计划零调用；AS-6 单次调用、失败续跑、成功即停、`<title/>` 与 `<title>none</title>` 终止。
- **Test surface**: `tests/agent/test_title_router.py`（唯一主测试面），`scripts/run_tests.sh` 执行。


## Contract
<!--
  Reference to the frozen contract (post-negotiation) with its commit SHA.
  Format: `sprint-<N>-contract.md @ <SHA>`
-->

- sprint-2 contract frozen @ 90468cada771f12c9e1a7382fe0c578a347bbcef

## Negotiation
<!--
  Orchestrator-curated summary of negotiation rounds. Raw per-agent messages
  live in feedback/{role}-<iter>.md. Keep this terse: round, outcome.
-->

- [2026-11-18T08:16:00Z] round=1 agent=generator signal=counter delta=Fn=1.0/Craft=0.85/Cons=0.9/Doc=0.8/max_iter=6 file=feedback/generator-neg-1.md
- [2026-11-18T08:26:00Z] round=1 agent=evaluator signal=counter delta=Fn=1.0/Craft=0.85/Cons=0.9/Doc=0.85/max_iter=8 + agent/AGENTS.md in scope file=feedback/evaluator-neg-1.md
- [2026-11-18T08:30:00Z] round=2 agent=generator signal=accept delta=Fn=1.0/Craft=0.85/Cons=0.9/Doc=0.85/max_iter=8 + note-5 wording fix file=feedback/generator-neg-2.md
- [2026-11-18T08:44:00Z] round=2 agent=evaluator signal=accept delta=thresholds frozen (1.0/0.85/0.9/0.85, max_iter=8) + note-5 paragraph + AS-3 body file=feedback/evaluator-neg-2.md

## WorkLog
<!--
  Timestamped summary of each Generator iteration.
  Format: `[<ISO-8601>] iter=<N> agent=generator action=<summary> commit=<SHA>`
  Detailed per-iteration notes go to feedback/generator-<iter>.md.
-->

- [2026-11-18T09:10:00Z] iter=1 agent=generator action=title_model_plan + run_title_plan + 14 tests + AGENTS.md bullet commit=(pending) — see feedback/generator-1.md

## Evaluation
<!--
  Timestamped summary of each Evaluator verdict.
  Format: `[<ISO-8601>] iter=<N> agent=evaluator verdict=<pass|fail> axes=<...>`
  Evidence paths (screenshots, traces) and rubric raw scores go to
  feedback/evaluator-<iter>.md and evidence/.
-->

- [2026-11-18T09:25:00Z] iter=1 agent=evaluator verdict=pass axes=functionality 1.0 / craft 0.9 / consistency 0.95 / documentation 0.9 evidence=evidence/iter-1/ (69/69 probe checks, 182 tests in the attributable gate) — see feedback/evaluator-1.md

## Decisions
<!--
  Orchestrator records irreversible state transitions here:
    - sprint status changes (negotiating → active → done | aborted)
    - Principal Skinner stops (reason, state.json cursor at stop)
    - PR creation (PR number, bundling mode)
    - Human escalations (pending_human set, who approved, when resumed)
-->

- [2026-11-18T09:36:00Z] decision: PR opened sprint-2 https://github.com/chaorenex1/My-H/pull/10 (bundling=split)
- [2026-11-18T09:28:00Z] decision: sprint-2 iter-1 pass at a49e22db7d3a83f324d795d9f974fb3aa9094616; phase=pr
- [2026-11-18T08:44:00Z] decision: contract frozen (round 2 mutual accept; no Planner ruling) @ 90468cada771f12c9e1a7382fe0c578a347bbcef
