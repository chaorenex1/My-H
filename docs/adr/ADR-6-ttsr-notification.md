# ADR-6：TTSR 通知的忠实移植（omp-ui ↔ Hermes）

**状态**：已决定（2026-11-02）。实现未开始。分阶段：**P1** 本地 viewer 的被动工具规则匹配 +
`ttsr.triggered` 事件 + `TtsrNotificationComponent` + 隐藏 reminder；**P2** AST / judge 规则；**P3**
流式中断（stream rewind）路径另立 ADR。落地前 `docs/omp-ui-panels-gateway-plan.md` §2.3.2.1 的
TTSR 行保持 `无 producer`（§0.1）：客户端不画假通知、不用 `notification.show`/`status.update`/
`agent.print` 冒充。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§2.3.2、§2.3.2.1、§4.7；
`docs/appendix-omp-ui-realtime-rpc.md` A.11、A.15；ADR-2（D9 把 TTSR 判为“无 producer”、D3/D11 的
事件口径）；ADR-3（`append_display_row` 与行/事件边界）；ADR-5（D9 明确 TTSR 不在 Recap/压缩范围内）；
`rules/` corpus、`agent/rule_utils.py`、`tools/rules_tool.py`。

**范围**：只讨论 omp-ui ↔ Hermes canonical local gateway。消息平台、Desktop、Ink sidecar、ACP、
compute-host 的 worker 帧都不设计；也不新增任何跨客户端事件消费方。`local_viewer_turn` 本身不是
omp-ui 的判别式（还包含 one-shot / `chat -q` / ACP / TUI attach / detached 消息会话），所以实现必须用
D1 的 `client:"omp-ui"` session 标识收窄；子代理不在 P1 范围内。通知只走本地 viewer 的事件面，
不改 system prompt、不改 tools、不新增模型工具。

**取代关系（逐条，避免文档互斥）**：

- ADR-2 **D9** 与 ADR-5 **D9** 的 **TTSR 一项**（“无 producer，保持原判断”）由本 ADR 的 D1–D8 取代；
  D9 的其它项（独立 late-diagnostics、被剥离工具调用、collab prompt、reply write、自定义/hook
  消息、Background TAN）不变。
- plan §2.3.2.1:444 的 `TTSR 通知 | 无（omp 专属 stream rewind 规则） | ttsr_triggered 是本机事件 |
  无 producer`：P1 落地后状态改为 `已接（local）`，来源列改为 `ttsr.triggered`；实现前保持原文。
- 附录 A.11:501 的 `ttsr.triggered {rules:{name,path,content}[]}` 由 **D3** 更正为
  `{name,description,content}`；A.15:619 的 TTSR 行在 P1 后移出“不存在”清单。
- ADR-2 **D3/D11** 的事件口径继续适用：payload 构造复用一个 helper、不新增回调、不在事件里塞行；
  本 ADR 只加一个事件，不加 transcript 行型。
- plan §2.3.2 的 `ttsr-notification.ts` 渲染器描述**不变**：组件由 `ctx.present` 直挂，壳不改布局/文案/
  键位；变化只在数据生产者与映射层。

**取证口径**：Hermes 侧全部引用本仓库当前工作树（2026-11-02）。omp 侧源码来自本地 checkout
`C:\Users\zarag\projects\oh-my-pi`（HEAD `6a86df8539fccf3ad7818081266e554022fc21bc`，
`@oh-my-pi/pi-coding-agent` 18.14.0）；渲染侧引用壳实际安装的
`omp-ui/node_modules/@oh-my-pi/pi-tui`（18.14.1）。`omp-ui/src/vendor/` 是上游的裁剪副本；
`omp-ui/src/session/agent-session-events.ts:11-12` 明确把 TTSR 排除在本客户端词汇之外。

## 问题

TTSR 是 omp 的 **Time Traveling Stream Rules**：规则在模型输出流或工具参数流里命中后，中断/回滚当前
输出，并把规则正文作为系统提醒重新注入模型。**通知**只是这条链路的 UI 面：一个即时、可合并、可展开的
transcript notice。两者必须分开移植，否则很容易把“规则列表”“状态行”“注入模板”当成通知。

### 1. omp 的通知语义

**事件**：`AgentSessionEvent` 的一支 `{type:"ttsr_triggered"; rules: Rule[]}`
（`packages/coding-agent/src/session/agent-session-events.ts:88`）；RPC wire 也是
`ttsr_triggered {rules: JSON_OBJECT[]}`（`modes/rpc/wire/events.ts:155-156`）。

**触发源**（不是每个 TTSR 动作都发通知）：

| 来源 | 中断？ | 发 `ttsr_triggered`？ | 注入 |
|---|---|---|---|
| text/thinking 命中 + 允许中断 | 是 | 是（`ttsr-coordinator.ts:702`） | `ttsr-interrupt.md`，回滚后重试 |
| 工具参数命中 + 允许中断 | 是（工具副作用前） | 是（同上） | `ttsr-interrupt.md` |
| 工具参数命中 + `interruptMode: never` | 否 | 是（`ttsr-coordinator.ts:684-686`） | `ttsr-tool-reminder.md`，工具结果之后作为独立 developer 消息 |
| prose 命中 + `interruptMode: never` | 否 | **否** | 延迟 follow-up 注入；不通知 |
| `question` judge 判定命中 | 否 | 是（`ttsr-coordinator.ts:598`） | `ttsr-warning.md` |

去重规则：同一 stream key 的规则名只发一次（`ttsr-coordinator.ts:470-482`）；同一批次里一条规则只挂到
一个 sibling tool call 上（`#addPerToolInjections`，`:431-453`）。prose 的被动命中故意不通知，因为它在
回合结束后才作为 follow-up 注入，通知会先于事实出现。

**渲染组件**：`packages/tui/src/chat/ttsr-notification.ts` 的 `TtsrNotificationComponent`；
壳实际编译的是 `omp-ui/node_modules/@oh-my-pi/pi-tui/dist/types/chat/ttsr-notification.d.ts:4-24`：

- 输入只有 `{name: string; description?: string; content?: string}[]`；`path`、`condition`、`scope`、
  `_source` 都不参与渲染。
- 单条：`Injecting rule: <name> ↺`；正文取 `description || content`，collapsed 只显示前 2 行并追加
  ` (ctrl+o to expand)`（`:106-127`）。
- 多条：`Injecting N rules: ↺`；collapsed 最多 4 条（`MAX_COLLAPSED_RULES`），每条一行 `name: desc`，
  超出显示 `… +N more`；expanded 显示完整 description（`:129-170`）。
- native 终端 presentation 用 `Rule applied: <name>` / `N rules applied: …`，语义角色
  `omp.notice.ttsr`（`:72-101`）。legacy 与 native 的文案差异由组件自己承担，网关不送前缀。
- 连续事件合并：仅当上一块仍是 `chatContainer.children.at(-1)` 且 `canRemoveBlock(previous)` 时，
  `previous.addRules(event.rules)`；否则新建块（`modes/controllers/event-controller.ts:2904-2925`）。
  `addRules` 按 `name` 去重（`ttsr-notification.ts:55-64`）。合并只发生在“还没进入 native scrollback 的
  live tail”；已提交的块是不可变的视觉历史。
- 新块挂载时 `setExpanded(this.ctx.toolOutputExpanded)`，与工具输出展开状态一致
  （`event-controller.ts:2918-2924`）。

**生命周期**：通知不是 entry，没有 role/customType，不进 session JSONL，不进 `/copy`、rewind、resume；
只由实时事件创建，`resetTranscript`/会话切换后消失。`SessionManager` 只持久化
`ttsr_injection` 规则名（`session-manager.ts:3775-3790`），用于 repeat 策略，不持久化通知本身
（`docs/ttsr-injection-lifecycle.md` §7）。

**与注入的关系**：通知展示的是规则名与规则正文；模型看到的是另一份模板化文本
（`ttsr-interrupt.md` / `ttsr-tool-reminder.md` / `ttsr-warning.md`，`docs/omp-prompt-system.md:462-520`）。
通知不展示匹配到的代码片段，也不改写工具输出。

### 2. Hermes 现状：规则有了，运行时没有

**已有的只有“规则文件”这一层**，而且是 omp 兼容的：

- `agent/rule_utils.py:1-17` 明确 frontmatter 兼容 omp 的 `condition` / `astCondition` / `question` /
  `scope` / `globs` / `interruptMode` / `alwaysApply` / `agents`；`normalize_metadata`（`:79-100`）逐字解析，
  `paths` 作为 `globs` 的别名。
- 发现顺序是 project → profile → installation，每个 root 的 `.agents/rules`、`rules/` 以及 skill/plugin
  局部 `rules/`（`rule_search_roots`，`:175-198`）；同一 `rule_id` 第一个胜出（`discover_rules`，`:210-238`）。
- 规则文件已有 26 条（`rules/*.md`，2026-11-02 工作树），其中 21 条有 `condition`、5 条只有
  `astCondition`；**26 条全部是 `interruptMode: never`**。例如 `rules/ts-no-any.md:3-5`
  （`condition` + `scope: "tool:edit(*.ts), …"` + `interruptMode: never`）。
- 模型/CLI 面只有 `rules_list` / `rule_view` / `rule_manage`（`tools/rules_tool.py:169-186`，
  toolset `rules` 在 `toolsets.py:107-111`），以及 `hermes rules` 子命令
  （`hermes_cli/subcommands/rules.py:1-28`）。`rule_utils.check_rules` 已经用 Python `re` 编译
  `condition` 并报告非法正则（`:396-421`）。

**没有运行时**：全仓库没有 `discover_rules` 的运行时消费者（除 CLI/tools/tests 外无调用），没有流/工具
参数匹配器、没有 `ttsr_triggered` 事件、没有 `ttsr-injection` 持久化、没有注入模板。`rule_utils` 只做
“发现 + 解析 + CRUD”，不做匹配；`rules` 工具只是文件管理，不是执行器。这与 plan §2.3.2.1:444 和
附录 A.15:619 的“TTSR/自定义消息在 Hermes 不存在”一致。

**网关契约也没有**：`tui_gateway/contracts/events.py` 没有 `ttsr.*`；`EVENTS` 注册表的
`assert_complete` 会把“有契约无 emitter”的事件判为 orphan（`tui_gateway/contracts/registry.py:177-198`），
所以不能只加一个空契约。

**omp-ui 壳也没有消费点**：`agent-session-events.ts:1-35` 的 `AgentSessionEvent` 不包含 TTSR；
`gateway-session.ts:770-785` 的 `#notificationHandlers` 没有 `ttsr.triggered`，未知类型被表查找静默忽略
（`:787-791`）；`event-controller` 没有 `#handleTtsrTriggered`。安装的 pi-tui 已经带
`TtsrNotificationComponent`，所以渲染组件是现成的，缺的是事件、映射与挂载。

**工具参数不是流式到达**：Hermes 在流结束后组装 `tool_calls`
（`_assemble_tool_calls`，`agent/chat_completion_helpers.py:3308-3313`），执行前才通过
`tool_start_callback` 把**完整**的 `display_args` 交给网关（`agent/tool_executor.py:1040`；
回调装配在 `gateway/run_turn_runner.py:501-508`）。所以 Hermes 没有 omp 的 `toolcall_delta` 可扫，
P1 只能在工具执行前对最终参数做一次匹配。

**工具名不同**：Hermes 的编辑/写入工具是 `patch` 与 `write_file`
（`tools/file_tools.py:1174-1190,1192-1235`，注册在 `:1411,1434`）；omp 的规则 scope 写的是
`tool:edit(...)` / `tool:write(...)`。不做别名映射，规则永远不会命中。

**被动提醒的落点**：Hermes 没有 omp 的 `afterToolCall.additionalContext`，但有工具批次的收尾点
`_finalize_tool_batch`（`agent/tool_executor.py:1220-1231`），以及“持久化但显示隐藏”的既有行机制：
`display_kind="hidden"` 的行在 `_history_to_messages` 被跳过
（`tui_gateway/session_history.py:315-318`），而 `display_kind`/`display_metadata` 在送给 provider 前
被 `PERSISTENCE_ONLY_MESSAGE_FIELDS` 剥掉（`agent/message_metadata.py:46-54`），行本身仍在模型上下文里。
压缩侧也把 `display_kind` 行排除在“human input”之外（`agent/context_compressor.py:4319`），不会把
reminder 当成用户插话。这正好可以承载“给模型看、不给 transcript 看”的 reminder。

**缓存不变量**：Hermes 的会话前缀缓存是硬约束；本 ADR 的 P1 只**追加**一条隐藏 user 行在工具结果之后，
不改写任何已有消息，因此 provider 的已缓存前缀逐字不变，只有后缀增长。P3 的 stream rewind 会回滚
assistant 尾部，属于另一个量级的决定，不能混在通知移植里。

### 3. 直接照搬会错的地方

| 做法 | 结果 |
|---|---|
| 把 `ttsr.triggered` 映射成 `notification.show` / `status.update` / `showWarning` | 只会得到一行 toast/状态行；没有 `TtsrNotificationComponent` 的合并、折叠、展开、`Rule applied` 文案 |
| 从 `rules_list` 的结果合成通知 | 规则存在 ≠ 命中；这是假通知 |
| 在 payload 里送 `path`（附录 A.11 的现有写法） | 组件不读 `path`；还会把本地规则文件路径送进 wire。附录应改为 `{name,description,content}` |
| 对 `json.dumps(args)` 直接跑规则正则 | JSON 转义把换行变成 `\n`，`(?m)^` 与跨行条件失效；还会匹配到路径/键名而非源码 |
| 直接用 `tool:edit` / `tool:write` 匹配 Hermes 工具名 | 永不命中；必须 `edit→patch`、`write→write_file` |
| 把 reminder 作为 `role:"system"` / `"developer"` 插在工具结果后 | Hermes 的多 provider transport 只把首条 system 换 role（`agent/transports/chat_completions.py:367-372`），中途插 system/developer 会被严格 provider 拒绝；用隐藏 user 行最稳 |
| 复用用户 `interrupt` 实现 TTSR 中断 | 用户中断的 stop reason、lease、placeholder、UI 抑制语义都不同；P3 之前不做，不能伪装成被动提醒 |
| 把通知写进 transcript/DB，resume 再重建 | omp 的通知不是 entry；重建会多出原件没有的块，且无法确定它当时合并到哪一块 |
| 只在客户端根据本地 rules 匹配 | 客户端没有模型流/工具执行权威，规则文件也可能在服务端；会产生客户端与服务端不一致的假通知 |

## 决策

### D1. TTSR 是 local-viewer 能力，不是全局 agent 能力

- **不能只用 `local_viewer_turn(self)`**：它的真值是 `Platform.LOCAL` **或** `detached_route_of(source)`
  （`gateway/run_turn_progress.py:1010-1016`），而 `Platform.LOCAL` 还包含 one-shot、`chat -q`、ACP、
  TUI attach（`gateway/run_turn_persistence.py:473-476`），detached 的消息会话也命中。只用它会把这些
  客户端一起打开。
- 新增 **session 级客户端标识**：`session.create` 增加 `client: Literal["omp-ui"] | None = None`
  （`tui_gateway/contracts/canonical_sessions.py:21-56`），`CREATE_FIELDS` 接受 `client`
  （`gateway/session_policy.py:15-22`），策略把它冻结在 `request_json` 并提供
  `LocalSessionPolicy.client`（cached property）；omp-ui 的 create 请求显式带 `client:"omp-ui"`
  （`omp-ui/src/modes/rpc/gateway-session.ts:1204-1208`）。
- coordinator 的安装门是
  `local_viewer_turn(self) and active_policy() is not None and active_policy().client == "omp-ui"`
  （`active_policy` 在 `gateway/session_policy.py:1211-1214`，turn 由 `policy_scope` 包住，
  `gateway/run_turn_runner.py:1242-1250`）。CLI、Desktop、消息平台（含 detached）、ACP、one-shot、
  compute-host 没有 `client:"omp-ui"`，不安装 coordinator、不匹配、不发事件。
- `session.resume` 不重发 `client`：策略已冻结；本 ADR 之前创建的 omp-ui 会话没有该字段，视为
  “未 opt-in”，不启用 TTSR（不猜）。`client` 是 **session 级** surface opt-in，不是连接级、也不是
  安全边界：omp-ui 创建的会话被 Desktop 等客户端 resume 时会继承该标识；若将来要求 per-connection，
  再加连接级 capability，本 ADR 不做。
- **子代理不参与 P1**：coordinator 只挂在父 turn 的 agent 上；`delegate_task` 子代理没有
  `combined_tool_start_callback`/`_publish_execution`，`agents` gate 只按父会话的 agent name 评估。
  子代理的规则执行如需支持，另立变更。
- **匹配与事件生产**放在网关 turn 侧（`TurnRunner` / `run_turn_progress`）；核心只在
  `_finalize_tool_batch` 留一个受 `agent.ttsr_coordinator` 属性门控的调用点（无属性时零变化），
  agent 在 turn 作用域内挂这个可选 coordinator，turn 结束清掉。默认的 CLI/消息/Desktop 路径没有该属性，
  行为逐字不变。
- 事件发布复用 `TurnRunner._publish_execution`（`gateway/run_turn_runner.py:207-213`）→
  `authority.publish_execution`（`gateway/session_authority.py:1007-1019`），与 `tool.start`/`tool.complete`
  同一条 execution generation 校验。generation 不匹配时事件自然不投递。
- 不新增核心模型工具、不改 system prompt、不改 tools 列表；`rules` toolset 已经是文件管理面，不扩成
  执行器。

理由：只有 omp-ui 消费这个通知；把它做成 core feature 会让每次 API 请求多带一个没有其它消费者的工具/提示，
也会让跨客户端行为无意改变。local viewer 的门已经在状态、notice、usage 上使用，语义一致。

### D2. P1 只支持“工具源 + `interruptMode: never`”的被动提醒；其它 TTSR 语义明确拒绝

P1 的触发面：

- 规则必须在 `discover_rules` 的可见集合里，`enabled != false`，`agents` gate 匹配当前 agent；
- **同名规则按发现顺序第一条胜出**：omp 的 TTSR key 是 `name`，而 Hermes 的 `discover_rules` 只按
  `rule_id` 去重；运行时要再做一次 name 折叠，否则两条同名规则会各注入一次。
- 有效 `interruptMode` 必须是 `never`（规则自身覆盖优先；全局 P1 固定为 `never`）。**未写
  `interruptMode` 的规则在 P1 也按 `never` 处理**，而 omp 的全局默认是 `always`；这是 P1 的能力边界，
  P3 恢复 omp 默认。当前 shipped corpus 的 26 条都显式写了 `never`，行为不受这条差异影响；
- `scope` 显式包含一个工具 scope（`tool` / `toolcall` / `tool:<name>` / `tool:<name>(<glob>)`），
  **或没有 `scope`**（omp 的默认 scope 是 text + tool，thinking 不在内；P1 只使用其中的 tool 部分，
  因此无 scope 的规则仍可在工具参数上命中）；
- 有 `condition`（Python `re` 可编译）；`astCondition`/`question` 在 P1 不参与匹配。

对不满足的规则：

- `interruptMode` 为 `always` / `prose-only` / `tool-only`：**不激活**；只有 `astCondition` 或只有
  `question`：P1 同样不激活。两类不兼容规则在会话开始/规则重载时**汇总成一条**可见提示：走
  `status.update{kind:"ttsr_unsupported", text:"…"}`（壳已消费，`gateway-session.ts:966-971` →
  `#onStatusUpdate` → notice），**不走** `notification.show`——壳的 `#notificationHandlers` 没有
  `notification.show`，会按未知类型静默丢弃（`gateway-session.ts:770-791`）。提示按 name 列出，不是
  `ttsr.triggered`；P2/P3 再逐类实现；
- 非法 `condition`：`rule_utils.check_rules` 已经能报错；运行时再编译失败则跳过该规则并 debug 日志，
  不发通知、不注入；
- `repeatMode` 在 P1 固定为 `once`；`after-gap`/`repeatGap` 在 P1 不消费，配置出现时 warn（P2 实现）。

理由：omp 的 `ttsr_triggered` 对“中断”和“被动工具提醒”都会发；但 Hermes 目前没有 stream rewind。
如果把它降级成被动提醒，通知会声称“已注入”而实际没有发生中断，破坏忠实性。拒绝并告知，比伪造好。
当前 shipped corpus 全部是 `never`，其中 21 条 regex 工具规则在 P1 完整可用；5 条 AST-only
规则在 P2 前明确 warn、不激活（通知不会假装它们已生效）。

**P1 的匹配点**：`combined_tool_start_callback`（`gateway/run_turn_progress.py:847-857`）拿到完整
`args`；在这里先匹配、发 `ttsr.triggered`，再发既有的 `tool.start`，让通知排在对应工具卡之前（与 omp
的通知先于工具结果一致），并把命中规则 stash 到 `{tool_call_id: rules}`。此时工具副作用尚未发生，与
omp 在工具执行前判定一致。

**P1 的交付点**：`_finalize_tool_batch`（`agent/tool_executor.py:1220-1231`）在所有工具结果落盘后调用
coordinator：把所有 stash 的规则按 assistant call 顺序拼成一份 `ttsr-tool-reminder` 文本，追加**一条**
`role:"user"`、`display_kind:"hidden"` 的消息到 `messages`。工具结果本身逐字不改。

交付条件对齐 omp：**实际执行并产生结果的 tool call 都交付，包括工具自身返回 error 的结果**——omp 的
`afterToolCall` 无条件消费该 call 的 per-tool bucket（`ttsr-coordinator.ts:291-292,318-321`）；只有
assistant 消息 `stopReason === "aborted" | "error"` 时才清空 buckets（`:510`），或显式
`cancelBridgedToolCall`（`:314-316`）。Hermes 只在“该 call 没有结果行”（被跳过/取消/未执行）或 assistant
整体 aborted/error 时丢弃；**不因单个工具失败而丢弃**。

### D3. wire 契约：`ttsr.triggered` 只送组件需要的三个字段

```text
event ttsr.triggered {
  rules: [
    {name: string, description?: string, content?: string}
  ]
}
```

- `name` = 规则 frontmatter `name`（缺失时 `rule_id`，与 `rule_utils.parse_rule_text` 一致，`:102-116`）。
- `description` = frontmatter description（可为空）。
- `content` = 规则正文（`rule["body"]`，去掉 frontmatter）；组件在 description 为空时用它。
- **不送** `path`、`condition`、`astCondition`、`scope`、`globs`、`interruptMode`、`_source`。组件不读它们；
  `path` 是本地文件系统信息，进 wire 没有收益。
- 同一事件内按发现顺序排列，按 `name` 去重；一条规则只出现一次。
- 事件不新增 `session_id` 字段（网关 envelope 已有）；不新增 `tool_call_id`（组件不需要）。
- 契约加在 `tui_gateway/contracts/events.py`，用 `event("ttsr.triggered", TtsrTriggeredPayload, ...)`
  注册；`Payload` 默认 `extra="forbid"`，payload 漂移会在测试里失败。
- 必须同一 PR 内有真实 emitter，否则 `registry.assert_complete` 的 orphan 门失败
  （`tui_gateway/contracts/registry.py:177-198`）；不能用 `notification.show` 转发代替。
- 重新跑 `scripts/gen_gateway_contracts.py` 与 `scripts/gen_panel_inventory.py`，更新
  `docs/omp-ui-panels.json` 的事件清单（附录 A.14 的生成规则）。

附录 A.11:501 的 `ttsr.triggered {rules:{name,path,content}[]}` **由本 D3 取代**：`path` 改为
`description`。这是更正，不是新增第二份契约。

### D4. 匹配语义：scope 别名 + 每文件 digest + Python regex

**scope 解析**（逐字移植 omp 的 token 语法，`packages/coding-agent/src/capability/rule.ts:77-190`）：

- token `text` / `thinking` / `tool` / `toolcall` / `tool:<name>` / `tool:<name>(<glob>)`；
- 逗号分隔，括号/花括号/引号内的逗号不分割；
- 没有 `scope` 时默认 text + tool（thinking 不在内），但 P1 只使用其中的 tool 部分；
- `globs`（或 Hermes 的 `paths` 别名）是**全局文件路径 gate**，与 scope 内的路径 glob 同时生效；
- 路径 glob 语义按 omp 的 Bun.Glob：`*` 不跨 `/`，`**` 跨目录，`{a,b}` 展开。实现用一个小的
  segment-aware matcher；不要直接用 `fnmatch`（它会让 `*.ts` 跨目录）。

**工具名别名**（必须显式，不可推断）：

| omp scope | Hermes 工具 |
|---|---|
| `edit` | `patch`（replace 与 V4A 两种 mode） |
| `write` | `write_file` |
| `read` | `read_file`（P1 不参与 TTSR，但保留映射供未来 scope 使用） |

**每文件 digest**（对齐 omp 的 `matcherEntries` / `matcherDigest`，`docs/ttsr-injection-lifecycle.md` §1）：

- `write_file`：`args["content"]` 全文，路径 `args["path"]`；
- `patch` replace mode：`args["new_string"]`，路径 `args["path"]`（不是 `old_string`，也不是整个 JSON）；
- `patch` V4A mode：用现有的 `tools/patch_parser.py::parse_v4a_patch`（`:59-120`）拆成
  `(path, added_lines)`；`Add File` 取全部 `+` 行，`Update File` 取所有 `+` 行，按文件分别匹配；
  `Delete`/`Move` 没有新增源码，跳过；
- 其它工具：只对 `args` 中显式的 `path`/`paths` 做路径 gate；没有源文本就不跑条件（与 omp 的
  “streams without a usable file path skip AST conditions” 同向，但 P1 连 regex 也只在这些 digest 上跑）。

**regex**：

- 用 Python `re`（`rule_utils.check_rules` 已经是这个口径，`:396-421`）；omp 的 `compileRuleCondition`
  会把开头的 `(?m)`/`(?s)` 等 inline flag 拆成 flags（`capability/rule.ts:363-370`），Python 原生支持
  开头 inline flag，行为一致；其它 JS-only 构造编译失败就跳过并 warn，不静默。
- 编译结果按 `(rule_id, pattern)` 缓存，turn 内不重复编译；规则文件变更（`hermes rules` 写文件）在
  下一次 turn 重新发现。
- 只扫描 digest，不扫描 `json.dumps(args)`；单文件 digest 以 1 MiB 为硬上限，超过时**跳过该文件**并
  debug 记录，不截断后假装匹配过（避免病态正则扫描整份大文件，也避免把截断点后的真实命中漏成“通过”）；
  shipped corpus 的条件都是局部模式。
- 一条规则只在一个 tool call 上命中一次；同一批次里同名规则先到先得（对齐
  `#addPerToolInjections`，`ttsr-coordinator.ts:431-453`）。

**repeat**：

- P1 只实现 `once`：规则在**成功交付** reminder 后记入 durable 状态；同一会话后续命中不再发通知、
  不再注入。
- 状态落在新表 `session_ttsr_injections(session_id, rule_name, injected_at, PRIMARY KEY(session_id,
  rule_name))`，随会话删除级联清掉。理由：omp 把注入状态持久化在 session 里，重启/resume 后仍抑制；
  用隐藏行 metadata 承载会在压缩归档后丢失，通知就会重复。表有真实消费者（repeat 策略），不是死重量。
  P2 的 `after-gap` 在同一张表加 `completed_turns` 计数，不另起一套。
- 匹配成功但 reminder 没交付（工具失败/中断/被拦截）时，只在内存里记 `once` 抑制，**不写表**；
  与 omp “in-memory mark 不回滚、持久化只在交付时写”一致（`docs/ttsr-injection-lifecycle.md` §4）。

### D5. 注入：工具结果之后的隐藏 user 行，逐字复用 omp 模板

- 模板文本逐字移植 `packages/coding-agent/src/prompts/system/ttsr-tool-reminder.md`（`docs/omp-prompt-system.md:488-505`
  有全文），保留 `{{#if builtin}}` 两条开场：shipped corpus 走 built-in 文案，project/profile 规则走
  user-defined 文案。**不要用 `rule["scope"] == "installation"` 判 builtin**：`install_root()` 在开发
  检出就是仓库根（`pm/paths.py:13-18`），而 `rule_search_roots` 先扫 `cwd/rules` 再去重同目录的
  installation root（`agent/rule_utils.py:175-199`），从仓库根跑时随附 corpus 会被标成 `project`。
  判定改为 `Path(rule["source"]).resolve()` 是否位于 `install_root()`（含 bundled skills/plugins）下，
  并用“cwd=仓库根”的用例验收。`path` 用相对 cwd 的显示路径（复用 `rule_utils` 的 source 与
  `os.path.relpath`，不要把绝对路径发给模型）。
- 多条规则用空行连接，顺序按 assistant tool call 顺序、规则发现顺序；一条规则只出现一次。
- 追加为 `{"role":"user", "content": <rendered>, "display_kind":"hidden"}`：
  - `display_kind="hidden"` 让 `_history_to_messages` 跳过（`tui_gateway/session_history.py:315-318`），
    resume 不会把它画成用户气泡；
  - `PERSISTENCE_ONLY_MESSAGE_FIELDS` 在送给 provider 前剥掉 `display_kind`/`display_metadata`
    （`agent/message_metadata.py:46-54`），模型仍看到 reminder；
  - 用 `user` 而不是 `developer`/`system`：Hermes 的 transport 只重写首条 system 的 role
    （`agent/transports/chat_completions.py:367-372`），中途插 system/developer 在严格 provider 上会 400。
- 插入点：`_finalize_tool_batch` 的最后，`_apply_pending_steer_to_tool_results` 之后（`:1231`），
  保证它是本批次最后一条、且不会与 steer 合并。若本批次有工具没有结果（被中断/跳过），只为有结果且
  未被判定为错误的工具交付 reminder；错误判定沿用 `agent.display._detect_tool_failure`（`agent/display.py:1031`；
  `gateway/run_turn_progress.py:63-64,840-842` 的同一来源），不另写启发式。
- 不修改 tool result 内容；不新增 `custom`/`hookMessage` 行；不把 reminder 写进 assistant 文本。

### D6. 客户端：一个 shell 事件 + 现成组件，不新增行、不落库

- `omp-ui/src/session/agent-session-events.ts` 增加 shell 自有的一支：
  `{ type: "ttsr_triggered"; rules: {name: string; description?: string; content?: string}[] }`。
  这与 omp 本机事件同名同形，但不是 `AgentEvent` 的子集，属于本客户端词汇（与 `notice`、
  `queue_update` 同类）。
- `gateway-session.ts` 的 `#notificationHandlers` 增加 `"ttsr.triggered": payload => this.#onTtsrTriggered(payload)`；
  `#onTtsrTriggered` 只做字段校验（`Array.isArray(rules)`、每项 `name` 非空）并 `#emit` 上述事件。
  不做合并、不做截断、不做文案；这些全部属于组件。
- `event-controller.ts` 增加 `#lastTtsrNotification` 与 `#handleTtsrTriggered`，逻辑逐行对齐
  `event-controller.ts:2904-2925`：
  - 若 `previous && chatContainer.children.at(-1) === previous && chatContainer.canRemoveBlock(previous)`，
    `previous.addRules(event.rules)` 并 `ui.requestRender()`；
  - 否则 `new TtsrNotificationComponent(event.rules)`、`setExpanded(this.ctx.toolOutputExpanded)`、
    `ctx.present(component)`，并记下 `#lastTtsrNotification`。
- 在 `#handleAgentStart`（`event-controller.ts:205-216`）与 transcript 重建/切换会话处清空
  `#lastTtsrNotification`，避免指向已移除的块。
- 不写 `#messages`、不写 `sessionManager`、不写 DB、不参与 `/copy`/rewind/outline；`resume` 后不出现。
  `ctx.present` 是唯一挂载点（`interactive-mode.ts:2125-2134`），与 `StatusNotice` 不同，不进
  `#lastStatus` 的合并线。
- 组件从 `@oh-my-pi/pi-tui/chat/ttsr-notification` 引入；`role: "omp.notice.ttsr"`、native/legacy 两套
  presentation、`MAX_COLLAPSED_RULES=4`、`addRules` 去重都由组件负责，壳不改布局/文案/键位。
- 如果某条规则内容为空且 description 也为空，组件只画 header；这是组件既有行为，客户端不补占位。

### D7. 配置：只加有消费者的三个开关

在 `hermes_cli/config_defaults.py` 增加 `ttsr` 块（behavior config，不进 `.env`；根 AGENTS 的
“`.env` 只放 secrets”规则）：

| key | 默认 | 消费者 |
|---|---|---|
| `ttsr.enabled` | `true` | coordinator 的总开关；false 时不发现、不匹配、不发事件、不注入 |
| `ttsr.builtin_rules` | `true` | false 时跳过 source 位于 `install_root()` 下的 shipped corpus（按 source/root 判定，不按 `rule["scope"]`） |
| `ttsr.disabled_rules` | `[]` | 按 `name` 排除规则（大小写敏感，与 omp 一致） |

不新增 `ttsr.interrupt_mode`（P1 只支持 `never`）、`ttsr.repeat_mode`/`repeat_gap`（P1 固定 `once`）、
`ttsr.context_mode`（只有中断路径才需要）。这些在 P2/P3 有真实消费者时再加，避免“没人读的开关”。

### D8. 分阶段与明确不承诺的项

| 阶段 | 内容 | 完成判据 |
|---|---|---|
| P1 | D1–D7：local viewer、regex 工具规则、`ttsr.triggered`、组件、隐藏 reminder、`once` 持久化 | 随附 26 条规则里 21 条 regex 规则可命中；通知与 reminder 同时出现；resume 不重画通知；其它客户端无变化 |
| P2 | `astCondition`（需要 ast-grep 后端）、`question` judge（复用 `auxiliary.goal_judge` 或新 judge 角色）、prose/thinking 被动 follow-up、`after-gap` | P2 的命中与 omp 的对应模板一致；未支持的规则仍有 warn |
| P3 | `interruptMode: always/prose-only/tool-only` 的流式中断 + `contextMode: discard/keep` + retry | 另立 ADR；必须同时解决 assistant 尾部回滚、stop reason 抑制、prompt cache 代价与 resume 语义，不能在本 ADR 里夹带 |

P3 的 stream rewind 会改变会话尾部（回滚 partial assistant、重新请求），与 Hermes 现有的“用户中断”
不是同一语义（`agent/interrupt_control.py` 的 stop reason/lease/placeholder 是用户中断专用）。在 P3 之前，
需要中断的规则被拒绝，而不是降级；这保证“通知出现”永远对应真实的注入。

## 协议

```text
# server -> client (local viewer only)
event ttsr.triggered {
  rules: [
    {name: "<frontmatter name or rule_id>",
     description: "<frontmatter description, may be ''>",
     content: "<rule body without frontmatter, may be ''>"}
  ]
}

# 客户端内部（不是 wire）
AgentSessionEvent += {type: "ttsr_triggered", rules: NotificationRule[]}
```

不新增方法、不新增 server request、不新增行型；`session.resume`/`session.history` 的 payload 逐字不变。

## 实现落点

| 层 | 位置 | 改动 |
|---|---|---|
| 运行时 | `agent/ttsr_runtime.py`（新） | 规则发现/过滤、scope 解析、别名、glob、digest、Python regex、repeat 状态读取/写入、reminder 渲染 |
| 模板 | `agent/ttsr_prompts.py`（新） | `ttsr-tool-reminder` 逐字文本与 builtin/user-defined 两个开场；不复制到客户端 |
| 网关 | `gateway/run_turn_progress.py` | `combined_tool_start_callback` 调 `coordinator.match_tool_start`，命中则 `_publish_execution("ttsr.triggered", payload)`；`_finalize_tool_batch` 调 `coordinator.after_tool_batch` |
| 身份 | `tui_gateway/contracts/canonical_sessions.py`、`gateway/session_policy.py` | `CanonicalCreateParams.client: Literal["omp-ui"]`；`CREATE_FIELDS` 接受；`LocalSessionPolicy.client` 从冻结 request 读 |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.ts` | `session.create` 带 `client:"omp-ui"`；resume 不重发 |
| 网关 | `gateway/run_turn_runner.py` | `_wire_turn_agent_callbacks` 在 `local_viewer_turn(self)` **且** `active_policy().client == "omp-ui"` **且** `ttsr.enabled` 时挂 `agent.ttsr_coordinator`；turn 结束清掉 |
| 行 | `agent/tool_executor.py::_finalize_tool_batch` | 在 steer 之后调用 `agent.ttsr_coordinator.after_tool_batch(messages)`；无 coordinator 时零变化 |
| 契约 | `tui_gateway/contracts/events.py` | `TtsrRulePayload` / `TtsrTriggeredPayload` + `event(...)` |
| 状态 | `hermes_state_common.py` / `hermes_state_ttsr.py`（新） | `session_ttsr_injections` 建表、`mark_injected`、`injected_names`、会话删除级联 |
| 配置 | `hermes_cli/config_defaults.py` | `ttsr.enabled/builtin_rules/disabled_rules` |
| 客户端 | `omp-ui/src/session/agent-session-events.ts` | 增加 `ttsr_triggered` 词汇 |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.ts` | 增加 `ttsr.triggered` handler 与 `#onTtsrTriggered` |
| 客户端 | `omp-ui/src/modes/controllers/event-controller.ts` | 增加 `#lastTtsrNotification`、`#handleTtsrTriggered`、重置点 |
| 生成 | `scripts/gen_gateway_contracts.py`、`scripts/gen_panel_inventory.py` | 重新生成并 `--check` |
| 文档 | `docs/omp-ui-panels-gateway-plan.md` | 实现后 §2.3.2.1 TTSR 行 `无 producer` → `已接（local）`；实现前保持 |
| 文档 | `docs/appendix-omp-ui-realtime-rpc.md` | A.11 的 `path` 改为 `description`；A.15 的 TTSR 行在 P1 后移出“无 producer” |
| 测试 | `tests/agent/test_ttsr_runtime.py`（新） | scope/别名/digest/glob/regex/repeat/隐藏行 |
| 测试 | `tests/tui_gateway/contracts/test_generated.py` | `ttsr.triggered` 有真实 emitter，orphan 门通过 |

## 验收

1. **命中与通知**：`patch` 一个 `.ts` 文件写入 `const x: any = 1`，客户端恰好收到一条
   `ttsr.triggered`，`rules[0].name == "ts-no-any"`，payload 没有 `path`/`condition`/`scope` 键；
   transcript 出现 `TtsrNotificationComponent`，文案/颜色/图标由组件决定。
2. **合并**：同一回合连续两次 tool call 各命中不同规则，且第一个通知块仍是 live tail 时，两个块合并成
   一块（`addRules` 去重）；若中间插入了工具卡导致上一块不是 tail，则出现两块。expanded 状态跟随
   `toolOutputExpanded`。
3. **注入位置**：下一回合发给 provider 的 `messages` 在对应 tool result 之后恰好有一条 role=user 的
   reminder；tool result 的 `content` 与前缀逐字不变；reminder 文本等于 `ttsr-tool-reminder.md` 的渲染结果。
4. **隐藏**：`session.resume` 的 `display_messages` 不包含 reminder，也不包含 TTSR 通知；客户端
   `#messages`/`/copy`/rewind 看不到它；模型请求仍包含它（`display_kind` 被剥掉，行还在）。
5. **缓存**：命中前后，provider 请求的前缀（system + 命中前的全部消息）逐字节相同；只有 tool result 之后
   多了 reminder。不得出现对已有 user/assistant 消息的重写。
6. **工具别名与 digest**：`write_file` 到 `.go` 且内容含 `runtime.SetFinalizer` 命中 `go-add-cleanup`；
   `patch` replace mode 只看 `new_string`，`old_string` 里的同一模式不命中；V4A 的 `+` 行按文件分别匹配，
   `-` 行不命中。工具自身返回 error（例如 patch 失败）时，只要该 call 实际执行并产生结果，reminder
   仍交付（与 omp 的 `afterToolCall` 无条件消费一致）。
7. **scope/glob**：`tool:edit(*.ts)` 不匹配 `.md`；`(**/*.ts)` 匹配嵌套路径；`{ts,tsx}` 展开；
   没有可解析文件路径的工具不跑条件；无 `scope` 的规则按 omp 默认（text+tool）在工具参数上参与；
   `globs`/`paths` 与 scope 同时生效。
8. **拒绝语义**：`interruptMode: always` 的规则与只有 `astCondition`/`question` 的规则在会话开始时
   **汇总成一条** `status.update{kind:"ttsr_unsupported"}` 提示（不是 `notification.show`，壳会丢弃它），
   不产生 `ttsr.triggered`、不注入、不中断（P2/P3 前不伪装）。
9. **开关**：`ttsr.enabled=false` 时无事件/无注入；`disabled_rules` 按 name 排除；
   `builtin_rules=false` 时 source 位于 `install_root()` 下的 shipped corpus 不加载；project/profile
   规则仍可用（cwd=仓库根也要正确判定）。
10. **repeat**：同一会话第二次命中同一规则不再发通知/注入；重启/resume 后仍抑制（`session_ttsr_injections`
    有记录）；未交付的命中不写表，resume 后可再次命中。
11. **客户端门**：只有 `session.create{client:"omp-ui"}` 的会话安装 coordinator。`local_viewer_turn` 为真
    但没有该标识的路径——one-shot、`chat -q`、ACP、TUI attach、detached 消息会话——都不匹配、不发
    `ttsr.triggered`、不注入 reminder；`session.resume` 读冻结策略，旧会话没有 `client` 就不启用。
    同一回合 Desktop/消息平台的上下文没有 reminder。子代理（`delegate_task`）也不执行 P1。
12. **契约门**：`scripts/gen_gateway_contracts.py --check` 与 `scripts/gen_panel_inventory.py --check`
    通过；`tests/tui_gateway/contracts/test_generated.py` 的 orphan 门通过；`ttsr.triggered` 在
    `BackendGatewayEventMap`/`docs/omp-ui-panels.json` 的事件清单里。

## 风险

| 风险 | 缓解 |
|---|---|
| 病态正则阻塞 agent 线程 | digest 有上限；规则是本地可信配置；shipped corpus 已由 `hermes rules check` 编译验证；P1 只在工具执行前扫描一次，不在每个 token 上跑 |
| Python `re` 与 JS regex 语义差异 | `compileRuleCondition` 只翻译开头 inline flag（`rule.ts:363-370`），Python 原生支持；编译失败跳过并 warn，不把“无命中”伪装成“通过” |
| JSON 转义导致漏匹配 | 只用每文件源码 digest（`write_file.content`、`patch.new_string`、V4A 的 `+` 行），不扫整个 args |
| 工具名别名漏掉 | 别名表是唯一入口；单测覆盖 `edit/write/read` 与 Hermes 的三个真实工具名；未知工具只做路径 gate |
| 隐藏 user 行改动模型行为 | 这是 TTSR 注入的本意；只对 local viewer 开启；内容来自本地规则；`display_kind=hidden` 不进 transcript |
| 严格 provider 拒绝中途 system/developer | 用 role=user + `<system-reminder>` 文本；不中途插 system/developer |
| 压缩把注入状态/行清掉 | 注入状态放独立表，不依赖隐藏行；隐藏行只是内容载体，压缩后即使丢了，也只影响下一回合的模型提醒，不影响 UI 通知 |
| 通知与注入不一致（事件已发、reminder 未交付） | 与 omp 一致：事件在匹配时发；只有 call 未执行（跳过/取消）或 assistant 整体 aborted/error 时不交付，单个工具 error 仍交付；通知是即时状态，不是持久事实；验收 10 明确未交付不写 repeat 表 |
| 事件 payload 撑大帧 | 只送 `name`/`description`/`content`；规则正文来自本地文件；超过 frame 上限时按契约错误暴露，不静默截断成假正文 |
| 客户端把通知写进 `#messages` | D6 明确只 `ctx.present`，不碰 `#messages`；验收 4 断言 resume/`/copy` 无它 |
| 规则正文被当成用户输入 | reminder 用 omp 的 `<system-reminder reason="rule_violation" …>` 包装并声明 not prompt injection；与 omp 同文 |
| 规则文件在 turn 中途变化 | 每次 turn 重新 `discover_rules`；turn 内使用同一份快照，避免同一批次命中两套规则 |

## 不采纳

| 方案 | 为什么 |
|---|---|
| 把 `ttsr.triggered` 映射为 `notification.show`/`status.update` | 那是状态行，不是 `TtsrNotificationComponent`；没有合并、折叠、native 文案，且会与 `agent.print` 重复 |
| 从 `rules_list`/`rule_view` 合成“规则已应用” | 没有命中事实；通知必须对应真实注入 |
| 在客户端本地跑规则匹配 | 客户端没有模型流/工具执行的权威，规则发现也在服务端；会产出与服务端不一致的通知，且无法持久化 repeat 状态 |
| 在 payload 里带 `path`/`condition`/`scope` | 组件不读；`path` 泄露本地路径；附录 A.11 的 `path` 由 D3 更正为 `description` |
| 用 `json.dumps(tool_args)` 跑条件 | 转义破坏多行 regex，且匹配键名/路径而非源码 |
| 把工具名 `edit`/`write` 原样匹配 | Hermes 的工具叫 `patch`/`write_file`；必须别名 |
| 把 reminder 改成 tool result 的一部分 | 违反 omp 的“never as part of tool output”；会污染工具结果与持久化 |
| 中途插 `role:"system"`/`"developer"` | Hermes transport 只重写首条 system；严格 provider 会拒绝 |
| 复用用户 `interrupt` 实现 TTSR 中断 | stop reason、lease、placeholder、UI 抑制语义不同；P3 前不做，避免假中断 |
| 把通知做成 `display_kind="ttsr"` 的持久行 | omp 的通知不是 entry；持久行会出现在 resume/`/copy`/rewind，且无法重建合并边界 |
| 新增独立 `ttsr` 模型工具 | `rules` toolset 已存在；模型工具每次 API 请求都携带，且匹配是服务端运行时，不是模型动作 |
| 用 `review.summary`/`agent.print` 冒充通知 | 语义不同；会污染 review/状态通道 |
| 在 P1 顺便实现 AST/judge/interrupt | 每个都需要新的后端/上下文改写；夹带会让通知的“真实触发”无法验收，且 P3 会破坏缓存不变量 |

## 遗留

- P2 的 AST 后端：当前 Python 环境没有 ast-grep 绑定（`import ast_grep_py` 失败）；需要 PM 依赖或
  `ast-grep` CLI + 进程桥，另立变更。
- P2 的 judge：复用 `auxiliary.goal_judge` 还是新增 judge 角色，需要单独决定；P1 不产生 judge 通知。
- P3 的 stream rewind：必须先定义 assistant 尾部回滚、`stop_reason` 抑制、`contextMode` 与 prompt cache
  代价；本 ADR 只登记接缝，不实现。
