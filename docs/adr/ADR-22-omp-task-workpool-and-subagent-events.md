# ADR-22：omp task / workpool / eval agent 与子代理事件模板对齐

**状态**：提议（2026-11-14，第三轮修订）。**实现暂停**：本轮只修订决策；D1/D4 为最新口径，
取代首版。**范围**：`tools/agent_tool.py`、`agent/agent_run.py`、
`tools/eval_host_ops.py`、`tools/code_execution_tool.py`、`agent/agent_session_runtime.py`、
`agent/prompts/{assembler,renderer,prompt_sections}.py`、
`agent/prompts/system/{task,task-async-contract,task-coordination-advisory,
task-specialization-advisory,task-spawn-feedback,task-summary,task-follow-up,
workpool-batch,workpool-turn-result,subagent-async-pending,subagent-completion-probe,
subagent-yield-reminder}.md`。

**第三轮修订（2026-11-14，本版）**：

1. **D1 再修订**：不只是 `task.md`。`agent` 的工具描述改为**整个 task 系列模板**按调度模式
   组装：`task.md` 保持单文件整体，`task-async-contract`/`task-coordination-advisory`/
   `task-specialization-advisory`/`task-spawn-feedback`/`task-summary`/`task-follow-up`/
   `workpool-*` 是独立契约文件；`AGENT_SCHEMA["description"]` 在代码中为 `""`，经
   manifest → renderer → `assemble_task_tool(mode, ctx)` 在会话构建时填充。
2. **调度模式**：`delegation.scheduling_mode` 默认 `parent`（父子代理）；`team`、`room` 只预留
   名字，选中即 fail closed，不静默降级。模式是纯会话级，不是 per-call 参数，agent
   definition 也不能覆盖，避免同一会话出现多份工具描述。
3. **D4 保留**：`apply`/`merge` 在范围内；`apply=auto` 作废，worktree 走显式状态机。
4. **实现暂停**：本轮只修订 ADR，不写代码。首版之后已产生的本地代码改动不作为本 ADR 的
   已实现状态；实现顺序见文末映射。

**关联**：ADR-15（resident named sessions）、ADR-16（session bundle / `agent_out`）、
ADR-18/19（系统提示与请求级模板分层）、ADR-20（请求级注入通道）。本 ADR 不改变
“请求级模板不进系统提示”的缓存不变量。

---

## 上下文

omp 的 `task` 工具、eval `agent()`/`workpool()` 与 Hermes 的 `agent`/`delegate_task`/eval host ops
是同一类能力，但三处语义没有对齐：

1. `agent/prompts/system/` 下的 `subagent-async-pending.md`、`subagent-completion-probe.md`、
   `subagent-yield-reminder.md` 已从 omp 移植并登记为 `scope: request`，但生产代码零调用点。
   结果是模板存在、行为缺失：子代理在自有后台 job 仍在跑时提前结束；长任务没有进度探测；
   预算耗尽/空转时没有 yield 专用提醒。
2. Hermes 的 `agent` 工具已经覆盖 omp `task` 的多数参数（`tasks[]`、`task_file`、`context`、
   `solution_space`、`output_schema`/`schema_mode`、`isolated`/`worktree`、`effort`、
   `model`、`enable_tools`、`name`、`blocking`），但模型可见的工具描述仍是 Hermes 自己的
   说明，**整个 task 系列契约没有 Hermes 载体**；`agent://` 结果、并行建议、
   阻塞/结构化标记也没有进入模型可见的契约。omp 的 `task.md` 是单文件整体，
   不能拆成多个 schema/request 片段；其余 `task-*` 文件是独立契约，不是它的拆分。
3. eval `workpool()` 当前是“每个 item 一个一次性子代理”（`tools/eval_host_ops.py`
   `_workpool_worker`），没有 omp 的 keep-alive worker、`<workpool>` batch 提示词、
   逐 item `yield { key, data }` 协议。`subagent-system-prompt.md` 已有
   `work_pool_yield_items` 分支，但没有运行时能把它置真。
4. eval `agent()` 只透传 `prompt/agent/label/tools`，omp 的 `schema`、`schemaMode`、
   `isolated`/`apply`/`merge`、`model`、`effort`、`solutionSpace` 没有进入 assignment。
5. `agent/prompts/system/subagent-yield-reminder.md` 的 `budget_stop` 分支与
   `agent/conversation_loop.py` 的通用 `RUN_BUDGET_WRAPUP_NOTICE` 重复；`idle` 分支与
   `agent/turn_stop_gates.py` 的通用停止门重复。

---

## 决策

### D1. `agent` 的工具描述 = task 系列模板按调度模式组装；代码中定义为空

omp 的 `task` 语义直接由现有 `agent` 工具承接，不新增工具、不做 `task` 别名。原因：

- Hermes 的核心工具面已经很大，新工具的缓存/发现成本高于收益（根 `AGENTS.md` 的窄腰原则）。
- `agent` 已有命名定义、resident session、`yield`、`agent_out`、worktree 与 batch；omp `task`
  的差异是提示词、结果契约和少数参数，不是执行模型。

**不是只移植 `task.md`。** omp 的 task 提示是一个模板族，至少包括：

| 文件 | 角色 | Hermes 载体 |
|---|---|---|
| `task.md` | 核心工具契约；**保持单文件整体，不拆内部 `# Inputs`/`# Format`/`# Delegation`** | `scope: tool_description` |
| `task-async-contract.md` | async 结果/委派契约 | `scope: tool_description`（按 async 裁剪） |
| `task-coordination-advisory.md` | 多 sibling 协调 | `scope: request`（按 sibling count 事件渲染） |
| `task-specialization-advisory.md` | 泛型 worker 提示 | `scope: request`（spawn 事件） |
| `task-spawn-feedback.md` | spawn 回执 | `scope: request`（spawn 结果） |
| `task-summary.md` | 结果摘要 | `scope: request`（run settle） |
| `task-follow-up.md` | 后续/恢复提示 | `scope: request`（resident/follow-up） |
| `workpool-batch.md` / `workpool-turn-result.md` | workpool 输入/回执 | `scope: request`（eval host op） |

**`task.md` 仍是一份完整文件**；上表的其他文件是独立的 `task-*` 契约，不是把 `task.md`
拆开。禁止把 `task.md` 的 `# Inputs`/`# Format`/`# Delegation` 搬成多个文件，也禁止把它的
正文复制进 `AGENT_SCHEMA`。

#### D1.1 载体：代码中描述为空，文件 → renderer → assembler

`AGENT_SCHEMA["description"]` 在代码中固定为 `""`。模型看到的描述由
`assemble_task_tool(mode, ctx)` 返回，路径是：

```text
manifest.json 的 task 系列条目
  → renderer 的 typed render_task_*()
  → assembler.assemble_task_tool(mode, ctx)
  → 会话工具面构建时填入 agent 工具的 description
```

- `manifest.json` 新增 `scope: "tool_description"`。`prompt_sections.load_sections()` 接受它；
  `render_section()` 仍只接受 `system`/`request`，工具描述走
  `render_tool_description(key, context)`。
- `assemble_main()` 与 `assemble_request()` **不读取** `scope: tool_description`；三条通道
  互不混用。`tool_description` 仍复用 `context_keys` / `context_types` / `StrictUndefined`。
- `assemble_task_tool()` 只选 `kind: description` 的条目；`kind: event` 的 task 文件由各自
  调用点渲染，不进入工具描述。
- 实现模式必须返回非空描述；空结果视为模板/注册错误，**fail closed**，不能静默发送一个
  没有契约的 `agent` 工具。

#### D1.2 调度模式：默认 `parent`，预留 `team` / `room`

`agent` 的调度模式是会话级配置 `delegation.scheduling_mode`，默认 `parent`：

| mode | 状态 | 组装语义 |
|---|---|---|
| `parent` | 默认、已实现 | 父子代理：`task.md` + async contract + parent 模式的协作/回执事件；siblings 用 `agent_message`，无持久团队/房间 |
| `team` | **仅预留名字** | 未来的持久团队/roster/ownership；当前选中即 fail closed（`scheduling_mode "team" is not implemented`），不静默降级到 parent |
| `room` | **仅预留名字** | 未来的共享 room/IRC；当前选中即 fail closed，不静默降级 |

模式是**纯会话级**：唯一来源是 `delegation.scheduling_mode`，默认 `parent`，在会话构建时
读取一次。它不是每次 `agent` 调用的参数；**agent definition 不能覆盖它**——一个 definition
若声明不同的 `scheduling_mode`，会话构建直接 fail closed（`agent definitions may not override
delegation.scheduling_mode`），否则同一会话里不同 `agent` 调用会需要不同的工具描述，破坏
静态前缀。`team`/`room` 的实现不在本 ADR；名字先进入 schema/config 的 enum
（`parent | team | room`）以固定拼写，运行时校验对 `team`/`room` 返回明确错误，绝不静默
当成 `parent`。

#### D1.3 动态裁剪规则

- 只在**会话构建时**按 `mode` + capabilities 裁剪：`async_enabled`、`batch_enabled`、
  `structured`、`file`、`isolation_enabled`、`apply_isolated_changes`、`scout_available`、
  `default_agent`、`agents`。
- 裁剪逻辑在 `assemble_task_tool()`，不在模板里重复判段级开关；模板内部只保留 omp 原文的
  条件分支（Handlebars → Jinja2）。
- 逐轮变化的事实（siblings 数量、`m<N>` 绑定、运行中 job）不进工具描述，走
  `task-coordination-advisory.md`、`task-specialization-advisory.md`、`task-spawn-feedback.md`
  与工具结果。
- mid-session 设置变化只对**下一个会话**生效；不存在原地替换工具描述的 `--now` 路径。
  立即生效只能新开会话。

#### D1.4 一致性约束

- `task.md` 与 omp 源文件用固定快照做逐行 diff；任何 Hermes 特有内容在模板头部或本 ADR
  注明，不能靠“拆文件”隐藏差异。
- `task-*` 事件模板也各自单文件，不把 `task-coordination-advisory` 等再拆成更多片段。
- `assemble_task_tool("parent")` 的输出顺序固定（manifest `order` + 代码），同输入字节稳定。

### D2. 三个 subagent 事件模板的接线点

全部保持 `scope: request`，不进系统提示；调用点直接使用 `agent/prompts/renderer.py` 的
`render_subagent_*`。

| 模板 | 触发 | 行为 |
|---|---|---|
| `subagent-async-pending.md` | 子代理提交终态 `yield` 后，`process_registry.running_owned_by(task_id)` 非空 | 不结算；等待自有后台 job 结束，把结果与模板追加为一轮 user 消息，再运行一次，直到 job 清空且拿到新的终态 `yield`（或达到轮次上限） |
| `subagent-completion-probe.md` | resident session 运行中，且 `agent_sessions.completion_probe` 为真 | 用 `agent.ephemeral_prompt.run_ephemeral_prompt` 发一次无工具的 side turn；解析单行 JSON `{pct,stage,watch}`，写入 session 快照与日志，不写 transcript |
| `subagent-yield-reminder.md` | `budget_stop`：run 预算跨过 80%；`idle`：结构化子代理结束但没有终态 `yield` | 预算分支替换通用 `RUN_BUDGET_WRAPUP_NOTICE`（仅 `_subagent_role` 存在时）；idle 分支在 `_execute_turn` 中做一次有界重试（`max_retries=1`） |

#### D2.1 async-pending 的 drain 契约

- 所有权沿用既有 `tools/delegate_tool_child_run.py` 的 `running_owned_by(child_task_id)` /
  `unread_completions_owned_by(child_task_id)` 语义，不再造第二套 ownership；新增工作只有
  “把 job 结果送回子代理的 wake turn 并让它重新 yield”。
- 循环条件必须是 `running_owned_by(task_id)` 非空，不能只看一次 `wait()`：
  `process_registry.wait` 受 `TERMINAL_TIMEOUT`（默认 180s）钳制，返回
  `exited | timeout | interrupted | not_found | error`。
- `timeout` 表示 job 仍在跑：继续循环，不把它当完成。
- `interrupted` 表示用户 steer/redirect 落到了这个 wait：把它当 steer 边界，停止 drain，
  让子代理先处理新消息并重新 yield；不要把 interrupted 的 snapshot 当 job 结果。
- 把 `wait` 返回的 `output` 放进 follow-up 之前必须经
  `tools.delegation_redaction.redact_child_text`，避免子进程输出里的秘密进入 prompt。
- 轮次上限与总 deadline 必须显式；超过上限时按“未完成”结算，payload 说明还有哪些 job。

#### D2.2 completion probe 的接线

- 使用现有 `agent/ephemeral_prompt.py:run_ephemeral_prompt`（omp
  `runEphemeralTurn` 的对应物）：cache-parity fork、工具在 dispatch 处拒绝、不写 transcript。
- 通过 `agent_run.run(..., on_agent=...)` 捕获活的 child agent，调度器只持有它的弱引用/getter；
  agent 已关闭或 turn 已结束时跳过该次 probe。
- 结果写入 resident session 的进度快照（`pct` / `stage` / `watch`），并可在 `agent_out`
  的 status/peek 中读取；不新增 probe 工具、不新增消息通道。
- 成本门：`agent_sessions.completion_probe` 默认关闭；开启后按固定退避（2/5/10/30/60 分钟，
  末档重复）调度，失败只记日志。

**不采用**的做法：

- 不把 async-pending 做成新的进程/队列服务；`process_registry` 已经是自有 job 的权威。
- 不让 completion probe 进入 transcript、不写 session row、不增加核心工具；它只是 side turn。
- 不把 yield reminder 塞进系统提示；它必须走 user/tool 行（ADR-19 D10）。

### D3. Workpool = keep-alive 子代理的 batch turn，不是“每 item 一个一次性子代理”

`workpool()` 的语义改为：

1. `push([...])` 把 item 排入池队列；池在 `limit` 内启动一个 keep-alive 子代理。
2. 一次 turn 取当前队列的一批 item，渲染 `<workpool pool="…" batch="…">`（模板
   `workpool-batch.md`），item 用 1-based 序号编号。
3. 子代理的 `yield` 支持 `{ key: <1-based>, data|error }`；每次 yield 记录一个 item，
   最后一个 key 自动结束 turn。输出 schema 按 item id（`<pool>#<seq>`）组装，调用方
   从 agent 结果取 `{itemId: value}`。
4. 池继续用同一个 keep-alive 子代理处理后续 batch；`close()` 取消未完成 item。

#### D3.1 keep-alive 池的 key 必须按 turn 重置

`build_yield_tool` 与 `YieldState` 在 agent 构建时创建一次；keep-alive worker 的第二个 batch
会继承第一个 batch 的 `keys`，如果 schema 里还写死 `maximum`，更大的后续 batch 会被旧上界
拒绝。因此：

- `YieldState.begin_turn(keys)` 在**每个 workpool turn 开始前**设置 `self.keys`、清空
  `_completed_keys`/`sections`/`payload`；非 workpool turn 传空序列恢复普通协议。
- `build_yield_tool` 的 JSON Schema **不写 `maximum`**；`key` 的范围在 `record()` 时按
  `1..len(self.keys)` 校验，错误返回可读的 `key must be between 1 and N`。
- `work_pool_yield_items` 通过 `SubagentRole` 置真，使
  `subagent-system-prompt.md` 的 Workpool 协议分支生效。**不**把该字段暴露给普通模型调用：
  它只由 host workpool 在内部传入。

### D3.2 task 系列的 scope 划分

- `scope: tool_description`：`task.md`、`task-async-contract.md`（组成 `agent` 描述）。
- `scope: request`：`task-coordination-advisory.md`、`task-specialization-advisory.md`、
  `task-spawn-feedback.md`、`task-summary.md`、`task-follow-up.md`、`workpool-batch.md`、
  `workpool-turn-result.md`（由各自事件调用点渲染）。
- `assemble_task_tool()` 只消费 `tool_description` 且 `kind: description` 的条目；事件模板
  不进入工具描述。两种 carrier 不能混用。

### D4. eval `agent()` 的 assignment 与选项

#### D4.0 assignment 契约（与选项表分开）

`assignment` 仍然是 `parsed.prompt` 的 `trim()` 结果；`subagent-user-prompt.md` 只做
`Complete assignment thoroughly:` 包装。选项**不进入** assignment 文本，它们是 host op →
`agent` 工具的参数；因此同一个 prompt 配不同 schema/isolation 时，`{{assignment}}` 字节不变。

#### D4.1 选项表

| omp `agent()` | Hermes 参数 | 备注 |
|---|---|---|
| `agent` | `agent_name` | |
| `label` | `subject` | |
| `schema` / `schemaMode` | `output_schema` / `schema_mode` | |
| `isolated` | `isolated`（alias `worktree`） | |
| `tools` | `enable_tools`（仅注册工具） | eval 自定义工具不进入 `agent`；ADR 记录为差异 |
| `model` | `model` | |
| `effort` | `effort` | |
| `solutionSpace` | `solution_space` | |
| `context` | `context` | XML 之外的共享契约块 |
| `continueFrom` | `continue_from` | |
| `background` | `background` | |
| `apply` | `apply` | 显式应用开关；默认 `false`；**不再有 `auto`** |
| `merge` | `merge` | 默认 `true`；`true` = merge commit，`false` = 只产出 patch，保留 branch |

#### D4.2 worktree 生命周期（显式状态机，废弃 `apply=auto`）

Hermes 当前的 `worktree: true → _finalize_worktree(apply="auto")` 把“是否改父工作区”藏在一个
隐式默认里：父区干净就 merge，父区脏就保留 branch，调用方无法提前知道结果，也无法表达
“只隔离、不要应用”。这与 omp `isolated` / `apply` / `merge` 的契约不一致，必须改。

状态机（一次 isolated run 的生命周期）：

```text
requested
  ├─ 非 git / 非本地 backend / add 失败 → unavailable（用共享工作区，payload 说明原因）
  └─ created
       └─ running
            └─ inspected
                 ├─ apply=false                 → retained（branch/patch 保留，父区不动）
                 ├─ apply=true, merge=false     → patched（写 patch，branch 保留，父区不动）
                 ├─ apply=true, merge=true      → applied（commit + merge）
                 └─ 父区脏 / 冲突 / probe 失败  → conflicted | failed（abort merge，branch 保留）
```

规则：

1. **默认 `apply=false`。** `worktree: true` / `isolated: true` 只表示“隔离执行”，不再隐式
   merge。需要改父区的调用方必须显式传 `apply: true`；这是一次有意的行为变更。
2. **`merge` 只在 `apply=true` 时有意义。** `merge=true` 用 `--no-ff` merge；`merge=false`
   只把 diff 写到 patch 文件并把 branch/branch head 留给调用方，父区不动。
3. **父区前置条件。** `apply=true` 前必须确认父区干净（沿用现有双 `core.autocrlf` 探测）；
   不干净直接 `conflicted`，不尝试 merge。`apply=false` 不检查父区。
4. **失败不吞。** commit 失败、merge 冲突、probe 失败都进 `conflicted`/`failed`，执行
   `git merge --abort`（若已开始 merge），branch 保留，payload 带 `apply_error` 与 branch。
5. **清理只在终态后。** `applied` 后可 prune worktree；`retained`/`patched`/`conflicted`/
   `failed` 必须保留 worktree 或它的 patch + branch，调用方需要能复查。
6. **payload 是契约。** 每次返回 `state`、`applied`、`merge`、`branch`、`patch_path`、
   `parent_clean`、`apply_error`、`conflict`、`worktree_path`；不得再用“0 commits/clean”
   或 `exitCode == 0` 暗示应用成功。

| state | apply | merge | 父区 | 必返回 |
|---|---|---|---|---|
| `retained` | false | 任意 | 不动 | `branch`、`worktree_path`、`applied=false` |
| `patched` | true | false | 不动 | `branch`、`patch_path`、`applied=false` |
| `applied` | true | true | merge 后更新 | `branch`、`worktree_path`、`applied=true`、`merge_commit` |
| `conflicted` | true | 任意 | 不动 | `branch`、`conflict`、`apply_error`、`applied=false` |
| `failed` | 任意 | 任意 | 不动 | `branch`、`worktree_path`、`apply_error`、`applied=false` |
| `unavailable` | 任意 | 任意 | 共享工作区 | `requested=true`、`created=false`、`note` |

`apply` / `merge` 同时进入 `agent` 工具参数与 eval `agent()` 的 host op。`apply` 是本次
ADR 的一部分，不再列为非目标；实现顺序紧随 D1（task 系列组装 + `parent` 模式）之后，单独提交、
单独回归，因为它改变现有 `worktree: true` 的语义，需要迁移说明。

#### D4.3 迁移

- 现有依赖 `worktree: true` 自动 merge 的调用方必须在同一提交里显式加 `apply: true`；
  未改的调用方得到 `retained`，payload 的 `applied=false` 与 branch 是唯一提示。
- `delegation.worktree_isolation` 的 host 配置只决定“是否允许隔离”，不再决定“是否应用”。
- ADR-16 的 `agent_out` artifact 仍记录 report/tool logs；worktree 的枝/patch 通过
  `worktree` payload 与 `agent_out` 的 artifact 列表暴露，不新增 URI。

### D5. 结果契约保持 `agent_session_id` / `agent_out`，不复制 omp 的 `agent://` 读写面

omp 的 `task-result` XML、`agent://` 字段读取、`history://` 由 Hermes 的 `agent_out`
（`history`/`artifact`/`ast_summary`/`inbox`）承担。模板移植只补“输入/协作/完成”提示，
不新增 URI 协议；`agent://` 只保留为 handle 字符串与 `agent_out` 的既有身份。

| 模板 | 结果载体 |
|---|---|
| `task-summary.md` | `agent` 工具的同步结果文本 / 后台完成回执；`agent_out` 的 `history`/`artifact` 是同一结果的持久读取面。字段仍是 `agent_session_id`/`status`/`transcript_path`。 |
| `task-follow-up.md` | resident/background 的 follow-up/wake 提示；通过 `agent_message` / inbox 送达，不新增 channel。 |
| `task-spawn-feedback.md` | `agent` 工具的 spawn 回执文本（同步/后台同一套）。 |
| `task-coordination-advisory.md` / `task-specialization-advisory.md` | sibling/worker 的事件建议，挂在 spawn 回执旁边，不改变 JSON 字段。 |
| `workpool-turn-result.md` | `workpool()` 的 `status()` / `peek()` 返回的 pool 结果；`push()` 仍返回 item id。 |

**不新增 URI**：不实现 `agent://<id>/<field>`、`history://`、`task-result` 的自定义读取语法；
`agent://` 只作为 handle 字符串，实际读取仍走 `agent_out` / `workpool status|peek`。

---

## 实施映射

| 模块 | 改动 |
|---|---|
| `tools/agent_yield_tool.py` | `YieldState(keys=...)`、`key` 参数、逐 item 记录/剩余 key、终态组装 |
| `agent/prompts/system/workpool-batch.md` | 新请求级模板；`renderer.render_workpool_batch()` |
| `tools/eval_host_ops.py` | 池队列 → batch worker；渲染 batch 模板；传 `work_pool_yield_items` |
| `tools/code_execution_tool.py`、`tools/eval_js_runner.ts` | `workpool()` 文档/签名；`agent()` 选项透传 |
| `agent/prompts/system/task.md` | omp `task.md` 整文件移植（**不拆内部小节**） |
| `agent/prompts/system/task-{async-contract,coordination-advisory,specialization-advisory,spawn-feedback,summary,follow-up}.md` | 其余 task 系列，各自单文件；`kind` 决定进描述还是事件 |
| `agent/prompts/prompt_sections.py` | `scope: tool_description`；`kind`/`order`/`modes` 元数据；`render_section()` 仍拒绝 tool_description |
| `agent/prompts/renderer.py` | `render_task_*()`（每个 task 文件一个 typed 入口）；`render_workpool_batch()` |
| `agent/prompts/assembler.py` | `assemble_task_tool(mode, ctx)`：选 `kind=description`、按 mode/order 渲染并 join |
| `tools/agent_tool.py` | `AGENT_SCHEMA["description"]=""`；内部 `work_pool_yield_items`；`apply`/`merge` 参数 |
| `delegation.scheduling_mode`（config / `AgentSession`） | 唯一 mode 来源；默认 `parent`；definition 覆盖或 `team`/`room` 选中均 fail closed |
| 会话工具面构建处（`tools/registry.py` / `model_tools.py` 的 schema 收集路径） | 用 `assemble_task_tool()` 的返回值填入 `agent` 描述，一次成型 |
| `tools/subagent_worktree.py` | 显式 worktree 状态机（D4.2）；`apply`/`merge`；移除 `apply=auto` 路径 |
| `agent/agent_run.py` | `_execute_turn`：async-pending drain + 一次 yield-reminder 重试；worktree finalize 传入 `apply`/`merge` |
| `agent/conversation_loop.py` | 子代理 run 预算使用 `subagent-yield-reminder` budget 分支 |
| `agent/completion_probe.py` | 解析器 + 调度器；resident session 接线 |
| `agent/agent_session_runtime.py` | `completion_probe` 配置、启动/停止、快照字段 |
| `docs/adr/ADR-22` | 本文件 |

> 实现顺序（修订后）：D1 的 task 系列 + `tool_description` scope + `parent` mode →
> D4.2 worktree 状态机（含迁移）→ D2 三个事件模板 → D3 workpool → D4.1 eval 选项。
> worktree 语义变更单独提交、单独回归，不与模板接线混做。

## 与既有计划的关系

- 本 ADR **修订** `docs/agent-delegation-implementation-plan.md` §2.9 的 `subagent-*` 接线表：
  `subagent-role.md` 的 `render_subagent_role` 仍无生产调用点，不在本 ADR 补；三个事件模板
  的接线以 D2 为准。
- 本 ADR **扩展** ADR-20 P3a/P3b：task 系列新增独立 `scope: tool_description` 与
  `assemble_task_tool()`，不并入 request 装配；`workpool-batch.md` 仍是 request 模板；
  不另起一份并行 plan。
- 本 ADR **修订** ADR-15 的 worktree 自动 apply 语义（D4.2）；resident session 的生命周期
  不受影响。

## 验证

- `tests/tools/test_agent_yield.py`：keyed yield 的边界、重复 key、终态组装；
  `begin_turn()` 后第二批 keys 不复用第一批，schema 无过期 `maximum`。
- `tests/tools/test_eval_host_ops.py`：workpool batch 渲染、逐 item 结果、`agent()` 选项透传。
- `tests/agent/test_subagent_events.py`：async-pending drain（`timeout` 续等、`interrupted`
  steer 边界、redaction）、yield reminder 重试、预算模板。
- `tests/agent/test_completion_probe.py`：JSON/百分比解析、一次 probe 写入快照。
- `tests/tools/test_subagent_worktree.py`：`created → inspected →` 四态转换、父区脏拒绝
  apply、merge 冲突 abort、`exitCode=0` 但 `applied=false` 的契约。
- `tests/tools/test_subagent_worktree_apply.py`：`retained` / `patched` / `applied` /
  `conflicted` 的 payload 字段。
- `tests/agent/test_task_tool_template.py`：
  - **`task.md` 整文件 diff**：与固定快照 `tests/fixtures/prompts/omp-task.md`
    （omp `prompts/tools/task.md` 的逐字节副本）逐行比较，断言没有 `# Inputs`/`# Format`/
    `# Delegation` 被拆成独立文件；
  - **task 系列完整性**：每个 `task-*.md` 都登记、都可渲染，渲染后无 `{{`/`{%`；
  - **动态裁剪**：`assemble_task_tool("parent", ctx)` 在 async/batch/file/isolation 不同
    组合下只保留对应部分，顺序稳定。
- `tests/agent/test_task_tool_assembly.py`：
  - `AGENT_SCHEMA["description"] == ""`，运行时由 `assemble_task_tool()` 填充；
  - `parent` 的 part 顺序固定；`async`/`batch`/`file`/`isolation` 组合只保留对应部分；
  - `team`/`room` fail closed；definition 声明不同 mode 也 fail closed；
  - 每个 `task-*` 模板单独渲染成功，无 `{{`/`{%`；assembler 对未实现 mode 返回空即失败。
- `tests/agent/test_prompt_sections.py`：`scope: tool_description` 可被 `load_sections()`
  接受、被 `render_section()` 拒绝，且 `assemble_main()`/`assemble_request()` 不读取它；
  `kind`/`order`/`modes` 元数据校验。
- `tests/agent/test_agent_scheduling_mode.py`：默认 `parent`；`team`/`room` 选中即
  fail closed 且错误可读；mode 是会话级，不存在 per-call 参数。
- `tests/tools/test_agent_tool.py`：`AGENT_SCHEMA["description"] == ""`；会话构建时
  `assemble_task_tool()` 填充一次，工具描述与会话缓存前缀字节稳定；mid-session 设置
  变化不改变当前描述。
- `scripts/ci/prompt_surface.py`：新增 task 系列（`tool_description` 与 `request` 两组）
  的 surface 快照，以及 `parent`/`team`/`room` 三种 mode 的组装结果（后两者为 fail-closed
  错误，不是描述文本）。

## 非目标

- 不新增核心工具、不新增 `task` 别名。
- 不拆分 `task.md`：不生成 `# Inputs`/`# Format` 多文件，也不把条件分支迁到 Python。
- 不把 task 系列再拆成更细的片段；`task.md` 与每个 `task-*` 文件都是各自完整的契约。
- 不把 task 正文硬编码进 `AGENT_SCHEMA`；代码中描述为 `""`，只由 `assemble_task_tool()`
  从 manifest/模板返回。
- 不实现 `team`/`room` 的运行时行为；只预留名字，选中即 fail closed。
- 不提供 per-call `scheduling_mode`；agent definition 也不能覆盖
  `delegation.scheduling_mode`。
- 不恢复 `apply=auto`；`worktree: true` 不再隐式 merge。
- 不在 mid-session 替换工具描述；设置变化只对下一个会话生效。
- 不移植 omp `sourceSession`/`seance` 与 eval 自定义工具（`tools` 只映射到已注册工具）。
- 不改变系统提示的缓存边界；三个 subagent 事件模板仍然不进系统提示；task 系列的
  `tool_description` 部分只在会话工具面构建时渲染一次。
- 不复制 omp 的 `proc://`/`history://` URI；继续用 `process_manage`/`agent_out`。
- `apply`/`merge` **在范围内**（D4.2）；本次修订只改 ADR，不写实现代码。
