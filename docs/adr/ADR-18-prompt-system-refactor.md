# ADR-18：提示词系统章节化与能力判定搜索（omp prompt-system 对齐）

**状态**：提议（2026-11-08）。分阶段：**P0** 章节注册表（零行为）；**P1** 常量迁移与预算；
**P2** 新章节；**P3** 统一 LRU 与判定搜索；**P4** 默认范围与工具清单；**P5** 清理与文档。

**决策摘要**：

1. 系统提示词从 Python 常量改为“**章节注册表 + Markdown 叶子文本 + Python renderer**”。
   装配入口仍是 `build_system_prompt_parts()`，仍返回 `stable/context/volatile` 三档。
2. 章节集合采用 `prmpt-system.md` 的 16 节，但**不采用它的 tier 顺序**：tier 由 Hermes 的
   缓存语义决定，`<project-context>` 不作为字面缓存断点。
3. 每节字符预算进 `config.yaml` 的 `prompts.budgets`；默认值先用实测上限，再按 eval 下调，
   不直接采用 omp 的 400/2400/2000/1000。
4. 新增章节：RFC 2119、safety、communication/personality、override、additionalCommunication、
   internal URLs、memories 包装、skills/rules、tool inventory、delivery、critical。
5. 能力搜索统一为“检索层 → aux LLM 生成 ≤3 条 `choice` 问题 → `judge_batch` → 合并去重”：
   `tool_search` 增加判定路径，`skills_list` 增加可选 `query` 参数，`judrg` 增加 ≤5 候选词
   扩展；全部可回退 BM25/rg。
6. 新增统一 LRU：上限、purpose 隔离、LRU/LFU/FIFO/TTL 策略、批量淘汰；`judrg` /
   `tool_search` / `skills_list(query=...)` 使用 `cap=10, evict=("lfu", 3)`；不替代判定
   答案 SQLite 缓存。
7. **P0/P1 必须让 `tests/ci/test_prompt_surface.py` 快照逐字节不变**；P2 起任何 prompt
   字节变化必须同 PR 重新生成快照并附维护者批准。

**关联**：ADR-4（eval prelude 明确不移植）、ADR-5（compaction/recap）、ADR-6（TTSR）、
ADR-11/ADR-17（judgment 旁路）、ADR-12（omp 工作流消费者）；
`website/docs/developer-guide/prompt-assembly.md`、
`tests/ci/test_prompt_surface.py`、`scripts/ci/prompt_surface.py`。

**取证口径**：设计源 `C:\Users\zarag\Documents\prmpt-system.md`；Hermes 当前工作树
（2026-11-08）。字符数用仓库 `.venv` 直接导入 `agent.prompt_builder` / `agent.coding_context`
测得；omp 行为只作为设计输入，不作为 Hermes 现有实现的证据。

**范围**：

- **做**：系统提示词章节化、预算、override、请求级注入的边界确认、tool/skill/judrg 的判定
  路由、统一 LRU、默认技能与工具清单、持久化与缓存不变量、CI 快照门。
- **不做**：新增通用 `judge` 模型工具；把请求级提醒搬进系统提示；迁移所有历史 ad-hoc 缓存；
  改变主 provider 路由；消息平台/Desktop/ACP 的专属 prompt 设计；eval prelude 本体。

---

## 问题

### 1. 提示词是代码常量，没有章节与预算

`agent/system_prompt.py:738` 的 `build_system_prompt_parts()` 按 `stable → context → volatile`
拼接，但所有正文都在 `agent/prompt_builder.py` 的 Python 字符串里。`agent/prompts/` 目前只有
judge JSON 与 `unexpected-stop-retry.md`，**没有 `agent/prompts/system/`**。结果是：

- 无法按章节配置预算、gating 或 override；
- 文案改动与代码改动混在同一 diff，review 看不到真实 prompt 文本；
- 运营者只能改代码，不能改 `$HERMES_HOME` 下的章节覆盖。

### 2. 缺少若干产品章节

主 prompt 没有：RFC 2119 约定、safety、communication/personality、additionalCommunication、
internal URLs、rules 索引、tool inventory、delivery、critical。现有安全语句散落在 coding
brief 与工具审批中；规则（TTSR）只在运行时匹配，不在 prompt 中可发现；工具清单只存在于
API `tools[]`，不在 prompt 中。

### 3. 能力选择只有词法检索

- `tools/tool_search.py:439` 的 `dispatch_tool_search()` 是 BM25，没有判定参与；
- `skills_list` 只有 `category` 过滤，没有语义检索或判定参与；默认技能索引是全量；
- `agent/judgment/find.py:640` 的 `judrg` 是“一条 query → 一次 intent 判定 → 一种 matcher”，
  没有候选词扩展与多结果合并。

### 4. 缓存各自为政

`agent/prompt_builder.py:1099` 技能索引 LRU（32）、`agent/prompt_cache_boundary.py`（32）、
`agent/conversation_loop.py` 参数规范化缓存、`agent/model_metadata.py` 多处缓存，各有自己的
上限与淘汰规则；没有 purpose 隔离，也没有调用方选择策略的接口。

### 5. 文档预算与 Hermes 实际内容不匹配

实测（字符）：

| 内容 | 实测 | 文档预算 | 结论 |
|---|---:|---:|---|
| `DEFAULT_AGENT_IDENTITY` | 663 | identity 2400 | 单 identity 可容纳 |
| `CODING_AGENT_GUIDANCE` | 2631 | identity 2400 | 若计入 identity 会截断 |
| `OPENAI_MODEL_EXECUTION_GUIDANCE` | 3952 | tool policy 2000 | 会截断 |
| `TOOL_USE_ENFORCEMENT_GUIDANCE` | 824 | tool policy 2000 | 合计已超 |
| `TASK_COMPLETION_GUIDANCE` | 769 | delivery 2000 | 可容纳 |
| `PARALLEL_TOOL_CALL_GUIDANCE` | 618 | tool policy 2000 | 合计已超 |
| memory guidance | 1036 | memories 2000 | MEMORY 2200 + USER 1375 后必超 |
| `MEMORY.md` / `USER.md` 上限 | 2200 / 1375 | memories 2000 | 会截断 |
| `KANBAN_GUIDANCE` | 6584 | critical 1000 | 仅 kanban worker 注入 |

这些常量带有实证原因（例如 `agent/prompt_builder.py` 中记录 Anthropic 内容过滤器对特定措辞
返回 400 的实验结论）。按文档数字硬截断是行为变更，不是排版调整。

### 6. 文档与代码已漂移

`website/docs/developer-guide/prompt-assembly.md:38` 写“skills are part of the **stable**
tier”，但 `agent/system_prompt.py` 把 `skills_prompt` 放进 `volatile_parts`。这次重构正好
触碰该表面，必须同一 PR 修正文档，否则章节 tier 设计会建立在错误前提上。

### 7. 任何 prompt 字节变化都受 CI 门控

`tests/ci/test_prompt_surface.py` 用 `scripts/ci/prompt_surface.py render` 渲染 9 个 surface
（cli × 4 模型族、code workspace、desktop、telegram、discord、cron），与
`tests/fixtures/prompt_surface/` 逐字节比较；`website/docs/developer-guide/contributing.md:354`
要求重新生成快照并附 `hermes-agent-core` 批准。因此重构计划必须把“零字节变化”作为 P0/P1
的硬验收，而不是“尽量不变”。

---

## 选项

| 选项 | 结论 | 理由 |
|---|---|---|
| A. 只改文案，不建章节/预算 | 否 | 无法满足预算、override、章节 gating 的需求；每节改动仍是代码 diff |
| B. 全量照搬 omp 章节顺序与模板引擎 | 否 | 破坏 Hermes 三档缓存与 6 处持久化解析器；引入 Handlebars 依赖；预算与实测冲突 |
| C. 分阶段：先零行为章节化，再迁移内容，再上判定搜索（本 ADR） | **采用** | P0/P1 可保持快照逐字节不变；P2 起每步都有可审的真实 prompt diff；判定搜索可独立回退 |

---

## 决策

### D1. 章节注册表：Markdown 叶子文本 + Python renderer

新增 `agent/prompts/system/` 与 `agent/prompt_sections.py`：

```text
agent/prompts/system/
  00-rfc2119.md
  01-identity.md
  02-safety.md
  03-communication.md
  04-internal-urls.md
  05-memories.md
  06-skills.md
  07-rules.md
  08-tool-inventory.md
  09-tool-policy.md
  10-workflow.md
  11-delivery.md
  12-critical.md
  subagent-role.md
  manifest.json
```

```python
@dataclass(frozen=True)
class PromptSection:
    key: str
    path: str | None
    tier: str                 # stable | context | volatile
    budget: int | None
    gate: str                 # always | tools | coding_repo | subagent | kanban
    override: bool = False
    truncate: str = "tail"    # tail | head_tail | drop_oldest_lines | error
    renderer: str | None = None
```

规则：

- **静态正文在 md**；动态内容（技能索引、记忆内容、工具清单、internal URLs、项目上下文）
  仍由 Python renderer 产出，md 只放包装与引导。
- 复用 `agent/advisor_prompts.py:142` 的 `render_system_prompt()` 模式：有序 block 列表，
  空块直接省略。**不引入 Handlebars**；文档的 `{{#if}}/{{#each}}` 编译为 Python gate 与
  renderer，md 只保留叶子文本。
- md 文件**进程内只读一次**，保证字节稳定；预算从 config 读取，改动在下一会话生效。
- `build_system_prompt_parts()` 签名与返回结构不变，`_cached_system_prompt_static`、
  `reconstruct_static_prefix()`、插件段恢复无需重写。

### D2. 章节到 tier 的映射（不照搬文档顺序）

| 章节 | tier | 门控 | 说明 |
|---|---|---|---|
| RFC 2119 | stable | always | 固定 400 字符，永不截断 |
| identity | stable | always | SOUL.md 或 `DEFAULT_AGENT_IDENTITY` |
| safety | stable | always | 策略性约束，不复述工具 schema |
| communication/personality | stable | always | 人格与输出风格；SOUL.md 可覆盖 |
| override communication | stable | override 存在时 | 完全替换上一节 |
| additionalCommunication | stable | `auxiliary.background_review.enabled` | 按行淘汰 |
| internal URLs | stable | always | 只放路径，不放内容 |
| rules | stable | coding_repo | TTSR 规则的名称、描述、路径 |
| tool policy | stable | tools | 现有 guidance 归并 |
| workflow | stable | always | coding brief 与工作流约束 |
| delivery / critical | stable | always | 现有完成度约束归并、去重 |
| memories | volatile | memory enabled | `<memories>` 包装；内容冻结快照 |
| skills index | volatile | skills tools | 保持现状（代码为准，文档待修正） |
| tool inventory | volatile | tools + tool_search 激活 | 只列延迟/门控工具组，避免重复计费 |
| project context | context | always | 现有 `.hermes.md` / AGENTS.md / CLAUDE.md / `.cursorrules` |
| subagent role | context | subagent | 由 `system_message` 或 agent 定义提供 |
| eval-prelude-notice | — | 未实现 | ADR-4 D10 不移植 prelude，本节 N/A |

说明：

- **skills 留在 volatile**。它是运行时可变内容，放 stable 会让一次技能写入改变 static
  前缀；`website/docs/developer-guide/prompt-assembly.md` 的 stable 说法是旧文档，P1 修正。
- **tool inventory 留在 volatile**。MCP 晚连接会改变下一轮工具面；若放进 stable，static
  前缀会被动变化。放在 volatile 尾与 `tools[]` 的变化边界一致。
- **`<project-context>` 不是缓存断点**。Hermes 的断点是
  `_cached_system_prompt_static` + `build_prompt_cache_plan(static_system_prefix=...)`；
  `<project-context>` 只作为内容包装。照搬文档断点会与现有机制重复。

### D3. 预算与截断

- 配置：`prompts.budgets.<section>`，`0` / 缺省 = 不限制；默认值 = `max(文档预算, 实测上限)`，
  由 P1 的测量脚本产出并提交。
- 截断策略：

| 策略 | 适用 | 行为 |
|---|---|---|
| `error` | RFC 2119、identity 的固定部分 | 超限直接拒绝构建并诊断，不静默截断 |
| `tail` | safety、communication、tool policy、delivery | 保留头部，尾部加 `[...truncated N chars...]` |
| `head_tail` | workflow、大段 rules | 头 70% + 标记 + 尾 20%（复用 `_truncate_content` 的比例） |
| `drop_oldest_lines` | additionalCommunication | 从首行开始丢弃，直到满足预算 |

- 每次截断发一条 `_emit_diagnostic_status`，每会话每节最多一次。
- 预算检查发生在构建时，不在会话中途；与现有“prompt 只构建一次”一致。

### D4. override 与 additionalCommunication

- override 路径：`$HERMES_HOME/agents/<agent_name>/prompts/<section>.md`；主会话
  `agent_name=default`。存在即**完全替换**基础章节，不做拼接。
- `additionalCommunication` 路径：
  `$HERMES_HOME/memories/<agent_name>/additionalCommunication.md`；主会话 `default`。
- 生产者：`agent/background_review.py` 在 review 成功且有可提取的交流风格时追加一行；
  `auxiliary.background_review.enabled` 关闭时不生成、不注入。
- **两个来源都过 `agent/prompt_builder._scan_context_content`**。它们是用户/AI 可写文件，
  不扫描就是新的 prompt 注入面；文档未覆盖这一点。
- `tools/agent_tool.py` 的 definition body 仍走 `system_message`（context tier），不当作
  override，避免同一角色内容出现两次。

### D5. memories 章节

```xml
<memories>
<memories-guidance>记忆使用与反馈向导</memories-guidance>
<memories-global>
<memories-user>跨会话稳定事实、偏好、交流风格</memories-user>
<memories-self>稳定的自我表示与工作标准</memories-self>
</memories-global>
</memories>

<memories-provider name="插件名">插件注入的信息</memories-provider>
```

- 内置 `MEMORY.md`（2200）与 `USER.md`（1375）继续作为冻结快照；包装层不改变内容。
- 预算默认 **5000**（guidance 1036 + 2200 + 1375 的实测上限），不是文档的 2000；否则会截断
  用户记忆。若产品决定收紧，必须先下调 `memory_char_limit` / `user_char_limit`，再同步预算。
- provider 块保持现有 `MemoryManager.build_system_prompt()` 的输出，只在外面包标签。

### D6. skills 与 rules

- **默认技能范围**：内置技能（`.bundled_manifest` 可判定）+ 插件注册技能保留完整名称与描述；
  用户/项目/hub/外部技能降级为 `[names only]` 一行，复用
  `agent/prompt_builder.py:1317` 的现有降级机制。
- 不默认彻底隐藏用户/agent 创建的技能：`agent/prompt_builder.py:1325` 已记录
  “NEVER drop entries — agent-created skills are the model's project memory”。彻底隐藏需
  显式配置 `skills.index.scope: builtin_plugin`，默认 `all+demote`。
- `skills_list` 增加可选 `query` 参数（D9），按需发现被降级的技能；不新增独立核心工具。
- rules 章节只在编码仓库拼接：按仓库语言组成列出 `$HERMES_HOME/rules` 与内置 rules 的
  名称、描述、路径；规则正文不注入（运行时由 TTSR 按条件匹配）。
- 非编码仓库不拼 rules、不拼 codegraph、不拼 coding workflow。

### D7. tool inventory 与默认工具集

- 文档列的 15 组在 Hermes 已存在（`toolsets.py:78` 起）：`browser`、`clarify`、
  `code_execution`、`cronjob`、`file`、`judgment`、`computer_use`、`session_search`、
  `skills`、`terminal`、`todo`、`vision`、`web`、`agents`、`codegraph`。不新增工具集。
- tool inventory **只列延迟/门控工具组**：当 `tool_search` 激活（tier > 0）时，列出工具组
  名称 + 描述 + 该组延迟工具名；eager 工具不列，因为 API `tools[]` 已经发送完整 schema，
  再列一遍是纯 token 重复。
- 复用 `tools/tool_search.py:238` `_search_description()` 的 listing 生成逻辑，不另写一套。
- `codegraph` 仅在编码仓库列出；`judgment` 组在无 native judge 且 `tools.enabled=auto` 时
  不列（与 `agent/judgment/find.py` 的 `judgment_tools_available` 一致）。
- 文档的 15 组只作为 **inventory 分组**，不作为“默认启用集合”去砍掉现有 `memory`、
  `rules`、`image_gen`、`tts`。

### D8. 统一 LRU

新增 `agent/cache/lru.py`：

```python
@dataclass(frozen=True)
class CachePolicy:
    max_entries: int
    eviction: str = "lru"       # lru | lfu | fifo | ttl
    evict_batch: int = 1
    ttl_s: float | None = None

class PurposeCache:
    def __init__(self, purpose: str, policy: CachePolicy): ...
    def get(self, key) -> Any | None      # 命中时 counter += 1
    def put(self, key, value) -> None
    def stats(self) -> dict
```

- 进程内、线程安全；按 **profile home + purpose** 隔离（一个进程服务多个 profile）。
- `judrg` / `tool_search` / `skills_list(query=...)` 使用
  `CachePolicy(max_entries=10, eviction="lfu", evict_batch=3)`，即文档的“最近 10 条、
  淘汰命中次数最少的 3 条”；同分按最久未使用淘汰。
- 键包含查询、cwd/scope、配置指纹；**必须带 TTL 或文件 mtime 指纹**（建议 `ttl_s=60`），
  否则改文件后返回陈旧搜索结果，这是文档漏掉的正确性风险。缓存命中也记录来源，诊断可见。
- 不与 `agent/judgment/cache.py` 混用：那是 SQLite 判定答案缓存（`max_states=50_000`），
  语义是“同 state 同问题”，不是“最近搜索结果”。

### D9. 判定路由

统一形态：

```text
query → 检索层（BM25 / rg）→ 候选 ≤K
      → aux LLM 生成 ≤3 条 choice 问题（候选名称+描述）
      → judge_batch(purpose=tool_choice | skill_choice)
      → 合并去重 → 统一 LRU → 返回
```

- **tool_search**：保留 BM25 召回；当 `tools.tool_search.judge=auto|on` 且 judge 可用、
  候选数 ≥ `judge_min_candidates`（默认 8）时，生成 ≤3 条 `choice` 问题，
  `judge_batch(purpose="tool_choice")`，判决结果与 BM25 命中合并去重。任一步失败回退 BM25。
- **技能搜索（扩展 `skills_list`，不新增核心工具）**：`skills_list` 增加可选 `query`
  参数。有 judge 且 `skills.search.enabled=auto|on` 时，候选来自现有 `find_all_skills()` +
  插件技能 catalog，生成 ≤3 条 `choice` 问题，`judge_batch(purpose="skill_choice")`；
  无 judge 时用 `rg` 对名称/描述/路径做词法匹配（复用
  `agent/judgment/find.py:370` `resolve_rg()`）。输出仍为名称+描述，加载仍走
  `skill_view`。选择扩展而非新工具的理由：`skills_list` 已在 `_HERMES_CORE_TOOLS` 中且已
  解析全部技能根，新增常驻 schema 违反 AGENTS.md 的 narrow-waist 规则；扩展只增加一个
  可选参数，且 `query` 为空时返回结果与当前实现一致（schema 仍会变化，见风险 9）。`skills.search.enabled=off` 只保留词法过滤。
- **judrg**：新增 `_expand_terms(query)`，aux LLM 生成 ≤5 个候选词；失败回退现有
  `search_terms()`。一次 `rg -e t1 -e t2 …` 或按 plan 执行；结果按
  `(path, line, range)` 去重合并；`purpose="find"` 不变。
- 每条 `choice` 问题的候选上限 8，问题数上限 3，避免撞原生 `max_questions` / criteria 上限。
- 不新增通用 `judge` 模型工具：模型不能传任意 questions/state；所有问题由产品工具构造
  （与 ADR-17 D1 一致）。
- 成本：复用 `judgment.cost_gate`；`judgment.tools.enabled=auto` 且无 native judge 时，
  `judgrg` 隐藏、`tool_search` 与 `skills_list(query=...)` 走词法回退。

### D10. 请求级注入与会话级重写保持不变

以下内容是“看起来像系统提示、实际是消息”，**不并入系统提示**：

| 机制 | Hermes 等价 | 位置 |
|---|---|---|
| 记忆召唤 | `<memory-context>` 围栏召回 | `agent/memory_context.py:169`，经 `agent/turn_context.py:118` |
| 附加提醒 | recap / no-tools reminder | `agent/recap.py:23` |
| 表面切换 | surface switch note | `agent/surface_switch.py:103` |
| 诊断通知 | `<system-notice>` | `agent/lsp/reporter.py:221` |
| 增强用户输入 | 无直接等价；`pre_llm_call` 上下文追加到 user 消息 | `website/docs/developer-guide/prompt-assembly.md` §API-call-time-only |
| 首轮日期/cwd | `Conversation started` + runtime env 已在系统提示内 | `agent/system_prompt.py:495` |
| 软工具约束 | 无；靠 `tools[]` + `TOOL_USE_ENFORCEMENT_GUIDANCE` | `agent/prompt_builder.py` |
| 压缩 summary | compaction summary + skill safety rule | `agent/context_compressor.py` / `agent/context_compressor_tail.py:156` |
| 记忆 resume 重放 | MEMORY/USER 冻结快照 + 每轮围栏召回 | `tools/memory_tool_store.py:649` |

新增 `<enhanced-user-input>`、`<additional-system-reminder>`、首轮 date-cwd 提醒**不在本 ADR
范围**；它们会与现有通道重复，并破坏“系统提示是唯一缓存指令面”的设计。

### D11. 缓存、持久化与 CI 不变量

1. **快照门**：`tests/ci/test_prompt_surface.py` 是硬验收。
   - P0/P1：快照逐字节不变；
   - P2+：任何 prompt 或 tool schema 变化，同 PR 执行
     `scripts/run-in-hermes-env python scripts/ci/prompt_surface.py render`，提交
     `tests/fixtures/prompt_surface/`，并在 PR 描述中附 diff；
   - md 提取必须保留原有 `\n\n` 连接符、空段丢弃与尾随换行，不能顺手 `.strip()` 改字节。
2. **持久化解析器**：改章节顺序或时间行位置时，必须同步更新并测试：
   - `agent/system_prompt.py:138` `_restore_plugin_prompt_sections`（插件段后必须紧跟
     `\n\nConversation started:`）
   - `agent/system_prompt.py:614` `_persisted_workspace_block`
   - `agent/surface_switch.py:39` `runtime_host_value`
   - `agent/surface_switch.py:53` `identity_line_value`
   - `agent/conversation_loop.py:958` `_stored_prompt_matches_runtime`
   - `agent/system_prompt.py:837` `reconstruct_static_prefix`
   - `agent/chat_completion_helpers.py::rewrite_prompt_model_identity`
3. **prompt schema 版本**：在 session `model_config` 记录 `prompt_schema`。旧会话继续逐字重放
   旧 prompt；`/new` 或无存储会话才使用新布局。不尝试原地升级旧字节。
4. **md 只读一次**：模板源只在进程启动后读一次；改 md 需重启。这是字节稳定的前提。
5. **会话中途不改 prompt**：预算、override、技能索引变化都在下一会话或下一次允许的重建
   边界生效；不在 turn 内重渲染。

### D12. 配置面

```yaml
prompts:
  budgets:
    rfc2119: 400
    identity: 2400          # P1 实测后可能上调
    safety: 1000
    communication: 1200
    additional_communication: 1000
    memories: 5000          # 与 MEMORY 2200 + USER 1375 + guidance 1036 对齐
    tool_policy: 6000       # P1 实测后收紧
    workflow: 3000
    delivery: 2000
    critical: 1000
  overrides_dir: ""         # 默认 $HERMES_HOME/agents/<agent_name>/prompts
  additional_communication:
    enabled: "auto"         # auto = 跟随 auxiliary.background_review.enabled

skills:
  index:
    scope: all+demote       # all+demote | builtin_plugin
  search:
    enabled: auto

tools:
  tool_search:
    judge: auto
    judge_min_candidates: 8
    max_questions: 3

cache:
  purposes:
    find:        {max_entries: 10, eviction: lfu, evict_batch: 3, ttl_s: 60}
    tool_choice: {max_entries: 10, eviction: lfu, evict_batch: 3, ttl_s: 300}
    skill_choice:{max_entries: 10, eviction: lfu, evict_batch: 3, ttl_s: 60}
```

新增的顶层键会被 `tests/ci/test_docs_drift.py` 的 ratchet 检查。**每个引入新键的阶段**
（P1 的 `prompts.*`、P3 的 `tools.tool_search.judge` / `cache.purposes`、P4 的
`skills.index` / `skills.search`）都必须在同一 PR 更新
`website/docs/user-guide/configuration.md` 并运行该测试，否则 PR 红。

---

## 协议与实现落点

| 组件 | 文件 | 内容 |
|---|---|---|
| 章节注册表 | 新 `agent/prompt_sections.py` | `PromptSection`、manifest 加载、预算、截断、gate |
| 章节正文 | 新 `agent/prompts/system/*.md` + `manifest.json` | 叶子文本与顺序 |
| 装配 | `agent/system_prompt.py` | 保持三档返回；改为调用 registry |
| 渲染器 | `agent/prompt_builder.py` | identity、skills、memory、context、internal URLs、rules、inventory |
| 统一缓存 | 新 `agent/cache/lru.py` | `PurposeCache`、`CachePolicy` |
| 工具搜索 | `tools/tool_search.py` | 判定路径、候选上限、回退、缓存 |
| 技能搜索 | `tools/skills_tool.py`（`skills_list(query=...)`） | catalog + rg + judge + 缓存 |
| judgrg | `agent/judgment/find.py` | 候选词扩展、合并去重、缓存 |
| 问题文件 | 新 `agent/prompts/judge/tool-choice.json`、`skill-choice.json`（`find.json` 同目录） | `choice` 问题模板 |
| 配置 | `hermes_cli/config_defaults.py` + 新 `config_defaults_prompts.py` | `prompts.*`、`skills.index/search`、`tools.tool_search.judge`、`cache.purposes` |
| 背景 review | `agent/background_review.py` | 产出 additionalCommunication，按预算裁剪 |
| 打包 | `pyproject.toml` | `prompts/system/*.md`、`prompts/system/*.json` |
| 文档 | `website/docs/developer-guide/prompt-assembly.md` | 修正 skills tier 漂移；补章节表 |
| 测试 | `tests/ci/test_prompt_surface.py`、新 `tests/agent/test_prompt_sections.py`、`tests/agent/test_lru_cache.py`、`tests/tools/test_skills_tool.py` | 快照、预算、缓存、回退 |

**不再保留**：迁移完成后删除 `agent/prompt_builder.py` 中被 md 取代的章节常量与
`agent/ttsr_prompts.py` 的模板字符串（保留渲染函数）；不保留 re-export shim。

---

## 分阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 章节注册表 + md loader；内容仍用现有常量渲染；快照不变 | `test_prompt_surface` 的 9 个 surface 通过；新增单测覆盖 subagent/kanban/no-tools 章节选择；无内容 diff |
| P1 | 常量迁移到 `agent/prompts/system/*.md`；预算配置面（先只告警）；修正 prompt-assembly 文档 | 快照逐字节不变；预算报告列出每节实测/上限；文档与代码 tier 一致；新增 `prompts.*` 已在 `website/docs` 文档化，`tests/ci/test_docs_drift.py` 通过 |
| P2 | 新章节：RFC、safety、communication/override、additionalCommunication、internal URLs、rules、delivery、critical；预算生效 | 同 PR 提交重新生成的快照；override 文件过注入扫描；截断策略测试；旧会话重放不变 |
| P3 | 统一 LRU + `tool_search` 判定 + `skills_list(query=...)` + `judrg` 候选扩展 | judge 不可用/报错 100% 回退；purpose 隔离；10/LFU-3 测试；成本门生效；`skills_list` 可选参数改动同 PR 更新工具快照；`tools.tool_search.judge` / `cache.purposes` 已文档化且 `test_docs_drift` 通过 |
| P4 | tool inventory + 默认技能范围 + codegraph/rules gating | token 增量有实测；agent 自建技能仍以名称行可见；非编码仓库不拼 codegraph/rules；快照更新；`skills.index` / `skills.search` 已文档化且 `test_docs_drift` 通过 |
| P5 | 删除旧常量/模板字符串；ADR/docs 收尾；清理测试 | `python scripts/check` 通过；无重复文案；无死代码 |

---

## 验收

1. P0/P1 的 `tests/ci/test_prompt_surface.py` 快照逐字节不变；P2+ 的 PR 均包含重新生成的
   快照与真实 prompt diff。
2. 每个章节在 `prompts.budgets` 下可配置；超限按 D3 策略确定性截断，且有诊断；RFC/identity
   固定部分超限直接失败。
3. override 与 additionalCommunication 文件均经过 `_scan_context_content`；注入命中按现有
   规则 block/warn。
4. additionalCommunication 在 `auxiliary.background_review.enabled=false` 时不生成、不注入；
   超预算从首行淘汰。
5. tool_search 在 judge 缺失、超时、解析失败时返回 BM25 结果；`skills_list(query=...)` 在无
   judge 时返回 rg/词法结果；judrg 在 aux LLM 失败时回退 `search_terms()`。
6. 三个搜索的缓存 purpose 分别为 `find` / `tool_choice` / `skill_choice`；容量 10、
   LFU-3、TTL 生效；命中计数可观测。
7. `_stored_prompt_matches_runtime`、`reconstruct_static_prefix`、插件段恢复、workspace 恢复
   在旧 prompt 与新 prompt 两种布局下都有测试；旧会话不因重构而重建。
8. 默认技能范围下，agent 创建的技能仍以名称行出现在 `<available_skills>`；`skill_view` 可加载。
9. 非编码仓库不出现 rules、codegraph、coding workflow 章节；编码仓库出现。
10. `pyproject.toml` 的 package-data 覆盖 `prompts/system/*.md` 与 `*.json`，wheel 安装后
    可加载。
11. 新增 `prompts.*` / `skills.index` / `skills.search` / `cache.purposes` 在 `website/docs`
    有对应条目，`tests/ci/test_docs_drift.py` 通过。

---

## 风险

1. **预算截断是行为变更**。文档数字低于 Hermes 实测（见问题 5）；P1 必须以实测为上限，
   P2 起每次下调都附 eval 或 prompt-surface diff，不能静默截断带实证原因的模型门控文本。
2. **技能发现回退**。把非内置技能降级为名称行会降低弱模型的主动加载率；若 eval 显示回退，
   回退到全量索引（config 开关），不硬推文档默认。
3. **tool inventory 重复计费**。只列延迟工具组；若实测 token 增量超过阈值（建议 2K tokens），
   改为仅列组名。
4. **判定搜索的延迟与成本**。每次搜索多一次 aux 调用 + judge；必须缓存、限额、回退；
   `cost_gate` 默认不动。
5. **override 注入面**。用户可写章节必须扫描；prompt 注入防护不能只依赖 system 角色。
6. **多 profile 串味**。缓存键、override 路径、additionalCommunication 路径都必须走
   `get_hermes_home()`；禁止硬编码 `~/.hermes`（HX001）。
7. **陈旧缓存**。代码搜索缓存没有 TTL 会返回改文件前的结果；D8 强制 TTL 或 mtime 指纹。
8. **顺序调整破坏解析器**。任何章节重排必须在同一 PR 更新 D11 列出的 7 处解析器与测试；
   否则持久化恢复会静默退化。
9. **`skills_list` schema 变化**。`query` 是可选参数，但 `tools[]` 快照仍会变化；P3 必须
   同 PR 重新生成 `prompt_surface` 快照。

---

## 不采纳

- **照搬 omp 章节顺序与 `<project-context>` 字面断点**：与 Hermes 三档缓存和 6 处解析器
  冲突；收益只是形状一致。
- **引入 Handlebars/模板引擎**：`agent/advisor_prompts.py` 已证明有序 block + Python
  renderer 足够；新依赖增加字节不确定性。
- **直接采用文档预算数字**：会截断 identity/coding/tool policy/memory 的现有内容。
- **把请求级提醒（memory recall、recap、surface note、LSP notice）并入系统提示**：
  破坏缓存前缀，且与现有通道重复。
- **默认彻底隐藏 agent 创建的技能**：与 `agent/prompt_builder.py:1325` 的既有设计冲突；
  默认 `all+demote`，彻底隐藏为显式配置。
- **在 prompt 中列 eager 工具**：与 `tools[]` schema 重复计费。
- **一次性迁移所有历史 ad-hoc 缓存**：本 ADR 只服务新消费者；旧缓存合并另立 ADR。
- **新增通用 `judge` 模型工具**：模型不能传任意 questions/state（ADR-17 D1）。
- **在无 native judge 时默认开启判定搜索**：`judgment.tools.enabled=auto` 保持现有门控，
  词法回退始终可用。
- **新增独立 `skill_search` 核心工具**：`skills_list` 已解析全部技能根；扩展一个可选
  `query` 参数即可，新增常驻 schema 违反 narrow-waist 规则。
- **首轮 date-cwd 提醒**：日期与 cwd 已在系统提示与 runtime env 中；再加一层会重复并破坏
  字节稳定。

---

## 遗留

1. `eval-prelude-notice` 依赖 ADR-4 的 prelude 本体；ADR-4 D10 仍有效，本节暂不实现。
2. `ast_edit` / `lsp` 的模型工具对齐不在本 ADR；omp 工具映射表只作为文档，不新增工具。
3. 预算收紧需要一套 prompt eval（golden 任务集 + 成本/正确率），本 ADR 只提供测量与门控。
4. additionalCommunication 的生产者质量（什么算“交流风格”）需要独立 eval；首版只做
   “用户纠正风格/语气/格式”三类信号。
5. tool inventory 的 token 增量需在 9 个 surface 上分别测量，再决定 P4 是否保留两层结构。
