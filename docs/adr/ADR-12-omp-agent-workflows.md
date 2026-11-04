# ADR-12：omp 专属 agent 工作流的取舍（OMFG / ratchet / prewalk / auto-graph）

**状态**：已决定（2026-11-04）；**未实现**。四个功能不共享同一实现层，因此本 ADR 分开决策：
**OMFG 采纳为 skill + 现有 rules/TTSR 基座**；**auto-graph 采纳为纯客户端接线**；
**ratchet、prewalk 不采纳**，在 Hermes 中保持 `unsupported`，并登记重新评估的条件。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §10（本 ADR 收口该行）、§0、§2.3.7、§4.7；
`docs/omp-ui-gap-plan.md` 的 D 类清单；`gateway/panel_inventory.py:158`（`omfg` 面板）；
ADR-4（eval 执行、D10 prelude 边界、D11.2 handle 接缝）；ADR-6（TTSR 通知、规则发现、
regex/AST 匹配）；ADR-11（typed judgment、TTSR `question` 判定、D12 的“不移植 `/omfg`”）；
`tools/rules_tool.py`、`agent/rule_utils.py`、`agent/ttsr_runtime.py`、`agent/judgment/`、
`hermes_cli/subcommands/rules.py`；`omp-ui/src/modes/settings.ts`、
`omp-ui/src/modes/interactive-mode.ts`、`omp-ui/src/session/settings.ts`、
`omp-ui/node_modules/@oh-my-pi/pi-tui/src/chat/table-chart.ts`。

**范围**：只讨论 §10 的四个 omp 专属工作流。不设计 cleanse/autoresearch、annotation、
collab、in-process extensions 等其它 D 项；不改 ADR-6 的 TTSR 匹配/注入契约，不改 ADR-11 的
judgment 引擎与 `question` 阈值；不新增网关实时事件、transcript 行或模型工具（`rule_manage`
的只读 `validate` 动作除外）。消息平台、Desktop、ACP、compute-host 的 worker 帧不设计。

**取证口径**：Hermes 侧引用本仓库当前工作树（2026-11-04）。omp 侧行为以本地 checkout
`C:\Users\zarag\projects\oh-my-pi`（HEAD `6a86df85`，`@oh-my-pi/pi-coding-agent` 18.14.0）为准；
下文 omp 路径均相对 `packages/coding-agent/` 或该 checkout 的 `docs/`。`omp-ui/node_modules`
只作为客户端已安装的 pi-tui 行为证据，不当作上游源码。

**取代关系**：ADR-11 D12 与「不采纳」清单中“`/omfg` 不移植”的**该项**由本 ADR D1–D3 取代；
ADR-11 的判定引擎、`question` 规则实现、其它“不移植”项（auto-prewalk、stats、git staging 等）
不变。ADR-11 对 prewalk 的“不移植”结论由本 ADR D5 重申，而不是取代。ADR-4 D10 对 eval prelude
的边界不变；本 ADR 不实现 prelude。

## 问题

§10 把 **OMFG / ratchet / prewalk / auto-graph** 并为一行「omp 专属 agent 工作流」，要求先出决策记录。
但这四项处在完全不同的层：

| 功能 | 所在层 | 依赖 | 在 Hermes 的缺口 |
|---|---|---|---|
| OMFG | 规则作者工作流（模型 + TTSR） | 规则发现/存储、TTSR 匹配、judge | 没有“从抱怨生成并验证规则”的 skill/工具动作 |
| ratchet | host 侧 eval 状态机 + eval kernel prelude | eval kernel、`task` 子代理、审批对话框、held-out split | 没有 prelude/handle，也没有 host 审批面 |
| prewalk | 会话级模型/thinking 交接 | todo gate、首次 edit/write 边界、模型角色、auto 分类器 | 没有交接状态机；ADR-11 已排除 auto-prewalk |
| auto-graph | 纯客户端渲染 | pi-tui 表格分析/图表规划 | 设置已注册但未接线；无 judge RPC |

把它们合成一个“做/不做”会得到错误结论：OMFG 的基座已经存在，缺的只是薄薄一层作者流程；
ratchet 的核心恰恰是 host 侧不可争辩的 gate，去掉它就不是 ratchet；prewalk 的价值在“正确的
turn 边界切换模型”，prompt 指令做不到；auto-graph 根本不需要后端。

## 1. OMFG：抱怨 → 一条 TTSR 规则

### 1.1 omp 的语义

`/omfg <complaint>` 不是让模型“记住别这样”，而是生成一个**可执行的 TTSR 规则文件**：

- 生成器（`prompts/system/omfg-user.md`）要求优先 `condition`（JS 正则，流式命中即中断）或
  `astCondition`（ast-grep，对 edit/write 的源码快照匹配，工具副作用前拦截）；只有语义/语气/
  意图这类无法低误报模式化的行为才用 `question`（输出完成后由 judge 判 yes/no，事后警告，不中断）。
- 候选在保存前要对**当前会话的已完成输出**做校验；`question` 候选还要 judge 在最近 8 个
  in-scope 输出上给出 yes（`modes/controllers/omfg-rule.ts`、`docs/ttsr-injection-lifecycle.md` §10）。
- 最多 3 次生成，失败可带反馈重试；保存位置为项目 `.omp/rules` 或全局
  `~/.omp/agent/rules`（`modes/controllers/omfg-controller.ts`）。
- 规则生效后由 TTSR 运行时执行：regex 命中 abort 并注入纠正；AST 在工具调用前检查；
  `question` 走判定后端，阈值 0.7，不中断（ADR-6 / ADR-11 P3）。

### 1.2 Hermes 已有基座

Hermes 不是从零开始：

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

缺口只有三处：没有“从抱怨生成规则”的**作者流程**；`rule_manage` 没有**按会话历史验证**的动作；
`rule_manage` 不能写项目规则（这是安全设计，不是遗漏）。

## 2. ratchet：host 拥有的 eval 爬山

omp 的 `/ratchet`（`slash-commands/builtin-modes.ts`）先建/复用 eval，再逐轮修改并只在 held-out
测试集上保留胜者。真正的功能在 host：

- 状态与 gate 在 `ratchet/ratchet.ts`：审批内容哈希、冻结 split、完整性/泄漏检查、paired delta、
  `keep|revert|rerun`，全部由 host 决定，模型不能改 `_state.json`。
- 三轮人工审批 `inputs|grader|plan` 通过 host 对话框；每轮一个变体 `vN`，`train` 只给分析 agent，
  test trace 不落盘（`prompts/tools/ratchet.md`、`prompts/ratchet-kickoff.md`）。
- 依赖 eval kernel 的 `ratchet(flow)` prelude（Python/JS 同构）与 `task` 子代理；`/ratchet` 在
  eval/task 工具未激活时直接拒绝。

Hermes 的现状：

- 有 `execute_code`/eval 执行（ADR-4），但 ADR-4 **D10** 明确不移植 omp prelude 词汇
  （`agent()`/`wait()`/`workpool()`/`judge`/`completion`，`:452`）。
- ADR-4 **D11.2** 只登记了 eval 侧 handle 桥的设计，未实现（`:471`）。
- 没有 host 侧 `approve()` 对话框协议，也没有 `task` 子代理与 `ratchet()` 的绑定。

没有这些，把 ratchet 写成 skill 只会得到“模型自己评分、自己决定保留”的循环——恰好是 ratchet
要消除的东西。新增整套 host 状态机 + 审批 RPC + eval 桥，成本远大于该功能，且与 ADR-4 的边界冲突。

## 3. prewalk：规划/实现两段式模型接力

omp 的 prewalk 是**会话级、一次性**的模型/thinking 交接（`docs/prewalk.md`）：

- 默认关闭；`prewalk.enabled` 或 `--prewalk` 武装，目标默认 `@smol`，来源默认 `@default`。
- `todo` 工具激活时，一次成功的 todo 调用（含只读 `view`）才打开交接闸门；没有 todo 则闸门一开始就开。
- 在包含第一个 `edit`/`write` 结果的 assistant turn 边界切换，持久化到 session；resume/revive 继续在
  目标模型上。
- `prewalk.auto` 用原生 judge 判断任务是否“贵推理集中在前期”，阈值 0.70；`primary`/`direct` 不交接。
  交接是会话状态，不是一条 prompt 指令。

Hermes 有 `model.roles.*`、`session.setModel`（`tui_gateway/methods_config_set.py` 的 `_set_model`）
和 todo 工具，但没有：

- 在“首个 edit/write 完成”这个精确边界切换的会话状态；
- todo gate 与一次性 disarm；
- auto 分类器及其 per-task 恢复语义。

ADR-11 已明确不移植 auto-prewalk（`:32`、`:151`、`:763`），§0 也把 prewalk 列为非目标。用一条
skill 指令让模型“先规划再提醒用户换模型”不是 prewalk：它既不能自动切换，也不能保证边界。

## 4. auto-graph：表格到图表的客户端渲染

omp 的 `tui.autoGraph`（`docs/settings.md`）在 assistant 回答的数值表格下画图：

- `off`：不画；`always`：内置启发式对每个可读作数值的表格画；`smart`：judge 逐表决定图类型、
  列、行名/分组、刻度、参考行、极性、焦点行与标题，或决定不画；`auto`：judge 为原生
  TypeSafe jev 时等同 `smart`，否则 `always`。
- 实现：`packages/coding-agent/src/auto-graph/planner.ts`（judge 选图）+
  pi-tui `src/charts/chart-plan.ts`、`src/chat/table-chart.ts`（规划、缓存、渲染）。
- 无 judge/超时/异常时回退本地猜测，不阻塞回答（`planner.ts` 的 `PICK_TIMEOUT_MS`；pi-tui
  `lookupTableChart`）。

Hermes 客户端使用同一套 pi-tui，且 `omp-ui/src/modes/settings.ts:468` 已注册
`tui.autoGraph`，`interactive-mode.ts:135` 把它并入 live settings。但：

- `omp-ui/src` 从未调用 `setTableCharts`（全仓 grep 只有 pi-tui 定义与 `tableChartsVisible`），
  而 pi-tui 的默认 `chartMode = "off"`（`chat/table-chart.ts:73`）。
- `interactive-mode.ts:643` 的 `tableChartsVisible` 硬编码 `true`，只控制可见性，不设置模式。
- 网关侧没有 judge RPC，`gateway-session` 无法提供 `smart` 所需的 `TableChartPlanner`。

因此当前设置是“存在但无效”，不是“后端缺失”。

## 选项

| 功能 | A | B | C | 本 ADR |
|---|---|---|---|---|
| OMFG | 映射到现有 rules/TTSR + 一个 skill（无网关后端） | 不做 | 新网关 `omfg.*` RPC + 规则存储 | **A** |
| ratchet | 映射到 skill + 外部 eval 脚本 | 不做，保持 unsupported | 新 host 状态机 + 审批 RPC + eval 桥 | **B** |
| prewalk | 映射到 skill/提示词的手动换模型 | 不做，保持 unsupported | 新会话交接状态机 + auto 分类器 | **B** |
| auto-graph | 客户端接线到现有 pi-tui（无后端） | 不做，移除/隐藏设置 | 新 judge RPC 供 `smart` | **A**（仅客户端） |

## 决策

### D1. OMFG 采纳为 skill，不新增网关后端

新增内置 skill `skills/rule-forging/SKILL.md`（名称沿用“规则锻造”，不把 `/omfg` 作为命令名）。
它由用户在会话内显式调用（`/skill:` 或自然语言），流程固定：

1. 用一句话复述反复出现的行为，并引用当前会话中的一个具体输出作为反例；
2. 触发方式优先级：代码结构 → `astCondition`；字面 token/短语 → `condition`；只有语义/意图/
   语气且无法低误报模式化时才 `question`，并尽量加 `condition` 预过滤；
3. `scope` 尽可能窄（如 `tool:edit(*.ts)`），禁止 `tool, text` 宽范围；
4. 调 `rule_manage` 写入（profile scope），随后调 `rule_manage(action="validate", ...)`；
5. 验证失败则修正并重试，最多 3 次；仍失败就报告，不保存；
6. 报告规则名、路径、触发方式与验证结果。

不新增 `/omfg` 网关方法；`gateway/panel_inventory.py` 的 `omfg` 面板继续 `unsupported`，note 改为
“由 rule-forging skill 提供，非网关后端”。这样 UI 不会出现假入口。

### D2. `rule_manage` 增加只读 `validate` 动作

在 `tools/rules_tool.py` 的 `RULE_MANAGE_SCHEMA` 中把 `action` 扩为
`create|update|delete|validate`；`validate` 不写文件：

```json
{"action": "validate", "rule_id": "ts-no-any",
 "texts": ["optional explicit outputs"], "path": "src/x.ts", "max_outputs": 8}
```

实现契约：

- 只读；`texts` 省略时用 dispatcher 传入的 `session_id` 读取最近 `max_outputs`（默认 8）条
  assistant 输出；`SessionDB.get_messages` / `get_messages_as_conversation` 是唯一来源。
- `condition`：用 `agent.ttsr_runtime` 的编译/匹配函数与 `tool_digests`，对文本与工具参数摘要匹配；
  不在 `rules_tool` 里重写正则或 scope 语义。
- `astCondition`：用 `agent/ttsr_ast.py` 的进程桥；必须有可推断语言的 `path`，否则返回
  `unsupported`，不猜测。
- `question`：`resolve_judge(purpose="ttsr")` + `NoulQuestion`，对每个 in-scope 输出问一次；
  yes 概率 ≥ 0.7 记为命中；无 judge 返回 `judge_unavailable`，由 skill 询问用户是否保存未确认规则。
- 返回 `{success, rule_id, matched, matched_outputs, trigger, judge?, reason?}`；不返回原文全文，
  只返回截断摘录，避免把敏感内容回显进上下文。
- `create` 不强制先 validate（保持现有契约），但 skill 的验收要求先 validate；`validate` 失败时
  skill 不得直接 create。

新增 `agent/ttsr_validate.py` 承载上述纯逻辑，`rules_tool` 只做参数/JSON/错误包装。这样
`agent/ttsr_runtime.py` 的流式状态机不被工具调用路径污染。

### D3. OMFG 只写 profile 规则；项目规则不自动写

`rule_manage` 继续只写 `$HERMES_HOME/.agents/rules`（`managed_rule_root`），不修改项目、
installation 或导入 corpus。理由：

- 项目规则会进入仓库、影响所有协作者与后续会话；agent 自动写入等于让一次对话改变仓库行为，
  且难以审计。
- 现有 docstring 已把“只写 profile”定义为安全边界；OMFG 不应为了 parity 打破它。

skill 在用户要求项目级规则时，只输出建议的 Markdown 与目标路径，由用户显式落盘/提交。若要支持
`scope=project` 的自动写，另立 ADR，至少需要：目标路径限制在 cwd 的 `.agents/rules`、显式
`confirm`、内容 diff 展示、以及不覆盖非 agent-owned 文件的哈希/来源标记。

这是与 omp 的**已知差异**：omp 可在项目/全局之间选择，Hermes v1 只有 profile。

### D4. ratchet 不采纳，保持 unsupported

不在 Hermes 实现 `ratchet(flow)`、`.omp/ratchet` 状态机或 `/ratchet` 命令。理由见 §2：
去掉 host 审批/gate/split 的 ratchet 只剩“模型自评”，没有产品意义；补齐它需要 eval prelude
（ADR-4 D10 明确拒绝）、eval handle（D11.2 未实现）与审批 RPC，规模超过本功能。

`runtime.describe` / panel inventory 对 ratchet 保持 `unsupported`；slash 命令不注册。用户需要
同类方法论时，用外部 eval harness + `execute_code`/terminal，自行保存结果；Hermes 不声称提供
held-out gate。

重新评估条件（全部满足才另立 ADR）：ADR-4 D11.5 判定桥与 D11.2 handle 桥落地；存在可复用的
host 审批面（不能是模型自答）；有一个真实 flow 与明确的预算/安全需求。

### D5. prewalk 不采纳，保持 unsupported

不在 Hermes 实现会话级模型交接、todo gate、auto-prewalk 分类器或 `/prewalk` 命令。理由见 §3：
它是会话状态机，不是提示词；ADR-11 已排除 auto-prewalk，且 §0 将其列为非目标。

手动替代不需要新代码：用户在规划完成后用现有 `/model` 或 model roles 切到便宜模型。文档要明确
这不是 prewalk：没有自动边界、没有一次性 disarm、没有 per-task 恢复。`omp-ui/src/session/settings.ts`
里的 prewalk 设置继续不接线；如果客户端显示它，必须标 `unsupported`，不能静默保存后无效。

重新评估条件：会话级模型/thinking 切换已有可持久化的 handoff 状态、todo 完成事件可被 host 订阅、
且能在 assistant turn 边界做一次性切换；届时另立 ADR，不与本 ADR 的 OMFG/auto-graph 混在一起。

### D6. auto-graph 采纳为纯客户端接线，不新增后端

在 `omp-ui/src/modes/interactive-mode.ts` 中：

- 启动与 `tui.autoGraph` 变更时调用 pi-tui 的 `setTableCharts(mode, planner?)`；
- `off` → `"off"`；`always` → `"always"`；`smart` / `auto` → `"always"`（本地猜测），因为
  Hermes 网关没有 judge RPC；等未来有 judge 能力再传 planner，不改协议；
- `tableChartsVisible` 改为 `cfgTuiAutoGraph.get(settings) !== "off"`，而不是硬编码 `true`；
- 设置项描述在网关会话中注明 `smart` 当前等同 `always`（本地图表），避免虚假承诺。

不新增 `tui.autoGraph` 的网关字段/事件；不向服务端发送表格内容；图表完全由客户端从已渲染的
Markdown 生成。这与 §0「终端渲染能力是客户端 pi-tui 的事」一致。

### D7. 能力声明与文档

- `gateway/panel_inventory.py`：`omfg` 继续 `UNSUPPORTED`，note 指向 rule-forging skill；
  ratchet/prewalk 不作为面板新增（它们是命令级能力），在 §10 与 `runtime.describe` 的能力说明中
  保持 unsupported。
- auto-graph 不是面板，不在 `runtime.describe.panels` 中广告；它是客户端设置。
- `docs/omp-ui-panels-gateway-plan.md` §10 行改为“已决定（ADR-12）”，并链接本文件；
  `docs/omp-ui-gap-plan.md` 的 D 类描述不再把四项混为同一后端。
- 本 ADR 落地时，ADR-11 的“不移植 `/omfg`”条目加注“已由 ADR-12 D1–D3 取代（仅 `/omfg` 项）”。

### D8. 不新增的东西

- 不新增网关 RPC/事件/transcript 行；
- 不新增模型工具（`rule_manage` 的 `validate` 动作是现有工具的扩展）；
- 不改变 TTSR 匹配、注入、repeat、context mode 的现有语义；
- 不改变 system prompt 前缀缓存：规则仍在会话启动/规则重载时发现，OMFG 保存的规则**不在当前
  turn 自动生效**（与现有规则发现边界一致）；
- 不自动运行 OMFG：只有用户显式调用 skill 才生成规则。

## 协议与实现落点

### OMFG

| 文件 | 改动 |
|---|---|
| `skills/rule-forging/SKILL.md` | 新增；触发条件、首选 `astCondition`/`condition`、scope 收窄、3 次重试、validate 后 create、只写 profile |
| `tools/rules_tool.py` | `RULE_MANAGE_SCHEMA.action` 加 `validate`；新增 `texts`/`path`/`max_outputs`；handler 接受 `session_id` |
| `agent/ttsr_validate.py` | 新增纯函数：`validate_rule_against_outputs(rule, outputs, *, path=None, judge_factory=None)`；复用 `ttsr_runtime` 的编译/digest 与 `judgment.resolve_judge` |
| `agent/ttsr_runtime.py` | 只读导出/复用现有匹配函数；不改流式协调器行为 |
| `tests/tools/test_rules_tool.py`（或现有规则测试文件） | validate 的 regex/AST/question/无 judge/无 session 分支 |
| `tests/agent/test_ttsr_validate.py` | 纯函数边界：空输出、scope 过滤、截断、judge 阈值 |

`validate` 的返回示例：

```json
{"success": true, "rule_id": "ts-no-any", "trigger": "condition",
 "matched": true, "matched_outputs": 1,
 "excerpt": ": any", "judge": null}
```

无 judge 的 `question`：

```json
{"success": false, "error": "judge_unavailable",
 "rule_id": "no-unverified-test-claims", "matched": false}
```

### auto-graph

| 文件 | 改动 |
|---|---|
| `omp-ui/src/modes/interactive-mode.ts` | 启动/设置变更调用 `setTableCharts`；`tableChartsVisible` 由模式决定；`smart`/`auto` 传 `"always"` |
| `omp-ui/src/modes/interactive-mode.test.ts`（或现有设置测试） | `off` 不画、`always` 画、`smart` 不调用 planner、切换即时生效 |
| `omp-ui/src/modes/settings.ts` | 网关会话的描述注明 `smart` 当前等同 `always` |

不修改 `omp-ui/node_modules`；pi-tui 的 `setTableCharts`/`lookupTableChart` 已存在。

## 分阶段

- **P1（OMFG）**：`agent/ttsr_validate.py` + `rule_manage validate` + skill + 测试 + `hermes rules check`
  互操作。完成前 `omfg` 面板保持 unsupported。
- **P2（auto-graph）**：客户端接线 + 设置描述 + 客户端测试。不涉及网关。
- **P3（文档收口）**：§10、gap plan、panel inventory note、ADR-11 取代注记。
- **不做**：ratchet、prewalk 的任何代码；不新增 RPC/事件。

## 验收

1. **OMFG 生成**：对一个具体抱怨，skill 产生一条合法规则（kebab-case name、description、body、
   至少一个 trigger、窄 scope）；`hermes rules list` 能看到，`hermes rules check` 无错误。
2. **OMFG 验证**：对包含违规片段的会话历史，`condition`/`astCondition` 返回 `matched=true`；
   对干净历史返回 `false`；`question` 在 judge 可用时按 0.7 判定，judge 不可用时返回
   `judge_unavailable` 且不伪造 yes。
3. **OMFG 安全**：`rule_manage` 仍只写 profile；任何项目/installation 路径的写入被拒绝；
   skill 不自动执行，不把规则正文回显为未截断的会话内容。
4. **OMFG 生效边界**：保存后当前 turn 不改变 TTSR；下一次规则发现/会话启动才加载。
5. **auto-graph**：`off` 时数值表格下无图；`always` 时有图；`smart`/`auto` 在无 judge 时仍出本地图，
   且不发起网络/网关调用；设置切换无需重启会话。
6. **ratchet/prewalk**：`runtime.describe`/panel inventory 仍为 unsupported；没有 `/ratchet`、
   `/prewalk` 命令，没有 `.omp/ratchet` 状态写入；客户端切换 prewalk 设置不会静默改变模型。
7. **回归**：ADR-6 的 TTSR 规则发现/匹配/注入、ADR-11 的 judgment CLI 与 `question` 阈值不变；
   `scripts/check` 通过。

## 风险

- **规则是提示词注入面**：用户抱怨可能诱导生成压制重要行为的规则。缓解：显式调用、profile-only、
  保存前展示规则正文/路径、`hermes rules check`、可随时删除、不自动写项目。
- **验证不是证明**：`validate` 只证明规则能命中给出的/最近的输出，不证明未来无误报。skill 要
  提示用户先用 `condition`/`astCondition` 的窄 scope；`question` 有 judge 成本与误判。
- **judge 成本**：`question` 每个 in-scope 输出一次；`max_outputs` 默认 8，且只在用户显式
  validate 时发生；无 judge 不重试。
- **auto-graph 误导**：本地猜测可能选错图类型；默认不改变数据，只影响展示；`off` 可完全关闭；
  `smart` 在 Hermes 不做模型判断，不声称等价。
- **预期落差**：用户可能期待 ratchet/prewalk 的自动能力。文档与 UI 必须明确 unsupported 与手动替代，
  不提供假入口或静默 stub。

## 不采纳

- **新增 `omfg.*` 网关 RPC / 新面板后端**：现有 skill + rules 工具已覆盖；网关只会重复规则存储。
- **允许 OMFG 自动写项目规则**：需要独立的审批/归属/覆盖设计，见 D3。
- **把 ratchet 写成 skill**：没有 host gate 的“爬山”是自评循环，违背该功能的目的。
- **把 prewalk 写成提示词 skill**：不能切换模型/thinking，也不能在正确 turn 边界生效。
- **为 auto-graph 新增 judge RPC**：当前没有消费者需求；先接线本地 `always`，未来有 judge 再接 planner。
- **在本 ADR 实现 eval prelude / handle 桥**：归 ADR-4 D11.2/D11.5。

## 遗留

1. 项目级规则保存（`scope=project`）是否值得做，取决于用户是否需要把 OMFG 产物提交进仓库；
   需要审批、路径限制与来源标记，另立 ADR。
2. `smart` 图表在网关获得 judge 能力后是否接线；届时只需在客户端传 `TableChartPlanner`，不改 wire。
3. ratchet 的重新评估条件见 D4；prewalk 见 D5。条件未满足前保持 unsupported，不排期。

## 对 §10 的修正

`docs/omp-ui-panels-gateway-plan.md` §10 的 **OMFG / ratchet / prewalk / auto-graph** 行由本 ADR
收口：

| 项 | 决定 | 退出条件 |
|---|---|---|
| OMFG | **A（skill + rules/TTSR，无网关后端）** | D1–D3 验收通过；面板仍 unsupported，skill 可用 |
| ratchet | **B（不做）** | 保持 unsupported；D4 的重评条件满足才另立 ADR |
| prewalk | **B（不做）** | 保持 unsupported；D5 的重评条件满足才另立 ADR |
| auto-graph | **A（纯客户端接线）** | D6 验收通过；无网关改动 |
