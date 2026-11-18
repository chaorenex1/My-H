## 统一lru缓存管理

1. 支持定义缓存的上限
2. 调用方传入purpose进行隔离
3. 支持多种缓存淘汰策略，支持调方选择缓存策略

## judgrg流程

```text
Query
  ↓
  LLM生产多个候选词，候选词上限为5个
    ↓
    Judgment对query意图识别,plan工具组合
    ↓
LLM生成的候选词+Judgment决策的工具组合，执行工具调用
 ↓
 验证，去重合并
```

### judgrg约束

1.lru缓存judgrg结果，缓存最近的10个结果，超过上限时淘汰最近搜索命中次数最少的3个结果

## 装配接线状态（ADR-19）

- 唯一装配入口：`agent/prompts/assembler.py::assemble_main()`。按代码顺序调用块入口，
  空块丢弃，`"\n\n"` join 成一段正文；`agent/system_prompt.py` 只求值 `SessionFacts`、
  调装配器、维护缓存/持久化。
- 块顺序：`rfc2119` → `role`（01–03）→ `runtime`（04/06–08）→ `autoload_skills` →
  `tool_policy`（09）→ `memories`（05）→ `closing`（10–12）→ `project_context`（尾块）。
- manifest 只做模板注册与 `scope`（`system` / `request`）；`tier` / `gate` 是元数据，不驱动装配。
- 已接线：00–12 全部章节、`autoload-skills.md`、`project-prompt.md`（含 `<workstation>` 的
  运行时/模型/provider/platform 行）。
- 未接线（只建文件、不登记 manifest）：`workspace.md`、`plugin-sections.md`、
  `system-message.md`、`title-marker-instruction.md`（ADR-21 D5 命名协议的存档提示词，
  已被 `title-system.md` + `<title>` 标记解析取代）。`profile` 名在 `01-identity.md` 顶部；
  时间/session 行已从系统提示删除。
- 请求级模板（`irc_incoming`、`eager_task`、`eager_todo`、`subagent_*` reminder、
  `session_tool_search_notice`、`session_skill_search_notice`、
  `background_review_skill_reminder`、`background_review_memory_reminder`、
  `title_system`（ADR-21 D5 命名提示词，`title_language` 变量）、`task_label`）保持
  `scope: request`，只由 `renderer.render_request(key, context)` 渲染（拒绝 `system` 键），
  不进入系统提示。`subagent_system_prompt` 是例外：它是子代理角色块，经同一入口渲染后由
  `assemble_subagent` 追加在共享 base 之后。新模板的注入时机与数据契约见
  `docs/adr/ADR-20-request-level-injection.md`。
- 子代理入口：`assemble_subagent(ctx, role)` = `assemble_main(ctx)`（`subagent=True`）+
  `"\n\n"` + `render_subagent_prompt(role)`；角色块恒在末尾，`role.body` 为空即构建失败。

## 默认加载内置工具集

1. browser: 浏览器自动化
2. clarify: 澄清用户问题
3. code_execution: 代码执行
4. cronjob: 定时任务
5. file: 文件操作
6. judgment: 判断决策
7. computer_use：计算机使用
8. session_search：会话搜索
9. skills ：技能
10. terminal：终端
11. todo：待办事项
12. vision：视觉识别
13. web：网络搜索和内容提取
14. agents: 多步任务执行的子代理
15. codegraph：代码图谱

## hermes内部工具搜索流程

```text
tool_search -> llm根据候选工具列表生成多条问题指令，上限3条-> judgment分批验证，purpose等于tool_choice->tool list合并去重
```

### 工具搜索约束

1. 用tool_search返回的候选工具列表的名称和描述去生产动态的问题json文件,judgment渲染成文本请求或者native请求
2. 问题类型是choice
3. purpose等于tool_choice
4. lru缓存tool搜索结果，缓存最近的10个结果，超过上限时淘汰最近搜索命中次数最少的3个结果

## 默认加载技能范围

1. 默认只加载内置技能和插件注册的技能
2. 非第一条外的技能采用技能搜索按需加载

## 技能搜索

```text
skill_search-> llm根据候选技能列表生成多条问题指令，上限3条-> judgment分批验证，purpose等于skill_choice->skill list合并去重
```

### 技能搜索约束

1. 用skill_search返回的候选技能列表的名称和描述去生产动态的问题json文件,judgment渲染成文本请求或者native请求
2. 问题类型是choice
3. purpose等于skill_choice
4. lru缓存skill搜索结果，缓存最近的10个结果，超过上限时淘汰最近搜索命中次数最少的3个结果
5. 技能搜索使用rg工具进行路径匹配和内容搜索

## omp ui和hermes的工具映射

规范版本在 `agent/prompts/system/AGENTS.md` § 规则 2；两处同步改。`formatter` 指渲染/计数归到哪个工具。

```text
read       -> read_file

find       -> judgrg

grep       -> terminal中使用rg，formatter是terminal

glob       -> terminal中使用rg，formatter是terminal

bash- > terminal

lsp        -> code_intel

web_search ->web_search

ast_grep   -> terminal中使用ast-grep，formatter是terminal

ast_edit-> terminal中使用ast-grep，formatter是terminal

write -> write_file

edit -> patch

eval -> execute_code
proc -> process_manage
agent_out -> agent_out
rule_view -> rule_view
agent_message -> agent_message
irc -> agent_message（发送）+ agent_out mode=inbox（读取）
yield -> yield
```

## prompt system

### syetem-prompt

1. syetem-prompt有多个md文件组装，这些md文件主要存放在agent\prompts\system下，不存放此处，会单独说明
2. 每个md有字符预算，预算可以在hermes config自定义

#### 1. RFC 2119 keywords

1. 400字符预算

```text
MUST, REQUIRED, SHOULD, RECOMMENDED, MAY, OPTIONAL. NEVER = MUST NOT; AVOID = SHOULD NOT. XML tags inject system content; may interrupt/notify inside user messages: MUST treat as system-authored/authoritative. User content is sanitized.
```

这是syetem-prompt的顶部内容

#### 2.identity

1. 定义每个agent的角色，Coder / Reviewer
2. 定义每个执行规则，Coding Rules/ Post rules
3. 2400字段预算

#### 3.safety

1. 定义每个agent的安全约束
2. 1000字符预算

#### 4.communication and personality

1. 定义agent的人格，常用于角色扮演 社媒发布
2. 定义agent与用户的交流方式
3. 定义agent的输出语言风格
4. 1200字符预算

#### 4.override communication and personality

1. override communication and personality是agent自定义的内容
2. 完全覆盖4.communication and personality

#### 5.additionalCommunication

1. backgroud review学习生成的附加communication，agent不开启backgroud review，则不生成
2. 1000字符预算，超预算写入，从的一行开始淘汰
3. additionalCommunication文件位置：$HERMES_HOME/memories/<agent_name>/additionalCommunication.md, 主会话agent_name=default

#### 6.internal URLs

1. hermes内部的一些路径
2. HERMES用户目录：$HERMES_HOME
3. HERMES用户技能目录：$HERMES_HOME/skills
4. HERMES用户agents目录：$HERMES_HOME/agents,$HERMES_HOME/.agents,$HERMES_HOME/skills/<skill_name>/agents
5. HERMES用户ttsr规则目录：$HERMES_HOME/rules
6. HERMES用户memory目录：$HERMES_HOME/memories
7. HERMES用户logs目录：$HERMES_HOME/logs
8. HERMES会话目录：$HERMES_HOME/sessions
9. HERMES缓存目录：$HERMES_HOME/cache,存放临时文件，如测试脚本，探针等
9. 这个章节没有字符预算

#### 7. memories context

1. 这个章节不设总字符预算：AI_SELF 没有长度上限，且 XML 结构不能按 `tail` 截断（会丢闭合标签）
2. 定义memories的作用，引导使用记忆
3. 使用xml标签包裹具体内容

##### memories context
```xml 
<memories>
<memories-Guidance>
md文件 记忆的使用和反馈向导
</memories-Guidance>
<memories-global>
<memories-user>用户跨会话稳定的事实、偏好和交流风格，来源 $HERMES_HOME/memories/USER.md</memories-user>
<memories-self>ai跨会话的稳定的自我表示，包含如何理解用户真正的意图，工作达到什么程度才能让用户满意，需求/计划/执行步骤的思考深度等，来源 $HERMES_HOME/memories/agents/&lt;agent_name&gt;/AI_SELF.md</memories-self>
<memories-work>最近会话标签（MEMORY），来源 $HERMES_HOME/memories/agents/&lt;agent_name&gt;/MEMORY.md</memories-work>
</memories-global>
</memories>
```

- 主会话的 `agent_name` 是 `default`；子代理传入自己的名称。
- `memory_enabled=false` 时整个 05 章节（含 provider 块）不渲染；`user_profile_enabled=false` 只隐藏 `<memories-user>`。
- 三个文件都直接从磁盘读取，不查询会话数据库；文件缺失时渲染空标签。
- 记忆文件进入系统提示前按 strict 作用域做注入扫描，命中时用 `[BLOCKED: ...]` 占位。

##### memories provider
1. 记忆插件注入的系统信息块，渲染在 `<memories>` 内部、`<memories-global>` 之后
2. 信息块
```xml 
<memories_plugin_provider>记忆插件的信息</memories_plugin_provider>
```

#### 8.skills/rules

##### skills

1. 拼接默认加载技能范围的技能名称和描述
2. 定义非默认加载技能范围内的技能搜索规则
3. 根据每个agent允许使用的技能进行拼接，默认允许全部

##### rules

1. 按仓库的语言构成拼接$HERMES_HOME/rules的ttsr规则描述和路径
2. 按仓库的语言构成拼接内置rules的ttsr规则描述和路径
3. 当前目录不是编码仓库，不拼接ttsr规则信息

#### 9.tool inventory

1. 拼接默认加载工具集每个工具的名称和描述。两层结构，第一层工具集的名称和描述，第二层每个工具集下具体工具的名称和描述
2. 当前目录不是编码仓库，不拼接codegraph工具集信息
3. 定义非默认加载工具集的工具搜索规则
4. 根据每个agent允许使用的工具集进行拼接，默认允许全部

#### 10.tool policy

1. 只负责工具选择与调用方式：General / Tool I/O / Specialized Tools / Search and File Discovery /
   Read / Edit / AST / Symbols and Graph / Delegation / Parallel and Retry
2. 不渲染工具清单；专用工具优先于 shell 等价物
3. `rg` 允许通过 terminal 运行，是文件发现与文本搜索的首选，`rg --files` 等同于 glob；
   `grep`/`find`/`ls`/`fd`/`awk` 禁止。`search_files` 仅作为结构化输出（files_only/count/
   分页）或无 shell 时的回退；未知行为或位置先用 `judgrg`，其 `ranges` 可作为
   `read_file` 的可选 `offset`/`limit`
4. `patch` 做局部手术式修改，`write_file` 只在新建或整体覆盖时使用；禁止用 shell 的
   `sed`/`perl`/`python` 做单点编辑；`ast-grep` 的选型、用法与安全规则集中在 AST 一节
5. `code_intel` 是符号、引用、调用链、类型、依赖与影响的权威；`rg`/`ast-grep`/`read_file`
   只消费它的输出作为下一步输入，不作为图谱不完整时的回退
6. 委派使用 `agent`/`agent_out`；`agent` 已全面取代 `delegate_task`，功能更完整：命名 agent、
   独立 prompt/tools/model、JSONL 历史、后台常驻、输出契约与 artifact，并支持 `continue_from`
7. 委派判定树复用 omp 的 switches：`delegation_bias`（gated/eager）、`eager_tasks`、
   `eager_tasks_always`、`inline_first`、`max_concurrency`
8. 根据实际工具面裁剪：没有 `agent` 就没有 Delegation，没有 `ast-grep` 就没有 AST，
   没有 `code_intel` 就没有 Symbols and Graph；`rg`/`ast-grep` 来自
   `agent/prompts/toolsets.py::PM_DEFAULT_TOOLSETS`，仅在 terminal 可用时注入
9. 不写 Completion/验证小节，验证由 Workflow/Delivery 负责
10. 章节不设总预算，避免截断末尾的 Parallel and Retry

#### 10.override tool policy

1. 部分agent自定义的tool policy，通常由多个文件组成
2. 完全覆盖9.tool policy
3. 这个章节2000字符预算

#### 11.workflow

1. 这个章节3000字符预算
2. 按工具面裁剪：`code_intel`/`todo_list`/`clarify`/`browser_exec`/`computer_use`
   缺失时对应小节或工具行不渲染
3. `subagent=true` 时第 5 步走 Hand-off，验证交给主 agent；否则走 Verify

##### global scope

1. 全局范围加载执行aislop和过度工程技能约束
2. 非代码仓库不加载执行aislop代码部分和过度工程技能约束

#### scope

1. 第一步阅读相关技能和规则
2. 计划多步工作前，阅读相关文件

##### flow

```text
global scope->scope->Research Before Editing ->Decompose->Implement->Hand-off(subgent step)/Verify->Cleanup
```

#### 11.override workflow

1. 部分agent自定义的workflow，通常由多个文件组成
2. 完全覆盖11.workflow
3. 这个章节3000字符预算

#### 12.Delivery

1. contract / completeness / evidence-and-output / yielding
2. 这个章节2400字符预算
3. `subagent=true` 时 yielding 只保留本切片一行，验证由主 agent 负责

#### 13.Critical

1. 本agent所有Critical信息条目
2. 这个章节1000字符预算


#### 14.eval-prelude-notice

```xml 
<system-notice id="prelude-extension">
所有eval prelude notice条目
</system-notice>
```

#### 15.`<project-context>` 缓存断点

```handlebars
<project-context>
<workstation>
- OS: …  - Arch: …  - Model: …
</workstation>

{{#if contextFiles.length}}
<repo-rules>
MUST follow these context files for all tasks:
{{#each contextFiles}}<file path="{{path}}">{{content}}</file>{{/each}}
</repo-rules>
{{/if}}

{{#if agentsMdSearch.files.length}}
<dir-context>
Some directories may have rules; deeper rules override higher ones.
Before changes in these directories, MUST read: …
</dir-context>
{{/if}}

{{#if includeWorkspaceTree}}<workspace-tree>…</workspace-tree>{{/if}}
{{#if additionalWorkspaceRoots.length}}<workspace-roots>…</workspace-roots>{{/if}}
{{#if activeRepoContext}}{{activeRepoContext}}{{/if}}
</project-context>

<critical>
- Each response MUST advance the task; completion only stopping condition.
- MUST default to informed action; do not ask for confirmation when tools or repo context can answer.
{{#if subagent}}- Changes complete → yield; verification is main agent's job. …{{else}}- Before yielding, MUST verify significant behavioral changes: …{{/if}}
</critical>

{{#if appendPrompt}}{{appendPrompt}}{{/if}}
```

真实子会话的尾块（469 字符）证明了两个机制同时生效：

```
<project-context>
<workstation>
- OS: win32 10.0.26300
- Arch: x64
- Model: deepseek/deepseek-v4-flash
</workstation>
</project-context>

<critical>
- Each response MUST advance the task; completion only stopping condition.
- MUST default to informed action; do not ask for confirmation when tools or repo context can answer.
- Changes complete → yield; verification is main agent's job. NEVER run it yourself unless your assignment explicitly instructs it.
</critical>
```

没有 `<repo-rules>`，因为**任务子代理丢弃所有 `AGENTS.md`**（文件名过滤，含用户级）；`RULES.md` 与 `@` 导入进来的内容不受影响。`subagent` 分支同时把验证职责换成了移交。


#### 16. 子代理：`§ Subagent Role` 块与角色文件

主会话的 block 0 与尾块之后，子代理再多一块（`assemble_subagent` = `assemble_main` + 角色块）。模板是 `src/prompts/system/subagent-system-prompt.md`，开头就是我们在转录里看到的形状：

```handlebars
§ Subagent Role
{{agent}}

{{#if context}}
§ Context
{{context}}
{{/if}}

{{#if planReference}}
§ Plan
This session is executing an approved plan. …
{{/if}}

§ Coop
You are operating on a piece of work assigned to you by the main agent.

{{#if ircSelfId}}
# Peers
Message peers via `write` with `path: "agent_out({{ircSelfId}})"` … Your id is `{{ircSelfId}}`. …
{{/if}}

§ Completion
{{#if structured}}
… report results with `yield`. …
{{else}}
No TODO tracking, no progress updates. Execute; your final message is your result.
{{/if}}
```

`{{agent}}` 就是角色文件去掉 frontmatter 后的正文。所以一块 `§ Subagent Role` 的构成是：

```
"§ Subagent Role\n" + agent.systemPrompt + "\n§ Context\n" + task.context + … + "§ Coop\n…" + "§ Completion\n…"
```

---

### 请求级注入

系统块之外，模型还会收到这些**看起来像系统提示、实际是消息**的内容：

**记忆召唤**：

```xml
<memories-context>
<memories-recall-context>
<memories id="插件的记忆内容id">
由记忆插件的记忆产生记忆条目
</memories>
</memories-recall-context>
<memories-feedback>
<memories id="插件的记忆内容id">
反馈使用情况
</memories>
</memories-feedback>
</memories-context>
```

**附加提醒**：

```xml
<additional-system-reminder id="window-summary">
每一轮会话的窗口总结
</additional-system-reminder>
```

**增强用户输入**:

```xml
<enhanced-user-input>
对用户输入的改写
</enhanced-user-input>
```

**首轮日期与 cwd**（`date-cwd-reminder.md`）——所以日期不是模板变量：

```xml
<system-reminder>
Today: {{date}}; current working directory: '{{cwd}}'. Do not repeat this information in your reply.
</system-reminder>
```

**eval 前导变更通知**（`eval-prelude-notice.md`）——只列变化，避免每轮重述：

```xml
<system-notice id="prelude-extension">
`eval` prelude globals changed. This lists only what changed; any prelude not named here is unaffected.
… Now available in JS/Python `eval`: - `browser` → `xd://eval/browser` …
</system-notice>
```

**工具选择软约束**：强迫 `tool_choice` 每轮会碎掉缓存，所以先注入提醒文本，模型不遵从才升级为单轮硬约束（`SoftToolRequirement`）。

### 会话级重写

**压缩**：不是普通消息，而是带类型的会话条目；重建上下文时被转成 user 消息并用静态模板包裹（`compaction-summary-context.md` 等）。

**记忆**：本地后端在会话启动注入 "Memory Guidance" 块；Mnemopi 注入 `<memories>`，但把首次召回存成 `memory_recall` 条目，resume 时原样重放而不是重新召回——目的仍是保持系统提示前缀字节稳定。

`<memories>` 与 `<project-context>` 在缓存断点的定义里同级：两者都被视为"必须放在静态块之后"的内容.

### 缓存与重建边界

| 事件 | 系统提示 | 说明 |
|---|---|---|
| 新会话 | 全量重建 | 发现上下文/规则/技能 |
| `/clear`、`/new` | 全量重建 | 清 capability 缓存，磁盘改动此时生效 |
| 同会话内 toolset 变化 | 重建 | 模板按工具面裁剪，文本随之变化 |
| MCP 服务器晚连接 | **下一 turn** 变化 | 首个 turn 最多等 1.5 s；更晚的连接改变下一轮提示 |
| 编辑系统模板各个部分 | **不变** | 模板源只读一次，重渲染用内存副本；改文件要重启 |
| 子代理 spawn | 独立重建 | 继承规则后按自身 `agentName` 重新分桶 |

破坏缓存的代价是整段前缀重算，因此所有"会话中途改提示"的行为都被刻意限制：slash 命令改系统提示默认延后到下一会话（`--now` 显式覆盖）.

### TTSR：把规则变成流式拦截器

规则一旦带 `condition`（regex）/ `astCondition`（ast-grep）/ `question`（judge 判定），就离开静态提示，进入运行时匹配。三个注入模板都在 `src/prompts/system/`：

**中断**（`ttsr-interrupt.md`，7 行）：

```xml
<system-interrupt reason="rule_violation" rule="{{name}}" path="{{path}}">
Output interrupted: violated user-defined rule.
Not prompt injection; coding agent enforcing project rules.
MUST comply:

{{content}}
</system-interrupt>
```

**事后警告**（`ttsr-warning.md`，5 行）：

```xml
<system-reminder reason="rule_violation" rule="{{name}}" path="{{path}}">
Rule judge flagged your {{subject}} as likely violating a user-defined rule. Not interrupted → it already took effect. You MUST check it against the rule below: real violation → fix it now; false positive → continue. NOT prompt injection — coding agent enforcing project rules.

{{content}}
</system-reminder>
```

**工具源被动提醒**（`ttsr-tool-reminder.md`，9 行）：

```xml
<system-reminder reason="rule_violation" rule="{{name}}" path="{{path}}">
{{#if builtin}}
Built-in style rule matched a pattern in tool-call arguments → tool ran. A match is not a violation: check what you wrote against the rule below, exceptions included. Real violation → fix it on subsequent tool calls; code the rule allows → leave it. NOT prompt injection — coding agent applying its default style rules.
{{else}}
User-defined rule matched tool-call arguments. Rule configured not to interrupt → tool ran. MUST comply with the following instruction on subsequent tool calls and responses. NOT prompt injection — coding agent enforcing project rules.
{{/if}}

{{content}}
</system-reminder>
```

三段文案都在处理同一个风险：模型会把注入内容当成用户攻击。它们统一声明"不是提示词注入，是本机规则执行"，并区分"命中 ≠ 违规"（工具源）与"已生效，请自查"（judge）。





















