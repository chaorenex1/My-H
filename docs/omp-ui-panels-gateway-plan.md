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
| Theme selector / skin | `skin.list`、`config.get` | `config.set` | A/M | N | skin.list does not exist yet (N); config.get/set are served |
| Settings menu | `config.schema`、`config.get` | `config.set` | A/M | N | config.schema does not exist yet (N) |
| Thinking / effort | `model.options` | `session.mutate` | A | — |  |
| Usage dashboard | `session.usage`、`session.stats`、`billing.state`、`usage.bars` | — | A/L | — |  |
| Jobs panel / job tree | `process.list`、`spawn_tree.list` | `process.stop`、`process.kill` | A/L | E | process.list stats fields (E) and process events are pending |
| Agent Hub / dashboard / transcript | `subagent.list`、`subagent.tail`、`delegation.status` | `subagent.steer`、`subagent.interrupt`、`delegation.pause`、`subagent.config` | A/L/M | N | subagent.config (N) is pending |
| MCP manager / add wizard | `mcp.catalog`、`mcp.servers.list`、`mcp.servers.status` | `mcp.servers.add`、`mcp.servers.remove`、`mcp.servers.set_api_key`、`mcp.servers.oauth.start`、`mcp.servers.oauth.poll`、`mcp.servers.oauth.callback`、`mcp.servers.oauth.cancel` | L | — |  |
| Extensions center | `plugins.list`、`hooks.list` | `plugins.manage`、`hooks.toggle` | A/L/M | N | hooks.list/toggle (N) are pending; Hermes has no in-process extension registry |
| Git TUI | `git.status`、`git.diff`、`git.log`、`git.branches` | `git.stage`、`git.unstage`、`git.commit` | M | N | the git.* contract is not implemented yet (N) |
| Context viewer | `session.context_breakdown`、`session.history` | — | A | — |  |
| Hotkeys sheet | — | — | — | — |  |
| Changelog | `runtime.describe` | — | A | E | runtime.describe does not carry a version yet (E); the changelog content is client-side |
| Debug selector | `runtime.describe`、`session.info` | — | A | — |  |
| BTW panel / history | `btw.history` | `prompt.btw` | A/M | N | btw.history does not exist yet (N) |
| OMFG | — | — | — | — | no Hermes backend: the omp rule-forging workflow has no gateway equivalent (P6) |
| Plan review / save / TOC | `session.plan.read` | `session.plan.save`、`session.plan.approve` | M | N | session.plan.* does not exist yet (N) |
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
## 3. 改动分类与总量

| 类 | 含义 | 方法 |
|---|---|---|
| **L 适配**（已有实现，零重写） | sidecar handler 经 `dispatch_legacy` 同进程执行，绑定 profile；视为已接通 | `mcp.*`、`skills.manage`、`plugins.manage`、`cron.manage`、`billing.*`、`subscription.*`、`usage.bars`、`connectors.catalog/accounts/tools/policy.get/accounts.*`、`vault.*`、`profiles.describe/create/configure/set_asset`、`pet.*`、`voice.*`、`wake.*`、`i18n.*`、`shared_metrics.*`、`spawn_tree.*`、`browser.manage`、`model.save_key`、`model.disconnect`、`session.most_recent`、`delegation.status` |
| **A 会话缺口**（authority 必须自服） | sidecar handler 读自己的 `_sessions`，authority 会话不在其中 | `session.usage`、`image.attach`、`pdf.attach`、`connectors.list`（session-owned 分支） |
| **N 新建** | 网关没有该协议，需 contract + handler + test | `git.*`、`hooks.list/toggle`、`skin.list`、`config.schema`、`btw.history`、`session.plan.read/save/approve`、`subagent.config`、`kanban.board`（可选） |
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
git.stage    {session_id, paths:[...]}           -> {staged}
git.unstage  {session_id, paths:[...]}           -> {staged}
git.commit   {session_id, message, sign?}        -> {sha, summary}
```

- **必须**在会话的 terminal backend 执行（`tools/environments/*`），与 `complete.path` 同规则；
  远程/SSH 会话不得读网关宿主的 git。
- 写操作走 `request_id`；`git.commit` 不做自动 amend，消息为空拒绝。
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
| `subagent.config` | `{subagent_id, model?, prewalk?, advisor?, thinking?}`（角色配置面板） |
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
| Agent Hub | `subagent.spawn_requested/start/thinking/tool/progress/complete` | 已有 |
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
    "git":     {supported: true,  methods: ["git.status", ...]},
    "hooks":   {supported: true,  methods: ["hooks.list", "hooks.toggle"]},
    "omfg":    {supported: false, reason: "no Hermes backend"},
    "kanban":  {supported: false, reason: "desktop-only board"}
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
| **P1** | **适配语义 + 会话缺口**：L 改为“适配器受支持”；`capabilities` 广告 A+L；只补 `session.usage`、`image.attach`、`pdf.attach`、`connectors.list`（session 分支） | 每个面板方法 ∈ {A, L}（M 仅 P2 计划项）；会话缺口 dispatcher 测试通过；不新增 profile 级重写 |
| **P2** | 新方法：`git.*`、`hooks.*`、`skin.list`、`config.schema`、`btw.history`、`session.plan.*`、`subagent.config` | UI 面板可端到端读写；终端作用域测试通过 |
| **P3** | 扩展：`model.options`、`session.list/history`、`process.list`、branch summary | 面板数据完整；无客户端拼凑 |
| **P4** | 事件补齐：process stats、plan.updated、hooks.changed、kanban.board（可选） | 实时面板不轮询 |
| **P5** | **客户端轨道（`omp-ui/src`）**：`modes/rpc/gateway-client.ts`；`AgentSession`/`SessionManager` 就地接线；事件/错误码映射；面板 controller 接线 | `bun run check`；smoke 输出 `ok/pending/unsupported`，无 FAIL |
| **P6** | 设计轨道（与 P0–P5 并行）：§10 的 D 项逐项 ADR；决定为“做”的进入 P2/P3 | 每个 D 项有决定、约束、验收；无未决死入口 |

**P0 状态**：已落地。`docs/omp-ui-panels.json`（45 面板 / 146 方法 / 21 事件，A 59 / L 70 / M 17）、
覆盖门 `tests/tui_gateway/panels/test_panel_coverage.py`、`runtime.describe.panels`
（`PanelCapability`）。清单以 `gateway/panel_inventory.py` 为源，`scripts/gen_panel_inventory.py`
生成；改清单后必须重跑生成器，覆盖门与 `test_generated` 会把漂移挡在 CI。

**P1 状态**：已落地（方向修正后）。`L` 的语义改为“由 `tui_gateway/ws_legacy_fallback.py::dispatch_legacy`
同进程执行、绑定调用者 profile，受支持”；`capabilities()` 广告 A+L；23 个面板 `ready`。
authority 只新增四个会话缺口方法：`session.usage`、`image.attach`、`pdf.attach`、`connectors.list`
（session 分支），契约在 `tui_gateway/contracts/canonical_promoted.py` 复用 legacy 模型。
家族级重写（mcp/vault/billing/profiles/pets/voice/browser/...）已删除——它们由适配器服务，
每个方法只有一份实现。测试：`tests/gateway/test_promoted_{usage,connectors,attachments,surface}.py`。

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
   声明 + 适配器可执行）；只有 P2 计划项是 M；
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
