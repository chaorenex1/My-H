# ADR-4：Eval 执行的忠实移植（`$` cell + `execute_code` 的 JS 后端）

**状态**：已决定（2026-11-01）。实现未开始。分阶段：**P1** 内核分块流 + `eval.exec`/`eval.cancel` + 行落库；
**P2** 客户端 `$` 入口 + `EvalExecutionComponent` 实时块 + replay 投影；**P3** `$$` 的 display-only 行 + 文档
状态；**P4** `execute_code` 的 `language` 参数（Python 行为不变）；**P5** JS 后端（Bun kernel + 变换器 +
工具桥 + 打包）。落地前 `docs/omp-ui-panels-gateway-plan.md` §2.3.2.1 的 Eval 行保持 `部分`，客户端不画假块、不改渲染分支（§0.1）。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§0.3、§2.3.2、§2.3.2.1、§4.7、§9、§10；
ADR-2（行/事件惯例、`display_kind`/`display_metadata` 投影）；ADR-3（`append_display_row`、pending flush 的同一 primitive）；
PM 的 `bun` 包（`pm/packages.py::Bun`、`pm/lock.json`；`hermes pm install bun`）。

**范围**：只讨论 omp-ui ↔ Hermes（canonical local gateway）。消息平台、Desktop、Ink sidecar、ACP 不设计；
用户行是普通 `role="user"` 行，未识别它的客户端只会显示模型文本（诚实降级）；`eval` 工具卡在其它客户端退回
通用工具卡，不是本 ADR 的目标。

**取代关系（逐条，避免文档互斥）**：

- plan §2.3.2.1 第 452 行把 Eval 判为“网关没有共享 kernel”：**由 D3 取代**——网关本就有 session kernel
  （`execute_code`），缺的是用户面入口；D3 让 `eval.exec` 与 `execute_code` 落到同一个 key tuple。该行状态在实现
  完成前仍写 `部分`（§0.1），客户端不画假块。
- plan §0.3 第 96 行把 `eval/` 列入“随本机业务代码删除”：**只对 producer 重新实现**，不恢复任何本机 eval
  业务代码（`omp-ui/src/eval/**`、`EvalRunner`、`handlePythonCommand` 都不回来）；那条删除不变。
- plan §2.3.2 第 410 行的 `eval-execution.ts` 渲染器描述不变：客户端仍复用 `EvalExecutionComponent`，本 ADR 只加
  数据载体（事件 + 行）与两个投影点。
- D1 **保持**：不新增 `eval` 模型工具。JS 在 omp 只从 eval 工具面可达，但 Hermes 已有同一个“持久 kernel +
  工具桥”的模型面入口 `execute_code`；D8 给它加 `language` 参数承载 JS，而不是在每次 API 请求里多带一个
  重叠的核心工具（footprint ladder：扩展既有代码优先）。`$` 仍走 canonical 方法、不走 slash、不走 admission。
- “不采纳”表的 JS 行**仍成立、范围由 D9 收窄**：JS 不进用户面（行型是 Python-only），只作为
  `execute_code(language:"js")` 的模型面第二后端。
- ADR-2 的 D9 “无 producer”清单不含 Eval，无需取代；ADR-2 的 `display_kind`/`display_metadata` 投影惯例继续适用。
- ADR-3 的 `append_display_row`/pending flush 是同一 primitive：ADR-3 先落地则本 ADR 复用，否则本 ADR 先实现。

## 问题

omp 的 Eval 执行有两个面，本 ADR **两个都移植**：用户面（§2.3.2 第 410/452 行的 “用户 eval，共享 agent 的
kernel session”）与模型面的 `eval` 工具（JS 只存在于后者）：

1. **用户面**：`$code`（Python）与 `$$code`（不进模型上下文）在 TUI 里直接跑一个 cell。UI 创建一个
   `EvalExecutionComponent`（`>>>` 头、流式 stdout、`(exit N)`/`(cancelled)` 尾），完成后把一条
   `pythonExecution` message 追加进 transcript。上游源码在本仓库历史里（`git show 5bd7dabd213^:<path>`）：
   `omp-ui/src/session/eval-runner.ts` 的 `EvalRunner.executePython` 用 `onChunk` 流式回调 →
   `recordPythonResult` 组装 `{role:"pythonExecution", code, output, exitCode, cancelled, truncated, meta,
   excludeFromContext}`，`isStreaming()` 时排入 `#pendingMessages`、否则 `appendSessionMessage`；
   `omp-ui/src/modes/controllers/command-controller.ts:2002-2043` 的 `handlePythonCommand` 先建组件（流式中挂
   `pendingMessagesContainer`）、边跑边 `appendOutput`，结束时
   `setComplete(exitCode, cancelled, {output, truncation, artifactError})`；
   `omp-ui/src/session/messages.ts:981-995` 的 `pythonExecutionToText` 生成模型文本
   （`Ran Python:` + `Output:`/`(no output)` + 失败/取消行 + `formatOutputNotice`），`excludeFromContext`
   时整条丢弃（`es()` 的 `pythonExecution` 分支）。输入语法在
   `omp-ui/src/modes/controllers/input-controller.ts:217-248`（见 D7a）。
2. **Kernel 共享**：同一个 cell 与 agent 的 `eval` 工具共享解释器状态。证据：pi-tui 的
   `PythonExecutionMessage` 注释 *“Shares the same kernel session as eval's Python backend”*
   （`omp-ui/node_modules/@oh-my-pi/pi-tui/src/chat/messages.ts:209-226`），以及
   `git show 5bd7dabd213^:omp-ui/src/session/eval-runner.ts` 里 `executePython` 传的
   `sessionId: namespacePythonSessionId(this.getSessionId())` 与 `kernelOwnerId` 正是 eval 工具同一对
   `getSessionId()/getKernelOwnerId()`（注释 *“shared by the eval tool and user Python shortcuts”*）。

3. **模型面（`eval` 工具）**：模型调用 `eval {language, code, …}`，Python 与 JavaScript 都在持久 kernel 里，
   工具卡由 pi-tui 的 `evalToolRenderer` 渲染。历史源码（`git show 5bd7dabd213^:<path>`）：
   - schema：`{language: "py"|"js", code, title?, timeout?, reset?}`（`omp-ui/src/tools/eval.ts`；
     `language`/`code` 必填，`timeout` 是秒、`0` 关闭截止，`reset` 只清该 kernel）；
   - 两个后端返回同一形状 `{output, exitCode, cancelled, truncated, totalLines, …, displayOutputs}`
     （`eval/py/executor.ts`、`eval/js/executor.ts`），`onChunk` 流式、`onStatus` 状态事件；
   - JS 后端是**常驻 Bun 子进程** + `WorkerCore`（`eval/js/worker-core.ts`、`process-entry.ts`），
     **刻意不用 `node:vm`**：`eval/js/shared/indirect-eval.ts` 记录 Bun 在 `Worker.terminate()` 打断
     `vm.runInContext` 时会 SIGTRAP，因此用户代码用间接 `eval` 在 worker 全局作用域执行；
     `shared/rewrite-imports.ts`（Babel）把顶层声明“降级/发布”以跨 cell 存活，并把静态 import 改成动态 import；
     `tool-bridge.ts` 把工具调用转给宿主；
   - kernel 身份按 `sessionId` + `kernelOwnerId` 保留，Python/JS 各一套（`disposeKernelSessionsByOwner` /
     `disposeVmContextsByOwner`）。

Hermes 的承载面是 `execute_code`（D8）：它已经是“持久 kernel + 工具桥”的模型入口，也是 D3 里 `$` 的 Python
kernel 持有者；加 `language` 即可覆盖 JS，omp 的 eval 卡（cells/语言徽标/details）不移植。

omp 的渲染契约（已随 pi-tui 装进客户端，本 ADR 不改）：

- `EvalExecutionComponent`：`chat/eval-execution.ts:68` 构造 `(code, ui, excludeFromContext, language="python")`，
  `appendOutput`（`:164-169`）、`setComplete(exitCode, cancelled, {output, truncation, artifactError})`
  （`:171-190`）、`isTranscriptBlockFinalized/Version`（`:100-107`）；默认折叠 20 行
  （`chat/execution-shared.ts:26`）。
- Replay：`chat/chat-transcript-builder.ts:365-373` 把 `pythonExecution` 还原成同一个组件。
- 选择器：`tree-selector.ts:747-753`、`copy-selector.ts:86-87,198,1114-1150` 已把该 role 标成
  `omp.tool.eval`，`omp-ui` 的 `/copy` 直接复用。
- 输出预算：行 3000 / 50KB、单行 512B、artifact 16MiB（`tools/streaming-output.ts:17-27`）；
  截断提示来自 `TruncationMeta`（`tools/output-meta.ts:222-252`，支持 `middle`）。

Hermes 现状（逐项，全部核对过）：

- **execute_code 是 Python cell 引擎**：`tools/code_kernel.py` 维护每会话一个 `SessionKernel`
  （key = `(_resolve_owner(task_id), mode, child_python, child_cwd, sorted(sandbox_tools))`，`code_kernel.py:811`），
  子进程用 `exec` 在持久 `GLOBALS` 命名空间跑 cell（`RUNNER_CELL_SOURCE`，`:47-74`），有工具 RPC、
  超时/中断杀 kernel、`reset`、LRU/空闲回收（`:359-372,624-656,717-731`）。结果是一个 JSON 字符串
  （`status/output/exit_code/stdout_truncated/...`，`code_execution_tool.py:56-81,738-805`）。
- **没有流式通道**：`response_q` 只在 cell 结束时拿到整帧（`_stdout_reader`，`code_kernel.py:461-515`），
  `kernel.raw`/`kernel.stderr` 也只在 settle 时 drain。没有任何 `eval.*`/`python.*` 方法，客户端没有 `$` 入口
  （`omp-ui/src` 全树 0 命中；`input-controller.ts:1-10` 明确“没有本机 bash/python”）。
- **没有用户 eval 的行型**：`append_message` 已支持自由 `display_kind`/`display_metadata`
  （`hermes_state_messages.py:396-429`），`_history_to_messages` 原样转发（`tui_gateway/session_history.py:364-368`），
  但客户端 `rowToAgentMessage` 只投影 user/assistant/tool（`gateway-session.ts:357-383`），
  `addMessageToChat` 只接 user/developer/assistant（`ui-helpers.ts:130-186`）。
- **唯一相近的先例**：`shell.exec`（`gateway/session_shell.py:26-105`，契约 `tui_gateway/contracts/tools_commands.py:155-196`）
  是 canonical、线程内执行、local-only、不进 transcript 的 request/response；它没有 kernel、没有流、没有行。
- **同 kernel 的 agent 面**：`execute_code` 在本地 viewer turn 内已自动批准
  （`tools/approval.py:1274` 命中 `_is_local_viewer_turn`，定义在 `:1213`），所以“用户 `$` 与模型 execute_code 共享状态”
  在 Hermes 里是可达的，前提是两边算出**同一个 kernel key**。
- **模型面缺口**：`execute_code` 没有语言参数，跑不了 JS；工具卡是通用卡（omp 的 eval 卡属于“工具执行”
  行，本 ADR 不改）。JS 运行时由 PM 提供（`pm/packages.py::Bun`，可选安装）。

因此本 ADR 决定：用户面把 `$` cell 做成 canonical `eval.exec`/`eval.cancel` + 分块事件 + 一条
`display_kind="eval_execution"` 的 user 行；模型面给 `execute_code` 增加 `language` 参数（D8）——它已经是
D3 里 `$` 的 Python kernel 持有者——Python 继续共享同一个 session kernel，JS 走新的 Bun kernel（D9）；
不新增工具、不改 `execute_code` 的既有字段与卡片。

## 决策

### D1. `$` 是 canonical 方法，不是工具、不是 slash 命令、不是 admission

- 新增 `eval.exec {session_id, execution_id, code, exclude_from_context?}` → `{execution_id}`，与
  `eval.cancel {session_id, execution_id?}` → `{cancelled: [execution_id]}`，都用 `canonical_method` 声明
  （authority-only；sidecar 不实现，避免覆盖门把 sidecar 当服务方）。
  `execution_id` 由客户端生成（uuid），使调用**重试幂等**：同 id 在跑 → 直接返回原 id（不跑第二份）；
  同 id 已结束 → `execution_id_reused`（不重跑用户代码；每会话保留最近 32 个已结束 id 的有界集合即可）。`eval.cancel` 省略 id 表示取消该会话全部在跑 cell，
  未知/已结束的 id 不进 `cancelled` 列表（幂等）。
- 处理模块 `gateway/session_eval.py`（新）暴露 `handlers(connection)`，在
  `gateway/session_controls.py::handlers` 里 `handlers.update(...)`（与 `session_shell.py` 同一注册方式）。
- **不新增 `/eval` slash 命令**：入口是客户端 `$` 前缀（与 omp 一致，见 D7）；`commands.catalog`/`complete.slash`
  不变。
- **不新增工具**：用户面入口是客户端前缀（D7）；模型面给 `execute_code` 加 `language`（D8）承担 JS，
  Python 复用 `$` 的 kernel，`execute_code` 的既有字段与卡片保持原契约（见“取代关系”）。
- **不走 admission**：eval 是旁路执行，不是 FIFO turn；若走 admission，模型会被结果行插进当前回合，
  且 `viewer_turn` 自动批准会改变 turn 语义。对照 `shell.exec` 的 docstring：
  *“Nothing enters the transcript or the admission FIFO.”*

### D2. 授权与安全：local viewer 的自己的输入

`eval.exec` 的闸门顺序（全部在 handler 内、spawn 之前）：

1. `authorize(connection, ref, 'session:control')`（执行是控制动作；`session_authority.authorize` 同时拒绝
   history mount，`gateway/session_authority.py:214-215`）。
2. 闭集参数校验由 dispatcher 先做（`canonical_param_problems`，`session_controls.py:340-350`）。
3. `authority._require_admission_open()`（draining 时 `runtime_draining`）。
4. `authorize_local_source(authority.runner, live.source) is True`；否则 `permission_denied`。这同时挡掉
   messaging/API/A2A/remote worker（没有本地 adapter）、subagent/cron/kanban（非本地 source）。
   与 `shell.exec` 一样，服务端只认本地 surface、不认具体客户端；其它本地客户端不实现入口就不会触发，
   服务端语义不变（本 ADR 只接线 omp-ui）。
5. 终端策略：`policy_for_source(...)` 必须存在，且 `TERMINAL_ENV` 属于本地集合（`''`/`local`）；
   docker/ssh 等后端返回 `unsupported_terminal_backend`（与 `shell.exec` 同一判定）。共享的 session kernel
   只在本地存在，拒绝比“悄悄跑一个不共享的 kernel”诚实。
6. 绑定作用域后复用 `execute_code` 的**硬闸**（不是批准闸）：
   - `tools.terminal_scope.enforce_no_refusal()`（策略未解析则拒绝）；
   - gateway 生命周期命令 / 宿主解释器 kill 的硬阻断（从 `execute_code` 抽成共享 helper，见 D3）；
   - kernel 可用性（`check_sandbox_requirements` 的同一判定）。
7. **不调用 `check_execute_code_guard`**：那是“模型自主执行要用户批准”的闸。`$` 是本地用户在键盘上输入的
   代码（`session:control` + local adapter 已是同一人），上游 omp 的用户面也不走工具批准（`approval="exec"`
   只挂在 eval 工具上）。对照 `hermes_cli/bang_shell.py:41-56` 的理由：只有人坐在键盘前的本地会话才允许这类
   输入；本方法把该条件收紧成 local viewer only。硬闸仍然保留，所以“杀掉 gateway 自己”依旧被拒。
   这也是**不能**把它接到 `execute_code()` 的原因：那个函数在批准闸处会阻塞在审批传输上，而回合外
   `viewer_turn` 未绑定（只在 `execute_admission` 内设置，`gateway/session_ingress.py:281-283`）、
   `_gateway_notify_cb` 也未注册，批准只能挂到超时——一个没有 turn 可回答的死锁。
8. **单飞**：同一 session 只允许一个 in-flight execution；另一个 `execution_id` 的 `eval.exec` 返回
   `eval_busy`，同 id 重试直接 join（见 D1），不排队、不落行（对齐 omp 的 `isEvalRunning` 提示）。

执行线程绑定（缺一即错挂/泄漏 kernel）：

- `owner_scope(authority, hydrate_secrets=True)` + `policy_scope(policy, authority=authority)`（`gateway/session_tui_side.py:55-80` 同款）；
- `set_session_vars(session_key=<turn 同款 key>, session_id=session_id, cwd=policy.cwd, profile=...)`；
  session key 用 `runner._session_key_for_source(live.source)`（`gateway/run.py:3626`），即回合绑定到
  `set_current_session_key` 的同一个值（`gateway/run_turn_runner.py:1009-1012`）；
- `set_terminal_scope(json.loads(policy.terminal_json))` + `set_session_cwd(policy.cwd)`（`gateway/session_commands.py:398-415` 同款）；
- 线程进 `authority._side_agents` 账本（restart/shutdown 的计数、drain 与 join 都按它算；结束
  `persist_work_count`）。`holder[0]` 放一个小对象，暴露 `hard_interrupt(message=None)` → 置位该 execution 的
  cancel event（`agent/interrupt_compat.request_hard_interrupt` 的既有 ABI，`gateway/session_runtime_workers.py:35-44`），
  等价于一次 `eval.cancel`；holder 另带 `abandoned()`，关闭中的会话不再发事件。

### D3. 共享 kernel：一个 key，一条派生路径

`eval.exec` 与 `execute_code` 必须是**同一个** `SessionKernel`，否则“共享 kernel session”是假的。

- 新增 `tools/code_execution_session.py::session_kernel_target(authority, ref, policy)`：
  - `mode = _get_execution_mode()`、`child_python = _resolve_child_python(mode)`、
    `child_cwd = _resolve_child_cwd(mode, "", task_id=session_id)`（与 `execute_code` 完全同源：
    `tools/code_execution_tool.py:796-797`，实现 `tools/code_execution_env.py:264-315`）；
  - `sandbox_tools = frozenset(_sandbox_tools_for(names))`；`names` 取该 session 已解析的工具面：
    有常驻 agent 用 `agent.valid_tool_names`（`agent/agent_init.py:1047`），否则用
    `policy.current_toolsets(config)` + `get_tool_definitions(enabled_toolsets=…, disabled_toolsets=…,
    quiet_mode=True)` 的名字（`model_tools.py:212-251`）
    （**不要**读 `model_tools._last_resolved_tool_names` 这个进程全局，它会跨会话串味）。
- `execute_code`、`eval.exec` 与 `eval` 工具的 Python 分支都通过
  `run_session_cell(code, *, task_id, target, timeout, reset=False, on_chunk=None, is_interrupted)`
  进入 `execute_in_session_kernel`：
  - 从 `execute_code(...)` 里拆出“审批前/审批后”两段：`_hard_guards(code, env_type, has_host_access)` 与
    `run_session_cell(...)`。工具入口 = 硬闸 + `check_execute_code_guard` + `run_session_cell`；
    eval 入口 = 硬闸 + `run_session_cell`。`execute_code` 的 schema、JSON 字段、key 顺序全部不变。
- `reset` 语义保持“只清这个 kernel”（`_acquire_kernel(reset=True)`，`code_kernel.py:624-656`）；`$` 不暴露 reset。
- timeout：固定取 `code_execution.timeout`（默认 300s），`$` 不带每 cell timeout（omp 用户面同样没有）。
- **工具权限随线程**：`CellAuthority` 在 `_run_cell` 里从调用线程捕获上下文（`code_kernel.py:197-250,838`），
  所以 eval 线程必须先绑好 D2 的 profile/approval/session 上下文；否则 cell 内
  `from hermes_tools import …` 的 RPC 会以 `No active execute_code cell` 失败（cell 的 Python 本身仍能跑）。

### D4. 分块流：只在请求显式要求时启用，execute_code 零变化

内核协议（`code_kernel.py:76-192,461-515,717-731`）新增可选的 chunk 帧：

1. 请求加 `{"stream": true}`。只有它为真时，runner 把 `contextlib.redirect_stdout/stderr` 换成
   tee 写入器（仍累积到原来的 1MB 缓冲，最终 reply 的 `stdout`/`stderr` 与今天逐字节一致），
   每个 chunk 立刻写一帧：`CHUNK_SENTINEL + len + "\n" + {"type":"chunk","stream":"stdout"|"stderr",
   "seq":N,"text":…}`。`seq` 在 runner 内单调递增（stdout/stderr 共用一个计数器）。
2. `_stdout_reader` 先认 chunk sentinel：命中 → `kernel.chunk_sink(text, stream, seq)`；否则走原 result
   sentinel。非帧字节仍进 `kernel.raw`（子进程 fd 输出），语义不变。
3. `_await_cell(kernel, timeout, is_interrupted, on_chunk=None)`：chunk 帧不返回，继续等 result/EOF；
   超时/中断判定不变。`kernel.chunk_sink` 按 cell 安装、settle 后清空。
4. gateway 侧的 sink：`redact_sensitive_text(strip_ansi(...))` → 50ms/8KB 节流（与 omp 的 50ms 一致，
   参考 `agent/tool_progress_relay.py:32-47` 的节流形状）→
   `live.event_stream.publish(session_id, payload, event_type="eval.output")`。
   为避免秘密被 chunk 边界切开，保留 256B 尾部 carry，只有超出 carry 的部分才下发；settle 的最终
   `output` 仍是权威值（再整体脱敏一次）。
5. 顺序：`eval.start` 先发，chunk 按 `seq` 单调，`eval.complete` 前 flush 最后一个 carry。
   chunk 帧与 result 帧共用一把 runner 内写锁，两个 sentinel 各自是独立随机 token、**不互为前缀**
   （否则 `_stdout_reader` 会认错帧）；cell 内派生线程的输出也只经这把锁。

**P1 不做降级分支**：runner 与 host 同版本；chunk 通道任何异常都按普通执行错误处理，`eval.complete` 仍带
settle 的最终 `output`，客户端不会停在“只有半截流”的状态。
`stream=false`（即所有 `execute_code` 调用）走原路径，帧格式、结果 JSON、`kernel.raw` 归属全部不变；
既有 `tests/tools/test_code_kernel.py:174-183`（raw passthrough）与 `test_code_execution.py` 的 key-order
测试必须保持绿。

### D5. 事件、行与模型文本

**事件**（authority 直发；不用 `authority.publish_execution`，它要求 `executing[session]==generation`，
`gateway/session_authority.py:1007-1021`，回合外恒为 False；用 `SessionEvents.publish`，
`gateway/session_events.py:38-68`，与 `/bg` 的 `_spawn` 同一路径，`gateway/session_tui_side.py:157-162`）：

```text
eval.start    {execution_id, code, language:"python", exclude_from_context, timestamp}
eval.output   {execution_id, stream:"stdout"|"stderr", seq, text}
eval.complete {execution_id, status:"complete"|"error"|"cancelled", exit_code?, cancelled,
               output, truncated, meta:{truncation?, artifactError?},
               kernel:{mode, reused, execution_count, state_reset}, error?}
```

`eval.complete` 的 `output` 是最终（截断后、脱敏后）正文，供客户端用 `setComplete({output})` 覆盖流式缓冲；
客户端按 `execution_id` 去重，按 `seq` 丢弃乱序 chunk。`eval.start` 在 spawn 之前由 handler 在 loop 线程
同步 publish（先于任何 chunk 与方法 reply）；`eval.output`/`eval.complete` 从 worker 线程用
`live.event_stream.publish` 发出（与 `/bg` 的 `_spawn` 同一路径），两条时序都由事件流的分发顺序保证。
spawn 前捕获 `live`，每次 publish 前重查 `authority.sessions.get(session_id)`（会话被回收则丢弃事件），
worker 线程进 `_side_agents` 账本并由其 restart/retire 处理（D2）。

**行**（settle 后写一次；模型可见文本 = `content`）：

```text
role          = "user"
content       = "Ran Python:\n```python\n<code>\n```\n"
                + (output ? "Output:\n```\n<output>\n```" : "(no output)")
                + (cancelled ? "\n\n(execution cancelled)" :
                   exit_code not in (0, None) ? "\n\nExecution failed with code <exit_code>" : "")
                + truncation_notice        # 例：\n\n[output truncated: showing <n> of <m> bytes]
display_kind  = "eval_execution"
display_metadata = {"eval_execution": {
    execution_id, code, output, language: "python", exit_code, cancelled, truncated,
    meta: {truncation?, artifactError?}, exclude_from_context}}
                 + (exclude_from_context ? {"display_only": true} : {})
```

- 主体逐字对齐 `pythonExecutionToText`（`Ran Python:`/`Output:`/`(no output)`/失败/取消行；空输出时只有
  `(no output)`，**没有** `Output:` 标签）；上游
  `$S(meta)` 的截断/artifact 尾注在 Hermes 用等价的 `[output truncated: …]` 行表达。`exclude_from_context`
  时**整条不进模型**（D6）。
- 只有 display 需要的 `code`/`output` 也进 `display_metadata`（≤256KB code、≤50KB output），客户端 replay
  不解析正文；正文同时是模型文本，二者由同一组值生成。
- **写入时机**：idle → 立即 `append_display_row(...)`（ADR-3 D3 定义的同一 primitive：压缩 lineage 去重、
  拒绝 active lease；去重键 `$.eval_execution.execution_id`）。回合流式中 → 进
  `live.pending_display_rows`，在下一个 `execute_admission` 开头（`gateway/session_ingress.py:233-235`，
  `note_if_unviewed`/`_handle_message` 之前）flush，使行落在上一轮已持久化的行之后、下一条 user 行之前；
  恢复后立即可见；并在该回合 settlement 后 best-effort 再 flush 一次（用户不再发 prompt 也能在 resume 看到），
  owner 退出前也 best-effort flush 一次（未及写出则丢，与 omp 的进程内 pending 一致）。这与 omp 的语义一致：
  `recordPythonResult` 在 `isStreaming()` 时只入 `#i`，下一次 prompt 前 `flushPending`。
- **不写** `display_kind="hidden"`：`_history_to_messages` 会跳过 hidden
  （`tui_gateway/session_history.py:317`），resume 就丢了这一块。
- 失败/取消也落行（omp 对每个 settle 的 cell 都 `recordPythonResult`）；启动前的拒绝不落行。
- 行写失败只记日志，**不**影响 `eval.complete`（客户端已看到结果；下次 resume 缺这一块，与 append 失败一致）。
- 行按普通 user 行处理，所以“user 后面再跟一条 user”（例如 steer 之后的 eval）是既有形态，不需要新修复路径。

### D6. 截断元数据与 `$$`

Hermes 的核结果给的是 head+tail（40/60）截断：`stdout_truncated`/`stdout_bytes_captured`/
`stdout_bytes_total`/`stdout_bytes_omitted`（`code_execution_tool.py:56-81`）。为让 pi-tui 的
`TruncationMeta` 能画对提示，给核结果**加两个只读字段**（additive，execute_code 也受益）：
`stdout_lines_total`、`stdout_lines_shown`（在 `_truncate_stdout_text` 里从完整文本直接数）。

映射：

| `meta.truncation` | 来源 |
|---|---|
| `direction: "middle"` | Hermes 的 head+tail |
| `truncatedBy: "bytes"` | 字节上限触发 |
| `totalBytes` / `outputBytes` / `elidedBytes` | `stdout_bytes_total` / `stdout_bytes_captured` / `stdout_bytes_omitted` |
| `totalLines` / `outputLines` | 新增的 `stdout_lines_total` / `stdout_lines_shown` |
| `artifactId` | **不填**——Hermes 的 spill 是文件路径，不是 `artifact://` id；正文里给路径 |
| `artifactError` | spill 失败且发生截断 → `"write"`（枚举里唯一对应项） |

`$$`（排除上下文）在 Hermes 没有反向标记（现有只有 `MODEL_ONLY_DISPLAY_METADATA_KEY="model_only"`，
作用是从 display 里藏起、喂给模型，`agent/context_compressor.py:310`、`hermes_state_messages.py:58-61`）。
新增对称常量 `DISPLAY_ONLY_DISPLAY_METADATA_KEY = "display_only"`（与 `model_only` 同处定义），
并在 `_rows_to_conversation` 解码前跳过（`hermes_state_messages.py:1564`；只影响模型读取，
`_history_to_messages`/display projection 仍返回该行）。不加表列、不加 CHECK。

### D7. 客户端：`$` 入口、实时块、replay

**(a) 输入**：`input-controller.#submitText`（`omp-ui/src/modes/controllers/input-controller.ts:148-193`）在
`/` 分支旁按上游 `parsePythonCommandInput`（历史 `input-controller.ts:217-248`）移植入口判定：
  - sigil 只在后面跟空白时成立（`$HOME`、`$!` 等仍是普通 prose，见 `pythonCommandPrefixLength` 注释 #2944）；
  - 单个 `$` 且正文像粘贴的 shell prompt（`looksLikePastedShellPrompt`）→ 不当 Python，照常提交给模型；
  - `$$` → `excludeFromContext=true`；去 sigil 后 `session.evalExec(code, {excludeFromContext})`。
同时让 `#composerNativeState()`（`interactive-mode.ts:1401-1412`）返回
`shell: {kind:"python", excluded}`（pi-tui 的 `SHELL_SIGIL_RE`/`composerState().shell` 已就绪，
`custom-editor.ts:51,56-61,962-964`），只影响 sigil 隐藏与边框颜色，不改布局。

**(b) 会话**：`GatewayOmpSession` 增加：
- `evalExec(code, {excludeFromContext})` 生成 `execution_id = crypto.randomUUID()` 并
  `client.request("eval.exec", {session_id, execution_id, code, exclude_from_context})`，返回同名 id；
- `evalCancel(execution_id?)` → `client.request("eval.cancel", ...)`；
- `#evals: Map<execution_id, {code, exclude, seq, output, component, startedAt}>`；
- `#notificationHandlers`（`gateway-session.ts:758-786`）新增 `eval.start`/`eval.output`/`eval.complete`；
  未知类型仍按现有 `?.` 丢弃。事件按 `notification.sessionId` 的既有过滤生效。

**(c) 实时渲染**：`eval.start` 时 `new EvalExecutionComponent(code, ctx.ui, exclude)`；chunk →
`appendOutput(text)`；complete → `setComplete(exitCode, cancelled, {output, truncation, artifactError})`。
会话 `#running` 时组件先挂到 `ctx.pendingMessagesContainer`（`interactive-mode.ts:462,735`，与排队消息同一
挂载点）。当前 shell 已随 §0.3 删掉 bash 的 pending 组件机制（`pendingPythonComponents`/
`flushPendingBashComponents` 在 `omp-ui/src` 全树 0 命中），所以本 ADR 只新增一个
`pendingEvalComponents: EvalExecutionComponent[]` 与同形的 `flushPendingEvalComponents()`（历史实现
`git show 5bd7dabd213^:omp-ui/src/modes/utils/ui-helpers.ts:1595-1605`），在 `turn.end`/`session.settled` 时
`removeChild`+`chatContainer.addChild` 落到 transcript 位置；**不恢复 bash 路径**、不新增容器。
Escape 的既有中断分支补一档：`isEvalRunning` 时先 `evalCancel()`（历史 `input-controller.ts:644-648`
的顺序：streaming → bash → bashMode → eval → pythonMode）。

**(d) Replay**：`rowToAgentMessage` 增加
`display_kind === "eval_execution"` → `{role:"pythonExecution", code, output, exitCode, cancelled, truncated,
meta: display_metadata.eval_execution.meta, timestamp, excludeFromContext}`；
`addMessageToChat` 增加 `case "pythonExecution"`（`ui-helpers.ts:130-186`），构造/填充方式与
`chat-transcript-builder.ts:365-373` 完全一致；`renderSessionContext` 复用同一 switch，自动获得 replay。
`copy-selector`/`tree-selector` 已有该 role 的分支，无需改动。

**(e) 快照期间的运行中 cell**：`adoptSnapshot` 整体替换 `#messages` 时，按 `#evals` 里仍 running 的条目
重建组件与已收 chunk；complete 后条目删除。运行中的 cell 不落行，重连丢块是可接受且与 omp 一致的
（omp 的组件同样不在 session 里，直到 settle）。

### D8. 模型面：`execute_code` 增加 `language`，JS 走 sibling Bun kernel

omp 的 eval 工具在 Hermes 的承载面是 `execute_code`：它已经是“持久 kernel + 工具桥”的模型入口，也是 D3 里
`$` 的 Python kernel 持有者。给它加一个可选语言参数比新增核心工具高一级（footprint ladder：扩展既有代码
优先），也避免每次 API 请求多带一个与 `execute_code` 重叠的工具 schema（核心工具的准入门槛正是“每次请求都
携带”）。

- schema（additive）：`{code, language?: "py"|"js" = "py", reset?}`。既有 schema 重建器
  `model_tools._rewrite_execute_code` / `build_execute_code_schema` 把 `language` 枚举收窄到已安装后端
  （`py` 恒有；`js` 仅当 `pm.installed_package("bun")` 有值），描述从 “Run Python…” 改成可用后端的说明
  （omp 的 `Execute Python and JavaScript…` / `No eval runtimes…` 同义）。不加 `title`/多 cell：
  omp 的 eval 卡不在本区块，`execute_code` 的卡片继续是通用卡（§2.3.2.1 工具执行行）。
- 审批与硬闸：`check_execute_code_guard` 增加 `language` 关键字（`<<'PY'`/`<<'JS'`、卡片文案随语言；默认
  `python` 让既有调用与测试不变）；先跑 D2 第 6 条的硬闸；本地 viewer turn 内自动批准（既有
  `_is_local_viewer_turn`）。gateway 生命周期硬闸是文本匹配 + argv 标点归一化
  （`cron/lifecycle_guard.py:394-450`），`Bun.spawn(["launchctl","bootout",…])` 也会命中。**用户 `$` 仍不走批准**
  （D2 第 7 条）。
- 分派：`language=="py"` → 既有路径（`run_session_cell`，D3）；`js` → `run_js_cell`（D9）。两者返回既有结果 JSON
  形状（`status/output/exit_code/stdout_truncated/...`，`code_execution_tool.py:738-805`），只加一个 additive 的
  `language` 字段；模型契约不变，JS 的 output/traceback 映射到同样的字段。
- `reset`：清该语言的 kernel（`py` 现有行为；`js` 杀 Bun 进程）。超时仍取 `code_execution.timeout`；不加每调用
  `timeout`（omp 的 timeout 属于 eval 工具卡面）。
- 工具面：`execute_code` 的 core/`code_execution` 成员身份不变，不新增工具、不改 toolsets；`details`/`cells`/
  `languages` 不进入本 ADR（通用卡只读结果文本）。不做 async 后台化、`title`、多 cell、views/statusEvents。

### D9. JS 后端：PM Bun 子进程上的间接 eval kernel

- 运行时：只认 PM 的 bun（`pm.installed_package("bun").binary`）。缺失时 `language:"js"` 不在 schema 枚举里；
  显式传 `js` 返回 `tool_error("JavaScript needs Bun: hermes pm install bun")`。不读 `PATH`/`~/.bun`，保证锁定版本。
- 进程：`tools/eval_js_kernel.py`（新）为 `(owner, "js", bun_path, child_cwd)` 维护一个 Bun 子进程，
  与 `tools/code_kernel.py` 并列（Python 的 `SessionKernel` 绑定 Python runner，不复用）；同样的 idle/LRU 上限
  （`code_execution` 配置）、`shutdown_*_for_owner`、atexit、`spawn_owned(..., purpose="eval-js-kernel")`。
  `shutdown_js_kernels_for_owner` 接进 `tools/approval.clear_session`（现在只清 Python local/remote 两个
  registry，`tools/approval.py:314-322`）。
- 启动：`bun --no-install tools/eval_js_runner.ts`，**cwd = kernel staging dir**，env 复用 `_scrub_child_env`
  并额外去掉 `NODE_OPTIONS`/`BUN_INSTALL`/`BUN_INSTALL_CACHE_DIR`、把 `BUN_INSTALL_CACHE_DIR` 与
  `HOME`/`USERPROFILE` 指到 staging。runner 收到 init 后 `process.chdir(session_cwd)`：project 的
  `bunfig.toml`（含 `preload`）不会被读——本机验证过默认会读、`--config` 不能阻止、而“staging 启动后再 chdir”
  不会——cell 的相对路径仍按 session cwd 解析。`--no-install` 关掉 Bun 默认的自动安装（`--install=auto`）。
  runner 另把 `globalThis.require = createRequire(session_cwd + "/cell.cjs")` 暴露给 cell（`require()` 与
  `import()` 都按 session cwd 解析）。
- 生命周期：runner 每 2s `process.kill(process.ppid, 0)`，宿主消失即退出（POSIX 由 `spawn_owned` 的
  owner-death 进程组兜底，Windows 由 job object 兜底）。
- 执行模型：**间接 eval**（`globalThis.eval`）在 runner 全局作用域执行 cell；不用 `node:vm`：omp 记录了
  Bun 在 `Worker.terminate()` 打断 `vm.runInContext` 时的 SIGTRAP（`shared/indirect-eval.ts`），且 Bun 1.4.4
  的 `vm` 缺 `importModuleDynamically`（本机实测动态 import 报 `ERR_VM_DYNAMIC_IMPORT_CALLBACK_MISSING`）。
  cell 体包成 `(async () => { … })()` 并 `await`，顶层 `await` 可用。
- 变换（`tools/eval_js_transform.ts`，随 runner 发货）：先用 `Bun.Transpiler({loader:"ts"})` 统一 TS/JS
  （本机验证：去类型、保留顶层语句与 import），再做一层 tokenizer 级变换，只在顶层语句且不在字符串/模板/
  注释/regex 内时，为每个顶层声明插入**发布语句**（声明本身保持局部）：
  - `const x = …` / `let y = …` / 顶层 `var z = …`（含解构）→ 原声明后插 `globalThis.x = x;`（名字从
    declarator pattern 提取；对象/数组解构取绑定名）；
  - `class X …` / `function f(…) {…}` → 声明后插 `globalThis.X = X;` / `globalThis.f = f;`；
  - 顶层 `import`（default/named/namespace/side-effect）→ 改写成 wrapper 内的
    `const m = await import(<resolved>)`，并发布绑定；解析用 `createRequire(session_cwd + "/cell.cjs")`
    （本机验证 `resolve` + `import(pathToFileURL(...))` 可用；`node:`/绝对/`file:` 直接 import）；
  - 后续 cell 的裸标识符会命中全局对象上的这些属性（全局对象环境记录），因此**不需要**改写引用，也**不能**把
    `let`/`const` 降级成 wrapper 函数里的 `var`——wrapper 函数作用域的 `var` 不会跨 cell 存活；本机验证
    `eval("const z=3; globalThis.z=z")` 之后 `eval("z")==3`，async wrapper 内发布同样生效；
  - 顶层 `export`、`import.meta`、顶层 `return`、无法可靠分类的 `/`（regex vs 除法）、声明名为
    `globalThis`/`undefined`/`NaN`/`Infinity` → **fail closed**：`unsupported_js_construct`，不执行；
  - 嵌套块里的 `var` 不发布（它绑定到 wrapper 的函数作用域）；omp 的模块语义会保留它，这是已知差异。
  不 vendor Babel：仓库没有 JS parser 依赖，引入 parser 是新的供应链/体积负担；有限变换器可测且失败即拒绝，
  验收用对抗 cell 证明不误改。这是相对 omp（Babel + 464 行模块加载器）的明确缩范围：只支持上述四种静态
  import 形态与顶层声明发布，其余 fail closed。
- 结果：runner 回 `{status:"ok"|"error", stdout, stderr, exit_code, execution_count}`，host 复用 D6 的
  截断/脱敏映射。cell 内 `console.*`/`process.stdout.write` 在 cell 期间被换成缓冲写入器（结果帧仍用
  `HERMES_JS_SENTINEL` 前缀，宿主优先认帧，其余字节按 raw 处理，与 Python runner 同款）。
  `process.exit()` 会直接结束 runner：宿主看到 EOF → `status="error"`、kernel 丢弃、下次调用换新进程
  （Python 的 `sys.exit` 是 `status="exit"`，JS 没有等价的拦截点）。
  超时/取消 = 杀进程，VM 状态丢失，结果文本带 omp 同款注释（*“The JS worker was force-killed and its VM
  state was reset…”*，历史 `eval/js/executor.ts::formatJsTimeoutAnnotation`）。`reset` = 起新进程。
- 不移植：`views`/`statusEvents`/`images`（`display()`）、`out://`、`agent()/wait()/workpool()/judge/completion`
  prelude、session snapshot/prelude 注入、`%bun add`/包安装、speculation/shadow 执行、并发多 cell。
- 上条的「并发多 cell」要分两层看：
  - **卡面多 cell（不移植）**：omp 的 eval 卡按多 cell 设计——渲染器接受 `args.cells` 数组
    （`pi-tui/src/tools/eval.ts:176` `getRenderCells`）、head 打 `${cellCount} cells`（:766）、
    `details.cells?: EvalCellResult[]` 带每 cell 的 status（:117-128）；这是“不采纳”表的「eval 卡面」
    （async job manager、steering、工具卡 details 通路）。但 vendored 的 tool schema 仍是单 cell
    （`prompts/tools/eval.md:1` “One cell per call”；`tools/eval.ts:139-149`），`#runCells` 只从单份
    params 构造一个 cell（`tools/eval.ts:534`），`concurrency = "exclusive"`（:430）再由 agent loop
    串行化（`vendor/agent/src/agent-loop.ts:3864-3889`）——“多 cell”是卡的容量，不是当前入参。
  - **内核多 cell 并发（不移植）**：kernel 协议按 `runId` 记账、能容忍并发 run
    （`eval/js/worker-core.ts:205-220`；Python runner 每请求一个 asyncio task，`eval/py/runner.py:2899`）。
    Hermes 的 `$` 选 per-session 单飞，第二个 `eval.exec` 返回 `eval_busy`（见风险表）。
- 历史佐证：本仓库 schema 侧从未有多 cell 入参——`git log -S'cells:' -- omp-ui/src/tools/eval.ts`
  只命中两次 vendoring 提交（`76cbd169da`、`5bd7dabd21`），`cells:` 只出现在 details/渲染器。
- 补齐的前置设计见 D11（各项互不依赖）。
- 隔离：间接 eval 不是安全边界；JS cell 与 Python cell 一样拥有宿主机能力（Bun `spawn`/`fetch`），闸门与
  Python 完全一致（D2/D8），env scrub 如上；不承诺沙箱。
- **打包**：runner/transform 是非 Python 资源；`pyproject.toml` 的 `[tool.setuptools.package-data] tools` 现在只列
  `bot_desktop/launcher.sh`、`bot_desktop/wallpaper.png`（注释说明未声明的资源会从 sealed wheel 消失），必须加上
  `eval_js_runner.ts`、`eval_js_transform.ts`；bundle 的 tools 拷贝清单同步。否则 JS 只在源码检出可用、打包安装
  静默坏掉。

### D10. JS 的工具桥与 prelude 范围

- 复用 Python kernel 的 line-JSON RPC server（`tools/code_execution_rpc.py::_rpc_server_loop`）：把循环抽成可传入
  owner 的 `serve_rpc(...)`，JS kernel 自己 bind socket/token；`CellAuthority` 的“每 cell 捕获、settle 后 retire”
  照旧（`code_kernel.py:838,868`）。JS runner 用 `node:net` 连接 `HERMES_RPC_SOCKET`/`HERMES_RPC_TOKEN`
  （UDS 或 `tcp://127.0.0.1:port`，与 `code_execution_tool._connect` 同一套地址）。
- 预置：runner 收到工具名清单后定义 `globalThis.<name> = (args) => call(name, args)`，与 Python 的
  `hermes_tools` 同一 allowlist、同一每 cell 预算、同一拒绝/脱敏。调用在 runner 内单飞行排队（RPC server 一次
  一条请求）。
- 不移植 omp 的 prelude 词汇（`agent()`/`wait()`/`workpool()`/`judge`/`completion`/`out://`/`display()`）：
  工具桥已经暴露 Hermes 自己的工具；这些是 omp 的产品面，不是本区块的契约。`%` 命令同样不移植。

### D11. 后续移植的前置设计（本 ADR 不实现，只登记接缝）

七项互不依赖，也不改 P1–P5 的契约；登记它们是为了让“不移植”有可执行的下一步，而不是一句品味判断。

1. **`views`/`statusEvents`/`images` → 工具卡 details 通路**
   - 缺口：`execute_code` 只回一个 JSON 字符串（`code_execution_tool.py:56-81`），结果里没有 details 载体；
     工具行虽然会透传 `display_metadata`（`tui_gateway/session_history.py:364-368`），但客户端把 tool 行投影成
     通用工具卡、只读结果文本（`gateway-session.ts:357-383`、`ui-helpers.ts:130-186`），不渲染 views/images。
   - 设计：工具结果加 additive `display` 块 `{images: ImageContent[], views: [{text, view}], statusEvents}`，
     工具行写入时把它放进 `display_metadata`（走 `append_message` 已有的自由 `display_metadata`，
     `hermes_state_messages.py:396-429`）；客户端在工具卡内按 `text` 匹配 `views[]` 渲染（与 `EvalOutputPart`
     （`pi-tui/src/tools/eval.ts:771`）、`placeViews`（`tools/eval.ts:1100`）同形）。模型正文不变，`views`
     只服务用户。
   - 验收：无 view 命中时退回纯文本；view 的 `text` 被截断/不在输出里时跳过而不是错位；图片走同一
     `ImageContent` 编码，不新增第二套。

2. **`agent()`/`workpool()` → eval 侧 handle 桥**
   - 缺口：Hermes 现有委派是模型面工具调用——`delegate_task`（`delegation` toolset）与
     `agent`/`agent_out`（`agents` toolset），见 `toolsets.py:24-25,153-156`——结果回上下文，没有内核内
     可跨 cell 等待的句柄。并且 `CellAuthority` 每 cell 捕获调用线程身份与 prompt 回调、settle 后 `retire()`
     （`code_kernel.py:197-250,838,868`），迟到的 RPC 直接返回 `No active execute_code cell`（:228）。
   - 设计：`serve_rpc` 增加 `__agent__`/`__workpool__` 宿主 op，映射 `delegate_task`/`agent`/`agent_out`；
     handle 用 `agent://<id>` 标识，`status`/`wait`/`send`/`cancel` 各是一个 op。关键是 **per-session（非
     per-cell）句柄注册表**，owner 按 profile scope 绑定：后台 handle 必须活过 cell，迟到回调走会话级身份，
     不能复用已 retire 的 `CellAuthority`；budget/cancel 由 cell 的 runner 承载，注册表只管生命周期与授权。
     prelude 走 D10 的工具桥清单：位置对应 omp `SessionSnapshot.preludes`，但 Hermes 只注入工具名清单
     （不注入源码），由 kernel init 一次下发、cell 之间不重发（会话内工具面固定）。
   - 验收：cell 超时/取消后 handle 仍可 `wait`/`cancel`；迟到回调不被 `No active execute_code cell` 拒绝；
     未 wait 的结果按既有自动投递语义交付。

3. **speculation/shadow → agent 流式工具参数事件**
   - 缺口：工具只在完整参数到达后执行（`model_tools.handle_function_call`）；omp 的接缝是工具级
     `ToolSpeculationPolicy.stream`（`tools/eval.ts:445-470`）+ 参数增量解码
     （`eval/speculation/eval-args-stream.ts`）。
   - 设计：在 `handle_function_call` 之前增加工具级 `stream` 钩子（partial JSON + abort），eval 工具据此
     静态投影 `read(...)` 计划并投机执行；权威执行按 `(siteId, dynamicPath, name, fingerprint, occurrence)`
     claim 命中复用，miss 回退普通路径；kernel 快照（revision/digest）不符即丢弃 shadow cell
     （`eval/speculation/cell-session.ts:71-190`）。
   - 验收：claim 未命中时行为与无投机逐字节一致；副作用只来自已提交的调用；流式参数重启/改写即丢弃。

4. **并发多 cell → 放弃 per-session 单飞**
   - 缺口：D2 第 8 条把同会话限制为一条 in-flight；kernel 本身按 `runId` 记账、可并发
     （`eval/js/worker-core.ts:205-220`；`eval/py/runner.py:2899`）。
   - 设计：去掉 `eval_busy` 闸，`execution_id` 成为事件/行/取消的唯一键：`eval.output` 已带
     `execution_id`，`eval.cancel {execution_id?}` 省略即取消全部；行按 settle 顺序写；
     chunk 按 `execution_id + seq` 归并。
   - 验收：两个 cell 交错输出不串流；同一 id 重试仍幂等（D1）；取消一个不影响另一个。

5. **`judge()`/`judge_batch()`/`completion()` → host 侧模型角色链 + 批量 run**
   - 缺口：Hermes 的辅助模型选择在 `agent/auxiliary_client.py`（`resolve_aux_model`/`default_aux_model`
     角色梯），但没有“内核内一次调用跑分类/批量”的桥；omp 的接缝是 `eval/judgment-bridge.ts:273`
     （`sessionJudge`）与 `eval/judgment-batch-bridge.ts`。
   - 设计：`serve_rpc` 加 `__judge__`/`__judge_batch__`/`__completion__` op；`judge` 按 aux 角色链解析模型、
     校验 `questions` schema、图片按 `ImageContent` 传；`judge_batch` 由 host 拥有整轮 run（有界 fan-out、
     每项一次重试、`min_ok`），cell 侧 `drain` 分批取 settled；usage 记到与会话相同的账本。
   - 验收：cell 超时不取消 host 的 batch run；单 item 失败返回 `error` 不抛；角色/预算缺失报 `tool_error`
     而不是静默换模型。

6. **`out://` → 回合内 reply sink**
   - 缺口：Hermes 没有 `out://`（`grep -rn 'out://'` 在 Python 侧与 `omp-ui/src` 均零命中）；回复只在回合
     结束或 gateway 通知路径产生。omp 的接缝是写-only 的 `internal-urls/url-filesystem.ts:256` 与 bash 的
     `onReply`（`tools/bash.ts:653`）。
   - 设计：cell 内 `write("out://", md)`（及 bash `> out://`）绑定到本地 viewer 的 live event stream，
     作为一条 assistant 文本事件发出，不入 transcript、不结束回合；≤64 KiB；非本地 surface 与子 agent 拒绝。
   - 验收：超限截断并报错；回合结束后的写入被拒；print/RPC/ACP 默认关闭。

7. **`%bun add`/包安装 → managed 环境 + 锁（拒绝 project 级）**
   - 缺口：PM 只提供 bun 二进制（`pm/packages.py:587` `Bun`），没有会话级依赖目录/锁；Hermes 现状是
     skill/terminal 安装，与内核内 `%bun add` 不是同一条路。omp 的接缝是 `eval/js/package-installer.ts`
     （managed/project 两种环境 + project lock）。
   - 设计：只做 **managed**——目录在 Hermes home 下按 session cwd 哈希分桶（`package.json` + lock + 文件锁），
     `%bun add` 是独立 cell（不重置 kernel），安装成功才提交环境；runner 继续 `--no-install`。
     **拒绝 project 级**：不写用户项目的 `package.json`/`node_modules`/锁文件，`%environment
     project` 返回 `unsupported_environment`；改工程依赖仍走 skill/terminal 的显式操作。模块解析仍以
     session cwd 为主、managed 目录作 fallback（对应 omp `packageRoot` 的次序），不改 D9 的 `createRequire`。
   - 验收：安装前后 session cwd 无新增/修改文件；`%environment project` 被拒；并发安装同 cwd 串行；缺包
     import 失败而不是隐式安装。

## 协议

```text
eval.exec (new, canonical)
  {session_id, execution_id, code, exclude_from_context?: false} -> {execution_id}
eval.cancel (new, canonical)
  {session_id, execution_id?} -> {cancelled: [execution_id]}

eval.start (new)
  execution_id: string
  code: string
  language: "python"
  exclude_from_context: bool
  timestamp: float

eval.output (new)
  execution_id: string
  stream: "stdout" | "stderr"
  seq: int
  text: string

eval.complete (new)
  execution_id: string
  status: "complete" | "error" | "cancelled"
  exit_code?: int
  cancelled: bool
  output: string
  truncated: bool
  meta: {truncation?: object, artifactError?: "open"|"write"|"flush"|"end"}
  kernel: {mode: str, reused: bool, execution_count: int, state_reset: bool}
  error?: string

kernel request (additive, internal)
  {"id", "code", "stream"?: true}

transcript row (display-only; model sees `content` unless display_only)
  role = "user"
  display_kind = "eval_execution"
  display_metadata = {"eval_execution": {execution_id, code, output, language, exit_code,
                       cancelled, truncated, meta, exclude_from_context},
                      "display_only"?: true}

execute_code (additive)
  {code, language?: "py"|"js" = "py", reset?} -> existing result JSON + "language"
  # 成功/失败沿用既有 status/output/exit_code/stdout_* 字段；失败仍是 status="error"+error

JS kernel (internal)
  init    {"type":"init","cwd":..., "tools":[...], "rpc":{...}}
  request {"id","code","reset"?}
  reply   {"status":"ok"|"error","stdout","stderr","exit_code",
           "traceback","execution_count"}     # process.exit => EOF, not a reply
```

`shell.exec` 不变（仍是 `!cmd` 的 request/response，不进 transcript）；`execute_code` 只增加 `language` 入参与
结果里的 `language` 字段，其余 schema/字段/卡片不变。

## 实现落点

| 层 | 位置 | 改动 |
|---|---|---|
| 契约 | `tui_gateway/contracts/canonical.py` | `EvalExecParams/Result`、`EvalCancelParams/Result`、两个 `canonical_method` |
| 契约 | `tui_gateway/contracts/events.py` | `EvalStartPayload`/`EvalOutputPayload`/`EvalCompletePayload` + `event(...)` |
| 网关 | `gateway/session_eval.py`（新） | 闸门、作用域绑定、单飞、线程、chunk 发布、行写入、cancel |
| 网关 | `gateway/session_controls.py::handlers` | `handlers.update(session_eval.handlers(self))` |
| 内核 | `tools/code_kernel.py` | `stream` 请求、chunk sentinel/tee、`_stdout_reader` demux、`_await_cell(on_chunk)`、`chunk_sink` |
| 内核 | `tools/code_execution_tool.py` | 拆 `_hard_guards` / `run_session_cell`；`_truncate_stdout_text` 加 lines 字段；结果 JSON additive |
| 内核 | `tools/code_execution_session.py`（新） | `session_kernel_target(...)`（mode/python/cwd/sandbox_tools 单一派生） |
| 行 | `hermes_state_detached_results.py` | `append_display_row(...)`（ADR-3 D3 同一 primitive；若未落地则本 ADR 先实现） |
| 行 | `hermes_state_messages.py` | `_rows_to_conversation` 跳过 `display_only`；`DISPLAY_ONLY_DISPLAY_METADATA_KEY` |
| 行 | `agent/context_compressor.py` | `DISPLAY_ONLY_DISPLAY_METADATA_KEY = "display_only"`（与 `model_only` 并列） |
| 行 | `gateway/session_authority.py::LiveSession` | `pending_display_rows` |
| 行 | `gateway/session_ingress.py::execute_admission` | admission 最开头 flush pending 行 |
| 客户端 | `omp-ui/src/modes/controllers/input-controller.ts` | `$`/`$$` 分支 → `session.evalExec` |
| 客户端 | `omp-ui/src/modes/interactive-mode.ts` | `composerState().shell`；eval 组件挂载/pending 迁移；Escape → `evalCancel` |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.ts` | `evalExec`/`evalCancel`、`#evals`、三个 handler |
| 客户端 | `omp-ui/src/modes/utils/ui-helpers.ts` | `case "pythonExecution"` |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.ts::rowToAgentMessage` | `display_kind="eval_execution"` 投影（仅用户块） |
| 模型面 | `tools/code_execution_tool.py` | `execute_code` schema/入参/结果加 `language`；py/js 分派 |
| 模型面 | `model_tools.py` | `_rewrite_execute_code`/`build_execute_code_schema` 按已安装后端收窄 `language` 并改描述 |
| 模型面 | `tools/approval.py` | `check_execute_code_guard(..., language=)`：heredoc 标签/描述随语言；`clear_session` 同时清 JS kernel |
| JS 后端 | `tools/eval_js_kernel.py`（新） | Bun 子进程注册表、init/chdir、请求/回复、idle/LRU、owner 清理、`--no-install` |
| JS 后端 | `tools/eval_js_runner.ts`（新） | 间接 eval、`(async()=>{})()`、`Bun.Transpiler`、结果帧、RPC 工具桥、父进程退出轮询 |
| JS 后端 | `tools/eval_js_transform.ts`（新） | tokenizer 级顶层声明/import 变换；不可分类即 fail closed |
| JS 后端 | `tools/code_execution_rpc.py` | `serve_rpc(...)` 抽成可传 owner 的循环（Python/JS 共用） |
| 打包 | `pyproject.toml` | `[tool.setuptools.package-data] tools` 增加 `eval_js_runner.ts`、`eval_js_transform.ts`（sealed wheel 否则丢资源） |
| 文档 | `docs/omp-ui-panels-gateway-plan.md` | 实现后 §2.3.2.1 Eval 行 `部分` → `已接（local）`；§2.3.2 渲染器描述不变 |
| 生成 | `scripts/gen_gateway_contracts.py` / `scripts/gen_panel_inventory.py` | 重新生成并 `--check` |

## 不采纳

| 方案 | 理由 |
|---|---|
| 新增独立 `eval` 模型工具 | 与 `execute_code` 重叠；核心工具每次 API 请求都携带，footprint ladder 要求先扩展既有代码。`execute_code(language)` 已覆盖 JS，且 `$` 的行型与 eval 卡不是本区块 |
| 复用 `shell.exec` | 语义是 shell、无 kernel、无流、明确不进 transcript（`session_shell.py` docstring）；改它会破坏 `!cmd` |
| 走 admission + `viewer_turn` 拿自动批准 | 结果行会插进当前回合，模型会中途看到 eval 并改变行为；eval 不是 turn |
| 用 `check_execute_code_guard` 弹批准卡 | `$` 是本地用户自己的输入；批准卡在回合外没有 `viewer_turn`，会阻塞到超时。硬闸仍保留 |
| 给用户面加 `$js` / 复用 `pythonExecution` 行 | omp 用户面只有 Python（`PythonExecutionMessage` 无 `language`）；造一个上游不存在的入口与行型，不忠实。JS 只走 `execute_code(language:"js")` |
| 用 `node:vm` 承载 JS cell | omp 记录 Bun 在 terminate 打断 `vm.runInContext` 时 SIGTRAP；Bun 1.4.4 的 vm 也缺动态 import 回调。用间接 eval + tokenizer 变换 |
| vendor Babel 做声明/import 改写 | 仓库没有 JS parser 依赖；引入 parser 是新的供应链与体积负担。有限变换器 + fail closed |
| 移植 omp prelude（`agent`/`wait`/`workpool`/`judge`/`out://`/`display`） | omp 产品面 helper，不是本区块契约；Hermes 的工具桥已暴露自己的工具 |
| eval 自动后台化 / `title` / 多 cell（卡面）/ eval 卡 details | 需要 async job manager、steering、工具卡 details 通路；这些属于 omp 的 eval 卡面（多 cell 仅指卡的容量，schema 仍单 cell，见 D9/D11），v1 用 `execute_code` 的通用卡 |
| `%load/%pip/%bun/%environment` magics | 属独立输入语法；Hermes 已有 skill/terminal 做依赖安装，且用户区块只要求 code+output（后续若做，只做 managed，见 D11 第 7 条） |
| 项目级包安装（`%environment project`，写用户的 `package.json`/`node_modules`/锁） | 越过会话预算修改用户的工程依赖且不可回滚；只保留 Hermes home 下的 managed 缓存目录（D11 第 7 条），工程依赖仍由 skill/terminal 显式安装 |
| `views`/`statusEvents`/图片/`artifact://`/auto-background | 用户面的 `PythonExecutionMessage` 本来就不含这些字段（pi-tui `messages.ts:209-226`），不存在 producer 需求；async 后台化是工具面语义 |
| `display_kind="hidden"` 存行 | resume 时被 `_history_to_messages` 丢弃 |

**明确的取舍（与 omp 的差异，写清楚而不是假装等价）**：

1. 超时/取消会杀掉 kernel 并丢失命名空间（`execute_code` 既有契约，`code_kernel.py:717-731,748-750`）；omp 的
   cell 取消可能保留 namespace（有 cancel budget）。这是安全上的选择：不提供无法中断的解释器。
2. 截断是 head+tail 且用 spill 文件；没有 `artifact://`。客户端看到的是 `middle` 提示 + 正文里的 spill 路径。
3. 文件描述符级输出（子进程直接写 fd）在 cell 结束时才进入结果，不实时流（用户 `$` 的 tee 只覆盖 Python
   层写；JS 同样只缓冲 `console`/`process.stdout.write`）。如需 fd 级实时，另立变更（pipe + pump）。
4. `$` 不带 `timeout`/`reset`/`title`；只有 `$$` 的排除上下文。
5. JS 的跨 cell 持久化是“顶层声明后发布到 `globalThis`”（声明保持局部，裸名在后续 cell 通过全局对象解析）；
   嵌套块内的 `var` 不发布，无法可靠分类的 cell 直接拒绝（Python 后端没有这些问题）。
6. JS 工具桥是 Hermes 自己的 allowlist 函数（`web_search(...)` 等），不是 omp 的 prelude 词汇；`display()` 视图
   与 `out://` 不产生。
7. 模型面没有 omp 的 eval 卡（cells/语言徽标/details）：Hermes 用 `execute_code` 的通用卡；这是刻意的
   footprint 取舍，不是“没做完”。

## 风险

| 风险 | 缓解 |
|---|---|
| chunk 帧破坏 `execute_code` | 只在请求带 `stream:true` 时启用；`stream=false` 走原路径；golden 测试（结果 JSON、key 顺序、raw passthrough） |
| 秘密被 chunk 边界切开 | 256B carry + 每 chunk 脱敏 + settle 整体脱敏；最终 `output` 权威 |
| 用户 eval 与模型 `execute_code` 不是同一个 kernel | `session_kernel_target` 单点派生 + 验收 1 的跨路径测试 |
| 回合中写行打乱顺序/触发 lease 冲突 | idle 直写；running 进 `pending_display_rows`，admission 开头 flush（ADR-3 D3 同一 primitive） |
| `$$` 行仍被模型看到 | `display_only` 在 `_rows_to_conversation` 解码前跳过；验收 6 断言请求前缀 |
| 同一会话第二个 `eval.exec`（cell）并发 | `eval_busy`；对应 D9 的「内核多 cell 并发」不移植项——kernel lock 之外再加 per-session 单飞 |
| 模型 execute_code 与用户 cell 同时来 | 同一 kernel lock 串行；eval 的 reply 先返回、组件先出现，cell 按到达顺序跑，不报错 |
| 远程/沙箱后端下“共享”不成立 | D2 第 5 条直接拒绝 `unsupported_terminal_backend` |
| 契约/面板门失败 | 新方法必须有 authority handler（`tests/tui_gateway/panels/test_panel_coverage.py:44-58`）；跑两个生成器 |
| 大 code/output 撑爆 metadata | code ≤256KB、output ≤50KB（核上限），超限拒绝/截断 |
| JS 变换器误改 cell | tokenizer 只在深度 0 且能分类时改；其余 `unsupported_js_construct` 拒绝；对抗用例 |
| Bun 启动读 project `bunfig.toml` 的 preload | 以 staging cwd 启动、init 后 chdir（本机验证 `--config` 不能阻止，staging 启动可以） |
| Bun 自动安装缺失依赖 | `--no-install`；缺依赖的 import 直接失败 |
| JS 与 Python 语义差（无 `node:vm`、import 解析） | 间接 eval + `createRequire`；不承诺 vm 隔离 |
| `.ts` 资源漏打包 | `package-data tools` 增加两个文件 + 打包验收（sealed venv 里跑 JS cell） |
| `execute_code` 结果加字段破坏契约测试 | additive 字段 + 更新 pin 的 schema/key-order 测试；`language` 缺省时行为逐字不变 |
| Desktop/消息平台把行画成 user 文本 | 诚实降级；本 ADR 不设计其它客户端，验收只覆盖 omp-ui |
| 新增 core 工具改变工具表 | 一次性的 prompt-cache 失效（合并时发生一次）；会话内工具面固定，不中途增删 |

## 验收

1. 新本地会话先 `$x = 41`，再让模型调用 `execute_code("print(x + 1)")` → 打印 `42`，结果里 kernel `reused`
   为真；反向顺序同样共享。
2. `$` 一个产生 5 行的 cell：组件在 `eval.complete` 之前就逐块显示输出，结束显示 `(exit 0)`，行落库；
   `session.resume` 后同一块按 `pythonExecution` 还原（code/output/exit/cancelled/meta 一致）；空输出 cell 的
   正文是 `(no output)`（没有 `Output:` 标签）。
3. 抛异常的 cell：组件显示 traceback 与 `(exit 1)`；行正文以 `Execution failed with code 1` 结尾；模型下一轮
   看到该用户消息。
4. 运行中 Escape：组件 `(cancelled)`，`eval.complete.cancelled=true`，kernel 被杀，下一次调用是新 kernel
   （`state_reset` 可见）；行仍落库。
5. 超时：与 4 同形，状态 `error`，正文含超时说明；kernel 被丢弃。
6. `$$` cell：组件正常显示（dim/prompt），但 `get_messages_as_conversation` 不含该行，下一轮模型请求的
   消息前缀里没有它；`session.resume` 的 display projection 里有它。
7. 回合流式中 `$`：cell 正常执行并实时显示；行在**下一个** admission 开头落库，位置在上一轮持久化行之后、
   新 user 行之前；当前回合的消息/usage 不变。
8. 第一个 cell 未结束时用**另一个** `execution_id` 再次 `eval.exec` → `eval_busy`，无新事件、无新行；
    用同一个 id 重试 → 返回原 id，不跑第二份。
9. 非本地 source（Telegram/API/A2A）、history mount、`TERMINAL_ENV=docker|ssh`、draining owner →
   分别得到 `permission_denied`/`session_read_only`/`unsupported_terminal_backend`/`runtime_draining`，不 spawn、不落行。
10. `execute_code` 回归：schema 文本、结果 JSON 字段与 key 顺序、64KiB 结果边界、raw fd passthrough、
    `test_code_execution.py` 全绿；`stream:false` 下 kernel 子进程行为不变。
11. 契约门：`tests/tui_gateway/panels/test_panel_coverage.py`、`scripts/gen_gateway_contracts.py --check`、
    `scripts/gen_panel_inventory.py --check` 全绿（含生成后的 `gateway-contract.generated.ts`）。
12. 客户端单测：`$ ` / `$$ ` 进入（`$$` 排除上下文）、`$HOME`/`$!`（无空白）仍是普通输入、`$ echo hi`
    （粘 shell prompt）仍提交给模型；`display_kind="eval_execution"` → `pythonExecution`；`eval.output` 乱序按
    `seq` 丢弃；`adoptSnapshot` 期间重建运行中 cell。
13. `execute_code`（Python）：先 `$x = 41`，再 `execute_code(code="print(x + 1)")` → 输出 `42`、kernel
    `reused`；显式 `language:"py"` 与省略行为一致；结果 JSON 只多一个 `language` 字段。
14. `execute_code(language:"js")`：连续两个 cell 的顶层 `var`/`let`/`const`/`class`/`function` 通过 `globalThis`
    发布跨 cell 可见；**`let x = 1` 的 cell 之后，一个含顶层 `await` 的 cell 仍能读到 `x`**；顶层 `await`、
    按 session cwd 解析的静态 import 与 `require()`、TS 注解均可用；`reset` 后全部丢失；失败 cell 仍是
    `status="error"`、输出含 stack。
15. JS 变换器对抗：字符串/模板/regex/注释里的 `let`/`class` 不被改写；嵌套块内的声明不变；`export`、
    `import.meta`、顶层 `return`、无法分类的 `/` → `unsupported_js_construct`，且 cell 未执行；嵌套块内 `var`
    不跨 cell（已知差异，断言其行为）。
16. JS 工具桥：`await web_search({...})` 与 Python `hermes_tools.web_search` 同一 allowlist；预算耗尽后调用
    返回错误；未 allowlist 的工具被拒。
17. Bun 缺失：`language` 枚举只有 `py`；显式 `js` → 明确错误；`hermes pm install bun` 后下一次会话出现 `js`
    （工具定义缓存不跨会话误留）。
18. bunfig/安装安全：session cwd 的 `bunfig.toml` 含 `preload` 时 cell 不执行 preload；缺依赖的 import 在
    `--no-install` 下直接失败，不自动联网安装。
19. 审批与硬闸：`execute_code(language:"js")` 在非本地 viewer turn 弹一次批准，标签是 execute_code、heredoc 为
    `<<'JS'`；本地 viewer turn 自动批准；用户 `$` 不弹。JS cell 里
    `Bun.spawn(["launchctl","bootout","ai.hermes.gateway"])` 被硬闸拒绝（Python `subprocess.run([...])` 同款
    文本/标点归一化路径）。
20. JS 超时/取消：进程被杀、VM 状态丢失、输出带 force-kill 注释；下一次调用是新进程。
21. 打包：sealed wheel/bundle 安装后 `tools/eval_js_runner.ts`、`tools/eval_js_transform.ts` 仍在，
    `execute_code(language:"js")` 可执行（源码检出之外也能跑）。
