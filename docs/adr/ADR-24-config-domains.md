# ADR-24：配置域拆分（agent / subagents(agent 工具) / todos / ttsr）

**状态**：提议（2026-11-17，第六版）。**范围**：`config.yaml` 顶层配置域、dashboard
`/config` 的域顺序与下拉、`agent` 工具的 `subagents` 域与 `subagents.isolation` 子域。

**关联**：ADR-2 D11（todo 停止门提醒）、ADR-8（“配置域”= 顶层 `config.yaml` 段）、
ADR-16 D2（`sessions/<project-dir>` 路径编码）、ADR-18（delegation 工具策略章节）、
`hermes_cli/config_defaults_subagents.py`（新域的唯一数据源）、
`hermes_session_paths.encode_project_dir()`（项目路径归一化）。

**范围**：

- **做**：新增 `subagents` 顶层域，服务 **`agent` 工具**（它已完全替代 `delegate_task`）；
  把 `agent` 需要的行为键在 `subagents` 下新建（共享键复制默认值，不读 `delegation`）；
  新增 `subagents.isolation` 子域（`strategy` / `merge` / `cleanup` /
  `worktree_base_dir`）：调用方未传 `worktree` 时，`auto` 在 spawn 前用 judgment 决定
  isolation（`local`/`worktree`）、在 settle 前决定 merge，`local` 直接使用当前工作树，
  `worktree` 按配置的 merge/cleanup 执行；同步接线 agent 侧的六个共享键（并发、摘要上限、
  子运行 inactivity、压缩上限、one-shot spawn 预算、MCP toolset 继承）；新增 `todos` 顶层域；
  在 dashboard 配置页把 `subagents`/`todos`/`ttsr` 作为顶层域显示；
  `agent.unexpected_stop_detection` 变成下拉。
- **不做**：**不修改 `delegate_task`**。它的 `delegation.*` 配置、读取点、行为全部原样保留；
  不迁移、不删除、不重命名 `delegation.*`。会话 worktree（`hermes -w` / `/worktree new`）、
  kanban task workspace、GitHub PR checkout 的布局也不变。

---

## 问题

1. **`agent` 已替代 `delegate_task`，但配置还在旧的 `delegation` 域。** `agent` 工具
   （`tools/agent_tool.py`）是新的命名 agent/批量入口；`delegate_task` 是 legacy 委托工具，
   两者行为键高度重叠（预算、并发、提示策略、子代理 worktree、交付……），但 `agent`
   没有自己的配置域，用户只能去 `delegation` 找，且 `delegation` 的语义属于 legacy 工具。
2. `delegation.worktree_isolation` 是 `delegate_task` 子代理的开关，不是 `agent` 工具的
   默认策略。`agent` 的 `worktree`/`isolated` 只有 per-call 布尔，调用方不传时无法表达
   “自动隔离”。
3. todo 停止门开关藏在 `agent.todo_reminders*`，与 agent 生命周期/推理配置混在一起。
4. `ttsr` 已是顶层段，但 dashboard 配置页没有显式域顺序/图标；`agent.unexpected_stop_detection`
   只能手输，选项 `none|mechanical|smart` 不可见。
5. 新顶层段若不进入 schema/域顺序，就只会在 dashboard 的“其它”里按字母排序，用户看不到。

---

## 决策

### D1. `delegation` 与 `delegate_task` 完全冻结

本 ADR **不修改** `delegation` 的任何键、默认值、读取点或迁移：

- 路由：`backend`、`backends`、`model`、`provider`、`base_url`、`api_key`、`api_mode`、
  `request_overrides`、`reasoning_effort`、`fallback_providers`；
- `delegate_task` 行为：`bias`、`eager_tasks`、`eager_tasks_always`、`inline_first`、
  `spawn_directive`、`inherit_mcp_toolsets`、`max_iterations`、`max_summary_chars`、
  `child_timeout_seconds`、`max_concurrent_children`、`prefix_stagger_seconds`、
  `independent_completions`、`gateway_mode`、`max_spawn_depth`、`orchestrator_enabled`、
  `oneshot_max_children`、`subagent_auto_approve`、
  `surface_child_process_notifications`、`compression_threshold_tokens`、
  `worktree_isolation`。

`tools/delegate_tool_config.py`、`gateway/session_worker_child.py`、
`tools/process_registry.py`、`tools/delegate_tool*` 继续读 `delegation`，一个字符都不改。
`agent/prompts/delegation.py` 是共享的 tool-policy 渲染器：只有 legacy `delegate_task` 会话
读 `delegation`；暴露 `agent` 工具的会话按 D8 选 `subagents`。

### D2. 新增 `subagents` 顶层域，服务 `agent` 工具

`subagents` 是 `agent` 工具的完整配置域。**共享键在 `subagents` 下新建**：键名沿用，
默认值优先对齐 `agent` 工具的当前有效默认（见下），读取点只读 `subagents`；`delegation`
的副本保留给 `delegate_task`，两者互不影响。

```
subagents:
  # 提示策略（键名沿用 delegation，独立存储）
  bias: eager
  eager_tasks: false
  eager_tasks_always: false
  inline_first: true
  spawn_directive: true

  # 本 ADR 直接接线的运行默认
  max_iterations: 9999      # agent_run.MAX_ITERATIONS 同步改为 9999（不是 delegation 的 250）
  independent_completions: true   # agent 工具的当前默认
  max_spawn_depth: 3        # agent_tool.MAX_DEPTH（不是 delegation 的 1）
  max_concurrent_children: 10     # 每批 agent run 的并发门；默认 = agent_tool.MAX_BATCH_TASKS
  max_summary_chars: 2000         # completion summary 上限；默认 = agent_tool.MAX_NOTIFICATION_CHARS，0 = 不截断
  child_timeout_seconds: 0        # resident run 的额外 inactivity 上限（floor 30）；0 = 关闭
  compression_threshold_tokens: 0 # agent run 的 compaction 绝对上限；>=16000 才生效，0 = 关闭
  oneshot_max_children: 2         # one-shot 会话的 agent spawn 总预算；0 = 不限
  inherit_mcp_toolsets: true      # 收窄 toolsets 时保留父会话的 MCP toolsets；false = 严格交集

  # 只属于 agent 工具的隔离策略
  isolation:
    strategy: auto          # auto | local | worktree；auto = spawn 判 isolation、settle 判 merge
    merge: off              # off | auto；local 强制 off，worktree 默认 off
    cleanup: settle         # settle | keep；worktree 的清理时机
    worktree_base_dir: ""   # 空 = $HERMES_HOME/worktree/<project-dir>
```

**本 ADR 只创建有 reader 的键**。第四版已把 `max_concurrent_children`、`max_summary_chars`、
`child_timeout_seconds`、`compression_threshold_tokens`、`oneshot_max_children`、
`inherit_mcp_toolsets` 的 agent 侧 reader 一并接线（见下方 reader 列表与 D8）。下列
`delegation` 行为键与 agent 工具有概念重叠，但 agent 侧仍没有对应读取点；在 reader 落地前
不写进 `DEFAULT_CONFIG`，避免“能设置但什么都不做”的空转配置（root 的 `DEFAULT_CONFIG`
不变量）：

| 共享键 | 待接的 agent 侧 reader |
|---|---|
| `prefix_stagger_seconds` | agent 批量的 prompt-cache warm-up gate |
| `gateway_mode` | 网关会话的 inline/background 交付选择 |
| `orchestrator_enabled` | agent role 解析（当前 `agent` 工具没有该 role 概念） |
| `subagent_auto_approve` | agent 子运行的审批回调 |
| `surface_child_process_notifications` | agent 子进程通知路由 |

它们现在继续只存在于 `delegation`（供 `delegate_task` 使用）；将来接线时再在 `subagents`
下新增同名键，并补 reader 与测试。

规则：

- 这些键在 `subagents` 下是**新键**，不存在“迁移旧值”。默认值优先取 `agent` 工具
  **当前的有效默认**（`_KNOB_DEFAULTS` / `agent_run.MAX_ITERATIONS` / `agent_tool.MAX_DEPTH`）；
  `agent` 没有对应默认的键才复制 `delegation` 的同名默认值。例外是
  `max_iterations`：第四版把 `agent_run.MAX_ITERATIONS` 从 10 提高到 9999，`subagents`
  默认值随之是 9999，不是 `delegation` 的 250。`max_concurrent_children` 取 agent 的批上限
  `MAX_BATCH_TASKS`（10），`max_summary_chars` 取 agent 的 `MAX_NOTIFICATION_CHARS`（2000），
  都不复制 `delegation` 的 10 / 24000。用户显式设置
  `subagents.max_iterations` 才影响 `agent`；改 `delegation.max_iterations` 只影响
  `delegate_task`。
- 提示策略键（`bias`/`eager_tasks`/`eager_tasks_always`/`inline_first`）是共享
  tool-policy chapter 的输入，不是两套运行时。渲染时按本会话实际暴露的工具选源：暴露
  `agent` → `subagents`；只有 legacy `delegate_task` → `delegation`。同一份 prompt 不会
  同时读两个源，delegate_task-only 会话行为不变。
- `subagents` 不读 `delegation`，`delegation` 不读 `subagents`；没有 fallback、没有别名。
- 下列 reader 是**本 ADR 落地后的目标状态**；当前代码还没有 `subagents` 域，现有
  `spawn_directive` 仍读 `delegation`。实施时 reader 与测试必须一起落地。
- 已接线的 agent 侧 reader：`tools/agent_tool.py::_spawn_directive_enabled`（`spawn_directive`）、
  `_caller_knobs` / `_build_call`（`max_iterations`、`independent_completions`）、
  `_build_call` 的深度检查（`max_spawn_depth`）、`agent_run._attach_worktree`（`isolation.*`）、
  `agent_run` 构建 tool-policy 时的 `resolve_delegation_policy(..., section="subagents")`
  （四个提示策略键）。
- 第四版新增的六个 reader，全部只读 `subagents`：
  - `max_concurrent_children`：`tools/agent_tool.py::agent_tool` 的 `tasks[]` 批处理，语义是
    **per-batch gate**，不是 per-session 子代理槽位。计数器随一批的派发创建（批处理局部的
    `threading.BoundedSemaphore(N)`），在 `_execute_call` 提交前 acquire，在该 run 的
    `_settle` / `on_settle` 回调（含提交到池之前就失败）release；多出的任务留在本批等待，
    不丢弃。默认值 10 等于 `MAX_BATCH_TASKS`；`agent_loop.max_workers` 仍是独立的全局池上限，
    两者取小。单次 `agent` 调用是 1 任务的批，自然满足 floor 1。
  - `max_summary_chars`：只作用于 `tools/agent_tool.py::_summary` 生成的 completion summary
    正文（`_notify` 的 `summary` 字段），**不改** `_payload`（阻塞调用必须返回完整答案）。
    它取代模块常量 `MAX_NOTIFICATION_CHARS`（2000）成为新的天花板：默认 2000 保持现有行为，
    0 = 不截断；超限按现有 head 截断并保留 transcript 指针。agent 侧没有动态 headroom 预算
    （那是 `delegate_task` 独有）。
  - `child_timeout_seconds`：只给 **resident（`keep_alive`）agent run** 追加一条基于进展的
    inactivity 计时，读取点 `agent/agent_session_runtime.py`。它不覆盖
    `agent_loop.max_run_seconds`（1800s 硬墙钟），也不覆盖 `agent_sessions.idle_ttl_seconds`
    （1800s 空闲 park）：三个时钟独立，谁先触发谁生效；本键在每次进展（完成的工具调用、
    活动时钟 tick 等）时重置，不限制总时长。0 或负数关闭，`> 0` 取 floor 30。非 resident run
    仍只受 `agent_loop.max_run_seconds` 约束。
  - `compression_threshold_tokens`：由 `tools/agent_tool.py::_params` 作为 **per-call kwarg**
    传给 `agent_run.run`，`agent_run` 在 `on_agent` 拿到子 `AIAgent` 后调用现有
    `tools/delegate_tool.py::_apply_child_compression_cap(child, {"compression_threshold_tokens":
    value})`。不在 `agent_run.run` 内部读 `subagents`：该函数还被 resident runtime、
    plugins_service_ctx 和测试复用，内部读配置会改变非 `agent` 调用方。校验复用 delegate 的
    语义：>= 16000 才生效，其余值告警并忽略，0 = 关闭；主会话的
    `compression.threshold_tokens` 不受影响。
  - `oneshot_max_children`：`tools/agent_tool.py` 的 spawn 前置门，会话判定复用
    `agent/oneshot_footprint.is_single_query_session()`。计数器放在 `agent_tool` 自己的
    `_subagents_oneshot_children_spawned`（挂在调用会话上），**不复用** `delegate_task` 的
    `parent_agent._oneshot_children_spawned`，否则会改变 delegate 的预算算术与错误文案。
    超过时返回 tool_error 并点名 `subagents.oneshot_max_children`；0 = 不限。
  - `inherit_mcp_toolsets`：读取点是 agent toolset 解析，但 handler 现在没有父 agent：
    `agent_tool(args, *, session_id, task_id, **_)`，`model_tools._execute_tool` 的
    `dispatch_kwargs` 只给 `execute_code` 带 `enabled_tools`。实施时必须先把调用方的
    `enabled_toolsets` 转发进 `agent` handler（扩展 `_execute_tool` 的 `agent` 分支，或绑一个
    turn ContextVar），然后在该解析里 `true` 追加父会话的 MCP toolsets、`false` 保持严格
    交集。**不能**直接复用 `tools/delegate_tool_toolsets._resolve_child_toolsets`：它调用
    `_get_inherit_mcp_toolsets()`（读 `delegation`）。要么新增 agent 专用解析，要么给 helper
    加 `section` 参数且 delegate 调用默认仍是 `delegation`，保证 `delegate_task` 不受影响。
- `tools/agent_tool.py::_KNOB_DEFAULTS` 仍是 per-call / definition 的调用契约
  （`background`、`skip_memory`、`skip_context_files`、`skip_background_review`、
  `load_soul_identity`、`enable_tools`、`max_iterations`、`reasoning_effort`、`worktree`、
  `toolsets`、`model`）。`subagents.*` 只提供**全局默认**，调用方参数 / definition 仍优先；
  `background`、`skip_*`、`enable_tools`、`toolsets`、`model` 没有全局配置覆盖。
- 默认值由 `hermes_cli/config_defaults_subagents.py` 提供，`DEFAULT_CONFIG` 只组合。

### D3. 新增 `todos` 顶层域

`agent.todo_reminders` → `todos.reminders`，`agent.todo_reminders_max` → `todos.reminders_max`。
读取点（`agent/todo_reminder.py`）只读 `todos`；旧键由 D6 的 canonicalize/迁移搬走。
该域刻意保持小：todo 计划本身是会话状态，不是配置。

### D4. `subagents.isolation`：只负责 `agent` 工具的隔离、合并与清理策略

字段：

```
subagents.isolation:
  strategy: auto        # auto | local | worktree
  merge: off            # off | auto（auto = 提交子改动并合回父工作树）
  cleanup: settle       # settle | keep
  worktree_base_dir: "" # 空 = $HERMES_HOME/worktree/<project-dir>
```

- `subagents.isolation.strategy`：
  - `auto`：**调用方没有传 `worktree` / `isolated` 时**，spawn 前用一次 typed judgment 决定
    isolation（`local` / `worktree`）；选中 worktree 后，在 settle 前再判一次 merge
    （`off` / `auto`）。两个判决点见下。terminal backend 不是 local 或不在 git work tree
    内时直接 `local`，不调用 judge。
  - `local`：直接在当前工作树运行，不创建 worktree；merge 策略强制为空（`off`）。
  - `worktree`：可用时强制每个 agent run 使用独立 worktree；不可用（非 git / 非 local）时
    按现有语义降级为 `local` 并在结果里标注 requested/created。merge 取
    `subagents.isolation.merge`，默认 `off`（不自动合回）。
- `subagents.isolation.merge`：`worktree` 策略（以及调用方/definition 显式要求 worktree）的
  merge 策略；`auto` 策略下由 judge 决定，本键不参与。`off`（空/不合并）= 留下 branch 供
  人工 review（现有 `finalize_subagent_worktree(apply="off")`）；`auto` = 提交子改动并在父
  工作树干净时 `git merge --no-ff`（现有 `apply="auto"`）。local 强制 `off`。
  `finalize_subagent_worktree` 的 `apply` 仍只有 `off | auto`，本 ADR 不新增值。
- `subagents.isolation.cleanup`：worktree 的清理时机。
  - `settle`（默认）：run 结束即按现有规则处理——无 commit 且干净 → 删除；有 commit/改动 →
    archive 到 `refs/hermes/archive/<tree>` 后删除；探测失败 → 保留并标注。
  - `keep`：run 结束只做探测并返回 `worktree` payload，不删除、不 archive；树与 branch 留给
    `hermes worktree` / 后续 sweep / 人工处理。`cleanup` 对 `local` 是 no-op。
  - 本 ADR 不新增 TTL 清理：agent 基目录还不在 `worktree_gc`/`worktree_sweep` 的扫描根内
    （D5），在专用 sweep 落地前只提供 `settle | keep`，不假装有后台回收。
- `subagents.isolation.worktree_base_dir`：agent worktree 的基目录。
  - 默认空串 = `$HERMES_HOME/worktree/<project-dir>`。
  - `<project-dir>` 复用 `hermes_session_paths.encode_project_dir()`，与
    `$HERMES_HOME/sessions/<project-dir>` 同一套编码（home/temp 相对前缀、绝对路径双横线、
    120 字符 + `sha256[:8]` 截断）。不新增第二套路径编码。
  - 显式值必须是绝对路径（支持 `~` 与 `${VAR}`；变量由配置加载器展开）。相对路径拒绝并回退
    默认，否则创建方与清理方会按各自 cwd 解析出不同目录（omp `worktree.base` 的同一约束）。

**`auto` 的 judgment 契约**（复用 ADR-17 的 judgment 服务，不新增第二套判决通道）。

问题文件 `agent/prompts/judge/subagents-isolation.json`（`load_questions()` 读取），每次
`JudgmentRequest` 只带当前阶段需要的问题。两个判决点：

1. **spawn 前：isolation**（purpose `subagents.isolation`）
   - 问题 `isolation`：`ChoiceQuestion`，标签 `local` / `worktree`。
   - `state`：`{"agent": <name>, "task": <prompt 前 4000 字符>, "toolsets": [...],
     "all_tools": <bool>}`；不含 secrets。
   - 可用性门：只有 `resolve_judge()` 能给出候选时调用（`judgment.mode` ≠ `off`，链/原生
     路由可用）；否则直接走确定性回退，不发起请求。
   - 选择：概率最高的标签；平局/缺答案 → `local`。`local` 时不再有 merge 判决。
   - 失败回退（`JudgmentUnavailable` / timeout / budget / parse error / 无路由）：local + git
     可用 → `worktree` 且 merge 视为 `off`；否则 `local`；warn 一次。绝不因服务不可用把两个
     并行写者放进同一工作树。
2. **settle 前：merge**（只在该 run 建了树且策略是 `auto` 时；purpose
   `subagents.isolation.merge`）
   - 问题 `merge`：`ChoiceQuestion`，标签 `off` / `auto`。
   - `state`：`{"agent", "task": <prompt 前 4000 字符>, "commits": <int>, "dirty": <bool>,
     "diffstat": <有界的前 4000 字符>, "parent_clean": <bool>}`；来自 finalize 的现有探测，
     judge 不重新跑 git。spawn 时还没有 commit/diff，所以 merge 不能在建树前判。
   - 选择：概率最高的标签；平局/缺答案/服务不可用 → `off`（默认不合并）。
   - `auto` 只表示“尝试 `finalize_subagent_worktree(apply="auto")`”；父工作树脏、commit
     失败或冲突时仍按现有逻辑保留 branch 并返回 `applied=false` / `apply_error`。
- batch 里的 spawn 判决不逐个串行调用 `judge`：`tasks[]` 的 isolation 问题用现有
  `agent.judgment.judge_batch`（每项一个 `BatchItem`，只带 `isolation` 问题，`min_ok=0`），
  `max_concurrency` 用默认 4。最坏等待是 `ceil(N/4) × judgment.timeout_ms`（10 个任务约
  3 个超时窗口，超时不重试），而不是 `N × timeout`；单项失败只回退该项的确定性判定，
  不影响整批。
- 成本/缓存：沿用 `judgment.cost_gate`（每次 0.05 USD / 每 session 0.50 USD）、
  `judgment.timeout_ms`、native answer cache；缓存键是问题 + 有界 state。merge 只在真正建树
  的 run 上发生，所以一次 `auto` worktree run 最多两次判决（isolation + merge），`local`
  run 一次。spawn 判决在入池前完成，`background` 任务不例外；merge 判决在 settle 的 worker
  线程内完成，只推迟结果落账，不占用 tool call 的等待时间。
- 结果落在 run payload：`isolation_source`（`caller|definition|judge|config`）、
  `isolation_decision`（`local|worktree`）、`merge_source`（`judge|config`）、
  `merge_decision`（`off|auto`）；wake/revive 不重判。judge 只是策略选择，不替代
  `_apply_worktree` 的 `parent_clean` / 冲突保护。

优先级（谁决定一次 `agent` run 是否隔离）：

1. 调用方显式参数 `worktree: true|false`（`isolated` 是它的别名；`false` = `local`）；
2. agent definition 的 `worktree`；
3. `subagents.isolation.strategy`（`auto` 再做上面的 judgment）。

merge/cleanup 的优先级：调用方显式 worktree 参数或 definition 决定“是否隔离”，不决定
merge；`auto` 策略的 merge 由 judge 决定，`local` 强制 `off`，其余取
`subagents.isolation.merge`。`cleanup` 始终取配置。

`delegate_task` 不读本子域；它继续用 `delegation.worktree_isolation`（D1），child worktree
也没有 merge/cleanup 新语义。

实现注意：`tools/agent_tool.py::_caller_knobs` 已经用 `_flag` 产出 `None`（参数缺省），
但 `_build_call` 目前把 `knobs["worktree"]` 直接 `bool()` 掉。必须保留三态：`True`/`False`
是调用方显式决定，`None` 才进入 definition → `strategy` 的判定；`strategy=auto` 的可用性
测试与现有 `_attach_worktree` 一致（git work tree + local backend），不可用时走现有的
requested/created 降级 payload，不报错。`agent/agent_run.py::_attach_worktree` 需要接收
`cleanup`；merge 不在建树时决定：`strategy=auto` 的 worktree run 在 settle 前做一次 merge
判决，其余取 `subagents.isolation.merge`，再以 `apply="off"|"auto"` 传给
`finalize_subagent_worktree`；`prune=False` 对应 `cleanup=keep`。

### D5. `agent` 工具树的适用范围与清理

适用范围只有：

- `tools/agent_tool.py` 的 `worktree`/`isolated` → `agent/agent_run.py::_attach_worktree`
  → `tools/subagent_worktree.py`。

`delegate_task` 的 child worktree（`delegation.worktree_isolation`）**不走**
`subagents.isolation.worktree_base_dir`；它继续落在 `<repo>/.worktrees/subagent-<id>`，
清理/归档语义不变。共享的 `tools/subagent_worktree.create_subagent_worktree` 必须接受显式
基目录参数，由两个调用方分别传入，避免 agent 工具的新基目录意外接管 delegate 子代理。

不会改变：

- `hermes -w` / `/worktree new` 的 `<repo>/.worktrees/` 布局（kanban canonical workspace、
  TUI resume 信任检查、`worktree_gc`/`worktree_sweep` 仍以它为根）；
- kanban task workspace（`kanban_db_workspace.py`）与 `HERMES_KANBAN_WORKSPACES_ROOT`；
- GitHub PR checkout 的独立根（PR review 需要时由对应流程创建）。

清理：

- `finalize_subagent_worktree` 仍是 agent 树的唯一清理执行者。`cleanup: settle` 时按现有
  规则：无改动且干净 → 删除；有工作 → archive 到 `refs/hermes/archive/<tree>` 后删除；
  探测失败 → 保留并标注。`cleanup: keep` 时只探测并返回 payload，不删除、不 archive。
- merge 由 `subagents.isolation.merge`（或 `auto` 策略下 judge 的 `merge`）以
  `apply="off"|"auto"` 传给同一个 `finalize_subagent_worktree`；`parent_clean`、commit 失败
  与冲突保护不变。
- agent 树在 `$HERMES_HOME/worktree/<project-dir>/` 下，**不在** `worktree_gc` /
  `worktree_sweep` 的 `<repo>/.worktrees` 扫描根内；`cleanup: keep` 留下的树与崩溃遗留
  都不会自动回收。这是本 ADR 的已知边界：后续要么给 agent 基目录加专用 sweep，要么把该
  基目录纳入统一 resolver；本次不假装 `worktree_gc` 已经覆盖它，也不提供 TTL 清理。
- 仅当基目录落在仓库内时才写 `.git/info/exclude`；默认在仓库外，不修改仓库忽略文件。

### D6. 迁移与读取兼容

1. `_config_version` 53 → 54；`config_migrations_subagents.migrate_to_54` 只迁移 todo：
   - `agent.todo_reminders` → `todos.reminders`；
   - `agent.todo_reminders_max` → `todos.reminders_max`；
   - 仅在目标键**不存在**时搬（用户显式的新键优先）；从 `agent` 删除旧键；
   - 该步骤加入 `LEGACY_KEY_STEPS`（未打版本戳的 config 也迁移）。
2. `config_canon.normalize_todos_config` 在**用户层**合并前做同样的提升；这样未跑迁移的
   config（只读加载、远程后端、`--ignore-user-config` 之外的边界）也只有一个读取路径；
   迁移只负责把结果写回磁盘。
3. `subagents.*` 全是新键，深合并默认值即可，**没有**从 `delegation` 的迁移；旧
   `delegation.*` 原样保留（包括 `spawn_directive`、`worktree_isolation`）。
4. 迁移只持久化“与默认值不同”的值（`_persist_migration` 既有约束）。
5. `delegation` 的键不会消失；`hermes config get delegation.max_iterations` 仍返回
   `delegate_task` 的值。想改 `agent` 工具的行为，必须写 `subagents.<key>`。

### D7. Dashboard 配置页的顶层域

`/api/config/schema` 从 `DEFAULT_CONFIG` 生成字段，`category` 取首段路径。
`_CATEGORY_ORDER` 增加 `subagents`、`todos`、`ttsr`，`ConfigPage` 增加对应 lucide 图标与
i18n 标签。`subagents.isolation.*` 是 `subagents` 域内的字段，不单独成为一个顶层 tab：
不把 `isolation` 加进 `_CATEGORY_ORDER`/`CATEGORY_ICONS`/i18n `categories` 记录。
`_SCHEMA_OVERRIDES` 增加：

- `agent.unexpected_stop_detection`: `none | mechanical | smart`
- `subagents.isolation.strategy`: `auto | local | worktree`
- `subagents.isolation.merge`: `off | auto`
- `subagents.isolation.cleanup`: `settle | keep`
- `ttsr.judge`: `auto | on | off`
- `ttsr.interrupt_mode`: `always | prose-only | tool-only | never`
- `ttsr.repeat_mode`: `once | after-gap`

### D8. 读取点：agent 读 subagents，delegate_task 读 delegation

- `agent` 工具：本 ADR 已接线的 `subagents.*`（`spawn_directive`、`max_iterations`、
  `independent_completions`、`max_spawn_depth`、`max_concurrent_children`、
  `max_summary_chars`、`child_timeout_seconds`、`compression_threshold_tokens`、
  `oneshot_max_children`、`inherit_mcp_toolsets`、`isolation.*`、四个提示策略键），reader 见
  D2；未接线的共享键不创建、不读取。`agent/agent_run.py` / `tools/subagent_worktree.py`
  负责 isolation 策略、judgment 调用、merge 与 cleanup 时机。
- tool-policy 渲染：`agent/prompts/delegation.py` 按会话暴露的工具选源——
  有 `agent` 工具读 `subagents`，只有 legacy `delegate_task` 读 `delegation`。
- todo：`agent/todo_reminder.py` 读 `todos`。
- `delegate_task`：`tools/delegate_tool_config.py`、`gateway/session_worker_child.py`、
  `tools/process_registry.py`、`tools/delegate_tool*` 继续读 `delegation`，一个字符都不改。

唯一兼容层是 D6 的 todo canonicalize/迁移；`subagents` 与 `delegation` 之间没有 fallback。

---

## 后果

- `delegate_task` 完全不受影响：配置、读取点、默认值、行为、worktree 布局都不变。
- `subagents` 成为 `agent` 工具的唯一配置域；本 ADR 只创建已接线的键与 isolation 子域，
  其余共享键等 agent 侧 reader 落地后再加，避免空转配置。
- `agent` 工具的默认预算从 10 次迭代提高到 9999（`agent_run.MAX_ITERATIONS`），长任务不再
  因默认预算过小而中断；`delegation.max_iterations` 保持 250，`delegate_task` 行为不变。
- 新增的六个 reader 全部读 `subagents`，`delegation` 的同名键继续只服务 `delegate_task`；
  例如 `subagents.oneshot_max_children: 0` 只放开 `agent` 工具的 one-shot 预算，
  `delegation.oneshot_max_children: 2` 仍约束 `delegate_task`。
- 已接线的同名键在 `delegation` 与 `subagents` 各有一份，改一个不影响另一个；这是“agent
  已替代 delegate_task”期间的明确取舍，避免 `delegate_task` 任何改动。
- `agent` 工具在调用方未传 `worktree`/`isolated` 且 `strategy: auto` 时，spawn 前由 judgment
  服务按任务决定 `local` / `worktree`，建树后在 settle 前再判 `off` / `auto` merge；judgment
  不可用时退回 local + git 的确定性判定（建树、不合并）。每次 run 最多两次判决，native
  缓存可复用。
- `worktree` 策略与显式 `worktree: true` 的默认 merge 是 `off`：run 结束不再自动把 branch
  合回父工作树（此前 `agent_run` 固定 `apply="auto"`）；需要自动合并必须显式
  `subagents.isolation.merge: auto`。`cleanup: keep` 可把树留给人工/后续 sweep。
- `local` 取代旧的 `none`：直接复用当前工作树，不创建任何 tree；`local` 的 merge 固定
  `off`，cleanup 是 no-op。
- agent 树默认落在 `$HERMES_HOME/worktree/<project-dir>/`；会话 worktree、kanban task
  workspace、PR checkout、`delegate_task` 子代理树的布局不变。
- agent 树的崩溃遗留不在 `worktree_gc`/`worktree_sweep` 的扫描根内，需专用 sweep 或人工
  清理；本 ADR 明确不假装已覆盖（D5）。

---

## 验证

- `DEFAULT_CONFIG` 有顶层 `subagents`（已接线的共享键 + `isolation.strategy` /
  `isolation.merge` / `isolation.cleanup` / `isolation.worktree_base_dir`）、`todos`、`ttsr`；
  没有顶层 `isolation`；未接线的共享键不在 `DEFAULT_CONFIG`。
- `DEFAULT_CONFIG["delegation"]` **逐键不变**（`spawn_directive`、`max_iterations`、
  `worktree_isolation` 等全部保留），`delegate_task` 仍读旧域。
- 设 `delegation.max_iterations: 50`：`delegate_task` 用 50，`agent` 仍用
  `subagents.max_iterations`（默认 9999，对齐 `agent_run.MAX_ITERATIONS`）。设
  `subagents.max_iterations: 50` 只影响 `agent`。
- 六个新接线键各有独立读取测试：`subagents.max_concurrent_children: 2` 时 4 个 `tasks[]`
  同时最多 2 个在跑，其余排队，且 `agent_loop.max_workers` 仍是独立上限；
  `subagents.max_summary_chars: 100` 时 completion summary 被截断且保留 transcript 指针，
  默认 2000 与现状逐字一致、`_payload` 的阻塞答案不截断；`subagents.child_timeout_seconds:
  60` 时停止进展的 resident run 以 inactivity 失败、持续推进的 run 不被误杀，且
  `agent_loop.max_run_seconds` / `agent_sessions.idle_ttl_seconds` 的既有测试不变；
  `subagents.compression_threshold_tokens: 200000` 时 agent run 的 compaction 触发点取该值与
  ratio 阈值的较低者，非 `agent` 调用方的 compaction 不变；one-shot 会话里
  `subagents.oneshot_max_children: 1` 时第二次 `agent` spawn 返回点名该键的 tool_error，
  `delegate_task` 的 `_oneshot_children_spawned` 计数与错误文案不变；
  `subagents.inherit_mcp_toolsets: false` 时收窄 toolsets 的 agent run 不继承父 MCP toolsets，
  而 `delegate_task` 的 `_resolve_child_toolsets` 仍只读 `delegation.inherit_mcp_toolsets`。
- 以上六个键不存在时 `DEFAULT_CONFIG` 给出 10 / 2000 / 0 / 0 / 2 / true；它们不出现在
  `delegation` 读取路径上，`delegation` 同名键的既有测试不变。
- 原始 config 写 `agent.todo_reminders: false`：`load_config()` 读到
  `todos.reminders: false`；`migrate_config()` 写回后旧键消失。
- `delegation.worktree_isolation: true` 仍创建 `delegate_task` 子代理树，基目录仍是
  `<repo>/.worktrees`；`subagents.isolation.worktree_base_dir` 不影响它。
- `CONFIG_SCHEMA["agent.unexpected_stop_detection"]["options"] == ["none","mechanical","smart"]`；
  `CONFIG_SCHEMA["subagents.isolation.strategy"]["options"] == ["auto","local","worktree"]`；
  `CONFIG_SCHEMA["subagents.isolation.merge"]["options"] == ["off","auto"]`；
  `CONFIG_SCHEMA["subagents.isolation.cleanup"]["options"] == ["settle","keep"]`；
  `/api/config/schema` 的 `category_order` 含 `subagents`、`todos`、`ttsr`。
- `strategy: auto`（judgment 被 mock）：
  - spawn 前的 `purpose="subagents.isolation"` 请求只带 `isolation` 问题；settle 前的
    `purpose="subagents.isolation.merge"` 请求只带 `merge` 问题，state 含 commits/dirty/
    diffstat/parent_clean。
  - `isolation=worktree, merge=auto` → 建树并在 settle 时提交 + 合回父工作树；
  - `isolation=local` → 不建树，也不发 merge 判决（每个 local run 只有一次判决）；
  - 平局/缺答案 → spawn 阶段 `local`；merge 阶段 `off`；
  - isolation 判决不可用/timeout/budget/parse error/无路由 → local + git 下建树且
    merge=`off`，非 local/非 git 下 `local`，且不重复调用 judge；
  - merge 判决不可用/超预算 → `off`（保留 branch，不合并）；
  - 非 local 或非 git：完全不调用 judge。
- `tasks[]` 批量：N 个任务的 isolation 只发一次 `judge_batch`（`max_concurrency=4`,
  `min_ok=0`），tool call 的阻塞等待不超过 `ceil(N/4) × judgment.timeout_ms`；单个 item
  失败只让该 task 回退确定性判定，`local` 的 task 不发 merge 判决。
- `strategy: worktree`：`merge` 缺失/`off` 时 run 结束保留 branch（合并不发生）；
  `merge: auto` 且父工作树干净时产生 merge commit；`cleanup: keep` 时树与 branch 保留、
  `pruned=false`，`cleanup: settle` 恢复现有的 prune/archive 规则。
- `strategy: local`：不创建 worktree，`isolation_decision=local`，merge/cleanup 不生效。
- 调用方 `worktree: true/false` 与 definition 的 `worktree` 仍覆盖 `strategy`，并跳过
  judgment（不产生 judgment 调用）；`auto` 的判决只发生在这两者都缺省时。
- agent worktree 默认路径等于
  `get_hermes_home() / "worktree" / encode_project_dir(repo)`；显式
  `subagents.isolation.worktree_base_dir`（绝对路径）生效，相对路径被拒绝并回退默认。
- 会话 worktree、kanban task workspace、PR checkout、`delegate_task` 子代理的路径与清理
  行为不变。
- 目标测试：`tests/tools/test_agent_tool.py`、`tests/tools/test_agent_session_runtime.py`、
  `tests/tools/test_subagent_worktree.py`、`tests/tools/test_subagent_worktree_apply.py`、
  `tests/agent/test_judgment_service.py`、`tests/agent/test_oneshot_footprint.py`、
  `tests/agent/test_todo_reminder_stop_gate.py`、`tests/hermes_cli/test_config*.py`。
