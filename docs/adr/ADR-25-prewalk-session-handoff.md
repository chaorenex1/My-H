# ADR-25：prewalk——会话级规划/实现两段式模型交接（omp 移植）

**状态**：已决定（2026-11-17）。基础交接状态机已落地；**D8 的对齐决策要求每一项差异都向
oh-my-pi 收敛，未完成项按 D12 实施，不声称已实现**。**本 ADR 是 prewalk 落地契约的唯一权威**；
ADR-12 D5 只保留 2026-11-04 的决策背景。细化 ADR-17 P5 的 auto 消费者；ADR-12 的选项 C 结论与
“默认关闭、只在边界切换”的约束不变。

**关联**：omp `docs/prewalk.md`、`packages/coding-agent/src/prompts/judge/auto-prewalk.json`（本地
checkout `C:\Users\zarag\projects\oh-my-pi`，HEAD `6a86df85`）；ADR-11（typed judgment、native
判定）、ADR-17 P5（`auto-prewalk` 问题库、`resolve_judge`、0.70 阈值）、ADR-18/ADR-19（系统提示与
请求级注入）、ADR-20（请求级注入：D8 的 planning nudge / implementation checklist 走此路径，
**不改系统提示**）。

**范围**：本地交互会话（`cli` / `tui` / `desktop` / `omp-ui` / `local`）的一次性模型/thinking
交接、它的会话状态、RPC/CLI、auto 分类器与失败策略。**D8 的对齐决策把以下原“不做”项纳入范围**：
子代理 prewalk、`--prewalk*` 启动参数、omp 的 planning nudge / implementation checklist（请求级
注入）、omp-ui `/prewalk` 命令、本地 notice。**仍不做**：消息平台、headless 运行、plan 模式
进入/退出的联动、远程 provider 的凭据发现之外的模型选择。

**实现落点（当前工作树）**：

| 层 | 文件 |
|---|---|
| 状态机 | `agent/prewalk.py`（`arm`/`disarm`/`restart`/`status`/`begin_auto_task`/`advance_after_tool_batch`） |
| 回合接线 | `agent/turn_consumers.py:43`（任务开始）、`agent/turn_tool_round.py:264`（工具批边界） |
| 网关 | `tui_gateway/methods_prewalk.py`、`tui_gateway/contracts/prewalk.py` |
| CLI | `hermes_cli/cli_prewalk_command.py`、`hermes_cli/cli_commands_mixin.py:2096` |
| 配置 | `hermes_cli/config_defaults_consumers.py:27`、`hermes_cli/cli_config_load.py:204` |
| 问题文件 | `agent/prompts/judge/auto-prewalk.json` |
| 测试 | `tests/agent/test_prewalk.py`、`tests/tui_gateway/test_prewalk_rpc.py`、`tests/hermes_cli/test_prewalk_slash.py` |

**D8/D12 的目标落点（待实现）**：`agent/prewalk.py`（触发/same-route/清理顺序）、
`agent/prompts/prewalk*.md` + 请求级注入（nudge/checklist）、`agent/prewalk_subagents.py`（P3）、
`hermes_cli/`（启动参数 + `/prewalk` 本地 notice）、`omp-ui/src/`（`/prewalk` 命令 + 设置）、
`tui_gateway/contracts/prewalk.py`（若新增取消/状态字段）。

---

## 问题

强模型的价值集中在前期：读仓库、定位边界、诊断、定方案。方案定了之后，实现往往是机械的、可验证
的。omp 的 prewalk 把这件事做成**会话状态**，而不是一条 prompt 指令：

1. **提示词做不到“一次”**：模型不能可靠地自行判断何时该换模型，也不能保证只换一次；把“请交给
   快模型”写进 prompt 会让缓存前缀随会话内容漂移，且无法在 resume/revive 后恢复正确模型。
2. **切换时机必须可判定**：在 turn 中间换模型会让同一批工具调用、同一段流式输出跨两个 provider，
   既破坏缓存前缀，也让计费与 transcript 归属含混。
3. **auto 需要校准概率**：普通文本模型给不出可比较的 0.70 概率，只有 native judge（TypeSafe
   jev / OpenRouter / OpenAI Decisions / Workers AI Clef）能。
4. **失败必须 open**：判定服务、目标模型解析、provider 切换任一失败，都不能把用户的任务卡住或
   偷偷换模型。

omp 的答案（`docs/prewalk.md`）是：默认关闭；显式武装或 auto 判定；todo 成功打开闸门；在包含
第一个 `edit`/`write` 结果的 assistant turn **结束后**一次性切换；状态写进 session；resume 继续在
目标模型上；每个新任务先回到该任务的规划模型再判定。

Hermes 已有 `model.roles.*`、`switch_model`、`todo` 工具与 ADR-17 的 judgment 服务，缺的正是这层
会话状态机。ADR-12 D5 已把“怎么做”定到选项 C；本 ADR 记录**实际落地的契约**与和 omp 的差异。

---

## 决策

### D1. 状态机拥有交接，提示词不拥有

`agent/prewalk.py` 是唯一 owner：武装/解除/重启、闸门、切换、auto 判定、持久化都在这里；网关
（`methods_prewalk.py`）和 CLI（`cli_prewalk_command.py`）只是薄适配器。**当前版本没有 planning
nudge / implementation checklist，也不改系统提示或工具面**；D8/D12 已决定通过 ADR-20 的请求级
注入补上这两段，ADR-18/19/20 的 prompt 装配与缓存不变量保持不变。模型不需要“知道”prewalk，
提示词只负责在 armed 时给出规划提示、交接后给出实现清单。

切换只发生在工具批**完成之后**（`turn_tool_round.py:264`），不在单次工具调用之间、不在流式输出
中间、不在请求构造中途。

**提示词面按 omp 对齐（待实现，D8/D12）**：武装会话注入 planning nudge；交接后注入
implementation checklist。两段都必须走 ADR-20 的请求级注入（不改系统提示、不换工具面、不写
transcript），并在交接完成的同一回合移除 nudge。

### D2. 会话状态：`model_config.prewalk`，不新增表/列

状态键 `PREWALK_KEY = "prewalk"`（`agent/prewalk.py:22`），存进 session 行的 `model_config`：

```json
{"armed": true, "from": "@default", "into": "@smol", "consumed": false,
 "auto": false, "auto_target": "",
 "planning_model": "…", "planning_thinking": {"enabled": true, "effort": "high"},
 "planning_provider": "…", "planning_base_url": "…", "planning_api_mode": "…"}
```

读写语义（`_load_state` `:99`、`_persist` `:137`）：

- **懒加载**：首次读时从 `get_session_model_config_value(sid, "prewalk")` 水合；agent 上缓存
  `_prewalk_state`，同一回合内不重复读库。
- **行存在即已决策**：只要读到 mapping（哪怕是 `armed=false`），`_prewalk_state_from_row=True`；
  `prewalk.enabled` 的自动武装**只在该会话首次水合、且还没有 `prewalk` 行时生效**。因此
  resume/revive 一个从未写过该行的会话也会在首个任务按配置武装；而写过该行（含 `/prewalk off`
  或已消费）的会话不会重新武装。
- **消费即终态**：`consumed=true` 时强制 `armed=false`；resume 后不会第二次交接。
- **失败软化**：读写库、发布事件、解析 effort、切换 provider 的异常一律 debug 日志；状态机对外返回
  结构化 outcome，不向 turn 抛异常。
- `planning_thinking` 经 `_jsonable` 归一化；未知对象字符串化，保证可写回 JSON。

### D3. 目标解析与切换

`arm()` **先验证再写状态**（`:358`）：

- `into` 用 `resolve_model_selector()` 解析，必须得到一个非空 `model`；`@default` 或未 pin 的 auto
  aux 槽没有独立目标 → 报 `ValueError`，不会“武装后永远不切换”。
- `from` 同样走 selector 校验；`@default` 是“继承主路由”的合法标记，只用于 `/prewalk restart`
  的返回目标。
- 解析结果包含 `provider/model/base_url/api_key/reasoning_effort/role`。

切换（`_switch_to_route` `:225`）：

- 跨 provider：`resolve_runtime_provider()` 解析目标 provider 的 endpoint/凭据/API mode（与
  `/model` 同一条 ladder）；显式 `base_url`/`api_key` 优先。
- 同 provider：保留 live endpoint/凭据，`api_mode` 留空让 `switch_model` 按目标模型重解析
  （dual-wire provider）。
- 调用 `agent.switch_model(...)`，随后 `_apply_effort()` 应用目标的 `reasoning_effort`。
- **same-route 判定按 omp 对齐（待实现，D8/D12）**：目标模型与当前模型一致，且有效
  thinking/effort 也一致时，交接直接 disarm，不调用 `switch_model`，避免一次无收益的缓存重建。
  路由等价用规范化后的 `(provider, model, api_mode, effort)` 判定；任一不同仍执行切换。

`planning_*` 在武装时快照（`:264`），`/prewalk restart` 与 auto 的“每任务恢复”都用它
（`_restore_planning` `:272`）：模型/provider 不同才切换，thinking 总是 `deepcopy` 回填。

### D4. 武装 / 解除 / 重启与本地权威

| 操作 | 语义 | 入口 |
|---|---|---|
| arm | 一次性武装；**`prewalk.enabled=false` 时也可显式武装**；清除 `_prewalk_auto_suppressed`；快照 planning；重置闸门；持久化并发布 `prewalk.armed` | `/prewalk [into] [from]`、`prewalk.arm` |
| disarm | 只清 `armed`，**不改模型、不改 consumed/planning 历史**；**目标（P1，与 omp 对齐）**：取消进行中的切换；置 `_prewalk_auto_suppressed=True` 阻止本会话 auto 再武装；同时持久化一行 `prewalk`，因此“仅首次水合”的 `prewalk.enabled` 武装也不会重放。直到显式 arm/restart 或 `/new` | `/prewalk off`、`prewalk.off` |
| restart | 先 `_restore_planning`，再以 `into/from`（缺省取状态/配置）重新 arm；**目标（P1，与 omp 对齐）**：若已武装且请求的 `into` 与现有目标不同，先拒绝（不改 active model），不静默重定向 | `/prewalk restart`、`prewalk.restart` |
| status | 只读：`enabled/armed/from/into/consumed/auto/auto_mode/auto_target/planning_*/gate` | `/prewalk status`、`prewalk.status` |

- **本地权威**：RPC 方法带 `@_profile_scoped`；CLI handler 只接受
  `LOCAL_SURFACES = {cli,tui,desktop,omp-ui,local}`。消息平台/headless 不能武装他人会话的模型交接。
- 事件：`prewalk.armed`（arm/restart/off 的状态变化，payload 只含 enabled/armed/from/into/
  auto/auto_target）与 `prewalk.handoff`（真正切换时的 from/into/provider）。事件只发给发起会话的
  本地 viewer，不写入 transcript。
- `restart` 在**已武装且目标不同**时不静默重定向：先校验目标，restore 失败只记日志。omp 对
  “已武装且目标不同”直接拒绝；Hermes 的行为是 restart 以新目标重新武装，`from` 仍回到快照。
- `/new` 由一个全新 session 行开始：没有 `prewalk` 行 → 按 `prewalk.enabled/into/from` 重新武装。
  旧会话的 consumed 状态不会跨会话继承。

### D5. 闸门与触发：todo 成功才开闸，首个 `edit`/`write` 结果交接

`advance_after_tool_batch()`（`:635`）每个完成的工具批调用一次：

1. 未武装/已消费/被打断 → 直接返回（`not_armed` / `interrupted`）。
2. 收集本批 assistant 消息的 `tool_calls`，只取匹配 `tool_call_id` 的 tool 结果；批后追加的 TTSR/
   steer 行、其它 call 的结果一律不算（`collect_tool_results` `:573`）。
3. **todo 闸门**：工具面包含 `todo_list`（旧名 `todo`）时，任何**成功**的 todo 调用——包括只读
   `view`——打开 `_prewalk_todo_gate`；工具面没有 todo 时闸门恒开（`todo_available` `:295`、
   `_gate_open` `:309`）。同一批里 todo 成功 + 编辑结果也直接交接。
4. **触发**：本批第一个 `write_file`/`patch` 结果（`EDIT_TOOL_NAMES` `:32`）。**与 omp 对齐：
   不要求成功**；失败的编辑也算实现已经开始，交接后由目标模型接手修复。todo 闸门仍要求成功
   （omp 的 todo gate 与 edit/write trigger 本来就是两套语义）。
5. 解析目标；若目标与当前路由等价（模型 + 有效 thinking/effort）→ 直接 disarm（same-route，
   见 D3）；否则 `switch_model` + effort；置 `consumed=true, armed=false`；清闸门；持久化；
   发布 `prewalk.handoff`。
6. 目标解析失败保持武装（`target_unresolved`），切换抛异常保持武装（`switch_failed`），用户可
   重试或 `/prewalk off`。

**与 omp 的当前差距**：代码仍是“成功编辑才触发”（`_succeeded` 也用于 edit/write）。D8/D12 的
P1 改为只看工具名与结果存在，`_succeeded` 只保留给 todo gate。

### D6. auto-prewalk：native-only、固定 0.70、每任务重新判定

配置 `prewalk.auto ∈ {off, suggest, on}`（默认 `off`），`prewalk.auto_target`（默认 `@smol`）。
`begin_auto_task()`（`:484`）在回合开始（`prepare_turn_consumers`）执行，顺序是：

1. **只处理新的一级本地任务**：`_turn_origin` 非空（fork/背景评审/侧问）或 `_delegate_depth > 0`
   时根本不进入；`_local_interactive()`（`:325`）排除消息平台与 quiet/headless。
2. **配置武装**：`enabled=true` 且状态未武装、未消费、且**本次是首次水合**（无持久化行）→ 以
   `from/into` 武装（`auto=false`）。这一轮不再做 auto 判定（`reason=config_arm`），避免同一任务
   被武装两次。
3. **每任务复位（先于 mode 门，目标 P1；与 omp 对齐）**：若当前武装来自 auto 且未消费，先解除
   （不跨任务携带）；若上一任务已消费且有 `planning_model`，先 `_restore_planning`。这两步在
   `auto` 判定之前执行，因此 `auto=off` 也会返回规划模型并丢弃未用武装。**当前实现仍在 mode 门
   之后执行**（见 D8“当前实现”列与 D12 P1）。
4. 清理完成后，`auto=off` 到此结束；`on/suggest` 才继续做分类。
5. `classify_auto(request, history)`（`:439`）：
   - `has_native_judge()` 为假 → `reason=no_native_judge`，不调用文本 judge；
   - 装载 `auto-prewalk.json` 的 `route` 问题，state = `{request: 前 4000 字符,
     previous_request, previous_response}`；前两条 history 各截 2000 字符，只用于解析
     “do that / continue”这类指代；
   - `judge(..., purpose="auto-prewalk", timeout_ms=4000)`；概率取 `probabilities["prewalk"]`，
     缺失且 choice=prewalk 时按 1.0；异常/超时/problem 解析失败 → `judge_failed`，不武装。
6. 只有 `route == "prewalk"` 且 `probability >= 0.70`（`AUTO_PREWALK_THRESHOLD` `:26`）才武装，
   目标取 `auto_target`；目标不可解析 → `target_unresolved`，不武装。
7. **显式武装优先**：已有非 auto 的武装时不重定向（`explicit_arm`）；`/prewalk off` 通过
   `_prewalk_auto_suppressed` 阻止本会话再次 auto 武装。
8. `suggest` 只返回分类结果（`route/probability/reason`），永不 arm；**与 omp 对齐**：每个被判定
   的任务输出一行 `Auto-prewalk: prewalk 0.84` / `primary (prewalk 0.18)` /
   `direct (prewalk 0.06)` / `prewalk 0.55 (below threshold)`，只写日志/本地状态行，不进
   transcript、不写系统提示。当前实现丢弃 outcome（D8/D12 P2）。

判定失败、目标不可用、低概率、`primary`/`direct` 都保持模型/thinking/prewalk 原样（fail open）。
判定调用记入 usage（purpose `auto-prewalk`），不写 transcript。

### D7. 配置面与启动参数

`hermes_cli/config_defaults_consumers.py:27` 与 `cli_config_load.py:204` 同时提供：

```yaml
prewalk:
  enabled: false      # 新会话自动武装
  from: "@default"    # /prewalk restart 的返回目标
  into: "@smol"       # 交接目标
  auto: "off"         # off | suggest | on
  auto_target: "@smol"
```

- 没有 `prewalk.*` 的旧 `config.yaml` 全部按上述默认值工作（保守：关闭）。
- **与 omp 对齐（待实现，D8/D12 P1）**：新增 `--prewalk`、`--no-prewalk`、`--prewalk-into <model-or-role>`
  启动参数。`--prewalk` 只影响启动会话；`--no-prewalk` 关闭启动会话的配置武装并让 auto 不进入，
  直到 `/new`；`--no-prewalk` 不能与 `--prewalk`/`--prewalk-into` 组合；`--prewalk-into` 隐含
  武装，只对启动会话生效。`/new` 始终按 `prewalk.enabled/into/from` 重新决定，不继承启动参数。
- **与 omp 对齐（待实现，D8/D12 P3）**：新增 `task.prewalk`、`task.agentPrewalk`、
  `task.agentPrewalkAuto` 与 agent frontmatter 的 `prewalk`；子代理的控制与父会话隔离，按
  spawn 时的 assignment 判定（native-only、0.70、4s），未配置等价 `off`。

### D8. 与 oh-my-pi 差异的对齐决策（逐项）

**决策：每一项差异都向 omp 收敛，不保留“Hermes 特有版本”。** 下表左列是 omp 目标行为，第三列是
当前实现，第四列是对齐动作与阶段（D12）。未完成的项以“当前实现”列为准，不声称已实现。

| 维度 | omp 目标行为 | 当前实现 | 对齐决策（阶段） |
|---|---|---|---|
| 默认 | 关闭 | 关闭（`enabled=false, auto=off`） | 保持 |
| 武装 | 配置、`--prewalk*`、`/prewalk`、auto | 配置、`/prewalk`、RPC、auto；无启动参数 | P1 增加 `--prewalk/--no-prewalk/--prewalk-into`；P2 增加 omp-ui `/prewalk` |
| 规划提示 | armed 注入 planning nudge；交接后注入 implementation checklist | 无提示词注入 | P1 按 ADR-20 请求级注入两段文本；不改系统提示、不写 transcript |
| todo 闸门 | todo 激活时任意成功 todo 调用开闸（含 `view`） | 同左 | 保持 |
| 触发 | 第一个 `edit`/`write` **结果**所在的 completed turn，不要求成功 | 要求成功 | P1 只看工具名与结果存在；`_succeeded` 只留给 todo |
| 一次性 | 是；consumed 持久化 | 同左 | 保持 |
| 切换内容 | active model + 可选 thinking | active model + effort | P1 同时应用/恢复 thinking；role 配置不变 |
| 相同路由 | 模型与有效 thinking 都相同 → disarm，不切换 | 仍切换并消费 | P1 规范化 `(provider, model, api_mode, effort)` 等价判定 |
| `/prewalk off` | 取消待定交接与进行中的切换；阻止 auto 到 `/new` | 清 armed + 抑制 auto | P1 增加取消进行中切换；抑制语义保持 |
| `/prewalk restart` | 已武装且目标不同 → 先拒绝，不改 active model | 先 restore 再重新武装 | P1 目标不同即拒绝 |
| 每任务恢复 | 自动交接属于发起任务；下个任务先回规划模型再判定；`auto=off` 也返回并丢弃未用武装 | 仅 `on/suggest` 执行 | P1 把清理/恢复移到 mode 门之前 |
| auto 判定 | native-only、0.70、4s；`suggest` 每任务一行报告 | 判定相同；无报告行 | P2 输出一行 `Auto-prewalk: …`（本地日志/状态行） |
| 子代理 | agent frontmatter、`task.prewalk`、`task.agentPrewalkAuto`，spawn 时判定 | 不移植 | P3 移植三个控制面；与父会话隔离，默认 `off` |
| 会话恢复 | handoff 记录在 session；resume/restore/revive 继续目标模型；retry fallback 覆盖它 | 状态快照在 `model_config.prewalk` | P2 对齐 revive/restore 与 fallback 记账 |
| `/new` | 按配置重新武装；除非被后续选择替换，恢复规划模型 | 按配置重新武装 | P2 增加“恢复 planning，除非已被替换” |
| 启动提示 | `Prewalk: armed for …`、`cancelled …` 等本地 notice | RPC 事件 + CLI 一行 | P2 增加对应本地 notice；不写 transcript |
| 客户端命令 | `/prewalk` 客户端斜杠入口 | 仅 CLI | P2 在 omp-ui 恢复命令，调用现有 RPC |

**对齐后的不变量**：默认关闭；切换只在 completed tool batch 边界；一次交接接受一次缓存重建；
不新增核心工具、不改系统提示（nudge 走请求级）、不跨 profile。

### D9. 失败矩阵

| 事件 | 行为 |
|---|---|
| 配置目标无法解析 | 不武装；`config_arm_failed`（debug）；turn 继续 |
| 显式 arm 目标非法 | `ValueError` → RPC `4002` / CLI 一行提示；状态不变 |
| 判定无 native judge | 不调用模型，`no_native_judge`；不武装 |
| 判定超时/异常/答案非法 | `judge_failed`；不武装；只 debug |
| `prewalk` 概率 < 0.70，或 route 为 `primary`/`direct` | 不武装 |
| auto_target 不可解析 | `target_unresolved`；不武装 |
| 显式武装已存在 | auto 不重定向（`explicit_arm`） |
| 已消费后 resume | `armed=false`，不第二次交接 |
| turn 被打断 | 本批不交接（`interrupted`） |
| 本批没有 `edit`/`write` 结果 | 不交接（`no_edit`），下一批继续 |
| `edit`/`write` 结果失败 | **仍交接**（与 omp 对齐）；目标模型接手修复 |
| 目标与当前路由等价 | disarm，不调用 `switch_model`（same-route） |
| `auto=off` 且上一任务有未消费 auto 武装 | 丢弃武装；已消费则先恢复规划模型（P1） |
| 启动参数目标不可解析 | 启动警告并保持未武装；不阻塞会话（P1） |
| 子代理 prewalk 目标不可用 | spawn 时不武装，记录一次警告（P3） |
| 目标在交接时不再可解析 | 保持武装（`target_unresolved`） |
| `switch_model` 抛异常 | 保持武装（`switch_failed`）；不失败 turn |
| 状态持久化失败 | debug；内存状态继续工作（本次会话） |
| 事件发布失败 | debug；不影响 turn |

### D10. 缓存与不变量

- **默认关闭**：一次交接至少重建一次 provider 缓存前缀；这是功能成本，不是缺陷。
- **只在 completed tool batch 边界切换**：同一批工具调用、同一段流式输出不跨模型；不在请求构造
  中途换模型。
- **不改系统提示/工具面**：planning nudge 与 implementation checklist 走请求级注入（ADR-20），
  不新增系统提示段落、不注册工具、不改 `_HERMES_CORE_TOOLS`、不写 transcript。
- **不改 role 配置**：切换只改会话 active model/effort，不写 `model.roles.*`。
- **profile 隔离**：状态在 session 行；RPC `@_profile_scoped`；路径/凭据解析走现有 profile 机制。

### D11. 验收（测试锁定）

| 文件 | 覆盖 |
|---|---|
| `tests/agent/test_prewalk.py` | 默认关闭；显式 arm 持久化；非法目标拒绝；disarm 只清 arm；todo 闸门；无 todo 时闸门恒开；**失败编辑也交接**；一次性；`patch` 触发；同批 todo+edit；批后行忽略；resume 消费；restart 回规划模型；**目标不同时 restart 拒绝**；打断不交接；切换失败保持武装；**same-route disarm 不切换**；auto native/超时/purpose；0.70 边界（0.70 武装、0.69 不）；`primary`/`direct` 不武装；suggest 只报告；显式武装不被重定向；判定失败不武装；目标不可解析；**`auto=off` 先恢复/丢弃再判定** |
| `tests/agent/test_prewalk_prompts.py`（新增） | planning nudge 只在 armed 请求出现；交接后移除并注入 checklist；不改系统提示/工具面/transcript（请求级注入，ADR-20） |
| `tests/agent/test_prewalk_subagents.py`（新增） | frontmatter/`task.prewalk`/`agentPrewalkAuto` 优先级；spawn 时判定；无 native/超时/低信号不武装；与父会话隔离；kept-alive 保持 spawn 决策 |
| `tests/hermes_cli/test_prewalk_flags.py`（新增） | `--prewalk`/`--no-prewalk`/`--prewalk-into` 互斥；只影响启动会话；`/new` 按配置重算；非法目标警告且不武装 |
| `tests/tui_gateway/test_prewalk_rpc.py` | 方法/事件声明；`from` 别名；未知参数拒绝；arm/status/off 事件；restart 返回规划模型；非法目标结构化错误 |
| `tests/hermes_cli/test_prewalk_slash.py` | `/prewalk` 本地面、状态行、arm/off/restart 参数；`suggest` 一行报告 |
| `omp-ui` 测试 | `/prewalk` 斜杠入口调用 RPC；设置变更实时生效；启动 notice 文案 |

测试用临时 `HERMES_HOME` 与 fake judge；不得写真实 profile。运行入口是
`scripts/run_tests.sh`。

### D12. 对齐实施顺序

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| **P1** | 触发改为成功无关；same-route disarm；restart 目标冲突拒绝；`auto=off` 的清理/恢复前移；请求级 planning nudge + checklist；`--prewalk*` 启动参数 | D11 的 `test_prewalk.py`/`test_prewalk_prompts.py`/`test_prewalk_flags.py` 全绿；既有 RPC/CLI 契约不变 |
| **P2** | `suggest` 一行报告；本地 notice；`/new` 恢复 planning（除非被替换）；resume/restore/revive/fallback 记账；omp-ui `/prewalk` 命令 | 六项各有行为测试；事件/命令契约生成通过 |
| **P3** | 子代理 prewalk：agent frontmatter、`task.prewalk`、`task.agentPrewalkAuto`；spawn 时判定与 kept-alive 决策固化 | 子代理测试覆盖优先级/隔离/fail-open；父会话状态不受影响 |

未完成阶段保持“当前实现”行为；ADR-12 D5 不再另立契约。

---

## 后果

**正面**

- 交接是会话状态，resume/revive 行为可预测；不用靠模型“记得”换模型。
- 默认关闭 + 边界切换，缓存代价有界且可解释。
- 判定/切换/持久化全部 fail-soft；任何失败都退化为“什么都没发生”。
- auto 与显式武装的优先级明确，显式用户意图不会被分类器改写。

**负面 / 代价**

- 一次交接必然重建缓存前缀；高频 `prewalk` 任务可能净亏。
- **D8 对齐项尚未全部实现**：成功无关触发、same-route、restart 冲突拒绝、`auto=off` 清理/恢复、
  请求级 nudge/checklist、启动参数、子代理 prewalk、客户端命令与本地 notice 仍按 D12 分阶段落地；
  在此之前行为以 D8“当前实现”列为准。
- 请求级 planning nudge 会增加每个 armed 请求的少量 token；它不进系统提示，但仍是缓存前缀之外的
  附加内容。
- 子代理 prewalk（P3）会在 spawn 时增加一次判定调用；批量 spawn 需要 batch 化以控制延迟。
- 本地 notice 与 `suggest` 报告只存在于本地会话，消息平台/headless 不可见（与 omp 的本地 TUI 语义
  一致）。

**中性**

- `/prewalk` 的 CLI 文案与 omp 不同（一行状态 + todo gate），RPC 字段用 `from` 别名；D8 只对齐
  行为，不要求逐字文案。
- omp 的启动参数按 P1 补齐后，`prewalk.enabled=true` 仍是持久化配置的等价入口。

## 未采纳的替代方案

| 方案 | 否决理由 |
|---|---|
| 用系统提示/prompt 指令让模型自行换模型 | 无法保证一次性；改系统提示破坏缓存；模型不能调用 `switch_model` 之外的可信路径 |
| 在 turn 中间/单次工具调用后切换 | 同一请求跨 provider；流式输出、计费、transcript 归属含混 |
| 用文本 judge 做 auto-prewalk | 无校准概率，0.70 阈值没有意义；ADR-17 已限定 native-only |
| 把状态写进新的 session 列/独立表 | `model_config` 已是会话级 JSON 扩展点；新列需要迁移且没有查询需求 |
| 每次 auto 任务都重新武装（不恢复规划模型） | `primary` 任务会被留在交接目标上，模型能力静默降级 |
| 保留“成功编辑才触发”的 Hermes 版本 | D8 已决定向 omp 对齐：失败草稿同样是实现开始，交给目标模型修复 |
| 不移植子代理 prewalk | D8 已决定对齐；多代理工作流需要与父会话隔离的 per-assignment 判定 |
| 不做 planning nudge / implementation checklist | D8 已决定对齐；用 ADR-20 请求级注入同时满足缓存不变量 |
| 保留 `auto=off` 不恢复/不清理陈旧武装 | D8 已决定对齐；否则关闭 auto 后模型与武装状态不可预测 |

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 缓存重建造成成本上升 | 默认关闭；只显式/判定为 prewalk 时发生；只在 batch 边界一次 |
| todo 工具被禁用导致永不交接 | 工具面不含 todo 时闸门恒开（D5） |
| native judge 不可用 | 不分类、不武装；显式 arm 仍可用 |
| 目标模型凭据失效 | arm 时解析；切换失败保持武装并 debug；`/prewalk restart` 可改目标 |
| resume 后重复交接 | `consumed` 持久化并强制 `armed=false` |
| auto 覆盖用户选择 | 显式武装不被重定向；`/prewalk off` 抑制到 `/new`；选择后消费状态仍记录原规划模型 |
| 状态行损坏/旧 profile | 读取失败按“未武装”处理；写入失败只影响本次会话 |
| 消息平台武装模型交接 | CLI 本地面检查 + RPC `@_profile_scoped` |

## 开放问题

D8 已关闭此前的产品决策（提示词、触发成功无关、same-route、`auto=off`、`suggest` 报告、子代理、
启动参数、客户端命令），以下只剩实现细节：

1. **nudge/checklist 文本**：逐字采用 omp 的 `prompts/prewalk*`，还是按 Hermes 工具名
   （`write_file`/`patch`、`todo_list`）本地化？决定：语义逐项一致，工具名用 Hermes 名称；需在
   P1 的请求级模板里锁定 golden 文本。
2. **子代理批量判定**：一次 `agent` 批量的多个 assignment 是否用 `judge_batch` 合并（每项一次
   `prewalk` 判定），避免 N 次串行 4s 超时？P3 实现时按 batch 并发上限 4 处理。
3. **启动参数的解析时机**：`--prewalk-into @default` 应在 `--model` 覆盖之前解析 `@default`
   （与 omp 一致）；P1 用启动参数快照，不写回 config。
4. **客户端命令文案**：omp-ui 的 `/prewalk` 是否需要与 omp 的 notice 文案逐字一致？D8 只要求行为
   对齐；文案按 Hermes 现有本地化风格。
