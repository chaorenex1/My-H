# agent/prompts/ — 提示词组装层

适用根 `AGENTS.md`（提示词缓存不变量、facade + siblings）与 `agent/AGENTS.md`（消息流不变量）。

## 规则 1：本目录是提示词系统的组装位置，且一律条件装配

这里组装 Hermes 收到的全部提示词：系统提示词（`§ Role` + `§ Runtime`）、用户提示词、notice（`<system-notice>`）与 reminder（`<system-reminder>` / `<additional-system-reminder>`）注入。**没有任何一块是无条件塞进去的**：每个片段都必须带一个可判定的条件入口，条件不成立时整段不出现（不留空行、不降级成占位文本）。

条件入口只有四类，新增提示词必须挂在其中之一：

| 入口 | 位置 | 条件 |
|---|---|---|
| manifest 段 | `system/manifest.json` `sections[]` | `tier`（stable/context/volatile）、`gate`（`always`/`tools`/`skills`/`tool_search`/`coding_repo`/`subagent`/`kanban`/`memory`/`background_review`）、`budget` + `truncate`、`override` |
| 模板内门 | `system/*.md` 的 `{% if ... %}` | `tools` 集合、`subagent`、开关变量（`delegation_bias`/`eager_tasks`/…） |
| renderer 函数 | `renderer.py::render_*` | 调用方显式传参（`memory_enabled`、`first_input` 判定、`coding_project` 判定等） |
| 请求级注入 | `unexpected-stop-retry.md`、`date-cwd-reminder.md`、`eval-prelude-notice.md`、`tool-roster-notice.md` 等 | 运行时事件；这些走 user/tool 行，**不进系统提示词**（缓存不变量，见 `agent/AGENTS.md`） |

- `prompt_sections.py` 用 `StrictUndefined`：模板引用了 manifest 未声明或调用方未传入的变量会直接渲染失败。新增变量必须同时补 `manifest.json` 的 `context_keys`/`context_types`。
- 新增一段 = 模板 `.md` + manifest 条目；`tier` 在 manifest 定，模板不得自己决定 tier/顺序（ADR-18 D1/D2）。
- 预算超限按段声明的 `truncate` 处理（`tail`/`head_tail`/`drop_oldest_lines`/`error`）；`truncate: error` 的段落超预算就是 bug，不许提高预算掩盖。
- 组装入口在 `renderer.py`：`render_head()`（00–03）、`render_runtime()`（06–08）、`render_closing()`（10–12）、按配置的 `render_tool_policy_from_config()`。数据来源在兄弟模块：`delegation.py`、`toolsets.py`、`rules.py`、`skills.py`、`project_context.py`、`urls.py`、`kanban.py`、`hermes_help.py`、`platforms.py`、`judge/`。
- 长文设计与各段语义：`docs/prompt-system.md`、`website/docs/developer-guide/prompt-assembly.md`。
