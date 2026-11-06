# ADR-15：Semgrep 即时化收口与命名 agent 会话运行时（`@model` / `output` / inbox）

**状态**：已决定（2026-11-05）；**P0 已实现**（2026-11-05），**P1 已实现**（2026-11-05），**P2 已实现**（2026-11-05），**P3 已实现**（2026-11-05），**P4 已实现**（2026-11-05），P5 未实现。分阶段：**P0** Semgrep 即时化收口（删索引期扫描与
`_run_semgrep` 持久化，查询面排除 Semgrep 诊断）；**P1** agent 定义 `model: "@role"`；
**P2** 定义契约 `output` / `spawns` / `blocking`；**P3** 命名 agent 的 session-owned inbox、
owner `steer` 与注册接缝（仅 live）；**P4** wake turn、park/revive 与独立配额；**P5**
peer/broadcast 发送面（`agent(action="message")`）与 `interrupts`。

**关联**：ADR-8（Code Intelligence；D17 的索引期 Semgrep 层、`CodeDiagnostic` /
`CodeAnalysisRun`）、ADR-9（Late diagnostics；Semgrep 即时源与“查询面”条款）、ADR-12（prewalk
已使用 `@smol`，本 ADR 的模型角色解析直接复用，不取代 prewalk）、ADR-14（Room 的常驻 Roster /
Inbox；本 ADR 的 session inbox 是它下层的投递队列，不取代 Room 的持久 Inbox）。

Hermes 侧：`hermes_codegraph/indexer.py`、`hermes_codegraph/update.py`、
`hermes_codegraph/query.py`、`hermes_codegraph/schema.py`、`tools/code_intel_tool.py`、
`hermes_cli/subcommands/code.py`、`hermes_cli/config_defaults.py`、
`hermes_cli/config_defaults_auxiliary.py`、`hermes_cli/aux_slots.py`、
`hermes_cli/imported_roles.py`、`hermes_cli/model_presets.py`、`tools/agent_definitions.py`、
`tools/agent_tool.py`、`tools/agent_out_tool.py`、`tools/delegate_tool.py`、
`tools/delegate_tool_registry.py`、`tools/delegation_output_schema.py`、`agent/agent_run.py`、
`agent/agent_loop_service.py`、`agent/agent_session_runtime.py`、`agent/subagent_lifecycle.py`、
`agent/ttsr_prompts.py`、`hermes_cli/config_defaults_agent_sessions.py`、
`hermes_state_agent_sessions.py`、`website/docs/user-guide/features/agent-tool.md`。

omp 侧：本地 checkout `C:\Users\zarag\projects\oh-my-pi`（HEAD `6a86df85`，
`@oh-my-pi/pi-coding-agent` 18.14.0）：`packages/coding-agent/src/prompts/agents/{frontmatter,
scout,reviewer}.md`、`src/config/model-resolver.ts`、`src/discovery/helpers.ts`、
`src/task/settings.ts`、`src/task/executor.ts`、`src/session/irc-bridge.ts`、
`docs/task-agent-discovery.md`。

**取证口径**：Hermes 侧引用本仓库当前工作树（2026-11-05）。omp 侧以本地 checkout 为准；
`omp-ui/node_modules` 只作为客户端已安装行为证据，不当作上游源码。

**修订关系**：

- 本 ADR **修订 ADR-9** 中“索引期 Semgrep 层”和“查询面可返回 Semgrep 旧行”的条款：
  `ADR-9:78-79`（第二句）、`:647-648`、`:845`、`:946-948`。
- 本 ADR **修订 ADR-8** D17 / P4 / P6 中“`hermes code index` 仍跑 Semgrep”的部分；Semgrep
  作为即时诊断源的结论不变。
- 本 ADR **不取代 ADR-14**，且**不改变** ADR-14 D2 的映射“常驻成员 = gateway 在稳定
  `sessions` 行里跑 turn；不是 `agent` 的临时 session”。本 ADR 的 session runtime 面向
  `agent` 工具的命名 session 与 ADR-14 P2 的 **worker lane**（`agent_run` 命名工人）；Room 的
  resident lane（ADR-14 P1）身份/生命周期仍归 ADR-14。Room 的 durable Inbox 仍是跨 agent 的
  持久、定向、幂等队列（Kanban board 内，带 lease/ack）；本 ADR 的 inbox 只是单 session 的
  投递队列。resident 若复用本 ADR 的 wake/inbox，只是投递机制复用，不改变其稳定 `sessions`
  行身份；park/revive 默认不适用于 resident，除非 ADR-14 另行修订。
- 本 ADR **不取代** ADR-12 的 prewalk。prewalk 的目标 `@smol` 由本 ADR 的模型选择器解析。

**范围**：四项——Semgrep 只保留 live 路径；`model` 支持 `@role`；agent 定义支持
`output`/`spawns`/`blocking`；命名 agent 的常驻会话（inbox、wake、park/revive、独立配额）。
**不设计**：Room 的 Callab/Inbox/报告（ADR-14）；prewalk/OMFG/ratchet/auto-graph（ADR-12）；
omp 的 `structured`、`autoloadSkills`、`advisor`、`read-summarize`；消息平台、Desktop、ACP 的
客户端字段；新的 IRC 工具或全局消息总线。

---

## 问题

### 1. Semgrep 有两个真源

当前 Semgrep finding 同时存在于：

- **图**：`Indexer.run` 在 `code_intel.semgrep.enabled=true` 时调用 `_run_semgrep`
  （`hermes_codegraph/indexer.py:145-150`），把 finding 写成 `CodeDiagnostic` 与
  `CodeAnalysisRun(kind="semgrep")`（`:263-320`）；`refresh_file(semgrep=true)` 也走同一路径
  （`hermes_codegraph/update.py:94-105`）。
- **即时扫描**：`write_file`/`patch` 的编辑检查直接调用 `semgrep_scan`
  （`agent/lsp/semgrep_check.py`），`code_intel(diagnostics, path=...)` 也现场扫描
  （`hermes_codegraph/api.py:268-315`）。

图里的 `CodeDiagnostic` 没有 Revision/内容身份字段（`hermes_codegraph/schema.py:135-150`），
ADR-9 已承认按 path 查询可能返回旧字节的 finding（`:202-204`、`:946-948`）。两个真源带来的
问题：

- 索引变慢、可能联网、结果依赖索引时刻；
- 查询面与即时面可能给出不同答案；
- 为了“新鲜度”还要给 `CodeDiagnostic` 加 Revision，而即时路径根本不需要它。

### 2. agent 定义缺 omp 的模型角色与结构化字段

Hermes 的 agent 定义只认 `provider:model` / 裸模型名（`tools/agent_tool.py:133-144`），
`KNOWN_FIELDS` 也没有 `output` / `spawns` / `blocking`
（`tools/agent_definitions.py:18-32`）。omp 的同名定义使用：

- `model: "@smol"` / `"@slow"`（`prompts/agents/scout.md:5`、`reviewer.md:6`），按
  `modelRoles.<role>` 解析（`src/config/model-resolver.ts:1061-1125`）；
- `spawns: scout`（`reviewer.md:5`）、`blocking: true`、`output:` schema
  （`docs/task-agent-discovery.md:30-46`）。

直接把 omp 定义拷进 Hermes 会失败：`@slow` 被当作字面模型名；`output`/`spawns`/`blocking`
被当成未知字段警告后丢弃。

### 3. `agent` 工具是一次性 run，没有可注入的会话

`agent_run.run` 的契约是“Never leaves an agent open”（`agent/agent_run.py:322`）；后台 run 走
`agent_loop_service.submit`（`tools/agent_tool.py:796-806`），结束后 `_settle` 写
`agent_session` 并进 completion queue。追加指令只能：

- 用 `continue_from` 起一个**新 run**（`tools/agent_tool.py:555-565`）；或
- 对 `delegate_task` 的 child 用 `steer`（`tools/delegate_tool.py:849`、
  `tools/delegate_tool_registry.py:124`）。

`agent` 工具没有注册进 `_active_subagents`，所以 `steer_subagent` 够不到它；也没有 wake 或
park/revive。`agent_loop.DEFAULT_MAX_RUN_SECONDS = 1800`
（`agent/agent_loop_service.py:38`）是**卡死上限**，不是 idle TTL；拿它当“停留 30 分钟”会误杀
常驻 agent。

omp 的参考实现是 session 自有的 `IrcBridge`（`src/session/irc-bridge.ts:223-318`）：
streaming 时把父消息转成 steering；否则 `wakeForIrc` 起一个 wake turn；idle 超
`task.agentIdleTtlMs`（默认 **420_000 ms = 7 分钟**，`src/task/settings.ts:396-399`）后落盘为
parked，被消息/resume 自动 revive（`src/task/executor.ts:3555-3666,3709`）。30 分钟是
`task.maxRuntimeMs` 的一个选项（`:363-377`），默认 0（不限），是硬墙钟上限，不是 idle TTL。

---

## 决策

### D1：Semgrep 只在 live 路径；索引不跑、图不存、查询面不返回

**写路径**

1. 删除 `Indexer.run` 的 Semgrep 分支（`indexer.py:145-150`）：索引只产出语法/语义事实，
   不再启动 Semgrep。
2. 删除 `Indexer._run_semgrep`（`indexer.py:263-320`）。它现有的两个副作用
   （`CodeDiagnostic` + `CodeAnalysisRun(kind="semgrep")`）一并删除。
3. `refresh_file(semgrep=true)`（`update.py:94-105`）改为**纯 live**：直接调用
   `hermes_codegraph.analysis.semgrep_scan`，把 findings 按 `api.py:506` 的 `_live_finding`
   形状返回；该 helper 随 P0 移到 `hermes_codegraph/analysis.py::live_finding`，两条 live 路径
   共用。不写 `CodeDiagnostic`、不写 `CodeAnalysisRun`。`semgrep=true` 参数保留，因为
   ADR-9 D8 要求显式查询返回即时结果。
4. post-turn reconciler 继续固定 `semgrep=False`（`hermes_codegraph/auto_update.py:251`），
   不因本 ADR 改变。

**查询面**

5. `hermes_codegraph/query.py::get_diagnostics`（`:630-651`）排除 `tool == "semgrep"`：
   底层 `nodes.search` 只支持等值过滤，因此在内存中过滤（先取 `limit * 2`，过滤后再截断；
   结果不足时按需再取）。`code_intel(diagnostics)` 无 `path` 时走这条查询
   （`tools/code_intel_tool.py:231-250`），`hermes code diagnostics`
   （`hermes_cli/subcommands/code.py:300-303`）同理。
6. `code_intel(diagnostics, path=...)` 的 live 扫描不变（`api.py:268-315`），仍由
   `code_intel.semgrep.enabled` 和 `mode` 控制；这是 Semgrep 唯一的查询来源。
7. `CodeDiagnostic` 节点保留给解析/分析诊断（解析超时、歧义符号候选等，
   `indexer.py:1286`）；`CodeAnalysisRun` 保留给索引/覆盖率/其它分析运行，不再有
   `kind="semgrep"` 的新行。历史 Semgrep 行不迁移，由 `hermes code prune` 或后续清理处理。

**状态与配置**

8. `code_intel.semgrep.enabled` / `mode` 语义不变，只控制 live 扫描；`lsp.display.semgrep`
   仍只控制展示/写路径扫描。`hermes code status` 的 semgrep 块增加
   `source: "live_only"`，不再声称索引层状态（`hermes_cli/subcommands/code.py:401-406`）。
9. `hermes code index` 的返回 payload 删除 `semgrep` 字段；`hermes code doctor` 的
   `semgrep_available` 保留（live 路径仍需要二进制）。

**验收**

- `semgrep.enabled=true` 跑 `hermes code index`：零 `CodeDiagnostic(tool="semgrep")`、零
  `CodeAnalysisRun(kind="semgrep")`。
- `write_file`/`patch` 仍返回 `[semgrep]` 行；`code_intel(diagnostics, path=...)` 仍返回
  live findings。
- 预置旧 Semgrep 行后，`code_intel(diagnostics)` 与 `hermes code diagnostics` 都不返回它们；
  返回 `source: "stored_graph"` 且 `semgrep: "live_only"` 的说明。

### D2：`model: "@role"`，不新增 `role` 字段

`model` 的选择器语法扩展为三种：

| 形式 | 解析 |
|---|---|
| `provider:model` | 现有行为 |
| `model`（裸名） | 现有行为 |
| `@role` | 按 `modelRoles` 等价物解析为 `(provider, model, base_url, reasoning_effort, fallback_chain)` |

**角色映射**

| `@role` | Hermes 路由 |
|---|---|
| `@default` | host 主路线（空 provider/model，继承父会话） |
| `@task` | `delegation`（`hermes_cli/imported_roles.py::read_imported_route`） |
| `@advisor` | `advisor`（同上） |
| `@<aux-task>` | `auxiliary.<task>`，词表来自 `hermes_cli/aux_slots.py::AUX_TASK_KEYS`（`review`、`smol`、`slow`、`plan`、`vision`、`judge` 等） |

**解析与优先级**

1. `agent_definitions` 只做语法检查；`agent_tool._validate` 在 `_split_model`
   （`tools/agent_tool.py:133-144`）之前调用新的 `resolve_model_selector`：
   - 空 → 空；
   - `@...` → 角色解析；
   - 其它 → 现有 `_split_model`。
2. 优先级：caller `model` > 定义 `model` > host 默认。定义里的 `@role` 在**调用时**解析，
   不是文件加载时；改 `auxiliary.<task>` 不需要改定义。
3. 未知角色（拼错、不在词表）在 `_validate` 抛 `DefinitionError`，在构造 run 之前失败；
   不静默回落到 host 模型。
4. 角色解析结果里的 `fallback_chain` 传给 run；`reasoning_effort` 进入现有
   `agent_run.run(reasoning_effort=...)`。不新增 `thinking-level` 字段（Hermes 用
   `reasoning_effort`）。
5. `model_role` 作为 `agent_run.run(..., model_role=...)` 的元数据传入，回显在
   `AgentRunResult`；**不进 system prompt**，不改变 `account_task` 的计费口径。

**非目标**：`model` 的 CSV/数组多候选（omp 支持列表）不在 v1；出现列表时报
“one selector per definition”，由调用方用 `@role` 或 `provider:model` 表达。

**验收**

- `model: "@smol"` 解析到 `auxiliary.smol`；`model: "@task"` 解析到 `delegation`；
  `@advisor` 解析到 `advisor`。
- `model: "@nope"` 在 run 前失败，错误信息列出可用角色。
- caller `model="openai:gpt-5"` 覆盖定义里的 `@smol`。
- 定义文件中的 `model` 行不进入 system prompt。

### D3：定义契约加入 `output` / `spawns` / `blocking`

`tools/agent_definitions.py` 的 `KNOWN_FIELDS`（`:18-32`）增加三项。

**`output`：机器校验的输出模板**

- Hermes 的规范形状是 **JSON Schema**；omp 的 frontmatter `output` 形状
  （`properties` / `optionalProperties` / `elements` / `metadata`）在解析时归一化：

  | omp | JSON Schema |
  |---|---|
  | `properties` 的键 | `properties` |
  | `optionalProperties` 的键 | `properties`，且不在 `required` |
  | `properties` 中未出现在 `optionalProperties` 的键 | `required` |
  | `elements` | `type: "array"`，`items` 由 `elements` 的 `properties`/`type` 生成 |
  | `metadata.description` | `description` |
  | `enum` / `type` | 原样 |

- 归一生效后，复用 `tools/delegation_output_schema.py`：
  `append_output_contract(context, schema)` 把 **OUTPUT CONTRACT** 追加到本次 run 的
  context；`validate_output(text, schema)` 校验最终答案；失败用
  `build_retry_message(errors)` 做**一次**有界重试（与 `delegate_task` 完全一致，
  `tools/delegate_tool_child_run.py:541-581`）。第二次失败返回原文 +
  `output_schema_valid: false` + errors，不静默通过。
- `output` 不写入 system prompt；contract 只追加在本次 run 的 context 尾部，schema 不变时
  前缀稳定。

**`spawns`：可派生名单**

- 语法：`*`、CSV 或数组；空/缺省按 omp 的兼容规则：若定义的 `toolsets` 含 `agents` 或
  `delegation`，等价于 `*`；否则等价于空（不能派生）。
- 约束（v1 **只作用于 `agent` 工具**）：
  - `agent_name` 在 `agent` 工具里是必填的（`tools/agent_tool.py:121-125`，schema
    `required: ["agent_name", "user_prompts"]`），所以 `spawns` 只是 allow-list，不承担
    “省略名称时取第一项”的默认选择——Hermes 没有这个省略形态。
  - `agent_name` 必须在 `spawns` 内；`MAX_DEPTH=3` 仍保留
    （`tools/agent_tool.py:71,744-746`）。
  - **不作用于 `delegate_task`**：`delegate_task` 的 task item 只有
    `goal`/`context`/`output_schema`/`images`/`group`（`tools/delegate_tool.py:790-830`），
    没有命名 agent 选择器；它的 `role` 是 `leaf|orchestrator`，不是定义名。将来若给
    `delegate_task` 加命名选择器，必须复用同一 allow-list 检查，不在本 ADR 实现。
- 实现用 contextvar（与 `_DEPTH` 同模式，`tools/agent_tool.py:80`）在子 run 内携带，run 结束
  恢复；不把名单写进 prompt。

**`blocking`：父等子**

- `blocking: true` 令父等待该 agent，即使后台执行可用：定义级 `blocking` 覆盖 host 的
  `background` 默认（`_KNOB_DEFAULTS["background"]=True`）；调用方的 `background: true`
  不能覆盖它（omp：`blocking: true` makes the parent wait even when async execution is
  enabled，`docs/task-agent-discovery.md:45`）。
- `blocking: false`/缺省保持现有 `background` 语义；调用方仍可选 `background: false` 内联。

**非目标**：`structured`、`autoloadSkills`、`prewalk`、`advisor`、`read-summarize` 不在本 ADR。

**验收**

- omp 的 `scout.md`/`reviewer.md` 原样放入 `skills/agent-presets/agents/` 后：
  `@smol`/`@slow` 解析成功，`output` 归一化为 JSON Schema，`spawns: scout` 生效。
- 输出 schema：合法答案直接返回；非法答案触发一次重试；第二次仍非法返回
  `output_schema_valid: false`。
- `spawns: [scout]` 的定义不能派生 `reviewer`；`agent_name` 缺失仍按现有必填校验报错；
  `delegate_task` 不受 `spawns` 影响（没有命名选择器）。
- `blocking: true` 的后台调用实际阻塞到结果返回。

### D4：session-owned inbox

**身份**：`agent_session_id` 是 IRC id（`tools/agent_tool.py` mint，`run_transcript` 与
`agent_session` 行都以它为准）。`agent_name` 只是标签；`subject` 不唯一。

**队列**：每个 live session 持有三个队列，语义与 omp 对齐：

| 队列 | 语义 | 注入点 |
|---|---|---|
| `interrupts` | 需要打断可中断 `wait` 的 peer/broadcast 消息（P5 启用） | 下一个工具/步骤边界；不硬杀进行中的工具 |
| `asides` | 非打断消息、扩展/用户旁注 | 下一个步骤边界 |
| `deferred_wakes` | 被“池化契约/活跃 run”暂扣的唤醒 | 契约释放后转成受监控的 wake |

**发送面（两条独立路径）**：

| 阶段 | 路径 | 权限 |
|---|---|---|
| P3 | owner → 命名 agent：`delegate_task(action="steer", subagent_id=<agent_session_id>, message=...)` | 现有 `_owns_subagent_record`（`tools/delegate_tool_registry.py:215-237`），只允许 owner 的 spawn tree |
| P5 | sibling/broadcast → `agent(action="message", agent_session_id=..., message=..., delivery="steer\|aside\|wake")` | 同 profile；发送方必须是 live 命名 agent session（或 owning gateway session）；目标 session 必须存在；不跨 profile |

`delegate_task(action="steer")` **不能**作为 peer 路径：它的 authority 是 owner-scoped，
sibling 调用会被 `_owns_subagent_record` 拒绝。P3 因此只声明 owner→session 的注入；
peer/broadcast 的 `interrupts` 队列随 P5 的 `agent(action="message")` 一起启用。

**持久化**：未投递的记录写 `state.db` 的新表 `agent_inbox`：

```sql
agent_inbox(
  id TEXT PRIMARY KEY,
  agent_session_id TEXT NOT NULL,
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  body TEXT NOT NULL,
  kind TEXT NOT NULL,            -- message | broadcast | progress | wake_relay
  reply_to TEXT,
  broadcast INTEGER NOT NULL DEFAULT 0,
  progress INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,          -- pending | delivered | failed
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  delivered_at REAL
);
CREATE INDEX idx_agent_inbox_pending ON agent_inbox(agent_session_id, status, created_at);
```

`agent_session` 行增加 `last_activity_at REAL`、`parked_at REAL`、`model_role TEXT`、
`metadata_json TEXT`（definition source、toolsets、flags、worktree 路径；不存 credentials）。

**投递**：

1. **live streaming**：owner 消息 → `AIAgent.steer()`，以 `steering: true` 的 user 行进入下一个
   边界；peer/broadcast（仅 P5）→ `interrupts`。
2. **live idle**：`wakeForIrc` 起一个 wake turn（D5）。
3. **parked**：先 revive，再按 live idle 投递。
4. **没有 live session**（已 settle 且未 keep-alive）：写 `agent_inbox(status="pending")`；
   下一次 run / revive 先 drain pending，再执行新指令。

**注册接缝**：`agent_run.run` 当前在内部构造 `AIAgent`（`agent/agent_run.py:381`）且没有回调，
`steer_subagent` 无法到达命名 agent。新增：

- `on_agent: Callable[[AIAgent], None] | None`——在 `AIAgent(...)` 之后、
  `run_conversation(...)` 之前调用；
- `on_settled: Callable[[], None] | None`——在 `finally` 关闭 agent 之前调用；
- `keep_alive: bool = False`——为 `True` 时 `finally` 不调用 `agent.close()`，由 park/stop
  负责关闭。

`agent_tool` 在 `on_agent` 里把
`{subagent_id: agent_session_id, agent, owner_agent_session_id, owner_session_id,
owner_transport, accepting_steer: True}` 写进 `_active_subagents`；`on_settled` 负责注销。
这样 P3 的 owner `steer` 才能到达命名 agent。

**安全**：

- 所有 agent 作者正文经 `agent/ttsr_prompts.py::escape_harness_tags` 转义，不能闭合或伪造
  harness 标签；
- **owner 路径**沿用 `steer_subagent` 的 owner session/transport 校验
  （`tools/delegate_tool_registry.py:124-175`）；
- **peer 路径**（P5）不复用 `_owns_subagent_record`：它要求同 profile、发送方是 live 命名
  agent session（或 owning gateway session）、目标 session 存在；发送方身份由当前 run 的
  `agent_session_id` 上下文推断，不由模型自报；peer 只能进 `interrupts`，
  不能直接改父的 system prompt；
- inbox 按 profile 隔离（HX002）；跨 profile 投递一律拒绝。

**控制面**：

- P3：扩展 `delegate_task` 的既有控制动作 `list | steer | stop`
  （`tools/delegate_tool.py:849`）使其能解析命名 agent 的 `agent_session_id`；权限仍是
  owner-scoped。
- P5：给 `agent` 增加可选 `action`（缺省 `"run"`）；`action="message"` 用于同 profile 的
  peer/broadcast 发送，此时 `agent_name`/`user_prompts` 不再是必填，改由
  `agent_session_id`/`message`/`delivery` 组成（schema 用 `if action != "message"` 的
  `required` 分支，或 handler 前置校验；不把 `agent_name` 全局改成可选）。目标 id 来自
  `delegate_task(action="list")` 或 `agent_out`。不新增独立工具。
- `agent_out` 增加 `mode="inbox"`（读取/排空 pending + 最近投递）；ADR-16 P2 后模式为
  `history` / `artifact` / `ast_summary` / `inbox`，`final_response` 已删除。

**实现注记（P3，2026-11-05）**：`asides` 由 live `AIAgent.steer()` 承载（同一把 registry 锁内投递）；
`deferred_wakes` 在 P3 就是 `agent_inbox(status="pending")` 行——没有 wake 消费者，P4 的
wake/revive 负责排空。`agent_out(mode="inbox")` 在 P3 只读 pending + 最近 delivered，不排空。
`agent_run` 的 `keep_alive` 接缝已就位但默认 `false`，常驻开关与 idle TTL 属于 P4。

### D5：wake turn

- **单飞**：同一 `agent_session_id` 同时最多一个 run。streaming 时不 wake，先排队。
- **构造**：idle 且有待投递记录时，用 `agent_run.run(history=transcript,
  agent_session_id=<same>, ...)` 起一个 wake run；IRC 记录作为本次 run 的 user/custom 输入。
  `max_iterations` 仍按每次 run 有界；`agent_sessions.max_wake_turns` 另外限制一条外部消息
  可触发的 wake turn 数（防两个 idle peer 互相 ping-pong），二者独立。
- **追加**：wake 只追加到既有 transcript，不重写任何已发送行；`agent_session.final_response`
  更新为快照，历史仍保留全部 turn。
- **回执**：wake 的最终输出写入 `agent_inbox` 的 result 行（`kind="wake_relay"`），并：
  - 若发送方有 live 会话，走 `process_registry.completion_queue` 唤醒它；
  - 否则由 `agent_out` 读取，不制造无人消费的完成事件。
- **plan/read-only**：只 append 记录，不 wake；与 omp 的 plan-mode idle 分支一致
  （`irc-bridge.ts:308-316`）。
- **预算**：wake run 计入 `agent_sessions` 的独立配额（D7），不计入调用方的
  `agent_loop`。

### D6：park / revive

- **TTL**：`agent_sessions.idle_ttl_seconds` 默认 **1800**（30 分钟，按本 ADR 的要求；
  omp 的默认 420 秒作为文档化的 parity 选项）。条件：无 live run、无 pending inbox、无未完成
  worktree。满足后：
  - `agent_session.status = "parked"`，写 `parked_at`；
  - 关闭 `AIAgent`（工具子进程、HTTP client）、释放 live 配额槽；
  - 保留 transcript、`agent_session` 行、`agent_inbox` 行。
- **硬上限**：`agent_sessions.max_runtime_seconds` 默认 `0`（不限），独立于 idle TTL；命中时
  以 `timed out` 中止并记 `error`，不转 parked。
- **revive**：消息/`resume` 时用 transcript 重建 run（`history` + 原定义重新解析的
  `model_role`、toolsets、definition source；`auxiliary`/`delegation` 的配置变化在 revive 时
  生效），重新注册进 `_active_subagents`，然后投递待处理记录。revive
  不自动发生在进程启动时（避免 thundering herd）；只有显式的消息/resume 触发。
- **worktree**：`worktree: true` 的 run 在 v1 **不 park**；run 结束即 `_finalize_worktree`
  （`agent/agent_run.py:432`），避免 parked 期间留下无人管理的 worktree。需要常驻的
  worktree agent 另立 ADR。
- **重启**：parked 状态跨进程存活；`agent_out` 与 `delegate_task(list)` 能列出 parked；
  `stop` 把 parked 行标为 `cancelled` 并清理 pending。

### D7：独立配额

新增配置块（默认值进 `hermes_cli/config_defaults.py`，不新增 `HERMES_*` 环境变量）：

```yaml
agent_sessions:
  keep_alive: true            # 后台命名 run 结束后保持 live，直到 idle TTL / stop
  idle_ttl_seconds: 1800      # 30 分钟；0 = 不 park
  max_runtime_seconds: 0      # 硬墙钟上限；0 = 不限
  max_live: 8                 # 同时 live 的命名 agent session
  max_parked: 64              # 同时 parked 的 session
  wake_queue: 32              # 待 wake 的消息上限
  max_wake_turns: 1           # 一条外部消息最多触发的 wake turn 数
```

`keep_alive: true` 是行为变化：当前后台命名 run 在 settle 后立即关闭
（`agent/agent_run.py:322` 的“Never leaves an agent open”）；P3/P4 后保持到 idle TTL。
`background: false`（以及定义级 `blocking: true`）仍是一次性内联 run，不进入常驻池；
`keep_alive: false` 恢复旧行为。

- **独立池**：新 `AgentSessionPool`（或 `agent_loop_service` 的独立实例），**不复用**
  `agent_loop.max_workers`（当前默认 8，`agent/agent_loop_service.py:36`）。常驻/唤醒 run
  占自己的槽，不能饿死其它后台任务。
- **溢出**：`max_live` 满时，新 `agent` 后台调用返回结构化 `queue_full`；给 parked agent 的
  消息留在 `agent_inbox(status="pending")`，不丢弃。
- **计数**：按 profile 计数；不做跨 profile 全局池。
- **观测**：`agent_out(mode="inbox")` 与 `delegate_task(action="list")` 返回
  `live/parked/pending` 计数；`hermes doctor` 报告配额配置错误（未知键、负数）。

**实现注记（P4，2026-11-05）**：`agent/agent_session_runtime.py` 是常驻池与 wake/park/revive
的唯一实现；`agent_sessions.*` 的默认值在 `hermes_cli/config_defaults_agent_sessions.py`，
`hermes doctor` 的 `_validate_agent_sessions_config` 报告未知键/负数/类型错误（运行时只 clamp
不抛）。schema 侧 `agent_session` 增加 `last_activity_at`、`parked_at`、`model_role`、
`metadata_json`（重建 spec：定义来源/正文、toolsets、flags、model selector；不含凭据），状态词表
增加 `parked` / `cancelled`。

- 常驻只覆盖**后台**、非 `blocking`、非 worktree 的 run：`agent_tool` 在
  `agent_sessions.keep_alive=true` 时把首跑提交给 `AgentSessionRuntime`（自己的线程与
  `max_live` 槽），`keep_alive=false`、内联 run 与 worktree run 仍走 `agent_loop`/一次性路径。
- **live idle 的 wake** 复用 settle 后仍打开的 `AIAgent`：`agent_run.continue_run(...)` 在同一
  agent 上跑下一 turn，transcript 以内容指纹去重后只追加新事件（不重建、不重写前缀）。
- **parked / 跨进程的 revive** 用 `agent_run.run(history=read_history(...))` 重建，并重新解析
  定义正文与 `@role`（`auxiliary`/`delegation` 的配置变化在 revive 时生效）。
- **park**：watchdog 每 `min(15s, TTL/2)` 扫描；条件是无 run、`pending` inbox 为 0、非 worktree，
  命中后关闭 agent、释放 live 槽、`status="parked"`+`parked_at`。`max_parked` 溢出时取消最旧的
  parked 行（`cancelled`，pending inbox 标 `failed`），行/transcript 保留。
- **按 profile 落库**：池是进程级的，行不是。watchdog/worker 对 `agent_session`/`agent_inbox`
  的每次读写都用会话自己的 `profile` 显式打开 `acquire(<home>/state.db)`，不依赖线程的 ambient
  home（HX002）；两个 profile 各有 idle session 时，sweep 后各自库里都是 `parked`。
- **硬上限**：`max_runtime_seconds>0` 命中时立刻释放槽、行记 `error`（`timed out after Ns`）、
  请求协作中断；不转 parked，迟到的 run 结果不覆盖 error。
- **投递**：running → `AIAgent.steer()`；idle → 唤醒；parked → revive；无常驻 → `pending`。
  `max_live`/`wake_queue` 溢出时消息仍写 `pending`，控制面回复带 `queue_full: true`。
- **stop**：无 live record 时 `delegate_task(action="stop")` 取消 owned 的 idle/parked session
  （行 `cancelled`，pending inbox `failed`）；live 仍沿用 P3 的协作中断。
- **wake relay**：每个 wake turn 的答案写一条 `kind="wake_relay"` 的 delivered 行；只有 sender
  本身是 live 常驻 session 时才向 `completion_queue` 推完成事件，否则由 `agent_out` 读取。
- **max_wake_turns**：P4 的 owner 路径把一条外部消息排队的多个 body 合并成一个 wake turn，并在
  投递时把计数归零；计数与上限为 P5 的 relay 链预留（届时越界只保留 pending，不自动唤醒）。

### D8：上下文与缓存边界

- 定义 metadata（`model`、`output`、`spawns`、`blocking`）不进 system prompt；解析在 run 前
  完成。
- inbox 记录在投递前不进入 prompt；投递时只追加在尾部。
- wake/revive 用同一 transcript 重建，不重写已发送前缀；压缩仍按现有策略处理。
- OUTPUT CONTRACT 追加在 context 尾部；schema 稳定时前缀稳定。schema 变化只影响新 run。
- `park/revive` 不改变 `sessions` 主会话的前缀；它只作用于命名 agent 的 `agent_session`。

---

## 数据与协议

| 对象 | 变化 |
|---|---|
| `Indexer.run` 返回 | 删除 `semgrep` 字段 |
| `CodeDiagnostic` | 保留；不再有 `tool="semgrep"` 的新行 |
| `CodeAnalysisRun` | 保留；不再有 `kind="semgrep"` 的新行 |
| `code_intel(diagnostics)` | 无 path：只返回非 Semgrep 的存图诊断；有 path：live scan |
| `hermes code diagnostics` | 同上；`--rule`/`--severity` 只作用于非 Semgrep 行 |
| `agent_definitions.KNOWN_FIELDS` | `+ output, spawns, blocking` |
| `agent` 定义 `model` | 支持 `@role` |
| `agent_run.run` | `+ model_role: str = ""`（元数据）、`+ base_url` / `+ api_key` / `+ fallback_chain`（`@role` 解析结果随路由走）；`keep_alive` 由 `agent_tool` 管理 |
| `AgentRunResult` | `+ model_role`, `output_schema_valid`, `output_schema_errors` |
| `agent_session` | `+ last_activity_at, parked_at, model_role, metadata_json`；status `parked` |
| `agent_inbox` | 新表（见 D4） |
| `delegate_task` control actions | `list/steer/stop` 可解析命名 agent session（owner scope） |
| `agent` | `+ 可选 action="message"`（P5；同 profile peer 发送）；`action="run"` 时 `agent_name`/`user_prompts` 仍必填 |
| `agent_out` | `+ mode="inbox"`（ADR-16 P2：`final_response` 删除，默认 `history`） |

**配置**：`agent_sessions.*`（D7）；`code_intel.semgrep.*` 语义不变（只控制 live）。

---

## 分阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0** | D1：删索引期 Semgrep、删 `_run_semgrep`、`refresh_file` 纯 live、查询面排除 Semgrep；修订 ADR-8/9 | 索引零 Semgrep 行；写路径仍出 `[semgrep]`；查询面无 Semgrep 旧行 |
| **P1** | D2：`@role` 选择器、角色解析、未知角色报错、`model_role` 元数据（已实现：`hermes_cli/model_roles.py`；`model` 列表报“one selector per definition”） | omp 定义原样可跑；caller 覆盖；未知角色 run 前失败 |
| **P2** | D3：`output` 归一化 + 校验/一次重试；`spawns` 限制；`blocking` 等待 | schema 合法/非法/重试；spawns 拒绝；blocking 阻塞 |
| **P3**（已实现） | D4：session 注册接缝、owner `delegate_task(steer)`、`asides`/`deferred_wakes`、`agent_out(inbox)`；仅 live，不 park | 运行中注入；idle 排队；无 live 时 pending；转义与 owner authority |
| **P4**（已实现） | D5–D7：wake turn、park/revive、独立配额、观测 | 30 分钟 park；消息 revive；配额溢出结构化错误；不占 `agent_loop` 槽 |
| **P5** | peer/broadcast：`agent(action="message")`、`interrupts`、同 profile 授权与广播 | sibling 发送可达；owner-scoped steer 不能冒充 peer；跨 profile 拒绝 |

依赖：P3 依赖 `agent_run` 的 `on_agent`/`keep_alive` 接缝；P4 依赖 P3（park 需要 live 注册与
inbox）；P5 依赖 P4（持久 inbox 与 `interrupts`）。P0 独立，可先合。

---

## 验收（整体）

1. `semgrep.enabled=true` + `hermes code index`：`CodeDiagnostic(tool="semgrep")` 与
   `CodeAnalysisRun(kind="semgrep")` 计数为 0；`write_file`/`patch` 仍内联/延迟 `[semgrep]`。
2. `code_intel(diagnostics)` 在预置旧 Semgrep 行后只返回解析诊断；`source="stored_graph"`，
   并带 `semgrep: "live_only"`。
3. `model: "@smol"` / `"@task"` / `"@advisor"` 解析正确；`"@nope"` 在构造 run 前报错。
4. omp 的 `scout.md` / `reviewer.md` 原样放入 `skills/agent-presets/agents/`：`@smol`/`@slow`
   解析，`output` 校验，`spawns` 生效，`blocking` 生效。
5. P3：owner 的 `delegate_task(action="steer", subagent_id=<agent_session_id>,
   message="...")` 向运行中的命名 agent 注入；下一个边界可见。
6. P5：sibling 的 `agent(action="message", ...)` 到达 `interrupts`；owner-scoped
   `delegate_task(steer)` 不能冒充 peer；跨 profile 在两种路径都被拒。
7. idle 30 分钟后 `agent_session.status="parked"`；发消息后 revive 并继续同一
   `agent_session_id`；transcript 前缀不变。
8. `agent_sessions.max_live` 满时新 run 返回 `queue_full`；给 parked agent 的消息保留为
   `pending`；常驻 run 不占 `agent_loop.max_workers`。
9. 跨 profile 投递被拒；agent 正文中的 harness 标签被转义。

---

## 不采纳

| 方案 | 理由 |
|---|---|
| 保留索引期 Semgrep，并给 `CodeDiagnostic` 加 Revision | 两个真源仍存在；即时路径不需要 Revision；ADR-9 已把 Semgrep 定为即时源 |
| 查询面继续返回 Semgrep 旧行，只标 freshness | 用户明确要求查询面不返回；旧行只会误导 |
| 新增 `role:` 字段 | omp 用 `model: "@role"`；两个名字表达同一件事，定义不可直接移植 |
| 新建 IRC 工具/全局总线 | 窄腰；session-owned inbox + 既有 `delegate_task` 控制面足够 |
| 用 `delegate_task(steer)` 作为 peer 发送路径 | authority 是 owner-scoped（`_owns_subagent_record`），sibling 调用会被拒；peer 走 P5 的 `agent(action="message")` |
| 用 `agent_loop.max_run_seconds` 当常驻 TTL | 它是卡死上限，会误杀 live agent；idle TTL 是独立语义 |
| `blocking` 由调用方覆盖 | omp 的 `blocking: true` 是“即使异步也等待”的契约；定义优先 |
| 用散文 `references/*.md` 代替 `output` schema | omp 的输出模板是机器校验 schema；散文无法保证结构 |
| 直接把 Room Inbox 当 session inbox | Room Inbox 是跨 agent 的持久队列；session inbox 是单会话投递，二者层级不同 |

---

## 遗留

- **历史 Semgrep 行**：不迁移；`hermes code prune` 不专门处理它们，`hermes code doctor` 只报告
  计数。是否在 P0 顺手删除由实现决定（删除需单独命令/审批）。
- **`model` 多候选**：omp 支持列表；v1 只支持单选择器，列表报错。
- **`structured` / `autoloadSkills` / `advisor` / `read-summarize`**：不在本 ADR；`advisor`
  若落地，复用 `@advisor` 的 `advisor` 路由。
- **worktree 常驻**：v1 不 park；需要时另立 ADR 定义 worktree 与 park 的所有权。
- **跨进程 gateway 唤醒**：本 ADR 的 completion queue 只覆盖 live 会话；跨进程需复用
  ADR-14 的 Room runner / gateway 调度，不在本 ADR 设计。
- **jsonschema 缺失**：沿用 `delegation_output_schema` 的降级（解析 JSON 但不校验），
  `output_schema_valid` 仍返回 `true`，并在 payload 中标注 `validated: false`。
