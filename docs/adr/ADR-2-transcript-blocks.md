# ADR-2：Transcript 区块目录的数据来源与网关增量

**状态**：已决定（2026-11-01）。实现未开始。分阶段：`session.history` 参数随 §9 **P3**，`message.*`
字段与 `message.adviser` 随 **P4**，客户端映射随 **P5**。落地前 `docs/omp-ui-panels-gateway-plan.md`
§2.3.2.1 的 `partial` / `待接` 保持原样：不伪造数据，不改渲染分支（§0.1）。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§2.3.2、§2.3.2.1、§4.7、§9；ADR-1（Pause）。

## 问题

§2.3.2 的 27 个 transcript 区块，数据只有两个来源：`session.create`/`session.resume` 快照
（`display_projection:true` → `display_messages`，由 `tui_gateway/session_history._history_to_messages`
投影）与实时事件（附录 A）。逐项核对后：

- **19 个区块不需要网关改动**：数据已在 wire 上，缺的是 `omp-ui/src/modes/rpc/gateway-session.ts`
  的投影（`rowToAgentMessage` 只读 `role`/`text`/`timestamp`/`model`/`tool_call_id`/`name`/`is_error`）
  和 `handleNotification` 的事件映射；或明确无 producer（D9）。
- **8 个区块需要新增 producer/数据**：
  - 助手消息、Cache miss 标记 → `tool.start.step_usage` / `message.complete.step_usage`（每步）；
  - Served model 标记 → `message.complete.served_model` / `requested_model`；
  - Advisor → canonical 路径的 `message.adviser`；
  - 工具执行、图片/SVG/figure → `session.history.include_tool_content`（resume 后的工具正文与图片）；
  - Transcript 选择器 → `session.history.outline`（rewind 入口本身是客户端接线，不是网关数据）；
  - Todo 提醒 → `todo.reminder` + 持久化的 reminder 行（D11）。

所以本 ADR 只决定网关增量；其余一律落在客户端映射层。

## 决策

### D1. 每条 assistant 消息带 `step_usage`（每一步）；`usage` 只补填累计 cache 桶

`contracts/common.py::Usage` 已经声明 `cache_read`、`cache_write`、`cost_usd`、`cache_hit_pct`，
缺的是 producer：`tui_gateway/server.py::_get_usage` / `gateway/session_usage.py::tui_usage` 只填
`cache_hit_pct`，`cache_read`/`cache_write` 一直是 `None`；这些视图都返回**会话累计**。
Ink/Desktop 状态行与 `/usage` 读的正是这个累计值（`gateway/session_usage.py::usage_frame` 的契约注释）。

omp 的 usage 行与 cache 标记是**每条 assistant 消息**粒度：`event-controller.ts:304-317` 直接读
`message.usage`，`TurnUsageTally`（pi-tui `overlays/usage-row.ts:56-88`）负责把一轮内多次请求求和，
并只在 `stopReason !== "toolUse"` 的最后一条上显示 "this turn"；`display.showTokenUsage`
（`event-controller.ts:318-325`）同样每条一行。因此 wire 上不能发预先求和的轮总量。

| 字段 | 粒度 / 语义 | 数据源 | 下发点 |
|---|---|---|---|
| `usage`（现有 + 补填） | 会话累计：input/output/reasoning/total/calls/context/cache_hit_pct；本 ADR 补填已声明的 `cache_read`/`cache_write`（仍为累计，additive） | `agent.session_*`，经 `tui_gateway/server.py::_get_usage` / `gateway/session_usage.py::usage_stats` | 现有 `message.complete` 路径 |
| `step_usage`（新增） | **一步** = 一次 provider response（一条 assistant message / 一次 API 请求）：input/output/reasoning/cache_read/cache_write/total/cost_usd | `agent._last_turn_usage`（`agent/turn_usage.py:261`，MoA fold 后）+ 新增 `agent._last_turn_cost_usd`（同函数里的 `_cost_delta`） | `tool.start`（触发工具的这次响应）与 `message.complete`（最后一次响应） |

决定：

- `usage` 保持会话累计语义，并补填 `cache_read`/`cache_write`（`_get_usage` 与 `tui_usage` 从
  `session_cache_read_tokens`/`session_cache_write_tokens` 取值；只增字段值，不改任何既有字段）。
- 新增 `step_usage: Usage | null`，分别挂在 `tool.start` 与 `message.complete` 上；同一响应触发的
  多个 `tool.start` 携带同一值，客户端只在关闭 assistant 段时取一次。客户端把它写进该条
  assistant message 的 `usage`，轮总量交给现有 `TurnUsageTally` 汇总，wire 不发轮总量。
- 没有 provider usage 的响应保持 `null`，不填 0；`message.complete` 的 `step_usage` 只代表最后
  一次响应，不是整轮。
- **tool-only step**：`gateway-session.ts:1092-1097` 的 `#closeSegmentForTool` 在没有 text/thinking
  时直接 return，不发 `message_end`。客户端收到 `tool.start.step_usage` 后必须把这一步直接计入
  `TurnUsageTally`（合成一条 usage-only assistant 记录即可），不渲染空块；`#mergeLastAssistant`
  也不得覆盖已计入的 step usage。
- **managed / remote**：`gateway/run_turn_progress.py:78 publish_worker_tool_event` 对 worker 帧做
  严格字段校验（`set(frame) != fields` 即拒绝），且 managed worker 的结果 allowlist 不含
  `_last_turn_usage`。v1 中这类回合的 `step_usage` 为 `null`，验收不得假设每回合都有值；扩展 worker
  帧与 allowlist 另立变更。
- 不需要回合计数器差分，也不需要新增 instrumentation：`_last_turn_usage` 已在每次响应后写入，
  本 ADR 只补一个 per-step cost 暂存。

### D2. `served_model` / `requested_model` 新增到 `message.complete`

数据已存在：`agent/served_model.py::result_model_fields` 写进 turn result（`agent/turn_finalizer.py:778`），
canonical 的 `completion_payload` 从 `captured['result']` 读取。`gateway/run_turn_persistence.py:221`
只是把同一个值拼进可选的 runtime footer 文本，不是数据源，也不是持久化到行。决定新增两个可选字段，
分别取 turn result 的 `served_model` / `requested_model`（缺失为 `null`）。omp 的 `ServedModelTracker`
读 `AssistantMessage.upstreamModel`（字段已存在），映射层直接赋值。

### D3. canonical 本地 viewer 补发 `message.adviser`

`plugin_message_callback` 目前只在 sidecar 安装（`tui_gateway/agent_callbacks.py:189,199`），
canonical turn 的 `gateway/run_turn_runner.py:489 _wire_turn_agent_callbacks` 没有装，所以 advisor
只有 resume 快照、没有实时卡片。

决定：在 `_wire_turn_agent_callbacks` 装一个过滤 `display_kind=="adviser"` 的 callback，复用
`tui_gateway/agent_callbacks.py::_adviser_payload`，通过 `_publish_execution("message.adviser", ...)`
发出（契约 `tui_gateway/contracts/events.py:245` 已存在）。同一个 dispatcher 也按 `display_kind`
分发 `todo_reminder`（D11），sidecar 与 canonical 复用同一个 payload 构造 helper。

快照侧不改：`_history_to_messages` 已保留 `display_kind`（`session_history.py:364-368`），客户端按
`display_kind=="adviser"` 画 Advisor 块，不新增行类型。

### D4. 压缩摘要不做 transcript 块

`agent/compaction_display.py::project_compaction_message_for_display` 对纯 handoff 返回 `None`（
display 投影必须移除模型侧 handoff），`display_kind="hidden"` 的行也被 `_history_to_messages` 跳过。
`CompactionSummaryMessageComponent` 需要的 `compactionSummary` entry 在 Hermes 没有 producer。

决定：保持现状——压缩只留 `agent.print` / `status.update` 通知行，不合成摘要块。理由：模型侧
handoff 文本不应进用户 transcript，且 `display_messages` 同时服务 Desktop，新增行型是跨客户端行为变化。

### D5. Transcript 选择器 / rewind：新增客户端入口，不新增方法

现状：`selector-controller.ts` 没有 `getTree`/`getEntry`/`navigateTree`；`#transcriptEntries()`（
`selector-controller.ts:494`）用内存 `sessionManager.getMessages()` 拼线性链，只服务 CopySelector；
`RewindSelectorComponent` 未被 shell 引用，`transcript-browser.ts` / `transcript-outline.ts` 也不在
`omp-ui/src`。即 rewind **没有客户端入口**，不是“本机逻辑换成网关”。

决定：

- 客户端新增 rewind / outline 入口，数据走 `session.history`（`entries` 供选择器，`messages` 供工具卡）；
- 截断走既有 `session.mutate`：`operation:"rewind"`、`payload:{target_message_id}`、`expected_revision`、
  `expected_generation`（`gateway/session_mutations.py:331`、`hermes_state_mutations.py:145`）；
- 不新增 `session.rewind` / `session.truncate` 方法。

`target_message_id` 是 durable row id；`session.history` 的 `entries[].entry_id` 与快照行的 `row_id`
同源，客户端直接使用。

### D6. `session.history` 增加 `include_tool_content`，替换悬空的 `include_tools`

事实：`_history_to_messages` **始终**投影 tool 行（`session_history.py:330-343`），`args`/`labels`/
`context` 都在；`entries` 只收带 `row_id` 的行（`tui_gateway/history_paging.py:19-35`），tool 行随
`messages` 切片同行。唯一缺的是结果 `content`：`session_history.py:339` 只对
`write_file`/`patch`/`skill_manage` 放行。因此 §4.7 的 `include_tools` 若实现为“是否包含 tool 行”
是 no-op。

但 DB 里的 `content` 是**模型侧**文本：高风险工具（`web_extract`/`web_search`/`browser_*`/`mcp_*`）
被 `_maybe_wrap_untrusted` 包上 `<untrusted_tool_result …>` 分隔符，大结果还会在包装前追加
`_UPSTREAM_ELISION_NOTICE`（`agent/tool_dispatch_helpers.py:448-598`）；而 live 的
`tool.complete.result` 是包装前的原始结果。直接回 `m['content']` 会让 resume 后的 web/browser/MCP
工具卡多出安全包装文本，与 live 不一致。

因此 `include_tool_content` 必须做 display-safe 投影：按工具名剥离恰好一层 wrapper 与已知 elision
notice（多模态按每个 text part 处理），非高风险工具原样返回。实现放在
`agent/tool_dispatch_helpers.py::display_tool_result_content(name, content)`，`session_history` 调用它，
不复制正则。另注：非 vision 模型的工具结果在写入时已被 `vision_message_prep` 换成文本摘要，resume
只能还原存下来的形态，图片是 best-effort；写入时 `_neutralize_delimiters` 会把正文里的字面
`<untrusted_tool_result` 中和为 `untrusted-tool-result`，display-safe 投影保留该中和形式（不恢复原字面量）。

决定（该字段尚未实现，改名零成本）：

```text
session.history {session_id, include_tool_content?: "none" | "text" | "full" = "none",
                 include_outline?: bool = false, ...}
  -> {..., outline?: [{row_id, role, summary}]}
```

- `none`（默认，resume 快照同）：tool 行只有 `context`/`args`/`labels`，`content` 仍按现有白名单；
- `text`：追加 display-safe 文本块，图片部分丢弃；
- `full`：display-safe 文本 + 图片块，单行受字节上限（复用现有 data-url 上限常量），超限回落
  `[image]`/路径占位；
- `outline`：仅 `include_outline=true` 时按本页 entries 生成，避免每次分页都做摘要。

display-only，不进入模型上下文，不影响 prompt cache。

### D7. Reaction：网关不改

实时 `message.reaction`（`tools/react_to_message_tool.py`）与 `reaction{kind}`（
`gateway/local_viewer_events.py`）已存在；快照行也**已经**带 reactions——它们存在
`display_metadata.reactions`（`hermes_state_messages.py:519-546`），`get_messages_as_conversation`
解出 `display_metadata`，`_history_to_messages` 原样转发（`session_history.py:367`）。

决定：不新增 `reactions` 字段；客户端读 `display_metadata.reactions` 并在 live 事件上更新。

### D8. 客户端映射清单（无网关改动）

- `tool.start.step_usage` / `message.complete.step_usage` → 当前 assistant 段的 `message.usage`；
  轮总量交给 pi-tui `TurnUsageTally`，客户端不预先求和；缺失保持 draft 的零值，不填累计值；
- tool-only step（无 text/thinking 可关闭）不产生空块，但 `step_usage` 仍要计入 `TurnUsageTally`；
  `message.complete` 落到 `#mergeLastAssistant` 时同样先计数，不能只覆盖上一条的 usage；
- `rowToAgentMessage` 补读 `display_kind`、`display_metadata`、`reasoning`/`reasoning_content`/
  `reasoning_details`、`args`、`labels`、`content`、`context`、`row_id`；工具行还要把
  `display_metadata.tool_result_metadata.inline_diff` 映射成 `details.diff`，`is_error` 读顶层投影；
- `thinking.delta` 是 spinner 文本（`gateway/local_viewer_events.py:44`），不是模型思考，必须过滤；
  思考只取 `reasoning.delta` 与快照/`message.complete.reasoning`；
- `message.block_end.content`、`message.complete.rendered`/`response_transformed` 作为漏 delta 的兜底；
- `notification.show`（文本在 `text`，不是 `message`）、`command.output`、`agent.print`、
  `session.reclaimed`、`session.resume_progress` 映射为 notice；
- `todo.reminder` → `todo_reminder` 事件 → `TodoReminderComponent`；快照行
  `display_kind="todo_reminder"` 用 `display_metadata.todo_reminder` 还原，不画成 user 气泡；
- 工具名别名：`read_file`→`read`、`todo_list`→`todo`；
- `/skill-*` 已由 `command-controller.ts:160` 处理 `type:"skill"` 并提交回合；剩余工作是把
  `command.dispatch` 的 `display` 与快照 `display_kind="skill_invocation"` 画成 invocation 块，
  而不是一次性状态行；
- `error_surface`、`approval.cancelled` 映射到错误帧 / 取消提示。

### D9. 无 producer：保持空

Recap、TTSR、独立 late-diagnostics 块、被剥离工具调用、collab prompt、reply write、
自定义/hook 消息、Background TAN、压缩摘要块。Hermes 没有等价数据（或已随 §0.3 裁剪），不造块、
不改布局。Todo 提醒已由 D11 决定实现，不在此列。

### D10. 工具卡片补全：`is_error` + `inline_diff` 持久化

事实：

- canonical `tool.complete` 经 `_tool_lifecycle_payload` **已带 labels**（`gateway/run_turn_progress.py:51-54`），
  不需要新增；客户端 `toolResultDetails` 会放进 `details.labels`。
- `TranscriptMessage` 没有 `is_error`，`_history_to_messages` 不投影它，`_commit_tool_result`
  （`agent/tool_executor.py:1073`）也没写进 `display_metadata`，所以 resume 的卡片全部显示成功。
- `inline_diff` 只在 live `complete_fields`（canonical，`gateway/local_viewer_events.py:206`）里算；
  sidecar 的 `_prepare_tool_result_metadata` 会写进 `display_metadata.tool_result_metadata`。
  canonical 的 `_wire_turn_agent_callbacks` 没装该 callback，所以 resume 的 edit/patch 卡没有 diff。
- 通用卡不读 `summary`/`duration_s`：`ToolExecutionComponent` 用本地 elapsed 与
  `details.diff`/`phases`/`images`（`tool-execution.ts:621-632,67-79`）。因此不为通用卡补
  `summary`/`duration_s`，也不要持久化 duration。

决定：

1. 统一存放形状：`display_metadata.tool_result_metadata.{is_error, inline_diff}`
   （与 sidecar 的 `_prepare_tool_result_metadata` 对齐）。
   - `_commit_tool_result` 在 append 前写 `is_error`（它已有该 verdict），不改动 callback 的
     `inline_diff`；
   - canonical `tool_result_metadata_callback`（`_wire_turn_agent_callbacks` 安装，仅 local viewer turn）
     只算 `inline_diff`，从 `_local_edit_snapshots` 只读 peek，不 pop（`complete_fields` 还要用）。
2. `_history_to_messages` 从 `display_metadata.tool_result_metadata.is_error` 投影出 tool 行的
   `is_error`，并补 `TranscriptMessage.is_error`；**只存 display_metadata 一份**，顶层字段是投影。
3. 客户端 D8 把 `display_metadata.tool_result_metadata.inline_diff` 映射成 `details.diff`
   （不是 `details.inline_diff`）；`is_error` 客户端已有读取。`inline_diff`/`is_error` 的别名属于
   客户端映射层，不进网关。
4. running 阶段的 labels 不缺 wire：`tool.start.labels` 已在；只是客户端 `#onToolStart` 没带进
   `tool_execution_start`（D8 的处理项，不是网关）。
5. running/partial 状态不落库：transcript 只存已完成的 tool 行；已完成/失败由 `is_error` 表达
   （按第 2 条持久化）。

### D11. Todo 提醒忠实移植（stop gate + `todo.reminder`）

**决定：做。** 复用 `agent/turn_stop_gates.py::apply_stop_gates` 的“追加 nudge + 同回合 continue”，
不新开 gateway 回合/admission。

**策略（对齐 omp `todo-tracker.ts::checkCompletion`）**

- 配置：`agent.todo_reminders`（bool，默认 true）、`agent.todo_reminders_max`（int，默认 3），
  加在 `hermes_cli/config_defaults.py::DEFAULT_CONFIG["agent"]`（`max_verify_nudges`/`verify_on_stop`
  同段），由 `load_config_readonly()` 读取；不新增 env var。默认 true 的前提是**仅交互 surface**；
  若以后放开到消息平台，应像 `verify_on_stop` 一样改默认 False。
- 状态：`agent._todo_reminder_attempts`、`agent._todo_reminder_awaiting_progress`。
- 触发（全部满足）：
  1. enabled、`attempts < max`、surface 是交互式（`session_is_messaging_surface()` 为 false）；
  2. `not getattr(agent, "is_subagent", False)`；`finite_turn_required()` 为 true 时跳过；
     cron/kanban（`HERMES_KANBAN_TASK`）跳过；
  3. `agent._todo_store.snapshot()["todos"]` 有 `pending`/`in_progress`；
  4. `not agent._todo_reminder_awaiting_progress`：上次提醒后至少发生过一次工具结果
     （对齐 `onToolResult` 的 `reminderAwaitingProgress = false`；**不是** TodoStore revision，
     也不是“必须有变更型工具”——后者是 mid-run nudge 的 `mutationsSinceLastTouch`）；
  5. 不在等用户回答：pending clarify/approval 或 closing-question 启发式（移植
     `todo-tracker.ts::isAwaitingUserAnswer`）；
  6. 没有 pending async wake（`tools.async_delegation.active_count() == 0`）。
- 动作：
  1. `_append_interim_answer(...)` 保留已停止的回答；
  2. append `{role:"user", content: reminder_text, display_kind:"todo_reminder",
     display_metadata:{"todo_reminder":{todos, attempt, max_attempts}}}`；
  3. `attempts += 1`，`_todo_reminder_awaiting_progress = True`；
  4. `final_msg["finish_reason"] = "todo_reminder"`，返回 `continue_turn=True`
     （fallback = final_response）。
- 事件：`todo.reminder {todos, attempt, max_attempts}`。行 append 后调 `notify_plugin_message(agent, row)`；
  sidecar 扩展 `tui_gateway/agent_callbacks.py::_announce_plugin_row`，canonical 在 D3 的
  `plugin_message_callback` 里做同一 `display_kind` 分发（`adviser` / `todo_reminder`），payload 构造
  复用一个 helper（从 `display_metadata.todo_reminder` 取）。不新增回调。
- 进展清零：`agent/tool_executor.py::_commit_tool_result` 在每次工具结果后把
  `_todo_reminder_awaiting_progress = False`（成功/失败都清，对齐 `onToolResult`）。
  若要移植 mid-run nudge，再单独用 `agent/tool_guardrails.py::MUTATING_TOOL_NAMES` 计数；本 ADR 不做。
- 快照：`display_kind`/`display_metadata` 由 `_history_to_messages` 原样转发；客户端按
  `display_metadata.todo_reminder` 还原。**不要**用 `display_kind="hidden"`，否则 resume 丢失。
- 重置：todos 为空或没有 active 项时，`attempts = 0`、`awaiting_progress = False`；预算跨 prompt 保留。

**不采用**：gateway `session_auto_continue` 式回合后重提；TodoStore revision 当进展；仅以 mutating
tools 判断 stop reminder 的进展（上游那是 mid-run nudge 的语义）。

**边界**

- managed/remote：stop gate 在 worker 内照常继续；`todo.reminder` 事件依赖
  `plugin_message_callback`，managed worker 没有该回调 → v1 事件为 `null`；不为它扩帧。
- 与其它 gate：放在 kanban 之后。verify/pre_verify/kanban 已 continue 时本轮不再叠加，下个 stop 再评估。
- 消息平台误开 → 额外消息；默认只在交互 surface 启用。

**风险**

| 风险 | 缓解 |
|---|---|
| 每次提醒多一次 completion | max 默认 3；只在有未完成 todo 且上次提醒后有过工具结果时触发 |
| 误判“在等用户回答” | pending clarify/approval + closing-question 启发式；预算兜底 |
| 子代理各自续跑 | `is_subagent`/`_parent_session_id` 跳过 |
| 有限回合被续跑 | `finite_turn_required()`、cron/kanban 跳过 |
| 与 goal/loop 续跑叠加 | goal followup 在回合后，todo gate 在回合内；最多一轮 |
| reminder 行污染用户 transcript | `display_kind` + 客户端映射成 notice；模型侧文本不直接渲染 |
| 消息平台多发消息 | 默认只在交互 surface 启用 |

**验收**

1. 有未完成 todo 且模型停止 → 同一 turn 内继续一次；事件与 reminder 行各一次。
2. 达到 max 后不再继续；上次提醒后没有工具结果时不重复提醒。
3. todos 全完成/清空 → 不提醒且计数清零。
4. 最后一段是问句 / pending clarify/approval / async 任务在跑 → 不提醒。
5. 子代理、`-z`/`chat -q`/cron、kanban worker → 不触发。
6. resume 后 reminder 显示为 reminder 块（`display_metadata.todo_reminder` 还原），不是 user 气泡。
7. 上一请求的消息前缀不被改写（只 append；对比 messages 前缀）。
8. managed/remote：继续发生但事件为 `null`，不报错。
9. config 关闭时行为与现状完全一致。

## 协议

```text
tool.start (additive)
  step_usage?:    Usage | null   # 触发该工具调用的那次 provider response

message.complete (additive)
  step_usage?:    Usage | null   # 最后一次 provider response，不是整轮
  served_model?:  string | null
  requested_model?: string | null

message.adviser                  # payload 不变（events.MessageAdviserPayload）
  # canonical 本地 viewer 也发；sidecar 行为不变

todo.reminder (new)
  todos:          TodoItem[]      # {id?, content, status, phase?}；只含 pending/in_progress
  attempt:        int
  max_attempts:   int

session.history (E)
  include_tool_content?: "none" | "text" | "full" = "none"
  include_outline?:      bool = false
  -> outline?: [{row_id, role, summary}]
```

`step_usage` 复用 `Usage` 形状，不新增类型；轮总量由客户端 `TurnUsageTally` 汇总，wire 从不发
预先求和的值；`usage` 的结构与累计语义不变，本 ADR 只补填两个既有字段的值。

## 实现落点

| 层 | 位置 | 改动 |
|---|---|---|
| 契约 | `tui_gateway/contracts/events.py::ToolStartPayload` | `step_usage` |
| 契约 | `tui_gateway/contracts/events.py::MessageCompletePayload` | `step_usage`、`served_model`、`requested_model` |
| 契约 | `tui_gateway/contracts/sessions.py::SessionHistoryParams/Result` | `include_tool_content`、`include_outline`、`outline` |
| 用量 | `agent/turn_usage.py::record_response_usage` | 在 `_last_turn_usage` 旁暂存 `_last_turn_cost_usd = _cost_delta` |
| 用量 | `gateway/session_usage.py` | `tui_usage` 补填累计 `cache_read`/`cache_write`；`publish_usage(record=True)` 把 `step_usage` 写入 captured |
| 用量 | `tui_gateway/server.py::_get_usage` | 补填累计 `cache_read`/`cache_write`（与 `session_cache_*_tokens` 一致） |
| 用量 | `gateway/run_turn_progress.py::combined_tool_start_callback` | `tool.start` 附 `step_usage`（当前 agent 的 `_last_turn_usage` + `_last_turn_cost_usd`） |
| 用量 | `tui_gateway/tool_progress.py::_on_tool_start` | 同上（TUI 路径，`session["agent"]`） |
| 用量 | `gateway/run_turn_progress.py::publish_worker_tool_event` | v1 不改：worker 帧字段严格校验，managed/remote 回合 `step_usage=null`；扩展需同时加帧字段、worker allowlist 与 `read` 白名单 |
| 用量 | `tui_gateway/prompt_turn.py::_complete_turn_payload` | 填 `step_usage`、`served_model`、`requested_model` |
| 用量 | `gateway/session_settlement_recovery.py::completion_payload` | 从 captured 转出三个字段 |
| advisor | `gateway/run_turn_runner.py::_wire_turn_agent_callbacks` | 装过滤后的 `plugin_message_callback` → `message.adviser` |
| todo | `agent/todo_reminder.py`（新） | enabled/max、active todos、surface/subagent/finite 守卫、closing-question、async wake |
| todo | `agent/turn_stop_gates.py` | kanban gate 后加 todo gate：append reminder 行 + `continue_turn=True` |
| todo | `tui_gateway/contracts/events.py` | `todo.reminder {todos, attempt, max_attempts}` |
| todo | `tui_gateway/agent_callbacks.py::_announce_plugin_row` | `display_kind=="todo_reminder"` 发 `todo.reminder`（复用 `notify_plugin_message`） |
| todo | `agent/tool_executor.py::_commit_tool_result` | 每次工具结果后 `_todo_reminder_awaiting_progress = False` |
| todo | `hermes_cli/config_defaults.py` | `DEFAULT_CONFIG["agent"].todo_reminders` / `todo_reminders_max` |
| history | `tui_gateway/session_history.py:339` | 白名单换成 `include_tool_content` 三态投影；调用 `display_tool_result_content` |
| history | `agent/tool_dispatch_helpers.py` | 新增 `display_tool_result_content(name, content)`：剥一层 untrusted wrapper + elision notice，逐 text part 处理 |
| 卡片 | `gateway/run_turn_runner.py::_wire_turn_agent_callbacks` | local viewer 安装 canonical `tool_result_metadata_callback`，只返回 `{"tool_result_metadata": {"inline_diff": ...}}` |
| 卡片 | `gateway/local_viewer_events.py` | 新增只读 peek（不 pop `_local_edit_snapshots`），供 callback 与 `complete_fields` 共用 |
| 卡片 | `agent/tool_executor.py::_commit_tool_result` | local viewer 的 tool 行在 append 前把 `is_error` 并入 `display_metadata.tool_result_metadata` |
| 卡片 | `tui_gateway/contracts/common.py::TranscriptMessage` | 加 `is_error` |
| 卡片 | `tui_gateway/session_history.py` | tool 行从 `display_metadata.tool_result_metadata.is_error` 投影 `is_error` |
| history | `tui_gateway/history_paging.py` / `methods_session_history.py` | 透传参数、生成 `outline` |
| 生成器 | `scripts/gen_gateway_contracts.py` | 重跑并提交 `gateway-contract.generated.ts` / OpenRPC |

## 未采纳

- **把 `usage` 改成每回合值**：会破坏 Ink/Desktop 状态行与 `/usage`（它们读同一帧的累计值）。
- **在 wire 上发预先求和的轮总量（`turn_usage`）**：预工具消息 `usage` 为 0、最后一条被放大，
  cache marker 会把一轮内的 warm 读求和后漏报冷步；客户端 `TurnUsageTally` 已经负责求和。
- **用 `notification.show` 冒充 advisor 卡片**：丢失 severity/label 与卡片语义，且 toast 会被 TTL 清除。
- **`include_tools` 布尔值**：tool 行本来就始终投影，改了是 no-op，验收无法成立。
- **从模型侧 handoff 合成 compaction summary**：把内部交接文本写进用户 transcript，并改变 Desktop 的
  `display_messages` 行为。
- **给快照新增 `reactions` 字段**：`display_metadata.reactions` 已经存在，重复建模只会产生两个真相源。
- **新增 `session.rewind`**：`session.mutate` 已有 `rewind`，且带 revision/generation 栅栏。

## 风险

| 风险 | 缓解 |
|---|---|
| `step_usage` 与累计 `usage` 被混用 | 命名区分；单条消息只写 `step_usage`，缺失保持 `null`，客户端不把累计值塞进单条消息 |
| 多工具批次重复携带同一 `step_usage` | 客户端只在关闭 assistant 段时取一次；无 draft 的后续 `tool.start` 忽略 |
| provider 未返回 usage 的步 | 保持 `null`，不填 0；`TurnUsageTally` 自然跳过 |
| tool-only step 没有可关闭的 assistant 段 | 客户端把 `step_usage` 直接计入 `TurnUsageTally`，不渲染空块（D8） |
| managed / remote 回合没有 per-step usage | 字段为 `null`，客户端显示缺省，不回退到累计 `usage`；验收覆盖 |
| `message.adviser` 与快照重复 | resume 以快照替换 transcript；live 事件只追加，不合并历史 |
| `include_tool_content:"full"` 放大 payload | 默认 `none`；单行字节上限；超限回落占位；resume 快照不启用 |
| DB tool `content` 是模型侧包装文本 | `include_tool_content` 走 `display_tool_result_content`（按工具名剥 wrapper/notice）；验收覆盖 web/browser/MCP |
| metadata callback 与 `complete_fields` 争用 edit snapshot | peek 或缓存渲染结果；before-state 只消费一次 |
| `is_error` 双写 | 只存 `display_metadata.tool_result_metadata.is_error`；顶层 `is_error` 是只读投影 |
| `inline_diff` 字段名与卡片不一致 | 客户端映射成 `details.diff`（D8）；网关不改字段名 |
| `outline` 每次分页都算 | 默认关闭，仅选择器打开时请求 |
| rewind 的 row id 失效（已 rewind/compaction） | 沿用 `session.mutate` 的 `not_found`/`stale_generation`；客户端刷新 `session.history` |

## 验收

1. 每条 assistant 消息的 `usage` 来自该步 `step_usage`：预工具段 = 触发工具的响应，最后一条 =
   最后一次响应；`TurnUsageTally` 的汇总值等于整轮，且只在最后一条非 `toolUse` 消息上显示
   "this turn"；cache marker 用相邻消息的 `usage` 比较（`detectCacheInvalidation`），不得读累计值。
   `usage` 除新增的 `cache_read`/`cache_write` 外逐字段一致，两值等于 `session_cache_read_tokens`/
   `session_cache_write_tokens`（Ink 回归）。无 provider usage 的步为 `null`，不填 0。
2. tool-only step（模型只调工具、没有 text/thinking）不产生空块，但该步 `step_usage` 仍计入整轮
   汇总；走 `#mergeLastAssistant` 的完成路径也不丢这一步。
3. managed / remote 回合：`step_usage` 为 `null`，客户端不显示伪值、不报错，也不回退到累计
   `usage` 冒充单步。
4. fallback 回合 `served_model != requested_model`；正常回合两者一致或 `served_model` 缺省。
5. canonical 本地 viewer 收到且只收到一次 `message.adviser`；resume 后同一行按 `display_kind="adviser"`
   还原，不重复。
6. `session.history`：`none` 的响应字节不高于现状；`text`/`full` 返回 **display-safe** 投影——
   `web_extract`/`browser_*`/`mcp_*` 的文本与 live `tool.complete.result` 一致，不含
   `<untrusted_tool_result` 包装与 `[hermes note:`；非高风险工具逐字节一致；`full` 的图片超限回落
   占位、不报错；`include_outline=true` 时 `outline` 与 `entries` 对齐。
7. `/skill-*` 仍产生回合；invocation 块由客户端从 `display` + `display_kind` 渲染。
8. rewind：选择 `entry_id` → `session.mutate rewind` 截断 → 重新提交成功；过期
   `expected_generation` 被拒；失败后客户端重读 `session.history`。
9. `python scripts/gen_gateway_contracts.py --check`、`python scripts/gen_panel_inventory.py --check`、
   `tests/tui_gateway/panels/test_panel_coverage.py` 通过；面板覆盖门不变（无新方法）。
10. 工具卡：resume 的失败工具显示失败（`is_error=true`，来自
    `display_metadata.tool_result_metadata.is_error` 的投影）；edit/patch 卡 resume 后有
    `inline_diff` 并映射到 `details.diff`，与 live 一致；`summary`/`duration_s` 不作为通用卡要求。

## 对 §2.3.2.1 的修正（本 ADR 取代以下判断）

| 原判断 | 修正 |
|---|---|
| Skill 调用“`#gatewayPrompt` 只提交 `type:"send"`，`/skill-*` 不产生回合”（待接） | 该符号不存在；`command-controller.ts:160` 已处理 `send`/`skill`。真实缺口是 invocation 块未渲染 → **部分** |
| 跨区块第 3 条“`promptCustomMessage`/`sendCustomMessage` 无网关分支” | 符号已随 §0.3 删除；Background TAN 无本机实现，按无 producer 处理 |
| Reaction“快照行不带 reactions” | 错：在 `display_metadata.reactions`，投影已转发 → 纯客户端映射 |
| Transcript 选择器“本机 `getTree`/`navigateTree`，入口是 `RewindSelectorComponent`” | 当前无任何 rewind 入口；需新增入口 + `session.history` + `session.mutate rewind` |
| `include_tools` 扩展 | 改为 `include_tool_content` 三态；`include_tools` 是 no-op |
| Todo 提醒“无 producer” | D11 决定做：agent 侧 stop gate + `todo.reminder` + 持久化 reminder 行；managed/remote 事件为 `null` |
