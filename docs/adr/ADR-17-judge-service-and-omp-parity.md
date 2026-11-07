# ADR-17：判定器作为一等旁路服务（omp 全量对齐与外部消费者）

**状态**：已决定（2026-11-06）；**P0–P5 已实现；P6 未实现**。分阶段：**P0** 契约与 fixture；
**P1** 问题库/渲染器/标签；**P2** 文本后端对齐；**P3** 候选链/目录/凭证/成本；
**P4** 原生对齐与通用 RPC；**P5** 消费者；**P6** 面板/进度。

**决策摘要**：

1. 判定器是一等旁路服务。外部消费者直接传 `state` / `questions` / `images` / `contexts`，
   拿回结构化答案；不进 transcript、不改 system prompt、不是模型工具。
2. 问题定义与文本提示词落文件；`criteria: {}` 用调用方标签补齐；instructions 按题上下文渲染。
3. 文本后端对齐 omp：`submit_judgment` 重试工具、显式参数、按后端重试、逐次 usage、
   提示词字节一致。
4. 候选链对齐 omp：catalog kind、native-only 尾、`default` 兜底、TTL、401/403 刷新/轮换、
   冷却、成本门。
5. 原生对齐：assets、按模型 bounds、`requestModelId`、分片、usage 表、缓存键。
6. 全部消费者落地：TTSR、eval 桥、auto-thinking、auto-prewalk、auto-graph、
   title drift、unexpected-stop、git staging、P5 面板、batch progress。

**关联**：ADR-11（本 ADR 取代其 D3/D4/D5/D6/D12/D15 与“不移植”清单的相关行）、
ADR-12（D5 prewalk / D6 auto-graph 作为本 ADR 的消费者）、ADR-4 D11.5（eval
`__judge__`/`__judge_batch__`）、ADR-6（TTSR）；`agent/judgment/`、
`agent/auxiliary_client.py`、`agent/auxiliary_task_config.py`、`hermes_cli/aux_slots.py`、
`hermes_cli/config_defaults_judgment.py`、`hermes_cli/model_catalog.py`、
`agent/ttsr_runtime.py`、`agent/title_generator.py`、
`agent/turn_stop_gates.py`、`web/src/pages/`。

**取证口径**：omp 本地 checkout `C:\Users\zarag\projects\oh-my-pi`（HEAD
`6a86df8539fccf3ad7818081266e554022fc21bc`，`@oh-my-pi/pi-coding-agent` 18.14.0）；
Hermes 当前工作树（2026-11-06）。`omp-ui/node_modules` 只作为已安装客户端行为证据，
不当作上游源码。

**范围**：把 `agent/judgment/` 从“ADR-11 裁剪过的旁路原语”升级为**一等判定服务**：
问题库、文本/原生全量对齐、候选链/目录/凭证/成本、通用 RPC/CLI、全部 omp 消费者。
不新增 transcript 行；不新增通用 `judge` 模型工具；不改主 provider 路由；消息平台、
Desktop、ACP 不设计。

**取代关系**：

| 旧决定 | 本 ADR |
|---|---|
| ADR-11 D3：不引入 `submit_judgment` | 实现：重试带强工具，从 tool call 取答案 |
| ADR-11 D4：401/402/403 只冷却 | 加 `withAuth`：刷新/轮换后重试，耗尽再冷却 5 分钟 |
| ADR-11 D5：无 `default` 角色兜底、无链 TTL | `default` 兜底、链 TTL 5 分钟，按 omp 顺序 |
| ADR-11 D5：内联模板，YAML 字节不要求 | 模板落文件；提示词字节与 omp 18.14.0 对齐 |
| ADR-11 §2/D4：显式路由，无 catalog kind | 加 catalog kind 与默认链；路由仍可显式覆盖 |
| ADR-11 D5：`has_native_judge` 只看 native 路由 | 改为链首候选是否为 native |
| ADR-11 D6：文本依赖 `call_llm` 的 fallback | 文本候选禁用内部 fallback；回退由链负责 |
| ADR-11 678：TTSR 判定失败只记日志 | judge API 抛错；TTSR 消费点 catch（与 omp 同构） |
| ADR-11 D6：无 usage 表 | 加 usage 表；journal 仍 DELETE（`hermes_state_wal.py:265-275` 平台例外） |
| ADR-11 D12：不移植 auto-thinking / title drift / unexpected-stop / git staging | 全部纳入 |
| ADR-11 D15：仅 base64、无 assets | 加 assets 与按模型 bounds |
| ADR-4 D11.5：eval 桥不实现 | P4 实现 `__judge__`/`__judge_batch__` |
| ADR-12 D5/D6：pending | 本 ADR 是前置；P1 完成后解锁，D5/D6 产品面不变 |

问题库、渲染器和 `criteria: {}` 标签不依赖 ADR-12 的裁决：本 ADR 自己拥有它们。

---

## 问题

### 1. 当前实现把 omp 行为裁成了“偏差/范围外”

ADR-11 的裁剪在当时减少了成本与风险，但现在与需求冲突：

- 文本后端没有 `submit_judgment`，小模型解析失败时只能重发同一类提示词；
- 提示词是内联常量，state 的 YAML/浮点字节与 omp 不同；
- 候选链没有 catalog kind、`default` 兜底、TTL、凭证刷新和成本门；
- 缓存没有 usage 表，缓存键与 omp 不同；
- auto-thinking、title drift、unexpected-stop、git staging 被判为不移植；
- P6 面板、batch progress 未实现（eval 桥已随 P4/P5 落地）。

### 2. 外部消费者没有入口

- 没有随包问题库；`agent/prompts` 不存在，`agent/judgment/prompts/` 里的 JSON 是
  `intent/plans` 决策表，不是通用问题文件。
- `questions_from_file()` 只服务 `hermes judgment ask` 的 `$HERMES_HOME/judgment/eval/questions/`
  用户文件；没有按名字解析随包文件。
- `criteria: {}` 加载成空 `ChoiceQuestion`，但 `validate_request` 在 judge 时拒绝
  （`agent/judgment/types.py:311-313`）。omp `auto-graph.json` 的 `label`/`rowPolarity`
  正是这种形状，移植后会“加载成功、每次调用失败”。
- instructions 不做模板渲染（`agent/judgment/text.py:254` 原样插入）。omp `renderQuestion`
  支持 `{{column}}`、`{{#each columns}}`、`{{#if repeats}}` 等；不渲染会把字面量发给模型。
- 没有 host RPC / Python 公共函数让外部消费者直接传 `state`/`questions`/`images`。

### 3. 两个行为差异

- **本地后端**：Hermes 继承 `parse_retries=2`，并在重试时把输出预算从 16 提到 1024
  （`agent/judgment/local.py:114-119`）；omp `LocalTextBackend` 没有 `parseRetries`
  （即 0），预算按模型是否推理选 16/1024。
- **TTSR 上限**：omp 按 Jev token 截到 32,000（`export/ttsr.ts:64-84`，Jev 约 33k 上限）；
  Hermes 按字符截到 56,000（`agent/ttsr_runtime.py:34`）。CJK 约 1 char/token，
  56k 字符会越过 Jev 限制并增加成本。

### 4. 证据

- omp 文本桥：`packages/ai/src/judgment/text.ts`、`chat.ts`、`openai-decisions.ts`、
  `systemone.ts`、`question-file.ts`；链：`packages/coding-agent/src/judgment/index.ts`；
  缓存：`.../judgment/cache.ts`。
- Hermes 现状：`agent/judgment/{chain,text,chat,local,native,wires,cache,questions,types}.py`；
  已接消费者：`agent/ttsr_runtime.py`。
- 同一请求的文本提示词差分（已用 `omp-ui/node_modules` 的 18.14.0 验证，未构建 omp）：
  - system：omp 无多余空行、无结尾换行；Hermes 多两个空行并带结尾换行。
  - user：omp 的 `1.0` 渲染为 `1`、YAML 序列缩进 2 格；Hermes 为 `1.0`、0 格缩进。
  - 原因：`_strip_standalone_lines`、缺少 post-render `format()`、PyYAML vs Bun YAML。

---

## 选项

| 选项 | 结论 | 理由 |
|---|---|---|
| A. 保持 ADR-11，仅补消费者 | 否 | 消费者仍没有共享问题协议；每次新消费者都要手写 `Question` |
| B. 全量对齐 + 一等服务（本 ADR） | **采用** | 与 omp 语义一致；问题文件/模板/链/缓存/账本只维护一套 |
| C. 每个消费者各建判定器 | 否 | 重复协议、缓存、账本、凭证和冷却；成本不可控 |

---

## 决策

### D1. 判定器是旁路服务，不是 transcript 行或模型工具

- 公共契约：
  - `judge(request, *, cancel_event, purpose, budget_usd) -> JudgmentResult`
  - `judge_batch(items, *, min_ok, max_concurrency) -> BatchResult`
- `JudgmentRequest`：`state`、`questions`、`images`、`purpose`；问题在加载时已完成模板渲染与
  标签补齐，judge 不再接受半成品。
- `JudgmentResult`：`kind`（`native`/`local`/`online`）、`api`、`provider`、`model`、
  `answers`、`usage`、`cost_usd`、`cached_questions`。
- 一次 `judge()` 只做旁路调用；不追加消息、不改 system prompt、不注册模型工具、不发 UI 事件、
  不启动 turn。调用方拥有调度、超时、预算和生命周期。
- 不新增通用 `judge` 模型工具：模型不能传任意 questions/state。只保留受限产品工具
  （`git_stage` 等），它们自己构造问题。

### D2. 问题库：文件、形状推断、标签、模板

- **路径**：`agent/prompts/judge/*.json`（与需求一致）。`pyproject.toml` 的 package-data
  加 `agent = ["prompts/judge/*.json", "judgment/prompts/*.md"]`。
- **加载**：
  - `load_questions(name, *, contexts=None, labels=None) -> dict[str, Question]`
  - 按名字解析随包文件；绝对路径/用户文件仍可显式传入。
  - 形状推断沿用 `agent/judgment/questions.py`：缺 `criteria` → `noul`；`true`/`false`
    仅两项 → `noul` 带 rubric；字符串数组 ≥2 → `score`；其他对象 → `choice`；
    `image` 索引 → `box`。
  - `criteria: {}`：必须由 `labels[question_id]` 补齐；缺标签在**加载时**报
    `JudgmentValidationError`，不再等到 judge。
  - `contexts[question_id]`：渲染该题 instructions；含 `{{...}}` 但无上下文时在加载时报错。
- **渲染**：`render_question(question, context)` 用与文本桥相同的 Handlebars 子集；
  不支持 `{{#with}}`/自定义 helper；未解析变量 fail fast（omp `strict: false` 会渲染空串，
  这里收紧，避免把字面量发给模型）。
- **随包文件**：`auto-thinking.json`、`auto-prewalk.json`、`auto-graph.json`、
  `title-drift.json`、`unexpected-stop.json`、`git-stage.json`。
- 现有决策表（`agent/judgment/decision.py`）不在本 ADR 范围，保持现状。

### D3. 文本后端：文件模板 + 字节对齐 + 工具重试

- 模板落文件：`agent/judgment/prompts/text-judge.md`、`text-judge-state.md`、
  `text-judge-retry.md`、`judgment-local.md`。
- 渲染器必须产生与 omp 18.14.0 **逐字节相同**的 system/user/retry：
  - 保留现有 Handlebars 子集，并移植 post-render `format()`（trimEnd、折叠 2+ 空行、
    Markdown 表格压缩、code-block 感知）。
  - state 标量按 JS `String()` 语义（布尔小写、整数值浮点不带 `.0`、`null`）。
  - 嵌套 YAML 用与 Bun `YAML.stringify(value, null, 2)` 一致的发射器：2 空格序列缩进、
    字符串引号规则、无尾随换行。实现为小的 Python 发射器，用 golden fixture 锁定；
    不依赖 PyYAML 的默认排版。
  - P1 验收：同一请求的 system/user/retry 与 omp fixture 字节一致。
- 重试：
  - 首次尝试不带 tools。
  - 重试把 system 换成 retry 模板，并带 `submit_judgment` 工具；
    `tool_choice` 强制该工具，从 tool call 的 `answer` 取答案。
  - `call_llm` 增加可选的 `tool_choice` 参数（纯增量，其他调用方不传即不变）。
- 参数：`temperature=0`；显式 `reasoning_config={"enabled": False}`（不再依赖
  `auxiliary.judge.reasoning_effort`）；`max_tokens=4096`（可配，但低于 1024 时告警）；
  `parse_retries` 按后端：chat 2，local 0。
- 响应：
  - 只取 text block，按空格拼接；重试时把 `submit_judgment.answer` 并入。
  - 检查 `stopReason`：aborted/error 直接抛。
  - 每次完成尝试（含瞬态失败）通过 `on_attempt` 回调记账；最终
    `JudgmentResult.usage` 只汇总给调用方，不再二次记账。
- 不回退到 `call_llm` 的 provider/model fallback：文本候选固定一个 provider/model，
  `fallback_chain`/`fallback_chains` 在该次调用中禁用；回退由链负责。
- `api` 用后端 api（如 `openai-completions`），新增 `kind="online"`；消费者用 `kind`
  判 `uncalibrated`，不再依赖 `api == "text"` 哨兵。

### D4. 候选链：catalog kind、native-only 尾、`default` 兜底、TTL、凭证、成本

- **catalog**：新增判定模型元数据（`hermes_cli/judgment_catalog.py` 或
  `model_catalog` 扩展）：`kind`（native/local/online）、`api`、`provider`、`model`、
  `base_url`、`api_key_env`、`request_model_id`、`max_questions`、`image_input`、
  `boxes`。
- **链条目语法**：`native:<id>` / `local:<id>` / `<role>`；`<id>` 必须匹配
  `judgment.models[].id`（默认 `provider/model`，可显式覆盖）；`<role>` 是角色名，
  不带 `online:` 前缀。
- **链**：`judgment.chain` 默认
  `native:typesafe/jev-1.14 → native:openrouter/typesafe-jev-1.14 → tiny → smol →
  default`。角色名解析到同名 `auxiliary.<role>` 槽位：
  - `tiny` → `auxiliary.tiny`（P3 新增；默认复制 `title_generation` 的
    provider/model/timeout）
  - `smol` → `auxiliary.smol`（已有）
  - `default` → `auxiliary.default`（P3 新增；默认 `{provider: auto, model: ""}`，
    即主模型）
  - `memory` / `commit` 不在默认链；需要时分别映射到
    `auxiliary.background_review` / `auxiliary.commit_message`。
  - `auxiliary.judge` 保留为 `mode: text` / 兼容入口，默认链不再列 `judge`。
  - 当前 `aux_slots.py` 只有 `smol` 是已有同名槽位；`tiny` / `default` 由 P3 新增，
    不假设它们已存在。
- **规则**：
  - 从第一个 native 候选起，只保留 native（native-only 尾）。
  - 没有 native 时走 `tiny → smol → default`；有 native 但全部失败时，默认不追加文本，
    `text_after_native: true` 才追加。
  - 解析计划（chain → 目录/角色条目）按 `(profile home, 配置指纹)` 缓存 5 分钟；
    配置改动改指纹即重解析。凭证与冷却按候选逐次解析，登录/轮换在下一次判定生效。
    omp 的 `CANDIDATE_TTL_MS` 是 1s（只影响远程目录发现）；Hermes 无远程目录，
    取 5 分钟避免多 profile 进程重复解析。5 分钟同时是拒绝冷却的值，两者是不同旋钮。
  - `has_native_judge()` = 解析后的**第一个**候选是否为 native；不联网。
- **凭证**：401/403 先刷新/轮换（OpenAI/OpenRouter 走现有凭证池；
  TypeSafe/Cloudflare 环境变量无兄弟凭证，直接失败），重试一次；仍失败则冷却 5 分钟。
  402 不重试，直接冷却。
- **冷却**：进程内、按 profile home 键隔离。
- **成本门**：`judgment.cost_gate`（默认单次 $0.05、单会话 $0.50，可配）；
  每个候选 `admit()`，成功后 `record()`；失败的提示词尝试把已计费 usage 记入（原生分片
  仍只走 session ledger）；超限抛 `JudgmentBudgetExceeded`，
  消费者可降级到本地/关闭，不得静默换昂贵模型。P3 的 spend 桶是进程内、
  按 profile home + session id 隔离；外部 RPC 的 `budget_usd` 由 P4 的 host 入口强制。
- **P3 配置来源**：`resolve_judge()` / `has_native_judge()` / `doctor_routes()` 仍读取
  当前 profile 的 live config；冻结会话配置的绑定随 P4 的 host 入口统一处理。
- **本地**：`kind=local`；合并 system+user；`guard_state=false`；无图像/box；
  `parse_retries=0`；输出预算按模型是否推理选 16/1024，不做重试升级。
- **图像/box 过滤**：按候选 `image_input`、`boxes`、`max_questions` 过滤；
  无兼容候选显式报错。

### D5. 原生后端、缓存与账本

- 保留四种 wire：`typesafe`、`openrouter-decisions`、`openai-decisions`、
  `workers-ai-systemone`；端点/编码/解码沿用 `agent/judgment/wires.py`。
- **assets**：`JudgmentImageAsset` 支持 base64 与本地文件路径（cwd / `$HERMES_HOME`
  下），不支持远程 URL（避免 SSRF）；TypeSafe 校验 state/instructions/criteria 引用
  每个 asset；OpenRouter/OpenAI/Workers 按各自编码。
- **按模型 bounds**：从 catalog 的 `image_input` 取 `maxCount`/`maxBytes`/`minSide`/
  `maxSide`/`maxPixels` 等；wire 级上限作为兜底；Jev 仍 fit/pad，Workers/OpenAI 拒绝。
- **`requestModelId`**：wire 用 catalog 的请求模型 id，账本/缓存用配置 id。
- **重试**：10s/attempt、3 attempts、408/429/5xx/transport、Retry-After 上限 5s、
  500ms 指数退避；401/403 先刷新/轮换，耗尽冷却 5 分钟；402 直接冷却。
- **分片**：`max_questions`（或端点默认 64）超过时分片，最多 4 并发；
  `allSettled` 语义：任一分片失败即停止派发，已成功分片照常记账，
  通过 `on_partial_usage` 上报；结果 model 取第一个成功分片。
- **缓存**：
  - 仍只缓存 native 答案；文本/local 不缓存。
  - 键：state 的 canonical JSON sha256；answer 键 `(state, model, name, type,
    instruction, criteria)`；`model` 用 `provider/id`（对齐 omp；不再用 `api:` 前缀）；
    box 的 criteria 存 `null`；image identity 的 `detail` 未设时按 `"auto"`。
  - 新增 `usage` 表（仅 native）：每次计费 HTTP 尝试一行（provider/model/tokens/price）；
    纯缓存命中不写；文本/local 仍只走 session ledger，不写该表。
  - 缓存失败只记 warning，判定继续。
  - **journal 仍 DELETE**：`hermes_state_wal.py:265-275` 要求新库不要用 WAL
    （当前捆绑 SQLite 3.50.4 的 WAL-reset 风险）；`usage` 表与答案/审计同库，
    DELETE 下语义不变。这是平台安全例外，不改变本 ADR 的答案/账本/链语义。
- **答案校验**：只验 discriminant 与必需字段；概率 map 原样透传（不补零、不裁剪），
  与 omp 的“trusted past discriminant”一致。问题本身仍由 `validate_request` 校验。

### D6. 通用外部入口：Python、RPC、CLI

- **Python**：`Judge` 协议（`judge` / `judge_batch`）是唯一对象契约；模块级
  `agent.judgment.judge()` / `judge_batch()` 是便捷包装。外部消费者直接传
  `state`、`questions`（内联或文件名）、`contexts`、`labels`、`images`、`purpose`、
  `budget_usd`、`timeout_ms`。
- **一个 host API**：`judge` / `judge_batch` 是唯一实现与语义。
  - `serve_rpc` 的 `__judge__` / `__judge_batch__` 是 eval 别名（ADR-4 D11.5）。
  - `tui_gateway` 的 `judgment.judge` / `judgment.judge_batch` 是本地代理，仅 local
    authority，不暴露到远程平台。
  - 三者共用同一 schema、预算、账本与错误类型；不另造一套。
  - 请求 schema：`{state, questions? | questions_file?, contexts?, labels?, images?,
    purpose?, budget_usd?, timeout_ms?}`；响应：`{answers, kind, api, provider, model,
    usage, cost_usd, cached_questions}`。
- **CLI**：`hermes judgment ask` 增加 `--json`、`--questions <name|path>`、`--context
  k=v`、`--budget`；新增 `hermes judgment batch --file`。
- **批量**：host 拥有整轮 run；有界 fan-out（4）、每项一次重试、`min_ok`、
  `drain` 分批取 settled；单 item 失败返回 `error` 不抛；usage 记同一账本。
- 外部消费者不直接持有 provider 凭证；由 `resolve_judge` 解析。
- **P0 schema**：`agent/judgment/schemas/judgment-v1.schema.json` 固定 `JudgeRequest`、
  `JudgeResult`、`BatchRequest`、`BatchResult`。请求 `state` 为 string/object/array，且
  `questions` 与 `questions_file` 必须且只能提供一个。
  `judge_batch` 的 `items` 是 `{key, state, images?}`；问题定义与 `contexts`/`labels` 共用一份，
  `max_concurrency` 为 1–4。返回的 `results` 带原 `key` 且与输入同序；每项为
  `{key, ok: true, result}` 或 `{key, ok: false, error: {type, message}}`，并返回 `min_ok_met`。
- **P0 fixtures/package data**：可重放 fixture 锁定 omp 18.14.0 的问题形状推断；schema
  和 `agent/prompts/judge/*.json`、`agent/judgment/prompts/*.md` 按包资源声明，打入 wheel。


### D7. 消费者（全部落地）

| 消费者 | 入口 | 问题文件 | 语义 |
|---|---|---|---|
| TTSR `question` | `agent/ttsr_runtime.py` | 规则 frontmatter | 完成输出后判定，`noul` + 0.7；内容按 32,000 Jev tokens 截断（无 tokenizer 时用 24,000 字符保守上限） |
| eval `judge()`/`judge_batch()` | `serve_rpc` | 调用方 | `__judge__`/`__judge_batch__`；host batch、`min_ok`、drain |
| auto-thinking | 新 classifier | `auto-thinking.json` | 请求前选 `low/medium/high/xhigh/max`；`auto` 仅 native，`on` 允许文本 |
| auto-prewalk | ADR-12 D5 | `auto-prewalk.json` | `prewalk` 概率 ≥0.70 且目标可解析才武装；默认 off |
| auto-graph | ADR-12 D6 | `auto-graph.json` | `table_chart.plan`；contexts/labels 由表格提供；15s 超时；无 judge 回退本地 |
| title drift | `agent/title_generator.py` | `title-drift.json` | 回合后检测漂移，必要时重生成标题；失败只记日志 |
| unexpected-stop | `agent/turn_stop_gates.py` | `unexpected-stop.json` | 回合异常结束时分类；可配置一次续跑 |
| git staging | 新 `hermes git stage` / RPC | `git-stage.json` | 对 hunk 判定是否暂存；只写 index，不提交；omp TUI 不移植 |
| P5 面板 | dashboard `/judgment` | — | `runs` 过滤/成本/延迟；不存正文 |
| batch progress | `judgment.batch_progress` | — | `{id,intent,done,total,failed,cost}`；不新增 transcript 行 |

- TTSR 判定失败：judge API 抛错；TTSR 消费点（`_judge_outputs`）catch 并 `logger.warn`，
  与 omp `ttsr-coordinator.ts:575-580` 的 `.catch` 同构；不伪造通知、不改写回合结果。
  引擎不捕获；捕获策略属于消费者。

### D8. 配置

```yaml
judgment:
  chain: [native:typesafe/jev-1.14, native:openrouter/typesafe-jev-1.14,
          tiny, smol, default]
  models:
    - {id: typesafe/jev-1.14, kind: native, api: typesafe, provider: typesafe,
       model: jev-1.14.0}
    - {id: openrouter/typesafe-jev-1.14, kind: native, api: openrouter-decisions,
       provider: openrouter, model: typesafe/jev-1.14}
  text:
    parse_retries: 2
    max_tokens: 4096
    disable_reasoning: true
  local:
    parse_retries: 0
    answer_max_tokens: 16
    reasoning_max_tokens: 1024
    merge_prompt: true
  cache: {enabled: true, max_states: 50000, usage: true}
  audit: {enabled: true, max_runs: 20000}
  cost_gate: {max_usd_per_call: 0.05, max_usd_per_session: 0.50}
  images:
    enabled: true
    assets: {allow_local: true, allow_remote: false, max_bytes: 16777216}
  tools: {enabled: auto}
```

- 角色映射：`tiny` → `auxiliary.tiny`（P3 新增，默认复制 `title_generation` 的
  provider/model/timeout）；`smol` → `auxiliary.smol`；`default` → `auxiliary.default`
  （P3 新增，默认 `{provider: auto, model: ""}`，即主模型）。
  `memory`/`commit` 不在默认链，分别对应 `auxiliary.background_review`/
  `auxiliary.commit_message`；`auxiliary.judge` 仅保留为 `mode: text`/兼容入口。
  内置 aux 槽位没有 `inherit_from`；新增的 `tiny`/`default` 用显式默认块，
  不依赖继承。
- `judgment.mode` 保留兼容：`auto` = chain，`native` = native-only，
  `text` = 文本角色链（`tiny → smol → default`；`auxiliary.judge` 若显式配置则作为
  第一个文本候选），`off` = 无候选。
- 默认 native 路由为空时只有显式调用方触发文本；TTSR 的 `auto` 仍用
  `has_native_judge()` 门控，避免后台静默花主模型的钱。

### D9. 安全与边界

- 判定 state 是不可信数据；`guard_state` 默认 true，只有本地分类器可关。
- 原生 egress 走 `redact_for_egress`；redaction 失败拒绝发送。
- assets 只允许 base64 与本地路径（cwd / `$HERMES_HOME`）；默认不抓远程 URL。
- RPC 仅 local authority；不暴露到 Telegram/Discord/Slack 等远程平台。
- 不存 state/问题/答案正文到 `runs`；只存哈希、计数、成本、延迟。
- 外部消费者不能传任意 provider 凭证；预算由 host 校验。
- 不新增通用 `judge` 模型工具；`git_stage` 是受限仅 index 工具。

### D10. 分阶段与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 契约、schema、包路径、omp fixture 语料 | `judge`/`judge_batch` schema 固定；`agent/prompts/judge/*.json` 与 `agent/judgment/prompts/*.md` 可打包；fixture 可重放 |
| P1 | 问题库、`criteria: {}` 标签、`render_question`、文本模板文件、字节对齐渲染器 | golden fixture 的 system/user/retry 与 omp 18.14.0 逐字节一致；auto-graph.json 能加载并渲染 |
| P2 | `submit_judgment` + `tool_choice`、显式参数、无内部 fallback、stop/usage | 首次不可解析→重试工具答案；失败尝试记账；无 provider 切换 |
| P3 | catalog/chain、同名槽位（`tiny`/`default`）、native-only 尾、`default` 兜底、TTL、`withAuth`、冷却、成本门、local 对齐 | 链顺序/门控/冷却/预算测试；同名槽位解析；local 0 重试 + 16/1024 |
| P4 | assets、按模型 bounds、`requestModelId`、分片、usage 表、通用 RPC/CLI | 四 wire 契约；分片 partial usage；`judge`/`judge_batch` 与 `__judge__` 别名 |
| P5 | TTSR 32k tokens、eval 桥、auto-thinking、prewalk、auto-graph、title drift、unexpected-stop、git staging | 每个消费者有边界/失败/成本测试 |
| P6 | P5 面板、batch progress | 不存正文；事件只发本地 viewer |

**P5 实现备注**：

- **TTSR**：`JUDGED_CONTENT_MAX_TOKENS = 32_000` 记录 omp 预算；Python 尚无 Jev tokenizer，
  按 ADR 的过渡口径落到 24,000 字符上限（`ttsr_runtime.JUDGED_CONTENT_MAX_CHARS`），不再用 56k。
- **eval 桥**：`execute_code` 生成的 `hermes_tools` 增加 `judge()` / `judge_batch()`，走
  `__judge__` / `__judge_batch__` host op；远程文件沙箱仍拒绝（local authority）。
- **auto-thinking**：`agent.reasoning_auto`（`off|auto|on`，默认 off）、
  `reasoning_auto_max_effort`（`xhigh|max`）、`reasoning_auto_timeout_ms`（默认 4000）。
  每用户回合分类一次并固定整轮；`auto` 仅 native，`on` 允许文本；解析结果按路由 clamp。
- **prewalk**：`agent/prewalk.py` 会话状态机（`model_config.prewalk` 持久化）、
  `prewalk.arm|restart|off|status` RPC、`/prewalk` 命令与 `prewalk.armed|handoff` 事件；
  默认 `prewalk.enabled=false`、`prewalk.auto=off`；todo 成功打开 gate，首个 `write_file`/`patch`
  成功后一次性交接；auto 仅 native、阈值 0.70、4s；`/prewalk off` 抑制配置重启武装。
- **auto-graph**：`table_chart.plan` 只做 judge 精修，客户端已算好的 `guess` 作为回退；
  15s 超时；`auto` 无 native 时直接回退 local，`smart` 可用文本；markdown/答案不持久化。
- **title drift**：`auxiliary.title_generation.refresh_after_prompts`（默认 0=off）；
  只用最近 3000 字符窗口判定，`user` 标题永不改写；同源 CAS 刷新防止覆盖并发重命名。
- **unexpected-stop**：`agent.unexpected_stop_detection`（`none|mechanical|smart`，
  默认 mechanical）；只有 `smart` 对文本收尾判定，阈值 0.5，每回合一次续跑；nudge 是
  scaffolding 行，不落 SQLite。
- **git staging**：`hermes git stage "<instruction>"` 与受限 `git_stage` 工具；按 hunk 判定，
  `git apply --cached` 只写 index，永不提交、永不改 worktree。

---

## 测试

- **提示词字节**：字符串/对象/数组/嵌套/Unicode/浮点/特殊字符、单问/多问、
  choice/noul/score、retry；与 omp fixture 比对。
- **问题库**：`criteria: {}` 有/无 labels；模板有/无 contexts；坏文件在加载时报错。
- **文本**：tool retry、stop reason、逐次 usage、无 fallback、local 0 重试。
- **链**：catalog 顺序、native-only 尾、`default` 兜底、TTL、401/403 轮换、冷却、成本门。
- **原生**：四 wire、assets 引用校验、按模型 bounds、分片 partial usage、缓存键。
- **缓存/账本**：usage 行、纯缓存命中不记账、失败分片记账、box key、image detail。
- **RPC**：单次/批量、`min_ok`、超时、预算、单 item 失败不抛。
- **消费者**：TTSR 32k tokens、eval batch、auto-thinking 档位、
  prewalk 0.70、auto-graph 回退、title drift、unexpected-stop、git staging 只写 index。
- **安全**：远程 RPC 拒绝、assets 远程 URL 拒绝、redaction 失败拒绝、profile 隔离。

## 风险

- **成本**：`default` 兜底和 auto 消费者可能花主模型的钱；用成本门、默认 native 门控、
  默认关闭 auto 来约束。
- **提示词字节维护**：上游 18.14.0 一旦变化，golden fixture 会失败；按 ADR 的取证口径
  固定版本，升级时同步更新 fixture。
- **Jev tokenizer**：Python 没有 `pi-natives` 的 `countTokens`；P5 需移植 Jev 编码或
  用 24k 字符保守上限。移植前不得用 56k 字符。
- **SQLite**：usage 表 + DELETE journal；遵守 `hermes_state_wal.py` 的 WAL 约束。
- **外部 RPC**：只允许 local authority；assets 不抓远程 URL；预算由 host 强制。
- **byte parity 与 YAML emitter**：Bun YAML 的引号/缩进规则需要 fixture 驱动；
  不用 PyYAML 默认输出做“差不多”对齐。

## 附录 A：问题文件

| 文件 | 问题 | 备注 |
|---|---|---|
| `auto-thinking.json` | bucket / level / solutionSpace | `solutionSpace` 为空 criteria，调用方补 labels |
| `auto-prewalk.json` | route | choice：prewalk/primary/direct |
| `auto-graph.json` | kind/transpose/shading/order/... | `{{column}}` 等模板；`label`/`rowPolarity` 空 criteria |
| `title-drift.json` | drifted | noul/choice |
| `unexpected-stop.json` | unexpected | noul/choice |
| `git-stage.json` | stage | choice/noul，按 hunk |

## 附录 B：与 ADR-12 的关系

- ADR-12 D5（prewalk）与 D6（auto-graph）仍是产品决策；本 ADR 提供它们所需的
  问题库、模板、标签和 judge 服务。P1 完成后即可实现，不再被 ADR-11 的
  “不移植/无问题文件”阻塞。
- 问题库、渲染器和 `criteria: {}` 标签由本 ADR 拥有，不依赖 ADR-12 的裁决；ADR-12 只消费。
