# ADR-9：Late diagnostics 的移植（`diagnostics.late` ↔ omp `lsp-late-diagnostic`）

**状态**：已决定（2026-11-03）。实现未开始。分阶段：**P1** 延迟 verdict 携带结构化
`files` + `diagnostics.late` 事件 + omp-ui 实时挂载（native 进编辑/写入卡，否则独立块）；
**P2** 按路径回填原编辑/写入 tool 行、标记 turn-end 已有的 user note 行、标记 carrier tool 行，
并让 resume 重建；**P3** Semgrep 危险模式诊断作为与 LSP 并列的即时诊断源接入（同一预算/延迟队列/
工具结果），以及同一 turn 的多条 verdict 合并。P2 依赖 ADR-2 **D8** 的 tool 行
`display_metadata` 投影与卡片重建。落地前
`docs/omp-ui-panels-gateway-plan.md` §2.3.2.1 的 Late diagnostics 行保持 `无 producer`（§0.1）；
客户端不画假块、不把内联诊断伪装成 late block、不用 `notification.show`/`status.update`/`agent.print`
冒充。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§2.3.2、§2.3.2.1、§4.7；
`docs/appendix-omp-ui-realtime-rpc.md` A.14、A.15；ADR-2（D8 的 tool 行投影、D9 的“无 producer”清单、
`display_kind`/`display_metadata` 惯例）；ADR-5/ADR-6（对 D9 其它项的转述）；ADR-6 **D1** 的
`client:"omp-ui"` session 级 opt-in；`hermes_codegraph/analysis.py`（Semgrep 扫描实现）、
`hermes_codegraph/update.py`、`hermes_codegraph/api.py`、`hermes_codegraph/query.py`、
`tools/code_graph_update_tool.py`。**本 ADR 不采用 ADR-8 D17 的“仅显式索引 / 写路径不跑”限制**；
Semgrep 与 LSP 同为即时诊断源，ADR-8 的索引诊断只作为查询面的既有实现保留。
`agent/lsp/deferred.py`、`agent/lsp/reporter.py`、
`agent/lsp/edit_check.py`、`tools/file_operations_lint.py`、`agent/tool_executor.py`、
`agent/turn_stop_gates.py`、`agent/turn_context.py`、`gateway/local_viewer_events.py`、
`gateway/run_turn_progress.py`、`tui_gateway/contracts/events.py`、
`tui_gateway/session_history.py`。

**范围**：只讨论 omp-ui ↔ Hermes canonical local gateway。消息平台、Desktop、Ink sidecar、ACP、
compute-host 的 worker 帧都不设计；不新增模型工具、不改 system prompt、不改 LSP 的检查/节流/预算；
不把 late diagnostics 变成新的 turn 触发源。`agent/lsp` 的延迟投递语义（谁在何时看到 verdict）由本
ADR 之外的既有实现负责，本 ADR 只增加展示面。

Semgrep **是**新的即时诊断源，与 LSP 并列：`write_file`/`patch` 的编辑检查同时启动两者，同一
`WRITE_BUDGET` 内返回就内联进工具结果，超出预算就进入同一个 `deferred` 队列；模型调用诊断工具时，
结果作为该 tool 的即时 result 返回。`code_intel.semgrep.enabled` 仍是开关，`mode` 仍决定规则来源；
不再要求先跑 `hermes code index`/`refresh_file`，也不再沿用 ADR-8 D17 的写路径排除。

**取证口径**：Hermes 侧引用本仓库当前工作树（2026-11-03）。omp 侧源码来自本地 checkout
`C:\Users\zarag\projects\oh-my-pi`（`@oh-my-pi/pi-coding-agent` 18.14.0）；渲染侧同时核对壳实际安装的
`omp-ui/node_modules/@oh-my-pi/pi-tui`（18.14.1）。`omp-ui/src/vendor/` 是上游的裁剪副本。本 ADR 不改变
pi-tui 的渲染分支、文案、折叠与色调；失效清空需要给 `ToolExecutionComponent` 增加一个只清数据的
`clearLateDiagnostics` seam，这不属于渲染语义改动。

**取代关系（逐条，避免文档互斥）**：

- ADR-2 **D9** 的 **独立 late-diagnostics** 一项（`ADR-2-transcript-blocks.md:215-216`：Hermes 没有等价
  数据，不造块）由本 ADR 的 D1–D10 取代。D9 的其它项（被剥离工具调用、collab prompt、reply write、
  自定义/hook 消息）不变。
- ADR-5:32 与 ADR-6:23 对 D9 的转述中，“独立 late-diagnostics 不变”一句只对本项失效；其余项继续有效。
- plan §2.3.2.1:445 的 `Late diagnostics | … | 无 producer`：实现 P1 后来源列改为
  `diagnostics.late`，状态改为 `已接（local）`；P2 完成后 resume 也保留，状态仍为 `已接（local）`。
  该行“诊断已在工具卡内，不另造块”只描述**内联** verdict；本 ADR 补的是内联之外、写预算耗尽后才到达的
  verdict，且仍不新增 transcript 行。实现前保持原文。
- plan §2.3.2:403 的 `late-diagnostics-message.ts` 渲染器描述**不变**：组件、折叠行数、展开键位、
  错误/警告色调都由 pi-tui 决定；变化只在数据生产者与映射层。
- ADR-2 **D8** 继续适用：`rowToAgentMessage` 读 `display_kind`/`display_metadata`；本 ADR 的 P2 复用
  该投影，不新增行类型或顶层字段；唯一的投影行为变化是按 `late_diagnostics_carrier.suffix` 做精确
  尾部剥离，以及让 `display_kind="late_diagnostics"` 的 user 行走 late 分支。
- ADR-6 **D1** 的 `client:"omp-ui"` 是本 ADR 的 session 级门；本 ADR 不新增第二种 opt-in 机制。
  如果 ADR-6 D1 尚未落地，本 ADR 的 P1 随之等待（或作为 D1 的第一个消费者一并落地）。

## 问题

omp 的 **late diagnostics** 是“编辑/写入工具已经返回之后才到达的 LSP 诊断”的展示面。它同时是
模型消息和 transcript 区块：模型从 custom message 的 `content` 读到诊断，客户端从 `details.files`
渲染卡内诊断或独立块。Hermes 已经有“延迟到达的诊断”，但只有模型面，没有对应的结构、事件和客户端
消费点。两者不是同一条链路，直接照搬会把展示、模型投递、持久化三件事混在一起。

### 1. omp 的语义

**数据**：`DeferredDiagnosticsEntry`（`packages/coding-agent/src/tools/index.ts:192-210`）：
`{path, summary, messages, errored, isStale()}`。`path` 是绝对路径；`summary` 是一行严重度摘要
（如 `"2 error(s), 1 warning(s)"`）；`messages` 是已经格式化好的可显示行；`errored` 表示是否含
severity 1；`isStale()` 在注入时求值，文件被更新 mutation 覆盖就丢弃。

**模型消息**：`buildLateDiagnosticsBatchMessage`（`packages/coding-agent/src/sdk.ts:434-464`）把一批
entry 合成 `role:"custom"`、`customType:"lsp-late-diagnostic"`、`display:true`、
`details:{files:[{path,summary,errored,messages}]}` 的消息；`content` 由 `lateDiagnosticTemplate`
渲染，供模型阅读。消息通过 session 的 `yieldQueue` 注册
（`sdk.ts:5291-5294`），空闲时 `injectIdle` 走 `agent.prompt(...)`，流式中走
`injectStreaming`；`isStale` 在真正注入时过滤（`session/yield-queue.ts:43-230`）。所以“late”在 omp
既是“工具返回后才到”，也是“跨请求注入”：空闲到达会启动一个新 turn。

**持久化**：custom message 进入 session entries，resume 时由
`ChatTranscriptBuilder#appendCustomMessage` 重建（`packages/tui/src/chat/chat-transcript-builder.ts:606-613`）。

**渲染**：

- `LateDiagnosticsMessageComponent`（`packages/tui/src/chat/late-diagnostics-message.ts:69-175`）
  聚合 `files` 的 `messages`/`summary`/`errored`，复用工具诊断的树渲染器；`summary` 拼进标题，
  collapsed 最多 5 行（`COLLAPSED_DIAGNOSTICS`），展开跟随全局 tool-output 开关。
- 实时挂载：`routeLateDiagnostics(chatContainer.children, files)` 在 native 渲染下把每个文件交给
  最近的、路径匹配且实现了 `attachLateDiagnostics` 的 tool frame
  （`late-diagnostics-message.ts:38-67`；`tool-execution.ts:993-1013` 用 `displayPath` 比对，
  只接受 native）。未被任何 frame 接收的文件才生成独立组件
  （`modes/utils/ui-helpers.ts:321-334`）。ANSI 路径全部独立成块。
- 同一文件后续到达的 late 诊断替换该卡上的 `#lateDiagnostics`，不叠加第二块。

### 2. Hermes 现有链路：诊断有了，展示没有

**内联 vs 延迟**：`write_file` / `patch` 返回前只等 LSP 一个很短的预算（`WRITE_BUDGET = 0.08s`，
`agent/lsp/edit_check.py:44`；`inline_wait` 取剩余预算）。预算内拿到 verdict，就写进
`WriteResult.lsp_diagnostics` / `PatchResult.lsp_diagnostics`
（`tools/file_operations.py:1598-1605`），随 `tool.complete.result` 进入工具卡；预算耗尽则
`tools/file_operations_lint.py:365-390` 起 done-callback，verdict 进入
`agent/lsp/deferred.py`。**只有后者是 late**。

**延迟队列**：`deferred.py` 以 `owner`（session id，其次 task id）和绝对路径为键，每个
`(owner, path)` 只保留一条 `_Note{text, baseline_text}`（`:61-73`）。新编辑 `take` 走旧 note，
新检查以旧 baseline 做差；旧检查被 supersede 时不 post；没有 verdict 时 `restore`/`carry`
（`:74-93`、`edit_check.py:134-178`）。“同一文件只报该 session 自己的错误”已有回归测试
（`tests/agent/lsp/test_write_never_waits_on_lsp.py`）。

**投递点**（都是模型面，没有 UI 事件）：

1. 下一个工具结果：`agent/tool_executor.py:1160` 在 append 前调用 `forward_to_agent`，note 被
   `queue_plugin_notice` 放进 live-notes，随后 `attach_to_tool_message` 追加到该 tool result 尾部
   （`agent/plugin_live_notes.py:30-55`）。
2. 下一个用户 turn：`agent/turn_context.py:870` 在 turn 开始时把尚未投递的 verdict 并进 plugin
   notice，随用户消息注入。
3. turn 结束：`agent/turn_stop_gates.py:154-176` 调 `settle_for_agent`，最多等
   `TURN_END_WAIT = 5.0s`，把仍未投递的 verdict 作为一条以 `[System note: ...]` 开头、`role:"user"`
   的 note 返回，由 `apply_stop_gates` append；超过 5s 的 verdict 留给下一个用户 turn。

**只有文本**：`_queue_late_verdict` 用 `report_for_file` 生成
`<diagnostics file="...">ERROR [l:c] …</diagnostics>`，`deferred.post` 只存字符串和 baseline
（`tools/file_operations_lint.py:393-417`）。`reporter.py` 的 `_sanitize_field` 会 HTML 转义
`< > &` 并截断，因为这是要嵌进模型 `<diagnostics>` 块的内容。展示层如果直接复用这段文本，会显示
`&lt;` 等实体，也会把“模型文本”当成“展示文本”。

**没有 UI 面**：`tui_gateway/contracts/events.py` 没有 diagnostics 事件；`local_viewer_events.wire`
只装工具/思考/状态/桌面等 sink（`gateway/local_viewer_events.py:34-74`）；omp-ui 的
`#notificationHandlers` 没有对应条目，未知事件被表查找静默忽略
（`omp-ui/src/modes/rpc/gateway-session.ts:798-831`）。`LateDiagnosticsMessageComponent` 已在安装的
pi-tui 中，缺的是数据、事件与挂载。

**Semgrep 危险模式诊断的现状**：

- 扫描实现已有：`code_intel.semgrep.enabled` 默认 `false`，`mode=auto` 从 registry 拉规则，
  `mode=custom_dir` 只读 `HERMES_HOME/code-intelligence/semgrep/`；
  `hermes_codegraph/analysis.py::semgrep_scan` 已经在内存里返回
  `{path, rule_id, severity, message, start_line, start_column, end_line, end_column}` 的
  `findings`，不执行仓库代码、不写文件、`--metrics=off`。
- 当前只在 `hermes code index` / `refresh_file(semgrep=true)` 里调用；`indexer._run_semgrep`
  把这些 finding 写成 `CodeDiagnostic` 与 `CodeAnalysisRun`。`update.refresh_file` 目前**丢弃**
  `_run_semgrep` 的返回值（`update.py:95-99`），所以工具调用拿不到 findings；这是实现缺口，不是
  本 ADR 的边界。
- `code_intel` / `hermes code diagnostics` 能查询已写入的 `CodeDiagnostic`；当前 schema 没有
  Revision 字段，查询结果不保证对应当前字节。因此**展示层不能靠按 path 查询旧图来冒充即时诊断**；
  即时诊断必须来自刚刚对当前字节执行的扫描。
- 没有 UI 事件：Semgrep 结果只进图或查询，不会出现在编辑卡/工具卡。

本 ADR 不沿用 ADR-8 D17 “只有显式索引才跑、写路径不跑”的限制：既然模型调用工具和写入工具都能
即时得到 LSP 诊断，Semgrep 也应作为**同一诊断链路的即时源**，而不是等用户手动索引。代价是每次
编辑可能多一次扫描；由 `code_intel.semgrep.enabled`、`mode`、超时和延迟队列控制。

**兼容约束**：

- 模型面投递已经定型，且两条路形状不同：下一个工具结果把 note 追加在该 tool result 的 `content`
  尾部（不新增消息）；turn end 则追加一条 durable `role:"user"` 行
  （`agent/turn_stop_gates.py:282`，`_late_lsp_verdicts` 返回的 note），**它是一条模型可见的真实
  user row，当前在客户端画成用户气泡**。改成 omp 的 custom message 会改变模型上下文和 prompt cache
  的前缀，不是展示层可以决定的事；本 ADR 只给这条已有行加展示类型，不新增第二条 note 行。
- `display_metadata` 是纯展示列：写入时随消息持久化，送给 provider 前由
  `PERSISTENCE_ONLY_MESSAGE_FIELDS` 剥掉（`agent/message_metadata.py:46-54`）；resume 投影在
  `tui_gateway/session_history.py:341-345` 原样带出。它适合承载 late diagnostics 的结构化载荷。
- 没有“只展示、不进模型”的行类型：`display_kind="hidden"` 会从展示投影中消失
  （`session_history.py:315-318`），其它 `display_kind` 行仍在模型历史里。因此不能为 late
  diagnostics 新造一条独立行而不让模型重复读到同一段诊断。

### 3. 直接照搬会错的地方

| 做法 | 结果 |
|---|---|
| 把 late verdict 做成新的 `role:"custom"` 行（omp 原样） | 模型会在 tool result 的 note 之外再读一遍同一诊断；prompt cache 与消息交替都改变 |
| 解析 tool result 里的 `<diagnostics>` 文本来生成块 | 模型文本是 HTML 转义的、带 header 的、被 `truncate` 过的；解析脆弱且丢失 severity/结构 |
| 直接把 `deferred` 的 `text` 填进组件 | 展示 `&lt;`/`&amp;` 实体；路径与消息的截断口径也不属于展示层 |
| 只在客户端本地再跑一次 LSP | 客户端没有写入权威，也没有服务端 snapshot；会得到与模型不同步的诊断 |
| 把内联 `lsp_diagnostics` 也画成 late block | 内联 verdict 已经在编辑卡里；重复块违反“一个事实一次” |
| 用 `notification.show`/`status.update`/`agent.print` 转达 | 只会得到 toast/状态行；没有 `LateDiagnosticsMessageComponent` 的折叠、路径匹配与卡内挂载 |
| 把 late block 写成一条 `display_kind="hidden"` 的展示行 | 行会被展示投影丢弃；若不加 hidden，又会被模型读到，形成重复 |
| 忽略 turn-end 已经追加的 user row，另画一个结构化块 | 该 user row 是模型可见的；另画一块会与用户气泡重复。应给这条已有行加 `display_kind`/`display_metadata`，让壳把它画成 late block |
| 保留 carrier tool result 尾部的 note 文本在 resume 投影里 | 若 carrier 是 `write_file`/`patch`，卡片会同时出现原始 note 与结构化块；必须用 `late_diagnostics_carrier.suffix` 在展示投影里剥掉 |
| 在 `post()` 时就发事件 | verdict 可能被新编辑 supersede、被 turn-end 丢弃、或在进程退出前从未投递；UI 会显示模型从未看到的内容 |
| 对所有本地 viewer 都发事件 | TUI/Desktop/附件没有消费点；还会为它们写展示元数据。用 ADR-6 的 `client:"omp-ui"` 收窄 |
| 让 late diagnostics 启动一个新 turn（omp 的 idle `agent.prompt`） | 改变 Hermes 的 turn 语义、计费与用户预期；展示层不得触发模型调用 |
| 沿用 ADR-8 D17 的“只有显式索引才跑 Semgrep，写路径不跑” | 模型/用户在一次编辑后拿不到危险模式诊断；与“Semgrep 和 LSP 一样是即时诊断”矛盾 |
| 直接 `get_diagnostics(path=...)` 并当成即时结果 | 旧 Revision 的诊断会混进当前文件；当前 schema 也没有 Revision 字段。即时结果必须来自对刚写入字节的扫描 |
| 把 Semgrep 只当展示、不放进工具结果/延迟 note | 模型看不到危险模式，无法在同一个 turn 修正；与 LSP 的即时/延迟投递不一致 |
| 只有 LSP error 才允许出现 Semgrep 行 | LSP 干净但 Semgrep 命中危险模式时会被吞掉；Semgrep 是独立来源 |
| 把 Semgrep 放在 LSP 预算之外、每次都同步等待 | 一次编辑会被 Semgrep 拖住；必须复用同一 `WRITE_BUDGET`/`deferred` 机制，超时走延迟 |

## 决策

### D1. 移植为“编辑/写入卡的附件 + 独立回退”，不是新的 transcript 行

- **LSP 部分**只描述**已经投递给模型**的 deferred verdict；**P3 的 Semgrep 部分**是同一诊断链路的
  即时源，见 D10。两者都不改 system prompt、不启动 turn。**LSP 不新增模型消息、不改任何模型可见的
  `content`**；Semgrep 会向已有工具结果/延迟 note 增加诊断行（D10），但不新增消息类型。展示元数据
  只写 `display_metadata`（turn-end 已有的 user note 行还加 `display_kind`，见下一条）。
- native 渲染复用 `routeLateDiagnostics`：按路径命中最近的编辑/写入 tool frame，调用
  `ToolExecutionComponent.attachLateDiagnostics`。没有命中（卡已滚出、被压缩、ANSI 路径、resume
  缺卡）才创建 `LateDiagnosticsMessageComponent` 独立块。
- **V4A patch 例外（独立块，与 omp 一致）**：`patch(mode="patch")` 的目标路径在 V4A 文本里
  （`tools/file_tools.py:959-961, 971-977`），args 不承载这个多文件列表；`ToolExecutionComponent.
  matchesPath` 只读 `args.path`/`file_path`/`edits[].path`/`files[].path`
  （`tool-execution.ts:977-996`）。omp 的 `apply_patch`/`hashline` schema 也只有 `{input}`
  （`packages/coding-agent/src/edit/schemas.ts:33-40`），其 `#targetPaths` 同样匹配不到，所以 omp 对
  这类编辑也落独立块。**本 ADR 保持同一行为：V4A 永不 attach，不新增 seam；卡内挂载只承诺
  `write_file` 与 `patch(replace)`。** 不要为此在壳里解析 V4A 或伪造 `files` 参数。
- **turn-end 行（P2）**：`_late_lsp_verdicts` 已经追加一条 `role:"user"` 的 durable note
  （`agent/turn_stop_gates.py:282`）。对 omp-ui，这条已有行在 append 时带
  `display_kind="late_diagnostics"` 与 `display_metadata.late_diagnostics.files`（由 local-viewer
  presenter 返回；没有 presenter 时保持当前普通 user 行），壳把它画成结构化 late block，而不是用户
  气泡。**不新增第二条行**；模型仍读同一 `content`。
- **carrier tool 行（P2）**：下一个工具结果承载 note 时，仍把文本追加在该 tool result 的 `content` 尾部
  （模型面逐字不变）。对 omp-ui，同一条 tool 消息的 `display_metadata` 记
  `late_diagnostics_carrier = {files, suffix}`；`session_history` 的展示投影只把 `suffix` 从
  `content` 里剥掉，resume 不会在卡片正文里重复 note。origin 编辑卡另由 `late_diagnostics.files`
  生成结构化块。
- 不在网关或客户端重新计算“哪个卡属于这次编辑”；路径匹配与 `findLast` 语义由 pi-tui 决定。
  V4A patch 是已知例外，见上一条，不在 P1/P2 内补匹配逻辑。
- 内联 `lsp_diagnostics`/Semgrep 结果留在工具结果里，不重复发 late 事件；两者的 late 结果都只由
  `deferred` 的投递点产生。模型主动调用诊断工具得到的 Semgrep result 是即时工具结果，不走 late。

理由：Hermes 已经把延迟 verdict 以“附着在下一个工具结果”的方式交给模型；展示面最自然的对应物是
同一张编辑卡的诊断区，而不是复制一条 omp custom message。turn-end 的 note 行是既有事实，展示层只能
重标它，不能忽略它再画一块。这样既不重复模型上下文，也不新增行类型。

### D2. 结构化 verdict 在 `deferred` 源头生成；模型文本逐字不变

`_Note` 增加 `files: tuple[LateDiagnosticFile, ...]`（正常只有一项，与 path 对应）。
`deferred.post` 的签名扩展为 `post(owner, path, text, baseline_text, files)`；`take`/`restore`/
`pop_notes` 原样携带。`forward_to_agent` 与 `settle_for_agent` 的返回值增加 `files`，或返回一个
`DeliveredNote{path, text, files}`；具体形状是实现点，但**LSP 部分的 `text` 必须保持当前字节**；
P3 的 Semgrep 行是 D10 额外合并进 `files`/note 的内容，不改变已有 LSP 行。

`files` 的字段与 omp 的 `LateDiagnosticsFile` 对齐：

```text
{path: string, summary: string, errored: bool, messages: string[]}
```

- `path`：`deferred.post` 收到的绝对路径（`os.path.abspath` 已在上游完成）。
- `messages`（LSP 部分）：与模型块同一批诊断、同一 `DEFAULT_SEVERITIES`（当前只有 ERROR）、同一
  `MAX_PER_FILE`/`MAX_TOTAL_CHARS` 上限；但必须是 **pi-tui 可解析的展示行**，不能用模型块的
  `ERROR [12:5] …` 格式，也不能直接复用 `reporter.format_diagnostic`。`formatDiagnostics` 的
  `parseDiagnosticMessage` 只接受
  `path:line:col [severity] ([source] )?message( (code))?`（`render-utils.ts:510-524`）：
  `severity` 小写，`source` 在 message 前，`code` 在最后。展示行因此形如
  `C:\repo\src\app.ts:12:5 [error] [typescript] Type 'string' is not assignable … (2322)`。
  路径、message、source、code 与模型侧用同一套纯文本 sanitizer（控制字符、CR/LF、长度上限；
  **不做 HTML 转义**），绝不复用 `<diagnostics>` 包裹文本。无法解析的行会落入 `unparsed`，
  丢失按文件分组/排序，验收必须覆盖这一点。
- **解析/排序口径固定**：每条展示行都必须匹配 `parseDiagnosticMessage`；正常 payload 不允许出现
  `unparsed` 行（出现即展示缺陷，不是可接受的降级）。`formatDiagnostics` 先按行内 `filePath` 分组，
  组内按 severity rank（error < warning < info < hint）→ line → col → message 排序；壳不得二次排序、
  过滤或改写。`path` 可以带 Windows 盘符冒号（`C:\...`），懒匹配已验证可用；验收加 Windows 路径
  用例。
- **行内 path 与 `files[].path` 逐字一致**：每个 `messages` 行解析出的 `filePath` 必须与该文件项的
  `files[].path` 完全相同（同一绝对路径、同一分隔符/大小写口径）。`formatDiagnostics` 按**行内
  path** 分组建树，`routeLateDiagnostics` 按 **`files[].path`** 挂卡；两者不一致会出现“挂到 A 卡、
  树里显示 B 路径”或同一文件被拆成两组。`summary`/`errored` 也按同一路径口径计算，不能一边用
  绝对路径、一边用 `displayPath` 后的相对路径。
- `summary`：对同一批过滤后的诊断计数，复刻 omp 的复数形式（`"2 error(s)"`、`"1 warning(s)"`；
  多个用 `", "` 连接）。P1 只会有 error；未来放开 severity 时无需改组件。
- `errored`：是否存在 severity 1。
- 如果过滤后没有诊断（`report_for_file` 返回空），现有代码不会 `post`，因此也不会产生事件。
  不要为“clean”造一个空块。

`reporter.py` 增加一个纯展示格式化入口（例如 `display_lines_for_file(diagnostics, …)`），
`report_for_file` 复用它来避免两套 line/truncate 逻辑；模型侧继续在返回的块上做 HTML 转义。
如果实现时发现复用会改变模型文本，宁可保留两个小函数，也不要让模型文本漂移。

### D3. wire 契约：`diagnostics.late` 只送组件需要的四个字段

```text
event diagnostics.late {
  files: [
    {path: string, summary: string, errored: bool, messages: string[]}
  ]
  removed_paths?: string[]
}
```

- 事件名用 `diagnostics.late`，不是 `lsp.*`：它是“晚到”的展示语义，不是 LSP 请求/响应。
- `removed_paths` 是 P3 的失效信号：这些 path 的 late 缓存/卡内附件/独立块必须移除（新的
  write/patch 使旧 Semgrep 行失效，且没有新的 LSP 文件可替换时）；不在其中的 path 不受影响。
- **不送** `text`（模型文本）、`baseline_text`、原始 diagnostic dict、`session_id`（envelope 已有）、
  `tool_call_id`（组件按路径匹配；P2 的持久化在网关内部用 call id）。V4A patch 的目标路径不在 args
  匹配字段里，按路径匹配不命中，见 D1/D5；本 ADR 不为它开 `tool_call_id` 事件字段。
- 同一事件内按 pop 顺序排列；同一路径只出现一次（`deferred._QUEUED` 本身按 path 去重）。
- P3 起 `messages` 可以含 `[semgrep]` 行（D10）；不新增 `source_tool`/`rule_id` 字段，source 与
  code 已编码在展示行里。没有 Semgrep 结果时形状与 P1 完全相同。
- 每个 `messages` 行已经截断；整个 payload 仍受 `MAX_PER_FILE` / `MAX_TOTAL_CHARS` 约束。
- 契约加在 `tui_gateway/contracts/events.py`，`Payload` 默认 `extra="forbid"`；`removed_paths`
  是可选字段，除它之外不接受额外键。必须同一 PR 内有真实 emitter，否则
  `registry.assert_complete` 的 orphan 门失败。不能用一个空契约占位。
- 重新运行 `scripts/gen_gateway_contracts.py` 与 `scripts/gen_panel_inventory.py`，让 omp-ui 的
  `gateway-contract.generated.ts` 与 `docs/omp-ui-panels.json` 同步。

**发送范围**：只在 `local_viewer_turn(turn)` 且 session policy 的 `client == "omp-ui"` 时发布
（ADR-6 D1）。CLI、TUI、Desktop、消息平台、ACP、one-shot、compute-host 都不发；它们既不消费这个
组件，也不该为它写展示元数据。客户端的未知事件行为保持“忽略”。

### D4. 只在 verdict 真正交给模型时发事件

- 事件由 `deferred` 的投递点触发，而不是 `post()`：
  - `forward_to_agent`：note 已从 `_QUEUED` pop，即将随下一个工具结果交给模型；
  - `settle_for_agent`：turn-end 等待结束，note 将作为 `role:"user"` 的 note 行交给模型；
  - 下一个用户 turn 的 `forward_to_agent`：note 将随用户消息注入。
- 每条被 pop 的 note 只发一次；`forward_to_agent`/`settle_for_agent` 之后 note 已从队列删除，不会重发。
  被 supersede、被 `result.diagnostics is None` 丢弃、或从未 pop 的 verdict 不发。
- turn-end 的 5s 超时只 pop 已经 `post()` 的 note；仍在运行的检查还没有 note，它留在
  `_PENDING`，之后完成时 `post()` 才写入 `_QUEUED`，等下一个 `forward_to_agent`（通常下一个用户
  turn）再交付。事件也在那个投递点才发。不要为了让 UI 早出现而提前发，也不要把“检查还在跑”说成
  “note 已在队列”。
- 事件发布沿用 `TurnRunner._publish_execution` 的 execution generation 校验；generation 不匹配时
  自然不投递。`local_viewer_events.wire` 安装 sink，`restore` 清除，cached agent 不残留。
- 发布顺序：sink 在 pop 之后、模型投递路径继续之前调用。native 命中时块挂到较早的编辑卡，顺序不依赖
  当前工具；未命中时独立块在事件到达处追加，**不为它伪造历史位置**。P3 若需要严格“carrier 之后”的
  顺序，再由 turn/事件层合并；P1 不引入调度延迟。
- **独立块位置/时序（P1）**：未被卡接收的文件在事件到达处追加到 transcript 末尾，不插回历史位置，
  不为“应该在编辑卡后面”而延迟；同一事件内多个未路由文件合成一个
  `LateDiagnosticsMessageComponent`（组件按 payload 的 `files` 顺序聚合）。native 命中时块出现在较早
  的编辑卡上，这是路由结果，不是重排。turn-end 与 carrier 的块（P2）留在各自已有行的位置。omp 的
  idle 注入可以起新 turn，Hermes 不实现，因此跨客户端的到达时刻不承诺一致；不要用延迟/缓冲伪造
  omp 的时序。rebuild 后的快照重放同样追加在当前末尾（或挂回卡），不回填历史。
- **Semgrep 不是例外**：D10 让 Semgrep 与 LSP 共用同一个 `WRITE_BUDGET`/`deferred` 队列和投递点；
  Semgrep-only 的 late verdict 同样在 pop 时发 `diagnostics.late`。模型主动调用诊断工具得到的
  Semgrep result 是**即时工具结果**，不走 late 队列（见 D10）。

理由：omp 的 late 消息是在注入时创建的；Hermes 的“注入”是模型投递点。以 pop 为界，UI 与模型看到的
是同一批、同一时刻仍有效的 verdict，不会显示被 supersede 或从未送达的诊断。

### D5. P2：重标 turn-end 行、回填 origin tool 行、剥离 carrier 的 note 文本

P1 只做实时展示；P2 让 resume 也能重建。**不新增行**，而是给三类已有位置加展示信息：

**（a）turn-end note 行**：这条 `role:"user"` 行已经存在（`turn_stop_gates.py:282`）。对 omp-ui，
在 append 时就带：

```text
display_kind = "late_diagnostics"
display_metadata.late_diagnostics = {files:[...]}
```

由 local-viewer presenter 在 `_late_lsp_verdicts` 返回后生成；没有 presenter（CLI、TUI、Desktop、
消息平台）时保持当前普通 user 行，展示行为零变化。若实现选择先 append 再 stamp，可用
`SessionDB.set_latest_matching_message_display_kind(session_id, role="user", content=<note>,
display_kind="late_diagnostics", display_metadata=...)`（`hermes_state_messages.py:499-516`），但它必须
在行 flush 之后、且只对 omp-ui 执行；直接 append 时带字段更稳，避免内容匹配和竞态。

- 该行的 `content` 不变，模型仍读到同一 note；`display_kind` 只影响展示。
- 客户端不得把它画成用户气泡；它走 late block 的渲染分支。
- 非 omp-ui 不上这个 `display_kind`，否则会改变其它客户端对一条真实 user row 的解释。

**（b）origin 编辑/写入 tool 行**：

- 网关在 `tool.start` 记录 session 级的 `path -> tool_call_id`（最新覆盖）：`write_file` 取
  `args.path`；`patch` replace 取 `args.path`；V4A 用 `tools/patch_parser.parse_v4a_patch` 拆出
  `Add File`/`Update File` 的路径。路径按 live session 的 cwd 解析为绝对路径，并与 `deferred.post`
  的 path 用同一规范化（`realpath`/大小写按平台）比较。映射要有界（例如最近 64 个路径），
  在 session 结束时释放。
- late 事件产生时，用 `path` 找到 `tool_call_id`；通过新增的
  `SessionDB.set_tool_message_display_metadata(session_id, tool_call_id, patch)` 合并写入该 tool 行：
  `display_metadata.late_diagnostics = {files:[...]}`。合并而不是覆盖 `tool_result_metadata`、
  `reactions`、`data_use` 等已有键。找不到活动行（rewind/压缩/子代理/one-shot）就跳过，
  只发实时事件。
- 同一路径后续 verdict 替换该键的 `files`；与 omp 的 `attachLateDiagnostics` “最新替换”一致。
- **V4A 例外**：path→`tool_call_id` 映射能把 V4A 的路径写到 patch 行的 `display_metadata`，但客户端
  重建卡片后仍调用 `attachLateDiagnostics`，它按 args 路径过滤；V4A 的权威目标在 patch 文本里，
  args 不承载该列表，因此 P2 的 resume 对 V4A 也只按独立块处理。D5 的映射在这里只解决“写哪一行”，
  不解决“组件接收”；这与 omp 的 `apply_patch`/`hashline` 行为一致，不新增 seam。

**（c）carrier tool 行**：note 文本是追加到某个后续 tool result 的 `content` 尾部交给模型的
（`plugin_live_notes.attach_to_tool_message`）。对 omp-ui，同一条 tool 消息的
`display_metadata` 记：

```text
late_diagnostics_carrier = {files:[...], suffix:"<the exact appended notes suffix>"}
```

`suffix` 是 `attach_to_tool_message` 实际追加的整段 notes（可能同时含 plugin go-live notice；它们也是
模型面提醒，不是工具输出，展示层一并剥离）。`session_history._history_to_messages` 的展示投影在该键
存在且 `content` 以 `suffix` 结尾时，把 `suffix` 从**展示用** `content` 中剥掉；模型消息、DB 里的
`content`、prompt 字节都不动。若追加后 turn budget 又重写了 content（`keep_after_turn_budget`），
suffix 可能不在尾部：此时不剥，宁可让 resume 卡片多显示一次文本，也不能猜一个位置去切模型内容。

注意写入顺序：`agent/tool_executor.py` 先跑 `forward_to_agent`/`attach_to_tool_message`，随后
`prepare_metadata` 才把 `tool_message["display_metadata"] = metadata`。carrier 元数据必须在
`prepare_metadata` 之后合并（或让 `prepare_metadata` 保留已有键），否则会被整体覆盖。

- carrier 的 `files` 在 resume 时可用于兜底：如果 origin 卡不在（被压缩/不在窗口），客户端按
  `files` 的路由规则生成独立块，位置在该 carrier 之后；若 origin 卡在，则挂到 origin 卡，carrier
  正文因 suffix 已剥而不重复。
- 持久化只写 `display_metadata`；不改 `content`、不改 `display_kind`（carrier 是 tool 行）、不新增
  `role`。因此 provider 看到的字节不变（`PERSISTENCE_ONLY_MESSAGE_FIELDS` 已剥离该列）。
- resume：`_history_to_messages` 已经为 tool 行带出 `display_metadata`
  （`session_history.py:341-345`）。ADR-2 D8 落地后，客户端重建 tool 卡时读取
  `display_metadata.late_diagnostics.files`，先尝试 `attachLateDiagnostics`；卡片不接受（ANSI/非编辑
  工具）就在该卡之后追加独立块。不要在 resume 时重新跑 LSP，也不要把这段文本再插进模型历史。

P2 的依赖是 ADR-2 D8 的 tool 行卡片重建；如果该依赖未落地，P1 仍可独立上线，resume 会缺块，plan
状态在 P2 前保持 `部分`（实时已接、快照未接），不伪装成完整。

### D6. 客户端：一个 shell 事件 + 现成组件，不新增 `#messages`

- `omp-ui/src/modes/rpc/gateway-session.ts` 的 `#notificationHandlers` 增加
  `"diagnostics.late"`：校验 `files` 数组的形状与长度，更新 shell 的 path→`files` 快照（同一 path
  替换；`removed_paths` 删除），再转成 shell 自有事件 `{type:"late_diagnostics"; files}`。不要把它
  塞进 `#messages`，也不要写 `sessionManager`。
- `message.start`/`message_end` 若携带 `display_kind="late_diagnostics"`（或 resume 的快照行），
  **不要走 user 气泡分支**；这些行由 `diagnostics.late` 事件或 replay 的 late 分支渲染。否则 live
  与 resume 会各出现一个用户气泡。
- `omp-ui/src/session/agent-session-events.ts` 的 `AgentSessionEvent` 增加这一支（shell-owned，
  不放 vendor）。`event-controller.ts` 的 switch 增加 case：
  1. `routeLateDiagnostics(this.ctx.chatContainer.children, files)`；
  2. 返回的未路由文件创建 `LateDiagnosticsMessageComponent`，`setExpanded(ctx.toolOutputExpanded)`，
     `chatContainer.addChild`；
  3. `requestRender()`。
- **同一 path 原位替换**：独立块不能只靠传送层 `seq` 去重；同一 path 的后续事件/快照重放必须替换
  原独立块（或卡内区），并保持**首次出现的位置**。壳维护 `path -> standalone component`（以及该块
  聚合的 `files`）；替换时用 `TranscriptContainer.insertChildAt(newComponent, oldIndex)` 再移除旧
  组件（或等价的原位 API），不要 `removeChild` 后 `addChild` 到末尾。若一个块聚合多个 path，替换
  其中一个就用更新后的 `files` 重建整块、在原 index 插入，并更新其余 path 的映射。卡内区由
  `attachLateDiagnostics` 的替换语义处理。
- **色调由组件决定**：`LateDiagnosticsMessageComponent` 与卡内诊断区按 `errored` 选 error，否则
  warning；P1 的 LSP 只有 ERROR（`DEFAULT_SEVERITIES`），因此 P1 块恒为 error 色调。P3 的 Semgrep
  允许 warning-only（warning 色调），`info`/`hint` 不进 payload；壳不得按 source 或行内 severity
  文本自行改色，也不要把 warning-only 当成 error。
- **会话内 rebuild（壳快照重放）**：`rebuildChatFromMessages()` 先 `chatContainer.clear()`，随后只重放
  `viewSession.messages`（`interactive-mode.ts:1425-1454, 1521`）。P1 的独立块和已挂到已结算工具卡的
  late 区都不在 ledger，也不在 `liveComponents` 的保留集合（那里只保 `streamingComponent` 与
  `pendingTools`）；事件已在 pop 时发过，不会重发。因此壳必须在 `diagnostics.late` 时按 path 累积
  完整快照（同一 path 替换；`removed_paths` 删除），并在 `rebuildChatFromMessages()`/
  `renderInitialMessages()` 之后重放 `routeLateDiagnostics`：能命中重建后的卡就挂卡，不能（ADR-2
  D8 前 settled 卡不重建）就补独立块；独立块按 path 复用/替换，避免与后续同 path 事件重复。
  **P2 的 SQLite `display_metadata` 只服务 resume 投影，live `message.complete` 不带它，不能当作
  会话内 rebuild 的恢复来源。** 同一 session 的重绘重放快照；真正切换 session 时清空快照，避免跨
  会话残留。omp 的 late 是 ledger 里的 custom message，rebuild 会重新 route；壳快照是 Hermes 的
  等价物。
- **resume 的 turn-end 行**：`rowToAgentMessage`/replay 遇到
  `display_kind="late_diagnostics"` 的 user 行时，不调用 `addMessageToChat` 画气泡；它从
  `display_metadata.late_diagnostics.files` 合成一次 `{type:"late_diagnostics", files}`，走同一
  路由（能挂卡就挂，否则独立块）。行的 `content` 仍可留给未来的“查看原文”，但默认不画。
  这条分支必须按 `display_kind` 精确命中，不能靠 `[System note:` 之类文本嗅探。
- **resume 的 carrier tool 行**：`_history_to_messages` 已按 `late_diagnostics_carrier.suffix` 剥掉
  正文尾部；客户端若看到 carrier 的 `display_metadata.late_diagnostics_carrier.files`，也只在
  origin 卡缺失时用它兜底生成独立块，不能在 carrier 卡内再画一遍。
- **removed_paths**：收到后按 path 删除累加器的 LSP/Semgrep 快照、移除对应独立块，并清空卡上的
  late 诊断区。`ToolExecutionComponent` 需要一个小而窄的 `clearLateDiagnostics(path?)` seam（或等价的
  “空快照”语义）；它只清空数据并 `invalidate()`，不改变渲染分支、折叠或色调。不要在壳里重造
  tool card，也不要用空 `messages` 的普通 `files` 项冒充删除（会留下空 section/计数）。
- 不做自己的路径匹配；`ToolExecutionComponent.attachLateDiagnostics` 已处理 `displayPath`、
  `args.path`/`file_path`/`edits`/`files` 与 native 门。
- 全局 tool-output 展开/隐藏沿用现有 `isExpandable`/`chatContainer.setToolActivityVisible` 机制；
  组件自身已实现 `setExpanded`/`setToolActivityVisible`。
- 不在客户端解析 `tool.complete.result.lsp_diagnostics`，不在客户端调用 LSP，不把事件映射成
  `notification.show` 或状态行。
- `diagnostics.late` 的 `files` 是每个 path 的**完整快照**（LSP + P3 的 Semgrep）；客户端按 path
  替换，不做增量拼接。P3 的 LSP/Semgrep 合并由网关的 session 级累加器完成，客户端不解析 source
  标签。
- 事件重放去重按 notification 的 `seq`/`replayEpoch`/`executionGeneration`（`GatewayNotification`
  已有这些字段），**不按 `path + messages` 做内容去重**：同一个错误在修复后再次出现是合法的，内容
  去重会吞掉第二个真实块。同一张卡上的重复 attach 由 `attachLateDiagnostics` 的替换语义处理。
- 同一 path 的**新 seq** 不是重复帧：它必须替换原独立块/卡内区，并按 D6 的 path→component 映射在
  原位置替换；不能因为 path 相同就丢弃，也不能因为 seq 不同就再 append 一个块。只有同一
  seq/replayEpoch/generation 的重放帧才直接丢弃。

### D7. 归属、去重、staleness 都以 `deferred` 为准

- `deferred` 已经保证：一个 `(owner, path)` 一条 note；新编辑吸收旧 note；被 supersede 的检查不
  post；两个 session 编辑同一文件时各自只收到自己的 verdict（`test_write_never_waits_on_lsp.py`）。
  **LSP 侧**不增加第二个 mutation version / digest；P3 的 Semgrep 是独立来源，另按扫描内容身份 +
  成功 write/patch 失效（D10），不把两套 digest 混为一个。
- 事件只发给 note 的 `owner` 对应的 session；owner 是 task id（子代理）时不升级到父会话。子代理的
  late diagnostics 不在 P1 的展示范围内；如需支持，另立变更。
- 同一路径的多张编辑卡：`routeLateDiagnostics` 用 `findLast` 命中最近一张；这与“新编辑吸收旧
  verdict”同向。不要改成第一张匹配。
- 一个 late 事件内的 `files` 按 path 唯一；如果上游将来把多个 session 的 note 合并，必须在
  `deferred` 层拆开，客户端不猜 owner。

### D8. 降级、失败与安全

- LSP 未启用、workspace 未信任、服务端不回答、预算内无 verdict、verdict 为 clean：都不发事件、不画
  占位。模型侧仍按现有策略在下一个工具结果/turn end/下一个用户 turn 收到或不收到。
- 客户端缺失/旧版：未知事件被忽略，模型行为不变。
- 非 omp-ui：不发事件、不写 `display_metadata`；TUI/CLI/消息平台的输出逐字不变。
- 诊断文本来自刚解析用户代码的 language server，按不可信处理：纯文本 sanitizer 去掉控制字符、
  折叠换行、限制 message/code/source/path 长度；事件有总行数/总字符上限；不送文件内容。
  客户端用 `plainText` 渲染，不解释 HTML/Markdown。
- 展示写库失败只记 debug，不影响 turn、工具结果或模型投递；失败时实时事件仍可显示。
- 路径匹配失败（符号链接、大小写、跨 cwd）只导致“不挂到卡”，退化为独立块；不能因此丢事件或
  改模型文本。实现时统一用 live session cwd 解析，并补 Windows/大小写测试。

### D9. 兼容性与 footprint

- 不新增模型工具、不改 `toolsets`、不改 system prompt、不新增配置项（P1/P2）。`lsp.enabled`、
  `lsp.wait_timeout`、severity 过滤继续是唯一开关。
- LSP 的 write 延迟不变：事件在 LSP loop 的 done-callback 之后才产生，写路径仍是
  `WRITE_BUDGET`/`INLINE_GRACE`。P3 的 Semgrep 在同一预算内并行启动，超时即转 deferred；不允许
  同步等待完整 Semgrep 扫描。
- prompt cache：LSP 路径的模型消息/工具结果内容/系统提示都不因本 ADR 改变。P3 的 Semgrep 在
  `code_intel.semgrep.enabled=true` 时会有意把危险模式行加入工具结果/延迟 note，这是新诊断内容，
  不是隐藏的 cache 改写；`display_metadata` 与 `diagnostics.late` 事件仍是展示面（metadata 在
  provider 前剥离）。
- omp 的“late 到达会启动新 turn”在 Hermes **不实现**：展示层不调用模型，也不把 verdict 变成
  admission。

### D10. Semgrep 危险模式诊断：与 LSP 并列的即时诊断源（P3）

- **同一触发/预算**：`write_file`/`patch` 的编辑检查在启动 LSP 的同时启动 Semgrep（要求
  `code_intel.semgrep.enabled=true` 且 `mode` 有效）。两者共用 `WRITE_BUDGET`/`INLINE_GRACE`：
  预算内返回就并入该次写入的工具结果（`WriteResult`/`PatchResult` 的 `lsp_diagnostics` 与新增
  `semgrep_diagnostics`；或统一 `diagnostics` 但保留 source 区分）；
  超出预算就进入同一个 `deferred` 队列，按同一投递点（下一个工具结果 / turn end / 下一个用户
  turn）交给模型和 UI。Semgrep-only 也可以内联/延迟，不要求 LSP 有 error。不要再把 Semgrep 限制成
  “只有显式 `hermes code index`/`refresh_file` 才运行”。
- **模型调用工具**：模型主动调用诊断/索引工具时，结果作为该 tool 的 **result** 即时返回并显示在
  工具卡，不进入 late 队列。`code_graph_update(refresh_file, semgrep=true)` 现场扫描当前文件；
  `code_intel(diagnostics, path=...)` 在 path 明确且 Semgrep enabled 时也现场扫描并即时返回，
  无 path/未启用时返回已存结果并标注 freshness（不能把旧图冒充当前文件的即时诊断）。
  `refresh_file` 必须把 `semgrep_scan` 的 findings（rule、severity、span、message）放进 result；
  当前 `update.py:95-99` 丢弃返回值是实现缺口，必须修。
- **映射**：每条 finding 变成一行 `path:line:col [severity] [semgrep] message (rule_id)`。
  severity 统一小写；source 固定 `semgrep`；code 用 `rule_id`。LSP 行与 Semgrep 行按 path 合并到
  同一个 `files` 项；`summary`/`errored` 按合并后的 severity 重算。模型文本也包含这些行，因为
  Semgrep 已和 LSP 一样是即时诊断，不再只靠 `code_intel` 查询。
- **工具结果字段**：`WriteResult`/`PatchResult` 增加 `semgrep_diagnostics`（结构化 findings +
  展示行）；`tool.complete.result` 原样携带，不把 Semgrep 混进 `lsp_diagnostics`（后者语义保持
  LSP-only）。late note 的 header 从 “LSP diagnostics introduced by …” 改成 source-neutral 的
  “Diagnostics introduced by …”，turn-end note 的 “the language server reported …” 也要改成同样的
  中性措辞；Semgrep 行自带 `[semgrep]`。这样内联与 late 的模型/展示形状一致。
- **severity 口径**：Semgrep 展示 `error` 与 `warning`（危险模式通常落在这两级），`info`/`hint`
  不显示；`errored` 只在含 `error` 时为 true，纯 warning 用组件 warning 色调。LSP 侧继续
  `DEFAULT_SEVERITIES`（当前只有 ERROR）。合并时 `summary` 分别计数、`errored` 取并集。
- **失效**：每次成功的 write/patch 都重新扫描当前字节；Semgrep 结果按内容 digest 替换，不保留旧
  行。Semgrep 关闭、未安装、扫描失败/超时且没有新结果时，必须从卡、origin metadata 和**尚未投递
  的** deferred note 中移除该 path 的旧 `[semgrep]` 行，不能把旧 finding 合并回来；纯 Semgrep 块用
  `removed_paths` 清空；即使新结果内联进 tool result，写完成回调也要为“旧 Semgrep 行已消失且没有
  新的 late files”的 path 发这个信号。LSP 的 `deferred` digest/`take/restore` 不变；Semgrep 用自己的
  content identity，不混成同一 digest。
- **持久化**：origin 编辑/写入 tool 行的 `display_metadata.late_diagnostics.files` 保存合并后的
  LSP+Semgrep 行；`late_diagnostics_carrier` 镜像实际追加到模型 note 的 files（**可以含
  Semgrep**），`suffix` 仍剥离整段 note；turn-end 行同理带 `display_kind` + metadata。失效时同步
  删除持久化的 `[semgrep]` 行。
- **降级/门控**：`code_intel.semgrep.enabled=false`、semgrep 未安装、`custom_dir` 为空、扫描
  `skipped`/`failed`：不显示 Semgrep 行，也不把失败当诊断；LSP 不受影响。`mode=auto` 可能联网，
  用户开启即接受；仍 `--metrics=off`、不执行仓库代码、不写文件。即时工具结果对所有客户端可见
  （它就是普通 tool result）；`diagnostics.late` 事件仍只对 local viewer 且 `client:"omp-ui"` 发送。
- **不再依赖 ADR-8 的显式索引/Revision 查询**：即时路径直接用 `semgrep_scan` 对刚写入字节的
  in-memory findings，不需要 `CodeDiagnostic` 的 Revision 字段，也不需要先跑索引。`code_intel`
  的旧图查询仍可能存在，但它不是本 ADR 的即时诊断来源；不要用旧查询结果冒充当前字节的诊断。

## 协议

**实时事件**：

```json
{
  "type": "diagnostics.late",
  "payload": {
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

**失效帧**（P3；同一个事件名）：

```json
{
  "type": "diagnostics.late",
  "payload": {
    "files": [],
    "removed_paths": ["C:\\repo\\src\\app.ts"]
  }
}
```

- 成功 write/patch 后，如果该 path 没有新的 LSP 文件可替换，就发这个帧；壳删除缓存、独立块与卡内
  附件。不要用空 `messages` 的普通 `files` 项代替，否则会留下空 section/计数。

**持久化键**（P2；全部只在 `display_metadata` 内，不新增顶层消息字段）：

```json
{
  "late_diagnostics": {
    "files": [
      {"path": "C:\\repo\\src\\app.ts", "summary": "3 error(s)", "errored": true,
       "messages": ["C:\\repo\\src\\app.ts:12:5 [error] [typescript] … (2322)",
                    "C:\\repo\\src\\app.ts:18:1 [error] [typescript] … (2304)",
                    "C:\\repo\\src\\app.ts:40:9 [error] [semgrep] … (typescript.lang.security.audit.eval)"]}
    ]
  }
}
```

- `late_diagnostics` 挂在 origin 的 `write_file`/`patch` tool 行；turn-end 的 user note 行用同一个
  键，并额外带 `display_kind="late_diagnostics"`。
- carrier tool 行额外记：

```json
{
  "late_diagnostics_carrier": {
    "files": [{"path": "C:\\repo\\src\\app.ts", "summary": "3 error(s)", "errored": true,
               "messages": ["C:\\repo\\src\\app.ts:12:5 [error] [typescript] … (2322)",
                            "C:\\repo\\src\\app.ts:18:1 [error] [typescript] … (2304)",
                            "C:\\repo\\src\\app.ts:40:9 [error] [semgrep] … (typescript.lang.security.audit.eval)"]}],
    "suffix": "<the exact notes suffix appended to this tool result's content>"
  }
}
```

- `suffix` 只用于展示投影的精确尾部剥离；不匹配尾部就不剥，绝不按 substring 或正则删除。它与
  `attach_to_tool_message` 实际追加的整段 notes 相同（可能含 plugin notice）。
- 合并写入；同一路径重复时替换 `files` 中该 path 的项。
- `messages` 可以混合来源：`[typescript]`/`[pyright]` 等是 LSP source，`[semgrep]` 是危险模式
  source；`summary`/`errored` 按合并后的 severity 重算。`late_diagnostics_carrier` 镜像实际追加到
  模型 note 的 files，因此**可以含 Semgrep 行**；`suffix` 仍按整段 notes 精确剥离，不会把
  Semgrep 行留在卡片正文里。
- 单条 `messages` 行、单文件条数、总字符都受与模型块相同的上限；超出用现有 elision 文案。

## 实现落点

| 层 | 文件 | 变更 |
|---|---|---|
| LSP 队列 | `agent/lsp/deferred.py` | `_Note` 增加 `files`；`post`/`take`/`restore`/`pop_notes` 透传；投递点可拿到 `files` |
| LSP 格式化 | `agent/lsp/reporter.py` | 纯文本、pi-tui 可解析的展示行（`path:line:col [severity] [source] message (code)`）+ 摘要/errored 的单一格式化入口；模型块继续现有 `ERROR [12:5] …` 与 HTML 转义 |
| 延迟投递 | `tools/file_operations_lint.py` | `_queue_late_verdict` 同时构造 `files`；`deferred.post` 传入；模型 `text` 逐字不变 |
| 投递消费 | `agent/tool_executor.py`、`agent/turn_stop_gates.py`、`agent/turn_context.py` | `forward_to_agent`/`settle_for_agent` 的返回值或回调携带 `files`；不改变 append/内容；turn-end presenter 在 append 时给 row 加 `display_kind`/`display_metadata` |
| 本地 viewer | `gateway/local_viewer_events.py` | `wire` 安装 late sink，`restore` 清除；只对 `client:"omp-ui"` 发布；P2 的 path→tool_call_id 注册 |
| 事件发布 | `gateway/run_turn_progress.py` | 复用 `_publish_execution`；P2 用 `complete_fields`/session live 记录目标 |
| Semgrep 即时源（P3） | `hermes_codegraph/analysis.py`、`tools/file_operations_lint.py`、`agent/lsp/edit_check.py` | 在 write/patch 检查里与 LSP 同时启动 `semgrep_scan`；预算内进工具结果，超时进同一 `deferred` 队列；映射 `[semgrep]` 行 |
| Semgrep 工具结果（P3） | `hermes_codegraph/update.py`、`tools/code_graph_update_tool.py`、`tools/code_intel_tool.py` | `refresh_file(semgrep=true)` 返回 findings；`code_intel(diagnostics, path=...)` 现场扫描并返回；结果即时进 tool result |
| Semgrep 内联字段（P3） | `tools/file_operations.py`、`tools/file_operations_lint.py` | `WriteResult`/`PatchResult` 增加 `semgrep_diagnostics`；`lsp_diagnostics` 保持 LSP-only；note header 改为 source-neutral |
| Semgrep 合并（P3） | `agent/lsp/reporter.py` / `deferred` 的 files 构造 | 按 path 合并 LSP/Semgrep 行，重算 summary/errored；模型 note 与 origin metadata 都含合并结果 |
| Semgrep 失效（P3） | `tools/file_operations_lint.py`、`gateway/local_viewer_events.py` | 新写入重新扫描；关闭/失败/超时删除旧 `[semgrep]` 行与持久化；按需发 `removed_paths` |
| 清空 seam（P3） | `omp-ui/node_modules/@oh-my-pi/pi-tui/src/chat/tool-execution.ts` | `clearLateDiagnostics` 只清 `#lateDiagnostics` 并 `invalidate()`；不改渲染语义 |
| 契约 | `tui_gateway/contracts/events.py` | `DiagnosticsLatePayload` + `LateDiagnosticsFile`，可选 `removed_paths: list[str]`；同 PR 有 emitter 并重生成 TS |
| 持久化（P2） | `hermes_state_messages.py` | `set_tool_message_display_metadata`（按 session + tool_call_id 合并）；`set_latest_matching_message_display_kind` 作为 turn-end 行的后备 |
| 投影（P2） | `tui_gateway/session_history.py` | tool 行带 `display_metadata`（已有）；对 `late_diagnostics_carrier.suffix` 做精确尾部剥离；turn-end 的 `display_kind` 行原样投影 |
| 壳 | `omp-ui/src/modes/rpc/gateway-session.ts` | 新增 `diagnostics.late` handler → shell 事件 |
| 壳 | `omp-ui/src/session/agent-session-events.ts` | 新增 `late_diagnostics` 事件支 |
| 壳 | `omp-ui/src/modes/controllers/event-controller.ts` | `routeLateDiagnostics` + `LateDiagnosticsMessageComponent` + 展开/可见性 |
| 壳（P1 rebuild） | `omp-ui/src/modes/interactive-mode.ts`、`omp-ui/src/modes/utils/ui-helpers.ts` | 按 path 维护 `files` 快照；`rebuildChatFromMessages`/`renderInitialMessages` 后重放 `routeLateDiagnostics`，独立块按 path 复用/替换 |
| 壳（P2） | `omp-ui/src/modes/rpc/gateway-session.ts`、replay 路径 | `rowToAgentMessage` 带 `display_metadata`；`display_kind="late_diagnostics"` 的 user 行走 late 分支；重建时 attach 或独立块 |
| 生成物 | `scripts/gen_gateway_contracts.py`、`scripts/gen_panel_inventory.py` | 重生成 TS 契约/面板清单 |

## 分阶段

- **P1（实时）**：D2 的结构化 `files`；D3 事件与契约；D4 sink/发布；D6 shell handler/组件/路由。
  验收：写一个 Python 文件让 mock LSP 延迟返回，omp-ui 的编辑卡出现诊断区；内联路径不重复；
  clean/timeout 不出现；其它客户端零变化。P1 的块不入 ledger，壳用 path→files 快照在
  rebuild/renderInitial 后重放（D6/验收 11）；ADR-2 D8 前 settled 卡不重建时落独立块。
- **P2（持久化/resume）**：D5 的 path→call id、origin/carrier/turn-end 三处 `display_metadata`、
  resume 重建。验收：关闭再 resume 后，同一编辑卡仍有 late 诊断（V4A patch 除外，见 D1）；turn-end
  的 note 不再画成用户气泡；carrier 卡片正文不重复 note；模型历史里没有新增重复诊断行；rewind 到
  编辑之前后不显示。
- **P3（Semgrep 即时源）**：D10 的 Semgrep 与 LSP 同预算/同延迟队列/同工具结果；write/patch 触发
  扫描，模型诊断工具返回即时 result；按 path 合并 `[semgrep]` 行，模型 note 与 origin metadata 都含
  合并结果。验收：LSP clean 但 Semgrep 命中时仍出现；超时走 late；新编辑不保留旧行；工具结果即时
  可见。P3 同时做同一 turn 的多条 verdict 合并/排序（如有多块噪声）。

## 验收

1. **实时挂载**：`write_file`/`patch(replace)` 的 late verdict 在 native 下进入对应编辑/写入卡；
   没有对应卡时生成一个 `LateDiagnosticsMessageComponent`；ANSI 下为独立块。V4A `patch(mode="patch")`
   的目标路径不在 args 匹配字段里，按 D1 的例外走独立块（与 omp 一致）。
2. **不重复**：内联 `lsp_diagnostics` 不触发事件；同一 verdict 不因 `forward_to_agent` 与
   `settle_for_agent` 重复发；被新编辑 supersede 的旧 verdict 不发；turn-end 已有的 user note 行
   只画成 late block，不额外出现用户气泡；carrier 正文不出现第二份 note。
3. **归属**：两个 session 编辑同一文件，A 的 UI 只出现 A 的错误，B 只出现 B 的；子代理不冒泡到父
   会话。
4. **模型不变**：`WriteResult.lsp_diagnostics`、tool result 内容、turn-end note 文本与当前逐字一致；
   现有 `tests/agent/lsp/test_write_never_waits_on_lsp.py` 全绿。
5. **载荷**：事件只含 `files` 的四个字段；无 `text`/baseline/原始 dict；行数/长度受上限；展示文本无
   HTML 实体；控制字符被剥离；每一行都能被 `parseDiagnosticMessage` 解析（不会落到 `unparsed`），
   severity 为小写、source 在 message 前、code 在末尾。
6. **范围**：`client != "omp-ui"` 的本地 viewer 与所有消息平台不收到事件、不写
   `display_metadata`；omp-ui 的未知事件行为不变。
7. **投递语义**：turn-end 5s 内到达的 verdict 在该 turn 内发；超时后留到下一个用户 turn 再发；
   一-shot/无 next turn 时无 UI 事件、模型面行为不变。
8. **P2 resume**：重新加载后 tool 行的 `display_metadata.late_diagnostics` 恢复同一块；turn-end 的
   `display_kind="late_diagnostics"` 行画成结构化块而不是用户气泡；carrier 行的正文已按
   `late_diagnostics_carrier.suffix` 剥掉 note，不重复；模型侧历史不新增诊断行；压缩/rewind 后不
   复活已移除的块。
9. **Semgrep 即时诊断**：`write_file`/`patch` 在预算内返回 `[semgrep]` 行并并入工具结果；超时则
   进入同一 `deferred` 队列并在 late block/note 出现。模型调用 `code_graph_update
   refresh_file(semgrep=true)`/`code_intel` 时，findings 直接在 tool result 中。LSP clean 时也能
   显示；每行可被 `parseDiagnosticMessage` 解析，source=`semgrep`、code=rule_id；模型能读到这些
   行，不依赖 `code_intel` 二次查询。
10. **Semgrep 失效**：先得到 `[semgrep]` 行，再对同一 path 成功写入/打补丁；新扫描结果替换旧行，
   不得把旧 finding 合并回来；origin metadata 与新 note 同步。Semgrep 关闭/失败/超时时旧行必须删除，
   `removed_paths` 清掉纯 Semgrep 块/卡内附件。没有“先跑索引”的前置条件。
11. **会话内 rebuild**：P1 收到 late 块后触发一次 `rebuildChatFromMessages()`（例如修改
   `display.showTokenUsage`），壳按 path 快照重放：ADR-2 D8 前 settled 卡不重建时生成独立块，D8
   落地后挂回重建的卡；同一 path 不出现第二块。不得依赖 SQLite `display_metadata` 的 live 投影来
   恢复。
12. **V4A 挂载**：`patch(mode="patch")` 的 late verdict 不进入 patch 卡（目标路径只在 V4A 文本里，
   `matchesPath` 看不到），实时与 resume 都按独立块处理；与 omp 的 `apply_patch`/`hashline` 一致，
   不新增 seam，也不得在壳里解析 V4A 文本来伪造匹配。
13. **解析/排序**：事件中的每一行都通过 `parseDiagnosticMessage`，不出现 `unparsed`；行内解析出的
   `filePath` 与 `files[].path` 逐字一致；组内顺序为 severity → line → col → message，与 pi-tui
   一致；Windows `C:\...` 路径用例通过。
14. **色调**：P1 全为 error 图标/色调；P3 的 warning-only 使用 warning 色调，`info`/`hint` 不出现；
   壳没有按 source 或文本自行改色。
15. **独立块位置**：未路由文件在事件到达处追加，同一事件合成一个块；native 命中挂到较早的编辑卡；
   同一 path 的新 seq 原位替换、保持首次出现位置，不追加第二块；rebuild 后的重放在当前末尾/卡上，
   不回填历史；不因等待 carrier 而延迟。

## 风险

- **ADR-6 D1 依赖**：`client:"omp-ui"` 未落地时无法门控；本 ADR 不另造 opt-in，P1 随 D1 一起落地。
- **路径归一化**：`deferred.post` 用绝对路径，工具 args 可能相对；Windows 大小写/分隔符、symlink、
  session cwd 与进程 cwd 不一致都会导致“挂不到卡”。兜底是独立块，但不能静默丢事件；实现要有
  跨平台测试。
- **会话内 rebuild 依赖壳快照**：`rebuildChatFromMessages` 只重放 `viewSession.messages`，P1 的
  live 块与卡内 late 区不入 ledger；必须由壳按 path 累积快照并在 rebuild/renderInitial 后重放
  （D6/验收 11）。SQLite `display_metadata` 只在 resume 投影时读取，不能修复会话内 rebuild；快照与
  P2 metadata 需按 path 去重，避免同一块出现两次。
- **V4A patch 保持独立块**：`patch(mode="patch")` 的目标路径在 V4A 文本里，args 的 `path`/`files`
  不是权威列表，`matchesPath` 覆盖不到（D1/D5/验收 12），实时与 resume 都按独立块处理；omp 的
  `apply_patch`/`hashline` 也只有 `{input}`，行为一致。不新增 seam，也不要在壳里解析 V4A 或改写
  args 假装匹配。
- **展示口径漂移**：行解析/排序、error/warning 色调、独立块位置分别由 pi-tui、`errored` 和事件到达
  顺序决定（D2/D4/D6）；壳若二次排序、按 source 改色、或延迟等待卡，会与 omp 分叉。行内 `filePath`
  与 `files[].path` 不一致会让树和挂卡指向不同文件；同 path 新事件若 remove+append 会让块跳到末尾。
  验收 13–15 固定这些口径，不把它们留给实现者自由发挥。
- **DB 写入频率**：每次 late verdict 一次 `display_metadata` 合并；数量受 `_MAX_PER_OWNER`（32）与
  `_MAX_OWNERS`（64）限制，仍应在 session 结束/rewind 时清理 path→call id 映射。
- **缓存/压缩**：`display_metadata` 不进 provider，但压缩/分支复制会携带它；确认压缩后的 tool 行
  仍投影同一键，且不会把 `late_diagnostics` 当成 human input。
- **组件行为**：native/ANSI 路径不同（`routeLateDiagnostics` 在 ANSI 全独立），这是上游行为，不要
  在壳里“修正”成统一挂载；否则与 pi-tui 的折叠/展开语义分叉。
- **重复事件**：事件流重连/回放可能重复同一载荷；壳层去重只做展示，不改变持久化。
- **turn-end 行的双语义**：`display_kind="late_diagnostics"` 的行仍是模型可见的真实 user row；
  只对 omp-ui 打标，其它客户端继续画气泡。压缩/分支复制必须保留该 `display_kind`，否则 resume
  会退化成用户气泡；同时 `context_compressor` 已把非 steer 的 `display_kind` 行排除在 human input
  之外，新增 kind 不能绕过这条判断。
- **carrier 后缀剥离**：只在 `content` 精确以 `suffix` 结尾时剥；budget spill/重写后不剥。剥错了会
  截断真实工具输出，因此宁可少剥、让 resume 多显示一次文本，也不能做模糊匹配。
- **Semgrep 实现缺口**：当前 `update.refresh_file` 丢弃 `_run_semgrep` 的返回值，write/patch 也没有
  启动 `semgrep_scan`；P3 必须把“扫描 → findings → 工具结果/延迟队列”接通，并让
  `refresh_file(semgrep=true)`/`code_intel` 把 findings 放进 result。这不是查询旧图的替代方案。
- **Semgrep 延迟/成本**：`mode=auto` 可能联网拉规则，冷启动扫描可能远超 write budget。必须复用
  `WRITE_BUDGET`/`deferred`，设置单文件超时；超时就发 late，不能阻塞写入。用户开启
  `code_intel.semgrep.enabled` 即接受这次扫描成本。
- **Semgrep 失效**：每次成功 write/patch 都重新扫描当前字节；旧 websocket/进程结果晚到且
  content identity 不匹配时丢弃。Semgrep 关闭/失败/超时不得保留旧 `[semgrep]` 行，否则会把过期
  危险模式留在卡上。缓存/去重键必须包含内容身份，不能只用 path。
- **Semgrep 与 LSP 合并**：同一 path 的 LSP/Semgrep 行必须合并成一份完整 `files`；任何一方更新都
  重算 summary/errored，不能用第二次事件把另一方覆盖掉。相同 finding 在后续编辑后合法复现，不能
  按 `messages` 内容去重；只按 notification seq/内容身份去重。
- **ADR-8 文本与工具面不一致**：ADR-8 D17 说 `code_graph_update` 固定 `semgrep=false`，当前工具
  仍暴露并透传 `semgrep`。本 ADR 以工具面的显式参数为准；ADR-8 文本应在独立变更里更正。若反过来
  移除该参数，模型仍可通过 write/patch 的即时 Semgrep 获得诊断，不依赖它。

## 不采纳

- **新 transcript 行 / custom message**：模型会重复读同一诊断，且没有“只展示不进模型”的行类型。
- **从 tool result 文本反解析**：HTML 转义、header、truncate 都使解析不可靠；会丢 severity 与结构。
- **客户端重新查询 LSP**：客户端没有写入权威与服务端 snapshot，结果会与模型不同步。
- **把内联诊断也做成 late 块**：重复展示同一事实。
- **用通知/状态行代替组件**：没有折叠、路径匹配、卡内挂载。
- **让 late diagnostics 触发新 turn**：改变 Hermes 的 turn/计费语义；展示层不调用模型。
- **对所有本地 viewer 广播**：没有消费点，且扩大持久化面；用 ADR-6 的 client 门。
- **为 late diagnostics 新增模型工具或配置**：没有消费者；LSP 现有开关足够。
- **沿用 ADR-8 D17 的“仅显式索引、写路径不跑”**：这会让 Semgrep 在编辑后不可见，与“Semgrep 和
  LSP 一样是即时诊断”矛盾；本 ADR 明确不采用该限制。
- **按 path 直接展示所有 `CodeDiagnostic`**：旧 Revision 的 finding 会混入当前文件；即时诊断必须
  来自刚写入字节的 `semgrep_scan`，查询旧图只能作为单独的查询面。
- **把 Semgrep 只当展示、不放进工具结果/延迟 note**：模型看不到危险模式，无法在同一 turn 修正；
  与 LSP 的即时/延迟投递不一致。
- **为 Semgrep 新增独立事件/独立块**：组件按 path 合并即可，独立事件会与 LSP 块重复、破坏折叠。

## 遗留

- P2 的 `path -> tool_call_id` 在跨进程 resume 后为空：这是预期的，只有本进程内仍在运行的检查才会
  产生 late verdict；历史会话的块由 `display_metadata` 恢复，不依赖映射。
- 如果未来 omp-ui 需要 warnings/info（当前 `DEFAULT_SEVERITIES` 只有 ERROR），另立 ADR 讨论
  severity 展示与模型通道的关系；本 ADR 的 `summary`/`errored` 已为此留好形状。
- 如果未来需要把 late diagnostics 作为 carrier 工具结果之后的严格顺序块，P3 再引入批量/排序；本 ADR
  不承诺跨客户端一致的时间线。
- Semgrep 的 `info`/`hint` 与 LSP warning 的展示口径不在本 ADR 内；当前只显示 Semgrep 的
  `error`/`warning` 与 LSP 的 ERROR。要统一 severity 策略需要单独的配置/展示决策。
- `code_intel`/`hermes code diagnostics` 对已存 `CodeDiagnostic` 的查询仍可能返回旧 Revision 的
  行；它只是查询面，不是写入后即时诊断的来源。若未来要让查询面也证明新鲜度，再给 schema 加
  Revision/内容身份；本 ADR 的即时路径不依赖它。
