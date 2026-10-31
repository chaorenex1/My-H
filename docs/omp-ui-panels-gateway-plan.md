# omp-ui 面板接通：Hermes 网关改动计划

> 目标：把 omp-ui 的**所有面板**接到 Hermes 网关。本文只描述**网关侧**要做的改动（contract、
> handler、事件、能力协商、测试）；UI 侧移植见 `docs/omp-ui-gap-plan.md`。
>
> 前提：omp-ui 的最终实现就在 `omp-ui/coding-agent/src`（就地改），客户端直连 canonical
> WebSocket。面板不允许依赖 legacy sidecar 的 `-32601` 回退——回退只在“旧客户端兼容”时保留。

## 0. 范围与非目标

**范围内**：面板需要的每一个读/写 RPC、实时事件、能力声明、profile/终端作用域、契约生成与测试。

**非目标**：
- omp 专属后端（OMFG、cleanse、ratchet、prewalk、auto-graph、in-process extensions）。
- 桌面/网页独有面板（Kanban 看板、Bot Mode、Bot Screen、多窗口、富媒体预览）。
- 终端渲染能力（mermaid/SVG/内联图等）——那些是客户端 `pi-tui` 的事。

这些在 §10 统一标 `unsupported`，由 `runtime.describe` 告知 UI，不静默失败。

## 1. 设计原则

1. **Authority 优先**：面板方法必须由网关 owner（`gateway/*` handler + `CANONICAL_METHODS` 或
   authority 的 `METHODS`）直接服务；legacy sidecar 只保留给旧客户端。
2. **契约先行**：每个方法/事件在 `tui_gateway/contracts/*.py` 声明 `Params`/`Result`/`Payload`，
   `extra="forbid"`；生成 `apps/shared/src/gateway-contract.generated.ts` 与 OpenRPC。
3. **作用域**：会话方法带 `session_id`，走 `@_profile_scoped`；无会话方法带 `profile`，
   走 `_profile_scoped_rpc`（home + secret + terminal scope 三件套）。
4. **终端在会话语义里**：`git.*`、附件、路径类方法必须跑在**会话的 terminal backend**（本地/远程/SSH），
   不能跑在网关宿主；与 `complete.path` 同一条规则。
5. **写操作幂等**：请求带 `request_id`；会话状态改写带 `expected_revision` / `expected_generation`。
6. **事件优先**：实时面板（jobs、agent、usage、plan、pause）用事件，不引入轮询。
7. **能力协商**：`runtime.describe` 返回 `panels` 映射，UI 启动时据此把未实现面板标为
   `pending`/`unsupported`，而不是运行时收到 `-32601` 才炸。

## 2. 面板清单与依赖矩阵

状态图例：**A** authority 已服务（`gateway/*.py` 的 handler，含 `session_ancillary.handlers()` 这类
comprehension 表：`session.control.read`、`process.list`、`subagent.list/tail/steer/interrupt`、
`process.stop/kill`、`reload.mcp`、`file.attach`、`tools.show`、`skills.reload`、`approval.pending/received`）；
**L** 契约存在但只有 legacy sidecar handler；**M** 完全没有方法；**E** 存在但字段/参数不足；
**C** 纯客户端；**U** 不支持；**D** 待设计（当前不支持或部分支持，需先出设计再实现）。

三类拆分的口径（P0 脚本按此判定）：
(a) `gateway/` 有 handler（authority）；
(b) `tui_gateway/contracts` 有声明、handler 只在 `methods_*.py` 的 sidecar；
(c) 完全没有声明。
覆盖清单只覆盖**面板实际调用的方法**，不追求把 281 个方法全部 canonical 化。

| 面板 | 读 | 写 | 现状 | 改动 |
|---|---|---|---|---|
| Model hub / picker / browser | `model.options`、`config.get` | `session.mutate{model}`、`model.save_key`、`model.disconnect` | A / L | **E + P**：`model.options` 补元数据；凭据方法提升 |
| Session selector | `session.list`、`session.active_list`、`session.most_recent` | `session.mutate{rename,archive,delete}` | A / L | **E**：`session.list` 加 `query`/`pinned`/`preview`；`most_recent` 提升 |
| Tree selector | `session.history`、`session.info` | `session.mutate{rewind,branch}` + 分支摘要 | A | **E**：branch payload 支持 `summarize`/`instructions` |
| Rewind selector | `session.history` | `session.mutate{rewind}` | A | 无 |
| Copy selector | 客户端 transcript | — | C | 无 |
| Theme selector / skin | `skin.list`、`config.get` | `config.set{skin}` | M | **N**：`skin.list`（含 token 预览） |
| Settings menu | `config.schema`、`config.get` | `config.set` | M | **N**：`config.schema`（类型/默认/枚举/scope/restart） |
| Thinking / effort | `model.options` | `session.mutate{model,reasoning}` | A | 无 |
| Usage dashboard | `session.usage`、`session.stats`、`billing.state`、`usage.bars` | — | L / A | **P**：usage/bars 提升（`stats` 不改） |
| Jobs panel / job tree | `process.list`、`spawn_tree.list` | `process.stop`、`process.kill` | A / L | **E + P**：`process.list` 补 cpu/mem/started_at/log_tail；spawn_tree 提升 |
| Agent Hub / dashboard / transcript | `subagent.list`、`subagent.tail`、`delegation.status` | `subagent.steer`、`subagent.interrupt`、`delegation.pause`、`subagent.config` | A | **E + N**：`subagent.config`（角色模型/prewalk/advisor） |
| MCP manager / add wizard | `mcp.catalog`、`mcp.servers.list/status` | `mcp.servers.add/remove/set_api_key`、`mcp.servers.oauth.*` | L | **P**：全部提升 |
| Extensions center | `plugins.list`、`hooks.list` | `plugins.manage`、`hooks.toggle` | L / M | **P + N**：hooks 新建 |
| Git TUI | `git.status/diff/log/branches` | `git.stage/unstage/commit` | M | **N**：整套 `git.*`，跑会话终端 |
| Context viewer | `session.context_breakdown`、`session.history` | — | A | 无（`session.context_breakdown` 已由 `gateway/session_tui_ops.py` 服务） |
| Hotkeys sheet | 客户端 | — | C | 无 |
| Changelog | `runtime.describe{version}` + 静态文件 | — | C | 无 |
| Debug selector | `runtime.describe`、`session.info` | — | A | 无 |
| BTW panel / history | `prompt.btw` 事件 + `btw.history` | `prompt.btw` | A / M | **N**：`btw.history` |
| OMFG | — | — | D | 待设计：是否移植 omp 的规则锻造后端（见 §10） |
| Plan review / save / TOC | `session.plan.read` | `session.plan.save/approve` | M | **N**：`session.plan.*` |
| Annotation overlay | — | — | D | 待设计：存储/协议/导出（见 §10） |
| Pause screen | `session.control.read` | `delegation.pause` | A / D（部分） | **partial 待设计**：需要“暂停在跑回合”的原语（见 §10） |
| Session info overlay | `session.info`、`session.stats` | — | A | 无 |
| Transcript browser / outline | `session.history` | — | A | **E**：分页/`row_id`/outline 元数据 |
| Skill picker | `skills.manage`、`commands.catalog`、`complete.slash` | `skills.manage` | L / A | **P**：`skills.manage` 提升 |
| Autoresearch / cleanse / ps-top / live | `process.list`、`voice.*` | — | L / D | ps-top→**E**；live→**P**；cleanse/autoresearch→**D**（见 §10） |
| Billing / subscription | `billing.*`、`subscription.*`、`usage.bars` | `billing.charge`、`subscription.*` | L | **P** |
| Connectors | `connectors.*` | `connectors.connect`、`accounts.*` | L / A | **P** |
| Vault | `vault.list/sources` | `vault.add/remove/unlock/lock/source.set` | L | **P** |
| Profiles / multiplex | `profiles.list/describe` | `profiles.create/configure/set_asset` | A / L | **P** |
| Rollback / checkpoints | `rollback.list/diff` | `rollback.restore` | A | 无（`rollback.session_diff` 已 canonical） |
| Pets | `pet.gallery/info/cells` | `pet.select/hatch/generate` | L | **P** |
| Cron | `cron.manage`、`cron.status` | `cron.submit/cancel`、`cron.manage` | L / A | **P**：`cron.manage` 提升 |
| Login / logout / provider auth | `model.options`、`config.get` | `model.save_key`、`model.disconnect` | L | **P** + 产品决定 OAuth 粘贴 |
| Approvals / ask / sudo / secret | server→client requests | `approval.respond`、`clarify.respond` | A | 无 |
| Todo HUD | `todo.updated` | — | A | 无 |
| Goal / loop bars | `session.control.read` | `session.control`（allowlist actions）+ `session.automation` | A | 无（两者都已 owner 服务） |
| i18n | `i18n.languages/catalog` | — | L | **P** |
| Shared metrics consent | `shared_metrics.status` | `shared_metrics.set` | L | **P** |
| Kanban（只读面板） | `kanban.run`（读模式） | — | A | 可选 **N**：`kanban.board` 若需要列/卡片快照 |
| Spawn-tree replay / diff | `spawn_tree.list/load/save` | — | L | **P** |
| MoA 展示 | `moa.*` 事件 | `config.set`（如需配置） | A | 无/可选 E |
| Collab / QR / LIVE | `groups.*`（桌面 hosted rooms） | — | D | 待设计：omp collab 与 groups 的协议映射/权限/中继（见 §10） |
| Review 摘要 | `review.summary` 事件 | — | A | 无 |

## 3. 改动分类与总量

| 类 | 含义 | 方法 |
|---|---|---|
| **P 提升**（sidecar → authority） | 已有 handler，迁到 owner/canonical 契约 | `mcp.*`、`skills.manage`、`plugins.*`、`cron.manage`、`billing.*`、`subscription.*`、`connectors.*`、`vault.*`、`profiles.*`、`pet.*`、`wake.*`、`voice.*`、`i18n.*`、`shared_metrics.*`、`spawn_tree.*`、`browser.manage`、`image.attach`、`pdf.attach`、`model.save_key`、`model.disconnect`、`session.active_list`、`session.most_recent` |
| **N 新建** | 无方法，需 contract + handler + test | `git.status/diff/log/branches/stage/unstage/commit`、`hooks.list/toggle`、`skin.list`、`config.schema`、`btw.history`、`session.plan.read/save/approve`、`subagent.config`、`kanban.board`（可选） |
| **E 扩展** | 字段/参数/事件不足 | `model.options`、`session.list`、`session.history`、`process.list`、`session.mutate{branch.summarize}` |
| **G 能力协商** | `runtime.describe.panels` | 所有面板 |
| **C 客户端** | 无网关改动 | copy、hotkeys、changelog、context 渲染、theme 预览（omp 主题） |
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

## 5. 侧车提升（P）的统一做法

1. 在 `tui_gateway/contracts/` 为方法补 `Params`/`Result`（若已有 legacy 声明，迁到 canonical 契约）。
2. 在 `gateway/` 找 owner：
   - 会话相关 → `gateway/session_ancillary.py` / `session_tui_*.py` / `session_controls.py`；
   - 进程级/配置类 → 对应 owner 模块，**不要**回退 sidecar。
3. 处理 profile scope：`@_profile_scoped` 或 `_profile_scoped_rpc`；两 home 探针。
4. 去掉对 legacy fallback 的依赖：测试中断言 owner 直接返回，`ws_legacy_fallback` 命中数为 0。
5. 旧客户端仍走 fallback：按 `client.capabilities`/版本门控，不删除 sidecar handler。

提升顺序（按 UI 价值）：`mcp.*` → `skills.manage` → `plugins.*` → `cron.manage` → `billing.*` →
`connectors.*` → `vault.*` → `profiles.*` → `pet.*` → `i18n.*` → `shared_metrics.*` →
`spawn_tree.*` → `voice/wake` → `browser.manage` → `image/pdf.attach` → `usage`。

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

omp-ui 启动时读一次，未支持的面板直接标 `unsupported`，不发请求。新增/提升方法时同步更新该映射，
用覆盖测试保证“面板清单 = 契约清单”。

## 8. 契约、生成与测试

1. 改 `tui_gateway/contracts/*.py` → 跑 `scripts/gen_gateway_contracts.py` → 提交生成物
   （`apps/shared/src/gateway-contract.generated.ts`、`gateway-contract.openrpc.json`）。
2. 契约测试：每个方法一组 `Params`/`Result` 正反例；未知参数 4000；未声明方法 import 期拒绝。
3. 面板覆盖门（P0 产出）：`tests/tui_gateway/panels/test_panel_coverage.py`
   - 读 `docs/omp-ui-panels.json`（面板 → 方法/事件）；
   - 断言每个方法在 `METHODS`/`CANONICAL_METHODS` 声明、由 owner 服务、并且 `runtime.describe.panels`
     覆盖；
   - 断言没有面板方法只存在于 legacy sidecar。
4. profile 作用域：两个磁盘 home + 只在副 profile 存在的 `.env`，调用面板方法断言解析正确、启动 profile
   未被污染。
5. 终端作用域：`git.*`/附件方法在远程会话下不得读网关宿主路径（用假的 remote backend 断言）。
6. 幂等/CAS：写方法重复 `request_id` 返回同一回执；`session.mutate` 过期 revision 返回 409x。

## 9. 分阶段

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| **P0** | 面板清单落盘（`docs/omp-ui-panels.json`）、覆盖脚本、`runtime.describe.panels` 骨架 | 覆盖门能列出 A/L/M；CI 可跑 |
| **P1** | 提升高价值 sidecar：mcp、skills、plugins、cron.manage、billing、connectors、vault、profiles、pet、i18n、shared_metrics、spawn_tree、voice/wake、browser、image/pdf attach | 每项 owner 直服；fallback 命中 0；契约测试通过 |
| **P2** | 新方法：git、hooks、skin.list、config.schema、btw.history、session.plan.*、subagent.config | UI 面板可端到端读写；终端作用域测试通过 |
| **P3** | 扩展：model.options、session.list/history、process.list、branch summary | 面板数据完整；无客户端拼凑 |
| **P4** | 事件补齐：process stats、plan.updated、hooks.changed、kanban.board（可选） | 实时面板不轮询 |
| **P5** | 旧客户端门控：interactive 新客户端不再触发 fallback；旧客户端保持 | fallback 仅旧版本命中 |
| **P6** | 设计轨道（与 P0–P5 并行）：§10 的 D 项逐项 ADR；决定为“做”的进入 P2/P3 | 每个 D 项有决定、约束、验收；无未决死入口 |

**P0 状态**：已落地。`docs/omp-ui-panels.json`（45 面板 / 146 方法 / 21 事件，A 59 / L 70 / M 17）、
覆盖门 `tests/tui_gateway/panels/test_panel_coverage.py`、`runtime.describe.panels`
（`PanelCapability`）。清单以 `gateway/panel_inventory.py` 为源，`scripts/gen_panel_inventory.py`
生成；改清单后必须重跑生成器，覆盖门与 `test_generated` 会把漂移挡在 CI。

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
| 提升 sidecar 引入行为漂移 | sidecar handler 有进程级语义（如 `process.stop`/`reload.mcp`） | 只提升面板需要的只读/会话级方法；进程级保持 owner 规则 |
| 终端作用域 | git/附件在网关宿主执行会读写错目录 | 统一走 `tools/environments`，远程会话测试 |
| 契约膨胀 | 30+ 面板方法一次性进 canonical | 按 P1–P4 分批；每批生成 TS/OpenRPC 并跑覆盖门 |
| profile 泄漏 | 面板方法无 session 时默认 launch home | `_profile_scoped` + 两 home 探针 |
| 能力协商漂移 | `runtime.describe.panels` 与真实契约不一致 | 覆盖测试从 `panels.json` 生成断言 |
| 旧客户端 | 删除 fallback 会破坏旧 Ink/desktop | 版本门控，sidecar 保留 |
| D 项长期悬空 | “待设计”变成永久 unsupported | P6 每项必须有决定与验收；UI 只显示真实状态 |

## 12. 验收

```bash
python scripts/gen_gateway_contracts.py --check
scripts/run_tests.sh tests/tui_gateway/panels/ tests/tui_gateway/contracts/
# 面板覆盖门
python -m pytest tests/tui_gateway/panels/test_panel_coverage.py -q
```

通过条件：

1. `docs/omp-ui-panels.json` 中每个方法都有契约、owner handler、`runtime.describe.panels` 条目；
2. 新客户端调用面板方法时 legacy fallback 命中数为 0；
3. 写方法幂等、CAS 拒绝可复现；
4. 远程会话下 `git.*`/附件不读网关宿主；
5. 两个 profile 的作用域探针通过；
6. 契约生成物无漂移（`--check` 通过）；
7. §10 的每个 D 项都有 ADR/决定；决定为“做”的已进入契约清单，决定为“不做”的由
   `runtime.describe.panels` 返回 `supported:false` 与原因。
