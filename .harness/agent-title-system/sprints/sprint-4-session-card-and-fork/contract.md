---
sprint: 4
feature: session-card-and-fork
bundling: split
bundled_with: []
generator_backend: omp
generator_backend_reason: "user 已确认 omp；P3 涉及副回合缓存前缀不变量与 Go→Jinja 模板转换，host runtime 便于直接对照现有 prompt cache 测试"
goal: 会话卡片与 fork 生成器——默认走主回复副回合命名，失败回落候选计划并在 `card_icons != boring` 时补一次卡片请求；卡片是 `sessions.title` 的字符串子形态，随重生成保留、随比较/漂移/检索剥离
acceptance_scenarios:
  - id: AS-1
    text: "卡片解析接受三种形态：`<title emoji='…' code='…'>…</title>` 属性、`<icon> <CODE>: title` 行、`CODE: title` 前缀；code 必须为 1–6 位大写字母/数字、icon 为 1–8 个非 ASCII 字符，缺失或非法退化为纯标题，`nf` 属性被忽略。"
  - id: AS-2
    text: "`title-card.md`/`title-fork.md` 已由 Go 模板转为 Jinja 并以 `scope=request` 注册为 `title_card`（`context_keys: ['nerd_fonts']`）与 `title_fork`（`context_keys: ['card','title_language','nerd_fonts']`）；两者在 `StrictUndefined` 下渲染，`title_language` 为空/非空两种字节均可渲染，`nerd_fonts=False` 时不出现任何 Nerd Font 指导。"
  - id: AS-3
    text: "fork 副回合从主回复的既有前缀分叉：同模型/同系统提示/同 tools[]，只读历史快照，不推进主会话 leaf、不写 transcript；fork 与随后的卡片请求 usage 都记在 `title_generation`。"
  - id: AS-4
    text: "fork 失败/超时/拒绝/父会话模型不可用时回落 `title_model_plan` 候选计划；`generator=fork` 且 `card_icons != boring` 时计划成功后只发一次 `title_card` 请求并解析 `[emoji] CODE`（code 之后的文本忽略）；`generator=plan` 不跑 fork 也不补卡片，键缺失时默认 `fork`。"
  - id: AS-5
    text: "`card_icons=boring` 时 fork 提示词不含卡片指导、不回解析模型输出中的卡片，也不发卡片请求：结果始终为纯标题。"
  - id: AS-6
    text: "重生成沿用当前卡片（新标题无卡片则保留旧卡），`<title/>` 仍终止且不写标题；重写比较、漂移判定与按标题检索/索引一律以去卡文本为准（仅卡片不同不算一次重写或漂移）。"
  - id: AS-7
    text: "手工标题（`source=user`，`/rename`/`sessions rename`）不触发任何模型调用或卡片请求且不被覆盖；命名路径不消费 Nerd Font：`nerd_fonts` 恒 false，模型输出中的 `nf` 属性被丢弃。"
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
    description: Prompt registration, card protocol and fork/plan dispatch match ADR-19 and ADR-21 D2/D4/D5/D6/D10/D11
  - axis: Documentation
    weight: low
    threshold: "?"
    description: Docstrings and template comments describe the shipped card/fork behavior, not the retired paths
max_iterations: "?"
status: pending-negotiation
---

# Sprint 4 Contract — session-card-and-fork

## Acceptance Scenarios

### AS-1: 卡片的三种解析形态与合法性校验

```
Given  `title-card.md` / `title-fork.md` 尚未接线，`title_generator` 没有卡片解析入口
When   pytest 把下列模型输出交给卡片解析入口（`split_card_title`）：
       1. `<title emoji='🅿️' code='PARK' nf='nf-md-parking'>Fix flaky park tests</title>`
       2. `🅿️ PARK: Fix flaky park tests`
       3. `PARK: Fix flaky park tests`
       以及非法/缺失输入：无 `emoji` 属性、`code='park'`（小写）、`code='PARKING'`（7 位）、
       ASCII icon（`A PARK: Fix flaky park tests`）、`<title>Fix flaky park tests</title>`（无卡片）
Then   1 与 2 都得到卡片 `🅿️ PARK` + 纯标题 `Fix flaky park tests`；
       3 得到无 icon 的卡片 `PARK` + 纯标题 `Fix flaky park tests`；
       非法/缺失输入全部得到“无卡片 + 未改写的纯标题”（不抛异常、不吞掉标题文本）；
       `nf` 属性从不进入返回值，输出中不出现 `nf-` 前缀；
       code/icon 的边界由同一入口判定：1 位 code 合法、6 位 code 合法、7 位非法；
       1 个与 8 个非 ASCII 字符的 icon 合法、9 个非法
```

Evidence: `sprints/sprint-4-session-card-and-fork/evidence/AS-1.pytest.txt`

### AS-2: 两个卡片模板的 Go→Jinja 转换与请求级注册

```
Given  `agent/prompts/system/title-card.md` 与 `title-fork.md` 仍是 Go 模板
       （`{{#if nerdFonts}}`、`{{else if card}}`），manifest 中没有 `title_card`/`title_fork`
When   Generator 把两个模板转成 Jinja（`{% if nerd_fonts %}`、`{% elif card %}`），
       在 `title-fork.md` 的 `</title-request>` 之前追加同一语言行
       （`{% if title_language %}- Write the title in {{ title_language }}.{% endif %}`），
       并注册：
         `title_card`  — scope=request，context_keys=['nerd_fonts']，context_types={'nerd_fonts': 'bool'}
         `title_fork`  — scope=request，context_keys=['card','title_language','nerd_fonts']，
                         context_types={'card': 'bool', 'title_language': 'str', 'nerd_fonts': 'bool'}
       pytest 用 `render_request` 渲染：
         `title_card`  → {'nerd_fonts': False}
         `title_fork`  → {'card': True,  'title_language': '',       'nerd_fonts': False}
         `title_fork`  → {'card': True,  'title_language': 'French', 'nerd_fonts': False}
         `title_fork`  → {'card': False, 'title_language': '',       'nerd_fonts': False}
Then   三次 `title_fork` 渲染都成功且非空；`card=True` 变体含
       `<title emoji=… code=…>` 指示行，`card=False` 变体只含 `<title>…</title>` 指示行；
       空 `title_language` 的渲染不含 “Write the title in”，非空渲染以
       `- Write the title in French.` 结尾；`title_card` 渲染含 `emoji`/`code` 指导；
       两个模板的 `nerd_fonts=False` 变体都不含 `nf:`、`nf-`、`Nerd Fonts` 字样，
       且渲染结果不含 `{{#if`/`{{else`/`{{/if}}` 残留；
       省略任一已声明键（如 `title_fork` 不传 `card`）时渲染以未定义变量错误失败
       （`jinja2.UndefinedError` 或包装后的 `PromptSectionError`），不静默渲染成空段；
       两个键都在 manifest 中且 `scope` 为 `request`，`render_section(..., scope='system')`
       拒绝它们
```

Evidence: `sprints/sprint-4-session-card-and-fork/evidence/AS-2.pytest.txt`

### AS-3: fork 副回合的前缀一致性与 transcript 隔离

```
Given  一个活跃父 agent：runtime 三元组 (provider, model, base_url)、`_cached_system_prompt`、
       `tools[]`、`_session_messages` 快照，以及 temp `HERMES_HOME` 下的 SessionDB
When   `generator` 取默认值 `fork`，pytest 调用
       `auto_title_session(db, session_id, opener, agent=parent)`，
       spy `build_cache_parity_fork` / `run_conversation` / `release_cache_parity_fork`
Then   恰好发起一次副回合，且没有第二次模型请求（除 AS-4 的补卡场景外）；
       fork 的 `(provider, model, base_url)` 与父一致，`_cached_system_prompt` 与父
       逐字节相同，`tools[]` 与父的 schema 逐项相同（同值比较，不比较延迟或命中计数）；
       `run_conversation` 收到的 `conversation_history` 值等于父 `_session_messages` 快照，
       且是独立副本（修改 fork 侧不改变父列表）；
       副回合在空工具白名单下运行、`max_iterations=1`；
       运行后父 `_session_messages` 的内容与长度不变、`SessionDB` 消息行数不变、
       无 `append_message` 写入、主会话尾部（leaf）不前进；
       fork 的 `_session_db is None` 且 `_persist_disabled is True`；
       父会话 accounting 收到 `task='title_generation'` 的 usage（fork 与随后的卡片请求
       都记在该 task 下）；最终标题以 `source='llm'` 写入
```

Evidence: `sprints/sprint-4-session-card-and-fork/evidence/AS-3.pytest.txt`

### AS-4: fork 失败矩阵与补卡

```
Given  `_title_config` 可注入 `generator` 与 `card_icons`；`title_model_plan` 候选计划可注入
When   (a) `generator='fork'`、`card_icons='emoji'`，fork 入口分别：
           抛 `TimeoutError`、抛 provider 拒绝（`RuntimeError('refusal')`）、返回 `''`、
           父 agent 缺失（会话模型不可用）；
           随后计划候选返回纯标题 `Fix flaky park tests`，
           卡片请求回复 `<title>🅿️ PARK: extra words</title>`
       (b) `generator='plan'`，fork 入口被 spy
       (c) 配置中完全没有 `generator` 键
       (d) 计划层候选返回 `<title/>`
Then   (a) 每种失败都只尝试一次 fork，然后按 `title_model_plan` 的顺序尝试候选；
           成功后恰好发一次 `title_card` 请求，解析只取 `🅿️ PARK`（`extra words` 被忽略），
           最终存储 `🅿️ PARK: Fix flaky park tests`；
           卡片请求失败、返回空、或 code/icon 非法时保留纯标题
           `Fix flaky park tests`，不重试卡片请求；
           计划本身无产出时保留 derived 标题，不写 `llm` 标题；
       (b) fork 入口调用 0 次，`title_card` 请求 0 次，直接使用计划结果；
       (c) 行为与 `fork` 相同（默认值生效），并发出卡片请求；
       (d) 终止命名：不写标题、不补卡片、不再试后续候选
```

Evidence: `sprints/sprint-4-session-card-and-fork/evidence/AS-4.pytest.txt`

### AS-5: `boring` 不请求也不解析卡片

```
Given  `card_icons='boring'`
When   (a) fork 成功，但模型仍返回带卡片属性的输出
           `<title emoji='🅿️' code='PARK'>Fix flaky park tests</title>`
       (b) fork 失败，计划候选返回纯标题
       (c) 对已带卡片的标题执行漂移重生成
Then   fork 渲染上下文为 `card=False`，提示词中不出现 `emoji`/`code` 指导行；
       (a) 存储的是纯标题 `Fix flaky park tests`，不含 `🅿️`、`PARK` 或卡片前缀；
       (b) 不发任何 `title_card` 请求（0 次），存储纯标题；
       (c) 重生成结果仍是纯标题，不把旧卡片补回；
       任何路径下 `split_card_title` 的结果卡片均为 None
```

Evidence: `sprints/sprint-4-session-card-and-fork/evidence/AS-5.pytest.txt`

### AS-6: 卡片保留、`<title/>` 终止与去卡比较

```
Given  会话标题为 `🅿️ PARK: Fix flaky park tests`（`source='llm'`），
       `refresh_after_prompts` 允许漂移检查，漂移 judge 可注入
When   (a) 重生成返回纯标题 `Fix flaky auth tests`
       (b) 重生成返回纯标题 `Fix flaky park tests`（与去卡文本相同）
       (c) 重生成返回 `<title/>`
       (d) 直接调用 `split_card_title('🅿️ PARK: Fix flaky park tests')`、
           `keep_title_card('Fix flaky auth tests', '🅿️ PARK: Fix flaky park tests')`，
           并用纯标题 `Fix flaky park tests` 调用 `resolve_session_by_title`
Then   (a) 存储 `🅿️ PARK: Fix flaky auth tests`，CAS 的 `expected_title` 是旧的完整标题；
       (b) 不发起写操作（比较先用 `split_card_title` 去卡），
           漂移 judge 的 `state['title']` 是 `Fix flaky park tests`，不含卡片；
       (c) 现有标题保持不变，本次命名终止：不补卡片、不回落候选计划、不写新标题；
       (d) 分别得到 `('🅿️ PARK', 'Fix flaky park tests')` 与
           `'🅿️ PARK: Fix flaky auth tests'`，且按纯标题检索命中该带卡片行；
       全程没有新增 `sessions` 列或卡片 payload 字段：卡片只存在于 `sessions.title` 字符串内
```

Evidence: `sprints/sprint-4-session-card-and-fork/evidence/AS-6.pytest.txt`

### AS-7: 手工标题不触发模型/卡片，且无 Nerd Font 消费

```
Given  会话已由用户命名（`set_session_title` → `source='user'`），
       `title_fork`/`title_card` 已注册
When   (a) 后续回合调用 `maybe_auto_title` / `auto_title_session`
       (b) 漂移检查对该会话运行
       (c) 以 `nerd_fonts=False` 渲染两个模板，并让模型返回
           `<title nf='nf-md-parking' emoji='🅿️' code='PARK'>Fix flaky park tests</title>`
Then   (a) 不调用 `call_llm`、不构造 fork、不发卡片请求；标题保持用户值且无卡片前缀；
       (b) 跳过 `user` 标题（无 judge 调用、无重写），也不补卡片；
       (c) 渲染结果不含 `nf-`、`nf:`、`Nerd Fonts` 清单或 glyph 目录引用；
           解析结果卡片只含 `🅿️` 与 `PARK`，`nf` 属性被丢弃；
           命名路径所有 `title_fork`/`title_card` 调用点都传 `nerd_fonts=False`
           （spy 断言），且没有新增 glyph 资源、`nf` 配置键或字体下载逻辑
```

Evidence: `sprints/sprint-4-session-card-and-fork/evidence/AS-7.pytest.txt`

## Rubric Detail

| Axis | Weight | Threshold | Scoring notes |
|---|---|---|---|
| Functionality | high | TBD in Negotiation | Binary-ish: every AS passes or sprint fails |
| Craft | std | TBD in Negotiation | Card parser/fork tests are focused; no dead Go-template remnants; follows `agent/AGENTS.md` facade+sibling rule |
| Consistency | std | TBD in Negotiation | ADR-19 `scope=request` manifest contract + ADR-21 D2/D4/D5/D6/D10/D11 exactly |
| Documentation | low | TBD in Negotiation | Docstrings/template comments describe real behavior; no doc claims of Nerd Font or `/rename` support |

Total pass condition: **every axis meets its threshold**. Weight only affects
Evaluator's ordering when reporting failures (high-weight fails reported first).

## Notes for Generator & Evaluator

- **缓存前缀一致性按参数断言，不按耗时**：比较 fork 构造参数与请求内容——runtime 三元组、
  `_cached_system_prompt` 字节、`tools[]` 的 JSON 序列化、传给 `run_conversation` 的
  `conversation_history` 值。不要在测试里断言延迟、缓存命中计数或 HTTP 头。
  现有可复用机制是 `agent/ephemeral_prompt.run_ephemeral_prompt()`（同模型路径、
  空工具白名单、`build_cache_parity_fork`/`release_cache_parity_fork`）；命名只是它的
  又一个一次性消费者，不要另造 fork 生命周期。
- **失败注入**：直接 monkeypatch fork 入口让其抛 `TimeoutError` / provider 拒绝异常 /
  返回空串，或用 `agent=None` 模拟会话模型不可用；四种都必须落到计划路径。不要靠真实网络
  或 sleep 制造超时。
- **transcript/leaf 断言**：比较运行前后 `SessionDB` 的消息行数与尾部，以及父 agent
  `_session_messages` 的内容和长度；断言 fork 侧 `_session_db is None`、
  `_persist_disabled is True`、且没有 `append_message` 调用。不要只断言返回值。
- **卡片是字符串子形态**：存储形态固定为 `<icon> <CODE>: <title>`（第三形态可无 icon，即
  `CODE: <title>`），仍写入 `sessions.title`；不得新增列、payload 字段或渲染字段。
  合成后的标题仍受 `sanitize_title` 与 100 字符上限约束。
- **测试可导入面**（facade `agent.title_generator` 转发；实现可放 sibling 模块，
  用函数内 late import 避免循环）：
  `split_card_title(text) -> (card | None, plain_title)`、
  `keep_title_card(new_title, current_title) -> str`、
  `parse_card_reply(text) -> card | None`（卡片请求回复，只取 `[emoji] CODE`）、
  `run_title_fork(parent, prompt, *, history=None) -> str`（失败返回 `""`，不抛），
  以及 `auto_title_session(..., agent=<parent>)` 的 fork 分支。
  `generator`/`card_icons` 通过 `_title_config()` 读取：`generator` 缺省 `fork`，
  `card_icons` 缺省 `emoji`；值为 `boring` 时关闭卡片。
- **`<title/>` 语义继承 sprint 1**：解析器返回“终止”时，fork 与计划路径都不得再试下一个
  候选、不得补卡片；不要在本 sprint 重新解释标记协议。
- **保留既有语义**：`runtime_validator` 返回 False（后台线程启动后用户切换了模型）仍然
  静默跳过整次 stage 1；`user` 标题权威不变；`refresh_auto_title` 的 CAS 仍以旧标题为
  `expected_title`。这些是既有回归，不是本 sprint 的新增行为。
- **回归面**：`tests/agent/test_title_generator.py`、`tests/agent/test_title_drift.py`、
  `tests/agent/test_prompt_sections.py`、`tests/agent/test_title_input.py` 必须保持全绿。
  两个新模板是 `scope=request`，不进入系统提示词，因此
  `tests/fixtures/prompt_surface/` 快照不应变化；若实现动了系统提示词装配，用
  `python scripts/ci/prompt_surface.py render` 重新生成快照，禁止手工编辑。
- **范围边界**：不移植 omp 的 `/rename`（手工标题无卡片、无 revision）；不消费 Nerd Font；
  不改工具卡标题（P4）；不做 schema 迁移、`agent_session.title`、子代理标签与
  `agent_message` 寻址（P2/P3 前半）；除 `generator`/`card_icons` 外不新增配置键；
  `title-marker-instruction.md` 保持仅存档。
- **观测日志**：沿用 ADR-21 D10 的 `title-fork: outcome=ok|fallback|error` 与
  `title-card: outcome=ok|cancel|error`，只记结果不记提示词正文；测试可通过日志断言补卡
  次数，但计费/用量断言以 accounting 的 `task='title_generation'` 为准。
- **Evidence**：每个 AS 的 pytest 输出保存到
  `sprints/sprint-4-session-card-and-fork/evidence/AS-<n>.pytest.txt`（含命令、通过/失败
  计数与关键断言），Evaluator 的冲刺脚本以这些文件为判定依据。

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

