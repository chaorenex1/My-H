# ADR-26：auto-graph——助手表格的 judge 图表规划（omp 移植）

**状态**：已决定（2026-11-17）。**本 ADR 是 auto-graph 落地契约的唯一权威**；ADR-12 D6 只保留
2026-11-04 的决策背景（其 `{session_id, markdown, table, guess}` 参数列表已过时，实际契约见 D2）。
**当前实现仍是单轮折叠**；四项决策（两轮候选图、客户端计算 takeaways/title、`auto` 无 native 时
按 `always` 且单表失败逐表回退、支持 on-device judge）按 D10 迁移，未完成项不声称已实现。
ADR-12 的选项 C 结论（`table_chart.plan` + 客户端接线）不变；ADR-17 P5 的 `auto-graph` 问题库在
P1 **扩展**（新增 `worth`/`metrics`/`conditions`/`chart`，预算改为 40/10），不是原样复用。

**关联**：omp `packages/coding-agent/src/auto-graph/planner.ts`、`docs/settings.md`
（`tui.autoGraph`）、pi-tui `charts/chart-plan.ts` 与 `chat/table-chart.ts`（本地 checkout
`C:\Users\zarag\projects\oh-my-pi`，HEAD `6a86df85`）；ADR-11（typed judgment、native 判定）、
ADR-17 P5（`auto-graph.json` 的初始问题集、15s 超时、无 judge 回退；本 ADR 扩展其问题集）、
ADR-4 D13（工具卡标题，**无关**：本 ADR 只处理助手回答里的 Markdown 表格）。

**范围**：助手回答中数值表格下方的图表规划与能力探测；host 产出 `ChartPlan` JSON，客户端 pi-tui
负责渲染、takeaways 与标题。**不做**：服务端绘图/图像生成、transcript 持久化、子代理/print/RPC/
ACP 输出的图表、图表交互、`table_chart.plan`/`table_chart.status` 之外的 omp 客户端代码。

**实现落点（当前工作树）**：

| 层 | 文件 |
|---|---|
| planner | `agent/auto_graph.py`（`judge_request`/`plan_from_answers`/`plan_table_chart`） |
| 问题库 | `agent/prompts/judge/auto-graph.json`（16 个 question id） |
| 网关 | `tui_gateway/methods_table_chart.py`、`tui_gateway/contracts/table_chart.py` |
| 客户端 | `omp-ui/src/modes/interactive-mode.ts:1177`（`#applyAutoGraphSetting`）、`:654`（`tableChartsVisible`）、`omp-ui/src/modes/settings.ts:468`（`tui.autoGraph`，默认 `auto`） |
| 测试 | `tests/agent/test_auto_graph.py`、`tests/tui_gateway/test_table_chart_rpc.py` |

---

## 问题

助手经常用 Markdown 表格比较模型、耗时、成本、通过率。把表格画成图能直接暴露趋势/对比，但“画什么
图”不是纯结构问题：

- **结构可猜，语义不可猜**：列类型能判断“这是数值列”，但判断不了“延迟越低越好”“base/var 是一组
  before/after”“这列是该行的目标上限”“这四行是分位数的阶梯”。
- **猜错比不画更糟**：把混合单位画成同一坐标轴、把标识符当度量、把单调递增的行重排，都会产生
  误导性图表。
- **不能阻塞回答**：图表是回答的附属品；判定服务超时/不可用时必须立刻退回本地猜测，绝不拖住
  turn，也绝不把表格内容写进 transcript。
- **不能把渲染放进 host**：客户端 pi-tui 已有 `ChartPlan` → 图表的完整实现；host 重复实现会形成
  两套渲染语义。

omp 的方案（`planner.ts`）是：`always` 用本地启发式；`smart` 让 judge 逐表决定；`auto` 在 judge
role 解析到 native TypeSafe jev 时等同 `smart`，否则 `always`；任何失败/超时/异常回退本地猜测，
不阻塞回答。ADR-12 D6 已把 Hermes 的选择定为 C（host `table_chart.plan` + 客户端接线）；本 ADR
记录**实际落地的规划算法、契约与和 omp 的差异**。

---

## 决策

### D1. 职责切分：客户端读表，host 只做语义精修

| 层 | 职责 |
|---|---|
| pi-tui（客户端） | 解析 Markdown 表格、推断 `TableAnalysis`、生成本地 `ChartPlan` **guess**、按 `chartMode` 决定是否画、实际渲染；**对最终 plan 计算 `tableTakeaways` 并选标题** |
| `omp-ui` | 设置项 `tui.autoGraph`；启动/设置变更时 `setTableCharts(mode, planner)`；`smart`/`auto` 时调 `table_chart.plan`；维护 native/on-device 能力缓存 |
| `agent/auto_graph.py` | **两轮 judge**：reading（worth/label/结果列/metrics/conditions/结构/语义）→ 候选图（最多 10 张）→ `chart` 选择；返回 `ChartPlan` |
| `tui_gateway` | `table_chart.plan` 与 `table_chart.status` 本地 RPC：profile 作用域、参数校验、结构化错误；不持久化 |

**host 不画图、不缓存图表、不写 transcript**。客户端拿不到 judge 时用本地 guess；host 拿不到
judge 时返回 `source:"local"`。**takeaways 与标题在客户端计算**（决策 2），host 不移植
`tableTakeaways`、不发起 title 判定。

### D2. RPC 契约：两次规划 + 一次能力探测、本地权威、无持久化

`tui_gateway/contracts/table_chart.py`：

```
table_chart.status  params: {profile}
                    result: {native: bool, on_device: bool}

table_chart.plan    params: {profile, markdown, table, guess, mode: "auto"|"smart" = "smart"}
                    result: {plan: ChartPlan|null, source: "judge"|"local"}
```

- `@_profile_scoped`：方法只服务本地 viewer；`profile` 是传输作用域，handler 在转给 planner 前
  剥离（`methods_table_chart.py`），planner 的 payload 不包含 `profile`。
- `table_chart.status` 只做本地能力探测：`native = has_native_judge()`，`on_device` = 解析出的
  候选链中存在 kind `local` 的候选；**不发起网络请求、不安装依赖**。客户端用它实现“`auto` 无
  native → `always`”，而不是每张表都等一次失败。
- 未知参数由 pydantic 拒绝（`4000`）；planner 异常统一映射为 `5100`；正常失败（无 judge、超时、
  答案非法、空表、无可画候选）**不是错误**，返回 `{"plan": null, "source": "local"}`。
- `mode="auto"` 只允许 **native judge**；`smart` 允许 native、on-device（`local`）与文本/链式
  judge（ADR-11 的 `resolve_judge`）。
- 表格内容只在该次请求里发送；不落盘、不进 transcript、不进系统提示。usage 记入会话账本，
  purpose `auto-graph`。

### D3. 规划流水线：两轮判定（reading → 候选图 → 选择）

目标对齐 omp `planner.ts` 的 `pickTableChart`；当前 `plan_table_chart` 的单轮折叠（`judge_request`
→ `plan_from_answers`）按 D10 P1 替换为：

1. **校验**：`markdown: str`、`table: Mapping`、`guess: Mapping`；缺失/类型不对 → `local`。
2. **判定 judge kind**：`auto` 必须在 `table_chart.status` 中 `native=true`，否则 host 直接
   `local`，客户端按 `always` 处理；`smart` 允许 native、on-device（`local`）与文本/链式 judge。
3. **第一轮 reading**（`readingQuestions` 的 Python 移植；**on-device 跳过这一轮**，用本地
   reading = guess + 结构化列分析）：
   - state = `{table: markdown[:4000], columns: [{name, type}]}`；`MAX_TABLE_CHARS = 4_000`。
   - 问题：`worth`、`label`、`c<index>`（结果量）、`metrics`、`k<first column>`（同单位列是否为
     同一量的不同条件，最多 2 组）、`n<column>`/`g<column>`、`shading`、`order`、`reference`，
     以及语义优先级 `rivals → move → polarity → focus → rowPolarity`；`MAX_QUESTIONS = 40`，
     `MAX_POLARITY_QUESTIONS = 8`，`MAX_CONDITION_GROUPS = 2`。
   - `worth` 为 noul 且 `< MIN_WORTH (0.3)` → 判为“不值得画”，返回 `plan:null, source:"judge"`。
   - `readTable` 把答案读成 `Reading`：label、可绘制 measures、results、lead、shared（同一轴能容纳
     的最大结果组）、`metricRows`、`conditions`；没有可绘制结果 → 无图。
4. **候选图**（`candidateCharts` 的 Python 移植）：
   - 以本地 guess 为第一个候选；按 `chart` 问题的 kind 顺序（bar…stat）用 `kindShapes` 生成每个
     kind 在该 reading 下的所有可画形状；`shapePlan` 应用 judge 的 label/结果列/转置/命名/极性
     /参考行/排序；`judgedMeaning` 补齐语义字段。
   - `comparesLike` 过滤“把不同量放在同一轴/面板”的图；`describeChart` 用 `chart` 问题的 per-kind
     模板 + plan 的列名/行名生成描述；同描述去重；最多 `MAX_CANDIDATES = 10`。
   - 只做描述所需的轻量 spec（kind、series、label、transpose、shading、reference、order 等），
     **不在 Python 重写 pi-tui 的渲染 `buildChart`**。
5. **第二轮选择**：`chart` 问题，criteria 为 `A…J` + `none`，每个候选配其描述；**on-device 只
   回答这一轮**（本地 reading + 本地候选）。`chosenChart` 的规则：
   - 候选 `A` 是本地结构建议；其他候选的胜出概率必须领先它 `PICK_MARGIN = 0.02`；
   - `none` 只在第一轮 `worth < SURE_WORTH = 0.6` 时成立；第一轮已确信有价值时 `none` 被忽略；
   - 选中的 plan 原样返回，**不询问 title**。
6. **标题在客户端**：客户端对返回的 plan 调 pi-tui `tableTakeaways`，取 `headline`（或最强
   striking fact）作为标题；host 不移植 `tableTakeaways`、不发 title 请求（决策 2）。**候选图
   不需要 takeaways**：标题只在最终选中的 plan 上计算，host 不持有 per-candidate takeaways。
7. **失败与无候选**：
   - judge 不可用/超时/答案结构异常/planner 内部异常 → `{"plan": null, "source": "local"}`；
   - 空表或没有可绘制结果（judge 都不调用）→ `local`；
   - 第一轮与候选构建都成功、但没有一张候选通过 `comparesLike`/去重 → `{"plan": null,
     "source": "judge"}`（等价 judge 的 `none`，不伪造成“本地回退”）。

**答案缺失/不自信不改变本地结构**：每个字段只在通过置信门时覆盖；否则保留 guess 与结构化事实。

### D4. 问题门控与置信阈值

**第一轮 reading**（只问“答案能改变候选图/读数”的问题；对齐 omp `readingQuestions`）：

| 问题 | 门控 |
|---|---|
| `worth` | 恒问：图画出来是否帮助读者；noul `< 0.30` 直接判无图 |
| `label` | 至少 2 个候选（前缀度量 + label/temporal 列） |
| `c<index>` | 每个可绘制度量列一次（noul：“这是要画的结果量吗”） |
| `metrics` | ≥3 行（每行是否是独立度量） |
| `k<first column>` | 同 dim/unit 的结果列成组，最多 2 组（同一量的不同条件） |
| `n<column>` / `g<column>` | 存在第二个 label 列时：命名列与分组列 |
| `shading` | 至少 4 个同单位列，或本地 guess 已是 heatmap；`composition.exact` 不问 |
| `order` | 可排序：有 label、非独占坐标系、非转置、无 baseline、≥4 行、首序列非单调 |
| `reference` | 仅 bar/multiples/heatmap、无 baseline、2–8 个不同标签值、值 ≤48 字符且非 `none` |
| `rivals` / `move` / `p<index>` / `focus` / `q<row>` | 语义优先级：`rivals → move → polarity → focus → rowPolarity`；≥3 行才问 rivals/focus，最多 8 个极性列、12 个焦点行 |

预算：`MAX_QUESTIONS = 40`，`MAX_POLARITY_QUESTIONS = 8`，`MAX_CONDITION_GROUPS = 2`，
`MAX_PANEL_ROWS = 12`，`MAX_REFERENCE_CHOICES = 8`，`MAX_REFERENCE_CHARS = 48`。

**第二轮 chart**（候选图选择）：

| 问题 | 规则 |
|---|---|
| `chart` | criteria = 每个候选的描述（`A…J`）+ `none`；最多 `MAX_CANDIDATES = 10`；描述里最多列 8 个行/系列名（`MAX_DESCRIBED`） |
| `title` | **host 不问**；客户端对最终 plan 用 pi-tui `tableTakeaways` 取 headline |

**答案 → 计划的置信门**（对齐 omp 常量）：

| 字段 | 门 |
|---|---|
| `worth` | `< 0.30` 无图；`< 0.60` 时允许 `chart=none` 胜出 |
| `shading` | 0.70 |
| `reference` | 0.80 |
| `order` | 0.60 |
| `polarity` / `move` / `rowPolarity` | 0.70 |
| `rivals` | 0.70 |
| `focus` | 0.75；行已被本地结构点名时 0.40 |
| `metrics` | `≥0.80` 视为逐行独立度量；`<0.50` 时转置 small multiples 不成立 |
| `conditions` | `≥0.70` 视为同一量的不同条件；`<0.30` 时多系列单轴图被排除 |
| `c*` / `name` / `group` / `label` | noul/choice ≥ 0.50 |
| 候选胜出 | 必须领先本地结构候选 `PICK_MARGIN = 0.02` |

`none` 不是无条件生效：第一轮已确信值得画（`worth ≥ 0.60`）时，`chart=none` 被忽略；`share`
超过 10 段时回退本地 guess 的 kind（`MAX_SHARE_SEGMENTS`）。

### D5. 客户端接线与四种模式

`omp-ui/src/modes/interactive-mode.ts`：

| `tui.autoGraph` | 行为 |
|---|---|
| `off` | `setTableCharts("off")`；表格保持原样；`tableChartsVisible === false` |
| `always` | `setTableCharts("always")`；pi-tui 只用本地 guess，不调 RPC |
| `smart` | `setTableCharts("smart", planner)`；pi-tui 对本地 `shouldJudge` 为真的表**首读**时调一次 `table_chart.plan`，`mode:"smart"`；judge `none` → 不画；**该表**失败 → 抛错让 pi-tui 保留本地 guess，下一张表仍继续请求 |
| `auto` | **启动/设置变更时调一次 `table_chart.status`**：`native=true` → 与 `smart` 相同（`mode:"auto"`）；`native=false` → `setTableCharts("always")`，本会话不再请求 judge |

- 默认值 `auto`（`omp-ui/src/modes/settings.ts:468`），与 omp 的 `docs/settings.md` 一致。
- **无 sticky 降级**：单表超时/无 judge/答案非法只影响该表（`source:"local"`），下一张表重新尝试；
  只有 `status.native=false` 才把 `auto` 整体切到 `always`。这与 omp 的逐表回退一致（决策 3）。
- **on-device judge**（决策 4）：`smart` 下若 `status.on_device=true`（链中存在 `local` 候选），
  host 跳过第一轮 reading，用本地 reading 生成候选，只把 `chart` 选择交给本地 judge；`auto` 仍
  要求 `native=true`，不会因为存在 on-device judge 就进入 smart。
- `tableChartsVisible` 由模式派生（`:654`），不再硬编码 `true`。
- 设置变更会重新应用模式并重新探测 `table_chart.status`（`:1324` 的 `any("tui.autoGraph")` 分支）；
  pi-tui 按表格源做 LRU 缓存（`CACHE_LIMIT = 256`），已经缓存过的表格沿用其已计算结果。
- 本地 guess 还要通过 pi-tui 的 `worthCharting` 才会画；judge 选出的 plan 直接画。**标题**：客户端
  对最终 plan 调 `tableTakeaways`，取 `headline`/最强 striking fact；host 不返回标题（决策 2）。
- 子代理 transcript、print、RPC、ACP 输出不画图；host 不为它们提供 planner。

### D6. 与 oh-my-pi 的对齐决策（逐项）

| 维度 | omp 目标 | 当前实现 | 对齐决策（阶段） |
|---|---|---|---|
| 判定轮次 | 两轮：reading → 候选图 → `chart` 选择 | 单轮折叠 | P1 按 D3 实现两轮 |
| 候选图 | `candidateCharts` 枚举 kind × 可比列，按描述去重、`comparesLike` 过滤，最多 10 张 | 不枚举候选 | P1 移植 `readTable`/`kindShapes`/`shapePlan`/`comparesLike`/`describeChart`/`chosenChart` |
| reading 问题 | `worth`、`metrics`、`conditions`、`k*` 等 | 仅 kind/transpose + 结构/语义问题 | P1 补 `worth`/`metrics`/`conditions`/`chart` 问题与预算常量 |
| on-device judge | 只答第二轮，基于本地 reading | 无 | P3 接 `LocalChatBackend`；`smart` 可用，`auto` 仍要求 native |
| `auto` 回退 | native 不存在 → `always`；单表失败回退本地，下一表继续 | 无 native → local；首次 local 后 sticky | P2 增加 `table_chart.status`；删除 sticky，逐表回退 |
| 标题 | 计算 takeaway 并由 judge 选标题 | host 在单轮里问 title | P2 改为客户端 `tableTakeaways`/headline；host 不问 title（决策 2） |
| 计算位置 | 候选图与 takeaways 都在 TS（planner + pi-tui） | 候选图 host、takeaways 可选透传 | 候选图 host（Python 移植），**takeaways/标题客户端** |
| 渲染 | pi-tui 全权 | 同左 | 保持 |
| 本地 guess | 参与候选与 `worthCharting` | 骨架/默认值 | P1 作为第一个候选并参与去重 |
| 表格上限 / 超时 | 4000 字符 / 15s | 同左 | 保持 |
| 触发面 / 默认 | 主会话 TUI / `auto` | 本地 viewer / `auto` | 保持 |

**唯一有意保留的差异**：标题不由 judge 选择，而是客户端对最终 plan 计算 `tableTakeaways` 后取
headline（决策 2）。其余行为按上表向 omp 收敛；未完成项以“当前实现”列为准，不声称已实现。

### D7. 隐私、计费与缓存

- **表格内容只在本次规划中发送**：两轮 state 都只含截断后的 markdown 与列名/类型；不包含会话
  历史、系统提示、工具结果、answer/plan。请求结束即丢弃。
- **不持久化**：`ChartPlan` 只返回给当前客户端；不写 session、不写 transcript、不进后续请求。
  标题由客户端对 plan 本地计算，不需要再把表格发给模型。
- **计费归属**：两轮 judge 调用都记入现有账本，purpose `auto-graph`；`auto` 无 native judge 时
  零调用。on-device judge 的本地 token 不产生出站费用，但仍记 usage。
- **缓存**：不触碰 prompt cache 前缀（不修改消息、不换工具面、不改系统提示）；图表是客户端渲染
  产物，对模型不可见。
- **失败不阻塞回答**：RPC 超时/异常返回 `local`；客户端 planner 抛错时 pi-tui 用本地 guess。

### D8. 失败矩阵

| 事件 | 行为 |
|---|---|
| `auto` + `table_chart.status.native=false` | 客户端直接 `always`，不发起 `table_chart.plan`；零 judge 调用 |
| `smart` 单表无 judge / 抛错 / 超时 | 该表 `{plan:null, source:"local"}`；客户端用本地 guess；**下一张表继续尝试** |
| 两轮共享 15s 预算 | 第一轮耗时从第二轮预算扣除；总超时 → `local` |
| 无 worth 判定（on-device 跳过第一轮） | 用本地 reading；仍可返回图，不做 `worth` 否决 |
| `worth < 0.30` | `{plan:null, source:"judge"}`；judge 判定不画 |
| `worth >= 0.60` 且 `chart=none` | 忽略 `none`，画胜出的候选 |
| 所有候选被 `comparesLike`/去重过滤 | `{plan:null, source:"judge"}` |
| 答案缺失/不自信/非法 choice | 保留本地结构/guess 的对应字段；不报错 |
| 空表 / 无可绘制结果 | 不调用 judge，`local` |
| markdown 超长 | 截断到 4000 字符后判定 |
| `share` 段数 >10 | 回退本地 guess 的 kind |
| on-device judge 不可用（本地 llama.cpp 未启动） | `smart` 落到链的下一候选；全不可用 → `local` |
| 请求缺字段/类型错误 | `local`；RPC 参数校验失败则 `4000` |
| planner 内部异常 | RPC `5100`；客户端按 `local` 处理 |
| 客户端无 gateway | 直接本地 guess，不发起 RPC |
| 客户端 takeaways/title 失败 | 仍画图，只是无标题 |
| `off` | 不画、不请求 |

### D9. 验收（测试锁定）

| 文件 | 覆盖 |
|---|---|
| `tests/agent/test_auto_graph.py` | state 截断与列类型；reading 问题（worth/metrics/conditions/label/c*/k*/n*/g*/shading/order/reference/语义优先级）；答案→Reading；候选图生成/去重/`comparesLike`；`chart` 选择与 `PICK_MARGIN`/`SURE_WORTH`；`worth<0.3` 无图；`none` 规则；guess 不被原地修改；judge 失败/坏 payload/空表回退 local；**不问 title**；15s 总预算与 purpose |
| `tests/agent/test_auto_graph_on_device.py`（新增） | `smart` + on-device：跳过第一轮、只发 `chart`；本地不可用落链；`auto` 不因 on-device 进入 smart |
| `tests/tui_gateway/test_table_chart_rpc.py` | `table_chart.status` 契约（`native`/`on_device`，无网络）；`plan` 参数（`profile` 剥离、`mode`）与 `local`/`5100`/`4000` |
| `omp-ui` 测试 | `auto` 无 native → `always`；单表失败只回退该表；`smart` 逐表重试；客户端 takeaways/title；设置变更重新探测 status |

运行入口是 `scripts/run_tests.sh`；测试不得写真实 profile。

### D10. 实施顺序

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| **P1** | 问题库补 `worth`/`metrics`/`conditions`/`chart`；两轮 planner 与候选图移植；常量对齐；预算 40/10；`chosenChart` 规则 | `test_auto_graph.py` 覆盖两轮/候选/选择；无 title 请求；15s 总预算 |
| **P2** | `table_chart.status`；客户端 `auto` 无 native → `always`；删除 sticky；逐表回退；客户端 `tableTakeaways`/标题 | RPC + omp-ui 测试；单表失败后下一表仍请求 |
| **P3** | on-device judge：`smart` 跳过 reading、只答 `chart`；本地不可用落链 | `test_auto_graph_on_device.py`；`auto` 仍 native-only |
| **P4** | 文档/设置说明（`auto`/`smart` 差异、on-device、无 sticky）；契约生成 | 文档不再描述单轮折叠；设置描述与实际一致 |

未完成阶段保持单轮实现；当前测试作为行为基线，P1 起按本 ADR 更新。

---

## 后果

**正面**

- 两轮判定让 kind 与列的组合约束参与选择，避免“图种对、列不对”的单轮折叠错误。
- 语义问题有校准概率（native/on-device judge），结构问题有确定性门控；不确定时保留本地结构。
- host 与客户端职责清楚：host 产 `ChartPlan`，客户端渲染并计算标题。
- 表格内容不落盘、不进 transcript；失败零阻塞；`auto` 无 native judge 时零 judge 调用。
- 参数校验、profile 作用域、能力探测与结构化错误都在契约层。

**负面 / 代价**

- 每张可画表最多 **两轮** judge 调用（共享 15s 预算）；大量表格的回答会产生额外延迟与费用。
- 候选图逻辑是 omp TS `planner.ts` 的 Python 移植：两套实现必须同步，kind/阈值/描述模板漂移会
  产生静默差异。
- 标题在客户端确定性地取 headline，不如 omp 的 judge 选择灵活；某些表可能没有标题。
- 逐表回退意味着持续故障时每张表都可能付一次超时预算（`auto` 只在“无 native”时整体退化为
  `always`）。
- 同一张表在客户端与 host 各有一份结构解析（客户端 `TableAnalysis`、host `_columns` 归一化）；
  两边字段语义必须同步。

**中性**

- 图表只在本地 viewer 显示；消息平台/print/RPC/ACP 不画。
- `table_chart.plan` / `table_chart.status` 不是模型工具；模型看不到也不能调用它们。
- on-device judge 只答第二轮，第一轮用本地 reading；`auto` 仍要求 native。

## 未采纳的替代方案

| 方案 | 否决理由 |
|---|---|
| 只在客户端用启发式（`always`） | 无法理解“越低越好”“base/var”“分位数阶梯”等语义，错误图表比不画更糟 |
| host 端渲染图表 | pi-tui 已有完整渲染；重复实现会产生两套语义，且 host 不该生成图像 |
| 把表格/答案写进 transcript 供后续复用 | 泄漏表格内容、破坏缓存前缀、让图表影响模型上下文 |
| 用普通聊天模型顶替 native judge | 无校准概率，无法设置信门；`auto` 不进入 smart |
| 保留 sticky 降级（一次失败整会话退回 always） | omp 是逐表回退；sticky 让一次瞬时故障长期降级体验 |
| 把 takeaways/title 留在 host | 需要把 pi-tui 的 `tableTakeaways` 移植到 Python；决策 2 明确放客户端 |
| 把整个 planner 移到客户端 | 问题库/渲染/排序在 host 与客户端之间会形成第二套 judge 编排；候选图仍在 host |
| 把 `table_chart.plan` 注册为模型工具 | 窄腰：模型不需要、也不该驱动客户端渲染；RPC 是本地 viewer 的接口 |
| 扩展 `ChartPlan` 为服务端 schema 并做迁移 | pi-tui 的 `ChartPlan` 是客户端契约；host 只透传 JSON，不新增版本 |

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 两轮 judge 延迟/费用 | 共享 15s 上限；问题按“能改变图表”门控；`auto` 无 native 零调用；客户端按表缓存 |
| 单表故障拖慢阅读 | 该表回退本地；只有 `status.native=false` 才整体 `always`；超时不重试同表 |
| 持续超时反复付费 | 逐表预算独立；同一表格缓存失败结果，不重复请求 |
| 表格内容泄漏给第三方 judge | 只发截断 markdown + 列名/类型；不含历史/系统提示；不落盘；用户可用 `off` 关闭 |
| Python 移植与 omp TS 漂移 | 移植清单/golden 描述 fixture；问题模板逐字对账；阈值常量集中一处 |
| 客户端与 host 的表格结构漂移 | 契约测试锁定 `profile` 剥离、`mode`、来源；host 归一化对缺字段宽容 |
| 错误图表误导 | 两轮判定 + `comparesLike` 过滤；不确定答案保留本地结构；极性/参考行/排序有独立门 |
| `share`/`heatmap` 等图种在小表上退化 | `MAX_SHARE_SEGMENTS`、`MIN_HEAT_COLUMNS`、`MAX_CANDIDATES` 上界；回退本地 guess |
| 设置默认 `auto` 让用户意外付费 | 无 native judge 不调用；只有可画表才请求；`off`/`always` 完全零 RPC |

## 开放问题

四项决策已关闭：

1. **两轮候选图**：采用（D3/D4）。
2. **takeaways 计算**：在客户端（D5/D6）。
3. **回退**：`auto` 无 native → `always`；单表失败逐表回退本地，无 sticky（D5）。
4. **on-device judge**：支持，仅答第二轮（D5）。

剩余是实现细节：

1. **移植范围**：哪些 chart kind 先落（建议先 bar/line/grouped/stacked/share/paired/change/
   scatter/heatmap/multiples），其余在 P1 后按 `chart` criteria 补齐；每个 kind 需要 golden 描述。
2. **on-device 模型能力**：本地 judge 的上下文/输出预算是否够放 10 个候选描述？若不够，
   `MAX_CANDIDATES` 对 on-device 降到 6，仍保持 `PICK_MARGIN`。
3. **标题策略**：客户端 headline 之外，是否需要在 P4 加一次可选的 title judge（会引入第三轮
   请求，默认关闭）？
4. **性能基线**：两轮在 10 列 × 20 行表上的 p95 目标（疑问项：15s 预算是否应拆成
   reading 8s + chart 7s，避免第一轮耗尽预算）？
