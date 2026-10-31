# omp-ui 面板接通：Hermes 网关改动计划

> 目标：把 omp-ui 的**所有面板**接到 Hermes 网关。本文主体描述**网关侧**要做的改动（contract、
> handler、事件、能力协商、测试）；UI 侧移植与按需代码搬运见 `docs/omp-ui-gap-plan.md`。
>
> **2026-10-31 方向修正：单一 wire，两端适配。**
>
> 旧方向（本文件上一版 P1）：把 sidecar 的 74 个面板方法逐一重写进 session authority，追求
> “fallback 命中 0”。**作废**——它把已有实现复制了一遍（约 4.7k 行网关代码 + 2.6k 行测试），
> 只改变了 method 的归属，没有增加任何面板能力。
>
> 现在的方向：**Hermes JSON-RPC 是唯一 wire**；`omp-ui/src` 增加 `modes/rpc/gateway-client.ts`
> 并把 `AgentSession`/`SessionManager` 就地接到 Hermes method；Hermes 网关只补 `src` 真正缺的
> 协议（新方法、字段扩展、事件）。面板方法由 **authority 直服** 或 **进程内 sidecar 适配**
> （`tui_gateway/ws_legacy_fallback.py::dispatch_legacy`，绑定调用者 profile）服务，二者都算
> “已接通”；不为搬家而重写业务逻辑。

## 0. 范围与非目标

**范围内**：面板需要的每一个读/写 RPC、实时事件、能力声明、profile/终端作用域、契约生成与测试。

**非目标**：
- omp 专属后端（OMFG、cleanse、ratchet、prewalk、auto-graph、in-process extensions）。
- 桌面/网页独有面板（Kanban 看板、Bot Mode、Bot Screen、多窗口、富媒体预览）。
- 终端渲染能力（mermaid/SVG/内联图等）——那些是客户端 `pi-tui` 的事。

这些在 §10 统一标 `unsupported`，由 `runtime.describe` 告知 UI，不静默失败。

**两端改动**：
- **客户端（`omp-ui/src`）**：`modes/rpc/gateway-client.ts` 说 Hermes JSON-RPC（请求/响应、
  `event`、server→client request、断线重连）；`session/agent-session.ts` 与
  `session/session-manager.ts` 就地把 omp 的类映射到 Hermes method/事件/历史行；错误码与
  legacy 数字码在一个映射函数里归一。omp 自己的 `rpc-client.ts`（`{type:"prompt"}` 自有帧）
  不接网关，保留但不在运行路径。
- **网关**：只补 §4 的新方法、§4.7 的字段扩展与 §6 的事件；已有 sidecar 方法经适配器服务，
  不重写。

**UI 侧代码搬运**：所需 omp 代码按 import 闭包从本地 checkout
`C:\Users\zarag\projects\oh-my-pi`（HEAD `6a86df8539fccf3ad7818081266e554022fc21bc`）复制到
`omp-ui/src/vendor/`；不得从 `omp-ui/node_modules` 复制。依赖边界见
`docs/omp-ui-gap-plan.md` 的 D3/§3（`package.json` 只保留 `@oh-my-pi/pi-tui`）。

## 1. 设计原则

1. **单一实现，两种归属**：每个面板方法只有一份业务实现。authority 自有方法（会话/准入/控制）
   由 `gateway/*` 服务；profile 级 sidecar 方法由网关**同进程**执行原 handler
   （`dispatch_legacy`，绑定 ticket profile），不复制、不重写。只有“sidecar 读自己的
   `_sessions`”这类会话绑定缺口才升到 authority（§5）。
2. **契约先行**：每个方法/事件在 `tui_gateway/contracts/*.py` 声明 `Params`/`Result`/`Payload`，
   `extra="forbid"`；生成 `omp-ui/src/modes/rpc/gateway-contract.generated.ts` 与 OpenRPC
   （`apps/shared/src` 的旧副本留给 Hermes 原有界面，不再由生成器改写）。
3. **作用域**：会话方法带 `session_id`，走 `@_profile_scoped`；无会话方法带 `profile`，
   走 `_profile_scoped_rpc`（home + secret + terminal scope 三件套）。
4. **终端在会话语义里**：`git.*`、附件、路径类方法必须跑在**会话的 terminal backend**（本地/远程/SSH），
   不能跑在网关宿主；与 `complete.path` 同一条规则。
5. **写操作幂等**：请求带 `request_id`；会话状态改写带 `expected_revision` / `expected_generation`。
6. **事件优先**：实时面板（jobs、agent、usage、plan、pause）用事件，不引入轮询。
7. **能力协商**：`runtime.describe` 返回 `panels` 映射，UI 启动时据此把未实现面板标为
   `pending`/`unsupported`，而不是运行时收到 `-32601` 才炸。

## 2. 面板清单与依赖矩阵

状态图例：**A** authority 直服（`gateway/*.py` handler + `CANONICAL_METHODS`，含
`session_ancillary.handlers()` 这类 comprehension 表）；**L** 由 legacy sidecar handler 经网关的
**进程内适配器**服务（`dispatch_legacy`，绑定调用者 profile；**受支持**）；
**M** 完全没有方法；**E** 存在但字段/参数不足；**C** 纯客户端；**U** 不支持；
**D** 待设计（需先出决策记录）。

三类拆分的口径（P0 脚本按此判定）：
(a) `gateway/` 有 handler（A）；
(b) `tui_gateway/contracts` 有声明、handler 在 `methods_*.py`，由适配器执行（L，受支持）；
(c) 完全没有声明（M）。
`runtime.describe.panels` 的 `methods` 广告 **A + L**；只有 M 不可调用。覆盖清单只覆盖
**面板实际调用的方法**。

| 面板 | 读 | 写 | 现状 | 改动 | 备注 |
|---|---|---|---|---|---|
| Model hub / picker / browser | `model.options`、`config.get` | `session.mutate`、`model.save_key`、`model.disconnect` | A/L | E | model.options metadata (E) is pending |
| Session selector | `session.list`、`session.active_list`、`session.most_recent` | `session.mutate` | A/L | E | session.list query/pin/preview (E) is pending |
| Tree selector | `session.history`、`session.info` | `session.mutate` | A | E | branch summarize/instructions payload (E) is pending |
| Rewind selector | `session.history` | `session.mutate` | A | — |  |
| Copy selector | — | — | — | — |  |
| Theme selector / skin | `skin.list`、`config.get` | `config.set` | A | — | `skin.list` 由 authority 直服 |
| Settings menu | `config.schema`、`config.get` | `config.set` | A | — | `config.schema` 由 authority 直服 |
| Thinking / effort | `model.options` | `session.mutate` | A | — |  |
| Usage dashboard | `session.usage`、`session.stats`、`billing.state`、`usage.bars` | — | A/L | — |  |
| Jobs panel / job tree | `process.list`、`spawn_tree.list` | `process.stop`、`process.kill` | A/L | E | process.list stats fields (E) and process events are pending |
| Agent Hub / dashboard / transcript | `subagent.list`、`subagent.tail`、`delegation.status` | `subagent.steer`、`subagent.interrupt`、`delegation.pause`、`subagent.config` | A/L | — | `/agents` = Agent Hub；`/hub` = live agent dashboard / transcript；`subagent.config` 读写 profile `agent_config` 表 |
| MCP manager / add wizard | `mcp.catalog`、`mcp.servers.list`、`mcp.servers.status` | `mcp.servers.add`、`mcp.servers.remove`、`mcp.servers.set_api_key`、`mcp.servers.oauth.start`、`mcp.servers.oauth.poll`、`mcp.servers.oauth.callback`、`mcp.servers.oauth.cancel` | L | — |  |
| Extensions center | `plugins.list`、`hooks.list` | `plugins.manage`、`hooks.toggle` | A/L | — | 方法已直服；`hooks.changed` 属 P4 |
| Git TUI | `git.status`、`git.diff`、`git.log`、`git.branches` | `git.stage`、`git.unstage`、`git.commit` | A | — | 全部在会话 terminal backend 执行 |
| Context viewer | `session.context_breakdown`、`session.history` | — | A | — |  |
| Hotkeys sheet | — | — | — | — |  |
| Changelog | `runtime.describe` | — | A | E | runtime.describe does not carry a version yet (E); the changelog content is client-side |
| Debug selector | `runtime.describe`、`session.info` | — | A | — |  |
| BTW panel / history | `btw.history` | `prompt.btw` | A | — | 答案落 `<profile>/session/btw/<session>.jsonl` |
| OMFG | — | — | — | — | no Hermes backend: the omp rule-forging workflow has no gateway equivalent (P6) |
| Plan review / save / TOC | `session.plan.read` | `session.plan.save`、`session.plan.approve` | A | — | 方法已直服；`session.plan.updated` 属 P4 |
| Annotation overlay | — | — | — | — | no annotation backend; storage and protocol are undesigned (P6) |
| Pause screen | `session.control.read` | `delegation.pause` | A | — | delegation.pause only gates new spawns; freezing in-flight turns needs a new primitive (P6) |
| Session info overlay | `session.info`、`session.stats` | — | A | — |  |
| Transcript browser / outline | `session.history` | — | A | E | session.history pagination/outline extension (E) is pending |
| Skill picker | `commands.catalog`、`complete.slash` | `skills.manage` | A/L | — |  |
| Autoresearch / cleanse / ps-top / live | `process.list`、`voice.record` | `voice.toggle`、`voice.tts` | A/L | E | cleanse/autoresearch have no Hermes backend (P6); ps-top needs process.list stats (E) |
| Billing / subscription | `billing.state`、`billing.auto_reload`、`billing.charge_status`、`usage.bars`、`subscription.state`、`subscription.preview` | `billing.charge`、`billing.keep_going_on_credits`、`billing.step_up`、`subscription.change`、`subscription.upgrade`、`subscription.resume` | L | — |  |
| Connectors | `connectors.list`、`connectors.catalog`、`connectors.accounts`、`connectors.tools`、`connectors.policy.get`、`connectors.operation.status` | `connectors.connect`、`connectors.accounts.remove`、`connectors.accounts.rename` | A/L | — |  |
| Vault | `vault.list`、`vault.sources` | `vault.add`、`vault.remove`、`vault.unlock`、`vault.lock`、`vault.source.set` | L | — |  |
| Profiles / multiplex | `profiles.list`、`profiles.describe` | `profiles.create`、`profiles.configure`、`profiles.set_asset` | A/L | — |  |
| Rollback / checkpoints | `rollback.list`、`rollback.diff` | `rollback.restore` | A | — |  |
| Pets | `pet.gallery`、`pet.info`、`pet.cells` | `pet.select`、`pet.hatch`、`pet.generate` | L | — |  |
| Cron | `cron.manage`、`cron.status` | `cron.submit`、`cron.cancel`、`cron.manage` | A/L | — |  |
| Login / logout / provider auth | `model.options`、`config.get` | `model.save_key`、`model.disconnect` | A/L | E | model.options metadata (E) is pending |
| Approvals / ask / sudo / secret | — | `approval.respond`、`clarify.respond` | A | — |  |
| Todo HUD | — | — | — | — |  |
| Goal / loop bars | `session.control.read` | `session.control`、`session.automation` | A | — |  |
| i18n | `i18n.languages`、`i18n.catalog` | — | L | — |  |
| Shared metrics consent | `shared_metrics.status` | `shared_metrics.set` | L | — |  |
| Kanban (read-only board) | `kanban.run`、`kanban.board` | — | A/M | N | kanban.run is served; the kanban.board snapshot and kanban.changed are not (N) |
| Spawn-tree replay / diff | `spawn_tree.list`、`spawn_tree.load` | `spawn_tree.save` | L | — |  |
| MoA display | — | `config.set` | A | — |  |
| Collab / QR / LIVE | — | — | — | — | omp collab has no Hermes protocol mapping; groups.* hosted rooms are desktop-only (P6) |
| Review summary | — | — | — | — |  |

### 2.1 Hermes 后端给 omp-ui 的两类 RPC

Hermes 网关对 omp-ui 暴露的不是“每个面板各有一个后端”，而是两类 RPC。面板只是消费者；
方法归属仍以 `gateway/panel_inventory.py` 与 `tui_gateway/contracts/*` 为准。

**1. 实时 RPC（server → client）**

- 形态：`event("<type>", Payload)`；会话级通知带 `params.session_id`，`event_replay._stamp_event` 再补
  `seq`（无 session 的全局事件两者都没有）；`revision` 只在个别 payload（`session.control.update` 的
  `control.revision`、`todo.updated.revision`、`tool.complete.revision?`），不是信封字段。
- 覆盖运行态（已实现）：`session.control.update`、`subagent.*`、`todo.updated`、`session.usage`、
  `message.complete`（usage 字段）、`skin.changed`、`moa.*`、`review.summary`、`btw.complete`。
- P4 待补：`process.*`、`session.plan.updated`、`hooks.changed`、`kanban.changed`。
- 作用域：按会话/profile 订阅；断线重连用 `session.events.since` 回放；UI 只更新本地状态，不轮询。

**2. 业务 RPC（client → server request/response）**

- 形态：JSON-RPC method；读/写方法包括 `session.*`、`model.*`、`config.*`、`git.*`、`mcp.*`、
  `skills.*` / `plugins.*` / `hooks.*`、`vault.*`、`billing/subscription.*`、`connectors.*`、`profiles.*`、
  `pet.*`、`cron.*`、`rollback.*` 等。
- 契约：`tui_gateway/contracts/*.py` 的 `Params`/`Result`，`extra="forbid"`；由 authority 直服，
  或由进程内 sidecar 适配（`dispatch_legacy`，绑定调用者 profile）。
- 写操作：请求带 `request_id`；会话状态改写带 `expected_revision` / `expected_generation`。

面板与 RPC 的对应关系不再逐后端列表：实时面板消费第 1 类，读/写面板消费第 2 类，多数面板两者都用。
`runtime.describe.panels` 只声明每个面板的 `supported/status/methods`，不复制后端实现。

#### 2.1.1 两类 RPC 的协议定义现状（实现级，不含业务接入）

**Hermes 侧：两类都有机器可校验的定义，且是 Python 单一真源。**

| | 定义 | 注册表 | 生成物 |
|---|---|---|---|
| 实时 RPC | `tui_gateway/contracts/events.py`（每个通知一个 `Payload`） + `server_requests.py`（server→client 请求） | `EVENTS` 79、`SERVER_REQUESTS` 16 | `omp-ui/src/modes/rpc/gateway-contract.generated.ts` 的 `Notifications` / `Server→client requests` 段；OpenRPC 的 `x-notifications` / `x-server-requests` |
| 业务 RPC | `tui_gateway/contracts/*.py` 的 `Params` / `Result` | `METHODS` 257、`CANONICAL_METHODS` 68（其中 42 个 canonical-only，合计 299 个方法名） | 同文件的 `Client→server methods` / `Canonical methods` 段；OpenRPC `methods` + `x-canonical-methods` |

线格式：JSON-RPC 2.0。通知是 `{"jsonrpc":"2.0","method":"event","params":{"type","session_id","payload",…}}`
（`tui_gateway/server.py::_event_frame`）；`event_replay._stamp_event` 给会话级通知加 `seq`（每会话单调，
重放水位），`stream_segments` 给回复帧加 `turn_id`/`segment`/`offset`（幂等去重），`replay_epoch` 由
`gateway.ready` / `session.events.since` 提供；§2.1 说的“`revision`”只出现在
`session.control.update` 的 `control.revision`、`todo.updated.revision`（以及 `tool.complete` 的可选
`revision`）等 payload 内，**不是**通知信封字段。服务端请求是
`{"jsonrpc":"2.0","id","method","params":{"session_id",…}}`，客户端用同一 `id` 回包。

**omp-ui 侧：也有完整定义，但是另一套协议。**

- `omp-ui/src/modes/rpc/wire/`：`commands.ts`（74 个 `RpcCommand`，含 `params`/`result`/`timeoutMs`/
  `completion`）、`events.ts`（33 个 `AgentSessionEvent`）、`frames.ts`（`ready`、`prompt_result`、
  `session_settled`、12 种 `extension_ui_request`、host tool/URI 帧、`RpcResponse`）、`state.ts`/
  `content.ts`（`SessionState`、`AgentMessage`、`ModelInfo` 等）。生成 `wire/rpc-wire.schema.json`
  （`x-rpc.commands` 机器可读）与 `wire/rpc-wire.generated.ts`；`rpc-client.ts` 是其客户端实现（JSONL
  stdio，单帧上限 1 MiB，v2 分片重组）。
- 信封不同：命令 `{id?,type,…}`、响应 `{type:"response",id?,command,success,data?,error?,code?}`、
  事件 `{type,…}`。没有 `jsonrpc`/`method`/`params`，也没有 `session_id`/`seq`/`replay_epoch`/profile 作用域。
- 仓库里已经放了 Hermes 生成的 `gateway-contract.generated.ts`（类型齐全），但**没有**
  `gateway-client.ts`，也没有 `type → method` 的映射表。结论：**两端都不缺协议定义，缺的是映射层**
  （信封解包、方法名/枚举/字段别名、缺失字段补齐）。

逐字段对齐结果见两份附录：

- `docs/appendix-omp-ui-realtime-rpc.md`（实时 RPC，server → client）；
- `docs/appendix-omp-ui-business-rpc.md`（业务 RPC，client → server request/response）。

暂不支持的面板（OMFG、annotation、collab/QR/LIVE、cleanse/autoresearch、in-process extensions、
pause freeze-all、Kanban board 快照、Bot Mode、多窗口、富媒体预览、Tern Surface）由
`runtime.describe.panels` 返回 `supported:false`，UI 不渲染假入口（见 §10）。

### 2.2 面板入口：omp TUI 从哪里打开

入口以实现为准：客户端 controller 与选择器在 `omp-ui/src/modes/controllers/*`
（`input-controller`、`selector-controller`、`command-controller` 等），slash 命令名与默认键位来自
上游 `packages/coding-agent/src/slash-commands/*` 与 `packages/tui/src/app-keybindings.ts`
（随 vendor 进入 `omp-ui`；`.omp/keybindings.yml` 可覆盖）。表里的“无 omp TUI 入口”只表示
该 checkout 没有对应 surface，不代表 Hermes 后端缺少方法。

| 面板 | 打开方式 | 备注 |
|---|---|---|
| Model hub / picker / browser | `/model`、`Alt+M`；临时模型 `Alt+P` | `/model` 无参进 Model Hub；`Alt+P` 只改本会话 |
| Session selector | `/resume` | CLI `omp --continue` 直接续接上一会话 |
| Tree selector | `/tree`；双 `Esc`（`doubleEscapeAction=tree`） | 默认双 `Esc` 是 rewind，不是 tree |
| Rewind selector | `/branch`（别名 `/rewind`）；双 `Esc`（默认） | 选中消息即回退并把旧路径留作分支 |
| Copy selector | `/copy` | `Alt+Shift+C` 复制 prompt；`Alt+Shift+L` 复制当前行 |
| Theme selector / skin | `/settings` → Appearance → Theme | 皮肤列表由 Settings 的 `themePickerOptions` 提供 |
| Settings menu | `/settings` | 分页：Appearance / Model / Interaction / Context / Memory / Files / Shell / Tools / Tasks / Providers |
| Thinking / effort | `/effort`；`Shift+Tab` 循环；`Ctrl+T` 开关 | `/effort` 仅在模型支持 reasoning 时打开选择器 |
| Usage dashboard | `/usage` | `/stats` 是另一个本地网页统计仪表盘 |
| Jobs panel / job tree | `/jobs`；点击 jobs pill | `/jobs full` 输出静态报告而非 sheet |
| Agent Hub / dashboard / transcript | `/agents`（Agent Hub）；`/hub`（别名 `/subagents`）、`Alt+A`、`Ctrl+S`（live agent dashboard / transcript） | Agent Hub 管每 agent 配置；live dashboard 看实时子代理与 transcript |
| MCP manager / add wizard | `/mcp list`；`/mcp add` | 无参 `/mcp` 只显示 help |
| Extensions center | `/extensions`（别名 `/status`） | 插件安装/卸载另走 `/plugins`、`/marketplace` |
| Git TUI | `/git [revision]` | 在当前项目目录打开；带 revision 看历史版本 |
| Context viewer | `/context` | 无模型或上下文窗口未知时拒绝打开 |
| Hotkeys sheet | `/hotkeys` | 原生终端为 sheet，其余为 Markdown 面板 |
| Changelog | `/changelog [full\|last N]` |  |
| Debug selector | `/debug` |  |
| BTW panel / history | `/btw`（带问题提问；不带问题开历史） | 答案面板内 `b` 分支、`c` 复制 |
| OMFG | `/omfg <complaint>` |  |
| Plan review / save / TOC | `/plan-review`；plan mode 结束时自动打开 | Save overlay 在 review 内；TOC 用 `Tab` 聚焦 |
| Annotation overlay | `/annotate [last\|session\|code-review\|<file>\|"text"]` | bundled custom command |
| Pause screen | `/pause` |  |
| Session info overlay | `/session` 或 `/session info` |  |
| Transcript browser / outline | `/hub` / `Alt+A` → 选中 agent | live agent transcript 内的 viewer |
| Skill picker | 输入 `/skill:`；注册表 `/skills` | `/skills {search\|install\|installed\|update}` |
| Autoresearch / cleanse / ps-top / live | `/cleanse`、`/live`、`omp ps` | autoresearch 无 omp 入口（扩展工具驱动）；ps-top 走 CLI |
| Billing / subscription | 无独立面板 | `/usage` 看额度；`/login`、`/setup` 管账号/订阅 |
| Connectors | 无 omp TUI 入口 | Hermes sidecar 面板，待移植 |
| Vault | 无 omp TUI 入口（Hermes sidecar 凭据 Vault） | omp 的 `vault://` 是 Obsidian 文件协议，与此面板无关 |
| Profiles / multiplex | 无 omp TUI 入口 | `omp --profile` 是 auth/session/settings 隔离，不是 Hermes agent profile roster |
| Rollback / checkpoints | 无 omp TUI 入口 | Hermes 侧 |
| Pets | 无 omp TUI 入口 | Hermes sidecar |
| Cron | 无 omp TUI 入口 | Hermes 侧 |
| Login / logout / provider auth | `/login`、`/logout`、`/setup`（providers） | Model Hub 内也可触发登录 |
| Approvals / ask / sudo / secret | 自动弹出 | `Ctrl+Enter` / `Ctrl+Q` 批准；`Alt+H` 隐藏 ask |
| Todo HUD | 常驻；`/todo expand\|collapse` |  |
| Goal / loop bars | `/goal`、`/loop` | 激活后在状态栏常驻 |
| i18n | 无 omp TUI 入口 | Hermes sidecar |
| Shared metrics consent | 无 omp TUI 入口 | Hermes sidecar |
| Kanban (read-only board) | 无 omp TUI 入口 | 桌面看板 |
| Spawn-tree replay / diff | 无独立 omp TUI 入口 | `/hub` / `Alt+A` 的 tree 视图只显示 spawn 关系，不含 replay/diff |
| MoA display | 无 omp TUI 入口（Hermes 侧事件） | 无独立面板 |
| Collab / QR / LIVE | `/collab`、`/share`、`/join`、`/leave` | `/live` 是语音模式，不是 collab |
| Review summary | 自动渲染（reviewer 子代理） | 无独立入口 |

术语：`/agents` = **Agent Hub**（`showAgentsDashboard` → `AgentsHubComponent`，per-agent model/prewalk/advisor）；
`/hub`（别名 `/subagents`）、`Alt+A`、`Ctrl+S` = **live agent dashboard / live transcript**
（`showAgentHub({initialSection:"activity"})` → `AgentHubComponent` + transcript viewer）。

### 2.3 主界面逐项（omp TUI）

主界面由 `omp-ui/src/modes/interactive-mode.ts` 组装；组件来自
`packages/tui/src/{chat,prompt,status-line,chrome}`；状态行策略在
`omp-ui/src/modes/status-line-host.ts`。下面按屏幕从上到下，再拆每一行/区块。

#### 2.3.1 纵向结构（native dock）

| # | 区域 | 内容 | 依据 |
|---|---|---|---|
| 1 | 终端标题栏 | 会话名、运行 spinner、PR 号 | `setSessionTerminalTitle`、`tui.titleState`、`tui.titleSpinner` |
| 2 | 欢迎头 | 渐变 logo + `omp` 字标 + 版本 + 本会话提示（`[NEW]` 高亮）；`startup.quiet` 关闭时显示；启动 splash 仅在显式启用、非 resume/quiet/timing、且 stdin/stdout 都是 TTY 时播放 | `WelcomeComponent`、`shouldShowStartupSplash` |
| 3 | 配置警告 | 每条 `Warning: …` 一行 | `#buildConfigWarningComponents` |
| 4 | What's New | 启动 changelog；`hidden`/`summary`/`expanded`（默认 `summary`） | `startup.changelogMode` |
| 5 | Transcript | 追加式聊天记录（§2.3.2），滚入终端 history | `chatContainer` |
| 6 | HUD pills | 右对齐的 Agents / Jobs 计数；两者都为 0 时整行隐藏 | `describeHudPills` |
| 7 | inline 面板 | BTW、OMFG、cleanse、错误横幅、模型轮换 chip、延迟命令、下载/判定进度 | `btw/omfg/cleanse/errorBanner/modelCycle/deferredCommand/progressHud` 容器 |
| 8 | Sticky HUD | Todo checklist + 子代理列表，位于 working 行上方（同属 `statusContainer` 的 activity 行） | `describeStatusHud`、`TodoHudContainer` |
| 9 | Working/状态行 | spinner、elapsed、`·`、意图 shimmer、grow、Stop；retry 为倒计时环 + Cancel；compaction 为进度条 | `describeWorkingRow`、`DEFAULT_WORKING_MESSAGE="Working…"` |
| 10 | 排队消息 | 待发送/排队 prompt，可逐条 dequeue | `pendingMessagesContainer`、`queued-messages` |
| 11 | 附件 chips | Image/Video/Paste 芯片，位于输入框上方 | `AttachmentChipsBand` |
| 12 | 编辑器上方 hook UI | 扩展 hook 自定义组件 | `hookWidgetContainerAbove` |
| 13 | 编辑器 / composer | 输入框、边框、提示、自动补全、内嵌状态行 | `Composer`、`CustomEditor` |
| 14 | 编辑器下方 hook UI | 扩展 hook 自定义组件 | `hookWidgetContainerBelow` |

native dock 的精确顺序（自下而上由代码给出，渲染时自上而下）：
`HudPillsRow → btw → omfg → cleanse → errorBanner → modelCycle → deferredCommand →
progressHud → statusContainer → pendingMessages → hookWidgetAbove → editorContainer →
hookWidgetBelow`；`chatContainer` 在其上，todo/subagent 属于 `statusContainer` 的 activity 行。

#### 2.3.2 Transcript 区块目录

| 区块 | 内容/行为 | 源文件 |
|---|---|---|
| 用户消息 | prompt 气泡；hover 工具栏提供 retry / switch-model / rewind / resume / copy | `user-message.ts`、`transcript-actions.ts` |
| 助手消息 | Markdown 正文、代码块、thinking、工具调用、usage 行 | `assistant-message.ts` |
| Thinking | 可折叠思考块；`hideThinkingBlock` 隐藏、`expandThinkingBlocks` 默认展开 | `thinking-display.ts` |
| Advisor | advisor 评审意见；`display.advisorCollapsedLines` 控制折叠行数 | `advisor-message.ts` |
| Background TAN | 后台 detached agent 的结果消息 | `background-tan-message.ts` |
| Skill 调用 | `/skill:<name>` 的 prompt 与标题输入块 | `skill-message.ts`、`skill-title-input.ts` |
| 自定义/hook 消息 | 扩展与 hook 注入的消息，样式区别于用户消息 | `custom-message.ts`、`hook-message.ts` |
| 压缩摘要 | compaction 摘要；`display.collapseCompacted` 控制折叠 | `compaction-summary-message.ts` |
| Recap | 空闲 recap（`※ recap:`），会话进展摘要 | `recap-notice.ts` |
| Todo 提醒 | 待办完成提醒，写入 transcript 而不悬浮 | `todo-reminder.ts` |
| TTSR 通知 | 规则通知 | `ttsr-notification.ts` |
| Late diagnostics | 回合结束后到达的诊断 | `late-diagnostics-message.ts` |
| Cache miss 标记 | prompt cache 丢失后的分隔线 | `cache-invalidation-marker.ts`、`display.cacheMissMarker` |
| Served model 标记 | 实际响应模型与请求模型不同 | `served-model-marker.ts` |
| 被剥离的工具调用 | 分支上被剥离调用的暗色占位 | `stripped-tool-calls-placeholder.ts` |
| Reaction | 对前一块的 emoji 徽章（回复以 emoji 开头时提取） | `reaction.ts`、`tui.reactions` |
| 工具执行 | 通用工具调用卡片（参数/结果/状态） | `tool-execution.ts` |
| Bash 执行 | 用户 `!` 命令与流式输出 | `bash-execution.ts` |
| Eval 执行 | 用户 eval，共享 agent 的 kernel session | `eval-execution.ts` |
| Read 工具组 | 连续 read 调用合并成组 | `read-tool-group.ts` |
| 图片/SVG/figure | 内联图片、SVG 代码块、Mermaid/figure 渲染 | `svg-figure.ts`、`fence-figure.ts`、`image.ts` |
| 表格图表 | 数值表下的 auto-graph（`tui.autoGraph`） | `table-chart.ts` |
| Collab prompt | 访客 prompt，所有参与者可见 | `collab-prompt-message.ts` |
| Reply write | `out://` 写回被渲染为助手正文，不结束回合 | `reply-write.ts` |
| 错误帧 | 错误信息 + retry 等操作 | `error-block.ts`、`transcript-actions.ts` |
| 通知/分隔 | transcript notice、status notice、message divider/frame | `chrome/*` |
| Transcript 选择器 | 临时全屏 rewind / copy 浏览器（从 session entries 重建） | `transcript-browser.ts`、`transcript-outline.ts` |

#### 2.3.3 Composer / editor 细节

| 细节 | 行为 | 设置/来源 |
|---|---|---|
| 形状 | 默认 `band`（状态带与输入框贴合）；可选其他 composer shape | `composer.shape` |
| 边框 | 边框颜色跟随 thinking level / session accent | `getComposerStyle`、`sessionAccent` |
| 状态行 | 状态行嵌在 composer 底部 | `StatusLineComponent` |
| 提示（hints） | 双击 `←` 看运行中 agents（打开 live dashboard）；`Shift+Tab` 改 thinking；各出现 3 次后不再提示 | `composer-hints.ts` |
| Slash 补全 | `/` 打开命令面板，显示描述与子命令 | builtin registry |
| 文件/路径补全 | 文件路径、`@` 提及、内部 URL | `file-mentions`、`internal-url-autocomplete` |
| 模型提及 | 模型名 chip | `model-mention-autocomplete` |
| GitHub 引用 | issue/PR 引用补全 | `github-ref-autocomplete` |
| Emoji | emoji 补全与 emoticon 展开 | `emoji-autocomplete` |
| Prompt actions | 动作补全 | `prompt-action-autocomplete` |
| Skill token | `/skill:` chip 与 token 折叠 | `skill-tokens`、`skill-command` |
| 单词补全 | `off` / `auto` / `ngram` / `smollm` / `apple` | `word-completion.ts` |
| 预测 | 下一词预测 | `composer.predictions` |
| Magic keywords | 关键词高亮与相位动画 | `magic-keywords.ts` |
| Prose gate | 仅对自然语言做拼写/装饰，避免代码误判 | `prose-gate.ts`、`macos-spelling.ts` |
| Vim | 编辑模式与状态行模式指示 | `tui.vimMode`、`tui.vimModeDisplay` |
| 附件 | Image/Video/Paste chip、占位符、粘贴路径、WebP 回退、vision marker | `attachment-chips.ts`、`composer-attachments.ts`、`image-loading.ts` |
| 排队 | queue shorthand、queued message 列表、dequeue | `queue-input.ts`、`queued-messages.ts` |
| 输入模式 | bash mode / python mode 切换 | `input-controller.ts` |
| 外部编辑器 | `Ctrl+G` 打开 `$EDITOR` | `app.editor.external` |
| 历史搜索 | `Ctrl+R` 搜索输入历史 | `app.history.search` |
| 草稿恢复 | 压缩/切会话后恢复 detached draft 与 queued 文本 | `draft` helper、`restoreCompactionQueueToEditor` |
| IME 安全 | 底边框独立成行，避免 macOS IME preedit 顶飞 | `tui.imeSafeCursor` |

#### 2.3.4 HUD 与实时行

| HUD | 细节 | 触发/设置 |
|---|---|---|
| Todo HUD | 按 phase 的 checklist；`/todo expand\|collapse`；状态持久化为 revealed/dismissed；自动清除计时 | `TodoHudContainer`、todo state data |
| 子代理 HUD | 标题 `Subagents · N running`，右侧显示 live dashboard 键；默认折叠 3 行 + expander；`display.subagentLivePreview` 时每行下方显示当前/最近工具调用与 elapsed | `renderSubagentHudLines`、`display.pinnedAgents` |
| HUD pills | Agents 段（运行数；有已完成子代理时保留暗色图标）+ Jobs 段（运行 job 与 job widget）；点击分别开 live dashboard / jobs sheet；双击 `←` 开 live dashboard | `describeHudPills`、`shortcut: formatDoubleTap("left")` |
| Working row | `starburst` spinner、elapsed、`·`、intent shimmer、grow、Stop（Esc） | `describeWorkingRow` |
| Idle 状态行 | 空闲时保留上次 tok/s 与 band 标题（右对齐）；失败后显示 `F5 to Retry` | `renderIdleStatusHud`、`#workingRowTrailer` |
| Retry row | 倒计时环 + `Retrying · attempt N of M` + Cancel | 同上，`variant.kind="retry"` |
| Compaction row | 不确定进度条 + `Compacting context…` | 同上，`variant.kind="compaction"` |
| Token rate | 生成 tok/s；`composer.tokenRate` 控制是否在 working row/composer bar 显示 | `composer.tokenRate` |
| 标题 trailer | band composer 下 working row 右侧显示会话标题 | `#workingTitleTrailer` |
| 模型轮换 chip | `Ctrl+P` 循环角色模型，chip track 悬停在编辑器上方并自动清除 | `showModelCycleTrack` |
| 下载/判定进度 | editor 锚定的 download activity 与 judgment batch progress | `progressHudContainer` |
| BTW / OMFG / cleanse | 内联面板位于编辑器上方；各自有 Esc/分支/复制等键 | `btwContainer`、`omfgContainer`、`cleanseContainer` |
| 错误横幅 | 可 dismiss 的错误行 | `errorBannerContainer` |
| 排队消息 | 位于 working row 与 composer 之间，逐条 dequeue | `pendingMessagesContainer` |
| 延迟命令 | 等待回合结束的 slash 命令 | `deferredCommandContainer` |
| Hook widgets | 扩展在编辑器上方/下方挂载的 UI | `hookWidgetContainerAbove/Below` |

#### 2.3.5 状态行

默认 preset：左 `pi, vim, model, mode, collab, stream, path, git, pr, context_pct, cost`，
右 `session_name`。preset 可选 `default` / `minimal` / `compact` / `full` / `nerd` / `ascii` /
`custom`；分隔符可选 `powerline` / `powerline-thin` / `slash` / `pipe` / `block` / `none` /
`ascii`；context line 可选 `off` / `percentage` / `annotated` / `embedded`。

| Segment | 含义 |
|---|---|
| `pi` | 品牌字形；带 profile 标签；focused agent 时显示 ghost+agent id；运行中变 spinner + 回合计时 |
| `status` | hook/extension 状态文本，多个用 `·` 连接 |
| `model` | 模型名 + thinking level + advisor 徽章 + fast 模式图标；compact 时用 thinking 字形替代模型图标 |
| `mode` | Plan / Plan(paused) / Prewalk / Goal / Vibe / Loop(state, limit, condition) |
| `path` | 项目目录/worktree，可缩写、超链接；`stripWorkPrefix` 控制 |
| `git` | 分支 + staged/unstaged/untracked 计数 |
| `pr` | 当前 PR 号，可点击 |
| `subagents` | 运行中子代理数 |
| `token_in` / `token_out` | 会话累计输入/输出 token（`usageStats`） |
| `token_total` | 会话累计 input+output+cacheWrite+orchestration（不含 cacheRead） |
| `token_rate` | tok/s |
| `cost` | 会话自身花费；子代理与 advisor 成本单列 |
| `context_pct` | 上下文占用百分比 |
| `context_total` | 上下文窗口大小 |
| `time_spent` | 会话内 `agent_start`→`agent_end` 的累计活跃时间（不含空闲 wall-clock） |
| `time` | 当前时钟（12/24h） |
| `session` | 会话 id 前 8 位 |
| `hostname` | 短主机名 |
| `cache_read` / `cache_write` | cache 读/写 token |
| `cache_hit` | cache 命中率（read / (read+write+input)） |
| `session_name` | 会话标题 |
| `usage` | 5h/1d/7d/mo 配额百分比、重置时间、reset credits、tier |
| `collab` | `collab:N`（host）或 `collab guest:N` |
| `stream` | `● LIVE N` / `● REC` |
| `vim` | INSERT/NORMAL/VISUAL/V-LINE/REPLACE + 选择行数 + pending 命令 |

状态行设置：`statusLine.preset`、`leftSegments`、`rightSegments`、`separator`、`contextLine`、
`sessionAccent`、`transparent`、`compactThinkingLevel`、`showHookStatus`、`segmentOptions`；
`goalStatusInFooter` 决定 goal 是否进入 footer。

#### 2.3.6 键位（默认值，`keybindings.yml` 可覆盖）

| 类别 | 默认键 |
|---|---|
| 中断/退出 | `Esc` 中断；`Ctrl+C` 清屏/取消；`Ctrl+D` 退出；`Ctrl+Z` 挂起 |
| 模型 | `Ctrl+P` 下一个模型（编辑器）；`Shift+Ctrl+P` 上一个；`Alt+M` 选择模型；`Alt+P` 临时模型 |
| Thinking | `Shift+Tab` 循环 thinking level（`/effort` 设置 level）；`Ctrl+T` 显隐 thinking 块（可被 `hideThinkingBlock` 持久化） |
| 会话 | `/new`、`/tree`、`/branch`、`/resume` 无默认键（slash only）；resume picker 内 `Ctrl+R` 重命名、`Ctrl+D` 删除、`Ctrl+Backspace` 非侵入删除、`Alt+K`/`Alt+Shift+K` 固定/取消固定 |
| Agent Hub / live dashboard | `/agents` 打开 Agent Hub；`Alt+A` / `Ctrl+S` 打开 live agent dashboard / transcript（编辑器）；选择器内 `Ctrl+S` 是切换排序 |
| 计划 | `Alt+Shift+P` 切换 plan mode |
| 输入 | `Ctrl+G` 外部编辑器；`Ctrl+R` 历史搜索（编辑器）；`Ctrl+O` 展开工具；`Ctrl+Shift+O` 显隐工具活动；`Ctrl+V` 粘贴图片/文本；`Ctrl+Shift+V` / `Alt+Shift+V` 原样粘贴 |
| 消息 | `Ctrl+Q` / `Ctrl+Enter` follow-up；`Alt+Up` / `Shift+Up` dequeue；`F5` / `Alt+R` 重试 |
| 复制 | `Alt+Shift+C` 复制 prompt；`Alt+Shift+L` 复制当前行 |
| 审批/询问 | `Ctrl+Enter` / `Ctrl+Q` 批准；`Alt+H` 隐藏/显示 ask |
| 语音/直播 | `Ctrl+L` 开关 live voice；`Space` 按住说话（push-to-talk） |
| 其他 | `Alt+L` 重置显示；`Alt+G` 打开 `local://` 文件；`Alt+O` 选择链接提示；`Alt+K` / `Alt+Shift+K` 固定/取消固定会话（resume picker） |

#### 2.3.7 Slash 面板

| 类别 | 命令 |
|---|---|
| 模型/推理 | `model`(`models`)、`effort`、`fast`、`slow`、`switch`、`modelpreset`、`extended-context`、`computer`、`skillful` |
| 会话/历史 | `new`、`fresh`、`resume`、`tree`、`branch`(`rewind`)、`fork`、`session`、`rename`、`delete`、`pin`、`clear`、`compact`、`retry`、`stats`、`usage`、`changelog`、`hotkeys`、`context`、`todo` |
| 代理/任务 | `agents`（Agent Hub）、`hub`（`subagents`，live dashboard/transcript）、`jobs`、`goal`、`guided-goal`、`loop`、`queue`、`tan`、`advisor`、`seance` |
| 工具/集成 | `mcp`、`plugins`(`plugin`)、`reload-plugins`、`marketplace`、`skills`、`skill:<name>`、`extensions`(`status`)、`tools`、`security` |
| 代码/环境 | `git`、`wt`、`move`、`add-dir`、`remove-dir`、`dirs`、`local`、`open`、`copy`、`export`、`share` |
| 模式/控制 | `plan`、`plan-review`、`vibe`、`pause`、`live`、`record`、`force`、`forget-approvals`、`debug`、`ratchet`、`prewalk` |
| 账号/协作 | `login`、`logout`、`setup`(`providers`)、`collab`、`join`、`leave`、`browser`、`ssh` |
| 其他 | `btw`、`omfg`、`cleanse`、`annotate`、`review`、`memory`、`handoff`、`shake`、`restart`、`exit`、`quit` |

命令全量以 `packages/coding-agent/src/slash-commands/builtin-*.ts` 的 `name` 为准；自定义命令
（`annotate`、`review`）来自 bundled custom commands，`skill:<name>` 来自 skill registry。

#### 2.3.8 影响主界面的显示设置

| 设置 | 默认 | 作用 |
|---|---|---|
| `composer.shape` | `band` | 输入框/状态带形状 |
| `composer.tokenRate` | `false` | 显示实时 tok/s |
| `display.shimmer` | `classic` | working/loading 文字动画 |
| `display.pinnedAgents` | `collapsed` | 子代理 HUD 折叠策略 |
| `display.subagentLivePreview` | `false` | 子代理行下显示当前工具调用 |
| `display.smoothStreaming` | `true` | 平滑显示流式文本/工具输入 |
| `display.hideToolActivity` | `false` | 隐藏模型工具调用与结果 |
| `display.showTokenUsage` | `false` | 助手消息显示每回合 token |
| `display.showTurnTime` | `false` | 助手消息显示回合总耗时 |
| `display.cacheMissMarker` | `false` | cache miss 后显示分隔线 |
| `display.advisorCollapsedLines` | `0` | 折叠 advisor 卡片最多显示行数（0 = 完整预览） |
| `display.collapseCompacted` | `true` | 折叠已压缩历史 |
| `hideThinkingBlock` | `false` | 隐藏助手 thinking 块 |
| `expandThinkingBlocks` | `false` | 默认展开 thinking 块 |
| `startup.quiet` | `false` | 隐藏欢迎头/启动动画 |
| `startup.changelogMode` | `summary` | 启动 What's New：`hidden`/`summary`/`expanded` |
| `tui.reactions` | `true` | agent emoji 反应徽章 |
| `tui.titleState` | `true` | 终端标题显示运行状态 |
| `tui.titleSpinner` | `braille` | 终端标题 spinner 样式 |
| `tui.hyperlinks` | `auto` | 终端超链接 |
| `tui.mouse` | `false` | 鼠标点击聚焦 |
| `tui.tight` | `false` | 去掉左右 1 字符留白 |
| `tui.imeSafeCursor` | `false` | IME 安全布局 |
| `tui.vimMode` | `false` | Vim 编辑模式 |
| `tui.vimModeDisplay` | `text` | 状态行 Vim 指示样式 |
| `tui.renderMermaid` | `true` | Mermaid 转 ASCII |
| `tui.renderSvg` | `true` | SVG 内联渲染 |
| `tui.autoGraph` | `auto` | 数值表自动图表 |
| `tui.maxInlineImageColumns` | `100` | 内联图最大列 |
| `tui.maxInlineImageRows` | `20` | 内联图最大行 |
| `tui.maxInlineImages` | `8` | 单条消息最多内联图 |

## 3. 改动分类与总量

| 类 | 含义 | 方法 |
|---|---|---|
| **L 适配**（已有实现，零重写） | sidecar handler 经 `dispatch_legacy` 同进程执行，绑定 profile；视为已接通 | `mcp.*`、`skills.manage`、`plugins.manage`、`cron.manage`、`billing.*`、`subscription.*`、`usage.bars`、`connectors.catalog/accounts/tools/policy.get/accounts.*`、`vault.*`、`profiles.describe/create/configure/set_asset`、`pet.*`、`voice.*`、`wake.*`、`i18n.*`、`shared_metrics.*`、`spawn_tree.*`、`browser.manage`、`model.save_key`、`model.disconnect`、`session.most_recent`、`delegation.status` |
| **A 会话缺口**（authority 必须自服） | sidecar handler 读自己的 `_sessions`，authority 会话不在其中 | `session.usage`、`image.attach`、`pdf.attach`、`connectors.list`（session-owned 分支） |
| **N 新建** | P2 已落地（§9）；仅 `kanban.board` 未建（P4 可选）。分类保留用于追溯 | **已落地**：`git.*`、`hooks.list/toggle`、`skin.list`、`config.schema`、`btw.history`、`session.plan.read/save/approve`、`subagent.config`；**未建**：`kanban.board` |
| **E 扩展** | 字段/参数/事件不足 | `model.options`、`session.list`、`session.history`、`process.list`、`session.mutate{branch.summarize}` |
| **G 能力协商** | `runtime.describe.panels` 广告 A + L | 所有面板 |
| **C 客户端** | 无网关改动，`src` 内接线 | copy、hotkeys、changelog、context 渲染、theme 预览（omp 主题） |
| **D 待设计** | 无 Hermes 后端或语义不等价；先出决策记录 | OMFG、cleanse、ratchet、prewalk、auto-graph、in-process extensions、annotation、collab、pause freeze-all、desktop-only 面板（§10） |

## 4. 新方法契约（重点）

所有类继承 `Params` / `Result` / `Payload`；`extra="forbid"`；新增后跑
`scripts/gen_gateway_contracts.py`。

### 4.1 `git.*`（Git TUI）

```text
git.status   {session_id}                        -> {branch, ahead, behind, files:[{path, xy, staged}]}
git.diff     {session_id, path?, staged?, context?} -> {diff, truncated}
git.log      {session_id, limit?}                -> {commits:[{sha, subject, author, date}]}
git.branches {session_id}                        -> {current, branches:[{name, upstream, ahead, behind}]}
git.stage    {session_id, paths:[...], request_id?}      -> {staged}
git.unstage  {session_id, paths:[...], request_id?}      -> {staged}
git.commit   {session_id, message, sign?, request_id?}   -> {sha, summary}
```

- **必须**在会话的 terminal backend 执行（`tools/environments/*`），与 `complete.path` 同规则；
  远程/SSH 会话不得读网关宿主的 git。
- 写操作走 `request_id`；`git.commit` 不做自动 amend，消息为空拒绝；同一 `request_id` 重试返回
  首次回执（不会二次提交，也不会因“nothing to commit”失败）。`stage`/`unstage` 本身幂等。
- 复用 `tui_gateway/git_probe.py` 的解析，但不要复用它“只读探针”的宿主 cwd 语义。

### 4.2 `hooks.*`（Extensions/Hooks 面板）

```text
hooks.list   {session_id?, profile?} -> {hooks:[{name, event, enabled, scope, source, description}]}
hooks.toggle {session_id?, profile?, name, enabled, request_id} -> {name, enabled}
```

- 数据源：`hermes hooks` 使用的同一份配置（项目/全局）；写操作在 profile scope 下。
- 事件：`hooks.changed {hooks:[...]}`。
- omp 的 “Extensions Control Center” 映射到 `plugins.*` + `hooks.*`；不存在 in-process extension RPC。

### 4.3 `skin.list` / `theme.list`

```text
skin.list {profile?} -> {current, skins:[{name, display, dark, light, colors:{...}}]}
```

- 只读；`config.set{key:"skin"}` 切换，`skin.changed` 广播已有。
- omp 自己的 renderer 主题仍是客户端列表；`skin.list` 只服务 Hermes 皮肤。

### 4.4 `config.schema`（Settings 面板）

```text
config.schema {profile?} -> {settings:[{key, type, default, enum?, description, scope, restart_required}]}
```

- 让 omp 的 `Settings` 菜单能按类型渲染（开关/枚举/数字/字符串/路径），避免硬编码 key。
- 只读；写仍走 `config.set`。

### 4.5 `btw.history` / `session.plan.*`

```text
btw.history      {session_id, limit?} -> {items:[{id, question, answer, created_at, task_id?}]}
session.plan.read{session_id}         -> {path, text, status, updated_at}
session.plan.save {session_id, text, request_id} -> {path, updated_at}
session.plan.approve{session_id, request_id}     -> {status}
```

- plan 文件在会话语义里；`approve` 只翻转状态，不自动继续执行（执行仍由 `prompt.submit`/`command.dispatch`）。
- 若产品决定 plan 只走 slash，可只保留 `read`，`save/approve` 走 `command.dispatch`。

### 4.6 goal / loop / pause 的写入口（不新增方法）

- goal/loop/subgoal/heartbeat：写用 **`session.control {action, args}`**（allowlist：
  `goal.pause/resume/clear/unwait`、`loop.pause/resume/stop`、`subgoal.add/remove/clear`、
  `heartbeat.pause/resume/clear`），读用 `session.control.read`，实时状态用 `session.control.update`
  事件；`session.automation {kind, call, args, kwargs}` 是 classic CLI 的 manager 代理，保留给
  需要直接调 manager 的调用方。两者都已在 authority 注册。
- pause：**没有等价物**。`delegation.pause {paused}` 只挡新 spawn，活动子代理继续跑
  （`gateway/session_tui_ops.py:169`、`tools/delegate_tool_registry.set_spawn_paused`）；
  omp `/pause` 冻结主/子代理/advisor，需要一个新的“暂停在跑回合”的原语才能等价。
  在 Hermes 提供该原语前，UI 标 **partial**，不要把它渲染成 omp 的冻结状态。
- 因此不新增 `session.control.write`；goal/loop 已有两条写入口，pause 另立需求。

### 4.7 扩展项

| 方法 | 新增字段 |
|---|---|
| `model.options` | `name`、`context_window`、`max_output`、`pricing`、`auth_status`、`available`、`provider_display` |
| `session.list` | `query`（标题/预览搜索）、`pinned`、`archived`、`preview`、`last_active_at` |
| `session.history` | `offset`/`limit`、`before_row_id`、`include_tools`、`outline [{row_id, role, summary}]` |
| `process.list` | `cpu`、`mem`、`started_at`、`log_tail`（受字节上限） |
| `session.mutate`（branch） | payload `{target_message_id, summarize?, instructions?}`；摘要结果进 `branched_session_id` 回执 |
| `subagent.config` | `{subagent_id, session_id?, source?, model?, provider?, prewalk?, advisor?, thinking?, pattern?, prompt_path?}`；行键 `(agent_id, source=prompt_path)`，存 profile `state.db` 的 `agent_config` 表（角色配置面板） |
| `kanban.board`（可选） | `{project, columns, cards}` 只读快照 |

上下文面板不需要扩展：`session.context_breakdown`（`contracts/sessions.py`）已由
`gateway/session_tui_ops.py` 服务。

## 5. 侧车方法怎么接入：适配，不重写

现有适配器已经具备同进程执行 sidecar handler 的能力：

- `tui_gateway/ws.py::_dispatch_request`：authority 返回 `-32601` 后，若 method 在
  `server._methods` 且 `legacy_fallback_allowed`，调用 `dispatch_legacy`；
- `dispatch_legacy`：绑定 ticket 的 profile（`_session_profile_runtime_scope`），把 WS
  `transport` 传入 `server.dispatch`（`_caller_transport()` 即当前 socket）；
- `_SESSION_FALLBACK_ALLOWED` 已覆盖 store-wide 读（如 `session.most_recent`）；
- `_PROCESS_GLOBAL` 继续挡掉进程级危险动词（`process.stop`、`reload.mcp` 等）；需要 owner 语义的
  已在 authority 实现。

因此规则是：

1. **默认 L**：profile 级面板方法保持 sidecar 实现，网关不新增 handler；测试断言“适配器可服务”。
2. **只有会话缺口升 A**：handler 依赖 sidecar `_sessions` 的，写一个最小 authority handler：
   - `session.usage`（同 `session.stats` 的数据；sidecar 版查旧会话表）；
   - `image.attach` / `pdf.attach`（staging + 终端作用域；`image.attach_bytes`/`file.attach` 已有
     authority 版）；
   - `connectors.list` 的 session-owned 分支。
3. **契约加法而非搬迁**：这些方法在 `METHODS` 保留原声明，另在 `canonical_promoted.py` 用同一
   `Params`/`Result` 注册 canonical 条目；不移动、不复制模型。
4. **能力广告**：`runtime.describe.panels` 同时广告 A 与 L；客户端不会对 L 触发探测或报错。
5. **错误归一在客户端**：canonical 用 `{code,message,data.reason}`，适配器可能返回 legacy 数字码；
   `src` 的 `gateway-client.ts` 用一个映射函数处理，不在网关造第二套错误。
6. **测试**：覆盖门断言面板方法 ∈ {A, L}；不再要求 `ws_legacy_fallback` 命中数为 0。会话缺口
   另有 dispatcher 测试（含无 session 的入口与 profile 选择器拒绝）。

## 6. 事件与实时面板

| 面板 | 需要的事件 | 现状 |
|---|---|---|
| Jobs / job tree | `process.started/updated/ended`、`spawn_tree.changed` | 部分；补 stats 字段 |
| live agent dashboard | `subagent.spawn_requested/start/thinking/tool/progress/complete` | 已有 |
| Usage | `session.usage`、`message.complete.usage` | 已有 |
| Goal/loop/pause | `session.control.update` | 已有 |
| Plan | `session.plan.updated` | 新 |
| Hooks | `hooks.changed` | 新 |
| Skin | `skin.changed` | 已有 |
| MoA | `moa.*` | 已有 |
| Review | `review.summary` | 已有 |
| BTW | `btw.complete` | 已有（历史需 `btw.history`） |

事件统一走 `event("<type>", Payload)`，payload 必须带 `session_id`（会话级）与 `revision`（可 CAS 的写后广播）。

## 7. 能力协商

扩展 `runtime.describe`：

```text
runtime.describe {profile?} -> {
  version, skin, protocol,
  panels: {
    "git_tui":         {supported: true,  status: "ready",   methods: ["git.status", ...]},
    "extensions_center": {supported: true,  status: "pending", methods: ["hooks.list", "hooks.toggle", "plugins.list"]},
    "omfg":            {supported: false, status: "unsupported", reason: "no Hermes backend"},
    "kanban_board":    {supported: true,  status: "pending", methods: ["kanban.run"],
                        reason: "kanban.board snapshot not implemented; the board UI itself is client-side"}
  }
}
```

`methods` 同时列出 **A（authority）与 L（进程内 sidecar 适配）**；只有 M 不出现在列表里。
omp-ui 启动时读一次，未支持的面板直接标 `unsupported`，不发请求。新增方法时同步更新该映射，
覆盖测试保证“面板清单 = 契约清单”。

## 8. 契约、生成与测试

1. 改 `tui_gateway/contracts/*.py` → 跑 `scripts/gen_gateway_contracts.py` → 提交生成物
   （`omp-ui/src/modes/rpc/gateway-contract.generated.ts`、`gateway-contract.openrpc.json`；
   `apps/` 是 Hermes 原有界面，暂不改）。
2. 契约测试：每个方法一组 `Params`/`Result` 正反例；未知参数 4000；未声明方法 import 期拒绝。
3. 面板覆盖门（P0 产出）：`tests/tui_gateway/panels/test_panel_coverage.py`
   - 读 `docs/omp-ui-panels.json`（面板 → 方法/事件）；
   - 断言每个方法的真实归属：A = authority handler；L = `METHODS` 声明且适配器可执行；
     M = 两边都没有；
   - 断言 `runtime.describe.panels` 广告 A + L，且 ready 面板没有 M。
4. profile 作用域：两个磁盘 home + 只在副 profile 存在的 `.env`，调用面板方法断言解析正确、启动 profile
   未被污染。
5. 终端作用域：`git.*`/附件方法在远程会话下不得读网关宿主路径（用假的 remote backend 断言）。
6. 幂等/CAS：写方法重复 `request_id` 返回同一回执；`session.mutate` 过期 revision 返回 409x。

## 9. 分阶段

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| **P0** | 面板清单落盘（`docs/omp-ui-panels.json`）、覆盖脚本、`runtime.describe.panels` 骨架 | 覆盖门能列出 A/L/M；CI 可跑 |
| **P1** | **适配语义 + 会话缺口**：L 改为“适配器受支持”；`capabilities` 广告 A+L；只补 `session.usage`、`image.attach`、`pdf.attach`、`connectors.list`（session 分支） | 每个面板方法 ∈ {A, L}（M 仅 `kanban.board` 与 P4 事件）；会话缺口 dispatcher 测试通过；不新增 profile 级重写 |
| **P2** | 新方法：`git.*`、`hooks.*`、`skin.list`、`config.schema`、`btw.history`、`session.plan.*`、`subagent.config` | UI 面板可端到端读写；终端作用域测试通过 |
| **P3** | 扩展：`model.options`、`session.list/history`、`process.list`、branch summary | 面板数据完整；无客户端拼凑 |
| **P4** | 事件补齐：process stats、plan.updated、hooks.changed、kanban.board（可选） | 实时面板不轮询 |
| **P5** | **客户端轨道（`omp-ui/src`）**：`modes/rpc/gateway-client.ts`；`AgentSession`/`SessionManager` 就地接线；事件/错误码映射；面板 controller 接线 | `bun run check`；smoke 输出 `ok/pending/unsupported`，无 FAIL |
| **P6** | 设计轨道（与 P0–P5 并行）：§10 的 D 项逐项 ADR；决定为“做”的进入 P2/P3 | 每个 D 项有决定、约束、验收；无未决死入口 |

**P0 状态**：已落地。`docs/omp-ui-panels.json`（45 面板 / 146 方法 / 21 事件；P0 时为 A 59 / L 70 /
M 17，P2 后为 A 77 / L 68 / M 1）、
覆盖门 `tests/tui_gateway/panels/test_panel_coverage.py`、`runtime.describe.panels`
（`PanelCapability`）。清单以 `gateway/panel_inventory.py` 为源，`scripts/gen_panel_inventory.py`
生成；改清单后必须重跑生成器，覆盖门与 `test_generated` 会把漂移挡在 CI。

**P1 状态**：已落地（方向修正后）。`L` 的语义改为“由 `tui_gateway/ws_legacy_fallback.py::dispatch_legacy`
同进程执行、绑定调用者 profile，受支持”；`capabilities()` 广告 A+L；P1 时 23 个面板 `ready`（P2 后 28）。
authority 只新增四个会话缺口方法：`session.usage`、`image.attach`、`pdf.attach`、`connectors.list`
（session 分支），契约在 `tui_gateway/contracts/canonical_promoted.py` 复用 legacy 模型。
家族级重写（mcp/vault/billing/profiles/pets/voice/browser/...）已删除——它们由适配器服务，
每个方法只有一份实现。测试：`tests/gateway/test_promoted_{usage,connectors,attachments,surface}.py`。

**P2 状态**：已落地。七个新协议组由 authority 直服：`git.*`（7）、`hooks.list/toggle`、
`skin.list`、`config.schema`、`btw.history`、`session.plan.read/save/approve`、`subagent.config`。
契约按域拆在 `tui_gateway/contracts/{git_tui,hooks_skins_settings,plans_btw}.py`（`subagent.config`
并入既有 subagent 契约）；handlers 在 `gateway/session_{git,hooks,skins,config_schema,plan,btw_history,subagent_config}.py`，
共用 `gateway/session_terminal_command.py` 把命令送进会话自己的 terminal backend（远程/沙箱不读网关
宿主）：仅当 `terminal_uses_host_cwd`（local / docker mount）时把 `policy.cwd` 作为临时 workdir；
SSH/modal 等保留后端自己的 cwd，避免把网关宿主路径 `cd` 到远端不存在的目录。`git.diff` 用 `pipefail` 保留 git 失败并 drain 管道，避免大 diff 的 SIGPIPE 假错误。
`/btw` 答案落 `<profile>/session/btw/<session>.jsonl` 供历史面板重连后回读；plan 取 workspace
`.hermes/plans` 最新文件，`<plan>.status` 记录 draft/approved。`subagent.config` 把每个 agent 的自定义
配置（`model`/`provider`、`advisor`/`prewalk` 开关、`thinking`、`pattern`、`prompt_path`）写进 profile
`state.db` 的新表 `agent_config`（`hermes_state_agent_config.py`），行键为
`(agent_id, source)`，其中 `source` 就是命名 agent 定义文件的 **prompt 路径**。静态定义来自
`agent` 工具自己的扫描（`tools.agent_tool.discover_agent_definitions`）：项目/运行目录
`.agents`、`agents`、`skills/*/agents`、`.hermes/plugins/*/agents` → profile（`HERMES_HOME`）
同构目录 → installation 的 bundled skills/plugins。`session_id` 提供运行目录，显式 `source` /
`prompt_path` 可直接锁定定义。`subagent.list` 新增 `agents`：每条含静态定义（`model`/`provider`/`thinking`/`prompt_path`，
`scope` = project/profile/installation/custom）和动态行 `config`（同名覆盖字段），因此 Agent Hub
（`/agents`）可以直接枚举 scoped 静态 agent 并区分“定义值”与“动态覆盖”，而不是只看实时子代理。WATCHDOG/adviser 名册不在这条路径上。表里只存配置，不复用 `config.yaml`（profile 默认）或
`WATCHDOG.yml`（adviser 名册）；Agent Hub（`/agents`）因此为 `ready`。`hooks.changed`、`session.plan.updated` 仍属 P4，Extensions/Plan
面板在事件落地前保持 `pending`（方法已可用）。测试：
`tests/gateway/test_session_git_panel.py`、`test_session_plan_panel.py`、
`test_session_hooks_skins_settings.py`、`test_session_btw_history.py`、
`test_session_subagent_config.py`、`tests/hermes_state/test_agent_config.py`；其中 plan 的本地
后端 round-trip 标 `platforms("posix")`。

## 10. 不支持 / 部分支持：待设计清单

规则：**U/D 不是“不做”**，而是“未设计”。每项必须先有决策记录（ADR）与验收条件；决策前 UI 只显示
`unsupported` / `partial`，不提供假入口，也不静默 stub。设计轨道与 §9 的 P0–P5 并行（P6）。

| 项 | 现状/差距 | 待设计要点 | 方案选项 | 退出条件 |
|---|---|---|---|---|
| **Pause freeze-all** | `delegation.pause` 只挡新 spawn；omp `/pause` 冻结主/子代理/advisor | ① 冻结语义（暂停准入 / 冻结在跑 LLM 流 / 冻结工具子进程）② 与 `session.interrupt` 的边界（abort vs pause）③ 子代理与工具子进程（协作式 gate vs SIGSTOP）④ 暂停期间的 approval/ask ⑤ 持久化与超时 ⑥ profile/会话作用域 | A. 协作式 pause gate（工具边界检查）；B. OS 级 SIGSTOP 子进程；C. 只做“暂停新任务”并改名 | 回合中途 pause → resume 不丢上下文、不重发请求；子代理状态一致；两 profile 探针通过 |
| **Extensions Control Center** | Hermes 无 in-process extension（tool/renderer 注册）；现有 plugins/hooks | ① 统一“扩展”身份模型（omp extension vs plugin vs hook）② 可检查的注册面（commands/tools/renderers）③ 启停与热重载 ④ 安全/权限 ⑤ 与 `plugins.*`/`hooks.*` 的映射 | A. 只做 plugins+hooks 的只读视图；B. 新 `extensions.*` 抽象层；C. 不提供面板 | 面板展示真实能力，不伪造 omp 的 tool/renderer 注册；启停可回滚 |
| **Annotation overlay** | 无通用注释后端 | ① 注释对象（消息/文件/区域）② 存储（会话 DB vs 本地）③ 协议（`annotation.*` + 事件）④ 导出/分享 ⑤ 权限 | A. 仅本地草稿（不进网关）；B. `annotation.*` 会话级；C. 不做 | resume 后注释仍在；导出包含注释；不污染 prompt cache |
| **Collab / QR / LIVE** | Hermes 有 `groups.*`（桌面 hosted rooms）；omp collab 是另一套 | ① 协议映射（guest/host、权限、中继）② 终端 QR/加入流程 ③ LIVE 观众与消息回流 ④ 安全模型 | A. 复用 `groups.*` 做终端只读观众；B. 新 `collab.*`；C. 仅桌面 | 两个客户端可加入同一会话；guest 权限受控；断线重连可恢复 |
| **Kanban 看板（完整）** | `kanban.run` 可读写；无 board 快照/事件 | ① 读模型（列/卡片/顺序）② 写动作（移动/派发）③ 事件（`kanban.changed`）④ 并发/冲突 ⑤ 终端交互 | A. 只读 lanes（`kanban.board` 快照）；B. 完整拖拽；C. 不做 | 看板与服务端一致；并发移动不丢更新 |
| **Bot Mode / group chat / Bot Screen** | 桌面插件；终端无 RFB | ① 终端是否需要 roster/群聊；② Bot Screen 是否只做状态/缩略图；③ `groups.*`/`bot_relay.*` 的角色 | A. roster + 群聊只读；B. 全不接；C. 新终端屏幕协议 | 终端不出现死入口；桌面能力不被降级 |
| **多窗口 / pane / 浮动** | 桌面 pane shell；终端有 tmux/zellij | ① 用宿主多路复用器还是网关 ② fork 语义（新会话/同会话）③ 焦点/尺寸同步 | A. 客户端集成 tmux/zellij；B. 网关 pane 注册；C. 不做 | `/fork pane` 在支持的宿主可用；无宿主时给出提示 |
| **富媒体预览** | 终端无通用图片/视频/PDF/HTML | ① 终端能力协商（kitty/sixel/iterm）② 远程媒体获取（`media.fetch`?）③ 降级文本 ④ 尺寸/缓存 | A. 内联图 + 文本降级；B. 仅文本；C. 新 media RPC | 支持的终端渲染图片；不支持的终端有可读降级 |
| **Autoresearch / cleanse** | omp 专属 agent 后端 | ① 是否移植后端（研究循环、cleanse checkers）② 与 Hermes 工具/权限模型的冲突 ③ 成本/安全 | A. 移植为 Hermes 工具/技能；B. 明确不做；C. 只读报告 | 决策记录 + 若移植则有工具契约与预算控制 |
| **OMFG / ratchet / prewalk / auto-graph** | omp 专属 agent 工作流 | ① 是否有 Hermes 等价（hooks/plugins/skills）② 规则锻造的存储/校验 ③ 预算/安全 | A. 映射到 hooks/skills；B. 不做；C. 新后端 | 决策记录；若映射则用现有 hooks/skills 契约 |
| **Tern 原生 Surface / Warp 桥** | 终端宿主协议，非网关 | ① 宿主检测 ② 降级策略 ③ 是否值得维护 | A. 客户端可选支持；B. 不做 | 非宿主终端无副作用 |

设计轨道的产出物：每项一份 ADR（问题、约束、选项、决定、验收），落在 `docs/adr/` 或本文件追加；
决定为“做”时，再按 §4 的格式补 contract/handler/事件，并进入 §9 的 P2/P3。

## 11. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 适配层语义漂移 | sidecar handler 有进程级语义（如 `process.stop`/`reload.mcp`） | `_PROCESS_GLOBAL` 继续挡住；需要 owner 语义的已在 authority 实现 |
| 会话绑定缺口 | sidecar handler 读自己的 `_sessions`，authority 会话不在其中 | 只把这类方法升 A（P1 的四个）；其余不重写 |
| 终端作用域 | 附件 path 在网关宿主执行会读写错目录 | `terminal_uses_host_cwd` 检查；远程会话只接受 bytes |
| profile 泄漏 | 适配器无 session 时默认 launch home | `dispatch_legacy` 绑定 ticket profile；`require()` 校验 profile |
| 能力协商漂移 | `runtime.describe.panels` 与真实归属不一致 | 覆盖门从 `panels.json` 生成断言；A/L/M 以 handler 表为准 |
| 客户端协议分叉 | omp 自有 `{type:"prompt"}` 帧与 JSON-RPC 并存 | `rpc-client.ts` 保留但不接网关；`gateway-client.ts` 是唯一传输 |
| 错误码两套 | canonical `data.reason` vs legacy 数字码 | 客户端一个映射函数归一，网关不造第二套 |
| D 项长期悬空 | “待设计”变成永久 unsupported | P6 每项必须有决定与验收；UI 只显示真实状态 |

## 12. 验收

```bash
python scripts/gen_gateway_contracts.py --check
python scripts/gen_panel_inventory.py --check   # 或重跑后 git diff 为空
scripts/run_tests.sh tests/tui_gateway/panels/ tests/tui_gateway/contracts/
scripts/run_tests.sh tests/gateway/test_promoted_usage.py tests/gateway/test_promoted_connectors.py \
  tests/gateway/test_promoted_attachments.py tests/gateway/test_promoted_surface.py
```

通过条件：

1. `docs/omp-ui-panels.json` 中每个面板方法都有真实归属：A（authority handler）或 L（`METHODS`
   声明 + 适配器可执行）；只有 `kanban.board`（N）与 P4 待补事件是 M；
2. `runtime.describe.panels` 广告 A + L，客户端不会对已服务方法触发探测或 `-32601`；
3. 会话缺口（`session.usage`、`image.attach`、`pdf.attach`、`connectors.list` session 分支）由
   authority 直服，dispatcher 测试通过；
4. 远程会话下附件不读网关宿主；`git.*`/`hooks.*`（P2）同样走会话语义；
5. 两个 profile 的作用域探针通过；
6. 契约生成物无漂移（`--check` 通过）；
7. `omp-ui/src`：`gateway-client.ts` + `AgentSession`/`SessionManager` 接线后 `bun run check`
   通过，smoke 无 FAIL；
8. §10 的每个 D 项都有 ADR/决定；决定为“不做”的由 `runtime.describe.panels` 返回
   `supported:false` 与原因。
