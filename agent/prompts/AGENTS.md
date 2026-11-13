# agent/prompts/ — 提示词组装层

适用根 `AGENTS.md`（提示词缓存不变量、facade + siblings）与 `agent/AGENTS.md`（消息流不变量）。

## 规则 1：本目录是提示词系统的组装位置，且一律条件装配

这里组装 Hermes 收到的全部提示词：系统提示词（`§ Role` + `§ Runtime`）、用户提示词、notice（`<system-notice>`）与 reminder（`<system-reminder>` / `<additional-system-reminder>`）注入。**没有任何一块是无条件塞进去的**：每个片段都必须带一个可判定的条件入口，条件不成立时整段不出现（不留空行、不降级成占位文本）。

条件入口只有四类，新增提示词必须挂在其中之一：

| 入口 | 位置 | 条件 |
|---|---|---|
| manifest 段 | `system/manifest.json` `sections[]` | `budget` + `truncate`（`tier`/`gate` 仅登记，不驱动装配；顺序在 `assembler.assemble_main`） |
| 模板内门 | `system/*.md` 的 `{% if ... %}` | `tools` 集合、`subagent`、开关变量（`delegation_bias`/`eager_tasks`/…） |
| renderer 函数 | `renderer.py::render_*` | 调用方显式传参（`memory_enabled`、`first_input` 判定、`coding_project` 判定等） |
| 请求级注入 | `unexpected-stop-retry.md`、`date-cwd-reminder.md`、`eval-prelude-notice.md`、`tool-roster-notice.md` 等 | 运行时事件；这些走 user/tool 行，**不进系统提示词**（缓存不变量，见 `agent/AGENTS.md`） |

- `prompt_sections.py` 用 `StrictUndefined`：模板引用了 manifest 未声明或调用方未传入的变量会直接渲染失败。新增变量必须同时补 `manifest.json` 的 `context_keys`/`context_types`。
- 新增一段 = 模板 `.md` + manifest 条目（模板注册）；装配顺序由 `assembler.assemble_main` 的代码决定，模板不得自己决定出现位置。
- 预算超限按段声明的 `truncate` 处理（`tail`/`head_tail`/`drop_oldest_lines`/`error`）；`truncate: error` 的段落超预算就是 bug，不许提高预算掩盖。
- 组装入口在 `assembler.py`：`assemble_main(ctx)` 按代码顺序调用 `render_rfc2119()`、`render_role()`、`render_runtime()`、`render_autoload_skills()`、`render_tool_policy()`、`render_memories()`、`render_closing()`、`render_project_context()`，丢弃空串后 join 成一段正文。**没有** manifest 顺序/tier/gate 表；块方法自己决定内容并返回**完整正文**（`render_closing(project_dir=…)` 自行判定非仓库并省略 workflow、`render_tool_policy` 空工具面返回空串、`render_runtime` 自行判定 rules/help/kanban、`render_role` 自行解析平台提示、`render_autoload_skills` 空值零字节）。`manifest.json` 只做模板注册与 `scope`（`system`/`request`）标记；`tier`/`gate` 仅登记，不参与装配。数据来源在兄弟模块：`delegation.py`、`toolsets.py`、`rules.py`、`skills.py`、`project_context.py`、`urls.py`、`kanban.py`、`hermes_help.py`、`platforms.py`、`judge/`。
- 子代理入口是 `assemble_subagent(ctx, role)` = `assemble_main(ctx)`（`subagent=True`）+ `"\n\n"` + `render_subagent_prompt(role)`；角色块恒在末尾，`role.body` 为空即 `PromptSectionError`。`agent_run` 把角色块作为 `system_message` 并把 `role` 挂到 `_subagent_role`；`build_system_prompt_parts` 让 `stable` = base、`context` = 角色块，join 后与 `assemble_subagent` 同字节。无定义正文的匿名 run 不构造角色块，保持 bare-prompt 契约。
- `scope: request` 模板只由 `renderer.render_request(key, context)` 渲染；它拒绝 `system` 键，装配器的块序列不调用它。新增请求级模板必须登记 `scope: request` 并走这个入口。`subagent_system_prompt` 是例外：角色块经同一入口渲染，由 `assemble_subagent` 追加在 base 之后。
- 长文设计与各段语义：`docs/prompt-system.md`、`website/docs/developer-guide/prompt-assembly.md`。
