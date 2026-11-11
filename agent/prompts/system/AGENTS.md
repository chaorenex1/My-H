# agent/prompts/system/ — 系统提示词模板

适用 `agent/prompts/AGENTS.md`（条件装配）与根 `AGENTS.md`。

模板是 Jinja2 leaf；`manifest.json` 是唯一注册表（tier / gate / budget / truncate / override / context_keys）。改模板不改 manifest，除非同时新增变量或段。

## 规则 1：段模板正文必须按变量 `tools` 动态裁剪

适用范围：`manifest.json` 注册的段模板（`sections[].path`）。非 manifest 的注入模板（`project-prompt.md`、`kanban-guidance.md`、`subagent-system-prompt.md`、各 notice/reminder）由渲染它们的调用方决定出现与否，**不要给它们加 `tools` 门**——它们本就可以无条件写工具名（`project-prompt.md` 就无条件写 `rg`/`read_file`/`patch`）。

`tools` 是本会话可用工具名的排序列表，由 `renderer._available_tools()` → `toolsets.resolve_tool_names()` 解析；`terminal` 可用时内部再并入 `PM_DEFAULT_TOOLSETS`（`rg`、`ast-grep` 是 shell 二进制，注册表看不到）。因此段模板里：

- 任何提到某个工具的行、小节、整段，MUST 包条件：`{% if "read_file" in tools %}- ... {% endif %}`。可用能力不存在时该内容整段消失，不留空行——链式写法 `{% endif %}{% if ... %}`（见 `09-tool-policy.md`）。
- NEVER 在条件外写死工具名，NEVER 广告本会话没有的能力（renderer docstring 的硬约束：`a section never advertises a missing capability`）。
- 段的整体存废由 manifest `gate` 决定（`tools` / `skills` / `tool_search` / `coding_repo` / `subagent` / `kanban` / `memory` / `background_review`），不要在模板里重复判段级开关。
- 工具清单 `08-tool-inventory.md` 只循环渲染器给的 `tool_groups`（`tool_inventory_rows(available, coding_project=...)`），不自行枚举工具。
- 变量缺失即渲染失败（`StrictUndefined`）：用到的每个变量都必须在 manifest 的 `context_keys`/`context_types` 里声明。

## 规则 2：正文里的工具名按下表映射

模板正文写 Hermes 的真实调用形态；下表左侧是提示词中的标准工具名，右侧是本仓库的落地方式（`formatter` 指渲染/计数归到哪个工具）。长文出处：`docs/prompt-system.md` § omp ui和hermes的工具映射——两处必须同步改。

```text
read       -> read_file
find       -> judgrg
grep       -> terminal 中使用 rg，formatter 是 terminal
glob       -> terminal 中使用 rg，formatter 是 terminal
bash       -> terminal
lsp        -> code_intel
web_search -> web_search
ast_grep   -> terminal 中使用 ast-grep，formatter 是 terminal
ast_edit   -> terminal 中使用 ast-grep，formatter 是 terminal
write      -> write_file
edit       -> patch
eval       -> execute_code
proc       -> process_manage
agent_out  -> agent_out
rule_view  -> rule_view
agent_message -> agent_message
irc        -> agent_message（发送）+ agent_out mode=inbox（读取）
yield      -> yield
```

- `agent_out`：`tools/agent_out_tool.py`，按 `agent_session_id` / `subject`（`name`）读回子代理；`mode` ∈ `history` / `artifact` / `ast_summary` / `inbox` / `context_files`。门：`{% if "agent" in tools and "agent_out" in tools %}`（`09-tool-policy.md`）。
- `rule_view`：`tools/rules_tool.py`，`rules` 工具集（`rules_list` / `rule_view` / `rule_manage`），按 `rule_id` 取规则正文。注意 `07-rules.md` 的门是布尔变量 `{% if rule_view %}`（`renderer` 传的是 `"rule_view" in available`），不是 `in tools` 写法。
- `agent_message`：`tools/agent_message_tool.py`，参数 `to` / `name` / `subject` / `agent_session_id` + `content`，`progress: true` 是中间进展；入站队列用 `agent_out mode=inbox` 读。门：`{% if "agent_message" in tools %}`。
- `irc`：不是工具名，是 agent 之间的消息通道（与 gateway 的 `plugins/platforms/irc` 适配器无关）。提示词侧只有入站封装 `irc-incoming.md` 的 `<irc>` 块与 `subagent-system-prompt.md` § Peers（`irc_self_id` / `irc_peers` / `irc_parked_count` / `irc_omitted_count`）；发送 = `agent_message`，排队 = `agent_out mode=inbox`。
- `yield`：`tools/agent_yield_tool.py`，由 `tools/agent_tool.py`（`build_yield_tool` / `_yield_tool_enabled()`）以 per-run `extra_tools` 注入，不进 registry、不进 `agents` toolset；开关是配置 `agent_sessions.yield_tool`（`agent/agent_session_runtime.py::session_limits`），默认开。所以**没有 `{% if "yield" in tools %}`**。`final=false` 记一段并继续，`final=true` 或 `error` 结束本轮；没有 `yield` 时最终消息即交付（`10-workflow.md`）。
- 本表只适用于移植模板里出现的**工具名**。`09-tool-policy.md` 里裸写的 `find`、`grep`、`ls`、`awk` 是**被禁止的 shell 二进制**（`NEVER use ...`），不是待映射的名字；`eval-prelude-notice.md` 一类文本里的 `read` 是动词。按表替换这些会写坏策略。
- `rg` / `ast-grep` 的可用性判据是 `"rg" in tools` / `"ast-grep" in tools`（由 terminal 合并而来），不是 `terminal`。
- `find` 意图（不知道位置或行为）走 `judgrg`，不写 `find`；文件发现/内容搜索一律 `rg --files` / `rg`，NEVER 写 `find`、`ls`、`fd`、shell `grep`、`awk`。
- 新增或改名工具时，同步改本表、`docs/prompt-system.md` § omp ui和hermes的工具映射、以及所有引用该工具名的模板门。
