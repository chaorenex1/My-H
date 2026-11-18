# ADR-23：job/widget 标题与 job 说明行（caption）

**状态**：提议（2026-11-17，首版）。**范围**：`agent/prompts/system/job-widget-system.md`、
新增 `agent/prompts/system/job-caption-system.md` / `job-caption-user.md`、
`agent/prompts/system/manifest.json`、网关进程事件与 omp-ui Jobs 面板/瓦片。

**关联**：ADR-21（命名服务的 `<title>` 协议、候选路由、门控与守卫；本 ADR 只消费它们，
不新增第二套协议）、ADR-4（工具卡/omp-ui 事件形态）、ADR-22（job 与子代理事件模板）、
`docs/omp-ui-panels-gateway-plan.md` §2.3.1 行 4（Jobs HUD 等 `process.list`/`process.*`）。

**取证口径**：omp 为 `C:\Users\zarag\projects\oh-my-pi` HEAD `1bdbf6d242`；
Hermes 为本仓库 2026-11-17 工作树。

**范围**：

- **做**：job 标题契约（留在 widget spec，单次生成）、job caption（独立一次请求）、
  两者的门控/失败/计费、网关 `process.*` 载荷与 omp-ui 消费点。
- **不做**：改 widget 的 rules/blocks schema、重写 job 运行时、直接改造 kanban/cron
  调用点（只定义它们将来消费的字段）、给 job 标题引入第二个生成器、把 caption 并入
  widget spec。

---

## 问题

### 1. job 标题有契约、没有消费者

Hermes 的 `job-widget-system.md:8` 已经要求 spec 作者输出 `title`（≤48 字符，写“watched
的是什么”），示例也带 `title`（`:33`–`:42`）。但 Hermes 至今没有 widget runtime：只有模板与
`auxiliary.widget` 槽，而 `hermes_cli/aux_slots.py:42` 明写 “omp job-widget role · no built-in
workload yet”。结果是标题有提示词契约、无生产者、无渲染面。

### 2. caption 完全缺失

omp 把“这个后台命令在做什么、何时结束”做成独立的一次请求（
`packages/coding-agent/src/prompts/system/job-caption-system.md`，≤12 词；只在命令超过
60 字符或含控制流时请求；阈值见 `job-widgets/caption.ts:28`，谓词是 `:42` 的
`needsJobCaption`）。Hermes 没有 `job-caption-*` 模板，也没有对应字段：Jobs 面板只能用
命令首行充当说明。这不是标题，不能用 `title` 顶替。

### 3. 两个消费者都没有 Hermes 载荷

omp 的 jobs sheet 用灰色一行画 caption（`packages/tui/src/overlays/jobs-panel.ts:490`），
瓦片标题按 `task → 代理名；其余 → widget title → 命令首行 → id` 取值
（`packages/tui/src/tools/job-widget.ts:471`）。Hermes 的 `process.list` 只有进程统计，
`process.*` 事件仍在 P4 待补（`docs/omp-ui-panels-gateway-plan.md:215`），Jobs 行标记“待接”。

---

## 决策

### D1. 单一所有者：标题留在 widget spec，caption 独立

- **标题仍由 widget spec 的 `respond.title` 生成**：一次 spec 请求同时给出图表与标题，
  不额外发一次模型调用，也不新增 `job-title.md`。omp 同样把 title 放在
  `job-widget-system.md` 的 `respond` 里，不存在独立的 job 标题提示词。
- **caption 是独立 workload**：单独一次请求、单独的 `job_caption` oneshot，
  绝不并入 spec（caption 不需要输出样本，只需要 command/intent/timeout 的语义不同）。
- 命名服务（ADR-21）不生成 job 标题；本 ADR 只把 title 作为 spec 的**固定契约**，
  避免同一字符串出现两个生成器。

### D2. 标题契约（收敛现状，不改生成路径）

- ≤48 字符，写“watched 的是什么”（吞吐/延迟/通过数/进度），不是命令复述；subagent job
  同样适用。
- 一次 job 只命名一次；**spec 重写（re-authoring）沿用已有标题**，不因输出变化重命名，
  避免看板/瓦片漂移。
- `{"skip":"<why>"}` 时不产出标题；消费端回退命令首行，不落空串。
- 标题不写进 transcript、工具结果或系统提示；仅 display metadata。

### D3. caption 契约

- 一行 **≤12 词、≤200 字符**，纯文本；写“做什么/何时结束”，不复述启动时的 purpose，
  不提 shell 名；无引号、无句末标点、无列表符。
- **只在需要解释时请求**：命令长度 >60 字符，或含 `;`、`|`、`&`、`$(...)`、反引号、
  多行控制流（与 omp 同一谓词：`caption.ts:28` 的 60 字符阈值、`:42` 的 `needsJobCaption`）。
  短命令直接用命令文本，不付费。
- 输入仅 `command`、`intent`、`timeout`；**不含输出、不含 secrets**；
  沿用 `agent/redact.py` 的 `redact_sensitive_text()`。
- **输入预算与 omp 相同**：`intent` ≤300 字符、`command` ≤4000 字符（`caption.ts:30`–`:31`）；
  回复 ≤120 tokens、≤200 字符（`:33`–`:34`）。
- **subagent job 也要 caption**：不复用 `job_caption`（`job-caption-system.md` 只描述 shell
  命令），改用 ADR-21 D7 的任务标签（≤9 词、来自 assignment、write-once）作为该 job 的
  caption；标签未生成或失败时回退 assignment 首行。不为此多发一次模型调用。
- 每个 job 至多一次（首次展示后异步请求）；晚到允许，失败留空，不重试、不替换标题。

### D4. 模板与 manifest

| 文件 | 处置 | 绑定 |
|---|---|---|
| `job-caption-system.md` | **新增**：从 omp 语义逐字移植（Handlebars → Jinja，其余文本不变） | `job_caption`，`scope: "request"`，无变量 |
| `job-caption-user.md` | **新增**：`<job>` 块，可选 `intent`/`timeout_sec` + `command`；Jinja `{% if %}`，`StrictUndefined` | 同上，`context_keys: ["intent","command","timeout_sec"]` |
| `job-widget-system.md` | **不改生成路径**；把 D2 的“一次命名、重写沿用、回退命令首行”写进契约说明 | `job` 标题，仍在 `respond.title` |

caption 不接入系统提示（缓存不变量不变）；只在 jobs 消费者请求时按 request 级渲染。

### D5. 路由、门控与计费

- 两个 workload 都走 **`auxiliary.widget`**（Hermes 自有槽位，不是 smol 别名）：
  按该槽的 `fallback_chain` 顺序逐候选尝试一次，`fallback=False`（链已展开，同 ADR-21 D3）。
- **开关与 omp `async.widgets` 同义**：`auxiliary.widget.enabled`（默认 true）。false →
  不写 spec、不生成 caption，Jobs 只显示命令文本。`provider: auto` 按主模型解析；
  要控成本就把该槽显式 pin 到便宜模型。
- 成本记在 `widget` task：spec 一次、caption 至多一次；不因输出更新而追加调用。
- 超时取 `auxiliary.widget.timeout`（默认 120s，见 `config_defaults_auxiliary.py:144`）。

### D6. 触发、线程与可观测

- caption 触发与 omp 同义：job 首次在 Jobs 面展示且达到最小 age 后请求一次（没有消费面就不请求）；
  后台线程执行为 daemon，不阻塞 spawn、回合开始或 `process.list`。
- 同一 job 的 caption 请求单飞（job id 去重）；spec 重写不重置 `captionAsked`。
- 日志：`job-caption: job=<id> outcome=ok|skip|error`；不记录命令正文、不新增出站遥测。
- 标题不单独记日志（它随 spec 请求成功/失败）。

### D7. 载体与消费

- 字段扩展锚定既有计划：`docs/omp-ui-panels-gateway-plan.md` §4.7 的 `process.list`
  字段行与 §6 的 `process.*` 事件（P4）。只加可选 `title`/`caption`（字符串，缺省=未生成），
  **不新增方法名或事件名**；与 session 事件分离。
- omp-ui Jobs sheet / job 树 / HUD 瓦片按 omp 语义消费：
  - 瓦片标题：subagent → 代理名；其余 → `widget.title` → 命令首行 → id；
  - caption 作为命令下方的灰色一行（`jobs-panel.ts:490` 同款）；
  - 缺字段时保持现状（不占位、不伪造）。
- **kanban/cron 复用同一 `title`/`caption` 字段**：未来接入时只读这两个字段与上述消费契约，
  不新增提示词、不新增事件；“从一行创建卡片并命名”也不另起生成器。
- P3 之前只落契约，不加空字段。

### D8. 失败矩阵

| 事件 | 结果 |
|---|---|
| `auxiliary.widget.enabled=false` | 不写 spec、不生成 caption；标题回退命令首行 |
| spec `{"skip":…}` | 无标题；caption 仍可独立生成（它不依赖图表） |
| 标题为空/超 48 字符 | 消费端回退命令首行；不重命名 |
| spec 重写 | 沿用原标题；图表规则可换 |
| caption 命令不需要解释 | 不请求；Jobs 显示命令文本 |
| subagent 标签未生成/失败 | caption 回退 assignment 首行；不额外请求 |
| caption 候选全部失败 | 留空；不重试、不影响标题/图表 |
| caption 晚于 job 结束 | 允许；文案仍显示，不再发起新请求 |
| 模型返回多行/带引号/句号 | 取首个非空行，去列表符/引号/句末句号（同 omp `normalizeJobCaption`） |

### D9. 与 omp 的差异（有意保留）

| 维度 | oh-my-pi | Hermes（本 ADR） |
|---|---|---|
| 标题生成 | widget spec 的 `respond.title` | 同左；不另立提示词/生成器 |
| 开关 | `async.widgets`（默认 true） | 同左：`auxiliary.widget.enabled`（默认 true） |
| caption 触发 | `async.widgets` + 60 字符/控制流判定 | 同左 |
| caption 路由 | `widget` 角色（smol 别名） | `auxiliary.widget`（不是 smol 别名）；按 `fallback_chain` 展开 |
| caption 预算 | intent 300 / command 4000 / 回复 120 tokens、200 字符 | 同左 |
| subagent caption | 无（只对 bash 命令） | 有：复用 ADR-21 任务标签，缺失回退 assignment 首行 |
| 重写行为 | 可带上一版 spec 重写 | 标题沿用；`captionAsked` 不重置 |
| kanban/cron | 无 job 标题接入 | 复用本 ADR 的 `title`/`caption` 字段，不新增提示词/事件 |

### D10. 分阶段与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | `job-caption-*` 模板；manifest `job_caption`；`job-widget-system.md` 契约说明 | manifest 拒绝未注册键；`StrictUndefined` 下缺 intent/timeout 可渲染；模板文本与 omp 差异仅 Jinja 语法 |
| P1 | caption 生成器：`needsJobCaption` 谓词、`normalizeJobCaption`、脱敏、单飞、omp 同等预算 | 短命令 0 请求；长命令/控制流 1 请求；多行/引号/句号归一；重复触发只 1 次；失败留空；intent/command 按 300/4000 截断 |
| P2 | spec 标题契约落地（校验/回退/重写沿用）；`auxiliary.widget.enabled` 门控 | ≤48 字符；skip 无标题；重写标题不变；关闭时 0 模型调用 |
| P3 | 网关 `process.*` 载荷 + omp-ui Jobs sheet/瓦片接线；subagent caption 取任务标签 | 事件携带 `title`/`caption`；subagent 显示标签、缺失回退 assignment 首行；缺失字段保持现状；不阻塞 `process.list`；无 N+1 请求 |
| P4 | 文档（`configuration.md` widget 槽、`docs/prompt-system.md` 请求级清单） | 文档不再说 job 标题由 widget 独立发明；caption 只出现在 Jobs 面 |

**测试落点**：`tests/agent/test_prompt_sections.py`（manifest/`StrictUndefined`）；
新 `tests/agent/test_job_caption.py`（谓词/归一/脱敏/单飞/门控/预算截断）；
`tests/agent/test_job_widget_title.py`（≤48、skip、重写沿用、回退）；
`tests/gateway/`（`process.list`/`process.*` 载荷）；`tests/tools/test_agent_message*.py`
覆盖的标签路径作为 subagent caption 来源；omp-ui 侧不新增本地生成逻辑。

### D11. 不做什么（复核清单）

- 不改 `job-widget-system.md` 的 rules/blocks schema 与示例结构；
- 不新增独立 `job-title.md`，不把标题移出 spec；
- 不把 caption 塞进 spec 或 widget 输出流；
- kanban/cron 接入只消费本 ADR 的 `title`/`caption` 字段，不新增提示词或事件；
- 除 `auxiliary.widget.enabled` 外不新增配置键；
- 不把标题/caption 写进 transcript、工具结果或系统提示；
- 不在候选之间并发/比较/二次重试；不在 spec 更新时重发命名请求。
