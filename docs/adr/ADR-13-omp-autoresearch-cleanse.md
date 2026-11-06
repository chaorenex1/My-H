# ADR-13：omp Autoresearch / Cleanse 的移植（选项 C：新 host 后端，不含 CLI 子命令）

**状态**：已决定（2026-11-04）；**选项 C：两项均建立新的 host 后端/网关能力**（沿用 ADR-12 对 C
的定义）；**未实现**——`agent/autoresearch/`、`agent/cleanse/` 尚未落盘。本 ADR 的收口条件是
`autoresearch.*` / `cleanse.*` RPC、事件契约、斜杠命令与 omp-ui dashboard/overlay 接线；
**不新增任何 CLI 子命令**。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§10（本 ADR 收口其中“Autoresearch / cleanse”
一行）；ADR-4（不移植 omp eval prelude）；ADR-11（不新增判定路线）；ADR-12（C = 新 host 后端的
先例、客户端命令调 RPC 的写法）；`tui_gateway/AGENTS.md`、`tui_gateway/contracts/`（含
`events.py`）、`tui_gateway/method_ctx.py`、`tui_gateway/methods_tools.py`
（`_profile_scoped_rpc`）、`tui_gateway/server.py`、`gateway/panel_inventory.py`、
`gateway/session_commands.py`、`tools/delegate_tool.py`、`hermes_cli/commands.py`、
`hermes_cli/cli_commands_mixin.py`、`hermes_cli/slash_exec.py`（只放纯格式化 executor）、
`scripts/gen_gateway_contracts.py`、`scripts/gen_panel_inventory.py`、`agent/verify/`、`skills/`；
客户端：`omp-ui/src/modes/interactive-mode.ts`、`omp-ui/src/modes/types.ts`、
`omp-ui/src/modes/controllers/`、`omp-ui/src/modes/rpc/gateway-session.ts`、
`omp-ui/src/session/agent-session{,-events}.ts`、`omp-ui/src/VENDORED.md`。

**取证口径**：Hermes 侧引用本仓库当前工作树（2026-11-04）。omp 侧以本地 checkout
`C:\Users\zarag\projects\oh-my-pi`（HEAD `6a86df85`，`@oh-my-pi/pi-coding-agent` 18.14.0）为准：
`packages/coding-agent/src/autoresearch/`（`index.ts`、`state.ts`、`storage.ts`、`tools/`、
`prompt*.md`）与 `packages/coding-agent/src/cleanse/`（`checkers.ts`、`parsers.ts`、`balance.ts`、
`loop.ts`、`agent.ts`、`prompts/`）。客户端面以同一 checkout 的
`packages/tui/src/apps/{autoresearch-dashboard,autoresearch-data,cleanse-board}.ts`、
`packages/tui/src/overlays/cleanse-panel.ts`、`packages/coding-agent/src/autoresearch/index.ts`
（extension 命令/widget/overlay）与
`packages/coding-agent/src/modes/controllers/cleanse-command-controller.ts` 为准；Hermes 侧客户端
引用 `omp-ui/src` 当前工作树。本机安装的 `18.16.0.exe` 只用于交叉核对 drift
（`omp cleanse --help`、TUI 命令元数据、内嵌 prompt）；`omp-ui/src/vendor/` 只作客户端镜像，不作
行为依据。Cleanse 另有 `omp://cli-reference.md:272`，只作为 omp **行为**证据，不移植其 CLI 形态。
这样与 ADR-12 的 omp 取证口径一致。

**范围**：移植两个工作流的后端语义：Autoresearch 的实验会话、Git 隔离、基准协议、keep/discard、
状态与恢复；Cleanse 的 checker 发现、输出解析、加权文件分批、并行修复与复验。并把 omp 的
autoresearch dashboard 与 cleanse 实时 overlay 作为 host 状态的**投影**接回 omp-ui（D10）。

C 的交付面固定为五层，缺一不算完成：

| 层 | 交付物 |
|---|---|
| host core | `agent/autoresearch/`、`agent/cleanse/`：确定性状态机、Git、解析、分批、派发 |
| 网关 RPC + 事件 | `autoresearch.*`、`cleanse.*`：契约、handler、notification、profile 作用域 |
| 斜杠命令 | `/autoresearch`、`/cleanse`：CLI、网关、TUI/Desktop 三个 surface 的**薄**入口 |
| skill | `skills/software-development/{autoresearch,cleanse}/SKILL.md` |
| omp UI | `/autoresearch` dashboard widget/overlay、`/cleanse` 实时 overlay；只消费 RPC + 事件 |

**明确不做**：`hermes autoresearch` / `hermes cleanse` 子命令、`hermes_cli/subcommands/*`、
`hermes_cli/main.py` 注册、argv/`--json`/退出码契约；不移植 ps-top/live visualizer（`ps_top_live`
面板保持原状），不复刻 in-process extension host，也不复刻 omp 的
`api.setActiveTools(...EXPERIMENT_TOOL_NAMES)` 工具挂/卸（见 D10）。不新增 core model tool，
不新增 daemon，不移植 omp eval kernel 的 `agent()`/`wait()`/`workpool()` prelude（ADR-4 D10 不变）。

**取代关系**：`docs/omp-ui-panels-gateway-plan.md` §10 的“Autoresearch / cleanse”行由本 ADR 收口为
**C**。该行原先把 C 写成“只读报告”；本 ADR 采用 ADR-12 的 C 语义（新建 host 后端/网关能力），
并同步修正该行。`gateway/panel_inventory.py:184` 与 `docs/omp-ui-panels.json:907` 的合并面板拆为
`autoresearch`、`cleanse`、`ps_top_live` 三行（D8）：前两者在 RPC/事件落地前 `unsupported`
（`supported:false`），落地后 `pending`，客户端 controller 接线后 `ready`；`ps_top_live` 保持
`partial`。不伪造 ready，也不把未接线的 surface 标成可用。

## 问题

omp 把两个“让代理连续工作”的能力做成 host 后端：

1. **Autoresearch**：`/autoresearch [goal]` 进入持久实验模式。主代理先写 `./autoresearch.sh`，再
   通过 `init_experiment` 建会话；之后每轮 `run_experiment` → `log_experiment`，host 根据指标决定
   提交还是回滚。它有专用 Git 分支、baseline、run 目录、指标/ASI 解析、scope 偏差、可疑 run 标记
   和置信度，并跨轮恢复模式与目标。
2. **Cleanse**：`omp cleanse` 发现项目 checker，跑出诊断，按严重度加权成文件不重叠的批次，并行派生
   子代理修复，最后重跑 checker 验证。它是直接改代码的编排器，不是报告器。

Hermes 有 `delegate_task`、`terminal`、`hermes verify` 的 recipe 检测、`/review` 的后台子代理、
session DB 与 profile 作用域，但没有这两个工作流的状态机、预算边界和可被客户端调用的 host 服务。
直接照搬 omp 的四个 core tool 会违反“narrow waist / footprint ladder”：每个工具进入每次 API 请求
的 schema，中途启用/停用还会破坏 prompt cache。C 要求把它们做成 **host 服务 + 网关 RPC + 斜杠命令
+ skill**，而不是模型工具，也不是一组新 CLI 子命令。

## omp 的语义（移植对象）

### Autoresearch

- 入口：`/autoresearch [goal]` 开启；`/autoresearch off` 关闭；`/autoresearch clear
  [--keep-tree|--reset-tree]` 清会话并可重置到 baseline。
- Phase 1（harness setup）：写 `./autoresearch.sh`，必须退出 0 并打印 `METRIC <name>=<value>`；
  通过普通 shell 验证后再 `init_experiment`。
- Phase 2（iteration loop）：`run_experiment` 固定执行 `bash autoresearch.sh`，捕获输出，解析
  `METRIC`/`ASI`；`log_experiment` 的 `keep` 提交改动，`discard`/`crash`/`checks_failed` 回滚改动；
  `update_notes` 维护 playbook 与 ideas。
- 隔离：`autoresearch/<slug>-YYYYMMDD[-N]` 分支；要求干净工作树；纯 Jujutsu 或非 Git 目录拒绝启动。
- 状态：每项目 DB/artifact 目录，记录 session、segment、baseline commit、runs、pending run、
  scope/off-limits、max iterations；恢复时回放 `autoresearch-control`（mode/goal）。
- 决策：best metric 排除 flagged/discarded；keep 的 scope 偏差需 justification；confidence 是噪声底
  的倍数；达到迭代上限自动关闭模式。

### Cleanse

- 入口：`omp cleanse [REQUEST]`；无 REQUEST 时交互选择 checker，或让 discovery agent 产出命令。
- 发现：从 manifest/配置探测项目本地二进制；支持 Rust/Go/Python/JS/TS/Ruby/PHP/Swift/Dart/Shell/
  Haskell/Terraform/actionlint 等，缺失可执行文件则跳过。
- 解析：`rust|rust-test|go|go-test|staticcheck|golangci|ruff|pyright|mypy|pylint|flake8|ty|eslint|biome|oxlint|deno-lint|stylelint|rubocop|phpstan|psalm|swiftlint|dart|credo|shellcheck|hlint|terraform|tflint|actionlint|generic`。
- 加权：error 5 / warning 3 / info 1；文件级诊断 +2；无 code +1；无 suggestion +1。按权重打包成
  文件不重叠的批次，默认最多 32 个 worker。
- 修复：worker 只改自己的写作用域，不能派生子代理，不能跑全项目检查/格式化；新诊断转给所属
  worker；结束后编排器重跑 checker 验证。

## Hermes 的约束

1. **prompt cache 神圣**：不为“进入模式”改 system prompt 或 tool schema。两个功能都不提供 core
   tools；模型只用普通 `terminal`/`read_file`/`patch`/`delegate_task`。状态转换由 host 在斜杠命令
   或 RPC 调用时执行，不靠模型调用专用工具。
2. **core 窄腰**：两个功能是可选的工程工作流，不是 `_HERMES_CORE_TOOLS`。C 的“host 后端”指 CLI/
   网关进程内的核心模块、RPC 与斜杠命令，不指模型工具。
3. **一个进程服务多个 profile**：状态路径、子进程环境、Git 操作必须绑定调用者 profile；路径用
   `get_hermes_home()`，RPC 走 `_profile_scoped` / `_profile_scoped_rpc`，子进程用
   `build_subprocess_env`/`served_profile_child_env`。
4. **审批边界**：Cleanse 会改代码。`cleanse.plan` 与 `/cleanse` 默认只计划；`cleanse.repair` 或
   `/cleanse repair` 才派发 worker，worker 使用现有 delegation 的显式审批路径，不用静默 yolo 绕过。
5. **无 daemon**：不新增常驻进程。Autoresearch 的每次状态转换由一次显式的斜杠命令/RPC 触发；
   不实现后台自动续跑，也不写会话“模式”字段。
6. **不新增 CLI 子命令**：不添加 `hermes autoresearch` / `hermes cleanse`，不改
   `hermes_cli/subcommands/`、`hermes_cli/main.py` 的 `_BUILTIN_SUBCOMMANDS`，不定义 argv 语法、
   `--json` 或进程退出码。理由：核心窄腰要求能力只有一个进程内实现（`agent/*`），对外只有
   **RPC 与斜杠命令**两个入口；再加一层 CLI 子命令会产生第三套参数/结果契约（退出码、stdout
   格式），与 RPC 的 `Result` 模型重复且必然漂移。脚本/自动化直接说 JSON-RPC；终端用户用
   TUI/Desktop 面板或斜杠命令。**代价**：没有客户端或网关 surface 的纯 headless 场景无法驱动
   Autoresearch 循环（见风险）。

## 选项

**两项决定均为 C；A/B 是已否决的备选。**

| 功能 | A（否决） | B（否决） | C（本 ADR，两项均选 C） |
|---|---|---|---|
| Autoresearch | skill + 提示词，无 host 状态 | 不做 | 新 host 后端：`autoresearch.*` RPC/事件 + 状态机 + 斜杠命令 + dashboard + skill |
| Cleanse | skill + 外部脚本 | 不做 | 新 host 后端：`cleanse.*` RPC/事件 + checker 编排 + 斜杠命令 + overlay + skill |
| 共同 | 近似实现 | 无 | 新方法/事件/面板能力 + omp-ui 投影；不新增 core tool；**不新增 CLI 子命令** |

选择 C 的理由：两个工作流都需要 host 所有权（Git 状态、run 目录、诊断解析、预算、审批），纯 skill
无法保证一致性；core tool 又会进入每次请求。C 把它们做成可测试、可被 CLI/网关/TUI/Desktop 调用的
host 服务，同时保持 prompt cache 与 core 边界。**C 的入口是斜杠命令与 RPC，不是 CLI 子命令**：
斜杠命令负责人类交互，RPC 负责面板与自动化，两者调用同一核心函数；omp 原有的 dashboard 与
cleanse overlay 作为这些 RPC/事件的投影保留。

## 决策

### D1. Autoresearch 是 host 服务，斜杠命令与 RPC 共用同一核心

新增 `agent/autoresearch/`（状态、Git、run/log/notes/status/clear），并暴露：

```text
autoresearch.status  ({session_id?, cwd?, profile?}) -> {result: {session, recommendation}}
autoresearch.init    ({session_id?, cwd?, profile?, name?, goal?, primary_metric?, metric_unit?,
                       direction?, scope_paths?, off_limits?, constraints?, secondary_metrics?,
                       max_iterations?, new_segment?}) -> {result: state}
autoresearch.run     ({session_id?, cwd?, profile?, run_id, timeout_seconds?}) -> {result: pending run}
autoresearch.log     ({session_id?, cwd?, profile?, status, description, metric?, metrics?, asi?,
                       justification?, commit?, flag_runs?}) -> {result: {message, run, state}}
autoresearch.notes   ({session_id?, cwd?, profile?, body? | append_idea?}) -> {result: state}
autoresearch.clear   ({session_id?, cwd?, profile?, keep_tree?, reset_tree?}) -> {result: cleared}
```

配套 notification（S→C，发给发起调用的 connection；声明在 `tui_gateway/contracts/events.py`）：

```text
autoresearch.updated       {session_id?, state, recommendation, reason}
autoresearch.run.started   {session_id?, run_id, command, started_at}
autoresearch.run.finished  {session_id?, run_id, exit_code, duration_seconds, metric?, log_path,
                            output_truncated?, run_directory?}
```

- `updated.reason ∈ {init, log, notes, clear, segment}`；dashboard 收到后整块替换 state，不轮询。
- `run.started/finished` 只服务 dashboard 的 `runningExperiment` 与耗时显示；`run` 的 RPC 响应仍
  是权威结果。`run_id` 由客户端生成并在事件中回显，客户端丢弃不匹配/过期 `run_id` 的事件。

- RPC handler 与斜杠 handler 调用同一函数；不复制业务逻辑。host core 不依赖 argparse。
- `session_id` 存在时，`cwd` 从该会话的 workdir 解析；没有 `session_id` 时 `cwd` 必填（4000 错误，
  不读 `os.getcwd()`）。`profile` 只用于无会话调用。
- `init` 要求 `./autoresearch.sh` 存在、工作树干净；不在 `autoresearch/*` 分支时创建
  `autoresearch/<project-slug>-YYYYMMDD[-N]`。`primary_metric` 可省略：省略时 `init` 先跑一次
  baseline，从输出中取**唯一**的 `METRIC <name>=` 名称；出现零个或多个不同名称 → 4000 错误并
  回显候选名，不做猜测。`name` 省略时用分支 slug。baseline 的输出同时写入 run `0000`。
- `run` 固定执行 `bash autoresearch.sh`（没有 bash 才回退 `sh`）；不接受模型或客户端提供的命令。
- `log` 的 `metric` 省略时使用 pending run 解析出的主指标；解析不到才要求显式传入。
- `log` 的 `keep` 只提交本次 run 修改的路径；`discard|crash|checks_failed` 只回滚本次 run 修改的
  tracked 文件并删除本次新建的 untracked 文件，run 之前的 dirty 改动保留。
- **斜杠入口**（CLI、网关、TUI/Desktop 三个 surface 的薄封装，均调用上面的 RPC/核心）：

| 命令 | 行为 |
|---|---|
| `/autoresearch [goal]` | 排入一条普通用户回合，要求代理写 `./autoresearch.sh`；不改 system prompt/tool |
| `/autoresearch start [metric]` | `autoresearch.init`；cwd 由斜杠 handler 从会话 workspace 解析（经典 CLI 用本进程 cwd），RPC 直调时 `cwd` 必填 |
| `/autoresearch run` | `autoresearch.run` |
| `/autoresearch keep [desc]` | `autoresearch.log(status=keep)` |
| `/autoresearch discard\|crash\|checks-failed [desc]` | `autoresearch.log(status=…)` |
| `/autoresearch notes [text]` | `autoresearch.notes` |
| `/autoresearch status` | `autoresearch.status`（只读，但仍需会话 workspace，走 session-aware 命令路径，不进 `slash_exec`） |
| `/autoresearch clear [reset]` | `autoresearch.clear`（`reset` → `reset_tree=true`） |
| `/autoresearch off` | **客户端本地**收起面板/停止提示；不调用 host，不删除状态 |

- **不写会话模式字段**：会话中不存在“autoresearch on/off”状态；面板展示的永远是
  `autoresearch.status` 的返回值。因此 `/autoresearch` 不会改变任何会话行为，`off` 只是 UI 收起。
- **驱动模型（显式二选一：面板/宿主导向）**：模型只负责写 harness 与改代码；`run` 之后
  `autoresearch.status` 额外返回 `recommendation ∈ {keep, discard, none}`（由 `direction`、pending
  run 与 best metric 计算），面板据此展示建议，用户确认后调 `log`。host 不自动提交、不自动开下一
  轮。**不采用**“专用 session 的 service-gated tool”：那会改变该会话对模型可见的 tool 面，为一条
  工作流引入第二套工具生命周期，收益不足以抵消窄腰与缓存代价。
- 循环由显式动作推进：`run`/`keep`/`discard` 各是一次 RPC。没有 turn-end 钩子、没有后台续跑；
  `/loop` 与 cron 只排普通 prompt，不能替代这些状态转换，本 ADR 不把它们接成自动循环。

### D2. Autoresearch 状态、预算与恢复

- 状态根：`get_hermes_home() / "autoresearch" / <cwd-key>`；`cwd-key` = 规范化 realpath 的 basename
  slug + `sha256(realpath)[:12]`。`session.json` 原子写入（临时文件 + `os.replace`）。
- 字段：`version, name, goal, primary_metric, metric_unit, direction, branch, baseline_commit,
  baseline_metric, segment, max_iterations, scope_paths, off_limits, constraints,
  secondary_metrics, notes, ideas, pending_run, runs[]`。
- `runs[]`：`run_number`（int）、`id`、`status`、`metric`、`metrics`、`asi`、`description`、`commit`、
  `started_at`/`finished_at`（epoch ms）、`duration_seconds`、`exit_code`、`log_path`、`modified_paths`、
  `scope_deviations`、`justification`、`flagged`、`flagged_reason`、`confidence`、`segment`。
- `status` 的 `session` 额外包含 `best_metric`、`confidence`（由 runs 计算）；客户端映射到 dashboard 的
  `bestMetric`/`confidence`，`sessionId` 留空（Hermes 用 `session_id` 信封字段）。
- run 产物：`runs/<0001>/benchmark.log`；日志有字节/行上限，完整输出路径写回结果。
- best metric 只统计 `keep` 且未 flagged 的 run；`direction` 决定比较方向；`max_iterations` 是软上限，
  达到后 `run` 拒绝，`init(new_segment=true)` 开新 segment。
- host 状态是唯一真相源；不存在后台自动 resume。客户端重新调用 `status`/`run` 即可继续；每次 RPC
  只做一次状态转换，重复的 `run` 在存在 `pending_run` 时返回 4090/明确错误，不重复执行。

### D3. Autoresearch 安全与 Git 边界

- 所有 Git 操作使用 argv 列表，禁止 `shell=True`；不解析/执行模型提供的任意命令。
- 非 Git 仓库或纯 Jujutsu 拒绝并说明缺少分支隔离/baseline 回滚；不静默假装有隔离。
- `clear(reset_tree=true)` 才执行 `git reset --hard <baseline>` + `git clean -fd`；默认保留工作树。
- `off_limits` 命中或 `scope_paths` 之外的 keep 会记录 `scope_deviations`；没有 `justification` 时在
  `status` 中标记，不阻止记录，但后续 prompt 会提示。
- `flag_runs` 只影响统计，不改写历史 run。
- 子进程环境绑定调用者 profile；分支名/路径不含用户控制的任意片段（slug 只保留 `[a-z0-9-]`）。

### D4. Cleanse 是 host 编排器，斜杠命令与 RPC 共用同一核心

新增 `agent/cleanse/`（发现、解析、加权、派发、复验），并暴露：

```text
cleanse.plan    ({session_id?, cwd?, profile?, run_id, request?, all?, max_agents?, include_tests?,
                  selected?, timeout_seconds?, discover_only?}) -> {result: plan}
cleanse.repair  ({session_id?, cwd?, profile?, run_id, request?, all?, max_agents?, include_tests?,
                  selected?, timeout_seconds?, model?}) -> {result: repair report}
cleanse.cancel  ({session_id?, cwd?, profile?, run_id}) -> {result: {cancelled: bool}}
```

配套 notification（S→C，发给发起调用的 connection；声明在 `tui_gateway/contracts/events.py`）：

```text
cleanse.phase            {session_id?, run_id, text: string|null}
cleanse.checker          {session_id?, run_id, phase: started|finished,
                          checker: {id, label}, duration_ms?, diagnostics_count?, error?}
cleanse.agent            {session_id?, run_id, phase: started|progress|finished, name,
                          assignment?: {index, groups, weight},
                          progress?: {intent, tool, tool_count, elapsed_ms},
                          outcome?: {success, error?, resolved_model?}}
cleanse.log              {session_id?, run_id, line, level: info|error}
cleanse.repair.finished  {session_id?, run_id}
```

- 这五个事件覆盖 `CleanseStatusBoard` 的全部方法（checker/agent 用 `phase` 区分
  started/finished）：`phase` / `checkerStarted|Finished` / `agentStarted|Progress|Finished` /
  `repairFinished`；`cleanse.log` 对应 `print`/`printError` 的永久行。
- `run_id` 由客户端生成；`plan`/`repair` 的响应仍是权威结果，事件只驱动 live board。客户端丢弃
  不属于当前 `run_id` 的事件。
- `plan` 结果包含 `status`、`checkers[]`（`{id, label, available, skipped_reason?}`）、
  `batches[]`（`{index, files, weight}`）与解析后的 `diagnostics[]`；无 REQUEST 时的 checker picker
  直接消费 `checkers`。
- `discover_only=true` 只做发现（不跑 checker、不解析、不分批），供 UI picker 使用；picker 选定后
  再调 `cleanse.plan(selected=[...])` 或 `cleanse.repair`。

- `cleanse.cancel` 按 `run_id` 取消正在执行的 `plan`/`repair`：host 为每个 run 保留一个
  `threading.Event`（或 asyncio equivalent），checker 子进程 terminate、worker delegation 取消，
  不再发射该 `run_id` 的事件；被取消的调用以 `status="cancelled"` 返回，`cancelled=true`。未知/已
  结束的 `run_id` 返回 `cancelled=false`，不报错。`cancel` 本身是短操作，留在 inline。
- `cleanse.agent` 是专用事件而不是复用 `subagent.*`：board 需要 `assignment`（文件组/权重）与
  `outcome.resolved_model`，而 `SubagentEventPayload` 没有这些字段；`subagent.*` 继续服务 Agent Hub，
  两者互不替代。
- `cleanse.plan` 只发现/运行/解析/分批，不派发；`cleanse.repair` 派发后必须重跑 checker。
- 两个方法都返回 `status` 字段，**不定义进程退出码**：
  `plan.status ∈ {ok, empty, unsupported, cancelled}`；`repair.status ∈ {clean, unresolved, unsupported, cancelled}`。
- 无 `REQUEST`：发现项目 checker；`all=true` 跑全部，否则默认跑全部已发现的确定性 checker
  （不交互），并在结果里列出可用 checker。`REQUEST` 无法映射到可用 checker 时返回
  `status="unsupported"`，不伪造“已修复”。
- **斜杠入口**：

| 命令 | 行为 |
|---|---|
| `/cleanse [REQUEST]` | `cleanse.plan`，只计划不修复 |
| `/cleanse repair [REQUEST]` | `cleanse.repair`，显式派发 |
| `/cleanse cancel` | `cleanse.cancel`（面板 `Esc` 的同一条路径） |
| `/cleanse status` | 展示最近一次 plan/repair 的客户端结果（不新增 host 方法） |

- `max_agents`、`include_tests`、`selected`、`timeout_seconds`、`model` 由面板/客户端通过 RPC 传入；
  斜杠命令只暴露 `repair` 与 `REQUEST`，避免再造一套 argv 解析。

### D5. Cleanse 的 checker、解析与加权

- 发现顺序：项目本地 `node_modules/.bin`、`.venv/bin`、`venv/bin`、`vendor/bin`，再 PATH；cwd 不得
  逃逸项目根。缺失可执行文件记入 `skipped`，不是错误。
- 首批支持：Rust（`cargo clippy --message-format=json`，回退 `cargo check`）、Go（`go vet`/
  `staticcheck`/`golangci-lint`）、Python（`ruff`/`pyright`/`mypy`/`pylint`/`flake8`）、JS/TS
  （`tsc --noEmit --pretty false`/`eslint -f json`/`biome`/`oxlint`），以及 gcc-style
  `file:line:col: severity: message` 的 `generic` 回退。
- 解析后统一为 `{checker,file,line,column,severity,code,message,suggestion}`；路径必须在项目根内；
  按 `(checker,file,line,column,code,message)` 去重。
- 权重：error 5 / warning 3 / info 1；文件级诊断 +2；无 code +1；无 suggestion +1。按降序权重装入
  最多 `max_agents` 个文件不重叠的批次；项目级诊断单独成批。
- 复验：修复后重跑同一组 checker；仍有诊断则 `unresolved`，不把“子代理说修好了”当作通过。

### D6. Cleanse 的 worker 派发与审批

- 核心接受注入的 `dispatch(batch, prompt) -> {success, output, error}`；默认实现使用现有
  `tools/delegate_tool.py` 的派发接口（agent 工具 / 进程内 delegation 服务），每个批次一个 worker，
  cwd 固定项目根。**不新增 CLI 入口、不新增子进程命令**；测试注入 fake dispatcher。
- `cleanse.repair` / `/cleanse repair` 是显式用户动作；worker 环境绑定当前 profile，使用
  `build_subprocess_env`/`served_profile_child_env`，不泄漏 launch profile 的 `.env`。
- 子代理 prompt 必须包含：诊断列表、写作用域、允许读的范围、禁止抑制诊断、禁止再派生子代理、
  禁止全项目 formatter、完成后只报改动/未解决项。
- 失败/超时/冲突不丢结果：worker outcome 记录到报告的 `dispatches[]`；批次失败不中止其他批次。
- 默认模型：配置的 `auxiliary` 小模型（若可用），否则父模型；RPC `model` 覆盖。预算沿用
  `delegation.child_timeout_seconds` 与请求的 `timeout_seconds`。

### D7. 斜杠命令与 skill

- **斜杠命令不是 CLI 子命令**：在 `hermes_cli/commands.py` 注册
  `CommandDef("autoresearch")` / `CommandDef("cleanse")`，用 `subcommands` 提供补全；执行接缝分三处，
  均只做参数解析与渲染：
  - 网关/TUI/Desktop：注册到 `gateway/session_commands.py::execute_command` 的本地命令路径——该路径
    已包 `_profile_runtime_scope(home)` 与 `_session_workspace(runner, live.source)`，能拿到**会话
    workspace/profile**；不新增 `GatewaySlashCommandsMixin` 子类，也不依赖 `slash worker`。
  - 经典 CLI：`hermes_cli/cli_commands_mixin.py` 就地处理，cwd 用本进程 cwd。
  - 客户端面板：直接调 RPC，`cwd` 显式传入。
- **在 `gateway/session_commands.py` 的 verdict 表登记**：`status` → `_INFO_READS`（`read`）、
  `init`/`run`/`log`/`notes`/`clear`/`plan`/`cancel` → `_CONTROL_COMMANDS`（`control`）、`repair` →
  `_APPROVAL_COMMANDS`（`session:approve`，因为它改代码）。漏登记会落到 `refused`，命令静默不可用。
- **不要挂到 `hermes_cli/slash_exec.EXECUTORS`**：该表的契约是只依赖 `ctx.args`/`ctx.options`、无会话
  上下文；`slash worker` 的 cwd 是网关进程的 `os.getcwd()`（Desktop 池化后端下不是会话目录），会让
  `init`/`plan` 指向错误仓库。`status` 虽只读，同样需要会话 workspace，也不放；`slash_exec` 继续只放
  纯格式化、无状态 executor。
- 新增 bundled skill `skills/software-development/autoresearch/SKILL.md` 与
  `skills/software-development/cleanse/SKILL.md`：何时使用、RPC/斜杠契约、代理在各阶段的职责与
  护栏。skill 只引用 `terminal`/`read_file`/`patch`/`delegate_task` 等原生工具，**不得声称存在
  `hermes autoresearch|cleanse` 命令**；skill 不复制 host 逻辑。
- Autoresearch skill 的核心职责：教会代理写合规的 `autoresearch.sh`（退出 0、打印
  `METRIC name=value`）、只在 scope 内改代码、不修改基准判据、在被要求时更新 notes。
- Cleanse skill 的核心职责：说明“发现/计划/修复/复验”的角色分工；worker 的护栏由 host 生成的
  prompt 强制执行，skill 不替代 host 校验。

### D8. 网关契约、profile 与能力声明

- 新方法落在 `tui_gateway/methods_autoresearch.py` / `methods_cleanse.py`，声明在
  `tui_gateway/contracts/research.py`，由 `contracts/__init__.py` 引入；`server.py` 的 split-module
  列表注册它们。新事件声明在 `tui_gateway/contracts/events.py`（`event(...)`），由同一模块 emit。
- 会话方法（带 `session_id`）必须 `@_profile_scoped`；无会话方法（只带 `profile`）用
  `tui_gateway/methods_tools.py::_profile_scoped_rpc`，绑定 home + secret + terminal scope。
  `cwd` 缺省为调用者会话 cwd；**无会话时 `cwd` 必填**（4000），绝不静默用 `os.getcwd()`。
- 参数/结果模型遵守 `tui_gateway/contracts/base.py`：`extra="forbid"`、snake_case、`JsonValue`
  承载核心模块的开放结构；不手写生成的 TS/OpenRPC 文件，改模型后运行
  `scripts/gen_gateway_contracts.py`。
- `gateway/panel_inventory.py` 与 `docs/omp-ui-panels.json`：**拆掉合并行**
  （`autoresearch / cleanse / ps-top / live`），改为三行：
  - `autoresearch`：方法 `autoresearch.status/init/run/log/notes/clear`；事件
    `autoresearch.updated/run.started/run.finished`。
  - `cleanse`：方法 `cleanse.plan/repair/cancel`；事件
    `cleanse.phase/checker/agent/log/repair.finished`。
  - `ps_top_live`：沿用原 `process.list`/`voice.*` 方法，`partial`，`note` 说明
    “ps-top 需要 process.list stats（E）；live visualizer 未移植”。
  - 状态：方法+事件落地前 `unsupported`（`supported:false`）；落地后 `pending`；客户端 controller
    接线并通过实机验证后 `ready`。`runtime.describe.panels` 的 `methods` 只列**今天可服务**的方法
    （authority/sidecar）；`PanelCapability` 增加 `events: list[str] = []`（与
    `TranscriptBlockCapability.events` 同形），只列已有 producer 的事件：
    `events: [e.name for e in panel.events if e.status in (AUTHORITY, SIDECAR)]`。客户端只调用/订阅
    列出项，未列出的 surface 渲染 `pending`，不发请求也不伪造数据。改完运行
    `scripts/gen_panel_inventory.py` 与 `scripts/gen_gateway_contracts.py`。
- 事件只发给发起调用的 connection（本地 viewer，绑定 transport），不广播到其他会话；带 `run_id` 的
  事件必须原样回显该 id，会话事件带 `session_id`，客户端据此丢弃过期事件。
- 长任务必须登记 `tui_gateway/server.py::_LONG_HANDLERS`，由 `tui_gateway/rpc_dispatch.py` 送 RPC
  pool：`autoresearch.init`（跑 baseline）、`autoresearch.run`（benchmark）、
  `autoresearch.clear`（`reset_tree` 时 `git reset --hard` + `git clean -fd`）、`cleanse.plan`
  （跑 checker）、`cleanse.repair`（checker + 并行 worker）。否则它们占住单条 RPC 读循环，冻结
  TUI/Desktop 的所有请求（含 `session.info`、中断）。`autoresearch.log`/`notes`/`status` 保持 inline。
- 用户文档：`website/docs/user-guide/features/autoresearch.md`、`cleanse.md`（或现有 CLI 参考页的
  一节），说明斜杠命令与 RPC；因为不新增子命令，`hermes --help` 与 CLI 参考页不变。

### D9. 边界与不变量

- 不实现 omp eval kernel 的 `ratchet()`/`agent()`/`wait()`/`workpool()`/`judge()`/`completion()`
  prelude；host 编排替代。
- 不新增 transcript 行、不改 system prompt 的 tool 列表、不做后台自动续跑；`/autoresearch` 与
  `/cleanse` 只排普通回合或调用 RPC。
- **不新增 CLI 子命令**：不动 `hermes_cli/subcommands/`、`hermes_cli/main.py` 的
  `_BUILTIN_SUBCOMMANDS`；不定义 `--json`/退出码。核心模块不得 import argparse。
- 所有写入路径用 `get_hermes_home()`；不硬编码 `~/.hermes`。
- RPC 结果与斜杠命令渲染使用同一核心返回结构；不维护第二套字段名。

### D10. omp UI：dashboard 与 cleanse overlay 是 host 状态的纯投影

**保留的交互**（按 plan §0.1，客户端交互只读）：

- **Autoresearch**：命令面按 D1（`goal|start|run|keep|discard|notes|status|clear|off`）；`Ctrl+X`
  折叠/展开 dashboard widget；`Ctrl+Shift+X` 打开 overlay。widget 展示实验名、mode、goal、baseline、
  best、当前 run 与耗时；overlay 展示完整 run 表。键位沿用 omp。
  - 与 omp 的差异：omp 的空参数 `/autoresearch` 在 mode 开启时关闭；Hermes 没有会话 mode，空参数
    等价于重新排 kickoff，关闭用 `/autoresearch off`；`off` 只在客户端收起，不改 host 状态。
- **Cleanse**：`/cleanse [REQUEST]` 在 composer 上方挂 anchor overlay；phase spinner、每 checker 一行、
  每 worker 一行（意图/当前工具/调用数/耗时），结束行沉淀到永久日志；`Esc` 先 `cleanse.cancel`、
  等调用以 `cancelled` 返回后再关闭面板；终态 `clean`/`unresolved`/`unsupported`/`cancelled` 与
  `CleansePanelComponent` 一致。
  - 与 omp 的差异：omp 的 `/cleanse` 直接修复；Hermes 的 `/cleanse` 先 `cleanse.plan`，overlay 在
    计划后提供 Repair 动作（等价 `/cleanse repair`），保持 D4 的显式审批边界。

**不复刻**：

- `api.setActiveTools([...EXPERIMENT_TOOL_NAMES])` 与 `runtime.autoresearchMode` 的工具挂/卸：
  Hermes 没有 in-process extension host，也不允许按模式改 tool schema（prompt cache）。dashboard 只是
  投影；`/autoresearch off` 只在客户端收起面板，不“卸载工具”（本来就没有加载）。
- omp 的 extension `api.on("agent_end")` 本地重放与 SQLite 读取：客户端不在本地读状态库，只调
  `autoresearch.status` 并订阅事件。
- ps-top / live visualizer：不在本 ADR。

**Autoresearch dashboard 字段映射**（`AutoresearchDashboardRuntime`）：

| omp 字段 | Hermes 来源 |
|---|---|
| `state` | `autoresearch.status.result.session`（name/goal/metric/direction/baseline/runs/segment/...） |
| `lastRunSummary` | `status.result.session.runs[-1]`（number/passed/primary） |
| `runningExperiment` | `autoresearch.run.started`（started_at/command）；`run.finished` 清除 |
| `autoresearchMode` | host 有该 cwd 的 active session，且客户端未本地 dismiss；**不持久化会话模式** |
| `dashboardExpanded` | 客户端本地状态 |

`ExperimentState` / `ExperimentResult` 映射（客户端在 `omp-ui/src/modes/rpc/` 做 snake_case→camelCase，
不改 host 字段名）：

| omp 字段 | Hermes 字段 |
|---|---|
| `results[]` | `runs[]`（`run_number/status/metric/metrics/asi/description/commit/confidence/flagged/flagged_reason/modified_paths/scope_deviations/justification/segment`） |
| `runNumber` / `timestamp` | `run_number` / `started_at`（epoch ms） |
| `bestMetric` / `confidence` | `status.session.best_metric` / `status.session.confidence` |
| `bestDirection` | `direction` |
| `metricName` / `metricUnit` | `primary_metric` / `metric_unit` |
| `secondaryMetrics` | `secondary_metrics` |
| `name` / `goal` / `branch` / `baselineCommit` | 同名字段 |
| `currentSegment` / `maxExperiments` | `segment` / `max_iterations` |
| `scopePaths` / `offLimits` / `constraints` / `notes` | 对应 snake_case 字段 |
| `sessionId` | 置空（会话作用域用信封 `session_id`） |

- omp 的 autoresearch dashboard 是 extension command + pi-tui widget/overlay（证据：
  `packages/tui/src/apps/autoresearch-dashboard.ts`、`autoresearch-data.ts`、
  `packages/coding-agent/src/autoresearch/index.ts`），不是“无 UI”；Hermes 只把它改成 RPC/事件投影。
- `runningExperiment` 的耗时由客户端从 `run.started.started_at` 本地计时；`run.finished` 的
  `output_truncated`/`run_directory` 供 summary 使用，不移植 `run_experiment` 的 tool renderer。

- 面板打开、session switch/resume、收到 `autoresearch.updated` 时各刷新一次 `status`；不轮询。
- dashboard 的 `AutoresearchDashboardHost` 由 shell 适配：`setWidget(key, component)` 挂到
  `autoresearchContainer`，`custom(..., {overlay: true})` 走现有 overlay 机制；不新增 extension API。
- `status` 的 `recommendation` 驱动 keep/discard 建议按钮；按钮调用 `autoresearch.log`，不自动提交。

**Cleanse board 映射**（`CleanseStatusBoard`）：五个 `cleanse.*` 事件覆盖 board 的全部方法
（checker/agent 用 `phase` 区分 started/finished）；`cleanse.log` → `log`/`logError`；
`cleanse.repair` 的响应 `status` → `panel.finish(status)`；网络/契约错误 → `panel.markError`。无
REQUEST 时的 checker picker 先用 `cleanse.plan(discover_only=true)` 取 `checkers`，选定后再
`cleanse.plan(selected=[...])`；不在客户端重新发现；事件按 `run_id` 过滤，切换会话或开始新 run 时
丢弃旧 run 的事件。

**客户端文件与接缝**：

- `omp-ui/src/modes/controllers/autoresearch-command-controller.ts`（新增）：命令解析、dashboard
  `createDashboardController()` 生命周期、`Ctrl+X`/`Ctrl+Shift+X`、订阅 `autoresearch.*`、调用 RPC。
- `omp-ui/src/modes/controllers/cleanse-command-controller.ts`（从 checkout 恢复并改写）：保留
  `CleansePanelComponent`/`CleanseBoardModel`（来自已安装的 `@oh-my-pi/pi-tui`，不复制进
  `src/vendor/`），把 `runCleanse(...)` 换成 `cleanse.plan`/`cleanse.repair` + 事件订阅。
- `omp-ui/src/modes/interactive-mode.ts`、`modes/types.ts`：新增 `autoresearchContainer` 与
  `cleanseContainer`（沿用 `AnchoredLiveContainer`）、键位、dispose。
- `omp-ui/src/session/agent-session-events.ts`：新增
  `autoresearch_updated` / `autoresearch_run_started` / `autoresearch_run_finished` /
  `cleanse_phase` / `cleanse_checker` / `cleanse_agent` / `cleanse_log` / `cleanse_repair_finished`。
- `omp-ui/src/modes/rpc/gateway-session.ts`：把上述 notification 投影成 `AgentSessionEvent`，并暴露
  `autoresearchStatus/Init/Run/Log/Notes/Clear`、`cleansePlan/Repair` 的薄方法；后者只做参数校验与
  `run_id` 生成。
- `omp-ui/src/modes/rpc/gateway-contract.generated.ts`：由 `scripts/gen_gateway_contracts.py` 重新生成，
  不手改。

**能力门控**：客户端只调用 `runtime.describe.panels.<id>.methods` 列出的方法、只订阅面板行声明的事件；
`supported:false` 时显示 `pending` 行，不挂载 controller、不发请求。`/autoresearch` 或 `/cleanse`
在能力缺失时回一条明确提示（“host backend pending”），不打开空面板。

**测试与验收**：客户端 `bun run check`、`bun run check:imports`、相关 `bun test src/modes/rpc`；
RPC 事件用 fake transport 断言字段/`run_id` 过滤；实机 `bun src/main.ts` 对运行中的网关验证
`/cleanse` board 与 `/autoresearch` widget/overlay、Esc/键位、pending 门控。

## 协议与实现落点

| 文件 | 改动 |
|---|---|
| `agent/autoresearch/{__init__,state,gitops,runner}.py` | 状态、Git、run/log/keep/discard/notes/status/clear |
| `agent/cleanse/{__init__,checkers,parsers,balance,loop}.py` | 发现、解析、加权分批、派发/复验 |
| `hermes_cli/commands.py` | `CommandDef("autoresearch"/"cleanse")` + `subcommands` |
| `hermes_cli/cli_commands_mixin.py` | CLI surface 的 `/autoresearch`、`/cleanse` 参数解析 → 核心函数 |
| `gateway/session_commands.py` | 网关/TUI 的 `/autoresearch`、`/cleanse` 本地命令路径（session workspace + profile scope） |
| `tui_gateway/methods_autoresearch.py`、`methods_cleanse.py` | RPC handler + notification emit |
| `tui_gateway/contracts/research.py`、`contracts/events.py`、`contracts/__init__.py`、`server.py` | 方法/事件契约与注册 |
| `tui_gateway/contracts/canonical.py` | `PanelCapability.events: list[str] = []`（能力协商） |
| `gateway/panel_inventory.py`、`docs/omp-ui-panels.json` | 三面板拆分与能力说明（`scripts/gen_panel_inventory.py` 重新生成） |
| `skills/software-development/{autoresearch,cleanse}/SKILL.md` | 操作手册 |
| `omp-ui/src/modes/controllers/autoresearch-command-controller.ts` | 新增：命令、dashboard widget/overlay、事件订阅 |
| `omp-ui/src/modes/controllers/cleanse-command-controller.ts` | 恢复并改写：`CleansePanelComponent`/`CleanseBoardModel` + RPC/事件 |
| `omp-ui/src/modes/interactive-mode.ts`、`modes/types.ts` | `autoresearchContainer`/`cleanseContainer`、键位、dispose |
| `omp-ui/src/session/agent-session-events.ts`、`agent-session.ts`、`modes/rpc/gateway-session.ts` | notification → `AgentSessionEvent`、RPC 薄方法、`run_id` |
| `omp-ui/src/modes/rpc/gateway-contract.generated.ts` | 生成物（不手改） |
| `tests/agent/test_autoresearch_port.py`、`test_cleanse_port.py` | 状态机/Git/解析/分批/复验 |
| `tests/hermes_cli/test_research_slash.py` | 斜杠参数解析、渲染、无子命令断言 |
| `tests/tui_gateway/test_research_methods.py` | RPC/事件契约、错误、profile 绑定 |
| `tests/gateway/test_canonical_wire_contract.py` | `PanelCapability.events` 生成物与 `capabilities()` 过滤 |
| `omp-ui/src/modes/rpc/gateway-research.test.ts` | notification 投影、`run_id` 过滤、pending 门控 |

**不做**：`hermes_cli/subcommands/{autoresearch,cleanse}.py`、`hermes_cli/{autoresearch_cmd,cleanse_cmd}.py`
（原草案中的 CLI handler）、`hermes_cli/main.py` 改动、`tests/hermes_cli/test_autoresearch_cleanse_cmd.py`。

## 分阶段

1. **P1 确定性核心**：`agent/autoresearch/`、`agent/cleanse/` + 单测。
2. **P2 网关后端**：`methods_autoresearch.py`、`methods_cleanse.py`、`contracts/research.py`、
   `contracts/events.py`；`gen_gateway_contracts.py` 重新生成；RPC/事件测试。
3. **P3 命令面与 UI**：`CommandDef`、CLI/网关斜杠 handler、skill、三面板拆分；omp-ui 的
   autoresearch controller + dashboard、cleanse controller + overlay、事件投影、能力门控。
4. **P4 硬化**：profile 作用域、预算/超时、并发上限、日志上限、Windows `bash` 缺失、实机联调
   （Esc/键位/pending）、Desktop 复用同一 RPC。

## 验收

1. **Autoresearch init**：临时 Git 仓库要求干净工作树与 `autoresearch.sh`；创建 `autoresearch/*`
   分支并提交 harness；记录 baseline；非 Git 目录拒绝并给出明确错误；`primary_metric` 省略时从
   baseline 输出取唯一 `METRIC` 名称，歧义返回 4000。
2. **Autoresearch run/log**：`run` 解析 `METRIC`/`ASI`、写 run 日志；`keep` 提交修改，
   `discard|crash|checks_failed` 回滚本次改动且保留 run 前的 dirty 改动；flagged run 不参与 best；
   `max_iterations` 生效；重复 `run` 在 pending 时拒绝；`clear reset` 才重置工作树。
3. **Cleanse discovery/parse**：fake checker 输出 gcc-style/JSON 诊断，路径逃逸被丢弃，缺可执行
   文件进入 `skipped`；去重生效。
4. **Cleanse batching**：权重公式正确；批次文件不重叠、数量不超过 `max_agents`；项目级诊断单独
   成批。
5. **Cleanse repair/verify**：`cleanse.repair`/`/cleanse repair` 调用注入的 dispatcher（测试用
   fake）；复验仍有诊断时 `status="unresolved"`；复验清零时 `status="clean"`；worker 失败不中止
   其他批次。`cleanse.cancel` 后 checker 子进程与 worker 停止、不再有该 `run_id` 的事件，被取消的
   `plan`/`repair` 返回 `status="cancelled"` 且 `cancelled=true`；未知 `run_id` 返回 `false`。
6. **RPC**：`autoresearch.*`/`cleanse.*` 全部有契约并通过 `scripts/gen_gateway_contracts.py`；
   未知参数返回 4000；无会话且无 `cwd` 返回 4000（不读 `os.getcwd()`）；无可用 checker 返回
   `unsupported`；`cleanse.plan` 不派发，`cleanse.repair` 必须复验；`cleanse.cancel` 按 `run_id`
   生效；profile 绑定通过双 home 测试；`runtime.describe.panels` 的 `events` 只列已有 producer 的
   事件，并通过 `tests/gateway/test_canonical_wire_contract.py`。
7. **命令面**：`/autoresearch`、`/cleanse` 在 `COMMAND_REGISTRY` 可见且有补全；斜杠 handler 与 RPC
   返回同一结构；`/autoresearch` 进入普通回合且不改 system prompt/tool schema；**`hermes --help`
   不出现 `autoresearch`/`cleanse` 子命令**，`_BUILTIN_SUBCOMMANDS` 未新增。
8. **回归**：`scripts/check`、`scripts/run_tests.sh` 的相关文件通过；不新增 core tool、不改 prompt
   cache。
9. **长任务不阻塞**：`autoresearch.init`/`run`/`clear`、`cleanse.plan`/`repair` 在 `_LONG_HANDLERS`
   中（走 RPC pool）；`autoresearch.run` 执行期间并发调用 `session.info` 与 `session.interrupt`
   仍有响应。
10. **omp UI**：`/autoresearch` dashboard 字段按 D10 映射，`Ctrl+X`/`Ctrl+Shift+X` 与 omp 一致；
    `/cleanse` 的 phase/checker/agent/log 行由事件驱动，`Esc` 调 `cleanse.cancel`、事件停止且 RPC
    返回 cancelled，终态与 `CleansePanelComponent` 一致；`supported:false` 时不挂载 controller、
    不发请求；`bun run check`、`bun run check:imports`、`bun test src/modes/rpc` 通过；实机
    `bun src/main.ts` 对运行中的网关完成一次 `/autoresearch` 与 `/cleanse` 联动。

## 风险

- **实验指标被 gaming**：keep/discard 只看数字，模型可能过拟合 benchmark。缓解：skill 明确“不改
  benchmark 判据、不猜测试输入、保持正确性”；`scope_deviations` 与 `justification` 留痕；可选
  `checks` 脚本在 keep 前跑。
- **Cleanse 子代理越权**：worker 有编辑权限。缓解：写作用域文件不重叠、prompt 禁止越界、
  `cleanse.repair` 显式 opt-in、复验必须通过；审批沿用现有 delegation 边界。
- **无 CLI 导致 headless 不可用**：没有客户端或网关 surface 时，Autoresearch 的
  `init/run/log` 没有调用入口，循环无法推进。这是本 ADR 的直接取舍；缓解：RPC 是稳定契约，
  任何脚本/客户端都可通过 JSON-RPC 驱动；文档明确说明，不提供隐式替代入口。
- **客户端驱动把“自动研究”降级为手动步进**：每轮需要一次显式 `run`。缓解：面板可把 `run` 绑定为
  一次点击；本 ADR 不引入自动续跑（那会变成隐藏 daemon 或会话模式）。
- **成本失控**：Cleanse 可并行很多 worker。缓解：`max_agents` 上限（默认 32）、`timeout_seconds`、
  日志上限。
- **客户端 UI 与 host 漂移**：事件乱序、断线或漏帧会让 board/dashboard 与 host 不一致。缓解：事件带
  `run_id` 与会话级 `seq`（`event_replay`），客户端只接受当前 `run_id`；打开面板、resume、重连时各
  拉一次 `autoresearch.status` / 重新 `cleanse.plan`，不复用旧状态；不轮询、不本地补数据。
- **RPC 契约漂移**：新方法/字段必须走 `scripts/gen_gateway_contracts.py` 与契约测试；不手改生成
  文件。
- **平台差异**：`bash`、Git 分支、路径分隔符。缓解：`shutil.which("bash")` 回退 `sh`；Git 不可用
  明确拒绝；Windows 用 `pathlib`/`os.replace`。

## 不采用的实现方式

- **CLI 子命令**（`hermes autoresearch` / `hermes cleanse`）：会在 RPC 之外再造一套 argv、stdout 与
  退出码契约，三套入口必然漂移；终端用户已有斜杠命令与面板，自动化走 JSON-RPC。本 ADR 明确不新增
  子命令。
- **四个 core tool**（`init_experiment`/`run_experiment`/`log_experiment`/`update_notes`）：进入每次
  请求的 schema，且中途启用会破坏 prompt cache；用 host RPC + 斜杠命令 + skill 达成同样能力。
- **Autoresearch 常驻 daemon 或 turn-end 自动续跑**：会引入第二套生命周期/恢复/预算，并需要会话
  模式字段；每次状态转换显式触发。
- **Cleanse 只读报告**：C 的 host 后端必须能执行修复；但默认计划、`cleanse.repair` 显式 opt-in
  保留安全边界。
- **复用 `hermes verify` 作为 Cleanse**：verify 是“启动/构建/测试”的 recipe，不产出可定位的
  lint/type 诊断，也不做文件分批。
- **omp in-process extension host 与 `setActiveTools` 模式**：会重新引入本地后端与按模式改 tool
  schema，破坏 core 窄腰与 prompt cache；dashboard/overlay 只做 host 状态的投影。
