# ADR-6：TTSR 的忠实移植（通知与 stream rewind，omp-ui ↔ Hermes）

**状态**：已决定（2026-11-02）；**P1 已实现（2026-11-04）**，P2 的 `question` judge 由
ADR-11 P3 同批落地；P2 的 `astCondition` **已实现**（ast-grep CLI + 进程桥，D4，2026-11-04），
prose 未开始；**P3** 流式中断（stream rewind）、`message.rewind` 与 repeat
`after-gap`/`repeat_gap` **已实现（2026-11-04，D9–D14）**；`contextMode: keep` 与 prose 被动
follow-up 见「遗留」。
**carrier 修订（2026-11-04）**：D5/D12 的注入行改为 omp 形状的 `role:"custom"` +
`custom_type:"ttsr-injection"`，复用既有的 `project_for_api` 转成 provider `developer`（行形状与
`details` 以 ADR-10 D1 为准；stream rewind 的 `details.matched` 另带摘录）。**已落地**：
`ttsr-injection` 已列入 `MESSAGE_CUSTOM_TYPES`（`agent/custom_messages.py`），P1 的两处 append
（`after_tool_batch`、`_judge_outputs`）经 `agent/ttsr_runtime.py::injection_row` 写一条
`role:"custom"` 隐藏行（`display:false`、`display_only:false`、`details.rules`），送 provider 前转
`developer`；由 `tests/agent/test_ttsr_runtime.py` 与 `tests/tui_gateway/test_custom_messages.py` 覆盖。
P1 落地后 `docs/omp-ui-panels-gateway-plan.md` §2.3.2.1 的 TTSR 行已改为 `已接（local）`（§0.1）。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§2.3.2、§2.3.2.1、§4.7；
`docs/appendix-omp-ui-realtime-rpc.md` A.11、A.15；ADR-2（D9 把 TTSR 判为“无 producer”、D3/D11 的
事件口径）；ADR-3（`append_display_row` 与行/事件边界）；ADR-5（取代关系明确 TTSR 不在 Recap/压缩范围内）；
ADR-11（`judge` 路线、typed judgments 与 `question` 规则的判定后端）；
`rules/` corpus、`agent/rule_utils.py`、`tools/rules_tool.py`；P2 AST 另涉及
`pm/code_intelligence_packages.py` 的可选 `ast-grep` 包、`hermes_platform/resolver` 的可执行解析、
`agent/ttsr_ast.py`（进程桥）。P3 另涉及 `agent/stream_delivery.py`、
`agent/chat_completion_helpers.py`、`agent/turn_api_call.py`、`agent/turn_iteration_prep.py`、
`agent/turn_tool_round.py`、`agent/turn_retry_state.py`、`agent/conversation_loop.py`、
`gateway/run_turn_runner.py`、`gateway/local_viewer_events.py`、`tui_gateway/contracts/events.py`、
`tui_gateway/stream_segments.py`，以及客户端 `omp-ui/src/modes/rpc/gateway-session.ts`、
`omp-ui/src/modes/controllers/event-controller.ts`。

**范围**：只讨论 omp-ui ↔ Hermes canonical local gateway。消息平台、Desktop、Ink sidecar、ACP、
compute-host 的 worker 帧都不设计；也不新增任何跨客户端事件消费方。`local_viewer_turn` 本身不是
omp-ui 的判别式（还包含 one-shot / `chat -q` / ACP / TUI attach / detached 消息会话），所以实现必须用
D1 的 `client:"omp-ui"` session 标识收窄；子代理不在范围内。通知只走本地 viewer 的事件面，
不改 system prompt、不改 tools、不新增模型工具。P3 的 stream rewind 只实现 omp 默认的
`contextMode: discard`；`keep` 明确拒绝（D14）。

**取代关系（逐条，避免文档互斥）**：

- ADR-2 **D9** 的 **TTSR 一项**（“无 producer，保持原判断”；ADR-5 的取代关系沿用该判断）由本 ADR 的
  D1–D14 取代；D9 的其它项（独立 late-diagnostics、被剥离工具调用、collab prompt、reply write、自定义/hook
  消息、Background TAN）不变。
- plan §2.3.2.1:444 的 `TTSR 通知 | 无（omp 专属 stream rewind 规则） | ttsr_triggered 是本机事件 |
  无 producer`：P1 落地后状态改为 `已接（local）`，来源列改为 `ttsr.triggered`；实现前保持原文。
- 附录 A.11 的 `ttsr.triggered {rules:{name,path,content}[]}` 由 **D3** 更正为
  `{name,description,content}`；A.15 的 TTSR 行在 P1 后移出“不存在”清单。
- ADR-2 **D3/D11** 的事件口径继续适用：payload 构造复用一个 helper、不新增回调、不在事件里塞行；
  本 ADR 只加 `ttsr.triggered`（P1）与 `message.rewind`（P3）两个 live-only 事件，不加 transcript 行型。
- plan §2.3.2 的 `ttsr-notification.ts` 渲染器描述**不变**：组件由 `ctx.present` 直挂，壳不改布局/文案/
  键位；变化只在数据生产者与映射层。

**取证口径**：Hermes 侧 P1/D1–D7 引用本仓库工作树（2026-11-02）；P2 AST（D4、D8、验收 13）以
2026-11-04 的 `ast-grep` CLI（0.50/0.51）行为与 `pm` 可选包为准；P3（D9–D14、验收 14–22）以
2026-11-04 的工作树为准，另引用 `agent/stream_delivery.py`、`agent/chat_completion_helpers.py`、
`agent/turn_api_call.py`、`agent/turn_iteration_prep.py`、`agent/turn_tool_round.py`。omp 侧源码来自本地 checkout
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

### 2. Hermes 现状（2026-11-02 决策；carrier 能力 2026-11-04 校准）：规则有了，运行时没有

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

**没有运行时**：决策时全仓库没有 `discover_rules` 的运行时消费者（除 CLI/tools/tests 外无调用），没有流/工具
参数匹配器、没有 `ttsr_triggered` 事件、没有 `ttsr-injection` 持久化、没有注入模板。`rule_utils` 只做
“发现 + 解析 + CRUD”，不做匹配；`rules` 工具只是文件管理，不是执行器。这与 plan §2.3.2.1:444 和
附录 A.15 的“TTSR/自定义消息在 Hermes 不存在”一致。

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
`_finalize_tool_batch`（`agent/tool_executor.py:1220-1231`），以及 ADR-10 的 custom 行 + 角色转换：
`role:"custom"` 行在 `agent/custom_messages.py:193-225` 的 `project_for_api` 转成 provider
`developer`（`ttsr-injection` 需列入 `MESSAGE_CUSTOM_TYPES`，`:37`），`display:false` 让客户端不渲染
（`omp-ui/src/modes/utils/ui-helpers.ts:205`）；OpenAI-compatible 适配器在不支持 `developer` 时由
`_normalize_developer_roles` 降成 `system`（`agent/transports/chat_completions.py:376-385`），
Anthropic/其它适配器按 ADR-10 D11 的 provider 归一化折叠（Anthropic 默认 `user`，仅特定模型可升级为
mid-conversation `system`）。这正是 omp 的 `developer` passive context 的等价物。旧的
`display_kind="hidden"` user 行仍服务于其它内部提醒，但 TTSR 不再使用它（D5/D12 的 carrier 修订）。

**缓存不变量**：Hermes 的会话前缀缓存是硬约束；本 ADR 的 P1 只**追加**一条 custom
`ttsr-injection` 行在工具结果之后，不改写任何已有消息，因此 provider 的已缓存前缀逐字不变，只有后缀
增长。P3 的 stream rewind 在 D9–D14 单独处理：它在 assistant 行 append 之前丢弃响应，同样不改写已发送
字节（D12）。

### 3. 直接照搬会错的地方

| 做法 | 结果 |
|---|---|
| 把 `ttsr.triggered` 映射成 `notification.show` / `status.update` / `showWarning` | 只会得到一行 toast/状态行；没有 `TtsrNotificationComponent` 的合并、折叠、展开、`Rule applied` 文案 |
| 从 `rules_list` 的结果合成通知 | 规则存在 ≠ 命中；这是假通知 |
| 在 payload 里送 `path`（附录 A.11 的现有写法） | 组件不读 `path`；还会把本地规则文件路径送进 wire。附录应改为 `{name,description,content}` |
| 对 `json.dumps(args)` 直接跑规则正则 | JSON 转义把换行变成 `\n`，`(?m)^` 与跨行条件失效；还会匹配到路径/键名而非源码 |
| 直接用 `tool:edit` / `tool:write` 匹配 Hermes 工具名 | 永不命中；必须 `edit→patch`、`write→write_file` |
| 把 reminder 作为裸 `role:"system"` / `"developer"` 行插在工具结果后 | 会话行不应携带 provider role；用 `role:"custom"` + `custom_type:"ttsr-injection"`，由 `project_for_api` 转成 `developer`（OpenAI-compatible 不支持 developer 时降 `system`；Anthropic/其它适配器按 ADR-10 D11 折叠） |
| 复用用户 `interrupt` 实现 TTSR 中断 | 用户中断的 stop reason、lease、placeholder、UI 抑制语义都不同；P3 也不复用，改用独立的被动 abort，不能伪装成用户停止 |
| 把通知写进 transcript/DB，resume 再重建 | omp 的通知不是 entry；重建会多出原件没有的块，且无法确定它当时合并到哪一块 |
| 只在客户端根据本地 rules 匹配 | 客户端没有模型流/工具执行权威，规则文件也可能在服务端；会产生客户端与服务端不一致的假通知 |

### 4. Hermes 现状（P3 决策时，2026-11-04）：可复用的中断/重试骨架

Hermes 没有 stream rewind，但重试所需的三段骨架已经存在；P3 不新造 turn 语义，只把它们接起来。

**流式 delta**：文本 delta 在 `agent/chat_completion_helpers.py:2822` 的 `_emit_text` →
`_fire_stream_delta`（`agent/stream_delivery.py:332`）到显示/TTS；reasoning 在 `_emit_reasoning`
（`:2833`）→ `_fire_reasoning_delta`（`stream_delivery.py:370`）。工具参数只有“开始生成”回调
（`_emit_tool_started`，`:2838`），没有逐参数 delta。网关 turn 的 omp-ui 能看到的文本走
`gateway/run_turn_runner.py:322` 的 `stream_delta_cb`（`message.block_start`/`message.delta`）；它不接
`reasoning_callback`（`run_turn_runner.py:515` 置 None），但 `_emit_reasoning` 不依赖该回调，P3 可在
agent 侧匹配。工具参数只能像 P1 一样用“最终参数”匹配。

**被动中止 + 同 turn 重试**：`redirect()`（`agent/interrupt_control.py:333-393`）已经实现“只取消当前
请求、不结束 turn”；`handle_api_interrupt`（`agent/turn_api_call.py:177-217`）在 `_pending_redirect` 存在
时把 `_retry.restart_with_redirected_messages` 置真并 `break`；`apply_retry_restarts`
（`agent/turn_iteration_prep.py:484-511`）退回 `api_call_count`/iteration budget 并让外层 `while` 重进
`begin_iteration`；`begin_iteration`（`:357-450`）在下一请求前应用修正并持久化。`perform_api_call`
（`turn_api_call.py:150-170`）还处理“响应与中止交叉”的竞态。`redirect()` 的用户语义
（`_turn_user_intervened`、`_pending_redirect` 文本、可见 correction 行）不能复用，P3 需要一条并行的
TTSR 通道（D11）。

**工具行是“先持久化、后执行”**：`run_tool_round` 在 `stage_tool_call_message`
（`agent/turn_tool_round.py:110`）后 `append_message`（`:113`）并 `_flush_messages_to_session_db`
（`:136`）才执行工具。P1 的匹配点在 `combined_tool_start_callback`
（`gateway/run_turn_progress.py:847-857`），已经在持久化与执行之后，不能阻止副作用。P3 必须把工具匹配
移到 `run_tool_round` 的 staging 之前（D10）；在该点丢弃响应不需要删除任何已落库行。

**custom 行与缓存**：TTSR 的 reminder 由 `role:"custom"` + `custom_type:"ttsr-injection"` +
`display:false` + `details:{rules}` 承载；`project_for_api` 在送 provider 前把它转成 `developer` 并剥掉
custom/display 字段（`agent/custom_messages.py:193-225`），客户端按 `display:false` 不渲染
（`omp-ui/src/modes/utils/ui-helpers.ts:205`）。`build_api_messages`
（`agent/turn_context.py:1267-1278`）要求历史行的 `api_content`/字节稳定；P3 的丢弃发生在 assistant 行
append 之前，因此被中止的响应从未进入任何后续请求，前缀缓存不受影响（D12）。

**没有的东西**：没有 delta 级 TTSR 匹配器；`select_rules` 目前把非 `never` 与缺失 `interruptMode` 的
规则判为 unsupported（`agent/ttsr_runtime.py:408-415`）；没有“丢弃 live draft”的 wire 事件；没有可用于
“删除刚 flush 的 assistant 行”的 helper（`SessionDB.rewind_to_message` 是 turn 粒度，且 P3 不需要它）。

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
- 有效 `interruptMode` 必须是 `never`（规则自身覆盖优先；全局 P1 固定为 `never`）。**带
  `condition`/`astCondition` 但未写 `interruptMode` 的规则按 omp 全局默认 `always` 处理，归入
  unsupported**——P1 没有 stream rewind，不能静默降级成被动提醒；`question`-only 规则没有中断路径，
  可以省略。当前 shipped corpus 的 26 条都显式写了 `never`；`hermes rules check` 对缺字段的 regex
  规则报 warning；
- `scope` 显式包含一个工具 scope（`tool` / `toolcall` / `tool:<name>` / `tool:<name>(<glob>)`），
  **或没有 `scope`**（omp 的默认 scope 是 text + tool，thinking 不在内；P1 只使用其中的 tool 部分，
  因此无 scope 的规则仍可在工具参数上命中）；
- 有 `condition`（Python `re` 可编译）或 `astCondition`（ast-grep pattern）；两者是 OR 关系，
  任一命中即算命中。`question` 由 ADR-11 P3 的判定后端消费（见下）。

对不满足的规则：

- `interruptMode` 为 `always` / `prose-only` / `tool-only`，或 regex/AST 规则缺失该字段：**P1 不激活**
  （P3 起由 D9 按规则/全局 `ttsr.interrupt_mode` 恢复这四类语义；本段只是 P1 的能力边界）；
  只有 `astCondition` 且本机没有 ast-grep CLI：归入 unsupported（reason 指向
  `hermes pm install ast-grep`），CLI 可用时正常激活。
  `question` 规则不走 regex 条件匹配：P1 不激活；ADR-11 P3 落地后由判定后端对完成后的 assistant
  输出（reply/reasoning）与工具调用 digest 判定，命中仍复用本 ADR 的 `ttsr.triggered` 与隐藏 warning
  （ADR-11 D9）。不兼容规则在会话开始/规则重载时**汇总成一条**可见提示：发
  `status.update{kind:"ttsr_unsupported", text:"…"}`，并同时发一条 `transcript.notice`（level
  `warning`、source `ttsr`）——in-tree shell 的 `#onStatusUpdate` 只处理 compact，不消费未知 kind 的
  text，`transcript.notice` 才是它实际渲染的通道。**不走** `notification.show`。提示按 name 列出，不是
  `ttsr.triggered`；P2/P3 再逐类实现；
- 非法 `condition`：`rule_utils.check_rules` 已经能报错；运行时再编译失败则跳过该规则并 debug 日志，
  不发通知、不注入；
- `repeatMode` 在 P1 固定为 `once`；`after-gap`/`repeatGap` 在 P1 不消费，配置出现时不报错、按 `once`
  执行（P3 实现，见 D4/D14）。

理由：omp 的 `ttsr_triggered` 对“中断”和“被动工具提醒”都会发；但 Hermes 目前没有 stream rewind。
如果把它降级成被动提醒，通知会声称“已注入”而实际没有发生中断，破坏忠实性。拒绝并告知，比伪造好。
当前 shipped corpus 全部是 `never`，其中 21 条 regex 工具规则在 P1 完整可用；5 条 AST-only
规则在 ast-grep CLI 可用时由 D4 的进程桥激活，CLI 缺失时明确 warn、不激活（通知不会假装它们已生效）。

**P1 的匹配点**：`combined_tool_start_callback`（`gateway/run_turn_progress.py:847-857`）拿到完整
`args`；在这里先匹配、发 `ttsr.triggered`，再发既有的 `tool.start`，让通知排在对应工具卡之前（与 omp
的通知先于工具结果一致），并把命中规则 stash 到 `{tool_call_id: rules}`。此时工具副作用尚未发生，与
omp 在工具执行前判定一致。

**P1 的交付点**：`_finalize_tool_batch`（`agent/tool_executor.py:1220-1231`）在所有工具结果落盘后调用
coordinator：把所有 stash 的规则按 assistant call 顺序拼成一份 `ttsr-tool-reminder` 文本，追加**一条**
`role:"custom"`、`custom_type:"ttsr-injection"`、`display:false` 的 carrier 到 `messages`（D5）。
工具结果本身逐字不改。

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
- 重新跑 `scripts/gen_gateway_contracts.py --check`；`ttsr_notification` 区块由
  `gateway/panel_inventory.py::TRANSCRIPT_BLOCKS` 声明、经 `runtime.describe.transcript_blocks`
  暴露（`docs/omp-ui-panels.json` 只列 panels/methods/events，不含 transcript blocks）。

附录 A.11 的 `ttsr.triggered {rules:{name,path,content}[]}` **由本 D3 取代**：`path` 改为
`description`。这是更正，不是新增第二份契约。

### D4. 匹配语义：scope 别名 + 每文件 digest + regex / ast-grep

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
- `scope`/`globs` 对每个 `(path, text)` 单独判定，不能用整批 paths 做“任一匹配”后扫描所有 digest：
  V4A 同时改 `a.ts` 与 `b.md` 时，`tool:edit(*.ts)` 不能命中 `b.md` 的 `+` 行。

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

**astCondition（ast-grep CLI + 进程桥）**：

- ast-grep 是 **optional PM package**（`hermes pm install ast-grep`），不进入应用 venv、不新增
  Python 依赖。解析顺序：`pm.install.env_for("ast-grep")` 的激活 PATH →
  `hermes_platform.resolver.locate_command("ast-grep")`；只找 `ast-grep`，不回退到 `sg`
  （Linux 上 `sg` 是 util-linux）。探测每 turn 一次，不常驻。
- 只对 D4 的每文件 digest 跑；语言由工具路径扩展名推导（`ts/tsx/js/jsx/go/java/c/cpp/...`；
  `mts`/`cts`→`ts`，`mjs`/`cjs`→`js`，`rs`→`rust`，`py`/`pyi`→`python`，`yml`→`yaml`，
  `sh`/`zsh`→`bash`，`h`→`c`，`cc/cxx/hpp/hh/hxx`→`cpp`）。没有扩展名、未知扩展名或语言不被
  ast-grep 支持时不跑该文件，debug 记录；不把“没跑”伪装成“没命中”。
- 每个 pattern 一次进程：
  `ast-grep run --pattern <p> --lang <lang> --strictness smart --stdin --files-with-matches
  --color never --threads 1`。源码走 stdin，**不写临时文件、不扫 workspace、不经 shell**；
  多个 pattern 任一命中即规则命中。`condition` 与 `astCondition` 是 OR，同一规则任一条件命中一次。
- 有界：单 pattern 超时 2s（`AST_GREP_TIMEOUT_S`），每次 tool call 的 AST 总预算 3s；超时 /
  退出码 8（pattern 或语言错误）/ 空输出 / 其他非 0/1 退出码一律视为**不命中**并 debug，
  不静默通过。结果按 `(pattern, language, sha256(source))` 在 turn 内缓存（上限 512），
  同一 digest 不重复起进程。
- CLI 缺失时：只有 `astCondition` 的规则在 `select_rules` 归入 unsupported，reason 带安装提示；
  同时带 `condition` 的规则仍按 regex 工作，AST 部分跳过并 debug（不能因为可选后端缺失把整条规则
  禁用）。
- AST 结果只参与“命中/不命中”判断：不写 transcript、不新增事件字段、不把 match 文本送进 wire；
  reminder 仍只含规则正文。

**repeat**（P3 同时交付 `after-gap`/`repeat_gap`，对齐 omp 的 `repeatMode`/`repeatGap`）：

- 两种模式，全局 `ttsr.repeat_mode` 默认 `once`（D14）：
  - `once`：规则在**成功交付** reminder 后记入 durable 状态；同一会话后续命中不再发通知、不再注入。
  - `after-gap`：交付后不永久抑制；当 `completed_turns - last_injected_turn >= repeat_gap` 时规则重新
    可触发（`repeat_gap` 默认 10，整数 ≥1）。
- `completed_turns` 只在 `turn_end` 递增，按**完成回合**而不是 stream chunk 计数；同一 turn 内同一规则
  命中多次仍然只交付一次，gap 不会在 turn 中途满足（对齐 omp 的 `#messageCount`）。
- **状态归属**：`TtsrCoordinator` 每个 turn 重建（`agent/ttsr_runtime.py::install_coordinator`），所以
  `completed_turns` 与 per-rule `last_injected_turn` 必须和 `_ttsr_memory_claimed` 一样挂在 **agent**
  上（session 生命周期：`agent._ttsr_repeat_turns[session_id]`、`agent._ttsr_last_injected_turn[session_id]`）；
  放在 coordinator 上会让 gap 每个 turn 归零。`once` 仍用 `memory_claimed` 永久集合；`after-gap` 下
  claim 记录命中时的 turn 计数（含未交付的 match），达到 gap 后重新可命中（对齐
  `docs/ttsr-injection-lifecycle.md` §9）。
- durable 状态只存规则名：`session_ttsr_injections(session_id, rule_name, injected_at, PRIMARY KEY(
  session_id, rule_name))`，随会话删除级联清掉，**不新增 `completed_turns` 列**。理由：omp 也只持久化
  规则名；turn 计数是进程内状态，resume 后从 0 计，存计数既无消费者又会与会话真实 turn 数错位。用
  custom 行 metadata 承载会在压缩归档后丢失，通知就会重复；表有真实消费者（repeat 策略），不是死重量。
- resume 语义对齐 omp 的 `restoreInjected()`：重启/`session.resume` 只恢复规则名，`completed_turns`
  从 0 重新计、恢复的规则以 `last_injected_turn = 0` 参与判定；`after-gap` 规则因此需要再完成
  `repeat_gap` 个 turn 才可再次触发，不按旧墙钟补算。
- 匹配成功但 reminder 没交付（工具失败/中断/被拦截）时，只在内存里记 claim，**不写表**；
  与 omp “in-memory mark 不回滚、持久化只在交付时写”一致（`docs/ttsr-injection-lifecycle.md` §4）。
  内存标记挂在会话 agent 上，跨 turn 保留；进程重启/`session.resume` 后丢失，因此未交付的命中仍可在
  新进程里再次命中（验收 10）。

### D5. 注入：工具结果之后的隐藏 custom 行，逐字复用 omp 模板

- 模板文本逐字移植 `packages/coding-agent/src/prompts/system/ttsr-tool-reminder.md`（`docs/omp-prompt-system.md:488-505`
  有全文），保留 `{{#if builtin}}` 两条开场：shipped corpus 走 built-in 文案，project/profile 规则走
  user-defined 文案。**不要用 `rule["scope"] == "installation"` 判 builtin**：`install_root()` 在开发
  检出就是仓库根（`pm/paths.py:13-18`），而 `rule_search_roots` 先扫 `cwd/rules` 再去重同目录的
  installation root（`agent/rule_utils.py:175-199`），从仓库根跑时随附 corpus 会被标成 `project`。
  判定改为 `Path(rule["source"]).resolve()` 是否位于 `install_root()`（含 bundled skills/plugins）下，
  并用“cwd=仓库根”的用例验收。`path` 用相对 cwd 的显示路径（复用 `rule_utils` 的 source 与
  `os.path.relpath`，不要把绝对路径发给模型）。
- 多条规则用空行连接，顺序按 assistant tool call 顺序、规则发现顺序；一条规则只出现一次。
- 追加为 omp 形状的 custom 行（ADR-10 D1 的 `role:"custom"` 行，不新增行型/角色）：
  `{"role":"custom", "custom_type":"ttsr-injection", "content":<rendered>, "display":false,
  "display_only":false, "details":{"rules":[<names>]}, "attribution":"agent", "legacy_role":"user"}`。
  - `custom_type:"ttsr-injection"` 复用既有角色转换：在 `agent/custom_messages.py:37` 的
    `MESSAGE_CUSTOM_TYPES` 里注册该类型即可，`project_for_api`（`:193-225`）把它转成 `developer`，
    不新增转换器；TTSR 的 `attribution:"agent"` 不走 skill/collab 的 user 特例。
    omp 的工具提醒本身就是 `developer` 被动上下文
    （`tool-context.ts:48-54`），这是最接近的角色。
  - `display:false` 让客户端不渲染（`omp-ui/src/modes/utils/ui-helpers.ts:205`）；`display`/`details`/
    `custom_type` 等字段在送 provider 前被 `project_for_api`/`PERSISTENCE_ONLY_MESSAGE_FIELDS` 剥掉，
    模型仍看到 reminder。
  - 不要直接把 `role:"developer"`/`role:"system"` 写进会话行：provider role 由转换层决定；
    OpenAI-compatible 适配器在不支持 `developer` 时由 `_normalize_developer_roles`
    （`agent/transports/chat_completions.py:376-385`）降成 `system`；Anthropic/其它适配器按 ADR-10 D11
    的 provider 归一化折叠（Anthropic 默认 `user`，仅特定模型可升级为 mid-conversation `system`）。
- 插入点：`_finalize_tool_batch` 的最后，`_apply_pending_steer_to_tool_results` 之后，保证它是本批次
  最后一条、且不会与 steer 合并。交付条件按 D2/验收 6：**只要该 call 实际执行并产生了结果行就交付，
  包括工具自身返回 error**；只有该 call 没有结果行（被跳过/取消/未执行）或 assistant 整体
  aborted/error 时不交付。不另写工具失败启发式。
- 不修改 tool result 内容；不新增 `hookMessage` 行，也不把 reminder 写进 assistant 文本。P1 的两处
  append（`after_tool_batch` 与 `_judge_outputs`）已改为 `injection_row` 生成的 `role:"custom"`
  隐藏行，`ttsr-injection` 已在 `MESSAGE_CUSTOM_TYPES` 注册，`project_for_api` 转成 `developer`。

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
- 在 `#handleAgentStart` 清空 `#lastTtsrNotification`；transcript 重建/切换会话由
  `resetTranscript()` 清空 `chatContainer`，tail 检查随即失败，旧指针只在下一次
  `ttsr_triggered` 时被覆盖。
- 不写 `#messages`、不写 `sessionManager`、不写 DB、不参与 `/copy`/rewind/outline；`resume` 后不出现。
  `ctx.present` 是唯一挂载点（`interactive-mode.ts:2125-2134`），与 `StatusNotice` 不同，不进
  `#lastStatus` 的合并线。
- 组件从 `@oh-my-pi/pi-tui/chat/ttsr-notification` 引入；`role: "omp.notice.ttsr"`、native/legacy 两套
  presentation、`MAX_COLLAPSED_RULES=4`、`addRules` 去重都由组件负责，壳不改布局/文案/键位。
- 如果某条规则内容为空且 description 也为空，组件只画 header；这是组件既有行为，客户端不补占位。

### D7. 配置：只加有消费者的开关

在 `hermes_cli/config_defaults.py` 增加 `ttsr` 块（behavior config，不进 `.env`；根 AGENTS 的
“`.env` 只放 secrets”规则）：

| key | 默认 | 消费者 |
|---|---|---|
| `ttsr.enabled` | `true` | coordinator 的总开关；false 时不发现、不匹配、不发事件、不注入 |
| `ttsr.builtin_rules` | `true` | false 时跳过 source 位于 `install_root()` 下的 shipped corpus（按 source/root 判定，不按 `rule["scope"]`） |
| `ttsr.disabled_rules` | `[]` | 按 `name` 排除规则（大小写敏感，与 omp 一致） |

`ttsr.judge`（`auto|on|off`，默认 `auto`）由 ADR-11 D9 同批加入，消费者是 coordinator 的
`judge_enabled()`；它由 ADR-11 拥有，不在本 ADR 的改动清单里。

P1 不加 `ttsr.interrupt_mode`（P1 只支持 `never`）；P3 由 **D14** 增加它，以及 repeat 的
`ttsr.repeat_mode`/`ttsr.repeat_gap`（消费者见 D4）。`ttsr.context_mode` 也不加（P3 固定 `discard`，D14）。
这些在 P2/P3 有真实消费者时再加，避免“没人读的开关”。

AST 后端也不新增配置：`ast-grep` 是否存在由 PM/PATH 探测，缺失即把 AST-only 规则列为 unsupported；
`ttsr.ast_enabled` / `ttsr.ast_path` 没有独立消费者，安装状态和 PATH 就是唯一事实。

### D8. 分阶段与明确不承诺的项

| 阶段 | 内容 | 完成判据 |
|---|---|---|
| P1 | D1–D7：local viewer、regex 工具规则、`ttsr.triggered`、组件、custom `ttsr-injection` reminder、`once` 持久化 | 随附 26 条规则里 21 条 regex 规则可命中；通知与 reminder 同时出现；resume 不重画通知；其它客户端无变化 |
| P2 | `astCondition`（ast-grep CLI + 进程桥，见 D4）、`question` judge（由 `docs/adr/ADR-11-judgment.md` 的 `judge` 路线与 `judgment` 原生后端提供；不迁移 `auxiliary.goal_judge`）、prose/thinking 被动 follow-up | AST：CLI 可用时 5 条 AST-only shipped 规则可命中，CLI 缺失时仍显式 unsupported；regex 与 AST 任一命中只发一次通知；未支持的规则仍有 warn |
| P3 | D9–D14：`always/prose-only/tool-only` 的文本/思考流式中断、最终工具参数执行前中断、`discard`、`ttsr-interrupt.md` custom reminder、同 turn 重试、`message.rewind`、repeat `after-gap`/`repeat_gap` | 文本/思考命中后请求被被动中止，下一请求带 custom `ttsr-injection` reminder；工具命中时无持久行/无副作用；客户端 draft 被丢弃；resume 无 ghost；`after-gap` 在完成回合数达到 gap 后重触发、`once` 默认不变；其它客户端逐字不变。`keep`/prose 被动不在 P3 |

P3 的 stream rewind 会改变会话尾部（丢弃 partial assistant、重新请求），与 Hermes 现有的“用户中断”
不是同一语义（`agent/interrupt_control.py` 的 stop reason/lease/placeholder 是用户中断专用）。P3 的详细
决定见 D9–D14：被动 abort、assistant 行 append 之前的丢弃、custom `ttsr-injection` reminder、同 turn
重试、`message.rewind`，以及 repeat 的 `after-gap`/`repeat_gap`（D4/D14）；`keep` 与 prose 被动
follow-up 留给后续变更。

### D9. P3 的规则门控：`interruptMode` 按 omp 语义生效，但只中断可中断的源

- 支持 `always` / `prose-only` / `tool-only` / `never`；规则自身字段优先，缺失时取全局
  `ttsr.interrupt_mode`（D14，默认 `always`，与 omp 一致）。非法值或非字符串 → unsupported（reason
  指向合法值）；不静默降级成 `never`。
- 源映射：文本 delta → `text`；reasoning delta → `thinking`；工具调用最终参数 → `tool`。
  `prose-only` 只中断 `text`/`thinking`，`tool-only` 只中断 `tool`，`always` 两者都中断，`never` 不中断。
- scope 仍然逐条生效：`text` 命中要求 `allow_text`，`thinking` 要求 `allow_thinking`（默认 scope 不含
  thinking），`tool` 沿用 P1 的 scope/globs/别名。`question` 规则没有中断路径（ADR-11 D9 的判定只在
  消息完成后），即使写了 `interruptMode` 也不进入 P3 的中断匹配。
- 当前 shipped corpus 的 26 条都显式 `never`，P3 不改变它们的行为；只有用户新增的
  `always`/`prose-only`/`tool-only` 或省略字段（取全局默认）的规则进入中断路径。
- P2 的 `prose` 被动 follow-up 仍未实现：P3 中 `text`/`thinking` 命中但未中断的规则不交付；`tool` 命中且
  未中断的规则继续走 P1 的批次后 reminder。不要为了“有消费者”把 prose 被动路径塞进 P3。
- 规则仍按 `name` 折叠、`disabled_rules` 排除、`builtin_rules` 过滤；有效规则集合与 P1 相同，只多了
  interrupt mode 的判定。

### D10. 匹配：流式文本/思考 + 最终工具参数；丢弃前不落库

**流式文本/思考**：

- 新增 `TtsrCoordinator.check_stream_delta(source, delta, final=False)`；调用点在 provider 适配器的原始
  delta 汇聚处：Chat Completions 的 `_emit_text`/`_emit_reasoning`
  （`agent/chat_completion_helpers.py:2822,2833`）和工具轮的问前文本路由 `_route_suppressed_text`
  （`:2841-2847`），Anthropic/Bedrock/Codex 的同类 emit 点；不要只挂在 `_fire_stream_delta`（它收到的是
  已 scrub 的显示文本，且 `_route_suppressed_text` 绕过它）。
- 每个 assistant response 维护 `text`/`thinking` 两条缓冲，响应开始（`_reset_stream_delivery_tracking`）
  与 turn 开始清空；被中止的 response 的缓冲不能带到重试（否则重试的 delta 会与旧文本拼接）。
- 增量扫描逐字移植 omp 的 `StreamBuffer`/`ConditionTraits`（`export/ttsr.ts`）：行内条件从上一行边界重扫；
  可跨行条件在缓冲 ≤4 KB 或较上次全扫增长 ≥25% 时全扫；`final=True` 强制全扫。Python `re` 已在 P1 编译，
  不引入 JS regex 语义。
- 跨行条件可能在 delta 之间被节流；response 结束时（组装 assistant 行之前）对整段 text/reasoning 做一次
  `final=True` 检查，保证“块结束仍命中”不会漏。
- 同一 response 只发一次中止：第一次命中把规则放入 `_pending_rewind`（按 name 去重），后续 delta 继续
  累积规则但不再发第二次 abort。`ttsr.triggered` 在命中时发一次；按 `(response, rule)` 去重。
- 摘录：命中位置前后各 60 字符、总长 ≤120、空白折叠；用 `json.dumps(..., ensure_ascii=False)` 加引号，
  再按 omp `escapeHarnessTags` 把 `<` 在 harness 标签名前转成 `&lt;`。摘录只进 interrupt 模板，不进事件。

**最终工具参数**：

- P1 的 `match_tool_start` 从 `combined_tool_start_callback` 移到 `run_tool_round` 的
  `stage_tool_call_message`/`append_message`/`_flush_messages_to_session_db` 之前
  （`agent/turn_tool_round.py:110-141`）；对每个 call 复用 P1 的 `tool_digests`、scope、globs、regex、AST。
- 任一 call 命中中断规则：不 stage、不 append、不持久化、不执行任何工具（包括同批的其他 call），
  整个 assistant tool-call response 丢弃。未中断的 tool 命中仍进 P1 的 `_stash`，在批次后交付。
- 这样工具侧没有“已落库 assistant/tool 行要删”的问题，也不需要 `SessionDB.rewind_to_message`；
  P3 的 discard 是事务性的（omp 先持久化 aborted partial，再在 replay 时过滤；Hermes 更简单）。
- AST 只在最终参数上跑（P1 D4 不变）；文本/思考流不跑 AST。
- 非流式 provider：没有 delta 可扫，response 返回后、append 之前对 text/reasoning/tool digest 做一次
  `final=True` 匹配；命中则丢弃该 response 并重试。功能等价，只是不省中止前的输出 token。

### D11. 中止：被动 stream abort，不走用户 interrupt

- 新增 `TtsrCoordinator.request_rewind(...)`：在锁内写入 `_pending_rewind`（规则、摘录），设置
  `agent._ttsr_rewind_requested = True`，并调用现有 `_ic_abort_active_request`（`interrupt_control.py`）
  关闭在途请求的 socket；不调用 `InterruptControlMixin.interrupt()`——后者会设置
  `_interrupt_message`/`_turn_user_intervened`、打印 "Interrupted"、给工具线程发信号，并把 turn 当成用户停止。
  omp 的对应实现在 `ttsr-coordinator.ts:690-760`：`agent.abort(reason)` → 50 ms 后校验 token/generation/
  abortPending → `replaceMessages(slice(0, targetAssistantIndex))`（`contextMode=discard`）→
  追加隐藏 `customType:"ttsr-injection"` → `scheduleAgentContinue`；P3 用 Hermes 的 retry 标志与
  `begin_iteration` 表达同一顺序。
- 所有流循环的中止判断从 `agent._interrupt_requested` 改为
  `agent._stream_abort_requested()`（`_interrupt_requested or _ttsr_rewind_requested`）：
  `chat_completion_helpers.py:3093`、Anthropic/Bedrock 的 `on_interrupt_check`、stream monitor
  (`chat_completion_stream_monitor.py:222`)、post-worker 的 `:3954-3957`。只替换判断，不改各 provider 的
  收尾/错误语义。
- 响应与中止交叉：`perform_api_call` 在 response 返回后检查 `_ttsr_rewind_requested`（与 redirect 的
  `_pending_redirect` 检查同点，`turn_api_call.py:150-170`）；置真则丢弃 response，不 append、不持久化。
- `handle_api_interrupt` 增加 TTSR 分支：清 `_ttsr_rewind_requested`/`_interrupt_requested`，置
  `_retry.restart_with_ttsr_messages = True`，返回 `break`。不 append partial 行、不写 placeholder、
  不调用 `_persist_session`（丢弃路径无新增内容）。
- `TurnRetryState` 增加 `restart_with_ttsr_messages`；`apply_retry_restarts` 在 redirect 分支之后处理：
  退回 `api_call_count` 与 iteration budget，`restart_count` 与 `max_retries` 同 redirect 一样设上限，
  超出则 `_turn_exit_reason="ttsr_restart_limit_exceeded"` 并结束 turn。上限路径先把 pending reminder
  作为 custom `ttsr-injection` 行交付再结束，保证已发的 `ttsr.triggered` 不对应“从未注入”的假通知。
- `begin_iteration` 在 `_drain_pending_redirect` 之后、interrupt 退出之前应用 pending rewind：
  渲染并 append custom `ttsr-injection` reminder（D12），清 pending，`_persist_session`，然后下一次请求。
- 命中中断时清空本 response 的 `_stash`/`_judged_stash`/`_pending_text_outputs`（对齐 omp 的
  `#perToolInjections.clear()`），避免被丢弃 response 的被动规则在下一批误交付；
  `_batch_claimed`/`memory_claimed` 保留（omp 的 match-time in-memory claim 同样不回收）。
- **优先级**：用户 stop/hard interrupt > redirect > TTSR。用户 stop 发生时丢弃 pending rewind（不写
  repeat 表，内存 claim 保留，符合 P1 的“未交付不落表”）；redirect 已有 pending 文本时，TTSR 命中只记内存
  claim，不再发第二个 abort。
- **不可见性**：TTSR abort 不产生 `interrupted` 的 turn result、不设 `_turn_user_intervened`、不打印
  `_vprint`、不发 `status.update`/`notification.show`；网关只看到“同一 turn 又发了一次请求”。
  `message.complete` 由最终成功的 response 产生，不带 aborted stop reason。
- **上限/重试语义**：`once`（默认）下每个规则在 `memory_claimed` 里被永久 claim，一次会话（进程内）
  不会因同一规则反复中断；`after-gap` 下按 D4 的完成回合数解除，但仍受 `restart_count`/`max_retries`
  约束。不要用新的 `ttsr.max_rewinds` 开关——没有独立消费者。

### D12. 回滚与注入：discard 是默认且 P3 唯一支持的语义

- P3 只实现 `contextMode: discard`（omp 默认）：被中止的 assistant response 不 append、不 flush、不写
  session DB；live `messages` 尾部保持请求前的 user/tool 行。具体丢弃点：response intake 的最终检查
  （D10）在任何 assistant 行落盘前执行；正常文本路径在 `turn_final_response.py:402` 的
  `append_message(messages, final_msg)` 与 `:407-414` 的 flush 之前，nudge 路径的 `:353` 同样在此之前；
  tool-call response 在 `turn_tool_round.py:110-141` 的 stage/append/flush 之前（D10）。重试请求 = 原前缀
  + 一条 custom `ttsr-injection` reminder（投影后 developer），provider 的前缀缓存字节不变，只有后缀
  增长。omp 侧对应 `ttsr-coordinator.ts:713-741` 的 `replaceMessages` + hidden custom message；Hermes
  因为丢弃发生在落行前，不需要 omp 的 replay 过滤（`session-context.ts:753-789`）。
- 不实现 `keep`：Hermes 的 assistant 行在下一请求前必须可 replay，tool-call abort 的 keep 会留下没有
  tool result 的 assistant `tool_calls`（严格 provider 会 400），而 text keep 需要一个“把 live draft
  落成 interrupted 行并重开 assistant block”的客户端协议，属于独立变更。配置出现 `ttsr.context_mode`
  或 `keep` 时 warn 并按 discard 执行（D14），不假装支持。
- 新增 `agent/ttsr_prompts.py::render_interrupt`，逐字移植 `ttsr-interrupt.md`：
  `<system-interrupt reason="rule_violation" rule="{name}" path="{path}">`，opening 行
  “Output interrupted: violated user-defined rule.”，可选 `Matched: {excerpt}` 与“untrusted quoted data”
  声明，然后 `MUST comply:` 和规则正文。路径按 `_display_path` 相对 cwd；不送绝对路径。
- 多条规则空行连接，顺序按 delta/tool-call 命中顺序再按规则发现顺序；一条规则只出现一次。
- 交付行复用 D5 的 carrier 与 Hermes 既有的 omp custom 角色/角色转换（ADR-10 D1 的 `role:"custom"` 行），
  不新增行型、角色或转换器：
  `{"role":"custom","custom_type":"ttsr-injection","content":<rendered>,"display":false,
  "display_only":false,"details":{"rules":[<names>],"matched":{<name>:<excerpt>}},
  "attribution":"agent","legacy_role":"user"}`，用 `append_message` 追加。`matched` 只出现在
  interrupt 行（`details` 形状以 ADR-10 D1 的 `ttsr-injection` 行为准），工具提醒只有 `rules`。
  `project_for_api`
  （`agent/custom_messages.py:193-225`）在送 provider 前把它转成 `developer` 并剥掉 display/custom
  字段，`display:false` 使 resume/`/copy` 看不到，provider 侧仍收到。omp 的 interrupt 注入同样是
  hidden `customType:"ttsr-injection"` + `attribution:"agent"`（`ttsr-coordinator.ts:730-741`）。
- 交付时机：discard 之后、下一请求之前；此时尾部是 user/tool，投影后的 `developer` 行合法。多规则一次
  只写一行。
- repeat 状态：命中时写 `memory_claimed` 与 `last_injected_turn`（会话进程内，挂在 agent 上，D4）；
  只有 reminder 成功 append 后才 `session_db.mark_ttsr_injected`（`session_ttsr_injections`，只写
  规则名）。用户 stop/limit 丢弃时只保留内存状态，不写表；进程重启/resume 后可再次命中（与 P1
  验收 10 同口径）。`after-gap` 下内存状态不是永久抑制，达到 gap 后允许重新命中。
- 崩溃窗口：命中后、append 前进程退出，没有 durable 行也没有表记录；重启后规则可再次触发。这是可接受的
  “通知是即时状态、不是持久事实”语义，不引入事务表。

### D13. 客户端：一个 live-only 的 `message.rewind` 事件

- 新增 canonical local-viewer 事件 `message.rewind {reason: "ttsr", message_id?: str, segment?: int}`，
  在 abort 后、重试前发一次。它只表示“刚才流出的 assistant draft 作废”，不是 transcript 行，不参与
  resume、`/copy`、rewind、outline。
- 网关发送点：`gateway/local_viewer_events.publish_message_rewind(turn)`；先让 `TurnSegments.rewind()`
  关闭/丢弃当前 text block 并递增 `segment`/清零 `offset`（`tui_gateway/stream_segments.py`），再
  `turn._publish_execution("message.rewind", …)`。重试的 delta 因此带新 `segment`，不会与作废段按
  offset 去重。事件仍只对 `client:"omp-ui"` 的 turn 发（D1），其它客户端从不收到。
- omp-ui：`gateway-session.ts` 的 `#notificationHandlers` 增加 `message.rewind` →
  `#onMessageRewind`：`this.#draft = undefined`，`this.#emit({type:"message_rewind"})`；
  `event-controller.ts` 的 `#handleMessageRewind` 移除 `ctx.streamingComponent`（若有）、清
  `ctx.streamingMessage`、`requestRender()`。下一次 delta 的 `#ensureDraft` 会重新 `#openDraft` 并
  emit `message_start`，`#handleMessageStart` 也会清掉遗留的 streaming block。不写 `#messages`、
  `sessionManager`、DB。
- `agent-session-events.ts` 增加 shell 自有事件 `{type:"message_rewind"}`（与 `ttsr_triggered`、
  `notice` 同类，不是 `AgentEvent` 子集）。
- `ttsr.triggered` 的组件/合并逻辑不变：通知在命中时出现；`message.rewind` 只处理 draft，不画“规则已注入”
  的第二个 UI。若规则最终因用户 stop 没交付，通知已经出现——这是 omp 的既有语义（通知是即时状态），
  验收 10 明确未交付不写 repeat 表。
- 契约：`tui_gateway/contracts/events.py` 增加 `MessageRewindPayload` + `event("message.rewind", …)`；
  同一 PR 必须有真实 emitter（`gateway/local_viewer_events`），否则 orphan 门失败；重跑
  `scripts/gen_gateway_contracts.py --check`。

### D14. 配置与降级：只加有消费者的开关

- 新增 `ttsr.interrupt_mode`：`always|prose-only|tool-only|never`，默认 `always`（omp 的全局默认）。
  消费者是 `select_rules`：规则自身缺 `interruptMode` 时取它。非法值 → 配置 warning + 回退 `always`；
  规则自身的非法值 → unsupported（不静默降级）。
- 新增 `ttsr.repeat_mode`：`once|after-gap`，默认 `once`；`ttsr.repeat_gap`：整数，默认 10。消费者是
  coordinator 的 repeat 判定（D4）。非法 `repeat_mode` → warning + 回退 `once`；非正整数 `repeat_gap`
  → warning + 回退 10。`repeat_gap` 在 `once` 下无消费者但不报错（omp 两键独立，切到 `after-gap` 时生效）。
- **不加** `ttsr.context_mode`：P3 固定 discard。配置里出现该键（或值 `keep`）时 warn
  “ttsr.context_mode=keep is not implemented; using discard”，并按 discard 执行；不新增没有实现的开关。
- P3 与 P1 共用 `ttsr.enabled`、`builtin_rules`、`disabled_rules`、`ttsr.judge`；不新增 per-rule 开关。
- 降级：没有 coordinator（CLI/Desktop/消息平台/ACP/子代理）或 `client != "omp-ui"` 时，P3 的所有路径
  都是 no-op；`ttsr.enabled=false` 时不匹配、不 abort、不注入。`interruptMode` 无效的规则仍进入
  unsupported 汇总（`status.update`+`transcript.notice`），P3 不再把 `always/prose-only/tool-only`
  列为 unsupported。

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

# server -> client (P3, local viewer only, omp-ui session)
event message.rewind {
  reason: "ttsr"
  message_id?: string   # the draft's message identity, when known
  segment?: int         # the retry's fresh segment counter
}

# 客户端内部（P3）
AgentSessionEvent += {type: "message_rewind"}

# 会话行（不是 wire；ADR-10 D1 的 custom 行，由 project_for_api 投影为 developer）
{role: "custom", custom_type: "ttsr-injection", content: "<rendered>",
 display: false, display_only: false, attribution: "agent", legacy_role: "user",
 details: {rules: [<names>], matched?: {<name>: <excerpt>}}}
```

不新增方法、不新增 server request、不新增行型；新增一个只丢 live draft 的事件。`session.resume`/
`session.history` 的 payload 逐字不变。

## 实现落点

| 层 | 位置 | 改动 |
|---|---|---|
| 运行时 | `agent/ttsr_runtime.py`（新） | 规则发现/过滤、scope 解析、别名、glob、digest、Python regex、AST 调度/预算/缓存、repeat 状态读取/写入、reminder 渲染；注入行统一为 `custom_type:"ttsr-injection"`；P3 的 `completed_turns`/`last_injected_turn` 挂在 agent 上并在 `turn_end` 递增（D4） |
| 角色转换 | `agent/custom_messages.py` | `MESSAGE_CUSTOM_TYPES` 加入 `ttsr-injection`；`project_for_api` 已能把 agent 归因的 message-bearing custom 行转成 `developer`，不需新转换器 |
| AST 桥 | `agent/ttsr_ast.py`（新） | PM/PATH 解析 `ast-grep`、扩展名→语言、stdin 进程调用、`--files-with-matches` 判定、超时/错误降级 |
| 模板 | `agent/ttsr_prompts.py`（新） | `ttsr-tool-reminder` 逐字文本与 builtin/user-defined 两个开场；不复制到客户端；P3 增加 `ttsr-interrupt` 的 `render_interrupt` |
| 流式匹配 | `agent/stream_delivery.py`、`agent/chat_completion_helpers.py`、`agent/ttsr_stream_hooks.py`、`agent/codex_runtime.py` | P3：`check_stream_delta` 挂在 `_emit_text`/`_emit_reasoning`/`_route_suppressed_text`、Bedrock 回调与 codex `_fire_delta`；每 response 的 text/thinking 缓冲与 `final` 检查。`ttsr_stream_hooks` 只做两行转发，避免撑大 `chat_completion_helpers` 的 ratchet |
| 被动中止 | `agent/interrupt_control.py`、`agent/turn_api_call.py`、`agent/turn_retry_state.py` | P3：`_ttsr_rewind_requested` + `_pending_rewind`、`_stream_abort_requested()`、`handle_api_interrupt` 的 TTSR 分支、`restart_with_ttsr_messages` |
| 重试应用 | `agent/turn_iteration_prep.py`、`agent/conversation_loop.py` | P3：`apply_retry_restarts` 的 TTSR 分支；`begin_iteration` 在重发前 append custom `ttsr-injection` 行并持久化 |
| 工具前置 | `agent/turn_tool_round.py` | P3：在 `stage_tool_call_message`/`append_message`/`_flush_messages_to_session_db` 之前匹配最终工具参数；命中中断则不 stage/不执行；P1 的 gateway 匹配点随之移除 |
| 网关 | `gateway/run_turn_progress.py` | P1：`combined_tool_start_callback` 调 `coordinator.match_tool_start`；P3 改由 `run_tool_round` 匹配，本回调只发 `tool.start`；`_finalize_tool_batch` 继续调 `coordinator.after_tool_batch` |
| 客户端事件 | `gateway/local_viewer_events.py`、`tui_gateway/stream_segments.py` | P3：`publish_message_rewind` + `TurnSegments.rewind()`（新 segment、offset 清零），只对 omp-ui 发 |
| 契约 | `tui_gateway/contracts/events.py` | P1 `TtsrRulePayload`/`TtsrTriggeredPayload`；P3 `MessageRewindPayload` + `event("message.rewind", …)` |
| 配置 | `hermes_cli/config_defaults_ttsr.py` | P1 `enabled/builtin_rules/disabled_rules`；P3 `interrupt_mode`（默认 `always`）、`repeat_mode`（默认 `once`）、`repeat_gap`（默认 10） |
| 身份 | `tui_gateway/contracts/canonical_sessions.py`、`gateway/session_policy.py` | `CanonicalCreateParams.client: Literal["omp-ui"]`；`CREATE_FIELDS` 接受；`LocalSessionPolicy.client` 从冻结 request 读 |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.ts` | `session.create` 带 `client:"omp-ui"`；resume 不重发 |
| 网关 | `gateway/run_turn_runner.py` | `_run_sync_scoped` 在 `local_viewer_turn(self)` **且** `active_policy().client == "omp-ui"` **且** `ttsr.enabled` 时挂 `agent.ttsr_coordinator`（`install_turn_coordinator`）；`finally` 里 `restore_coordinator` 清掉 |
| 行 | `agent/tool_executor.py::_finalize_tool_batch` | 在 steer 之后调用 `agent.ttsr_coordinator.after_tool_batch(messages, result_ids=...)`（steer 前收集的结果行 id，避免切片偏移）；无 coordinator 时零变化 |
| 状态 | `hermes_state_common.py` / `hermes_state_ttsr.py`（新） | `session_ttsr_injections` 建表、`mark_injected`、`injected_names`、会话删除级联（只存规则名，D4） |
| 客户端 | `omp-ui/src/session/agent-session-events.ts` | P1 增加 `ttsr_triggered`；P3 增加 `message_rewind` 词汇 |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.ts` | P1 增加 `ttsr.triggered` handler 与 `#onTtsrTriggered`；P3 增加 `message.rewind` handler 与 `#onMessageRewind`（清 `#draft`） |
| 客户端 | `omp-ui/src/modes/controllers/event-controller.ts` | P1 增加 `#lastTtsrNotification`、`#handleTtsrTriggered`；P3 增加 `#handleMessageRewind`（移除 live streaming block） |
| 生成 | `scripts/gen_gateway_contracts.py`、`scripts/gen_panel_inventory.py` | 重新生成并 `--check` |
| 文档 | `docs/omp-ui-panels-gateway-plan.md` | 实现后 §2.3.2.1 TTSR 行 `无 producer` → `已接（local）`；实现前保持 |
| 文档 | `docs/appendix-omp-ui-realtime-rpc.md` | A.11 的 `path` 改为 `description`；A.15 的 TTSR 行在 P1 后移出“无 producer” |
| 测试 | `tests/agent/test_ttsr_runtime.py`（新） | scope/别名/digest/glob/regex/AST 调度/repeat/custom carrier |
| 测试 | `tests/tui_gateway/test_custom_messages.py` | `ttsr-injection` 在 `project_for_api` 中投影为 `developer`，display/custom 字段被剥掉 |
| 测试 | `tests/agent/test_ttsr_ast.py`（新） | 二进制解析、扩展名→语言、match/no-match/超时/异常退出、命令不含 shell |
| 测试 | `tests/tui_gateway/contracts/test_generated.py` | `ttsr.triggered` 有真实 emitter，orphan 门通过；P3 另断言 `message.rewind` 有真实 emitter |
| 测试 | `tests/agent/test_ttsr_stream.py`（新，P3） | delta 匹配/缓冲重置/final 检查/摘录转义、passive abort 不设用户中断字段、restart 分支、discard 不落库、上限 |
| 测试 | `tests/gateway/test_ttsr_gateway.py` | P3：`message.rewind` 只对 omp-ui 发；工具中断在 `run_tool_round` staging 前生效；`ttsr.triggered` 与 reminder 对账 |

## 验收

1. **命中与通知**：`patch` 一个 `.ts` 文件写入 `const x: any = 1`，客户端恰好收到一条
   `ttsr.triggered`，`rules[0].name == "ts-no-any"`，payload 没有 `path`/`condition`/`scope` 键；
   transcript 出现 `TtsrNotificationComponent`，文案/颜色/图标由组件决定。
2. **合并**：同一回合连续两次 tool call 各命中不同规则，且第一个通知块仍是 live tail 时，两个块合并成
   一块（`addRules` 去重）；若中间插入了工具卡导致上一块不是 tail，则出现两块。expanded 状态跟随
   `toolOutputExpanded`。
3. **注入位置**：下一回合发给 provider 的 `messages` 在对应 tool result 之后恰好有一条
   `role="developer"` 的 `ttsr-injection` reminder（会话行是 `role:"custom"`）；tool result 的
   `content` 与前缀逐字不变；reminder 文本等于 `ttsr-tool-reminder.md` 的渲染结果。
4. **隐藏**：`session.resume` 的 `display_messages` 里该行 `display:false`，客户端不渲染；`#messages`/
   `/copy`/rewind 看不到它；模型请求仍包含它（`project_for_api` 已转成 `developer` 并剥掉 display/custom
   字段）。
5. **缓存**：命中前后，provider 请求的前缀（system + 命中前的全部消息）逐字节相同；只有 tool result 之后
   多了 reminder。不得出现对已有 user/assistant 消息的重写。
6. **工具别名与 digest**：`write_file` 到 `.go` 且内容含 `runtime.SetFinalizer` 命中 `go-add-cleanup`；
   `patch` replace mode 只看 `new_string`，`old_string` 里的同一模式不命中；V4A 的 `+` 行按文件分别匹配，
   `-` 行不命中；scope/globs 也逐文件生效（`a.ts` + `b.md` 的同一 V4A patch 中，
   `tool:edit(*.ts)` 不能借 `a.ts` 放行 `b.md`）。工具自身返回 error（例如 patch 失败）时，只要该 call
   实际执行并产生结果，reminder 仍交付（与 omp 的 `afterToolCall` 无条件消费一致）。
7. **scope/glob**：`tool:edit(*.ts)` 不匹配 `.md`；`(**/*.ts)` 匹配嵌套路径；`{ts,tsx}` 展开；
   没有可解析文件路径的工具不跑条件；无 `scope` 的规则按 omp 默认（text+tool）在工具参数上参与；
   `globs`/`paths` 与 scope 同时生效。
8. **拒绝语义**：P3 之前，`interruptMode: always`、缺失 `interruptMode` 的 regex/AST 规则被拒绝；
   P3 落地后，它们按 D9/D14 激活（缺字段取全局 `ttsr.interrupt_mode`），不再进入 unsupported。
   两种阶段下，非法 `interruptMode`、以及 ast-grep CLI 缺失时只有 `astCondition` 的规则，仍在会话开始时
   **汇总成一条** `status.update{kind:"ttsr_unsupported"}` + `transcript.notice` 提示（不是
   `notification.show`），不产生 `ttsr.triggered`、不注入、不中断（不伪装）。
9. **开关**：`ttsr.enabled=false` 时无事件/无注入；`disabled_rules` 按 name 排除；
   `builtin_rules=false` 时 source 位于 `install_root()` 下的 shipped corpus 不加载；project/profile
   规则仍可用（cwd=仓库根也要正确判定）。
10. **repeat `once`**：默认 `ttsr.repeat_mode=once` 时，同一会话第二次命中同一规则不再发通知/注入；
    重启/resume 后仍抑制（`session_ttsr_injections` 有记录）；未交付的命中不写表，resume 后可再次命中。
11. **客户端门**：只有 `session.create{client:"omp-ui"}` 的会话安装 coordinator。`local_viewer_turn` 为真
    但没有该标识的路径——one-shot、`chat -q`、ACP、TUI attach、detached 消息会话——都不匹配、不发
    `ttsr.triggered`、不注入 reminder；`session.resume` 读冻结策略，旧会话没有 `client` 就不启用。
    同一回合 Desktop/消息平台的上下文没有 reminder。子代理（`delegate_task`）也不执行 P1。
12. **契约门**：`scripts/gen_gateway_contracts.py --check` 与 `scripts/gen_panel_inventory.py --check`
    通过；`tests/tui_gateway/contracts/test_generated.py` 的 orphan 门通过；`ttsr.triggered` 在
    `BackendGatewayEventMap` 里，且
    `runtime.describe.transcript_blocks.ttsr_notification.events` 含它
    （`gateway/panel_inventory.py::TRANSCRIPT_BLOCKS`）。
13. **AST 条件**：`patch`/`write_file` 写 `.ts` 且内容匹配 `astCondition: 'const $A: any = $B'` 时，
    客户端收到一条 `ttsr.triggered`，批次后收到一条 reminder；同一规则同时有 `condition` 与
    `astCondition` 时任一命中只发一次。无扩展名或 `.md` 不跑 AST；CLI 缺失时 AST-only 规则进入
    unsupported 汇总并带 `hermes pm install ast-grep`；坏 pattern、语言不支持或超时按不命中处理，
    不产生通知/注入。
14. **文本流式中断**：规则 `interruptMode: always` + 匹配 `text` 的 `condition`，模型开始流出违规文本后：
    客户端先收到 `ttsr.triggered`，随即收到 `message.rewind`；请求在下一个 delta 前结束，**没有**
    assistant 行 / `display_kind` 行落到 `messages` 或 session DB；下一请求只多一条 custom
    `ttsr-injection` reminder（投影为 `developer`，`ttsr-interrupt.md` 渲染，含摘录），retry 的最终回复
    正常流式出现。`message.complete` 不出现 aborted/interrupted 状态，CLI/网关不打印“Interrupted”。
15. **思考中断与 scope**：`scope: thinking` + `prose-only` 的规则在 reasoning delta 命中时中止；
    默认 scope（不含 thinking）的同一规则不命中；`scope: text` 的规则不被 reasoning 命中。中止后
    reasoning/text 缓冲都重置，retry 的 delta 不与旧文本拼接。
16. **跨行 final**：条件在 delta 之间因节流未全扫（例如跨两行的 `(?s)` 模式），在 response 结束的
    `final=True` 检查命中；仍然只发一次通知/一次 reminder，且没有 append 被丢弃的 response。
17. **工具执行前中断**：`patch`/`write_file` 的最终参数命中 `interruptMode: always` 规则时，
    `run_tool_round` 在 staging/persist 之前丢弃整个 tool-call response：工具没有执行（无副作用）、
    没有 `tool.start`/`tool.complete`、没有 assistant/tool 行落库、没有 `_flush_messages_to_session_db`；
    客户端收到 `message.rewind` + 一条 reminder；下一请求重新生成。同批的其他 call 也不执行。
    同批未中断的 `never` 规则仍按 P1 在工具执行后交付。
18. **非流式**：关闭 streaming 后，response 完整返回再匹配；命中时 response 被丢弃、不 append、
    注入一条 reminder 并重试；不产生 `message.rewind`（没有 live draft），客户端不出现两个回复。
19. **优先级与用户 stop**：TTSR abort 在途时 `/stop` 优先：turn 结束、pending rewind 被丢弃、不写
    `session_ttsr_injections`、不进入 TTSR retry；用户 redirect 与 TTSR 同时到达时只执行 redirect，
    TTSR 只保留内存 claim。用户 stop 后同一规则在新进程/resume 后可再次命中。
20. **客户端**：只有 `client:"omp-ui"` 的 turn 收到 `message.rewind`；omp-ui 丢弃 draft 并清 live
    streaming block，retry 文本只出现一次；`session.resume` 的 history/`/copy`/outline 没有作废的
    partial 或 reminder；Desktop/TUI/消息平台从不收到该事件，行为逐字不变。
21. **上限与契约/配置**：同一 response 多个规则命中只发一次 abort；`once`（默认）下每规则每会话最多
    一次（`after-gap` 按 D4 的 gap 重触发）；超过
    `max_retries` 时 `turn_exit_reason="ttsr_restart_limit_exceeded"`，pending reminder 先作为 custom
    `ttsr-injection` 行交付再结束，不无限重试。`ttsr.interrupt_mode` 缺省 `always`、非法值回退并 warn；
    `ttsr.context_mode=keep` warn 后按 discard；`scripts/gen_gateway_contracts.py --check`、
    `gen_panel_inventory.py --check` 与 orphan 门通过。
22. **repeat `after-gap`**：`ttsr.repeat_mode=after-gap`、`ttsr.repeat_gap=2` 的规则在命中并交付后，
    同一 turn 内不再重复交付；完成 1 个 turn 后仍不触发；完成第 2 个 turn 后再次命中时重新发
    `ttsr.triggered` 并注入。`once` 默认行为不变；未交付的命中不写表；重启/resume 后 turn 计数从 0
    计，需再完成 `repeat_gap` 个 turn 才可再次触发；durable 表只记规则名，不写计数。非法
    `repeat_mode` 回退 `once`，非正整数 `repeat_gap` 回退 10，均 warn。

## 风险

| 风险 | 缓解 |
|---|---|
| 病态正则阻塞 agent 线程 | P1 只在工具执行前扫描一次；P3 的流式匹配按 D10 节流：行内条件只从上一行边界重扫，跨行条件仅在缓冲 ≤4 KB 或较上次全扫增长 ≥25% 时全扫，`final=True` 才强制全扫；缓冲按 response 重置，且规则是本地可信配置、shipped corpus 已由 `hermes rules check` 编译验证 |
| Python `re` 与 JS regex 语义差异 | `compileRuleCondition` 只翻译开头 inline flag（`rule.ts:363-370`），Python 原生支持；编译失败跳过并 warn，不把“无命中”伪装成“通过” |
| ast-grep 进程慢/卡死阻塞工具执行 | 单 pattern 2s、每次 tool call AST 总预算 3s；超时按不命中；同一 `(pattern, lang, sha256)` 结果在 turn 内缓存；只对 digest 跑 |
| ast-grep 语言/grammar 差异 | 退出码 8、空输出、未知语言一律按不命中并 debug；CLI 缺失时 AST-only 规则显式 unsupported，不把“未运行”当成“通过” |
| 进程桥注入/路径泄露 | 参数列表不经 shell；源码只走 stdin；不写临时文件、不扫 workspace；二进制只从 PM 激活 PATH 或 PATH 解析 |
| ast-grep 版本行为漂移 | 只用 `run --stdin --files-with-matches --strictness smart` 的稳定子集；判定只看退出码与 stdout 是否非空 |
| JSON 转义导致漏匹配 | 只用每文件源码 digest（`write_file.content`、`patch.new_string`、V4A 的 `+` 行），不扫整个 args |
| 工具名别名漏掉 | 别名表是唯一入口；单测覆盖 `edit/write/read` 与 Hermes 的三个真实工具名；未知工具只做路径 gate |
| custom 行改动模型行为 | 这是 TTSR 注入的本意；只对 local viewer 开启；内容来自本地规则；`display:false` 不进显示投影 |
| 严格 provider 不认中途 developer | `project_for_api` 只在 provider 副本上转 role；OpenAI-compatible 由 `_normalize_developer_roles` 降成 system，Anthropic/其它适配器按 ADR-10 D11 折叠（Anthropic 默认 user）；会话行始终是 custom |
| 压缩把注入状态/行清掉 | 注入状态放独立表，不依赖 custom 行；custom 行只是内容载体，压缩后即使丢了，也只影响下一回合的模型提醒，不影响 UI 通知 |
| 通知与注入不一致（事件已发、reminder 未交付） | 与 omp 一致：事件在匹配时发；只有 call 未执行（跳过/取消）或 assistant 整体 aborted/error 时不交付，单个工具 error 仍交付；通知是即时状态，不是持久事实；验收 10 明确未交付不写 repeat 表 |
| 事件 payload 撑大帧 | 只送 `name`/`description`/`content`；规则正文来自本地文件；超过 frame 上限时按契约错误暴露，不静默截断成假正文 |
| 客户端把通知写进 `#messages` | D6 明确只 `ctx.present`，不碰 `#messages`；验收 4 断言 resume/`/copy` 无它 |
| 规则正文被当成用户输入 | reminder 用 omp 的 `<system-reminder reason="rule_violation" …>` 包装并声明 not prompt injection；与 omp 同文 |
| 规则文件在 turn 中途变化 | 每次 turn 重新 `discover_rules`；turn 内使用同一份快照，避免同一批次命中两套规则 |
| 中止与后续 delta 交叉（abort 已发，stream 仍送来一个 chunk） | 中止标志只置一次；`_pending_rewind` 按 name 去重；`perform_api_call` 在 response 返回后复查标志并丢弃 response；重试缓冲在 `begin_iteration` 重置 |
| provider 覆盖不齐，某 transport 没接 delta hook | 所有 provider 的文本/思考最终都经过 `_emit_text`/`_emit_reasoning` 或 response 级 `final` 检查；即使流式 hook 漏接，非流式/最终检查仍能拦截，只是不省中止前的 token |
| 中断规则把正常输出打成无限重试 | `memory_claimed` 立即 claim；`once` 永久抑制，`after-gap` 按完成回合数解除（D4）；`session_ttsr_injections` 只记已交付；`restart_count > max_retries` 强制结束 |
| 客户端残留被丢弃的 draft | `message.rewind` 清 `#draft` 并移除 live block；下一次 delta 通过 `message.start` 重建；resume 不包含它；验收 20 |
| 前缀缓存/输出费用 | discard 在 append 前，重试请求前缀字节不变；被中止的 output token 仍计费，这是 stream rewind 的固有代价；不承诺退还 |
| `ttsr.interrupt_mode` 默认 `always` 让缺字段规则从被动变主动 | 只影响没写字段的 regex/AST 规则；shipped corpus 全部显式 `never`；`hermes rules check` 继续对缺字段 warning；可在配置里设 `never` 恢复旧行为 |
| 工具批次只执行了一部分 | 中断判定在 staging/execute 之前，任一 call 命中就不执行整批；没有“先跑了一半”的窗口 |
| 用户 stop 与 TTSR abort 抢占 | 优先级固定为用户 stop > redirect > TTSR；`clear_interrupt`/turn finalizer 清 `_ttsr_rewind_requested`；验收 19 |

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
| 直接把 `role:"system"`/`"developer"` 写进会话行 | 会话行是 custom；provider role 由 `project_for_api` 决定，否则 resume/display 与 provider 语义混在一起 |
| 复用用户 `interrupt` 实现 TTSR 中断 | 用户中断的 stop reason、lease、placeholder、UI 抑制语义都不同；P3 用独立的 `_ttsr_rewind_requested` + `message.rewind`，不伪装成用户停止 |
| 丢 durable 行来实现 discard | 工具/assistant 行一旦 flush 就有 `_DB_PERSISTED_MARKER`，删行需要 `SessionDB.rewind_to_message` 的 turn 粒度软删和 replay 过滤；P3 在 append 之前丢弃，不需要删 |
| P3 同时实现 `contextMode: keep` | keep 要把 partial assistant 落成 interrupted 行并重开 assistant block；tool-call keep 还会留下没有 tool result 的 `tool_calls`（严格 provider 400）。P3 固定 discard，keep 另立变更 |
| 先把整段回复缓冲到结束再匹配 | 用户会失去流式输出；P3 在 delta 上增量匹配，仍保留流式；只有非流式 provider 退回最终检查 |
| 在网关 `combined_tool_start_callback` 阻止工具 | 该回调在持久化/执行之后（`turn_tool_round.py:113-141`）；无法阻止副作用，必须前移到 core |
| 新增 `ttsr.max_rewinds` 开关 | `memory_claimed`/`completed_turns` 的 repeat 判定 + `restart_count/max_retries` 已足够；没有独立消费者 |
| 为 TTSR 新增 transcript 行或 assistant 工具 | 通知/回滚都是运行时状态；新行会进 resume/`/copy`，新工具会让每次请求多带一个没有其它消费者的工具 |
| 把通知做成 `display_kind="ttsr"` 的持久行 | omp 的通知不是 entry；持久行会出现在 resume/`/copy`/rewind，且无法重建合并边界 |
| 新增独立 `ttsr` 模型工具 | `rules` toolset 已存在；模型工具每次 API 请求都携带，且匹配是服务端运行时，不是模型动作 |
| 用 `review.summary`/`agent.print` 冒充通知 | 语义不同；会污染 review/状态通道 |
| 把 AST/judge/interrupt 与通知移植夹在同一发布 | 每个都需要独立后端/上下文改写；AST 用 CLI 桥、judge 走 ADR-11、interrupt 是 P3，分开才能验收“通知对应真实注入” |
| 引入 `ast_grep_py` / tree-sitter Python 绑定 | 当前环境没有绑定；新增编译依赖与 ABI 风险，而 PM 已有可选的 `ast-grep` 二进制。CLI 进程桥保持默认安装零新增 Python 依赖 |
| 常驻 ast-grep daemon | 匹配频率是每次 tool call 一次、规则量小；daemon 增加生命周期、权限与端口面。按需进程 + 超时/预算足够 |

## 遗留

- P2 的 AST 语言覆盖面：ast-grep 内置 grammar 决定可用语言；不支持的扩展名或语法按不命中处理
  （debug），不假装通过。需要更多语言时升级 PM 的 `ast-grep` 包或用户安装带对应 grammar 的 CLI。
- P2 的 judge：已由 ADR-11 D9 收口（独立 `judge` 路线 + `ttsr.judge`），不迁移
  `auxiliary.goal_judge`；本 ADR 不再保留待决项。
- P3 的 `contextMode: keep`：需要把 partial assistant 落成 interrupted 行、重开 assistant block，并处理
  tool-call keep 的 tool-result 配对；本 ADR 固定 discard，keep 另立变更。
- P3 的 prose 被动 follow-up（`interruptMode: never` 的 text/thinking 延迟注入）仍属 P2；当前未实现，
  P3 不交付未中断的 text/thinking 命中。
- P3 的 after-gap 恢复语义：resume 后规则 age 从 0 重新计（对齐 omp `restoreInjected()`），不补算
  进程存活期间的墙钟；若未来要跨重启累计，需要把 turn 计数持久化并与会话真实 turn 数对齐，另立变更。
- P3 的 provider 覆盖：实现时逐个 transport 检查文本/思考 delta 汇聚点；没有 delta 的路径由 response
  级 final 检查兜底，验收只要求行为等价，不要求所有 provider 都省 token。当前已接
  chat_completions、Anthropic、Bedrock、codex app-server 的文本/思考 delta；**codex app-server 的
  工具调用由其内部执行**，不经过 `run_tool_round`，因此工具参数中断在该 transport 上不生效（文本/
  思考仍生效）；若要覆盖，需要 codex 的 `item/*` 工具参数事件接入 P3 的最终参数匹配，另立变更。
