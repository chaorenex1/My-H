# ADR-9：Late diagnostics 作为 Hermes 的 `lsp-late-diagnostic` custom transcript 行

**状态**：已决定（2026-11-04）。**P1 已落地**（`lsp-late-diagnostic` custom 行、三个投递点、
`transcript.custom`、omp-ui 渲染、`lsp.display.lsp`）；**P2 已落地**（Semgrep 与 LSP 同预算/同
`deferred` 队列/同行，`semgrep_diagnostics` 内联字段，`refresh_file(semgrep=true)` 与
`code_intel(diagnostics, path=...)` 的即时结果，`lsp.display.semgrep`）；**P3 已落地**（同一投递点
的多条 verdict 合并为一行、`files` 按 `deferred` pop 顺序且同 path 唯一，组内保持 source order、
壳不二次排序；
`deferred` 以 `realpath` + Windows `normcase` 作为队列身份，展示/投递仍用调用方的路径；相对写入
路径按后端 cwd 解析；provider `developer` 角色在 OpenAI/Anthropic 适配器的兼容与内部字段剥离有
回归；展示/模型行数与字符上限、无效 `lsp.display` 回退与 `hermes lsp status` 输出有回归）。分阶段：**P1** LSP 延迟 verdict 变成
`role:"custom"` 行（`custom_type:"lsp-late-diagnostic"`），在三个既有投递点追加同一行，发一次
`transcript.custom` 实时事件，omp-ui 复用 `LateDiagnosticsMessageComponent`，快照/resume 直接由行
重建；同时接 `lsp.display.lsp`（`off|inline|late|both` + `error|warning`）；**P2** Semgrep 危险模式
诊断与 LSP 并列（同一 `WRITE_BUDGET`/`deferred` 队列/同一行），内联工具结果与即时查询，并接
`lsp.display.semgrep`；**P3** 同一 turn 多条 verdict 合并/排序、无效配置回退与展示口径硬化。落地后
`docs/omp-ui-panels-gateway-plan.md` §2.3.2.1 的 Late diagnostics 行改为 `已接（local）`，
`gateway/panel_inventory.py` 的 `late_diagnostics` 能力改为 `supported:true`。实现前该行保持原文
（`无 producer`，§0.1）；客户端不画假块、不用 `notification.show`/`status.update`/`agent.print`
冒充。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§2.3.2、§2.3.2.1、§4.7、§7；
`docs/appendix-omp-ui-realtime-rpc.md` A.14、A.15；ADR-10（D1 的 `role:"custom"` +
`custom_type`/`details`、D8 的客户端单一投影、D9 的模型转换、D11 的 provider 归一化）；ADR-6
**D1** 的 `client:"omp-ui"` session 级 opt-in；ADR-2 **D9** 的“独立 late-diagnostics”条目；
ADR-5/ADR-6 对 D9 的转述；ADR-8（D17 的 Semgrep 分层与显式索引限制）。
`agent/custom_messages.py`、`agent/session_persistence.py`、`hermes_state_messages.py`、
`agent/lsp/deferred.py`、`agent/lsp/reporter.py`、`agent/lsp/edit_check.py`、`agent/lsp/display.py`、
`agent/lsp/cli.py`、`hermes_cli/config_defaults.py`,
`tools/file_operations_lint.py`、`agent/tool_executor.py`、`agent/turn_stop_gates.py`、
`agent/turn_context.py`、`agent/plugin_events.py`、`tui_gateway/agent_callbacks.py`、
`gateway/run_turn_runner.py`、`gateway/local_viewer_events.py`、
`tui_gateway/contracts/events.py`、`tui_gateway/session_history.py`、
`gateway/panel_inventory.py`；`omp-ui/src/modes/rpc/gateway-session.ts`、
`omp-ui/src/modes/utils/ui-helpers.ts`；`hermes_codegraph/analysis.py`、
`hermes_codegraph/update.py`、`tools/code_graph_update_tool.py`、`tools/code_intel_tool.py`；
`website/docs/user-guide/features/lsp.md`。

**范围**：late custom 行只讨论 omp-ui ↔ Hermes canonical local gateway。消息平台、Desktop、Ink
sidecar、ACP、compute-host 的 worker 帧都不设计；不新增模型工具、不改 system prompt、不改 LSP 的
检查/节流/预算。新增 `lsp.display` 展示策略（D12），它只决定诊断面的来源/表面/severity，不改变
扫描/索引开关；其 `inline`/`severities` 部分作用于所有客户端的 write/patch 工具结果（默认配置下与
现状逐字一致），`late` custom 行仍只对 omp-ui 生成。
Late diagnostics **是**模型上下文消息（`display_only:false`），会触发当前 turn 的修复；它**不**在
turn 已结束后启动一个 idle turn。`agent/lsp` 的延迟投递语义（谁在何时看到 verdict）由本 ADR 之外的
既有实现负责，本 ADR 只改变“交给模型和展示的形状”。

Semgrep **是**与 LSP 并列的即时诊断源：`write_file`/`patch` 的编辑检查同时启动两者，同一
`WRITE_BUDGET` 内返回就内联进工具结果，超出预算就进入同一个 `deferred` 队列；模型调用诊断工具时，
结果作为该 tool 的即时 result 返回。`code_intel.semgrep.enabled` 仍是开关，`mode` 仍决定规则来源；
不再要求先跑 `hermes code index`/`refresh_file`，也不再沿用 ADR-8 D17 的写路径排除。

**取证口径**：Hermes 侧引用本仓库当前工作树（2026-11-04；ADR-10 P1/P5 已落地）。omp 侧源码来自
本地 checkout `C:\Users\zarag\projects\oh-my-pi`（`@oh-my-pi/pi-coding-agent` 18.14.0）；渲染侧同时
核对壳实际安装的 `omp-ui/node_modules/@oh-my-pi/pi-tui`（18.14.1）。`omp-ui/src/vendor/` 是上游的
裁剪副本。本 ADR 不改变 pi-tui 的渲染分支、文案、折叠与色调。

**取代关系（逐条，避免文档互斥）**：

- ADR-2 **D9** 的 **独立 late-diagnostics** 一项（`ADR-2-transcript-blocks.md:231-233`：Hermes
  没有等价数据，不造块）由本 ADR 的 D1–D10 取代。D9 的其它项不变。
- ADR-5 与 ADR-6 对 D9 的转述中，“独立 late-diagnostics 不变”一句只对本项失效；其余项继续
  有效。
- plan §2.3.2.1:445 的 `Late diagnostics | … | 无 producer`：P1/P2 后来源列改为
  `transcript.custom` + `role:"custom"` 行，状态改为 `已接（local）`。该行“诊断已在工具卡内，不另造
  块”只描述**内联** verdict；本 ADR 补的是内联之外、写预算耗尽后才到达的 verdict，并且现在**就是**
  一条真正的 transcript 行。实现前保持原文。
- plan §2.3.2:403 的 `late-diagnostics-message.ts` 渲染器描述**不变**：组件、折叠行数、展开键位、
  错误/警告色调都由 pi-tui 决定；变化只在数据生产者、行与映射层。
- ADR-10 **D1** 继续是唯一结构：本 ADR 的行走 `role:"custom"` + `custom_type`/`details`/
  `display`/`display_only`/`attribution`/`legacy_role`；不新增行类型、不写 `display_kind` 作为
  新 producer 的形状（`display_metadata` 只作为自定义行的持久化信封）。
- ADR-10 **D9** 的模型转换继续适用：本 ADR 把 `lsp-late-diagnostic` 加入
  `MESSAGE_CUSTOM_TYPES`，因此该行按 message-bearing custom 映射成 `developer`；`details`/
  `display`/`attribution` 等在 `PERSISTENCE_ONLY_MESSAGE_FIELDS` 中，永不到达 provider。
- ADR-6 **D1** 的 `client:"omp-ui"` 是本 ADR 的 session 级门；本 ADR 不新增第二种 opt-in 机制。
  如果 ADR-6 D1 尚未落地，本 ADR 的 P1 随之等待（或作为 D1 的第一个消费者一并落地）。
- ADR-8 **D17** 的“Semgrep 只在显式索引/`refresh_file` 跑、写路径不跑”**不采用**；Semgrep 与 LSP
  同为即时诊断源。ADR-8 的索引诊断只作为查询面的既有实现保留。

## 问题

omp 的 **late diagnostics** 是“编辑/写入工具已经返回之后才到达的 LSP 诊断”的展示面。它同时是
**模型消息**和 **transcript 区块**：模型从 `role:"custom"` 的 `content` 读到诊断，客户端从
`details.files` 渲染卡内诊断或独立块。Hermes 已经有“延迟到达的诊断”，但只有模型面，没有对应的
结构、行和客户端消费点。旧 ADR 试图把它做成“编辑/写入卡的附件 + 独立事件”，绕开了 Hermes 当时
没有 `custom` 行的事实；这个前提在 ADR-10 P5 之后已经改变。

### 1. omp 的语义

**数据**：`DeferredDiagnosticsEntry`（`packages/coding-agent/src/tools/index.ts:192-210`）：
`{path, summary, messages, errored, isStale()}`。`path` 是绝对路径；`summary` 是一行严重度摘要
（如 `"2 error(s), 1 warning(s)"`）；`messages` 是已经格式化好的可显示行；`errored` 表示是否含
severity 1；`isStale()` 在注入时求值，文件被更新 mutation 覆盖就丢弃。

**模型消息 + 展示单元**：`buildLateDiagnosticsBatchMessage`
（`packages/coding-agent/src/sdk.ts:438-464`）把一批 entry 合成：

```text
role: "custom"
customType: "lsp-late-diagnostic"      // LSP_LATE_DIAGNOSTIC_MESSAGE_TYPE
content: <lateDiagnosticTemplate 渲染的 system-notice>
display: true
attribution: "agent"
details: {files: [{path, summary, errored, messages}]}
```

消息通过 session 的 `yieldQueue` 注册（`sdk.ts:5291-5294`），空闲时 `injectIdle` 走
`agent.prompt(...)`，流式中走 `injectStreaming`；`isStale` 在真正注入时过滤
（`session/yield-queue.ts:43-230`）。所以“late”在 omp 既是“工具返回后才到”，也是“跨请求注入”：
空闲到达会启动一个新 turn。

**模型角色**：omp 的 `convertMessageToLlm`（`packages/agent/src/compaction/messages.ts:214`）把
`custom`/`hookMessage` 映射成 `developer`；`attribution:"agent"` 保留。也就是说，late diagnostics
不是纯展示帧，它真的进入模型上下文，下一轮请求就能据此修复。

**持久化**：custom message 进入 session entries，resume 时由
`ChatTranscriptBuilder#appendCustomMessage` 重建
（`packages/tui/src/chat/chat-transcript-builder.ts:606-613`），按 `details.files` 重新
`routeLateDiagnostics`。

**渲染**：

- `LateDiagnosticsMessageComponent`
  （`packages/tui/src/chat/late-diagnostics-message.ts:69-175`）聚合 `files` 的
  `messages`/`summary`/`errored`，复用工具诊断的树渲染器；`summary` 拼进标题，collapsed 最多 5 行
  （`COLLAPSED_DIAGNOSTICS`），展开跟随全局 tool-output 开关。
- 实时挂载：`routeLateDiagnostics(chatContainer.children, files)` 在 native 渲染下把每个文件交给
  最近的、路径匹配且实现了 `attachLateDiagnostics` 的 tool frame
  （`late-diagnostics-message.ts:38-67`；`tool-execution.ts:993-1013` 用 `displayPath` 比对，
  只接受 native）。未被任何 frame 接收的文件才生成独立组件
  （`modes/utils/ui-helpers.ts:321-334`）。ANSI 路径全部独立成块。
- 同一文件后续到达的 late 诊断替换该卡上的 `#lateDiagnostics`，不叠加第二块。

### 2. Hermes 的现状：有 custom 行，没有 late-diagnostics 行

**ADR-10 P5 已落地**：Hermes 现在有 `role:"custom"` 会话行、`custom_type`/`details`/`display`/
`display_only`/`attribution`/`legacy_role` 六个字段、持久化信封与 provider 转换。关键事实：

- 生产者只在 `custom_messages:true` 的会话写 `role:"custom"`（ADR-10 D9）；omp-ui 的
  `session.create` 带 `{client:"omp-ui", custom_messages:true}`
  （`omp-ui/src/modes/rpc/gateway-session.ts:1534-1541`）。Desktop/Ink 会话保留 legacy 行。
- `agent/custom_messages.py::project_for_api`（`:193-225`）把 message-bearing custom 映射成
  `developer`（`skill-prompt`/`collab-prompt` 且 user 归因→`user`）；`display_only:true` 丢弃；
  `custom_type` 必须在 `MESSAGE_CUSTOM_TYPES`（`:37`）里才会被当作 message-bearing。
- 持久化：`agent/session_persistence.py:254-260` 把六个字段带进 writer；
  `hermes_state_messages.py::_message_row_params` 调
  `agent.custom_messages.storage_metadata` 放进 `display_metadata["_custom_message"]`；读取时
  `lift_stored_fields` 抬回顶层。`messages.role` 白名单已经包含 `custom`
  （`hermes_state_runtime.py:834`）。
- wire：`tui_gateway/session_history.py::_history_to_messages`（`:481`）对 custom 行调用
  `_custom_message`（`:306`），输出 `role:"custom"` + `custom_type`/`details`/`display`/
  `display_only`/`attribution`/`legacy_role`/`row_id`；`display_kind`/`display_metadata` 不出网关。
- 客户端：`rowToAgentMessage`（`omp-ui/src/modes/rpc/gateway-session.ts:552-565`）对 custom 行走
  `customRowToMessage`；`#emitCustomMessage`（`:1264-1272`）按 `row_id` 去重，live 与快照不会画
  两块；`addMessageToChat` 的 custom 分支按 `customType` 选组件
  （`omp-ui/src/modes/utils/ui-helpers.ts:204-241`）。

**LSP 延迟 verdict 的现状**：

- 内联 vs 延迟：`write_file`/`patch` 返回前只等 LSP 一个很短的预算（`WRITE_BUDGET = 0.08s`，
  `agent/lsp/edit_check.py:45`；`inline_wait` 取剩余预算）。预算内拿到 verdict，就写进
  `WriteResult.lsp_diagnostics` / `PatchResult.lsp_diagnostics`
  （`tools/file_operations.py:1598-1605`），随 `tool.complete.result` 进入工具卡；预算耗尽则
  `tools/file_operations_lint.py:365-390` 起 done-callback，verdict 进入 `agent/lsp/deferred.py`。
  **只有后者是 late**。
- 延迟队列：`deferred.py` 以 `owner`（session id，其次 task id）和绝对路径为键，每个
  `(owner, path)` 只保留一条 `_Note{text, baseline_text}`（`:41-46`）。新编辑 `take` 走旧 note，
  新检查以旧 baseline 做差；旧检查被 supersede 时不 post；没有 verdict 时 `restore`/`carry`
  （`:74-93`、`edit_check.py:134-178`）。“同一文件只报该 session 自己的错误”已有回归测试
  （`tests/agent/lsp/test_write_never_waits_on_lsp.py`）。
- 投递点（都是模型面，没有 UI 行/事件）：
  1. 下一个工具结果：`agent/tool_executor.py:1163` 在 append 前调用 `forward_to_agent`，note 被
     `queue_plugin_notice` 放进 live-notes，随后 `attach_to_tool_message` 追加到该 tool result
     尾部（`agent/plugin_live_notes.py:30-55`）。
  2. 下一个用户 turn：`agent/turn_context.py:866-889` 在 turn 开始时把尚未投递的 verdict 并进
     plugin notice，随用户消息注入。
  3. turn 结束：`agent/turn_stop_gates.py:158-176` 调 `settle_for_agent`，最多等
     `TURN_END_WAIT = 5.0s`，把仍未投递的 verdict 作为一条以 `[System note: ...]` 开头、
     `role:"user"` 的 note 返回，由 `apply_stop_gates` append（`:282`）；超过 5s 的 verdict 留给
     下一个用户 turn。
- **只有文本**：`_queue_late_verdict` 用 `report_for_file` 生成
  `<diagnostics file="...">ERROR [l:c] …</diagnostics>`，`deferred.post` 只存字符串和 baseline
  （`tools/file_operations_lint.py:395-417`）。`reporter.py` 的 `_sanitize_field` 会 HTML 转义
  `< > &` 并截断，因为这是要嵌进模型 `<diagnostics>` 块的内容。展示层如果直接复用这段文本，会显示
  `&lt;` 等实体，也会把“模型文本”当成“展示文本”。
- **没有 UI 面**：`tui_gateway/contracts/events.py` 没有 late-diagnostics 事件；
  `gateway/panel_inventory.py:298-303` 仍把 `late_diagnostics` 标为 `UNSUPPORTED`；omp-ui 的
  `#notificationHandlers`（`gateway-session.ts:979-1025`）没有对应条目，未知事件被表查找静默忽略。

**Semgrep 危险模式诊断的现状**：

- 扫描实现已有：`code_intel.semgrep.enabled` 默认 `false`，`mode=auto` 从 registry 拉规则，
  `mode=custom_dir` 只读 `HERMES_HOME/code-intelligence/semgrep/`；
  `hermes_codegraph/analysis.py::semgrep_scan`（`:115-205`）已经在内存里返回
  `{path, rule_id, severity, message, start_line, start_column, end_line, end_column}` 的
  `findings`，不执行仓库代码、不写文件、`--metrics=off`。
- 当前只在 `hermes code index` / `refresh_file(semgrep=true)` 里调用；`indexer._run_semgrep`
  把这些 finding 写成 `CodeDiagnostic` 与 `CodeAnalysisRun`。`update.refresh_file` 目前**丢弃**
  `_run_semgrep` 的返回值（`update.py:95-99`），所以工具调用拿不到 findings；这是实现缺口，不是
  本 ADR 的边界。
- `code_intel` / `hermes code diagnostics` 能查询已写入的 `CodeDiagnostic`；当前 schema 没有
  Revision 字段，查询结果不保证对应当前字节。因此**展示层不能靠按 path 查询旧图来冒充即时诊断**；
  即时诊断必须来自刚刚对当前字节执行的扫描。
- 没有 UI 行/事件：Semgrep 结果只进图或查询，不会出现在编辑卡/工具卡。

本 ADR 不沿用 ADR-8 D17 “只有显式索引才跑、写路径不跑”的限制：既然模型调用工具和写入工具都能
即时得到 LSP 诊断，Semgrep 也应作为**同一诊断链路的即时源**，而不是等用户手动索引。代价是每次
编辑可能多一次扫描；由 `code_intel.semgrep.enabled`、`mode`、超时和延迟队列控制。

### 3. 旧 ADR 的“附件/事件”方案为什么不够

旧 ADR 把 late verdict 做成“编辑/写入卡的附件 + 独立展示块”，模型面仍走 tool-result 尾部文本或
turn-end 的 user note。它在 ADR-10 P5 之后不再是正确形状：

- 它需要 `path -> tool_call_id` 映射、`display_metadata.late_diagnostics`、
  `late_diagnostics_carrier.suffix` 精确剥离、客户端 path→files 快照、`removed_paths` 和
  `clearLateDiagnostics` seam；这些复杂度全部来自“没有自己的行”。
- 模型看到的是 tool-result 文本或一条 user row，展示层看到的是另一份 metadata；两者必须靠
  suffix/路径约定对齐，任何漂移都会让卡上多显示一份 note 或让块消失。
- 它不能自然满足“触发修复”：turn-end 的 note 是 user row，不是 omp 的 custom→developer；模型
  能读，但形状与 omp 不一致，且展示层只能重标这条已有行。
- resume/rebuild 必须靠 metadata 回填或壳快照；而 custom 行本来就是 durable history，快照、
  实时、rebuild 三者可以共用一套投影。

| 做法 | 结果 |
|---|---|
| 仍把 late verdict 追加到 tool result 的 `content` 尾部 | 模型能看到，但没有结构化行；UI 只能解析文本；resume/rebuild 无法重建；不是 omp 的 custom 形状 |
| 只发 `diagnostics.late` 展示事件 + tool 行 metadata | 模型面没有新行；需要 path→call id、suffix 剥离、shell 快照、`removed_paths`；resume 不天然 |
| 新造 `role:"diagnostics"` / 独立行类型 | provider 与 ADR-10 的 custom 投影都不认识；必须再造一套转换与客户端分支 |
| 把 late 行做成 `display_kind="hidden"` 的展示行 | 行会被展示投影丢弃；不加 hidden 又会让模型读不到或重复读 |
| 直接在客户端解析 tool result 里的 `<diagnostics>` 文本 | HTML 转义、header、truncate 都使解析不可靠；会丢 severity/source/code |
| 只在客户端本地再跑一次 LSP | 客户端没有写入权威，也没有服务端 snapshot；会得到与模型不同步的诊断 |
| 把内联 `lsp_diagnostics` 也做成 late 行 | 内联 verdict 已经在编辑卡里；重复行违反“一个事实一次” |
| 用 `notification.show`/`status.update`/`agent.print` 转达 | 只会得到 toast/状态行；没有 `LateDiagnosticsMessageComponent` 的折叠、路径匹配与卡内挂载 |
| 让 late diagnostics 启动一个 idle turn（omp 的 `injectIdle`） | 改变 Hermes 的 turn 语义、计费与用户预期；本 ADR 只在 turn-end 继续当前 turn |
| 把 Semgrep 只当展示、不放进模型上下文 | 模型看不到危险模式，无法在同一 turn 修正；与“Semgrep 和 LSP 一样是即时诊断”矛盾 |
| 直接 `get_diagnostics(path=...)` 并当成即时结果 | 旧 Revision 的诊断会混进当前文件；当前 schema 也没有 Revision 字段。即时结果必须来自对刚写入字节的扫描 |

## 决策

### transcript 行在 Hermes 中支持 omp 的 `custom` 角色

#### D1. 唯一结构：`role:"custom"` + `custom_type:"lsp-late-diagnostic"`

每批 late verdict 生成一条**真正的 transcript 行**，形状与 omp 的
`CustomMessage<LateDiagnosticsDetails>` 对齐：

```text
role: "custom"
custom_type: "lsp-late-diagnostic"
content: "<system-notice> Diagnostics for the files you edited: ... </system-notice>"
display: true
display_only: false
attribution: "agent"
legacy_role: "user"
details: {files: [{path, summary, errored, messages}, ...]}
```

- `custom_type` 用 omp 的常量值 `"lsp-late-diagnostic"`
  （`packages/tui/src/chat/messages.ts:23` 的 `LSP_LATE_DIAGNOSTIC_MESSAGE_TYPE`），不是
  `"late-diagnostics"` 或 `"diagnostics.late"`。
- `display_only:false`：这是 message-bearing 行，必须进入模型上下文；`details` 只用于展示。
- `content` 是模型读到的唯一字节，由 `details.files` 渲染；`details` 本身在
  `PERSISTENCE_ONLY_MESSAGE_FIELDS` 中，provider 永远不会看到。
- `attribution:"agent"`，`legacy_role:"user"`：与 omp 一致；客户端只读 `custom_type`/`details`，
  不解析 `legacy_role`。
- 生产者只在 `active_policy().custom_messages` 为 true 的会话写这个形状
  （`agent/adviser_delivery.py:133-155`、`agent/todo_reminder.py:169-184` 是同一惯例）；其它会话
  保持现有 tool-result 文本 / user note，默认 `lsp.display` 下逐字不变（显式配置时按 D12 过滤）。

模型转换：把 `"lsp-late-diagnostic"` 加入
`agent/custom_messages.py::MESSAGE_CUSTOM_TYPES`（`:37`）。这样
`project_for_api`（`:193-225`）会把它按 message-bearing custom 映射成 `developer`，与 omp 的
`convertMessageToLlm` 一致。**不加这个集合项，行会被 `project_for_api` 丢弃，模型永远看不到诊断，
“触发修复”就不成立**。`display_only` 不能为 true，否则同样被丢弃。

`content` 模板（source-neutral，LSP + Semgrep 共用）：

```text
<system-notice>
Diagnostics for the files you edited:
{for each file}
{path} — {summary}
{for each message}
{message}
{/for}
{/for}

Fix them if they are real, then continue.
</system-notice>
```

`content` 与 `details.files[].messages` 使用同一套行格式；`content` 受模型上限
（`MAX_PER_FILE`/`MAX_TOTAL_CHARS`）约束，可以在末尾追加 `... and N more`（模型可见）；
`details.files[].messages` 用更大的**显示上限**（例如 `MAX_DISPLAY_PER_FILE = 200`、
`MAX_DISPLAY_TOTAL_CHARS = 32000`），并**不追加**不可解析的省略行（D2）。`summary` 始终按完整
过滤集合计数，所以“完整展示”看到的是计数 + 可展开的显示行，模型看到的是同一批诊断的受控子集。

#### D2. 结构化 verdict 在 `deferred` 源头生成；模型文本与展示文本分开

`agent/lsp/deferred.py` 的 `_Note` 增加 `files: tuple[LateDiagnosticFile, ...]`：

```text
LateDiagnosticFile = {
    "path": str,        # 绝对路径，与 deferred.post 收到的 path 同一口径
    "summary": str,     # "2 error(s), 1 warning(s)"
    "errored": bool,    # 是否含 error
    "messages": list[str],
}
```

`post(owner, path, text, baseline_text, files)`、`take`、`restore`、`pop_notes` 原样携带。
`text` 保持当前字节，供非 custom 会话的 legacy 投递；custom 会话只读 `files`。

`agent/lsp/reporter.py` 增加纯展示入口（例如 `display_files_for(path, diagnostics, ...)`），与
`report_for_file` 共用 severity 过滤和纯文本 sanitizer 的输入口径，但**使用独立的显示上限**
（`MAX_DISPLAY_PER_FILE`/`MAX_DISPLAY_TOTAL_CHARS`），不复用模型上限，也**不复用 HTML 转义**。
`elide` 只用于模型 `content` 的整段截断；`details.messages` 不做会破坏行解析的省略行。

- 行格式固定为
  `path:line:col [severity] ([source] )?message( (code))?`，例如
  `C:\repo\src\app.ts:12:5 [error] [typescript] Type 'string' is not assignable … (2322)`。
  `severity` 小写；`source` 在 message 前；`code` 在最后；`line`/`col` 都是 1-based（LSP 的
  0-based `range.start` 在这里 +1，Semgrep 的 `start_line`/`start_column` 已经是 1-based）。
- `path`、`message`、`source`、`code` 用纯文本 sanitizer：去控制字符、CR/LF 折叠为空格、单项长度上限
  （message 300 / code 80 / source 80）；**不做 HTML 转义**。模型侧的 `<diagnostics>` 块继续走
  `_sanitize_field` + `html.escape`，两者不互相复用输出。
- 每一行必须匹配 pi-tui 的
  `parseDiagnosticMessage`（`render-utils.ts:510-524`）；正常 payload 不允许出现 `unparsed` 行。
  出现即展示缺陷，不是可接受的降级。
- `summary` 对过滤后的完整集合计数，复刻 omp 的复数形式（`"2 error(s)"`、`"1 warning(s)"`；
  多个用 `", "` 连接），不因模型截断而少计。模型 `content` 用
  `MAX_PER_FILE`/`MAX_TOTAL_CHARS`；`details.files[].messages` 用
  `MAX_DISPLAY_PER_FILE`/`MAX_DISPLAY_TOTAL_CHARS`，只放可解析行，不追加省略行。总字符上限按
  **整行**截断：达到上限时丢弃最后一条会超限的行，不切半行（切半行会变成 `unparsed`）；
  `summary` 仍计完整集合。
- `errored` 为是否存在 `error`（LSP severity 1；Semgrep `error`）。
- **行内 path 与 `files[].path` 逐字一致**：同一绝对路径、同一分隔符/大小写口径。
  `formatDiagnostics` 按行内 path 分组件树，`routeLateDiagnostics` 按 `files[].path` 挂卡；两者不
  一致会出现“挂到 A 卡、树里显示 B 路径”或同一文件被拆成两组。Windows 盘符冒号是合法用例，验收
  覆盖。
- `files` 顺序沿用 `deferred` 的 pop 顺序；组内 severity→line→col→message 排序由 pi-tui
  `formatDiagnostics` 决定，壳不二次排序。
- 过滤后没有诊断（`report_for_file` 返回空）就不 `post`、不生成行；不为 clean 造空块。

Semgrep 的 `files` 由 `semgrep_files_for(path, findings)` 生成（D8），与 LSP 的 `files` 按 path
合并；同一 path 的 `summary`/`errored` 按合并后的 severity 重算。

#### D3. 三个既有投递点都追加同一行，不再改写 tool result / user note

对 `custom_messages:true` 的会话，late verdict 在以下三个点各追加**至多一条** custom 行（该点
弹出的所有 path 合并到一条行的 `files`）：

1. **下一个工具结果之后**：`agent/tool_executor.py` 仍先构造并 append `tool_message`，但不把
   LSP note 追加到它的 `content`；在 tool 行 flush 之后，用 `agent/lsp/delivery.py` 的 helper
   append custom 行、flush、`notify_plugin_message`。模型在下一次 API 调用看到
   `tool` + `developer`，可以立即修。
2. **turn 结束**：`agent/turn_stop_gates.py::_late_lsp_verdicts` 返回结构化的 `files`（或整行），
   不再返回以 `[System note:` 开头的 user note；`apply_stop_gates` 在 interim assistant 行之后
   append custom 行、flush、notify，然后 `continue_turn=True`。`_MAX_LSP_TURN_END_NOTES = 3`
   的上限保留。
3. **下一个用户 turn**：`agent/turn_context.py::_merge_gateway_notes` 不再把 LSP note 折进用户
   消息的 `content`；它在 `current_turn_user_idx` 之后 append custom 行，由 `_persist_turn_start`
   的 flush 落库，flush 后（`_row_id` 已存在）再 notify。`plugin_live_notes` 继续只承载插件
   go-live notice，不再承载 `lsp:*` note。

非 custom 会话（Desktop/Ink/消息平台）继续走现有三条 legacy 路径，默认 `lsp.display` 下模型文本与
展示逐字不变；显式配置 `lsp.display` 时 inline/late 按 D12 过滤。
`custom_messages` 标志在投递时由 `gateway.session_policy.active_policy()` 读取（agent 线程可用；
LSP done-callback 线程不读，只生成 `files`）。

**顺序与去重**：

- 同一投递点弹出的多个 path 合成一条行；`files` 按 path 唯一。
- 同一 path 的新 verdict 在 `deferred.post` 合并，不在客户端叠加第二块。
- 行 append 后由所在投递点的既有 flush 落库；`_DB_PERSISTED_MARKER` 去重，重复 flush 不会写两次。
- `row_id` 是 live 事件与快照合并的唯一键；不用 `path + messages` 做内容去重（同一个错误修复后
  再次出现是合法的，内容去重会吞掉第二个真实块）。

#### D4. wire 事件 `transcript.custom` 携带行本身，不另造 `diagnostics.late`

新增一个通用事件，让 live 与 resume 共用 `rowToAgentMessage`：

```text
event transcript.custom {
  message: TranscriptMessage   # role:"custom" + custom_type/details/display/... + row_id
}
```

- 载荷就是 `tui_gateway/session_history._history_to_messages` 对同一行的投影（抽成
  `custom_row_message(row, text=...)` 单一 helper），不手写第二份形状。
- 发射点：行已 durable（`_row_id` 已设置）之后，通过 `agent/plugin_events.py::notify_plugin_message`
  → `tui_gateway/agent_callbacks.py::plugin_row_event` 返回
  `("transcript.custom", {"message": <row>})`；canonical 的 `_canonical_plugin_event`
  （`gateway/run_turn_runner.py:529-537`）用 `_publish_execution` 发；sidecar 的
  `_announce_plugin_row` 改为 `_emit(name, sid, payload)` 或显式 `transcript.custom` 分支，
  **不得**落进现有的 `else: _emit("message.adviser", …)`。
- 只对 local viewer 且 session policy 的 `client == "omp-ui"` 发布（ADR-6 D1）；CLI、TUI、Desktop、
  消息平台、ACP、one-shot、compute-host 都不发。`_canonical_plugin_event`/`_announce_plugin_row`
  在 `transcript.custom` 分支再检查一次 `active_policy().client`，非 omp-ui 直接 return；不要依赖
  客户端忽略未知事件来“静默”。客户端未知事件行为保持“忽略”。
- **不送** baseline、原始 diagnostic dict、`session_id`/`tool_call_id`；模型文本只在行的
  `text` 里，展示结构只在 `details` 里，`row_id`/`timestamp` 由行携带。
- 契约加在 `tui_gateway/contracts/events.py`，`Payload` 默认 `extra="forbid"`；同一 PR 必须有真实
  emitter，否则 `registry.assert_complete` 的 orphan 门失败。重跑
  `scripts/gen_gateway_contracts.py`，让 omp-ui 的 `gateway-contract.generated.ts` 同步。
- 不新增 `diagnostics.late`、`removed_paths`、`clearLateDiagnostics`：行是 durable history，没有
  需要客户端清理的 path 缓存；同一 path 的新 verdict 是一条新行/新卡，旧行保留为历史。
- 这也是 A.15 里“带 `custom_type`/`attribution` 的 `custom.message`”缺口的通用形状；本 ADR 只
  消费 `custom_type:"lsp-late-diagnostic"`，其它 custom 类型仍走各自的专用事件或后续变更。
- 事件名用 `transcript.custom`（与 `transcript.notice` 同族），不沿用 A.15 的占位名
  `custom.message`；载荷是完整 transcript 行，不依赖名字。若后续统一命名，可在不改载荷的情况下
  改名。

#### D5. omp-ui 渲染：一个 `case`，复用现成的组件

`omp-ui/src/modes/utils/ui-helpers.ts::#customBlock`（`:204-241`）增加：

```ts
case "lsp-late-diagnostic": {
    const details = custom.details as {files?: LateDiagnosticsFile[]} | undefined;
    if (!details?.files) return generic();
    const files = routeLateDiagnostics(this.ctx.chatContainer.children, details.files);
    if (files.length === 0) return [];
    const component = new LateDiagnosticsMessageComponent(files);
    component.setExpanded(this.ctx.toolOutputExpanded);
    component.setToolActivityVisible(!this.ctx.hideToolActivity);
    return [this.#child(component)];
}
```

- 从 `@oh-my-pi/pi-tui/chat/late-diagnostics-message` 导入
  `LateDiagnosticsMessageComponent`/`routeLateDiagnostics`/`LateDiagnosticsFile`；壳实际安装的
  18.14.1 已有该模块（`omp-ui/node_modules/@oh-my-pi/pi-tui/src/chat/late-diagnostics-message.ts`）。
- `routeLateDiagnostics` 在 native 下把每个文件交给最近的、路径匹配的编辑/写入卡
  （`ToolExecutionComponent.attachLateDiagnostics`）；未被接收的文件才生成独立块。ANSI 全部独立。
- **V4A patch 例外（独立块）**：`patch(mode="patch")` 的目标路径在 V4A 文本里，args 不承载这个
  多文件列表；`matchesPath` 只读 `args.path`/`file_path`/`edits[].path`/`files[].path`。omp 的
  `apply_patch`/`hashline` schema 也只有 `{input}`，同样匹配不到，所以 omp 对这类编辑也落独立块。
  本 ADR 保持同一行为：V4A 永不 attach，不在壳里解析 V4A 或伪造匹配。
- 缺 `details` 时回退 `CustomMessageComponent`（text-only），不画半截诊断树。
- 不解析 `tool.complete.result.lsp_diagnostics`，不在客户端调用 LSP，不把事件映射成
  `notification.show`/状态行。

`gateway-session.ts` 增加 handler：

```ts
"transcript.custom": payload => this.#onTranscriptCustom(payload)

#onTranscriptCustom(payload) {
    const row = isRecord(payload.message) ? payload.message : undefined;
    if (!row) return;
    const message = GatewayOmpSession.rowToAgentMessage(row as TranscriptMessage);
    if (message?.role === "custom") this.#emitCustomMessage(message, asNumber(row.row_id));
}
```

`#emitCustomMessage` 已按 `row_id` 去重（`:1264-1272`），快照已带的行不会被 live 事件再 append
一次。live 与 resume 走同一个 `#customBlock`，没有第二套渲染分支。

#### D6. 持久化与 resume：行就是快照

- 写入：`agent/session_persistence.py:254-260` 已把六个 custom 字段带进 writer；
  `hermes_state_messages._message_row_params` 用 `storage_metadata` 放进
  `display_metadata["_custom_message"]`；`append_message` 的 role 白名单已接受 `custom`。
- 读取：`lift_stored_fields` 抬回顶层；`_history_to_messages` 调 `_custom_message` 输出
  `role:"custom"`；`display_kind`/`display_metadata` 不出网关。
- resume：`session.resume` 的 `display_messages` 带这条行；`rowToAgentMessage` → `#customBlock`
  → `routeLateDiagnostics`/`LateDiagnosticsMessageComponent`。不需要 path→tool_call_id 映射、
  `late_diagnostics_carrier.suffix`、`set_latest_matching_message_display_kind` 或壳快照。
- 会话内 rebuild：custom 行在 `#messages`/`#rowMessages` 里，`rebuildChatFromMessages` 重放时
  仍走 `#customBlock`；卡被压缩/滚出时自动退化为独立块。
- 压缩/分支：`tui_gateway/methods_session_branch.py::_BRANCH_COPY_FIELDS` 与
  `tui_gateway/session_workdir.py::_WORKDIR_SEED_FIELDS` 必须把六个 custom 字段
  （`custom_type`/`display`/`display_only`/`details`/`attribution`/`legacy_role`）加入复制列表，
  或用 `storage_metadata` 重新生成 `_custom_message` 信封。只复制 `display_metadata` 不够：
  `_branch_source_history` 走 display 投影，顶层 custom 字段会被丢掉，分支恢复后会退化成
  text-only。P1 加分支测试。`role:"custom"` 不计入 user turn 的
  `truncate_before_user_ordinal` 地址空间。`rewind` 按 durable row 截断，行随之删除，不会复活。
- **同一行只出现一次**：快照与 live 都按 `row_id` 合并；不按字符内容比较。
- **`display_metadata` 是持久化信封，不是 wire 形状**：客户端永远只读 `custom_type`/`details`，
  不读 `_custom_message`。

#### D7. 归属、去重、staleness 都以 `deferred` 为准

- `deferred` 已经保证：一个 `(owner, path)` 一条 note；新编辑吸收旧 note；被 supersede 的检查不
  post；两个 session 编辑同一文件时各自只收到自己的 verdict
  （`test_write_never_waits_on_lsp.py`）。
- 事件只发给 note 的 `owner` 对应的 session；owner 是 task id（子代理）时不升级到父会话。子代理的
  late diagnostics 不在 P1 的展示范围内；如需支持，另立变更。
- 同一路径的多张编辑卡：`routeLateDiagnostics` 用 `findLast` 命中最近一张；与“新编辑吸收旧 verdict”
  同向。
- 一个 late 行内的 `files` 按 path 唯一；如果上游将来把多个 session 的 note 合并，必须在
  `deferred` 层拆开，客户端不猜 owner。
- Semgrep 是独立来源，另按扫描内容身份 + 成功 write/patch 失效（D8），不与 LSP 的
  digest/baseline 混为一个。

#### D8. Semgrep 危险模式诊断：与 LSP 并列的即时源（P2）

- **同一触发/预算**：`write_file`/`patch` 的编辑检查在启动 LSP 的同时启动 Semgrep（要求
  `code_intel.semgrep.enabled=true` 且 `mode` 有效）。两者共用 `WRITE_BUDGET`/`INLINE_GRACE`：
  预算内返回就并入该次写入的工具结果（`WriteResult`/`PatchResult` 的 `lsp_diagnostics` 与新增
  `semgrep_diagnostics`；或统一 `diagnostics` 但保留 source 区分）；超出预算就进入同一个
  `deferred` 队列，按同一投递点交给模型和 UI。Semgrep-only 也可以内联/延迟，不要求 LSP 有 error。
- **映射**：每条 finding 变成一行
  `path:line:col [severity] [semgrep] message (rule_id)`。`path` 必须按扫描 root 解析成与
  `files[].path` 相同的绝对路径（`semgrep_scan` 返回的是相对路径、`/` 分隔），不能直接把
  finding 的相对路径写进行内，否则 `formatDiagnostics` 的 filePath 与 `routeLateDiagnostics`
  的挂卡路径不一致。severity 统一小写；source 固定 `semgrep`；code 用 `rule_id`（截断到 80
  字符）。LSP 行与 Semgrep 行按 path 合并到同一个 `files` 项；`summary`/`errored` 按合并后的
  severity 重算。
- **模型文本**：late 行的 `content` 也包含这些行，因为 Semgrep 已和 LSP 一样是即时诊断，不再只靠
  `code_intel` 查询。内联工具结果同样包含 `[semgrep]` 行。
- **工具结果字段**：`WriteResult`/`PatchResult` 增加 `semgrep_diagnostics`（结构化 findings +
  展示行）；`tool.complete.result` 原样携带，不把 Semgrep 混进 `lsp_diagnostics`（后者语义保持
  LSP-only）。late note/行的 header 从 “LSP diagnostics introduced by …” 改成 source-neutral 的
  “Diagnostics introduced by …”，turn-end/自定义行统一用 “Diagnostics for the files you edited …”。
- **severity 口径**：默认 LSP `[error]`、Semgrep `[error, warning]`；
  `lsp.display.*.severities` 可在 `error`/`warning` 间覆盖（D12）。`info`/`hint` 不显示；
  `errored` 只在含 `error` 时为 true，纯 warning 用组件 warning 色调。合并时 `summary` 分别计数、
  `errored` 取并集。
- **失效**：每次成功的 write/patch 都重新扫描当前字节；新行按内容身份替换旧 Semgrep 行，不保留
  旧 finding。Semgrep 关闭、未安装、扫描失败/超时且没有新结果时，新行没有 `[semgrep]` 行；旧行
  作为历史留在 transcript，**不把旧 finding 合并回来**。不为“清空”发事件，也不修改旧行。
- **模型调用工具**：模型主动调用诊断/索引工具时，结果作为该 tool 的 **result** 即时返回并显示在
  工具卡，不进入 late 队列。`code_graph_update(refresh_file, semgrep=true)` 现场扫描当前文件；
  `code_intel(diagnostics, path=...)` 在 path 明确且 Semgrep enabled 时也现场扫描并即时返回，
  无 path/未启用时返回已存结果并标注 freshness（不能把旧图冒充当前文件的即时诊断）。
  `refresh_file` 必须把 `semgrep_scan` 的 findings（rule、severity、span、message）放进 result；
  当前 `update.py:95-99` 丢弃返回值是实现缺口，必须修。
- **降级/门控**：`code_intel.semgrep.enabled=false`、semgrep 未安装、`custom_dir` 为空、扫描
  `skipped`/`failed`：不显示 Semgrep 行，也不把失败当诊断；LSP 不受影响。`mode=auto` 可能联网，
  用户开启即接受；仍 `--metrics=off`、不执行仓库代码、不写文件。即时工具结果对所有客户端可见
  （它就是普通 tool result）；`transcript.custom` 事件仍只对 local viewer 且 `client:"omp-ui"`
  发送。
- **不依赖 ADR-8 的显式索引/Revision 查询**：即时路径直接用 `semgrep_scan` 对刚写入字节的
  in-memory findings，不需要 `CodeDiagnostic` 的 Revision 字段，也不需要先跑索引。`code_intel`
  的旧图查询仍可能存在，但它不是本 ADR 的即时诊断来源。

#### D9. 触发修复：行是 `developer` 消息，turn-end 继续当前 turn

“完整展示”和“触发修复”是同一行的两件事：

- **模型面**：`custom_type:"lsp-late-diagnostic"` 在 `MESSAGE_CUSTOM_TYPES` 中，
  `project_for_api` 把它转成 `developer`。模型读到的是：
  - mid-turn：下一个 API 调用中 tool 结果之后的 developer 行；
  - turn-end：interim assistant 行之后的 developer 行，`apply_stop_gates` 返回
    `continue_turn=True`，模型在同一 turn 内修复；
  - 下一个用户 turn：用户消息之后的 developer 行，模型在回答前看到诊断。
- **展示面**：同一条行的 `details.files` 由 `LateDiagnosticsMessageComponent` 渲染；native 挂到
  最近的编辑/写入卡，否则独立块；`details` 不参与 provider 请求。
- **不启动 idle turn**：如果 turn 已经结束且没有下一个用户 turn，行持久化在 transcript 中，但
  不会像 omp 的 `injectIdle` 那样自动 `agent.prompt`。展示层不调用模型；要自动修复需要另立
  turn/计费决策。
- **不新增模型工具、不改 system prompt**：模型通过已有的工具重新编辑来修复，不需要新的诊断工具。
- **prompt cache**：行追加在 live turn 的尾部，不重写已发送前缀；一旦追加，字节稳定。resume 重放
  同一行/同一 `content`，前缀不漂移。`details`/`display`/`attribution` 是 persistence-only，
  不进 provider。
- **修复后的状态**：新编辑产生新的 tool result；如果新 verdict 为空，不追加新行。旧行保留为历史，
  与 omp 的 custom message 语义一致；模型按时间顺序看到旧问题与后来的修复。

#### D10. 降级、失败与安全

- LSP 未启用、workspace 未信任、服务端不回答、预算内无 verdict、verdict 为 clean：都不生成行、
  不画占位。模型侧仍按现有策略在下一个工具结果/turn end/下一个用户 turn 收到或不收到。
- Semgrep 关闭/未安装/`custom_dir` 空/扫描失败或超时：不生成 `[semgrep]` 行，不影响 LSP。
- 客户端缺失/旧版：未知 `custom_type` 回退 `CustomMessageComponent`；未知事件被忽略；模型行为
  不变。
- `custom_messages:false` 的会话（Desktop/Ink 创建）不写 custom 行；`client != "omp-ui"` 的
  local viewer 不发 `transcript.custom`。默认 `lsp.display` 下 TUI/CLI/消息平台的输出逐字不变；
  用户显式设置 `lsp.display` 后，inline `lsp_diagnostics`/`semgrep_diagnostics` 按策略过滤（所有
  客户端），late 行仍不写、不发。omp-ui 会话被其它客户端 resume 时 `custom_messages` 仍是 session
  state；行照常写，事件按当前 viewer 的 client 门收窄。
- 诊断文本来自刚解析用户代码的 language server / Semgrep，按不可信处理：纯文本 sanitizer 去掉
  控制字符、折叠换行、限制 message/code/source/path 长度；行数/总字符有上限；不送文件内容。
  客户端用 `plainText` 渲染，不解释 HTML/Markdown。
- 持久化失败只记 debug，不影响 turn、工具结果或模型投递。`_row_id` 由 flush 设置，
  `plugin_row_event` 在没有 `_row_id` 时返回 `None`，所以**不伪造实时事件**；行留在 `messages`
  里由下一次 flush 重试，成功后再发事件。不要为了“先显示”合成 row_id：resume 会多出一块。
- 路径匹配失败（符号链接、大小写、跨 cwd）只导致“不挂到卡”，退化为独立块；不能因此丢事件或改
  模型文本。实现时统一用 live session cwd 解析，并补 Windows/大小写测试。

#### D11. 兼容性与 footprint

- 不新增模型工具、不改 `toolsets`、不改 system prompt；新增 `lsp.display` 展示策略（D12）。
  `lsp.enabled`、`lsp.wait_timeout`、`code_intel.semgrep.enabled`、`mode` 继续是扫描/索引开关。
- LSP 的 write 延迟不变：行在 LSP loop 的 done-callback 之后才产生，写路径仍是
  `WRITE_BUDGET`/`INLINE_GRACE`。P2 的 Semgrep 在同一预算内并行启动，超时即转 deferred；不允许
  同步等待完整 Semgrep 扫描。
- prompt cache：LSP 路径的已发送前缀不因本 ADR 改变；新增行只追加在尾部。P2 的 Semgrep 在
  `code_intel.semgrep.enabled=true` 时会有意把危险模式行加入工具结果/开发者行，这是新诊断内容，
  不是隐藏的 cache 改写；`details`/事件仍是展示面。
- omp 的“late 到达会启动新 turn”在 Hermes **不实现**：展示层不调用模型，也不把 verdict 变成
  admission。turn-end 的 `continue_turn` 是既有 stop-gate 语义，不是新的 turn 触发源。
- ADR-8 D17 的“仅显式索引、写路径不跑”不采用；Semgrep 与 LSP 同为即时诊断源。

#### D12. 展示策略配置：`lsp.display.lsp` / `lsp.display.semgrep`

在 `config.yaml` 的 `lsp` 节增加纯展示策略（不改扫描/索引开关）：

```yaml
lsp:
  display:
    lsp:
      mode: both                 # off | inline | late | both
      severities: [error]        # error | warning
    semgrep:
      mode: both
      severities: [error, warning]
```

- `mode` 决定该来源在诊断面上的出现位置：
  - `inline`：预算内 verdict 进工具结果/编辑卡；超时丢弃，不生成 late 行。
  - `late`：预算内 verdict 也转 `deferred`，只生成 late 行，不内联。
  - `both`（默认）：预算内内联；超时进 late 行（当前行为）。
  - `off`：该来源不参与诊断面；不内联、不生成 late 行。Semgrep 额外跳过 write/patch 的即时扫描；
    LSP 额外跳过该次 edit check，但不改变 `lsp.enabled` 对其它消费者的服务。
- **`mode=late` 的文案**：预算内 verdict 被策略性转 `deferred` 时，legacy note/自定义行的 header
  不能写“the language server answered after that tool result returned”——事实不成立。用
  timing-neutral 的 “Diagnostics introduced by your edit of …”；只有真正超时才保留“answered
  after …”的说明。
- `severities` 只接受 `error`/`warning`。LSP 默认 `[error]`；Semgrep 默认 `[error, warning]`。
  过滤发生在 `deferred.post` 和 `details`/`content` 生成之前；`summary`/`errored`/色调按过滤后的
  集合计算。`info`/`hint` 不在本 ADR 的展示面（组件没有独立色调），要开另立 ADR。
- **模型/展示同源**：过滤后的集合同时用于 `content`（模型）和 `details`（展示）；不做“UI 隐藏、
  模型仍读”的双轨。`severities` 是诊断面的策略，不是 UI 过滤器。
- **对 legacy 会话同样生效**：`lsp.display` 在 custom/legacy 分支之前读取；非
  `custom_messages` 会话的 `late` 仍走现有 note/tool-result 文本，`inline`/`off` 同样生效，只是
  没有 custom 行。不要只为 omp-ui 读这个配置。
- **显式工具结果不受展示策略过滤**：在 `code_intel.semgrep.enabled=true` 的前提下，模型主动调用
  `code_intel`/`code_graph_update(refresh_file, semgrep=true)` 时，findings 作为该 tool 的 result
  返回；`lsp.display` 只过滤自动写入/补丁诊断面，不静默裁剪显式查询。`mode=off` 不阻止显式查询，
  只跳过 write/patch 的即时扫描。总开关关闭时，显式 `refresh_file(semgrep=true)` 同样不扫描，
  `code_intel(diagnostics)` 只能返回已存图并标注 freshness（不能把旧图冒充当前字节）。
- **作用域**：`lsp.display` 的 `inline`/`severities` 部分作用于每次 write/patch 的工具结果，因此
  对 CLI/Desktop/消息平台的 inline `lsp_diagnostics`/`semgrep_diagnostics` 同样生效；默认配置下
  仍逐字不变。late custom 行仍只对 `custom_messages:true`（omp-ui）生成，`transcript.custom`
  仍只对 `client:"omp-ui"` 发送。
- **caps 不是配置键**：`MAX_PER_FILE`/`MAX_TOTAL_CHARS`/`MAX_DISPLAY_PER_FILE`/
  `MAX_DISPLAY_TOTAL_CHARS` 是安全上限，P1 固定；不暴露给 `lsp.display`，避免用户放大事件/持久化
  载荷。需要调大时改常量 + 验收，不新增配置面。
- **开关优先级**：
  - LSP：`lsp.enabled=true` 且 `lsp.display.lsp.mode != off`。
  - Semgrep：`code_intel.enabled=true`、`code_intel.semgrep.enabled=true` 且
    `lsp.display.semgrep.mode != off`。
  - `mode=off` 只关闭诊断面（Semgrep 还跳过即时扫描）；`hermes code index` 的 Semgrep 层仍只看
    `code_intel.semgrep.enabled`，不新增第二个索引开关。
- **解析/失败**：`agent/lsp/display.py::DisplayPolicy.from_config()` 读
  `load_config_readonly()["lsp"]["display"]`。未知 `mode` → 警告 + `both`；未知 severity 条目
  丢弃；空/非法 `severities` → 该来源默认集合；任何解析失败都不让写入失败。配置在每次写入/检查时
  读取（或缓存并在 `hermes lsp restart`/config reload 时失效）。新键加入 `DEFAULT_CONFIG`，按现有
  `_config_version`/`hermes config migrate` 约定补齐缺失字段，不覆盖用户已有值。
- **默认即现状**：没有 `display` 块时，LSP=`{mode: both, severities: [error]}`、
  Semgrep=`{mode: both, severities: [error, warning]}`，与当前行为逐字一致；新增键只在用户显式
  配置时改变展示。
- **`hermes lsp status`** 增加一行解析后的 policy（`display.lsp.mode`/`severities` 等），便于
  排障；不新增命令。用户通过 `config.yaml` 的 `lsp.display` 配置，实现后
  `hermes config get lsp.display` 可查看该 section。
- **不复制扫描开关**：`lsp.display.semgrep.mode` 不是 `code_intel.semgrep.enabled` 的别名；前者
  只控制诊断面/即时扫描，后者控制 Semgrep 层总开关。

## 协议

**行（持久化与快照都走这一形状）**：

```json
{
  "role": "custom",
  "custom_type": "lsp-late-diagnostic",
  "text": "<system-notice>\nDiagnostics for the files you edited:\nC:\\repo\\src\\app.ts — 3 error(s)\nC:\\repo\\src\\app.ts:12:5 [error] [typescript] Type 'string' is not assignable to type 'number'. (2322)\n…\n\nFix them if they are real, then continue.\n</system-notice>",
  "display": true,
  "display_only": false,
  "attribution": "agent",
  "legacy_role": "user",
  "row_id": 12345,
  "timestamp": 1762214400.0,
  "details": {
    "files": [
      {
        "path": "C:\\repo\\src\\app.ts",
        "summary": "3 error(s)",
        "errored": true,
        "messages": [
          "C:\\repo\\src\\app.ts:12:5 [error] [typescript] Type 'string' is not assignable to type 'number'. (2322)",
          "C:\\repo\\src\\app.ts:18:1 [error] [typescript] Cannot find name 'foo'. (2304)",
          "C:\\repo\\src\\app.ts:40:9 [error] [semgrep] Unsafe use of eval. (typescript.lang.security.audit.eval)"
        ]
      }
    ]
  }
}
```

- DB 的 `content` 在 wire 上投影为 `TranscriptMessage.text`（`_custom_message` 的 `text` 字段）；
  行内部仍叫 `content`。`details` 原样带出，客户端只读 `custom_type`/`details`。

**实时事件**：

```json
{
  "type": "transcript.custom",
  "payload": {
    "message": { "role": "custom", "custom_type": "lsp-late-diagnostic", "…": "同上" }
  }
}
```

- `message.row_id` 必填（行已持久化）；客户端按它合并 live 与快照。
- 事件只在 `local_viewer_turn(turn)` 且 `client == "omp-ui"` 时发布。
- 不送 `display_metadata` 信封；`details` 即展示载荷。

**持久化键**（内部，不出网关）：

```json
{
  "display_metadata": {
    "_custom_message": {
      "custom_type": "lsp-late-diagnostic",
      "display": true,
      "display_only": false,
      "attribution": "agent",
      "legacy_role": "user",
      "details": {
        "files": [
          {"path": "C:\\repo\\src\\app.ts", "summary": "3 error(s)", "errored": true,
           "messages": ["C:\\repo\\src\\app.ts:12:5 [error] [typescript] … (2322)"]}
        ]
      }
    }
  }
}
```

- `content` 是 DB 的 `content` 列；六个 custom 字段只走 `_custom_message`。
- 合并写入；同一行重复 flush 由 `_DB_PERSISTED_MARKER` 去重。
- 单条 `messages` 行、单文件条数、总字符都受显示上限（`MAX_DISPLAY_PER_FILE` /
  `MAX_DISPLAY_TOTAL_CHARS`）约束；超出用 `summary` 计数，`details.messages` 不追加不可解析的
  省略行。模型 `content` 另受 `MAX_PER_FILE`/`MAX_TOTAL_CHARS` 约束。

## 实现落点

| 层 | 文件 | 变更 |
|---|---|---|
| custom 类型 | `agent/custom_messages.py` | `MESSAGE_CUSTOM_TYPES` 增加 `lsp-late-diagnostic`；确认 `project_for_api`→`developer` |
| 配置默认值 | `hermes_cli/config_defaults.py` | `lsp.display.lsp`/`lsp.display.semgrep` 默认块：`mode: both`，severities `[error]`/`[error, warning]`；按约定更新 `_config_version` 并让 migrate 补齐 |
| 配置解析 | `agent/lsp/display.py`（新增） | `DisplayPolicy.from_config()`：读 `lsp.display`，校验 `off|inline|late|both` 与 `error|warning`，返回默认/回退；不让写入失败 |
| LSP gating | `tools/file_operations_lint.py`、`agent/lsp/edit_check.py`、`agent/lsp/reporter.py` | 按 `lsp.display.lsp`：`off` 跳过 edit check；`inline` 超时丢弃；`late` 把预算内 verdict 转 `deferred`；`both` 现状；severity 过滤后算 summary/errored |
| Semgrep gating | `tools/file_operations_lint.py`、`hermes_codegraph/analysis.py`、`hermes_codegraph/update.py` | 按 `lsp.display.semgrep`：`off` 跳过即时扫描；severity 过滤；`code_intel.semgrep.enabled` 仍是总开关，索引层不受 `mode` 影响 |
| 状态输出 | `agent/lsp/cli.py` | `hermes lsp status` 显示解析后的 `display.*.mode`/`severities` |
| 文档 | `website/docs/user-guide/features/lsp.md` | 记录 `lsp.display` 的键、默认值、`mode` 语义与优先级 |
| 延迟队列 | `agent/lsp/deferred.py` | `_Note` 增加 `files`；`post`/`take`/`restore`/`pop_notes` 透传并按 path/source 合并 |
| 展示格式化 | `agent/lsp/reporter.py` | `display_files_for`/`display_lines_for`：pi-tui 可解析行、summary/errored、纯文本 sanitizer；模型块保持现有 HTML 转义 |
| late verdict | `tools/file_operations_lint.py` | `_queue_late_verdict` 同时构造 `files`；`deferred.post` 传入；模型 `text` 逐字不变 |
| 行投递 | 新增 `agent/lsp/delivery.py`（或 `agent/late_diagnostics.py`） | `append_row(agent, messages, files)`：custom vs legacy 形状、append、flush、notify；三条投递点共用 |
| 工具结果投递 | `agent/tool_executor.py` | 不再把 LSP note 追加到 tool `content`；tool 行 flush 后 append custom 行 |
| turn-end | `agent/turn_stop_gates.py` | `_late_lsp_verdicts` 返回 `files`/行；append custom 行；`continue_turn` 保留 |
| 下一个用户 turn | `agent/turn_context.py` | `_merge_gateway_notes` 对 custom 会话 append 行，不折进用户内容；flush 后 notify |
| 实时事件映射 | `tui_gateway/agent_callbacks.py` | `plugin_row_event` 增加 `lsp-late-diagnostic`→`transcript.custom`；`_announce_plugin_row` 分支 |
| canonical 发射 | `gateway/run_turn_runner.py` | `_canonical_plugin_event` 发 `transcript.custom`（omp-ui 门） |
| 契约 | `tui_gateway/contracts/events.py` | `TranscriptCustomPayload{message: TranscriptMessage}` |
| 单一投影 | `tui_gateway/session_history.py` | 抽出 `custom_row_message`，live 与 `_history_to_messages` 共用 |
| 能力表 | `gateway/panel_inventory.py` | `late_diagnostics` → `READY`，事件 `transcript.custom`，字段 `custom_type`/`details`/`row_id` |
| 持久化 | `agent/session_persistence.py`、`hermes_state_messages.py` | 验证 custom 六字段/role 白名单/`_custom_message` 信封 |
| 分支/压缩 | `tui_gateway/methods_session_branch.py`、`tui_gateway/session_workdir.py` | `_BRANCH_COPY_FIELDS`/`_WORKDIR_SEED_FIELDS` 增加六个 custom 字段（或重新封装 `_custom_message`）；分支测试 |
| Semgrep 即时源（P2） | `hermes_codegraph/analysis.py`、`tools/file_operations_lint.py`、`agent/lsp/edit_check.py` | 与 LSP 同时启动 `semgrep_scan`；预算内进工具结果，超时进同一 `deferred` 队列；映射 `[semgrep]` 行 |
| Semgrep 工具结果（P2） | `hermes_codegraph/update.py`、`tools/code_graph_update_tool.py`、`tools/code_intel_tool.py` | `refresh_file(semgrep=true)` 返回 findings；`code_intel(diagnostics, path=...)` 现场扫描；修掉 `update.py:95-99` 的丢弃 |
| Semgrep 内联字段（P2） | `tools/file_operations.py`、`tools/file_operations_lint.py` | `WriteResult`/`PatchResult` 增加 `semgrep_diagnostics`；`lsp_diagnostics` 保持 LSP-only；header 改 source-neutral |
| Semgrep 合并（P2） | `agent/lsp/reporter.py` / `deferred` 的 files 构造 | 按 path 合并 LSP/Semgrep 行，重算 summary/errored |
| 壳事件 | `omp-ui/src/modes/rpc/gateway-session.ts` | `transcript.custom` handler；`rowToAgentMessage` + `#emitCustomMessage`（按 `row_id` 去重） |
| 壳渲染 | `omp-ui/src/modes/utils/ui-helpers.ts` | `#customBlock` 增加 `lsp-late-diagnostic`：`routeLateDiagnostics` + `LateDiagnosticsMessageComponent` + 展开/可见性 |
| 生成物 | `scripts/gen_gateway_contracts.py`、`scripts/gen_panel_inventory.py` | 重生成 TS 契约/面板清单 |

## 分阶段

- **P1（LSP 行、实时 + resume）**：D1–D3 的行/`files`/三个投递点；D4 的 `transcript.custom`；
  D5 的壳 handler/组件；D6 的持久化/resume；D7 的归属；D12 的 `lsp.display.lsp`（mode/severities）。
  验收：turn-end 行出现、模型按 `developer` 读到、当前 turn 继续；mid-turn 行在 tool 结果之后；
  resume 重建同一块；live/快照按 `row_id` 只画一次；非 omp-ui 零变化；默认配置与当前行为逐字
  一致。
- **P2（Semgrep 即时源）**：D8 的 Semgrep 与 LSP 同预算/同延迟队列/同工具结果；write/patch 触发
  扫描，模型诊断工具返回即时 result；按 path 合并 `[semgrep]` 行，模型行与 `details` 都含合并
  结果；D12 的 `lsp.display.semgrep`（mode/severities）。验收：LSP clean 但 Semgrep 命中时仍
  出现；超时走 late；新行不保留旧 `[semgrep]`；工具结果即时可见；`mode=off` 跳过即时扫描但不
  影响 `hermes code index`。
- **P3（硬化）**：同一 turn 多条 verdict 的合并/排序；Windows 路径/大小写/symlink；warning 色调；
  provider `developer` 角色兼容（ADR-10 D11）；能力表/文档；行数与字符上限的回归；D12 的无效
  mode/severity 回退、`hermes lsp status` 输出与配置文档。

## 验收

1. **turn-end 触发修复**：写一个 Python 文件让 mock LSP 延迟返回；turn-end 时生成一条
   `custom_type:"lsp-late-diagnostic"` 行，`content` 为 source-neutral system-notice，
   `details.files` 带完整诊断；模型请求里该行以 `developer` 出现；`continue_turn` 让模型在同一
   turn 内尝试修复；不再出现 `role:"user"` 的 late note / 用户气泡。
2. **mid-turn 投递**：编辑不是最后一个工具调用时，下一个工具结果之后生成同一行；tool result 的
   `content` 与当前逐字一致，不再追加 LSP note；模型下一次请求能看到该行。
3. **下一个用户 turn**：turn 结束后才到达的 verdict 在下一次用户消息之后生成一行，且只生成一次；
   模型在回答前看到它。
4. **展示挂载**：`write_file`/`patch(replace)` 的 late verdict 在 native 下进入对应编辑/写入卡；
   没有对应卡时生成一个 `LateDiagnosticsMessageComponent`；ANSI 下为独立块。V4A
   `patch(mode="patch")` 按 D5 的例外走独立块，实时与 resume 一致。
5. **不重复**：内联 `lsp_diagnostics` 不生成 late 行；同一 verdict 不因 mid-turn 与 turn-end 重复
   成两行；被新编辑 supersede 的旧 verdict 不生成行；同一 `row_id` 的 live 事件与快照只画一块；
   同一 path 的新 verdict 是合法的新行/新卡，不按内容去重。
6. **resume/rebuild**：重新加载后 `session.resume` 带同一 custom 行，omp-ui 重建同一块；会话内
   `rebuildChatFromMessages()` 重放 `#messages` 仍走 `#customBlock`，不依赖壳快照或
   `display_metadata` 的 live 投影。
7. **载荷/解析**：`details.files` 只有 `path`/`summary`/`errored`/`messages`；每一行都能被
   `parseDiagnosticMessage` 解析，不出现 `unparsed`；行内解析出的 `filePath` 与 `files[].path`
   逐字一致；severity 小写、source 在 message 前、code 在末尾；Windows `C:\...` 用例通过。
8. **模型不变（legacy，默认配置）**：非 `custom_messages` 会话的
   `WriteResult.lsp_diagnostics`、tool result 内容、turn-end user note 文本在默认
   `lsp.display` 下与当前逐字一致；`tests/agent/lsp/test_write_never_waits_on_lsp.py` 全绿。
9. **范围/安全**：`client != "omp-ui"` 的本地 viewer 与所有消息平台不收到 `transcript.custom`；
   `custom_messages:false` 的会话不写 custom 行；默认 `lsp.display` 下这些客户端的 inline 输出
   逐字不变，显式配置后 inline 诊断按策略过滤；诊断文本无 HTML 实体、控制字符被剥离、
   行数/长度受上限；不送文件内容。
10. **Semgrep 即时诊断**：`write_file`/`patch` 在预算内返回 `[semgrep]` 行并并入工具结果；超时
    则进入同一 `deferred` 队列并在 late 行出现。模型调用 `code_graph_update
    refresh_file(semgrep=true)`/`code_intel` 时，findings 直接在 tool result 中。LSP clean 时也能
    显示；每行可被 `parseDiagnosticMessage` 解析，source=`semgrep`、code=rule_id。
11. **Semgrep 失效**：先得到 `[semgrep]` 行，再对同一 path 成功写入/打补丁；新行不包含旧 finding，
    旧行作为历史保留；Semgrep 关闭/失败/超时时新行没有 `[semgrep]` 行，不把旧 finding 合并回来。
    没有“先跑索引”的前置条件。
12. **触发边界**：turn 已结束且没有下一个用户消息时不自动启动新 turn；展示层不调用模型；不新增
    模型工具；配置只限 `lsp.display`（D12）。
13. **能力表**：`runtime.describe.transcript_blocks` 的 `late_diagnostics` 报
    `supported:true`，事件为 `transcript.custom`，字段为 `custom_type`/`details`/`row_id`；
    `docs/omp-ui-panels.json` 与 TS 契约重新生成且 orphan 门通过。
14. **压缩/rewind**：压缩/分支复制保留 custom 行与 `details`；rewind 到行之前后不显示该块；
    `role:"custom"` 不被当作 user turn 计数。
15. **provider 形态**：OpenAI-compatible 与 Anthropic 适配器都能接受该行（ADR-10 D11 的
    `developer` 折叠）；请求体不出现 `custom`/`details`/`display`/`attribution` 等内部键。
16. **默认配置**：没有 `lsp.display` 块时，LSP 只显示 error、inline+late 都开；Semgrep 显示
    error/warning、inline+late 都开；输出与当前行为逐字一致；`lsp.enabled`/
    `code_intel.semgrep.enabled` 的默认值不变。
17. **`lsp.display.lsp`**：`off` 不内联、不生成 late 行、跳过该次 edit check；`inline` 超时丢弃
    且不生成 late 行；`late` 把预算内 verdict 转 `deferred`、不内联；`both` 保持当前行为；
    `severities` 只留 `error` 或加上 `warning` 时，`details`/`content`/`summary`/`errored` 同步
    变化。
18. **`lsp.display.semgrep`**：`code_intel.semgrep.enabled=false` 时无论 `mode` 如何都不扫描、
    不显示，显式 `refresh_file(semgrep=true)` 也不扫描（`code_intel(diagnostics)` 只返回已存图并
    标 freshness）；master switch 开启时，`mode=off` 跳过 write/patch 的即时扫描，但
    `hermes code index` 的 Semgrep 层不受影响，显式 `code_intel`/`code_graph_update(refresh_file,
    semgrep=true)` 的 findings 仍在 tool result；`severities` 过滤后 summary/errored 重算。
19. **无效配置**：未知 `mode` → 警告 + `both`；未知 severity 条目丢弃；空/非法 `severities`
    → 该来源默认集合；写入、工具结果、turn 不因配置错误失败；`hermes lsp status` 显示解析后的
    值。
20. **模型/展示同源**：同一 `lsp.display` 过滤后的集合同时用于 `details` 和 `content`；不存在
    “UI 隐藏但模型仍读”的分叉。

## 风险

- **provider 角色**：`developer` 行插在 tool 结果之后或 assistant 行之后，个别严格适配器可能拒绝
  该位置。ADR-10 D11 已定义归一化；验收 15 覆盖 OpenAI/Anthropic。不要在会话层改成 `user` 来
  规避，否则与 omp 和 ADR-10 的 custom→developer 语义分叉。
- **`MESSAGE_CUSTOM_TYPES` 漏加**：`project_for_api` 会静默丢弃该行，模型看不到诊断，“触发修复”
  失效，但展示仍正常。验收 1/15 必须在 provider 请求里断言 `developer` 行存在。
- **prompt cache**：行只能追加在尾部；不得在 append 后回写 `content`/`details` 或重排历史。同一
  row 的 resume 重放必须字节稳定。
- **行增长**：turn-end 每次最多 3 条（`_MAX_LSP_TURN_END_NOTES`）；`files`/`messages` 受
  `MAX_DISPLAY_PER_FILE`/`MAX_DISPLAY_TOTAL_CHARS` 限制（模型 `content` 另受
  `MAX_PER_FILE`/`MAX_TOTAL_CHARS` 限制）；不要为每个 path 各发一条行。
- **压缩/分支**：custom 行是模型上下文消息，压缩器可能把它纳入摘要；`display_only:false` 不能
  改成 true，否则模型丢失诊断。`role:"custom"` 不得进入 user-turn 的 rewind 地址空间。
- **路径归一化**：`deferred.post` 用绝对路径，工具 args 可能相对；Windows 大小写/分隔符、symlink、
  session cwd 与进程 cwd 不一致都会导致“挂不到卡”。兜底是独立块，但不能静默丢行/事件。
- **Semgrep 成本**：`mode=auto` 可能联网拉规则，冷启动扫描可能远超 write budget。必须复用
  `WRITE_BUDGET`/`deferred`，设置单文件超时；超时就发 late，不能阻塞写入。用户开启
  `code_intel.semgrep.enabled` 即接受这次扫描成本。
- **Semgrep 与 LSP 合并**：同一 path 的 LSP/Semgrep 行必须合并成一份完整 `files`；任何一方更新都
  重算 summary/errored，不能用第二次行把另一方覆盖掉。相同 finding 在后续编辑后合法复现，不能按
  `messages` 内容去重；只按 notification seq/内容身份去重。
- **事件重复**：事件流重连/回放可能重复同一行；客户端按 `row_id` 去重，不按内容去重。同一 path 的
  新 row_id 不是重复帧，必须画新行/新卡。
- **旧客户端**：不认 `lsp-late-diagnostic` 的客户端回退 `CustomMessageComponent`（text-only），
  不画诊断树；这比伪造块安全。Desktop/Ink 会话不产生该行。
- **turn-end 双语义**：custom 行是模型可见的真实消息，不是 user row；只对 `custom_messages`
  会话写。压缩/分支复制必须保留六字段，否则 resume 会退化成 text-only 或用户气泡（如果误写成
  legacy user row）。
- **sidecar 分支**：`plugin_row_event` 被 sidecar 与 canonical 共用；新增 `transcript.custom`
  必须同时更新 `_announce_plugin_row`，否则会落进 `message.adviser` 的 else 分支。
- **ADR-8 文本与工具面不一致**：ADR-8 D17 说 `code_graph_update` 固定 `semgrep=false`，当前工具
  仍暴露并透传 `semgrep`。本 ADR 以工具面的显式参数为准；ADR-8 文本应在独立变更里更正。若反过来
  移除该参数，模型仍可通过 write/patch 的即时 Semgrep 获得诊断，不依赖它。
- **`mode=inline` 的静默丢失**：该模式明确丢弃超时 verdict（没有 late 行）。这是用户选择的策略，
  不是 bug；文档要写明“需要晚到也显示就用 `both` 或 `late`”，不要让实现偷偷改成 `both`。
- **`mode=late` 的时序**：预算内 verdict 也会被转 `deferred`，所以行可能在编辑后一个投递点才出现；
  模型 `content` 用 timing-neutral 的 “Diagnostics for the files you edited:”，不要写回
  “arrived after the edit returned” 造成事实错误。
- **配置读取/缓存**：`lsp.display` 在每次写入/检查时读取，或缓存并在 `hermes lsp restart`/config
  reload 时失效；读配置失败、未知 mode、非法 severity 都只能回退默认并记 warning，绝不能让
  `write_file`/`patch` 失败。验收 16–19 固定这些口径。
- **展示策略不是 UI-only**：过滤后的集合同时进 `content` 和 `details`；不要实现“客户端隐藏、
  模型仍读”的双轨，否则用户看到的诊断与模型修复的诊断会分叉。
- **`lsp.display.semgrep` 与 `code_intel.semgrep.enabled` 混淆**：前者是诊断面/即时扫描的策略，
  后者是 Semgrep 层总开关；`mode=off` 不影响 `hermes code index`。文档和 `hermes lsp status`
  要同时显示两者，避免用户以为关掉展示就关掉了索引。

## 不采纳

- **旧 ADR 的“编辑/写入卡附件 + `display_metadata` + carrier suffix”**：行本身是 durable custom
  message，不需要 path→tool_call_id、suffix 剥离、shell 快照或 `removed_paths`。
- **只发 `diagnostics.late` 展示事件**：与行重复；live 与 resume 会走两套形状。
- **新造 `diagnostics` 角色 / 独立行类型**：ADR-10 的 `role:"custom"` 是唯一结构；provider 只认
  `developer`/`user` 等白名单角色。
- **`display_kind="hidden"` 或 `display_only:true`**：行会被展示投影或模型转换丢弃；“完整展示 +
  触发修复”都不成立。
- **从 tool result 文本反解析诊断**：HTML 转义、header、truncate 都使解析不可靠；会丢
  severity/source/code。
- **客户端重新查询 LSP**：客户端没有写入权威与服务端 snapshot，结果会与模型不同步。
- **把内联诊断也做成 late 行**：重复展示同一事实；内联留在工具卡。
- **用通知/状态行代替组件**：没有折叠、路径匹配、卡内挂载。
- **让 late diagnostics 启动新 turn**：改变 Hermes 的 turn/计费语义；展示层不调用模型。
- **对所有本地 viewer 广播 `transcript.custom`**：没有消费点，且扩大持久化面；用 ADR-6 的
  client 门。
- **为 late diagnostics 新增模型工具**：没有消费者；展示策略只加 `lsp.display`（D12），不新增
  模型工具。
- **把 `lsp.display` 做成纯 UI 过滤**：会与 `content` 分叉，用户看到的诊断和模型修复的诊断不同；
  过滤必须同时作用于 `details` 和模型行。
- **在 `lsp` 下复制 `code_intel.semgrep.enabled`**：那是扫描/索引总开关；`lsp.display.semgrep`
  只控制诊断面/即时扫描，`mode=off` 不关闭 `hermes code index` 的 Semgrep 层。
- **把 `mode=off` 当成 `lsp.enabled=false` / `code_intel.enabled=false`**：展示策略不关闭子系统；
  关闭服务仍用原有开关。
- **沿用 ADR-8 D17 的“仅显式索引、写路径不跑”**：这会让 Semgrep 在编辑后不可见，与“Semgrep 和
  LSP 一样是即时诊断”矛盾。
- **按 path 直接展示所有 `CodeDiagnostic`**：旧 Revision 的 finding 会混入当前文件；即时诊断必须
  来自刚写入字节的 `semgrep_scan`，查询旧图只能作为单独的查询面。
- **为 Semgrep 新增独立事件/独立块**：同一行按 path 合并即可，独立事件会与 LSP 块重复、破坏
  折叠。
- **`removed_paths`/`clearLateDiagnostics`**：行是历史，不是 path 缓存；同一 path 的新 verdict 是
  新行，旧行保留。清空旧行会篡改历史，并与 omp 的 custom message 语义分叉。

## 遗留

- 旧 ADR 的 `path -> tool_call_id` 映射与 `late_diagnostics_carrier.suffix` 不再需要：历史块由
  custom 行恢复；如果未来要支持“把某条 late 行挂回特定历史卡”的额外交互，再单独设计。
- warnings 已由 `lsp.display.*.severities` 支持（默认 LSP 仍只有 `error`）；如果未来要展示
  `info`/`hint`，需要同时处理组件的色调/折叠口径，另立 ADR。`summary`/`errored` 已为此留好形状。
- 如果未来需要把 late diagnostics 作为严格“carrier 之后”的顺序块，P3 再引入批量/排序；本 ADR
  不承诺跨客户端一致的时间线。
- `lsp.display.*.severities` 只接受 `error`/`warning`；Semgrep 的 `info`/`hint` 与 LSP 的
  `info`/`hint` 仍不在展示面（组件没有独立色调）。要统一完整 severity 策略，需要另立配置/展示
  决策。
- `code_intel`/`hermes code diagnostics` 对已存 `CodeDiagnostic` 的查询仍可能返回旧 Revision 的
  行；它只是查询面，不是写入后即时诊断的来源。若未来要让查询面也证明新鲜度，再给 schema 加
  Revision/内容身份；本 ADR 的即时路径不依赖它。
- `transcript.custom` 是通用 custom 行事件；本 ADR 只定义 `lsp-late-diagnostic` 的消费者。其它
  custom 类型若需要 live 事件，可直接复用该事件，但必须自己的 `custom_type` 在
  `MESSAGE_CUSTOM_TYPES` 中有明确模型语义。
