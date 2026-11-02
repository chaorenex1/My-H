# ADR-3：Background TAN 的忠实移植（`/tan` 后台快照克隆）

**状态**：已决定（2026-11-01）。实现未开始。分阶段：**P1** 网关命令 + 快照克隆 + 派发行；
**P2** 客户端 pill / 完成卡映射；**P3** 完成唤醒（automation admission）。落地前
`docs/omp-ui-panels-gateway-plan.md` §2.3.2.1 的 Background TAN 行保持 `部分`，不伪造块、不改渲染分支（§0.1）。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§0.3、§2.3.2、§2.3.2.1、§4.7、§9、§10；
ADR-2（D9 将 Background TAN 判为“无 producer”，本 ADR 取代该判断）。

**取代关系（逐条，避免文档互斥）**：

- ADR-2 第 186 行（D9 清单）与第 419 行（“无本机实现，按无 producer 处理”）中的 **Background TAN 一项**由本
  ADR 取代；D9 的其它项（Recap、TTSR、compressed summary 等）保持原判断。
- plan §0.3 把 `/tan` 列入“随本机业务代码删除的命令”：**保留**——`TanCommandController` 与本机 clone
  不恢复；本 ADR 只把同一能力重新实现为 gateway 命令。
- plan §2.3.2.1 第 438 行（Background TAN 标 `部分`）：实现完成后改为“已接（local）”；实现前保持
  `部分`，客户端不画假数据。
- plan §2.3.2 第 396 行的渲染器描述保持：`background-tan-message.ts` 仍是唯一 pill 渲染器；变化只在
  数据生产者。

## 问题

omp 的 Background TAN 不是“后台跑一条 prompt”，而是 `/tan <work>` 的**会话分叉**：

- 上游 `TanCommandController.start`（pinned snapshot `6cdda18efca`；删除前的同一文件见
  `git show 5bd7dabd213^:omp-ui/src/modes/controllers/tan-command-controller.ts`）
  把父会话的持久化 transcript fork 成一个后台 clone，clone 继承父的 system prompt、工具集、模型、
  thinking、cwd 与 artifacts，并以 `resetInheritedCost` 独立计费；clone 只处理 `work`，父会话继续原任务。
- 派发时 parent 写入一条 **custom message**：`content` 是 `background-tan-dispatch.md` 的
  `<system-notice reason="background_task_dispatched" job="…">`（给模型看），`details` 是
  `{jobId, work, sessionFile}`（给 UI 看），`triggerTurn:false`；父回合仍在流式时用
  `deliverAs:"nextTurn"` 排队，避免打断在途回答。
- UI 只画一行派发状态条：`background-tan-message.ts` 的 `createBackgroundTanDispatchBlock`
  （`BACKGROUND_TAN_DISPATCH_MESSAGE_TYPE = "background-tan-dispatch"`，
  `pi-tui/src/chat/messages.ts:25`、`chat-transcript-builder.ts:647`）。
- 任务完成走 async job delivery：parent 收到 `async-result` custom message（模型侧
  `<system-notice> Background job <id> has completed. Resume your work…`），UI 用
  `buildAsyncResultBlock` 画 “Background job completed” 行；不是派发行本身。

Hermes 现状：

- `/tan` 随 §0.3 的本机业务代码删除（`TanCommandController`、`promptCustomMessage`/`sendCustomMessage`
  分支均不在 `omp-ui/src`），客户端只剩渲染器；`BACKGROUND_TAN_DISPATCH_MESSAGE_TYPE` 仍由
  `@oh-my-pi/pi-tui` 提供。
- `prompt.background`（`/bg`）是**全新上下文**的 side agent（`gateway/session_tui_side.py:174`），
  不 fork transcript，结果只发 `background.complete {task_id,text}`，不写 parent transcript；
  与 TAN 语义不同，不能复用为 TAN。
- `session.mutate branch` / `branch_in_transaction` 要求 parent idle，创建 durable child 并把 viewer 切到
  child（`gateway_session_store.branch`、`gateway/slash_commands_session._handle_branch_command`）；TAN 必须
  能在 parent 流式中派发，且 parent 继续活跃，不能复用。
- 客户端 `rowToAgentMessage` 只投影 `user`/`assistant`/`tool`（`gateway-session.ts:357`），
  `addMessageToChat` 没有 custom 分支（`ui-helpers.ts:130`），所以即使网关写了派发行也画不出来。
- `background.complete` 在 omp 客户端未处理；ADR-2 D9 因此判“无 producer”。但把 `/bg` 的结果文本塞进
  派发状态条会丢失 `jobId`/`work` 语义，且完成结果不是派发行（上游是两个块）。

因此本 ADR 决定：把 TAN 作为**网关能力**重新引入，只恢复“数据生产者 + 映射”，不恢复 omp 的本机 agent
业务代码，也不改渲染器。

## 决策

### D1. `/tan` 是网关命令 + `prompt.tangent`，不是客户端本机命令

- `hermes_cli/commands.py` 增加
  `CommandDef("tan", "Run a tangent in a background fork of this conversation", "Session",
  args_hint="<work>", gateway_only=True, local_gateway_only=True, busy_policy="dispatch")`。
  `gateway_only=True` 让本机 CLI 帮助不列出它；新增的 `local_gateway_only=True` 让
  `tui_gateway/command_discovery._catalog_registry` 在本地 gateway 的 `commands.catalog` 中仍然列出它
  （现有 `_catalog_registry` 会直接跳过所有 `gateway_only`，见
  `tui_gateway/command_discovery.py:146`）。`complete.slash` 的 popover 走
  `SlashCommandCompleter`（其 built-ins 来自 `COMMANDS`，同样排除 `gateway_only`），所以
  `slash_completions` 必须在末尾 `_SLASH_EXTRAS` 的合并循环旁，把
  `hermes_cli.commands.local_gateway_commands()` 的条目去重后追加进 `items`（不能只改
  `_catalog_registry` 的 `cat.pairs`，那不进 completer）；
  `build_commands_by_category` 继续排除它（CLI 帮助不列）。`busy_policy="dispatch"` 让它在父回合
  流式中也能执行。
- `gateway/session_commands.py::execute_command` 在通用 busy/worker 分支之前拦截 `canonical == "tan"`，
  调 `gateway/session_tangent.py::start`；返回 `{type:"exec", output:"Dispatched background tan <id>"}`
  （上游同时 `showStatus`，不是 transcript 块）。`command.dispatch` 与 `complete.slash` 因此自动获得
  该命令；客户端**不**新增本机 `/tan` 条目，也不恢复 `TanCommandController`。
- 新增 canonical 方法 `prompt.tangent {session_id, text} -> {task_id}`：**只有 authority 服务**
  （`tui_gateway` sidecar 不实现），所以用 `tui_gateway/contracts/canonical.py::canonical_method` 声明，
  复用 `prompt_voice.py` 的 `SideAgentParams`/`TaskIdResult`（不要用 `method()` 放进 `METHODS`，
  否则覆盖门会把 sidecar 当成服务方）。供客户端、测试与脚本直接调用；
  `command.dispatch` 只是它的命令外壳。
- `/bg` 保持“全新上下文、事件即结果”；不把 `fork` 选项塞进 `prompt.background`，避免两个语义共享一个方法。

```text
prompt.tangent {session_id, text} -> {task_id}
```

### D2. 分叉 = 父 transcript 快照 + 独立 side agent，不是 durable branch

`gateway/session_tangent.py` 的克隆流程：

1. **快照**：有 live parent 时取 `authority.agent(ref)._session_messages`；无 live agent（已 evict/重连）时
   取 `transcript_target(authority, ref)` + `authority.db.get_messages_as_conversation(target,
   include_ancestors=True)`（`/refine` 的同一读法，`gateway/session_refine.py:39-44`）。
2. **尾部修剪**：`agent.side_question.trim_snapshot_for_fork` 丢弃未完成的尾部（未闭合 `tool_calls`、
   tool 结果、在途 user 行），只保留以完整 assistant 文本结尾的前缀。上游对未闭合调用补“unknown
   result”；本 ADR 选择丢弃，理由：clone 不应把父的在途工具调用当成自己的待办，且丢弃不改变任何
   已完成的上下文行。
3. **克隆 agent**：复用 `gateway/session_tui_side._side_scope` / `_side_agent` / `_spawn`
   （已有 profile 作用域、session 冻结路由、workspace 绑定、`_side_agents` 生命周期账本）：
   - `session_id = task_id = "tan_<hex>"`，`parent_session_id = ref.session_id`（仅记录 lineage，用于审计/清理；
     它不是 `session.mutate branch` 的 branch child），`session_db = authority.db`，
     `platform = policy.platform`，`side_agent=True`；因此有独立 session 行、独立 cost、独立终端/资源，
     不会关掉父的 terminal/browser/memory（这与 `_side_agent` 的既有边界一致）。
   - 模型/凭据/工具集走 session 的冻结 route（`_resolve_policy_agent_runtime`）；不继承 `/model` 这类
     一次性覆盖（与 `/bg` 一致）。
   - 有 live parent 且 model/provider 相同时，复制缓存前缀与工具面：
     `_cached_system_prompt`、`session_start`、`_inherited_cache_scope =
     resolve_prompt_cache_scope_safe(parent)`、`_prompt_cache_fork_tag = "tan"`，并用
     `agent/background_review._inherit_parent_tool_surface` 复制 `tools`/`valid_tool_names`。
     有 live parent 但模型不同则构建自己的 prompt（缓存本来就冷）；无 live parent 时同理。
     `_prompt_cache_fork_tag` 让 xAI 这类 slot-keyed 缓存在 clone 自己压缩后落到 `<scope>::tan`，
     不驱逐父的 slot（`agent/prompt_cache_scope.py:201-217` 的既有规则）。**这是有意的取舍**：只对
     一次性 side clone 复制 `_inherited_cache_scope`（与 `/btw`/review 同源），不用于 durable branch
     child，避免绕过 `is_explicit_fork_child` 的 fork 隔离；如果评审要求严格隔离，退化为冷前缀
     （见“未采纳”）。
   - **清空继承的 todo**：设置新标志 `agent._skip_todo_hydration = True`，并在
     `run_agent.py::_hydrate_todo_store` 早退；不靠删 history 行，避免破坏 assistant→tool 配对。
     否则 `_hydrate_from_history` 会把父的 todo 还原成 clone 的待办。
4. **给 clone 的上下文切换**：`run_conversation` 的 `user_message` 前加
   `tan-context-switch.md` 的 `<system-notice cause="fork">…`，并用
   `persist_user_message=<work>`，让 clone 的 DB 行只显示工作内容，模型仍看到“只处理本次请求、
   父会话继续、不要跟进旧任务”的约束。`conversation_history=trimmed_snapshot`。
5. **不建 branch child、不跑 `/branch`**：TAN 必须在父流式中派发且父保持活跃；`branch` 的
   `require_idle`、viewer 切到 child、durable child 都不符合。clone 是带 `parent_session_id` lineage 的
   `tan_*` side session（与 `/bg` 的 `bg_*` 同约定，用于独立 cost/审计），不是父会话的 branch child、
   不出现在父的 `session.history` 里，也不作为 UI 入口（Agent Hub/`sessionFile` 已随 §0.3 删除）。

### D3. 派发行：persist 一次、排队到回合边界、只 append

行形状（`role="user"`，模型可见）：

```text
content       = render(background_tan_dispatch, {jobId, work})   # 与上游同文案
display_kind  = "tangent_dispatch"
display_metadata = {"tangent_dispatch": {"jobId": task_id, "work": preview(work)}}
```

- **不能**用 `display_kind="hidden"`：`_history_to_messages` 会跳过 hidden（`session_history.py:315`），
  resume 后派发行丢失；也不要新增 transcript 行类型。内容保持上游的 `<system-notice …>`，**不要**
  用 `[System:` 开头：`_is_display_hidden_marker` 在读取 `display_kind` 之前就过滤 role=user 的
  `[System:` 行（`session_history.py:322-323,228-236`），否则 omp 的 pill 在 resume 时直接消失。
- **idle**：`authority.db.append_display_row(...)`（新增的公开包装，内部复用
  `_append_detached_result` 的 active-lease 拒绝、去重与压缩 lineage 逻辑；派发行的去重键是
  `json_extract(display_metadata, '$.tangent_dispatch.jobId')`）立即写，然后 publish
  `tangent.dispatched`；这与上游 `triggerTurn:false` 的“只 append、不跑回合”一致。若写入时正好有
  回合拿到 lease（idle 判断后的争用），按 running 处理，转入 pending。
- **父回合 running**：不立即写库（否则行会落在在途 assistant 行之前，且会被下一轮工具循环读到）。
  写入 `live.pending_display_rows`；在下一个 admission 的 `gateway/session_ingress.execute_admission`
  最开头（`note_if_unviewed` 与 `_handle_message` 之前）flush，使它落在下一条 user 行之前、上一轮
  assistant 行之后；回合结束后再 best-effort flush 一次，保证 resume 立刻可见。flush 复用
  `_append_detached_result` 的“拒绝 active turn lease + 去重 + 压缩 lineage”逻辑
  （`hermes_state_detached_results.py:40-59`）。
- 立即 publish `tangent.dispatched {task_id, row_id?, work_preview, timestamp}`；running 时 `row_id`
  先缺省，flush 后不重发（客户端已画 pill，resume 用行）。
- 派发行是模型通知，不是用户气泡；omp 客户端按 `display_kind` 映射（D5）。**不要**在内部事件的
  `metadata` 里给它 `display_text`（`_producer_card` 会把它当成 automation producer card）。

### D4. 完成 = `tangent.complete` + async-result 行 + 父回合唤醒

任务结束时（成功/失败）：

1. publish `tangent.complete {task_id, text, label, status, duration_ms}`（`label = "/tan <preview>"`，
   `text` 是 clone 的最后 assistant 文本，失败时为错误文本；截断到 12k 字符并附
   `…(truncated)`，避免超大 user 行）。该事件是 UI 的即时完成卡；不是派发行。**不移植**
   `agent://`/`history://` 句柄：Agent Hub 已随 §0.3 删除，结果全文在完成行/通知里（12k 截断）。
2. **唤醒父会话**：在 side-agent 线程里用 `loop.call_soon_threadsafe` /
   `asyncio.run_coroutine_threadsafe` 把 admission 投到 owner loop，构造内部事件并
   `authority.admit_automation`：
   - `MessageEvent(text=<async-result system-notice>, source=live.source, internal=True)`，元数据：
     `gateway_session_key=live.route`、`gateway_session_id=route_entry(...).session_id`、
     `display_kind="async_result"`、`display_text="Background job completed: /tan …"`、
     `display_metadata={"async_result": {"jobId": task_id, "type": "task", "label": …, "status": …, "durationMs": …}}`。
   - `identity = "tan:" + task_id`，同一任务重放只入队一次；`admit_automation` 的队列天然把结果排在
     在途回合之后，等价于上游 async-result follow-up。
   - model 文本就是 async-result 通知（“Background job … completed. Resume your work …” + 结果），
     父 agent 因此在下一条 turn 继续工作，而不是只看到一条旁注。
3. **持久化**：admission 的 user 行就是完成行，带 `display_kind="async_result"` 与
   `display_metadata.async_result`；resume 后 omp 客户端还原成完成卡。无需再写第二条 row。
4. **没有 local adapter / admission 不可用**（headless、关闭中、managed）：回退为
   `append_display_row(..., display_kind="async_result",
   display_metadata={"async_result": {jobId, type:"task", label, status, durationMs},
   "display_text": "Background job completed: /tan …"})` + `tangent.complete` 事件，**不唤醒**；
   行仍在，下一次用户回合模型会看到它。这是降级路径，不是正常路径。
5. **失败**：`status="failed"`，仍走完成行/唤醒（让父知道失败并决定下一步），文本以
   `tangent failed: …` 开头；**取消/中止**（关闭、retire、显式 interrupt）只发
   `tangent.complete{status:"cancelled"}`，不唤醒、不写行（不制造一条“完成”）。

### D5. 客户端映射：复用既有渲染器，不改布局

- `omp-ui/src/modes/rpc/gateway-session.ts::rowToAgentMessage` 增加：
  - `display_kind === "tangent_dispatch"` → `{role:"custom", customType:"background-tan-dispatch",
    content:text, details:display_metadata.tangent_dispatch, display:true, timestamp}`；
  - `display_kind === "async_result"` → `{role:"custom", customType:"async-result", content:text,
    details:display_metadata.async_result, display:true, timestamp}`。
- `#notificationHandlers` 增加 `tangent.dispatched` 与 `tangent.complete`：
  前者 emit 一条 custom message 的 `message_start`/`message_end`（不置 `#running`）；后者在
  `#messages` 中按 `details.jobs[].jobId` 去重后 emit 完成卡。`message.start{display_kind,
  display_metadata}` 也要在 `#onMessageStart` 里先画 producer card 再开 assistant draft，避免
  automation 回合只出现回答、没有完成卡（ADR-2 已指出该缺口）。
- `omp-ui/src/modes/utils/ui-helpers.ts::addMessageToChat` 增加 `role:"custom"` 分支：
  `background-tan-dispatch` → `createBackgroundTanDispatchBlock`，`async-result` →
  `buildAsyncResultBlock`；`display:false` 直接返回。渲染器、文案、折叠/预览规则全部复用
  `@oh-my-pi/pi-tui`，不改 `background-tan-message.ts`/`transcript-render-helpers.ts`。
- 快照侧不改：`_history_to_messages` 已原样转发 `display_kind`/`display_metadata`
  （`session_history.py:364-368`）；`rowToAgentMessage` 是唯一投影点。
- 去重：同一条消息可能来自 `notice`/`message.start`/完成事件/resume 快照；按
  `(customType, details.jobs[0].jobId)` 归一，resume 的 `adoptSnapshot` 整体替换 `#messages`，
  不保留 live 副本。

### D6. 作用域：只做 local 交互式会话，其它 surface 明确拒绝

允许：omp-ui 的 canonical gateway local viewer 会话，有 `Platform.LOCAL`
source、adapter 可授权、`authorize_local_source` 通过。

拒绝（返回 `unsupported_command` / 明确提示，不落行、不 spawn）：

- 消息平台（Telegram/Discord/Slack/…）：`gateway/slash_commands.py` 的 `/tan` 分支直接拒绝；
  `CommandDef(gateway_only=True)` 只是不列本机 CLI，不等于允许消息平台。
- subagent（`is_subagent`/`_parent_session_id`）、finite turn（`finite_turn_required()`）、cron、kanban。
- managed/remote worker：worker 帧里没有父 transcript/本地 adapter，TAN 无法忠实；`prompt.tangent`
  返回 `unsupported_command`，不伪造空 clone。
- history mount / read-only viewer：`is_history_mount` 拒绝（与 `/branch` 的只读判定一致）。

`/bg` 不变；`session.history`、`session.mutate`、`session.list` 不新增方法。

### D7. 生命周期与失败

- dispatch 前完成全部校验（text、local surface、adapter、policy、model、非 managed、admission open）；
  校验失败不写派发行、不 spawn。
- clone 线程进入 `authority._side_agents` 账本（`_spawn` 既有），restart/shutdown/retire 按 `/bg` 的
  规则 interrupt + join；`_abandoned` 时跳过完成事件/唤醒。
- clone 线程启动成功后才写派发行（快照已在 spawn 前取好；行写失败则 interrupt 线程并返回错误）；
  线程启动失败不写行。行写成功后的创建/运行失败发 `tangent.complete{status:"failed"}`，按 D4 唤醒/写完成行。
- 结果文本截断 12k；work 在 `display_metadata` 中截断到 2k 预览（完整 work 在模型通知里）。
- 没有重试、没有 `session.rewind` 交互、没有 jobs 面板条目；取消只走既有 `/stop`/关闭路径
  （由 `_side_agents` 的 interrupt 处理）。

## 协议

```text
prompt.tangent (new)
  {session_id, text} -> {task_id}

tangent.dispatched (new)
  task_id:      string
  row_id?:      int              # running 时先缺省，flush 后由 history 提供
  work_preview: string
  timestamp:    float

tangent.complete (new)
  task_id:      string
  text:         string           # clone 最终文本，≤12k；失败为错误文本
  label:        string           # "/tan <preview>"
  status:       "completed" | "failed" | "cancelled"
  duration_ms:  int

message.start (additive, P3)
  display_metadata?: object      # automation 回合把完成卡细节带进 live 视图

transcript row (display-only; model sees content)
  display_kind = "tangent_dispatch"
    display_metadata = {"tangent_dispatch": {jobId, work}}
  display_kind = "async_result"
    display_metadata = {"async_result": {jobId, type: "task", label, status, durationMs},
                        "display_text": …}
```

`background.complete` 不变（`/bg` 仍只发事件、不写 parent transcript、不唤醒）。TAN 的完成使用独立
`tangent.complete`，避免把 `/bg` 的“全新上下文结果”误当成 TAN 的完成卡。

## 实现落点

| 层 | 位置 | 改动 |
|---|---|---|
| 命令 | `hermes_cli/commands.py` | `CommandDef("tan", …)`；`gateway_only=True`、`local_gateway_only=True`、`busy_policy="dispatch"`；`local_gateway_commands()` helper |
| 命令 | `tui_gateway/command_discovery.py::_catalog_registry` | 允许 `gateway_only` 且 `local_gateway_only` 的命令进入本地 catalog（CLI 帮助仍排除） |
| 命令 | `tui_gateway/command_discovery.py::slash_completions` | 把 `local_gateway_commands()` 并入 popover items（`SlashCommandCompleter` 的 built-ins 不含 `gateway_only`） |
| 命令 | `gateway/session_commands.py::execute_command` | `tan` 早期分支 → `session_tangent.start`；不经过 busy/worker 表 |
| 命令 | `gateway/session_controls.py::handlers` | 注册 `session_tangent.handlers(self)`（`prompt.tangent`） |
| 契约 | `tui_gateway/contracts/canonical.py` | `canonical_method("prompt.tangent", params=SideAgentParams, result=TaskIdResult)`；复用 `prompt_voice.py` 的类型 |
| 任务 | `gateway/session_tangent.py`（新） | 快照/修剪、clone 构建、派发行、排队 flush、完成事件、admission |
| 任务 | `gateway/session_tui_side.py` | 复用 `_side_scope`/`_side_agent`/`_spawn`；`_spawn` 增加完成回调（status/duration/extra） |
| 任务 | `gateway/session_tui_side.py::_side_agent` | `parent_session_id` 覆盖；clone 的 session 身份 |
| 任务 | `agent/background_review.py` | 复用 `_inherit_parent_tool_surface`；不改 fork builder |
| 任务 | `agent/prompt_cache_scope.py` | 已支持 `_inherited_cache_scope` + `_prompt_cache_fork_tag`，不改 |
| 任务 | `run_agent.py::_hydrate_todo_store` | `_skip_todo_hydration` 早退（clone 专用） |
| 行 | `hermes_state_detached_results.py` | 新增 `append_display_row(...)`：包装 `_append_detached_result`（按 job id 去重、拒绝 active lease、压缩 lineage）；idle 派发行与降级完成行共用 |
| 行 | `hermes_state_messages.py::append_message` | 不需要新字段；`append_display_row` 走同一 INSERT |
| 行 | `gateway/session_authority.py::LiveSession` | `pending_display_rows`；`_execute_claimed`/settlement 后 flush |
| 行 | `gateway/session_ingress.py::execute_admission` | 在 `_handle_message` 前 flush pending rows |
| 完成 | `gateway/session_tangent.py` | `tangent.complete` + `admit_automation`；admission 不可用时回退 `append_display_row` |
| 完成 | `gateway/session_automation.py::automation_display_metadata` | 接受 `display_metadata`（仅 `async_result`，字段/大小校验） |
| 完成 | `gateway/session_automation.py::admission_display` | 把 `display_metadata` 带进 `message.start` |
| 完成 | `gateway/response_filters.py` | `PRODUCER_NOTICE_DISPLAY_KINDS` 加 `async_result`；`_producer_card` 把描述符的 `display_metadata` 并进 user 行顶层 metadata；`display_metadata_for_event` 转发 |
| 完成 | `gateway/session_automation.py::_AUTOMATION_METADATA` | 允许 `display_metadata` |
| 契约 | `tui_gateway/contracts/events.py` | `MessageStartPayload.display_metadata`；`TangentDispatchedPayload`/`TangentCompletePayload` |
| 生成器 | `scripts/gen_gateway_contracts.py` | 重跑并提交 `gateway-contract.generated.ts` / OpenRPC |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.ts` | `rowToAgentMessage`、`#notificationHandlers`、`#onMessageStart` |
| 客户端 | `omp-ui/src/modes/utils/ui-helpers.ts` | `addMessageToChat` 的 custom 分支（只接 TAN/async-result） |

## 未采纳

- **复用 `/bg`（`prompt.background`）当 TAN**：它是全新上下文，父 transcript 不进 clone；结果也没有
  `jobId`/`work` 的派发语义。
- **复用 `session.mutate branch`**：要求 parent idle、创建 durable child 并把 viewer 切到 child；TAN 要在
  流式中派发且 parent 继续活跃。
- **恢复 `TanCommandController`/本机 clone**：违反 §0.3；会在客户端跑第二个 agent，绕过 gateway 的
  profile/路由/成本/生命周期。
- **把 `background.complete` 填进派发状态条**：派发行是“已派发”面包屑，完成是另一个块；ADR-2 D9 的
  结论是这个语义不能混。
- **派发行用 `display_kind="hidden"`**：`_history_to_messages` 会丢弃，resume 后消失；hidden 是模型脚手架，
  不是 UI 行。
- **流式中立即写派发行**：会落在在途 assistant 行之前，且被下一轮工具循环读成新上下文；用 pending +
  admission 边界 flush。
- **`build_cache_parity_fork` 原样用于 TAN**：它 `_persist_disabled`/`_session_db=None`/共享 parent
  `session_id`/工具白名单，适合 review 与 `/btw`，不适合一个要独立计费、跑完整工具的 clone。
- **完全不复刻缓存前缀**：可作为严格隔离的保守选项（删除 `_inherited_cache_scope` 复制，接受一次冷
  前缀）；本 ADR 选择复刻，因为 TAN 是一次性 side clone，与 `/btw`/review 同源。
- **把完成结果只发 `background.complete` 当 TAN 完成卡**：会混淆 `/bg` 的纯文本事件；TAN 用
  `tangent.complete` + async-result admission 行 + 父回合唤醒。
- **只发事件、不唤醒父回合**：父 agent 不会继续，丢失 omp async-result 的 follow-up 语义；
  仅在 admission 不可用时降级为“行 + 事件，不唤醒”。
- **新增 `session.tangent`/`session.branch` 方法**：`prompt.tangent` + `session.mutate` 已覆盖；不扩大
  session 方法面。

## 风险

| 风险 | 缓解 |
|---|---|
| clone 继承父的在途半截回合 | `trim_snapshot_for_fork` 丢弃未完成尾部；不把父的 pending tool 调用当 clone 待办 |
| 派发行插在在途回答之前 | pending 到下一 admission/回合结束后 flush；验收比较行序 |
| 父回合的 prompt 前缀被改写 | 只 append；派发行在回合边界进入 history，不改已发送请求 |
| clone 与父并发编辑同一 cwd | 上游的 context-switch 文案已明确警告；clone 不接管父的 todo/计划 |
| clone 继承父 todo 并触发提醒 | `_skip_todo_hydration`；clone 的 TodoStore 为空 |
| 同模型缓存冷启动 | 有 live parent 且同 route 时复制 `_cached_system_prompt`/tools/`_inherited_cache_scope`；否则接受一次冷前缀并记录 |
| `_inherited_cache_scope` 绕过 fork 隔离 | 只用于一次性 side clone（与 `/btw` 同源），不用于 durable branch child；若要求严格隔离，删除该复制、接受冷前缀 |
| xAI slot 驱逐 | `_prompt_cache_fork_tag="tan"`，clone 自己压缩后落 `<scope>::tan` |
| 完成结果被投成用户气泡 | `display_kind="async_result"` + omp custom 映射；模型文本只在通知里 |
| 完成重复入队/重复卡片 | `identity="tan:<task_id>"` + 客户端按 job id 去重；resume 用快照替换 |
| 父会话在完成前关闭/删除 | `_abandoned`/admission 拒绝时跳过唤醒；不写悬挂行 |
| 无 local adapter（headless/managed） | `/tan` 直接拒绝；运行中 adapter 消失则降级为 detached 行 + 事件，不唤醒 |
| 完成唤醒让父在无人输入时多跑一个回合 | 这是 omp async-result 的既有语义；admission 排在在途回合之后，不 steer；`/stop` 仍可中断 |
| 消息平台误开 `/tan` | `start` 检查 `Platform.LOCAL`；消息平台返回明确拒绝，不 spawn |
| 结果文本过大 | 12k 截断 + `…(truncated)`；display_metadata 只存 jobId/label/status/duration |
| 子代理/cron/kanban 递归 | `is_subagent`/`finite_turn_required()`/kanban 跳过 |

## 验收

1. idle `/tan work`：只写一条 `tangent_dispatch` 行（内容=上游 system-notice），只发一次
   `tangent.dispatched`；父不产生回合；客户端画出 pill；`session.history` resume 后 pill 仍在。
2. 父回合流式中 `/tan`：在途请求的 messages 前缀逐字节不变；派发行在 assistant 行之后、下一条 user
   行之前落库；`tangent.dispatched` 即时可见；没有把派发行注入在途请求。
3. clone：首条请求带父 transcript（尾部未完成回合已修剪）+ context-switch 通知；看不到派发通知；
   看不到父 todo；工具集与父同 route 一致；cost 记在 `tan_*` session，父 usage 不变；不创建 branch/
   Agent Hub 入口。
4. 完成：`tangent.complete` 一次；父收到 automation 回合（`display_kind="async_result"`，metadata 含
   jobId/type/label/status/duration），模型文本要求 resume；resume 后完成卡仍在，且不是 user 气泡；
   同一 task id 的事件/快照不重复画卡。
5. admission 不可用（headless/关闭中/managed）：回退为 `async_result` 行 + `tangent.complete` 事件，
   不唤醒、不报错、不丢行。
6. 失败：dispatch 校验失败无行无事件；clone 运行失败有完成卡（failed）且父被告知；取消/关闭只有
   `tangent.complete{cancelled}`，无唤醒、无行。
7. `/bg`：行为与现状逐字段一致（事件 `background.complete`、无 parent 行、无唤醒）。
8. 作用域：消息平台、subagent、`-z`/`chat -q`、cron、kanban、managed/remote、history mount 全部拒绝
   `/tan`，不 spawn、不落行。
9. `python scripts/gen_gateway_contracts.py --check`、`python scripts/gen_panel_inventory.py --check`、
   `tests/tui_gateway/panels/test_panel_coverage.py` 通过；`bun run check` 通过；新增
   `tests/gateway/test_session_tangent.py` 覆盖 1–8 的服务器侧；客户端测试覆盖
   `rowToAgentMessage`/`tangent.dispatched`/`tangent.complete` 去重。

## 对 §2.3.2.1 的修正（本 ADR 取代）

| 原判断 | 修正 |
|---|---|
| Background TAN“本机 `/tan`、clone 与结果都不进 Hermes transcript；无 producer” | 决定做：网关 `/tan` + 快照 clone + `tangent_dispatch` 行 + async-result 完成行；只做 local 交互式 |
| 跨区块第 3 条“`promptCustomMessage`/`sendCustomMessage` 无网关分支” | 不再需要本机 custom message；`/tan` 是 gateway command，客户端只映射行/事件 |
| `background.complete` 未处理 | 仍不映射为 TAN 派发行；TAN 用独立 `tangent.dispatched`/`tangent.complete` |
| “Background TAN 是部分，缺 producer” | 实现后为“已接”（local）；消息平台/headless 仍为不支持，按 D6 拒绝 |
