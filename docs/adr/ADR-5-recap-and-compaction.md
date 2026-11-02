# ADR-5：Recap 与压缩摘要的忠实移植（omp-ui ↔ Hermes）

**状态**：已决定（2026-11-01）。实现未开始。分阶段：**P1** 压缩摘要的持久化盖章 + `include_compaction`
投影 + 客户端分隔条；**P2** `display.collapseCompacted` 的渲染期折叠；**P3** `recap.generate` + 共用
ephemeral side turn + prompt 移植；**P4** 客户端 idle 定时器 + `RecapNotice`。落地前
`docs/omp-ui-panels-gateway-plan.md` §2.3.2.1 的「压缩摘要」「Recap」两行保持 `无 producer`（§0.1）：
客户端不画假块、不用 `status.update`/`agent.print`/`review.summary` 冒充。

**关联**：`docs/omp-ui-panels-gateway-plan.md` §0.1、§0.3、§2.3.2、§2.3.2.1、§4.7、§9；
ADR-2（`display_kind`/`display_metadata` 投影惯例、D4、D9、D11 的 stop-gate 与事件先例）；
ADR-3（`append_display_row`、行/事件边界）；ADR-4（`canonical_method`、分阶段与客户端接线方式）。

**范围**：只讨论 omp-ui ↔ Hermes canonical local gateway。消息平台、Desktop、Ink sidecar、ACP、
compute-host 的 worker 帧都不设计；但**默认 payload 逐字不变**（D2/D9），所以那些客户端不受影响。
两个功能都只走 display/事件面：不改模型上下文、不改 system prompt、不改 prompt cache 前缀。

**取证口径**：Hermes 侧全部引用本仓库的当前工作树。omp 侧源码来自 plan §0 指定的本地 checkout
`C:\Users\zarag\projects\oh-my-pi`（HEAD `6a86df8539fccf3ad7818081266e554022fc21bc`，
`@oh-my-pi/pi-coding-agent` 18.14.0），`omp-ui/src/vendor/` 是它的裁剪副本；渲染侧引用壳实际编译的
`omp-ui/node_modules/@oh-my-pi/pi-tui`（18.14.1）。`recap-notice.ts` 两侧逐字一致，可作同源性抽查。
本环境没有 `git show <rev>:<path>`（对象库未解包），所以“已删业务代码”的结论只依据 vendor 残留符号、
类型与注释，并在正文里标为推断。

**取代关系（逐条，避免文档互斥）**：

- ADR-2 **D4**（“压缩摘要不做 transcript 块”，`ADR-2-transcript-blocks.md:89-102`）：**由 D1–D5 取代**。
  D4 的两条理由分别被处理：(a) “模型侧 handoff 文本不应进用户 transcript”——块里显示的是
  `_strip_summary_prefix` 之后的**摘要正文**，不是模型侧 `SUMMARY_PREFIX` 包装；(b) “`display_messages` 同时
  服务 Desktop”——新投影是**显式参数**（`include_compaction`，默认 false），不传参的客户端拿到与今天逐字
  相同的 payload。
- ADR-2 **D9** 的 `Recap` 一项（`ADR-2-transcript-blocks.md:185-187`）：**由 D6–D9 取代**（新增 producer）。
  D9 的其它项（TTSR、独立 late-diagnostics、被剥离工具调用、collab prompt、reply write、自定义/hook
  消息、Background TAN）不变。
- ADR-2 **D9** 的“压缩摘要块”一项：**由 D1–D5 取代**；ADR-3 第 13 行“compressed summary 保持原判断”
  随之失效。
- plan §2.3.2 / §2.3.2.1 的**渲染器描述**（`compaction-summary-message.ts`、`recap-notice.ts`）不变：本 ADR
  只加数据载体、投影点与客户端映射，不新增渲染分支的布局。
- ADR-2 D11 的**事件口径**继续适用：行 + 事件各一份、payload 构造复用一个 helper、不新增回调。
- “不采纳”表里“用 `review.summary` 冒充 recap”的判断**保持**（plan §2.3.2.1:442）。

## 问题

omp 的这两块都是“会话自身状态”的显示：Recap 是**离开期间**的一句话进展，压缩摘要是**上下文被重置**的
边界。Hermes 各有原料，但没有一条把它们送到 UI 的通路；同时两者都容易用错数据冒充，所以先逐项列事实。

### 1. omp 的 Recap

**语义**：回合结束后空闲 N 秒，用**会话当前模型**跑一次 ephemeral side turn，产出 ≤40 词、1–2 句、
无 markdown 的“目标 + 当前任务 + 一个下一步”，在 transcript 末尾临时画一行 `※ recap: …`。

- 触发：`agent_end` 时 `pending.recap = priorMessageWasGenerated(...)`（最后一条必须是 assistant，且有非空
  text/thinking/toolCall；`idle-maintenance.ts:127-148, 388-397`）；`agent_start` /
  `auto_compaction_start|end` → `invalidate()`（`:128-141, 156-160`）；arm 前 `waitForIdle()` + 无 admitted
  submission，再判 `isBlocked() || !isQuiet()`（`:209-219`）；`idleSeconds` 钳到 `[1,3600]`、`unref()` 定时器
  （`:60-61, 230-237`）；到点再复查一次（`:313-322`）；运行前第三次复查（model 存在、仍有生成尾巴、
  blocked/quiet，`:330-356`）。
- 阻塞条件是**客户端状态**：`!ready || activity.target !== target || activity.isComposing`
  （`interactive-mode.ts:1836-1859`，`:1852`）。即：未就绪、会话不在屏幕上、composer 有草稿 → 不产出/不交付。
- 生成：`runEphemeralTurn` 用会话当前模型（`idle-maintenance.ts:345`），prompt 模板
  `prompts/system/recap-user.md:1-9`（原句：`<recap>User stepped away; returning. Recap: <40 words, 1–2 plain
  sentences, no markdown. Lead: overall goal, current task; then one next action. …`），并附 `Overall goal:`
  （goal-mode objective，否则会话名）与 `Active task:`（当前活跃 todo，`idle-maintenance.ts:341-344,
  374-378`）。请求体前置一条 developer 提醒“本轮没有工具、只回文本”（`agent-session.ts:12967-12977`）。
- 输入/隔离：snapshot = 完整历史（含在飞 streaming 文本，`agent-session.ts:12936-12966`）；工具目录照常附带
  **只为命中 prompt cache**（`agent-session.ts:12706-12709, 12774-12790`）；`runEphemeralTurn` 明确
  “The session's history is NOT modified by this call”（`:12711-12713`），provider 侧用独立 side lineage
  （`:12803-12820`）。
- 输出：不流式（`idle-maintenance.ts:345` 未传 `onTextDelta`），`dedupeEphemeralReply` 压重复行、4 KiB 截断
  （`agent-session.ts:12902-12905`）；空回复不落库不显示（`idle-maintenance.ts:354`）；失败只
  `logger.debug`（`:357-362`）。
- 渲染：`RecapNotice` 只接一个字符串（`pi-tui/src/chat/recap-notice.ts:14-22`），格式是 `Spacer(1)` +
  `※ recap: ${recap}` 的 dim+italic 整行；挂载前压成单行并截到 **280 列**
  （`TRUNCATE_LENGTHS.RECAP`，`render-utils.ts:185-195, 248-250`；上游检出
  `modes/controllers/event-controller.ts:480-484`）。
- 不是 entry：没有 role/customType，不进 session JSONL（`session-index.ts:11-14`）；只写 history.db 的
  `session_recaps` journal（`session-index.ts:41-48, 154-169`），消费者是 omp 的 archive/eval 视图；
  resume/replay 无分支（`chat-transcript-builder.ts:299-398`），`resetTranscript` 即消失
  （`interactive-mode.ts:8402-8407`）；不进 `/copy`、rewind、outline。
- 配置：`recap.enabled`（默认 true）、`recap.idleSeconds`（默认 240）；壳已保留这两个注册项但**无消费者**
  （`omp-ui/src/modes/settings.ts:1251-1283`），`interactive-mode.ts:97` 是一个死 import。

**Hermes 现状**：没有任何 idle recap 生产者。

- `hermes_cli/session_recap.py:1-2` 是“纯本地计算：no LLM call, no auxiliary model, no prompt-cache
  invalidation”；`build_recap`（`:141-177`）输出多行面板（Recent 轮次、Tools used、Files touched、
  Last ask/reply），与 omp 的单行 LLM 摘要是**不同功能**。
- 它**没有生产调用方**（只有 `tests/hermes_cli/test_session_recap.py`），`hermes_cli/commands.py` 里也没有
  `recap` 命令；`/recap` 只出现在 loop 的用法示例（`hermes_cli/loops.py:631`、`cli_loops_mixin.py:763`）。
- 网关无 recap 事件；`review.summary`（`gateway/session_refine.py:56-59`）是后台 review 结果；
  `compression.idle_compact_after_seconds`（默认 0，`config_defaults.py:684-687`）是**压缩**不是 recap。

### 2. omp 的压缩摘要

**语义**：压缩后进上下文的第一条是 `role:"compactionSummary"` 的消息（`messages.ts:50-76`：`summary`、
`shortSummary?`、`tokensBefore`、`tokensAfter?`、`method?`、`warning?`、`images?`、`timestamp`），序列是
`[摘要] + [firstKeptEntryId 起的原消息]`（`entries.ts:34-54`）；旧 entry 不删、只是“边界之前、永不再发送”
（`pruning.ts:56-62`）；会话日志只追加一条 `type:"compaction"` 边界记录。

- 渲染：`CompactionSummaryMessageComponent` 画分隔条 `──── 📷 <label> · 256K→20K · ctrl+o ────`，label 按
  method 映射（remote→remote-compacted、soft→soft-compacted、handoff→handed-off、snapcompact→
  snap-compacted、shake→shaken，未知→`compacted`）；正文 `Disclosure` **默认 collapsed**，`ctrl+o` 展开
  （`compaction-summary-message.ts:18-28, 107-118, 204-216`）。`tokensBefore=0` 时 badge 省略、详情退化为
  “Compacted context”（`:25-28, 255-269`）。
- `display.collapseCompacted`（默认 true）**不是**这个 disclosure：它把压缩点之前的 live 历史收进分隔条后面；
  关掉则全量 inline、每个压缩点都留一条分隔条（`omp-ui/src/modes/settings.ts:744-755`）。上游组件注释强调
  “display transcript keeps full history; only the LLM context was reset”（`compaction-summary-message.ts:
  188-203`）。该折叠的实现在上游已随业务代码删除，壳里只剩“设置变更触发重建”
  （`interactive-mode.ts:1228-1240`）。
- 选择器/计数：tree/copy 都把摘要当一条 entry（`tree-selector.ts:31, 1126-1129`；`copy-selector.ts:89-92,
  145-148`）；replay 由 `chat-transcript-builder.ts:386-390` 还原同一个组件。
- 唯一允许 mutation 的例外：`append-only-context.ts:228-231`（“The only mutation path is replaceTail(),
  reserved for compaction.”）；摘要请求本身复用 live system prompt/tools 以命中缓存（`compaction.ts:1090`）。

**Hermes 现状**：行都在，但被两处过滤；实时只有文本状态。

- 形状：`[head: protect_first_n] + [summary 行 或 merge 进 tail 行] + [tail]`
  （`agent/context_compressor.py:5551-5582`）。独立行 `content = SUMMARY_PREFIX + 正文 + "\n\n" +
  _SUMMARY_END_MARKER`（`SUMMARY_PREFIX` `:258-297`，end marker `:508`），带内存键
  `_compressed_summary`；占位冲突时 `_merge_summary_into_tail_row` 把摘要折进 tail 行
  （`[PRIOR CONTEXT…]` 原尾部 + `[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]` + 摘要 + marker，
  `:5355-5378`）。
- 落库：`display_kind` 在 **flush** 路径由 `_summary_display_kind` 定：对 standalone handoff 返回 `"hidden"`
  （`agent/session_persistence.py:109-118`），行字段装配在 `:240-256`；而**压缩提交**路径走
  `_message_row_params`，直接绑定 `msg.get("display_kind")`（`hermes_state_messages.py:288, 299`，不调
  `_summary_display_kind`），`compress()` 又不设该键 → 提交落库的 standalone 摘要行是 NULL。同一个 builder 原样
  写入 `display_metadata`（`hermes_state_messages.py:270-284`），in-place 提交走它
  （`archive_and_compact` → `_insert_message_rows` → `_message_row_params`，`hermes_state_messages.py:
  907-935, 659-688`）。**摘要行目前不写 `display_metadata`**。
- 快照：`session.resume` 的 display 投影先过 `project_compaction_message_for_display`
  （`tui_gateway/session_history.py:306-308`），standalone 返回 `None` 被丢；merged carrier 剥成“真实尾部
  内容”并 **`pop("display_kind")`**（`agent/compaction_display.py:33-54`）；然后 `display_kind=="hidden"` 再跳
  一次（`session_history.py:315-318`）。所以要识别压缩行，**`display_kind` 不可靠**，只有
  `classify_summary_content`（`context_compressor.py:4218-4227`，standalone/merged）或
  `display_metadata` 可靠。
- 显示谱系：`get_resume_conversations` 返回 `display_history` = 完整 lineage + in-place 压缩归档行，按
  `_dedupe_display_generations` 去重（`hermes_state_messages.py:1650-1685, 1135-1143`），所以 resume 里
  “压缩前历史 + 摘要 + 去重后的尾部”本来就都在，只是摘要不可见。
- 实时：`status.update{kind:"compacting"|"compacted"|"compressing"}`。canonical 路径的发布点是
  `gateway/run_turn_progress.py::_status_callback_sync`（`:981-1000`）→ `_publish_execution`
  （`gateway/run_turn_runner.py:208-213`），完成边由 `agent/conversation_compression.py:88-94` 触发；
  sidecar 路径另有 `tui_gateway/server.py:741-752` 的重标与 `tui_gateway/methods_session.py:2062-2064, 2093`
  的手动 `compressing`/`ready`。以及 `agent.print`（`gateway/local_viewer_events.py:40-42`，客户端显式丢弃
  `omp-ui/src/modes/rpc/gateway-session.ts:782`）。**没有**结构化事件、token 数字或摘要句柄。
- 数字与方式已有真值，但只在遥测里：`tokens_before`/`tokens_after`（`estimate_messages_tokens_rough`，
  `context_compressor_telemetry.py:123-165`；提交边界换成实际形态 `conversation_compression_telemetry.py:
  45-64`）、`method` ∈ {`llm_summary`,`aux_fallback_main`,`deterministic_fallback`}
  （`context_compressor_telemetry.py:123-129`）、`trigger` ∈ {`idle`,`turn_start_threshold`,
  `engine_preflight`,`pre_api`,`post_tool`,`overflow`,`gateway_hygiene`,…}
  （`turn_context_compaction.py:206,389,467`、`turn_preflight.py:149,316`、`turn_overflow.py:156`、
  `gateway/run.py:258`）。手动 `/compress` 的 canonical 入口是 `session.mutate{operation:"compress"}`，
  `prepare_compress` 已算出 `before_tokens`/`after_tokens`（`gateway/session_mutation_compress.py:108-109`）。
- 客户端：`snapshotFrom` 优先 `display_messages`（`gateway-session.ts:465`），`rowToAgentMessage` 只映射
  user/assistant/tool、其余返回 `undefined`（`:357-392`），`addMessageToChat` 只接 user/developer/assistant
  （`omp-ui/src/modes/utils/ui-helpers.ts:130-190`），`status.update` 只切 `#compacting` 布尔
  （`gateway-session.ts:334, 396-399, 968-969`）。

因此本 ADR 决定：压缩摘要用**已有行 + `display_metadata` 盖章 + 显式投影参数**送到 omp，并补一条结构化
实时事件；Recap 用**客户端定时 + canonical `recap.generate`**，服务端复用 `/btw` 的 cache-parity fork 做一次
不落库的 side turn。两者都不改模型面。

## 决策

### D1. 压缩摘要的持久化：在 `compress()` 给摘要行盖 `display_metadata`，不动 `display_kind`、不新增列

盖章点在 **`ContextCompressor.compress()` 的返回前**（`agent/context_compressor.py:5434-5549`，独立行/merged
carrier 都在 `_assemble_compressed` 里成型，`_finalize_compressed` 已经把本次 attempt 的数字写进 `telemetry`
`_record_compression_effect`，`agent/context_compressor_telemetry.py:131-165`）：

```python
compressed = self._finalize_compressed(...)
self._stamp_compaction_display(compressed, telemetry)   # 新增；幂等
return compressed
```

```text
display_metadata["compaction"] = {
  "tokens_before": int,            # estimate_messages_tokens_rough，消息级
  "tokens_after": int,             # 同一口径
  "token_method": "estimate_rough",
  "method": "llm_summary" | "aux_fallback_main" | "deterministic_fallback",
}
```

- **为什么在 `compress()` 而不是 `_commit_compaction`**：canonical 的手动压缩并不经过 agent 的
  `_commit_compaction`——`gateway/session_mutation_compress.py::prepare_compress` 用一个 **detached**
  `ContextCompressor` 跑 `compress()`（`:81-88`），再把结果交给 receipt 写入。`compress()` 是两条路径**唯一**
  共同的生产者；在它返回前盖章，agent 提交、canonical mutation、rotation 三种落库都天然带上。
- 承载行的判定用现有的 `is_compaction_summary_message`（`context_compressor.py:5584-5590`），取 `compressed`
  里**最后一条**匹配项：standalone 摘要行，或 merged carrier（`_merge_summary_into_tail_row` 已给它打了
  `_compressed_summary`）。
- 只在本 attempt 真的产出压缩时盖章（`self._last_compression_made_progress`，由 `_finalize_compressed` 置位）；
  所有 no-op / abort / 拒收路径在 `compress()` 里提前 `return canonical_messages`，不盖章。
- 幂等：盖章前读旧值，相同则跳过。这会进入 `compress()` 之间的消息比较（如 `_engine_compress` 的
  `compressed == head`），所以“同值不重写”是硬要求，不能每次生成新 dict。
- 数字口径：`tokens_before`/`tokens_after` 取本次 attempt 在 `_finalize_compressed` 里写下的
  `_record_compression_effect` 值（`context_compressor_telemetry.py:131-165`），即**消息级
  `estimate_messages_tokens_rough`、压缩前是剪枝后的窗口**。它不读 `_record_committed_compression_effect`
  （那在 `compress_context` 里、提交之后才执行，`conversation_compression.py:4233`），因此与提交后的
  “实际形态”口径可能不同；行与事件都只承诺“同一份 estimate_rough”，验收 5 不要求它等于
  `prepare_compress` 的展示口径。
- `display_kind` **保持原值，不新增值**：压缩提交路径（`_insert_message_rows` → `_message_row_params`）写的是
  `msg.get("display_kind")`（`hermes_state_messages.py:288, 299`），而 `compress()` 从不设它 → standalone
  摘要行落库是 **NULL**；只有 flush 路径的 `_summary_display_kind` 会给 standalone 写 `"hidden"`
  （`session_persistence.py:109-118`）。两条路径都不显示摘要行：提交路径靠
  `project_compaction_message_for_display` 返回 `None`（`agent/compaction_display.py:33-48`），flush 路径靠
  hidden 跳过。**不要**依赖 `display_kind` 识别压缩行（D2）。这样所有现有投影（含 Desktop 的
  `display_messages`、CLI resume 面板、RAG/导出）行为逐字不变。
- 不新增 DB 列、不做迁移：`display_metadata` 是既有列，两种写入路径（`_message_row_params`，
  `hermes_state_messages.py:270-284`）都已经搬运它；`_strip_persistence_markers` 只删 `_db_persisted`
  （`context_compressor.py:381-388`），`_fresh_compaction_message_copy` 整 dict 复制
  （`:349-353`），metadata 不会在半路消失。
- 不接收 `trigger`/`in_place`：`compress()` 不知道它们（detached compressor 更是无从知道），行里只存**它确实
  知道**的四个值。`trigger` 只出现在实时事件里，由两个知道自己的调用方各自填写（D3）。
- 只在**摘要真被写进这套消息**时盖章：provider 原生压缩（`agent/native_compaction.py`）与 micro-compaction
  的相邻 user 合并不是 omp 的 compaction 语义，不盖章、不出块。

理由：omp 的块是“一条边界记录 + 摘要正文”，Hermes 的摘要行正好是这条记录；缺的只是数字/方式与“允许
看见它”的许可。把许可做成**投影参数**而不是行状态，才能同时满足 ADR-2 D4 的跨客户端约束。

### D2. 快照投影：`include_compaction` 显式参数 + 一个提取函数

- `CanonicalResumeParams` 增加 `include_compaction: bool = False`（`tui_gateway/contracts/
  canonical_sessions.py:70-74`，紧邻 `display_projection`）；`gateway/session_controls.py:709-714` 把它传给
  `display_projection`，再传给 `server._history_to_messages(..., include_compaction=...)`。
- 默认 false → 与今天**逐字相同**的 `display_messages`；true → 额外的 compaction entry。Desktop 目前只传
  `display_projection`（它读 REST 的同一投影），所以不受影响。
- 投影规则（只在 `include_compaction=True` 时生效；必须在 `project_compaction_message_for_display` **之前**
  判断，因为该函数对 standalone 返回 `None`、对 merged 会 `pop("display_kind")`
  （`agent/compaction_display.py:33-54`）；也必须在 `role not in _HISTORY_ROLES: continue`
  （`tui_gateway/session_history.py:282, 311-312`）之前 `append` 并 `continue` 掉该行——`_HISTORY_ROLES` 是
  **模型行**角色集合（user/assistant/tool/system），**不要**把 `compactionSummary` 加进去）：

| 行 | 输出 |
|---|---|
| standalone 摘要行（`classify_summary_content == "standalone"`） | 一条 `{role:"compactionSummary", text:<正文>, timestamp, display_metadata:{compaction:{...,"boundary_row_id":<该行 id>}}}`；**不**再输出该行的文本 |
| merged carrier（`"merged"`，或带 `display_metadata.compaction`） | 先输出 compaction entry（`boundary_row_id` 指向 carrier），再按今天的方式输出 carrier 的正常投影（`_strip_context_summary_handoff_message` 已剥成真实尾部内容） |
| 其它行 | 原样走今天的逻辑 |

- 正文提取：新增公开静态方法 `ContextCompressor.summary_body_for_display(message) -> str`，实现就是
  `_strip_summary_prefix(_content_text_for_contains(content))`（`context_compressor.py:4189-4207`：先按
  `_MERGED_SUMMARY_DELIMITER` 取摘要段，再剥 current/legacy/historical 前缀，最后在 end marker 处截断）。
  非摘要行返回 `""`。**不**复制正则，也**不**在 DB 里存第二份正文。
- 数字与方式：取 `display_metadata.compaction`；缺失（本 ADR 之前的历史压缩）→ 客户端映射为
  `tokensBefore: 0, tokensAfter: undefined, method: undefined`，组件据此不画 badge、详情显示 “Compacted
  context”（`compaction-summary-message.ts:25-28, 255-269`）。**不估算、不编造**：老会话只多一条分隔条，
  没有金额。
- `boundary_row_id` 由投影从行的 `_row_id` 补进输出的 `display_metadata.compaction`，**不落库**（避免与
  `_row_id` 重复）；实时事件同源（D3）。它只用于客户端去重与折叠定位。
- 不加 `display_kind`：客户端分派键是 role（`addMessageToChat` 按 `message.role`），
  `TranscriptMessage.role` 是自由字符串（`tui_gateway/contracts/common.py:168-187`）；再加一个
  `display_kind` 就是第二份真值。
- `session.history` 不新增同名参数：omp 的 `/copy`、rewind 用的是 `#messages`（快照 + 实时），
  不需要另一条 RPC（ADR-2 D5 的入口判断不变）。若以后 outline 需要，同一辅助函数可复用。

### D3. 实时边：新增 `compaction.summary` 事件，只由“真提交”产生

```text
compaction.summary {
  text: str,                     # 摘要正文（同 D2 的提取结果，不含 SUMMARY_PREFIX/end marker）
  tokens_before: int,            # 消息级 estimate_rough
  tokens_after: int | null,
  token_method: "estimate_rough",
  method: str,                   # D1 的 Hermes 词汇
  trigger: str,
  timestamp: float,
  boundary_row_id: int | null,    # 分隔条锚定的 durable row（已落库时）；客户端用它去重
}
```

- 声明在 `tui_gateway/contracts/events.py`（与 `message.adviser` 同风格，`:255-256`）。
- 生产点 1（回合内自动压缩，**canonical 进程**）：`_commit_compaction`
  （`agent/conversation_compression.py:3577`）在提交成功后从本次 `compressed` 的承载行（D1 的 metadata +
  `summary_body_for_display`）组装记录，写到单槽 `agent._last_compaction_display`，`trigger` 取本次 attempt
  的 `attempt.seed["trigger_source"]`（`conversation_compression.py:3897-3940` 的 seed）。
  `_emit_compaction_done`（`:88-94`）随后把 `"compacted"` 交给 canonical 的 `agent.status_callback`，它接的是
  `ctx._status_callback_sync`（`gateway/run_turn_runner.py:513-516`）。**真实发布点在
  `gateway/run_turn_progress.py::_status_callback_sync`（`:981-1000`）**：local viewer turn 里它已经把
  `status.update{kind:_viewer_status_kind(event_type, …)}` 投给 `self._publish_execution(...)`
  （`:996-1000`）；在同一处、`event_type == "compacted"` 时从 `self._ctx.agent_holder[0]`
  （`:114-119` 的取法）读单槽并 `self._publish_execution("compaction.summary", payload)`
  （`_publish_execution` = `gateway/run_turn_runner.py:208-213` → `authority.publish_execution`，
  按 `executing[session_id] == generation` 校验，`gateway/session_authority.py:1007-1019`）。
  **必须在 `_prepare_gateway_status_message` 返回 `None` 的早退之前**发布：状态行可能被平台噪声规则抑制，
  事件不能被同一规则吞掉。**不新增回调**（ADR-2 D3/D11 的口径）。
  - `tui_gateway/agent_callbacks.py::_agent_status_update`（`:112-117`）是 **Ink sidecar** 的同一状态回调，
    不是 omp 的路径；本 ADR 不改它（sidecar 客户端不消费该事件，保持逐字兼容）。
  - 单槽在**每次 attempt 开始时复位**（与 `_last_*` 一族同位，`_begin_compression_attempt`，
    `conversation_compression.py:3897-3902`），只在本次提交成功后写入。必须这样：`_CompactionLifecycle.complete(
    force_terminal=True)` 会在 abort 时也发 `"compacted"` 终局（`:2533-2544`），不清槽就会把上一次提交的记录
    再发一遍。`boundary_row_id` 取承载 dict 的 `_row_id`（`_insert_message_rows` 在插入后写回 dict，
    `hermes_state_messages.py:684-686`）；rotation 路径的 child 行同源。
  - 回合外压缩（`compression.idle_compact_after_seconds` 的 turn-start 场景仍在 execution 内；而
    `gateway/run.py:258` 的 `gateway_hygiene` 在两次回合之间）没有 execution generation →
    `publish_execution` 返回 False，**没有实时事件**；行已落库，分隔条在下一次快照出现（D9）。
- 生产点 2（手动压缩）：canonical 是 `session.mutate{operation:"compress"}`；提交成功（`applied`）后，在
  `gateway/session_mutations.py` 里与 `fire_post_compaction_hook` 同一个 `finally` 分支
  （`:283-286`）用同一条 helper 从 `prepared["messages"]`（`gateway/session_mutation_compress.py:115`
  刚返回的压缩列表）取承载行与正文，`trigger="manual"`，然后
  `live.event_stream.publish(session_id, payload, event_type="compaction.summary")`
  （与 `review.summary` 同一发布方式，`gateway/run_turn_runner.py:433`）。`prepared` 已在作用域内，不需要
  额外的 DB 读；`preview`/`noop`/`confirmation_required` 在提交前就返回（`session_mutations.py:251-257`），
  不会误发。
- 不产生事件的情形：没有承载行（plugin context engine 自己压缩、provider 原生压缩、structural no-op、
  aborted/refused）。此时保持今天的 `status.update` 文本，不造分隔条。
- `status.update` 不变：它仍负责“正在压缩”的忙碌提示与输入排队；事件是块，状态是行，二者不互相替代。
  `agent.print` 继续不用于块（客户端继续丢弃，`gateway-session.ts:782`）。

### D4. 词汇与口径：只送 Hermes 真值，不映射 omp 的 method 词汇

- omp 的 `method` 是它自己的压缩策略枚举（`remote|soft|handoff|snapcompact|shake`），Hermes 的
  `method` 是“摘要怎么产生的”（`llm_summary|aux_fallback_main|deterministic_fallback`）。两者**不是同一
  概念**，所以客户端**不做**同义词映射（尤其不把 `in_place` 映射成 `soft`），未知值走组件的既有兜底
  `compacted`（`compaction-summary-message.ts:18-24`）。
- `tokens_before/after` 的口径是**消息级** `estimate_messages_tokens_rough`（不含 system prompt 与 tool
  schema），与 omp 的 contextTokens 不同；如实标注 `token_method: "estimate_rough"`，不换算、不补
  system/tools 去“对齐”omp 的数字。客户端原样显示（`256K→20K` 的 badge 语义不变）。
- `trigger` 原样送：它解释“为什么压缩”（preflight/idle/overflow/…），omp 没有等价字段，客户端可忽略。

### D5. 客户端：分隔条 + disclosure 用编译好的组件；`collapseCompacted` 是渲染期省略

- `rowToAgentMessage`（`gateway-session.ts:357-392`）增加一支：`role === "compactionSummary"` →
  `{role:"compactionSummary", summary: text, tokensBefore, tokensAfter, method, timestamp}`；
  数字/方式从 `display_metadata.compaction` 取，缺失按 D2 的 0/undefined。
- `addMessageToChat`（`ui-helpers.ts:130-190`）增加 `case "compactionSummary"` →
  `new CompactionSummaryMessageComponent(message)`（`@oh-my-pi/pi-tui/chat/compaction-summary-message`）。
  组件默认 collapsed、`ctrl+o` 展开正文，分隔条靠前一个空行（`compaction-summary-message.ts:204-216`）。
  **不改**它的布局/文案。
- 实时：`gateway-session` 增加 `#onCompactionSummary`，把 entry 追加到 `#messages` 并 emit 一个 shell 自有
  事件（`AgentSessionEvent` 增加一支 `{type:"compaction_summary", message}`，与 `notice` 同类，属于本客户端
  的词汇，不伪装成 omp 本机事件）；`event-controller` 收到后走同一个 `addMessageToChat`。追加到 `#messages`
  是必须的：omp 的摘要**是 entry**，会出现在 `/copy` 与 tree（`copy-selector.ts:89-92`、
  `tree-selector.ts:31`）。类型上不需要额外声明：`CompactionSummaryMessage` 已经通过声明合并进入
  `CustomAgentMessages`（`omp-ui/src/vendor/agent/src/compaction/messages.ts:80-87`），`AgentMessage` 的并集
  本来就包含 `compactionSummary`；现有的 `#lastAssistantMessage`（`gateway-session.ts:1116-1121`）按
  `role==="assistant"` 过滤，追加摘要不会污染回合结束/合并路径。
- `display.collapseCompacted`（默认 true，`settings.ts:744-755`）：在 `renderSessionContext` 重建时，`true`
  则**省略**最新 compaction entry 之前的行（分隔条与它之后的尾部照常渲染），`false` 则全量 inline + 每个压缩
  点一条分隔条。省略只发生在渲染层：`#messages`、快照、`session.history`、rewind/导出都保留全部行；切换设置
  触发既有重建（`interactive-mode.ts:1228-1240`）即恢复。
  - 与上游的差异必须写进实现注释：折叠实现在上游已删除，本 ADR 把它定义为“渲染期省略、靠设置恢复 inline”，
    而不是“分隔条上的展开按钮拉出历史”。`ctrl+o` 只展开摘要正文（组件的既有语义）。

### D6. Recap 的职责切分：客户端定时 + canonical `recap.generate`

**客户端做**（只有它知道这些状态）：
- 定时：`agent_end` 且“最后一条是生成型 assistant”才 arm；`turn_start`、提交、压缩状态、断线/切换会话都
  invalidate。用 `recap.enabled` / `recap.idleSeconds`（壳里已有的注册项，钳 1..3600，`settings.ts:
  1251-1283`）。
- 抑制：未就绪、会话不是当前视图、`editor.getText().trim()` 非空（`pi-tui/src/components/editor.ts:3242`）
  都不 arm、也不交付。对齐 `interactive-mode.ts:1836-1859` 的 `isBlocked`。
- 交付：拿到文本后再复查一次同样的条件，才挂 `RecapNotice`。

**服务端做**（只有它是权威）：
- 会话是否还活着、是否在跑、有没有排队 admission / 待答 prompt / 未完成审批——这些以
  `authority._handle(ref).execution_state`（`gateway/session_commands.py:325,468,498` 的同一读法）与
  队列/Prompt 快照为准；不合格返回 `skipped`，不调模型。
- 最后一条消息是否仍是“生成型 assistant”（末尾不是 user-only、不是 toolResult）；
- 尾巴是否已经 recap 过（`_last_recap_tail_uid`）：同一尾巴再调只返回 `skipped`，把“一次活动一段一次
  recap”落在权威侧，客户端重试不会双花。

**不采用**：网关自己起 idle 定时器。它会缺 `isComposing`/当前视图这两个只有客户端有的输入，只能盲发，
必然产出 omp 不会有的 recap。

### D7. 生成：复用 `/btw` 的 cache-parity fork，prompt 逐句移植

- 方法：`recap.generate {session_id} -> {text, generated_at, skipped?}`，用
  `tui_gateway/contracts/canonical.py` 的 `canonical_method` 声明（authority-only，sidecar 不实现，避免覆盖
  门把 sidecar 当服务方；ADR-3 D1 的先例）。处理函数放新模块 `gateway/session_recap.py`，在
  `gateway/session_controls.py::handlers()`（`:221-236`）注册。
- 线程：返回 `DeferredReply(work)`（`gateway/session_llm_oneshot.py:137-160, 262`），模型调用跑在该 owner
  既有的 oneshot 池上，不占串行 dispatch；`work` 里 `with owner_scope(authority)` 绑定 profile 作用域
  （`:252-263` 的同一写法）。
- 生成体：`agent/recap.py::generate_recap(agent, *, goal, task)`：
  - fork：`agent/background_review.py::build_cache_parity_fork(parent, {}, max_iterations=1,
    write_origin="recap")`（`:969-1030`）——同 system prompt / 同 `tools[]` 命中缓存、`_persist_disabled`、
    `_session_db=None`、不建新会话行；
  - 禁工具：`set_thread_tool_whitelist(set(), deny_msg_fmt=…)`（`agent/side_question.py::_answer_via_fork`
    `:117-138` 的同一手法）；
  - 输入：`conversation_history=trim_snapshot_for_fork(parent._session_messages)`（`:40-51`；D6 的守卫
    已保证尾巴是完整 assistant，trim 通常为空操作）；
  - prompt：`_RECAP_PROMPT` = omp `recap-user.md:1-9` 的**同文**（含 `Overall goal:` / `Active task:` 段与
    “no markdown、1–2 句、≤40 词、先目标/当前任务再一个下一步、跳过根因与次要 todo”）+ developer 级
    “本轮没有工具、只回纯文本”提醒（`side-channel-no-tools.md:1-5`）；
  - 锚点：`goal` = `hermes_cli/goals.load_goal(session_id)`（`hermes_cli/goals.py:636`）的目标文本，否则会话
    title；`task` = `agent._todo_store` 里第一个 `pending|in_progress` 项（`agent/agent_init.py:1170`、
    `agent/conversation_compression.py:3010` 的同一数据源）；
  - 预算：`max_iterations=1`、不设 temperature（沿用父的 route 默认）；输出上限用既有的“一次性上限”
    `fork._ephemeral_max_output_tokens = 256`（`agent/chat_completion_helpers.py:1370-1383` 会消费它），
    provider 超时照常。
  - 清洗：`trim()` + 折叠 >3 次重复行 + 4 KiB 截断（对齐 `dedupeEphemeralReply`，
    `agent-session.ts:12902-12905`）；空串视为失败。
  - 记账：`release_cache_parity_fork(parent, fork, task="recap")`（`background_review.py:1151-1162`），成本记
    到该会话的 `recap` 任务下，不新增一次回合、不写 usage 行。
  - 失败：`logger.debug` 后返回空串（omp 静默，`idle-maintenance.ts:357-362`）；wire 上是
    `{text: "", skipped: "failed"}`，不弹错误。
  - 无 live parent（会话已被 evict/回收）：直接 `skipped: "no_agent"`，**不**回落 aux oneshot——那会把
    recap 变成另一套摘要语义，也拿不到同一段缓存前缀。
- **小重构**：把 `_answer_via_fork` 里“建 fork → 禁工具 → run → release”的骨架提成
  `agent/ephemeral_prompt.py::run_ephemeral_prompt(parent, prompt, *, task, max_iterations,
  max_output_tokens, on_fork=None)`（`max_output_tokens` 落成 `_ephemeral_max_output_tokens` 一次性上限），
  `/btw` 与 recap 共用；不复制第二份（`/btw` 的 prompt 仍是它自己的）。

### D8. 客户端交付：`RecapNotice` 直挂 chatContainer，不进 `#messages`

- `RecapNotice` 从 `@oh-my-pi/pi-tui/chat/recap-notice` 引入，构造参数就是**未经截断**的文本；`※ recap:`、
  dim+italic、`Spacer(1)`、native `omp.recap` 都由组件负责，网关**不送**前缀或格式。
- 单行化与 280 列截断在客户端：`previewLine(text, TRUNCATE_LENGTHS.RECAP)`（`render-utils.ts:185-195,
  248-250`）。
- 挂载点是 `chatContainer.addChild(...)`，**不**进 `#messages`：omp 的 recap 不是 entry，所以不能出现在
  `/copy`、rewind、resume 快照里（`session-index.ts:11-14`；`chat-transcript-builder.ts:299-398` 无分支）。
  重建/切换会话/`resetTranscript` 后消失，与上游一致（`interactive-mode.ts:8402-8407`）。
- 一次活动只交付一条：收到非空文本后清掉本段 pending；下一段 `agent_end` 重新 arm。
- **不移植** `session_recaps` journal：omp 的消费者是 archive/eval 视图，随 §0.3 已删除；为没有消费者的数据
  建表就是死重量。若将来要给 `out://`/archive 供数，再单独立项。
- **不改** `hermes_cli/session_recap.py`、不新增 `/recap` 命令：omp 没有手动 recap，本地多行面板是另一个功能，
  不得互相冒充（plan §2.3.2.1:442）。

### D9. 作用域与降级

| surface | 压缩摘要 | Recap |
|---|---|---|
| omp-ui ↔ canonical local gateway | 行 + `compaction.summary` + `include_compaction` | 客户端定时 + `recap.generate` |
| Desktop | 默认 payload 不变（不传 `include_compaction`）；继续看 `status.update` 文本 | 不调用该方法，无变化 |
| 消息平台 / ACP / headless | 不投影、不推事件（本来就只读文本状态） | 无调用者，无变化 |
| turn-isolated（compute host）压缩 | 行由 owner 落库，下次快照按 `include_compaction` 出分隔条；**实时事件不转发**（worker 帧不放宽，ADR-2 D1/D11 先例） | 不适用（recap 要求 resident agent；被 evict 的会话返回 `skipped: "no_agent"`） |
| 模型面（context / system prompt / tools） | 不变 | recap 的 side turn 带同一段前缀（命中缓存），但**不写会话**，下一回合前缀逐字不变 |

## 协议

```text
# 快照（additive，默认关闭）
session.resume {..., display_projection: true, include_compaction?: bool = false}
  -> {..., display_messages: [
        ...,
        {role: "compactionSummary", text: "<summary body>", timestamp: float,
         display_metadata: {"compaction": {tokens_before, tokens_after, token_method, method,
                                           boundary_row_id}}},
        {role: "user", ...},          # merged carrier 场景：分隔条之后是该 carrier 的真实尾部内容
        ...,
      ]}

# 实时（additive event）
compaction.summary {
  text, tokens_before, tokens_after|null, token_method, method, trigger, timestamp, boundary_row_id|null
}

# Recap（canonical，authority-only）
recap.generate {session_id} -> {text: str, generated_at: float, skipped?: "busy" | "no_new_turn" |
                                                                "no_agent" | "failed"}
```

## 分阶段

| 阶段 | 内容 | 完成判据 |
|---|---|---|
| P1 | D1 盖章 + D2 投影 + D3 事件 + D4 口径 + D5 分隔条映射 | omp 在回合中看到一条分隔条，resume 后在原位看到同一条；Desktop 的 `display_messages` 逐字节不变 |
| P2 | D5 的 `display.collapseCompacted` 渲染期省略 | 切换设置后重建，压缩前历史隐藏/恢复；`#messages`/`session.history` 行数不变 |
| P3 | D7 的 `recap.generate` + `ephemeral_prompt` 抽取 + prompt 移植 | 直接调用 RPC 得到 ≤40 词一句；不发请求时不写任何行、不改上下文 |
| P4 | D6/D8 的客户端定时器、抑制、`RecapNotice` | 空闲到点出现 `※ recap:`；输入草稿/离屏/在跑时不出现；resume 后不出现 |

## 验收

1. 回合内自动压缩：**恰好一条** `compaction.summary`；omp 在压缩点插入一条分隔条，位置在最后一条压缩前消息之后、
   第一条保留消息之前；`status.update` 仍是 notice，不重复成块。
2. 快照：`include_compaction=true` 时摘要行被投影成一条 `compactionSummary`，正文等于
   `summary_body_for_display`（无 `[CONTEXT COMPACTION …]`、无 end marker）；`merged` carrier 场景是
   “分隔条 + 真实尾部内容”，不丢行、不重复行（对比 `_dedupe_display_generations` 后的行数）。
3. 默认路径不变：同一批行，`include_compaction=false` 的 `display_messages` 与改动前逐字节相同
   （Desktop/CLI 的回归基线）。
4. 模型面不变：压缩前后送给 provider 的 `messages` 与 system prompt 只由压缩本身决定；盖章不改变
   `sanitize_context`/wire 结果（对比压缩后下一回合的首个请求）。
5. 手动压缩（`session.mutate{operation:"compress"}`）：提交后一条事件；事件里的 `tokens_before/after` 与
   落库行 `display_metadata.compaction` **逐字节相同**（同一份记录）。partial / 剪枝场景下它必然与
   `prepare_compress` 的 `before_tokens/after_tokens` 不同（后者对未剪枝的完整列表算），这是**预期**，
   不要求对齐；no-op/refused 不产生事件、不产生分隔条。
6. 老会话：本 ADR 之前压缩的会话 resume 后也出现分隔条，badge 无金额、详情为 “Compacted context”，
   不显示任何估算数字。
7. `display.collapseCompacted`：true 时压缩前历史不渲染；false 时全量 inline + 每个压缩点一条分隔条；
   两种状态下 `/copy` 与 rewind 都能拿到全部行（行没有被删除）。
8. Recap 正常：空闲到点 → 一行 `※ recap: …`，单行、≤280 列（超长由客户端截断），不新增 `#messages` 条目、
   不写 DB、`session.history` 行数不变。
9. Recap 抑制：composer 有草稿、会话不在前台、回合在跑、有排队输入、上一次 recap 之后没有新的生成型 assistant
   → 客户端不发请求 / 服务端返回 `skipped`，不花模型调用。
10. Recap 隔离：生成前后会话消息数组**逐字相同**；下一回合首请求的前缀字节不变（缓存不失效）；
    fork 用量记在 `task=recap` 下，不产生 assistant 行、不产生 usage 行。
11. Recap 失败（断网/鉴权/空回复）：UI 无任何提示或块，日志一条 debug；下次空闲可重试。
12. turn-isolated 压缩：客户端不收到 `compaction.summary`，也不报错；若该路径的引擎是
    `ContextCompressor`（本地 compute host 的默认），resume（`include_compaction=true`）能看到分隔条，
    plugin engine 不保证（没有承载行就不出块）。

## 风险

| 风险 | 缓解 |
|---|---|
| 摘要在 UI 里“看起来像新消息”被误当成用户输入 | 分隔条是折叠的 Divider+Disclosure，正文默认不展开；模型侧文本仍带 `SUMMARY_PREFIX`，显示正文经 `_strip_summary_prefix` 剥净 |
| `display_metadata` 渗进 wire/模型请求 | 既有链路已证明安全（`todo_reminder`/`tool_result_metadata` 同样存 `display_metadata`）；验收 4 专门盯首个请求 |
| 老行没有 metadata，客户端显示 0 tokens 被当成真值 | 客户端映射成“无金额”（0/undefined），组件本来就不画 badge；ADR 明确不估算 |
| 归档/dedupe 让分隔条落错位置 | 投影只按 DB 行序与 `display_metadata.compaction.boundary_row_id`，不按模型数组推断边界；验收 2 用 merged + in-place 两种提交形态各测一次 |
| 实时事件与快照重复画分隔条 | 实时 entry 与快照 entry 用同一个 `boundary_row_id`，客户端按它去重；实时事件先到、快照后到，或反之，都只留一条 |
| `collapseCompacted` 与上游语义不完全一致 | D5 明确差异（渲染期省略、靠设置恢复），实现注释写清；不提供“展开历史”的伪功能 |
| Recap 全量上下文请求成本 | 与 omp 同构（一次 full-context 输入，命中缓存）；每段活动最多一次，尾巴重复直接 `skipped`；失败静默不重试 |
| Recap 与新一轮提交竞态 | 服务端提交前校验 `execution_state`；客户端拿到回复后再复查一次未阻塞才挂载；迟到的回复直接丢弃 |
| 长/慢模型调用被 pool 占满 | 复用 oneshot 池的既有上限（4 跑 / 16 准入，`session_llm_oneshot.py:32-33`），超限按 `oneshot_busy` 拒绝，recap 视为 silent skip |
| profile 泄漏（后台线程读错 profile） | `work` 内 `owner_scope(authority)` + fork 继承父 agent 的 scope（HX002/HX004） |

## 不采纳

| 方案 | 为什么 |
|---|---|
| 把 `_summary_display_kind` 从 `"hidden"` 改成可见 | 一次性改动会影响所有客户端（Desktop、CLI resume、导出、RAG）；ADR-2 D4 的第二条理由仍成立，只是改成显式参数后才能安全放开 |
| 在 `display_messages` 里塞 `role:"user"` + 摘要正文 | 客户端只能画成用户气泡（plan §2.3.2.1:441 已判定），且与真实 user 行不可区分 |
| 用 `agent.print` / `status.update` 文本拼摘要块 | 那是纯文本状态，没有正文/数字/边界；造假块违反 §0.1 |
| 用 `review.summary` 当 recap | 语义是 review 结果，不是“离开期间进展”（plan §2.3.2.1:442） |
| 用 `hermes_cli/session_recap.py` 的本地面板当 recap | 无 LLM、多行统计，与 omp 的 ≤40 词一句不是同一产物 |
| 把 omp 的 `method` 词汇套在 Hermes 的压缩方式上 | 概念不同（策略 vs 摘要来源）；错映射会让分隔条写错维护方式 |
| 网关自起 idle 定时器 | 缺 composer/当前视图两个客户端输入，会产生 omp 不会有的 recap |
| 新增 `session_recaps` 表 | 消费者（archive/eval）已随 §0.3 删除；无消费者的持久化是死重量 |
| 直接复用 `answer_side_question` 当 recap | 它的 prompt 是 `/btw` 专用；且无 live parent 时回退 aux oneshot + 渲染摘要（`agent/side_question.py:147-200`），那不是 omp 的语义。recap 要求 resident agent，否则 `skipped:"no_agent"`；共用的是 fork 骨架与隔离手法，不是它的 prompt/fallback |
| 把 recap 持久化成 `display_kind="recap"` 行 | `display_kind`/`display_metadata` 只是从 wire 上被剥掉的字段（`PERSISTENCE_ONLY_MESSAGE_FIELDS`，`agent/message_metadata.py:46-54`），**行本身仍进 `build_api_messages`**（`agent/turn_context.py:1259-1306`）→ 下一回合会作为真实 user 输入、并改写缓存前缀。recap 只走实时组件，不落行 |
| 客户端自己调模型做 recap | 壳没有 system prompt/tool 面/模型 route；omp 的 recap 是会话自身 agent 的 side turn |
| 恢复 `recordSessionRecap`/archive prelude | 本 ADR 只补显示与生成，不复活已删业务代码 |
