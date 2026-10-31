# 附录 A：实时 RPC 协议对齐（omp-ui → Hermes，server → client）

> 附录 B 是业务 RPC（client → server 请求/响应）：`docs/appendix-omp-ui-business-rpc.md`。
>
> **基准**：omp-ui 端侧定义（`omp-ui/src/modes/rpc/wire/`，机器生成 `rpc-wire.schema.json` /
> `rpc-wire.generated.ts`）中**服务端发往客户端**的每一类帧及其字段。
> **对象**：Hermes 网关 `tui_gateway/contracts/events.py`（`event()` 注册的 79 个通知）、
> `tui_gateway/contracts/server_requests.py`（16 个 server→client 请求）与
> `tui_gateway/server.py::_event_frame` 的信封。
> **只谈协议，不谈业务接入**。字段级结论以两侧当前契约文件为准；标 `[推断]` 的条目是从两侧
> 类型推导、未在 emitter 中逐字段核对的语义。

## A.0 判定口径

| 标记 | 含义 |
|---|---|
| **F 完整对应** | omp 帧的每个字段都能在 Hermes 契约中找到同义字段；允许命名风格差异（camelCase ↔ snake_case）与 Hermes 多余字段 |
| **P 部分对应** | 有对应事件/请求，但有字段缺失、语义不等或枚举不等价；逐条列出**新增字段**或**别名** |
| **N 完全没有** | Hermes 无对应事件/请求；给出建议新增项的完整 wire 定义 |

判定按“omp 字段集合 ⊆ Hermes 可提供字段集合（经别名）”执行，不按事件名相似度。

## A.0.1 硬约束：三档都要有契约定义，P 档别名在 omp-ui 转换

本附录与 `docs/omp-ui-panels-gateway-plan.md` §0.1 同源，是判定与落地的规则：

1. **交互不变**：omp 的帧类型、渲染与交互是契约；本附录的输出只用于 `omp-ui/src/modes/rpc/` 的
   映射层。任何“为了对上 Hermes 而改 omp 渲染/键位/默认值”的方案都判无效。
2. **F/P/N 三档都必须有定义**：
   - **F**：Hermes 现有契约已覆盖；映射为恒等（允许命名差异），不新增字段。
   - **P**：Hermes 有对应事件/请求，但语义或字段不全。必须同时给出**客户端别名/映射规则**和
     **Hermes 需补的语义字段**（类型、可选性、作用域），不得只写“需对齐”。
   - **N**：Hermes 没有。必须给出完整 wire 定义（事件名 + payload 字段/类型/可选性；或服务端
     请求 method + params/result + 错误码 + 作用域），**即使当前没有 producer**。未落地时由
     `runtime.describe.panels` 广告 `supported:false`，UI 保持原交互并标 `pending`/`unsupported`。
3. **P 档别名在 omp-ui 侧转换**：字段改名、枚举映射、信封解包（`params.type`/`params.payload`）、
   单位与结构换算，全部由客户端完成并在 A.0.2 登记。Hermes 契约保持原生命名，**不得**为对齐 omp
   命名新增同义字段（例如不新增 `message` 去重复 `text`）。
   本条**只约束别名**：`新增字段/事件`（A.3.1 的 `protocol_version`、A.4.2 的 `message_id`/
   `content_index`、A.5 的 `is_error` 等）是 Hermes 的语义补项，不受本条限制。已落地的 omp 形状
   投影（`queue.updated`、`commands.catalog.entries`、`prompt.result.error`）保留；规则只约束
   **新增/变更**，不回改既有契约。
4. **单向投影**：转换只做 Hermes → omp；客户端不把 omp 的交互状态写回 Hermes 以改变渲染或默认值。
5. **禁止伪造**：N 档不得用空事件、假数据或客户端 stub 冒充“已接通”。

## A.0.2 P 档别名转换清单（omp-ui 侧）

| 组 | omp 侧 | Hermes 原生 | 客户端转换 |
|---|---|---|---|
| 信封 | `type` + 平铺字段 | `params.type` + `params.payload` | `unwrapEvent()`；`session_id`/`seq` 提升为帧元数据 |
| 连接 | `ready.protocolVersion` / `maxFrameBytes` … | `gateway.ready.protocol_version` / `max_frame_bytes` … | 重命名；保存 `replay_epoch` 供重连比较 |
| 错误/提示 | `extension_error.error`、`rpc_frame_error.error` | `error.message` | `error`→`message`；`original_type` 等缺项见 A.3.2 |
| 提示 | `notice {level,message}` | `notification.show {text,level,kind}` | `message`→`text`；`warning`→`warn` |
| 回合 | `agent_start.resumedToolCallIds`、`agent_end.isTerminal/yielded/awaitingAsyncWork` | `turn.start.resumed_tool_call_ids`、`turn.end` | 重命名；缺失语义由 A.4.1 补项或客户端派生 |
| 消息开始 | `message_start.message`（快照） | `message.start`（无快照） | 客户端以 `message_id` 构造空 `AgentMessage` 壳 |
| 文本增量 | `text_delta {contentIndex,delta,partial}` | `message.delta {text,content_index}` | `delta`→`text`、`contentIndex`→`content_index`；`partial` 用 complete 的 `partial` |
| 块边界 | `text_start/text_end/image_end` | `message.block_start/block_end` | `kind`/`content_index` 直传，客户端组装 `content` |
| 思考 | `thinking_start/delta/end` | `reasoning.delta` / `reasoning.available` | `delta`→`text`；end→available |
| 工具参数流 | `toolcall_start/delta/end` | `tool.generating {name}` | delta 归入 `tool.args_delta`（N 契约） |
| 结束 | `done.reason` / `error.reason` | `message.complete.stop_reason`（omp 词表）/ `status`（Hermes 原生） | `reason`→`stop_reason` 直传；只有 Hermes `status`（complete/error/interrupted）需映射，二者不混用 |
| 消息结束 | `message_end.message`（快照 + usage） | `message.complete.text/usage` | 客户端累积成 `AgentMessage`；`totalTokens`→`total`、`reasoningTokens`→`reasoning`、`cacheRead/Write`→`cache_read/cache_write` |
| 工具 | `toolCallId` / `toolName` / `isError` | `tool_id` / `name` / `is_error` | 重命名 |
| 工具进度 | `partialResult` / `update` | `tool.progress {progress,...}` | `tool.update`/`tool.stream` 为 N 契约；未落地时不渲染流式细节 |
| 队列 | `queue_update` | `queue.updated` | 直传（`steering`/`follow_up`/`live_steered` 同形） |
| Goal | `objective` / `state` | `control.goal` / `session.control.update` | `objective`↔`title`；`enabled/mode/reason` 见 A.7.2 补项 |
| Todo | `attempt` / `maxAttempts` | `todo.updated {todos,revision}` | 提醒帧字段为语义补项；`todo_auto_clear` 用 `reason:"auto_clear"` |
| 模型 | `model_changed` | `session.info` | 客户端把 `session.info` 投影为 `model_changed` |
| 推理档 | `thinkingLevel` / `configured` / `resolved` | `session.info.reasoning_effort` | `thinkingLevel`↔`reasoning_effort`；`configured/resolved` 为补项 |
| 目录 | `available_commands_update.commands` | `commands.changed`（信号，无数组） | 收到信号后重取 `commands.catalog` 并投影为 `AvailableSlashCommand[]` |
| 命令输出 | `command_output.text` | `command.output.text` | 直传 |
| 子代理 | `id` / `index` / `status` | `subagent.*.subagent_id` / `task_index` / `status` | 重命名；`aborted`→`interrupted` |
| 语音 | `live_phase.phase`、`captureId` | `voice.status.state`、`capture_id` | `phase`↔`state`；`captureId`→`capture_id` |
| 扩展 UI | `extension_ui_request.method` | 每用途一个 JSON-RPC 方法 | `method` 选择请求种类；`id`↔`id`；`questions[].id`→`qid`、`options`→`choices` |
| ask 应答 | `answers[]` | `clarify.answers{}` | 数组↔map；`selected_options`+`custom_input` 走 `answer_details`（A.12） |
| 取消 | `cancelled:"true"` | `request.cancel` | 映射为 `{id,error:{code:4404}}` 或统一 `result.cancelled` |
| Host | `host_tool_call` / `host_uri_request` | 无 | N 契约；能力未广告时客户端本地拒绝，不发请求 |

契约**只补语义**：上表“补项”指 Hermes 确实没有的数据，新增字段使用 Hermes 原生命名（可为 `None`，
producer 未落地时保持空值）。

## A.0.3 复核结论（2026-11-01）

- **覆盖**：omp 侧基线是 `wire/events.ts` 的 33 个 `AgentSessionEvent`，加上 `frames.ts` 的
  `ready`、`prompt_result`、`session_settled`、`extension_ui_request`、host 请求/应答与
  `rpc_chunk`；本附录逐族覆盖，无未归类帧。
- **本次检查修正**：
  1. 回合组按已落地的 `turn.start`/`turn.end`/`prompt.result`/`session.settled` 重新分档
     （A.4.1、A.4.3；计数见 A.14）；
  2. `available_commands_update` 按实际实现改为 `commands.changed` 信号 + 客户端重取目录（A.8）；
  3. `command_output` 已有 `command.output`，改判 F；
  4. 明确 `AgentMessage` 快照由客户端合成，不由 Hermes 返回同义结构（A.4.2），与 A.0.1 第 3 条一致。

## A.1 两端已有的协议定义

| 侧 | 定义位置 | 生成物 | 覆盖 |
|---|---|---|---|
| omp-ui | `omp-ui/src/modes/rpc/wire/{commands,events,frames,state,content}.ts` | `wire/rpc-wire.schema.json`、`wire/rpc-wire.generated.ts` | 74 命令、33 个 `AgentSessionEvent`、22 类其它通知、12 种 `extension_ui_request`、4 种 host 请求、4 种入站应答、1 种传输分片 `rpc_chunk` |
| Hermes | `tui_gateway/contracts/*.py`（Pydantic，单一真源） | `omp-ui/src/modes/rpc/gateway-contract.generated.ts`、`gateway-contract.openrpc.json` | `METHODS` 257 + `CANONICAL_METHODS` 68（42 个 canonical-only，合计 299 个方法名）、16 server→client 请求、79 通知 |

两侧都是**机器可校验**的；差异在**信封**与**字段模型**，不在“有没有定义”。

## A.2 信封映射

### A.2.1 通知（server → client 事件）

omp 把帧的判别字段放在顶层 `type`，其余字段平铺：

```json
{"type":"message_update","message":{...},"assistantMessageEvent":{...},"messageId":"..."}
```

Hermes 是 JSON-RPC 2.0 通知，判别字段在 `params.type`，业务数据在 `params.payload`：

```json
{"jsonrpc":"2.0","method":"event","params":{
  "type":"message.delta","session_id":"<sid>","payload":{"text":"...","turn_id":"...","segment":0,"offset":12},
  "seq":41,"replay_epoch":"..."}}
```

| omp 位置 | Hermes 位置 | 处理 |
|---|---|---|
| `type` | `params.type` | 取 `params.type`；omp 客户端需加一层解包（别名） |
| 其余业务字段 | `params.payload.*` | 解包 `payload`（Hermes 的所有通知都把业务对象放在 `params.payload`，无 payload 的通知省略该键） |
| — | `params.session_id` | **新增到 omp 帧模型**：会话级事件必须带；omp 现在只有连接级会话，无多会话字段 |
| — | `params.seq` | **新增**：每会话单调序号，用于断线重放去重 |
| — | `gateway.ready.replay_epoch` / `session.events.since.epoch` | **新增**：后端重启检测；实时帧不带该键，客户端比较两处的 epoch 后再决定是否丢弃本地 `seq` 水位 |
| — | 重放行（`session.events.since`）的 `authority_epoch` / `execution_generation` / `admission_id` | **新增（可选）**：authority 会话的 CAS/准入关联；实时帧不保证携带 |

**结论：P。** 需要客户端 `unwrapEvent()`：`type = params.type`、业务对象 = `params.payload`，并把
`session_id/seq/replay_epoch` 提升为帧元数据。omp 的 `set_event_filter` 语义可映射到 Hermes
`session.events.since` + 客户端过滤（见附录 B 的 `set_event_filter`）。

### A.2.2 服务端请求（server → client request）

omp 的服务端请求同样是 `type` 判别、无响应信封关联：

```json
{"type":"extension_ui_request","id":"req-1","method":"ask","questions":[...]}
```

Hermes 用 JSON-RPC 请求 id 关联：

```json
{"jsonrpc":"2.0","id":"srq-1a2b3c4d5e6f","method":"clarify",
 "params":{"session_id":"<sid>","questions":[{"qid":"q1","question":"...","choices":[...],"multi_select":false}]}}
```

客户端应答：omp `{"type":"extension_ui_response","id":"req-1","answers":[...]}`；Hermes
`{"jsonrpc":"2.0","id":"srq-...","result":{"answers":{"q1":"..."}}}`（或 `request.answer` 方法）。

| omp 字段 | Hermes | 处理 |
|---|---|---|
| `type`（固定 `extension_ui_request`/`host_tool_call`/…） | 请求 `method` | 用 `method` 判别；omp 的 `type` 被请求类别取代 |
| `id` | 请求 `id` + 应答 `id` | 字段语义一致（字符串关联），**别名** |
| `method`（`select`/`confirm`/…） | 无（由 `method` 本身承载） | omp 的二级 `method` 需要折叠进 Hermes 方法名（见 A.12） |
| — | `params.session_id` | **新增**到 omp 请求处理（无此字段时按当前会话） |

**结论：P。** 关联语义一致（id ↔ id），但判别方式与 `session_id` 不同。

### A.2.3 传输分片帧 `rpc_chunk`（仅 server → client）

omp 的 JSONL 单帧上限 1 MiB；逻辑帧更大时，服务端把它拆成 `rpc_chunk` 序列，客户端重组
（上限 64 MiB）。这是**传输层**帧，不属于通知/请求语义：

```json
{"type":"rpc_chunk","chunkId":"rpc-1","index":0,"count":4,"byteLength":900000,"data":"<base64>"}
```

| omp 字段 | 类型 | 含义 |
|---|---|---|
| `type` | `"rpc_chunk"` | 判别 |
| `chunkId` | string | 同一逻辑帧的所有分片共享；服务端每次 `rpc-<n>` |
| `index` / `count` | int | 0 基序号 / 分片总数 |
| `byteLength` | int | 重组后逻辑帧的 UTF-8 字节数 |
| `data` | string | base64 分片（每片 256 KiB 原始字节） |

Hermes 每个 JSON-RPC 消息就是一个 WS text frame 或 stdio 行，**没有分片**；超大结果由各方法自己
裁剪（`session.export` 的 content、`bubble` 截断、图片降级为占位）。对齐项：

- 状态 **N**；若 omp 客户端沿用现有 `rpc-frame.ts`，只需在 `gateway-client.ts` 里把 Hermes 帧视为
  “已重组”的单帧，永远不产生 `rpc_chunk`。
- 需要上限协商时用 A.3.1 的 `ready.max_frame_bytes` / `max_reassembled_frame_bytes`；Hermes 侧
  建议值分别取 1 MiB / 64 MiB（与 omp 相同），或声明 `0` 表示“不分片、无上限”。
- 反方向（client → server）omp 不分片：命令是单个 JSON 行；Hermes 也不分片，故附录 B 无对应项。

## A.3 连接、生命周期与杂项

### A.3.1 `ready` ↔ `gateway.ready`

| omp `ReadyEvent` | Hermes `GatewayReadyPayload` | 处理 |
|---|---|---|
| `protocolVersion?` | — | **新增** `protocol_version` |
| `supportedProtocolVersions?` | — | **新增** `supported_protocol_versions` |
| `maxFrameBytes?` | — | **新增** `max_frame_bytes` |
| `maxReassembledFrameBytes?` | — | **新增** `max_reassembled_frame_bytes` |
| — | `skin`（`SkinPayload`） | Hermes 多出；omp 客户端忽略或映射主题 |
| — | `change_events` / `replay_epoch` / `heartbeat` / `session_authority` | Hermes 多出；`replay_epoch` 建议进 omp 帧元数据 |

建议 Hermes payload 扩展（只加字段，不改现有）：

```json
{"skin":{...},"change_events":true,"replay_epoch":"r-...","heartbeat":true,"session_authority":true,
 "protocol_version":1,"supported_protocol_versions":[1,2],
 "max_frame_bytes":1048576,"max_reassembled_frame_bytes":67108864}
```

**结论：P。** 事件存在；omp 的 4 个传输字段需新增。

### A.3.2 错误与提示

| omp 帧 | Hermes | 状态 | 字段处理 |
|---|---|---|---|
| `extension_error {extensionPath,event,error}` | `error {message}` | P | `error`→`message`；**新增** `extension_path`、`event` |
| `rpc_frame_error {originalType?,error}` | `error {message}` | P | `error`→`message`；**新增** `original_type`（帧超限被丢弃时） |
| `notice {level,message,source?}` | `notification.show {text,level,kind,ttl_ms?,key?,id?,detail?}` | P | `message`→`text` 别名；`level` 枚举 `info/warning/error` → Hermes `info/warn/error/success`，`warning`→`warn`；**新增** `source` |
| `agent_end.telemetry` / `coverage` | `message.complete.usage` / `error_surface` | P | 对象字段 Hermes 侧为强类型子集；omp 侧保持 open record |

Hermes `notice {message}` 只用于能力刷新；通用提示走 `notification.show`，因此上表以
`notification.show` 为准。

## A.4 回合与消息流

### A.4.1 回合边界

| omp 帧 | Hermes | 状态 | 说明 |
|---|---|---|---|
| `agent_start {resumedToolCallIds?}` | `turn.start {resumed_tool_call_ids?,message_id?}` | P | 契约已存在（A.15）；Hermes 从不恢复中途工具调用，`resumed_tool_call_ids` 恒为空——语义不等，不是别名问题 |
| `agent_end {messages,messageCount?,isTerminal?,yielded?,awaitingAsyncWork?,telemetry?,coverage?}` | `turn.end {status,message?,tool_results,message_id?}` | P | `messages` 数组与 `isTerminal`/`yielded`/`awaitingAsyncWork`/`telemetry`/`coverage` 仍缺（语义补项）；终态可由 `turn.end` + `prompt.result` + `session.settled` 派生 |
| `turn_start {}` | `turn.start` | F | 已落地 |
| `turn_end {message,toolResults}` | `turn.end` | F | 契约字段同义且已落地；但实际 emitter 目前不填 `message`、`tool_results` 为空（A.15），客户端用 `message.complete` 重建——F 指契约对应，不代表数据已填充 |

契约（已落地，见 A.15）：

```text
event("turn.start", TurnStartPayload)
  # {resumed_tool_call_ids?: str[], message_id?: str}      # resumed_* 恒空
event("turn.end", TurnEndPayload)
  # {status?: "complete"|"error"|"interrupted", message?: JsonValue,
  #  tool_results: JsonValue[], message_id?: str}
```

`agent_start`/`agent_end` 与 `turn_start`/`turn_end` 在 omp 是两层（run / turn）；Hermes 用
`turn.start`/`turn.end` 承载 `turn_*`，`agent_*` 独有的 run 级字段（`isTerminal`/`yielded`/
`awaitingAsyncWork`）在 A.14 列为语义补项，由客户端从 `prompt.result`/`session.settled` 派生。

### A.4.2 消息生命周期

| omp 帧 | Hermes | 状态 |
|---|---|---|
| `message_start {message,messageId?}` | `message.start {display_kind?,display_text?,notification_category?,text?,turn_id?}` | P |
| `message_update {message,assistantMessageEvent,messageId?}` | `message.delta` / `reasoning.delta` / `reasoning.available` / `message.interim` | P |
| `message_end {message,messageId?}` | `message.complete {text,usage,status,...}` | P |

字段映射（`message_start`）：

| omp | 类型 | Hermes | 处理 |
|---|---|---|---|
| `message` | `AgentMessage` | — | **客户端合成**（A.0.1 第 3 条）：用 `message_id` 建空 `AgentMessage` 壳，由 delta / `message.block_end` 填充；Hermes 不返回同义快照 |
| `messageId` | string | `message_id`（已落地） | 别名；注意 `turn_id` 是**回合**身份，不能替代消息身份 |

字段映射（`message_update`，omp 的 `assistantMessageEvent` 是判别联合）：

| omp `assistantMessageEvent.type` | 语义 | Hermes | 处理 |
|---|---|---|---|
| `start` | 助手消息开始 | `message.start` | 字段映射 |
| `text_start` | 文本块开始 | `message.block_start` | 已落地；`kind:"text"`、`content_index`、`message_id?` |
| `text_delta {contentIndex,delta,partial}` | 文本增量 | `message.delta {text,content_index,message_id,...}` | `delta`→`text`、`contentIndex`→`content_index`（已落地）；`partial` 取 `message.complete.partial` |
| `text_end {contentIndex,content}` | 文本块结束 | `message.block_end` | 已落地；`kind:"text"`、`content_index`、`content`、`message_id?` |
| `thinking_start/delta/end` | 推理流 | `reasoning.delta {text,rendered?,verbose?}` / `reasoning.available {text}` | `delta`→`text` 已落地；`message_id`/`content_index` 仍是语义补项，客户端先用当前消息 id + 单块索引本地维护；`thinking_end`→`reasoning.available` |
| `toolcall_start/delta/end` | 工具参数流 | `tool.generating {name}` / `tool.start` | `toolcall_delta` 无对应；N 契约：`event("tool.args_delta", {tool_id, name, delta, index?})`（未落地） |
| `image_end {contentIndex,content,partial}` | 图片块 | `message.block_end`（`kind` 字段已有） | `kind:"image"` 无 producer（当前只发 text 块）；定义保留 |
| `done {reason,message}` | 助手消息结束 | `message.complete {status,stop_reason,message_id,partial}` | `reason`→`stop_reason` 直传（同一 omp 词表）；`status` 另按 `complete/error/interrupted` 映射；`message` 快照由客户端累积 |
| `error {reason,error}` | 助手消息失败 | `message.complete {status:"error",error,error_surface}` | P；`message_id` 已落地；`message` 快照由客户端合成 |

字段映射（`message_end` → `message.complete`）：

| omp | Hermes | 处理 |
|---|---|---|
| `message` | `text`（仅正文） | 客户端累积成 `AgentMessage` 快照（A.0.1 第 3 条）；`usage` 从 `message.usage` 提升为帧级 `usage` |
| `messageId` | `message_id`（已落地） | 别名 |
| `message.usage` | `usage`（`Usage`） | 客户端映射：omp `Usage{input,output,cacheRead,cacheWrite,totalTokens,contextTokens?,premiumRequests?,reasoningTokens?,orchestration?,cttl?,server?,credits?,cost:{...}}` → Hermes `Usage{model,input,output,reasoning,prompt,completion,total,calls,...,cache_read,cache_write,...,cost_usd}`；`totalTokens`→`total`、`reasoningTokens`→`reasoning`、`cacheRead/Write`→`cache_read/cache_write`、`cost` 对象只可得总价（`cost_usd`）；`orchestration/cttl/server/credits` 无来源，客户端置空并保留字段 |
| `message.stopReason` | `stop_reason`（omp 词表，已落地）/ `status`（Hermes 原生） | `stopReason`→`stop_reason` **直传**（`stop/length/toolUse/error/aborted` 同词表，已由 Hermes 单向投影）；只有 `status` 需要 `complete/error/interrupted` 映射，二者不混用 |

**结论：P（关键字段已落地，剩余为语义补项 + 客户端合成）。** A.15 已落地 `message_id`、
`content_index`、`partial`、`stop_reason` 与 `message.block_start`/`message.block_end`。仍缺的是
思考块的 `content_index`、`image` 块与工具参数流；`AgentMessage` 快照和 `assistantMessageEvent`
的嵌套结构一律由客户端用 `message.start` / `message.delta` / `message.block_*` / `message.complete`
重建，不在 Hermes 复制 omp 的消息模型。

### A.4.3 `prompt_result` 与 `session_settled`

| omp | Hermes | 状态 | 说明 |
|---|---|---|---|
| `prompt_result {id?,agentInvoked,status,error?,sessionSettled}` | `prompt.result {admission_id,agent_invoked,status,error?,session_settled}` | P | 已落地；`id`→`admission_id` 客户端别名；`session_settled` 恒 false，真正的 settle 由独立 `session.settled` 表达（语义拆分） |
| `session_settled {}` | `session.settled {idle_since?}` | F | 已落地 |

契约（已落地，见 A.15）：

```text
event("prompt.result", PromptResultPayload)
  # {admission_id, agent_invoked: bool, status: "completed"|"aborted"|"error",
  #  error?: {message, provider?, model?, http_status?, retryable, code?},
  #  session_settled: bool}
event("session.settled", SessionSettledPayload)   # {idle_since?: float}
```

`id` 在 omp 是**命令请求 id**；Hermes 的准入身份是 `admission_id`，二者是**别名**（客户端把
`prompt.submit` 的 `id` 与回执 `admission_id` 建映射）。

## A.5 工具执行

| omp | Hermes | 状态 |
|---|---|---|
| `tool_execution_start {toolCallId,toolName,args?,intent?}` | `tool.start {tool_id,name,context?,args?,args_text?,preview?,labels?}` | P |
| `tool_execution_update {toolCallId,toolName,args?,partialResult?}` | `tool.progress {tool_id,name,progress,total?,percent?,message?}` | P |
| `tool_stream_update {toolCallId,toolName,update?}` | 无 | N |
| `tool_execution_end {toolCallId,toolName,result?,isError?}` | `tool.complete {tool_id,name,args?,duration_s?,result,summary?,result_text?,inline_diff?,todos?,revision?,labels?}` | P |

字段处理：

| omp | Hermes | 处理 |
|---|---|---|
| `toolCallId` | `tool_id` | **别名** |
| `toolName` | `name` | **别名** |
| `args` | `args` | 一致（JSON 对象） |
| `intent` | — | **新增** `intent`（工具调用的人类可读意图；omp 已渲染） |
| `partialResult` | `progress/total/percent/message` | 语义不同：omp 是**部分结果对象**，Hermes 是 MCP 进度数值。**新增** `partial_result: JsonValue` 到 `tool.progress`，或新增事件 `tool.update` |
| `update` | — | **新增**事件 `tool.stream {tool_id,name,update}` |
| `result` | `result` | 一致（`JsonValue`） |
| `isError` | — | **新增** `is_error: bool` 到 `tool.complete` |

建议（若不让客户端合成）：

```text
event("tool.update", ToolUpdatePayload)  # {tool_id, name, args?, partial_result?}
event("tool.stream", ToolStreamPayload)  # {tool_id, name, update?}
```

**结论：P。** 三条既有事件可承载主体；`is_error`、`partial_result`、`intent`、`tool_stream_update`
需补。

## A.6 压缩、重试、缓存预热、模型回退

omp 的这四组帧在 Hermes **都没有事件**；现有能力是 `status.update {kind,text}`（`kind` 可为
`compacting`）与 `session.compress` 的同步结果。全部判 **N**，按下面的映射新增契约：

| omp 帧 | Hermes 契约事件 | 字段映射 |
|---|---|---|
| `auto_compaction_start` / `manual_compaction_start` | `compaction.start` | `reason`/`action` 直传；manual 用 `manual:true` + `mode` |
| `auto_compaction_end` / `manual_compaction_end` | `compaction.end` | `aborted`/`will_retry`/`error_message`/`skipped`；manual 用 `manual:true` |
| `auto_retry_start` | `retry.start` | `attempt`/`max_attempts`/`delay_ms`/`error_message` |
| `retry_fallback_applied` / `retry_fallback_succeeded` | 同名事件（`retry.fallback_applied` / `retry.fallback_succeeded`） | 直传 |
| `auto_retry_end` | `retry.end` | `success`/`attempt`/`final_error`/`retry_errors` |
| `cache_warming_start` / `cache_warming_end` | `cache_warming.start` / `cache_warming.end` | `phase`/`provider`/`model`；end 带 `outcome`/`usage` |

契约定义：

```text
event("compaction.start", CompactionStartPayload)
  # {reason: "threshold"|"overflow"|"idle"|"incomplete",
  #  action: "context-full"|"remote"|"handoff"|"shake"|"snapcompact",
  #  manual?: bool, mode?: string}          # manual_compaction_start 用 manual:true + mode
event("compaction.end", CompactionEndPayload)
  # {action, result?: CompactionResult, aborted: bool, will_retry: bool,
  #  error_message?: string, skipped?: bool, manual?: bool}
event("retry.start", RetryStartPayload)
  # {attempt, max_attempts, delay_ms, error_message, error_id?}
event("retry.end", RetryEndPayload)
  # {success, attempt, final_error?, retry_errors: JsonValue[]}
event("retry.fallback_applied", RetryFallbackAppliedPayload)
  # {from, to, role, reason?}
event("retry.fallback_succeeded", RetryFallbackSucceededPayload)
  # {model, role}
event("cache_warming.start", CacheWarmingStartPayload)
  # {phase: "streaming"|"idle", provider, model}
event("cache_warming.end", CacheWarmingEndPayload)
  # {phase, provider, model, outcome: "hit"|"miss"|"error"|"aborted",
  #  usage?: Usage, warming_stop_reason?: string}
```

`CompactionResult` 需两侧对齐（omp 字段）：

```json
{"summary":"...","shortSummary":"...","firstKeptEntryId":"...","tokensBefore":0,
 "details":null,"preserveData":{},"providerReplayThroughEntryId":"..."}
```

Hermes `session.compress` 的结果字段是 `status/removed/before_messages/after_messages/
before_tokens/after_tokens/summary{headline,token_line,...}/usage/...`。若事件要直接携带 omp 形状，
需在 payload 里**新增** `summary/ShortSummary/first_kept_entry_id/tokens_before` 等键（或客户端从
同步结果构造）。建议事件携带 Hermes 原生字段，omp 客户端做一次映射，避免在 Hermes 复制 omp 的
持久化结构。

## A.7 队列、Goal、Todo

### A.7.1 `queue_update` → 无（N）

omp 的队列快照 `{steering:string[],followUp:string[],liveSteered:number}` 无 Hermes 事件；
Hermes 只在 `session.resume/activate` 的 `pending[]` 里给准入行（`CanonicalPendingAdmission`：
`admission_id/input_id/text/sequence/status/controls[]/steering?`）。建议：

```text
event("queue.updated", QueueUpdatedPayload)
  # {pending: [{admission_id, input_id, text, sequence, status, steering?}],
  #  steering: string[], follow_up: string[], live_steered: int}
```

`live_steered` 在 Hermes 无等价概念（omp 指已送入流式回答的 steering 条目数）；若对齐，需在
准入行上**新增** `delivered_to_turn: bool` 并由队列事件聚合。

### A.7.2 `goal_updated` ↔ `session.control.update`（P）

| omp | Hermes | 处理 |
|---|---|---|
| `goal`（`Goal\|null`：`id,objective,status,tokenBudget?,tokensUsed,timeUsedSeconds,createdAt,updatedAt`） | `control.goal`（`GoalSnapshot{title,status,turns_used,max_turns,contract,subgoals,gates,created_at,updated_at,paused_reason,last_verdict,last_reason,wait_barrier}`） | 结构不等价：`objective`→`title` 近似（omp 用 objective，Hermes 用 title）；`tokenBudget/tokensUsed/timeUsedSeconds` **新增**到 `GoalSnapshot`；`status` 枚举需映射（omp `active/paused/budget-limited/complete/dropped` ↔ Hermes 字符串） |
| `state?`（`GoalModeState{enabled,mode:"active"/"exiting",reason?,goal}`） | — | **新增** `enabled`/`mode`/`reason` 到 `SessionControlSnapshot`，或在事件 payload 外再包一层 |

### A.7.3 `todo_reminder` / `todo_auto_clear` ↔ `todo.updated`（P）

omp `todo_reminder {todos:TodoItem[],attempt,maxAttempts}`；Hermes `todo.updated {todos,revision}`。

| omp | Hermes | 处理 |
|---|---|---|
| `todos` | `todos`（`list[JsonValue]`，`_normalize_todo_state` 快照） | 一致（open JSON） |
| `attempt` / `maxAttempts` | — | **新增** `attempt`、`max_attempts`（提醒帧） |
| `todo_auto_clear` | 无 | **新增** `reason:"auto_clear"` 或独立事件 |

`TodoItem` 的 omp 字段为 `content,status,blocker?,details?,notes?[]`；Hermes 未声明形状
（`JsonValue`），需在 `todo.updated` 的 `todos` 上**新增**该对象定义，否则无法做字段级校验。

## A.8 模型 / 配置 / 命令目录

| omp 帧 | Hermes | 状态 | 处理 |
|---|---|---|---|
| `model_changed {}` | `session.info`（`SessionLiveInfo`） | P | 事件名别名：`model_changed`→`session.info`；omp 无 payload，Hermes 带全量 info |
| `thinking_level_changed {thinkingLevel?,configured?,resolved?}` | `session.info`（`reasoning_effort` / `reasoning_effort_wire`） | P | `thinkingLevel`↔`reasoning_effort` 别名；`configured/resolved` **新增**（omp 区分选择器与实际档位） |
| `config_update {model?,thinkingLevel?}` | `session.info` | P | 字段别名；Hermes `session.info.model` 是 `str`，omp 是 `ModelInfo` 对象 |
| `config_warnings_changed {}` | `session.info`（`SessionLiveInfo.credential_warning`；canonical 快照的 `info.config_warning`） | P | 无独立事件；**新增** `warnings: string[]` 到 `session.info` 或事件 `config.warnings` |
| `session_info_update {title?,sessionId}` | `session.title {session_id,title}` | **F** | `sessionId`→`session_id`、`title`→`title`；字段一一对应 |
| `available_commands_update {commands}` | `commands.changed {}`（信号，无数组；`commands.catalog` 为拉取式） | P | 已落地 `commands.changed {commands?: JsonValue[]}`，payload 恒不带数组；客户端收到信号后重取 `commands.catalog`，把 `entries` 投影为 `AvailableSlashCommand[]`（A.0.2） |
| `command_output {text}` | `command.output {text,name?}` | **F** | 已落地；`text` 同义，`name?` 为 Hermes 多出字段 |

`AvailableSlashCommand`（omp）：`{name,aliases?,description?,input?{hint?},subcommands?[{name,description?,usage?}],source:"builtin"|"skill"|"extension"|"custom"|"mcp_prompt"|"file"}`。
Hermes `CommandsCatalogResult` 是 `pairs/categories/skills/plugin_commands` 的**分组形状**；
字段级无法直接对应；`commands.catalog` 已投影 `entries`（B.8.1，已落地），`commands.changed` 只作
信号，客户端重取目录后复用该数组，不在事件里复制命令列表。

## A.9 子代理

| omp | Hermes | 状态 |
|---|---|---|
| `subagent_lifecycle {payload}` | `subagent.spawn_requested` / `subagent.start` / `subagent.complete` | P |
| `subagent_progress {payload}` | `subagent.progress` / `subagent.thinking` / `subagent.tool` | P |
| `subagent_event {payload:{id,event}}` | 无（Hermes 不复用子会话原始事件） | N |

omp `SubagentLifecyclePayload`：`{id,agent,agentSource,description?,status,started|completed|failed|aborted,sessionFile?,parentToolCallId?,index,detached?}`。
Hermes `SubagentEventPayload`：`{goal,task_count,task_index,subagent_id?,parent_id?,child_session_id?,delegation_id?,depth?,model?,tool_count?,toolsets?,input_tokens?,output_tokens?,reasoning_tokens?,api_calls?,files_read?,files_written?,output_tail?,tool_name?,text?,status?,summary?,duration_seconds?,tool_preview?}`。

| omp | Hermes | 处理 |
|---|---|---|
| `id` | `subagent_id` | 别名 |
| `agent` | — | **新增** `agent`（omp 的命名 agent 名） |
| `agentSource` | — | **新增** `agent_source`（`bundled/user/project`） |
| `description` / `task` / `assignment` | `goal`（近似） | **新增** `description`、`task`、`assignment`；`goal` 不能替代三者 |
| `status`：`started/completed/failed/aborted` | `status`：`queued/running/completed/failed/error/timeout/interrupted` | 需枚举映射；omp 的 `aborted`→`interrupted` |
| `sessionFile` | `child_session_id`（不同） | **新增** `session_file` |
| `parentToolCallId` | — | **新增** `parent_tool_call_id` |
| `index` | `task_index` | 别名（omp index 从 0 计数，需核对基数） |
| `detached` | — | **新增** `detached` |
| `progress`（`AgentProgress` 原样） | `tool_count/toolsets/files_read/...` 的扁平投影 | 结构不等价；**新增** `progress: JsonValue` 或客户端重建 |

`subagent_event`（子代理自身的 `AgentSessionEvent` 转发）在 Hermes 没有对应协议；要么**新增**
`subagent.event {subagent_id, event: JsonValue}`，要么明确不支持（omp 的 `set_subagent_subscription`
level=`events` 会降级为 `progress`）。

## A.10 Live voice / Dictation

| omp | Hermes | 状态 | 处理 |
|---|---|---|---|
| `live_phase {phase}` | `voice.status {state}` | P | `phase:connecting/listening/working/speaking/muted/error` ↔ `state` 字符串；需枚举映射；**新增** `phase` |
| `live_levels {input,output}` | 无 | N | **新增** `voice.levels {input,output}`（0–1 RMS） |
| `live_transcript {role,turn,text,final}` | `voice.transcript {text,stop_phrase?,typed?,no_speech_limit?}` / `voice.partial {text}` | P | `role/turn/final` **新增**；Hermes 用两条事件区分 partial/final，omp 用 `final` 布尔 |
| `live_end {error?}` | `voice.interrupted`（无 payload） | P | **新增** `voice.ended {error?}`（或扩 `voice.interrupted` 为 `{reason?}`；`live_end` 是“结束”，不一定是打断） |
| `dictation_state {captureId,state}` | `voice.status {state}` | P | **新增** `capture_id`；`state:starting/recording/transcribing` 需并入 `voice.status` |
| `dictation_transcript {captureId,text}` | `voice.partial {text}` | P | **新增** `capture_id`；omp 是“累计预览”，Hermes 是“到目前为止的文本”，语义接近 |
| `dictation_end {captureId,text,error?}` | `voice.transcript {text}` | P | **新增** `capture_id`、`error`；`voice.transcript.stop_phrase/typed` 对 omp 无意义 |

`live_start/live_connect/live_stop/live_mute`（业务侧，见附录 B）与上述事件成对；Hermes 没有 WebRTC
live 会话，故 live 事件组整体是 P/N 混合。

## A.11 BTW / IRC / Advisor / TTSR

| omp | Hermes | 状态 | 处理 |
|---|---|---|---|
| `btw_delta {recordId,delta}` | 无 | N | **新增** `btw.delta {record_id,delta}` |
| `btw_record {record}` | `btw.complete {task_id,text,question?}` | P | 只有终态；`BtwHistoryRecord` 的 `id/leafId/followUps/status/createdAt/updatedAt/error/images` **新增**；建议 **新增** `btw.record {record}` 并在每次生命周期变化时广播 |
| `irc_message {message}` | 无（`message.adviser {id,text,severity,label,display_kind}` 是另一语义） | N | **新增** `custom.message {message: AgentMessage}`，或明确不支持 |
| `advisor_cost_changed {}` | 无 | N | **新增** `advisor.cost_changed {cost?}`；Hermes 只有 `message.adviser` |
| `advisor_yielded {}` | 无 | N | **新增** `advisor.yielded {}` |
| `ttsr_triggered {rules}` | 无 | N | **新增** `ttsr.triggered {rules:{name,path,content}[]}` |

## A.12 服务端请求：扩展 UI

omp 的 `extension_ui_request` 是一个**统一方法**，用 `method` 区分 12 种卡片；Hermes 是**每个用途一个
JSON-RPC 方法**。逐项：

| omp `method` | Hermes server request | 状态 | 字段处理 |
|---|---|---|---|
| `ask` | `clarify` | P | `questions[]`→`questions[]`：`id`→`qid`、`question`→`question`、`options[]`→`choices[]`（取 label）、`multi`→`multi_select`；**新增** `header`、`recommended`、`options[].description/preview`、`timeout` |
| `select` | 无 | N | **新增** `ui.select {title,options,option_details?,timeout?}` → `{value}` |
| `confirm` | 无 | N | **新增** `ui.confirm {title,message,timeout?}` → `{confirmed}` |
| `input` | `secret`（仅机密） | P/N | 普通输入无对应；**新增** `ui.input {title,placeholder?,timeout?,secret?}` → `{value}` |
| `editor` | 无 | N | **新增** `ui.editor {title,prefill?,prompt_style?}` → `{value}` |
| `cancel` | `request.cancel` 事件 | P | `targetId`→`id`；**新增** `method`（被撤销请求的方法）；Hermes 的 `reason` 枚举为 timeout/interrupted/shutdown/resolved/session_closed |
| `notify` | `notification.show` | P | `message`→`text`、`notifyType`→`level`（`info/warning/error` → `info/warn/error`）；Hermes `kind` **新增**为 `"agent"` |
| `setStatus` | `status.update {kind,text}` | P | `statusText`→`text`；**新增** `status_key`（omp 用 key 覆盖/清除，Hermes 按 kind 追加） |
| `setWidget` | 无 | N | **新增** `ui.widget {widget_key,widget_lines?,widget_placement?}`（或独立事件 `ui.widget`） |
| `setTitle` | `session.title` 事件 | P | `title` 一致；请求 id **新增**为响应关联（Hermes 是事件不是请求） |
| `set_editor_text` | 无 | N | **新增** `ui.editor_text {text}` |
| `open_url` | 无（桌面用 `preview.open` / `billing.step_up.verification`） | N | **新增** `ui.open_url {url,launch_url?,instructions?}` |

omp 的应答 `extension_ui_response` 变体：

| omp 应答 | Hermes | 处理 |
|---|---|---|
| `{id,value}`（select/input/editor） | `{id,result:{value}}` | 别名（值一致） |
| `{id,confirmed}`（confirm） | 无（若新增 `ui.confirm`，结果 `{confirmed}`） | 新增方法后一致 |
| `{id,cancelled:"true",timedOut?}` | `{id,error:{code:4404?}}` 或 `{id,result:{cancelled:true}}` | **新增**统一取消语义：`result.cancelled:true` + `timed_out?` |
| `{id,answers:AskAnswer[]}` | `clarify` 的 `{answers:{qid:string or null}}` | 结构不同：数组 → map；`selectedOptions[]`+`customInput?` → 单个字符串（omp 需要可逆编码，建议 Hermes **新增** `answer_details: [{qid,selected_options,custom_input}]`） |

## A.13 Host tools / Host URI（服务端请求 + 入站应答）

Hermes 的工具全部在服务端执行，没有“回调宿主执行工具”的子协议；omp 的 host tools 是给宿主
（TUI/扩展）用的。全部 **N**，建议新增（保持 omp 字段名转 snake_case）：

```text
server_request("host.tool.call",  params={tool_call_id, tool_name, arguments}, result=HostToolResult)
server_request("host.tool.cancel", params={target_id}, result={cancelled: bool})
event("host.tool.update",  {id, partial_result})      # 服务端 → 客户端流式部分结果
# 客户端 → 服务端完成：
{"jsonrpc":"2.0","id":"<call id>","result":{"content":[...],"details":?,"is_error":?,"useless":?,"provider_metadata":?}}

server_request("host.uri.request", params={operation:"read"|"write", url, content?}, result=HostUriResult)
server_request("host.uri.cancel",  params={target_id}, result={cancelled: bool})
```

omp 的 `HostToolResultPayload`：`{content:UserContent[],details?,isError?,useless?,providerMetadata?}`；
`HostUriResult`：`{content?,contentType?:"text/markdown"|"application/json"|"text/plain",notes?,immutable?,isError?,error?}`。
新增契约应逐字段照搬，避免 omp 侧再转一层。

## A.14 汇总与改动清单

| 组 | 帧数 | F | P | N |
|---|---:|---:|---:|---:|
| 传输分片（`rpc_chunk`） | 1 | 0 | 0 | 1 |
| 信封/连接 | 4 | 0 | 4 | 0 |
| 回合与消息 | 9 | 3 | 6 | 0 |
| 工具 | 4 | 0 | 3 | 1 |
| 压缩/重试/缓存 | 8 | 0 | 0 | 8 |
| 队列/Goal/Todo | 4 | 0 | 2 | 2 |
| 模型/配置/目录 | 7 | 2 | 5 | 0 |
| 子代理 | 3 | 0 | 2 | 1 |
| Live/Dictation | 7 | 0 | 6 | 1 |
| BTW/IRC/Advisor/TTSR | 6 | 0 | 1 | 5 |
| 扩展 UI 请求 | 12 | 0 | 5 | 7 |
| Host tools/URI | 4 | 0 | 0 | 4 |

按 omp 的类型族计数（2026-11-01 复核后的分档，改动见 A.0.3）；`extension_ui_response` 与 host 的
`*_result` 入站帧不单列，其字段对齐见 A.12/A.13。每档都保留了完整 wire 定义，N 档不等于“无契约”。

**Hermes 侧需要新增的契约（按优先级；已落地项见 A.15）：**

1. **消息流补字段**（不改事件名）：已落地 `message_id`、`content_index`、`partial`、`stop_reason`、
   `message.block_start`/`block_end`；`message` 快照**不由 Hermes 提供**，由客户端合成（见 A.4.2）。
   仍缺：思考块的 `content_index`、`image` 块、工具参数流 `tool.args_delta`。
2. **回合原语**：`turn.start`/`turn.end`、`prompt.result`、`session.settled`——已落地。
3. **工具补字段**：`tool.complete.is_error` 已落地；`tool.progress.partial_result`、`tool.start.intent`
   与 `tool.update`/`tool.stream` 仍缺。
4. **压缩/重试/预热**：`compaction.*`、`retry.*`、`cache_warming.*`（8 个事件）契约已定义，无 producer。
5. **队列/目录/输出**：`queue.updated`、`commands.changed`（信号）、`command.output`——已落地。
6. **子代理补字段**：`agent/agent_source/description/task/assignment/session_file/parent_tool_call_id/detached`；
   可选 `subagent.event`。
7. **UI 请求**：`ui.select`/`ui.confirm`/`ui.input`/`ui.editor`/`ui.widget`/`ui.editor_text`/`ui.open_url`；
   `clarify` 补 `header/recommended/timeout/answer_details`。
8. **Host 子协议**：`host.tool.*`/`host.uri.*`（仅当面板需要宿主工具）。

每一组都应遵循 `tui_gateway/contracts` 的规则：`Payload` + `event(...)`、`extra="forbid"`、
`scripts/gen_gateway_contracts.py` 重新生成；新增事件必须同步进 `GatewayEventMap`（生成器自动完成）
并更新 `docs/omp-ui-panels.json` 的事件清单。

## A.15 实现状态（2026-10-31）

**已落地**（契约 + 有真实 emitter，`tests/tui_gateway/contracts/test_generated.py` 的
orphan-event 门通过）：

| 组 | 已实现 |
|---|---|
| 传输/连接 | `gateway.ready` / `client.capabilities` 的 `protocol_version`、`supported_protocol_versions`、`max_frame_bytes`、`max_reassembled_frame_bytes`（A.3.1） |
| 回合与消息 | `turn.start`、`turn.end`、`prompt.result`、`session.settled`；`message.block_start`、`message.block_end`；`message.start`/`message.delta`/`message.complete` 增加 `message_id`，delta 增加 `content_index`，complete 增加 `stop_reason` |
| 工具 | `tool.complete.is_error`（用 `agent.display._detect_tool_failure` 在发射点计算，不改变 4 参回调 ABI） |
| 队列 | `queue.updated`（TUI next-turn 队列与 authority `_publish_pending` 两路） |
| 命令 | `command.output`（`command.dispatch` / `slash.exec`）、`commands.changed`（skill 根变化时广播；客户端重取 `commands.catalog`） |

上表的回合/消息/队列帧在**两条 wire** 上都有 producer：`tui_gateway/prompt_turn.py`（stdio / legacy 会话）
与 authority（`session_authority._stamp_claim` 的 `turn.start`/`message.start`、
`run_turn_runner` 的 block 帧与 `message_id`、`session_settlement_recovery._publish_completion`
的 `turn.end`/`prompt.result`/`session.settled`）。authority 的 message identity 由
`epoch:generation` 决定，start/delta/complete 共用一个 id；`tool.complete.is_error` 在 authority
的 `run_turn_progress._tool_complete_payload` 里原本就有。

**仍是 N，且当前没有真实 producer**。这些项不能只加契约：`registry.assert_complete` 会把没有
emitter 的事件判为 orphan，而伪造数据会污染 wire。落地它们需要先补对应运行时能力：

| 项 | 缺什么 |
|---|---|
| `compaction.start/end`、`retry.start/end/fallback_*`、`cache_warming.start/end` | 结构化数据只存在于 `agent/compaction_events.py`（Relay/OTel + `post_compaction` plugin hook）与 `agent/turn_recovery.py` 的内部变量；需要网关可订阅的 in-process 回调（cache warming 功能本身不存在） |
| `subagent.*` 的 `agent`/`agent_source`/`description`/`task`/`assignment`/`session_file`/`parent_tool_call_id`/`detached`/`progress`、`subagent.event` | `delegate_task` 不接收命名 agent/描述/父 tool call id；子代理原始事件只进 live-transcript 文件，无网关转发路径 |
| `voice.levels`、`capture_id`、`voice.ended` | 录音器只有内部 int RMS（0–32767），无输出电平、无 capture id、无独立的结束/错误帧 |
| `btw.delta`、`btw.record`、`advisor.cost_changed`、`advisor.yielded`、`ttsr.triggered`、`custom.message` | 侧问是非流式一次性返回；adviser 成本只在 JSONL 汇总；TTSR/自定义消息在 Hermes 不存在 |
| `ui.select/confirm/input/editor/widget/editor_text/open_url` | 当前没有任何后端流程会发起这些请求；每个都需要一个真实调用者才能通过 orphan 门 |
| `host.tool.*`、`host.uri.*` | Hermes 全部工具在服务端执行，没有“请宿主执行工具”的子协议 |
