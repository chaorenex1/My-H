# ADR-19：系统提示词装配器（`agent/prompts/assembler.py`）

**状态**：提议（2026-11-12）。**实施计划**：`docs/prompt-assembler-plan.md`（P0–P3 的任务、
文件、验收与 PR 拆分）。

**装配器契约（2026-11-12 修订）**：`assemble_main(ctx) -> str`——按代码顺序调用 renderer 的
块入口（`render_rfc2119` / `render_role` / `render_runtime` / `render_tool_policy` /
`render_memories` / `render_closing` / `render_project_context`），每块返回**完整正文**，空串
丢弃，`"\n\n"` join 成一段正文；顺序、tier 归属与段级开关都在块方法内部。装配器没有 tier
桶、没有 gate 表，也不按 manifest 顺序逐章二次渲染；manifest 只做模板注册与 `scope`。
**不引入 `PromptTiers` 或任何 tier 容器**：D1 的返回类型是 `str`，D9 的 `assemble_subagent`
直接在 `assemble_main` 的字节后追加角色块。D1/D3/D4 中与装配结构相关的描述以本节为准。
模板、请求级模板、预算/截断（ADR-18 D3/D13）不变。

**关系**：**本 ADR 修订 ADR-18**，不是并列新增。ADR-18 只规定了"要有一个装配器"，没有规定它
的位置与输入边界；本 ADR 补上边界，并**否决**上一轮讨论里"装配器按槽位兼容旧正文"的方案
（该方案未落库）。

被修订的 ADR-18 条款：

| ADR-18 | 原内容 | 本 ADR |
|---|---|---|
| D1 | "Markdown/Jinja2 叶子模板 + Python 装配器"；装配入口实现落在 `agent/system_prompt.py` | 装配器落在新 `agent/prompts/assembler.py`；`system_prompt.py` 只做 context 采集与缓存 |
| 协议与实现落点表 | `装配 \| agent/system_prompt.py \| 保持三档返回；改为调用 registry` | `装配 \| agent/prompts/assembler.py \| 代码顺序/块入口/一次 join`；`system_prompt.py` 改为调用装配器 |
| P0/P1 | 常量迁移期要求快照逐字节不变 | 语料替换是本 ADR 的目标，切换 PR 重新生成快照（D7） |
| D2 | `project_context` 归入 `context` tier | `project_context` 改 `volatile`，作为主代理尾块（装配结果的最后一个块） |

**不变**：D3 预算截断、D13 Jinja2 边界、D11 的持久化与缓存不变量。

代码里现有的 `ADR-18 D1/D3/D13` docstring 引用对 registry / renderer / Jinja2 仍然成立；
描述"装配入口"的 docstring 由 P1 的接线 PR 一并改为 ADR-19，不在本 ADR 提交里改代码。

**决策摘要**：

1. 新增 `agent/prompts/assembler.py`。它**只**装配 `agent/prompts/system/` 下的模板：
   输入是 `RenderContext`（含 `AssemblyInputs` 与会话路径），分段正文由 `renderer.py` 提供，
   输出是一段正文（块 join）与 `§ Role` / `§ Runtime` / 尾块的完整字节。
2. 装配器**不装配 Hermes 的旧内容**：不读 `agent/prompt_builder.py` 的常量，不调
   `AIAgent`，旧正文不作为输入、不作为回退、不作为"live 槽位"并存。数据读取（技能、规则、
   记忆、override、project context）由 `renderer.py` 的分段方法承担；装配器只编排、不取数。
3. Hermes 的调用点只做两件事：求值 `AssemblyInputs` / `RenderContext`，调用装配器。
   `system_prompt.py` 退化为数据采集与缓存 facade（`RenderContext`、`_cached_system_prompt(_static)`），
   三档的 join 由 `assemble_main()` 完成，facade 不再持有块列表或顺序。
4. 切换是**语料替换**，不是容器替换：第一次接线就会换掉 identity / safety / communication /
   workflow / delivery / critical 的正文，因此不承诺字节不变；快照在同一 PR 重新生成并附 diff。
5. `manifest.json` 增加 `scope`（`system` / `request`）。装配器只取 `scope=system`；`irc`、
   yield reminder、eager 提醒等请求级模板继续由各自调用点渲染，不进入系统提示。
6. **单一拼接器**：对**已接线**的章节，装配器一次 `join` 出字节，调用方不得追加或前置任何块；
   需要新块 = 新模板 + manifest 条目。未接线的载体（`system_message`、插件段、`project-prompt.md`
   既有块）见 D5-B，不在此约束内。
7. **子代理 = 主代理基础提示 + 角色块**（omp 形状）：`assemble_subagent()` 复用
   `assemble_main()` 的字节，再在末尾追加 `§ Role` 块；不再有独立的子代理语料。

**范围**：

- **做**：装配器的边界、输入契约、块顺序、段级开关归属、旧块处置清单、切换阶段、
  持久化解析器同步、测试与 CI 门。
- **不做**：请求级注入的通道（ADR-18 D10 不变）；judge 路由与 LRU（ADR-18 D8/D9）；
  `agent/judgment/rendering.py` 的方言渲染器；子代理角色文件的内容设计；
  **override / additionalCommunication（ADR-18 D4）——暂不纳入本 ADR，另行排期**。

---

## 问题

### 1. 装配零件齐了，装配器不存在

`render_head()` / `render_runtime()` / `render_closing()` / `render_role()` /
`render_memories()` 在 `agent/prompts/renderer.py` 里能渲染，但生产代码零调用。真正产出系统
提示的仍是 `agent/system_prompt.py:807` 的 `build_system_prompt_parts()`，它用
`prompt_builder.py` 的常量按 `stable → context → volatile` 拼装。结果是：

- `manifest.json` 的 15 个系统章节里只有 `tool_policy` 与 `project_context` 已接线
  （`system_prompt.py:580`、`:785`）；identity / safety / communication / memories / skills /
  rules / tool_inventory / workflow / delivery / critical 等仍是零调用；
- `PromptSection.gate` 被解析后无人读取（`prompt_sections.py:56,79`）；
- 章节顺序、tier、gate 三份知识只存在于 `manifest.json`，运行时用的是 `system_prompt.py`
  里的另一套手写顺序。

### 2. 排序知识已经分裂，并已经漂移

`build_system_prompt_parts()` 的 volatile 顺序是
`skills → memories → plugin(after_memory) → profile_line → timestamp`；
`_restore_plugin_prompt_sections()`（`system_prompt.py:155`）却要求插件块之后紧跟
`"\n\nConversation started:"`。实测真实 prompt 的插件块后面是 profile line，因此恢复恒返回
`()`——恢复路径静默丢弃插件段，只落回重渲染。这是"顺序写在两个地方"的既存事故，不是装配器
引入的风险。

### 3. 上一轮的 live 槽位方案会把双栈固化

"装配器按槽位合并旧正文"（identity 用 `DEFAULT_AGENT_IDENTITY`、tool policy 用
`TOOL_USE_ENFORCEMENT_GUIDANCE` 等）需要 manifest 同时表达 `live` / `template` / `merged`
三种来源，并长期维护两套正文与 parity 测试。旧栈永远删不掉，装配器被迫理解
`prompt_builder.py`，与"装配器只服务 `system/`"的边界相反。否决。

---

## 选项

| 选项 | 结论 | 理由 |
|---|---|---|
| A. 装配器按槽位兼容旧正文（live/merged 来源） | 否 | 双栈长期并存；装配器依赖旧常量；parity 测试成为永久负担 |
| B. 装配器只装配 `system/`，旧正文逐块退役，一次切换 | **采用** | 单一正文来源、单一顺序、单一拼接器；退役清单可见、可审 |
| C. 维持现状（常量拼装 + 未接线的 renderer） | 否 | 章节/预算/override/gate 永远落不了地；`prompt_sections` 是死代码 |

---

## 决策

### D1. 边界：装配器只做编排，分段渲染交给 `renderer.py`

```python
# agent/prompts/assembler.py
@dataclass(frozen=True)
class AssemblyInputs:
    """调用方求值后的会话事实；装配器不自己推导任何一项。"""
    tools: frozenset[str]          # 已解析的工具名（含 PM_DEFAULT_TOOLSETS 的 rg / ast-grep）
    subagent: bool
    memory: bool                   # 记忆章节开关

@dataclass(frozen=True)
class SessionFacts:
    """渲染器需要的会话事实；调用方求值一次，避免渲染器反查 AIAgent。"""
    platform: str = ""                     # identity caps / communication hint
    model: str = ""                        # project-prompt <workstation>
    provider: str = ""                     # 同上
    session_id: str = ""
    context_length: int | None = None      # 上下文文件预算
    context_cwd: str = ""                  # 项目上下文探测目录
    cwd_is_launch_artifact: bool = False   # desktop 启动目录保护
    skip_context_files: bool = False
    workspace_roots: tuple[str, ...] = ()  # <workspace-roots>
    toolsets: tuple[str, ...] = ()         # 技能索引 / 工具清单分组
    memory_provider: str = ""              # 外部记忆块（已渲染）
    delegation_policy: Mapping[str, Any] = field(default_factory=dict)
    append_prompt: str = ""
    agent_definition: Mapping[str, Any] | None = None

@dataclass(frozen=True)
class RenderContext:
    """传给每个渲染器方法的不可变上下文；由调用方构造一次。"""
    inputs: AssemblyInputs
    facts: SessionFacts = SessionFacts()
    agent_name: str = "default"
    home: Path | None = None
    project_dir: Path | None = None
    first_input: str | None = None

@dataclass(frozen=True)
class SubagentRole:
    """子代理的角色块数据；模板 system/subagent-system-prompt.md 的入参。"""
    body: str                       # agent 定义正文（frontmatter 已剥离）
    context: str = ""
    plan_reference: str = ""
    plan_reference_path: str = ""
    worktree: str = ""
    irc_self_id: str = ""
    irc_peers: tuple[str, ...] = ()
    structured: bool = False
    yield_schema: str = ""

def assemble_main(ctx: RenderContext) -> str: ...
def assemble_subagent(ctx: RenderContext, role: SubagentRole) -> str: ...

# agent/prompts/renderer.py —— 块入口返回完整正文
render_rfc2119() / render_role() / render_runtime() / render_tool_policy()
render_memories() / render_closing() / render_project_context()
```

**职责表**（相邻两层，谁都不越界）：

| 模块 | 负责 | 不负责 |
|---|---|---|
| `assembler.py` | 代码顺序、调 renderer 块入口、丢弃空块、一次 `join` | 读盘、构造段 context、正文、override |
| `renderer.py` | 块入口 `render_*(...) -> str`：采集该块需要的数据（skills 行、rules、urls、memories、tool_groups、project context…），处理 override（读取 + 注入扫描），调用 `prompt_sections.render_section` | 决定块间顺序 |
| `prompt_sections.py` | manifest 解析、context 白名单/类型校验、Jinja2 渲染、预算与截断 | 段选择与顺序 |
| `system_prompt.py` | 求值 `AssemblyInputs` / `RenderContext`、缓存与持久化 | 拼装与 join |

- 装配器按代码顺序调用 renderer 的块入口；块方法自己决定内容与段级开关，装配器不做
  manifest 分派，也不存在“登记了但没人渲染”的死条目。
- 装配器自身不含 `open` / `read_text` / `Path` 调用；数据获取是 renderer 的职责。确定性契约
  因此是"同一 `RenderContext` + 同一磁盘/配置状态 → 同一字节"，而不是"无 IO"。
- override 正文由 renderer 读取、扫描并整段替换模板输出；装配器不知道 override 存在。

允许的导入：`prompt_sections`、`renderer`、`dataclasses`/`typing` 等 stdlib。**禁止**导入
`agent.system_prompt`、`agent.prompt_builder`、`run_agent`、`hermes_cli.config`（装配器不得
反向依赖 facade 或旧正文）。

机械约束：`tests/agent/test_prompt_assembler_boundary.py` 用 AST 检查 `assembler.py` 的导入
集合与"无直接文件读取"；`tests/agent/test_prompt_assembler.py` 用 stub 块入口断言调用顺序 ==
代码顺序、每块恰好一次、空块丢弃。

### D2. 输入契约：调用方给事实，渲染器给正文，装配器只给结构

- `AssemblyInputs` 是 gate 与工具面的唯一来源；`RenderContext` 由 `SessionFacts`（平台、模型、
  provider、session、context 长度、cwd、workspace roots、toolsets、已渲染的记忆 provider 文本、
  delegation policy 等）+ 路径/名称组成。两者都由调用方构造一次，渲染器不反查 `AIAgent`。
- 每节 context 的键集合与类型继续由 `PromptSection.context_keys` / `context_types` 声明，
  `StrictUndefined` 保证漏键即失败；renderer 方法只传 `str | int | bool | list[str]`，
  不把对象/`Path`/回调透给模板。
- 工具名解析与 `PM_DEFAULT_TOOLSETS` 合并由调用方求值；`coding_repo`、`first_input`、
  平台提示、自求助与 kanban 判定，以及技能行、规则、记忆、override 等数据读取都在
  **renderer** 完成。
- **`project_context` 是主代理尾块**：`project-prompt.md` 由
  装配器在最后一个块调用 `render_project_context(ctx)`；manifest `tier: volatile` 且条目
  排在 `scope=system` 章节的最后（登记元数据）。
  `system_prompt.py` **不得**直接调用 `render_project_context`（`_context_files_part` 删除），
  只提供 `SessionFacts`（`context_cwd`、`workspace_roots`、`context_length`、`platform`、
  `append_prompt` 等）。子代理的 base 同样以该尾块结束，角色块再追加在其后。
- 契约是**确定性**：相同 `RenderContext` + 相同磁盘/配置状态 + 相同模板字节 → 相同输出。
  装配器不缓存、不重读、不做隐式探测；每次 `assemble_main()` 的结果只取决于
  renderer 块入口的当前实现。
- 同一份 `RenderContext` 在整次装配中只求值一次：tools/工具面不重复解析，renderer 不在
  同一次装配里读两遍同一个文件。

### D3. gate 与段级开关：块方法决定，manifest 只登记

- `gate` 是 manifest 的登记元数据，由 `prompt_sections.load_sections` 做白名单校验；装配器
  不读它，也没有 gate 求值表。
- 段级开关在块方法内部执行：`tools` 为空 → 09 零字节；`coding_repo=False` → 10 省略；
  `memory` 关闭 → 05 零字节；`subagent` → 10/11/12 换措辞。开关为假 = 零字节，不留空行、
  不降级为占位文本。
- 段内能力门（`{% if "tool" in tools %}`，ADR-18 D14）不变；模板不自行判断整段存废。
- `background_review` 等 gate 当前没有登记章节；`additional_communication` 暂缓。

### D4. 顺序与标题

- 装配顺序 = `assemble_main()` 的代码顺序：`rfc2119` → `role`（01–03）→ `runtime`
  （04/06–08）→ `tool_policy`（09）→ `memories`（05）→ `closing`（10–12）→
  `project_context`（尾块）。
- manifest `tier` 仅登记元数据，不参与装配；没有 tier 分桶，也不按 manifest 顺序逐章
  二次渲染。
- `§ Role` / `§ Runtime` / `§ Workflow` / `§ Delivery` / `§ Critical` 等标题属于模板正文，
  由模板产出；Python 不添加、不改写标题。
- join 分隔符固定 `"\n\n"`，空段丢弃。`build_system_prompt_parts()` 的三档返回仅为兼容
  facade 保留：`stable` = 装配正文，`context` = 调用方 `system_message`，`volatile` = 空串。

### D5. 旧块处置：正文直接删除，不吸收

原则：

1. `system_prompt.py` / `prompt_builder.py` 里的旧**提示正文**不做"吸收 / 改写 / 并段 / 搬进
   context"。切换 PR 直接删除它们；新章节正文以 `system/*.md` 现有内容为准，独立撰写、独立评审。
2. 运行时**数据**不是旧块：记忆快照、技能行、规则行、工具分组、工作区、`system_message` 等
   由 renderer 读取后按 B 表落位。它们没有正文语义，本来就不属于"旧提示文本"。
3. 新语料没有对应章节的旧能力，切换后**不再出现**。这是明确的行为变更，靠 9 surface 的
   prompt diff 评审，不补补偿文案、不保留兼容块。

**A. 删除的旧提示正文**（切换 PR 同 PR 删除，`grep` 不到）：

| 旧块 | 处置 |
|---|---|
| `SOUL.md` 注入 / `DEFAULT_AGENT_IDENTITY`（旧 Hermes 身份行） | 删除，不再注入。`01-identity.md` 的正文与默认值保持原样；`role` / `role_desc` 仅作可选覆盖 |
| `TASK_COMPLETION_GUIDANCE` | 删除；不并入 11 delivery，11 的正文独立存在 |
| `PARALLEL_TOOL_CALL_GUIDANCE` | 删除 |
| `TOOL_USE_ENFORCEMENT_GUIDANCE` / `GOOGLE_MODEL_OPERATIONAL_GUIDANCE` / execution guidance | 删除；09 正文不搬运这些段落 |
| `STEER_CHANNEL_NOTE` | 删除 |
| `build_memory_guidance` / `SESSION_SEARCH_GUIDANCE` / `SKILLS_GUIDANCE` | 删除；05 / 06 正文独立 |
| `HERMES_AGENT_HELP_GUIDANCE` / `_NO_SKILLS` 常量 | 删除；文本本身在 `system/hermes-agent-help-guidance*.md`，属于模板资产，不是被吸收的旧块 |
| `KANBAN_GUIDANCE` 常量 | 删除；文本在 `system/kanban-guidance.md` |
| `_alibaba_identity_part` | 删除 |
| coding brief（`coding_system_prompt_parts()[0]`） | 删除 |
| `PLATFORM_HINTS` | 删除；平台默认提示改由 `system/platforms/<platform>.md` 提供（ADR-18 D1） |
| `_tool_policy_part()` 的正文 | 删除；09 由 `renderer.render_tool_policy()` 独立产出 |
| `_timestamp_line`（时间 / session） | 删除；改走首轮请求级注入（不在本 ADR 实现范围） |
| `_active_profile_line` | 删除；profile 名改为 `01-identity.md` 顶部的 context |
| `RUNTIME_ENVIRONMENT_HEADING` 块 | 删除；环境信息并入 `project-prompt.md` 的 `<workstation>`，不再有独立块 |
| bot-mode protocol | 删除；不进入系统提示。`Capability epoch:` 的唯一写入点随之消失，Bot Chat 的能力刷新需另行保留 stamp 或显式记录为行为变更 |

**B. 运行时数据的落点与接线状态**（不是旧块）：

| 数据 | 落点 | 接线 |
|---|---|---|
| memories 快照 + provider 块 | 05 memories | 接线 |
| 技能索引行 / 钉住的技能正文 | 06 skills / `autoload-skills.md` | 接线 |
| 规则名称、描述、路径 | 07 rules | 接线 |
| 工具组与延迟工具名 | 08 tool-inventory | 接线 |
| model / provider | `project-prompt.md` 的 `<workstation>`（现有） | 接线（现有） |
| 工作区快照 / workspace-roots / active repo | `project-prompt.md`（现有）；`workspace_tree` 由 `project_context.py` 内部解析并缓存（D10）；`workspace.md` 只建文件 | **不接线** |
| context files / 目录规则 / append 尾块 | `project-prompt.md`（`project_context` 尾块） | 接线（装配器尾块） |
| 插件段（`after_memory`） | `plugin-sections.md` 只建文件 | **不接线** |
| 调用方 `system_message` | `system-message.md` 只建文件；现有路径保持不变 | **不接线** |
| 时间 / session | 删除；首轮请求级注入 | 不在本 ADR |
| Hermes 自求助 / kanban 判定结果 | 04 internal-urls 的 `hermes_help_guidance` / `kanban_guidance` | 接线 |
| 平台命中文本 | 03 communication 的 `platform_hint` | 接线 |
| profile 名 | `01-identity.md` 顶部 | 接线 |

**只建文件 = 不登记 manifest**：装配器只渲染 manifest 登记且 `scope=system` 的章节；未登记的
`.md` 是占位资产，不参与装配、不影响字节。`workspace.md`、`plugin-sections.md`、
`system-message.md` 按此处理（其中 workspace 的内容已在 `project-prompt.md`）。
`runtime-state.md`、`environment.md`、`profile.md` **不创建**：时间/session 归首轮请求级注入，
环境信息只保留 `<workstation>` 的 OS/Arch（host/home/cwd/scratch 删除，ADR-20 第二轮修订），
profile 名归 identity 顶部。

已接线的章节必须登记 manifest，装配器不接受"这一节由 renderer 或调用方临时补"的写法。

### D6. 调用点：一个入口，两层职责

| 层 | 文件 | 职责 |
|---|---|---|
| 采集 + 缓存 | `agent/system_prompt.py` | 求值 `AssemblyInputs` / `RenderContext`、调用 `assemble_main()`、写 `_cached_system_prompt(_static)` |
| 装配 | `agent/prompts/assembler.py` | 代码顺序、调 renderer 块入口、丢弃空块、一次 join |
| 分段渲染 | `agent/prompts/renderer.py` | 块入口返回完整正文：取数 + `render_section` |
| 正文 | `agent/prompts/system/*.md` | 全部提示文本 |
| 子代理 | `agent/agent_run.py` | `assemble_subagent(ctx, role)` = `assemble_main()` + 角色块；不再单独拼角色提示 |
| 请求级注入 | `agent/prompts/assembler.py` + 现有调用点（`turn_context.py`、`agent_run.py`） | `assemble_request()` / `request_notice()`：按模板条目排序、一次 join（ADR-20 D3）；不经过系统提示装配 |

`build_system_prompt_parts()` 的签名与 `{"stable","context","volatile"}` 返回结构不变，
`reconstruct_static_prefix()`、`_cached_system_prompt_static` 的语义不变。子代理走
`assemble_subagent()`，复用同一 registry 与 gate 求值器。

`system-message.md` 不接线：`system_message` 暂维持现有 context-tier 路径。"单一拼接器"约束
只覆盖**已接线**的章节，占位文件与现有路径不参与装配。

`project_context` 已接线且由装配器负责：`system_prompt.py` 只填 `SessionFacts`，不调用
`render_project_context`；`project-prompt.md` 是装配结果的最后一个块。

### D7. 切换、持久化与缓存不变量

1. **一次切换，无长驻开关**。`prompts.backend: legacy|assembler` 这类兼容开关不引入
   （回滚 = revert）。切换 PR 同时删除被取代的常量与块列表，不留 re-export shim。
2. **快照**：切换 PR 同 PR 运行
   `python scripts/ci/prompt_surface.py render`，提交 9 个 surface 的新快照，并在 PR 描述里
   附逐段 diff。不使用 `tests/fixtures` 里的旧快照做"尽量不变"的凑字节。
3. **持久化解析器**必须同 PR 更新并各有一条定向测试：
   - `_restore_plugin_prompt_sections`：插件段不接线，恢复逻辑维持现状；其"插件块紧跟时间行"
     的严格邻接缺陷单独修复，不阻塞本 ADR。
   - `_persisted_workspace_block` / `_frozen_workspace_snapshot` / `_seed_workspace_pin`：
     工作区快照不再进入系统提示，这些冻结/校验逻辑同 PR 删除，或改为只读 `project-prompt.md`
     的 `<workspace-tree>` / `<workspace-roots>`。
   - `surface_switch.runtime_host_value` / `identity_line_value`：`# Hermes runtime environment`
     块删除后，host/home/cwd/scratch 都不再从 `<workstation>` 读取（ADR-20 第二轮修订把这些
     行直接删除）；`identity_line_value` 只读 Model/Provider/Platform。
   - `conversation_loop._stored_prompt_matches_runtime`：model 校验锚 `<workstation>` /
     `<project-context>`；cwd 校验改锚 `date_cwd_reminder` 行的 `cwd`（与构建提示同一
     resolver），旧会话回退旧提示行（ADR-20 D10）。
   - `chat_completion_helpers.rewrite_prompt_model_identity`：model/provider 锚点保持在
     `<workstation>`。
   - 时间/session 不再出现在系统提示：`_timestamp_line` 及其读取方删除。

> **ADR-20 第二轮修订**：日期/cwd 改走请求级 `date_cwd_reminder`；host/home/scratch 从
> `<workstation>` 直接删除（无替代载体）。`surface_switch.runtime_host_value` /
> `conversation_loop._stored_prompt_matches_runtime` 的 cwd 锚点改读 `date_cwd_reminder` 行
> （旧会话回退旧提示行）。请求级模板统一经 `assemble_request()` 渲染。

4. **prompt schema 版本**：session `model_config` 记录 `prompt_schema: 2`。旧会话逐字重放旧
   prompt，不原地升级；`/new` 或无存储会话才使用装配器产物。
5. **进程内只读**：模板源一次读入，改 md 需重启（ADR-18 D13 不变）。

### D8. 失败策略

| 情况 | 行为 |
|---|---|
| manifest 缺模板 / 未知 gate / 未知 truncate | `PromptSectionError`，构建失败 |
| context 缺键或类型不符 | `UndefinedError` / `PromptSectionError`，构建失败 |
| `truncate: error` 超预算 | `PromptBudgetError`，构建失败（不许调大预算掩盖） |
| gate 为假 | 正常，零字节 |
| override 文件含注入（**暂缓，见分阶段**） | `[BLOCKED: ...]` 占位（ADR-18 D4） |

不允许"装配失败回退旧常量"：旧常量已删除，回退路径不存在。

### D9. 子代理系统提示 = 主代理基础提示 + 角色块（omp 形状）

omp 的子代理 system prompt 不是另一套语料：主会话 base 块之后追加一块 `§ Role`。
Hermes 采用同一形状：

```python
def assemble_subagent(ctx: RenderContext, role: SubagentRole) -> str:
    base = assemble_main(ctx)              # 与主代理同一语料、同一块顺序
    return f"{base}\n\n{render_subagent_prompt(role)}"
```

- **位置固定**：角色块永远在 base 之后（volatile 尾部之后），不插进 stable/context，也不改写
  base 的字节。同一批子代理因此共享完全相同的 base 前缀；差异只在尾部角色块，缓存复用最大化。
- **同一语料**：base 由 `assemble_main()` 产出，`inputs.subagent=True` 让 10 workflow 走
  Hand-off、11 delivery 走 slice、12 critical 用对应措辞；不再有"子代理专用系统提示"。
- **标题去重**：`01-identity.md` 的 `§ Role` 保留不动（base 的字节与主代理完全一致）；
  `subagent-system-prompt.md` 的首行从 `§ Role` 改为 `§ Subagent Role`，Context/Plan/Coop/
  Peers/Completion 子标题不变。最终 prompt 里 `§ Role` 恰好一次、`§ Subagent Role` 恰好一次，
  不出现两个同名顶层段。
- **角色块**：`render_subagent_prompt(role)` 渲染 `system/subagent-system-prompt.md`
  （§ Subagent Role / § Context / § Plan / § Coop / § Peers / § Completion），只由 `SubagentRole`
  驱动；`body` 为空 → 构建失败，不回退到"裸 prompt"。
- **删除旧路径**：`agent_run._subagent_prompts()` 里"只有角色块"的分支与异常回退一并删除；
  `render_subagent_system_prompt()` 改为 `render_subagent_prompt(role: SubagentRole)`，成为角色块
  的唯一入口。**不要**复用 `render_subagent_role`：该名字已属于请求级 `subagent_role` 模板。
- **工具面**：`AssemblyInputs.tools` / `skills` / `tool_search` 由子代理自己的工具集求值，
  不是主代理工具面的副本。

### D10. `workspace_tree` 内部解析与缓存（`project_context.py`）

现状：`render_project_context(..., workspace_tree=..., workspace_tree_truncated=...)` 由调用方
传入已渲染的树；生产代码没有任何生成者。改为**渲染器自己按路径解析**：

```python
def effective_project_dir(project_dir: Path | None = None) -> Path: ...
def workspace_tree(project_dir: Path | None = None, *, depth: int = 3,
                   session_id: str = "") -> tuple[str, bool]: ...
```

- **删除外部参数**：`render_project_context` 去掉 `workspace_tree` / `workspace_tree_truncated`；
  调用方与装配器只传 `cwd`（`project_dir`）。
- **启用门（必须有消费者）**：编码仓库才扫描并渲染，非仓库完全不扫描；`coding_repo` 由
  渲染器自行判定（与 `render_runtime` 同一判定），不新增配置开关，也不进 `AssemblyInputs`。因此字节变化只发生在 `coding_repo=True` 的 surface（P4 重生成 code workspace
  快照），其余 surface 不变。
- **解析规则**：`Working-directory layout: newest mtime first; depth ≤ 3.`；每层按 mtime 新→旧
  排序，目录名带 `/`，2 空格缩进，深度 ≤ 3；超过条目上限置 `truncated=True`（模板已有
  "Some entries elided…" 提示）。
- **会话内冻结（缓存不变量）**：树落在主代理尾块（`project_context`，volatile），压缩重建会
  再次调用渲染器；1h TTL 在会话
  中途过期会改变已有前缀。因此第一次解析结果按 `(profile, session_id, path, depth)` 在进程内
  固定（有界 LRU，例如 `lru_cache(maxsize=64)`），本次会话后续重建复用同一字节。`hermes_cache`
  的 1h TTL 只服务**跨会话**复用，会话内不得因过期而重算。
- **跨会话缓存**：走 `hermes_cache` 公共 API，
  `get_cache().namespace("prompt_workspace_tree", default_ttl=3600, max_entries=32,
  eviction=EvictionPolicy.LRU)`（在调用时通过 `get_cache()` 创建，解析活动 profile）。key =
  `f"{resolved_path}|d={depth}"`，value = `{"tree": str, "truncated": bool}`（JSON 可序列化）。
  缓存 miss/异常一律回退现场解析（fail open），不得让提示词构建失败。
- **用户目录重定向（单点）**：`effective_project_dir(path)` 是唯一入口——解析后等于
  `Path.home()` 时改用**稳定、profile 作用域**的空目录
  `$HERMES_HOME/tmp/empty-workspace`（`mkdir_under_hermes_home()` + `apply_secure_dir_policy()`，
  禁止每次 `mkdtemp`/`tempfile.gettempdir()` 随机路径，否则缓存永远 miss 并留下垃圾目录）。
  context files 发现、`dir_context_files`、`workspace_tree`、`<workspace-roots>` 默认根，以及
  调用方填充 `SessionFacts.context_cwd` 时的仓库探测，全部经该函数；**不扫描**用户主目录，
  树为空。
- **边界**：解析函数在 `agent/prompts/project_context.py`，属 renderer 层取数；装配器仍只
  接收 `RenderContext`，不感知 `workspace_tree`。

---

## 实现落点

| 组件 | 文件 | 内容 |
|---|---|---|
| 装配器 | 新 `agent/prompts/assembler.py` | `AssemblyInputs`、`RenderContext`、`SubagentRole`、按代码顺序调块入口、一次 join；`assemble_subagent` = base + 角色块 |
| 分段渲染 | `agent/prompts/renderer.py` | 块入口（`render_role` / `render_runtime` / `render_tool_policy` / `render_memories` / `render_closing` / `render_project_context`）各自返回完整正文；段内取数留在本层 |
| 注册表 | `agent/prompts/prompt_sections.py` | 增加 `scope` 解析与校验；gate 白名单 |
| manifest | `agent/prompts/system/manifest.json` | 补 `scope`；`project_context` 的 `tier` 改 `volatile`（主代理尾块，保持最后一个 system 章节）；删除 `workspace_tree*` context_keys（D10）；只登记 `autoload-skills` 等已接线章节 |
| 新章节 | `system/autoload-skills.md`（接线，登记 manifest）；`system/{workspace,plugin-sections,system-message}.md`（只建文件，**不登记** manifest） | 占位文件不参与装配；不创建 `profile.md` / `runtime-state.md` / `environment.md` |
| 调用点 | `agent/system_prompt.py` | 删块列表/顺序，构造 `RenderContext` 并改调 `assemble_main()` |
| 子代理 | `agent/agent_run.py` | 改调 `assemble_subagent(ctx, role)`；删除"只有角色块"的分支与异常回退 |
| 退役 | `agent/prompt_builder.py` | 删除 D5-A 列出的常量与 helper，不迁移、不保留 shim |
| 测试 | 新 `tests/agent/test_prompt_assembler.py`、`tests/agent/test_prompt_assembler_boundary.py` | 块顺序、每块一次、空块丢弃、无直接 IO 边界、字节所有权 |
| 快照 | `tests/fixtures/prompt_surface/` | 切换 PR 重新生成 |

---

## 分阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 装配器 + `scope` + gate 求值 + 边界/分派测试；**不接线** | 旧快照不变；gate 矩阵（每个 gate 真/假）与"gate 假 = 零字节"通过；导入边界、无直接 IO、调用顺序与次数测试通过 |
| P1 | D5 新章节与旧块处置；`build_system_prompt_parts()` 改调装配器；删除退役常量 | 9 surface 快照重新生成并附 diff；D7.3 列出的每处持久化解析器各有定向测试；`scripts/check` 通过 |
| P2 | 子代理路径统一到 `assemble_subagent(ctx, role)`；请求级模板改用 `request_notice()` / `assemble_request()` | 子代理 prompt 以 `assemble_main()` 字节开头、角色块在末尾；同批子代理 base 前缀完全相同；`subagent` gate 矩阵；无 `system_prompt` 反向导入 |
| P3 | `prompts.budgets` 配置读取（ADR-18 D3 落地）；override / additionalCommunication 不在本阶段 | 预算截断与配置用例；文档与配置漂移测试通过 |

**暂缓**：override 与 additionalCommunication（ADR-18 D4）不进入 P0–P3 的任何一步。
renderer 的 override 分支与 `background_review` gate 只保留设计描述，本 ADR 不实现、不接线、
不加测试；实现排期另立。相应地，D8 表中"override 文件含注入"与本 ADR 的验收无关。

**manifest 处理**：暂缓章节**从 `sections[]` 移除**，不保留 `path: null` 的空壳条目（当前
`additional_communication` 就是这种条目：无模板、无块入口渲染）。理由：不允许死条目；装配器不引入 `scope: deferred` 分支，P0 的"章节集合与接线
清单一致"因此自然成立。未来排期时，模板、renderer 方法与 manifest 条目在同一个 PR 加回。

---

## 验收

1. **字节所有权**：已接线块在最终 prompt 中按代码顺序**连续出现且恰好一次**；测试逐块
   记录 `(key, offset, length)`。该集合之外的每个字节都必须能映射到 D5-B 声明的未接线载体
   （`system_message`、插件段、`project-prompt.md` 既有块等），映射不到即失败。
2. **块级开关**：开关为假的块零字节，不留空行或占位文本；已接线的块恒出现。
3. **边界**：`assembler.py` 的导入集合仅 `prompt_sections` / `renderer` / stdlib，不含
   `agent.system_prompt` / `agent.prompt_builder` / `run_agent` / `hermes_cli`；AST 扫描确认
   装配器自身没有 `open` / `read_text` / `Path` 调用（取数只在 renderer）。
4. **编排**：stub renderer 块入口时，调用序列 == 代码顺序，每块恰好一次，空块丢弃；
   装配器源码无读盘、无 `Path`，导入集合仅 `prompt_sections` / `renderer` / stdlib。
5. **身份无旧文本**：`SOUL.md` 与 `DEFAULT_AGENT_IDENTITY`（旧 Hermes 身份行）不再注入；
   `01-identity.md` 按模板原样渲染，`role` / `role_desc` 为可选覆盖。
6. **切换可审**：切换 PR 附 9 surface 的 prompt diff；旧常量在同一个 PR 删除，
   `grep` 不到被取代的常量名。
7. **持久化**：resume / 压缩 / surface 切换各有一条失败即红的测试；时间行、profile 行、
   工作区快照删除后，`_persisted_workspace_block`、`_timestamp_line` 的读取方与
   `runtime_host_value` 不再引用旧锚点。插件段的严格邻接缺陷单独修复，不在本 ADR 验收范围。
8. **子代理**：`assemble_subagent(ctx, role)` 的输出以 `assemble_main(ctx)` 的字节开头，角色块
   只在末尾出现一次；同一 `ctx` 的多个子代理共享相同 base 前缀；`role.body` 为空时报错；
   全 prompt 中 `§ Role` 恰好一次（来自 identity），角色块标题为 `§ Subagent Role`。

---

## 风险

| 风险 | 处置 |
|---|---|
| 语料替换导致行为回退（identity/safety/delivery 措辞变了） | 切换 PR 的 diff 即评审面；`test_prompt_surface` 9 个 surface 全量可见；不接受"部分 surface 先切"的灰色状态 |
| 旧能力被删除但没人发现（SOUL 注入、bot-mode protocol、时间行、profile 独立段） | D5-A 表逐条 `grep` 常量名归零；9 surface 的 prompt diff 逐段评审；接受"能力消失"作为显式变更，不写补偿文案 |
| 占位文件被误登记进 manifest | D5 的"只建文件 = 不登记"规则；P0 测试断言块顺序、每块一次、空块丢弃 |
| 预算截断把 XML 闭合标签截掉（05 memories） | 05 不设总预算（ADR-18 D5）；新增 `truncate: error` 只用于固定小段 |
| 插件段 / 工作区快照不接线导致能力消失 | 列为显式行为变更：`workspace` 内容仍在 `project-prompt.md`；插件段恢复与工作区冻结的现有逻辑同 PR 删除或保留并加测试，不写补偿文案 |
| 子代理仍走旧路径 | P2 的导入边界测试禁止 `system_prompt` 被反向依赖，子代理只能经装配器 |

---

## 不采纳

- **live / merged 章节来源**：装配器不再理解旧正文，旧栈一次删净。
- **按 surface 或配置项长期双栈**：只允许 revert 回滚，不允许运行时选后端。
- **调用方在装配结果后追加 runtime 块**：任何追加都会重新引入"两个拼接器"。
- **为保持快照不变而保留旧常量**：本 ADR 的目标是替换语料，不是搬家。
