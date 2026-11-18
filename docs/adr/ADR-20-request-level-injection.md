# ADR-20：请求级注入——会话搜索回执与后台复盘回执

**状态**：提议（2026-11-14）。**P0 已实现**（2026-11-14）：四个请求级模板、manifest 注册、
`renderer` 渲染入口与定向测试；生产者与注入点（P1/P2）尚未接线，接线方式由 **D9** 决定。
第二轮修订把 P0 的 typed helper 定位为 `render_request_block` 的 wrapper，生产入口收敛到
`assemble_request`（既有 request 模板在 P3c 收敛，字节不变）。
分阶段：**P0** 模板与注册；**P1** 会话搜索回执（`request_state` 缓存键）与 turn-note 接线；
**P2** `background_review` 写入回执与 turn-note 接线；**P3a–c** 存量模板重构（D10：文本
合并、新 producer、接线、装配收敛；本轮 0 退役）；**P4** manifest 接线审计。

**第二轮修订（2026-11-14，本版）**：

1. **请求级装配器**：`scope: request` 模板一律走 `assemble_request()` → `renderer` 块入口 →
   `prompt_sections`，与系统提示同一分层、同一模块、同一 AST 边界；外部代码只调用 stage /
   consume，不导入 renderer、不读模板、不拼顺序（D3/D9）。
2. **模板为主的处置**：每条 request 模板的 carrier、order、去重、持久化与 producer 声明在
   **它自己的 manifest 条目**里；Python 消费端没有模板键字面量、没有按 key 的分支。ADR 的
   处置表是设计记录，权威来源是模板条目（D10）。
3. **`<workstation>` 删除 host/home/scratch**：这三类是错误写法，任何载体都不需要——直接从
   系统提示与所有请求级模板中删除，没有替代模板、没有新字段。date/cwd 仍按首轮修订走
   `date_cwd_reminder`（模板名不变）。
4. **manual-continue 的场景**：用户主动中断工具轮后的手动继续，不是任意回合的 Continue 动作
   （D10）。
5. **不建搜索账本**：回执状态复用 `hermes_cache` 的 `request_state` 命名空间（SQLite 持久化、
   LRU 上限、profile 解析都已存在），**不新增 `session_notices` 表、不改 SessionDB schema**。
   账本 = 重复建设（D8）。

**关联**：ADR-18 D10（请求级注入与会话级重写保持不变）、ADR-19 D5/D6（manifest 只做注册、
请求级模板只走请求级装配入口）、ADR-6（TTSR 信封）、ADR-9（late diagnostics 的
`<system-notice>`）。本 ADR 不修订上述缓存/装配结论：请求级注入仍然**不进系统提示词**，
系统提示对一个会话保持字节稳定。

**修订（运行时事实移出系统提示）**：ADR-18 D10 的“首轮日期/cwd 已在系统提示内”与 ADR-19
D5-B 的 `<workstation>` 行由本 ADR 修订——首轮日期/cwd 改走请求级 `date_cwd_reminder`
（模板 `system/date-cwd-reminder.md`，名称与职责不变），**不再由 `<workstation>` 承载**；
host/home/scratch 是错误内容，**直接删除且不迁移**：系统提示、请求级模板、解析器里都不再
出现。`<workstation>` 只保留 OS/Arch（含远程 backend 提示行）、Model、Provider、Platform
五类稳定行。`project_context.runtime_environment_rows()` 的 local 分支只产出 OS/Arch：
`_local_host_hints()` 的 Host / User home directory / Current working directory / Scratch
directory 行整段删除，依赖 home 的 Windows hostname 提示一并删除，Scratch 行上的 TMPDIR 指引
也删除（`TMPDIR` 已由 shell 环境提供，不在提示里重复）。与工具面相关的 Windows bash shell
提示不是路径事实，保留，但必须改由 `runtime_environment_rows()[1]`（notes）返回，不能再作为
`hints[0]` 被 splitlines 成 `<workstation>` 行。`tests/agent/test_project_context.py` 中
断言带 `- ` 的 home/scratch 行的用例同 PR 改为断言这些行不存在、OS/Arch 与 notes 仍渲染。
`surface_switch.runtime_host_value` 与 `conversation_loop._stored_prompt_matches_runtime` 的
cwd 锚点改读 `f"{session_id}:date_cwd_reminder"` 缓存键记录的 `cwd`（由构建提示的同一
`resolve_agent_cwd()` 产出），**不是** `sessions.cwd`——后者是 row metadata（workspace 分组、
`-c`、restore），不保证等于执行 cwd，拿它比较会让 `TERMINAL_CWD` 会话误判；旧会话回退到旧
提示里的 `Current working directory:` 行，两者都缺失时跳过校验。host 没有解析消费者，不新增。
缓存不变量不变：`<workstation>` 只剩会话内稳定字节。

**取证口径**：Hermes 侧引用本仓库当前工作树（2026-11-14）。模板清单来自
`agent/prompts/system/`；渲染契约来自 `agent/prompts/prompt_sections.py` 与
`agent/prompts/renderer.py`；持久层契约来自 `hermes_cache/AGENTS.md` 与
`hermes_cache/api.py`（`get_cache()` / `namespace()` / profile 解析 / LRU 容量）；
后台复盘动作来自 `agent/background_review.py` 的
`summarize_background_review_actions` / `_publish_review_summary`；搜索输出来自
`tools/tool_search.py::dispatch_tool_search` 与 `tools/skills_tool.py::skills_list`。

**范围**：

- **做**：把请求级注入的通道、信封、注册、装配与数据契约写成可执行的规则；新增“本会话最近
  3 条工具搜索结果”“最近 3 条技能搜索结果”“后台复盘已落地的技能写入”“后台复盘已落地的
  记忆写入”四个模板；新增 `assemble_request()` 请求级装配入口；把每条模板的接线处置写进
  它自己的 manifest 条目；按 D10 对存量模板做接线或内联合并（P3a–c）；从 `<workstation>`
  删除 host/home/scratch。
- **不做**：把请求级提醒搬进系统提示；新增常驻模型工具；改变工具面/技能索引；重写
  `background_review` 的判定逻辑；**退役存量模板**（本轮 0 退役，D10 只接线/合并）；
  为 host/home/scratch 另找载体（删除即完成）。

---

## 问题

### 1. 请求级注入是一组互不相认的模板，没有装配与渲染契约

`agent/prompts/system/` 里同时住着两类东西：装配器独占的系统章节，和“运行时事件”模板。
后者按用途至少可分成六组（下表）。P0 之前只有 `eager_task` / `eager_todo` / `irc_incoming` /
`subagent_*` 登记了 `scope: request`；P0 的四个回执虽已登记，producer 与注入点（P1/P2）尚不
存在；其余模板由调用点直接读文件或内联文本渲染。登记过的模板也没有装配器，调用点各自直接
调 `render_request`。结果是：

- 同一类信息出现 `<system-notice>`、`<system-reminder>`、`<system-interrupt>`、
  `<system-injection>`、`<recap>`、`<btw>` 与纯文本七种信封，没有“什么时候用哪一种”的规则；
- 新模板没有强制入口：不登记 manifest 也能被调用，`StrictUndefined` 与上下文白名单形同虚设；
- 没有请求级装配器：调用点直接调 `render_request` / `render_*`，块顺序、空值语义、去重和
  转义各写一遍；新增模板必须同时改调用点代码，模板不是唯一事实来源；
- 逐模板的接线状态只存在于本 ADR 的表格与散落代码里，没有模板侧声明，无法从模板本身审计
  “谁生产、何时注入、怎么去重”；
- 模板插入的文本（查询、结果名、规则正文、复盘摘要）没有统一的转义与长度约束，存在伪造
  信封标签的风险。

### 2. 压缩之后，模型不记得自己搜过什么

`tool_search` 与 `skills_list(query=...)` 的结果只存在于工具行里。上下文压缩会按策略剪掉旧
工具结果，模型随后重复同一个查询，或者把“没搜到”误判成“没有这个能力”。工具侧已有答案缓存
（ADR-18 D8，`hermes_cache` 的 `tool_choice` 300s / `skill_choice` 60s），它避免重复执行，
但不记录“本会话最近搜过什么”。回执不需要新存储：同一套缓存接口加一个 session-scoped 的
小键即可（D8），**不建账本**。

### 3. 后台复盘的写入对下一轮不可见

`background_review` 在回合结束后异步运行，写入技能与记忆，只通过
`_publish_review_summary` 给用户端一条展示摘要（`background_review_callback`）。下一轮模型
看不到“上一轮复盘刚创建/更新了什么”，于是可能重复写入，或把已被复盘纠正的旧记忆当作现状。

### 4. 请求级注入受两条硬约束

- **缓存**：内容一旦进入系统提示，就成了会话前缀的一部分；每轮变化的搜索/复盘信息不能进
  系统提示（root `AGENTS.md` 的 prompt-caching invariant）。
- **角色交替**：回合内不得插入合成 user 行；唯一例外是 `/steer`。请求级回执只能在
  “合法边界”进入请求：合并进下一条真实 user 消息，或在前一条是 assistant/tool 行时作为独立
  user 行追加（`assistant(tool_calls) → tool → user` 在所有 provider 路径合法）。

---

## 现有请求级模板的分组

状态口径（**现状**；目标处置见 D10）：**已接线** = 生产调用点直接读取/渲染该文件；
**内联** = 调用点内联复刻正文，`system/` 副本未被读取；**仅模板** = 仓库内零调用点
（port-only）；**已注册** = manifest `scope: request` + renderer 入口，生产者见 P1/P2。

| 组 | 模板（canonical） | 状态 | 信封 | 语义 |
|---|---|---|---|---|
| 1 | `agent/prompts/unexpected-stop-retry.md`（`system/` 副本未引用） | 已接线 | `<system-injection>` | 文本停止但任务未完成时的一次重试；要求下一个具体工具或最终答案 |
| 2 | `system/ttsr-interrupt.md` | 内联（`ttsr_prompts.py`） | `<system-interrupt reason="rule_violation">` | 丢弃输出，带规则正文重试 |
| 2 | `system/ttsr-tool-reminder.md` | 内联 | `<system-reminder reason="rule_violation">` | 工具参数命中规则；工具已执行，后续调用需修正或确认合规 |
| 2 | `system/ttsr-warning.md` | 内联 | `<system-reminder reason="rule_violation">` | judge 判定输出可能违规；未中断，自查后修正或继续 |
| 3 | `system/tool-call-loop-redirect.md` | 仅模板 | `<system-interrupt reason="tool_call_loop_detected">` | 同工具同参连续调用；禁止再次以同参调用 |
| 3 | `system/tool-roster-notice.md` | 仅模板 | `<system-notice>` | 工具面变化；只列变化，当前 schema 权威 |
| 4 | `system/session-agent-notice.md` | 仅模板 | `<system-notice id="session-agents">` | 用户标记的模型 agent 变化；只列变化 |
| 4 | `system/session-stop-blocked.md` | 仅模板 | 纯文本 | `session_stop` 无理由阻塞；先解决未完成工作 |
| 5 | `agent/recap.py`（正文内联；`system/recap-user.md` 未引用） | 已接线 | `<recap>` | 用户离开后返回的 ephemeral 侧回合；≤40 词 |
| 6 | `system/auto-continue.md` | 仅模板（gateway 续跑逻辑不使用该文件） | 纯文本 | 中断/崩溃后续跑；恢复最近意图 |
| 6 | `system/manual-continue.md` | 仅模板 | `<system-notice>` | 用户主动中断工具轮后的手动继续；从停点续跑，不总结、不确认 |
| 6 | `system/btw-user.md` | 仅模板 | `<btw>` | 交互中的临时侧问；仅当前上下文，禁工具、禁追问 |
| 6 | `system/date-cwd-reminder.md` | 仅模板 | `<system-reminder>` | 首轮日期与 cwd；不要求复述 |
| 6 | `system/eager-task.md` / `eager-todo.md` | 已接线（调用点直接调 renderer） | `<system-reminder>` | 委派/todo 策略提醒 |
| 6 | `system/eval-prelude-notice.md` | 仅模板 | `<system-notice id="prelude-extension">` | eval prelude 变化；只列变化 |
| 6 | `system/eval-state-context.md` | 仅模板 | `<system-reminder>` | eval 运行时状态；宿主保留的 kernel 状态 |
| 6 | `system/interrupted-thinking.md` | 仅模板 | 纯文本 + 代码围栏 | reasoning 被打断时复述内容 |
| 6 | `system/mid-run-todo-nudge.md` | 仅模板 | `<system-reminder>` | 回合中仍有未完成 todo；立即更新或继续 |
| 6a | `system/session-tool-search-notice.md` | 已注册 | `<system-notice id="session-tool-search">` | 本会话最近 3 条查询与命中工具名 |
| 6b | `system/session-skill-search-notice.md` | 已注册 | `<system-notice id="session-skill-search">` | 本会话最近 3 条查询与命中技能名 |
| 7 | `system/background-review-skill-reminder.md` | 已注册 | `<system-reminder id="background-review-skills">` | 已落地的技能创建/更新 |
| 8 | `system/background-review-memory-reminder.md` | 已注册 | `<system-reminder id="background-review-memory">` | 已落地的记忆创建/更新 |

同组内还有 `system/irc-incoming.md` 与 `system/subagent-*.md`，经请求级装配入口接线，
不属于本 ADR 的六类；`system/subagent-system-prompt.md` 是唯一由 `assemble_subagent` 追加的
request 模板（角色块，ADR-19 D9）。**canonical 判定**：组 1 的
`agent/prompts/unexpected-stop-retry.md` 才是生产文件，`agent/prompts/system/unexpected-stop-retry.md`
是未被引用的副本；组 2/5 的 `system/` 文件与调用点的内联正文是两份副本，P3 才统一。
**仅模板** 的条目不得被本 ADR 描述为已生效的注入；它们只是待迁移资产。本表的“状态/信封”
是审计快照；每条模板的**权威处置**是它自己的 manifest 条目（D10 的 `carrier`/`order`/
`dedupe`/`lifetime`/`producer`/`wired`），表格与条目冲突时以条目为准。

---

## 决策

### D1. 请求级注入只走消息行，永不改系统提示

| 载体 | 适用 | 约束 |
|---|---|---|
| 合并进下一条真实 user 消息 | 搜索回执、复盘回执、date-cwd 等回合前提醒 | 组装请求时拼接 turn-note；持久化仍存操作者原文 |
| 独立合成 user 行（note 附于其上） | 自动续跑 / wake turn 且没有真实 user 消息 | 仅当前一条是 assistant/tool 行；带 `synthetic_kind`；note 不另起一行 |
| 工具结果追加 | 子目录提示、late diagnostics | 追加到刚产生的 tool 行；不修改已持久化的旧行 |
| 流中断 | TTSR、工具循环 | 丢弃当前输出并重试；不产生可回放的半条 assistant 行 |
| 独立 ephemeral 回合 | recap、`/btw` | 自己的请求；不复用主会话的持久化 |

**禁止**：回合中途插入 `user` 行；把请求级模板交给 `assemble_main`；在请求级模板里修改
`tools[]`、系统提示或历史行；调用点自己调 `render_request`/`render_*` 或拼接块。搜索/复盘
回执在“回合前或回合后”注入，不在工具循环中间注入。

### D2. 信封按语义固定，不按调用点临时发明

- `<system-notice id="...">`：已经发生的**状态变化**（工具面、prelude、会话 agent、搜索回执），
  只列变化或最新快照。
- `<system-reminder id="...">`：对**当前事实**的提醒（日期/cwd、策略、todo、已落地的复盘写入），
  不声称是用户指令。
- `<system-interrupt reason="...">`：当前输出被中止，必须重试；用于规则与工具循环。
- `<system-injection>`：对“该继续却没继续”的一次性 nudge。
- 领域信封（`<recap>`、`<btw>`）只用于独立 ephemeral 回合；不得出现在主回合的消息行里。
- 纯文本只用于“用户可见的继续提示”；不得承载机器可解析的状态。

每个信封恰好一个 `id` 或 `reason`；同一 id 在同一请求里至多出现一次。多个回执的合并顺序
由模板条目的 `order` 决定（D3），块之间空一行；语义分组（状态变化 → 回合前事实 → 策略
提醒）通过 `order` 区间表达，不在调用点排序。

### D3. 请求级模板走同一个装配器→渲染器路径

`scope: request` 不是“调用点自行渲染”的许可，而是**第二个装配入口**。分层与 ADR-19 相同：

| 模块 | 负责 | 不负责 |
|---|---|---|
| `agent/prompts/assembler.py` | `assemble_request(ctx)`：按模板声明的 `order` 迭代注册表、丢弃空块、一次 `"\n\n"` join | 取数、格式化、转义、去重状态 |
| `agent/prompts/renderer.py` | `render_request_block(key, payload)`：按模板条目的 `sanitize`/`max_rows` 处理列表行（折叠、裁剪、`escape_harness_tags`），调用 `render_section(..., scope="request")` | 决定块顺序、决定是否注入 |
| `prompt_sections.py` | manifest 解析、scope 校验、`StrictUndefined`、预算 | 块选择 |
| `agent/request_notices.py` | `request_state` 缓存键读写、stage/consume、`dedupe: snapshot` 的读回比较 | 渲染与排序 |

```python
# agent/prompts/assembler.py
@dataclass(frozen=True)
class RequestNotice:
    key: str
    context: Mapping[str, Any]      # producer 采集的结构化事实

@dataclass(frozen=True)
class RequestContext:
    notices: tuple[RequestNotice, ...]
    facts: SessionFacts = SessionFacts()

def assemble_request(ctx: RequestContext, *, carrier: str,
                     only: frozenset[str] | None = None,
                     skip: frozenset[str] = frozenset()) -> str: ...
def request_notice(key: str, context: Mapping[str, Any], *, carrier: str) -> str:  # 单模板便捷入口
```

`carrier` 是**必填**分组：`assemble_request` 只装配声明了同一 carrier 的条目，绝不把
gate row / interrupt / ephemeral 混进 turn-note 文本。外部调用点只调自己 carrier 的入口。

规则：

1. `assemble_request` 与 `assemble_main` 同模块、同边界：没有 `open`/`read_text`/`Path` 读取，
   只导入 `renderer`、`prompt_sections` 与 stdlib；AST 边界测试对两个入口同时生效。
2. 装配器不查 `AIAgent`、不读缓存：`notices` 由调用方采集，装配器只排序与
   join。`skip` 由消费端按去重策略算出，装配器不解释它。
3. **顺序由模板声明**：`scope: request` 条目新增 `order`（整数；缺省按 key 字典序，重复值
   再按 key 字典序打破平局），装配器只排序，不维护白名单。ADR-19 D4 的“manifest 不参与
   装配”只约束 `scope: system`；request 集合是开放的，把顺序写回 Python 等于“新增模板必须
   改代码”，正是本 ADR 要消除的硬编码。
4. **外部代码只调用**：producer 调 `stage_request_notice(agent, key, context)`；回合边界调
   `consume_request_notices(agent)`；单发模板用 `request_notice(key, context, carrier=...)`。
   调用点不得导入 `renderer`/`prompt_sections`，不得读 `system/*.md`，不得自行拼接、排序或
   转义。
5. 只在**有内容**时渲染；空数据返回 `""`，装配器丢弃，不留空行、不降级占位。
6. `context_keys` / `context_types` 必须声明完整；`StrictUndefined` 让漏键在构建期失败。
   `context_types: list` 只接受 `list[str]`，producer 预格式化每一行。
7. **信封模板不设 `budget`**：`tail` 截断会切掉闭合标签，模型会读到未闭合的块。信封条目
   `budget: null`、`truncate: error`；行级内容由 renderer 的 `sanitize: rows` 裁剪
   （单行、≤320 字符），模板始终完整输出。
8. **转义边界由条目声明，不按模板类别猜**：`sanitize: "rows"` = 每个字符串 context 值先
   `escape_harness_tags`（标量路径/日期同样处理），list 元素再折叠单行、按 `max_rows`
   裁剪；`sanitize: "none"` = 字节原样，只给 IRC 正文、plan 正文、subagent assignment 这类
   必须逐字保留的模板。默认必须显式声明，漏写即构建失败——不能出现“搜索 query 没转义”
   或“IRC 正文被改写”两种错误。
9. 存量请求模板（`eager_task`、`irc_incoming`、`subagent_*` 等）在 P3c 改为走
   `request_notice()`，行为以 golden 字节不变为验收；`renderer.render_request` 退为
   `render_request_block` 的内部实现，调用点不得直接使用；`subagent_system_prompt` 由
   `assemble_subagent` 追加，是系统提示的角色块，不在此列。
10. 未登记模板不得新增调用点；登记 + 装配路径是唯一入口。

### D4. 组 6a/6b：会话搜索回执

**模板**：`session-tool-search-notice.md`、`session-skill-search-notice.md`。
**注册键**：`session_tool_search_notice`、`session_skill_search_notice`（`scope: request`，
`context: searches: list[str]`）。条目同时声明 `order: 40` / `41`、`carrier: turn-note`、
`sanitize: rows`、`max_rows: 3`、`dedupe: snapshot`、`lifetime: session`、
`source: agent.request_notices:build_search_notice`、
`producer: agent.request_notices:stage_session_search_notes`、`wired: true`。

**回执状态**（P1 实现，`agent/request_notices.py`；工具侧只调用记账入口，不自己存）：

- **存储复用 `hermes_cache`**：`get_cache().namespace("request_state", default_ttl=None,
  max_entries=256, eviction=LRU)`；键 `f"{session_id}:{template_key}"`，值
  `{"v": 1, "records": [{"query": str, "matches": [str, ...]}, ...]}`（newest first，≤3）。
  **不建 `session_notices` 表、不加 SessionDB schema/mixin**（D8）。
- 采集点：工具结果提交后。`tool_search` 先经 `tool_executor._unwrap_tool_search_call` 解析
  底层工具名；按返回的 `results[]` 每个 `query` 合并一条。`skills_list` 仅在 `query` 非空时
  记，每条调用一条。同 `(source, query)` 去重保留最新，截 3。解析失败、`success != true`、
  结果为空都跳过。
- 只存查询与名称：不存描述、工具 schema、完整 payload、时间戳。时间戳会让同一快照在不同请求
  里产生不同字节，且没有信息价值。`tool_search` 的 `tools` map 带完整 schema，回执只取
  `results[].matches` 的名称。答案缓存（`tool_choice` / `skill_choice`）继续只管执行去重，
  回执键与它分开，互不复用 key。
- 不采集：`_persist_disabled` 的 fork、`_turn_origin in {"background_review", "btw"}`、
  子代理的独立上下文（子代理有自己的 agent 实例，天然隔离）。
- 生命周期：缓存键跨 agent 实例与进程；压缩不清空（缓存不参与 messages rewrite）；
  `/new` 新 session_id 从空开始；旧会话键由命名空间 LRU 上限淘汰。agent 上不保留 deque，
  `source` 每次直接读缓存（失败按空处理）。
- 注入：`build_search_notice(agent)` 读键并预格式化行（无键/空 → `None`）；`dedupe: snapshot`
  由消费端执行——把渲染结果与 transcript 最新同块比较，相同则不 stage。压缩后若最新 note 已被
  丢弃，读回为空 → 重新 stage；若 note 仍在，读回抑制重复。
- 文案已声明“记录结果，不是新指令；当前 schema / 技能索引权威”，因此陈旧快照不会被当成
  能力清单。

**示例行**：`` `weather api` → `web_search`, `web_extract` ``。空命中写 `` `query` → no matches ``。

### D5. 组 7/8：后台复盘写入回执

**模板**：`background-review-skill-reminder.md`、`background-review-memory-reminder.md`。
**注册键**：`background_review_skill_reminder`、`background_review_memory_reminder`
（`scope: request`，`context: actions: list[str]`）。条目同时声明 `order: 30` / `31`、
`carrier: turn-note`、`sanitize: rows`、`max_rows: 8`、`dedupe: once`、`lifetime: pending`、
`source: agent.request_notices:build_background_review_notice`、
`producer: agent.request_notices:stage_background_review_notes`、`wired: true`。

**回执状态**（P2 实现，`agent/background_review.py` 的兄弟模块；同样不建表）：

```python
@dataclass(frozen=True)
class ReviewWrite:
    target: Literal["skill", "memory"]
    action: Literal["created", "updated", "deleted", "support_file",
                    "added", "replaced", "removed"]
    name: str          # 技能名 / memory 目标 + 条目标识
    detail: str        # 单行摘要；不含正文、diff、密钥
```

- 采集点：`_run_review_in_thread` 算完 `summarize_background_review_actions` 之后。新增
  `collect_background_review_writes(review_messages, prior_snapshot)`，复用
  `_tool_messages` / `_collect_review_call_details` 的解析，按工具名（`skill_manage` /
  `memory`）与请求参数分类；**不解析本地化展示字符串**，也不受 `memory_notifications`
  影响（`_classify_review_result` 只用于日志分类）。
- 持久化：待注入批次写入 `request_state` 缓存键 `f"{session_id}:{template_key}"`，值
  `{"v": 1, "actions": [str, ...]}`；同一 `(target, action, name)` 去重保留最新，≤8 行。
  复盘线程必须显式绑定父会话 profile：`get_cache(home=Path(_session_db.db_path).parent)`
  （或 `spawn_context_thread`），不得读 `os.environ` 推导。复盘 fork 自身不持有回执；
  fork 的 `_persist_disabled` / 共享 `session_id` 语义不变。
- 注入：复盘完成后的**下一次请求**注入一次；note 随 user 行持久化后删除缓存键。复盘若在一个
  回合进行中完成，等下一回合，绝不插入当前工具循环。agent 淘汰或 resume 后，未消费的批次
  仍从缓存键恢复并注入一次（缓存是 SQLite 持久层，不依赖 agent 内存）。
- 只列名称与动作（“Skill created: `deploy`”“Memory updated: release branch”）；不贴正文。
  模板明确“写入已落地，不要重复；当前文件权威”。
- 用户端的 `_publish_review_summary` / `background_review_callback` 保持不变；模型侧回执是
  另一条通道，二者不互相替代。

### D6. 注入时机、去重与请求形状

1. **回合前**：新 user 消息进入请求前，把待注入的块合并为其 turn-note（`api_content` 或
   text part）；持久化仍存用户原文。这是 `skills_index_delta` 的同一形态，不会产生第二条
   user 行。
2. **无用户消息**：自动续跑或 wake turn 会先构造一条合成 user 消息；note 附在该消息上，
   不另加第二条 user 行。
3. **回合中**：不注入。搜索回执在工具结果里已有原始结果；复盘回执等下一回合。
4. **至多一次**：每个 id 每个请求至多一个块；策略来自模板条目：`snapshot` 以“最新 note
   文本 vs 当前渲染快照”为判据，`once` 以待注入行为判据，`per-turn` 以读取即清为判据。
5. **单出口**：`consume_request_notices(agent)` 逐条目取 context → `assemble_request(carrier=
   "turn-note")` 排序/join → 返回一个字符串；顺序与空块语义只在装配器实现一次，调用点不做
   第二次拼接。
6. **压缩**：压缩是唯一允许的缓存断点；压缩后若最新搜索 note 已不在 transcript，读回为空
   → 下一次请求重新 stage；若 note 仍在，读回抑制重复。复盘回执不因压缩重放（已经注入过
   就消费掉）。
7. **缓存**：注入只改“最新 user 行”或其后的追加行，不改它之前的字节；系统提示与工具面
   完全不动。

### D7. 失败与成本

| 情况 | 行为 |
|---|---|
| 回执采集解析失败 | 跳过该条，`logger.debug`，不影响工具结果 |
| 复盘摘要计算失败 | 已有路径视为空动作；不产生回执 |
| 模板渲染失败（漏键/未知键） | `PromptSectionError`，构建失败；不静默回退 |
| 空快照 | 零字节，不产生空信封 |
| 成本 | 每个回执 ≤3 条搜索 / ≤8 条动作，单行 ≤320 字符；无额外模型调用 |

不新增配置键：回执是会话事实的派生视图，开关应由调用点（是否启用工具搜索/复盘）决定，
不再增加一层用户配置。

### D8. 持久化边界：回执状态复用 `hermes_cache`，不新增表

- **不建表**：ADR 上一版拟新增 `session_notices`（`session_id, kind, payload_json`）与 SessionDB
  schema/mixin——那是重复建设。`hermes_cache` 已经提供 SQLite 持久化、profile 解析、TTL、
  命名空间容量上限与 LRU 淘汰；回执只是 session-scoped 的小键值。
- **命名空间**：`get_cache().namespace("request_state", default_ttl=None, max_entries=512,
  eviction=EvictionPolicy.LRU)`；键 `f"{session_id}:{template_key}"`，值是该模板的 `context`
  加 `{"v": 1}`。`default_ttl=None` = 不按时间过期；`max_entries` 是全局上限（≈100 个会话 ×
  5 个键），旧 session 由 LRU 淘汰；`/new` 用新 session_id，从空开始。
- **profile 解析**：`get_cache()` 在调用时解析活动 home，缓存实例按 profile 绑定。复盘线程不是
  父会话的默认上下文，必须 `get_cache(home=Path(_session_db.db_path).parent)` 或经
  `hermes_threading.spawn_context_thread` 继承，禁止读 `os.environ` 推导 home
  （HX002/HX004/HX005）。
- **`lifetime` 语义**：`turn` → agent 内存 slot；`session` → 缓存键保留（搜索快照、首轮
  watermark）；`pending` → 缓存键在被注入且 note 随 user 行持久化后删除（复盘批次）。消费端
  只按注册表里的 request 键读写，不硬编码 key。
- **回执随 user 行持久化，读回用于去重**：stage 的 note 通过 D9 的 turn-note 通道进入当前
  user 行（字符串走 `api_content`，multimodal 走 text part），随该行持久化，并可能在后续
  请求中重放（`agent/memory_context.py` 的既有语义）。新 agent / resume 从 transcript 的
  最新同块读回当前快照，相同则不重复 stage。**水位线不单独落盘**：读回就是水位线。
- **首轮提醒**：`date_cwd_reminder` 的 `source` 计算当前 `{date, cwd}`；与
  `f"{session_id}:date_cwd_reminder"` 不同（或键不存在）时 upsert，该键兼作 cwd 漂移校验的
  watermark。`source` 每次返回当前 context，是否注入由 `dedupe: snapshot` 与 transcript
  读回决定：note 仍在则抑制，压缩丢弃后重放。cwd 漂移校验读这个键的 `cwd`（与构建提示同一
  resolver），不读 `sessions.cwd`（见首页修订段）。
- **非模板内部状态**：`manual_continue` 的中断侧只把 `{turn_id, stopped_stage, tool_name,
  pending_calls}` 写到 `f"{session_id}:manual_continue"`，surface Continue 动作据此决定是否
  `stage_request_notice` 并删除该键；模板 `context_keys` 仍为空，不声明这些键
  （`validate_context` 会拒绝未声明键，声明未使用键则是死字段）。新指令或回合结束后清除。
- **失败开放**：cache get/set 失败只 `logger.debug`，不得让搜索、复盘或回合失败；缓存值带
  `v`，未知版本/损坏值按 miss 处理（provider 已隔离损坏 db）。回执丢失只影响一次提示，
  不影响正确性。
- **MoA / codex_app_server**：这些 mode 不 stamp `api_content`，D9 的 note 读回/去重不可用。
  搜索 note 跳过注入（缓存键仍写）；复盘 pending 键保留，等下一次支持的模式再注入。不要改为
  往 `content` 追加来绕过：那会改持久化原文，破坏“用户原文不变”。

### D9. 接线通道：一个 stage 入口，一个 consume 出口

模板**不**在工具或复盘调用点各自插入消息，也不直接改系统提示；接线复用既有的 per-turn
note 通道（`surface_switch` / `skills_index_delta` 已在生产使用），但通道本身与模板解耦：

1. **stage（事件侧，通用）**：`stage_request_notice(agent, key, context)` 只写通知存储，不渲染：
   `lifetime: turn` → agent 内存 slot；`lifetime: session` / `pending` → `request_state`
   缓存键 `f"{session_id}:{key}"`（值 `{"v":1, ...context}`）。producer 只采集结构化事实，
   不拼字符串、不排序。来自复盘线程等非会话默认上下文的调用必须显式传父 profile
   （`home=Path(_session_db.db_path).parent`），不得读 `os.environ`。
2. **consume（唯一出口）**：`agent/turn_context.py::_merge_gateway_notes` 每回合调用
   `consume_request_notices(agent)`。它对 `carrier: turn-note` 的条目按 `order` 逐个取
   context：条目声明了 `source` 就调用 `source(agent)`（返回 context 或 `None`），否则读
   stage slot；`None`/空 → 跳过；最后 `assemble_request(carrier="turn-note")` 一次 join。
   session 状态（搜索快照、复盘批次、首轮 watermark）的读取全部由各自的 `source` 完成，
   `turn_context` 不再逐个装载；消费端没有模板键分支，也没有“固定顺序”列表。
   gate row / interrupt / ephemeral 走各自的 carrier 入口，不经过 `consume_request_notices`。
3. **载体（note transport）**：字符串 user 行走 `api_content` sidecar；multimodal（list）走
   `append_notes_to_multimodal_content` 的 durable text part。两条路径都在 cached prefix
   之后，系统提示/工具面字节不动。
4. **空值**：`assemble_request` 对空 context 返回空块；`_pop_turn_note` 读取即清空，长驻
   agent 不重放。
5. **去重（模板声明、代码通用）**：`snapshot` → 渲染结果与 transcript 最新同块比较，相同不
   stage；`once` → note 随 user 行持久化后删除缓存键；`per-turn` → 读取即清。

| 模板 | 条目声明的 carrier / 去重 | producer（何时） | consume（source） |
|---|---|---|---|
| `session_tool_search_notice` | `turn-note` / `snapshot` | `agent/tool_executor.py` 结果提交后写 `request_state` 键 | `build_search_notice`：读缓存键（dedupe 由消费端做） |
| `session_skill_search_notice` | `turn-note` / `snapshot` | 同上（`skills_list(query=...)`） | 同上 |
| `background_review_skill_reminder` | `turn-note` / `once` | `_run_review_in_thread` 完成后写缓存键 | `build_background_review_notice`：读缓存键，持久化后删除 |
| `background_review_memory_reminder` | `turn-note` / `once` | 同上 | 同上 |

**顺序**由条目的 `order` 决定，区间约定（注册测试校验单调）：

| 区间 | 内容 |
|---|---|
| 10–19 | 首轮提醒（`date_cwd_reminder`） |
| 20–29 | 用户/插件发起的 gate（`manual_continue`、`session_stop_blocked`） |
| 30–39 | 后台复盘写入（技能 → 记忆） |
| 40–49 | 会话搜索回执（工具 → 技能） |
| 90–99 | 其它 gate row / interrupt |

多个 note 之间空一行；没有内容的块不占位。顺序是模板条目的数据，消费端不维护列表，新增
模板只在自己的条目里选区间。

**新 agent / resume 的读回**：

- 搜索：`build_search_notice` 读 `f"{session_id}:session_tool_search_notice"` 缓存键；最新
  note 从 user 行的 `api_content` / text part 读回（`skills_index_delta._announced` 同型的
  `rsplit` 扫描），与当前渲染快照比较。相同 → 不再 stage；不同 → stage 最新 3 条。
  resume/每回合重建既不会失去“最新 3 条”，也不会重复注入。水位线不单独落盘。
- 复盘 note 的待注入批次在两个复盘模板键的缓存键里；读取后 stage，note 随 user 行持久化
  之后再删除该键。崩溃窗口内可能重复一次，但文案是幂等的“已经落地”。

**载体限制**：MoA 与 `api_mode="codex_app_server"` 不 stamp `api_content`
（`skills_index_delta` 明确跳过这两种模式）。搜索 note 在这些模式不注入（缓存键仍写），
复盘 pending 键保留等待下一次支持的模式；不要用 `content` 追加绕过，避免改写用户原文或
破坏读回。

**存量模板处置**：D9 只定义通道；每个存量模板是接线还是内联合并，见 **D10**。

**否决的接线方式**：

- 在工具执行点直接 `append` 一条 user 行：会制造连续 user 行（`/steer` 之外不允许），并且
  工具循环中途的注入没有合法边界。
- 调用点直接渲染或直接改 system prompt：绕过装配器的顺序/空值/转义，破坏缓存不变量
  （ADR-18 D10）。
- 每个调试点自行拼 note：顺序、去重、空值语义会再次分裂；必须只有一个
  `consume_request_notices` 出口。

---

### D10. 存量模板接线决策：处置写在模板自己的条目里

这是**重构决策**：不以现有调用点为边界，也不以“现有内联文本与模板措辞不同”为保持内联的
理由；内联正文一律视为待合并的重复副本。目标状态下，`agent/prompts/system/` 里每个
`scope: request` 模板必须二选一（**本轮 0 退役**）：

1. **接线**：有明确 producer，按 D9 的通道（turn-note / gate row / ephemeral turn）注入；
2. **内联合并**：生产代码里的同文内联副本删除，模板成为唯一正文。

没有 producer 的模板先建 producer，不得以“已覆盖/客户端负责”为由删除。退役只在未来由
独立 ADR 决定。

**模板为主**：处置不是一张 Python 表，也不只写在本 ADR 里。每条模板的 manifest 条目就是它
的接线契约（`carrier` / `order` / `dedupe` / `lifetime` / `sanitize` / `max_rows` /
`source` / `producer` / `wired`）；运行时消费端只读注册表，不出现模板键字面量。新增或改接线
= 改 `.md` 与它的条目（+ 该模板自己的 source/producer）；装配器、`consume_request_notices`、
`request_notices` 适配器不改。下面的表格是设计记录与评审清单，与条目冲突时以条目为准。

Python agent 是模型侧提示的唯一 owner；只在客户端渲染的字符串不留在
`agent/prompts/system/`。**接入配方**分两层：

模板侧（每条模板）：

0. 转 Jinja2：`{{#if}}` / `{{#each}}` → `{% if %}` / `{% for %}`，camelCase → snake_case；
   `StrictUndefined` 下漏键必须构建失败。
1. 条目声明 `scope: request` + `context_keys` / `context_types` + 上述接线字段。
   `context_types: list` 只接受 `list[str]`，producer 预格式化每一行（如 `query → tool`）。
2. `sanitize: rows` 的模板接受模型生成的文本（查询、名称、摘要）；`sanitize: none` 只用于
   字节原样正文（IRC、plan、assignment）。

运行时侧（一次实现，不随模板变化）：

3. `stage_request_notice` 只接收结构化事实；不渲染、不拼字符串。
4. `consume_request_notices` → 逐条目调用声明的 `source(agent)`（或读 stage slot）→
   `assemble_request` 统一排序、丢弃空块、一次 join。
5. renderer 的 `render_request_block` 按条目做折叠、裁剪与 `escape_harness_tags`。
6. 测试注册表驱动：遍历所有 request 条目，覆盖最小合法 context、空 context → `""`、敌意行
   转义、`order` 单调、carrier 不混装；source/producer 另有行为测试（无事件零字节 / 事件
   一次 / 重复去重）。

**接入点分类**：

| 类别 | carrier | producer 边界 | 去重/持久化 |
|---|---|---|---|
| A 状态回执 | turn-note（D9） | 搜索/复盘事件写 `request_state` 键 | 最新快照读回；pending 消费删除 |
| B 停止/中断 gate | nudge / interrupt row | stop gate、tool round、plugin stop hook | 每 turn 上限 |
| C 独立 ephemeral 回合 | 自己的 user prompt | recap、`/btw`、side question | 不写主 transcript |
| D 首轮/运行时提醒 | turn-note | turn start | 首轮或状态变化 |
| E 客户端 | 客户端自有 | 客户端 | 不归 Python |

**分析方法**：对每条模板问三个问题，答案决定处置：

1. **有没有稳定的 producer？** 有 → 接线；没有 → 进入第 2 问。
2. **生产代码里有没有同文内联？** 有 → 内联合并（删内联，模板成为唯一正文）；没有 → 第 3 问。
3. **能力是否已被系统提示/工具结果覆盖？** 是 → 仍接线或合并（本轮不以“已覆盖”为由退役）；
   否 → 先建 producer（新 guard/hook/registry），再接线。

这样避免两种错误：给没有事件源的模板硬造注入点；或者保留 Python 内联副本，让模板与生产
文本长期漂移。**本轮 0 退役**：四个原拟退役项（manual-continue、date-cwd、eval-prelude、
eval-state）都改为接线。

**逐模板决策**（“接入点”是重构后 producer 应放的位置，不受当前代码位置限制）：

| 模板 | 现状 | 目标 producer / 接入点 | 模板 context | 决策与动作 | 阶段 |
|---|---|---|---|---|---|
| `unexpected-stop-retry.md` | `agent/prompts/` 生产读取；`system/` 副本未引用 | `turn_stop_gates` 的停止判定 | `retry_count`, `max_retries` (int) | **接线**：canonical 用 `system/`，删 `agent/prompts/` 副本，占位符改 Jinja2；`request_notice`（`order: 90`，`carrier: gate-row`） | P3a |
| `ttsr-interrupt.md` | `ttsr_prompts.py` 内联复刻 | `ttsr_prompts.render_interrupt`（流中断） | `name`, `path`, `content`, `matched` | **内联合并**：删 `INTERRUPT_*` 常量，模板渲染；保留转义 | P3a |
| `ttsr-tool-reminder.md` | 内联复刻 | `ttsr_prompts.render_tool_reminder` | `name`, `path`, `content`, `builtin` | **内联合并**：同上 | P3a |
| `ttsr-warning.md` | 内联复刻 | `ttsr_prompts.render_judged_warning` | `name`, `path`, `content`, `subject` | **内联合并**：同上 | P3a |
| `tool-call-loop-redirect.md` | 零调用点 | 新 `ToolLoopGuard`（`turn_tool_round`，N 次同参） | `tool_name`, `count`, `arguments_summary`, `result_summary` | **接线**：新增 guard，触发时发 `<system-interrupt reason="tool_call_loop_detected">` | P3b |
| `tool-roster-notice.md` | 零调用点 | `tools/mcp_tool_agent._publish_tool_snapshot`（晚连接 MCP/插件工具） | `added`, `removed` (list[str]) | **接线**：snapshot 发布点返回 added/removed，stage turn-note | P3b |
| `session-agent-notice.md` | 零调用点 | 用户标记 model agent 的 roster 变更（CLI/gateway 的 agents 注册表） | `added`, `removed`（名称+描述行） | **接线**：在 roster 变更处产生事件；无 roster producer 则先建注册表 | P3b |
| `session-stop-blocked.md` | 零调用点 | 新 `session_stop` plugin hook 的阻塞结果 | 无 | **接线**：hook 无理由阻塞时追加 gate row；hook 不存在则先加 hook API | P3b |
| `recap-user.md` | `recap.py` 内联复刻 | `recap.build_recap_prompt` | `goal`, `task` (str) | **内联合并**：删 `_RECAP_HEADER`，改 `request_notice`（`carrier: ephemeral`）；Handlebars → Jinja2 | P3a |
| `auto-continue.md` | gateway 内联 `[System note: ...]` | gateway auto-continue / resume-pending 路径 | 无 | **接线**：模板替换内联文本；用 `synthetic_kind` 取代字符串前缀检测 | P3b |
| `manual-continue.md` | 零调用点；无 Python producer | surface 的“手动继续”动作（前置：turn exit 的 human-origin 中断标记） | 无 | **接线**：`lifetime: turn`、`carrier: turn-note`、`dedupe: per-turn`；中断侧只在**用户显式中断进行中的工具轮**时落一段 pending 元数据——`interrupt_issuer(agent) is None`，reason 为 `explicit stop requested` / `user interrupt`（`/stop` 的显式 stop 算用户中断；不含 `user sent a new message`，那条已带新正文），且本回合有被跳过/取消的工具行；surface Continue 动作确认 pending 与工具轮在飞后 `stage_request_notice`，下一条新指令丢弃 pending、不注入；lease/watchdog/TTSR 等系统 issuer 一律不记录 | P3b |
| `btw-user.md` | `side_question.py` 内联 `_FORK_PROMPT`/`_ONESHOT_INSTRUCTIONS` | `agent/side_question.py` | `question`, `fork` (bool) | **内联合并**：模板吸收两套规则，`fork` 门控；删内联常量 | P3a |
| `date-cwd-reminder.md` | 零调用点 | 会话首个请求（`turn_context`）；date/cwd 变化时重放 | `date` (str), `cwd` (str) | **接线**：`order: 10`、`carrier: turn-note`、`sanitize: rows`（cwd 作为字符串同样转义）、`dedupe: snapshot`、`lifetime: session`、`source: agent.request_notices:build_date_cwd_notice`；`request_state` 键 `f"{session_id}:date_cwd_reminder"` 在 date/cwd 变化时 upsert（兼作 watermark），注入由 `dedupe: snapshot` 读回决定；`<workstation>` 删除 host/home/cwd/scratch 行（无替代载体；Windows bash 提示改由 notes 返回，不进 rows）；cwd 漂移锚这个键的 cwd（旧会话回退旧提示行）；`surface_switch.runtime_host_value` 不再解析这些行 | P3b |
| `eager-task.md` / `eager-todo.md` | 已接线（调用点直接调 renderer） | `renderer.render_eager_*` | 现有 | **保持语义**：P3c 改为 `request_notice`，golden 字节不变 | P3c |
| `eval-prelude-notice.md` | 零调用点 | eval prelude registry（`execute_code` prelude 变更） | `added`, `removed`, `sections` | **接线**：prelude 注册/变更时 stage；无 prelude 则先建 producer，不退役 | P3b |
| `eval-state-context.md` | 零调用点 | `tools/code_kernel.py` 的 `state_reset` / `state_lost` → stage | `state_summary` (str) | **接线**：kernel 状态变化后的下一请求提醒；工具结果仍是原始事实 | P3b |
| `interrupted-thinking.md` | 零调用点 | `conversation_loop` 的 interrupt scaffold | `reasoning` (str) | **接线**：中断分支用模板渲染，替代内联 scaffold | P3b |
| `mid-run-todo-nudge.md` | `todo_reminder._reminder_text` 内联 | `todo_reminder.stop_gate_reminder` | `todos` (list[str]), `count`, `plural` | **内联合并**：模板扩展为渲染 todo 行；删 `TODO_REMINDER_HEADER` | P3a |

**manifest 接线元数据（模板条目即契约）**：所有 `scope: request` 条目声明完整接线字段：

```json
{
  "key": "session_tool_search_notice",
  "path": "session-tool-search-notice.md",
  "scope": "request",
  "order": 40,
  "carrier": "turn-note",
  "sanitize": "rows",
  "max_rows": 3,
  "dedupe": "snapshot",
  "lifetime": "session",
  "source": "agent.request_notices:build_search_notice",
  "producer": "agent.request_notices:stage_session_search_notes",
  "wired": true
}
```

| 字段 | 取值 | 作用 |
|---|---|---|
| `order` | int | 装配器唯一的排序依据（`scope: request` 专用） |
| `carrier` | `turn-note` / `gate-row` / `interrupt` / `ephemeral` / `tool-append` | 注入通道；`assemble_request` 只装配同 carrier 的条目 |
| `sanitize` | `rows` / `none` | `rows` = 标量 `escape_harness_tags`，list 行再折叠/裁剪；`none` = 字节原样 |
| `max_rows` | int / null | `sanitize: rows` 的行上限；模板不再自带上限常量 |
| `dedupe` | `snapshot` / `once` / `per-turn` / `none` | 去重策略，实现一次 |
| `lifetime` | `turn` / `session` / `pending` | `turn` = agent 内存 slot；`session` = `request_state` 缓存键保留（快照/watermark）；`pending` = 缓存键在注入后删除（复盘批次） |
| `source` | `"module:function"` / null | consume 侧 context 适配器：`(agent) -> Mapping \| None`；session/pending 条目必填，turn 条目可省略（读 stage slot） |
| `producer` | `"module:function"` / null | 事件来源；有事件源的条目必填且可导入 |
| `wired` | bool | 已接线标记；release 分支不得为 false |

**与 ADR-19 D6 的关系**：这是对 manifest 契约的**扩展**，不是替代。上述字段是注册元数据；
`assemble_main` 不读 `order`，字节不变；`assemble_request` 只读 `carrier`/`order` 并调用
`source`。ADR-19 D6 的“manifest 只做注册”对 `scope: system` 仍然成立。同 PR 必须更新：
`agent/prompts/prompt_sections.py` 的 `PromptSection`/`load_sections`（字段解析 + 枚举校验）、
`manifest.json` 的 `description`、`tests/agent/test_prompt_sections.py` 的 manifest 契约测试。
若维护者不接受扩 manifest，等价方案是 sidecar `agent/prompts/system/wiring.json`；本 ADR 选择
manifest 内联，因为模板与接线状态必须同 PR 变更、同 diff 审阅。

规则：`wired=true` 必须至少有 `source` 或 `producer` 之一，且指向的函数可导入（最小导入
测试）；`session`/`pending` 条目必须声明 `source`，`turn` 条目可只靠 stage。`wired=false`
只允许出现在未完成的重构分支。CI 校验：release 无 `wired=false`；`order` 区间单调；每个
request 条目都能被 `assemble_request` 渲染（空 context → `""`）。这样“注册了但没人渲染”
从隐性状态变成模板自带的可审计字段。

**迁移顺序**：

| 批次 | 内容 | 验收 |
|---|---|---|
| P3a 文本合并 | unexpected-stop、ttsr-*、recap、btw、todo nudge | 每模板 golden 字节；删内联常量后 grep 为 0；9 surface prompt 快照不变（除 intentional diff） |
| P3b 新 producer | tool loop、tool roster、session agent、session stop、auto-continue、interrupted-thinking、manual-continue、date-cwd、eval-prelude、eval-state | 每个 producer 一条行为测试；空/状态未变不注入；gate 边界合法；每会话一次（date-cwd） |
| P3c 装配收敛 | 既有 request 模板（`eager_*`、`irc_incoming`、`subagent_*` 等）改为 `request_notice()` | golden 字节逐字不变；调用点不再导入 `renderer`/`prompt_sections` |
| P4 审计 | manifest 接线字段 + CI 门 | release 无 `wired=false`；无“已登记但零调用”的模板；消费端无模板键字面量 |

**D10 验收**：

1. `agent/prompts/system/` 的每个 request 模板都有完整接线字段；不存在未声明的第四种状态。
2. 已接线模板的正文只存在于 `.md`：Python 里不再有同文内联常量（用 canonical 句子 grep 为 0）。
3. **本轮 0 退役**：manual-continue、date-cwd、eval-prelude、eval-state 均有 producer 与
   测试；`system/unexpected-stop-retry.md` 的重复副本作为 P3a 合并删除，不算模板退役。
4. 新 producer 不新增常驻工具/系统提示；只产生请求级行或 ephemeral prompt。
5. 每个 producer 有“无事件零字节 / 事件发生一次 / 重复事件去重”三类测试。
6. **模板为主**：`assemble_request`、`consume_request_notices`、`request_notices` 中 grep
   不到任何 request 模板键；新增一条模板（`.md` + 条目 + 条目声明的 `source`/`producer`）
   不需要改这三个文件的逻辑。

---

## 实现落点

| 组件 | 文件 | 内容 |
|---|---|---|
| 模板 | `agent/prompts/system/session-tool-search-notice.md` 等 4 个 | 信封 + 文案，Jinja2 循环已预格式化行 |
| 装配 | `agent/prompts/assembler.py` | `assemble_request(carrier=...)` / `request_notice()`：只装配同 carrier 的条目，按 `order` 迭代、丢弃空块、一次 `"\n\n"` join；无模板键字面量 |
| 注册 | `agent/prompts/system/manifest.json` | 4 个 `scope: request` 条目 + `order`/`carrier`/`sanitize`/`max_rows`/`dedupe`/`lifetime`/`source`/`producer`/`wired` |
| 渲染 | `agent/prompts/renderer.py` | `render_request_block(key, payload)` 按条目做折叠/裁剪/转义；空数据返回 `""`；4 个 typed helper 只作 wrapper |
| 搜索回执 | P1：`agent/request_notices.py` | `request_state` 缓存键（≤3 条 {query, matches}）；采集、`stage_request_notice`、读回去重；无新表 |
| 接线 | P1：`agent/tool_executor.py` + `agent/turn_context.py::_merge_gateway_notes` | 结果提交后写缓存键；`build_search_notice` 读键，`consume_request_notices` 是唯一出口 |
| 缓存接入 | P1：`hermes_cache` 公共 API（`get_cache().namespace`） | `request_state` 命名空间、`default_ttl=None`、`max_entries=512`、LRU；不新增 provider/表 |
| 复盘回执 | P2：`agent/background_review.py` 兄弟模块 + `agent/request_notices.py` | `collect_background_review_writes` + `request_state` 键（`home=` 显式绑定父 profile） |
| 复盘注入 | P2：`consume_request_notices` | 从缓存键恢复 → stage → note 持久化后删除键 |
| 测试 | `tests/agent/test_request_notices.py`、`tests/agent/test_prompt_assembler_boundary.py` | 注册表驱动渲染、上限、空数据、转义；stage/consume 顺序与去重；装配器 AST 边界 |

---

## 验收

1. **装配路径**：`assemble_request(carrier=...)` 是唯一的请求渲染入口，且只装配同 carrier
   的条目；`consume_request_notices` 是 `turn-note` 的唯一出口；调用点不导入
   `renderer`/`prompt_sections`；`assemble_request` 与 `assemble_main` 同模块、同 AST 边界
   （无文件读取、导入集合受限）。
2. **注册与作用域**：四个键在 `load_sections()` 中为 `scope: request`；`assemble_request` /
   `render_request_block` 接受，`assemble_main` / `render_section(scope="system")` 拒绝。
3. **空数据**：任一 request 模板在空 context 上返回 `""`；装配器丢弃空块，不产生空信封、
   不留空行。
4. **上限与顺序**：行上限来自条目 `max_rows`（搜索 3 / 复盘 8）；块顺序 = 条目 `order`
   （缺省/重值按 key 字典序），与调用方传入顺序无关。
5. **信封安全**：`sanitize: rows` 下每个字符串值（含标量 cwd 这类路径）与每个 list 行都被
   `escape_harness_tags`、单行化、超长裁剪；`sanitize: none` 的 IRC/plan/assignment 字节
   不变。
6. **系统提示**：request 模板不进入 `assemble_main`；`<workstation>` 不再含
   host/home/cwd/scratch，也不含 date（date/cwd 走 `date_cwd_reminder`）；
   `tests/fixtures/prompt_surface/*` 由 P3b 用 `scripts/ci/prompt_surface.py render` 重新
   生成并附逐段 diff；`tests/agent/test_project_context.py` 的 home/scratch 断言同 PR 反转。
7. **P1/P2 注入**：每个请求至多一个同 id 块；不产生连续两条 user 行；回合中不注入；压缩后
   若最新 note 被丢弃则重新注入，否则读回抑制重复；复盘 fork 不写父会话 transcript。
8. **持久化**：搜索/复盘/首轮状态都在 `request_state` 缓存键里；resume / 每回合重建后
   `source` 直接读 SQLite 键，不依赖 agent 内存；已注入的 note 随 user 行持久化并可能重放，
   新 agent 读回最新 note 后不再重复 stage；未消费的复盘键在淘汰/resume 后仍注入一次，
   已消费的不重放；**没有任何 `session_notices` 表或 SessionDB schema 变更**。
9. **接线形状（D9）**：producer 只 stage；`turn_context` 不按模板逐个装载，session/pending
   的 context 全部由条目声明的 `source(agent)` 产生；字符串 user 行走 `api_content`，
   multimodal 走 durable text part；顺序由条目 `order` 决定（首轮提醒 → gate → 复盘技能 →
   复盘记忆 → 工具搜索 → 技能搜索）；空/未变化的 note 不产生字节；`_pop_turn_note` 后长驻
   agent 不重放。
10. **手动继续（D10）**：用户显式中断进行中的工具轮（`interrupt_issuer` 为 None，reason 为
    `explicit stop requested` / `user interrupt`）→ 记录 pending；下一条手动继续请求注入一次
    并清空；下一条新指令丢弃 pending、不注入；系统 issuer（lease/watchdog/TTSR）与其它
    Continue 动作不记录、不注入。
11. **模板为主**：`assemble_request` / `consume_request_notices` / `request_notices` 中
    grep 不到模板键；新增模板不改这三个文件的逻辑（只新增条目声明的 `source`/`producer`）；
    注册表驱动测试遍历所有 request 条目。
12. **载体限制**：MoA / `codex_app_server` 不产生搜索 note（缓存键仍写）；复盘 pending 键
    保留，直到下一次支持的模式；不得以改写 `content` 绕过。

---

## 分阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0（本次） | 4 个模板 + manifest 接线字段 + `render_request_block` + 定向测试；不接线 | 上述 2–5；`assemble_main` 字节不变（request 路径尚无调用点） |
| P1 | `request_state` 搜索回执 + turn-note 接线 + 读回去重 | 上述 7 的搜索半、8、9 的 stage/consume；不重复注入；fork 不采集 |
| P2 | 复盘写入回执 + 缓存 pending 键 + turn-note 接线 | 上述 7/8/9 的复盘半；本地化展示字符串不被解析；eviction/resume 后未消费批次仍注入一次 |
| P3a | 存量模板文本合并：unexpected-stop、ttsr-*、recap、btw、todo nudge | D10 验收 1–2；golden 字节；内联常量 grep 为 0 |
| P3b | 新 producer：tool loop、tool roster、session agent、session stop、auto-continue、interrupted-thinking、manual-continue、date-cwd、eval-prelude、eval-state | D10 验收 3–5；空/未变化零字节；gate 边界合法；`<workstation>` 删除行后快照重生成 |
| P3c | 既有 request 模板收敛到 `assemble_request`（`request_notice()`） | 上述 1；golden 字节逐字不变；调用点零 `renderer` 导入 |
| P4 | manifest 接线字段 + CI 审计 | 上述 11；release 无 `wired=false`；无“已登记但零调用”的模板 |

---

## 风险

| 风险 | 处置 |
|---|---|
| 每轮重复回执浪费 token | 读回最新 note 与当前快照比较，相同不 stage；复盘批次消费后删除；搜索最多 3 行 |
| 搜索快照陈旧，误导能力判断 | 文案声明当前 schema / 技能索引权威；只作回忆锚点 |
| 搜索回执写放大 | 每次搜索 upsert 一个 `request_state` 键（每会话固定 2 个搜索键），每 source ≤3 条；不按回合写、不追加历史 |
| 复盘回执泄露内容 | 只记名称与动作，单行裁剪；正文留在文件/用户端摘要 |
| 缓存键增长 | 每会话 ≤5 个键；命名空间 `max_entries=512` + LRU 淘汰旧会话；`/new` 不迁移旧键 |
| 注入破坏角色交替 | 合并进下一条 user 消息；独立行只在 assistant/tool 之后；回合中禁止；统一走 turn-note（D9） |
| 信封被查询文本伪造 | 统一 `escape_harness_tags` + 单行折叠；信封不设 budget 截断 |
| 复盘 fork 把回执写进主会话 | 写父会话的 `request_state` 键（`home=` 显式绑定 profile）；fork 自身 `_persist_disabled`、`session_db=None` 不变 |
| 缓存淘汰丢失回执 | 回执是提示性副作用，丢失只少一次提醒；`default_ttl=None` + 512 键上限把概率降到会话规模以下 |
| 第二个装配入口退化成第二套顺序/分派 | 与 `assemble_main` 同模块、共享 join 与 AST 边界；`order` 单调由注册测试守住；消费端没有模板键分支 |
| 模板条目与 ADR 表格漂移 | 条目是权威；注册表驱动测试遍历条目，表格只作设计记录 |
| 删除 host/home/scratch 后模型不知道路径习惯 | 它们本就可用 `pwd`/`$HOME`/`$TMPDIR` 现场取值；shell 提示（Windows bash）保留；不在每个请求里重复静态路径 |

---

## 不采纳

- **把搜索/复盘回执放进系统提示**：每个回合都变，破坏字节稳定与缓存；ADR-18 D10 / ADR-19 D5
  已排除。
- **新增常驻 `session_search` / `review_log` 模型工具**：信息是请求级副作用，不需要 schema；
  新增核心工具违反 narrow-waist 规则。
- **用展示摘要反推模型回执**：`background_review` 的摘要已本地化，前缀分类脆弱；必须用结构化
  采集。
- **为回执增加配置开关**：回执跟随工具搜索/复盘自身是否启用，不增加第二层开关。
- **新增 `session_notices` 表 / 搜索账本**：`hermes_cache` 已经提供 SQLite 持久化、profile
  解析、TTL 与 LRU；再建一套表、schema 与 mixin 是重复建设（D8）。
- **回执全文持久化进 transcript**：会与“临时前缀”语义冲突；复盘只保留 `request_state`
  键与消费标记，搜索只保留 `request_state` 快照与历史 note 的读回。
