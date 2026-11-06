# ADR-4：`execute_code` 的 JS 后端与原生 eval 卡（不移植用户面 `$` eval）

**状态**：已决定（2026-11-01；**2026-11-05 修订**）。**修订结论：用户面 `$`/`$$` eval 不移植；模型面
`execute_code` 增加 `language`（`py`/`js`）与 Bun JS 后端，并复用 omp 原生的 eval 工具卡（单 cell）。**
实现未开始。分阶段：**P1** `execute_code` 的 `language` 参数、py/js 分派与审批/硬闸；**P2** JS kernel
（Bun 子进程 + 间接 eval + 顶层声明/import 变换）；**P3** 工具桥（`serve_rpc` 提取）与 kernel 生命周期/清理；
**P4** 打包（sealed wheel 的 `.ts` 资源）与回归；**P5** 原生显示（render key + 结果投影 + 截断元数据，D12）。
落地前 `docs/omp-ui-panels-gateway-plan.md` §2.3.2.1 的 Eval 行已按修订写“无 producer”；客户端只在 D12 做
render-key/结果投影，不新增输入入口或行型（§0.1）。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§0.3、§2.3.2、§2.3.2.1；ADR-10（D8.2a render key、
D4/D5 工具行投影）；ADR-11（D11.5 的 eval 侧 `__judge__`/`__judge_batch__`/`__completion__` 接缝）、
ADR-12（D10 prelude 边界、D11.2 handle 接缝）；
`tools/code_execution_tool.py`、`tools/code_kernel.py`、`tools/code_execution_rpc.py`、
`tools/code_execution_env.py`、`tools/approval.py`、`model_tools.py`、`pyproject.toml`；
客户端 `omp-ui/src/session/agent-session.ts`、`omp-ui/src/modes/rpc/gateway-session.ts`、
`omp-ui/src/modes/utils/ui-helpers.ts`、`omp-ui/src/modes/controllers/event-controller.ts`、vendored
`pi-tui/src/tools/eval.ts`；
PM 的 `bun` 包（`pm/packages.py::Bun`、`pm/lock.json` 固定 1.4.4；`hermes pm install bun`；`hermes omp` 也用它）。

**范围**：模型面 `execute_code` 与其 kernel（canonical local gateway 的 turn 内工具调用），以及 omp-ui 的
**显示映射**（render key + tool 结果投影，D12）。用户面（`$`/`$$`）、RPC、事件、transcript 行、输入入口、
slash 命令都不在本 ADR；消息平台、Desktop、Ink sidecar、ACP 不设计。`execute_code` 在 omp-ui 里用 omp 原生
eval 卡（单 cell），其它客户端保持通用工具卡（诚实降级）。

**修订记录（2026-11-05）**：2026-11-01 版把用户面 `$` cell 也列为移植目标（`eval.exec`/`eval.cancel`、
`eval.*` 事件、`eval_execution` 行、`$$`/`display_only`、chunk 流、客户端入口）。本修订**撤销该半边设计**，
只保留 `execute_code` 的 JS 后端。代码事实：`omp-ui/src` 没有 `$` 入口（`input-controller.#submitText` 只分发
`/` 与 `.`/`c`；`parsePythonCommandInput`/`pythonCommandPrefixLength`/`EvalRunner`/`evalExec`/`evalCancel`/
`isEvalRunning` 全树 0 命中）；`ROW_PROJECTORS.python_execution` 是 `undefined`
（`omp-ui/src/modes/rpc/gateway-session.ts:275-288`），`ui-helpers.ts` 的 `pythonExecution` 分支（`:202`）
没有 producer；网关没有 `eval.*` 方法/事件，本地执行 RPC 只有不进 transcript 的 `shell.exec`
（`gateway/session_shell.py`）。撤销前的设计全文见 `git show 5e91617af0:docs/adr/ADR-4-eval-execution.md`。
**同修订追加 D12**：模型面在 omp-ui 复用 omp 原生 eval 卡（单 cell），保证原生显示；用户面入口仍不移植。

**取代关系（逐条，避免文档互斥）**：

- plan §2.3.2.1 第 454 行（Eval 执行）把状态写成“部分”，并假定网关有共享 kernel。本 ADR **不再取代**该行、
  也不实现用户面：该行按本 ADR 改为“无 producer（omp-ui 无 `$` 入口；网关无 eval 方法）”。2026-11-01 版的
  “由 D3 取代”随之撤销。
- plan §0.3 第 96 行把 `eval/` 列入“随本机业务代码删除”：不变。本 ADR 不恢复任何本机 eval 业务代码
  （`omp-ui/src/eval/**`、`EvalRunner`、`handlePythonCommand` 都不回来）。
- plan §2.3.2 第 412 行的 `eval-execution.ts` 渲染器描述不变，但它不再只是残留：D12 让 `execute_code` 通过
  render key 复用 `evalToolRenderer`（单 cell）；用户面仍不产生 `pythonExecution` 行、不新增入口。
- 2026-11-05 版曾把“模型面没有 omp 的 eval 卡”列为取舍：**由 D12 取代**——单 cell 原生卡移植，multi-cell /
  async job / `title` / `statusEvents` / `views`/`images` 仍不采纳（D11.1 登记后续接缝）。
- **不新增 `eval` 模型工具**：保持不变。JS 在 omp 只从 eval 工具面可达，而 Hermes 已有同一“持久 kernel +
  工具桥”的模型面入口 `execute_code`；D8 给它加 `language`，不在每次 API 请求里多带一个重叠的核心工具
  （footprint ladder：扩展既有代码优先）。
- ADR-2 的行/事件惯例、ADR-3 的 `append_display_row`/pending flush **不再被本 ADR 使用**：本 ADR 不写行、
  不发事件，因此既不依赖也不阻塞那两个 primitive（它们仍由各自的 ADR 拥有）。
- ADR-11 D11.5、ADR-12 D10/D11.2 引用本 ADR 的编号保持有效：D10/D11 内容不变（D11.4 撤销，见下）。

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

因此本 ADR 决定：只给 `execute_code` 增加 `language`（D8）与 JS kernel（D9），并在 omp-ui 用原生 eval 卡显示
（D12，客户端映射、wire 不变）；不新增工具、不改既有字段、不新增任何用户面入口（D1）。

## 决策

### D1. 用户面 eval 不移植（`$`/`$$`）

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

### D2–D7. 撤销（2026-11-05）

2026-11-01 版的 D2–D7 全部以用户面 `$` 为前提，随 D1 撤销；编号保留为空位，避免 D8–D11 的既有外部引用
失效。

| 已撤销 | 原内容 | 撤销理由 |
|---|---|---|
| D2 | `$` 的授权/闸门顺序、local viewer 判定、单飞、线程绑定 | 无入口；`execute_code` 仍走既有 `check_execute_code_guard` + 硬闸（D8） |
| D3 | `session_kernel_target(...)` 与 `run_session_cell(...)` 拆分，让 `$` 与 `execute_code` 共享 kernel | 只服务用户面共享；`execute_code` 的 Python 路径不需要重构（D8） |
| D4 | kernel `stream:true`、chunk sentinel/tee、`eval.output` 节流 | 只有 `$` 需要实时输出；`execute_code` 是请求/响应 |
| D5 | `eval.*` 事件、`eval_execution` 行、`pending_display_rows` flush、模型文本 | 无消费者；不写行、不发事件 |
| D6 | `TruncationMeta` 映射与 `$$` 的 `display_only` 标记 | `$$`/`display_only` 撤销；截断的最小行数映射（两个 additive 字段）由 D12 以原生卡消费者重新立项，不再引入旧 D6 的全量字段 |
| D7 | 客户端 `$`/`$$` 输入、`composerState().shell`、`#evals`、replay 投影、快照期重建 | 客户端只做 D12 的显示映射；`$` 入口不存在 |

### D8. `execute_code` 增加 `language`，JS 走 sibling Bun kernel

- schema（additive）：`{code, language?: "py"|"js" = "py", reset?}`。
  - 注册期 `EXECUTE_CODE_SCHEMA`（`code_execution_tool.py:949`）保持 Python-only，不在 import 时读 PM。
  - 每会话重建器 `model_tools._rewrite_execute_code`（`model_tools.py:347-352`）检查
    `pm.installed_package("bun")`：**没有 Bun 时不加 `language` 属性**（schema 与今天逐字一致）；有 Bun 时
    加 `language`（`enum: ["py","js"]`）并把描述从 “Run Python…” 改成可用后端的说明（omp 的
    `Execute Python and JavaScript…` 同义）。schema 仍单 cell、不加 `title`；卡面由 D12 复用 omp 的 eval 卡
    （单 cell），不再退回通用卡（§2.3.2.1 工具执行行）。
- handler：`language` 归一化为 `"py"`/`"js"`；缺省按 `py` 执行（结果不带 `language` 字段）；未知值 →
  `tool_error`。`js` 但 Bun 缺失 →
  `tool_error("JavaScript needs Bun: hermes pm install bun")`。`js` 但 terminal 后端不是本地
  （`env_type != "local"`，与 `execute_code` 自己的远程分派逐字一致）→ `tool_error`：JS kernel 在宿主本地，
  远程/容器后端既没有 PM Bun 也没有同一个 kernel，拒绝比“悄悄换一个不存在的后端”诚实。`code` 为空的错误
  文案随语言（不再固定说 Python）。
- 审批与硬闸：`check_execute_code_guard(..., language="py")` 增加关键字参数，heredoc 标签用 `<<'PY'`/`<<'JS'`，
  pending body 的代码围栏语言随语言；默认 `py` 让既有调用与测试不变。先跑既有硬闸（`SANDBOX_AVAILABLE`、
  `enforce_no_refusal`、gateway 生命周期/宿主解释器 kill 的文本匹配），再走批准闸；本地 viewer turn 内自动
  批准（既有 `_is_local_viewer_turn`）。gateway 生命周期硬闸是文本匹配 + argv 标点归一化
  （`cron/lifecycle_guard.py:394-450`），`Bun.spawn(["launchctl","bootout",…])` 也会命中。
- 分派：省略 `language` → 现有路径，未截断时行为与结果逐字不变（截断时只多 D12 的两个行数字段）；
  显式 `language=="py"` → 同一条现有路径
  （`execute_in_session_kernel`），结果末尾加 `language:"py"`；`js` → `run_js_cell`（D9），结果末尾加
  `language:"js"`。三者都返回既有结果 JSON 形状（`status/output/exit_code/stdout_truncated/...`，
  `code_execution_tool.py:738-805`），不重排既有 key；JS 的 output/traceback 映射到同样字段。
- `reset`：清该语言的 kernel（`py` 现有行为；`js` 杀 Bun 进程）。超时仍取 `code_execution.timeout`；不加每
  调用 `timeout`。
- 工具面：`execute_code` 的 core/`code_execution` 成员身份不变，不新增工具、不改 toolsets；结果里的
  `details`/`cells` 只由 D12 的显示映射消费，不改变工具身份。不做 async 后台化、`title`、多 cell、
  views/statusEvents。

### D9. JS 后端：PM Bun 子进程上的间接 eval kernel

- 后端：`js` 只在本地 terminal 后端可用（与 D8 的 `env_type != "local"` 判定同源）；非本地后端在分派前
  fail closed，不 spawn Bun、不触达宿主文件系统。
- 运行时：只认 PM 的 bun（`pm.installed_package("bun").binary`，`pm/lock.json` 固定 1.4.4）。缺失时
  `language:"js"` 不在 schema 里；显式传 `js` 返回 `tool_error("JavaScript needs Bun: hermes pm install bun")`。
  不读 `PATH`/`~/.bun`，保证锁定版本。
- 进程：`tools/eval_js_kernel.py`（新）为 `(owner, "js", bun_path, child_cwd)` 维护一个 Bun 子进程，与
  `tools/code_kernel.py` 并列（Python 的 `SessionKernel` 绑定 Python runner，不复用）；同样的 idle/LRU 上限
  （`code_execution` 配置）、`shutdown_js_kernels_for_owner`、atexit、
  `spawn_owned(..., purpose="eval-js-kernel")`（`hermes_cli/process_identity.py:1076`）。
  `shutdown_js_kernels_for_owner` 接进 `tools/approval.clear_session`（现在只清 Python local/remote 两个
  registry，`tools/approval.py:313-322`）。
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
  `ERR_VM_DYNAMIC_IMPORT_CALLBACK_MISSING`）。cell 体包成 `(async () => { … })()` 并 `await`，顶层 `await`
  可用。
- 变换（`tools/eval_js_transform.ts`，随 runner 发货）：先用 `Bun.Transpiler({loader:"ts"})` 统一 TS/JS
  （设计期实测：去类型、保留顶层语句与 import），再做一层 tokenizer 级变换，只在顶层语句且不在字符串/
  模板/注释/regex 内时，为每个顶层声明插入**发布语句**（声明本身保持局部）：
  - `const x = …` / `let y = …` / 顶层 `var z = …`（含解构）→ 原声明后插 `globalThis.x = x;`（名字从
    declarator pattern 提取；对象/数组解构取绑定名）；
  - `class X …` / `function f(…) {…}` → 声明后插 `globalThis.X = X;` / `globalThis.f = f;`；
  - 顶层 `import`（default/named/namespace/side-effect）→ 改写成 wrapper 内的
    `const m = await import(<resolved>)`，并发布绑定；解析用 `createRequire(session_cwd + "/cell.cjs")`
    （设计期实测 `resolve` + `import(pathToFileURL(...))` 可用；`node:`/绝对/`file:` 直接 import）；
  - 后续 cell 的裸标识符会命中全局对象上的这些属性（全局对象环境记录），因此**不需要**改写引用，也**不能**
    把 `let`/`const` 降级成 wrapper 函数里的 `var`——wrapper 函数作用域的 `var` 不会跨 cell 存活；设计期实测
    `eval("const z=3; globalThis.z=z")` 之后 `eval("z")==3`，async wrapper 内发布同样生效；
  - 顶层 `export`、`import.meta`、顶层 `return`、无法可靠分类的 `/`（regex vs 除法）、声明名为
    `globalThis`/`undefined`/`NaN`/`Infinity` → **fail closed**：`unsupported_js_construct`，不执行；
  - 嵌套块里的 `var` 不发布（它绑定到 wrapper 的函数作用域）；omp 的模块语义会保留它，这是已知差异。
  不 vendor Babel：仓库没有 JS parser 依赖，引入 parser 是新的供应链/体积负担；有限变换器可测且失败即拒绝，
  验收用对抗 cell 证明不误改。这是相对 omp（Babel + 464 行模块加载器）的明确缩范围：只支持上述四种静态
  import 形态与顶层声明发布，其余 fail closed。
- 结果：runner 回 `{status:"ok"|"error", stdout, stderr, exit_code, execution_count}`；host 复用
  `code_kernel.py::_cell_result` 的收尾（`:738-805`：`redact_sensitive_text(..., code_file=True)` +
  `_truncate_stdout_text` + `--- stderr ---` 合并 + timeout 文案；必要时抽成共用 helper），key 顺序与 Python
  逐字一致；显式传参时结尾加 `language`。
  cell 内 `console.*`/`process.stdout.write` 在 cell 期间被换成缓冲写入器（结果帧仍用
  `HERMES_JS_SENTINEL` 前缀，宿主优先认帧，其余字节按 raw 处理，与 Python runner 同款）。
  `process.exit()` 会直接结束 runner：宿主看到 EOF → `status="error"`、kernel 丢弃、下次调用换新进程
  （Python 的 `sys.exit` 是 `status="exit"`，JS 没有等价的拦截点）。
  超时/取消 = 杀进程，VM 状态丢失，结果文本带 omp 同款注释（*“The JS worker was force-killed and its VM
  state was reset…”*，历史 `eval/js/executor.ts::formatJsTimeoutAnnotation`）。`reset` = 起新进程。
- 不移植：`views`/`statusEvents`/`images`（`display()`）、`out://`、
  `agent()/wait()/workpool()/judge/completion` prelude、session snapshot/prelude 注入、`%bun add`/包安装、
  speculation/shadow 执行、并发多 cell。
- 上条的「并发多 cell」要分两层看：
  - **卡面多 cell（不移植）**：omp 的 eval 卡按多 cell 设计——渲染器接受 `args.cells` 数组
    （`pi-tui/src/tools/eval.ts:176` `getRenderCells`）、head 打 `${cellCount} cells`（:766）、
    `details.cells?: EvalCellResult[]` 带每 cell 的 status（:117-128）；这是“不采纳”表的多 cell/async
    部分（async job manager、steering、工具卡 details 通路）；基础单 cell 卡由 D12 采纳。但 vendored 的 tool
    schema 仍是单 cell
    （`prompts/tools/eval.md:1` “One cell per call”；`tools/eval.ts:139-149`），`#runCells` 只从单份
    params 构造一个 cell（`tools/eval.ts:534`），`concurrency = "exclusive"`（:430）再由 agent loop
    串行化（`vendor/agent/src/agent-loop.ts:3864-3889`）——“多 cell”是卡的容量，不是当前入参。
  - **内核多 cell 并发（不移植）**：kernel 协议按 `runId` 记账、能容忍并发 run
    （`eval/js/worker-core.ts:205-220`；Python runner 每请求一个 asyncio task，`eval/py/runner.py:2899`）。
    Hermes 的 `execute_code` 每次调用一个 cell，同一 JS kernel 的调用由 kernel 锁串行；没有并发入参，
    也没有 `eval_busy`/事件归并面。
- 历史佐证：本仓库 schema 侧从未有多 cell 入参——`git log -S'cells:' -- omp-ui/src/tools/eval.ts`
  只命中两次 vendoring 提交（`76cbd169da`、`5bd7dabd21`），`cells:` 只出现在 details/渲染器。
- 补齐的前置设计见 D11（各项互不依赖）。
- 隔离：间接 eval 不是安全边界；JS cell 与 Python cell 一样拥有宿主机能力（Bun `spawn`/`fetch`），闸门与
  Python 完全一致（D8 的批准/硬闸/后端限制），env scrub 如上；不承诺沙箱。
- **打包**：runner/transform 是非 Python 资源；`pyproject.toml` 的 `[tool.setuptools.package-data] tools`
  现在只列 `bot_desktop/launcher.sh`、`bot_desktop/wallpaper.png`（注释说明未声明的资源会从 sealed wheel
  消失），必须加上 `eval_js_runner.ts`、`eval_js_transform.ts`。否则 JS 只在源码检出可用、打包安装静默坏掉。

### D10. JS 的工具桥与 prelude 范围

- 复用 Python kernel 的 line-JSON RPC server（`tools/code_execution_rpc.py::_rpc_server_loop`）：把循环抽成
  可传入 owner 的 `serve_rpc(...)`，JS kernel 自己 bind socket/token；`CellAuthority` 的“每 cell 捕获、settle
  后 retire”照旧（`code_kernel.py:197-250,838,868`）。JS runner 用 `node:net` 连接
  `HERMES_RPC_SOCKET`/`HERMES_RPC_TOKEN`（UDS 或 `tcp://127.0.0.1:port`，与 `code_execution_tool._connect`
  同一套地址）。
- 预置：runner 收到工具名清单后定义 `globalThis.<name> = (args) => call(name, args)`，与 Python 的
  `hermes_tools` 同一 allowlist、同一每 cell 预算、同一拒绝/脱敏。调用在 runner 内单飞行排队（RPC server
  一次一条请求）。工具清单取该 session 已解析的工具面（`_sandbox_tools_for(enabled_tools)`，会话内固定）。
- 不移植 omp 的 prelude 词汇（`agent()`/`wait()`/`workpool()`/`judge`/`completion`/`out://`/`display()`）：
  工具桥已经暴露 Hermes 自己的工具；这些是 omp 的产品面，不是本区块的契约。`%` 命令同样不移植。

### D11. 后续移植的前置设计（本 ADR 不实现，只登记接缝）

以下六项（1/2/3/5/6/7）互不依赖，也不改 P1–P5 的契约；登记它们是为了让“不移植”有可执行的下一步，而不是
一句品味判断。4 已随用户面撤销。

1. **`views`/`statusEvents`/`images` → 工具卡 details 通路**
   - 缺口：D12 之后代码、输出、exit/duration 与截断提示已经原生；缺的是结构化展示载体——`execute_code`
     只回一个 JSON 字符串（`code_execution_tool.py:56-81`），里面没有 `views`/`statusEvents`/`images`，实时
     `tool.complete.result` 解出的对象也没有；快照 tool 行只带 `content`/`args`/`labels`/`is_error`/`row_id`，
     `TranscriptMessage` 没有 `display_metadata`（ADR-10 P5 已从 wire 删除，见
     `tui_gateway/contracts/common.py:187-216`）；顶层 `display` 已是 custom 行的 `bool`
     （`common.py:205`），不能拿来放对象。
   - 设计：工具结果加 additive `display` 块 `{images: ImageContent[], views: [{text, view}], statusEvents}`，
     实时路径直接读解码后的 result；持久化仍写 `display_metadata`（`append_message` 的自由字段，
     `hermes_state_messages.py:398-429`），由 `_tool_row_message`（`tui_gateway/session_history.py:438`）投影成
     `TranscriptMessage.details.display`（`details` 已存在，tool 行当前为 null；不碰顶层 `display` 的布尔语义）。
     同时在 `TRANSCRIPT_ROLES` 的 tool spec（`tui_gateway/contracts/transcript_roles.py:42`）登记 `details`，
     让 `runtime.describe.transcript_blocks[].fields` 广告它。客户端把 `display` 并进 D12 的
     `details.cells[0].views` / `details.images` / `details.statusEvents`，由同一个 eval 卡按 `text` 匹配
     `views[]` 渲染（与 `EvalOutputPart`（`pi-tui/src/tools/eval.ts:771`）、`placeViews`
     （`tools/eval.ts:1100`）同形）。模型正文不变，`views` 只服务用户。
   - 验收：无 view 命中时退回纯文本；view 的 `text` 被截断/不在输出里时跳过而不是错位；图片走同一
     `ImageContent` 编码，不新增第二套；resume 后的卡与 live 同形；custom 行的 `display` 布尔语义不变；
     tool 块的 `fields` 只新增 `details`，其余不变。

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

4. **~~并发多 cell → 放弃 per-session 单飞~~（2026-11-05 撤销）**
   - 前提是 2026-11-01 版 D2 第 8 条的用户面单飞；D2 已随 D1 撤销。`execute_code` 每次调用一个 cell，同一个
     kernel 的并发调用由 kernel 锁串行，不存在 `eval_busy`、事件归并或按 `execution_id` 分流的接口面。omp 的
     多 cell 仍只是卡面容量（D9），不移植。

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
     **拒绝 project 级**：不写用户项目的 `package.json`/`node_modules`/锁文件，`%environment project` 返回
     `unsupported_environment`；改工程依赖仍走 skill/terminal 的显式操作。模块解析仍以 session cwd 为主、
     managed 目录作 fallback（对应 omp `packageRoot` 的次序），不改 D9 的 `createRequire`。
   - 验收：安装前后 session cwd 无新增/修改文件；`%environment project` 被拒；并发安装同 cwd 串行；缺包
     import 失败而不是隐式安装。

### D12. 原生显示：`execute_code` 复用 omp 的 eval 卡（单 cell）

**目标**：`execute_code`（py 与 js）在 omp-ui 里画 omp 原生的 eval 工具卡，而不是通用卡。只做显示移植，不新增
输入入口、行型、事件或方法；用户面 `$` 仍不存在（D1）。

**需要移植的只有四件事**：① render key（`execute_code` → `eval`）；② 结果 JSON → `EvalToolDetails` 投影
（含 code/language/output/status/exit/duration）；③ 截断行数两个 additive 字段（网关侧）；④ resume 用同一投影
（`row.args` + `row.content`）。渲染器、编辑器与组件都是 vendored 的，不复制、不另画布局。

- **渲染器不复制**：卡面就是 vendored `pi-tui/src/tools/eval.ts::evalToolRenderer`（`toolRenderers.eval`，
  `pi-tui/src/tools/index.ts:36-56`）。本 ADR 只接 render key 与数据。
- **render key**：`TOOL_RENDER_NAMES`（`omp-ui/src/session/agent-session.ts:58-61`）加
  `execute_code: "eval"`；`getToolByName` 返回 `{name:"eval", label:"execute_code"}`，`toolRenderName` 用它选
  渲染器，而卡片标题/协议仍保留 wire 名（ADR-10 D8.2a）。其它工具不受影响。
- **调用面**：`tool.start.args` 是 `{code, language?}`；`getRenderCells`（`pi-tui/src/tools/eval.ts:176-190`）
  已经接受 `args.code` + `args.language`（`"js"`→javascript，其余→python），运行中显示代码单元与 spinner。
  render key 同时命中 `ToolExecutionComponent` 的 `toolName === "eval"` 分支
  （`pi-tui/src/chat/tool-execution.ts:1875-1881`），把 `output`/`expanded`/`previewLines` 传给渲染器——主接线
  只有 `TOOL_RENDER_NAMES` 这一处。`EvalLanguage` 只有 `"python"`/`"js"`；`"py"` 必须映射成 `"python"`。
  无需网关改动。
- **结果面**：新增纯函数 `executeCodeDetails(args, decodedResult)`（`gateway-session.ts`，live/resume 共用；
  输入是 `decodeToolResult` 后的对象）把既有结果 JSON 投成 `EvalToolDetails`：

  ```text
  language: "python" | "js"            # args.language === "js" ? "js" : "python"
  cells: [{index: 0, code: args.code, language,
           output: result.output,
           status: "complete" | "error",
           durationMs: round(result.duration_seconds * 1000),
           exitCode: result.exit_code}]
  meta?: {truncation}                  # 仅 result.stdout_truncated
  notice?: "FULL output: <stdout_spill_path>"   # 有 spill 时
  ```

  `status` 映射：`ok`/`exit` → `complete`；`error`/`timeout`/`interrupted`/`kernel-eof`/`protocol-error` →
  `error`；`tool.complete.is_error` 为真时即使 `status=="ok"` 也置 `error`（避免卡面 done 与工具失败判定不一致）。
  `timeout`/`interrupted`/内核死亡时不带 `exitCode`（负值只表示内核被杀，不画成 `exit -1`）。
  `isError` 沿用 `tool.complete.is_error`；message 的 `content` 换成 `result.output`（cells 路径不读，
  但 copy/兜底可读）。
- **截断**：卡面用 `runFoot` 的原生 warning（`pi-tui/src/tools/native-view.ts:229-266` 读
  `meta.truncation`）。`TruncationMeta` 需要 total/shown 行数，所以 `_truncate_stdout_text` **仅在截断时**给
  结果加 `stdout_lines_total`/`stdout_lines_shown`（分别按 `full_text.splitlines()` 与 head+tail 正文的
  `splitlines()` 计，不含截断提示行；非截断结果逐字不变）。客户端映射：

  ```text
  {direction:"middle", truncatedBy:"bytes", totalBytes:stdout_bytes_total,
   outputBytes:stdout_bytes_captured, elidedBytes:stdout_bytes_omitted,
   totalLines:stdout_lines_total, outputLines:stdout_lines_shown}
  ```

  并按 `... [OUTPUT TRUNCATED - ... N bytes omitted ...] ...` 这个 marker 正则从 `cell.output` 剥掉 Hermes
  的正文提示（否则同一事实显示两次）；**不要**依赖 `stripOutputNotice`——它只认 omp 自己的
  `formatOutputNotice(meta)`，剥不掉 Hermes 的 marker。spill 路径放 `details.notice`
  （`FULL output: <stdout_spill_path>`）。
- **resume**：`toolRow`（`gateway-session.ts`）已有 `row.args` 与 `row.content`（`include_tool_content` 为
  `text`/`full` 时是完整结果 JSON；omp-ui 的 `session.resume`/`history` 已传 `full`）。用同一个 helper 生成
  D12 字段，并 **merge** 进 `toolRow` 现有的 details（`args`/`context`/`labels`/`row_id`）——
  `details = {...existingDetails, ...executeCodeDetails(row.args, JSON.parse(row.content))}`；不能整体替换，
  `ui-helpers.ts:326-336` 的调用参数与渲染上下文都读这些键。live 的 `projectToolResult` 同样 merge 进
  `toolResultDetails(payload)`，不覆盖 labels/diff 等既有键。replay 与 live 同形。
  `include_tool_content:"none"` 没有 content → 不建 cells，走通用卡（诚实降级）。
- **载荷上限**：viewer 事件有 64KiB 上限（`gateway/session_tool_events.py::bounded_result`）。
  `tool.complete.result` 超过它时退化成 `{output, truncated}`（没有 status/exit_code）→ live 不建 cells，退回
  文本卡；`bounded_args` 把每个字符串键截到 4096 字符、总预算 64KiB（live 卡显示被裁的 code，resume 读完整
  存量行仍可原生）。stdout 上限 50KB、stderr 10KB，余量很薄——这是既有约束，不是新契约。
- **不伪造状态**：工具结果没有 cancelled 帧，`timeout`/`interrupted` 只映射成 `error` + 正文说明；不设
  `details.async` 或伪造 `(cancelled)` 尾（那是用户面 `$` 的卡，D1 不移植）。
- **降级**：JSON 解析失败、`status`/`output` 缺失、或 args 缺 `code` 时不设 `cells`/`meta`/`notice`，保留文本与
  通用卡（截断提示只出现一次）；镜像/room 转发缺 `args` 时退回 `renderResult` 的 args+content 兜底（无 exit
  code，但代码与输出仍对）。不猜、不伪造状态。
- **不移植**：多 cell（`details.cells` 恒为 1）、`title`、async job、`statusEvents`、steering、
  `views`/`images`（D11.1）、`display()`。schema 仍单 cell（D8）。
- **范围**：只接 omp-ui；Desktop/Ink/消息平台继续用通用工具卡（wire 不变）。不新增 transcript 行、不翻转
  `python_execution` 座位、不新增方法/事件；`$` 入口仍不存在。

## 协议

```text
execute_code (additive)
  {code, language?: "py"|"js" = "py", reset?} -> existing result JSON
      # 省略 language 且未截断时结果逐字不变；显式传参时末尾加 "language"
  # language 只在 Bun 已安装时出现在 schema；非本地后端由 handler 拒绝 js
  # stdout_truncated=true 时结果另带 stdout_lines_total / stdout_lines_shown（D12）

JS kernel (internal)
  init    {"type":"init","cwd":..., "tools":[...], "rpc":{...}}
  request {"id","code","reset"?}
  reply   {"status":"ok"|"error","stdout","stderr","exit_code",
           "traceback","execution_count"}     # process.exit => EOF, not a reply

tool card (client-side projection, no wire change)
  tool.start.args   {code, language?} -> eval renderer renderCall (code cell)
  tool.complete     {name:"execute_code", args, result, is_error, duration_s}
                    -> executeCodeDetails(...) = EvalToolDetails {language, cells, meta?, notice?}
  snapshot tool row {args, content} -> same executeCodeDetails (content = result JSON)
```

`shell.exec` 不变（仍是 `!cmd` 的 request/response，不进 transcript）；`execute_code` 只增加 `language` 入参，
并在显式传入时于结果末尾加 `language`；截断时加两个行数字段。wire/事件/行形状不变；omp-ui 的卡面由 D12
换成原生 eval 卡，其它客户端仍是通用卡。不新增任何用户面方法、事件或 transcript 行。

## 实现落点

| 层 | 位置 | 改动 |
|---|---|---|
| 模型面 | `tools/code_execution_tool.py` | `execute_code(..., language=None)`；py/js 分派；js 的本地后端/Bun 闸；显式传参时结果末尾加 `language`（省略且未截断时逐字不变）；空 code 文案随语言 |
| 模型面 | `model_tools.py` | `_rewrite_execute_code`/`build_execute_code_schema` 按已安装 Bun 增加 `language` 与描述；无 Bun 时逐字不变 |
| 模型面 | `tools/approval.py` | `check_execute_code_guard(..., language=)`：heredoc/围栏随语言；`clear_session` 同时清 JS kernel |
| 内核 | `tools/eval_js_kernel.py`（新） | Bun 子进程注册表、init/chdir、请求/回复、idle/LRU、owner 清理、`--no-install` |
| 内核 | `tools/eval_js_runner.ts`（新） | 间接 eval、`(async()=>{})()`、`Bun.Transpiler`、结果帧、RPC 工具桥、父进程退出轮询 |
| 内核 | `tools/eval_js_transform.ts`（新） | tokenizer 级顶层声明/import 变换；不可分类即 fail closed |
| 内核 | `tools/code_execution_rpc.py` | `serve_rpc(...)` 抽成可传 owner 的循环（Python/JS 共用） |
| 内核 | `tools/code_kernel.py` | 收尾整形（clean/脱敏/截断/JSON）抽成共用 helper；不新增 stream 帧 |
| 内核 | `tools/code_execution_tool.py` | `_truncate_stdout_text` 仅在截断时加 `stdout_lines_total`/`stdout_lines_shown`（D12） |
| 客户端 | `omp-ui/src/session/agent-session.ts` | `TOOL_RENDER_NAMES` 加 `execute_code: "eval"`（D12） |
| 客户端 | `omp-ui/src/modes/rpc/gateway-session.ts` | `executeCodeDetails(args, result)`；`projectToolResult`（live）与 `toolRow`（resume）同源调用；content 换成 output；截断/notice 映射 |
| 客户端 | `omp-ui/src/modes/utils/ui-helpers.ts` / `event-controller.ts` | 沿用既有 tool card 路径（details 已透传），无新分支；确认 `useBuiltInRenderer` 对 `execute_code` 为真 |
| 打包 | `pyproject.toml` | `[tool.setuptools.package-data] tools` 增加 `eval_js_runner.ts`、`eval_js_transform.ts`（sealed wheel 否则丢资源） |
| 文档 | `docs/omp-ui-panels-gateway-plan.md` | §2.3.2.1 Eval 行改为“无 producer（无入口）”，工具执行行补 `execute_code` → eval renderer；ADR 索引段同步 |
| 生成 | `scripts/gen_gateway_contracts.py` / `scripts/gen_panel_inventory.py` | 不新增方法/事件，`--check` 应无 diff（有 diff 说明误加了接口） |

## 不采纳

| 方案 | 理由 |
|---|---|
| 用户面 `$`/`$$` eval（`eval.exec` + 事件 + `eval_execution` 行 + 客户端入口） | omp-ui 当前 shell 没有 `$` 入口（无解析/runner/RPC，`ROW_PROJECTORS.python_execution` 为 `undefined`）；新增的 canonical 方法/事件/行只写不读；2026-11-05 撤销（D1/D2–D7） |
| 新增独立 `eval` 模型工具 | 与 `execute_code` 重叠；核心工具每次 API 请求都携带，footprint ladder 要求先扩展既有代码。`execute_code(language)` 已覆盖 JS |
| 复用 `shell.exec` | 语义是 shell、无 kernel、无流、明确不进 transcript（`session_shell.py` docstring）；改它会破坏 `!cmd` |
| 在远程/容器后端跑 JS | 没有 PM Bun 与宿主 kernel；与本机 kernel 的语义不一致；handler 直接报错 |
| 用 `node:vm` 承载 JS cell | omp 记录 Bun 在 terminate 打断 `vm.runInContext` 时 SIGTRAP；Bun 1.4.4 的 vm 也缺动态 import 回调。用间接 eval + tokenizer 变换 |
| vendor Babel 做声明/import 改写 | 仓库没有 JS parser 依赖；引入 parser 是新的供应链与体积负担。有限变换器 + fail closed |
| 移植 omp prelude（`agent`/`wait`/`workpool`/`judge`/`out://`/`display`） | omp 产品面 helper，不是本区块契约；Hermes 的工具桥已暴露自己的工具 |
| eval 卡的 async / 多 cell / `title` / steering / 结构化 details 面 | 基础单 cell 卡由 D12 移植；async job manager、steering、多 cell（schema 仍单 cell）与 `views`/`statusEvents`/images（D11.1）仍不采纳 |
| `%load/%pip/%bun/%environment` magics | 属独立输入语法；Hermes 已有 skill/terminal 做依赖安装，omp 的用户区块（本 ADR 不移植）也只保证 code+output（后续若做，只做 managed，见 D11 第 7 条） |
| 项目级包安装（`%environment project`，写用户的 `package.json`/`node_modules`/锁） | 越过会话预算修改用户的工程依赖且不可回滚；只保留 Hermes home 下的 managed 缓存目录（D11 第 7 条），工程依赖仍由 skill/terminal 显式安装 |
| `views`/`statusEvents`/图片/`artifact://`/auto-background | D12 只做单 cell 的文本/exit/duration/截断；结构化 views、`statusEvents`、图片与 `artifact://` 仍不移植（D11.1 登记接缝），async 后台化是工具面语义 |

**明确的取舍（与 omp 的差异，写清楚而不是假装等价）**：

1. JS 与 Python 是两个 kernel，状态互不可见（`execute_code(language:"py")` 与 `"js"` 不共享变量）；omp 的
   两个后端同样各一套 kernel。
2. 超时/取消会杀掉 kernel 并丢失命名空间（`execute_code` 既有契约，`code_kernel.py:717-731,748-750`）；这是
   安全上的选择：不提供无法中断的解释器。
3. 截断是 head+tail 且用 spill 文件；没有 `artifact://`。模型看到的是正文里的截断提示与 spill 路径
   （没有 `middle` 方向的元数据对象）。
4. `execute_code` 不是流式：Python 与 JS 都在 cell 结束时返回；fd 级输出（子进程直接写 fd）同样不实时，
   JS 只缓冲 `console`/`process.stdout.write`。若需实时，另立变更。
5. JS 的跨 cell 持久化是“顶层声明后发布到 `globalThis`”（声明保持局部，裸名在后续 cell 通过全局对象解析）；
   嵌套块内的 `var` 不发布，无法可靠分类的 cell 直接拒绝（Python 后端没有这些问题）。
6. JS 工具桥是 Hermes 自己的 allowlist 函数（`web_search(...)` 等），不是 omp 的 prelude 词汇；`display()` 视图
   与 `out://` 不产生。
7. 模型面用 omp 的 eval 卡（单 cell、语言徽标、exit/duration、截断 warning）；多 cell、async job、`title`、
   steering 与结构化 details（D11.1）不移植。卡面是 vendored 渲染器 + 客户端投影，不是另画一套布局。
8. `language` 参数只在 Bun 已安装时出现；未安装时 schema 与今天逐字一致（不广告不可用的后端）。省略该参数
   且未截断时结果也与今天逐字一致；显式传参才在结果末尾加 `language`，截断时另追加两个行数字段（D12）。
9. JS 只在本地 terminal 后端可用；docker/ssh/vercel 等后端没有 JS 后端。
10. 用户面 `$`/`$$` 不存在：用户要么用 `!cmd`/terminal，要么让模型调 `execute_code`。

## 风险

| 风险 | 缓解 |
|---|---|
| JS 变换器误改 cell | tokenizer 只在深度 0 且能分类时改；其余 `unsupported_js_construct` 拒绝；对抗用例 |
| Bun 启动读 project `bunfig.toml` 的 preload | 以 staging cwd 启动、init 后 chdir（设计期实测 `--config` 不能阻止，staging 启动可以） |
| Bun 自动安装缺失依赖 | `--no-install`；缺依赖的 import 直接失败 |
| JS 与 Python 语义差（无 `node:vm`、import 解析） | 间接 eval + `createRequire`；不承诺 vm 隔离 |
| `.ts` 资源漏打包 | `package-data tools` 增加两个文件 + 打包验收（sealed venv 里跑 JS cell） |
| `language` 破坏 schema/key-order 契约测试 | 无 Bun 时 schema 逐字不变；有 Bun 时只加 `language` 属性。省略 `language` 且未截断时结果逐字不变；显式传参只在末尾加 `language`，截断时再追加两个行数字段，其余 key 顺序不动 |
| 新增入参触发 prompt cache 失效 | 只发生在每会话 schema 重建边界（无中途增删工具）；一次性的前缀变化，随会话生效 |
| Bun 版本漂移 | PM 锁 1.4.4，不读 `PATH`/`~/.bun`；升级走 `hermes pm` |
| JS 绕过 gateway 生命周期硬闸 | 与 Python 同一文本/argv 归一化路径；`Bun.spawn([...])` 命中 |
| 远程后端误用 js | handler 明确拒绝，不 spawn |
| 工具桥预算/allowlist 绕过 | 复用 `CellAuthority`、`SANDBOX_ALLOWED_TOOLS`、每 cell 预算与 RPC 单飞 |
| kernel 泄漏 | `spawn_owned(..., purpose="eval-js-kernel")` + idle/LRU + `clear_session`/atexit；runner 2s ppid poll |
| 结果 JSON 不一致 | Python/JS 共用收尾 helper；key 顺序测试覆盖 py 与 js |
| 生成器/面板覆盖门失败 | 本 ADR 不新增方法/事件；`scripts/gen_*.py --check` 应无 diff |
| 误把 vendored 的 `pythonExecution` 分支当入口 | D1 的验收断言客户端与服务端都不存在 `eval.*`；分支保持无 producer |
| render key 误伤其它工具 | `TOOL_RENDER_NAMES` 只加 `execute_code`；其它工具仍走自己的 key（含插件的 `eval` 名，保持既有 registry 行为） |
| 结果投影失败导致假卡 | JSON/字段缺失时不设 `cells`/`meta`，退回通用卡；不伪造 status/exit |
| 截断提示双显 | 客户端建 `meta.truncation` 时删掉 `cell.output` 里的 Hermes 文本 notice；有落盘路径时走 `details.notice` |
| 镜像行缺 `args` | 不建 `cells`，用 `renderResult` 的 args+content 兜底；无 exit code 也保持代码/输出正确 |
| live 结果/参数被 viewer 64KiB 上限裁剪 | `bounded_result` → 不建 cells、退回文本卡；`bounded_args` → 卡显示被裁的 code；两者都不重跑、不伪造 status，resume 从完整存量行恢复 |
| resume 只带 `none` content | 不建 `cells`，通用卡；文档说明 `include_tool_content` 需 `text`/`full` |

## 验收

1. **用户面不存在**：omp-ui 输入 `$x = 41` 作为普通 prompt（无 `eval.exec` 调用）；`grep -rn "eval\.exec"
   omp-ui/src tui_gateway gateway` 零命中；`scripts/gen_gateway_contracts.py --check`、
   `scripts/gen_panel_inventory.py --check` 无 diff。
2. **Python 回归**：无 Bun 时 `build_execute_code_schema()` 与今天逐字一致；`execute_code(code="print(1)")`
   （省略 `language`、未截断）的结果与今天逐字一致（无 `language`/行数字段）；截断时只多
   `stdout_lines_total`/`stdout_lines_shown`；`tests/tools/test_code_execution.py`、
   `tests/tools/test_code_kernel.py` 全绿。
3. **有 Bun 时**：schema 只多 `language` 属性与描述句；`execute_code(code, language="py")` 与省略的 Python
   行为一致，结果只在末尾多 `language:"py"`。
4. **JS 跨 cell**：`let x = 1` 的 cell 之后，一个含顶层 `await` 的 cell 仍能读到 `x`；`var`/`const`/`class`/
   `function` 同样通过 `globalThis` 发布；TS 注解、`require()` 与按 session cwd 解析的静态 import 可用；
   `reset` 后全部丢失；失败 cell 仍是 `status="error"`、输出含 stack。
5. **JS 变换器对抗**：字符串/模板/regex/注释里的 `let`/`class` 不被改写；嵌套块内的声明不变；`export`、
   `import.meta`、顶层 `return`、无法分类的 `/` → `unsupported_js_construct`，且 cell 未执行；嵌套块内
   `var` 不跨 cell（已知差异，断言其行为）。
6. **JS 工具桥**：`await web_search({...})` 与 Python `hermes_tools.web_search` 同一 allowlist；预算耗尽后调用
   返回错误；未 allowlist 的工具被拒。
7. **Bun 缺失**：schema 无 `language`；显式 `js` → 明确错误；`hermes pm install bun` 后下一次会话出现
   `language`（工具定义缓存不跨会话误留）。
8. **bunfig/安装安全**：session cwd 的 `bunfig.toml` 含 `preload` 时 cell 不执行 preload；缺依赖的 import 在
   `--no-install` 下直接失败，不自动联网安装。
9. **审批与硬闸**：`execute_code(language:"js")` 在非本地 viewer turn 弹一次批准，heredoc 为 `<<'JS'`；本地
   viewer turn 自动批准；JS cell 里 `Bun.spawn(["launchctl","bootout","ai.hermes.gateway"])` 被硬闸拒绝
   （Python `subprocess.run([...])` 同款文本/标点归一化路径）。
10. **JS 超时/取消**：进程被杀、VM 状态丢失、输出带 force-kill 注释；下一次调用是新进程。
11. **kernel 隔离**：`execute_code(language:"py", code="x = 1")` 之后，`execute_code(language:"js",
    code="typeof x")` 得到 `"undefined"`；反向同样不共享；`reset` 只清对应语言的 kernel。
12. **非本地后端**：`TERMINAL_ENV=docker|ssh` 下 `language:"js"` → 明确错误，不 spawn、不落任何记录。
13. **打包**：sealed wheel/bundle 安装后 `tools/eval_js_runner.ts`、`tools/eval_js_transform.ts` 仍在，
    `execute_code(language:"js")` 可执行（源码检出之外也能跑）。
14. **仓库门**：`python scripts/check` 与 `scripts/run_tests.sh tests/tools/test_code_execution.py
    tests/tools/test_code_kernel.py` 全绿。
15. **原生卡（live）**：`execute_code` 在 omp-ui 画 eval 卡——代码单元 + 语言徽标（py→python、js→javascript）、
    运行中 spinner；完成后显示输出、`done`/`exit N` 与 duration；卡面不出现结果 JSON 文本。
16. **原生卡（resume）**：`session.resume` 后同一卡按 `row.args` + `row.content` 重建，代码/输出/exit/duration
    与 live 一致；`include_tool_content:"none"` 时退回通用卡。
17. **截断**：截断结果新增 `stdout_lines_total`/`stdout_lines_shown`（非截断结果逐字不变）；卡面只出现一次原生
    warning（不重复文本 notice），spill 路径在 notice 行；无 spill 时无 notice。
18. **失败/超时/取消**：卡面 state 为 failed，`exit 1`/timeout/interrupted 文案正确，不显示 done；JS 的
    force-kill 注释仍在输出里。
19. **降级**：坏 JSON、缺 `status`/`output`、缺 `args.code` → 不画 cells、退回通用卡；不崩溃、不伪造 exit。
20. **其它不变**：Desktop/Ink 仍用通用工具卡；其它工具的 render key/卡面不变；无新 RPC/事件/transcript 行；
    `python_execution` 座位仍 `seated`/无 producer。
