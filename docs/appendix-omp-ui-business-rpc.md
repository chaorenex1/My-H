# 附录 B：业务 RPC 协议对齐（omp-ui → Hermes，client → server）

> 附录 A 是实时 RPC（server → client 事件/请求）：`docs/appendix-omp-ui-realtime-rpc.md`。
>
> **基准**：omp-ui 端侧 `RpcCommand`（`omp-ui/src/modes/rpc/wire/commands.ts`，74 个命令）的
> **请求参数**与**成功 `data`**；以及 omp 的 `RpcResponse` 信封
> （`wire/frames.ts::RpcResponse`）。
> **对象**：Hermes `tui_gateway/contracts/*.py` 的 `Params`/`Result`（`METHODS` /
> `CANONICAL_METHODS`），按 `docs/omp-ui-panels-gateway-plan.md` §5 的口径分为 authority 直服与
> 进程内 sidecar 适配，本附录只关心**方法名与数据形状**。
> **只谈协议，不谈业务接入**。状态口径同附录 A：**F** 完整对应、**P** 部分对应（列新增/别名）、
> **N** 完全没有（给建议契约）。

## B.0 判定口径与基线

- **F**：omp 请求的每个字段都能在 Hermes 的 `Params` 中找到同义字段，omp `data` 的每个字段都能在
  `Result` 中找到同义字段（允许 snake_case 与多余字段）。
- **P**：存在功能对应的方法，但至少一个 omp 字段缺失、枚举不等或结果结构不同；逐项给出
  **别名**或**新增字段**。
- **N**：Hermes 没有对应方法；给出建议新增方法及其 `params`/`result` 完整定义。

omp 的 74 个命令按 `commands.ts` 的 `name` 逐一核对，不按 omp 的 slash 命令名核对。

## B.0.1 硬约束：三档都要有契约定义，P 档别名在 omp-ui 转换

本附录与 `docs/omp-ui-panels-gateway-plan.md` §0.1 同源：

1. **交互不变**：omp 的命令表、控制器与选择器是契约；本附录只定义数据，不描述改 UI。
2. **F/P/N 三档都必须有定义**：
   - **F**：Hermes 方法已覆盖；只做命名/信封别名。
   - **P**：Hermes 有功能对应，但字段缺失、语义不等或方法名不同。必须同时给出**客户端映射规则**
     和 Hermes 需补的**语义**字段；不得只写“需对齐”。
   - **N**：Hermes 没有对应方法。必须给出完整 wire 定义（method + params/result + 错误码 +
     `session_id`/`profile` 作用域），**即使当前没有 handler**。未落地时由
     `runtime.describe.panels` 广告 `supported:false`，UI 保持原交互并标 `pending`/`unsupported`。
3. **P 档别名在 omp-ui 侧转换**：`type`→`method`、平铺参数→`params`、`result`→`data`、
   `error.message`→`error`、`error.data.reason`→`code`、字段改名、枚举映射与单位换算，全部由
   `omp-ui/src/modes/rpc/` 完成并在 B.0.2 登记。Hermes 契约保持原生命名，**不得**为对齐 omp
   命名新增**仅做字段改名**的同义方法（如另建一个只是重命名 `session.resume` 的 `get_state`）；
   `session.state`（B.3.2）因含无来源字段（`is_compacting` 等）而成立，不属于此列。
   本条**只约束别名**：`新增字段/方法`是 Hermes 的语义补项，不受本条限制；已落地的 omp 形状
   投影（`commands.catalog.entries`、`btw.history.records`）保留，规则只约束新增/变更。
4. **单向投影**：客户端不把 omp 的 UI 状态（面板选中项、草稿、轮换顺序）写回 Hermes 去改变渲染。
5. **禁止伪造**：N 档不得用本地假数据把面板标成 `ok`；能力缺失就以 `unsupported` 呈现。

## B.0.2 P 档别名转换清单（omp-ui 侧）

| 组 | omp 命令/字段 | Hermes 原生 | 客户端转换 |
|---|---|---|---|
| 请求信封 | `{id?,type,...}` | `{jsonrpc,id,method,params}` | `type`→`method`、平铺字段→`params`；补 `session_id`/`profile`/`request_id` |
| 响应信封 | `{type:"response",id,command,success,data?,error?,code?}` | `{jsonrpc,id,result}` / `{jsonrpc,id,error:{code,message,data.reason}}` | `result`→`data`；由有无 `result` 派生 `success`；`error.message`→`error`；`data.reason`→`code`；`command` 按 `id` 回填 |
| 协商 | `protocolVersion` | `client.capabilities.protocol_version` | 字段改名（已落地，见 B.18） |
| prompt | `message` / `images` / `streamingBehavior` | `prompt.submit.text` / `attachments` / busy 路径 | `message`→`text`；`images` 走 `image.attach_bytes`（URL 无对应）；`streamingBehavior` 由客户端决定 `queued` 或先 `session.steer` |
| prompt 回执 | `id` / `agentInvoked` | `admission_id` / `AdmissionStatus` | `id`↔`admission_id`；`agentInvoked` 由准入结果派生 |
| steer / follow_up | `message` / `images` | `session.steer.text` / `prompt.submit{queued:true}` | `message`→`text`；`follow_up` 别名到 `prompt.submit` |
| 队列删除 | `message` / `queue` | `prompt.cancel.admission_id` | 用 `queue.updated` 的 id 定位（不做文本寻址） |
| 新建/打开 | `parentSession` / `sessionDir` | `parent_session_id` / `workspace` | 字段改名；`provider`/`modelId` 在 resume 无对应 |
| 切换会话 | `sessionPath` | `session.resume.session_id` | 客户端用 `session.list` 解析 stored id |
| 导出 | `outputPath` | `session.export` 返回 `content` | 客户端落盘到 `outputPath` |
| 历史 | `get_entries.messages` / `get_messages` | `session.history.entries` / `messages` | 客户端投影；`entryId` = durable `row_id` |
| 分支 | `branch.entryId` / `fork.entryId` | `session.mutate{through_message_id}` / `session.branch_whole` | `entryId`→`through_message_id`；`text`/`cancelled` 客户端从结果派生 |
| 重命名 | `name` | `session.mutate{rename}` / `session.title` | `name`→`title` |
| 压缩 | `customInstructions` | `session.compress.focus_topic` | 字段改名；压缩结果由客户端投影 |
| 模型 | `modelId` | `session.mutate.payload.model` | 字段改名；`ModelInfo` 由 `model.options.models` 投影 |
| 推理档 | `level` | `session.mutate{reasoning}.effort` | `off`→`"none"`、`inherit`→省略字段 |
| 快速模式 | `enabled` | `config.set{key:"fast"}` | `active` 从 `session.info` 派生 |
| goal | `op` / `objective` | `session.control` 动作 / `GoalSnapshot.title` | `get`→`session.control.read`；`op`→动作名；`objective`↔`title`；`{goal,state}` 客户端投影 |
| 命令目录 | `commands[]` | `commands.catalog.entries` | 条目字段映射；`source` 枚举按 B.8.1 投影 |
| bash | `command` / `output` / `exitCode` | `shell.exec.command` / `stdout`+`stderr` / `code` | `output` 由客户端拼接 `stdout`+`\n`+`stderr`；`exitCode`→`code` |
| 统计 | `tokens{}` / `cost` / `credits` | `session.stats` 扁平字段 / `cost_usd` | 客户端组装 `tokens`/`cost`；无来源字段置空 |
| 子代理 | `id` / `index` / `status` | `subagent.list.subagent_id` / `task_index` / `status` | 重命名；`aborted`→`interrupted` |
| 子代理取消 | `found` | `subagent.interrupt` | `cancelled = found`（注意 `found:false` 表示已结束） |
| 语音 | `captureId` | `voice.record.capture_id` | 字段改名 |
| BTW | `question` / `recordId` / `records` | `prompt.btw.text` / `record_id` / `btw.history.records` | 字段改名；`items`↔`records` |
| 登录 | `providerId` | `model.options.providers[].slug` / `auth.*` | `slug`↔`id` |
| 登出账户 | `credentialId` / `type` | `auth.accounts.credential_id` / `type` | 数值↔字符串；`api_key`/`oauth` 枚举校验 |
| 登出 | `providerId` / `credentialId` | `model.disconnect.slug` | `providerId`→`slug`；`credentialId` 见 B.14（未落地） |

“补项”指 Hermes 确实没有的数据，新增字段用 Hermes 原生命名（可为 `None`，handler 未落地时保持空值）。

## B.0.3 复核结论（2026-11-01）

- **覆盖**：`wire/commands.ts` 的 74 个命令与 B.16 矩阵逐项一致（脚本核对集合相等，无缺项、无多项）。
- **补齐的定义缺口**：`set_host_tools`、`set_host_uri_schemes`、`cycle_model`、
  `cycle_thinking_level`（B.7.4）；`get_branch_messages`、`get_last_assistant_text` 与
  `session.tree.*` / `session.entry.label`（B.5.2 的完整定义）；其余 N/P 项集中在 B.15.1。
- **按实现修正档位**：`negotiate_protocol` N→P（能力已由 `client.capabilities` 承载）、
  `get_session_lease` N→F、`get_logout_accounts` N→P（`credentialId` 数值 vs `credential_id` 字符串、
  `type` 枚举 vs 字符串，需客户端转换；B.14/B.18）。
- **统计**：F 4、P 34、N 36（共 74）。

## B.1 两端已有的协议定义

| 侧 | 业务 RPC 定义 | 生成/消费 |
|---|---|---|
| omp-ui | `wire/commands.ts`（74 命令：`name/doc/params/result/nullable/unwrap/timeoutMs/completion`） | `rpc-wire.schema.json` 的 `x-rpc.commands`、`rpc-wire.generated.ts`；`rpc-client.ts` 实现 |
| Hermes | `tui_gateway/contracts/*.py` 的 `Params`/`Result` | `gateway-contract.generated.ts` 的 `Client→server methods` 与 `Canonical methods`、OpenRPC |

omp 侧**没有** Hermes 方法名映射；Hermes 侧**没有** omp 命令名映射。对齐需要**双向映射表**（本附录的
矩阵即该表），不是补类型声明。

## B.2 信封映射

### B.2.1 请求

omp：

```json
{"id":"7","type":"set_model","provider":"anthropic","modelId":"claude-sonnet-4.5"}
```

Hermes：

```json
{"jsonrpc":"2.0","id":"7","method":"session.mutate",
 "params":{"session_id":"<sid>","request_id":"7","expected_revision":12,"operation":"model",
           "payload":{"model":"claude-sonnet-4.5","provider":"anthropic"}}}
```

| omp 字段 | Hermes 字段 | 处理 |
|---|---|---|
| `id?` | `id` | 一致（字符串关联）；omp 的 `id` 可缺省，Hermes 要求 | 
| `type` | `method` | **别名**：`type` 的 snake_case 名字到 Hermes 点分方法名的映射见 B.3–B.16 |
| 其余顶层字段 | `params.*` | **改名**：omp 参数平铺，Hermes 收进 `params` |
| — | `jsonrpc:"2.0"` | **新增**常量 |
| — | `params.session_id` / `params.profile` | **新增**：omp 命令没有会话/profile 维度 |
| — | `params.request_id` / `expected_revision` / `expected_generation` | **新增**：写方法的幂等/CAS 字段（omp 由 `id` 兼任 `request_id`） |

### B.2.2 响应

omp：

```json
{"type":"response","id":"7","command":"set_model","success":true,"data":{...}}
{"type":"response","id":"7","command":"set_model","success":false,"error":"model not found","code":"not_found"}
```

Hermes：

```json
{"jsonrpc":"2.0","id":"7","result":{...}}
{"jsonrpc":"2.0","id":"7","error":{"code":4001,"message":"invalid_params: ...","data":{"reason":"not_found"}}}
```

| omp 字段 | Hermes 字段 | 处理 |
|---|---|---|
| `type:"response"` | — | 由“有 `result` 或 `error`”判定 |
| `id` | `id` | 一致 |
| `command` | — | Hermes 不回显方法名；omp 客户端在请求表里按 `id` 查 |
| `success:true` | 存在 `result` | 派生 |
| `data` | `result` | **别名** |
| `success:false` + `error`（字符串） | `error.message` | **别名** |
| `code?`（字符串，如 `cost_cap`） | `error.code`（数字）/ `error.data.reason`（字符串） | **新增**：客户端同时接受数字码与 `data.reason`；建议 Hermes 错误体固定带 `data.reason` |
| — | `jsonrpc:"2.0"` | **新增**常量 |

**结论：P。** 关联语义一致（id ↔ id），但需要客户端做一次信封转换；建议在 `gateway-client.ts`
集中实现，避免每个面板各自解析（与计划 §5.5 一致）。

上行不分片：omp 的 `rpc_chunk` 只由服务端写 stdout 时产生，客户端命令始终是单行 JSON；Hermes 也不
分片。故业务 RPC 侧没有对应的传输帧，只需在客户端解码入站分片（见附录 A.2.3）。

## B.3 协议协商与会话状态

### B.3.1 `negotiate_protocol` → **P（能力已并入 `client.capabilities`）**

omp：`params {protocolVersion:int}` → `data {protocolVersion:int}`。

Hermes 没有协议版本协商方法。现有相关契约：

- `client.capabilities`（客户端 → 服务端，声明 `server_requests`/`desktop_local`，返回本后端可发的
  server→client 请求方法）；
- `gateway.capabilities`（`per_session_exclusive_submit`）；
- `gateway.ready` 的 `replay_epoch` / `session_authority`。

建议（二选一，推荐 A）：

A. 扩展现有方法，不新增命令：

```text
client.capabilities params += {
  protocol_version?: int,
  supported_protocol_versions?: int[],
  max_frame_bytes?: int,
  max_reassembled_frame_bytes?: int
}
result += {protocol_version: int, supported_protocol_versions: int[],
           max_frame_bytes: int, max_reassembled_frame_bytes: int}
```

B. 新增 `protocol.negotiate`：`params {protocol_version}` → `result {protocol_version}`（与 omp 字段
完全一致），由 `runtime.describe` 广告。若目标只是 omp-ui 单客户端，A 更省一次往返。

**已选 A 并落地**（B.18）：`client.capabilities` 与 `gateway.ready` 都携带协议版本与帧上限。
omp 侧 `gateway-client.ts` 把 `negotiate_protocol` 映射到 `client.capabilities`，因此档位由 N 升为 P
（功能对应，方法名与信封不同）。

### B.3.2 `get_state` → **P（建议新增 `session.state`）**

omp `SessionState` 是**聚合快照**；Hermes 把它拆在 `session.resume`/`session.activate`
（`CanonicalSnapshot`）、`session.info`（`SessionLiveInfo`）、`session.stats`、`session.control.read`、
`config.get` 里。逐字段：

| omp `SessionState` 字段 | Hermes 来源 | 处理 |
|---|---|---|
| `model?: ModelInfo` | `info.model`（string）或 `model.options` | 类型不同；**新增** `model_info`（对象）或客户端按 `model.options` 解析 |
| `thinkingLevel?` | `info.reasoning_effort` | 别名；枚举见 B.6.3 |
| `isStreaming` | `snapshot.running` 或 `info.running` | 近似；**新增** `is_streaming`（`running` 含“构建中”） |
| `isCompacting` | 无（只有 `status.update kind=compacting`） | **新增** `is_compacting` |
| `steeringMode` / `followUpMode` / `interruptMode` | 无 | **新增**（见 B.7.4） |
| `sessionFile?` | 无 | **新增** `session_file` |
| `sessionId` | `session_id` / `stored_session_id` | 别名（注意 runtime id 与 stored id 不同） |
| `sessionName?` | `info.title` | 别名 |
| `autoCompactionEnabled` | 无 | **新增** |
| `fastModeEnabled` / `fastModeActive` | `info.fast` / `info.service_tier` | `fastModeActive` 无；**新增** |
| `slowModeSupported/Enabled/Scope?` | 无 | **新增** |
| `usageLimit?` | `session.usage.plan_gauge` / `message.complete.usage_limit` | 结构不同（`PlanGauge{id,label,used_bp,resets_at_ms}` vs `UsageLimitState{stage,...}`） |
| `tokensPerSecond` | `session.stats.tokens_per_s` / `usage.avg_tps` | 别名 |
| `messageCount` | `snapshot.message_count` | 一致 |
| `queuedMessageCount` | `len(snapshot.pending)` | 派生；**新增**（避免客户端依赖准入数组） |
| `hasPendingAsyncWork` | 无 | **新增** |
| `isSettled` | 无 | **新增** |
| `queuedMessages` | `snapshot.pending[]` | 结构不同（`{steering:string[],followUp:string[],liveSteered:int}` vs 准入行） |
| `todoPhases` | `snapshot.todo_state.todos` | `TodoState{todos,revision}`；**新增** `todo_phases` 投影 |
| `systemPrompt` | `info.system_prompt`（string） | 类型不同（omp `string[]`）；**新增** `system_prompt_sections` |
| `dumpTools` | 无 | **新增** |
| `contextUsage?` | `usage.context_used/context_max/context_percent` | 聚合为 `{tokens,contextWindow,percent}` |
| `goal` | `session.control.read.control.goal` | 结构不同，见 A.7.2 |

建议新增：

```text
session.state {session_id} -> SessionStateProjection
  # 字段与 omp SessionState 一一对应（snake_case）：
  # {model?: ModelInfoProjection, thinking_level?, is_streaming, is_compacting,
  #  steering_mode, follow_up_mode, interrupt_mode, session_file?, session_id,
  #  session_name?, auto_compaction_enabled, fast_mode_enabled, fast_mode_active,
  #  slow_mode_supported, slow_mode_enabled, slow_mode_scope?, usage_limit?,
  #  tokens_per_second, message_count, queued_message_count, has_pending_async_work,
  #  is_settled, queued_messages, todo_phases, system_prompt, dump_tools,
  #  context_usage?, goal}
```

如果不想新增方法，客户端可用 4 次调用拼装（`session.resume` + `session.stats` +
`session.control.read` + `config.get{key:"full"}`），但 `is_compacting`/`has_pending_async_work`/
`is_settled`/`dump_tools` 在任何现有结果里都没有，仍属**新增**。

### B.3.3 `get_session_lease` → **F（已落地 `session.lease`）**

omp：`params {sessionIds:string[]}` → `data {held:string[],unknown:string[]}`。

```text
session.lease {session_ids: string[], profile?} -> {held: string[], unknown: string[]}
```

已落地（B.18）：`gateway/session_lease.py` 用 `active_session_registry_snapshot(strict=True)`；registry
不可证明存活时全部进 `unknown`，不拿 authority 会话冒充租约。omp 侧只做 `sessionIds`→`session_ids`
别名与信封转换，档位 F。

## B.4 Prompt、队列与中断

### B.4.1 `prompt` → `prompt.submit`（P）

omp：`params {message:string, images?:ImageContent[], streamingBehavior?:"steer"|"followUp"}`
→ `data PromptAck {agentInvoked?:bool}`（完成由 `prompt_result` 帧送达）。

Hermes `PromptSubmitParams`（canonical）：`{session_id, text, submission_id?, input_id?, queued?,
attachments?:[{path,mime}], finite?, unattended?, surface?, voice_context?, interrupted?, voice_turn?,
display_kind?, title_preview?, turn_author?, api_text?, model_override?, clarify_batch?}`；
结果 `AdmissionStatus {admission_id, ref{profile_id,session_id}, sequence, status, outcome,
authority_epoch, execution_generation}`。

| omp | Hermes | 处理 |
|---|---|---|
| `message` | `text` | **别名** |
| `images` | `attachments` 或先 `image.attach_bytes` | 结构不同：`ImageContent{type:"image",data(base64),mimeType,detail?,url?,providerFile?}` vs `{path,mime}`。base64 走 `image.attach_bytes {content_base64,filename,ext}`；URL 无对应（**新增** `url`） |
| `streamingBehavior` | `queued` / busy 路径 | 语义不同：omp 在**提交时**指定 steer/followUp；Hermes 由 `prompt.submit` 的 busy 路径决定或 `session.steer` 单独发。**新增** `streaming_behavior:"steer"/"follow_up"` 或文档化“先 steer 再 submit” |
| — | `submission_id` / `input_id` | **新增**（客户端生成，幂等） |
| — | `attachments` | 见上 |
| `data.agentInvoked` | 无 | **新增** `agent_invoked`（`false` 表示本地处理、无后续 `prompt_result`） |
| 请求 `id` | `admission_id` | **别名**（客户端建映射；omp 的 `prompt_result.id` 回填它） |
| — | `status:"streaming"/"queued"/"steered"/"redirected"` | **新增**到 omp 客户端模型（omp 的 ack 没有状态） |

### B.4.2 `steer` / `follow_up` → **P**

| omp | Hermes | 处理 |
|---|---|---|
| `steer {message,images?}` | `session.steer {session_id,text,expected_generation}`（canonical：`CanonicalCorrectionParams` 继承 `execution_generation`） | `message`→`text` 别名；`images` 无（**新增** `attachments`）；结果 `{status:"queued"/"redirected"/"rejected", text, execution_generation, authority_epoch}` 比 omp 的“无结果”多 |
| `follow_up {message,images?}` | `prompt.submit {queued:true,text}` | 无独立方法；**别名**到 `prompt.submit`；结果变为 `AdmissionStatus` |

注意语义：omp 的 `steer` 是**排队**（进入 steering 队列），Hermes `session.steer` 是**注入下一次工具结果**；
两者不等价。若面板依赖 omp 队列 UI，应使用 `prompt.submit{queued:true}` + `queue.updated`（附录 A.7.1），
把 `session.steer` 留给“立即影响当前回合”。

### B.4.3 队列编辑

| omp | Hermes | 状态 | 处理 |
|---|---|---|---|
| `remove_queued_message {message,queue}` | `prompt.cancel {session_id,admission_id}` | P | omp 按**文本 + 队列名**定位；Hermes 按 `admission_id`。**新增** `message`/`queue` 查找字段，或在 `queue.updated` 里给出 `admission_id` 供客户端保存 |
| `promote_queued_message {message}` | 无 | N | **新增** `prompt.promote_queued {session_id,admission_id}` → `{promoted:bool}`（Hermes 队列模型里 steering/follow-up 是两条 FIFO） |

### B.4.4 中断

| omp | Hermes | 状态 | 处理 |
|---|---|---|---|
| `abort` | `session.interrupt {session_id,expected_hosted_task_id?}` | **F** | omp 无参数、无结果；Hermes 多 `session_id`（信封给）与 `status/interrupted/turn_isolation`，不破坏 omp |
| `abort_and_prompt {message,images?}` | 无原子方法 | N | **新增** `prompt.abort_submit {session_id,text,attachments?,expected_generation?}` → `AdmissionStatus`；客户端两次调用存在“abort 后 prompt 前”竞态 |
| `abort_and_restore_queue` | 无 | N | **新增** `prompt.abort_restore {session_id}` → `{steering:[{text,images?}],follow_up:[...],images_dropped?,truncated?}` |

## B.5 会话生命周期、历史与分支

### B.5.1 新建 / 打开 / 切换

| omp | Hermes | 状态 | 字段处理 |
|---|---|---|---|
| `new_session {parentSession?}` | `session.create`（canonical `CanonicalCreateParams`） | P | `parentSession`→`parent_session_id` **别名**；Hermes 参数是多出集（`source/cwd/model/provider/reasoning/max_turns/toolsets/skills/title/hidden/...`）。结果：omp `CancellationResult{cancelled}` vs `CanonicalSnapshot`（会话快照）；**新增** `cancelled` 或客户端忽略 |
| `open_session {sessionDir,provider?,modelId?}` | `session.resume`（canonical） | P | `sessionDir`→`workspace`（canonical `CanonicalResumeParams.workspace`）+ `latest:"cli"/"tui"`；`provider/modelId` 在 `session.resume` **无**（只在 `session.create`），**新增** `provider`/`model_id` 到 resume，或改走 `session.create` |
| `switch_session {sessionPath,provider?,modelId?}` | `session.resume {session_id}` | P | `sessionPath` 是**文件路径**，Hermes 收 `session_id`（stored id/title）；**新增** `session_path` 或由客户端解析。`provider/modelId` 同上 |
| `export_html {outputPath?}` | `session.export {format:"html",redact?}` | P | 结果：omp `{path}`（服务端写文件） vs Hermes `{format,filename,content}`（客户端写）。**新增** `output_path` 参数或让 omp 客户端落盘 |

`open_session`/`switch_session` 的结果 `OpenSessionResult {cancelled,resumed,sessionId,sessionFile?}`
与 `CancellationResult {cancelled}` 在 Hermes 快照里没有 `cancelled/resumed/sessionFile`：

```text
session.resume result += {cancelled?: bool, resumed?: bool, session_file?: string}
```

### B.5.2 历史与树

| omp | Hermes | 状态 | 处理 |
|---|---|---|---|
| `get_entries {since?}` | `session.history {session_id}` | P | 结果 `{count,messages:[TranscriptMessage]}` 是**渲染投影**，omp 要 `{entries:SessionEntry[],leafId}`。**新增** `entries`、`leaf_id`；`since` 游标缺失（计划 §4.7 的 E 项） |
| `get_messages` | `session.history` | P | `messages[TranscriptMessage]` vs `AgentMessage[]`；见 B.5.4 字段表 |
| `get_messages_page {cursor?,limit?}` | `session.history` | P | **新增** `cursor`、`limit`、`next_cursor`、`total_messages`（计划 E） |
| `get_tree` | 无 | N | **新增** `session.tree {session_id}` → `{tree:JsonValue[],leaf_id:string or null}` |
| `branch {entryId}` | `session.mutate {operation:"branch",payload:{through_message_id,title?}}` 或 `session.branch` | P | `entryId`→`through_message_id`（int 物理行 id，omp 是 entry id，需映射）；写方法要 `request_id`/`expected_revision`/`expected_generation`；结果 omp `{text,cancelled}` vs `{branched_session_id,...}`。**新增** `text`/`cancelled` |
| `fork {entryId?}` | `session.branch_whole {name?,idempotency_key?}` | P | `entryId` 无对应（whole-history fork）；**新增** `through_message_id` |
| `navigate_tree {entryId,summarize?,customInstructions?}` | 无 | N | **新增** `session.tree.navigate {session_id,entry_id,summarize?,custom_instructions?,request_id,expected_revision}` → `{cancelled,aborted?,editor_text?}`（Hermes 的 `session.mutate rewind` 是删行，不是移动 leaf） |
| `abort_branch_summary` | 无 | N | **新增** `session.tree.navigate.abort {session_id}` |
| `set_label {entryId,label?}` | 无 | N | **新增** `session.entry.label {session_id,entry_id,label?}` |
| `get_branch_messages` | 无专用方法 | N | 可由 `session.history` 的 `role=user` + `row_id` 派生；**建议零新增**，在客户端派生。若必须服务端：`session.tree.branch_messages` |
| `get_last_assistant_text` | 无专用方法 | N | 可由 `session.history` 最后一条 assistant 派生；建议零新增 |
| `set_session_name {name}` | `session.title {title}` | **F** | `name`→`title` **别名**；结果 `title` 一致（Hermes 多 `session_key`/`pending`） |
| `handoff {customInstructions?}` | `handoff.request {platform}` | N | 语义不同（Hermes 是移交到消息平台 home channel，不是新会话）。**新增** `session.handoff {custom_instructions?}` → `{session_id,saved_path?}` |

N 档完整定义（尚无 handler；UI 保持原交互并标 `pending`/`unsupported`）：

```text
session.tree                    {session_id} -> {tree: JsonValue[], leaf_id: string|null}
session.tree.navigate           {session_id, entry_id, summarize?: bool, custom_instructions?: string,
                                 request_id, expected_revision}
                                -> {cancelled: bool, aborted?: bool, editor_text?: string}
session.tree.navigate.abort     {session_id} -> {cancelled: bool}
session.entry.label             {session_id, entry_id, label?: string, request_id?}
                                -> {entry_id: string, label?: string}
session.handoff                 {session_id, custom_instructions?: string, request_id}
                                -> {session_id: string, saved_path?: string}
# 以下两条优先由客户端派生（不新增服务端方法）；若服务端实现，契约固定为：
session.tree.branch_messages    {session_id} -> {messages: [{entry_id: string, text: string}]}
session.last_assistant_text     {session_id} -> {text: string|null}
```

### B.5.3 压缩

| omp | Hermes | 状态 | 处理 |
|---|---|---|---|
| `compact {customInstructions?}` | `session.compress {session_id,focus_topic?}` | P | `customInstructions`→`focus_topic` **别名**；结果：omp `CompactionResult{summary,shortSummary,firstKeptEntryId,tokensBefore,details?,preserveData?,providerReplayThroughEntryId?}` vs Hermes `{status,removed,before_messages,after_messages,before_tokens,after_tokens,summary{headline,token_line,...},usage,info,messages,...}`。需**新增** `short_summary`、`first_kept_entry_id`、`tokens_before`（或客户端映射） |
| `set_auto_compaction {enabled}` | 无 | N | **新增** `session.policy {auto_compaction?:bool}`，或 `config.set {key:"auto_compaction"}`（当前 setter 无此 key） |
| `set_cache_warming {mode}` | 无 | N | **新增** `session.policy {cache_warming?:"off"/"streaming"/"idle"}` |
| `set_auto_retry {enabled}` | 无 | N | **新增** `session.policy {auto_retry?:bool}` |
| `abort_retry` | 无 | N | **新增** `retry.abort {session_id}` → `{cancelled: bool}`（`cancelled:false` 表示没有可取消的退避等待） |

### B.5.4 `AgentMessage` ↔ `TranscriptMessage` 字段映射

| omp `AgentMessage`（`role`） | Hermes `TranscriptMessage` | 处理 |
|---|---|---|
| `user` | `role:"user"` | `content`（string 或块数组）→ `content`/`text`；`timestamp` |
| `assistant` | `role:"assistant"` | `content`（块数组）→ `content`；`provider/model/usage/stopReason` **新增**（`reasoning` 已有独立键） |
| `toolResult` | `role:"tool"`（Hermes 投影） | `toolCallId`→`tool_call_id`、`toolName`→`name`、`content`→`content`、`isError`→**新增** `is_error` |
| `bashExecution` / `pythonExecution` | 无专用 role | **新增** `role` 值或 `display_kind` |
| `custom` / `hookMessage` | `display_kind`/`display_metadata` | **新增** `custom_type`、`display` |
| `branchSummary` / `compactionSummary` | 无专用 role | **新增** `role:"branch_summary"/"compaction_summary"`，或 `display_kind` |
| `fileMention` | 无 | **新增** |
| — | `row_id` | omp 无（entry id 概念）；**新增** `entry_id` 到 omp 模型以便 `branch/navigate_tree` 回传 |

## B.6 模型、推理与快速模式

### B.6.1 `set_model` → `session.mutate{operation:"model"}`（P）

| omp | Hermes | 处理 |
|---|---|---|
| `provider` | `payload.provider` | 一致 |
| `modelId` | `payload.model` | **别名** |
| — | `payload.confirm` / `payload.reasoning` | **新增**（昂贵模型确认、同次切档） |
| — | `session_id` / `request_id` / `expected_revision` / `expected_generation` | **新增**信封字段 |
| `data ModelInfo` | `SessionMutateResult {session_id,operation,revision}` | 结果无模型描述；**新增** `model_info` 或客户端随后调 `model.options` |

替代（不推荐）：`config.set {key:"model", value:"provider/model", session_id, scope}`，但它不携带
`expected_revision`，且结果 `ConfigSetResult` 同样没有 `ModelInfo`。

### B.6.2 `get_available_models` → `model.options`（P）

omp：`data {models:ModelInfo[]}`，`ModelInfo{id,name,api,provider,baseUrl,reasoning,input[],cost{input,
output,cacheRead,cacheWrite},contextWindow,maxTokens,headers?,premiumMultiplier?,preferWebsockets?,
contextPromotionTarget?,priority?,thinking?{...},compat?}`。

Hermes：`ModelOptionsResult {providers:[ModelOptionProvider],model,provider}`，
`ModelOptionProvider{slug,name,models:string[],authenticated,auth_type,capabilities:{model:ModelCapabilities},
pricing:{model:ModelPricing},unavailable_models,plan_usage,...}`。

| omp | Hermes | 处理 |
|---|---|---|
| `provider` | `providers[].slug` | 别名 |
| `id` | `providers[].models[]`（仅字符串） | **新增**扁平 `models: ModelInfo[]`（计划 §4.7 的 E 项） |
| `name` | — | **新增**（模型显示名） |
| `api` | — | **新增** |
| `baseUrl` | `providers[].api_url`（provider 级） | **新增** model 级 |
| `reasoning` | `capabilities[model].reasoning` | 别名 |
| `input[]`（modalities） | 无 | **新增** |
| `cost{...}` | `pricing[model]{input,output,cache,free,...}`（**字符串** `$/Mtok`） | 类型不同：数值 vs 格式化字符串；**新增**数值字段或客户端解析 |
| `contextWindow` / `maxTokens` | 无 | **新增** |
| `thinking` | `capabilities[model].reasoning` 只能布尔 | **新增** `thinking` 对象（见 B.6.3） |
| `headers`/`compat`/`premiumMultiplier`/`priority` | 无 | **新增** |

### B.6.3 推理档位

| omp | Hermes | 状态 | 处理 |
|---|---|---|---|
| `set_thinking_level {level:ThinkingLevel}` | `session.mutate {operation:"reasoning",payload:{effort}}` | P | `level`→`effort` 别名；枚举映射：`off`→`"none"`、`inherit`→省略字段（Hermes 无 inherit），`minimal/low/medium/high/xhigh/max` 一致；Hermes 多 `expected_revision/generation` |
| `cycle_thinking_level` | 无 | N | 不新增方法：客户端用 `get_available_thinking_levels`（待补 `thinking.levels`）+ `set_thinking_level` 派生，与服务端状态无关 |
| `get_available_thinking_levels` | 无 | N | **新增** `model.options` 的每模型 `thinking.levels`（omp 的 `ThinkingConfig.efforts`） |
| `set_fast_mode {enabled}` | `config.set {key:"fast",value,session_id,scope?}` | P | 结果 omp `{enabled,active}` vs Hermes `ConfigSetResult{key,value,...}`。**新增** `active` |
| `set_slow_mode {enabled}` | 无 | N | **新增** `session.policy {slow_mode?:bool}`（含 omp 的 `slowModeScope`） |

## B.7 Goal、Todo、模式与订阅

### B.7.1 `goal` → `session.control` / `session.automation`（P）

omp：`params {op:"get"|"create"|"resume"|"pause"|"drop", objective?, token_budget?}` →
`data {goal:Goal|null, state:GoalModeState|null}`。

Hermes 允许的动作（`SessionControlAction`）：`goal.pause/resume/clear/unwait`；`get/create/drop` 没有
（`session.automation` 可代理 manager，但没有 `op` 形状）。映射：

| omp `op` | Hermes | 处理 |
|---|---|---|
| `get` | `session.control.read` | 结果 `{control:{goal,loop,heartbeat,revision,updated_at}}`；需投影为 `{goal,state}` |
| `create` | 无 | **新增** `goal.create`（`args.objective`、`args.token_budget`） |
| `resume` | `goal.resume` | 一致 |
| `pause` | `goal.pause` | 一致 |
| `drop` | `goal.clear` | **别名**（语义：清除 vs 放弃；需确认） |

字段：omp `Goal.id/objective/status/tokenBudget/tokensUsed/timeUsedSeconds/createdAt/updatedAt` vs
Hermes `GoalSnapshot.title/status/turns_used/max_turns/contract/subgoals/gates/created_at/updated_at/
paused_reason/last_verdict/last_reason/wait_barrier`。`objective`→`title` 是**近似别名**；
`tokenBudget/tokensUsed/timeUsedSeconds/id` **新增**。

### B.7.2 `set_todos` → **N**

omp：`params {phases:TodoPhase[]}` → `data {todoPhases:TodoPhase[]}`。
Hermes 只在 `todo.updated` 事件里给快照，**没有写方法**。建议：

```text
session.todos.set {session_id, phases: TodoPhase[]} -> {todo_phases: TodoPhase[], revision: int}
```

`TodoPhase {name, tasks: TodoItem[]}`、`TodoItem {content,status,blocker?,details?,notes?[]}`；
`status` 枚举 `pending|in_progress|completed|abandoned|blocked` 需在 Hermes 契约中声明（当前为
`JsonValue`）。

### B.7.3 `set_ask_dialog` / 模式 / 订阅 → **N**

| omp | 说明 | 建议 |
|---|---|---|
| `set_ask_dialog {enabled}` → `{enabled}` | omp 选择是否接收 `ask` UI 请求 | Hermes `clarify` 始终可用；**新增** `session.ask_dialog {session_id,enabled}` → `{enabled}` |
| `set_steering_mode {mode:"all"/"one-at-a-time"}` | 队列投递策略 | **新增** `session.policy {steering_mode?}` |
| `set_follow_up_mode {mode}` | 同上 | `session.policy {follow_up_mode?}` |
| `set_interrupt_mode {mode:"immediate"/"wait"}` | 工具中断策略 | `session.policy {interrupt_mode?}` |
| `set_event_filter {events:string[] or null,messageUpdates?}` | 只转发列出的会话事件 | **新增** `session.events.filter {session_id,events,message_updates}`；Hermes 侧 `message.delta` 无 full/delta 投影，`message_updates` **新增** |
| `set_subagent_subscription {level:"off"/"progress"/"events"}` | 子代理帧转发级别 | **新增** `subagent.subscribe {session_id,level}` |

### B.7.4 Host 注册与派生命令 → **N（契约先定义，当前不落地）**

这三组命令在 Hermes 没有对应能力；按 B.0.1 仍给出完整定义，避免客户端映射悬空：

```text
# 宿主工具/URI 注册：Hermes 的工具与 URI 全部在服务端解析，注册没有接收方。
# 当前决定：不落地。客户端本地 no-op（返回空集）并把 host 能力标 unsupported，
# 不发请求，也不伪造 server_request（A.13）。
session.host.tools.register        {session_id, tools: [HostToolDefinition]}
                                  -> {tool_names: string[]}
session.host.uri_schemes.register  {session_id, schemes: [HostUriSchemeDefinition]}
                                  -> {schemes: string[]}

# 模型/推理轮换：轮换顺序是客户端状态，不是 Hermes 状态。
# 当前决定：客户端用 model.options + session.mutate 派生；若实现则用以下契约。
session.model.cycle     {session_id, direction?: "next"|"prev"}
                       -> {model?: ModelInfoEntry, thinking_level?: string} | null
session.thinking.cycle  {session_id, direction?: "next"|"prev"} -> {level?: string} | null
```

`HostToolDefinition {name,label?,description,parameters,hidden?,loadMode?,readsSkillUris?}`、
`HostUriSchemeDefinition {scheme,description?,writable?,immutable?}` 与 omp 侧同名类型一致；
`ModelInfoEntry` 复用 `model.options.models` 的条目，档位枚举见 B.6.3。

派生契约（不新增方法）：`cycle_model` = 当前模型在 `model.options.models` 中的下一个（尾回卷，空则
`null`）+ `session.mutate{model}`；`cycle_thinking_level` = 当前档位在
`model.options.models[].thinking.levels` 中的下一个 + `session.mutate{reasoning}`。

## B.8 命令目录

### B.8.1 `get_available_commands` → `commands.catalog`（P）

omp：`data {commands:AvailableSlashCommand[]}`，条目
`{name,aliases?,description?,input?{hint?},subcommands?[{name,description?,usage?}],source}`。

Hermes：`CommandsCatalogResult {pairs:[[name,desc]],sub:{name:[]},canon:{alias:canonical},
commands:{name:{argument_mode,desktop,desktop_subcommands}},categories:[{name,pairs}],
skills:{name:{usage,origin}},skill_count,warning,plugin_discovery_error,plugin_commands:{name:desc}}`。

| omp | Hermes | 处理 |
|---|---|---|
| 数组 | 分组结构 | **新增**投影 `entries: AvailableSlashCommand[]`（建议在 `commands.catalog` 结果上加法，不替换现有字段） |
| `name` | `pairs[][0]` / `plugin_commands` key | 投影时合并 |
| `description` | `pairs[][1]` / `plugin_commands` value | 投影 |
| `aliases` | `canon` 反向索引 | 投影 |
| `subcommands` | `sub[name]`（仅名字） | `usage`/`description` **新增** |
| `source` | 来自 `categories`/`skills`/`plugin_commands` 的来源 | 映射：`skill`→`"skill"`、plugin→`"extension"`、builtin→`"builtin"`；`mcp_prompt`/`file` **新增** |
| `input.hint` | 无 | **新增** |

## B.9 Shell

### B.9.1 `bash` → `shell.exec`（canonical）（P）

omp：`params {command}` → `data BashResult {output,exitCode?,cancelled,timedOut?,truncated,
totalLines,totalBytes,outputLines,outputBytes,artifactId?,artifactElidedBytes?,artifactError?,
workingDir?,images?}`。

Hermes canonical `shell.exec`：`params {session_id,command,profile?}` → `result {stdout,stderr,code}`。

| omp | Hermes | 处理 |
|---|---|---|
| `command` | `command` | 一致 |
| `output` | `stdout` + `stderr` | **别名/拼接**：omp 只有一个 `output`；客户端按 `stdout+"\n"+stderr` 或服务端**新增** `output` |
| `exitCode` | `code` | **别名** |
| `cancelled` | 无 | **新增** |
| `timedOut` | 无 | **新增** |
| `truncated` | 无 | **新增** |
| `totalLines/totalBytes/outputLines/outputBytes` | 无 | **新增** |
| `artifactId/artifactElidedBytes/artifactError` | 无 | **新增** |
| `workingDir` | 无 | **新增** |
| `images` | 无 | **新增** |

另：Hermes `shell.exec` 是“安全（非危险）命令”；omp `bash` 是用户 `!` 命令。若面板需要任意命令，
应走 `cli.exec`（`argv`）或新增 `session.bash`，并在协议里明确危险命令的拒绝码。

### B.9.2 `abort_bash` → **N**

建议 `session.bash.abort {session_id,process_id?}` → `{cancelled:bool}`。

## B.10 统计

### B.10.1 `get_session_stats` → `session.stats`（P）

omp `SessionStats {sessionFile?,sessionId,userMessages,assistantMessages,toolCalls,toolResults,
totalMessages,tokens:TokenUsage,premiumRequests,cost,credits?,routedModels?,editRejections?,
contextUsage?}`；`TokenUsage{input,output,reasoning,cacheRead,cacheWrite,total}`。

Hermes canonical `SessionStats {input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,
prompt_tokens,completion_tokens,total_tokens,reasoning_tokens,api_calls,last_prompt_tokens,
context_tokens,context_length?,config_context_length?,context_source?,compression_count,
avg_latency_s?,tokens_per_s?,last_turn_tokens,rate_limit?,active_subagents,model,provider,
fallback_model?,plan_gauge?}`。

| omp | Hermes | 处理 |
|---|---|---|
| `sessionId` | —（会话键不在结果里） | **新增** `session_id` |
| `sessionFile` | 无 | **新增** |
| `tokens` 对象 | 扁平 `input_tokens/...` | **新增** `tokens` 对象或客户端组装；`reasoning`↔`reasoning_tokens`、`total`↔`total_tokens` |
| `userMessages/assistantMessages/toolCalls/toolResults/totalMessages` | 无（只有 `api_calls`、`last_turn_tokens` 等） | **新增**五个计数 |
| `premiumRequests` | 无 | **新增** |
| `cost` | 无（`plan_gauge` 不是花费） | **新增** `cost_usd` |
| `credits` | 无 | **新增** `credits {cost,committedCost,acuCost}` |
| `routedModels` / `editRejections` | 无 | **新增** |
| `contextUsage` | `context_tokens/context_length` | **新增**聚合对象 `{tokens,contextWindow,percent}` |

## B.11 子代理

| omp | Hermes | 状态 | 处理 |
|---|---|---|---|
| `get_subagents` | `subagent.list {session_id}` | P | omp `{subagents:[{id,index,agent,agentSource,description?,status,task?,assignment?,sessionFile?,lastUpdate,progress?,parentToolCallId?}]}` vs Hermes `{subagents:[SubagentSnapshot],delegations,agents}`。逐字段见下 |
| `get_subagent_messages {subagentId?,sessionFile?,fromByte?}` | `subagent.tail {session_id,subagent_id}` | P | Hermes 只给 `{available,text,truncated}`；omp 要 `{sessionFile,fromByte,nextByte,reset,entries[],messages[]}`。**新增** `entries/messages/next_byte/reset/from_byte`，或新增 `subagent.messages` |
| `cancel_subagent {subagentId}` | `subagent.interrupt {session_id,subagent_id}` | P | 结果 `{found,subagent_id}`；**别名** `found`→`cancelled`（相反语义：`found:false` = 已结束 = `cancelled:false`） |
| `steer_subagent {subagentId,message}` | `subagent.steer {session_id,subagent_id,text}` | **F** | `subagentId`→`subagent_id`、`message`→`text` **别名**；Hermes 多 `status/subagent_id/text`，omp 无结果 |

`SubagentSnapshot` 字段对齐：

| omp | Hermes | 处理 |
|---|---|---|
| `id` | `subagent_id` | 别名 |
| `index` | — | **新增** |
| `agent` / `agentSource` | — | **新增** `agent`、`agent_source` |
| `description` | `goal`（近似） | **新增** `description` |
| `status`：`pending/running/completed/failed/aborted` | `queued/running/completed/failed/error/timeout/interrupted` | 枚举映射 |
| `task` / `assignment` | `goal` | **新增** |
| `sessionFile` | — | **新增** `session_file` |
| `lastUpdate` | — | **新增** `last_update` |
| `progress` | `tool_count/last_tool/...` | **新增** `progress:JsonValue` 或客户端投影 |
| `parentToolCallId` | `parent_id`（不同：父 subagent） | `parentToolCallId` **新增**；`parent_id` 无 omp 对应 |

## B.12 Voice / Live / Dictation

| omp | Hermes | 状态 | 处理 |
|---|---|---|---|
| `live_start {voice?,instructions?}` → `{voice}` | 无 | N | **新增** `voice.live.start {voice?,instructions?}` → `{voice}` |
| `live_connect {sdpOffer,voice?,instructions?}` → `{voice,sdpAnswer}` | 无 | N | **新增** `voice.live.connect` → `{voice,sdp_answer}` |
| `live_stop` | 无 | N | **新增** `voice.live.stop` |
| `live_mute {muted?}` → `{muted}` | 无 | N | **新增** `voice.live.mute` → `{muted}` |
| `dictation_start {captureId}` | `voice.record {action:"start",session_id,profile?}` | P | 结果 `{status:"recording"/"stopped"/"busy",reason?}`；**新增** `capture_id` 回显 |
| `dictation_stop` → `{text}` | `voice.record {action:"stop"}` + `voice.transcript` 事件 | P | omp 要同步 `{text}`；Hermes `VoiceRecordResult` 只有 `status/reason`。**新增** `text` 到 `VoiceRecordResult`（或客户端等待事件） |

Hermes 已有 `voice.toggle {action:status|on|off|tts}`、`voice.tts {text}`、`wake.*`；这些在 omp 命令表
里没有对应命令，属于“Hermes 多出”。

## B.13 BTW

| omp | Hermes | 状态 | 处理 |
|---|---|---|---|
| `btw {question,recordId?}` → `{record:BtwHistoryRecord}` | `prompt.btw {session_id,text}` → `{task_id}` | P | `question`→`text` 别名；`recordId` 无（**新增** `record_id`）；结果 `task_id` vs 完整 `record`。**新增** `record`（或客户端用 `btw.record` 事件补） |
| `btw_cancel {recordId?}` → `{cancelled}` | 无 | N | **新增** `btw.cancel {session_id,record_id?}` → `{cancelled:bool}` |
| `get_btw_history` → `{records:BtwHistoryRecord[]}` | `btw.history {session_id,limit?}` → `{items:[{id,question,answer,created_at,task_id}]}` | P | **别名** `items`↔`records`；`BtwHistoryRecord` 的 `status,createdAt,updatedAt,leafId,followUps,images,error` **新增**（Hermes 只有 `created_at` 字符串） |

## B.14 账号 / 登录

| omp | Hermes | 状态 | 处理 |
|---|---|---|---|
| `get_login_providers` → `{providers:[{id,name,available,authenticated}]}` | `model.options` → `providers[{slug,name,authenticated,auth_type,...}]` | P | `slug`→`id` 别名；`available` **新增**（omp 表示“此 provider 当前可登录”） |
| `login {providerId}` → `{providerId}` | 无（只有 `model.save_key`/`model.disconnect`；MCP 有 OAuth 流程） | N | **新增** `auth.login.start {provider_id}` → `{flow_id,auth_url}` + `auth.login.poll {flow_id}` → `{status:"pending"/"approved"/"error",provider_id,error?}`；OAuth 的 `open_url`/`input` 复用附录 A.12 |
| `get_logout_accounts {providerId}` → `{accounts:LogoutAccount[]}` | `auth.accounts {provider_id,profile?}` → `{accounts:[{credential_id,provider,label,detail,type,active}]}` | **P** | 已落地（B.18）；`providerId`→`provider_id` 别名；客户端须转换 `credentialId:number.integer`↔`credential_id:string` 与 `type:'api_key'\|'oauth'`↔`type:string`（枚举校验）。token 从不进入结果 |
| `logout {providerId,credentialId}` | `model.disconnect {slug}` | P | 粒度不同：Hermes 删该 provider **全部**凭据；omp 按 `credentialId` 删一条。**新增** `credential_id?: string` 到 `model.disconnect`（缺省即全删），`credentialId` 由客户端从 `number` 转字符串 |

## B.15 其它

| omp | Hermes | 状态 | 说明 |
|---|---|---|---|
| `predict_word {text,cursor}` → `{suffix}` | 无 | N | **新增** `predict.word {session_id,text,cursor}` → `{suffix:string or null}` |
| `predict_word_feedback {text,cursor,suggestion,accepted}` | 无 | N | **新增** `predict.word.feedback` |

### B.15.1 其余 N/P 项的完整 wire 定义

B.7.3 / B.9 / B.11 / B.12 / B.13 / B.14 的表格只给结论；这里补齐字段级定义
（`session_id` 统一由信封提供，写方法另带 `request_id`）：

```text
session.ask_dialog     {session_id, enabled: bool} -> {enabled: bool}

session.policy         {session_id, auto_compaction?: bool, cache_warming?: "off"|"streaming"|"idle",
                        auto_retry?: bool, slow_mode?: bool, slow_mode_scope?: string,
                        steering_mode?: "all"|"one-at-a-time", follow_up_mode?: "all"|"one-at-a-time",
                        interrupt_mode?: "immediate"|"wait"}
                      -> {applied: JsonValue}          # 只回显实际写入的键

session.events.filter  {session_id, events: string[]|null, message_updates?: "full"|"delta"}
                      -> {events: string[]|null, message_updates?: "full"|"delta"}
subagent.subscribe     {session_id, level: "off"|"progress"|"events"} -> {level: string}
subagent.messages      {session_id, subagent_id?, session_file?, from_byte?: int}
                      -> {session_file: string, from_byte: int, next_byte: int, reset: bool,
                          entries: JsonValue[], messages: JsonValue[]}
session.bash.abort     {session_id, process_id?: string} -> {cancelled: bool}
btw.cancel             {session_id, record_id?: string} -> {cancelled: bool}
predict.word           {session_id, text: string, cursor: int} -> {suffix: string|null}
predict.word.feedback  {session_id, text: string, cursor: int, suggestion: string, accepted: bool}
                      -> {ok: bool}
auth.login.start       {provider_id: string} -> {flow_id: string, auth_url?: string}
auth.login.poll        {flow_id: string} -> {status: "pending"|"approved"|"error",
                                           provider_id: string, error?: string}
voice.live.start       {session_id, voice?: string, instructions?: string} -> {voice: string}
voice.live.connect     {session_id, sdp_offer: string, voice?: string, instructions?: string}
                      -> {voice: string, sdp_answer: string}
voice.live.stop        {session_id} -> {stopped: bool}
voice.live.mute        {session_id, muted?: bool} -> {muted: bool}
```

`shell.exec` 的未落地补项字段类型：`output: string`、`cancelled: bool`、`timed_out: bool`、
`truncated: bool`、`total_lines/total_bytes/output_lines/output_bytes: int`、`artifact_id?: string`、
`artifact_elided_bytes?: int`、`artifact_error?: string`、`working_dir?: string`、`images?: JsonValue[]`。
`get_subagent_messages` 的 `entries[]/messages[]` 需要结构化 JSONL 事件日志（B.18），在此之前只保证
byte 偏移与文本 tail。

## B.16 汇总矩阵

| 命令 | Hermes 对应 | 状态 |
|---|---|---|
| `negotiate_protocol` | `client.capabilities`（协议版本组） | P |
| `prompt` | `prompt.submit` | P |
| `steer` | `session.steer` | P |
| `follow_up` | `prompt.submit{queued}` | P |
| `remove_queued_message` | `prompt.cancel` | P |
| `promote_queued_message` | — | N |
| `abort` | `session.interrupt` | **F** |
| `abort_and_prompt` | — | N |
| `abort_and_restore_queue` | — | N |
| `new_session` | `session.create` | P |
| `open_session` | `session.resume{workspace,latest}` | P |
| `get_state` | 多方法聚合 | P |
| `set_fast_mode` | `config.set{fast}` | P |
| `set_slow_mode` | — | N |
| `goal` | `session.control` / `session.automation` | P |
| `set_ask_dialog` | — | N |
| `get_available_commands` | `commands.catalog` | P |
| `get_entries` | `session.history` | P |
| `get_tree` | — | N |
| `set_todos` | — | N |
| `set_host_tools` | — | N |
| `set_host_uri_schemes` | — | N |
| `set_subagent_subscription` | — | N |
| `set_event_filter` | — | N |
| `get_subagents` | `subagent.list` | P |
| `get_subagent_messages` | `subagent.tail` | P |
| `cancel_subagent` | `subagent.interrupt` | P |
| `steer_subagent` | `subagent.steer` | **F** |
| `live_start` / `live_connect` / `live_stop` / `live_mute` | — | N |
| `dictation_start` / `dictation_stop` | `voice.record` | P |
| `set_model` | `session.mutate{model}` | P |
| `cycle_model` | — | N |
| `get_available_models` | `model.options` | P |
| `set_thinking_level` | `session.mutate{reasoning}` | P |
| `cycle_thinking_level` | — | N |
| `get_available_thinking_levels` | — | N |
| `set_steering_mode` / `set_follow_up_mode` / `set_interrupt_mode` | — | N |
| `compact` | `session.compress` | P |
| `set_auto_compaction` | — | N |
| `set_cache_warming` | — | N |
| `set_auto_retry` | — | N |
| `abort_retry` | — | N |
| `bash` | `shell.exec` | P |
| `abort_bash` | — | N |
| `get_session_stats` | `session.stats` | P |
| `get_session_lease` | `session.lease` | **F** |
| `export_html` | `session.export` | P |
| `switch_session` | `session.resume` | P |
| `branch` | `session.mutate{branch}` / `session.branch` | P |
| `fork` | `session.branch_whole` | P |
| `navigate_tree` | — | N |
| `abort_branch_summary` | — | N |
| `set_label` | — | N |
| `get_branch_messages` | —（可由 `session.history` 派生） | N |
| `get_last_assistant_text` | —（可由 `session.history` 派生） | N |
| `set_session_name` | `session.title` | **F** |
| `handoff` | —（`handoff.request` 语义不同） | N |
| `get_messages` | `session.history` | P |
| `get_messages_page` | `session.history`（需分页字段） | P |
| `get_login_providers` | `model.options` | P |
| `login` | — | N |
| `get_logout_accounts` | `auth.accounts` | P |
| `logout` | `model.disconnect` | P |
| `predict_word` / `predict_word_feedback` | — | N |
| `btw` | `prompt.btw` | P |
| `btw_cancel` | — | N |
| `get_btw_history` | `btw.history` | P |

统计（2026-11-01 复核）：**F 4**、**P 34**、**N 36**（共 74）。修正项见 B.0.3。

## B.17 契约改动清单

按 B.0.1 分层：**Hermes 只补语义字段/方法**；命名、枚举与信封别名不在此清单，统一在 B.0.2 的
客户端映射。每项都必须落在 `tui_gateway/contracts/*.py` 并重新生成；已落地子集见 B.18。

**Hermes 语义字段补项（P 项，低风险）**

1. `session.resume`/`session.create`：`provider`、`model_id`、`session_file`、`resumed`、`cancelled`。
2. `session.history`：`entries`、`leaf_id`、`cursor`、`limit`、`next_cursor`、`total_messages`、`since`。
3. `session.stats`：`session_id`、`user_messages`、`assistant_messages`、`tool_calls`、`tool_results`、
   `total_messages`、`tokens`、`premium_requests`、`cost_usd`、`credits`、`routed_models`、
   `edit_rejections`、`context_usage`。
4. `session.compress`：`short_summary`、`first_kept_entry_id`、`tokens_before`（或统一 omp 映射）。
5. `shell.exec`：`output`、`cancelled`、`timed_out`、`truncated`、`total_lines`、`total_bytes`、
   `output_lines`、`output_bytes`、`artifact_id`、`artifact_elided_bytes`、`artifact_error`、
   `working_dir`、`images`。
6. `commands.catalog`：`entries: AvailableSlashCommand[]`。
7. `model.options`：扁平 `models: ModelInfo[]`（含 `context_window`、`max_tokens`、`input`、
   `thinking.levels`、数值 `cost`）。
8. `subagent.list`：`index`、`agent`、`agent_source`、`description`、`task`、`assignment`、
   `session_file`、`last_update`、`parent_tool_call_id`、`progress`。
9. `subagent.tail`：`session_file`、`from_byte`、`next_byte`、`reset`、`entries`、`messages`。
10. `btw.history`：`status`、`updated_at`、`leaf_id`、`follow_ups`、`images`、`error`。
11. `prompt.submit`：`agent_invoked`、`streaming_behavior`；`images`/`attachments` 的 URL 变体。
12. `voice.record`：`capture_id`、`text`（stop 时）。

**新增方法（N 项；已落地的三条见 B.18）**

```text
# 已落地
client.capabilities 扩展        # 取代 protocol.negotiate
session.lease
auth.accounts

# 仍待定义/落地
session.state
prompt.promote_queued
prompt.abort_submit
prompt.abort_restore
session.tree / session.tree.navigate / session.tree.navigate.abort
session.entry.label
session.tree.branch_messages / session.last_assistant_text   # 可选；默认客户端派生
session.handoff
session.todos.set
session.policy            # auto_compaction / cache_warming / auto_retry / slow_mode / 三种 queue 模式
session.ask_dialog
session.events.filter
subagent.subscribe
subagent.messages         # 若不在 subagent.tail 上扩字段
retry.abort
session.bash.abort
voice.live.start / voice.live.connect / voice.live.stop / voice.live.mute
btw.cancel
auth.login.start / auth.login.poll
predict.word / predict.word.feedback
session.host.tools.register / session.host.uri_schemes.register   # 定义保留，当前不落地
session.model.cycle / session.thinking.cycle                      # 定义保留，默认客户端派生
```

**不建议新增（由客户端派生）**

- `get_branch_messages`：`session.history` 的 `role=user` + `row_id` 已足够；
- `get_last_assistant_text`：`session.history` 最后一条 assistant；
- `cycle_model` / `cycle_thinking_level`：`model.options` + `set_model`/`set_thinking_level` 已足够，
  避免把“轮换顺序”变成服务端状态（派生规则见 B.7.4）；
- `promote_queued_message` 若产品接受“先 cancel 再 submit”，可不新增（但会改变队列位置语义）；
- `set_host_tools` / `set_host_uri_schemes`：Hermes 在服务端执行/解析，注册无接收方；客户端本地
  no-op 并标 `unsupported`，不伪造 `host.tool.call`（A.13）。

**验收**

```bash
python scripts/gen_gateway_contracts.py --check
scripts/run_tests.sh tests/tui_gateway/contracts/
```

新增方法必须同时出现在 `METHODS`/`CANONICAL_METHODS` 与 `docs/omp-ui-panels.json` 的方法清单
（若面板会调用它）；omp 侧 `gateway-client.ts` 的映射表以本附录 B.16 为源，按 `type → method`
建表，错误码映射集中在 B.2.2 的一处函数。

## B.18 实现状态（2026-10-31）

口径同附录 A.15：**契约 + 真实发射器**。方法侧的“发射器”就是真实 handler 的返回值——只加
`Params`/`Result` 而不改 handler，会被 `tests/gateway/test_canonical_wire_contract.py`
（`CANONICAL_METHODS ⊆ served`）或 `registry.assert_complete`（声明必须有 handler）当场拒绝；
字段只加声明而不在 handler 里赋值同样是空转，因此下表只列**handler 真的从运行时数据填出来的字段**。

### 已落地

| 档 | omp 命令 | 契约 | 发射器（真实数据来源） |
|---|---|---|---|
| F | `abort` / `steer_subagent` / `set_session_name` | 既有 `session.interrupt`/`subagent.steer`/`session.mutate{rename}` 不变 | 已由 A/L 两条路径服务，无需改动；命名差异留在客户端别名层 |
| P | `get_entries` / `get_messages` / `get_messages_page` | `SessionHistoryParams += since/cursor/limit`；`SessionHistoryResult += entries/leaf_id/next_cursor/total_messages`；新增 `HistoryEntry` | `gateway/session_tui_ops.py::session_history` 与 sidecar 同名 handler：`db.get_messages_as_conversation(..., include_row_ids=True)` 投影后按 `row_id` 分页；`entry_id` 就是 durable `row_id`，`leaf_id` 取全量最大 `row_id` |
| P | `get_session_stats` | `SessionStats += session_id/tokens/context_usage/cost_usd/user_messages/assistant_messages/tool_calls/tool_results/total_messages/routed_models`；新增 `TokenUsage`/`ContextUsage` | `gateway/session_usage.py::stats`：token/context 取 agent 的 `session_*` 计数；角色计数用 `SessionDB.message_role_counts`（active display 行），`tool_call_count`/`cost_usd` 取 `sessions` 行（actual 优先、estimate 兜底），`routed_models` 取 `session_model_usage` 的 `SUM(api_call_count)`；每个 DB 读独立降级为 `null` |
| P | `get_available_commands` | `CommandsCatalogResult += entries`；新增 `CommandCatalogEntry` | `tui_gateway/command_discovery.py::command_catalog`：由 `pairs`/`canon`/`commands`/`SUBCOMMANDS`/`skills` 投影出 name/description/aliases/source(`builtin\|quick\|plugin\|skill`)/argument_mode/hint/subcommands；authority 与 sidecar 共用 |
| P | `get_available_models` / `get_login_providers` | `ModelOptionsResult += models`；新增 `ModelInfoEntry`/`ModelCost`/`ModelThinking` | `hermes_cli/inventory.py::project_model_infos`，由 `gateway/session_config.py::model_options` 与 sidecar `methods_complete.model.options` 调用：`agent.models_dev.get_model_info(..., allow_network=False)` 提供 name/modalities/context_window/max_output/数值 cost；`capabilities[model].reasoning` 与 `route_supported_efforts` 提供 thinking.levels |
| P | `bash` | `ShellExecResult += output/cancelled/timed_out/truncated/total_lines/total_bytes/output_lines/output_bytes/artifact_*/working_dir/images` | `gateway/session_shell.py::_run`：total_* 取**切片前**子进程输出，output_* 取实际过线的（redacted+sliced）文本，`working_dir = policy.cwd`；`cancelled` 恒 false（没有取消句柄）、超时仍以 `command_timeout` 错误返回故 `timed_out` 恒 false、artifact_* 恒 null（无 spill）——原因见下表 |
| P | `get_subagents` | `SubagentSnapshot += index/agent/description/task/assignment/session_file/last_update/progress/parent_tool_call_id` | `tools/delegate_tool_child_run.py::_register_child` 写 `task_index`/`agent_role`/`last_update`，`delegate_tool_progress` 在每次工具进度刷新 `last_update`；`methods_subagents._snapshot_row` 与 `gateway/session_ancillary._subagent_snapshot` 做同一投影（description/task/assignment 都是真实 `goal`；`progress` 由 `tool_count/last_tool/status` 组成）。`session_file`/`parent_tool_call_id` 运行时目前不持有，恒为 null（可选键已声明，等有来源再填） |
| P | `get_subagent_messages` | `SubagentTailResult += session_file/from_byte/next_byte` | 两条路径的 tail：`_live_transcript_path` 的 `size` 与 `max(0,size-16KiB)` |
| P | `cancel_subagent` | `SubagentInterruptResult += cancelled` | 两条路径都用中断是否被接受（= `found`）填 `cancelled` |
| P | `dictation_start` / `dictation_stop` | `VoiceRecordParams += capture_id`；`VoiceRecordResult += capture_id` | `tui_gateway/methods_voice.py::voice.record`：start 生成/回显 capture id 并记住，stop 返回并清空；不匹配时 `busy`/`capture_mismatch` |
| P | `get_btw_history` | `BtwHistoryResult += records`；新增 `BtwHistoryRecord` | `gateway/session_btw_history.py`：把既有 oldest-first `items` 反向投影为 newest-first `records`，`created_at_ms` 由存储的 ISO 解析，`status="completed"`（该 store 只写已完成答案） |
| **F** | `get_session_lease` | 新增 `session.lease {session_ids, profile?} -> {held, unknown}`（`contracts/lease_auth.py`） | `gateway/session_lease.py`：`active_session_registry_snapshot(registry_home=<profile home>, strict=True)`；registry 不可证明存活时全部进 `unknown`。不拿 `authority.sessions` 冒充租约 |
| **P** | `get_logout_accounts` | 新增 `auth.accounts {provider_id, profile?} -> {accounts:[{credential_id,provider,label,detail,type,active}]}` | `gateway/session_auth_accounts.py`：`agent.credential_pool.load_pool(provider_id).entries()` + `peek()`；只出非密标识，token 从不进入结果 |

生成物已重新生成：`omp-ui/src/modes/rpc/gateway-contract.generated.ts`、`gateway-contract.openrpc.json`
（`scripts/gen_gateway_contracts.py`）。F 档四条（含本次改判的 `session.lease`）无需再改；
`auth.accounts` 为 P（`credential_id`/`type` 需客户端转换），`negotiate_protocol` 由
`client.capabilities` 的 `protocol_version` 组承载，档位为 P，omp 侧映射见 B.0.2。

### 仍未落地（没有真实 producer 或运行时原语）

这些项**不能只补契约**：要么 handler 拿不到数据，要么需要先改运行时/存储。

| 项 | 缺什么 |
|---|---|
| `get_state`（`session.state`） | `is_compacting` 只有 `status.update kind=compacting` 事件，状态位在 `live.claim_gate`（与 FIFO 共用，需要独立 `live.compressing`）；`steering/follow_up/interrupt_mode`、`auto_compaction_enabled`、`slow_mode_*`、`fast_mode_active` 没有 per-session 存储；`session_file` 不存在（SQLite 单库）；`usage_limit` 只有 `plan_gauge{used_bp,...}`，与 omp `UsageLimitState` 结构不同；`systemPrompt` 是单字符串、`dumpTools` 只有工具名分组。客户端目前可用 `session.resume`+`session.info`+`session.stats`+`session.control.read`+`todo_state` 拼出可得的子集 |
| `promote_queued_message` / `remove_queued_message{message}` | authority 只有一条 FIFO（`seq` 自增主键，无 reorder）；按文本寻址没有索引，身份只有 `admission_id`/`input_id`。删除请用 `prompt.cancel` + `queue.updated` 里携带的 id |
| `abort_and_prompt` / `abort_and_restore_queue` | 原子“中断→准入/取回”需要一个 owner 侧原语；authority 队列行在 interrupt 后仍在（可逐行 cancel），legacy sidecar 在 interrupt 时直接清空队列（`session_lifecycle.py`），没有可恢复的数据 |
| `set_todos` | 只有读侧 `todo.updated`/`TodoState`；写需要同时落 paired assistant tool_calls + tool result 行并广播 `todo.updated`，当前没有 RPC 安全的写入口 |
| `session.policy`（auto_compaction/cache_warming/auto_retry/slow_mode）、`set_steering_mode`/`set_follow_up_mode`/`set_interrupt_mode` | 没有 per-session 策略字段；auto-compaction/auto-retry 只有全局配置/`agent.api_max_retries`；cache warming 功能不存在；steering/interrupt 语义由 `session.steer` vs `session.redirect` 两个动词表达，没有“模式”状态 |
| `set_ask_dialog` / `set_event_filter` / `set_subagent_subscription` | 没有 per-subscription 存储；fan-out 当前无条件；`messageUpdates:"full"` 也不可得——`message.delta` 只有增量 `text`+`segment/offset`，累积文本会按块清空 |
| `get_tree` / `navigate_tree` / `abort_branch_summary` / `set_label` | `messages` 是扁平表，无 `parent_id`/`leaf_id`/label 列；`session.mutate{rewind}` 是**删行**不是移动 leaf；branch 语义是“克隆成新会话”。entry id 可用 `row_id`，但树/标签需要 schema 变更 |
| `handoff` | Hermes 的 `handoff.request` 是把当前聊天绑定到消息平台 home channel，不是“新会话 + 摘要”；也没有 session 文件可回 `savedPath` |
| `compact` 的 `shortSummary` / `firstKeptEntryId` | 压缩结果只有 `summary{headline,token_line}` 与 before/after 计数；没有 short summary 与“首个保留 entry id”（entry id 在压缩后不保留） |
| `login` / `logout{credentialId}` | provider 登录只有阻塞式 CLI 函数（Nous/xAI/Codex/MiniMax/OpenRouter 的设备码或 PKCE），没有 flow_id 注册表 + poll；`model.disconnect` 仍按 provider 全删，按凭据删除虽可由 credential pool 支持，但契约/handler 未改（本次只落地了只读 `auth.accounts`） |
| `live_start` / `live_connect` / `live_stop` / `live_mute` | 没有 WebRTC/SDP 全双工音频传输（`realtime_voice` 硬编码 false），现有语音是本地麦克风 + VAD + 一次性 TTS |
| `dictation_stop` 的 `text` | 强制转写在后台线程完成并经 `voice.transcript` 事件送达；没有可 await 的同步取词接口（本次只加了 capture id） |
| `predict_word` / `predict_word_feedback` | 没有行内补全/反馈后端；`llm.oneshot` 是通用补全，不是低延迟 suffix 预测 |
| `abort_bash` | `shell.exec` 用 `subprocess.run`，不保留进程句柄，无法取消 |
| `abort_retry` | 重试退避在 agent 回合循环内部，没有 cancel token（只能整回合 hard interrupt） |
| `btw_cancel` | authority 的 `_side_agents` 以线程为键、task_id 只在线程名里；legacy sidecar 的侧问是裸线程，没有 handle。需要 `task_id → holder` 索引 |
| `get_subagent_messages` 的 `entries[]`/`messages[]` | 子代理 live transcript 是给人看的文本日志（工具参数/结果单行截断），不是结构化事件流；byte 偏移已落地，结构化条目需要 JSONL 事件日志 |
| `bash` 的 `timedOut`/`cancelled`/`artifact*` | 超时仍走错误码 `command_timeout`（不改现有客户端分支）、没有取消句柄、`subprocess.run` 也没有 spill 工件；要补齐需把 `shell.exec` 改走 terminal tool 的 Popen/process-registry/spill 机制 |

### 验证

```bash
.venv/Scripts/python.exe scripts/gen_gateway_contracts.py --check   # 生成物无漂移
.venv/Scripts/python.exe -m pytest -q \
  tests/gateway/test_session_history_pagination.py tests/gateway/test_session_stats_counters.py \
  tests/gateway/test_session_lease.py tests/gateway/test_auth_accounts.py \
  tests/gateway/test_btw_history_records.py                          # 23 passed
```

宿主限制：本机 checkout 位于 `%LOCALAPPDATA%\hermes\hermes-agent`，`scripts/run_tests.sh` 在本机
（WSL）因 `dirname: command not found` / 环境校验失败无法使用；以上用 `.venv` 直跑，并把
`HOME`/`USERPROFILE`/`LOCALAPPDATA`/`HERMES_HOME` 指向临时根以避开 real-home guard（该 guard 会
把 install manifest 的读取当成写真实 home）。依赖子进程重启的用例
（`test_session_subagent_controls`）在这种沙箱 HOME 下会触发 `hermes_bootstrap` relaunch，属宿主
环境限制，需在正常测试环境复跑。

