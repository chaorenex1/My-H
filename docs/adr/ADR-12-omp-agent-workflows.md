# ADR-12：omp 专属 agent 工作流的新后端（OMFG / ratchet / prewalk / auto-graph，选项 C）

**状态**：已决定（2026-11-04）；**选项 C：四项均建立新的 host 后端/网关能力**，未实现。
四个功能分别落在规则作者、eval 编排、会话模型交接、客户端图表四个层，因此按功能分期，不把
它们合成一个实现。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0、§2.3.2、§2.3.7、§4.7、§10；
`gateway/panel_inventory.py:158`（`omfg` 面板）；
ADR-4（eval 执行、D10 prelude 边界、D11.2 handle 接缝）；ADR-6（TTSR 通知、规则发现、
regex/AST 匹配）；ADR-11（typed judgment、TTSR `question` 判定、D12 的“不移植 `/omfg`”）；
`tools/rules_tool.py`、`agent/rule_utils.py`、`agent/ttsr_runtime.py`、`agent/judgment/`、
`hermes_cli/subcommands/rules.py`；`tui_gateway/` 的方法/事件契约与 authority；
`omp-ui/src/modes/settings.ts`、`omp-ui/src/modes/interactive-mode.ts`、
`omp-ui/src/session/settings.ts`、`omp-ui/node_modules/@oh-my-pi/pi-tui/src/chat/table-chart.ts`。

**范围**：只讨论 §10 的四个 omp 专属工作流。不设计 cleanse/autoresearch、annotation、
collab、in-process extensions 等其它 D 项；不改 ADR-6 的 TTSR 匹配/注入契约；不改 ADR-11 的
judgment 引擎与 `question` 阈值。**不移植 omp eval kernel 的 `ratchet(flow)` prelude**：
ratchet 由 host 编排器实现，见 D4。消息平台、Desktop、ACP、compute-host 的 worker 帧不设计。

**取证口径**：Hermes 侧引用本仓库当前工作树（2026-11-04）。omp 侧行为以本地 checkout
`C:\Users\zarag\projects\oh-my-pi`（HEAD `6a86df85`，`@oh-my-pi/pi-coding-agent` 18.14.0）为准；
下文 omp 路径均相对 `packages/coding-agent/` 或该 checkout 的 `docs/`。`omp-ui/node_modules`
只作为客户端已安装的 pi-tui 行为证据，不当作上游源码。

**取代关系**：

- ADR-11 D12 与「不采纳」清单中“`/omfg` 不移植”的**该项**由本 ADR D1–D3 取代；ADR-11 的判定
  引擎、`question` 规则实现、其它“不移植”项（auto-prewalk、stats、git staging 等）不变。
- ADR-11 对 prewalk 的“不移植”结论由本 ADR D5 取代（仅限“是否移植”，判定引擎仍归 ADR-11）。
- ADR-4 **D10** 的“不移植 omp prelude 词汇”**不被取代**：本 ADR 不实现 `ratchet()`、`agent()`、
  `wait()`、`workpool()` 等 prelude；ratchet 用 host 编排器调用现有工具与任务能力。
- §10 的 OMFG / ratchet / prewalk / auto-graph 行由本 ADR 收口为 **C / C / C / C**。

## 问题

§10 把 **OMFG / ratchet / prewalk / auto-graph** 并为一行「omp 专属 agent 工作流」。选项 C 意味着
不再用 skill、提示词或纯客户端接线做“近似实现”，而是分别补 host 能力。四项的 C 不是同一种后端：

| 功能 | 所在层 | C 要新增的能力 | 不做的替代（已否决） |
|---|---|---|---|
| OMFG | 规则作者工作流 | `omfg.*` RPC + host forge/validate/save + 项目/全局规则存储 | skill-only、profile-only |
| ratchet | eval 编排 | host 状态机 + 审批请求 + 任务/命令执行 + `$HERMES_HOME/ratchet/<cwd-key>` 状态 | skill + 外部脚本、无 gate |
| prewalk | 会话模型交接 | 会话 handoff 状态机 + todo/edit 边界钩子 + auto 分类器 | 提示词手动换模型 |
| auto-graph | 客户端渲染 + judge | `table_chart.plan` judge RPC + `smart` planner | 仅 `always` 本地猜测 |

C 的共同代价：新增 RPC/事件、host 状态、客户端恢复、测试面；必须逐项设验收，不能用一个
“大后端”开关掩盖部分实现。

## 1. OMFG：抱怨 → 一条 TTSR 规则

### 1.1 omp 的语义

`/omfg <complaint>` 生成一个**可执行的 TTSR 规则文件**：

- 生成器（`prompts/system/omfg-user.md`）要求优先 `condition`（JS 正则，流式命中即中断）或
  `astCondition`（ast-grep，对 edit/write 的源码快照匹配，工具副作用前拦截）；只有语义/语气/
  意图这类无法低误报模式化的行为才用 `question`（输出完成后由 judge 判 yes/no，事后警告，不中断）。
- 候选在保存前对**当前会话的已完成输出**做校验；`question` 候选还要 judge 在最近 8 个
  in-scope 输出上给出 yes（`modes/controllers/omfg-rule.ts`、`docs/ttsr-injection-lifecycle.md` §10）。
- 最多 3 次生成，失败可带反馈重试；保存位置为项目 `.omp/rules` 或全局
  `~/.omp/agent/rules`（`modes/controllers/omfg-controller.ts`）。
- 规则生效后由 TTSR 运行时执行：regex 命中 abort 并注入纠正；AST 在工具调用前检查；
  `question` 走判定后端，阈值 0.7，不中断（ADR-6 / ADR-11 P3）。

### 1.2 Hermes 已有基座

- 规则发现/解析/校验：`agent/rule_utils.py`（frontmatter 兼容 omp 的 `condition` /
  `astCondition` / `question` / `scope` / `globs`，`:12`；`check_rules` `:396`）。
- 规则读写：`tools/rules_tool.py` 的 `rules_list` / `rule_view` / `rule_manage`；
  `rule_manage` 只写 profile 的 `.agents/rules`（docstring `:1-3`，`managed_rule_root` `:303`），
  支持 `create|update|delete`、`condition`/`astCondition`/`question`/`scope`（schema `:45-88`）。
- CLI：`hermes rules list|show|check`（`hermes_cli/subcommands/rules.py:10`）。
- TTSR 运行时：`agent/ttsr_runtime.py` 的 `select_rules`（`:484`）、流式缓冲、工具参数 digest、
  `question` 判定（`TtsrCoordinator` `:833`；judge 接缝 `:1328`）。
- 判定后端：`agent/judgment/chain.py::resolve_judge`（`:513`）与 ADR-11 的 `noul`/0.7 语义。
- 会话消息读取：`SessionDB.get_messages` / `get_messages_as_conversation`
  （`hermes_state_messages.py:1365`、`:1512`）。

C 需要补的是：**生成/流式展示/校验/保存的 host 服务与 RPC**，以及项目级规则存储；不是规则匹配
本身。

## 2. ratchet：host 拥有的 eval 爬山

omp 的 `/ratchet`（`slash-commands/builtin-modes.ts`）先建/复用 eval，再逐轮修改并只在 held-out
测试集上保留胜者。真正的功能在 host：

- 状态与 gate 在 `ratchet/ratchet.ts`：审批内容哈希、冻结 split、完整性/泄漏检查、paired delta、
  `keep|revert|rerun`，全部由 host 决定，模型不能改 `_state.json`。
- 三轮人工审批 `inputs|grader|plan` 通过 host 对话框；每轮一个变体 `vN`，`train` 只给分析 agent，
  test trace 不落盘（`prompts/tools/ratchet.md`、`prompts/ratchet-kickoff.md`）。
- omp 用 eval kernel 的 `ratchet(flow)` prelude 与 `task` 子代理承载调用；`/ratchet` 在 eval/task
  工具未激活时拒绝。

Hermes 的现状：

- 有 `execute_code`/eval 执行（ADR-4），但 ADR-4 **D10** 明确不移植 omp prelude 词汇
  （`agent()`/`wait()`/`workpool()`/`judge`/`completion`）。
- ADR-4 **D11.2** 只登记了 eval 侧 handle 桥的设计，未实现（D11 第 2 条）。
- 没有 **ratchet 专用的三阶段 approve 协议**，也没有把 `ratchet()` 方法绑定到会话的 RPC；
  但网关已有 server → client request（`tui_gateway/contracts/server_requests.py` 的
  `approval`/`clarify`），D4 复用该通道，不新增通用审批总线。

选项 C 不推翻 ADR-4 D10：**不实现 prelude**，而是在 host 侧复刻同一套状态机，通过
`delegate_task`/现有任务能力跑分析器，通过 `execute_code`/terminal 跑 runner，通过新的审批请求
拿用户决定。这样模型仍然不能自批、不能改 gate。

## 3. prewalk：规划/实现两段式模型接力

omp 的 prewalk 是**会话级、一次性**的模型/thinking 交接（`docs/prewalk.md`）：

- 默认关闭；`prewalk.enabled` 或 `--prewalk` 武装，目标默认 `@smol`，来源默认 `@default`。
- `todo` 工具激活时，一次成功的 todo 调用（含只读 `view`）才打开交接闸门；没有 todo 则闸门一开始就开。
- 在包含第一个 `edit`/`write` 结果的 assistant turn 边界切换，持久化到 session；resume/revive 继续在
  目标模型上。
- `prewalk.auto` 用原生 judge 判断任务是否“贵推理集中在前期”，阈值 0.70；`primary`/`direct` 不交接。
  交接是会话状态，不是一条 prompt 指令。

Hermes 有 `model.roles.*`、`session.setModel`（`tui_gateway/methods_config_set.py` 的 `_set_model`）
和 todo 工具，但没有交接状态机、todo gate、首次 edit/write 边界钩子、auto 分类器与 per-task 恢复。
选项 C 需要把这四件事做成 session authority 的一部分，并恢复 `/prewalk` 客户端命令。

**缓存代价**：切换模型会改变 provider 的缓存前缀，prewalk 的收益以一次缓存重建为代价。这是该功能
的固有成本，不是实现缺陷；D5 要求默认关闭、只在明确武装/判定为 `prewalk` 时发生、不在 turn 中间切换。

## 4. auto-graph：表格到图表的客户端渲染 + judge

omp 的 `tui.autoGraph`（`docs/settings.md`）在 assistant 回答的数值表格下画图：

- `off`：不画；`always`：内置启发式对每个可读作数值的表格画；`smart`：judge 逐表决定图类型、
  列、行名/分组、刻度、参考行、极性、焦点行与标题，或决定不画；`auto`：judge 为原生
  TypeSafe jev 时等同 `smart`，否则 `always`。
- 实现：`packages/coding-agent/src/auto-graph/planner.ts`（judge 选图）+
  pi-tui `src/charts/chart-plan.ts`、`src/chat/table-chart.ts`（规划、缓存、渲染）。
- 无 judge/超时/异常时回退本地猜测，不阻塞回答（`planner.ts` 的 `PICK_TIMEOUT_MS`；pi-tui
  `lookupTableChart`）。

Hermes 客户端已注册 `tui.autoGraph`（`omp-ui/src/modes/settings.ts:468`，
`interactive-mode.ts:135`），但 `omp-ui/src` 从未调用 `setTableCharts`，pi-tui 默认
`chartMode = "off"`（`chat/table-chart.ts:73`）；`tableChartsVisible` 硬编码 `true`
（`interactive-mode.ts:643`）；网关没有 judge RPC。选项 C 补两件事：客户端接线 + host
`table_chart.plan` judge 方法。

## 选项

| 功能 | A | B | C | 本 ADR |
|---|---|---|---|---|
| OMFG | skill + 现有 rules/TTSR，无网关后端 | 不做 | `omfg.*` RPC + host forge/validate/save + 项目/全局存储 | **C** |
| ratchet | skill + 外部 eval 脚本 | 不做 | host 状态机 + 审批请求 + 任务/命令编排 | **C** |
| prewalk | skill/提示词手动换模型 | 不做 | 会话交接状态机 + auto 分类器 | **C** |
| auto-graph | 客户端 `always` 本地猜测 | 不做/移除设置 | `table_chart.plan` judge RPC + `smart` planner | **C** |

## 决策

### D1. OMFG：新增 `omfg.*` host 服务，恢复 `/omfg` 客户端面板

新增会话级 RPC（client → server + server → client 事件），绑定调用者 profile/session：

| 方向 | 名称 | 语义 |
|---|---|---|
| C→S | `omfg.forge` | 参数 `{session_id, complaint}`；启动一次生成，返回 `run_id` |
| S→C | `omfg.delta` | `{run_id, text}`：生成草稿的增量文本，驱动面板 streaming |
| S→C | `omfg.candidate` | `{run_id, rule, validation}`：解析后的候选 + 校验结果 |
| S→C | `omfg.error` | `{run_id, message, kind}`：生成/解析/校验失败 |
| C→S | `omfg.cancel` | `{session_id, run_id}`：取消生成 |
| C→S | `omfg.save` | `{session_id, run_id, level, confirm?}`：保存已校验候选 |
| S→C | `omfg.saved` | `{run_id, path, level}` |

约束：

- `omfg.forge` 只读会话历史做校验，**不向会话写入消息**；规则在保存后的下一次规则发现/会话启动
  才生效，不改变当前 turn 的 TTSR。
- 生成使用会话的 active model；没有可用模型时返回错误。usage 记入现有账本，purpose `omfg`。
- 生成提示词移植 `prompts/system/omfg-user.md`：优先 `astCondition`/`condition`，`question` 只在
  无语义模式时使用；输出严格 JSON；最多 3 次，失败带反馈重试。
- `omfg.save` 是显式用户动作；**profile 统一写 `$HERMES_HOME/rules`**，project 写
  `<session cwd>/.agents/rules`。project 保存必须 `confirm=true`，且只允许 cwd 内的相对路径；
  覆盖已有文件必须 `overwrite=true`。**任何更早扫描根中的同名 id 一律硬拒绝**（见 D3），不能用确认
  绕过遮蔽。保存前 `omfg.candidate` 带完整规则正文与目标路径。
- 客户端按 `omp-ui/src/VENDORED.md` 从 checkout 恢复 OMFG 的 controller/rule/panel 代码，并在
  `interactive-mode` 恢复 `/omfg` 入口；不改面板交互（§0.1）。

### D2. OMFG 校验契约

- `agent/omfg.py` 的 `validate_candidate(rule, outputs, *, path=None, judge_factory=None)` 复用
  `agent.ttsr_runtime` 的编译/digest 与 `agent.judgment.resolve_judge`，不重写匹配器。
- `condition` 对文本和工具参数 digest 匹配；`astCondition` 必须有可推断语言的 `path`，否则
  返回 `unsupported`，不猜测；`question` 对最近 8 个 in-scope 输出逐个判定，yes ≥ 0.7 记为命中。
- 无 judge 时返回 `judge_unavailable`；`omfg.save` 对未确认的 `question` 规则要求
  `confirm_unconfirmed=true`，否则拒绝。
- 校验结果只返回截断摘录（≤ 500 字符），不回显完整会话内容；不把规则正文写进 transcript。

### D3. OMFG 存储与安全

- **profile 规则统一写 `$HERMES_HOME/rules`**：新增 `omfg_rule_root()`（`get_hermes_home() / "rules"`）
  与 `write_profile_rule(rule_id, text)`（根固定为 `omfg_rule_root()`）；`$HERMES_HOME/rules` 已在
  `agent/rule_utils.py::rule_search_roots` 的 profile scopes 内，发现/校验不需要另加根。
- project 规则走新增 `write_project_rule`（同一 YAML/正文格式化，但路径限制在
  `cwd/.agents/rules`，写入前校验不含 `..`/绝对路径/符号链接逃逸）。
- 因为 profile 扫描顺序是 `.agents/rules` 先于 `rules`，且 project 根整体先于 profile，任何更早根中的
  同名 rule id 都会遮蔽 OMFG 的 `$HERMES_HOME/rules` 文件。`omfg.save` 必须先调
  `discover_rules(session cwd)`：若同名 id 已存在于 `$HERMES_HOME/rules` 之外（`.agents/rules`、
  project `.agents/rules` 等），**硬拒绝**并返回冲突路径；用户必须先删除/改名/移动冲突规则，
  不能靠“确认覆盖”绕过。仅当同名文件就在 `$HERMES_HOME/rules` 时，才允许 `overwrite=true`。
- 保存后返回路径；`hermes rules list|show|check` 必须能立即看到/校验。
- 不自动保存、不自动启用、不自动写项目；所有写入都来自 `omfg.save`。规则正文是用户可见的
  提示词内容，UI 必须在保存前展示。
- `rule_manage` 的工具语义不变（仍写 `.agents/rules`）；OMFG 的 profile/project 写入是独立的
  host 服务，不复用工具写路径，避免模型绕过 `omfg.save` 的确认。

### D4. ratchet：新增 host 编排器与 `ratchet.*` RPC（不实现 eval prelude）

- 新增 `agent/ratchet/`（状态机 + 统计 + gate）与 `tui_gateway/methods_ratchet.py`；状态根为
  `$HERMES_HOME/ratchet/<cwd-key>/<flow>/`（`get_hermes_home()`，不硬编码 `~/.hermes`），其中
  `cwd-key` 是会话 cwd 的稳定键（规范化 realpath 后哈希），cwd 同时写进 `_state.json`。不能只用
  `$HERMES_HOME/ratchet/<flow>`：omp 的 `RATCHET_ROOT` 是 `cwd/.omp/ratchet`，两个仓库的同名 flow
  必须隔离。布局镜像 omp：`_state.json`、`baseline/`、`vN/`、`results.jsonl`、`errors.jsonl`、
  `traces/`（仅 train）、`status.md`、`narrative.md`。
- 方法：`ratchet.start`（kickoff/入口）、`ratchet.init`、`ratchet.plan`、`ratchet.split`、
  `ratchet.check`、`ratchet.gate`、`ratchet.train`、`ratchet.status`；`ratchet.progress` 是进度
  事件。客户端 `/ratchet` 调用 `ratchet.start`（kickoff 由 host 渲染）。
- 审批：host 生成 `{stage, question, preview, content_hash}`，通过现有 server → client request
  （`approval`/`clarify`，见 `tui_gateway/contracts/server_requests.py`）交给客户端对话框；用户
  返回 `{approved, aborted?, feedback?}`。审批绑定内容哈希，文件/plan 变化即 stale。
  没有审批面（headless）时只允许继续审批已 current 的 flow，否则拒绝启动。
- 运行：runner 命令通过现有 terminal/bash 异步作业执行；host 收集 `results.jsonl`/`errors.jsonl`/
  `traces/` 并做完整性、重复行、split/泄漏检查。`gate` 的 `keep|revert|rerun` 由 host 计算，
  模型只能读取结果。
- 分析器：用 `delegate_task`/现有任务能力派一个只读分析子代理，输入仅 train 行与 trace；不引入
  eval kernel 的 `agent()`/`wait()` prelude，不推翻 ADR-4 D10。
- 规模：这是四项中最大的一项，依赖异步作业管理、子代理结果回传与审批请求面；未具备时按 P4
  延期，不得用“模型自评”占位。

### D5. prewalk：会话级 handoff 状态机与 `prewalk.*` RPC

- 会话状态（持久化到 session DB）：`armed`、`from`、`into`、`consumed`、`auto`、`auto_target`、
  `planning_model`/`planning_thinking`；`/new` 重新按配置武装，resume 继承已消费状态。
- 方法：`prewalk.arm`、`prewalk.restart`、`prewalk.off`、`prewalk.status`；事件
  `prewalk.armed`/`prewalk.handoff`。客户端恢复 `/prewalk` 与现有设置项。
- 钩子：成功的 `todo` 调用（含 `view`）打开 gate；在包含第一个 `edit`/`write` 结果的 assistant
  turn 结束后切换模型/thinking；一次性 disarm；切换只改会话 active model，不改 model-role 配置。
- auto：`resolve_judge` 读取移植的 prewalk 问题文件，按 `prewalk|primary|direct` 分类；
  `prewalk` 概率 ≥ 0.70 且目标可解析才武装；`suggest` 只报告；每个新任务先恢复该任务规划时的
  model/thinking，再判定，避免 `primary` 任务留在 handoff 目标上。
- 明确接受一次 provider 缓存重建；不在 turn 中间切换，不做隐式切换；默认 `prewalk.enabled=false`、
  `prewalk.auto=off`。子代理 prewalk 走 agent 配置/`task.prewalk`/per-agent 覆盖，与父会话隔离。

### D6. auto-graph：新增 `table_chart.plan` judge 方法并接线客户端

- 新增只读 RPC `table_chart.plan`：参数 `{session_id, markdown, table, guess}`；host 移植
  `auto-graph` 的 judge 问题文件与 `planner.ts` 的答案→`ChartPlan` 逻辑，调用
  `resolve_judge(purpose="auto-graph")`；返回 `{plan: ChartPlan|null, source: "judge"|"local"}`。
  15s 超时；无 judge/异常返回 `source:"local"` 与本地 `guess`，不阻塞。
- 客户端：启动/设置变更调用 `setTableCharts(mode, planner)`；`always` 不调用 planner；`smart`
  调用 `table_chart.plan`；`auto` 调用一次 `table_chart.plan`，若返回 `source:"local"` 则按
  `always`（本地猜测）继续；`off` 关闭。`tableChartsVisible` 由模式决定，不再硬编码 `true`。
- 表格内容只在该次 judge 请求中发送，不持久化、不写入 transcript；usage 记入账本，purpose
  `auto-graph`。按表格源缓存结果；同一表格不重复请求。
- 不新增图表渲染后端；图表仍由 pi-tui 在客户端绘制。

### D7. 能力声明、命令与文档

- `gateway/panel_inventory.py`：`omfg` 从 `UNSUPPORTED` 改为 `PENDING`（ADR-12 C；方法/事件落地后
  升 `READY`）；`docs/omp-ui-panels.json` 同步重新生成。
- ratchet/prewalk 不是面板：在 §10 与命令能力表中登记为 `pending`，实现后注册 `/ratchet`、
  `/prewalk`；headless 下没有审批/显式武装时不得启动。
- auto-graph 不是面板：保持客户端设置；`runtime.describe` 不新增图表能力字段。
- §10 行改为 **C / C / C / C**，并链接本 ADR；ADR-11 的 `/omfg` 条目加“由 ADR-12 取代”。

### D8. 边界与不变量

- 不实现 omp eval kernel 的 `ratchet()`/`agent()`/`wait()`/`workpool()`/`judge()`/`completion()`
  prelude（ADR-4 D10 不变）；ratchet 的编排在 host。
- 不新增 transcript 行；OMFG/auto-graph 的调用不进入会话消息；prewalk 只切换模型/thinking。
- 不改变 TTSR 匹配/注入/repeat/context mode；OMFG 保存的规则在下次发现时生效。
- 所有新 RPC 走现有 authority/contract/capability 生成流程；事件只发给发起会话的本地 viewer。
- 所有写入路径用 `get_hermes_home()`；不硬编码 `~/.hermes`。

## 协议与实现落点

### OMFG

| 文件 | 改动 |
|---|---|
| `agent/omfg.py` | 新增：prompt 渲染、流式生成、JSON 解析、3 次重试、`validate_candidate` |
| `agent/ttsr_validate.py` | 可选拆分：纯匹配/判定函数，供 OMFG 与未来工具复用 |
| `agent/rule_utils.py` | 新增 `omfg_rule_root()`（`get_hermes_home()/rules`）、`write_profile_rule`（根固定为 `omfg_rule_root()`，不写 `.agents/rules`）、`write_project_rule`（路径校验 + 格式化）；`rule_manage` 的 `.agents/rules` 路径不变 |
| `tui_gateway/methods_omfg.py` | 新增 `omfg.forge/cancel/save`；事件 `omfg.delta/candidate/saved/error` |
| `tui_gateway/contracts/` | 方法/事件 schema；能力协商 |
| `omp-ui/src/modes/controllers/omfg-*.ts`、`overlays/omfg-panel.ts` | 从 checkout 恢复并接 `modes/rpc/gateway-*`；`/omfg` 入口 |
| `tests/` | 生成/校验/保存/项目路径/无模型/无 judge/取消 |

### ratchet

| 文件 | 改动 |
|---|---|
| `agent/ratchet/` | 状态机、split、统计、gate、报告；`ratchet.ts` 的 Python 对应实现 |
| `tui_gateway/methods_ratchet.py` | `init/plan/split/check/gate/train/status/start`；`approve` 请求；`progress` 事件 |
| `tui_gateway/contracts/` | 方法/请求/事件 schema；审批请求的 client 响应 |
| `omp-ui/src/` | 恢复 `/ratchet` kickoff 与审批对话框接线（不改交互结构） |
| `tests/` | 审批哈希/stale、split 冻结、泄漏拒绝、重复行、keep/revert/rerun、headless 拒绝 |

### prewalk

| 文件 | 改动 |
|---|---|
| session state/DB | handoff 字段与持久化；`/new`/resume 语义 |
| `agent/prewalk.py` | 武装/恢复/触发判定；auto 分类器；问题文件 |
| turn/tool hooks | todo 成功 gate；首个 edit/write 的 turn 边界切换 |
| `tui_gateway/methods_prewalk.py` | `arm/restart/off/status`；事件 |
| `omp-ui/src/` | 恢复 `/prewalk` 命令；设置项接线 |
| `tests/` | todo gate、一次性、resume、auto 路由/阈值、per-task 恢复、无 judge |

### auto-graph

| 文件 | 改动 |
|---|---|
| `tui_gateway/methods_table_chart.py` | `table_chart.plan`；judge 调用与 `ChartPlan` 映射 |
| `agent/` 问题文件 | 移植 `auto-graph.json`；`resolve_judge(purpose="auto-graph")` |
| `omp-ui/src/modes/interactive-mode.ts` | `setTableCharts` 接线；`tableChartsVisible` 由模式决定；planner 调 RPC |
| `omp-ui/src/modes/settings.ts` | 保持四值；描述 `smart` 走 judge、`auto` 条件 |
| `tests/` | off/always/smart/auto、超时回退、缓存、无 judge、无表格内容落盘 |

## 分阶段

1. **P1 OMFG**：`omfg.*` 服务 + 客户端恢复 + 项目/全局保存 + 测试。先落 profile 保存，再落
   project 确认写；两步都属 P1，不拆成“先 skill 后后端”。
2. **P2 auto-graph**：`table_chart.plan` + 客户端接线；这是四项中最小、可独立验收的一项。
3. **P3 prewalk**：handoff 状态机 + auto 分类器 + 客户端命令；默认关闭。
4. **P4 ratchet**：host 编排器 + 审批 + 作业/子代理；依赖最多，最后落。未具备审批/子代理前
   不启动“半套 ratchet”。

## 验收

1. **OMFG 生成/流式**：`/omfg` 打开面板；`omfg.delta` 增量可见；候选解析成功；无模型/取消/
   非法 JSON 都落到 `omfg.error` 且不保存。
2. **OMFG 校验**：违规历史 `condition`/`astCondition` 命中；干净历史不命中；`question` 0.7、
   最近 8 输出、无 judge 返回 `judge_unavailable`；不伪造 yes。
3. **OMFG 保存**：profile 写 `$HERMES_HOME/rules`；project 写 `cwd/.agents/rules` 且必须
   `confirm=true`；同名 id 已存在于 `.agents/rules` 或 project 根时**硬拒绝**并返回冲突路径，
   不能靠确认覆盖；仅 `$HERMES_HOME/rules` 内同名文件可 `overwrite=true`；`..`/绝对路径/符号链接
   逃逸被拒绝；`hermes rules check` 通过；当前 turn 不受影响。
4. **ratchet**：`approval`/`clarify` 三类审批内容哈希生效，改动后 stale；split 冻结；test trace
   缺失被 gate 拒绝；重复 result 行/不完整 slot 被拒绝；`keep|revert|rerun` 由 host 产生；headless
   无审批时拒绝启动；两个仓库使用同名 flow 时状态互不串扰；不存在 eval prelude。
5. **prewalk**：`/prewalk` 武装；todo 成功才开 gate；首个 edit/write 的 turn 结束切换；一次性；
   resume 继承；`/new` 按设置重置；auto `prewalk` ≥0.70 才武装，`primary`/`direct` 不切换；
   每任务恢复；无 judge 不分类；模型切换只发生在边界。
6. **auto-graph**：`off` 无图；`always` 本地图；`smart` 调 `table_chart.plan`，超时回退本地；
   `auto` 在 `source:"local"` 时等同 `always`；设置变更实时生效；表格内容不落盘/不进 transcript。
7. **回归**：ADR-6 TTSR 规则发现/匹配/注入、ADR-11 judgment CLI 与 `question` 阈值不变；
   `scripts/check`、面板覆盖测试、契约生成检查通过。

## 风险

- **后端面积大**：四项同时引入 RPC、事件、状态机、客户端恢复与测试。缓解：P1–P4 独立验收，
  未完成的项保持 `pending`/unsupported，不伪造能力。
- **ratchet 的信任边界**：审批、split、gate 必须在 host；任何“模型自己确认/自己改状态”的实现
  都违背本 ADR。test trace 泄漏、重复行、不完整结果必须拒绝而不是警告后继续。
- **prewalk 的缓存与成本**：模型切换必然重建 provider 缓存；auto 可能频繁切换。缓解：默认关闭、
  阈值 0.70、per-task 恢复、显式 `/prewalk`、不在 turn 中切换。若实测成本不可接受，回到 D5 的
  重新评估，而不是静默降级。
- **OMFG 的规则注入面**：模型生成的是未来会注入的提示词。缓解：显式 `/omfg`、保存前展示、
  项目写入确认、`hermes rules check`、可删除、当前 turn 不生效。
- **auto-graph 的数据与延迟**：表格内容会发给 judge provider；每表一次调用可能慢。缓解：仅
  `smart`/`auto`、15s 超时、按表格源缓存、失败回退本地、不持久化。
- **契约漂移**：新方法/事件必须走 `scripts/gen_gateway_contracts.py` 与面板覆盖门；客户端
  `gateway-contract.generated.ts` 重新生成，不手改生成文件。

## 不采用的实现方式

- **OMFG skill-only / profile-only**：不能覆盖项目规则，也没有 omp 的流式面板与保存确认。
- **ratchet skill + 外部脚本**：没有 host 审批/gate/split，等于模型自评。
- **prewalk 提示词手动换模型**：不能在 todo gate 与首个 edit/write 边界自动切换。
- **auto-graph 仅 `always`**：`smart`/`auto` 的 judge 选择缺失，不满足选项 C。
- **移植 eval kernel prelude（`ratchet()`/`agent()`/`wait()`/…）**：ADR-4 D10 仍然有效；本 ADR 用
  host 编排替代，不引入第二套 kernel 词汇。
- **为 OMFG 复用 `rule_manage` 写项目规则**：会绕过保存确认；项目写入走 `omfg.save`。
- **为 prewalk 改写 model-role 配置**：omp 只改会话 active model；本 ADR 保持会话级。

## 遗留

1. ratchet 的审批请求复用现有 `approval`/`clarify` server → client request；若现有 schema 不足以携带
   `stage`/`content_hash`，只扩展该 request 的字段，不新增通用审批通道，也不允许模型自答。
2. ratchet 的分析器用 `delegate_task` 还是未来的 handle 桥；若 ADR-4 D11.2 先落地，可替换实现，
   但 host gate 不变。
3. OMFG 项目规则的归属/覆盖策略（是否允许覆盖非 agent 创建的规则）在 P1 实现时以内容哈希与
   `overwrite` 确认收口；默认拒绝覆盖。
4. prewalk 的 auto 默认值是否保持 `off`；当前决定是保持，避免默认成本。
5. `table_chart.plan` 是否未来泛化为通用 `judgment.*` RPC；当前保持领域方法，避免把图表 schema
   塞进通用判定协议。

## 对 §10 的修正

`docs/omp-ui-panels-gateway-plan.md` §10 的 **OMFG / ratchet / prewalk / auto-graph** 行由本 ADR
收口为 **C / C / C / C**：

| 项 | 决定 | 退出条件 |
|---|---|---|
| OMFG | **C（`omfg.*` host 服务 + 面板）** | D1–D3、验收 1–3；面板 `pending` → `ready` |
| ratchet | **C（host 编排器 + 审批 + `ratchet.*`）** | D4、验收 4；未具备审批/子代理前保持 pending |
| prewalk | **C（会话 handoff + auto 分类器 + `prewalk.*`）** | D5、验收 5；默认关闭 |
| auto-graph | **C（`table_chart.plan` + 客户端 planner）** | D6、验收 6；无网关图表渲染 |
