# ADR-21：Agent 命名系统（标题、任务标签、卡片与子代理文件名）

**状态**：提议（2026-11-17，第四轮修订）。本版**取代**第三轮“session card/fork 不移植”的
裁决，并把卡片接为标题的子形态。分阶段：**P0** 模板与解析契约；**P1** 候选计划（路由）；
**P2** 会话标题 + 任务标签 + 子代理文件名；**P3** 会话卡片 + fork；**P4** 工具卡/文档。

**第四轮修订（本版，2026-11-17）**：

1. **路由**：会话标题默认 `fork`（主回复副回合），失败回落与 omp 同构的候选计划：
   辅助模型配置 → `tiny`/`commit`/`smol` 角色主路由 → 配置的 retry fallbackChains →
   当前会话模型（D3）。`auxiliary.title_generation` 只保留为 workload 开关与显式路由覆盖，
   不再是 tiny 的备份。
2. **协议**：候选混跑迫使协议唯一化——统一为 `<title>` 标记；内联 JSON schema 提示词、
   JSON 解析、示例 echo 守卫退役，语言规则改为模板变量（D4）。
3. **消费者**：`task-label.md` 接线为任务标签；工具卡标题、子代理 jsonl/report 文件名
   明确纳入命名服务（后者用 stage 0 派生 topic）。
4. **会话卡片与 fork**：`title-card.md`/`title-fork.md` 接线。卡片是 `session` 标题的字符串
   子形态（`<icon> <CODE>: <title>`），不新增字段；`fork` 成为默认生成器，失败回落 D3 计划。
   `/rename` 仍不移植（手工标题仍是纯用户写入），Nerd Font 图标不移植（本仓库无 glyph 目录）。

**关联**：ADR-4 D13（工具卡标题）、ADR-16 D4（`<topic>` 子代理产物）、ADR-17 P5（title drift）、
ADR-18 D10（请求级注入）、ADR-19 D5/D5-B/D10（manifest 只做注册、`scope=request` 由调用点渲染）、
ADR-20（request 模板处置与审计）、ADR-23（job/widget 标题与 job caption）、
`agent/prompts/AGENTS.md` 规则 1（提示词必须有条件入口）。

**取证口径**：Hermes 为本仓库 2026-11-14 工作树；omp 为 `C:\Users\zarag\projects\oh-my-pi`
HEAD `6a86df85`。

**范围**：

- **做**：一套命名服务（会话标题 + 卡片、任务标签、工具卡标题与子代理 jsonl/report 文件名）、
  与 omp 同构的辅助模型候选计划与 fork 生成器、单一标记协议、逐模板处置、持久化/线程/计费边界。
- **不做**：移植 omp `/rename`（手工标题仍是纯用户写入）；接线 `title-marker-instruction.md`；
  移植 Nerd Font 图标与 ONNX/MLX tiny worker；改 ADR-17 判定阈值；job/widget 标题、job caption、
  kanban/cron 接入（归 ADR-23）。

---

## 问题

### 1. 标题模型级是单点，路由与 omp 不是同一套

现状：`generate_title()` 只调 `call_llm(task="title_generation")`（`title_generator.py:507`、
`:553`），模型来自 `auxiliary.title_generation` 一个槽位；随后没有候选级联。omp 的标题生成
不是单个槽位：`getTitleModels()`（`utils/title-generator.ts:155`）展开
`collectOnlineTinyCandidates(["tiny","commit","smol"], …)`（`tiny/online-candidates.ts:116`），
再递归展开配置的 retry `fallbackChains`，最后追加当前会话模型并展开它自己的链。
Hermes 已经有这些原料——`auxiliary.tiny`/`commit_message`/`smol` 槽位
（`hermes_cli/aux_slots.py`、`config_defaults_auxiliary.py:73`）、模型键控
`auxiliary.fallback_chains` 与 per-task `fallback_chain`（`agent/auxiliary_client.py:4238`）、
当前运行时（`_normalize_main_runtime`，`:3119`）——但没有“按 omp 顺序展开成候选计划”的一层。

上一轮 ADR 把 `auxiliary.title_generation` 设计成 tiny 的“备份级”。这不是 omp 的模型，
也解释不了“三个角色都 pin 了该谁先跑”。本版删除该决策。

### 2. 五个命名提示词/模板只落地了零个消费者

`agent/prompts/system/` 下与命名有关的模板：

| 模板 | omp 消费点 | Hermes 现状 |
|---|---|---|
| `title-system.md` | tiny/在线标题模型（`tiny/title-client.ts:60`、`utils/title-generator.ts:37`） | 未注册、无调用点 |
| `task-label.md` | 子代理 UI 标签（`task/label.ts:10`、`:24`） | 未注册、无调用点 |
| `title-marker-instruction.md` | 覆盖 `TITLE_SYSTEM.md` 时追加 `<title>none</title>`（`utils/title-generator.ts:319`） | 未注册（本 ADR 定为仅存档） |
| `title-card.md` | `/rename` 与 icon 补全（`agent-session.ts:11123`） | 未注册（本 ADR 接线为自动标题的卡片请求） |
| `title-fork.md` | `fork` 生成器副回合（`agent-session.ts:10875`） | 未注册（本 ADR 接线为默认生成器） |

同时，实际跑着的标题提示词是内联 JSON：`_TITLE_PROMPT_TEMPLATE` +
`json_schema` + 语言规则 + echo 守卫（`title_generator.py:112`–`:130`、`:495`）。它与
`title-system.md` 的 `<title>` 标记协议不兼容——引入候选路由后，同一个请求可能落到 tiny
（标记）或当前模型（JSON），协议不能再跟着候选走。

### 3. 命名消费者已经散落在三个子系统

- **会话标题**：`apply_instant_title()` 写 derived（`title_generator.py:659`），后台升级为
  `llm`；漂移再生成走 `_refresh_title_if_drifted()`（`:1003`）。
- **工具卡标题**：ADR-4 D13 的 `card_titles()`（`tools/eval_card_metadata.py:108`）已经调用
  `derive_title()`，但没有人把它记为“标题系统的一个消费者”。
- **子代理文件名**：`<topic>.jsonl` / `<topic>.md` / `<topic>_ast.json` / `<topic>.json` /
  `<topic>/` 全部由 `topic` 派生（`agent/run_artifacts.py:297`–`:303`、
  `hermes_session_paths.py:344`–`:379`）；`topic` 缺省是 `user_prompts` 的首行
  （`tools/agent_tool.py:1662`、`:1362`），不是命名服务的产物。

### 4. `/rename` 仍不移植；session card 与 fork 本版接线

omp 的 `renameTitle()` 会给手工改名补 card、空标题时用模型重命名，并以 title revision/
reservation 丢弃过期结果。Hermes 的改名是纯用户写入（`sessions_cmd.py:858`、
`hermes_state_titles.py:181`），权威顺序 `derived < llm < user`；本 ADR 维持 `/rename` 不移植，
手工标题不带卡片、不触发模型。

但**自动标题**这一侧卡片与 fork 都接线：`fork` 从主回复的同一前缀跑一次短副回合（读 prompt
cache，能看到模型自己的理解），卡片直接并进标题字符串；fork 失败/超时/拒绝时回落 D3 候选
计划，再由 `title-card.md` 补一次卡片请求。卡片因此有消费者，不再只是存档模板。

---

## 决策

### D1. 命名服务：一个系统，四类消费者

| 消费者 | kind | stage 0（确定性） | stage 1（模型级：fork → 候选计划） | 产出 |
|---|---|---|---|---|
| 会话标题（可带卡片） | `session` | derived（首行，48 字上限） | 是（fork→计划，后台线程） | `sessions.title`（可带 `<icon> <CODE>: ` 前缀），`source=llm` |
| 任务标签（子代理 UI/寻址） | `task` | 无 | 是（spawn 后异步） | `agent_session.title`（地址别名 + 显示，write-once） |
| 工具卡标题（omp-ui 卡片） | `card` | 是（ADR-4 D13） | **否** | `tool.start`/`tool.title` 的 `title`/`cell_titles` |
| 子代理文件名 topic | `topic` | 是（assignment 首行，去 Markdown 头） | **否**（见 D8） | `<topic>.jsonl` / `<topic>.md` 等 |

会话卡片不是独立 kind：它是 `session` 标题的字符串子形态（`<icon> <CODE>: <title>`），
随标题生成/重生成，不新增列或渲染字段。

统一入口放在 `agent/title_generator.py`（现有 facade）：

- 公共：`derive_title()`（stage 0）、`title_model_plan()`（新，D3）、
  `generate_label(kind, text, ...)`（新，stage 1 的通用驱动）。
- 既有会话入口（`generate_title` / `maybe_auto_title` / `apply_instant_title` /
  `maybe_refresh_title_drift`）保持签名，内部改走同一命名服务（fork→plan，D6）。
- 大文件规则：新增路由/标签逻辑放 sibling（如 `agent/title_router.py`），facade 只留入口；
  sibling 用函数内 late import 拿 facade，禁止模块级循环（`agent/AGENTS.md`）。

### D2. 两级契约：确定性先行，模型级异步且不阻塞

- **stage 0 必须同步、零模型、永不失败**：`derive_title()` 仍是会话 derived 标题、工具卡标题、
  子代理 topic 的唯一来源。
- **stage 1 只在后台线程**，任何消费者都不得为了命名阻塞回合开始、`tool.start` 或 subagent
  spawn；`spawn_context_thread()` 继承 profile 与 accounting（`title_generator.py:824`）。
- stage 0 的产物可被 stage 1 覆盖（会话标题：derived→llm），但**文件名不覆盖**（D8）：

| 消费者 | 可被模型级替换？ | 原因 |
|---|---|---|
| 会话标题 | 是 | provenance 允许 derived→llm |
| 任务标签 | 不适用（无 stage 0） | 写入即最终地址（write-once）；不可再生成，否则寻址会漂移 |
| 工具卡标题 | 否 | 每次工具调用都会触发；模型级 = 每调用一次请求，成本/延迟不可接受（`tool.title` 可以后到，但不为它付费） |
| 子代理文件名 | 否 | 路径是句柄（D8） |

- **会话卡片只存在于模型级**：卡片整串写进 `sessions.title`；重生成沿用当前卡片
  （`keepTitleCard` 语义），`card_icons=boring` 时不带卡片；stage 0 的 derived 标题永不带卡片。

### D3. 模型候选计划：aux 配置 → omp 同构候选集

**生成器顺序**：`auxiliary.title_generation.generator`（D13）决定。`fork`（默认）先跑主回复
的副回合；失败/超时/拒绝/会话模型不可用 → 进入下面的候选计划。`plan` 直接走计划、不跑 fork。
两种模式下计划为空或全失败都只保留 stage 0。

`title_model_plan(main_runtime)` 返回一个**有序、去重**的候选列表；每个候选是
`(task, route|None)`，`route` 为空时由该 task 的 aux 配置解析。解析器放在 aux 层，
其他轻量工作负载将来可复用；本 ADR 只接命名消费者。构造顺序：

1. **显式 workload 覆盖**：`auxiliary.title_generation` 的 route 字段任一非默认
   （`provider` 不是 `auto`/空，或 `model`/`base_url`/`api_key` 非空）→ 第一候选，
   `task=title_generation`。这是“显式覆盖”，**不是备份**；保留该槽位是为了不静默废弃
   已发布的 `auxiliary.title_generation` provider/model 配置。**不 pin（默认）时步骤 2–4
   就是 omp 的同一候选集**；pin 时才多出这一个前置候选。
2. **角色主路由**（只有显式 pin 的槽位参与；`provider: auto` = 未配置，跳过）：
   `tiny` → `auxiliary.tiny`；`commit` → `auxiliary.commit_message`（没有独立
   `auxiliary.commit` 槽；`aux_slots.py` 的 COMMIT 标签就指它）；`smol` → `auxiliary.smol`。
   顺序与 omp 的 `["tiny","commit","smol"]` 一致。
3. **配置的重试链**：每个候选后接
   `auxiliary.<task>.fallback_chain`，以及模型键控的 `auxiliary.fallback_chains`
   （`provider/model`、`provider/*`），递归展开；等价路由只保留一次。语义复用
   `agent/auxiliary_fallback_chains.py` / `_try_configured_fallback_chain()`
   （`auxiliary_client.py:4238`），不新写一套。
4. **当前会话模型**：最后追加 `main_runtime` 的 `(provider, model, base_url)`
   （`_normalize_main_runtime` 的字段，`:3119`）；与前面候选同路由时跳过。
5. 计划为空 → 不发起模型调用（只保留 stage 0）。

规则：

- **排序归属是 plan，不是 `call_llm`**：plan 先把每个角色候选展开成显式路由
  `(provider, model, base_url)`（含该 task 的 `fallback_chain` 与模型键控链），再逐条
  `call_llm(provider=…, model=…, base_url=…, task=<role>, fallback=False)`。`fallback=False`
  关掉 `call_llm` 内部的 `auxiliary.fallback_chains` → per-task chain → main fallback 走查，
  因此同一条路由不会被两个层级各试一次。去重键是路由三元组；`auto` 角色解析到当前模型时
  与末尾候选合并。
- **每个候选只试一次**，成功即停；不并发、不“取更好者”、不重排。
- 每个候选的超时取该 task 的 `auxiliary.<task>.timeout`；当前模型候选取
  `auxiliary.title_generation.timeout`（默认 30s）。
- 调用时传 `fallback=False`（链已由 plan 展开），避免 `call_llm` 内部再走一遍链；
  `task` 只用于限额/计费/日志归属。
- 计费归属到实际命中的 task（`tiny`/`commit_message`/`smol`/`title_generation`），
  链上的 fallback 路由继承该 task 的归属。
- **本地候选失败继续走计划**：与 omp 在 `local-inference` 上的“失败即停、不付费”不同，
  本计划允许 tiny（本地）失败后落到后续候选/当前模型。理由：用户要求候选集包含当前会话
  模型，且命名需要可用性优先于“零计费边界”。该差异记录在 D12。只有会话标题/任务标签
  走这条链；工具卡标题与文件名不参与模型级（D2 表），因此不存在“每工具调用一次付费回退”。
- 门控（所有 kind 共用）：`auxiliary.title_generation.enabled=false` → 不发起任何 stage 1
  （会话连 derived 也不写，保持现状）；`model_upgrade_enabled=false` → 同样关闭所有 stage 1，
  但保留会话 derived 标题。两个开关都不影响 stage 0 的工具卡标题/子代理 topic（它们是产物
  必需字段，不是“自动命名”）。

### D4. 单一输出协议：`<title>` 标记（JSON schema 退役）

候选混跑后，协议不能跟着候选变。命名服务的 stage 1 统一为：

- system 提示词要求“只输出一行 `<title>…</title>`，无任务时 `<title/>`”。
- 解析：取首个非空行；去 `<title>`/`</title>`；`<title/>` 与 `<title>none</title>` → 无标题，
  且**终止本次命名**（不再试后续候选）；无标签的散文按首行接受（模型忽略标签时的兜底，
  同 omp `normalizeGeneratedTitle`）。
- 共享守卫：`_clean_title()`（去引号/`Title:` 前缀/句末标点）+ 长度 >80 字符或 >12 词视为
  答案型，**拒绝而不是截断**（截断仍是一个助手段落）；零词字符拒绝。
- `thinking` 关闭（`reasoning_config={"enabled": False}`），并先剥 `<think>` 块再解析。
- **卡片输出（fork）**：允许 `<title emoji="🅿️" code="PARK">Fix flaky park tests</title>`
  （`nf` 不消费）；解析顺序同 omp：属性 → `<icon> <CODE>: title` 行 → 标题文本开头的
  `CODE:`。code 必须是 1–6 位大写字母/数字，icon 是 1–8 个非 ASCII 字符；缺失或非法退化为
  纯标题。
- **卡片请求**：`title-card.md` 的回复只取 `[emoji] CODE`（code 之后的文本忽略）；
  `card_icons=boring` 不请求。
- **卡片保留**：重生成时新标题无卡片而当前标题有，沿用当前卡片；`<title/>` 仍然终止。

**明确退役**（与现行 shipped 配置的冲突在此裁决）：

| 现行 | 处置 | 替代 |
|---|---|---|
| `_TITLE_PROMPT_TEMPLATE` 的 `json_schema` 回复格式（`title_generator.py:112`–`:130`、`:561`） | 退役；`_TITLE_RESPONSE_FORMAT`/`extra_body.response_format` 不再用于命名 | `title-system.md` + 标记解析 |
| `_extract_json_title()` / `_title_from_reasoning()`（`:401`、`:451`） | 退役（`sessions retitle-skills` 同一服务，不保留旁路） | 标记解析 + 散文兜底 |
| echo 守卫 `_is_prompt_example_echo()`（`:495`） | 退役（提示词不再展示示例行，守卫集合失去锚点） | 答案型守卫 + 任务标签的 handle echo 守卫 |
| 语言规则 `_LANGUAGE_RULE_*`（`:131`） | 保留语义，改由模板变量 | `title-system.md`/`title-fork.md` 的 `{% if title_language %}` 行 |
| `title-marker-instruction.md` | 不接线（仅存档） | 无需“覆盖时换哨兵”的场景（D5） |

`auxiliary.title_generation` 槽位继续存在，但角色改变：

- **保留为工作负载旋钮**：`enabled`、`model_upgrade_enabled`、`generator`、
  `card_icons`、`language`、`refresh_after_prompts`、`timeout`；
- **route 字段**（`provider`/`model`/`base_url`/`api_key`）不再作为标题的唯一来源，
  只作为 D3 第 1 条的显式覆盖（候选 0）；不 pin 时完全不参与；
- 不再有“备份级”语义。

### D5. 模板与提示词逐文件处置

| 模板 | 处置 | 绑定 kind / 依据 |
|---|---|---|
| `title-system.md` | **接线**：`session` 的 system 提示词。manifest 新增 `title_system`（`scope: "request"`，`context_keys: ["title_language"]`），调用点 `render_request("title_system", …)` | 与 omp 同源；会话标题专用（3–6 词、`<chat>` 块） |
| `task-label.md` | **接线**：`task` 的 system 提示词。manifest 新增 `task_label`（`scope: "request"`，无变量），调用点 `render_request("task_label", {})` | omp `task/label.ts` 的同一契约：≤9 词、命令式、无任务 → `<title/>` |
| `title-marker-instruction.md` | **仅模板，不接线** | 它只服务于“用户覆盖 `TITLE_SYSTEM.md` 时把哨兵换成 `none`”；Hermes 没有标题提示词覆盖机制，`<title/>`/`none` 由解析器直接接受。保留用于与 omp 逐字节对账 |
| `title-card.md` | **接线**：`session` 的卡片请求。manifest 新增 `title_card`（`scope: "request"`，`context_keys: ["nerd_fonts"]`），调用点 `render_request("title_card", {"nerd_fonts": False})` | 卡片是标题的子形态；`/rename` 不移植，但自动标题缺卡时补一次 |
| `title-fork.md` | **接线**：`session` 的 fork 副回合。manifest 新增 `title_fork`（`scope: "request"`，`context_keys: ["card","title_language","nerd_fonts"]`），调用点 `render_request("title_fork", {...})` | 默认生成器；`card` 由 `card_icons != boring` 决定，`nerd_fonts` 恒 false；`title_language` 与 plan 路径同义 |

两个卡片模板是 Go 模板（`{{#if nerdFonts}}`、`{{else if card}}`）：注册前必须先转 Jinja
（`{% if nerd_fonts %}`、`{% elif card %}`），或预渲染要用的分支；**不能宣称逐字节接线**。
`nerd_fonts` 恒 false（本仓库无 glyph 目录），`card` 由 `card_icons` 决定。

`title-system.md` 保持与 omp 逐字节相同，只在末尾追加语言行：

```jinja
{% if title_language %}- Write the title in {{ title_language }}.{% endif %}
```

`title-fork.md` 转 Jinja 时必须追加同一行（`{% if title_language %}- Write the title in …`），
否则默认 fork 路径会绕过 `title-system.md`，让 `language` 设置静默失效。它和生成的
`title-system.md` 是仅有的两处语言行；两个卡片模板已转 Jinja，不在逐字节之列。
`title_language` 必须同时进 `title_system`/`title_fork` 的
`context_keys`/`context_types`（`StrictUndefined`，`prompt_sections.py:131`）。仅 `title-marker-instruction.md` 仍进
`docs/prompt-system.md` §装配接线状态的未接线清单；请求级清单加
`title_system`/`title_card`/`title_fork`/`task_label`。

### D6. 会话标题

- stage 0：`apply_instant_title()` 不变（同步、`derived`、冲突 `dedupe=False`）。
- stage 1：`auto_title_session()` 的后台线程按 `generator` 走。
  - `fork`（默认）：从主回复的同一前缀（同模型/系统提示/工具、读 prompt cache）跑一次
    `title-fork.md` 副回合，卡片内联解析；成功写 `source="llm"`。
  - 回落/`plan`：失败、超时、拒绝或会话模型不可用 → `generate_label("session", opener,
    plan=title_model_plan(...))`；`generator=fork` 时若结果无卡片且 `card_icons != boring`，
    再发一次 `title-card.md` 请求补卡片；`generator=plan` 不补卡片。
  - 失败保留 derived。轮次门控（`maybe_auto_title()` 的 `user_msg_count`）不变。
- 漂移：`refresh_after_prompts`、`title-drift` judge、阈值 0.5、3000/1000 窗口不变
  （ADR-17 P5）；再生成复用同一生成器，卡片沿用当前标题的卡片，仍用
  `refresh_auto_title(expected_title=...)` CAS。判漂移、标题检索与索引一律按
  `splitCardTitle` 去卡后的文本比较（卡片不是任务内容）。
- `user` 标题永不覆盖；手工改名不触发模型。kanban worker 的卡片标题保持 `llm` 权威、不加
  模型调用（`_kanban_task_title()`）。

### D7. 任务标签（子代理 UI）

- 触发：每次**具名**子代理 run 创建后，用 assignment（`user_prompts`/`task` 的正文）异步生成
  一个标签；批量豁免只针对 dispatcher 拥有的 worker
  （`agent/delegation_context.is_dispatcher_owned_worker_context`），避免一次批量任务触发
  N 次模型调用，与 `apply_subagent_title()` 的 no-model 原则一致。
- 提示词：`task-label.md`；输出标记；`<title/>` = 不命名。
- 守卫：label 不得等于 run 的 `name` 或 spawn handle 的 `Name-2` 形式（port
  `labelEchoesHandle`，`task/label.ts:13`）；≤9 词、去句末标点、去引号；超过 80 字符拒绝。
- 用途：omp-ui Agents 面板/子代理行的显示名，同时是 `agent_message` 的地址别名；**不写
  transcript、不进模型可见的工具结果**。
- **持久化**：`agent_session` 新增 `title TEXT NOT NULL DEFAULT ''` 列 + 非唯一索引
  （`idx_agent_session_title`），reconciler 按 `short_name` 的同款方式给 legacy store 补列
  （`hermes_state_schema.py:949` 的模式），**不回填**。写入用
  `UPDATE agent_session SET title=? WHERE agent_session_id=? AND title=''`：首写即终态，
  任何重命名/重试都不得覆盖非空值；空 label 不写。不复用 `metadata_json`（不可查询）。
- **寻址（取代原开放问题 3）**：`agent_message` 的自由文本 `to` 按 **label → id → name →
  subject** 解析，标签优先。规则：
  - 显式参数 `agent_session_id`/`name`/`subject` 保持原有语义（显式身份优先），只有 `to`
    走 label-first；`agent_out` 仍以 `subject` 为主，不变。
  - 比较：`strip()` + Unicode `casefold()` 后**精确匹配**，不做前缀/模糊匹配。
  - 命中 2 个及以上 run：拒绝并返回候选（`name`/`subject`/`agent_session_id`），不猜第一个。
  - label 尚未生成（异步）、被门控关闭或生成失败：跳过 label，继续 id/name/subject，**不阻塞**。
  - 写入时若同 profile 已有**存活** run 使用同一 label，追加数字后缀（` 2`、` 3`，同会话标题
    `#N` 的思路），让地址保持唯一；后缀判定与写入在同一事务内完成（并发命名最多重试一次），
    历史重复行仍走“拒绝并列出候选”分支。
  - 新增 `get_agent_sessions_by_title(title)`：先按索引精确查 `title=?`；未命中再对非空
    `title` 在 Python 侧做 `casefold()` 比较（run 数量有限，不走全表索引以外的结构）。
    label 一旦写入即为该 run 的稳定地址，不再重新生成或改写。
- 门控：D3 的 stage 1 门控（`enabled`/`model_upgrade_enabled` 任一为 false 即不生成）；
  空 assignment 不生成。

### D8. 子代理 jsonl / yield report 文件名（`topic`）

现状：一个 `topic` 同时派生 `<topic>.jsonl`、`<topic>.md`（settle 时写的最终报告）、
`<topic>_ast.json`、`<topic>.json`、`<topic>/`（`agent/run_artifacts.py:297`–`:303`）；
`topic` 由存储的 `subject` 经 `allocate_topic()` 去重（`hermes_session_paths.py:344`）。

决策：

- **一个 topic 命名同一次 run 的全部产物**，不新增独立文件名生成器；jsonl 与 yield report
  天然同名同源。
- `subject` 显式给出时照旧优先。缺省时走新增的 `derive_topic(assignment)`：先丢掉开头的
  Markdown 标题行（`#`）、围栏块与 HTML 注释，再取首个有意义行；内部复用 `derive_title()`
  的 48 字截断。`tools/agent_tool.py:1662` 的“首行”默认改为标题系统的 stage 0，首行本身
  不再是对外的契约。
- `derive_topic` 的结果经 `sanitize_component`（`[^A-Za-z0-9_-]` → `_`，`NAME_COMPONENT_CHARS`
  = 64 字符）+ `allocate_topic`（碰撞加 8 hex 后缀，`_fit_component` 同样按 64 截断）。
  **中文/emoji 标题**经 `sanitize_component` 会变成连续的 `_`：当 sanitized stem 不含
  `[A-Za-z0-9]` 时，改用 `topic-<subject 的 8-hex digest>`（复用现有 `_digest8` 语义）。
  不做音译（引入语言依赖与歧义），也不新增第二个 slugger。
- **身份在创建时固化**：run 创建时把分配到的 `<topic>` 的 transcript 路径写入
  `agent_session.transcript_path`（`run_transcript.stored_path`/`resolve_path` 读的就是它），
  settle 时把实际的 `report`/`ast`/`structured` 路径写进 `artifact_manifest`
  （`run_artifacts.identity_for_row` 优先读 manifest）；之后任何标题漂移、会话改名、任务标签
  生成都**不得**重算、重命名或移动文件。`identity_for_row` 在 manifest 缺路径的旧行上只用
  冻结的 `subject` 重算（不带 `allocate_topic` 的碰撞后缀，属于 legacy best-effort）；
  新行不依赖这条回退。
- **创建后不重命名**：`agent_out` 按存储的 `subject` 找 run，manifest 与通知里持久化了路径；
  模型标签（D7）只作为元数据 `title` 展示，绝不回写 topic。把模型标签用于文件名需要为等
  一次模型调用而阻塞 spawn（离线必失败），本 ADR 明确拒绝（D2 的表格）。
- stage 0 的输入：assignment 正文（与 `task-label.md` 同源）；空 assignment 回退到 run 的
  `name`。

### D9. 工具卡标题（omp-ui 卡片）

**范围**：D9 只裁决 **omp-ui 的工具卡标题**（`tool.start`/`tool.title` 的
`card_title`/`cell_titles`，ADR-4 D13）。会话卡片（`title-card.md` 的 icon/emoji/code）是
**标题的子形态**，归 D4/D5/D6，两者不是一回事：工具卡无模型、每次工具调用即时派生；
会话卡随自动标题生成、整串写进 `sessions.title`。

- 来源：ADR-4 D13 的 `card_titles()`（`tools/eval_card_metadata.py:108`）继续用
  `derive_title()`（stage 0）；`execute_code` 每 cell 一条，显式 `title` 优先。
- 传输：`tool.start` 帧带 `title`/`cell_titles`，`tool.title` 事件重复
  （`tui_gateway/tool_progress.py:291`、`:300`）；omp-ui 的 `#handleToolTitle` 会把迟到的
  标题合并进 pending 卡片。持久化在 `display_metadata`，恢复的 transcript 重建同一标题。
- 边界：display-only，永不进入工具结果；**不引入模型调用**（每次工具调用都触发，
  模型级等于每调用一次请求，成本/延迟不可接受）；不因 D3 路由变化而改变。
- **单一所有者**：工具卡标题归 ADR-4 D13 + `derive_title`；命名服务只登记它，不改行为。

### D10. 持久化、线程、计费与可观测

- 所有 stage 1 调用在 `spawn_context_thread()` 的后台线程执行；继承 profile、会话 accounting
  与 Portal 标签。`wait_for_title_upgrades()` 需要 join 会话标题线程；标签线程不阻塞退出
  （daemon），失败只 debug。
- `fork` 副回合从主回复的当前前缀分叉：不推进主会话 leaf、不写 transcript，只读缓存前缀；
  它的 usage 与随后的卡片请求都记在 `title_generation` task。
- 会话标题写 `set_auto_title(source="llm")`；漂移用 `refresh_auto_title` CAS。
- 任务标签写新增的 `agent_session.title`（非唯一索引、write-once），不写 `sessions.title`
  （会话标题专用）。
- 计费：按实际候选的 task 记账（D3）；每条候选只调用一次，无内部重试；失败回调每 kind
  每次生成至多一次，且只在整份计划无产出时上报（tiny 本地失败不再单独 warning）。
- 日志：`title-plan: kind=… candidate=<task> route=<provider/model> outcome=ok|empty|error`；
  `title-fork: outcome=ok|fallback|error` 与 `title-card: outcome=ok|cancel|error`。
  不记录提示词正文；不新增出站遥测。

### D11. 失败矩阵

| 事件 | 结果 |
|---|---|
| fork 失败/超时/拒绝/会话模型不可用 | 回落 D3 计划；`generator=fork` 时计划成功后补卡片请求 |
| plan 为空（无 pin、无链、无 current） | 不调用模型；会话保留 derived；标签无标题 |
| 候选 1 失败 | 试候选 2，直至计划末尾 |
| 候选返回空文本/纯标点/答案型/标记解析失败 | 该候选失败，继续下一个 |
| 候选返回 `<title/>` 或 `<title>none</title>` | **终止**：模型判定“无可命名任务”，不再试后续候选、不写标题 |
| 全部候选失败 | 会话保留 derived；标签缺失；文件名仍用 stage 0 |
| 卡片请求失败/无有效 code 或 icon | 保留纯标题；不重试 |
| `card_icons=boring` | 不请求卡片、不解析卡片，纯标题 |
| 重生成时新标题无卡片 | 沿用当前卡片（`keepTitleCard` 语义） |
| `enabled=false` | 无自动会话标题（derived 也不写，现状）；无标签；topic/工具卡仍走 stage 0 |
| `model_upgrade_enabled=false` | 无 stage 1；会话保留 derived；标签无标题；topic/工具卡仍走 stage 0 |
| 会话已有 `llm`/`user` 标题 | 跳过 stage 1 |
| 手工改名并发 | `user` 权威 + CAS（现状） |
| 子代理 topic 碰撞 | `allocate_topic` 加 8 hex 后缀；不重命名 |
| 工具卡参数无可用文本 | 无标题（不落空串） |
| 模型输出多行 | 只取首个非空行 |

### D12. 与 omp 的差异（有意保留）

| 维度 | oh-my-pi | Hermes（本 ADR） |
|---|---|---|
| 默认生成器 | `fork`（主回复副回合，可配 `tiny`） | `fork`（默认，副回合）→ 失败回落候选计划（D3） |
| `title_generation` 槽 | 不存在 | 保留为 workload 开关 + 显式路由覆盖（不是备份） |
| 本地候选失败 | 首路由 `local-inference` 失败即停 | 继续走计划（可用性优先；D3） |
| 输出协议 | `<title>` 标记（session/task-label） | 同左；fork 接受 `emoji`/`code` 属性；JSON schema 路径退役（D4） |
| 手工改名 | `renameTitle()` 可再生/补 card/revision | 纯用户写入；无 revision；手工标题无卡片 |
| session card / fork | 默认路径消费 | 接线：fork 默认、卡片并入标题串；`/rename` 不移植、Nerd Font 不消费 |
| 任务标签 | UI 标签 | 同一提示词；同时是 `agent_message` 的地址别名（write-once）；文件名用同一 stage 0 源（assignment） |
| 子代理文件名 | 会话文件按 id | `<topic>` 一个 stem 派生全部产物（ADR-16） |

### D13. 配置（新增 `generator` / `card_icons`）

```yaml
auxiliary:
  tiny:                       # 候选 1（角色主路由，显式 pin 才参与）
    provider: llamacpp
    model: <local-model>
    timeout: 30
  commit_message:             # 候选 2（omp "commit" 角色）
    provider: openrouter
    model: google/gemini-3-flash-preview
  smol:                       # 候选 3
    provider: openrouter
    model: <smol-model>
  fallback_chains:            # 模型键控重试链（现有键）
    openrouter/*:
      - {provider: nous, model: <fallback>}
  title_generation:           # workload 开关；route 非默认 = 显式覆盖（候选 0）
    enabled: true
    model_upgrade_enabled: true
    provider: auto            # auto = 不参与候选 0
    model: ""
    timeout: 30
    language: ""              # 空 = 跟随用户语言；plan 与 fork 两条路径都生效
    refresh_after_prompts: 0  # 0 = 关闭漂移检查
    generator: fork           # fork = 主回复副回合（默认）；plan = D3 候选计划
    card_icons: emoji         # emoji = 卡片；boring = 纯标题（不消费 Nerd Font）
```

### D14. 分阶段与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | `title_system`/`task_label` manifest；`title-system.md` 语言行；标记解析器；JSON 路径退役 | 未注册键被拒绝；`title_language` 两种字节；`<title>`/`<title/>`/`none`/散文/多行解析；旧 JSON 测试改为标记契约；`retitle-skills` 同协议 |
| P1 | `title_model_plan()`：角色主路由、显式覆盖、链展开、当前模型、去重、`fallback=False` | 无 pin → 只调当前模型；pin 三个角色 → 顺序 tiny→commit→smol→current；链去重；`enabled/model_upgrade_enabled` 门控；本地失败继续 |
| P2 | 会话标题/漂移改走生成器（fork→plan）；`task-label.md` 接线；`agent_session.title` 列 + 索引 + legacy reconciler（不回填）；子代理 `subject` 走 stage 0；`agent_message` 的 `to` 改 label-first（同步 `AGENT_MESSAGE_SCHEMA.to` 的描述）并加歧义拒绝 | 现有标题/漂移测试全绿；标签 echo 守卫；空 assignment 不生成；write-once（非空值不被覆盖）；legacy store 自动补列；jsonl/md 同 stem 且碰撞后缀；不重命名；`to` 命中 label 优先、重复 label 报错列候选、无 label 时回退 id/name/subject；`agent_out` 语义不变 |
| P3 | 会话卡片 + fork：`title-card.md`/`title-fork.md` 转 Jinja 并注册；fork 副回合；卡片解析/补卡/保留；`generator`/`card_icons`；fork 语言行 | 卡片属性/行/前缀三种解析；fork 失败回落并补卡；boring 无卡；重生成保留旧卡；`/rename` 不触发模型；无 nf 消费；`title_language` 两种字节 |
| P4 | 工具卡标题登记 + 契约测试；文档（`configuration.md` 标题/角色章节、`docs/prompt-system.md`）；`aux_slots.py` 的 `title_generation`/`tiny` 提示语与 `config_defaults_auxiliary.py` 注释同步 | 卡片标题仍即时、display-only；`execute_code` cell 标题不变；路由改动不影响卡片；文档不再把 `title_generation` 描述成标题的唯一来源 |

**测试落点**：新 `tests/agent/test_title_router.py`（计划/去重/门控/链）；
`tests/agent/test_title_generator.py`（标记解析/守卫/升级）；
`tests/agent/test_title_card.py`（属性/行/前缀解析、code/icon 校验、keep-card、boring）；
`tests/agent/test_title_fork.py`（副回合、回落补卡、超时/拒绝、不写 transcript）；
`tests/agent/test_title_input.py`（低信号/附件开盘）；`tests/agent/test_title_drift.py`（CAS）；
`tests/agent/test_task_label.py`（echo/空输入/长度/唯一后缀）；
`tests/tools/test_agent_message*.py`（label-first、歧义拒绝、回退 id/name/subject）；
`tests/hermes_state/test_agent_session_title.py`（补列、非唯一索引、write-once、无回填）；`tests/tools/test_agent_tool*.py` 或
`tests/agent/test_run_artifacts*.py`（topic 派生、碰撞、产物同 stem）；
`tests/agent/test_prompt_sections.py`（manifest/`StrictUndefined`）。测试用 temp `HERMES_HOME`。

### D15. 不做什么（复核清单）

- 不移植 `/rename`（手工标题无卡片、无 revision）、omp 的 ONNX/MLX tiny worker 或模型下载器；
- 不消费 Nerd Font 图标（本仓库无 glyph 目录），`nf` 属性一律忽略；
- 不改 ADR-4 D13 的工具卡路径（`derive_title`，无模型调用），也不新增 session card 的
  payload 字段或渲染（卡片只在标题字符串里）；
- 不接线 `title-marker-instruction.md`（`title-card.md`/`title-fork.md` 本版已接线）；
- 不保留 JSON schema 标题协议、`_title_from_reasoning`、示例 echo 守卫（D4 已裁决）；
- 不为工具卡标题或文件名引入模型调用；创建后不重命名子代理产物；
- 不在候选之间并发、比较或二次重试；除 D13 的 `generator`/`card_icons` 外不新增配置键；
- 不改 ADR-17 的 judge 阈值/窗口，不改 `derived < llm < user` 权威顺序；
- 不把命名结果写进 transcript、工具结果或系统提示（缓存不变量不变）。

## 开放问题

1. **`auxiliary.title_generation` 的最终形态**：本版保留其 route 字段作为显式覆盖（候选 0）。
   如果目标是彻底移除该槽的模型字段（与 omp 完全同形），需要一次配置迁移（读取旧 pin →
   写入对应角色槽或 `title_generation` 保留告警），不在本 ADR 的 P0–P4 内。
