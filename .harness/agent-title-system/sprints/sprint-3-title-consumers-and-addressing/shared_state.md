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

- **Goal**: 把 sprint 1 的标记协议与 sprint 2 的候选计划接到四个消费者（会话标题/漂移、子代理任务标签、子代理产物 `topic`、`agent_message` 寻址），并落地 `agent_session.title` + legacy reconciler + write-once 写入。
- **Acceptance scenarios**: AS-1 plan 路径与 fork 降级标记、漂移 CAS；AS-2 异步任务标签守卫（9 词/80 字符/echo handle、daemon 线程）；AS-3 四类跳过零调用零写入；AS-4 列/索引迁移幂等、write-once UPDATE；AS-5 `derive_topic` 规则与碰撞后缀；AS-6 路径冻结、不重命名；AS-7 label→id→name→subject 解析与重复标签候选拒绝。
- **Test surface**: `tests/agent/test_title_generator.py`、`test_title_drift.py`、`test_title_input.py`（追加）、新增 `tests/agent/test_task_label.py`；`scripts/run_tests.sh` 执行。


## Contract
<!--
  Reference to the frozen contract (post-negotiation) with its commit SHA.
  Format: `sprint-<N>-contract.md @ <SHA>`
-->

- sprint-3 contract frozen @ 91aeb160fd94bbe0f6c138a96c681e690d529965

## Negotiation
<!--
  Orchestrator-curated summary of negotiation rounds. Raw per-agent messages
  live in feedback/{role}-<iter>.md. Keep this terse: round, outcome.
-->

- [2026-11-18T09:58:00Z] round=1 agent=generator signal=counter delta=Fn=1.0/Craft=0.85/Cons=0.9/Doc=0.85/max_iter=8 + AS-4 Given correction file=feedback/generator-neg-1.md
- [2026-11-18T10:04:00Z] round=1 agent=evaluator signal=counter delta=same thresholds; scope += generator config key defaults+docs+example, tools/AGENTS.md clause; AS-4 approved file=feedback/evaluator-neg-1.md
- [2026-11-18T10:12:00Z] round=2 agent=generator signal=accept delta=scope additions accepted; AS-4 body frozen file=feedback/generator-neg-2.md

## WorkLog
<!--
  Timestamped summary of each Generator iteration.
  Format: `[<ISO-8601>] iter=<N> agent=generator action=<summary> commit=<SHA>`
  Detailed per-iteration notes go to feedback/generator-<iter>.md.
-->

- [2026-11-18T10:55:00Z] iter=1 agent=generator action=four consumers + agent_session.title migration + label-first addressing + drift gate fix; 7 evidence files green — see feedback/generator-1.md

## Evaluation
<!--
  Timestamped summary of each Evaluator verdict.
  Format: `[<ISO-8601>] iter=<N> agent=evaluator verdict=<pass|fail> axes=<...>`
  Evidence paths (screenshots, traces) and rubric raw scores go to
  feedback/evaluator-<iter>.md and evidence/.
-->

- [2026-11-18T12:05:00Z] iter=1 agent=evaluator verdict=pass axes=functionality 1.0 / craft 0.9 / consistency 0.92 / documentation 0.9 evidence=evidence/iter-1/ (82/82 independent probe; 324 tests green) — see feedback/evaluator-1.md

## Decisions
<!--
  Orchestrator records irreversible state transitions here:
    - sprint status changes (negotiating → active → done | aborted)
    - Principal Skinner stops (reason, state.json cursor at stop)
    - PR creation (PR number, bundling mode)
    - Human escalations (pending_human set, who approved, when resumed)
-->

- [2026-11-18T12:14:00Z] decision: PR opened sprint-3 https://github.com/chaorenex1/My-H/pull/11 (bundling=split)
- [2026-11-18T12:08:00Z] decision: sprint-3 iter-1 pass at 7eaad6cbbb14ee4791444148de286ed36e7ee24e; phase=pr
