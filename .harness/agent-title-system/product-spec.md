---
epic: agent-title-system
project_type: api
created_at: 2026-11-17T16:31:52Z
user_approved: true
---

# Product Spec: agent-title-system

## Why

今天的模型级命名只有一个单点槽位，而仓库里五个命名模板没有任何消费者，导致会话标题、子代理任务标签与子代理产物文件名各走一套逻辑、彼此漂移。命名因此无法按角色顺序回退，也无法在模型不可用时保持稳定、可寻址的名字；不修，每新增一个命名入口都会再复制一套协议与路由。

## What

- 命名收敛为一个统一服务，由可切换的生成器驱动：默认走主回复副回合，失败后回落与 omp 同构的候选计划（角色 tiny → commit → smol、各自配置的重试链、当前会话模型，显式配置覆盖排最前，路由去重、每个候选只试一次，计划为空则只用确定性标题）。
- 所有模型级命名共用唯一的 `<title>` 标记协议：`<title/>` 与 `none` 表示"无可命名任务"并立即终止本次命名；超长或带句子的答案型输出被拒绝而不是截断；忽略标记的散文按首行接受；多行只取首个非空行。
- 会话标题在回合开始时立刻有确定性标题，模型级标题随后在后台升级并可携带卡片（`<icon> <CODE>: 标题`）；重生成时新标题没有卡片则沿用当前卡片；图标配置为简洁模式时永不出现卡片；用户手工标题永不被覆盖。
- 具名子代理在创建后异步获得一个 write-once 的任务标签，既作为界面显示名，也作为自由文本寻址的别名；同一标签命中多个存活 run 时返回候选列表而不是猜第一个；标签缺失时回退到 id/name/subject；标签不得回声 spawn handle，且有词数与长度上限。
- 子代理的全部产物（jsonl、报告、AST、结构化数据、目录）共享一个由 assignment 确定性派生的 stage-0 topic；Markdown 标题行、围栏块与注释被跳过，中文/emoji 标题得到稳定的 digest 名，碰撞加后缀；产物创建后不再因任何标题变化而重命名。
- 工具卡标题保持即时、display-only、无模型调用，由确定性派生决定；`execute_code` 的逐 cell 标题与既有行为一致，命名路由的变化不影响卡片标题。
- 模型级命名永不阻塞回合开始、工具卡发出或子代理 spawn；两个门控开关关闭时只停用模型级命名而保留工具卡标题与产物文件名；模型级失败时用户只看到确定性标题或没有标签，不会看到错误。

## Out of Scope

- 不移植 omp 的 `/rename`：手工标题仍是纯用户写入，不带卡片、无 revision、不触发模型调用。
- 不移植 Nerd Font 图标（本仓库无 glyph 目录），`nf` 与图标属性一律忽略；不新增会话卡片的 payload 字段或渲染，卡片只存在于标题字符串中。
- 不引入 ONNX/MLX tiny worker，也不引入模型下载器。
- `title-marker-instruction.md` 保持仅存档、不接线，只为与 omp 对账而保留。
- job/widget 标题、job caption、kanban/cron 的命名接入归 ADR-23；现有 kanban worker 卡片标题行为不变。
- 不改 ADR-17 的 title-drift judge 阈值与窗口，也不改 `derived < llm < user` 的权威顺序。
- JSON schema 标题协议、`_title_from_reasoning`、示例 echo 守卫整体退役，不保留兼容旁路；`retitle-skills` 走同一标记协议。
- 不为工具卡标题或子代理文件名引入模型调用；子代理产物创建后不重命名、不移动。
- 不在候选之间并发、比较或取更优者，也不对同一候选做二次重试。
- 除 `generator` 与 `card_icons` 外不新增配置键。
- ADR-21 开放问题 1（把 `auxiliary.title_generation` 的 route 字段迁走）不做：本 epic 不删除该槽的模型字段、不提供配置迁移。

## Constraints

- 必须遵守 ADR-19/ADR-20 的提示词 manifest 契约：新命名提示词以 `scope=request` 注册，渲染时 `StrictUndefined`，且每个提示词必须有条件入口（`agent/prompts/AGENTS.md` 规则 1）。
- 必须维持逐会话的提示词缓存不变量：命名结果永不进入 transcript、工具结果或系统提示；副回合只读既有缓存前缀，不推进主会话 leaf。
- 必须保持既有标题权威顺序 `derived < llm < user` 与 `user` 标题不可覆盖的语义（ADR-17 P5、现有会话标题行为）。
- 子代理文件名是句柄而非展示物：按 ADR-16 D4，产物身份在创建时固化，创建后不得重命名。
- 必须复用现有辅助模型角色槽位与重试链配置（`auxiliary.tiny`/`commit_message`/`smol`、`auxiliary.fallback_chains` 与 per-task `fallback_chain`），不得新造一套解析。
- 测试必须使用临时 `HERMES_HOME` 并以 pytest 运行；每个 sprint 的验收由 evaluator 的 per-sprint pytest 脚本执行。

## Success Signals

- 既有标题、漂移与 prompt-surface 测试保持全绿，且新增契约测试覆盖 D14 五个阶段的每一条验收行。
- 回合开始、工具卡发出、子代理 spawn 均无额外可观测延迟：模型级命名严格在后台，测试可证明三条路径不被命名阻塞。
- 每个注册的命名提示词在 `StrictUndefined` 下、`title_language` 为空与非空两种变体都能渲染。
- 五个命名模板与 omp 对账完成：`title-system.md` 除语言行外逐字节一致，其余模板经 Jinja 转换后行为等价，且不再存在无消费者的命名模板。
- 命名输出在 transcript、工具结果与系统提示中均不可见，缓存不变量由测试断言。
- 无角色 pin 时标题生成只调用当前会话模型；pin 三个角色时按 tiny → commit → smol → current 顺序调用且路由去重。
