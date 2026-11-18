# ADR-4：`execute_code` 的 JS 后端与原生 eval 卡（不移植用户面 `$` eval）

**状态**：已决定（2026-11-01；**2026-11-05 修订**；**2026-11-07 D12/D11.6 修订**；
**2026-11-07 产品修订：完整 eval 面 + 流式 + `agent_out` 归档**）。
**修订结论：用户面 `$`/`$$` eval 仍不移植；模型面 `execute_code` 增加 `language`、`cells`、`title`、
`timeout`、`background`，实现 Bun JS 后端、D11 的全部 omp eval 面（views/statusEvents/images、agent/
workpool、speculation、多 cell、judge、`out://`、`%bun add`）、流式输出、完整 omp eval 卡与卡片标题生成；
截断使用既有字节元数据/归档，并由 `agent_out` 读取原始输出。**
实现状态：**P1–P5 已实现**（P1：`execute_code` 的 `language`/`cells`/`title`/`timeout`/`background`
入参、`py`/`js` 分派、逐 cell 审批/硬闸、多 cell 聚合与整调用 deadline；P2：`tools/eval_js_kernel.py`
的 Bun 子进程注册表/生命周期、`tools/eval_js_runner.ts` 的间接 eval 与工具 RPC 桥、
`tools/eval_js_transform.ts` 的 acorn AST 跨 cell 发布与 import/export/`import.meta` 改写、流式帧与
package-data；P3：`serve_rpc` 提取、D11.1 的 `display`/`status` host op 与 `display` 结果/`display_metadata`/
tool-row 投影、D11.2 的 `__agent__`/`__workpool__`/`__wait__`/`__send__`/`__cancel__` per-session handle、
D11.3 的流式只读 `read_file` 投机与 fingerprint/generation claim、D11.5 的 `__completion__`、D11.7 的
managed `%bun add` 与 `%environment project` 拒绝、eval host state 的 `clear_session`/atexit 回收；
P4：D14 的 kernel stream 帧（Python tee + JS `cell_index`）与 `tool.output` 独立事件通道、`tools/eval_jobs.py`
per-session async job（`background` 返回 `async`，流式/终帧按 `toolId`/`jobId` 填充同一卡，cancel/timeout
只结算一次并随 `clear_session`/atexit 回收；timeout/interrupted 终态为 `failed`）、D11.4 多 cell 同语言顺序/
跨语言并行（worker 继承 turn context 并读取提交线程的 interrupt bit）、D11.6 `__out__` 回合内 reply sink
（`out()`/`out://`，sink 每次调用重读、turn 结束后拒绝）、D15 的 `stdout_lines_*` 与 `agent_out(
mode="artifact", kind="tool")` 读取主会话 tool-output archive；`stdout_spill_path` 回退仅限
`execute_code` 行且路径在 profile cache/exec 或 kernel staging 内；契约与面板清单已重新生成；
P5：`tools/eval_card_metadata.py` 的 `derive_title` 标题（canonical 与 stdio 两条网关路径都在
`tool.start` 携带 `title`/`cell_titles`、发 `tool.title`、把 `display`/`card_title`/`cell_titles` 以
`display_metadata` 顶层持久化并经 `_tool_row_message` 投影）、omp-ui 的 `execute_code → eval` render key、
`executeCodeDetails`（多 cell/语言/状态/exit/duration/views/images/`meta.truncation`/`async`/`notice`）、
`tool.output` 50ms/4KiB 合并的 live partial、`tool.title` 的 per-call/cell 标签、async 卡驻留、resume 同源
投影与 `include_tool_content:"none"` 的 no-cells 分支、64KiB 降级经 `session.history` 回拉重建；面板清单把
`tool.output`/`tool.title`/`details` 登记进 tool_execution。sealed-wheel 安装后的 JS cell smoke（验收 13）
仍随打包门验证。P3/P4 的宿主机桥/display/managed-Bun/
投机/流式/job 用例与 `tests/tools/test_eval_js_kernel.py`、`tests/tools/test_eval_jobs.py` 在本机通过，
Python 与 JS kernel 的真实 cell smoke 已手工通过；`tests/tools/test_code_kernel.py` 在本机被真实 HOME
tripwire 阻断，仍需 CI 或隔离环境复核）。分阶段：**P1** `execute_code` 的 `language`/`cells`/`title`/`timeout`/`background`、py/js 分派与
审批/硬闸；**P2** 完整 JS kernel（Bun 子进程 + 解析器级跨 cell 变换 + 无函数限制）；**P3** 工具桥
（`serve_rpc` 提取）、D11.1/2/3/5/7 与 kernel 生命周期/清理；**P4** 流式、多 cell、async job 与 D11.4/6；
**P5** 完整 omp eval 卡、卡片标题、截断 warning 与 `agent_out` 归档、打包/回归。
用户面 eval 仍无 producer；`execute_code` 的完整卡只在 omp-ui，其它客户端保持通用工具卡。实现清单按
D1–D15 逐条覆盖：D1/D2–D7 是贯穿各阶段的负向门，D11 与 D13–D15 都实现。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§0.3、§2.3.2、§2.3.2.1；ADR-10（D8.2a render key、
D4/D5 工具行投影）；ADR-11（D11.5 的 eval 侧 `__judge__`/`__judge_batch__`/`__completion__` 接缝）、
ADR-12（D10 prelude 边界、D11.2 handle 接缝）；
`tools/code_execution_tool.py`、`tools/code_kernel.py`、`tools/code_execution_rpc.py`、
`tools/code_execution_env.py`、`tools/approval.py`、`model_tools.py`、`pyproject.toml`；
客户端 `omp-ui/src/session/agent-session.ts`、`omp-ui/src/modes/rpc/gateway-session.ts`、
`omp-ui/src/modes/utils/ui-helpers.ts`、`omp-ui/src/modes/controllers/event-controller.ts`、vendored
`pi-tui/src/tools/eval.ts`；
PM 的 `bun` 包（`pm/packages.py::Bun`、`pm/lock.json` 固定 1.4.4；`hermes pm install bun`；`hermes omp` 也用它）。

**范围**：模型面 `execute_code` 与其 kernel（canonical local gateway 的 turn 内工具调用），D11 的完整 omp eval
面，以及 omp-ui 的**完整显示映射**（render key + `EvalToolDetails` 投影 + 流式 partial + 卡片标题 + 截断
warning + resume）。本 ADR 会新增 additive wire（`execute_code` 结果字段、`tool.output`/`tool.title` 事件、
`display` details），但不新增用户面 `$`/`$$` 输入入口、eval transcript 行或 `eval.*` 方法。消息平台、Desktop、
Ink sidecar、ACP 继续用通用工具卡（未知事件忽略，未知 details 字段忽略）。`execute_code` 在 omp-ui 里用
omp 原生 eval 卡的完整数据模型（多 cell、live partial、title、async、statusEvents、views/images、meta），
其它客户端保持通用工具卡。

**归属标记（ownership）**：**H** = Hermes 100% 自有（代码、契约与生命周期都在本仓库，可单方面修改）；
**O** = 复用 omp（vendored 渲染器或 omp 的 details/类型契约，改动受适配边界约束）；**R** = 仅登记接缝
（本 ADR 不使用；保留标记以兼容历史引用）；**X** = 已撤销或明确不实现。

| 项 | 归属 | 说明 |
|---|---|---|
| `execute_code` 的 `language`/`cells`/`title`/`timeout`/`background`、py/js 分派、结果 JSON/截断元数据 | **H** | `tools/code_execution_tool.py`、`model_tools.py`；不依赖 omp |
| Python session kernel、审批/硬闸、中断/回收、流式帧 | **H** | `tools/code_kernel.py`、`tools/approval.py` |
| JS kernel（Bun 子进程、解析器级跨 cell 变换、全部函数形态、结果收尾） | **H** | 代码与生命周期在 Hermes；设计参考 omp，运行时不依赖 omp |
| 工具桥 allowlist/预算/`CellAuthority`、D11.2/3/5/7 | **H** | `tools/code_execution_rpc.py` 与 kernel 侧 prelude |
| D11.1 `views`/`statusEvents`/`images`、D11.4 多 cell、D11.6 `out://` | **H** | 结果 `display` 块、多 cell 调度、回合内 reply sink |
| `agent_out` 的 artifact/tool 模式、截断原文读取 | **H** | `tools/agent_out_tool.py`、tool-output archive |
| 卡片标题生成（会话 `derive_title` 同一确定性方法） | **H** | `agent/title_generator.py` + tool-result metadata |
| 打包/生成器/文档 | **H** | `pyproject.toml`、`scripts/gen_*` |
| eval 卡渲染器（`evalToolRenderer`、`renderCodeCell`、`runFoot`） | **O** | vendored `pi-tui`；不复制、不修改 |
| `EvalToolDetails`/`EvalCellResult`/`EvalLanguage`/`OutputMeta` 与 renderer 语义 | **O** | omp 的 details 契约；客户端只投影，不重画 |
| render key、`executeCodeDetails`、流式 partial、标题投影、resume、64KiB reconcile | **H** | `omp-ui/src` 适配层 |
| `$`/`$$` 用户面、`eval.*` 方法/事件/行 | **X** | D1 不实现 |
| D11 其余项（views/statusEvents/images、agent handle、speculation、多 cell、judge、`out://`、`%bun`） | **H** | 本 ADR 全部实现，不再只登记 |

**修订记录（2026-11-05）**：2026-11-01 版把用户面 `$` cell 也列为移植目标（`eval.exec`/`eval.cancel`、
`eval.*` 事件、`eval_execution` 行、`$$`/`display_only`、chunk 流、客户端入口）。本修订**撤销该半边设计**，
只保留 `execute_code` 的 JS 后端。代码事实：`omp-ui/src` 没有 `$` 入口（`input-controller.#submitText` 只分发
`/` 与 `.`/`c`；`parsePythonCommandInput`/`pythonCommandPrefixLength`/`EvalRunner`/`evalExec`/`evalCancel`/
`isEvalRunning` 全树 0 命中）；`ROW_PROJECTORS.python_execution` 是 `undefined`
（`omp-ui/src/modes/rpc/gateway-session.ts:275-288`），`ui-helpers.ts` 的 `pythonExecution` 分支（`:202`）
没有 producer；网关没有 `eval.*` 方法/事件，本地执行 RPC 只有不进 transcript 的 `shell.exec`
（`gateway/session_shell.py`）。撤销前的设计全文见 `git show 5e91617af0:docs/adr/ADR-4-eval-execution.md`。
**同修订追加 D12**：模型面在 omp-ui 复用 omp 原生 eval 卡（单 cell），保证原生显示；用户面入口仍不移植。
（该单 cell 结论已被 2026-11-07 产品修订取代。）

**修订记录（2026-11-07，D12 收口 + D11.6 撤销；已被同日产品修订取代）**：D12 原稿把 `TruncationMeta` 的行数当显示前提，给工具结果加了
`stdout_lines_total`/`stdout_lines_shown`——这是**模型可见的结果变更**，不是显示移植。本修订撤销该字段：
截断只做客户端 notice 近似（真实字节事实），不建 `meta`；`tool.complete.result` 超 64KiB 的降级形态改为
客户端用既有 `session.history(after=<row_id>)` 增量回拉完整行后重建，不再显示转义 JSON。状态映射改为结果
JSON 的真实词表（`success`→complete）；渲染器数据模型里没有 producer 的元素（live partial、多 cell、`title`、
async、`statusEvents`、`views`/`images`）统一标 out of scope，不在本 ADR 补 producer。受影响：D12、协议、
实现清单、风险、验收 2/15/16/17/18/19/20；D11（导语与第 6 项）、顶部归属标记表。**同修订：D11.6 `out://`
→ 回合内 reply sink 当时决定不实现**（已被同日产品修订推翻；D11.6 现在实现）。同时新增“归属标记”：H = Hermes 100% 自有、
O = 复用 omp、R = 仅登记接缝、X = 未实现/撤销。

**修订记录（2026-11-07，产品修订：完整 eval 面）**：同日的“单 cell / 不流式 / D11 只登记 / 截断只做
客户端近似”结论被撤销。本 ADR 现在要求：D11 全部实现；`execute_code` 支持 `language`、`cells`、`title`、
`timeout`、`background`，卡面显示多 cell；`execute_code` 流式输出（kernel chunk frames + `tool.output`）；
eval cell 与普通工具卡的标题复用 `agent.title_generator.derive_title`（确定性、无模型）；截断使用既有
`stdout_bytes_*`/`stdout_spill_path` 与 tool-output archive，`agent_out` 读取原始输出；JS 跨 cell 声明完整、
对函数形态无限制。D1（用户面 `$`/`$$` 不移植）不变。受影响：状态、归属、D2–D7、D8–D12、D13–D15、协议、
实现清单、不采纳、风险、验收。

**取代关系（逐条，避免文档互斥）**：

- plan §2.3.2.1 第 454 行（Eval 执行）把状态写成“部分”，并假定网关有共享 kernel。本 ADR **不再取代**该行、
  也不实现用户面：该行按本 ADR 改为“无 producer（omp-ui 无 `$` 入口；网关无 eval 方法）”。2026-11-01 版的
  “由 D3 取代”随之撤销。
- plan §0.3 第 96 行把 `eval/` 列入“随本机业务代码删除”：不变。本 ADR 不恢复任何本机 eval 业务代码
  （`omp-ui/src/eval/**`、`EvalRunner`、`handlePythonCommand` 都不回来）。
- plan §2.3.2 第 412 行的 `eval-execution.ts` 渲染器描述不变，但它不再只是残留：D12 让 `execute_code` 通过
  render key 复用 `evalToolRenderer` 的完整数据模型（多 cell、live partial、title、async、statusEvents、
  views/images、meta）；用户面仍不产生 `pythonExecution` 行、不新增输入入口。
- 2026-11-05 版曾把“模型面没有 omp 的 eval 卡”列为取舍：**由 D12 取代**——完整原生卡移植，multi-cell /
  async job / `title` / `statusEvents` / `views`/`images` 全部实现（D11.1 与 D13–D15）。
- **不新增 `eval` 模型工具**：保持不变。JS 在 omp 只从 eval 工具面可达，而 Hermes 已有同一“持久 kernel +
  工具桥”的模型面入口 `execute_code`；D8 给它加 `language`/`cells`/`title`/`timeout`/`background`，不在每次
  API 请求里多带一个重叠的核心工具（footprint ladder：扩展既有代码优先）。
- ADR-2 的行/事件惯例、ADR-3 的 `append_display_row`/pending flush 仍**不用于 transcript 行**：本 ADR 只在
  既有工具事件通道上新增 `tool.output`/`tool.title`，不写 transcript 行，因此不依赖也不阻塞那两个 primitive。
- ADR-11 D11.5、ADR-12 D10/D11.2 引用本 ADR 的编号保持有效；D11 现在全部实现（D11.4 不再是撤销项），
  D11.2/D11.5 的 eval 侧接缝由本 ADR 落地。

## 问题

omp 的 Eval 执行有两个面。本 ADR **只移植模型面**：

1. **用户面（不移植）**：omp 的 shell 把 `$code`/`$$code` 直接交给本机 `EvalRunner`，完成后把
   `pythonExecution` 消息追加进 transcript（历史源码 `git show 5bd7dabd213^:omp-ui/src/session/eval-runner.ts`；
   输入解析在历史 `input-controller.ts:217-248`）。**本仓库的 omp-ui 没有这个面**：
   - 输入：`omp-ui/src/modes/controllers/input-controller.ts` 的 `#submitText` 只处理 `/` 命令、`.`/`c`
     续跑与普通 prompt；没有 `$` 前缀解析。
   - 运行器/RPC：`omp-ui/src` 全树没有 `EvalRunner`、`evalExec`、`evalCancel`、`isEvalRunning`。
   - 网关：没有 `eval.*` canonical/sidecar 方法或事件；唯一相近的 `shell.exec` 是 local-only 的
     request/response，没有 kernel、没有流、不进 transcript（`gateway/session_shell.py` 的 docstring）。
   - 渲染：`ui-helpers.ts` 保留（vendored 的）`pythonExecution` 分支与 `EvalExecutionComponent`，但
     `GatewayOmpSession.rowToAgentMessage` 的 `ROW_PROJECTORS` 把 `python_execution` 映射成 `undefined`
     （`omp-ui/src/modes/rpc/gateway-session.ts:275-288,563-572`），所以这条分支没有 producer。
   因此用户面在这里不是“缺一块”，而是**没有消费者**：照 2026-11-01 版实现会新增 canonical 方法、三类事件、
   一种行型、`display_only` 模型上下文标记与客户端输入解析，全部只对一个不存在的入口服务（D1）。

2. **模型面（移植）**：omp 的 eval 工具接受 `{language:"py"|"js", code, title?, timeout?, reset?}`
   （历史 `git show 5bd7dabd213^:omp-ui/src/tools/eval.ts`），Python 与 JavaScript 都在持久 kernel 里，工具卡
   由 pi-tui 的 `evalToolRenderer` 渲染。下文 `pi-tui/…` 指 vendored 的
   `omp-ui/node_modules/@oh-my-pi/pi-tui/src/…`；`eval/…`/`tools/…`/`prompts/…` 指 omp 源码 checkout
   （取证口径同 ADR-11），本仓库历史版本用 `git show 5bd7dabd213^:<path>`。历史源码：
   - 两个后端返回同一形状 `{output, exitCode, cancelled, truncated, totalLines, …, displayOutputs}`
     （`eval/py/executor.ts`、`eval/js/executor.ts`），`onStatus` 状态事件；
   - JS 后端是**常驻 Bun 子进程** + `WorkerCore`（`eval/js/worker-core.ts`、`process-entry.ts`），
     **刻意不用 `node:vm`**：`eval/js/shared/indirect-eval.ts` 记录 Bun 在 `Worker.terminate()` 打断
     `vm.runInContext` 时会 SIGTRAP，因此用户代码用间接 `eval` 在 worker 全局作用域执行；
     `shared/rewrite-imports.ts`（Babel）把顶层声明“降级/发布”以跨 cell 存活，并把静态 import 改成动态
     import；`tool-bridge.ts` 把工具调用转给宿主；
   - kernel 身份按 `sessionId` + `kernelOwnerId` 保留，Python/JS 各一套。

Hermes 的承载面是 `execute_code`：它已经是“持久 kernel + 工具桥”的模型入口。
`tools/code_kernel.py` 维护每会话一个 `SessionKernel`（key = `(_resolve_owner(task_id), mode, child_python,
child_cwd, sorted(sandbox_tools))`，`code_kernel.py:811`），子进程用 `exec` 在持久 `GLOBALS` 命名空间跑 cell
（`RUNNER_CELL_SOURCE`，`:47-74`），有工具 RPC、超时/中断杀 kernel、`reset`、LRU/空闲回收
（`:359-372,624-656,717-731`）。结果是一个 JSON 字符串（`status/output/exit_code/stdout_truncated/...`，
`code_execution_tool.py:56-81,738-805`）。它没有语言参数，跑不了 JS；JS 运行时由 PM 提供
（`pm/packages.py::Bun`，可选安装，`pm/lock.json` 固定 1.4.4）。

因此本 ADR 决定：给 `execute_code` 增加 `language`/`cells`/`title`/`timeout`/`background`（D8）与完整 JS
kernel（D9），实现 D11 的全部 omp eval 面、流式/多 cell/async（D14）、卡片标题（D13）、截断/`agent_out`
归档（D15），并在 omp-ui 用完整原生 eval 卡显示（D12）；不新增独立工具、不新增用户面 `$` 入口（D1）。

## 决策

### D1. 用户面 eval 不移植（`$`/`$$`）（X）

- 不新增 `eval.exec`/`eval.cancel` canonical 方法，不新增 `eval.start`/`eval.output`/`eval.complete` 事件，
  不新增 `eval_execution` 行或 `display_only` 元数据，不新增客户端 `$`/`$$` 解析、`evalExec`/`evalCancel`、
  `EvalExecutionComponent` 的 live/pending 挂载。
- 不新增 `/eval` slash 命令；`commands.catalog`/`complete.slash` 不变。
- 现有座位保持原样：`TranscriptRole.python_execution`（`tui_gateway/contracts/transcript_roles.py:51`，
  `seated`、`fields=code/output/language`、`needs` 写 `$` 模式）不翻转成 `produced`、不加
  `ROW_PROJECTORS.python_execution`、不写该行；`session.history`/`session.resume` 不会返回它。
- 不新增 `eval` 模型工具：模型面的 JS 走 `execute_code(language:"js")`（D8）。
- 理由：入口不存在（见“问题”的证据），新增的 RPC/事件/行没有消费者；omp-ui 的 shell 已按 §0.3 删除本机
  业务代码，`ui-helpers.ts` 的 `pythonExecution` 分支是 vendored 残留；用户仍可用 `!cmd`/terminal
  （`shell.exec`）与模型面 `execute_code` 完成同类操作。
- 重新评估的条件：omp-ui 重新出现真实的 `$` 入口（输入解析 + 提交路径 + 消费者）时，另立 ADR 定义
  RPC/事件/行；本 ADR 不预留半成品（不做“只有网关没有客户端”的接口）。

### D2–D7. 用户面撤销；D4/D6 的模型面部分恢复

2026-11-01 版的 D2–D7 以用户面 `$` 为前提，随 D1 撤销；编号保留。D4 的“流式”和 D6 的“截断元数据”在
`execute_code` 的模型面重新立项（D14/D15），但都不恢复 `$` 的 RPC/事件/行。

| 项 | 原内容 | 结论 |
|---|---|---|
| D2 | `$` 的授权/闸门顺序、local viewer 判定、单飞、线程绑定 | **X**：仍撤销；`execute_code` 走既有 `check_execute_code_guard` + 硬闸（D8） |
| D3 | `session_kernel_target(...)`/`run_session_cell(...)` 拆分，让 `$` 与 `execute_code` 共享 kernel | **X**：仍撤销；多 cell/流式在 D14 内做，不拆用户面路径 |
| D4 | kernel `stream:true`、chunk sentinel/tee、`eval.output` 节流 | **部分恢复**：`execute_code` 流式由 D14 实现；事件是 `tool.output`，不是 `eval.output` |
| D5 | `eval.*` 事件、`eval_execution` 行、`pending_display_rows` flush、模型文本 | **X**：仍撤销；只用现有工具事件通道，不写 transcript 行 |
| D6 | `TruncationMeta` 映射与 `$$` 的 `display_only` 标记 | **部分恢复**：`$$`/`display_only` 仍撤销；`TruncationMeta` 由 D15 以 `execute_code` 原生卡消费者恢复 |
| D7 | 客户端 `$`/`$$` 输入、`composerState().shell`、`#evals`、replay 投影、快照期重建 | **X**：仍撤销；客户端只做 D12/D13/D14 的完整卡投影 |

### D8. `execute_code` 的完整入参/出参（H）

- schema（additive）：
  `{code?, language?: "py"|"js" = "py", cells?: [{code, language?, title?}], title?, timeout?, reset?, background?}`。
  - `code` 与 `cells` 二选一；`code` 是单 cell 简写，`cells` 是最多 N 个 cell 的数组（N 取
    `code_execution.max_cells`，默认 16）。
  - `title` 是可选人工标题；缺省由 D13 的标题生成器产生。`timeout` 覆盖本次调用的总 deadline（秒，受
    `code_execution.timeout` 上限约束）；`background` 请求 async job（D14）。
  - `EXECUTE_CODE_SCHEMA` 仍不在 import 时读 PM；`model_tools._rewrite_execute_code` 检查
    `pm.installed_package("bun")`，把 `js_available` 传给 `build_execute_code_schema(...)`。无 Bun 时
    `language` 枚举只有 `["py"]`，`cells[].language` 同样只有 `py`；有 Bun 时加 `js`。`cells`/`title`/
    `timeout`/`background` 与 Bun 无关，始终出现在 schema。
- handler：
  - 校验 `code`/`cells` 恰好一个；`cells` 非空且不超上限；每个 cell 的 `language` 归一化为 `py`/`js`，
    未知值 `tool_error`。`js` 但 Bun 缺失 → `tool_error("JavaScript needs Bun: hermes pm install bun")`；
    `js` 但 terminal 后端不是本地（`env_type != "local"`）→ `tool_error`，不 spawn、不落记录。
  - 单 cell：沿用现有 `execute_in_session_kernel` 路径；结果保留现有 key 顺序，新增 `language`（显式时）、
    `stdout_lines_*`（D15）和流式元数据；`title` 只在模型显式传入时回显，生成值走 `display_metadata`。
  - 多 cell：按 `cells` 顺序执行；`py`/`js` 各自进入对应 kernel。结果新增 `cells: [{index, code, language,
    title?, status, output, exit_code, duration_seconds, stdout_*, display?, views?, statusEvents?}]`，
    顶层 `images?`；`title` 只在模型显式传入时回显，生成值走 `display_metadata`。顶层 `status` 是聚合
    （任一 error → error，timeout/interrupted 优先），`output` 是各 cell 输出的拼接摘要。
  - `reset`：清本次涉及语言的 kernel（`py` 现有行为；`js` 杀 Bun 进程）。`timeout` 作用于整次调用；每个
    cell 的结果带自己的 `duration_seconds`。`background` 由 D14 的 job manager 处理，先返回
    `status:"running"` + `async:{state:"running", jobId, type:"eval"}`。
  - `code` 为空/`cells` 非法时文案随语言（不再固定说 Python）。
- 审批与硬闸：`check_execute_code_guard(..., language=)` 按每个 cell 的语言生成 heredoc 标签
  （`<<'PY'`/`<<'JS'`）与围栏语言；先跑既有硬闸（`SANDBOX_AVAILABLE`、`enforce_no_refusal`、gateway
  生命周期/宿主解释器 kill 的文本匹配），再走批准闸；本地 viewer turn 内自动批准。`Bun.spawn([...])` 走与
  Python 相同的文本/argv 归一化路径（`cron/lifecycle_guard.py`）。
- 工具面：`execute_code` 的 core/`code_execution` 成员身份不变，不新增工具、不改 toolsets；结果里的
  `cells`/`title`/`display`/`async` 由 D12–D15 的消费者使用，不改变工具身份。

### D9. JS 后端：PM Bun 子进程上的解析器级 kernel（H）

- 后端：`js` 只在本地 terminal 后端可用（与 D8 的 `env_type != "local"` 判定同源）；非本地后端在分派前
  fail closed，不 spawn Bun、不触达宿主文件系统。
- 运行时：只认 PM 的 bun（`pm.installed_package("bun").binary`，`pm/lock.json` 固定 1.4.4）。缺失时
  `language`/`cells[].language` 的枚举不含 `js`；显式传 `js` 返回
  `tool_error("JavaScript needs Bun: hermes pm install bun")`。不读 `PATH`/`~/.bun`，保证锁定版本。
- 进程：`tools/eval_js_kernel.py`（新）为 `(owner, "js", bun_path, child_cwd)` 维护一个 Bun 子进程，与
  `tools/code_kernel.py` 并列（Python 的 `SessionKernel` 绑定 Python runner，不复用）；同样的 idle/LRU 上限
  （`code_execution` 配置）、`shutdown_js_kernels_for_owner`、atexit、
  `spawn_owned(..., purpose="eval-js-kernel")`（`hermes_cli/process_identity.py:1076`）。
  `shutdown_js_kernels_for_owner` 接进 `tools/approval.clear_session`。
- 启动：`bun --no-install tools/eval_js_runner.ts`，**cwd = kernel staging dir**，env 复用
  `_scrub_child_env` 并额外去掉 `NODE_OPTIONS`/`BUN_INSTALL`/`BUN_INSTALL_CACHE_DIR`、把
  `BUN_INSTALL_CACHE_DIR` 与 `HOME`/`USERPROFILE` 指到 staging。runner 收到 init 后
  `process.chdir(session_cwd)`：project 的 `bunfig.toml`（含 `preload`）不会被读——设计期实测：默认会读、
  `--config` 不能阻止、而“staging 启动后再 chdir”不会——cell 的相对路径仍按 session cwd 解析。
  `--no-install` 关掉 Bun 默认的自动安装（`--install=auto`）。runner 另把
  `globalThis.require = createRequire(session_cwd + "/cell.cjs")` 暴露给 cell（`require()` 与 `import()` 都按
  session cwd 解析）。
- 生命周期：runner 每 2s `process.kill(process.ppid, 0)`，宿主消失即退出（POSIX 由 `spawn_owned` 的
  owner-death 进程组兜底，Windows 由 job object 兜底）。
- 执行模型：**间接 eval**（`globalThis.eval`）在 runner 全局作用域执行 cell；不用 `node:vm`：omp 记录了
  Bun 在 `Worker.terminate()` 打断 `vm.runInContext` 时的 SIGTRAP（`shared/indirect-eval.ts`），且 Bun 1.4.4
  的 `vm` 缺 `importModuleDynamically`（设计期实测动态 import 报
  `ERR_VM_DYNAMIC_IMPORT_CALLBACK_MISSING`）。cell 需要顶层 `await`/`return` 时包成 `(async () => { … })()`
  并 `await`；普通 cell 直接用间接 eval，不额外限制函数形态。
- 变换（`tools/eval_js_transform.ts` + vendored `tools/eval_js_parser.ts`，随 runner 发货）：
  - 先用 `Bun.Transpiler({loader:"ts"})` 统一 TS/JS，再用 `acorn`（vendored 单文件解析器）生成 AST；
    **不再使用 tokenizer/fail-closed 列表**，regex/除法、模板、注释、嵌套块由解析器区分。
  - 顶层 `const`/`let`/`var`/`class`/`function`（含解构、async/generator、箭头函数）在声明后发布到
    `globalThis`；只有顶层 `var` 发布，块内/函数内部的 `var` 保持 JS 词法作用域，不做跨 cell 发布
    （标准 JS 语义，不是人为限制）。
  - 顶层静态 `import`（default/named/namespace/side-effect）改写为 wrapper 内动态 `import()` 并发布绑定；
    解析用 `createRequire(session_cwd + "/cell.cjs")`；`node:`/绝对/`file:` 直接 import。
  - 顶层 `export` 去掉导出关键字并发布绑定；`import.meta` 替换为 `{url, dir, filename}`；顶层 `return`
    放进 async wrapper（仅该 cell）；不再拒绝 `globalThis`/`undefined`/`NaN`/`Infinity` 等名字，按 JS 语义处理。
  - 后续 cell 的裸标识符命中全局对象上的发布绑定；不改写引用，也不把 `let`/`const` 降级成 `var`。
  - 解析失败就是语法错误（`status:"error"`），不是“未支持的构造”；任何函数声明/表达式/闭包都不被拒绝。
  - 这是相对 omp 的明确范围：用 parser 做完整跨 cell 变换，不 vendor Babel 生成器；只保留源码范围插入，
    避免重排用户代码。
- 流式：runner 在 cell 执行期间把 `console.*`/`process.stdout.write`/fd 输出写成
  `{"type":"stream","id","stream":"stdout"|"stderr","text"}` 帧（`HERMES_JS_SENTINEL` 前缀）；宿主
  `_stdout_reader` 分发到 D14，最终结果帧仍是权威。`process.exit()` 直接结束 runner：宿主看到 EOF →
  `status="error"`、kernel 丢弃、下次调用换新进程。
- 结果：runner 回 `{status:"ok"|"error", stdout, stderr, exit_code, execution_count}`；host 复用
  `code_kernel.py::_cell_result` 的收尾（脱敏/截断/`--- stderr ---`/timeout 文案，必要时抽共用 helper），
  key 顺序与 Python 逐字一致；显式传参时结尾加 `language`。**host 必须把 runner 的 `ok` 归一成结果 JSON 的
  `success`**（`error` 保持；`process.exit()` 的 EOF 仍归 `error`）。
- 多 cell：`run_js_cell` 仍一次一个 cell；D8 的 `cells` 循环按顺序复用同一 kernel，每个 cell 独立结果。
  `background` 由 D14 job manager 持有；跨调用的同一 JS kernel 仍由 kernel 锁串行，卡面多 cell 不依赖
  跨调用并发。
- D11 的 `views`/`statusEvents`/`images`、`agent()`/`wait()`/`workpool()`、`judge()`/`completion()`、
  `out://`、`%bun add`、speculation/shadow **全部由 D11 实现**，JS runner 通过 D10 的 RPC 暴露。
- 隔离：间接 eval 不是安全边界；JS cell 与 Python cell 一样拥有宿主机能力（Bun `spawn`/`fetch`），闸门与
  Python 完全一致（D8 的批准/硬闸/后端限制），env scrub 如上；不承诺沙箱。
- **打包**：`pyproject.toml` 的 `[tool.setuptools.package-data] tools` 必须包含 `eval_js_runner.ts`、
  `eval_js_transform.ts`、`eval_js_parser.ts`；否则 JS 只在源码检出可用、打包安装静默坏掉。

### D10. JS 的工具桥与 prelude 范围（H）

- 复用 Python kernel 的 line-JSON RPC server（`tools/code_execution_rpc.py::_rpc_server_loop`）：把循环抽成
  可传入 owner 的 `serve_rpc(...)`，JS kernel 自己 bind socket/token；`CellAuthority` 的“每 cell 捕获、settle
  后 retire”照旧（`code_kernel.py:197-250,838,868`）。JS runner 用 `node:net` 连接
  `HERMES_RPC_SOCKET`/`HERMES_RPC_TOKEN`（UDS 或 `tcp://127.0.0.1:port`，与 `code_execution_tool._connect`
  同一套地址）。
- 预置：runner 收到工具名清单后定义 `globalThis.<name> = (args) => call(name, args)`，与 Python 的
  `hermes_tools` 同一 allowlist、同一每 cell 预算、同一拒绝/脱敏。调用在 runner 内单飞行排队（RPC server
  一次一条请求）。工具清单取该 session 已解析的工具面（`_sandbox_tools_for(enabled_tools)`，会话内固定）。
- 完整 prelude（D11）：`serve_rpc` 增加 `__display__`/`__status__`/`__agent__`/`__workpool__`/
  `__judge__`/`__judge_batch__`/`__completion__`/`__out__` 宿主 op；Python 的 `hermes_tools` 与 JS 的
  `globalThis` 同时暴露 `display()`/`status()`/`agent()`/`wait()`/`workpool()`/`judge()`/`judge_batch()`/
  `completion()`/`out://`。调用仍走 `CellAuthority`、allowlist、每 cell 预算；`agent`/`workpool` 句柄是
  per-session（非 per-cell），活过 cell；`judge_batch` 由 host 拥有整轮 run。`%bun add` 走 D11.7 的 managed
  环境，不写 project 依赖。

### D11. omp eval 完整面（本 ADR 全部实现）（H）

以下 1–7 全部在本 ADR 实现；它们共用 D10 的 RPC/prelude、D14 的流式与多 cell、D15 的 archive/`agent_out`。
D11.4 不再是“撤销项”，D11.6 不再“决定不实现”。

1. **`views`/`statusEvents`/`images` → 工具卡 details 通路**
   - 现状：D12 之后代码、输出、exit/duration 与截断提示已经原生；缺的是结构化展示载体——`execute_code`
     只回一个 JSON 字符串（`code_execution_tool.py:56-81`），里面没有 `views`/`statusEvents`/`images`，实时
     `tool.complete.result` 解出的对象也没有；快照 tool 行只带 `content`/`args`/`labels`/`is_error`/`row_id`，
     `TranscriptMessage` 没有 `display_metadata`（ADR-10 P5 已从 wire 删除，见
     `tui_gateway/contracts/common.py:187-216`）；顶层 `display` 已是 custom 行的 `bool`
     （`common.py:205`），不能拿来放对象。
   - 实现：工具结果加 additive `display` 块 `{images: ImageContent[], views: [{text, view}], statusEvents}`，
     实时路径直接读解码后的 result；持久化仍写 `display_metadata`（`append_message` 的自由字段，
     `hermes_state_messages.py:398-429`），由 `_tool_row_message`（`tui_gateway/session_history.py:438`）投影成
     `TranscriptMessage.details.display`（`details` 已存在，tool 行当前为 null；不碰顶层 `display` 的布尔语义）。
     同时在 `TRANSCRIPT_ROLES` 的 tool spec（`tui_gateway/contracts/transcript_roles.py:42`）登记 `details`，
     让 `runtime.describe.transcript_blocks[].fields` 广告它。客户端把 `display` 并进对应 cell 的
     `views`/`statusEvents`，图片进 `details.images`；由同一个 eval 卡按 `text` 匹配 `views[]` 渲染（与
     `EvalOutputPart`（`pi-tui/src/tools/eval.ts:771`）、`placeViews`（`tools/eval.ts:1100`）同形）。模型
     正文不变，`views` 只服务用户。
   - 验收：无 view 命中时退回纯文本；view 的 `text` 被截断/不在输出里时跳过而不是错位；图片走同一
     `ImageContent` 编码，不新增第二套；resume 后的卡与 live 同形；custom 行的 `display` 布尔语义不变；
     tool 块的 `fields` 只新增 `details`，其余不变。

2. **`agent()`/`workpool()` → eval 侧 handle 桥**
   - 现状：Hermes 现有委派是模型面工具调用——`delegate_task`（`delegation` toolset）与
     `agent`/`agent_out`（`agents` toolset），见 `toolsets.py:24-25,153-156`——结果回上下文，没有内核内
     可跨 cell 等待的句柄。并且 `CellAuthority` 每 cell 捕获调用线程身份与 prompt 回调、settle 后 `retire()`
     （`code_kernel.py:197-250,838,868`），迟到的 RPC 直接返回 `No active execute_code cell`（:228）。
   - 实现：`serve_rpc` 增加 `__agent__`/`__workpool__` 宿主 op，映射 `delegate_task`/`agent`/`agent_out`；
     handle 用 `agent://<id>` 标识，`status`/`wait`/`send`/`cancel` 各是一个 op。关键是 **per-session（非
     per-cell）句柄注册表**，owner 按 profile scope 绑定：后台 handle 必须活过 cell，迟到回调走会话级身份，
     不能复用已 retire 的 `CellAuthority`；budget/cancel 由 cell 的 runner 承载，注册表只管生命周期与授权。
     prelude 走 D10 的完整宿主 op：`display`/`status`/`agent`/`wait`/`workpool`/`judge`/`completion`/`out://`
     由 kernel init 一次下发、cell 之间不重发（会话内工具面固定）。
   - 验收：cell 超时/取消后 handle 仍可 `wait`/`cancel`；迟到回调不被 `No active execute_code cell` 拒绝；
     未 wait 的结果按既有自动投递语义交付。

3. **speculation/shadow → agent 流式工具参数事件**
   - 现状：工具只在完整参数到达后执行（`model_tools.handle_function_call`）；omp 的接缝是工具级
     `ToolSpeculationPolicy.stream`（`tools/eval.ts:445-470`）+ 参数增量解码
     （`eval/speculation/eval-args-stream.ts`）。
   - 实现：在 `handle_function_call` 之前增加工具级 `stream` 钩子（partial JSON + abort），eval 工具据此
     静态投影 `read(...)` 计划并投机执行；权威执行按 `(siteId, dynamicPath, name, fingerprint, occurrence)`
     claim 命中复用，miss 回退普通路径；kernel 快照（revision/digest）不符即丢弃 shadow cell
     （`eval/speculation/cell-session.ts:71-190`）。
   - 验收：claim 未命中时行为与无投机逐字节一致；副作用只来自已提交的调用；流式参数重启/改写即丢弃。

4. **多 cell → `execute_code` 的 `cells` 与同调用并发（H）**
   - 实现：D8 的 `cells` 数组；每 cell 独立 `code`/`language`/`title`；同语言共享 kernel 并按顺序执行，
     不同语言可并行；卡面 `details.cells` 显示全部 cell，每个 cell 有独立 status/duration/exit/
     views/statusEvents；`images` 在顶层 `details.images`。
   - 不引入用户面单飞/`eval_busy`；跨调用仍由 kernel 锁串行，避免同一 kernel 的状态竞态。`async` job 由
     D14 持有，卡面按 `details.async` 显示 background 徽标。

5. **`judge()`/`judge_batch()`/`completion()` → host 侧模型角色链 + 批量 run**
   - 现状：Hermes 的辅助模型选择在 `agent/auxiliary_client.py`（`resolve_aux_model`/`default_aux_model`
     角色梯），但没有“内核内一次调用跑分类/批量”的桥；omp 的接缝是 `eval/judgment-bridge.ts:273`
     （`sessionJudge`）与 `eval/judgment-batch-bridge.ts`。
   - 实现：`serve_rpc` 加 `__judge__`/`__judge_batch__`/`__completion__` op；`judge` 按 aux 角色链解析模型、
     校验 `questions` schema、图片按 `ImageContent` 传；`judge_batch` 由 host 拥有整轮 run（有界 fan-out、
     每项一次重试、`min_ok`），cell 侧 `drain` 分批取 settled；usage 记到与会话相同的账本。
   - 验收：cell 超时不取消 host 的 batch run；单 item 失败返回 `error` 不抛；角色/预算缺失报 `tool_error`
     而不是静默换模型。

6. **`out://` → 回合内 reply sink（H）**
   - 实现：`__out__` host op 写入当前 turn 的 reply sink；只在该 turn 活跃时可用，无活跃 turn 返回
     `tool_error`。内容走既有 reply/notification 路径，不新增 transcript 行、不改 bash/eval 输出路由。
   - `artifact://` 不新增：原始工具输出由 D15 的 `agent_out` 读取。

7. **`%bun add`/包安装 → managed 环境 + 锁（拒绝 project 级）**
   - 现状：PM 只提供 bun 二进制（`pm/packages.py:587` `Bun`），没有会话级依赖目录/锁；Hermes 现状是
     skill/terminal 安装，与内核内 `%bun add` 不是同一条路。omp 的接缝是 `eval/js/package-installer.ts`
     （managed/project 两种环境 + project lock）。
   - 实现：只做 **managed**——目录在 Hermes home 下按 session cwd 哈希分桶（`package.json` + lock + 文件锁），
     `%bun add` 是独立 cell（不重置 kernel），安装成功才提交环境；runner 继续 `--no-install`。
     **拒绝 project 级**：不写用户项目的 `package.json`/`node_modules`/锁文件，`%environment project` 返回
     `unsupported_environment`；改工程依赖仍走 skill/terminal 的显式操作。模块解析仍以 session cwd 为主、
     managed 目录作 fallback（对应 omp `packageRoot` 的次序），不改 D9 的 `createRequire`。
   - 验收：安装前后 session cwd 无新增/修改文件；`%environment project` 被拒；并发安装同 cwd 串行；缺包
     import 失败而不是隐式安装。

### D12. 完整 omp eval 卡：多 cell、流式、标题、async、结构化展示（H + O）

**目标**：`execute_code` 在 omp-ui 画 omp 原生 eval 卡的完整数据模型，而不是只画单 cell 的文本卡。用户面
`$` 仍不存在（D1）；本 ADR 不新增输入入口或 transcript 行，但会新增 additive wire（结果 `cells`/`display`/
截断字段、`tool.output`/`tool.title` 事件）来喂完整卡。渲染器、编辑器、组件仍是 vendored 的，不复制、不另画。

- **渲染器不复制**：卡面就是 vendored `pi-tui/src/tools/eval.ts::evalToolRenderer`（`toolRenderers.eval`，
  `pi-tui/src/tools/index.ts:36-56`）。本 ADR 只接 render key、数据与事件。
- **render key**：`TOOL_RENDER_NAMES`（`omp-ui/src/session/agent-session.ts:58-61`）加
  `execute_code: "eval"`；`getToolByName` 返回 `{name:"eval", label:"execute_code"}`，`toolRenderName` 用它选
  渲染器；wire 名仍留在消息/协议里（ADR-10 D8.2a），显示标题由 D13 生成并覆盖，不由 render key 决定。
- **调用面**：`tool.start.args` 是 `{code? , cells?, language?, title?, timeout?, background?, reset?}`；
  `getRenderCells`（`pi-tui/src/tools/eval.ts:176-190`）接受 `args.cells` 数组或单 `args.code`，每 cell 的
  `language` 映射 `"js"`→javascript、`"py"`/其余→python。render key 同时命中 `ToolExecutionComponent` 的
  `toolName === "eval"` 分支（`pi-tui/src/chat/tool-execution.ts:1875-1881`）。
- **结果面**：新增纯函数 `executeCodeDetails(args, decodedResult, opts)`（`gateway-session.ts`，live/resume
  共用）把结果 JSON 投成完整 `EvalToolDetails`：

  ```text
  language?: "python" | "js"          # 首个 cell / 兼容字段
  languages?: ("python" | "js")[]      # 按首次使用顺序
  cells: EvalCellResult[]              # 恒为 1..N；单 cell 结果也合成一个
  images?: ImageContent[]              # D11.1
  statusEvents?: EvalStatusEvent[]     # D11.1
  meta?: OutputMeta                    # D15 截断/归档
  notice?: string                      # fallback/归档提示
  async?: {state, jobId, type:"eval"}  # D14 background
  ```

  每 `EvalCellResult`：`{index, code, language, title, output, views?, status, durationMs?, exitCode?,
  statusEvents?}`。`status` 映射按结果 JSON 真实词表：`pending`/`running` 来自流式 partial；`success` →
  `complete`；`error`/`timeout`/`interrupted` → `error`（`ok`/`exit`/`kernel-eof`/`protocol-error` 是内核词，
  客户端看不到；JS host 归一化后只产出 `success`/`error`）。`tool.complete.is_error` 为真时即使
  `status=="success"` 也置 `error`。`timeout`/`interrupted`/内核死亡/负退出码不带 `exitCode`；`durationMs`
  由 `duration_seconds` 四舍五入。
- **标题**：每 cell 的 `title` 优先用模型显式传入的 `args.title`/`args.cells[].title`；缺省由 D13 的 `derive_title`
  产生（与 session instant title 同一确定性方法）。普通工具卡的标题同样走 D13；未生成前用 wire 名，生成后
  由 `tool.title` 更新，不阻塞工具执行。
- **结构化展示（D11.1）**：结果 `display` 块中 `views`/`statusEvents` 并进对应 cell，`images` 进顶层
  `details.images`；`views` 按 `text` 在 `output` 中匹配（`splitEvalOutput`），未命中跳过；`images` 用同一
  `ImageContent` 编码；`statusEvents` 进 cell，渲染器按 `EvalStatusEvent` 展示。
- **截断（D15）**：使用既有 `stdout_truncated`/`stdout_bytes_captured`/`stdout_bytes_total`/
  `stdout_bytes_omitted`/`stdout_spill_path` 与 `stdout_lines_*` 构建 `details.meta.truncation`
  （`direction:"middle"`, `truncatedBy:"bytes"`, `totalBytes`, `outputBytes`, `elidedBytes`, `totalLines`,
  `outputLines`），让 vendored `formatOutputNotice(meta)` 画原生 warning；不再用 dim notice 近似。卡面
  notice 同时给出 `agent_out(mode="artifact", kind="tool", tool_call_id=...)` 读取原文的提示；不生成
  `artifact://` URL。
- **流式（D14）**：`tool.output` 增量事件在 `executeCodeDetails` 的同一投影上更新 `cells[i].output/status`；
  partial 的 `cells` 与最终结果同形，客户端用 `updateResult(frame, true)` 更新，最终 `tool.complete` 覆盖。
  不重复追加：partial 携带累计文本或显式 delta，二者在客户端按 `cell_index` 归并。
- **resume**：`toolRow` 用 `row.args` + `row.content`（`text`/`full`）走同一 `executeCodeDetails`，并
  **merge** 进现有 details（`args`/`context`/`labels`/`row_id`/`artifact`）；不能整体替换。live 的
  `projectToolResult` 同样 merge，不覆盖 labels/diff。replay 与 live 同形。`include_tool_content:"none"`
  不建 cells，进入 eval 渲染器的 no-cells 分支（ANSI 文本；TSP 用 args 画代码框 + 文本），不是通用卡。
- **载荷上限（占位 + 增量回拉）**：`tool.complete.result` 超 64KiB 时退化成 `{output, truncated:true}`；
  识别为降级形态，不建 cells、不显示转义 JSON，先 dim 占位，然后 `session.history(after=<基线 row_id>)`
  回拉完整行并按 `tool_call_id` 重建。`history()` 包装需加 `after`。行在 `tool.complete` 前已落库，必须
  此刻 reconcile，不能拖到 `#sealPendingToolCards` 之后。`bounded_args` 仍可能裁 live code，resume 完整。
- **async（D14）**：`background` 结果带 `details.async`；卡面显示 `background` 徽标，后续 `tool.output`/
  `tool.complete` 帧按 `jobId` 填充同一卡。超时/取消只把 `async.state` 置 `cancelled`/`failed`，不伪造
  cell 输出。
- **不伪造状态**：没有 cancelled cell 帧时，`timeout`/`interrupted` 映射为 `error` + 正文说明；不设置
  `details.async` 除非 D14 真的返回了 job。
- **降级**：JSON 解析失败、`status`/`output` 缺失、或 args 缺 `code`/`cells` 时不设 cells，保留文本；
  禁止把 wrapper JSON 当正文。镜像/room 缺 `args` 时退回 `renderResult` 的 args+content 兜底。不猜状态。
- **out of scope（渲染器能力但不属于本 ADR 的 producer）**：steering/用户面 `$` 入口/`eval_execution` 行；
  其它客户端继续通用卡。完整卡的 multi-cell、live partial、title、async、statusEvents、views/images、meta
  都已在本 ADR 实现，不再标 out of scope。
- **范围**：只接 omp-ui；Desktop/Ink/消息平台继续用通用工具卡（未知事件/字段忽略）。不新增 transcript 行、
  不翻转 `python_execution` 座位、不新增 `eval.*` 方法；`$` 入口仍不存在。

### D13. 卡片标题：复用会话 instant title 的 `derive_title`（H）

- 产生器：`tools/eval_card_metadata.py`（新）调用 `agent.title_generator.derive_title`（会话 instant title
  的同一确定性方法：首行 + 词边界截断，**无模型、never fails**）。输入是工具名 + 有界参数（`execute_code`
  用 cell code 首段；普通工具用 command/path/首行），输出短标题。
- 复用与缓存：按 `(session_id, tool_call_id, cell_index)` 缓存；同一会话内相同输入去重。确定性函数，无需
  线程/超时；无结果时回退 wire 名或代码首行。
- 事件与持久化：新增 `tool.title` 事件 `{tool_id, name, title, cell_index?}`；`tool.complete`/tool row 的
  `display_metadata` 写 `card_title`（普通工具）或 `cell_titles`（eval），由 `_tool_row_message` 投影。
  `execute_code` 的 `title` 入参优先于生成值；generated title 不进入模型可见结果。
- 客户端：eval cell 用 `cell.title`；普通工具在构造 `ToolExecutionComponent` 时用 per-call
  `{name, label: card_title ?? name}`，不改变 `getToolByName` 的全局映射；`tool.title` 到达后更新 pending
  card。resume 读 `display_metadata.card_title`/`cell_titles`。
- 范围：标题只服务显示；不新增模型工具、不改 tool result、不产生 aux 调用、不影响 prompt cache。

### D14. 流式、多 cell 与 async job（H）

- 流式协议：kernel 增加 stream 帧
  `{"type":"stream","id","cell_index","stream":"stdout"|"stderr","text"}`（Python runner 用 tee writer；
  JS runner 用 console/process/stdout 写入器）。宿主 `_stdout_reader` 分发到 per-cell 回调；
  `execute_in_session_kernel`/`run_js_cell` 接受 `on_stream`；`execute_code` 绑定**独立的
  `tool.output` 通道**，不复用 `current_tool_progress()`（0.5s 节流且只带 message），也不受
  `display.tool_progress` 开关控制。
- 事件：新增 `tool.output` `{tool_id,name,cell_index,stream,text}`（additive）；客户端
  `#onToolOutput` 用 `tool_execution_update` + partial `EvalToolDetails` 更新同一 pending card；
  50ms/4KB 合并，最终 `tool.complete` 权威、覆盖 partial，不重复追加。`tool.output` 是 cell 输出，不是
  progress chrome，不能随 `display.tool_progress` 消失；其它客户端忽略未知事件。
- 多 cell：D8 的 `cells` 数组；同语言按顺序复用 kernel，不同语言可并行；`details.cells` 每项独立
  status/duration/exit/views/statusEvents，`details.images` 为顶层；卡面显示 `${cellCount} cells` 与每 cell
  caption。
- async：`tools/eval_jobs.py`（新）维护 per-session job（`execute_code` 的 `background`）；返回
  `status:"running"` + `async:{state:"running", jobId, type:"eval"}`；`tool.output`/`tool.complete` 按
  `jobId` 填同一卡；cancel/timeout 置 `cancelled`/`failed`，不伪造 cell 输出。job 生命周期由
  `clear_session`/atexit 回收。
- 不写 transcript 行；不新增 `eval.*` 方法或 `eval_execution` 行。流式/多 cell 只走工具事件与完整卡。

### D15. 截断元数据与 `agent_out` 归档（H）

- 结果字段：沿用 `_truncate_stdout_text` 的 `stdout_truncated`/`stdout_bytes_captured`/
  `stdout_bytes_total`/`stdout_bytes_omitted`/`stdout_spill_path`，并新增
  `stdout_lines_total`/`stdout_lines_shown`（additive、模型可见；原生 warning 需要准确行数）。多 cell 的
  每个 `cells[i]` 带同一组字段；顶层保留聚合。
- 客户端：`executeCodeDetails` 把上述字段投成 `EvalToolDetails.meta.truncation`
  （`direction:"middle"`, `truncatedBy:"bytes"`, `totalBytes`, `outputBytes`, `elidedBytes`, `totalLines`,
  `outputLines`），由 vendored `formatOutputNotice(meta)`/`runFoot` 画原生 warning；从 `cell.output` 剥离
  真实 marker，避免与原生 warning 双显；不建第二套 notice。`details.notice` 只放 `agent_out(...)` 读取原文
  的提示。
- `agent_out`：扩展 `AGENT_OUT_SCHEMA`/`agent_out_tool` 的 `mode="artifact"`、`kind="tool"`，接受
  `tool_call_id`（或 `step`+`tool_name`）与 `session_id`（与 agent identity 互斥）读取当前 conversation 的
  tool-output archive（`display_metadata.tool_output_artifacts`：`artifact_path`/
  `artifact_original_path`/`truncated`），也接受 `stdout_spill_path`；返回
  `content`/`original_content`/`truncated`/`path`。named agent 的现有路径不变。
- `artifact://`：不新增 URL scheme；omp 的 `artifact://` 语义由 `agent_out` 承担（用户说的
  “`artifact://` == `agent_out`”）。卡面 warning 的提示写 `agent_out(...)`，不写 `artifact://`。
- 安全：只读 `display_metadata` 记录的路径与 profile cache/temp 下的 spill；不猜路径、不读任意主机文件。

## 协议

```text
execute_code (additive)
  {code?, language?="py", cells?: [{code, language?, title?}], title?, timeout?,
   reset?, background?} -> result JSON
      # 单 cell: existing keys + language? + title? + stdout_lines_*
      # multi:   {status, output, cells:[{index,code,language,title,status,output,
      #           exit_code,duration_seconds,stdout_*,display?,views?,
      #           statusEvents?}], images?, async?}
  # language/cells[].language 只在 Bun 已安装时含 js；非本地后端拒绝 js
  # 截断结果带 stdout_bytes_* 与 stdout_lines_*（D15）

JS kernel (internal)
  init    {"type":"init","cwd":..., "tools":[...], "rpc":{...}}
  request {"id","code","reset","cell_index"?}
  stream  {"type":"stream","id","cell_index","stream","text"}
  reply   {"status":"ok"|"error","stdout","stderr","exit_code",
           "traceback","execution_count"}     # process.exit => EOF, not a reply

tool events (additive)
  tool.output  {tool_id, name, cell_index, stream, text}
  tool.title   {tool_id, name, title, cell_index?}
  tool.complete {name:"execute_code", args, result, is_error, duration_s}
      -> executeCodeDetails(...) = EvalToolDetails {language,languages,cells,
         images?,statusEvents?,meta?,notice?,async?}
  # result 为 {output, truncated:true}（64KiB 降级）时：占位 + session.history(after=row_id) 回拉重建
  snapshot tool row {args, content, display_metadata} -> same executeCodeDetails
```

`shell.exec` 不变（仍是 `!cmd` 的 request/response，不进 transcript）。`execute_code` 的 wire 只做 additive：
新入参/结果字段、`tool.output`/`tool.title` 事件、`display` details；不新增 `eval.*` 方法、`eval_execution`
行或 `$` 入口。omp-ui 的卡面由 D12 换成完整 eval 卡，其它客户端忽略未知事件/字段、继续通用卡。

## 实现清单（逐决策）

清单按 D1–D15 逐条覆盖，不只列模型面与客户端的主路径。归属见顶部标记：**H** 本仓库实现、**O** 只接
vendored 契约、**X** 明确不做。阶段沿用 P1–P5；**D1 与 D2–D7 的用户面部分是贯穿所有阶段的负向门**
（出现即回归）；D4/D6 的模型面部分分别由 D14/D15 实现。

| 决策 | 归属 | 阶段 | 实现动作 |
|---|---|---|---|
| D1 用户面 eval | **X** | P1–P5 负向门 | 不新增 `$` 方法/事件/行/入口；只做不存在性验证 |
| D2–D7 | **X/部分** | 负向门 | D2/D3/D5/D7 仍撤销；D4→D14、D6→D15 |
| D8 入参/出参 | **H** | P1 | `language`/`cells`/`title`/`timeout`/`background`、分派、审批、结果 |
| D9 JS kernel | **H** | P2 | PM Bun、acorn 变换、完整函数形态、stream 帧、生命周期 |
| D10 工具桥 | **H** | P3 | `serve_rpc`、完整 prelude op、allowlist/预算 |
| D11 完整面 | **H** | P3/P4 | views/statusEvents/images、agent/workpool、speculation、多 cell、judge、`out://`、`%bun` |
| D12 完整卡 | **H+O** | P5 | render key、`executeCodeDetails`、多 cell、resume/降级 |
| D13 标题 | **H** | P5 | `derive_title` 卡片标题、`tool.title`、display_metadata |
| D14 流式/多 cell/async | **H** | P4 | kernel stream 帧、`tool.output`、job manager |
| D15 截断/`agent_out` | **H** | P4/P5 | `meta.truncation`、`stdout_lines_*`、conversation artifact |
| 打包/文档/生成 | **H** | P4/P5 | package-data、plan、`--check` |

### D1（X）用户面 `$`/`$$` eval 不移植

- 网关：不新增 `eval.exec`/`eval.cancel` canonical 方法；不新增 `eval.start`/`eval.output`/`eval.complete`
  事件；`TRANSCRIPT_ROLES.python_execution` 保持 `seated`，不翻成 `produced`；`session.history`/`resume`
  不返回该行。
- 客户端：不新增 `$`/`$$` 输入解析、`EvalRunner`/`evalExec`/`evalCancel`/`isEvalRunning`；不新增 `/eval`
  slash 或 catalog 项；`ROW_PROJECTORS.python_execution` 保持 `undefined`；不挂载
  `EvalExecutionComponent` 的 live/pending 分支。
- 工具面：不新增 `eval` 模型工具；JS 只走 `execute_code(language:"js")`（D8）。
- 验证：验收 1/20；`grep -rn "eval\.exec" omp-ui/src tui_gateway gateway` 零命中；生成器允许 D13/D14 的
  `tool.title`/`tool.output` 与 `cells`/`display` details diff，但不允许出现 `eval.*` 方法/事件/行。

### D2–D7（X/部分）用户面撤销；D4/D6 模型面恢复

- D2：不引入 `$` 的授权/闸门顺序、local viewer 判定、单飞、线程绑定；`execute_code` 仍走 D8 的
  `check_execute_code_guard` + 硬闸。
- D3：不拆 `session_kernel_target(...)`/`run_session_cell(...)`；多 cell/流式在 D14 内做。
- D4：`$` 的 chunk/`eval.output` 仍不做；`execute_code` 的 kernel stream 帧与 `tool.output` 由 D14 实现。
- D5：不新增 `eval.*` 事件、`eval_execution` 行、`pending_display_rows` flush；流式只走工具事件。
- D6：`$$`/`display_only` 仍撤销；`TruncationMeta` 与 `stdout_lines_*` 由 D15 以 `execute_code` 原生卡
  消费者恢复。
- D7：不引入客户端 `$`/`$$` 输入、`composerState().shell`、`#evals`、replay 投影、快照期重建。
- 验证：验收 1/2/17/20；本仓库业务面不新增 `$` 的 RPC/事件/行；D14/D15 的新增字段/事件按各自验收。

### D8（H）完整入参/出参、分派与审批

- schema：
  - `tools/code_execution_tool.py::EXECUTE_CODE_SCHEMA` 保持注册期 Python-only，不在 import 时读 PM。
  - `model_tools._rewrite_execute_code(...)` 检查 `pm.installed_package("bun")`，把 `js_available` 传给
    `build_execute_code_schema(...)`：无 Bun 时 `language`/`cells[].language` 枚举只有 `["py"]`；有 Bun 时
    加 `js`。`cells`/`title`/`timeout`/`background` 始终在 schema 中；描述改为可用后端说明。
- handler（`tools/code_execution_tool.py::execute_code`）：
  - 校验 `code`/`cells` 恰好一个、`cells` 非空且不超 `max_cells`；每个 cell 的 `language` 归一化，
    未知值 `tool_error`。
  - `js` 且 Bun 缺失 → `tool_error("JavaScript needs Bun: hermes pm install bun")`；`js` 且
    `env_type != "local"` → `tool_error`，不 spawn、不落记录。
  - 单 cell：现有 `execute_in_session_kernel` 路径 + `language`/`title`/`stdout_lines_*`/流式元数据。
  - 多 cell：按 `cells` 顺序执行；结果 `cells[]` 每项独立 status/output/exit/duration/stdout/display；
    顶层 `status` 聚合，`output` 为摘要。`py`/`js` 分别走对应 kernel。
  - `reset` 清涉及语言的 kernel；`timeout` 作用于整次调用；`background` 由 D14 job manager 返回
    `status:"running"` + `async`。空 code/非法 cells 文案随语言。
- 审批/硬闸（`tools/approval.py`）：`check_execute_code_guard(..., language=)` 按 cell 语言生成 heredoc
  （`<<'PY'`/`<<'JS'`）与围栏；先跑既有硬闸再走批准闸；本地 viewer turn 自动批准；`Bun.spawn([...])` 走同一
  文本/argv 归一化路径；`clear_session` 同时清 JS kernel。
- 工具身份：core/`code_execution` 成员不变；不新增工具、不改 toolsets；`cells`/`title`/`display`/`async`
  由 D12–D15 消费。
- 验证：验收 2/3/4/7/9/11；`tests/tools/test_code_execution.py`、`tests/tools/test_code_kernel.py` 全绿。

### D9（H）PM Bun 上的 JS kernel

- `tools/eval_js_kernel.py`（新）：
  - 注册表 key `(owner, "js", bun_path, child_cwd)`，与 `SessionKernel` 并列，不复用 Python runner。
  - `tools/code_execution_env.py`：确认 `_scrub_child_env` 可被 JS kernel 复用；若它不暴露额外排除参数，
    由 `eval_js_kernel` 在返回的 env 上过滤 `NODE_OPTIONS`/`BUN_INSTALL*`，并设置 staging 的
    `HOME`/`USERPROFILE`/`BUN_INSTALL_CACHE_DIR`。
  - 只认 PM 的 bun（`pm.installed_package("bun").binary`，锁 1.4.4），不读 `PATH`/`~/.bun`。
  - `spawn_owned(..., purpose="eval-js-kernel")`；env 按上一条从 `_scrub_child_env` 构造。
  - 必须以 staging cwd 启动 `bun --no-install tools/eval_js_runner.ts`（避免读取 project `bunfig.toml` 的
    `preload`；`--config` 不能阻止，staging 启动后再 `chdir(session_cwd)` 可以）；runner 暴露按 session cwd
    解析的 `require()`；每 2s `process.kill(ppid, 0)` 父进程轮询。
  - idle/LRU 复用 `code_execution` 配置；`shutdown_js_kernels_for_owner`、atexit、`reset` 杀进程；
    超时/取消杀进程并丢弃 VM 状态；EOF/`process.exit()` 归 `error` 并换新进程。
  - `tools/approval.py::clear_session` 在 local/remote 两个 registry 之外同时调用
    `shutdown_js_kernels_for_owner`。
  - host 复用 `code_kernel.py::_cell_result` 的收尾（脱敏/截断/`--- stderr ---`/timeout 文案，必要时抽
    共用 helper），key 顺序与 Python 逐字一致；把 runner 的 `ok` 归一成结果 JSON 的 `success`。
- `tools/eval_js_runner.ts`（新）：
  - 间接 `globalThis.eval`、按需 `(async()=>{})()`、顶层 `await`；不用 `node:vm`/Worker。
  - `Bun.Transpiler({loader:"ts"})` + `eval_js_transform`；stream 帧用 `HERMES_JS_SENTINEL`；RPC 工具桥；
    force-kill 注释。
- `tools/eval_js_transform.ts` + `tools/eval_js_parser.ts`（新）：
  - `Bun.Transpiler` 后交给 vendored `acorn` 解析；按 AST 范围插入发布语句，不再用 tokenizer/fail-closed。
  - 顶层 `const`/`let`/`var`/`class`/`function`（含解构、async/generator、箭头函数）发布到 `globalThis`；
    只有顶层 `var` 发布，块内/函数内部 `var` 保持标准词法作用域。
  - 顶层 import 改写为动态 `import()` 并发布绑定；`export` 去关键字并发布；`import.meta` 合成；
    顶层 `return` 仅在需要时包 async wrapper。regex/除法/模板/注释由 parser 区分。
  - 解析失败就是语法错误；不再有 `unsupported_js_construct` 的函数限制；所有函数形态支持。
- `tools/code_kernel.py`：抽收尾 helper；增加 Python runner 的 stream tee 帧（D14）。
- `tools/code_execution_tool.py`：结果新增 `cells`/`title`/`stdout_lines_*`；不再“截断逐字不变”。
- 打包：`pyproject.toml` 的 `[tool.setuptools.package-data] tools` 增加 `eval_js_runner.ts`、
  `eval_js_transform.ts`、`eval_js_parser.ts`。
- 验证：验收 3/4/5/8/10/11/12/13/14。

### D10（H）工具桥与 prelude 范围

- `tools/code_execution_rpc.py`：把 `_rpc_server_loop` 的循环抽成可传 owner 的 `serve_rpc(...)`；
  Python/JS 共用，JS kernel 自己 bind socket/token。
- `tools/eval_js_runner.ts`：用 `node:net` 连接 `HERMES_RPC_SOCKET`/`HERMES_RPC_TOKEN`（UDS 或
  `tcp://127.0.0.1:port`，与 `code_execution_tool._connect` 同一套地址）。
- 工具面：runner 按会话固定工具清单定义 `globalThis.<name> = (args) => call(name, args)`；清单在 kernel
  init 时一次下发、cell 之间不重发；同一 allowlist、同一每 cell 预算、同一拒绝/脱敏；RPC server 单飞排队。
- `CellAuthority` 保持每 cell 捕获、settle 后 `retire()`；`agent`/`workpool` 句柄改用 per-session registry，
  活过 cell。
- 完整 prelude：`__display__`/`__status__`/`__agent__`/`__workpool__`/`__judge__`/`__judge_batch__`/
  `__completion__`/`__out__`；`display()`/`status()`/`agent()`/`wait()`/`workpool()`/`judge()`/
  `judge_batch()`/`completion()`/`out://` 在 Python/JS 两侧可用；`%bun add` 只做 managed（D11.7）。
- 验证：验收 6/14。

### D11（H）omp eval 完整面

- D11.1 `views`/`statusEvents`/`images`：工具结果加 `display` 块；`display_metadata` 持久化；
  `_tool_row_message` 投影 `details.display`；`TRANSCRIPT_ROLES` 的 tool spec 登记 `details`；客户端并进
  对应 cell 的 `views`/`statusEvents`，`images` 进 `details.images`；按 `text` 匹配，未命中跳过。
- D11.2 `agent()`/`workpool()`：`__agent__`/`__workpool__` op；`agent://<id>` handle 的
  `status`/`wait`/`send`/`cancel`；per-session、profile scope 绑定，活过 cell；budget/cancel 由 runner 承载。
- D11.3 speculation/shadow：`handle_function_call` 前工具级 `stream` 钩子（partial JSON + abort）；claim
  命中复用，miss 回退；kernel 快照不符丢弃 shadow。
- D11.4 多 cell：D8 的 `cells`；同语言顺序、跨语言可并行；`details.cells` 全部显示；不引入 `eval_busy`，
  跨调用仍由 kernel 锁串行。
- D11.5 `judge()`/`judge_batch()`/`completion()`：`__judge__`/`__judge_batch__`/`__completion__`；
  aux 角色链、questions schema、ImageContent、host-owned batch、usage 记账。
- D11.6 `out://`：`__out__` 写当前 turn reply sink；无活跃 turn `tool_error`；不进 transcript、不改 bash。
- D11.7 `%bun add`：managed 目录（Hermes home 按 cwd hash）+ lock；不写 project；`%environment project`
  返回 `unsupported_environment`；runner 继续 `--no-install`。
- 验证：D11.1/2/3/5/7 验收见原条目；D11.4/6 验收 15/18；`scripts/gen_*.py --check` 只登记真实新事件/字段。

### D12（H+O）完整 eval 卡

- **O（不改）**：vendored `pi-tui/src/tools/eval.ts::evalToolRenderer`、`renderCodeCell`、`runFoot` 不复制、
  不修改、不另画布局；只接 render key 与完整 `EvalToolDetails` 数据。
- render key（`omp-ui/src/session/agent-session.ts`）：`TOOL_RENDER_NAMES` 增加
  `execute_code: "eval"`；`getToolByName` 返回 `{name:"eval", label:"execute_code"}`；只加这一个 key。
- 结果投影（`omp-ui/src/modes/rpc/gateway-session.ts`）：新增
  `executeCodeDetails(args, decodedResult, opts)`，live 的 `projectToolResult` 与 resume 的 `toolRow`
  同源调用；输出完整 `EvalToolDetails`：
  - `cells[]`（1..N）：`index`/`code`/`language`/`title`/`output`/`status`/`durationMs`/`exitCode`/
    `views`/`statusEvents`；`images` 在 `details.images`；单 cell 结果也合成一个 cell。
  - `languages` 按首次使用顺序；`py` 映射 `python`；`isError` 沿用 `tool.complete.is_error`；`success`→
    `complete`，`error`/`timeout`/`interrupted`→`error`；timeout/interrupted/负值不带 exitCode。
  - `meta` 来自 D15；`display` 来自 D11.1；`async` 来自 D14；`title` 来自 D13；`notice` 只放
    fallback/`agent_out` 提示。
- 流式：`tool.output` partial 用同一投影更新 `cells[i].output/status`；`updateResult(frame, true)`；
  最终 `tool.complete` 覆盖 partial，不重复追加。
- resume：`row.args` + `row.content`（`text`/`full`）+ `display_metadata` 走同一 helper；merge 进
  `args`/`context`/`labels`/`row_id`/`artifact`；`include_tool_content:"none"` 进入 no-cells 分支，不是
  通用卡。
- 64KiB 降级：识别 `{output,truncated:true}`，不建 cells、不显示转义 JSON，占位后
  `session.history(after=<基线>)` 回拉重建；`history()` 包装加 `after`。行已在 `tool.complete` 前落库，
  必须即刻 reconcile。
- 降级：坏 JSON/缺 status/output/缺 code/cells 时不设 cells，保留文本；禁止 wrapper JSON 当正文；镜像缺
  args 时用 `renderResult` 兜底；不猜状态。
- 范围：只接 omp-ui；Desktop/Ink/消息平台通用卡；不新增 transcript 行、不翻转 `python_execution` 座位、
  不新增 `eval.*` 方法。完整卡的 multi-cell/live partial/title/async/statusEvents/views/images/meta 均已实现。
- 验证：验收 15/16/17/18/19/20。

### D13（H）卡片标题生成

- `tools/eval_card_metadata.py`（新）调用 `agent.title_generator.derive_title`（确定性、无模型、never
  fails）；输入是工具名 + 有界参数（eval 用 cell code 首段，普通工具用 command/path/首行）。
- 缓存 `(session_id, tool_call_id, cell_index)`；同步/廉价，不需要线程或 aux；无结果回退 wire 名/代码首行。
- `tool.title` 事件 `{tool_id,name,title,cell_index?}`；`display_metadata.card_title`/`cell_titles`
  持久化；`_tool_row_message` 投影；客户端 per-call label 覆盖与 cell.title；resume 读取。
- 验证：验收 15/16（标题出现、resume 同形、失败回退、无模型结果变化）。

### D14（H）流式、多 cell、async

- kernel stream 帧 `{"type":"stream","id","cell_index","stream","text"}`；Python runner tee、JS runner
  console/process/fd；host `_stdout_reader` 分发到 `on_stream`。
- `tool.output` `{tool_id,name,cell_index,stream,text}`；独立通道，不复用 `current_tool_progress()` 的
  0.5s 节流，也不受 `display.tool_progress` 控制；客户端 `#onToolOutput` → `tool_execution_update`
  partial；50ms/4KB 合并；最终 `tool.complete` 权威。
- 多 cell：D8 `cells`；同语言顺序、跨语言可并行；`details.cells` 全部显示；不引入 `eval_busy`，跨调用由
  kernel 锁串行。
- async：`tools/eval_jobs.py`（新）per-session job；`background` 返回 `status:"running"` + `async`；
  `tool.output`/`tool.complete` 按 `jobId` 填卡；cancel/timeout 置 `cancelled`/`failed`；`clear_session`
  回收。
- 验证：验收 15/18/19；其它客户端忽略新事件。

### D15（H）截断与 `agent_out`

- `_truncate_stdout_text` 增 `stdout_lines_total`/`stdout_lines_shown`（additive、模型可见）；多 cell 每项
  带同一组字段；沿用 `stdout_bytes_*`/`stdout_spill_path`。
- `executeCodeDetails` 投成 `meta.truncation`（middle/bytes/总字节/显示字节/elided/行数）；vendored
  `formatOutputNotice(meta)`/`runFoot` 画原生 warning；从 `cell.output` 剥离真实 marker，避免双显。
- `agent_out(mode="artifact", kind="tool", tool_call_id=...)` 读取 conversation 的
  `tool_output_artifacts`（`artifact_path`/`artifact_original_path`/`truncated`）或 `stdout_spill_path`；
  named agent 路径不变；返回 `content`/`original_content`/`path`。
- `artifact://` 不新增；`agent_out` 承担其语义；卡面提示写 `agent_out(...)`。
- 验证：验收 17；只读记录路径/profile cache/temp，不猜路径。

### 测试落点

- 模型/内核：扩展 `tests/tools/test_code_execution.py`（schema/多 cell/标题/流式字段/结果 key 顺序）与
  `tests/tools/test_code_kernel.py`（Python 回归）；新增 `tests/tools/test_eval_js_kernel.py`（无 Bun 时
  skip；跨 cell/完整函数形态/acorn 变换/stream 帧/`ok`→`success`/EOF/超时/`reset`）。
- D11：新增或扩展 `tests/tools/test_eval_jobs.py`（async/多 cell）、`tests/tools/test_eval_card_metadata.py`
  （`derive_title`/缓存/回退）、`tests/tools/test_agent_out_artifact.py`（conversation tool archive/原文）。
- 审批：扩展 `tests/tools/test_execute_code_approval_cluster.py`（每 cell 的 `<<'JS'`、本地 viewer、硬闸）。
- 客户端：扩展 `omp-ui/src/modes/rpc/gateway-session.test.ts`（完整 `executeCodeDetails`、live partial、
  resume、`meta.truncation`、64KiB 回拉、标题）与 `omp-ui/src/session/agent-session.test.ts`
  （`execute_code` render key/`tool.title`）。
- 打包：sealed wheel 安装后的 JS cell smoke（验收 13）；`omp-ui` 的 `bun test` 与
  `scripts/run_tests.sh` 同现有门。

### P4/P5 打包、文档与生成门

- `pyproject.toml`：`[tool.setuptools.package-data] tools` 增加 `eval_js_runner.ts`、
  `eval_js_transform.ts`、`eval_js_parser.ts`；sealed wheel/bundle 安装后仍需可执行 JS cell（验收 13）。
- `docs/omp-ui-panels-gateway-plan.md`：§2.3.2.1 Eval 行仍写“用户面 `$` 无 producer”；工具执行行补
  `execute_code` → 完整 eval renderer（multi-cell/live partial/title/async/views/images/meta）。
- `scripts/gen_gateway_contracts.py` / `scripts/gen_panel_inventory.py`：新增 `tool.output`/`tool.title` 事件
  与 `cells`/`display` details；重新生成并提交 diff，`--check` 应干净。
- 门禁：`python scripts/check` 与
  `scripts/run_tests.sh tests/tools/test_code_execution.py tests/tools/test_code_kernel.py
  tests/tools/test_eval_js_kernel.py` 全绿（验收 14）。

## 不采纳

| 方案 | 理由 |
|---|---|
| 用户面 `$`/`$$` eval（`eval.exec` + 事件 + `eval_execution` 行 + 客户端入口） | omp-ui 当前 shell 没有 `$` 入口（无解析/runner/RPC，`ROW_PROJECTORS.python_execution` 为 `undefined`）；新增的 canonical 方法/事件/行只写不读；2026-11-05 撤销（D1/D2–D7） |
| 新增独立 `eval` 模型工具 | 与 `execute_code` 重叠；核心工具每次 API 请求都携带，footprint ladder 要求先扩展既有代码。`execute_code(language,cells)` 已覆盖 py/js 与多 cell |
| 复用 `shell.exec` | 语义是 shell、无 kernel、无流、明确不进 transcript（`session_shell.py` docstring）；改它会破坏 `!cmd` |
| 在远程/容器后端跑 JS | 没有 PM Bun 与宿主 kernel；与本机 kernel 的语义不一致；handler 直接报错 |
| 用 `node:vm` 承载 JS cell | omp 记录 Bun 在 terminate 打断 `vm.runInContext` 时 SIGTRAP；Bun 1.4.4 的 vm 也缺动态 import 回调。用间接 eval + acorn 变换 |
| vendor Babel 做代码生成 | 只需要解析/范围插入；vendor `acorn` 单文件 parser + 源码范围编辑，避免 Babel generator 的体积与重排 |
| 再移植 omp 未列入 D11 的产品面 | D11 的 prelude/handle/judge/out:// 已实现；D11 之外的 omp 专属 surface（steering、用户面 `$`）不在本 ADR |
| 只做单 cell 的 eval 卡 | 完整卡要求 multi-cell/live partial/title/async/statusEvents/views/images；D8/D12–D15 全部实现 |
| 新增 `artifact://` URL scheme | `agent_out` 已能读 tool-output archive 与 `stdout_spill_path`，与 `artifact://` 语义等价；避免第二套 URL/读取器 |
| 用客户端 notice 近似截断 warning | 原生 `meta.truncation` 可用既有字节/行元数据精确构造；不再使用 dim notice 近似 |
| `%load/%pip/%environment` 等 magics | 只实现 D11.7 的 managed `%bun add`；其它 magics 仍属独立输入语法，走 skill/terminal |
| 项目级包安装（`%environment project`，写用户的 `package.json`/`node_modules`/锁） | 越过会话预算修改用户的工程依赖且不可回滚；只保留 Hermes home 下的 managed 缓存目录（D11 第 7 条），工程依赖仍由 skill/terminal 显式安装 |

**明确的取舍（与 omp 的差异，写清楚而不是假装等价）**：

1. JS 与 Python 是两个 kernel，状态互不可见（`execute_code(language:"py")` 与 `"js"` 不共享变量）；omp 的
   两个后端同样各一套 kernel。
2. 超时/取消会杀掉 kernel 并丢失命名空间（`execute_code` 既有契约，`code_kernel.py:717-731,748-750`）；这是
   安全上的选择：不提供无法中断的解释器。
3. 截断是 head+tail 且用 spill 文件；`artifact://` 的语义由 `agent_out` 承担（二者等价，不新增 URL
   scheme）。模型/用户可从 `stdout_spill_path` 或 `agent_out(mode="artifact", kind="tool", ...)` 读取原文；
   卡面用既有字节/行元数据画原生 warning。
4. `execute_code` **是流式**：Python 与 JS 都在 cell 执行期间发 stream 帧，`tool.output` 更新 pending card；
   fd 级输出也进入同一流。最终 `tool.complete` 仍是权威结果。
5. JS 的跨 cell 持久化是完整的：top-level `const`/`let`/`var`/`class`/`function` 发布到 `globalThis`，
   后续 cell 通过全局对象解析；块内/函数内部的 `var` 按标准 JS 词法作用域保持局部，不跨 cell 发布。没有
   “函数限制”或 fail-closed 构造列表，解析失败只是语法错误。
6. JS 工具桥既暴露 Hermes 的 allowlist 工具，也实现 D11 的 omp prelude（`display`/`status`/`agent`/`wait`/
   `workpool`/`judge`/`completion`/`out://`）；`%bun add` 只做 managed。
7. 模型面用 omp 的 eval 卡**完整数据模型**：multi-cell、语言徽标、title、async、statusEvents、views/images、
   meta/truncation、exit/duration 全部有 producer；截断用原生 `meta.truncation`，不是 notice 近似。卡面是
   vendored 渲染器 + 客户端投影，不是另画布局。事实与 omp 等价（数据来源不同，显示语义相同）。
8. `language`/`cells[].language` 始终在 schema 中：无 Bun 时枚举只有 `py`，有 Bun 时加 `js`。`cells`/
   `title`/`timeout`/`background` 与 Bun 无关。结果新增 `cells`/`title`/`stdout_lines_*`/`display`/`async`
   等 additive 字段；单 cell 的既有 key 顺序保留。
9. JS 只在本地 terminal 后端可用；docker/ssh/vercel 等后端没有 JS 后端。
10. 用户面 `$`/`$$` 不存在：用户要么用 `!cmd`/terminal，要么让模型调 `execute_code`。

## 风险

| 风险 | 缓解 |
|---|---|
| JS 变换器误改 cell | acorn AST + 源码范围插入；只发布绑定/改写 import/export；对抗用例覆盖 regex、模板、注释、嵌套函数 |
| Bun 启动读 project `bunfig.toml` 的 preload | 以 staging cwd 启动、init 后 chdir（设计期实测 `--config` 不能阻止，staging 启动可以） |
| Bun 自动安装缺失依赖 | `--no-install`；缺依赖的 import 直接失败 |
| JS 与 Python 语义差（无 `node:vm`、import 解析） | 间接 eval + `createRequire`；不承诺 vm 隔离 |
| `.ts`/parser 资源漏打包 | `package-data tools` 增加 `eval_js_runner.ts`/`eval_js_transform.ts`/`eval_js_parser.ts` + 打包验收（sealed venv 里跑 JS cell） |
| `language`/`cells` 破坏 schema/key-order 契约测试 | 无 Bun 时 `language` 枚举只有 `py`；有 Bun 时加 `js`。单 cell 既有 key 顺序保留，新字段只 additive；显式传参/多 cell 的结果按 D8 验收 |
| 新增入参触发 prompt cache 失效 | 只发生在每会话 schema 重建边界（无中途增删工具）；一次性的前缀变化，随会话生效 |
| Bun 版本漂移 | PM 锁 1.4.4，不读 `PATH`/`~/.bun`；升级走 `hermes pm` |
| JS 绕过 gateway 生命周期硬闸 | 与 Python 同一文本/argv 归一化路径；`Bun.spawn([...])` 命中 |
| 远程后端误用 js | handler 明确拒绝，不 spawn |
| 工具桥预算/allowlist 绕过 | 复用 `CellAuthority`、`SANDBOX_ALLOWED_TOOLS`、每 cell 预算与 RPC 单飞 |
| kernel 泄漏 | `spawn_owned(..., purpose="eval-js-kernel")` + idle/LRU + `clear_session`/atexit；runner 2s ppid poll |
| 结果 JSON 不一致 | Python/JS 共用收尾 helper；key 顺序测试覆盖 py 与 js |
| 生成器/面板覆盖门失败 | 新增 `tool.output`/`tool.title` 事件与 `cells`/`display` details；重新生成 contracts/panel inventory 并提交 diff，`--check` 应干净 |
| 误把 vendored 的 `pythonExecution` 分支当入口 | D1 的验收断言客户端与服务端都不存在 `eval.*`；分支保持无 producer |
| render key 误伤其它工具 | `TOOL_RENDER_NAMES` 只加 `execute_code`；其它工具仍走自己的 key（含插件的 `eval` 名，保持既有 registry 行为） |
| 结果投影失败导致假卡 | JSON/字段缺失时不设 `cells`，退回文本卡；不把 wrapper JSON 当正文；不伪造 status/exit |
| 截断提示双显 | 统一用 `meta.truncation` + vendored `formatOutputNotice`；从 `cell.output` 剥离真实 marker，不再另建 notice |
| 镜像行缺 `args` | 不建 `cells`，用 `renderResult` 的 args+content 兜底；无 exit code 也保持代码/输出正确 |
| live 结果被 viewer 64KiB 上限裁剪 | 识别 `{output, truncated:true}` → 不显示 wrapper JSON，占位后按 `session.history(after=row_id)` 回拉重建；`bounded_args` → live 显示被裁的 code，resume 完整；都不重跑、不伪造 status |
| resume 只带 `none` content | 不建 `cells`，eval 渲染器的 no-cells 分支（ANSI 文本、TSP 代码框 + 文本；非通用卡）；文档说明 `include_tool_content` 需 `text`/`full` |
| 完整卡元素无 producer | D11/D13–D15 为每个元素定义 producer；验收逐项覆盖 multi-cell/live partial/title/async/statusEvents/views/images/meta，不再标 out of scope |
| 流式事件乱序/重复 | 每个帧带 `cell_index`/`stream`；客户端按 cell 归并，最终 `tool.complete` 覆盖 partial；50ms/4KB 合并 |
| 标题生成拖慢/失败 | `derive_title` 是确定性、无模型函数；缓存/去重后同步生成，无结果回退 wire 名/代码首行，不阻塞工具、不进模型结果 |
| async job 泄漏/重复完成 | `tools/eval_jobs.py` per-session registry + `clear_session`/atexit；jobId 幂等，最终帧只结算一次 |
| `agent_out` 读到任意路径 | 只读 `display_metadata.tool_output_artifacts` 记录与 profile cache/temp 下的 spill；沿用 archive 的 containment 检查 |

## 验收

1. **用户面不存在**：omp-ui 输入 `$x = 41` 作为普通 prompt（无 `eval.exec` 调用）；`grep -rn "eval\.exec"
   omp-ui/src tui_gateway gateway` 零命中；重新生成后 `scripts/gen_gateway_contracts.py --check`、
   `scripts/gen_panel_inventory.py --check` 干净。
2. **Python 回归**：无 Bun 时 schema 含 `code`/`cells`/`title`/`timeout`/`background`，`language` 枚举只有
   `["py"]`；`execute_code(code="print(1)")` 保留既有 key 顺序并新增 `stdout_lines_*`；单 cell 不丢原有字段；
   `tests/tools/test_code_execution.py`、`tests/tools/test_code_kernel.py` 全绿。
3. **有 Bun 时**：schema 的 `language`/`cells[].language` 加 `js`；`execute_code(code, language="js")` 成功时
   `result.status === "success"`（runner 的 `ok` 必须在 host 归一化），失败为 `error`、`process.exit()` 的
   EOF 归 `error`；`cells` 多语言结果按 `cells[]` 返回。
4. **JS 跨 cell**：`let x = 1` 后，含顶层 `await` 的 cell 仍能读到 `x`；顶层 `var`/`const`/`class`/
   `function`、解构、async/generator、箭头函数都发布；块内/函数内 `var` 保持词法作用域；`reset` 后全部
   丢失；失败 cell 仍是 `status="error"`、输出含 stack。
5. **JS 解析/变换**：字符串/模板/regex/注释/嵌套函数不被误改；`export`、`import.meta`、顶层 `return` 按
   D9 处理而不是拒绝；解析失败只是语法错误；没有 `unsupported_js_construct` 函数限制。
6. **工具桥/prelude**：`await web_search({...})` 与 Python `hermes_tools.web_search` 同一 allowlist；预算耗尽
   返回错误；`display()`/`status()`/`agent()`/`wait()`/`workpool()`/`judge()`/`completion()`/`out://`
   可用且受预算/授权约束。
7. **Bun 缺失**：schema 仍有 `language`，枚举只有 `py`；显式 `js` → 明确错误；`hermes pm install bun` 后下一次
   会话枚举加 `js`（工具定义缓存不跨会话误留）。
8. **bunfig/安装安全**：session cwd 的 `bunfig.toml` 含 `preload` 时 cell 不执行 preload；缺依赖的 import 在
   `--no-install` 下直接失败，不自动联网安装。
9. **审批与硬闸**：每个 js cell 在非本地 viewer turn 弹一次批准，heredoc 为 `<<'JS'`；本地 viewer turn 自动
   批准；JS cell 里 `Bun.spawn(["launchctl","bootout","ai.hermes.gateway"])` 被硬闸拒绝。
10. **超时/取消**：进程被杀、VM 状态丢失、输出带 force-kill 注释；下一次调用是新进程；多 cell 的 timeout
    不错误归因到已完成的 cell。
11. **kernel 隔离**：`execute_code(language:"py", code="x = 1")` 之后，`execute_code(language:"js",
    code="typeof x")` 得到 `"undefined"`；反向同样不共享；`reset` 只清对应语言的 kernel。
12. **非本地后端**：`TERMINAL_ENV=docker|ssh` 下 `language:"js"` → 明确错误，不 spawn、不落任何记录。
13. **打包**：sealed wheel/bundle 安装后 `eval_js_runner.ts`、`eval_js_transform.ts`、`eval_js_parser.ts`
    仍在，`execute_code(language:"js")` 可执行。
14. **仓库门**：`python scripts/check` 与 `scripts/run_tests.sh tests/tools/test_code_execution.py
    tests/tools/test_code_kernel.py tests/tools/test_eval_js_kernel.py` 全绿。
15. **完整卡（live）**：omp-ui 画 eval 卡——多 cell 的代码单元/caption、语言徽标（py→python、js→js）、
    spinner、title、live 输出（`tool.output` partial）、cell views/statusEvents、顶层 images、async
    `background` 徽标、原生 truncation warning；完成后 ANSI/TSP 的 Done/exit/duration/Failed 与 omp 语义
    一致；卡面不出现结果 JSON 文本。
16. **完整卡（resume）**：`session.resume` 后按 `row.args` + `row.content` + `display_metadata` 重建，
    多 cell/标题/cell views/statusEvents/顶层 images/async/meta 与 live 一致；`include_tool_content:"none"`
    进入 no-cells 分支，不是通用卡。
17. **截断**：结果带 `stdout_bytes_*`/`stdout_lines_*`/`stdout_spill_path`；`executeCodeDetails` 生成
    `meta.truncation`，原生 warning 只出现一次；`agent_out(mode="artifact", kind="tool", tool_call_id=...)`
    能读到 `original_content`；不出现 `artifact://`。
18. **失败/超时/取消**：`error` 的 cell 在 TSP foot 显示 `exit N` 或 `Failed`，ANSI 错误图标；timeout/
    interrupted 不带 `exitCode`，原因在正文；JS force-kill 注释仍在输出里；async cancelled 只改 job 状态。
19. **降级**：坏 JSON、缺 `status`/`output`、缺 `args.code`/`cells` → 不画 cells、退回文本卡；
    `{output,truncated:true}` 的 64KiB 降级 → 不显示转义 JSON，占位后 `session.history(after=<基线>)` 回拉
    重建；不崩溃、不伪造 exit。
20. **其它客户端/契约**：Desktop/Ink/消息平台忽略 `tool.output`/`tool.title`/新 details，继续通用卡；其它
    工具 render key 不变；不新增 `eval.*` 方法或 `eval_execution` 行；`python_execution` 座位仍 `seated`/
    无 producer；contracts/panel inventory 已按新事件字段重新生成。
21. **卡片标题**：eval cell 与普通工具卡都用 `derive_title`（确定性、无模型）生成标题；`tool.title` 更新
    pending card；resume 从 `display_metadata.card_title`/`cell_titles` 恢复；无结果回退 wire 名/代码首行；
    不产生 aux 调用、不改模型结果。
22. **D11 子面**：views/statusEvents 正确并入 cell，images 进 `details.images`；agent/workpool 句柄活过 cell；speculation claim
    命中/回退一致；judge/judge_batch/completion 走 aux 角色链；`out://` 只写当前 turn；`%bun add` 只写
    managed 目录，`%environment project` 拒绝。
23. **async**：`background` 返回 `async` job；`tool.output`/`tool.complete` 按 `jobId` 填同一卡；cancel/
    timeout 结算一次；`clear_session`/atexit 回收，无泄漏。
