---
epic: agent-title-system
generated_at: 2026-11-17T16:38:35Z
approved: true
sprints:
  - n: 1
    feature: title-protocol
    bundling: split
    dependencies: []
    risk: medium
    generator_backend: omp
    generator_backend_reason: "user 已确认 omp；P0 为提示词注册与解析契约，UI-neutral，host runtime 可直接跑既有 pytest 与 prompt-surface 快照"
  - n: 2
    feature: title-model-plan
    bundling: split
    dependencies: [1]
    risk: medium
    generator_backend: omp
    generator_backend_reason: "user 已确认 omp；P1 为纯路由/计划逻辑，host runtime 便于用现有 aux 层测试夹具验证顺序与去重"
  - n: 3
    feature: title-consumers-and-addressing
    bundling: split
    dependencies: [2]
    risk: high
    generator_backend: omp
    generator_backend_reason: "user 已确认 omp；P2 跨 schema 迁移、异步写入与寻址契约，host runtime 与同一 pytest/临时 HERMES_HOME 工具链减少环境分叉"
  - n: 4
    feature: session-card-and-fork
    bundling: split
    dependencies: [3]
    risk: high
    generator_backend: omp
    generator_backend_reason: "user 已确认 omp；P3 涉及副回合缓存前缀不变量与 Go→Jinja 模板转换，host runtime 便于直接对照现有 prompt cache 测试"
  - n: 5
    feature: tool-card-titles-and-docs
    bundling: split
    dependencies: [4]
    risk: low
    generator_backend: omp
    generator_backend_reason: "user 已确认 omp；P4 为登记性契约测试与文档/配置注释同步，纯仓库内改动"
---

# Roadmap: agent-title-system

## Sprint Summary

| # | Feature | Goal (observable) | Bundling | Depends on | Risk | Backend |
|---|---|---|---|---|---|---|
| 1 | title-protocol | 未注册的 manifest 键被拒绝；`title_system`/`task_label` 以 `scope=request` 注册并在 `title_language` 空/非空两种字节下渲染；`<title>`、`<title/>`、`none`、无标签散文、多行输出按单一标记契约解析；旧 JSON schema 路径与 echo 守卫不再有调用点，`retitle-skills` 走同一解析器 | split | — | medium | omp |
| 2 | title-model-plan | 无角色 pin 时计划只含当前会话模型且只调用一次；pin tiny/commit/smol 时调用顺序为 tiny→commit→smol→current；配置链展开后等价路由只出现一次，每条路由只试一次（`fallback=False`）；`enabled`/`model_upgrade_enabled=false` 时不发起 stage 1；本地候选失败继续下一候选 | split | 1 | medium | omp |
| 3 | title-consumers-and-addressing | 现有标题/漂移测试全绿；具名子代理异步获得 write-once 标签且 echo 守卫拒绝 handle/`Name-2`；空 assignment 不生成；legacy store 自动补 `agent_session.title` 列与索引且不回填、非空值不被覆盖；jsonl/md 同 stem、碰撞带后缀、创建后不重命名；`agent_message.to` 按 label→id→name→subject 解析、重复 label 返回候选、`agent_out` 语义不变 | split | 2 | high | omp |
| 4 | session-card-and-fork | 卡片按属性/`<icon> <CODE>:` 行/`CODE:` 前缀三种形态解析；fork 失败/超时/拒绝回落候选计划并在 `card_icons≠boring` 时补一次卡片请求；`boring` 不请求也不解析卡片；重生成沿用当前卡片；`/rename` 不触发模型、无 Nerd Font 消费；`title_language` 两种字节下 `title_fork`/`title_card` 可渲染 | split | 3 | high | omp |
| 5 | tool-card-titles-and-docs | 工具卡标题仍即时、display-only、无模型调用；`execute_code` 逐 cell 标题与既有行为一致；命名路由改动不影响卡片；`configuration.md`、`docs/prompt-system.md` 与 `aux_slots.py`/`config_defaults_auxiliary.py` 的注释不再把 `title_generation` 描述成标题唯一来源，且请求级提示词清单含 `title_system`/`task_label`/`title_card`/`title_fork` | split | 4 | low | omp |

## Bundle Groups

无 bundle group：五个 sprint 各自独立 PR（均为 `bundling: split`）。理由逐对如下：

- **sprint 1 × 2**：2 只消费 1 产出的标记协议与已注册提示词，不写入任何 schema；1 的 manifest/解析器是 2 的只读前提，分开落地不会让同一迁移写两次。
- **sprint 2 × 3**：2 是有序候选计划，3 是消费者接线。3 写入的新 schema（`agent_session.title` + 索引）在 2 中不存在，2 也不改任何消费者；共享的只是 `call_llm` 调用边界，属于顺序依赖而非同一 seam 的双重接线。
- **sprint 3 × 4**：4 在 3 已建立的会话标题写路径上增加一个生成器分支（fork）与标题字符串子形态（卡片）。卡片不是新列或新 payload 字段，仍写 `sessions.title`；`agent_session.title` 迁移只在 3 发生一次。若合并成一个 PR，会把寻址/迁移的证据面与 fork 缓存前缀不变量混在同一评审里，反而降低可验收性，因此保持 split。
- **sprint 4 × 5**：5 是登记性测试与文档同步，不写 schema，也不改运行时契约；它与 4 的耦合只是"文档需描述已存在的 `title_card`/`title_fork` 与 `generator`/`card_icons`"，靠依赖边满足，不需要同 PR。
- **sprint 1 × 3 / 5**：1 的解析器与注册在 3 之前已稳定；5 的文档不触碰协议实现。无共享可写 schema 或公共签名。

### 为什么 D14 P2 保持一个 sprint（不按消费者再拆）

P2 的四件事（会话标题/漂移改道、任务标签、子代理 `topic`、`agent_message` label-first 寻址）共用同一个 stage-0 派生函数与同一个 stage-1 驱动入口，且共同写入唯一的 `agent_session.title` 列（标签即地址别名）。按消费者拆开会让同一列迁移、同一写入事务与同一寻址解析器在两个 PR 里各落一次；标签的"显示名 + 自由文本地址"也不能只交付一半。因此这是一个能力边界，而不是四个可独立发布的特性。

## Ordering rationale

依赖链为 **1 → 2 → 3 → 4 → 5**（`dependencies`：1:[]，2:[1]，3:[2]，4:[3]，5:[4]）。

- **1 先行**：候选混跑后协议必须唯一。未注册提示词、未裁决的解析器会让后续每个生成器路径各带一套兜底；JSON 退役与标记契约是同一次决策，必须先冻结。
- **2 在 3 之前**：会话标题与任务标签的 stage 1 都由 `title_model_plan()` 驱动。没有去重后的有序计划，3 只能临时直连单个 aux 槽位，正是本 epic 要删除的形态。
- **3 在 4 之前**：fork 是 3 建立的会话标题路径的另一个生成器分支；卡片是 3 所写 `sessions.title` 的字符串子形态。先有写路径与 `source=llm`/CAS 语义，4 才只是"换驱动 + 解析卡片"。
- **4 在 5 之前**：5 的文档必须列出实际注册的请求级提示词（`title_card`/`title_fork`）与新增配置键（`generator`/`card_icons`），这些只有 4 完成后才存在；同时 5 的验收要证明"路由改动不影响工具卡"，需要 4 的完整路由面已就位。
- **风险顺序**：本链是严格线性依赖，不存在 dependency-peer，因此 risk-first 规则无触发点；两个 high-risk sprint（3、4）连续排在中段，使得 schema/寻址与缓存前缀两类不确定性在 epic 早期暴露。

## Backend rationale

五个 sprint 全部为 `generator_backend: omp`（用户在 roadmap 阶段前已确认；`_config.yml.sprint_level_generator_override: true`，故 per-sprint 值生效）。

- **通用理由**：`omp` 是 host runtime，与 orchestrator 共享工具面、权限模型与会话语义；本 epic 的验收全部是 pytest（temp `HERMES_HOME`）+ 既有 prompt-surface 快照，不需要跨厂商实现的独立环境。
- **sprint 1（title-protocol）**：提示词注册与解析契约，UI-neutral；omp 可直接跑 `tests/agent/test_prompt_sections.py` 与快照重生成。
- **sprint 2（title-model-plan）**：纯路由逻辑，依赖现有 aux 层与测试夹具；omp 的同一 shell/pytest 面便于快速迭代顺序与去重断言。
- **sprint 3（title-consumers-and-addressing）**：schema 迁移 + 异步写入 + 寻址，属 backend/schema 性质；按 rubric 主选仍是 host runtime，避免为同一 pytest 套件再搭 codex 环境。
- **sprint 4（session-card-and-fork）**：涉及 prompt cache 前缀不变量与 Go→Jinja 转换，需要直接对照现有缓存测试；omp 无需额外 CLI 安装。
- **sprint 5（tool-card-titles-and-docs）**：登记性测试与文档，omp 即可。

## Risks

1. **P2/P3 的 fork 顺序张力（主要残留歧义）**：D14 P2 写"会话标题/漂移改走生成器（fork→plan）"，而 fork 本身在 P3 才落地。sprint 3 必须把生成器开关接好、以 `plan` 为实际生效路径，并保证 `generator=fork` 不会在 fork 未实现时静默不命名（临时按 plan 运行或显式降级）；sprint 3 的验收按 plan 路径判定，sprint 4 的契约再补 fork 路径。该边界需在 sprint-3 契约协商时写死。
2. **协议退役的回归面**：JSON schema、`_title_from_reasoning`、echo 守卫被删除后，`sessions retitle-skills`、漂移再生成与缓存提示词身份重写都会经过新解析器。sprint 1 必须以"旧 JSON 测试改写为标记契约 + 无调用点残留"为硬验收，sprint 3 以"现有标题/漂移测试全绿"为回归底线。
3. **缓存不变量易被无意破坏**：命名请求不得进入 transcript/工具结果/系统提示，fork 副回合不得推进主 leaf。需要在 sprint 3/4 断言线程边界与 write-once，而不是只测返回值。
4. **标签寻址的并发与歧义**：异步写入 + 同 profile 存活 run 唯一后缀 + 歧义拒绝必须同事务完成；legacy 行仍可能重复标题。测试需覆盖"label 未生成时回退 id/name/subject 且不阻塞"。
5. **子代理产物身份固化**：`topic` 在创建时写死，标题漂移/标签生成均不得重命名；中文/emoji 走 digest、碰撞加后缀。任何"顺手重命名"的实现都会破坏 `agent_out` 与 manifest 路径。
6. **路由成本与重复调用**：`fallback=False` 是防止同一路由被 plan 与 `call_llm` 各试一次的关键；"每个候选只试一次、不并发、不取更优"需要显式测试，否则计费与延迟语义漂移。
7. **文档与提示词清单漂移**：ADR-19 要求每个注册提示词都有条件入口；sprint 5 若漏掉 `title-marker-instruction.md` 的"仅存档"标注或把 `title_generation` 仍写成唯一来源，会与 P0–P3 的实现冲突。
8. **配置兼容**：`auxiliary.title_generation` 的 route 字段保留为显式覆盖（候选 0），不做配置迁移；ADR-21 开放问题 1 明确不在本 epic 内。
