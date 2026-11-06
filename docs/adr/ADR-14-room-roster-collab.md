# ADR-14：Room 子系统——常驻 Roster、Callab、Inbox 与模板化报告

**状态**：已决定（2026-11-04）；未实现。分阶段：**P0** 持久层（room_agents / room_members /
room_events / room_messages / room_reports，幂等、lease、ack）；**P1** 常驻 runner（gateway 定时唤醒、独立
session、Callab 水位）；**P2** 三条工人 lane（`agent_run` 命名工人、`delegate_task` 短任务、
Kanban 持久任务）与模板化 md 报告；**P3** 每 agent 账本汇总与观测面；**P4** omp Collab host 适配器。

**关联**：`tools/agent_tool.py`（`agent`：命名 in-process run、prompt 文件、独立 JSONL、
`continue_from`）、`tools/agent_out_tool.py`（`agent_out`：`history` / `artifact` /
`ast_summary` / `inbox` 的持久读侧；ADR-16 P2 删除了 `final_response`）、`agent/agent_run.py`、`hermes_state_agent_sessions.py`
（`agent_session` 行）、`tools/delegate_tool.py`（`delegate_task`、`list_active_subagents`、
`steer_subagent`、`interrupt_subagent`）、`tools/delegate_tool_registry.py`、
`tools/kanban_tools.py`、`hermes_cli/kanban_db.py`、`hermes_cli/kanban_db_dispatch.py`、
`hermes_cli/kanban_ops.py`（board、dispatcher、`tasks`/`task_comments`/`task_events`/
`task_attachments`）、`hermes_cli/kanban_db.py:575-590`（attachment 根目录）、
`website/docs/user-guide/features/kanban.md`、`website/docs/user-guide/bot-mode.md`
（Groups and group chats）、`website/docs/developer-guide/delegation-provider-plugin.md`、
`website/docs/developer-guide/subagent-lifecycle-api.md`、`hermes_state_common.py`
（`session_model_usage`）、`hermes_state.py`（`SessionDB`）、`cron/` 与 `gateway/` 的调度面、
`agent/aux_accounting.py`、`agent/turn_facade.py`；omp 侧本地 checkout
`C:\Users\zarag\projects\oh-my-pi`（HEAD `6a86df85`，`@oh-my-pi/pi-coding-agent` 18.14.0）的
`packages/coding-agent/src/collab/{protocol,host,guest,crypto,relay-client,controller}.ts` 与
`packages/collab-web/scripts/local-relay.ts`。

**范围**：新增 Hermes 的 Room 子系统，承载「常驻 roster 成员 + 共享 Callab + 持久 Inbox +
模板化报告」，并给出 omp Collab 的 host 适配边界。复用 `delegate_task`、Kanban、SessionDB、
gateway 调度与账本；不新增一次性 subagent 启动器，不新增第二套计费，不移植 omp 的 Agent Hub
渲染。**不设计**：消息平台群聊适配、Desktop/Ink/ACP 客户端、生产 `my.omp.sh` relay、
浏览器客户端托管、`/share` blob、Text-to-speech 与语音会话。

**取证口径**：Hermes 侧引用本仓库当前工作树（2026-11-04）。omp 侧以本地 checkout
`C:\Users\zarag\projects\oh-my-pi`（HEAD `6a86df85`，`@oh-my-pi/pi-coding-agent` 18.14.0）为准：
`packages/coding-agent/src/collab/protocol.ts`（`CollabFrame` 联合类型、`encodeEntryFrame`/
`encodeEventFrame`/`encodeSnapshotChunk`、`formatCollabLink`/`parseCollabLink`）、`crypto.ts`
（AES-256-GCM、room key、write token）、`host.ts`、`guest.ts`、`relay-client.ts`、
`controller.ts`，以及 `packages/collab-web/scripts/local-relay.ts`。`omp://collab.md` 只作
行为摘要，不作为帧/加密定义的唯一依据。生产 Go relay 与其二进制未发布。

**取代关系**：ADR-12「不设计 collab」中的 **collab 一项**由本 ADR 取代；ADR-12 的 OMFG /
ratchet / prewalk / auto-graph 结论不变。ADR-4 的 eval 接缝、ADR-6 的 TTSR、ADR-11 的
judgment 引擎与 `question` 规则不变。Bot Mode 的 Group Chat **不被取代**：它是本 ADR 的一个
前端/使用场景，而不是另一套运行时。

**术语与 Hermes 映射**：本 ADR 不依赖任何外部插件。此前讨论中的原语按下表落到 Hermes 现有
能力：

| 讨论中的原语 | Hermes 等同物 |
|---|---|
| 命名 in-process 子 agent（prompt 文件 + 独立上下文 + 独立 JSONL） | `tools/agent_tool.py::agent`；`continue_from` 接续上一次 `agent_session_id`；`agent_out` 读 `history` 与 bundle 产物（`artifact` / `ast_summary`） |
| 子 agent 的最终结果/报告读取 | `tools/agent_out_tool.py::agent_out`（`artifact(kind=report|tool)`、`ast_summary`、`history`） |
| 短任务 fork→join | `tools/delegate_tool.py::delegate_task`（fresh conversation、独立 `task_id`/session、父只拿摘要） |
| 常驻成员 | gateway 在一条稳定 `sessions` 行里跑 turn；不是 `agent` 的临时 session |
| 完成通知队列 | `delegate_task` 的返回/异步结果；持久任务走 Inbox + Kanban 完成事件 |
| `agent_watch` | `delegate_tool_registry` 的活动子 agent 表、`list_active_subagents` / `steer_subagent` / `interrupt_subagent`；Agent Hub 只做投影 |
| token 账本 | `SessionDB.session_model_usage`：主 turn 走 `agent/turn_facade.py`，辅助调用走 `agent/aux_accounting.py::record_aux_usage` |
| 模板目录 | 新增 `agent/room/report_templates/*.md` 与 `$HERMES_HOME/rooms/templates/*.md` 覆盖层 |
| 报告存储 | Kanban `task_attachments`（`kanban_complete(artifacts=[...])` 的 durable 副本）；`room_reports` 只做索引 |

## 问题

Bot Mode 的 Group Chat 已经证明「多个命名 agent + 一份共享房间日志 + 串行回合 + gateway 持久
driver」可用，但它的成员是 **profile**：每个 Bot 有独立 config、memory、skills、credentials 和
聊天历史。用户要的是更轻的形态：

1. **roster 成员常驻**，按计划或 Inbox 唤醒，拥有独立上下文与独立账本；
2. **成员之间共享 Callab（协作日志）与 Inbox（定向消息）**，但不共享私有 transcript；
3. **工人由 roster 派发**，短任务走 `delegate_task`，持久任务走 Kanban；工人有自己的上下文和
   账本，结果向派发者报告；
4. **工人产出模板化 Markdown 报告**（不同任务不同模板），报告是文件 artifact，不是聊天长文；
5. 这套 Room 能与 **omp Collab** 对接：guest 看到实时 Callab、可提问，提问进入 Inbox，而不是
   直接驱动唯一 host agent。

现有能力各自缺一块：`agent` 已经是“命名 in-process agent + prompt 文件 + 独立 JSONL +
`continue_from`”，但没有常驻生命周期、房间广播或 Inbox，且 `agent_tool` 当前不传
`account_task`；`delegate_task` 是函数调用（fork → join），不持久、不可恢复；Kanban 是持久
队列，但 worker 是 OS 进程 + profile，身份过重，且没有房间级广播；Bot Mode 是 Desktop/gateway
的 profile 房间，2–6 成员、回合有上限；SessionDB 有独立会话和账本，但没有共享黑板；
omp Collab 有实时复制、guest 写权限和 E2E，但没有多 agent 路由。

## 决策

### D1：Room 是 Kanban board 的扩展，不新建并行队列

Room 的持久状态放在 **board 的同一个 SQLite**（`~/.hermes/kanban.db` 或
`~/.hermes/kanban/boards/<slug>/kanban.db`）里，新增：

```sql
room_agents(id, name, prompt_ref, model, toolsets_json, wake_policy,
            status, created_at, updated_at)
room_members(room, agent_id, role, session_id, ledger_key,
             PRIMARY KEY (room, agent_id))
room_events(id, room, seq, ts, actor, kind, task_id, visibility,
            body_md, artifact_path, UNIQUE (room, seq))
room_messages(id, room, sender, recipient, kind, correlation_id, body,
              status, attempts, lease_until, created_at, delivered_at, acked_at,
              UNIQUE (room, recipient, correlation_id, kind))
room_reports(id, room, task_id, agent_id, template, path, status,
             ledger_json, created_at)
```

理由：board 已经是 Hermes 的多 agent 持久面，自带 SQLite、隔离边界、dispatcher、CLI、dashboard、
artifact 存储和幂等键；再建一套队列会复制 lease/重试/artifact 逻辑，违反 footprint ladder 的
“先扩展现有代码”。Room 表与 Kanban 的 `tasks` **共存**：`tasks` 仍表示“要完成的工作”，
`room_events` 表示“房间里发生了什么”，`room_messages` 表示“谁要谁做什么”。

不把 Callab 做成 `tasks.comments`：comments 是任务私有的讨论串，Callab 是房间级广播；把两者
合并会让“广播”被当成“任务”，或让所有成员消费同一条任务评论。

现有 Kanban 表的复用边界必须先说清，避免“五张新表”被读成复制：

| 现有表 | 能力 | 本 ADR 的用法 |
|---|---|---|
| `tasks` | 持久工作、`assignee`、`status`、`idempotency_key`、`claim_lock`/`claim_expires`、`workspace_*` | **复用**；`assignee` 增加 `agent:<id>` 形态，工作仍归 Kanban dispatcher 或 Room runner 之一 |
| `task_comments` | 任务内 inter-agent 讨论串，带 author/时间 | **不复用为 Inbox**：没有收件人、lease、ack、幂等键；只作为任务内讨论 |
| `task_events` | 只追加、按 task 索引、gateway notifier 已 tail | **不复用为 Callab**：它是 per-task，且 notifier/GC 依赖 `task_id`；房间广播需要 `room` 维度，另建 `room_events` |
| `task_attachments` | 完成时把 workspace 文件复制到 durable per-task 存储 | **复用为报告存储**；`room_reports.path` 记录 attachment 路径 |
| `task_runs` | 每次 claim 的尝试、PID、heartbeat | 复用为 profile lane 的运行记录；agent lane 用 Inbox/lease 记录 |

因此新增的是 **Room 维度**（谁在房间里、广播、定向队列、报告索引），不是任务/评论/事件/
附件机制的替代。

### D2：agent 是一行，不是 profile

`room_agents` 是轻量身份：`name`、`prompt_ref`（`$HERMES_HOME/agents/<name>.md`；安装树默认
`agents/<name>.md`，复用 `agent` 工具的 project/profile/install 搜索路径）、`model`、
`toolsets`、`wake_policy`。默认不创建 profile，不复制 config/memory/skills。

- **resident**：每个 `(room, agent)` 绑定一条稳定的 Hermes `sessions` 行；上下文就是该 session，
  账本自然记到该 `session_id`。
- **worker**：`agent` 工具每次 mint 一个 `agent_session_id`，transcript 在父会话 bundle 的
  `<bundle>/<topic>.jsonl`，settle 另写 `<topic>.md` / `<topic>_ast.json`；需要延续时用 `continue_from`。这已经是
  现成的“独立上下文”，不需要 profile。
- 凭据仍属于 **profile**：prompt-only 成员在宿主 profile 的凭据下运行，模型与工具面按
  `room_agents` 收窄。需要独立凭据/独立 memory 的成员显式使用 `role="profile"` 并指定
  profile；这是 opt-in，不是默认。**不**为每个 Bot 建 profile。
- `room_members.role` 区分 `resident`（常驻、可被唤醒、可派发）与 `worker`（仅任务期间存在）。

### D3：四条执行 lane，不新增启动器

| lane | 触发 | 执行 | 适用 |
|---|---|---|---|
| **resident lane** | Inbox / @mention / cadence / deadline | gateway 在成员自己的稳定 `sessions` 行里跑一次 turn | 常驻 roster |
| **worker lane** | resident 派发任务 | `agent` 工具（或同一 `agent_run.run`）以 prompt 文件启动命名 run：独立 JSONL、`continue_from`、`agent_out` 读回 | 轻量工人、模板报告 |
| **delegate lane** | resident 在当前 turn 内需要答案 | `delegate_task` 构造 child `AIAgent`，fresh conversation，结果回父 | 短、同步、无需跨重启 |
| **profile lane** | Kanban dispatcher 领取 `assignee=profile:<name>` | 现有 OS 进程 worker，profile 身份 | 需要 profile 隔离、跨重启、human-in-the-loop |

- 常驻成员**不是** `agent` 工具的临时 run，也不是 `delegate_task` child：它需要跨重启的
  session 身份和稳定账本。
- 工人默认走 `agent_run.run`（`agent` 工具背后的同一 runner）：prompt 查找、独立 transcript、
  `continue_from` 和 `agent_out` 读侧都已存在。模型面仍可用 `agent` 工具；Room runner 在需要
  传 `account_task`/稳定 ledger session 时直接调 runner，不扩展工具 schema。
- `delegate_task` 仍是一次 RPC；它的结果不写入 Callab，除非 resident 显式落一条事件。
- Kanban 的 `idempotency_key`、lease、`task_attachments`、`kanban_complete(artifacts=[...])`
  被 worker lane 复用；Room runner 只补“唤醒 + 在 session 里跑 turn + 写 Callab/Inbox”这一小段。
- `agent` 工具的后台完成通知仍进 `process_registry.completion_queue`（调用者会话），**不是**
  roster 的 Inbox。Room runner 必须包住这次调用：同步模式直接拿结果，后台模式在 settle 后写
  `room_messages(kind=result)` 并登记报告；否则结果只回到发起者的宿主会话，其他成员看不见。

### D4：Callab 是只追加的房间事件日志，不是 transcript

`room_events` 的语义：

- `seq` 由房间写锁/事务分配，所有成员按同一顺序读；`visibility` 至少 `room` / `private`。
- `kind`：`task`、`dispatch`、`progress`、`result`、`report`、`decision`、`notice`。
- 正文是 Markdown 摘要；长内容只放 `artifact_path`，不把报告正文复制进日志。
- 成员每次唤醒只读**自上次水位以来**的事件（有界：最近 N 条 / 字符上限），作为本轮 prompt 的
  尾部 context block；**不**重写成员私有 history，**不**把 Callab 拼进 system prompt。
- 默认不写工具参数、命令输出、凭据；需要时用 `visibility=private`。

Callab 对应 omp Collab 的“房间可见面”，但不是 omp 的 session entries 的替代：Hermes 是权威源，
Collab 是复制/展示协议。

### D5：Inbox 是持久、定向、幂等的队列

`room_messages` 的约束：

- `UNIQUE (room, recipient, correlation_id, kind)`：同一结果重复投递只执行一次。
- `lease_until` + `attempts`：worker 崩溃后任务可被重新领取；超过次数进 dead-letter 状态。
- `kind=task` 指向 Kanban task；`kind=result` 必须带 `correlation_id`；`kind=notice` 不触发 turn。
- 投递是 at-least-once，消费侧用 `correlation_id` 幂等；**不**复用
  `process_registry.completion_queue` 作为跨 agent 队列（它是调用者会话的宿主通知，不持久、不可路由）。
- profile lane 的 Kanban 完成事件由 Room runner 读回，写成 `kind=result` 的 `room_messages`
  给派发者；agent lane 的 resident 自己写 `kind=result`，两条 lane 的收件人语义一致。

### D6：报告是 artifact + 模板，不是长消息

单一流程，不出现第二个报告目录：

1. 报告属于一个 Kanban task；worker（resident 或 `agent` run）把 `<template>` 渲染的 md 写进该
   task 的 workspace（`scratch` / `dir:` / `worktree`）。
2. 完成时调用 `kanban_complete(artifacts=[...])`；Kanban 把声明的文件复制到
   `task_attachments` 的 durable per-task 存储（`hermes_cli/kanban_db.py:575-590`、
   `store_attachment_bytes`/`add_attachment`）。
3. `room_reports.path` 记录 **durable attachment 路径**，`source` 记录
   `kanban_attachment:<id>`；Callab 的 `report` 事件只放路径 + 一句话结论，Inbox 的 `result`
   只放摘要 + 路径。

- 模板：`agent/room/report_templates/<name>.md`，用户可在
  `$HERMES_HOME/rooms/templates/<name>.md` 覆盖；任务字段 `report_template` 选择模板。
- front matter 含 `task_id / room / agent / status / ledger`；正文按模板 section 校验（复用现有
  内容校验工具）。
- `agent_out(artifact)` 只用于 `agent` run 自己的已落盘产物（`<topic>.md`、`<topic>/<step>.<tool>.log`），
  不作为报告仓库；若某条 run 的产物指向 workspace 文件，仍必须在完成时通过
  `kanban_complete(artifacts=[...])` 转成 attachment。
- **未声明 artifact 的 scratch 文件会随完成删除**；报告不能只存在于 worker 的临时目录。

### D7：上下文与缓存边界

- 每个 **resident** `(room, agent)` 一条稳定 `sessions` 行；worker 的 `agent_session` 是每次
  run 的独立 transcript。成员只读自己的 history + 本轮 Callab 增量。
- system prompt 稳定（成员定义 + 房间协议），Callab 增量放在 user/context 尾部；
  `cache_scope` 按成员固定，避免每次唤醒打断成员自己的前缀缓存。
- 工人的 `delegate_task` child 使用 fresh conversation；结果回父；不把父 history 复制给 child。
- 常驻唤醒必须合并：按 `@mention`、Inbox 地址、cadence 或 deadline 触发；默认对同一成员的连续
  事件做短窗口合并，避免每条 Callab 事件都花一次模型调用。

### D8：账本沿用 `SessionDB.session_model_usage`

- `session_model_usage.session_id` 外键指向 `sessions`；`agent_session_id` 不是 `sessions` 行，
  不能直接当账本键。
- **resident**：每个 `(room, agent)` 的稳定 `sessions` 行就是账本分区；主 turn 走
  `agent/turn_facade.py` 的写入，天然按 agent 分开。
- **worker**：`agent_run._account(history_key, account_task, ...)` 只在调用方传入
  `account_task` 时写账（`agent_tool._params` 当前没有传，所以内置 `agent` 调用默认不记
  worker 用量）。Room runner 调同一 `agent_run.run` 时必须传
  `account_task="room:<room>:<agent>"` 与一个账本 session；`task` 列提供每个 worker 的独立
  维度，不新建 session，也不新增计费表。因此 worker lane 的 Room 实现走 `agent_run.run`，而不
  只能走没有 `account_task` 的 `agent` 工具；工具 schema 保持模型面不变。
- Room 级汇总按 `ledger_key`（`room + ":" + agent`）查询；`room_reports.ledger_json` 只存
  task→usage 的引用，不复制 token 行。所有写入者最终收敛到 `session_model_usage`，没有第二条
  账本路径。

### D9：omp Collab 是 Room 的协议前端，不反向成为调度器

- **可用**：本地 checkout 的 `packages/coding-agent/src/collab/protocol.ts`（`CollabFrame`
  联合类型含 `welcome`、`snapshot-chunk`、`entry`、`event`、`state`、`bus`、`agents`、
  `ui-request`、`transcript`、`bye`、`error`；guest→host 的 `hello`、`prompt`、`abort`、
  `agent-cmd`、`fetch-transcript`、`ui-response`）、`crypto.ts`（AES-256-GCM、room key、
  write token）、`host.ts`、`guest.ts`、`relay-client.ts`、`controller.ts`；链接格式与
  `history` 的 `readOnly` 标志也在 `protocol.ts`。本地 relay 替身：
  `packages/collab-web/scripts/local-relay.ts`。
- **不可用**：生产 Go relay 及其二进制、官方浏览器客户端托管、`/share` blob、`/healthz`。不以
  host 身份接入 `wss://my.omp.sh`；联网使用自建 relay。
- 适配器：Hermes 是权威 host；`room_events` → `entry`/`event`；`room_messages` → guest
  `prompt` 的落点；`room_agents` + 活动子 agent → `agents`；报告 → artifact/`fetch-transcript`
  的只读链接。view-only 链接不携带 write token，其 `prompt` 必须被拒绝。
- 最小原型只实现 `welcome` + `snapshot-chunk` + `entry` + `event` + guest `prompt`；
  `ui-request`、`bus`、`agents`、`fetch-transcript` 后补。协议版本必须固定并记录在适配器里。

### D10：非目标

- 不把 Bot Mode Group Chat 合并进 Room；它继续是 Desktop 的 profile 房间，可作为 Room 的一个
  前端/演示。
- 不为每个 agent 建 profile，不复制 memory/skills/credentials。
- 不把 Callab 当任务队列，不把 Inbox 当广播。
- 不在本 ADR 设计消息平台群聊、Desktop/Ink/ACP 渲染。
- 不实现生产 relay、浏览器客户端托管或 `/share`。
- 不改 `delegate_task` 的 fork→join 语义；不把它变成常驻运行时。

## 备选与取舍

| 选择 | 结论 | 理由 |
|---|---|---|
| 新建独立 `rooms.db` + 独立 dispatcher | 否决（现阶段） | 复制 lease/幂等/artifact/观测；Kanban board 已是持久多 agent 面 |
| 复用 Bot Mode room 作为核心 | 否决 | 绑定 Desktop/gateway profile、2–6 成员、回合上限；不是核心子系统 |
| 每个 Bot 一个 profile | 否决 | 用户明确要求 prompt-only；profile 只作为 opt-in 的凭据/隔离边界 |
| 额外的一次性 spawner | 否决 | Hermes 已有 `delegate_task`；不新增第二个启动器 |
| Callab 与 Inbox 合并 | 否决 | 广播与定向指令的投递/幂等语义不同 |
| omp Collab 作为权威源 | 否决 | Collab 是单 host 会话复制；多 agent 路由与持久任务在 Hermes |
| omp Collab 作为前端 | 采纳（P4） | 帧/链接/加密/客户端均已公开；生产 relay 不发布，自建 |

## 后果

**正面**：

- 常驻、派发、报告、账本、审计都落在既有持久面；崩溃可恢复，重复投递可去重。
- prompt-only 成员避免了 profile 的配置/记忆/凭据复制；需要隔离时再显式升级为 profile。
- 短任务用 `delegate_task`，持久任务用 Kanban，语义边界清楚。
- omp Collab guest 能看到实时 Callab 并把 prompt 落入 Inbox，而不会绕过 Room 的权限与路由。

**负面/代价**：

- Kanban schema 增加五张 Room 表；`assignee` 需要区分 `agent:<id>` 与 `profile:<name>`。
- 新增 gateway Room runner（定时/Inbox 驱动的 turn），需要与 Kanban dispatcher、board 所有权、
  `hermes pause` 的全局暂停语义对齐。
- prompt-only 成员共享 profile 凭据；工具面必须按 `room_agents.toolsets` 收窄，否则越权。
- Collab 适配器是新的协议维护面；上游帧集变化需要版本化。
- 每成员一条 session + 独立缓存分片意味着成本随成员数线性增长；只能用合并唤醒和有界 Callab
  增量控制，不能消除。

**安全**：

- Callab 默认只放摘要/路径；私有 transcript 不跨成员。
- guest 的链接即权限；view-only 不得写。房间密钥与 write token 按 omp 的信任模型处理。
- 报告与 Callab 落盘前走现有 secret redaction；不要把工具参数、环境变量、凭据写进事件。

## 实施阶段与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0** | Room 五表、`UNIQUE` 幂等、lease/ack、dead-letter；`assignee` 两种 kind | 重复投递只执行一次；崩溃后任务可重领；迁移幂等 |
| **P1** | Room runner：定时/Inbox 唤醒、成员 session、Callab 水位、合并窗口 | 一个 resident 被唤醒两次，只读增量事件；私有 history 不被 Callab 重写；账本按 agent 分区 |
| **P2** | 三条工人 lane；`report_template`；attachment 持久化；结果回 Inbox | 命名工人走 `agent_run` 并可由 `agent_out` 读回；短任务走 `delegate_task` 并回父；持久任务走 Kanban 并可 `kanban_complete(artifacts=[...])`；报告路径在 Callab/Inbox 可见 |
| **P3** | 账本汇总视图、活动 agent 表、失败/重试观测 | 按 `ledger_key` 能读出每成员成本；活动工人可 `steer`/`interrupt`；dead-letter 可见 |
| **P4** | omp Collab host 适配器（自建 relay；最小帧集） | `omp join` 能加入自建 relay；guest prompt 进入 Inbox；view-only 写入被拒；Callab 事件按序出现在 guest 视图 |

## 风险与开放问题

1. **凭据隔离**：prompt-only 成员共享 profile 凭据。若一个成员需要不同 API key/组织，必须显式
   使用 `role="profile"`；本 ADR 不提供 per-agent 凭据池。
2. **Kanban 语义漂移**：Room 表与 `tasks` 同库，必须明确 `tasks` 仍归 Kanban dispatcher，
   `room_messages` 归 Room runner；不能让两个 dispatcher 抢同一行。
3. **全局暂停**：`hermes pause` 必须同时停 Room runner 的新 turn 与 Kanban dispatch；已在跑的
   turn 完成，不杀。
4. **Collab 协议版本**：帧集虽公开，但无稳定版本号；适配器必须固定上游 commit/版本，并在
   `welcome`/日志里记录，避免静默不兼容。
5. **报告完整性**：scratch workspace 完成即删；报告必须在 `kanban_complete(artifacts=[...])`
   里声明，否则只剩摘要。
6. **成本**：resident 合并窗口、Callab 有界增量、`cache_scope` 固定是三个必须同时成立的约束；
   缺一个就会出现“每事件一次全量模型调用”。
7. **多 profile/多 board 所有权**：Room runner 必须绑定 board 的 owning gateway（与 Kanban
   dispatcher 相同），否则两个进程会重复领取 Inbox。
