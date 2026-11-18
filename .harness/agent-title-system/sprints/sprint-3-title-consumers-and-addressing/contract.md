---
sprint: 3
feature: title-consumers-and-addressing
bundling: split
generator_backend: omp
generator_backend_reason: "user 已确认 omp；P2 跨 schema 迁移、异步写入与寻址契约，host runtime 与同一 pytest/临时 HERMES_HOME 工具链减少环境分叉"
goal: 把命名服务接到会话标题、子代理任务标签、子代理产物 topic 与 agent_message 寻址上，并落地 agent_session.title 的 legacy 迁移与 write-once 语义。
acceptance_scenarios:
  - id: AS-1
    text: "generator=plan（或未设置 generator）时会话标题升级只走 title_model_plan() 的首个成功候选并把 provenance 写为 llm；generator=fork 显式归一化为 plan 路径并留下可断言的降级标记，而不是静默跳过命名；漂移再生成复用同一生成器与 refresh_auto_title CAS；现有 title/drift/input 测试保持全绿。"
  - id: AS-2
    text: "具名子代理 run 创建后用非空 assignment 异步获得 write-once 的 task-label 标题：标签至多 9 词、去引号与句末标点、超过 80 字符拒绝、等于 run name 或 echo Name-2 spawn handle 时拒绝、<title/> 或 none 不写；写入发生在 daemon 线程且 spawn 不等待它。"
  - id: AS-3
    text: "空 assignment、dispatcher-owned worker、enabled=false 或 model_upgrade_enabled=false 四种情形下，任务标签生成零模型调用、零写入、零标签线程，run 创建与回合开始均不被阻塞。"
  - id: AS-4
    text: "打开缺少 agent_session.title 的 legacy store 时由 reconciler 补出 TEXT NOT NULL DEFAULT '' 列与非唯一 idx_agent_session_title 索引且不回填；重复打开幂等并保留既有非空值；写入是唯一的 UPDATE agent_session SET title=? WHERE agent_session_id=? AND title=''，第二次标签与空标签都不改变列值。"
  - id: AS-5
    text: "derive_topic(assignment) 跳过开头 Markdown 标题行、围栏块与 HTML 注释后取首个有意义行，沿用 derive_title() 的 48 字截断，sanitize 为至多 64 字符组件；sanitized stem 无 ASCII alphanumeric 时回退 topic-<8-hex digest>，碰撞由 allocate_topic 加 8 hex 后缀；显式 subject 优先，空 assignment 回退 run name。"
  - id: AS-6
    text: "transcript 路径在 run 创建时冻结、manifest 路径在 settle 时冻结，全部产物共享同一 <topic> stem；生成任务标签或会话标题漂移后，任何文件都不被重算、重命名或移动，agent_out 与 identity_for_row 继续返回 manifest 路径。"
  - id: AS-7
    text: "agent_message.to 按 label → id → name → subject 解析，strip()+casefold() 精确匹配；同 profile 存活 run 的重复标签在写入同一事务内追加 ' 2'/' 3' 后缀；历史重复标签返回带候选的拒绝而不猜第一个；标签缺失时回退 id/name/subject 且不阻塞投递；显式身份参数与 agent_out 语义不变。"
rubric:
  - axis: Functionality
    weight: high
    threshold: 1.0
  - axis: Craft
    weight: std
    threshold: 0.85
  - axis: Consistency
    weight: std
    threshold: 0.9
  - axis: Documentation
    weight: low
    threshold: 0.85
max_iterations: 8
status: done
---

# Sprint 3 Contract — title-consumers-and-addressing

## Goal

把 sprint 1 的标记协议与 sprint 2 的候选计划接到四个消费者上：会话标题与漂移、子代理任务标签、子代理产物 `topic`、`agent_message` 自由文本寻址；同时新增 `agent_session.title` 列、legacy reconciler 与 write-once 写入。本 sprint 的验收以 pytest（临时 `HERMES_HOME`）为唯一证据面，不实现 fork 副回合与卡片（见「Frozen Boundary」）。

依赖：sprint 2 的 `title_model_plan()` 与 stage-1 生成入口先落。若 sprint 2 未交付一个只消费计划的 stage-1 驱动，本 sprint 必须补一个最小入口，但**不得重写计划本身**（顺序、去重、`fallback=False` 归 sprint 2）。

## Acceptance Scenarios

### AS-1: 会话标题与漂移改走生成器开关，plan 路径生效

```text
Given 一个已有 derived 标题的会话、临时 HERMES_HOME，以及被 monkeypatch 记录调用的模型边界
When  maybe_auto_title() 在 generator=plan（以及 generator 未设置）下运行；随后再以 generator=fork 运行；最后触发一次 title-drift 再生成
Then  plan 运行只走 title_model_plan() 的首个成功候选，会话标题 provenance 变为 llm；
      generator 未设置与 plan 行为一致；
      fork 运行显式归一化为 plan，并留下可断言的降级标记（日志行或返回值），不产生静默不命名的第二路径；
      漂移再生成复用同一生成器并走 refresh_auto_title(expected_title=...) CAS；
      tests/agent/test_title_generator.py、tests/agent/test_title_drift.py、tests/agent/test_title_input.py 全绿
```

- Surface：`tests/agent/test_title_generator.py`、`tests/agent/test_title_drift.py`、`tests/agent/test_title_input.py`（在既有文件追加用例，不另起并行套件）
- Evidence：`sprints/sprint-3-title-consumers-and-addressing/evidence/AS-1-pytest.txt`

### AS-2: 任务标签的异步生成、守卫与边界

```text
Given 临时 HERMES_HOME、一个具名 run（name=alice、spawn handle=alice-2）与非空 assignment
When  spawn 返回后标签线程完成，且模型分别返回：合规标签、恰好 9 词、10 词、带引号与句末标点、恰好 80 字符、81 字符、等于 alice、等于 alice-2、<title/>、none
Then  spawn 不等待模型（标签线程为 daemon）；
      合规标签去引号、去句末标点后写入 agent_session.title；
      9 词接受、10 词拒绝；80 字符接受、81 字符拒绝；echo name/handle 拒绝；<title/> 与 none 不写；
      标签不出现在 transcript、工具结果或系统提示中
```

- Surface：新增 `tests/agent/test_task_label.py`
- Evidence：`sprints/sprint-3-title-consumers-and-addressing/evidence/AS-2-pytest.txt`

### AS-3: 跳过生成的四类情形

```text
Given 临时 HERMES_HOME，且模型边界被替换为「一旦调用即失败」
When  分别创建：空 assignment 的具名 run；dispatcher-owned worker（is_dispatcher_owned_worker_context() 为真）；enabled=false；model_upgrade_enabled=false
Then  四条路径都不发起模型调用、不创建标签线程、agent_session.title 保持 ''；
      每次 run 创建/回合开始都在超时时间内返回，不等待任何标签工作
```

- Surface：`tests/agent/test_task_label.py`
- Evidence：`sprints/sprint-3-title-consumers-and-addressing/evidence/AS-3-pytest.txt`

### AS-4: legacy 迁移与 write-once 写入

```text
Given 一个按旧 DDL 建好的 store（agent_session 无 title 列，含若干既有行，且不含任何非空 title）
When  用 SessionDB/reconcile 打开一次；随后经原始 SQL 把其中一行 title 置为 'keep me'；
      连续再打开两次；最后依次调用标签写入（第一次、第二次不同标签、一次空标签）
Then  首次打开补出 title TEXT NOT NULL DEFAULT ''（PRAGMA table_info：notnull=1、dflt_value="''"）
      与非唯一索引 idx_agent_session_title；此刻所有既有行 title=''（不回填）；
      'keep me' 只由测试自身写入，不作为 reconciler 的证据；
      第 2、3 次打开不产生新的 schema 变更（幂等），且 'keep me' 原样保留；
      第一次写成功，第二次与空标签均不改变列值；
      PRAGMA index_list(agent_session) 中该索引 unique=0
```

- Surface：新增 `tests/hermes_state/test_agent_session_title.py`（复用现有 `tests/hermes_state/conftest.py` 的构造方式）
- Evidence：`sprints/sprint-3-title-consumers-and-addressing/evidence/AS-4-pytest.txt`

### AS-5: `derive_topic` 的跳行、截断、sanitize、digest 与碰撞

```text
Given assignment 形如 "## Heading\n```\nfenced\n```\n<!-- note -->\n真正任务首行…"，以及纯中文/emoji assignment
When  调用 derive_topic(assignment) 并把结果经 sanitize_component + allocate_topic 落盘
Then  标题行、围栏块、HTML 注释被跳过，取首个有意义行；
      >48 字在派生阶段截断；sanitize 后 ≤64 字符且只含 [A-Za-z0-9_-]；
      无 ASCII alphanumeric 的 stem 变成 topic-<8 位小写 hex digest>（同输入稳定）；
      同 bundle 第二次同名得到 -<8 hex> 后缀；
      显式 subject 优先；空 assignment 回退 run name
```

- Surface：`tests/test_session_paths.py`（扩展现有 `allocate_topic`/`sanitize_component` 用例）；run 级回退断言放 `tests/tools/test_agent_tool.py`
- Evidence：`sprints/sprint-3-title-consumers-and-addressing/evidence/AS-5-pytest.txt`

### AS-6: 产物路径创建后冻结

```text
Given 一个已创建并已 settle 的具名 run（临时 HERMES_HOME）
When  记录创建时的 transcript_path 与 settle 后 manifest 的 transcript/report/ast/structured/dir 路径；随后生成任务标签并触发会话标题漂移
Then  `<topic>.jsonl`、`<topic>.md`、`<topic>_ast.json`、`<topic>.json`、`<topic>/` 共享同一 stem；
      标签与标题变化后上述路径逐字不变，bundle 目录文件集合不变（无新增、无改名、无移动）；
      agent_out 与 identity_for_row 返回 manifest 记录的路径；transcript 内容不含标签文本
```

- Surface：`tests/tools/test_agent_out.py`（identity/manifest）、`tests/tools/test_agent_tool.py`（创建时路径）
- Evidence：`sprints/sprint-3-title-consumers-and-addressing/evidence/AS-6-pytest.txt`

### AS-7: `agent_message.to` 的 label-first 寻址与歧义拒绝

```text
Given 临时 HERMES_HOME 中三个具名 run（label 为 alpha、beta、beta 冲突后 beta 2），以及一条直插的历史重复 label 行
When  依次以 "ALPHA"、id、name、subject 作为 to 调用 agent_message；再以历史重复 label 调用；再在标签尚未生成时以 name/subject 调用
Then  strip()+casefold() 后精确匹配，label 优先于 id/name/subject；
      重复存活 run 的第二个标签写入为 "beta 2"，第三次为 "beta 3"，后缀判定与写入在同一事务内；
      历史重复 label 返回 tool_error 并列出候选（name/subject/agent_session_id），不投递；
      无标签时回退 id/name/subject 且投递成功、不阻塞；
      显式 agent_session_id/name/subject 的参数优先级不变，agent_out 语义不变；
      AGENT_MESSAGE_SCHEMA.to 的描述改为 label-first 并保留 id/name/subject 说明
```

- Surface：`tests/tools/test_agent_message.py`、`tests/tools/test_agent_out.py`
- Evidence：`sprints/sprint-3-title-consumers-and-addressing/evidence/AS-7-pytest.txt`

## Frozen Boundary: fork → plan

> Sprint 3 只实现 plan 生成器路径：`auxiliary.title_generation.generator=plan` 直接消费 `title_model_plan()`；`generator` 未设置或为 `fork` 时必须在同一代码路径上显式归一化为 `plan` 并留下可断言的降级标记（日志或返回值），绝不静默跳过命名；fork 副回合、卡片解析与 `card_icons` 的运行时行为归 sprint 4。

本 sprint 的验收一律按 plan 路径判定。任何"为让 `generator=fork` 看起来可用"而提前实现副回合、卡片解析或 `keepTitleCard` 的行为都属越界；sprint 4 的契约再补 fork 路径与卡片。

## Scope additions (negotiated, round 1→2)

- **新增配置键 `auxiliary.title_generation.generator`**（默认 `fork`）：必须在
  `hermes_cli/config_defaults_auxiliary.py` 的 `title_generation` 块中登记，否则
  `_validate_schema_key`（`hermes_cli/config.py:2985+`）会拒绝该子键；同时补
  `website/docs/user-guide/configuration.md` 标题小节一句与 `cli-config.yaml.example`
  一行，避免 `scripts/ci/check_docs_drift.py` 产生新的（sprint 可归因的）缺口。
  P4 的成篇散文仍留在 sprint 5。`card_icons` 本 sprint 不登记。
- **`tools/AGENTS.md` §"Named agents"** 增加一条子句，说明 `agent_session.title` 与
  `agent_message.to` 的 label-first 语义；子句 ≤600 字符（root + area 链上限 30,000，
  当前 28,999），改后必须重跑 `scripts/ci/check_agents_md_size.py`。
- **降级标记为 DEBUG 级**：`generator=fork` 是默认值，WARNING 会在每次普通运行触发；
  AS-1 的未设置分支通过归一化断言覆盖（未设置与显式 `fork` 都有标记，`plan` 无标记）。

## Rubric Detail

| Axis | Weight | Threshold | Scoring notes |
|---|---|---|---|
| Functionality | high | 1.0 | 七条 AS 的 pytest 断言全部通过；schema/写入/寻址/路径冻结按行为而非实现命名判定 |
| Craft | std | 0.85 | 复用既有 `title_model_plan`、`spawn_context_thread`、reconciler、`allocate_topic`；无重复命名逻辑；无对真实 store 的写入 |
| Consistency | std | 0.9 | 沿用 `short_name` 列/索引与 `_resolve` 身份优先级；错误文本与既有 `tool_error` 风格一致；命名标记与 sprint 1 契约一致 |
| Documentation | low | 0.85 | `AGENT_MESSAGE_SCHEMA.to` 描述与新增列/索引注释同步；不写超出本 sprint 范围的文档 |

Total pass condition: **every axis meets its threshold**。weight 只影响 Evaluator 报告失败时的排序（high 先报）。

## Notes for Generator & Evaluator

### 测试环境

- 所有新测试用临时 `HERMES_HOME`（`monkeypatch.setenv`，复用 `tests/conftest.py` 既有 fixture 模式）；不得读写操作者真实 store。
- 测试入口：优先 `scripts/run_tests.sh <test paths> [-- pytest 参数]`（仓库规则：禁止裸 `pytest`，见 `AGENTS.md` / `docs/coding-rules.md`）。在本工作站上该 wrapper 从 bash 调用不可用（`pwd` 返回 `C:\…`，`_activation.sh` 找不到，re-exec 把 `/mnt/c` 映射成 `C:\mnt\c`），因此以 `scripts/run_tests_parallel.py` 在固定 test 环境解释器下直接运行等价于同一 runner（沿用其 `TZ`/`LANG`/`PYTHONHASHSEED`）；sprint-local 的 `eval-<feature>.sh` 必须调用实际可用的入口，并把输出与退出码落到 `evidence/`。路径断言用 `Path` 比较，不写死分隔符；断言中避免依赖 mtime。
- 证据写入 `evidence/AS-<n>-pytest.txt`，至少包含所涉测试文件的 `-q` 汇总与失败时的断言原文。

### 迁移（AS-4）

- 列加入 `hermes_state_common.py` 的 `SCHEMA_SQL`：`title TEXT NOT NULL DEFAULT ''`；索引 `idx_agent_session_title` **非唯一**，并在 `_reconcile_columns()` 之后创建（参照 `idx_agent_session_short_name` 的注释与位置，`hermes_state_schema.py:949` 一带）。
- reconciler 的语义是"按 SCHEMA_SQL 补列"，因此 legacy 断言必须用**手工旧 DDL** 建库，而不是用当前 SCHEMA_SQL。
- 不回填、不改既有行；连续初始化两次必须幂等（duplicate column 分支已被 reconciler 容忍）。
- 写路径唯一：`UPDATE agent_session SET title=? WHERE agent_session_id=? AND title=''`；`rowcount==0` 即视为已命名/空值；不得存在第二条 UPDATE。
- 空标签在进入 DB 前拒绝（不执行写）。

### 身份固化（AS-5、AS-6）

- 创建：`tools/agent_tool.py::_finalize_run_paths` 是 topic 分配与 `transcript_path` 写入的唯一位置；settle：`agent/run_artifacts.py::settle_bundle` 是 manifest 的唯一位置。
- 标签与标题只是 DB/展示元数据；任何文件名都不得由标签派生。测试要在标签/标题变化前后比较**路径字符串集合**与目录文件集合。
- `identity_for_row` 在 manifest 存在时以 manifest 为准；不要为标签新增回退分支。legacy 行仅在 manifest 缺路径时用冻结的 `subject` 重算。

### 异步（AS-2、AS-3）

- 标签工作走 `hermes_threading.py::spawn_context_thread(..., daemon=True)` 或等价 daemon 线程，继承 profile 与 accounting；退出时不 join。
- spawn/回合开始不得等待标签：测试用阻塞的假模型 + `threading.Event` 证明返回先于标签完成。
- 计划为空、gate 关闭、assignment 为空、dispatcher-owned worker：不调用模型、不建线程、不写。
- 失败只 `debug`；不得向用户抛错，不得把失败文本写进 transcript。

### 寻址（AS-7）

- 只改 `to` 的解析链（label → id → name → subject）；显式参数保持 `_resolve` 的 id → name → subject 顺序与优先级。
- 比较先 `strip()` 再 `casefold()` 精确匹配，禁止前缀/模糊；标签命中优先于其它命中。
- 后缀判定与写入同事务（并发冲突最多重试一次）；历史重复行仍走"拒绝并列出候选"。
- `get_agent_sessions_by_title(title)`：先按索引精确查 `title=?`，未命中再对非空 `title` 在 Python 侧 `casefold()` 比较。
- 歧义错误必须包含候选的 `name`/`subject`/`agent_session_id`，且不得投递（`deliver_peer_message` 不被调用）。
- `agent_out` 仍以 `subject` 为主；不得因 label 改变其解析或输出。

### 非目标与回归底线

- 不实现 fork 副回合、卡片解析、`card_icons` 运行时行为、`/rename`、Nerd Font。
- 不改 ADR-17 的 judge 阈值/窗口（0.5、3000/1000），不改 `derived < llm < user` 权威顺序；`user` 标题永不覆盖。
- 命名输出不进 transcript、工具结果、系统提示；缓存不变量由断言覆盖。
- 不重命名/移动已创建产物；不为文件名引入模型调用。
- 现有 `tests/agent/test_title_*.py`、`tests/tools/test_agent_message.py`、`tests/tools/test_agent_out.py`、`tests/test_session_paths.py` 必须保持全绿。

## Negotiation Log

### Round 1

- **Generator**: `counter` — Functionality 1.0, Craft 0.85, Consistency 0.9, Documentation 0.85, max_iterations 8; migration and async commitments recorded (title column after `_reconcile_columns`, `spawn_context_thread(daemon=True)`, gates before thread, event-based ordering proof). (`feedback/generator-neg-1.md`)
- **Evaluator**: `counter` — thresholds and max_iterations accepted unchanged; counter is on scope only: the new `auxiliary.title_generation.generator` key needs a `DEFAULT_CONFIG` entry (else `_validate_schema_key` refuses it) plus `configuration.md` + example-config lines to avoid a sprint-attributable docs-drift gap, and `tools/AGENTS.md` §"Named agents" needs one clause. AS-4 five-step replacement approved. (`feedback/evaluator-neg-1.md`)

### Round 2

- **Generator**: `accept` — both scope additions accepted; chain-cap correction recorded (root + area = 28,999 of 30,000, so the AGENTS clause is ≤600 chars); DEBUG-level degradation marker; AS-4 body frozen verbatim from the Evaluator. (`feedback/generator-neg-2.md`)
- **Evaluator**: not required — Generator `accept` resolves the round; the Evaluator's round-1 proposal is the accepted delta.

### Round 3

- **Generator**: not required.
- **Evaluator**: not required.

### Ruling

- **Planner**: not required — mutual agreement in round 2. pending — `threshold` 与 `max_iterations` 由本轮协商填写。

## Sprint Outcome

- **Final iteration**: 1 (verdict pass; Fn 1.0 / Craft 0.9 / Consistency 0.92 / Documentation 0.9)
- **Last commit**: 7eaad6cbbb14ee4791444148de286ed36e7ee24e (iter-1 checkpoint)
- **Aborted reason** (if any): none
- **PR**: (recorded after Step 8)
