# ADR-10：Transcript 区块先由 Hermes 网关生产（omp-ui 只做映射）

**状态**：已决定（2026-11-03）。**P1 已落地**（2026-11-03：D1 契约与投影、D2 canonical adviser、D7
`transcript.notice`、D9 `custom_messages`/`project_custom_messages` 双门、D10 角色枚举与座位表、D11
provider 转换与 OpenAI `developer` 降级；客户端 opt-in、单一投影与 `satisfies never` 已接）；**P2 已落地**
（2026-11-03：D4 `include_tool_content`/`include_outline`/entries 与 `session.resume` 参数、D5 tool 行
`is_error` 与 display-safe `content`、验收 11 的 `include_compaction` 投影、验收 18 的 render key 与
wire 工具名分离）；**P3 已落地**（2026-11-03：D3 `todo.reminder` stop gate 与 durable reminder 行
（custom/legacy 双形状 + `row_id`）、`message.complete`/`tool.start` 的 `step_usage` 与
`requested_model`/`served_model`、capability 中 `todo_reminder`/`cache_miss_marker`/
`served_model_marker` 转 ready；客户端删除对应 seat、assistant 消息改用 `step_usage`（tool-only
步的 usage 计入 TurnUsageTally）并把 `served_model` 写入 `upstreamModel`）；**P4 已落地**（2026-11-03：
D6 `transcript_blocks` 补齐 plan 目录中的 `adviser`/`skill_invocation`/`tool_execution`/`notice`，
ADR-3/5/6/9 的 `background_tan`/`recap`/`ttsr_notification`/`late_diagnostics` 按各自 ADR 报
`supported:false` + `needs`；omp-ui 删除 `GATEWAY_PENDING_TRANSCRIPT_BLOCKS`，
`connectGatewayOmpSession` 非阻塞读取 `runtime.describe.transcript_blocks` 并暴露
`transcriptBlock(id)`；非角色区块的 live 挂载点（`transcript.notice`/`message.adviser`/
`todo.reminder`）只挂载显式 `supported:true` 的区块，角色行照常按 D10 渲染；
`compaction_summary` 保持 `partial`，ADR-5 P1 已升为 `produced`）；**P5 已落地**（2026-11-03：wire 删除 `display_kind`/
`display_metadata`，`TranscriptMessage`/`HistoryEntry` 不再携带；自定义区块一律 `role:"custom"` +
`custom_type`/`details`，旧 marker 行由网关投影时归一；合成行改用 `synthetic_kind`（`auto_continue`/
`failed_turn`/`steer`）与 `live_steered`；compaction counters 移到 `details.compaction`；
`project_custom_messages` 保留为 no-op 参数，投影不再有 viewer 分支；omp-ui/Ink 只读
`custom_type`/`details`，Desktop rewind 改读 `synthetic_kind`；`custom_messages` 仍是 create-only
session 标志，Desktop/Ink 的原始行与模型上下文保持旧形状）。分阶段：**P1** `role:"custom"` + `custom_type`/`details` 契约、投影/模型转换、
`TranscriptRole` 角色全集与 `TRANSCRIPT_ROLES` 座位表 +
canonical `message.adviser` + `transcript.notice` 去重；**P2** `session.history` 的 `entries`/`include_tool_content`/`include_outline` 与工具行补全（legacy `display_metadata` 仅 P5 前兼容）；
**P3** `todo.reminder` stop gate、`message.complete` 的 `step_usage`/`served_model`；
**P4** 按 ADR-3/5/6/9 接入其余区块，并用同一份 capability 清单收口；**P5** 删除 `display_kind`/`display_metadata` 与 legacy 回投影。落地前
`docs/omp-ui-panels-gateway-plan.md` §2.3.2.1 的对应行保持原文，客户端不画假块（§0.1）。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§0.2、§2.3.2、§2.3.2.1、§4.7、§9；
`docs/appendix-omp-ui-realtime-rpc.md` A.4、A.11、A.14；`docs/appendix-omp-ui-business-rpc.md` B.5；
ADR-2（transcript 区块与 `display_kind`/`display_metadata` 的先例）、ADR-3（Background TAN）、
ADR-5（Recap/压缩）、ADR-6（TTSR）、ADR-9（Late diagnostics）；`tui_gateway/contracts/common.py`、
`tui_gateway/contracts/sessions.py`、`tui_gateway/contracts/events.py`、
`tui_gateway/contracts/canonical.py`、`tui_gateway/session_history.py`、
`tui_gateway/agent_callbacks.py`、`gateway/run_turn_progress.py`、`gateway/run_turn_runner.py`、
`gateway/local_viewer_events.py`、`gateway/panel_inventory.py`、`agent/adviser_delivery.py`、
`agent/turn_stop_gates.py`、`agent/turn_usage.py`、`omp-ui/src/modes/rpc/gateway-session.ts`、
`omp-ui/src/modes/controllers/event-controller.ts`、`omp-ui/src/session/agent-session.ts`、
`omp-ui/src/modes/utils/ui-helpers.ts`。

**范围**：只讨论 omp-ui ↔ Hermes canonical local gateway。消息平台、Desktop、Ink sidecar、ACP、
compute-host 的 worker 帧不新增设计；所有新增字段默认值保持旧 payload 逐字不变。落地顺序是
**网关结构先行、omp-ui 映射随后**（同一阶段的后半）：网关先产出稳定、可测试的结构，客户端只消费
（D8）。本 ADR 不要求 omp-ui 先于网关生产结构；现有 `omp-ui` 里对 `adviser` 文本、`/skill:` 文本、
todo 文本的解析一律视为临时兜底，D1/D2/D3 落地后删除，不得作为长期契约。

**取代关系（逐条，避免文档互斥）**：

- ADR-2 **D1/D2** 的客户端消费面继续有效；但“先在客户端按 `display_kind` 解析文本并留 seat”不再
  是落地顺序。本 ADR 的 D1/D7 先定义网关结构，客户端投影只做字段改名与组件选择。
- ADR-2 **D3**（canonical 本地 viewer 补发 `message.adviser`）由本 ADR **D2** 具体化为：行先持久化，
  事件后发，`row_id`/`id` 可回指；sidecar 行为不变。
- ADR-2 **D5/D6/D8/D10** 的 `session.history`、`include_tool_content`、工具行投影由本 ADR
  **D4/D5** 收口；默认 payload 与 `include_tool_content="none"` 不变。
- ADR-2 **D11** 的 `todo.reminder` 由本 ADR **D3** 明确为网关 stop-gate 的 producer；客户端不再保留
  事件 seat 常量。
- ADR-3/5/6/9 已决定的区块（Background TAN、Recap/压缩、TTSR、Late diagnostics）仍由各自 ADR
  定义数据源；本 ADR 只要求它们接入 **D6** 的 capability 清单，不重复设计。
- 无 producer 的区块继续按 plan §0.1 保持空；本 ADR **D7** 只把“缺什么”放进网关能力广告，不在
  客户端渲染可见占位。

## 问题

omp-ui 的 transcript 有 27 类区块，但网关目前给客户端的主要是**显示投影**：
`session.history` 的 `TranscriptMessage` 只有 `text`、`display_kind`、`display_metadata` 和少量
工具字段（`tui_gateway/contracts/common.py:168-186`）。部分区块因此变成“客户端猜文本”：

- `adviser` 行只有 `display_metadata.severity`；`message.adviser` 的 canonical producer 未接，
  omp-ui 若从 `text` 的 `- [concern · reviewer] …` 反解 notes，网关换文案就坏。
- `skill_invocation` 行只有 `/skill:name args` 文本；`SkillMessageComponent` 需要
  `{name,path,args,prompt,lineCount}`，客户端只能猜 name/args，path 永远缺失。
- `todo_reminder` 在 Hermes 没有 stop-gate producer；客户端只能留 seat，不能显示。
- `message.complete` 没有 per-step usage、`served_model`/`requested_model`，cache/served 标记无法画。
- `session.history` 的 `entries` 只有 `entry_id/role/text/timestamp/display_kind`
  （`tui_gateway/contracts/sessions.py:496-503`），没有 `display_metadata` 与工具字段；
  客户端无法从 entries 重建卡片。
- 工具行 `is_error` 没有顶层投影，`content` 只对少数工具放行；resume 后卡片缺参数/标签/失败态。
- `agent.print` 与 `status.update` 会为同一条 lifecycle line 各发一次；客户端去重只是补丁。

共同结论：**语义属于网关**。客户端可以做 camelCase/snake_case、组件选择、折叠状态，但不应解析
adviser/ skill / todo 的正文来还原结构化数据，也不应靠本地常量猜测“网关有没有 producer”。

## 决策

### D1. `role:"custom"` + `custom_type`/`details` 是自定义区块的唯一结构；`display_kind`/`display_metadata` 删除中

`role:"custom"` 是**自定义区块**的唯一结构化角色；完整 transcript 角色全集、座位与枚举定义见 D10。每行携带：

```text
role: "custom"
custom_type: str          # advisor / skill-prompt / todo-reminder / model-switch / ...
content: str | blocks
display: bool             # UI-only; false 仍可能进模型上下文
display_only: bool        # true = 永不进入模型上下文（通知/操作行）
details: JsonValue        # 组件数据；不进入模型上下文
attribution: "user" | "agent"
legacy_role: str          # 回投影用；不进入模型上下文
```

`custom_type` 的 `details` 形状：

| `custom_type` | `details` 形状（新增/补齐） | `display_only` |
|---|---|---|
| `advisor` | `{"notes":[{"note":str,"severity":"nit"\|"concern"\|"blocker","advisor"?:str,"turn"?:int}],"severity"?:str,"label"?:str}` | false |
| `skill-prompt` | `{"name":str,"path":str,"args"?:str,"prompt":str,"lineCount":int}` | false |
| `todo-reminder` | `{"todos":[{"content":str,"status":"pending"\|"in_progress"\|"completed"\|"abandoned"\|"blocked"}],"attempt":int,"max_attempts":int}` | false |
| `ttsr-injection` | `{"rules":[str],"matched"?:{<name>:<excerpt>}}`（ADR-6 P1 工具提醒只有 `rules`；P3 interrupt 另带 `matched` 摘录） | false |
| `model-switch` / `personality-switch` / `process-complete` / `async-delegation-complete` / `internal-notification` | `{"level":"info"\|"warning"\|"error","source":str}` | true |
| `auto-continue` / `failed-turn` | 不转 custom；保留 legacy user/assistant 行（auto-continue 是模型可见的续跑 note，failed-turn 是失败 notice） | — |

规则：

- `advisor` 是 wire `custom_type`；`adviser` 只作为旧 `display_kind`/block id，两者不得混用。
- `custom_messages:true` 会话的新 producer 只写 `role:"custom"` + 上表字段；**不得再写**
  `display_kind` 或 `display_metadata`。未开启该标志的会话（Desktop/Ink）保留 legacy 原始行，
  由其 hydration 直接读取；wire 投影对两种形状都归一成 custom（P5）。
- `display_kind` / `display_metadata` 标记为 **DEPRECATED-FOR-DELETION**：P5 删除了 viewer 门控的
  legacy wire 回投影（`project_custom_messages` 分支与客户端回退映射）。服务端一次性
  marker→custom 归一保留（`_marker_custom_type`/`legacy_details`/`todo_details`，覆盖旧 adviser/
  todo/通知行与 adviser steer），输出仍只有 `custom_type`/`details`；不回写 DB。
- 持久化：`messages` 表没有 custom 列，writer 把六个 custom 字段放进
  `display_metadata["_custom_message"]` 信封（`agent/custom_messages.py`），读取时 lift 回顶层；
  producer 本身仍只写顶层字段，不写该信封，也不写 `display_kind`/`display_metadata`。
- viewer 双门（P5 前）：`project_custom_messages:true` 只在 session 的 `custom_messages:true` 时
  生效。P5 起投影恒为 custom，该参数保留但为 no-op。
- 客户端不再解析 `text` 反解 notes/name/todos；只读 `details`。缺 `details` 时只渲染 text-only
  通用块。
- `display_only:true` 只用于纯展示标记（model-switch 等）；`display_only:false` 的
  adviser/skill/todo/ttsr-injection 才走 D9 的 custom→developer/user 转换。`auto-continue`/`failed-turn`
  不转 custom，保持原 user/assistant 行，避免丢掉模型可见的续跑/失败信息。
- `details`/`display_only`/`legacy_role` 是 display-only/内部字段，不进入 provider 请求。

### D2. canonical `message.adviser` 由网关补发，行/事件可回指

- `gateway/run_turn_runner.py::_wire_turn_agent_callbacks` 安装只过滤 `display_kind=="adviser"` 的
  `plugin_message_callback`，复用 `tui_gateway/agent_callbacks.py::_adviser_payload`。
- 持久化行先 append，再发 `message.adviser`；payload 增加 `row_id`（durable `_row_id`）与
  `details.notes`（D1；旧行兼容读 `display_metadata.adviser.notes`）。客户端用 `row_id` 去重：快照已有该行时，实时事件只更新
  同一组件，不追加第二块。
- 行形状随 D9 的 **session** 标志：`custom_messages:true` 时 append `role:"custom"` +
  `custom_type:"advisor"`、`display:true`、`details`、`attribution:"agent"`、`legacy_role:"user"`；
  `false` 时保持 legacy `role:"user"` + `display_kind:"adviser"`。
- `_adviser_payload` 从行文本解析 notes 只作为**网关侧**兼容旧行的 helper；新 adviser 产生时应直接
  传入结构化 notes，不在客户端解析。canonical live handler 直接用 payload 的 `details`/`row_id`
  走 D8.1 的同一投影，不再从 `text` 重建行。
- sidecar（`tui_gateway/agent_callbacks.py`）的行为与 payload 保持；canonical 与 sidecar 共用同一
  payload 构造 helper，不新增回调。

### D3. `todo.reminder` 由网关 stop gate 生产

- 按 ADR-2 D11 在 `agent/turn_stop_gates.py` append reminder 行。行形状随 D9 的 session 标志：
  `custom_messages:true` 时 `role:"custom"`、`custom_type:"todo-reminder"`、`display:true`、
  `details`、`attribution:"agent"`、`legacy_role:"user"`；`false` 时 legacy `role:"user"` +
  `display_kind:"todo_reminder"`。两种形状都带 `details = {todos,attempt,max_attempts}`（旧行兼容读 `display_metadata.todo_reminder`）。
- 行 append 后发 `todo.reminder {todos,attempt,max_attempts,row_id}`；canonical local viewer 的
  `row_id` 必填（行已持久化），managed/remote v1 不发事件，但行照常持久化，resume 可重建。客户端只按
  `row_id` 去重（D8.3），不按文本或 `custom_type` 猜重复。
- omp-ui 不保留 `todo.reminder` seat 常量；P3 起 `runtime.describe.transcript_blocks` 报
  `todo_reminder.supported:true`（事件 `todo.reminder` + 行 `row_id`）。

### D4. `session.history` 提供可重建区块的 entries

`SessionHistoryParams` 增加：

```text
include_tool_content?: "none" | "text" | "full" = "none"
include_outline?: bool = false
```

同一 `include_tool_content` 也加到 canonical 的 `CanonicalResumeParams`：resume 快照的
`display_messages`/`messages` 走同一个 `_history_to_messages` 投影，omp-ui 因此一次调用就能拿到工具
`content`，不必再按行分页回填。`include_outline` 仍只属于 `session.history`。默认 `none`，不传参的
resume payload 与今天逐字一致。

`HistoryEntry` 增加可选字段（全部 display-only）：

```text
display_metadata?: JsonValue
content?: JsonValue          # 仅 include_tool_content != "none"
args?: object
labels?: ToolLabel[]
tool_call_id?: str
name?: str
context?: str
is_error?: bool
reasoning?: str
custom_type?: str
display?: bool
details?: JsonValue
attribution?: str
legacy_role?: str
```

约束：

- `entry_id` 仍是 durable `row_id`；`entries` 与 `messages` 同源、同序、可回指。
- `include_tool_content="none"` 时 `session.history` 与 `session.resume` 的 payload 都与今天逐字
  一致；`text`/`full` 只增加 `content`，且走
  `agent/tool_dispatch_helpers.py::display_tool_result_content` 的 display-safe 投影。
- `include_outline=true` 才生成 `outline`，定义按 ADR-2 D6；默认关闭。
- P5 前 entries 可保留 legacy `display_kind`/`display_metadata`（DEPRECATED-FOR-DELETION）；
  `project_custom_messages:true` 时
  `entries[].role` 与 `messages[]` 一样是 `role:"custom"`，并带 `custom_type`/`display`/`details`/
  `attribution`/`legacy_role`；默认 false 时 entries 保持 legacy role + `display_kind`。
  客户端不用再解析 `text`。
- 转换点：`_history_to_messages` 在算出 `display_kind` 之后、append 之前做投影；`text`/
  `display_kind`/`display_metadata` 保留兼容。`project_history_commentary` 在其后运行，必须显式
  保留 custom 行（不得把 role custom 丢弃或重新包成 user/assistant）。

### D5. 工具行补全 `is_error` 与结构化 metadata

- `_history_to_messages` 从 `display_metadata.tool_result_metadata.is_error` 投影
  `TranscriptMessage.is_error`；只存 `display_metadata` 一份，顶层是只读投影。
- canonical `tool_result_metadata_callback` 在 P2 安装（local viewer turn），只算 `inline_diff`；
  `_commit_tool_result` 在 callback 返回后 merge `is_error`，不得整键重写。
- tool 行继续携带 `name`/`args`/`labels`/`context`；`name` 是真实工具名，原样透传，客户端不改写
  （render key 只在构造组件时计算，D8.2）；`content` 由 D4 的三态参数控制。
- 客户端只把 `is_error`/`content`/`args`/`labels` 映射进现有 `ToolExecutionComponent`，不改卡片布局；
  `toolName` 保持 wire `name`，render key 与展示名分开。

### D6. `runtime.describe.transcript_blocks` 是唯一的 capability 清单

`RuntimeDescribeResult` 增加：

```text
transcript_blocks: dict[str, TranscriptBlockCapability]

TranscriptBlockCapability {
  supported: bool
  status: "ready" | "partial" | "pending" | "unsupported"
  events: list[str]        # 该区块消费的事件（无则空）
  fields: list[str]        # 依赖的 wire 字段
  needs: str | null        # 缺什么；supported=false 时必填
  reason: str | null
}
```

- block id 使用 plan §2.3.2 的稳定 id（`adviser`、`skill_invocation`、`todo_reminder`、
  `compaction_summary`、`recap`、`ttsr_notification`、`late_diagnostics`、`cache_miss_marker`、
  `served_model_marker`、`stripped_tool_calls`、`reaction`、`tool_execution`、`notice` 等）。这些是
  capability 表的键，沿用 plan 的 snake_case，**不是** D1 的 wire `custom_type`（`advisor`/
  `skill-prompt`/`todo-reminder`）。
- 客户端只读这张表决定“画/不画/标 pending”；**不再在 omp-ui 维护无 producer 的 seat 常量**。
  客户端落点（`connectGatewayOmpSession` 调用一次并缓存）见 D8.4。
- `supported:false` 不代表发假数据：网关不发该事件/行；客户端保持空，不渲染可见占位。
- 由 `gateway/panel_inventory.py` 的静态清单生成，和 `runtime.describe.panels` 同一来源、
  同一测试门。

### D7. `transcript.notice` 作为网关去重后的统一通知帧

`agent.print`（`_print_fn`）与 `status.update`（`status_callback`）对同一条 lifecycle line 会各发一次
（`agent/status_output.py::_emit_status_kind` 先 `_vprint` 再 `status_callback`）。网关新增：

```text
event("transcript.notice", {id, level, text, source, row_id?})
```

- `_emit_status_kind` / 直接 `_print_fn` 的公共 helper 发一次 `transcript.notice`；同一
  `(level,text,source)` 在短窗口内去重。
- `agent.print` / `status.update` 保留给旧客户端；omp-ui 只消费 `transcript.notice`，忽略两条旧通道。
  omp-ui 的 handler 走既有 `#emitNotice(level,text,source)`（`gateway-session.ts:1293`），不写
  `#messages`；带 `row_id` 且快照已有该 custom 行时合并，否则只发一次（D8.3）。
- `notification.show` 仍是 sticky/ttl notice 的通道；`transcript.notice` 只承载已发生的一次性行，
  不把 `notification.show` 复制成第二条。

### D8. omp-ui 侧：单一投影点、组件映射、opt-in 与能力消费

**改动面**：只改 `omp-ui/src/modes/rpc/gateway-session.ts`、`omp-ui/src/modes/utils/ui-helpers.ts`、
`omp-ui/src/modes/controllers/event-controller.ts`（live 工具卡）、`omp-ui/src/session/agent-session.ts`
（`tools.list` 展平）、生成的 `gateway-contract.generated.ts` 及其测试。不改 `omp-ui/src/vendor/` 与
`@oh-my-pi/pi-tui` 的布局/折叠/键位，不新增 `transcript-blocks.ts` 之类的平行实现。角色穷举、
seated 与 `bash/python/fileMention/hook` 的渲染移植见 D10；adviser/todo 的 `row_id` 去重契约见
D2/D3。

**现状（本 ADR 要删除的临时兜底）**：

- `rowToAgentMessage`（`gateway-session.ts:653`）是快照的唯一投影点，`snapshotFrom`（`:786`）优先
  `display_messages`。`displayKindMessage`（`:330`）当前从 `text` 解析 adviser notes、`/skill:`
  name/args、todo 行；`#onAdviserMessage`（`:1346`）与 `#onTodoReminder`（`:1368`）再手工造一条
  `TranscriptMessage` 复用同一解析。这些 helper 在 P1/P3 删除。
- 工具行当前在投影处就把 `name` 换成 `gatewayToolName(name)`（`:700`，`read_file`→`read`、
  `todo_list`→`todo`），`ToolResultMessage.toolName` 因此只剩 render key，卡片标题显示 `read`
  而不是真实工具名；live 的 `#onToolStart`/`#onToolComplete` 又直接透传原始名，所以 live/resume
  现在不一致。D8.2a 把映射移到两处共用的渲染边界，`toolName` 保持 wire 原值。
- `GATEWAY_PENDING_TRANSCRIPT_BLOCKS`（`:150`）是手写 seat 清单，与网关能力表重复；P4 删除，改读
  `runtime.describe.transcript_blocks`（D6）。`gateway-session.test.ts:526` 的对应断言同步删除。
- `addMessageToChat`（`ui-helpers.ts:143`）是行→组件的唯一渲染分派；P1 起按 D10 穷举 11 个角色。

**D8.1 单一投影点**：

- 快照与实时事件必须产生同一种 `AgentMessage`。抽出一个 `customRowToMessage(fields)`，供
  `rowToAgentMessage`、`#onAdviserMessage`、`#onTodoReminder` 共用；live handler 直接读 payload 的
  `custom_type`/`details`/`row_id`，**不得**再构造假行或调用文本解析 helper。
- 现有 `text`/`timestamp`/`model`/`tool_call_id`/`name`/`context`/`reasoning` 继续读；新增只读
  `custom_type`/`display`/`details`/`attribution`/`legacy_role`/`is_error`/`content`/`args`/`labels`/
  `row_id`。`display_kind`/`display_metadata` 只在 P5 前作为旧 payload 的读取兼容；`text` 只作为正文，
  不再被解析成结构。
- `display:false` 不渲染；`display_only` 不决定渲染（通知类仍画），只表示不进模型；缺 `details`
  按 D1 只画 text-only 通用块。

**D8.2 组件映射**（wire → 客户端 message → 渲染器；渲染器全部来自已安装的 pi-tui）：

| wire | 客户端 `AgentMessage` | 渲染 |
|---|---|---|
| `custom` + `custom_type:"advisor"` | `CustomMessage{customType:"advisor", details}` | `createAdvisorMessageCard` |
| `custom` + `"skill-prompt"` | `CustomMessage{customType:"skill-prompt", details}` | `SkillMessageComponent` |
| `custom` + `"todo-reminder"` | `CustomMessage{customType:"todo-reminder", details}` | `TodoReminderComponent` |
| `custom` + 通知类（`model-switch`/`personality-switch`/`process-complete`/`async-delegation-complete`/`internal-notification`） | `CustomMessage{customType:<wire>, details:{level,source}}` | 按 `details.level` 选 `TranscriptNotice.line` / `StatusNotice` |
| 其他 `custom` | `CustomMessage` | `CustomMessageComponent` |
| `tool` | `ToolResultMessage{toolName:wire.name, toolCallId:wire.tool_call_id, content, details:{args,labels}, isError}` | `ToolExecutionComponent(renderKey, tool)`；`tool = session.getToolByName(wire.name)`，`renderKey = toolRenderName(wire.name, tool)` |
| `compaction_summary` | `CompactionSummaryMessage` | `CompactionSummaryMessageComponent` |
| `branch_summary` | `BranchSummaryMessage` | `BranchSummaryMessageComponent`（seated） |
| `hook_message` | `HookMessage` | `HookMessageComponent`（seated） |
| `bash_execution` / `python_execution` / `file_mention` | 对应 entry | `BashExecutionComponent` / `EvalExecutionComponent` / `buildFileMentionBlock`（seated） |
| `user` / `developer` / `assistant` | 现有消息 | `UserMessageComponent`（developer 为 synthetic）/ `AssistantMessageComponent` |
| `system` | 适配器丢弃 | — |

wire 角色用 D10 的 snake_case；客户端 `AgentMessage` 用 omp 的 camelCase（`hookMessage` 等）。
`gateway-notice` 不再作为 wire 的 `custom_type` 出现（它只是当前客户端自造的兜底名）；通知类用
D1 的五个 `custom_type` + `details.level`。

**D8.2a 工具名：render key 与展示名分开**：

- `ToolResultMessage.toolName` / live `event.toolName` 始终是 wire `name`（如 `read_file`）。
- `AgentSession.getToolByName(wireName)`（当前未使用，`agent-session.ts:599`）返回最小投影
  `{name: renderName, label: wireName}`；renderer 别名表只在这里（`read_file`→`read`、
  `todo_list`→`todo`），不在 authority 名单内时返回 `undefined`（回退 wire 名与通用卡，不猜
  renderer）。
- 两处卡片构造（resume 的 `addMessageToChat`、live 的 `#handleToolExecutionStart`）都先取
  `const tool = session.getToolByName(wireName)`，再算
  `const renderName = toolRenderName(wireName, tool)`（pi-tui 导出，`tool.name ?? wireName`）；
  `renderName` 只作 `ToolExecutionComponent` 第一参，`tool` 作第四参。`#toolName` 因此是 render
  key（供 `toolRenderers`/todo/edit 分支），`#toolLabel = tool?.label ?? toolName`
  （`tool-execution.ts:507`）显示 wire 真实名。
- `hasBuiltInTool(wireName)` 仍是 authority gate（`tools.list` 返回真实名），不得拿 render key
  去查；read-group 的 start/end 判定（`event-controller.ts:363/411`）用 render key。
- render key 不得写回 `toolName`/`details`，也不得进入历史；新增别名只影响 renderer 选择。
- 前置修正：`#refreshTools` 现在把 `tools.list` 的 `result.tools` 当数组，实际名单在
  `toolsets[].tools`（`agent-session.ts:186-202`）；名单为空会让 `hasBuiltInTool` 恒 false，
  `useBuiltInRenderer` 失效。映射规则落地时一并展平。

**D8.3 快照合并**：

- 客户端维护 `Map<row_id, AgentMessage>`；`adoptSnapshot` 用快照整体重建该表并替换 `#messages`；
  live 事件命中已存在 `row_id` 时只更新同一块，不 append 第二块（`row_id` 的 producer 契约见 D2/D3）。
- `transcript.notice` 按事件 `id` 去重；带 `row_id` 时与快照行合并，否则只发一次（D7）。

**D8.4 能力表**：

- `connectGatewayOmpSession`（`:1589`）在 create/resume 后**非阻塞**调用一次 `runtime.describe`，
  缓存 `transcript_blocks` 并暴露 `transcriptBlock(id)`。未返回或失败时返回 `undefined`：非角色
  block（recap/TTSR/late diagnostics 等）不挂载，角色行照常按 D10 渲染；不得伪造
  `supported:true` 或画 pending 占位。
- 运行时 `supported` 只来自这张表；生成器的角色 seat 子集只提供编译期角色名（D10），不作为
  capability 判断。
- 客户端不维护 `GATEWAY_PENDING_TRANSCRIPT_BLOCKS` 之类的手写清单；`seated` 的渲染分支仍存在
  （D10 穷举要求），但能力表为 false 时不可达。

**D8.5 请求参数**（omp-ui 必须显式 opt-in；Desktop/Ink 不传，保持 legacy）：

- `session.create`：`{source:"tui", client:{name:"omp-ui"}, custom_messages:true}`。`custom_messages` 是
  session 级模型标志（D9），只在 create 生效；`client` 沿用 ADR-6 的客户端自述身份（对象形态，
  仅归因，不参与任何策略或授权）。
- `session.resume`：`{..., display_projection:true, project_custom_messages:true,
  include_tool_content:"full"}`；一次快照带齐工具 `content`（D4 把该参数同时加到 resume），不再
  二次 `session.history` 回填。
- `session.history`：`{..., project_custom_messages:true, include_tool_content:"full",
  include_outline:true}`；`history()`（`:938`）目前只传 `limit`，P2 补齐。
- `project_custom_messages` 是 per-viewer 参数，客户端不得写回 session state；`custom_messages` 是
  session state，resume 不得修改。

**D8.6 类型与测试**：

- `gateway-contract.generated.ts::TranscriptMessage.role`/`HistoryEntry.role` 用生成的
  `TranscriptRole` 联合；`details` 按 `custom_type` 生成可选形状，客户端不手写第二份联合。
- 穷举与 seated 分支按 D10；测试覆盖每类 custom/role 的投影、`display:false`、`row_id` 合并、
  `is_error`/`content`/`args`/`labels` 与组件选择；删除 `gateway-session.test.ts` 的 seat 断言。
- 门：`bun run check`（typecheck，含 D10 的 `satisfies never`）与
  `scripts/gen_gateway_contracts.py --check`。

### D9. `role:"custom"` 与模型转换；`display_kind`/`display_metadata` 删除

`role:"custom"` 是网关会话层的消息角色，不是 provider wire role。它对应 omp 的 entry 类型
`custom_message`（该 entry 重建后的消息角色才是 `custom`）；自定义区块在 wire 上只有
`role:"custom"` 一种表示。完整 transcript 角色全集见 D10；客户端直接使用该角色名。

- **行形状**：D1 的字段；message-bearing 的 adviser/skill-prompt/todo-reminder/ttsr-injection 为
  `display_only:false`，通知/操作行为 `display_only:true`。
- **两个开关**：
  - **模型侧 session 标志 `custom_messages`**：create 时解析并持久化到 session state；决定
    producer 是否写 `role:"custom"`，以及 `build_api_messages` 是否把 message-bearing 行
    按 custom 转换。resume 不改变它；新会话才可开启。
  - **显示侧 per-viewer 标志 `project_custom_messages: bool = false`**：`session.resume` /
    `session.history` 参数，只影响 `_history_to_messages`。默认 false 把 custom 行回投影成
    legacy `role:legacy_role` + `display_kind`；omp-ui 传 true 才拿到 `role:"custom"`。
- **一致性**：模型侧只读 session 标志；显示侧只读 viewer 标志。`true` 时 display 投影返回
  custom；模型侧转换同一批 message-bearing 行；`false` 时两边都保持 legacy。禁止无意分叉。
- **模型转换**：`agent/turn_context.py::build_api_messages` 在发送前调用纯函数
  `convert_custom_messages_for_api`。`custom_messages:true` 时处理两类输入：已写成的
  `role:"custom"` 行，以及仍为 `user`/`assistant` + message-bearing legacy
  `display_kind` 的旧行；**不能只匹配 role**。规则与 omp `convertToLlm` 一致：
  - `display_only:true` 的纯展示标记 → 丢弃（不进入模型上下文）；`auto-continue`/`failed-turn` 不走此转换；
  - 默认 message-bearing（adviser/todo/ttsr-injection 等）→ `developer`；
  - `custom_type:"skill-prompt"`/`"collab-prompt"` 且 `attribution:"user"` → `user`；
  - 普通 custom 含 image 块 → 文本 `developer` + 图片 `user`；user 归因 skill prompt 不拆分；
  - `display:false` 仍进入模型上下文；`display` 只控制 UI。
- **内部字段**：`custom_type`/`display`/`display_only`/`details`/`attribution`/`legacy_role` 以及
  旧 `display_kind`/`display_metadata` 必须在 API copy 前删除；provider 只看到
  `system`/`developer`/`user`/`assistant`/`tool`。不支持 `developer` 的适配器按 D11 折叠
  （Anthropic 默认折成 `user`，仅特定模型可升级为 mid-conversation `system`）。
- **映射表**：

| custom_type | attribution | display_only | 模型角色 |
|---|---|---|---|
| `advisor` | `agent` | false | `developer` |
| `skill-prompt` | `user` | false | `user` |
| `todo-reminder` | `agent` | false | `developer` |
| `ttsr-injection` | `agent` | false | `developer` |
| `model-switch`/`personality-switch`/`process-complete`/`async-delegation-complete`/`internal-notification` | `agent` | true | 不进入模型 |
| `auto-continue` | `user` | — | 不转 custom；legacy user 行仍进入模型 |
| `failed-turn` | `assistant` | — | 不转 custom；legacy assistant 行保留 |
| `steer` | `user` | — | 真实 user 行，不是 custom |
| `hidden` | — | — | 不投影 |

- **删除 `display_kind`/`display_metadata`**：
  - P1 起禁止新写入；旧行只在读取时归一化。
  - P5 删除 `TranscriptMessage`/`HistoryEntry` 的 `display_kind`/`display_metadata`、
    `_legacy_display_kind` 与 viewer 门控的 legacy wire 回投影；合成行改用 `synthetic_kind`/
    `live_steered`，compaction counters 移到 `details.compaction`。服务端保留一次性
    marker→custom 归一（旧行与 adviser steer 仍渲染成 custom block），输出不变。
  - 存储列保留为网关内部 marker（`hidden`/`steer`/`auto_continue`/`failed_turn`/model-switch
    pivot）与 custom envelope 的载体；`PERSISTENCE_ONLY_MESSAGE_FIELDS` 继续剥离它们，因为
    Desktop/Ink 的原生行与 provider 副本仍读同一份 durable dict。wire 上不再出现这两个字段。
  - 删除前置：所有 producer 已迁移；旧客户端有明确下线窗口；契约生成物重跑且无消费者读取旧字段。
- **prompt cache**：`custom` 行是模型消息；`custom_messages:true` 会话的 message-bearing
  行会改前缀，必须 session 内稳定。`display_only:true` 与 `details` 永不进入上下文。
- **resume 兼容**：`custom_messages` 随 session state 持久化且 resume 不可变；
  `project_custom_messages` 每次请求可不同。Desktop/Ink 不传它，拿回 legacy；omp-ui 在
  `session.create`/`session.resume`/`session.history` 显式 opt-in（D8.5）。

### D10. Transcript 角色全集：`TranscriptRole` 枚举 + 单一座位表

角色全集固定为 11 个（wire 用 snake_case；omp 的 camelCase 只出现在客户端映射层）：

```text
user, developer, assistant, tool, custom, hook_message,
bash_execution, python_execution, file_mention, branch_summary, compaction_summary
```

**枚举**：`tui_gateway/contracts/common.py` 新增 `class TranscriptRole(WireEnum)`（与 `SubagentStatus`
同款）；`TranscriptMessage.role` 与 `HistoryEntry.role` 改为该枚举。

`TranscriptRole` 含 11 个 transcript 角色 **加一个 legacy 成员 `system`**：
`_history_to_messages` 现在会输出 `role:"system"`，若枚举不含它，现有 payload 会校验失败并打破“默认
payload 逐字不变”。`system` 只在枚举里做兼容，不属于 `TRANSCRIPT_ROLES`，生成的客户端联合与适配器都
不含它（适配器丢弃 `system`）。**不采用**“同阶段过滤 system 行”，那会改变现有输出。

**单一座位表**：`tui_gateway/contracts/transcript_roles.py::TRANSCRIPT_ROLES`（只含 11 个 transcript
角色），每项：

```text
TranscriptRoleSpec {
  role: TranscriptRole
  status: "produced" | "partial" | "seated"
  fields: list[str]        # 该角色行必须携带的 wire 字段
  needs: str | null        # seated/partial 必填
  reason: str | null
}
```

它是唯一来源，派生三处，禁止手写第二份：

- `tui_gateway/session_history.py` 的角色过滤（`set(TRANSCRIPT_ROLES) | {"system"}`）；
- `gateway/panel_inventory.py::TRANSCRIPT_BLOCKS` 的**角色**条目与
  `runtime.describe.transcript_blocks` 的角色项；
- `scripts/gen_gateway_contracts.py`：TS 角色联合 + 客户端角色 seat 子集。

**非角色区块不归本表**：`adviser`、`skill_invocation`、`todo_reminder`、`recap`、
`ttsr_notification`、`late_diagnostics`、`stripped_tool_calls`、`collab_prompt`、`reply_write`、
`background_tan`、`cache_miss_marker`、`served_model_marker`、`tool_execution`、`notice` 是 D6 的
block 座位，不是角色。生成器只
替换其中 `compaction_summary`/`branch_summary` 两条角色项；其余仍由 D6 的
`runtime.describe.transcript_blocks` 广告，客户端不得因生成物而丢掉这些 seat。

| role | status | 说明 / `needs` |
|---|---|---|
| `user` | produced | 含 `steer`：`liveSteered=true`，正文为用户原话 |
| `developer` | seated | 无框架指令型行；现有机制行都归 `custom` |
| `assistant` | produced | `failed_turn` 走 assistant 正文 |
| `tool` | produced | 客户端映射为 `toolResult` |
| `custom` | produced | P5 前由 `display_kind` 归一化 |
| `hook_message` | seated | 无 hook/扩展注入 API |
| `bash_execution` | seated | 无 composer `!` 模式 |
| `python_execution` | seated | 无 `$` 模式 / REPL 单元格 |
| `file_mention` | seated | `@file` 只并进 user 文本，无独立行 |
| `branch_summary` | seated | 扁平 `messages` 表没有 `fromId`/分支边界 |
| `compaction_summary` | partial → **produced**（ADR-5 P1） | 摘要文本 + `display_metadata.compaction` 盖章；仅 `include_compaction:true` 时投影；merged carrier 追加第二条 entry，实时另有 `compaction.summary` |

**座位规则**：

- **投影门控**：`compaction_summary` 只在 viewer 传 `include_compaction:true` 时输出（默认 `false`），
  不传参的客户端 payload 保持逐字不变；`branch_summary` 一旦落位也必须走同款显式参数。
- `seated`：网关不产出该角色行/事件，`transcript_blocks[role].supported=false` 且 `needs` 必填；
  适配器分支存在但永远返回 `undefined`；渲染器分支存在但永远不触发。
- `partial`：允许缺省字段，但不得用 `display_kind`、空块或占位文本代偿。
- `display_kind` 删除后的语义等价（P5 落地）：`hidden` → 投影不输出；`steer` → `user` +
  `synthetic_kind:"steer"` + `liveSteered`；`failed_turn` → `assistant` + `synthetic_kind`;
  `auto_continue` → `user` + `synthetic_kind`；其余 → `custom.custom_type`。wire 上的
  `display_kind`/`display_metadata` 已删除，存储列只作为内部 marker/projection 输入。

**模型侧白名单不变**：`build_api_messages` 只允许 `system|developer|user|assistant|tool`；
`custom` 按 D9 转换，已产出的显示角色按 D11 转换，`seated` 角色在 API copy 前丢弃。

**客户端必须穷举**：`rowToAgentMessage` 与 `addMessageToChat` 的 switch 以
`default: message satisfies never` 结尾。新增/遗漏角色直接编译失败，不能静默落进 `return []`。

**渲染补齐不得新造布局**：`ui-helpers.addMessageToChat` 缺的四个分支从参考实现
`omp-ui/node_modules/@oh-my-pi/pi-tui/src/chat/chat-transcript-builder.ts` 移植：

- `bashExecution` → `BashExecutionComponent(command, ui, excludeFromContext)` + `appendOutput` + `setComplete`
- `pythonExecution` → `EvalExecutionComponent(code, ui, excludeFromContext, language)` 同上
- `fileMention` → `buildFileMentionBlock(files, 1)`（children 为空则不加）
- `hookMessage` → `HookMessageComponent(message)`

**后端角色过滤审计**：`hermes_state_runtime.py` 的 role 白名单、`_HISTORY_ROLES`、
`hermes_state_prune_rewrite.py` 的 held roles、FTS/metrics/status/rewind 的 `role='...'` 条件全部以
`TranscriptRole` 为准；新增角色默认不进统计、不算真实 user turn、不触发 interrupted。

### D11. omp 角色转换规则（`convertToLlm` 对齐）

Hermes 的模型转换以 omp 的 `convertToLlm` 为基准（`packages/coding-agent/src/session/messages.ts`
逐条转换 + `packages/agent/src/compaction/messages.ts` 的基础转换）：先把每条 transcript 消息转成 LLM
消息，未知角色与缺省分支直接丢弃；provider 层再做一次 wire 归一化。

| omp 角色 | 转换结果 | 条件 |
|---|---|---|
| `user` | `user` | `attribution` 缺省 `user` |
| `developer` | `developer` | `attribution` 缺省 `agent` |
| `assistant` | `assistant` | `stopReason:"aborted"` 且内容为空 → 丢弃 |
| `toolResult` | `toolResult` | `prunedAt` → 只保留 text（空则 `[Output truncated]`）；工具名按 provider 重映射；`attribution` 缺省 `agent` |
| `bashExecution` | `user`（文本 + images） | `excludeFromContext:true` → 丢弃 |
| `pythonExecution` | `user`（文本） | 同上 |
| `fileMention` | 纯文本文件 → `developer` 的 `<file path="…">`；带图文件 → `user`（文本 + image blocks） | 按每个 file 是否带 `image` 分派 |
| `custom` | 非 string/block 内容 → 丢弃；用户归因的 skill/collab prompt → `user`；其余 → `developer`（原始内容） | 子类型分派见 D9 |
| `hookMessage` | `developer`（同 `custom`） | legacy |
| `branchSummary` | `user` + branch-summary 模板 | `attribution:"agent"` |
| `compactionSummary` | `user` + compaction 模板（`method:"handoff"` 用 handoff 模板）；有 `blocks` 时 `summary + blocks` 直通 | `attribution:"agent"`；`providerPayload` 供原生 replay |
| 其它 / 未知 | 丢弃 | 不报错 |

规则：

- 本表只把角色转成 `developer|user|assistant|toolResult`；Hermes 再按自己的 wire 白名单映射为
  `system|developer|user|assistant|tool`（`system` 来自 system prompt，不经过角色转换）。
- `custom` 的 `display_only:true` 在进入本表前丢弃（D9）；message-bearing 的 `custom` 才走
  `developer`/`user` 分派。`custom_type:"skill-prompt"`/`"collab-prompt"` 且 `attribution:"user"` → `user`。
- 摘要类只允许作为“上下文”进入 user 轮次；Hermes 用本地等价模板时可以换文案，但不能改成 assistant 轮次，
  也不能把摘要文本当普通 user 提问参与标题生成。
- seated 角色（D10）不产生 API 消息；将来生产者落地时直接复用本表，不改 `build_api_messages` 结构。

provider 归一化（与 omp 一致；规则以 vendored 源码为准，如
`omp-ui/src/vendor/ai/src/providers/anthropic.ts` 的 developer 默认 `user` 与
`supportsMidConversationSystem` 升级分支）：

| LLM 角色 | OpenAI Chat | OpenAI Responses | Anthropic Messages | Gemini |
|---|---|---|---|---|
| `developer` | 支持时 `developer`，否则 `system` | `instructions` 或 `developer` item | 默认 wire `user`（`attribution:"user"` 保持 `user`）；仅 `supportsMidConversationSystem` 的模型在 placement 允许时升级为 mid-conversation `system` | `developer` 且 `attribution:"user"` → `user`，否则 `system` |
| `toolResult` | `role:"tool"` | `function_call_output` / `custom_tool_call_output` item | `user` + `tool_result` block | `functionResponse` / 分组 `toolResult` |
| `user` / `assistant` | 原样 | message item | 原样 | 原样 |

模型转换落点：

- `agent/turn_context.py::build_api_messages`：实现角色表与未知角色丢弃；
- `convert_custom_messages_for_api`：实现 `custom` 的 user/developer 分派与 `display_only` 丢弃；
- 未知角色、`display_only`、seated 行在 API copy 前丢弃，provider 请求体只含白名单角色。

## 实施顺序

| 阶段 | 网关改动 | 客户端改动 |
|---|---|---|
| P1 | D1 契约与 `_history_to_messages` 补投影；D9 `custom_messages` 会话标志 + custom 投影/转换；D2 canonical adviser；D7 `transcript.notice`；D10 `TranscriptRole` 枚举 + `TRANSCRIPT_ROLES` + 生成器/客户端 seat；D11 角色转换表 | `session.create` 带 `client:{name:"omp-ui"}`/`custom_messages:true`，`session.resume` 带 `project_custom_messages:true`；`rowToAgentMessage` 与 live handler 共用 D8.1 投影，删除 adviser 文本解析；忽略旧 notice 通道；switch 改 `satisfies never` |
| P2 | D4 `session.history`/`session.resume` 的 `include_tool_content`、entries/outline；D5 tool 行 | `resume` 传 `include_tool_content:"full"`，`history()` 传 `include_tool_content:"full"`/`include_outline:true`/`project_custom_messages:true`；用 entries 重建选择器与卡片；toolResult 读 `is_error`/`content`/`args`/`labels` |
| P3 | D3 todo stop gate + `todo.reminder`；`message.complete.step_usage`/`served_model`/`requested_model`（ADR-2 D1/D2） | 删 todo seat；cache/served 标记读新字段 |
| P4 | D6 capability 清单收口 ADR-3/5/6/9 的区块 | 删 `GATEWAY_PENDING_TRANSCRIPT_BLOCKS`；`connectGatewayOmpSession` 读 `runtime.describe.transcript_blocks`；只按 capability 渲染，pending 保持空 |
| P5 | 删除 `display_kind`/`display_metadata`、legacy 归一化与旧客户端回投影；重跑契约生成 | 只读 `custom`/`details` |

## 实现落点

| 层 | 位置 | 改动 |
|---|---|---|
| 契约 | `tui_gateway/contracts/common.py::TranscriptMessage` | 新增 `custom_type`/`display`/`display_only`/`details`/`attribution`/`legacy_role`；`display_kind`/`display_metadata` 标记 DEPRECATED-FOR-DELETION；`is_error` |
| 契约 | `tui_gateway/contracts/common.py::TranscriptRole` | 11 值 + legacy `system`（仅兼容现有 payload）；`TranscriptMessage.role`/`HistoryEntry.role` 改用；生成 TS 联合 |
| 座位 | `tui_gateway/contracts/transcript_roles.py` | `TRANSCRIPT_ROLES` 单一来源；派生 `_HISTORY_ROLES`、`panel_inventory` 角色项、客户端角色 seat 子集 |
| 生成 | `scripts/gen_gateway_contracts.py` | 产出 TS 角色联合与角色 seat 子集；非角色 block 仍由 D6 `transcript_blocks` 广告 |
| 后端 | `hermes_state_runtime.py` / `tui_gateway/session_history.py` / `hermes_state_prune_rewrite.py` | role 白名单、`_HISTORY_ROLES`、held roles 统一读 `TranscriptRole` |
| 会话标志 | `tui_gateway/contracts/canonical_sessions.py` / session state | `custom_messages` 仅 create、持久化且 resume 不可变；`project_custom_messages` 是 resume/history 的 per-viewer 参数 |
| 模型 | `agent/turn_context.py::build_api_messages` | 按 D11 角色表转换 + `convert_custom_messages_for_api`；读 **session** 标志；custom→developer/user；未知角色丢弃 |
| 字段 | `agent/message_metadata.py` | `custom_type`/`display`/`details`/`attribution` 加入 persistence-only |
| 契约 | `tui_gateway/contracts/sessions.py::HistoryEntry/SessionHistoryParams/Result` | `custom_type`/`display`/`details`/`attribution`/`legacy_role`/`content`/`args`/`labels`/`tool_call_id`/`name`/`context`/`is_error`/`include_*`/`project_custom_messages`；`display_metadata` 仅 legacy，P5 删除 |
| 契约 | `tui_gateway/contracts/canonical_sessions.py::CanonicalResumeParams` | `include_tool_content`（`none`/`text`/`full`，默认 `none`）；`include_compaction`（ADR-5 D3 的 resume-only 投影参数，默认 `false`）；`project_custom_messages` 与 `custom_messages` 的落点见 D9 |
| 契约 | `tui_gateway/contracts/events.py` | `transcript.notice`、`todo.reminder` |
| 契约 | `tui_gateway/contracts/canonical.py::RuntimeDescribeResult` | `transcript_blocks` |
| 投影 | `tui_gateway/session_history.py::_history_to_messages` | D1 补投影、D4 entries、D5 `is_error`/content；`project_custom_messages`/`include_tool_content` 默认 false/none |
| 投影 | `gateway/session_controls.py::display_projection` | 增加 `project_custom_messages=False` 与 `include_tool_content="none"`；只有 `session.resume`/`session.history` 的 opt-in 调用传 true/full |
| 投影 | `gateway/session_tui_ops.py` / `tui_gateway/methods_session_history.py` | 从请求参数读 `project_custom_messages`/`include_tool_content`；其他快照调用保持默认 |
| 投影 | `tui_gateway/history_paging.py` | `outline`/分页 entries 的同一来源；entries 带 custom 可选字段 |
| 生成 | `scripts/gen_gateway_contracts.py` | 重跑 `gateway-contract.generated.ts` / OpenRPC |
| adviser | `agent/adviser_delivery.py::append_card` | 写入 `details.notes`（D1 的 custom 形状）；`display_metadata.adviser.notes` 仅 `custom_messages:false` 的 legacy 行，P5 删除 |
| adviser | `gateway/run_turn_runner.py::_wire_turn_agent_callbacks` | 安装 canonical `message.adviser` callback |
| adviser | `tui_gateway/agent_callbacks.py::_adviser_payload` | 共用 payload；`row_id`/notes |
| todo | `agent/turn_stop_gates.py` | stop gate append reminder 行 + `todo.reminder` |
| usage | `agent/turn_usage.py` | `_last_step_usage`/`_last_step_cost_usd` |
| usage | `gateway/run_turn_progress.py` / `tui_gateway/prompt_turn.py` | `step_usage`、`served_model`、`requested_model` |
| notice | `gateway/local_viewer_events.py` / `agent/status_output.py` | `transcript.notice` 单发与去重 |
| capability | `gateway/panel_inventory.py` | `TRANSCRIPT_BLOCKS` 静态清单与 `capabilities()` |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.ts::rowToAgentMessage` / `#notificationHandlers` | 单一投影 helper；只读 `custom_type`/`display`/`details`/`attribution`/`is_error`/`content`/`args`/`labels`/`row_id`；`toolName` 保留 wire `name`，删除 `gatewayToolName`；删除 `adviserDetails`/`skillDetails`/`todoReminderDetails` 文本解析；按 `row_id` 去重 |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.ts::connectGatewayOmpSession` / `history()` | create 带 `client`/`custom_messages`；resume/history 带 `project_custom_messages`/`include_tool_content`/`include_outline`；缓存 `runtime.describe.transcript_blocks` |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.ts::GATEWAY_PENDING_TRANSCRIPT_BLOCKS` | 删除手写 seat 清单；能力改读 D6 表 |
| 客户端 | `omp-ui/src/modes/controllers/event-controller.ts::#handleToolExecutionStart` / `#handleToolExecutionEnd` | live 工具卡用 `getToolByName`/`toolRenderName`；read-group 的 start/end 判定（`:363`/`:411`）用 render key，卡片 label 保留真实名 |
| 客户端 | `omp-ui/src/modes/utils/ui-helpers.ts::addMessageToChat` | 11 角色穷举；custom 按 D8.2 选组件；工具行用 `getToolByName`/`toolRenderName` 选 renderer，`toolName`/label 保留真实名；缺的 bash/python/fileMention/hook case 从参考实现移植；`satisfies never` |
| 客户端 | `omp-ui/src/session/agent-session.ts::getToolByName` / `#refreshTools` | `getToolByName(wire)` 返回 `{name:renderName,label:wire}`（别名表）；`#refreshTools` 从 `tools.list` 的 `toolsets[].tools` 展平真实工具名 |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.test.ts` / `ui-helpers.test.ts` | 投影/组件/去重/`display:false`/能力表测试；删 seat 断言 |

## 兼容与风险

| 风险 | 缓解 |
|---|---|
| 旧客户端不认新 `display_metadata` 子对象 | 全部为可选新增；旧行保持原顶层 `severity` 等字段 |
| `session.history` payload 变大 | `include_tool_content` 默认 `none`；`include_outline` 默认 false；单行字节上限 |
| adviser 行与事件重复 | 行先写、事件带 `row_id`；客户端按 `row_id` 合并 |
| `transcript.notice` 与 `notification.show` 重复 | 只让 `_emit_status_kind`/`_print_fn` 走 `transcript.notice`；`notification.show` 保持独立语义 |
| capability 广告与实现漂移 | 静态清单 + 生成器/测试门；`supported:true` 必须有 producer 测试 |
| client-side 解析残留 | P1 后删除；代码审查禁止从 adviser/skill/todo 文本反解 |
| 工具名映射污染展示/历史 | `ToolResultMessage.toolName` 保留 wire `name`；render key 只用于组件选择与行为分支，卡片 label 显式传真实名 |
| prompt cache | `display_metadata`/`details` 不进模型消息；但 `custom_messages:true` 的会话把 adviser/todo 行转成 custom→developer/user，会改前缀，必须 session 内固定；默认 false 不变 |
| 跨客户端 resume | `project_custom_messages` 是 per-viewer；Desktop/Ink 默认 false 拿回 legacy role + display_kind，不因 omp-ui 会话的 session 标志而破 |
| 旧 display_kind 行不转换 | `build_api_messages` 在 `custom_messages:true` 时同时转换 message-bearing 的 legacy display_kind 行，不只匹配 role custom |
| resume 快照缺工具 `content` | D4 把 `include_tool_content` 加到 `CanonicalResumeParams`；omp-ui 的 resume 传 `"full"`，一次快照带齐，不二次分页回填 |
| live 与快照两套映射 | 共用一个 `customRowToMessage`；live handler 只读 payload `details`/`row_id`；测试断言同一行产出同形 |
| omp-ui 漏传 opt-in | 缺 `custom_messages:true` 时模型侧走 legacy，缺 `project_custom_messages:true` 时只回投影 legacy；客户端测试断言 create/resume/history 参数 |
| 能力表与渲染分支不一致 | `satisfies never` 保证角色穷举；capability 只决定是否挂载，不删除 seated 分支 |

## 验收

1. `runtime.describe.transcript_blocks` 覆盖 plan §2.3.2 的网关区块；`supported:false` 必带
   `needs`，且对应事件/行确实不存在；客户端不画占位。
2. adviser：`custom_type:"advisor"` 的行有 `details.notes`（custom_messages:false 的 legacy 形状才读
   `display_metadata.adviser.notes`）；canonical `message.adviser` 每行恰好一次，带 `row_id`；
   resume 与 live 不产生第二块。
3. skill：行有 `custom_type:"skill-prompt"` + `{name,path,args,prompt,lineCount}`；客户端不解析
   `/skill:` 文本即可渲染 name/args/prompt。
4. todo：stop gate 追加一行并只发一次 `todo.reminder`；resume 从行重建 reminder 卡。
5. `message.complete` 带 `step_usage`/`served_model`/`requested_model`；cache/served 标记从新字段
   触发，缺失为 `null`，不回退累计 usage。
6. `session.history` 的 `entries` 与 `messages` 同序同 `row_id`，带 `custom_type`/`display`/`details`/`attribution`；工具行
   `include_tool_content` 三态与 `is_error`/`args`/`labels` 正确。`session.resume` 传
   `include_tool_content:"full"` 时 `display_messages` 的工具行同样带 display-safe `content`；不传时
   与今天逐字一致。
6a. `custom_messages:true` 的会话 + `project_custom_messages:true`：adviser/skill_invocation/
   todo_reminder 三类旧行投影为 `role:"custom"` + `custom_type:"advisor"`/`"skill-prompt"`/
   `"todo-reminder"`，并带 `display`/`details`/`attribution`；模型侧同一会话把这三类转成
   developer/user，provider 请求中不出现 custom。
   `project_custom_messages:false` 的 viewer 在同一个会话上拿回 `role:"user"` + `display_kind`。
   `custom_messages:false` 的会话两边都保持 legacy；notice 类（`model-switch` 等）始终 display-only。
7. `agent.print` 与 `status.update` 对同一 lifecycle line 在 omp-ui 只产生一条
   `transcript.notice`；旧客户端仍收到原事件。
8. 既有 Ink/Desktop/消息平台 payload（不传新参数）与今天逐字一致；`gen_gateway_contracts`、
   `gen_panel_inventory` 与相关契约测试通过。
9. `TranscriptRole` 含 11 个 transcript 角色 + legacy `system` 成员；`TRANSCRIPT_ROLES` 只含前 11 个；
   `_HISTORY_ROLES`、`TRANSCRIPT_BLOCKS` 的角色项、生成的 TS 联合、客户端角色 seat 子集全部由该表派生。
   非角色 block（`todo_reminder`/`recap`/`ttsr_notification`/`late_diagnostics`/`stripped_tool_calls`/
   `collab_prompt`/`reply_write`/`background_tan`/`cache_miss_marker`/`served_model_marker`）仍由
   `runtime.describe.transcript_blocks` 广告，生成器不得删除它们。
10. `rowToAgentMessage` 与 `addMessageToChat` 都以 `default: message satisfies never` 收尾；
    新增角色未补分支时 typecheck 失败；`seated` 角色适配器返回 `undefined`、渲染器分支不可达。
11. `seated` 角色在 `transcript_blocks[role]` 报 `supported:false` 且带 `needs`，网关不发对应行/事件；
    `compaction_summary` 由 ADR-5 P1 升为 `produced`（仅 `include_compaction:true` 时出现）；未盖章的
    老行 `tokensBefore=0` 时正常渲染且省略徽标；不传该参数的 payload 与今天逐字一致。
12. `bashExecution`/`pythonExecution`/`fileMention`/`hookMessage` 的渲染分支逐行对照
    `chat-transcript-builder.ts`，没有新布局、折叠或键位分支。
13. 每个角色按 D11 表转换：`custom` 的 user 归因 skill/collab → `user`、其余 → `developer`、
    `display_only` → 丢弃；`bashExecution`/`pythonExecution` 的 `excludeFromContext` 不产生消息；
    未知角色不报错、不出现在请求中；`fileMention` 的纯文本与带图分别转 `developer`/`user`。
14. provider 归一化后请求体只含 `system|developer|user|assistant|tool`；OpenAI 不支持 `developer` 时
    降级 `system`，Responses 用 `instructions`/`developer` item，Anthropic 默认折成 `user`（仅
    `supportsMidConversationSystem` 的模型按 placement 升级为 `system`），Gemini 对
    `attribution:"user"` 的 developer 折成 `user`。
15. omp-ui 的 `session.create` 带 `client:{name:"omp-ui"}`/`custom_messages:true`，`session.resume` 带
    `project_custom_messages:true`/`include_tool_content:"full"`，`session.history` 带
    `include_tool_content:"full"`/`include_outline:true`；Desktop/Ink 不传这些参数时 payload 与今天
    逐字一致，且 resume 不改变 session 的 `custom_messages`。
16. 同一条 adviser/todo 行在快照与 live 事件中产生同一个 `AgentMessage`，按 `row_id` 只有一块；
    `display:false` 不渲染；缺 `details` 只画 text-only 通用块；客户端不再有 `adviserDetails`/
    `skillDetails`/`todoReminderDetails` 文本解析或 `gateway-notice` 自造类型。
17. `GATEWAY_PENDING_TRANSCRIPT_BLOCKS` 删除后，`connectGatewayOmpSession` 读取的
    `runtime.describe.transcript_blocks` 仍让 seated 分支不可达；`bun run check` 与
    `scripts/gen_gateway_contracts.py --check` 通过。
18. `read_file`/`todo_list` 的 live 与 resume 卡片都用 read/todo renderer，但标题显示
    `read_file`/`todo_list`；`ToolResultMessage.toolName` 与 `event.toolName` 等于 wire `name`，
    render key 不出现在 `toolName`/`details`/历史中；`getToolByName`/`toolRenderName` 产出
    render key，read-group 的 start/end（含 `tool_execution_end` 的图片内联）与 `toolRenderers`
    用它，`hasBuiltInTool` 仍查 wire `name`。

## 不采纳

- 在 omp-ui 里继续从 `text` 反解 adviser/skill/todo：网关换文案即失效，且无法恢复 path/notes。
- 在客户端维护无 producer 的 seat 常量：能力应由网关 `runtime.describe.transcript_blocks` 广告。
- 用 `notification.show`/`status.update`/`agent.print` 冒充压缩/recap/TTSR/错误块：保持 ADR-2 D9
  与各专用 ADR 的边界。
- 把 `role:"custom"` 直接发给 provider：它只是网关/会话内部角色，`build_api_messages` 必须先按
  D9 转成 `developer`/`user`；该角色与 omp 的 `custom` 同名。
- 为 `seated` 角色伪造行、空块或占位文本：座位只广告 `supported:false` + `needs`，网关不发数据。
- 在客户端手写角色联合或 seat 常量：角色与座位只从 `TranscriptRole`/`TRANSCRIPT_ROLES` 生成。
- 把 render key 写进 `toolName`/历史来复用 omp 卡片：`read_file` 会被显示成 `read`；映射只作用于
  组件选择与行为分支。
- 为对齐 omp 而改 pi-tui 的渲染布局、折叠或键位：本 ADR 只补数据与映射。
