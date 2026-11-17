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
    text: "工具卡标题仍同步派生自 `derive_title()` 且零模型调用：同一调用的 `card_title`/`cell_titles` 在默认配置与 `auxiliary.title_generation` 的 workload/route 变体（`model_upgrade_enabled=false`、`generator=fork`、`card_icons=boring`、tiny/commit/smol 被 pin）下逐字相同；spy `agent.title_generator.call_llm` 计数为 0，且派生过程不创建后台线程。"
  - id: AS-2
    text: "`tool.start` 帧携带 `title` 与非空时的 `cell_titles`，随后对同一 `tool_id` 重复发出 `tool.title`（逐 cell 时带 `cell_index`）；`tool.title` 不是生命周期边，wire contract 接受该 payload；迟到的 `tool.title` 以 `tool_id` + `cell_index` 让 pending 卡片合入标题；标题永不进入模型可见的工具结果。"
  - id: AS-3
    text: "`execute_code` 逐 cell 产出 `cell_titles`：有 `cells[]` 时每 cell 一条，显式 `title` 优先且不被改写；单 `code` 调用用顶层 `title`，否则用代码首行；args 缺失时回落到 result 的 `cells[]`；空/缺失输入返回 `{}`；普通工具只有 `card_title` 且没有 `cell_titles`。"
  - id: AS-4
    text: "持久化与恢复重建同一标题：`prepare_fields`/`_prepare_tool_result_metadata` 把 `card_title`/`cell_titles` 放在 `display_metadata` 顶层，`complete_fields` 与 `session.history`/`resume` 投影的 `details` 返回同一组值；模型可见的 tool result 文本与 `content` 不变，标题不进入 transcript 或系统提示。"
  - id: AS-5
    text: "文档与配置注释同步为新角色：`website/docs/user-guide/configuration.md` 的标题章节把默认生成器描述为 `fork`、失败回落候选计划（tiny → commit → smol → 当前会话模型），并把 `auxiliary.title_generation` 描述为 workload 开关 + 显式路由覆盖（候选 0），不再称其为标题唯一来源；被改写的段落不再把 `json_schema` 描述为现行标题请求格式；`docs/prompt-system.md` 的请求级提示词清单含 `title_system`/`task_label`/`title_card`/`title_fork`，未接线清单仍含 `title-marker-instruction.md`；`hermes_cli/aux_slots.py` 与 `hermes_cli/config_defaults_auxiliary.py` 的 `title_generation` 提示/注释同时含 `workload` 与 `override`，不再描述为 tiny 的 backup；`tests/agent/test_prompt_sections.py` 与 `tests/ci/test_prompt_surface.py` 保持绿（四个命名提示词为 `scope=request`，系统提示快照字节不变）。"
rubric:
  - axis: Functionality
    weight: high
    threshold: "?"
    description: All acceptance scenarios pass end-to-end via the sprint-local pytest script
  - axis: Craft
    weight: std
    threshold: "?"
    description: Tests are deterministic (no network, no timing races) and reuse the existing card/gateway surfaces
  - axis: Consistency
    weight: std
    threshold: "?"
    description: Card path stays ADR-4 D13 + ADR-21 D1/D2/D9; docs match ADR-21 D3/D4/D13; no second title convention
  - axis: Documentation
    weight: low
    threshold: "?"
    description: Doc and config-comment edits are accurate, complete, and do not restate a retired protocol
max_iterations: "?"
status: pending-negotiation
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
       `threading.enumerate()` / `agent.title_generator._UPGRADE_THREADS`
Then   每个变体的 `card_title`/`cell_titles` 与基线逐字相同；
       两个 `call_llm` spy 的调用计数均为 0；
       `agent.title_generator._UPGRADE_THREADS` 在调用前后相同（没有标题升级线程被创建），
       调用在同一次同步返回中完成；
       `derive_title()` 仍是唯一派生入口（spy 包一层后计数等于非空标题的派生次数）
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
       紧随其后的 `tool.title` 与 start 的标题一致，逐 cell 时每帧带 `cell_index`；
       `EVENTS['tool.title'].payload.model_validate(...)` 接受该帧；
       `tool.title` 不出现在生命周期边集合中（`_lifecycle` 只看到 start/complete）；
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
       `docs/prompt-system.md` 未列出命名提示词，
       `aux_slots.py` 的 hint 是 "session titles"，
       且 `config_defaults_auxiliary.py` 的 title 块没有 workload/override 说明
When   Generator 只改文档与注释（不改运行时行为），pytest 读取：
       `website/docs/user-guide/configuration.md` 的 title 章节、
       `docs/prompt-system.md` 的请求级/未接线清单、
       `hermes_cli/aux_slots.py` 的 `AUX_TASK_META["title_generation"]`、
       `hermes_cli/config_defaults_auxiliary.py` 的 title 块注释；
       并运行 `tests/agent/test_prompt_sections.py` 与
       `tests/ci/test_prompt_surface.py`
Then   configuration.md 的标题章节出现 `fork`、候选计划（`tiny`/`commit`/`smol`/
       current session model）与显式覆盖（candidate 0）语义，
       不再把 `title_generation` 描述为标题唯一来源，不再把 `json_schema`
       描述为现行标题请求格式；
       prompt-system.md 的请求级清单含四个命名提示词 key，
       "未接线"清单含 `title-marker-instruction.md`；
       aux_slots hint 与 defaults 注释同时含 `workload` 与 `override`，
       且不再出现 `backup` 描述；
       `title_system`/`task_label`/`title_card`/`title_fork` 在 manifest 中均为
       `scope=request`，系统提示 prompt-surface 快照字节不变（无需重新生成）；
       两个既有测试套件全绿
```

Evidence: `sprints/sprint-5-tool-card-titles-and-docs/evidence/AS-5.txt`

## Rubric Detail

| Axis | Weight | Threshold | Scoring notes |
|---|---|---|---|
| Functionality | high | TBD in Negotiation | Binary-ish: every AS passes (pytest) or the sprint fails |
| Craft | std | TBD in Negotiation | Deterministic tests, cache/ids isolated, no sleeps/network, reuses existing suites |
| Consistency | std | TBD in Negotiation | No behavioral drift in `card_titles()`/frames/metadata; docs match D3/D4/D9/D13 |
| Documentation | low | TBD in Negotiation | Docs/comments accurate, complete, no retired protocol restated |

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
  写成现行契约，也不得为工具卡另造一套协议。改动 configuration.md 的标题段落时，
  顺手把该段里 `json_schema` 的旧表述改为与标记协议一致的说法（只改描述，不改行为）。
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
  断言计数为 0；不要 patch `tools.eval_card_metadata.derive_title`（那会绕过被测路径）。
- **缓存**：`tools/eval_card_metadata._cache` 是模块级、按 `(session_id, tool_call_id, index)`
  键控；每个用例用唯一 id 或在 setup 中 `_cache.clear()`，否则跨用例污染会伪造"路由无影响"。
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
- **文档断言的粒度**：用文件文本断言（`read_text` + 关键词/段落切片），不要渲染
  website 或起 mkdocs；关键词大小写不敏感。四个 prompt key 与
  `title-marker-instruction.md` 用精确字符串匹配。
- **范围边界**：不做会话卡片/fork（sprint 4）、不做 `agent_session.title`、不做子代理标签与
  `agent_message` 寻址、不做 schema 迁移、不改 ADR-17 漂移阈值、不新增配置键。工具卡标题的
  唯一所有者仍是 ADR-4 D13 + `derive_title()`；本 sprint 只登记与对账。

## Negotiation Log

<!-- Round summaries are appended by harness-loop; contract.md freezes after status: active. -->

### Round 1

- **Generator**:
- **Evaluator**:

### Round 2

- **Generator**:
- **Evaluator**:

### Round 3

- **Generator**:
- **Evaluator**:

### Ruling

- **Planner**:

## Sprint Outcome

- **Final iteration**:
- **Last commit**:
- **Aborted reason** (if any):
- **PR**:

