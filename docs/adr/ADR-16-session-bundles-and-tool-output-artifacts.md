# ADR-16：会话 bundle、工具输出归档与 advisor 命名收口

**状态**：已决定（2026-11-05）；P0–P5 已实现。分阶段 **P0–P5**：P0 路径与配置契约；
P1 工具输出归档包装层；P2 subagent 产物与 `agent_out`；P3 手动镜像与 DB 缩减；P4 omp-ui
选择器；P5 统一 `advisor` 命名。

**关联**：ADR-5（recap / compaction；压缩是 rotate 还是 rewrite 的既有语义）、ADR-12
（omp agent workflows；`read-summarize` 的上游来源）、ADR-14（Room 报告模板与 artifact
约定）、ADR-15（命名 agent 会话运行时；`agent_session` 行、subject、transcript 路径）。
本 ADR **不取代**上述任何一项；只新增「会话落盘布局」「工具输出归档」「advisor 命名」三条
边界。

**取证口径**：Hermes 侧引用本仓库当前工作树（2026-11-05）。omp 侧以本地 checkout
`C:\Users\zarag\projects\oh-my-pi`（HEAD `6a86df85`，`@oh-my-pi/pi-coding-agent` 18.14.0）
为准；`omp-ui/node_modules` 只作为客户端已安装行为证据，不当作上游源码。

---

## 问题

### 1. `sessions/` 是混合目录，主会话没有文件形态

当前 `state.db` 是唯一权威（`CONTRIBUTING.md:329`）：主会话 transcript 只在 `messages`
表；`sessions/` 里同时住着 gateway 路由索引 `sessions.json`
（`hermes_cli/config_defaults.py:2055` `write_sessions_json`）、请求面包屑
（`agent/agent_init.py:1142` 把 `agent.logs_dir` 指向 `sessions/`）、DB 被替换时的 divert
JSONL（`hermes_state.py:441`，`sessions/<id>.jsonl`）、命名 agent 的
`sessions/<agent>/<agent_session_id>.jsonl`（`agent/run_transcript.py:65-68`）与
`.summaries/`（`:71-74`）。

UI、审计、跨进程读取都只能走 DB 或旧 agent 目录；没有「一次会话一个自包含 bundle」的
形态，也没有可供 omp-ui transcript selector 直接读取的稳定文件。

### 2. 截断散落在各工具，原始内容在截断点之后消失

`tool_output` 只有三个键（`hermes_cli/config_defaults.py:551`），实际只覆盖 terminal 与
`read_file`；`execute_code`、web、MCP 各自 head/tail 截断；通用层
`tools/tool_result_storage.py::maybe_persist_tool_result` 以阈值把超大结果溢写到
`cache/spillover/<id>.txt`，并在 `agent/tool_executor.py::_commit_tool_result` 里替换成
preview + 路径。结果是：

- 模型看到的是截断后的文本；原始文本只在工具内部短暂存在；
- 溢写位置是 cache，不是会话记录；
- `cache/spillover` 的 GC 依赖 state.db 消息里的 `spillover/` 引用
  （`tool_result_storage.py:101-160`），换位置会同时破坏远程可读性与 GC。

### 3. subagent 只有 transcript，没有报告与结构摘要

`agent_session` 行已有 `subject`、`parent_session_id`、`transcript_path`
（`hermes_state_agent_sessions.py`），但 `artifact_manifest` 从未写入
（`tools/agent_tool.py:560-562` 只写 `final_response` / `transcript_path`）。因此
`agent_out(mode="artifact")` 读的是一个永远为 `null` 的列；`summary` 模式读的是
`sessions/<agent>/<session>.summaries/index.json` 的 tree-sitter/AST 读摘要
（`tools/read_summary.py:11-14`），不是工作报告。

### 4. advisor 命名与落盘位置不一致

代码、配置与上游 omp 统一使用 `advisor`。改名前的 transcript 落在会话 bundle 之外
（`agent/advisor_recorder.py:65-67` 的旧实现），配置键、模块名、`agent.advisor` 属性、
UI 卡片 kind 曾分散在 65 个 `.py` 与 48 个文档/TS/YAML 文件中。

### 5. omp-ui selector 只认 DB

`SessionSelectorComponent` 的数据来自 `session.list` RPC
（`omp-ui/src/modes/controllers/selector-controller.ts:702-717`、
`omp-ui/src/modes/rpc/gateway-session.ts:884`），没有文件入口。文件布局落地后，UI 仍看不到
JSONL，也无法在远程 gateway 上直接读宿主文件系统。

---

## 决策

### D1：state.db 仍是权威；JSONL 是手动/按需的冷镜像

- **正常 turn 不写主会话 JSONL。** 主会话 transcript 的持续写入、压缩、rewind、branch、
  FTS 全部继续由 state.db 承担。这样避免把 rotate / rewrite 的一致性负担带进 live 路径。
- **镜像由手动命令产生**：`hermes sessions mirror` 以只读连接扫描 state.db，按会话导出
  bundle。镜像不是第二写路径，失败不影响 turn。
- **唯一在 live 路径落盘的是“执行时才会存在”的信息**：主会话的工具输出原文
  （`-original.log`）、subagent transcript 与报告/结构摘要、advisor transcript。DB 只有
  截断后的工具结果；历史会话无法回填 `-original.log`，这一点在 D3 明确写出。
- **缩减 state.db** 走「镜像 → 校验 → 可选 prune → 可 restore」：
  1. 导出并写入 manifest（session id、文件、sha256、bytes、message_count）；
  2. `--prune-source` 仅在文件校验通过、会话已结束且无 live lease 时删除 `messages` 行
     （FTS 随触发器清理），保留 `sessions` 元数据行与 `mirror_path` / `mirrored_at` /
     `offloaded`；
  3. `hermes sessions restore <id>` 从 bundle 重新导入，恢复可 resume 状态。
- **边界**：prune 之后，该会话 messages 的唯一副本是 JSONL；DB 仍是所有未 prune 会话与
  全部元数据的权威。ADR 用「DB 权威 + 冷镜像」而不是「JSONL 取代 DB」。

### D2：bundle 布局与路径推导

```text
$HERMES_HOME/sessions/
├─ sessions.json                         # 既有路由索引，保留
├─ request_dump_*.json                   # 既有调试面包屑，保留
├─ .mirror/<profile>.jsonl               # 镜像 manifest（新增）
└─ <project-dir>/
   ├─ <timestamp>_<session-id>.jsonl     # 主会话冷镜像
   └─ <timestamp>_<session-id>/          # 同名目录：artifact / subagent / advisor
      ├─ 1.terminal.log                  # 主会话工具归档（per-session 序号）
      ├─ 1.terminal-original.log         # 仅截断时
      ├─ 1.terminal.json                 # 可选：结构化 sidecar
      ├─ <topic>.jsonl
      ├─ <topic>.md
      ├─ <topic>_ast.json
      ├─ <topic>.json                    # 可选：结构化输出 sidecar
      ├─ <topic>/                        # subagent 自己的命名空间
      │  ├─ 1.read_file.log              # subagent 工具归档（它自己的 per-session 序号）
      │  ├─ 1.read_file-original.log     # 仅截断时
      │  └─ _advisor.jsonl               # subagent 的 advisor（可选）
      └─ _advisor.jsonl                  # 主会话 advisor（bundle 根）
```

**project-dir** 采用 omp 的编码规则（`packages/coding-agent/src/session/session-paths.ts`）：

| cwd 位置 | 目录名 |
|---|---|
| home 内 | `-` + home 相对路径（`/`、`\`、`:` 均替换为 `-`） |
| temp root 内（优先于 home，Windows 的 `%USERPROFILE%\AppData\Local\Temp` 嵌在 home 下） | `-tmp-` + temp 相对路径（同样替换分隔符） |
| 其它绝对路径 | `--` + 绝对路径（去掉前导分隔符，`/`、`\`、`:` 替换为 `-`）+ `--` |

- **时间戳**：`started_at` 的 ISO 串，`:` 与 `.` 替换为 `-`（omp 的 `fileSafeTimestamp`），
  再以 `_` 连接 session id。用户口中的「当前时间+会话id」里的 `+` 是连接之意，落盘用 `_`。
- **权威映射**：导出时把实际路径写入 session 元数据（`mirror_path`），读侧不靠重新推导；
  推导只用于新建 bundle 与 live artifact。
- **Windows**：组件长度上限 120，碰撞追加 session id 的 8 位 hash；比较时大小写不敏感。
- **保留名**：`sessions.json`、`request_dump_*`、`_advisor*`、`.mirror` 不与 project-dir
  冲突；project 目录名不得以 `.` 开头。

### D3：工具输出归档（live 包装层）

**触发点**：`agent/tool_executor.py::_commit_tool_result`。它在 guardrail observation、
file-mutation verifier 之后，在 tool message 入库之前，且持有 session / tool_call_id /
tool_name。`handle_function_call` 的出口不作为主接缝，因为它也被 execute_code / RPC 调用，
可能没有会话上下文；无上下文的调用只记录 debug 原因，不归档。

**配置**（继续放在 `tool_output` 下）：

```yaml
tool_output:
  max_bytes: 50000            # 既有：head/tail 预算，包装层对无专属 cap 的文本结果复用
  max_lines: 2000             # 既有：read_file 分页
  max_line_length: 2000       # 既有：read_file 行宽
  artifacts:
    enabled: true
    redact: true              # 写盘前强制脱敏
    exclude_tools: ["read_file", "todo", "agent_out"]  # 契约型/已自限的结果不二次截断
```

**文件语义**：

- `<tool-seq>.<sanitized-tool>.log`：**每次有结果的工具调用**都写，内容是最终进入模型
  上下文的文本（persisted result，已截断）。
- `<tool-seq>.<sanitized-tool>-original.log`：**仅当包装层实际截断时**写，内容是截断前、
  已脱敏的原文。
- 工具名清洗沿用 omp：`[^A-Za-z0-9_-]` → `_`，最多 64 字符，空则 `tool`。
- 写入原子化：临时文件 + rename；序号单调、永不复用。

**原始内容怎么拿到**：

- 工具内部不得先截断再交给包装层，否则 `-original.log` 是假原文。对 terminal /
  execute_code / web / MCP 的 head-tail 截断上移；terminal 的 `bounded_capture` 保留
  （防 OOM），在 drain 时把完整流 tee 到 `-original.log`，返回值仍是 head/tail。
- `-original.log` 与 `.log` 的分界必须是「包装层截断前 / 后」，不是「工具截断前 / 后」。

**模型可见指针不变**：

- 截断结果返回的恢复路径仍是 `cache/spillover/<id>.txt`（或工具既有路径）。
  `tools/credential_files.py:248-256` 只把 `cache/*` 纳入挂载/同步表；
  `_sandbox_visible_spillover_path`（`tool_result_storage.py:225-246`）依赖它做远程翻译与
  可读性探测。指向 `sessions/` 会让 Docker/SSH/云 sandbox 读不到。
- 会话目录只作为 **host 侧归档**，路径通过 JSON 字段 `artifact_path` 暴露给 UI /
  `agent_out`，不作为模型恢复路径。不把整个 `sessions/` 挂进 sandbox（会暴露其他会话）。
- `cache/spillover` 的 GC 保持原样；会话归档随 session prune/delete 清理，不按 mtime 单独
  建一套 GC。

**返回结构**：

- 纯文本：head + 既有 `truncation_notice` + tail + `[full output: <spillover path>]`，
  保持 `hermes.tool_output_truncation.count` 的 marker 兼容
  （`hermes_cli/observability/shared_metrics_efficiency.py` 的正则）。
- JSON：不得从中间切断。能安全截断的对象追加
  `"_truncated": {"original_path": ..., "bytes_total": ..., "bytes_returned": ...}`；
  不能安全截断时整段转为文本 preview，并保留 `status` / `error` 字段。

**步数（tool call 序号）**：

- 定义：**单个会话内**、tool call 的 1-based 单调序号。主会话的归档在 bundle 根，subagent
  的归档在 `<bundle>/<topic>/`，各自独立编号；`agent_out(artifact, step=...)` 的 `step`
  因此始终是目标会话自己的序号。
- 序号在结果提交时分配，**新增 `sessions.tool_output_seq INTEGER NOT NULL DEFAULT 0`** 列；
  该序号即归档文件名，结果行不重复存储。**rewind 后不回退、不复用**。历史会话由镜像按消息
  顺序分配；已有同名文件不覆盖。
- 与 omp 的差异：omp 的 `ArtifactManager` 让 parent + 整棵子树共享一个 ID 空间；本 ADR
  选择 per-session 序号 + `<topic>/` 命名空间，以匹配“会话内的步数”和 `agent_out` 的寻址。

### D4：subagent 三件套

| 文件 | 产生时机 | 内容 |
|---|---|---|
| `<topic>.jsonl` | live（`agent/run_transcript.py`） | 子会话 transcript；路径从 `sessions/<agent>/<id>.jsonl` 移到父 bundle |
| `<topic>.md` | settle | **最终报告原文，逐字写入**（omp 语义：`writeArtifact(artifactsDir/<id>.md, rawOutput)`，`packages/coding-agent/src/task/executor.ts:2888`），不二次生成、不加固定章节模板 |
| `<topic>_ast.json` | settle（**始终写**） | 结构摘要聚合：`{version, agent_session_id, subject, generated_at, sources:[{source, summary?, kept_lines, total_lines, elided_ranges, reason?}], reason?}`。新 run 不再写 `.summaries/`；来源是本次 run 收集的结构摘要，缺失时对读过/写过的文件做一次本地 `read_summary.summarize_read`（tree-sitter / stdlib ast，无 LLM）；仍无来源则 `sources: []` + `reason: "no_structural_sources"`。旧 `.summaries/` 只作为一次性迁移来源 |
| `<topic>.json` | settle（可选） | 仅当 run 有结构化输出（`delegate_task` 的 `output_schema`）时写 |

- `.md` 的“模板”是 agent 定义里的完成契约（ROLE + `yield` / 最终消息），与 omp 的
  `prompts/system/subagent-system-prompt.md` 同源；Hermes 不在 settle 时再调用一次模型。
- `_ast.json` 不依赖 `read.summarize.enabled`（默认 false）：`summarize_read` 本身只受
  `min_total_lines` / `prose` / 大小护栏约束。候选来源从 transcript 的 `read_file` /
  `write_file` / `patch` 调用去重后取最近 20 个；单文件沿用 `MAX_SUMMARY_BYTES`（2 MiB）与
  `MAX_SUMMARY_LINES`（20k）护栏；读不到、二进制或超限的文件记 `reason`，不中断 settle。
- `agent_out(ast_summary)` 因此总能读到文件；`sources: []` + `reason` 是合法结果，不是错误。
- 新 run 不创建 `.summaries/`；旧的 `sessions/<agent>/<id>.summaries/` 在一次性聚合进
  `_ast.json` 并校验写入成功后删除。读侧不再回退 `.summaries`。
- subagent 的工具归档写 `<bundle>/<topic>/<step>.<tool>.log`（序号是 subagent 自己的）；
  主会话的工具归档在 bundle 根。两者不共享序号空间，避免同名覆盖。
- `topic` 使用 **入库后的 stored subject**（唯一、可能带 ` (2)` 后缀），slug 化
  （`[^A-Za-z0-9_-]` → `_`，≤64，空则 `agent`）；文件级碰撞再加 8 位 hash。
  大小写不敏感比较，避免 Windows 覆盖。
- `parent_session_id` 为空（cron / layer / 独立 run）时，使用该 run 自己的 bundle；
  不强行挂到某个人类会话下。
- subagent 的 advisor 放 `<bundle>/<topic>/_advisor.jsonl`（omp 同构）；主会话 advisor 放
  bundle 根。
- `agent_session.transcript_path` 的旧值不重写；`agent_out` 解析时先看新路径，再回退旧值；
  镜像可带 `--update-paths` 做一次性元数据更新。

### D5：`agent_out` 新契约

- 模式：`history`、`artifact`、`ast_summary`、`inbox`；**删除 `final_response`**，默认
  `history`。`final_response` 列保留（settle 仍写），只是不再有读取模式。
- `history`：读 `<topic>.jsonl`，保持 40k 字符上限与 `transcript_path` 回退。
- `artifact`：**只服务 named agent session，不服务与用户交互的主会话**。必需
  `agent_session_id` 或 `subject`；可选：
  - `kind="report"`（默认）→ `<topic>.md`
  - `kind="tool"` + `step` + `tool_name` → `<bundle>/<topic>/<step>.<tool>.log` 与（若存在）
    `<step>.<tool>-original.log`
  - `kind="all"` → 可用文件清单
- `ast_summary`：读 `<topic>_ast.json`。
- 返回 identity 增加 `bundle_dir`、`report_path`、`ast_summary_path`；文件不存在时返回
  `null` + 解释，不猜内容。
- 同步更新 `tools/AGENTS.md`、ADR-14/15 的引用、`tests/tools/test_agent_out.py`、
  `tests/tools/test_agent_inbox.py`。

### D6：advisor 命名统一

- 代码、配置、文档、UI、测试统一使用 `advisor`；不做 Python 别名/再导出。
- 不保留旧配置键别名；`WATCHDOG.yml` 文件名不变，文档统一写 advisor。
- `agent.advisor`；`UPSTREAM_ADVISOR_*`（上游本来就是 `advisor`）。
- transcript：`_advisor.jsonl`（默认）与 `_advisor.<slug>.jsonl`（多 reviewer）；subagent 在
  `<topic>/_advisor.jsonl`。`_advisor` 是保留 stem，subagent topic 不得使用。
  `load_advisor_costs` 的目录扫描只认 `_advisor*` 前缀，避免把主 transcript / 子会话 JSONL 计入。
- transcript 直接写入会话 bundle；不迁移任何旧目录。

### D7：omp-ui transcript selector

- 新增只读 RPC：`session.transcript.list`（project → session 文件元数据）与
  `session.transcript.read`（分页 JSONL）。
- selector 优先用 RPC；**不直接读宿主文件系统**，因为 gateway 可能是远程 profile /
  Desktop backend。只有本地 gateway 且 RPC 不可用时才回退 `local://`。
- 过滤规则：只列 bundle 的主 `.jsonl`；排除 `sessions.json`、`request_dump_*`、
  `_advisor*`、subagent `<topic>.jsonl`；未镜像的 live 会话显示 DB 行并标注
  `not mirrored`，resume 仍走 `session.resume`。
- 写操作仍走既有 `session.*`；transcript RPC 只读。

### D8：镜像命令与不可回填项

```text
hermes sessions mirror [--all | --session-id <id> | --since <date>]
                       [--include-archived] [--dry-run] [--verify]
                       [--prune-source] [--older-than <days>] [--update-paths]
```

- 对每个物理 session id 独立导出（**不追随 compression tip 覆盖父会话**）。压缩是 rotate：
  parent 行保留，child 是新 session id；镜像为 child 新建 bundle，父文件不动。只有
  `/retry`、`/undo`、就地 rewrite 才替换同一 id 的 bundle（原子 rename）。
- `--include-archived` 才导出 `active=0` 行；未导出的 archived 行不参与 prune。
- 镜像可从 DB 回填 `<step>.<tool>.log`（内容 = 已持久化的截断结果）与 transcript；
  **无法回填 `-original.log`**。历史会话的该文件缺失是预期，不是错误。
- `--prune-source` 的默认作用域：**所有已结束、且 180 天没有新 user 输入的会话**
  （取最后一次 `user` message 的时间；没有 user 行则用 `started_at`）。
  `--older-than <days>` 覆盖 180；`--older-than 0` 是放开年龄门槛的唯一显式方式。
  `--session-id` 可点名单个会话，但仍须通过 ended / 无 live lease / manifest 校验；
  `--all` 只影响导出范围，不隐式放宽 prune 年龄。`hermes sessions restore <id>` 是逆操作；
  两者都有干跑与逐会话报告。
- prune 把会话标记为 **offloaded**（`sessions.mirror_path`、`mirrored_at`、`offloaded=1`）。
  读侧必须显式处理，不能把 offloaded 当普通空会话：
  - `session.list` 保留该行并带 `archived` / `restore_required`；
  - `session.most_recent` 跳过 offloaded（`-c` 不在启动时隐式写 DB）；
  - `session.resume` 先校验 mirror 文件，自动 `restore` 后再加载；文件缺失或校验失败则拒绝
    并说明原因，绝不返回空会话；
  - `hermes sessions restore <id>` 显式走同一流程，成功后清 `offloaded`。
- `divert_session_transcript_jsonl`（`hermes_state.py:441`）改为写 bundle 路径，避免出现
  第三种 transcript 位置；若无法定位 bundle，仍回退 `sessions/<id>.jsonl` 并记警告。

---

## 迁移

| 旧位置 | 新位置 | 处理 |
|---|---|---|
| `sessions/<agent>/<agent_session_id>.jsonl` | `<bundle>/<topic>.jsonl` | live 写新路径；`--update-paths` 或读侧回退 |
| `sessions/<agent>/<id>.summaries/` | `<bundle>/<topic>_ast.json` | 一次性聚合后删除旧目录；不保留双读 |
| `sessions/<id>.jsonl`（divert） | `<bundle>/<session>.jsonl` | 改写入器；无法定位时保留旧路径 |
| `agent_session.transcript_path` | 新路径 | 解析器双读；镜像可选更新 |
| `cache/spillover/<id>.txt` | 不变 | 模型恢复路径；GC 不变 |

---

## 后果

**正面**

- UI、审计、`agent_out` 有稳定文件入口；会话不再只能通过 DB 读取。
- 工具原文可恢复；截断不再是不可逆的信息损失。
- state.db 可按会话 prune 到冷归档，且 restore 可逆。
- 布局与 omp 同构（JSONL + 同名 artifact 目录、`<n>.<tool>.log`、`<topic>.md`），
  omp-ui 的解析器可直接复用。

**负面 / 代价**

- live 与冷归档两种读取路径：读侧必须先看 DB，再回退 JSONL；UI 要标注未镜像会话。
- `-original.log` 只对新会话生效；历史数据无法补齐。
- 工具归档会重复占用磁盘（spillover + session archive）；`-original` 仅在截断时写以控制体积。
- 镜像与 prune 引入新的运维命令与 manifest；restore 必须测试。
- advisor 命名统一是 breaking change（旧属性名、旧配置键不再支持，写入 release note）。

**不变**

- prompt cache 的前缀边界不变；镜像不进入模型上下文。
- `sessions/` 仍受 `agent/file_safety.py:242-245` 保护，通用文件工具不得改写。
- 远程 sandbox 的模型恢复路径仍是 `cache/spillover`。

---

## 备选与取舍

| 备选 | 不采纳原因 |
|---|---|
| JSONL 成为权威，DB 只做索引 | 需要迁移 resume、FTS、压缩、rewind、Web/Desktop/TUI/API；等于重写存储层 |
| live 双写 JSONL | 必须处理 rotate/rewrite/undo 的一致性与 Windows append，收益只是“文件更早出现” |
| 截断结果直接把 `original_path` 指向 `sessions/` | 远程后端读不到（`_CACHE_DIRS` 不含 sessions）；GC 失去引用基础 |
| 把整个 `sessions/` 挂进 sandbox | 暴露其他会话；动态挂载不可移植 |
| 复用 omp 的共享 ID 空间（parent + 子树一个 artifact 目录） | 与「会话内步数」及 `agent_out(subject, step)` 的寻址冲突；序号需升级为树级序号 |
| settle 时用 LLM 生成 `.md` 工作报告 | `.md` 定义为最终报告原文（与 omp `writeArtifact(..., rawOutput)` 一致），二次生成会引入漂移 |
| 保留 `final_response` 模式 | 与 `history` 的最终 assistant 行重复；契约收窄后无独立价值 |

---

## 实施阶段与验收

### P0：路径与配置契约

- 新增 path helper（home/tmp/abs 编码、timestamp、slug、大小写碰撞、Windows 长度）。
- `tool_output.artifacts` 默认值与配置校验；`hermes config` 可读写。
- 验收：路径单测覆盖 `C:\Users\...`、temp、home 外、中文/空格/`:`、大小写碰撞。

### P1：工具输出归档

- 包装层接入 `_commit_tool_result`；工具内部截断上移；terminal tee。
- 更新 `website/docs/user-guide/configuration.md` 的 `tool_output` 小节（`artifacts` 键）。
- 验收：
  - 截断的 terminal 调用产生 `.log` 与 `-original.log`，后者与截断前（脱敏后）逐字节一致；
  - 未截断调用只有 `.log`；
  - 远程/Docker 场景下模型拿到的仍是 `cache/spillover` 路径且可读；
  - 脱敏生效；JSON 结果不产生非法 JSON；
  - `hermes.tool_output_truncation.count` 仍按原 marker 计数。

### P2：subagent 与 `agent_out`

- `<topic>.jsonl` 迁入父 bundle；settle 写 `.md`（逐字）与 `_ast.json`。
- `agent_out` 模式改造。
- 验收：
  - 命名 agent 跑完后三件套存在；`.md` 与 `final_response` 逐字节一致；
  - `read.summarize.enabled=false` 时 `<topic>_ast.json` 仍存在；无来源时
    `sources: []` + `reason`，不是缺文件；
  - 旧 `.summaries/` 聚合后删除；新 run 不再生成 `.summaries/`；
  - `history` / `artifact(kind=report|tool)` / `ast_summary` 各命中；主会话调用 `artifact`
    被拒绝并说明原因；
  - `artifact(kind=tool, subject=<topic>, step=N, tool_name=...)` 命中
    `<topic>/<N>.<tool>.log`；文件缺失时返回 `null`，不猜测；
  - 旧 `transcript_path` 仍可读。

### P3：镜像与 DB 缩减

- `hermes sessions mirror`、manifest、`--prune-source`、`restore`。
- 更新 `CONTRIBUTING.md:329` 与 `website/docs` 中 `sessions/` 的描述（DB 权威 + 冷镜像）。
- 验收：
  - rotate：父 bundle 字节不变，child 新 bundle 存在；
  - rewrite / `/undo`：同一 bundle 原子替换，无重复行；
  - `mirror → prune → restore` 往返后消息数量与内容一致；
  - 181 天无 user 输入的 ended 会话被 prune；179 天的不动；live / 有 lease 的不动；
    显式 `--session-id` 未达 180 天也不动（除非 `--older-than 0`）；
  - prune 后 `session.list` 标记 `archived` / `restore_required`，`session.most_recent` 跳过，
    `session.resume` 自动 restore；mirror 缺失或校验失败时 resume 拒绝且不产生空会话；
  - 重复 mirror 幂等；`--dry-run` 不写盘。

### P4：omp-ui selector

- `session.transcript.list` / `read`；selector 接入；远程 profile 可用。

**实现注记（P4，2026-11-06）**：两个 RPC 在 `tui_gateway/methods_transcript.py`（`session.list`
之外的新 sibling，`_LONG_HANDLERS` 只把 list 放入线程池）。list 是 `sessions/<project>/*.jsonl` 的
文件扫描（`sessions.json`、`request_dump_*`、`.mirror`、子目录里的 `<topic>.jsonl` 与 `_advisor*`
天然不在结果里），manifest 命中时用其 `message_count` / `mirrored_at` / `offloaded`；read 只用
session id 寻址（manifest → 行的 `mirror_path`），分页按消息序号，客户端无法传路径。
`omp-ui` 的 selector 把 `session.transcript.list` 合并进 `session.list`（先剔除
`source="subagent"` 的 delegate 行——`sessions.show_subagents` 只放宽会话浏览，不放宽 resume
picker）：`restore_required` 的行标 `archived`（archived 无 DB 预览时用 read 的首个 user turn
填充），不在 bundle 索引里的行标 `not mirrored`；read 的行回退只读**本 profile** 的
`state.db`，不查 `params.profile` 之外的 store、也不为读操作 bootstrap 一个。RPC 不可用时不做
任何标注（gateway 可能远程，不回退本地文件系统）。标记走 pinned pi-tui 补丁新增的可选
`SessionSelectorEntry.note`（`src` + `dist/types`，见 `omp-ui/src/VENDORED.md`），不改键位、
布局或默认行为，也不污染 `title` / `firstMessage`。
- 验收：列表只含主会话；未镜像会话标注 `not mirrored`；offloaded 会话标注 `archived`，
  选中触发 restore，失败时显示原因；subagent/advisor 不出现在人类会话列表。

### P5：advisor 命名统一

- 一次性全改；不保留配置键或 transcript 别名，不迁移旧目录。
- 验收：全仓库不再出现旧拼写（含 ADR 历史）；多 reviewer 成本分账不变。

**实现注记（P5，2026-11-06）**：模块、类、配置段、`display_kind`、事件名与 UI 一律使用 `advisor`。
不保留配置键或 `display_kind` 别名，也不迁移旧 transcript。transcript 由 `AdvisorRecorder` 写入
`agent_artifact_dir(agent)`（主会话 bundle 根；命名 run 的 `<bundle>/<topic>/`）。

---

## 风险

| 风险 | 缓解 |
|---|---|
| 镜像与 DB 分叉 | 镜像只读 DB；manifest 校验；`restore` 可逆；不双写 |
| rotate 被误实现为 rewrite | 按物理 session id 导出；P3 两条独立验收；父文件字节断言 |
| `-original.log` 假原文 | 截断上移；terminal tee；P1 逐字节断言 |
| 远程读不到归档 | 模型路径保持 `cache/spillover`；归档路径只进 JSON 字段 |
| 磁盘增长 | `-original` 仅截断时写；归档随 session prune；`exclude_tools` |
| 秘密落盘 | 写盘前强制脱敏；`sessions/` 受 file_safety 保护；不挂进 sandbox |
| 镜像期间会话被压缩 | 导出以 DB 快照为准；导出后校验；不覆盖已有文件 |
| advisor 命名统一破坏插件 | 旧属性/配置键不兼容作为 breaking change 写入 release note |
