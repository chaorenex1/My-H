---
sprint: 1
feature: title-protocol
bundling: split
bundled_with: []
generator_backend: omp
generator_backend_reason: "user 已确认 omp；P0 为提示词注册与解析契约，UI-neutral，host runtime 可直接跑既有 pytest 与 prompt-surface 快照"
goal: 命名协议唯一化——注册命名提示词，冻结 `<title>` 标记解析契约，退役 JSON schema 路径
acceptance_scenarios:
  - id: AS-1
    text: "Manifest 以 `scope=request` 注册 `title_system`（context key `title_language`）与 `task_label`（无变量），两者在 `StrictUndefined` 下可渲染，未注册键被拒绝。"
  - id: AS-2
    text: "`title-system.md` 携带 Jinja 语言行，`title_language` 为空时不输出该行、非空时输出 `- Write the title in <language>.`。"
  - id: AS-3
    text: "标记解析器接受 `<title>…</title>`、`<title/>`、`<title>none</title>`（终止）、无标签散文单行与多行输出的首个非空行。"
  - id: AS-4
    text: "共享守卫去除引号、`Title:` 前缀与句末标点，超过 80 字符或 12 词以及零词输出被拒绝（不截断），`<think>` 块先被剥离。"
  - id: AS-5
    text: "JSON schema 标题路径（`extra_body.response_format`、`_extract_json_title`、`_title_from_reasoning`、`_is_prompt_example_echo`）在命名路径上无调用点，`sessions retitle-skills` 复用同一解析器，`title-marker-instruction.md` 保持未注册。"
rubric:
  - axis: Functionality
    weight: high
    threshold: "?"
    description: All acceptance scenarios pass end-to-end via the sprint-local pytest script
  - axis: Craft
    weight: std
    threshold: "?"
    description: Code is readable, tested, and follows project conventions
  - axis: Consistency
    weight: std
    threshold: "?"
    description: Manifest/renderer/parser contracts match ADR-19 and ADR-21 D4/D5
  - axis: Documentation
    weight: low
    threshold: "?"
    description: Docstrings and prompt-template registration docs match the shipped behavior
max_iterations: "?"
status: pending-negotiation
---

# Sprint 1 Contract — title-protocol

## Acceptance Scenarios

### AS-1: `title_system` / `task_label` 以请求级提示词注册

```
Given  `agent/prompts/system/manifest.json` 尚未注册任何命名提示词
When   Generator 增加 `title_system`（`scope: request`，`context_keys: ["title_language"]`，
       `context_types: {"title_language": "str"}`）与 `task_label`（`scope: request`，无变量），
       pytest 在 `title_language=""` 与非空值（如 `"French"`）两种字节下调用
       `renderer.render_request("title_system", {"title_language": ...})`，
       以 `render_request("task_label", {})` 渲染任务标签
Then   两种 `title_language` 字节都渲染成功且返回非空文本；
       `task_label` 渲染不含未声明变量残留；
       `render_request("title_system", {})` 因 `StrictUndefined` 抛 `PromptSectionError`
       （缺失变量被拒绝），`render_request("does-not-exist", {})` 抛
       `unknown prompt section`，`title-marker-instruction` 不在 manifest 键集合中
```

Evidence: `sprints/sprint-1-title-protocol/evidence/AS-1.txt`

### AS-2: `title-system.md` 的 Jinja 语言行

```
Given  `agent/prompts/system/title-system.md` 当前没有语言行
When   Generator 在模板末尾追加
       `{% if title_language %}- Write the title in {{ title_language }}.{% endif %}`
       并渲染该模板
Then   `title_language=""` 的输出逐字节等于追加前模板渲染结果（无空行、无占位文本）；
       `title_language="French"` 的输出以 `- Write the title in French.` 结尾，
       且该行在空值变体中完全不出现
```

Evidence: `sprints/sprint-1-title-protocol/evidence/AS-2.txt`

### AS-3: 单一 `<title>` 标记解析契约

```
Given  命名服务已改为以标记提示词请求模型
When   解析器接收以下模型输出：
       `<title>Fix retry queue</title>`、
       `<title/>`、
       `<title>none</title>`、
       无标签散文 `Fix retry queue`、
       多行 `\n  \n<title>Fix retry queue</title>\nextra`
Then   前三者分别得到标题 `Fix retry queue`、无标题且标记“本次命名终止”、
       无标题且标记“本次命名终止”；无标签散文得到 `Fix retry queue`；
       多行输出只取首个非空行（`extra` 被丢弃）
```

Evidence: `sprints/sprint-1-title-protocol/evidence/AS-3.txt`

### AS-4: 共享守卫拒绝答案型输出而非截断

```
Given  解析器已从模型输出取出候选标题
When   候选标题分别带引号、带 `Title:` 前缀、带句末标点，
       或长度为 81 字符、词数为 13、词数为 0，
       或在解析前被 `<think>…</think>` 包裹
Then   引号、`Title:` 前缀与句末标点被剥离后得到干净标题；
       81 字符与 13 词输出返回“无标题/拒绝”，且不产生 77 字符截断结果；
       零词输出（空白/纯符号）被拒绝；
       `<think>` 块先被剥离，其后的 `<title>` 仍被正确解析，思考文本不会进入标题
```

Evidence: `sprints/sprint-1-title-protocol/evidence/AS-4.txt`

### AS-5: JSON schema 路径退役且消费者同协议

```
Given  仓库仍存在 `_TITLE_RESPONSE_FORMAT` / `_extract_json_title` /
       `_title_from_reasoning` / `_is_prompt_example_echo` 及其调用点
When   Generator 删除这些符号并把命名提示词改为 `render_request("title_system" | "task_label", ...)`，
       `generate_title()` 不再向 `call_llm` 传 `extra_body.response_format`
Then   AST/源码扫描显示四个符号在命名路径上无定义且无调用点；
       spy `call_llm` 捕获的命名请求 kwargs 不含 `extra_body` 或 `response_format`；
       `hermes sessions retitle-skills` 的代码路径仍调用 `generate_title`（同一解析器），
       不再单独构造 JSON 请求；
       `title-marker-instruction.md` 不在 manifest 中注册
```

Evidence: `sprints/sprint-1-title-protocol/evidence/AS-5.txt`

## Rubric Detail

| Axis | Weight | Threshold | Scoring notes |
|---|---|---|---|
| Functionality | high | TBD in Negotiation | Binary-ish: every AS passes or sprint fails |
| Craft | std | TBD in Negotiation | Parser/guard unit tests, no dead symbols, follows `AGENTS.md` |
| Consistency | std | TBD in Negotiation | ADR-19 manifest contract + ADR-21 D4/D5 exactly; no legacy aliases |
| Documentation | low | TBD in Negotiation | Docstrings / `docs/prompt-system.md` registration list not contradicted |

Total pass condition: **every axis meets its threshold**. Weight only affects
Evaluator's ordering when reporting failures (high-weight fails reported first).

## Notes for Generator & Evaluator

- 回归面：`tests/agent/test_title_generator.py` 与 `tests/agent/test_prompt_sections.py`
  是既有回归套件；本 sprint 的 sprint-local pytest 脚本必须覆盖上述 5 个 AS，并让旧的
  JSON 契约测试改写为标记契约测试，而不是保留两套断言。
- Prompt-surface 快照：新的请求级模板会改变渲染结果时，必须用
  `scripts/run-in-hermes-env python scripts/ci/prompt_surface.py render` 重新生成
  `tests/fixtures/prompt_surface/`，禁止手工编辑快照文件。
- 范围边界：本 sprint 不做 fork / 卡片（`title-card.md`、`title-fork.md`）接线，也不做
  `title_model_plan()`、`agent_session.title` 列、子代理标签与 `agent_message` 寻址
  （均属 P1–P3）。`title-marker-instruction.md` 保持仅存档、不注册、不改写。
- `task_label` 在本 sprint 只完成注册与渲染契约；把它接到子代理创建路径是 sprint 3 的工作。
- 测试入口一律 `scripts/run_tests.sh <test paths> [-- pytest 参数]`；sprint-local 的
  `eval-<feature>.sh` 调用该 runner 并保留输出/退出码，禁止裸 `pytest`（`AGENTS.md`、
  `docs/coding-rules.md`）。

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

