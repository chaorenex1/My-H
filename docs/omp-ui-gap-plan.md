# omp-ui 差异分析与补全行动计划（已废弃）

> **文档状态：已废弃（2026-11-01）。**
>
> 本文不再作为执行依据，也不得按本文继续派工；保留仅供追溯。现行位置：
>
> | 主题 | 现行文档 |
> |---|---|
> | 面板接通、网关契约、能力协商、阶段 | `docs/omp-ui-panels-gateway-plan.md` |
> | 实时 RPC（server → client）对齐 | `docs/appendix-omp-ui-realtime-rpc.md` |
> | 业务 RPC（client → server）对齐 | `docs/appendix-omp-ui-business-rpc.md` |
> | vendor 来源、依赖规则 R1–R5、本地补丁、重放脚本 | `omp-ui/src/VENDORED.md` |
> | 客户端入口与网关接线 | `omp-ui/src/main.ts`、`omp-ui/src/modes/rpc/gateway-*.ts` |
>
> 仍可引用的事实：§1 的基线测量、§5 的 G1–G4 落地记录、§9 的网关实测结论——它们只作历史证据，
> 与现行文档冲突时以现行文档为准。

> **2026-10-31 方向重定（第二次修正）。**
>
> 旧方向一：在 `omp-ui/app/` + `omp-ui/coding-agent/hermes/` 做一层桥接。**作废**。
> 旧方向二（本文上一版）：在 `src/hermes/` 放客户端。**也作废**——那仍然是“加一层”。
>
> 现在的方向：**就地改 `src` 里已有的文件和类，不新增 `src/hermes/` 或任何平行命名空间。**
> RPC 客户端就是 `src/session/agent-session.ts` 这个类本身；传输代码放在已有的协议目录
> `src/modes/rpc/`。`InteractiveMode` 继续拿同一个 `AgentSession` 类型，**不需要 cast**。

## 0. 决策记录

| # | 决策 | 含义 |
|---|---|---|
| **D1** | 实现位置 | 只有 `omp-ui/src` 的**既有路径**。`omp-ui/app/`、`omp-ui/coding-agent/hermes/` 删除（G6）。不建 `src/hermes/`。 |
| **D2** | 不裁剪 | `src` 的文件与界面能力不删、不拆。改的是实现文件的内容，不是删功能。 |
| **D3** | 依赖边界 = 方案 A | `src` 的直接 import 只允许 `@oh-my-pi/pi-tui` 与 `node:*`/`bun`。其余 `@oh-my-pi/*` 全部 vendor 进 `src/vendor/`。**被取消的依赖代码按需要从本地 checkout `C:\Users\zarag\projects\oh-my-pi` 搬运**（基线 commit `6a86df8539fccf3ad7818081266e554022fc21bc`），不得从 `node_modules` 复制。 |
| **D4** | 客户端落点 | `session/agent-session.ts` 就地重写为网关客户端；`session/session-manager.ts` 就地重写为网关会话/树；WS 传输放 `modes/rpc/`。 |
| **D5** | 未接通能力 | smoke 标记 `pending` / `unsupported` + 原因；不静默 stub、不删代码。 |
| **D6** | 顺序 | vendor 补齐编译 → 边界改写 → 就地重写会话层 → 入口 → 删旧目录 → smoke 收敛。 |

为什么“不新增一层”很关键：omp 壳里的类型是 `AgentSession` / `SessionManager` 这些**类**（带私有字段）。
另建一个客户端类不可能结构满足它们，只能靠 `as unknown as`。把实现放回原路径、原类名，
壳拿到的就是同一个声明，编译器检查才是真实的。

## 1. 基线事实（2026-10-31 实测）

| 指标 | 数值 |
|---|---|
| `src/**/*.ts` | 101 个文件 |
| 缺失的相对模块（全量 `src`） | **273** 个 |
| 缺失的相对模块（仅 `InteractiveMode` 可达集，86 文件） | 218 个 ≈ 117k 上游行 |
| `src` 直接 import `pi-tui` | 434 次（346 值 + 88 类型） |
| 非 tui 直接 import | **169 处**：pi-utils 70（66 值）、pi-ai 43（14 值）、pi-agent-core 31（7 值）、pi-natives 10（8 值）、pi-wire 9（2 值）、pi-catalog 3、omp-stats 2、omptype 1 |
| `modes/**` 使用 `session.*` 不同成员 | **244**（`interactive-mode.ts` 103、`command-controller` 47、`selector-controller` 38、`extension-ui` 29、`input` 29、`event` 19…） |
| `sessionManager.*` 不同成员 | 36 |
| `settings.*` 不同成员 | 18 |
| `interactive-mode.ts` 的 `../` 值导入 | **71 个** |
| `../session/agent-session` 的值符号 | 只有 `SHUTDOWN_CONSOLIDATE_BUDGET_MS`（其余全是 type） |

需要就地重写的两个类（都带私有字段，无法用外部对象替代）：

| 文件 | 上游规模 | 壳的依赖 |
|---|---|---|
| `src/session/agent-session.ts` | 15,409 行 / 662KB | 244 个 `session.*` 成员、`AgentSessionEvent` 产生者 |
| `src/session/session-manager.ts` | 4,763 行 | 36 个 `sessionManager.*` 成员（历史、树、entry、draft） |

`config/settings.ts` 保留上游实现（读文件），但启动时把 omp 配置根指向 Hermes 作用域
（原 `config-home.ts` 的逻辑并入 `main.ts`）。

上游代码量（vendor 预算）：

| 目录 | TS 文件 | LOC |
|---|---|---|
| `packages/coding-agent/src` | — | **527,965** |
| `packages/ai` | 359 | 139,279 |
| `packages/agent` | 37 | 21,482 |
| `packages/utils` | 127 | 38,702 |
| `packages/catalog` | 99（+239 `.kdl`） | 41,979 |
| `packages/wire` | 4 | 2,009 |
| `packages/omptype` | 14 | 10,688 |
| `packages/stats` | 80 | 21,321 |
| `packages/snapcompact` | 2 | 2,502 |
| `packages/natives` | 预编译 N-API（无 TS 源码） | 9,955 行 `.d.ts`/胶水 |

## 2. 目标结构（就地修改，无新层）

```
omp-ui/
├── package.json                 # @oh-my-pi/pi-tui + @oh-my-pi/pi-natives（+ bun types）
├── tsconfig.json                # include: src
└── src/
    ├── main.ts                  # Hermes 入口：argv → InteractiveMode（不经 omp CLI）
    ├── cli.ts                   # 上游 omp CLI；仅保留为 worker-host 回退，不作入口
    ├── session/
    │   ├── agent-session.ts     # ★就地重写：网关客户端实现的 AgentSession（同路径/类名/导出）
    │   ├── session-manager.ts   # ★就地重写：网关历史/树/entry（同路径/类名/导出）
    │   ├── agent-session-events.ts / agent-session-types.ts / session-entries.ts / messages.ts  # 保留上游
    │   └── ...                  # 其余上游文件保留
    ├── modes/
    │   ├── interactive-mode.ts  # 壳：原则上不动；只在需要处改类型/开关
    │   ├── controllers/         # 就地接线：approval/ask、命令、面板、todo、通知
    │   └── rpc/
    │       ├── wire/**          # 协议词汇表（保留）
    │       ├── rpc-types.ts     # 命令/结果类型（保留）
    │       └── gateway-client.ts # 新增：Hermes WS JSON-RPC 传输（落在已有协议目录）
    ├── vendor/                  # 方案 A：pi-* 本地副本
    └── smoke.ts                 # 无头回归（状态标记）
```

不新增的：`src/hermes/`、`src/client/`、任何以产品名开头的命名空间。
新增的文件只有两类：`modes/rpc/gateway-client.ts`（协议传输，属于既有协议目录）和 `smoke.ts`。

### 边界规则

| 规则 | 判定 |
|---|---|
| R1 | `src/**/*.ts` 的 import specifier 不得以 `@oh-my-pi/` 开头，**除 `@oh-my-pi/pi-tui`** |
| R2 | 不得出现越过 `src/` 的相对路径 |
| R3 | 不得 import `@oh-my-pi/pi-coding-agent` |
| R4 | `vendor/**` 内部允许互相引用，对外只暴露与上游一致的入口文件 |
| R5 | `package.json` 的 dependencies 只有 `@oh-my-pi/pi-tui` |

纯度门：`omp-ui/scripts/check-imports.ts`（R1–R3 + 相对模块解析 = 0 + vendor 摘要）。

## 3. Vendor 清单（从 `projects/oh-my-pi` 搬什么）

**要求（D3/R5 的执行口径）：** 依赖被取消后，其代码从本地 checkout
`C:\Users\zarag\projects\oh-my-pi`（HEAD `6a86df8539fccf3ad7818081266e554022fc21bc`）**按需复制**到
`src/vendor/`：以当前 import 闭包为准（壳的 import + vendor 内部跨包 import），只搬可达文件，
不预搬未用模块；`.kdl`/JSON 等被引用的数据文件随模块一并复制。React client 等不可达部分不搬。
不得从 `omp-ui/node_modules` 复制（发布包可能缺数据文件或本地补丁）。
未搬运前，`src` 的非 tui import 仍会命中 pi-tui 的传递依赖——那只是过渡态，不算完成 R1。

上游 commit：`6a86df8539fccf3ad7818081266e554022fc21bc`（2026-10-30，`@oh-my-pi/*@18.14.0`）。

| 来源 | 目标 | 处置 |
|---|---|---|
| `packages/coding-agent/src/**` | `omp-ui/src/**` | 补齐缺失文件；**不删已有文件**；随后按 §4 就地重写两个类 |
| `packages/wire/src/**` | `src/vendor/wire/` | 按需闭包 |
| `packages/ai/src/**` | `src/vendor/ai/` | 按需闭包 |
| `packages/agent/src/**` | `src/vendor/agent/` | 按需闭包 |
| `packages/utils/src/**` | `src/vendor/utils/` | 按需闭包 |
| `packages/catalog/src/**` | `src/vendor/catalog/` | 按需闭包（含被引用的 `.kdl` 数据） |
| `packages/omptype/src/**` | `src/vendor/omptype/` | 按需闭包 |
| `packages/stats/src/**` | `src/vendor/stats/` | 按需闭包（shell 的 server 侧入口；React client 不搬） |
| `packages/snapcompact/src/**` | `src/vendor/snapcompact/` | 按需闭包 |
| `packages/natives/native/**` | `src/vendor/natives/native/` | 按需闭包：导入子路径可达的 JS + `index.d.ts`，不提交 `.node`；见下补丁 |

### 导入改写（不能按前缀 sed）

1. 从每个包的 `package.json#exports` 建立 `specifier → 源文件`（精确键 + `./*`、`./*.js` 通配）。
2. 覆盖 `import … from`、`import()`、`require()`；已知 `src/cli.ts:255,590` 使用
   `require("@oh-my-pi/pi-utils/postmortem.js")`。
3. 目标为 `src/vendor/<pkg>/<源路径>`；`@oh-my-pi/pi-tui[/sub]` 保持不动。
4. 改写表与命中点写入 `src/VENDORED.md`，由 `scripts/sync-upstream.sh` 重放。

### 本地补丁集（记录在 `src/VENDORED.md`）

1. **natives 懒失败**：`vendor/natives/native/index.js` 顶层 `loadNative()` 包 try/catch；
   失败时置空绑定，并让 `missingNativeExport()` 在“无 addon”时也返回 thrower（上游只在 `stale` 时返回）。
   `adaptDesktopSession(undefined)` 已验证可安全返回 `UnsupportedDesktopSession`。
   `HERMES_OMP_NATIVE_DIR` 可指向真实 `pi_natives.<platform>.node`；vendored 位置
   `isWorkspaceLoad=true`，绑定可直接放 `native/`，不需要平台 npm 包。
2. **导入改写**：由上面的 `exports` 表生成。

## 4. 就地改造清单（这是“改 src”的具体内容）

### 4.1 `session/agent-session.ts`（重写实现，保留模块契约）

- 保留导出：`AgentSession`（类）、`SessionBusyError`、`NavigateTreeResult`、
  `SHUTDOWN_CONSOLIDATE_BUDGET_MS`，以及壳/其他保留文件用到的事件与类型再导出。
- 类内部改为：持有 `GatewayClient`；`prompt/abort/steer/navigateTree/…` 映射到网关；
  网关事件在类内转成 `AgentSessionEvent` 并 `emit` 给订阅者（原 `projector.ts` 的逻辑并入此文件或其
  私有 helper）。
- 壳用到的 244 个成员全部有实现；保留文件（`rpc-mode.ts`、`acp-agent.ts` 等）引用但运行路径不走的成员，
  声明为抛 `Unsupported(capability)`，由 smoke 标 `pending`/`unsupported`。
- **无 cast**：类本身就是 `AgentSession`，但它的**字段类型**也是类（带私有字段），必须逐个决定怎么给值。

| 字段 | 类型 | 壳的用法（实测） | 决策 |
|---|---|---|---|
| `agent` | `Agent`（class） | `waitForIdle`/`state`/`promptCacheKey`/`metadataForProvider`，11 处 | **构造真实 `new Agent()`**。`AgentOptions` 全可选；`Tokenizer` 只读 `Encoding` 枚举对象 + snapcompact，不碰原生绑定，构造无 I/O。网关事件同步 `agent.state.messages/model/isStreaming`；`waitForIdle` 在非流式时立即 resolve |
| `sessionManager` | `SessionManager`（class） | 36 个成员 | 同文件就地重写（§4.2），字段类型不变 |
| `goalRuntime` | `GoalRuntime`（class） | 9 个方法（`dropGoal`/`resumeGoal`/`createGoal`…） | **构造真实实例**：`GoalRuntimeHost` 是 interface，实现一个把 `persist`/`emit`/`sendHiddenMessage` 转发到 `session.control.*` 的 host |
| `extensionRunner` | `ExtensionRunner \| undefined` | 10 处 `?.` 调用 | 字段本身可选，壳处处 `?.`：**保持 `undefined`**，不构造 |
| `modelRegistry` | `ModelRegistry`（class） | 11 个成员 + `authStorage` 21 处 | 先构造真实实例：`AuthStorage(SqliteAuthCredentialStore(<Hermes 作用域路径>))` + `new ModelRegistry(auth, undefined, { ignoreLocalModelConfig: true })`（只读 bundled catalog）。若构造器产生不可接受的 I/O，则把 getter 收窄为 `ModelRegistrySurface` / `AuthStorageSurface` 并改这 11+21 处调用点，仍然**不引入 cast** |
| `settings` | `Settings`（class） | 18 个成员 | 保留上游实现；启动时把配置根重定向到 Hermes 作用域 |
| `memoryEnabled` / `yieldQueue` / `editStore` / `getXdevToolEntries` | 值/可选 | 少量 | 直接给值（空队列、`undefined`、空数组） |

G3 的第一步是跑一个构造探针：`new Agent()`、`new GoalRuntime(host)`、`new ModelRegistry(...)` 在无网关、
无原生绑定的进程里能否安全构造；探针结果决定上表最后两个“先构造/否则收窄”的分支。

### 4.2 `session/session-manager.ts`（重写实现，保留模块契约）

- 36 个成员：`getBranch/getEntries/getTree/getLeafId/getEntry/buildSessionContext/…` 由网关
  `session.resume`/`session.history` 的行（含 `row_id`）线性投影；
  `append*`/`saveDraft` 等写操作映射到网关或声明 `Unsupported`。
- 壳里的 `session.sessionManager` 类型不变，无需 cast。

### 4.3 `modes/rpc/gateway-client.ts`（新增传输）

- `gateway.ready` → `client.capabilities`；JSON-RPC 请求/响应；服务端请求（`approval`/`clarify`/`sudo`/
  `secret`/`connection`）与 `request.cancel`；事件 envelope 解码。
- 只被 `agent-session.ts` 使用；不暴露给壳。

### 4.4 壳文件就地接线（原旧桥的逻辑按位置归位）

| 旧 `coding-agent/hermes/*` 逻辑 | 新落点（既有文件） |
|---|---|
| `session.ts` + `projector.ts` | `session/agent-session.ts` |
| `messages.ts` | `session/messages.ts`（行 → `AgentMessage`，含 `row_id`） |
| `dialogs.ts` | `modes/controllers/event-controller.ts`（approval/ask/sudo/secret 的对话框路由） |
| `commands.ts` | `modes/controllers/command-controller.ts` + `main.ts`（catalog 注入） |
| `skin.ts` | `modes/interactive-mode.ts` 的 theme 初始化 + `modes/settings.ts` |
| `panels.ts` / `manage.ts` / `settings.ts` / `browse.ts` | 对应 `modes/controllers/{selector,todo,mcp-command}-controller.ts` 等 |
| `todos.ts` / `notifications.ts` / `side.ts` / `control.ts` | `modes/controllers/{todo,event,btw}-controller.ts` 与 `modes/status-line-host.ts` |
| `gateway.ts` | `modes/rpc/gateway-client.ts` |
| `main.ts` | `src/main.ts`（Hermes 分支） |
| `smoke.ts` | `src/smoke.ts` |

### 4.5 入口

- `src/main.ts`：Hermes 入口。自行 `parseArgs` 后直接 `runRootCommand` → `InteractiveMode`；
  不加载 omp 的 CLI 命令表/子命令/profile 开关/预绘制分发。网关 `AgentSession` 与 `InteractiveMode`
  的装配仍在 G3。
- `src/cli.ts`：上游 omp CLI 原样保留，仅作为 worker 回退宿主（spawn 的 worker 以隐藏 argv
  选择符重新进入它）；`hermes omp` 不经过它。
- `hermes_cli/omp_launch.py`：entry 改为 `src/main.ts`，环境变量不变。

## 5. 迁移阶段

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| **G0** | 本文落盘；`app/`、`hermes/` 冻结 | 方向记录在案 |
| **G1** | 按需补齐 `src` 的缺失相对模块；按需复制 `vendor/`；生成 manifest | 纯度门“未解析相对模块” = 0 |
| **G2** | `exports` 表驱动的导入改写；R1–R5 | `check:imports` 通过；deps 只剩 tui + natives |
| **G3** | 就地重写 `agent-session.ts`、`session-manager.ts`；新增 `modes/rpc/gateway-client.ts`；壳文件接线 | `tsc` 通过；真实网关 + 测试终端渲染 welcome/status/一轮对话 |
| **G4** | `main.ts` 直接入口（`omp_launch.py` 指向 src） | PTY 实跑 `hermes omp` |
| **G5** | `src/smoke.ts` 状态机；迁移旧 40 条检查 | `ok/pending/unsupported` 三段，无 `FAIL` |
| **G6** | 删除 `omp-ui/app/`、`omp-ui/coding-agent/hermes/` | 仓库内无引用 |
| **G7** | 清理与文档 | 见 §11 |

G6 之前不删旧目录，避免 `hermes omp` 出现无入口空窗。

**G1/G2 状态（已落地）**：`omp-ui/src` 是 `packages/coding-agent/src` 在 `6a86df853` 的逐字节
快照（1456 个 `.ts` 加数据文件；重放脚本 `omp-ui/scripts/sync-upstream.py`）。除 `@oh-my-pi/pi-tui`
与 `@oh-my-pi/pi-natives` 外的 `@oh-my-pi/*` 依赖按 import 闭包 vendor 到 `src/vendor/`（752 个文件，
含 pi-tui 自己的 peer import 种子），所有 specifier 改写为相对路径；`pi-natives` 从 package.json
装入（平台 `.node` 走其 `optionalDependencies`，版本随壳所需符号固定为 18.14.1）；`tsconfig.json` 的
`paths` 让壳与 pi-tui 内部指向同一份 vendored 模块（`const enum`/类只有一个身份）。
`src/VENDORED.md` 记录来源、布局、入口补丁与 assets 迁移（R2）。原 `app/`、
`coding-agent/hermes/` 从未进入本仓库快照，G6 无对象。验证：`bun run check:imports`（2164 文件，
0 未解析，R1–R3/R5 干净）、`bun run check` 通过、`bun src/main.ts --version` 输出 shell 版本、
`bun test src/modes/rpc/gateway-client.test.ts src/modes/rpc/gateway-socket.test.ts` 31 通过。

**G4 入口（已落地）**：`hermes_cli/omp_launch.py` 的 entry 已改为 `omp-ui/src/main.ts`，
`--resume` 透传，omp 的 agent 目录（配置/会话/auth）指向 `<HERMES_HOME>/omp-ui/agent`
（`PI_CODING_AGENT_DIR`），不再读写 `~/.omp`；`--no-start` 随旧网关启动语义一并移除。
`HERMES_OMP_START` / `HERMES_OMP_RESUME` 不再设置。omp 的 CLI 命令表/子命令/profile 开关/
预绘制分发不在 `hermes omp` 路径上（`src/cli.ts` 仅作 worker 回退宿主）。

**G3 主界面（已落地，2026-11-01）**：`src/main.ts` 不再走 `runRootCommand`/`createAgentSession`。
它构建最小本地依赖（`Settings`、只读 catalog `ModelRegistry`、内存 `SessionManager`、`Agent`），
通过 `src/modes/rpc/gateway-session.ts` 连接配对网关，`session.create`/`session.resume` 取得快照，
把历史灌进 shell，再 `AgentSession.attachGateway()` 让网关成为**唯一的回合来源**。没有
“omp 模式 / Hermes 模式”开关：`hermes omp` 就是网关客户端，`src/cli.ts` 的本地路径只留给被 spawn 的
worker 宿主，后续按使用情况删除。

已通的数据：prompt/steer/follow-up/中断（带 generation 重试）、`message.*`、reasoning、
`tool.start/progress/complete`（`result` 从 JSON 字符串投影为 `AgentToolResult`）、`turn.*`、
`prompt.result`、`session.settled`、`queue.updated`、`todo.updated`、`session.info`、`session.usage`；
状态行的模型、上下文 token/window、cost；`command.dispatch` 处理 omp 未本地拦截的 slash；
历史/`--resume`；本地 `SessionManager` 只做用量与上下文记账，不再写第二份 transcript。

模型面板（`/model`/`Alt+M`/`Ctrl+P`）：`model.options` 的 provider/model 行经
`modes/rpc/gateway-models.ts` 装进 `GatewayModelRegistry`（只替换行、只装内存可用标记，
不落密钥库；`ignoreLocalModelConfig` 保持 Hermes 侧凭据/路由为唯一真源），并把
`getAll`/`getAvailable`/`find` 限定为 authority 广告的 provider——bundled 的 `local`/`web`
等行不再进入选择器或模型轮换；选择仍走 `session.mutate operation=model`。`/resume` 走
`session.list`，选中后 `AgentSession.switchGatewaySession()` 以新连接 resume 该 stored id
并重放快照（本地 `SessionManager` 重置后只作回放视图），`--resume` 与选择器共用同一条连接路径。

未接（下一步）：approval/clarify 对话框（当前 deny/cancel 并给 notice）、`/tree`/branch/rewind/compact
（仍改本地 SessionManager）、子代理/JOBS/usage 面板的其余字段、login/MCP/skills/plugins/cron 面板。

## 6. Smoke 标记规范

| 状态 | 含义 | 退出码 |
|---|---|---|
| `ok` | 端到端跑通 | 0 |
| `pending` | 代码在、接线未完成；带原因与阶段 | 0 |
| `unsupported` | Hermes 无对应能力 | 0 |
| `FAIL` | 回归 | 非 0 |

规则：

1. 每条 `pending`/`unsupported` 必须写原因与目标阶段。
2. 不允许用 Proxy/空实现换 `ok`；未接通就显式标记。
3. 末尾打印 `ok=N pending=N unsupported=N fail=0`，完整列表写到 `src/smoke.out.txt`。
4. 旧 `native.smoke.txt` 的 40 条结论只作为协议映射证据保留，迁移后重标。

## 7. 能力状态（迁移后）

| # | 能力 | 落点 | 初始状态 |
|---|---|---|---|
| 1 | 工具卡片详情 / 内联 diff | `session/agent-session.ts` → `modes/controllers/event-controller.ts` | pending → ok |
| 2 | Todo HUD | `modes/controllers/todo-command-controller.ts` | ok（空快照也清空） |
| 3 | 用量与上下文 | `agent-session.ts` + status line | ok |
| 4 | 队列 / steer | `agent-session.ts` | ok |
| 5 | 中断 / 重试 | `agent-session.ts` | ok |
| 6 | `/model` | `modes/controllers/selector-controller.ts` | ok |
| 7 | `/resume` | `selector-controller.ts` | ok |
| 8 | `/new` `/clear` `/help` | `command-controller.ts` | ok |
| 9 | 命令补全与执行 | `command-controller.ts` + `main.ts` | ok |
| 10 | `/export` | `command-controller.ts` | ok |
| 11 | 子代理 | `event-controller.ts` / selector | ok |
| 12 | 后台作业 | `command-controller.ts` | ok |
| 13 | goal / loop | `status-line-host.ts` | ok |
| 14 | 通知 | `event-controller.ts` | ok |
| 15 | 内联 diff | `event-controller.ts` | ok |
| 16 | 附件与图片 | `agent-session.ts`（`prompt.submit.attachments`） | ok；`pdf.attach` 见 §9.1 |
| 17 | 皮肤 | `interactive-mode.ts` + `settings.ts` | ok（token 子集） |
| 18 | Mermaid / 表格图表 / SVG | `modes`（omp 原能力） | ok |
| 19 | 浏览 / 复制 / `/tree` | `session-manager.ts` + `agent-session.ts` | `/tree` 只支持 user 回合；其它目标提示并取消 |
| 20 | MCP / skills / plugins / cron 面板 | `mcp-command-controller.ts` 等 | ok |
| 21 | 设置面板 | `settings.ts` | ok |
| 22 | `/btw` `/background` | `btw-controller.ts` | ok |
| 23 | 语音 / dictation / collab | `modes` 原实现 | 语音按 `unsupported`（与 Hermes voice 形态不同）；collab → **待设计（D）**（面板计划 §10） |
| 24 | ACP / RPC server / print / predict / ps / render / git-tui | `modes` 原实现（保留） | `unsupported` 或 `pending` |
| 25 | 原生绑定相关（VCS/AST/tokenize） | `vendor/natives` | `pending: natives`（本机绑定可用则 ok） |

## 8. 双向功能差异（2026-10-31）

### 8.0 口径与方法

- **Hermes 侧**以 `tui_gateway/contracts/*`（约 180 methods、60+ events、20 server requests）+
  `commands/` + `skills/` + `agent/` + `apps/desktop/src` + `web/src` + `hermes_cli/subcommands/` 为准；
  运行期 slash 全量是 `commands.catalog`（stock profile ≈602 条），Ink 自己只注册约 70 条本地命令。
- **omp 侧**以 `modes/controllers/*` 的公开方法 + `pi-tui` 的 overlay/component/terminal 能力为准。
  omp 的完整 slash 注册表在本地 checkout 的
  `packages/coding-agent/src/slash-commands/*`（`C:\Users\zarag\projects\oh-my-pi`），
  **不在当前 `src` 快照里**；G1 vendor 时必须把它一起搬进 `src`，否则“UI 已有”的命令
  在 src-native 路径上会消失。
- 静态字符串 grep 只用于识别 **omp 本地独有、没有 Hermes 对应**的命令（`/omfg`、`/cleanse`、
  `/ratchet`、`/prewalk`、`/wt`、`/rec` 等），不作为能力清单来源。
- “有/缺”指终端 UI 是否可达；仅桌面/网页/CLI 的能力单列。
- 面板级网关改动（contract、handler、事件、能力协商、测试）见
  [`omp-ui-panels-gateway-plan.md`](omp-ui-panels-gateway-plan.md)。

### 8.1 Hermes 有、omp UI 缺

#### (a) UI 或旧桥已有，只需把数据源换成 Hermes 网关（重接，成本最低）

| 能力 | omp UI 现状 | Hermes 后端 |
|---|---|---|
| MCP 管理 | `/mcp add·list·remove·test·reauth·enable·disable·smithery-*·reconnect·reload·resources·prompts` + add wizard | `mcp.servers.*`、`mcp.catalog` |
| Skills | `/skills` hub（list/inspect/install/update） | `skills.manage`、`skills.reload` |
| Plugins / Marketplace | `/plugins`、`/marketplace`、`/reload-plugins` | `plugins.manage/list/catalog` |
| 登录 / 登出 / 模型凭据 | `/login`、`/logout`、model picker 的 key 保存 | `model.save_key/disconnect` + auth RPC |
| HTML 导出 | `/export` + `src/export/html` 模板 | `session.export{format:html}` |
| 浏览器 | `/browser` | `browser.manage` |
| Cron | 旧桥 `coding-agent/hermes/manage.ts:98 showCron`（`cron.manage` list/pause/resume/remove） | `cron.submit/cancel/status/recover`（现有客户端为部分实现，补齐 submit/cancel） |
| 会话选择 / 搜索 | selector（pin/delete/preview） | `session.list`、`session.prune`、`session.mutate import` |

#### (b) 网关已就绪、omp UI 与旧桥都没有客户端实现（新建 UI）

| 能力 | Hermes 后端 | 备注 |
|---|---|---|
| 账单 / 充值 / 订阅 | `billing.*`、`subscription.*` | Ink 已有 overlay，可参考 |
| Connectors OAuth 账户 | `connectors.*` | Ink 已有 overlay |
| Vault | `vault.*` + 服务端请求 | 解锁/保存登录/2FA |
| Profiles / multiplex | `profiles.*` | 桌面/网页/CLI 已有 |
| 检查点 / 回滚 / 会话 diff | `rollback.*`、`rollback.session_diff` | |
| Pets / Journey / Insights | `pet.*`、`journey`、`insights` | |
| 子代理历史回放 / diff | `spawn_tree.list/load/save` | omp 只有实时 Agent Hub |
| MoA 参考/聚合块 | `moa.reference/progress/phase/aggregating` | |
| 自我改进 review | `review.summary` | 与 omp advisor 不是同一能力 |
| 共享指标授权 | `shared_metrics.*` | |
| Widget grid / SDK 小组件 | 客户端本地 | Ink `widgetGrid.tsx` |
| i18n 语言切换 | `i18n.languages/catalog` | omp shell 未见，需确认 |
| Kanban 读取 | `kanban.run` | 看板 UI 仅桌面/网页 |

#### (c) 仅桌面 / 网页 / CLI（终端 UI 不追）

富媒体（图片/视频/PDF/HTML 预览）、右侧文件浏览器、Kanban 看板、Bot Mode / 群聊 / Bot Screen（RFB）、
Computer-Use 预检、多窗口/分栏/浮动 pane/starmap、托盘/快速唤出/原生通知、主题编辑器/市场、
渠道/pairing/webhook 管理、Cron/Hooks/Vault/MCP GUI、配置 GUI、浏览器 PTY/移动端、Analytics/Logs/System、
Profile builder。

#### (d) Hermes 也还缺的协议（先补 contract，UI 才有落点）

`hooks.list/toggle`、canonical `pdf.attach`/`image.attach`、`theme.list`、公开 share 链接（当前只有导出）、
通用 OAuth 授权码粘贴。

### 8.2 omp UI 有、Hermes 缺

#### 渲染器（最大差距，纯前端可移植）

| 能力 | omp 证据 | Hermes 现状 |
|---|---|---|
| Mermaid → 终端 ASCII | `pi-tui/chat/fence-figure.ts` | Ink 无；desktop 有 Mermaid |
| SVG fence 实时栅格化 | `pi-tui/chat/svg-figure.ts` | 无 |
| 数字表格自动图表 | `pi-tui/chat/table-chart.ts` | 无 |
| 内联图片 Kitty/SIXEL/iTerm2 + 占位符 + tmux 透传 | `terminal-capabilities.ts` | Ink 仅宠物用 Kitty，对话流无通用内联图 |
| 只读工具调用折叠 | `chat/read-tool-group.ts` | 无 |
| 逐字流式揭示 | `streaming-reveal.ts` | Ink 整段流式 |
| 回合反应 / 渐变 / 重置烟花 | `settings.ts` | 无 |
| 24 段可配置状态行 | `status-line-host.ts`、`status-line/segments.ts` | Ink 状态栏较固定 |
| 大文本粘贴分流（`local://paste-N.md`） | `input-controller.ts:2850` | Ink 有 paste spill，无分流菜单 |
| 上下文查看器 / cache 失效线 / served-model 标记 | `overlays/context-viewer*`、`chat/*marker.ts` | 无或仅文本 |

#### 终端协议

Kitty 键盘协议（release/repeat、完整修饰键，Ink 仅部分 CSI-u）、OSC 66 文本缩放、Glyph Protocol（APC 25a1）、
DECCARA、DEC 2026 同步输出、OSC 5522 增强粘贴、鼠标路由（点击 HUD/状态行/列表）、Warp OSC 事件桥、
Tern 原生 Surface。

#### 编辑器 / 输入

Vim 模态 + 状态行模式徽标、模型 ghost-text 预测、提示历史搜索 overlay、清空草稿恢复、图片/视频附件 chip、
剪贴板（图片/原文/复制行/复制提示）、文件/内部 URL/emoji/模型/skill 自动补全、外部 `$EDITOR`、
拼写提示、IME 安全光标、composer 皮肤、魔法关键词发光、Bash `!` / Python `#py` 内联模式。

#### Overlay / 面板

树选择器（分支摘要）、Rewind 选择器、Copy 选择器、Model hub/browser、带预览的 Session selector、
Theme 实时预览、类型化 Settings 菜单、Thinking/effort 选择器、Jobs panel/tree、Agent Hub + 单代理
transcript viewer + Agents dashboard（角色模型/prewalk/advisor）、MCP add wizard、Extensions Control Center、
Git TUI、Hotkeys keycap sheet、Changelog、Debug selector、BTW panel、OMFG、Plan review/save/TOC、
Annotation、Pause screen、Session info、Transcript browser + 大纲导轨、Skill picker、
Autoresearch/Cleanse/ps-top/live visualizer、Startup splash/tips。

#### 会话 / 工作流 / agent UX

Fork 到 pane/window、worktree/move/多根目录、加密分享链接/collab 扫码/LIVE 徽标、`/trace` 本地 stats 面板、
会话 focus 到子代理、实时语音可视化 + TTS 闪避、auto-thinking、model roles/presets/`@role`、
`/plan` 评审/保存/TOC、OMFG/Cleanse/Ratchet/Prewalk/Auto-graph、屏幕录制 `/rec`、Warp/Tern 集成。

其中 **OMFG、Cleanse、Ratchet、Prewalk、Auto-graph** 归入 **待设计（D）**：Hermes 没有等价后端，
设计要点、方案选项与退出条件见 `omp-ui-panels-gateway-plan.md` §10；Advisor / Model roles /
Auto-thinking 在 Hermes 有部分等价物（MoA/refine、模型与配置选择）。**Model roles 已接通**：
`/models` 经 `model.roles.get` / `model.roles.set` 读写 `auxiliary.<task>`，`default` 角色走 `session.setModel`，
导入角色 `task`→`delegation`、`advisor`→`adviser`（`tui_gateway/methods_model_roles.py`）；
presets 落 `model_presets`（`model.presets.*`）、cycle 落 `omp.cycle_order`（`model.cycle.set`）、
模型键回退链落 `auxiliary.fallback_chains`（解析在 `agent/auxiliary_client.py`）。
omp 的 `plan` / `smol` / `slow` / `widget` 作为普通 aux 槽提供（`auxiliary.<name>`，hub 可分配、
插件可 `call_llm(task=...)` 调用），但暂无内置消费者，默认 `auto` 走主模型。
仅 **kind roles**（image/speech/dictation/web）仍 `unsupported`：其模型在 `tts.*`/`stt.*`/图像与搜索 provider
注册表里，不在 hub 浏览的 chat catalog 中。Advisor / Auto-thinking 未接通的形态仍按 `pending`/`unsupported` 标记，不当新后端立项。其余为 UI/渲染能力。

### 8.3 两边都有、只是形态不同（避免重复实现）

流式文本、thinking、工具卡片、审批/澄清/sudo/secret、todo、子代理、后台任务、goal/loop、会话列表/恢复、
模型选择、reasoning/fast、导出、MCP/skills/plugins 列表、主题皮肤选择、语音、通知、slash 目录与补全。
差异在形态：Hermes 偏文本命令 + 少量 overlay，omp 是重 overlay TUI。

### 8.4 工作量分档

| 档 | 内容 | 估算 |
|---|---|---|
| 重接（UI 已有） | MCP、skills、plugins、login/logout、HTML 导出、browser、cron（旧桥部分实现）、session 搜索 | 每项 0.5–2 天，主要是 RPC 映射与字段差异 |
| 新建 UI（后端已就绪） | 账单、connectors、vault、profiles、rollback、pets、journey、spawn-tree、MoA、review、shared metrics | 每项 1–5 天 |
| 移植渲染/终端 | pi-tui 的 mermaid/SVG/chart/inline-image/terminal-capabilities | 取决于终端矩阵，2–4 周 |
| 移植编辑器/overlay | vim、ghost text、history search、各 selector | 2–4 周 |
| 不追 / 待设计 | 桌面/网页独有；omp 专属 agent 工作流 | 桌面项记录理由；agent 工作流按 **D** 出 ADR（面板计划 §10） |

## 9. 保留的历史结论

### 9.1 网关实测（canonical WebSocket，2026-10-31）

| 方法 | 结果 |
|---|---|
| `subagent.list` / `process.list` / `session.control.read` / `tools.show` / `approval.pending` | ok |
| `mcp.servers.list` / `skills.manage` / `plugins.manage` / `cron.manage` | ok（canonical `-32601` 后走 legacy fallback） |
| `file.attach` | 绝对路径 / `data_url` ok；相对路径 4001 |
| `image.attach_bytes` | ok |
| `image.attach` / `pdf.attach` | 失败（legacy 查旧 sidecar 会话表） |
| `hooks.list` | `-32601` |
| `session.history` | 存在、带 `row_id`，不要依赖旧的 `limit` 参数 |

真正缺的协议（后续按需补）：`pdf.attach`/`image.attach` canonical、`hooks.list/toggle`、
`theme.list`、通用 OAuth 粘贴、Kanban 板面。

### 9.2 差异分桶

| 桶 | 含义 | 处理 |
|---|---|---|
| A | 网关已会、UI 未接线 | 客户端接线 |
| B | 网关没有该协议 | 先补 contract + handler + 生成 TS |
| C | 能力长在 `pi-tui` 渲染器里 | 接线即可 |
| D | Hermes 其它界面缺 | 跨界面回填 |
| E | 待设计 / 明确不移植 | U/partial 项先出 ADR（见面板计划 §10）；无 Hermes 等价且无移植价值的记录理由 |

### 9.3 明确不移植 / 待设计

> 本表只列**有明确理由的不移植**（CLI 模式、Tern/Warp 等）。与面板相关且语义不等价的项
> （OMFG、cleanse、annotation、collab、pause freeze-all 等）不在此表，按 **待设计（D）** 处理；
> 设计要点、方案选项与退出条件见 [`omp-ui-panels-gateway-plan.md`](omp-ui-panels-gateway-plan.md) §10。

| 项 | 理由 |
|---|---|
| ACP / RPC-stdio / print / predict / ps / render 这些 CLI 模式 | Hermes 已有对应入口；在 `src` 里保留代码，smoke 标 `unsupported` |
| `native/` TSP 语义渲染后端、Glyph Protocol 编解码 | omp 的终端协议栈；Hermes 以 ANSI/OSC 为主 |
| 屏幕录制 / `omp play` / 流式发布 | 与 Hermes 会话/分享模型不兼容 |
| 加密分享链接 | 需要新服务端与密钥策略；先做本地导出 |

## 10. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 两个类的重写量 | `agent-session.ts` 15,409 行、`session-manager.ts` 4,763 行；保留文件还会引用更多成员 | 先按壳的实际调用面实现，其余成员声明为抛 `Unsupported`；smoke 覆盖调用路径 |
| 仓库体量 | 方案 A + 不裁剪 ≈ 52.8 万行 `src` + 约 28 万行 vendor | 脚本同步、manifest 记录 |
| 两套同名类型 | `vendor/*` 与 tui 包内部的 pi-* 类型结构相同、名义不同 | 只用结构类型；禁止跨边界 `instanceof` |
| natives import 期失败 | 顶层 `loadNative()` 无绑定即抛；`interactive-mode`/`procmgr` 值导入会杀死壳；单个 `.node` 119MB | §3 补丁把加载失败降级为调用期 `Unsupported`；`HERMES_OMP_NATIVE_DIR` 可指真实绑定 |
| `tsc` 性能 | 50 万行 + vendor | 增量 tsconfig / `skipLibCheck` |
| tui 版本耦合 | 壳 import tui 内部路径，锁 18.14.x | 升级必须跑 smoke + render |
| 上游漂移 | vendor 后有本地修改 | `sync-upstream.py` 重放并在 `VENDORED.md` 列出补丁 |
| 许可证 | omp 为 MIT | 保留 `LICENSE` 与文件头；manifest 记录来源 |

## 11. 验收

```bash
cd omp-ui
bun install --ignore-scripts
python scripts/sync-upstream.py       # 可选：从 checkout 重建 src + vendor
bun run check            # tsc --noEmit（含 src）
bun run check:imports    # R1–R5 + 相对模块解析 = 0
HERMES_OMP_START=1 bun src/smoke.ts   # ok/pending/unsupported 三段
# 真实终端
HERMES_BUN=... python -m hermes_cli.main omp
```

通过条件：

1. `check:imports` 零违规、零未解析模块；
2. `tsc` 通过；
3. smoke 无 `FAIL`，每条 `pending`/`unsupported` 带原因与阶段；
4. PTY 实跑 `hermes omp` 打开 omp 的 `InteractiveMode`，welcome / status / 一轮真实对话 / `/tree` 可用；
5. `omp-ui/app/` 与 `omp-ui/coding-agent/hermes/` 已删除，仓库内无引用；
6. 仓库内不存在 `src/hermes/`（也没有任何以产品名开头的平行命名空间）。
