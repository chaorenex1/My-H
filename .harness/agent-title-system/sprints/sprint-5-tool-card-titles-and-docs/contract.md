---
sprint: 5
feature: tool-card-titles-and-docs
bundling: split
bundled_with: []
generator_backend: omp
generator_backend_reason: "user 已确认 omp；P4 为登记性契约测试与文档/配置注释同步，纯仓库内改动"
goal: |
  登记工具卡标题为命名系统的确定性消费者（同步派生、零模型调用、display-only），
  并用契约测试冻结"命名路由/生成器变化不影响卡片"的边界；同步文档与辅助模型注释，
  使 `auxiliary.title_generation` 不再被描述为标题的唯一来源。
acceptance_scenarios:
  - id: AS-1
    text: "工具卡标题仍同步派生自 `derive_title()` 且零模型调用：同一调用的 `card_title`/`cell_titles` 在默认配置与 `auxiliary.title_generation` 的 workload/route 变体（`model_upgrade_enabled=false`、`generator=fork`、`card_icons=boring`、tiny/commit/smol 被 pin）下逐字相同；两个 `call_llm` spy 计数为 0；`_UPGRADE_THREADS` 不增加（弱集合，断言子集关系而非相等），不创建升级线程，`card_titles()` 直接同步返回且不断言挂钟；`derive_title()` 仍是唯一派生入口——在 `tools.eval_card_metadata.derive_title` 上装透传包装器计数并委托原函数，禁止替换为桩。"
  - id: AS-2
    text: "`tool.start` 帧携带 `title` 与非空时的 `cell_titles`，随后对同一 `tool_id` 发出 `tool.title`（每一条非空 cell 一条并带 `cell_index`，否则一条）；同一 `tool_id` 的有序事件名序列中，`tool.start` 与其 `tool.title` 之间不出现 `tool.complete`/`tool.error`；`ToolStartPayload`/`ToolTitlePayload`（均为 `extra=\"forbid\"` 的闭合模型）接受该帧并拒绝多带的未知字段；迟到的 `tool.title` 以 `tool_id` + `cell_index` 让 pending 卡片合入标题；标题永不进入模型可见的工具结果。"
  - id: AS-3
    text: "`execute_code` 逐 cell 产出 `cell_titles`：有 `cells[]` 时每 cell 一条，显式 `title` 优先且不被改写；单 `code` 调用用顶层 `title`，否则用代码首行；args 缺失时回落到 result 的 `cells[]`；空/缺失输入返回 `{}`；普通工具只有 `card_title` 且没有 `cell_titles`。"
  - id: AS-4
    text: "持久化与恢复重建同一标题：`prepare_fields`/`_prepare_tool_result_metadata` 把 `card_title`/`cell_titles` 放在 `display_metadata` 顶层，`complete_fields` 与 `session.history`/`resume` 投影的 `details` 返回同一组值；模型可见的 tool result 文本与 `content` 不变，标题不进入 transcript 或系统提示。"
  - id: AS-5
    text: "文档与配置注释同步为新角色：`website/docs/user-guide/configuration.md` 的标题章节把默认生成器描述为 `fork`、失败回落候选计划（`tiny` → `commit_message` → `smol` → 当前会话模型），并把 `auxiliary.title_generation` 描述为 workload 开关 + 显式路由覆盖（pinned 路由在 pinned 时位于计划之首，未 pin 时不得声称固定序号），不再称其为标题唯一来源；改写后的段落把 `<title>…</title>` 标记协议写成现行请求格式（`json_schema` 缺失仅作非计分负向守卫）；`docs/prompt-system.md` 的请求级提示词清单含 `title_system`/`task_label`/`title_card`/`title_fork`，未接线清单仍含 `title-marker-instruction.md`（该文件当前已满足，仅作回归守卫）；`hermes_cli/aux_slots.py` 的 hint 与 `cli-config.yaml.example` 的 auxiliary title 块注释同时含 `workload` 与 `override`（注释形式，安装器逐字复制），`hermes_cli/config_defaults_auxiliary.py` 的注释同义但由 Phase 1 文档漂移读取验收、不由 pytest 读 `.py` 源文本；`tests/agent/test_prompt_sections.py` 与 `tests/ci/test_prompt_surface.py` 保持绿（四个命名提示词为 `scope=request`，系统提示快照字节不变）。"
rubric:
  - axis: Functionality
    weight: high
    threshold: 1.0
    description: All acceptance scenarios pass end-to-end via the sprint-local pytest script
  - axis: Craft
    weight: std
    threshold: 0.85
    description: Tests are deterministic (no network, no timing races) and reuse the existing card/gateway surfaces
  - axis: Consistency
    weight: std
    threshold: 0.9
    description: Card path stays ADR-4 D13 + ADR-21 D1/D2/D9; docs match ADR-21 D3/D4/D13; no second title convention
  - axis: Documentation
    weight: low
    threshold: 0.9
    description: Doc and config-comment edits are accurate, complete, and do not restate a retired protocol
max_iterations: 8
status: done
---

# Sprint 5 Contract — tool-card-titles-and-docs

## Acceptance Scenarios

### AS-1: 工具卡标题同步、确定性、零模型调用，且不随命名路由变化

```
Given  临时 HERMES_HOME 下存在若干 `auxiliary.title_generation` 变体：
       默认（无 pin）、`model_upgrade_enabled=false`、`generator=fork`、
       `card_icons=boring`、tiny/commit/smol 被 pin 到不同 provider/model；
       且 `agent.title_generator.call_llm` 与 `agent.auxiliary_client.call_llm`
       被替换为计数器
When   对同一组输入（`terminal {"command": "hermes pm install bun\nsecond line"}`、
       `read_file {"path": "docs/adr/ADR-4-eval-execution.md"}`、
       `execute_code {"cells": [...]}`）逐变体调用
       `tools.eval_card_metadata.card_titles(...)`，并在调用前后记录
       `agent.title_generator._UPGRADE_THREADS`（`threading.enumerate()` 仅作打印证据）
Then   每个变体的 `card_title`/`cell_titles` 与基线逐字相同；
       两个 `call_llm` spy 的调用计数均为 0；
       `_UPGRADE_THREADS` 不增加（`set(after) <= set(before)`；它是 `WeakSet`，
       不做严格相等），且没有新的 `agent.title_generator` 升级线程目标出现；
       `card_titles()` 直接同步返回 dict（不断言挂钟耗时）；
       `derive_title()` 仍是唯一派生入口：在 `tools.eval_card_metadata.derive_title`
       上安装**透传包装器**（计数后调用原函数，不得替换为桩），计数等于非空标题的派生次数
```

Evidence: `sprints/sprint-5-tool-card-titles-and-docs/evidence/AS-1.txt`

### AS-2: `tool.start` 携带标题、`tool.title` 重复、迟到帧可合并、永不进入工具结果

```
Given  一个 local viewer 会话（`gateway.local_viewer_events` 或
       `tui_gateway.tool_progress` 的现有捕获夹具），`tool_progress` 打开
When   触发 `_on_tool_start` / `combined_tool_start_callback`，参数为
       `read_file {"path": "x"}` 与 `execute_code`（两个 cell），
       随后检查同一 `tool_id` 的事件序列与 wire contract
Then   `tool.start` payload 含 `title`（以及 `execute_code` 的非空 `cell_titles`）；
       紧随其后的 `tool.title` 与 start 的标题一致，逐 cell 时每帧带 `cell_index`
       （每一条非空 `cell_titles` 一条，否则一条）；
       同一 `tool_id` 的有序事件名序列中，`tool.start` 与其 `tool.title` 帧之间
       不出现 `tool.complete` / `tool.error`；
       `EVENTS['tool.start'].payload.model_validate(...)` 与
       `EVENTS['tool.title'].payload.model_validate(...)` 均接受该帧，
       且多带一个未知字段时被拒绝（`Payload` 为 `extra="forbid"`）；
       同一 `tool_id` 的迟到 `tool.title` 仍被接受，客户端可据 `tool_id`+`cell_index`
       合入 pending 卡片（网关侧证据，`omp-ui` 的
       `#handleToolTitle`/`withEvalCellTitle` 是消费端契约，不在 pytest 范围）；
       tool result 字符串与模型可见的 `content` 中不含标题或标题字段
```

Evidence: `sprints/sprint-5-tool-card-titles-and-docs/evidence/AS-2.txt`

### AS-3: `execute_code` 逐 cell 标题与显式 title 优先

```
Given  `card_titles()` 的 module 级缓存在每个用例前清空，session/tool id 唯一
When   依次调用：
       (a) `execute_code {"cells": [{"code": "x = 1\nprint(x)"},
           {"code": "const y = 2", "language": "js", "title": "JS cell"}]}`；
       (b) `execute_code {"code": "print(1)", "title": "One"}`；
       (c) `execute_code {"code": "print(2)"}`；
       (d) `execute_code None` 且 `result={"cells": [{"index": 0, "code": "df.head()"}]}`；
       (e) 普通工具 `terminal/read_file` 与空参数
Then   (a) `cell_titles == ["x = 1", "JS cell"]`（显式 title 原样保留），
       `card_title == "x = 1"`；
       (b) `card_title == "One"`，`cell_titles == ["One"]`；
       (c) `card_title == "print(2)"`；
       (d) `cell_titles == ["df.head()"]`；
       (e) 普通工具只有 `card_title`、无 `cell_titles`，空参数返回 `{}`；
       同一 `(session, tool_call_id, index)` 的重复调用返回缓存值，参数变化不改变结果
```

Evidence: `sprints/sprint-5-tool-card-titles-and-docs/evidence/AS-3.txt`

### AS-4: `display_metadata` 持久化与恢复重建同一标题，模型可见结果不变

```
Given  一次 local viewer 的 `execute_code` 调用（`display` 视图 + 标题），
       以及一行已持久化的 tool row（`display_metadata` 顶层含
       `card_title`/`cell_titles`）
When   调用 `local_viewer_events.prepare_fields(...)` 与 `complete_fields(...)`，
       再对持久化行调用 `session.history` 的投影 `_history_to_messages([row])`
Then   `prepare_fields` 返回的 metadata 顶层（即随后并入 `display_metadata` 的那一层）
       含与 `complete_fields` 相同的 `card_title`/`cell_titles`（`display` 与标题同层）；
       `_history_to_messages` 的 `details` 与行内值逐字相同（含
       `tool_result_metadata` 旧 sidecar 的回退路径）；
       model-facing 的 `content`/tool result 文本与未加标题时逐字节相同；
       `stamp_tool_result_metadata` 只把这些键并入 `display_metadata`，
       不重写其他并发写入的键
```

Evidence: `sprints/sprint-5-tool-card-titles-and-docs/evidence/AS-4.txt`

### AS-5: 文档、配置注释与请求级提示词清单同步

```
Given  当前文档仍把 `auxiliary.title_generation` 写成标题模型的单一槽位，
       `aux_slots.py` 的 hint 是 "session titles"，
       `cli-config.yaml.example` 的 auxiliary title 块没有 workload/override 说明，
       且 `config_defaults_auxiliary.py` 的 title 块注释没有该说明
When   Generator 只改文档与注释（不改运行时行为），pytest 读取：
       `website/docs/user-guide/configuration.md` 中**文本锚定**的标题段落
       （从 "If you do not want Hermes to auto-generate titles…" 到 `### Stream-only endpoints` 之前，
       不是按 `###` 标题切片——最近的标题会带进 picker 散文）；
       `cli-config.yaml.example` 中 `auxiliary:` 下的 `#   title_generation:` 块切片
       （到下一个同级 `#   session_search:` 为止，不得整文件匹配）；
       **导入**的 `hermes_cli.aux_slots.AUX_TASK_META["title_generation"]`；
       `docs/prompt-system.md` 的请求级/未接线清单；
       并运行 `tests/agent/test_prompt_sections.py` 与
       `tests/ci/test_prompt_surface.py`
Then   configuration.md 的该段落出现默认生成器 `fork`、失败回落候选计划
       （`tiny` → `commit_message` → `smol` → 当前会话模型；`commit` 只是 omp 的角色名，
       不是可 pin 的键），并说明 pinned 的显式路由在 pinned 时位于候选计划之首
       （不得声称未 pin 配置下有固定序号），
       且把 `auxiliary.title_generation` 描述为 workload 开关 + 显式路由覆盖，
       不再称其为标题唯一来源；
       **计分**的请求格式断言是正向的：改写后的段落把 `<title>…</title>` 标记协议
       （ADR-21 D4）写成现行请求格式；"不再把 `json_schema` 描述为现行格式" 仅作
       非计分负向守卫（该字符串当前在文件中不存在，单靠它会空洞通过）；
       `cli-config.yaml.example` 的该块以 `#` 注释给出相同的 workload/override 语义
       （安装器逐字复制该文件，不得引入未注释的键）；
       prompt-system.md 的请求级清单含四个命名提示词 key，
       "未接线"清单含 `title-marker-instruction.md`（该文件当前已满足，仅作回归守卫，不重写）；
       aux_slots hint 同时含 `workload` 与 `override`，且不再出现 `backup` 描述；
       `config_defaults_auxiliary.py` 的注释是**撰写义务**，由 Evaluator 的 Phase 1
       文档漂移读取验收，**不由 pytest 读取 `.py` 源文本**（`docs/coding-rules.md:54` MUST）；
       `title_system`/`task_label`/`title_card`/`title_fork` 在 manifest 中均为
       `scope=request`，系统提示 prompt-surface 快照字节不变（无需重新生成）；
       两个既有测试套件全绿
```

Evidence: `sprints/sprint-5-tool-card-titles-and-docs/evidence/AS-5.txt`

## Rubric Detail

| Axis | Weight | Threshold | Scoring notes |
|---|---|---|---|
| Functionality | high | 1.0 | Every AS passes via the sprint-local script; zero runtime drift is a scoring condition, not an aspiration |
| Craft | std | 0.85 | Deterministic tests, `_cache` cleared per variant, monotone `_UPGRADE_THREADS` assert, no sleeps/network/stub-only evidence |
| Consistency | std | 0.9 | No behavioral drift in `card_titles()`/frames/metadata; docs match D3/D4/D9/D13; no second title convention |
| Documentation | low | 0.9 | Docs/comments accurate and complete; the positive `<title>` marker statement is scored, `json_schema` absence is a non-scoring guard |

Total pass condition: **every axis meets its threshold**. Weight only affects
Evaluator's ordering when reporting failures (high-weight fails reported first).

## Notes for Generator & Evaluator

- **这是登记 + 文档 sprint，不是重构 sprint。** 不要改 `card_titles()` 的行为、
  `derive_title()` 的算法、`tui_gateway/tool_progress.py` 的发射序列，或
  `display_metadata` 的键名/shape；不要给工具卡路径加模型调用、线程、缓存策略变化或新的
  payload 字段。运行时行为净差应为 0；`aux_slots.py` 只改 picker hint 文案，
  不改 `AUX_TASK_KEYS`/order，`config_defaults_auxiliary.py` 只改注释。
- **文档不得引入与 D4 冲突的新协议描述。** 统一协议是 `<title>…</title>` 标记；
  `<title/>` 与 `none` 表示无可命名任务并终止；不得把内联 JSON schema、示例 echo 守卫
  写成现行契约，也不得为工具卡另造一套协议。改动 configuration.md 的标题段落时，必须用正向表述
  把 `<title>…</title>` 标记协议写成现行请求格式（**这是计分断言**）；该文件当前**不存在**
  `json_schema` 字符串，所以"不提 json_schema"只作非计分负向守卫，不要为一个不存在的表述
  发明改动。
- **测试面**：直接复用
  `tests/tools/test_eval_card_metadata.py`、`tests/gateway/test_local_viewer_events_turn.py`、
  `tests/tui_gateway/test_history_tool_content.py`、
  `tests/tui_gateway/test_show_reasoning_display_gate.py`、
  `tests/agent/test_prompt_sections.py`、`tests/ci/test_prompt_surface.py` 的既有夹具与
  断言风格；sprint-local pytest 脚本只补 AS-1 的零模型计数与 AS-5 的文本/注册断言，
  不要复制整套实现测试。
- **零模型计数的 patch 点**：`agent/title_generator.py` 在模块层
  `from agent.auxiliary_client import call_llm`，因此 monkeypatch
  `agent.title_generator.call_llm`（并同时 spy `agent.auxiliary_client.call_llm` 作为兜底）。
  断言计数为 0。`derive_title` 的计数点：`tools/eval_card_metadata.py` 在导入时绑定该名字，
  因此 patch `agent.title_generator.derive_title` 对本路径无效；必须在
  `tools.eval_card_metadata.derive_title` 上安装**透传包装器**（计数后调用原函数）。
  替换为返回固定标题的桩是禁止的（那会绕过被测路径）。
- **缓存**：`tools/eval_card_metadata._cache` 是模块级、按 `(session_id, tool_call_id, index)`
  键控；必须在**每个配置变体之前**清空 `_cache`（不是每个用例一次），否则变体 2..n 直接命中
  缓存，"跨变体逐字相同"就变成同一份缓存的自我比较。需要验证缓存命中本身时（AS-3 末条）
  另用固定 id。
- **配置变体**：用项目 `tests/conftest.py` 的临时 `HERMES_HOME` 隔离；写入/改写
  `config.yaml` 后清空 `hermes_cli.config._LOAD_CONFIG_CACHE` 与 `_RAW_CONFIG_CACHE`
  （或使用现有配置夹具）。`generator`/`card_icons` 只在 sprint 4 引入，读取它们不影响
  `card_titles()`；不要在测试里实现它们。
- **迟到标题合并**：pytest 证明的是网关侧契约（同一 `tool_id` 的 `tool.title` 在
  `tool.start` 之后发出、payload 含 `cell_index`、wire contract 接受）。真正的 pending 卡片
  合并发生在 `omp-ui`（`#handleToolTitle` → `withEvalCellTitle`），其 vitest 不在
  evaluator_tools 内，**不要**为此加 TS 测试或把 vitest 塞进验收脚本。
- **prompt-surface 快照**：四个命名提示词是 `scope=request`，不得改变系统提示或
  `tools[]` 快照。若 AS-5 发现快照差异，正确动作是修实现/注册（或让前序 sprint 的
  实现收敛），不是手工编辑 `tests/fixtures/prompt_surface/`；确需重生成时用
  `scripts/run-in-hermes-env python scripts/ci/prompt_surface.py render`。
- **文档断言的粒度**：用文件文本断言（`read_text` + 段落切片），不要渲染 website 或起 mkdocs；
  关键词大小写不敏感。`configuration.md` 的标题段落必须**按文本锚定**：从
  "If you do not want Hermes to auto-generate titles" （`:1614`）到 `### Stream-only endpoints`
  （`:1664`）之前；不要按 `###` 标题切片——最近的标题在 `:1558`，会带进无关的 picker 散文。
  `cli-config.yaml.example` 只切 `auxiliary:` 下的 `#   title_generation:` 块（`:991`）到
  下一个同级 `#   session_search:`（`:1005`）；该文件另有一个 legacy `max_concurrency` 块
  （`:1022-1027`），整文件匹配会误命中。`docs/prompt-system.md:36-44` 当前已满足 AS-5
  （四个 prompt key + `title-marker-instruction.md` 均在），**不要重写该文件**，它只是回归守卫；
  `:1-20` 的 LRU/judgment 前导是先行存在的无关漂移，不计分。
- **范围边界**：不做会话卡片/fork（sprint 4）、不做 `agent_session.title`、不做子代理标签与
  `agent_message` 寻址、不做 schema 迁移、不改 ADR-17 漂移阈值、不新增配置键。工具卡标题的
  唯一所有者仍是 ADR-4 D13 + `derive_title()`；本 sprint 只登记与对账。
- **测试入口**：沿用 sprint-3/4 已接受的替代：本 shell 下 `scripts/run_tests.sh` 无法运行，
  sprint-local 的 `eval-<feature>.sh` 与全部验收命令使用 pinned test interpreter 下的
  `scripts/run_tests_parallel.py`，带上 runner 的 `TZ`/`LANG`/`PYTHONHASHSEED`，并把确切命令与
  退出码写进 evidence；禁止裸 `pytest`（`AGENTS.md`、`docs/coding-rules.md`）。
- **Phase 2.5 质量门**：`python scripts/check`（ruff + workflow guards + 改动文件的 code-health
  ratchet）加 `python scripts/ci/check_docs_drift.py`（AS-5 改写文档散文）。先行存在的红只有在
  给出 base-SHA 证据（文件在冻结基线上逐字节相同，且键已在 `docs_drift_baseline.json`）时才算
  非归因；新漂移键、新失败测试、或改动文件内的发现一律归因本 sprint 并判失败。
- **零运行时漂移是计分条件**：`card_titles()` 输出、`derive_title()`、`tool.start`/`tool.title`
  发射顺序、`display_metadata` 键名/shape 必须行为等同。在这些模块里"顺手小修"是 Consistency
  失败，不是加分项。
- **无 ADR / `AGENTS.md` 改动**：ADR-21 `:77`/`:105`/`:339-350` 已登记该消费者，仓库内也没有
  提到 `card_titles`/`eval_card_metadata` 的 `AGENTS.md`；不要发明第五个文档面。

## Negotiation Log

<!-- Round summaries are appended by harness-loop; contract.md freezes after status: active. -->

### Round 1

- **Generator**: `counter` — F 1.0 / Craft 0.85 / Consistency 0.9 / Documentation 0.9, `max_iterations` 8; asks the Evaluator to rule on AS-5's assertion surface (`config_defaults_auxiliary.py` source read vs behavioural substitute). `feedback/generator-neg-1.md`
- **Evaluator**: `counter` — thresholds and budget accepted unchanged; counter is on contract text: AS-5 `.py` source read violates `docs/coding-rules.md:54 [MUST]`, AS-2 names a non-existent `_lifecycle`, AS-1's `derive_title` spy point is a no-op, plus cache/thread/slice false-pass traps. Ruled AS-5's substitutes (imported `AUX_TASK_META`, `cli-config.yaml.example` block) and filed corrections (a)–(e) + readings 1–5. `feedback/evaluator-neg-1.md`

### Round 2

- **Generator**: `accept` — re-verified every correction against the checkout; accepted thresholds, (a)–(e) and readings 1–5, with four freeze clarifications: text-anchored AS-5 slice (`:1614` → `:1664`), the `json_schema` clause demoted to a non-scoring negative guard with the positive `<title>` marker assertion scored, `commit_message` as the pinnable key, and `cli-config.yaml.example` block boundaries/comment-only edits. `feedback/generator-neg-2.md`
- **Evaluator**: skipped — Generator's `accept` resolved the round; the Evaluator's round-1 proposal is the accepted delta.

### Round 3

- Not reached (resolved in round 2).

### Ruling

- Not required (no stalemate).

## Sprint Outcome

- **Final iteration**: 1
- **Last commit**: _pending_
- **Aborted reason** (if any): none
- **PR**: _pending_
- **Verdict**: pass — functionality 1.0 / craft 0.9 / consistency 0.9 / documentation 0.9; 0 Critical, 0 Improvement, 4 Minor
- **Notes**: AS-1..AS-5 executed by an independent Evaluator probe (10/10 checks); zero runtime drift proven by empty diff over the card/title/frame/metadata modules; all reds attributed to the base with reproduction logs.

