---
sprint: 2
feature: title-model-plan
bundling: split
generator_backend: omp
generator_backend_reason: "user 已确认 omp；P1 为纯路由/计划逻辑，host runtime 便于用现有 aux 层测试夹具验证顺序与去重"
goal: |
  交付 title_model_plan() 与计划执行器：按 omp 同构顺序产出有序、去重的候选路由，逐条以 fallback=False 单次调用并按拥有该候选的 task 计时/计费，门控关闭或计划为空时不发起任何模型调用。
acceptance_scenarios:
  - id: AS-1
    text: 无角色 pin、无链配置时，title_model_plan(main_runtime) 只返回当前会话模型一个候选；执行器只调用一次 call_llm，且传显式 provider/model/base_url、task=title_generation、fallback=False、timeout=title_generation.timeout。
  - id: AS-2
    text: 显式 pin tiny/commit_message/smol 时计划任务顺序为 tiny→commit_message→smol→当前会话模型，每个候选超时取拥有它的 task；provider 为 auto 的槽位即使填了 model 也不参与；当前会话模型只追加一次。
  - id: AS-3
    text: auxiliary.title_generation 的 route 字段任一非默认时，它作为候选 0（task=title_generation）且排在所有角色候选之前；默认配置（provider=auto 且 model/base_url/api_key 为空）时计划中不存在该前置候选。
  - id: AS-4
    text: 每个候选按 model-keyed 精确键→provider 通配键→per-task fallback_chain 的顺序深度优先递归展开成显式路由；等价路由三元组只出现一次，自引用/环不会无限展开；当前会话模型若已由前序候选覆盖则合并而不重复。
  - id: AS-5
    text: auxiliary.title_generation.enabled=false 或 model_upgrade_enabled=false 时 title_model_plan 返回空计划且执行器零模型调用；无 pin、无链、无当前会话模型时空计划同样零调用。
  - id: AS-6
    text: 执行器对每个候选最多发起一次 call_llm；候选失败（异常、空输出或不合格输出）继续下一候选，首个成功即停；<title/> 或 <title>none</title> 立即终止且不再调用后续候选；全程不并发、不比较、不重试，每次调用带 fallback=False、task 与超时均归属于该候选的 task。
rubric:
  - axis: Functionality
    weight: high
    threshold: 1.0
    description: AS-1 至 AS-6 全部通过；顺序、去重、门控、终止与失败续跑均可从 pytest 输出观察。
  - axis: Craft
    weight: std
    threshold: 0.85
    description: 路由/执行逻辑可读且无第二套配置解析；测试用 fake call_llm 记录调用而非计时；遵循 agent/AGENTS.md 的 facade + sibling 与 late-import 规则。
  - axis: Consistency
    weight: std
    threshold: 0.9
    description: 复用现有 aux task 配置、main runtime 归一化、fallback chain 读取与门控；不改变 call_llm 语义、既有标题行为或公开配置形状。
  - axis: Documentation
    weight: low
    threshold: 0.85
    description: 公共入口 docstring 说明顺序、去重键、门控与终止语义；本 sprint 不新增用户文档。
max_iterations: 8
status: active
---

# Sprint 2 Contract — title-model-plan

## Scope

### In scope

- 新增 `agent/title_router.py`（facade 的 sibling，避免 `title_generator.py` 继续膨胀）：
  - `title_model_plan(main_runtime)`：构造有序、去重的显式候选列表。
  - `run_title_plan(plan, messages, ...)`：按计划逐条单次调用 `call_llm`，返回首个被接受的标题或 `None`。
- 新增 `tests/agent/test_title_router.py` 作为本 sprint 的唯一主测试面；复用现有 aux 测试夹具与临时 `HERMES_HOME`。
- 更新 `agent/AGENTS.md` §"Model and provider resolution"（:99-107）：新增一条 bullet 命名 `agent/title_router.py`，说明 stage-1 候选顺序（显式覆盖 → tiny → commit_message → smol → 当前会话模型）、归一化三元组去重与 `fallback=False` 单次执行。
- 候选对象至少暴露 `task`、`provider`、`model`、`base_url`、`timeout` 五个字段（dataclass 或映射均可），测试按字段名读取。

### Out of scope

- **任何消费者接线**：不修改 `generate_title` / `auto_title_session` / `maybe_auto_title` / `apply_instant_title` / 漂移检查 / `generate_label` / 任务标签 / 子代理 topic。会话标题与标签在 sprint 3 接线；本 sprint 只交付计划与执行原语。
- 不写 schema、不迁移 `agent_session.title`、不改 `sessions.title`、不做 fork/卡片（sprint 4）、不做工具卡标题（sprint 5）。
- 不改提示词 manifest 与模板（sprint 1）；解析语义直接复用 sprint 1 的单一标记解析入口，不私建第二套解析。
- 不改 `agent/auxiliary_client.py` 的内部走查逻辑：计划层只通过显式 `provider` / `model` / `base_url` 与 `fallback=False` 调用，不新增、不重排、不修改 `_try_configured_fallback_chain`、`_try_main_agent_model_fallback` 或 `call_llm` 的 fallback 语义。
- 不新增配置键；不消费 `generator` / `card_icons`（sprint 4）；不处理 `title_generation` 的配置迁移。
- 不引入候选并发、best-of、跨候选共享响应或计划层重试。

## Acceptance Scenarios (executable)

所有场景由 `tests/agent/test_title_router.py` 中的独立 pytest 用例覆盖：用 `tmp_path` 写临时 `HERMES_HOME/config.yaml`，`main_runtime` 直接以 dict 注入，`call_llm` 用 fake 记录/返回，断言调用参数与调用顺序，不做墙钟计时断言。

### AS-1: 无 pin 时只调当前会话模型一次

```
Given 临时 HERMES_HOME 中 auxiliary.title_generation 为默认 route（provider=auto、model/base_url/api_key 为空），
      auxiliary.tiny/commit_message/smol 均未 pin，且未配置任何 fallback_chain 或 fallback_chains；
      auxiliary.title_generation.timeout=31
When  title_model_plan(main_runtime={"provider": "openrouter", "model": "main-x", "base_url": "https://openrouter.ai/api/v1"})
      随后 run_title_plan(plan, messages=[...])
      且 fake call_llm 返回 "<title>Fix login button</title>"
Then  plan 恰有一个候选：task=title_generation、provider=openrouter、model=main-x、
      base_url=https://openrouter.ai/api/v1、timeout=31；
      fake 只记录一次调用，kwargs 含 provider=openrouter、model=main-x、base_url=...、
      task=title_generation、timeout=31、fallback=False；
      run_title_plan 返回 "Fix login button"。
```

Evidence: `sprints/sprint-2-title-model-plan/evidence/AS-1.txt`

### AS-2: pin 角色时按 tiny→commit_message→smol→current 排序

```
Given auxiliary.tiny = {provider: "openrouter", model: "tiny-a", timeout: 11}，
      auxiliary.commit_message = {provider: "nous", model: "commit-b", timeout: 12}，
      auxiliary.smol = {provider: "openrouter", model: "smol-c", timeout: 13}，
      auxiliary.title_generation = {provider: "auto", model: "", timeout: 31}，main_runtime 为 openrouter/main-x
When  title_model_plan(main_runtime) 被调用
Then  候选的 task 顺序恰为 tiny、commit_message、smol、title_generation；
      provider/model 分别为 openrouter/tiny-a、nous/commit-b、openrouter/smol-c、openrouter/main-x；
      对应 timeout 为 11、12、13、31；
      另测 auxiliary.smol = {provider: "auto", model: "smol-c", timeout: 13} 时 smol 不出现，
      顺序变为 tiny→commit_message→title_generation；
      任何情况下当前会话模型候选只出现一次且位于末尾（未被去重合并时）。
```

Evidence: `sprints/sprint-2-title-model-plan/evidence/AS-2.txt`

### AS-3: 显式 title_generation route 仅非默认时作为候选 0

```
Given auxiliary.title_generation = {provider: "openrouter", model: "explicit-t", base_url: "https://ex.example/v1", timeout: 20}，
      且 tiny/commit_message/smol 均已 pin；
      main_runtime = openrouter/main-x
When  title_model_plan(main_runtime) 被调用
Then  plan[0] 的 task=title_generation、provider=openrouter、model=explicit-t、
      base_url=https://ex.example/v1、timeout=20；
      其后依次是 tiny、commit_message、smol、current 的候选；
      将 title_generation 改回 provider=auto、model/base_url/api_key 均为空后重新构造计划，
      计划中不再存在前置候选 0（角色候选从 tiny 开始；末尾仍按三元组去重保留当前会话模型的
      task=title_generation 候选，或在该路由已被前序角色候选覆盖时合并）。
```

Evidence: `sprints/sprint-2-title-model-plan/evidence/AS-3.txt`

### AS-4: 链递归展开、去重且不因环无限展开

```
Given auxiliary.tiny = {
        provider: "openrouter", model: "tiny-a", timeout: 11,
        fallback_chain: [{provider: "nous", model: "tiny-b"}, {provider: "nous", model: "tiny-b"}],
      }；
      auxiliary.fallback_chains = {
        "openrouter/tiny-a": [{provider: "nous", model: "tiny-c"}],
        "nous/*": [{provider: "openrouter", model: "tiny-a"}],
      }；
      commit_message/smol 未 pin，main_runtime = openrouter/main-x
When  title_model_plan(main_runtime) 被调用
Then  候选路由顺序为 openrouter/tiny-a → nous/tiny-c → nous/tiny-b → openrouter/main-x；
      重复的 nous/tiny-b 只出现一次，nous/* 回指 openrouter/tiny-a 时停止而不再展开；
      所有候选的 task 仍为 tiny（链上路由继承拥有它的 task），timeout 仍为 11，
      仅当前模型候选为 task=title_generation、timeout=title_generation.timeout；
      另测某角色 route 与 main_runtime 完全相同：该路由只保留首次出现的角色候选，
      末尾不再追加重复的当前模型候选。
```

Evidence: `sprints/sprint-2-title-model-plan/evidence/AS-4.txt`

### AS-5: 门控与空计划零模型调用

```
Given 已 pin tiny 且 main_runtime 为 openrouter/main-x；
      auxiliary.title_generation.enabled=false（model_upgrade_enabled 仍为 true）
When  title_model_plan(main_runtime) 与 run_title_plan(plan, messages) 被调用
Then  plan 为空列表，fake call_llm 调用次数为 0，执行器返回 None；
      将 enabled 改回 true、model_upgrade_enabled=false 后重复，结果相同（空计划、零调用）；
      最后清空所有 pin、删除 main_runtime 且 title_generation 保持默认 route，
      空计划同样不产生任何 call_llm 调用。
```

Evidence: `sprints/sprint-2-title-model-plan/evidence/AS-5.txt`

### AS-6: 单次调用、失败续跑、成功即停、标记终止

```
Given 计划已构造为 tiny(local) → commit_message → smol → current，四者路由互异；
      其 timeout 依次为 11、12、13、31
When  fake call_llm 对 tiny 抛 RuntimeError("local down")，对 commit_message 返回
      "<title>Fix flaky park tests</title>"，后续候选的 fake 会记录额外调用
Then  记录到的 task 顺序恰为 ["tiny", "commit_message"]（串行、按计划顺序），每个恰好一次；
      smol 与 current 未被调用；run_title_plan 返回 "Fix flaky park tests"；
      所有调用的 kwargs 都含 fallback=False，且 fallback 链未被 call_llm 二次走查；
      对应调用的 timeout 分别为 11、12，task 与实际候选一致；
      另测：tiny 返回 "<title/>" 时只调用 tiny 一次、返回 None、commit_message 不再被调用；
      tiny 返回 "<title>none</title>" 时同样终止；
      tiny 返回空文本时按该候选失败继续，commit_message 仍会被调用一次；
      任意用例中同一候选都不会被第二次调用（无计划层重试、无并发、无 best-of）。
```

Evidence: `sprints/sprint-2-title-model-plan/evidence/AS-6.txt`

## Rubric Detail

| Axis | Weight | Threshold | Scoring notes |
|---|---|---|---|
| Functionality | high | ? | AS-1…AS-6 必须全绿；顺序、去重、门控、终止与失败续跑都能从 pytest 输出直接读出。 |
| Craft | std | ? | 路由与执行逻辑单一职责；测试用 fake 调用记录；无死代码/重复解析；遵循 `agent/AGENTS.md`。 |
| Consistency | std | ? | 复用 `_get_auxiliary_task_config`、`_normalize_main_runtime`、`auxiliary_fallback_chains` 与现有门控；不改变 `call_llm`、既有标题/漂移测试行为。 |
| Documentation | low | ? | 公共函数 docstring 说明构造顺序、去重键、门控与终止；不新增用户文档。 |

Total pass condition: **every axis meets its threshold**. Weight only affects the
Evaluator's ordering when reporting failures (high-weight fails reported first).

## Notes for Generator & Evaluator

1. **不接线消费者。** 本 sprint 不修改 `generate_title`、`auto_title_session`、`maybe_auto_title`、漂移检查、任务标签或任何 `session`/`agent_message` 路径；`title_generator.py` 最多增加对 `agent/title_router.py` 的 late import 出口，既有调用点与返回值不变。sprint 3 才接线。
2. **不改 `call_llm` 内部。** 计划层把每个候选展开成显式路由后调用 `call_llm(provider=…, model=…, base_url=…, task=<role>, timeout=…, fallback=False, messages=…)`；禁止为了让计划工作而修改 `auxiliary_client.py` 的 fallback/重试/路由解析。`base_url` 为空时传 `None`。
3. **“每个候选只一次”是计划层语义。** 断言的是 `call_llm` 对同一候选只被调用一次；`call_llm` 自身既有的 same-provider transient 重试属于未改动的 transport 行为，不得由计划层再包一层重试或由此被判失败。
4. **配置读取复用现有入口。** 角色/task 的 route 与 timeout 用 `_get_auxiliary_task_config`；model-keyed 与 per-task 链用 `agent/auxiliary_fallback_chains.py` 的读取函数（`auxiliary_section` / `model_fallback_chains_for_route` 等）；main runtime 用 `_normalize_main_runtime`；门控复用 `title_generator._auto_title_enabled` / `_model_title_upgrade_enabled`（late import，避免模块级循环）。不要复制一套 YAML 解析。
5. **去重键。** `(provider.strip().lower(), model.strip(), base_url.strip().rstrip("/"))`；provider 只比较归一化值。空 provider 或空 model 的**链/展开路由**不进入计划（与 `_resolve_fallback_entry` 一致）；当前模型候选要求 `main_runtime` 归一化后同时有 provider 与 model。角色槽位只以 `provider` 为非 `auto`/空 为门控：`auxiliary.tiny` 等角色即使填了 model，provider 为 auto/空仍按 D3 第 2 条视为未配置、完全不生成候选；`model` 为空时保留空串，由 `call_llm` 经 `resolve_provider_client` 的现有阶梯解析（provider 默认 aux 模型 → 主模型），计划层不从 `main_runtime` 借用。候选 0 仅在 `provider` 为 `auto`/空时用 `main_runtime` 补齐其未填的 provider/model/base_url；`provider` 已 pin 时候选 0 的 route 字段整体来自该 task 自身 block，不借 `main_runtime` 的 model/base_url（继承 base task 时同样整块移动：任一 route 字段 pin 后不再继承 base 的 route 字段，`_AUX_ROUTE_KEYS` 单元移动语义）。补齐后的候选 0 仍按上述三元组参与去重，因此当前模型候选不会因这类槽位重复。
6. **顺序固定。** 显式覆盖候选 0 → 角色 `tiny`、`commit_message`、`smol` → 当前会话模型。每个候选的链展开是深度优先：先输出候选本身，再依次走 `fallback_chains[provider/model]` → `fallback_chains[provider/*]` → `auxiliary.<task>.fallback_chain`，后加入的路由同样递归展开；用 visited 集合防环。链上路由继承发起它的 task（计费/限额/超时归属）。
7. **超时。** 候选携带 `auxiliary.<owning task>.timeout`；当前模型候选固定用 `auxiliary.title_generation.timeout`（默认 30s）。不要在链条目上读取 entry-level timeout（D3 只授权 task 级超时）。
8. **门控在构造期生效。** `enabled=false` 或 `model_upgrade_enabled=false` 时 `title_model_plan` 直接返回 `[]`；执行器对空计划必须 0 调用。两个开关都不影响 stage 0（本 sprint 不触达 stage 0）。
9. **解析复用 sprint 1。** 执行器用 sprint 1 交付的单一标记解析入口判断“接受 / 候选失败 / 终止”；`<title/>` 与 `<title>none</title>` 是终止而非失败。实现时以 sprint 1 实际导出的符号为准（从 `agent/title_generator.py` 的公开面导入），不得在 router 内复制标记解析逻辑，也不得为绕过依赖而自建第二套解析。
10. **计数用 fake，不用计时。** 所有模型调用断言基于 fake `call_llm` 的调用记录（路由、task、timeout、fallback 标志、顺序、次数）；不测量耗时，不启动后台线程，不访问网络。测试写临时 `HERMES_HOME/config.yaml`，复用 `tests/agent/conftest.py` 的隔离与快重试夹具。
11. **模块边界。** `agent/title_router.py` 模块级导入 `call_llm`，测试以 `monkeypatch.setattr("agent.title_router.call_llm", fake)` 拦截；router 内不得在模块导入期读取配置（配置只在函数调用期读取）。
12. **测试入口。** 一律 `scripts/run_tests.sh <test paths> [-- pytest 参数]`；sprint-local 的 `eval-<feature>.sh` 调用该 runner 并保留输出/退出码，禁止裸 `pytest`（`AGENTS.md`、`docs/coding-rules.md`）。

## Negotiation Log

### Round 1

- **Generator**: `counter` — Functionality 1.0, Craft 0.85, Consistency 0.9, Documentation 0.8, max_iterations 6. Verified the four reuse entry points are read-time and that `parse_title_output` separates accept / terminal / reject. (`feedback/generator-neg-1.md`)
- **Evaluator**: `counter` — same axes except Documentation 0.85 and max_iterations 8, plus `agent/AGENTS.md` §"Model and provider resolution" in scope (docs omission would be a non-downgradable Critical). Confirmed `_aux()` defaults every role slot to `provider: auto`, closing the Generator's AS-1/AS-2 premise risk. (`feedback/evaluator-neg-1.md`)

### Round 2

- **Generator**: `accept` — all three round-1 deltas accepted; proposed one contract-precision fix to note 5 (role slots gate on `provider` only; empty `model` preserved and resolved in `call_llm`; candidate-0 backfill only when `provider` is auto/empty; chain entries keep the strict rule). (`feedback/generator-neg-2.md`)
- **Evaluator**: `accept` — thresholds frozen at Fn 1.0 / Craft 0.85 / Consistency 0.9 / Documentation 0.85, max_iterations 8; note-5 paragraph accepted with three editorial completions (auto/empty gate, both rungs of the resolution ladder, dedupe participation after backfill, and the unit-move parenthetical scoped to inheritance); AS-3 executable body corrected. (`feedback/evaluator-neg-2.md`)

### Ruling

- **Planner**: not required — mutual agreement in round 2.

## Sprint Outcome

- **Final iteration**:
- **Last commit**:
- **Aborted reason** (if any):
- **PR**:
