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

- goal: 命名协议唯一化——注册命名提示词，冻结 `<title>` 标记解析契约，退役 JSON schema 路径
- AS-1 请求级注册 `title_system`（context `title_language`）/ `task_label`，StrictUndefined 下可渲染
- AS-2 `title-system.md` 的 Jinja 语言行：空值不输出，非空输出 `- Write the title in <language>.`
- AS-3 单一 `<title>` 标记解析契约（闭合/自闭合/`none`/无标签散文/多行取首非空行）
- AS-4 共享守卫剥离引号/`Title:`/句末标点，>80 字符或 >12 词或 0 词拒绝（不截断），先剥 `<think>`
- AS-5 JSON schema 路径四符号无定义无调用点，`retitle-skills` 复用同一解析器，`title-marker-instruction.md` 保持未注册

## Contract
<!--
  Reference to the frozen contract (post-negotiation) with its commit SHA.
  Format: `sprint-<N>-contract.md @ <SHA>`
-->

- `sprint-1-contract.md @ 3bb8222f938633b5b8bd841bd27a9cec708dcc55` (status: active; max_iterations=6; rubric F1.0 / C0.85 / Co0.9 / D0.8)

## Negotiation
<!--
  Orchestrator-curated summary of negotiation rounds. Raw per-agent messages
  live in feedback/{role}-<iter>.md. Keep this terse: round, outcome.
-->

- [2026-11-18T05:27:47Z] round=1 agent=generator signal=counter delta=F1.0/C0.85/Co0.85/D0.7 iter6 file=feedback/generator-neg-1.md
- [2026-11-18T05:27:47Z] round=1 agent=evaluator signal=counter delta=F1.0/C0.85/Co0.9/D0.8 iter6 file=feedback/evaluator-neg-1.md
- [2026-11-18T05:32:05Z] round=2 agent=generator signal=accept delta=adopts F1.0/C0.85/Co0.9/D0.8 iter6 file=feedback/generator-neg-2.md
- [2026-11-18T05:32:05Z] round=2 agent=evaluator signal=skipped delta=generator accepted round-1 proposal file=(none)

## WorkLog
<!--
  Timestamped summary of each Generator iteration.
  Format: `[<ISO-8601>] iter=<N> agent=generator action=<summary> commit=<SHA>`
  Detailed per-iteration notes go to feedback/generator-<iter>.md.
-->

- [2026-11-18T06:43:58Z] iter=1 agent=generator action=marker-contract parser + manifest registration + JSON-path retirement + tests/eval/docs commit=pending

## Evaluation
<!--
  Timestamped summary of each Evaluator verdict.
  Format: `[<ISO-8601>] iter=<N> agent=evaluator verdict=<pass|fail> axes=<...>`
  Evidence paths (screenshots, traces) and rubric raw scores go to
  feedback/evaluator-<iter>.md and evidence/.
-->

- [2026-11-18T07:26:31Z] iter=1 agent=evaluator verdict=pass axes=functionality 1.0 / craft 0.9 / consistency 0.95 / documentation 0.9 evidence=evidence/iter-1/

## Decisions
<!--
  Orchestrator records irreversible state transitions here:
    - sprint status changes (negotiating → active → done | aborted)
    - Principal Skinner stops (reason, state.json cursor at stop)
    - PR creation (PR number, bundling mode)
    - Human escalations (pending_human set, who approved, when resumed)
-->

- [2026-11-18T05:32:45Z] decision: contract frozen after negotiation rounds=2 (generator accepted evaluator round-1 proposal); commit=3bb8222f938633b5b8bd841bd27a9cec708dcc55
- [2026-11-18T07:26:31Z] decision: sprint-1 iteration 1 passed all four thresholds; contract status=done, phase=pr (Step 8 PR creation next)
