# ADR-8：Hermes 代码智能子系统（ACIE Code Graph Schema v1.0 落地）

**状态**：已决定（2026-11-02）。P1–P6 已实现（2026-11-03）。分阶段：**P1** `hermes_codegraph/` 包、Schema 注册、
Language Registry 骨架、Scanner、Python/TypeScript 提取器、Symbol Graph、Evidence、按文件增量
索引、`hermes code index/status/doctor`；**P2** Go/Rust、Call/Type/Dependency Graph、静态
`TESTS`、影响分析、查询 API 与 CLI、仓库级快照过滤、按需 grammar 安装；**P3** Test Graph 完整化
（覆盖率导入与 `COVERS`）、Change Graph（`PARENT_OF`/`CHANGED_IN`/`DELETED_IN`、跨文件符号
移动匹配）、Revision 变更查询；**P4** `agent/lsp` 语义增强、ast-grep 结构搜索、Semgrep 危险模式
诊断（`code_intel.semgrep.enabled`）与可选 CodeQL 诊断适配、默认开启的只读模型工具与内置
skill；**P5** 分析事实回写工具 `code_graph_update`（按文件
刷新、证据约束的补丁、乐观并发、补丁状态与审计）；**P6** 回合结束后的自动增量更新
（post-turn reconciler，后台单飞、只刷新变更文件）。**本 ADR 的 v1 不接入 Memory、Knowledge、Advisor，
也不替换 `hermes lsp`、`hermes graph` 或 `gitnexus-explorer`。**

**修订（2026-11-03）**：Language Registry 自动加载两个位置——包内内置
`hermes_codegraph/adapters/languages/languages.yaml` 先加载，profile 的
`code-intelligence/tree-sitter/languages.yaml` 后加载并按 `language` 去重（不重复加载内置语言）；
`languages` 列表按 `language` 区分；配置域统一为 `code_intel`（总开关 +
`semgrep.enabled`/`semgrep.mode` 子开关）；新增 D17 定义语法/语义/危险模式诊断的分层与 Semgrep
契约。

**校准（2026-11-03，对照当前工作树）**：`hermes_graph/` 的现状与本文的增量假设已逐项核对：

- `edges` 只有 `create()`（每次 `new_node_id()` 生成 UUIDv7），没有 `upsert`；关系表**没有主键**
  （`_create_edge_table` 只有 `id`/`created_at`/`props` 加声明列），只有 `GraphEdgeRef(id PRIMARY KEY)`
  给出 id 级唯一，所以 D2 的边幂等必须靠“派生 id + `MATCH (r {id}) SET`，否则 `CREATE`”，不是数据库
  约束；`Edge` 模型也没有 `key` 字段。
- `index.snapshot()` 的签名是 `(scope, node_types=None, edge_types=None, *, use_cache=True)`，没有
  `filters`；`index.nx_graph(scope)` 同样只接 scope；`algorithms.*` 只接 scope（`centrality` 另有
  `method`/`weight`），并以 `nx_graph(scope)` 取图。
- `NodeType`/`EdgeType.fields` 只允许 `STRING`/`INT64`/`DOUBLE`/`BOOL`；schema 注册已经是加法迁移
  （`ALTER TABLE ADD`），拒绝改 scope、改端点或删列；每个类型表只有 `id STRING PRIMARY KEY`，
  `key` 列不唯一、无索引。
- `derived_id()` 的 namespace 确为 `hermes_graph:v1:{type}:{key}`，`nodes.upsert()` 用它派生主键；
  `validate_key()` 拒绝空串，D2 的边复合 key 因此不能复用该校验。
- `GraphLockedError`：store 的文件锁分支带 `{path, remedy}`，client 的“已按只读打开”分支只带
  `{path}`；两者都不识别占用进程。`hermes graph` 已有 status/schema/node/edge/query/traverse/
  analyze/snapshot/backup/doctor；缺的只是 `graph schema migrate --destructive`（ADR-7 D4 要求的
  入口），所以 D12 的 P1 只做加法注册、不依赖该命令。
- `agent/lsp/`：`LSPService` 已有 `enabled_for()`、诊断生命周期（`get_diagnostics_sync`/
  `start_edit_check`/`get_status`/idle reaper）与 workspace trust，但**没有** definition/references/
  implementation/documentSymbol 查询；`LSPClient._CLIENT_CAPABILITIES` 声明 hover/definition/
  references/documentSymbol 等（**不含 implementation**），请求入口只有私有 `_send_request*`。P4 的窄
  API 确为新增，`query_implementation` 还必须补 `textDocument.implementation` 能力。`lsp.enabled`
  默认 `true`、`install_strategy` 默认 `auto`、`trusted_workspaces` 默认空；`UNTRUSTED_SAFE_SERVERS`
  含 `pyright`/`typescript` 等八项，不含 `gopls`/`rust-analyzer`。
- PyPI 复核（2026-11-03 实查）：`tree-sitter==0.26.0` 提供 cp310–cp314 的 win_amd64/win_arm64、
  macOS x86_64/arm64、manylinux（含 aarch64/x86_64/riscv64）与 musllinux **仅 x86_64**；无 Android、
  无 musl aarch64。`tree-sitter-python==0.25.0`、`tree-sitter-go==0.25.0`、`tree-sitter-rust==0.24.2`
  的 abi3 wheel 含 musllinux aarch64+x86_64；`tree-sitter-typescript==0.23.2` 的 musl 仅 x86_64。
  即 musl aarch64 的阻塞点是 core `tree-sitter` 本身，不是这三个 grammar。D8 的 `open()` 默认值
  统一为 `root=None`（缺省当前目录）。
- `pm`：`extras.ANCHORS["graph"]="ladybug"`（networkx 不是 anchor，算法在调用处
  `AlgorithmUnavailableError` 降级）；`pm.available(extra)` = `extra_supported(extra)`（读
  `[tool.hermes.extras-platforms]`；已安装 anchor 优先）+ 全部 anchor 可导入，**不做 wheel 探测**。
  `install_hint()` = `hermes pm install --extra <extra>`；`ensure_import()` 在 available 时 no-op，
  否则先过平台门，再（TTY 下）交互确认，最后 `pm.client.sync_venv([extra])` + `adopt_selected`，
  不能热切换时抛“installed; restart Hermes”。`[tool.hermes.extras-platforms]` 已有
  `graph = "sys_platform != 'android'"`，且 `tests/pm/test_extras.py` 的
  `test_declared_extra_gates_match_dependency_selection` 要求每个 gate 与依赖 marker 的选择结果
  一致——D14 必须同时写依赖 marker 和 gate 表项。
- musl aarch64 无法用 PEP 508 marker 表达（marker 环境没有 libc 变量），`pm.available` 也不探测
  wheel：正确行为是 `available=false`（anchor 缺失）+ `ensure_import` 在 uv 解析处失败。因此
  `status`/`doctor` 与 `language install` 先用 `pm.store.current_target()`（会返回
  `linux-arm64-musl`/`linux-*-bionic`）给出明确 gap，不把 resolver 错误当安装提示。
- Language Registry：包内 `languages.yaml` 只有四个内置语言；每个 descriptor 带 `extraction`
  块，`adapters/languages/extractor.py` 是唯一抽取器（没有 `_EXTRACTORS`/`_GRAMMAR_LOADERS`，
  grammar 由 `grammar_package` 动态导入）。`pyproject.toml`/`pm/extras.py` 已预留
  `codegraph-java`，但包内没有 Java descriptor，因此当前没有任何可 `language install` 的按需
  语言。profile 新增语言只要提供 descriptor、`extraction` 与查询文件并已安装 grammar wheel，就
  走同一抽取器；缺 `extraction` 时索引为 `unsupported`。
- 工具面：`tools/registry.py::_check_fn_cache` 的键是 `(check_fn, check_fn_cache_scope())`；单 profile
  为 `None`（进程级），多路 gateway 用路由 profile 的 home override，不是 `hermes_home_key()`；TTL
  30 秒、成功后 60 秒失败宽限。`model_tools.get_tool_definitions()` 的 memo 键包含 registry
  generation + config 版本 + profile scope，**不含 check_fn 结果**；
  `_refresh_mcp_tools_between_turns` 只在 `tools.mcp_tool` 已导入且存在 MCP 工具时刷新，且
  `preserve_prefix=True` 保留已发 schema，不是通用 toolset 重建。会话内新装 extras 只有经
  `reprobe_tool_availability()`（CLI、TUI RPC、gateway 的 `/reload-mcp` 都会调用）或重启才出现；
  配置翻转因 `config_version` 变化在下次 `get_tool_definitions()` 生效
  （`mcp.auto_reload_on_config_change` 开启时还会顺带触发一次 reload）。D10 与验收 17 按此改写。
- 回合终结：`finalize_turn` 只是通用循环的终结路径；`codex_app_server` 分支（
  `agent/codex_runtime.py::run_codex_app_server_turn` → `_turn_result`，在 `finalize_turn` 之前
  `return`）、脚本 prelude 的 `("return", result)`、`agent/turn_preflight.py` 的
  `_done("return", ...)`（compression deferred/exhausted）、`finish_text_response` 的
  runaway-repetition `return`、`recover_empty_response`/`_run_api_retry_loop` 的 early return 都
  不经过它。`run_conversation()` 是唯一出口（所有 envelope 都经过），P6 挂在这里；非顶层回合用
  现有谓词排除：`is_delegated_child_context()`；`agent._turn_origin` 覆盖
  `build_cache_parity_fork` 的 background review / side-question fork；curator 直接构造 `AIAgent`，
  只设 `_memory_write_origin="background_review"`、`_turn_origin` 为空，所以谓词还要包含
  `getattr(agent, "_memory_write_origin", "assistant_tool") != "assistant_tool"`
  （`agent_init` 的默认值是 `"assistant_tool"`；`agent/delegation_backends.py` 已用
  `_turn_origin`/`_delegate_depth` 做同类判定）。
- `hermes_cli/config_defaults.py::DEFAULT_CONFIG` 结构已核对（`lsp` 节含上述默认值）。
  当前工作树的 `codegraph` 配置节在本 ADR 中改为 `code_intel`（总开关 + `semgrep.enabled`），
  不保留 `codegraph` 配置别名；`codegraph` 仅保留为 extra/toolset/包名。
  `pm/code_intelligence_packages.py`、`tests/pm/test_code_intelligence_packages.py` 仍是未跟踪新文件；
  工作树同时改了 `pm/registry.py`（`_BUILTIN_MODULES` 收录该模块）、`pm/package.py`
  （`PythonToolPackage`、`has_artifacts`/`bundle`）、`pm/install.py`/`pm/operations.py`/`pm/cli.py`/
  `pm/update.py`/`pm/lock.json`、`scripts/bundles/native.py`（`bundle=False`/StatePackage 不进
  native payload）和 `website/docs/reference/package-management.md`，这些是 D14/P4 可选工具层的前提，
  必须与本文一并提交。

**关联**：ADR-7（Hermes 核心图子系统，本 ADR 的唯一存储与算法底座）；`hermes_graph/`
（`GraphClient`、`NodeType`/`EdgeType`、`derived_id`、事务、快照、scope 版本）；`agent/lsp/`
（`LSPService`、`SERVERS`、`UNTRUSTED_SAFE_SERVERS`、`trusted_workspaces`、
`install_strategy`、`_lsp_local_only` 语义）；`pm/code_intelligence_packages.py`（现有
ast-grep / tree-sitter / CodeQL / Semgrep 安装项）；`pm/extras.py`（`available`/
`ensure_import`/`install_hint`、tuple anchor）；`hermes_cli/subcommands/graph.py` 与
`hermes_cli/main.py`（core CLI 注册与 `_BUILTIN_SUBCOMMANDS`）；`tools/registry.py`、
`tools/AGENTS.md`（`check_fn` 与服务门控）；`hermes_cli/config_defaults.py`；`scripts/run_tests.sh`、
`tests/AGENTS.md`。

**外部依据**：`ACIE Code Graph Schema v1.0` 草案及其 Graph-centric Architecture（Agent Layer →
Code Intelligence API → Syntax/Semantic/Analysis/Search → Unified Code Graph）；PyPI 元数据
（2026-11-03 复查）：`tree-sitter==0.26.0` 提供 cp310–cp314 的 Windows `win_amd64`/`win_arm64`、
macOS x86_64/arm64、manylinux aarch64/x86_64 与 musllinux **仅 x86_64** 的 wheel；无 musl aarch64、
无 Android wheel。`tree-sitter-python==0.25.0`、`tree-sitter-typescript==0.23.2`、
`tree-sitter-go==0.25.0`、`tree-sitter-rust==0.24.2` 均提供 `abi3` 的 `win_amd64`/`win_arm64`、
macOS 与 musllinux wheel（TypeScript grammar 的 musl 只有 x86_64）；LadybugDB 与 NetworkX 的
约束见 ADR-7。

**范围**：Hermes core 内的代码图子系统：统一节点/关系 Schema、完整 Language Registry、多语言
抽取、证据与置信模型、六类代码关系图（Symbol/Call/Type/Dependency/Test/Change）、LSP 语义增强、
Semgrep 危险模式诊断、`code_intel` 配置域、按文件增量索引、
仓库级调用图/依赖/影响/测试/变更查询、Agent 上下文构建，以及它的 Python API、CLI、配置、依赖、
profile 生命周期和失败策略。

**本 ADR 不是**：一个新的图数据库或第二套图 Schema；一个独立仓库/独立进程/HTTP 或 MCP 服务；
`hermes lsp` 的替代品；`gitnexus-explorer` 的替代品；记忆/知识图的扩展。P4/P5 的
`code_intel`/`code_graph_update` 是默认工具面里仅有的代码图入口（一个只读、一个证据约束的写回；
extras 缺失时由 `check_fn` 过滤）。

---

## 问题

Hermes 目前能“看到”代码，但没有一份**持久的、可追溯的、跨语言的代码关系网络**：

| 需求 | 现状 | 缺口 |
|---|---|---|
| Symbol Graph | 模型靠 `read_file`/`search_files` 逐次扫描；`hermes lsp` 只在写文件后取诊断 | 没有跨会话的符号/定义/引用索引 |
| Call Graph | 没有持久 CALLS | 无法回答“谁调用它”“改它会影响谁” |
| Type Graph | 只能从文本猜继承/实现 | 无继承、实现、重写、类型注解关系 |
| Dependency Graph | `search_files` 找 import 文本 | 无模块/包级依赖与循环检测 |
| Test Graph | 靠文件名猜测；覆盖率数据没有入口 | 无静态 `TESTS`，也无法把真实覆盖写成 `COVERS` |
| Change Graph | 每次读磁盘都是“当前事实” | 无跨 Revision 的符号变更/删除/移动记录 |
| 语言适配 | 每种语言各自解析，AST 节点名外泄 | 没有完整的 Language Registry，无法按需启用 grammar |
| 证据 | 模型只能引用文件行号 | 无法区分语法发现、LSP 解析、静态推断、运行时覆盖 |
| 版本一致性 | 读磁盘即“当前事实” | 索引与工作树、Revision 不一致时没有显式标记 |

如果让每个消费者（Coding Agent、Reviewer、Advisor、自改进循环）各自解析代码并各自维护缓存，
会立刻产生四个分叉点：语言 AST 的差异、符号身份、关系置信度、索引版本。任何一个消费者把
“Tree-sitter 看到了一个同名调用”当成“已解析到目标函数”，都会做出错误的修改决策。

ACIE 草案给出的方向是对的：把节点、关系、证据、分析结果分开，把解析事实与推断分开，并让每条
关系都能追溯到文件、位置、工具和版本。本 ADR 把这套 Schema 落成 Hermes 的一个 core 子系统，
并明确采用六类关系图，而不是只做调用图。

同时有三条 Hermes 硬约束决定它的形态：

1. **窄腰**：每个模型工具都进入每次 API 请求。代码索引是重依赖（原生 parser wheel、可选 LSP
   进程），因此 `[codegraph]` 默认不装、不导入；模型面只放一个默认开启的只读 `code_intel`，
   extras 缺失时 `check_fn` 让它不出现。重依赖与工具 schema 都只在需要时付出。
2. **一个进程服务多个 profile**：索引数据必须落在活动 profile 的图数据库里；模块级单例或固定
   `~/.hermes` 路径都会造成跨 profile 泄漏。
3. **prompt cache**：索引与上下文构建不能改系统提示、不能重写历史消息；`build_context` 只能作为
   当前回合的追加内容或工具结果返回。`code_intel` 默认开启，若在会话中途因 extras 安装而出现，
   接受一次 tools 数组重预填；用单工具、append-only 刷新把影响限制在一个 schema。

## 决策

### D1. 形态：core 子系统 `hermes_codegraph`，复用 ADR-7 的图底座

代码智能是 **Hermes core 的子系统**：

- 代码在仓库根 `hermes_codegraph/`，Python API 为 `from hermes_codegraph import CodeIntelligence`；
- 用户面是 `hermes code ...` CLI；
- 存储、事务、Schema Registry、scope 版本和算法**全部复用 `hermes_graph`**（ADR-7），不新建
  `code.lbdb`、不引入第二套图 Schema；
- 语法解析用 Tree-sitter Python 绑定；语义增强复用 `agent/lsp`；结构搜索可选复用 PM 已管理的
  ast-grep；Semgrep/CodeQL 只作为可选诊断来源；
- 不创建独立插件、不进入 `plugin-catalog/`、不发布 HTTP/MCP 服务。

v1 的消费者：`hermes code` CLI、core 测试、P4 的只读模型工具 `code_intel`、P5 的
`code_graph_update`，以及未来的 Advisor/Reviewer/Coding Agent（不在本 ADR 实现，只冻结 Python
API）。明确不接入：
`agent/memory_*`、`agent/learning_graph.py`、`agent/adviser*`、任何 gateway hook。P1–P3 不注册
模型工具；P4 把 `code_intel` 加入默认工具面（D10），P5 加入 `code_graph_update`（D15）。

**与 ACIE 架构图的对应**：

| 图中层 | 本 ADR 的落点 |
|---|---|
| Agent Layer（Coding Agent / Reviewer / Advisor） | `CodeIntelligence` API；P4 的 `code_intel` 只读工具 |
| Code Intelligence API（Symbol/Call/Reference/Impact/Context） | D8 的查询信封与六类图查询 |
| Syntax Layer（Tree-sitter） | D6 的默认解析器；**始终可用**的基线 |
| Syntax Layer（ast-grep） | 可选结构搜索/校验；通过现有 PM 安装，**不参与图事实生成** |
| Semantic Layer（LSP） | 复用 `agent/lsp` 的 server 注册表、trust、安装与生命周期 |
| Semantic Layer（Language Adapters） | D6 的 descriptor schema + 唯一通用抽取器 |
| Analysis Layer（Semgrep / CodeQL 可选） | Semgrep 危险模式诊断受 `code_intel.semgrep.enabled` 控制；只写 `CodeDiagnostic`，不阻塞索引，不生成调用事实 |
| Search Layer（ripgrep / Symbol Search） | 文本搜索复用现有 `search_files`；符号搜索走图 API |
| Unified Code Graph（LadybugDB / NetworkX / SQLite） | LadybugDB = 唯一事实源；NetworkX = 只读临时快照；**SQLite 不采用为事实源**（D3） |

图上的 SQLite 行在本 ADR 中明确不采用：索引状态放进 `CodeFile` 节点，按 UUIDv5 主键读取；
不引入第三个存储、不做双写。若未来需要跨仓库索引调度队列，SQLite 只能是**派生/临时**队列，
不得持有图事实，且需要独立 ADR。

**不采用** `acie/` 作为顶层包：它是外部规范的产品名，不是 Hermes 的模块名；`hermes_codegraph`
明确归属、便于打包白名单和 profile 生命周期管理。

### D2. 对 ADR-7 的两处最小增量修订

代码图不需要新的存储引擎，但需要对 `hermes_graph` 做两项**加法**修订，默认行为不变：

1. **`edges.upsert()`**：现有 `edges.create()` 每次生成新的 UUIDv7，重复索引同一文件会产生重复边。
   新增
   ```python
   graph.edges.upsert(source_id, target_id, type, *, key: str | None = None,
                      properties=None) -> Edge
   ```
   边 ID = `UUIDv5(NAMESPACE, f"hermes_graph:v1:edge:{type}:{source_id}:{target_id}:{key or ''}")`，
   幂等由“派生 id + `MATCH (r {id}) SET`，否则 `CREATE`”实现；关系表本身**没有主键**
   （只有 `GraphEdgeRef(id PRIMARY KEY)`），所以不能像节点那样把唯一性交给 DDL 约束。与节点
   `upsert(type, key)` 同一语义：同一 `(type, source, target, key)`
   再写一次是更新，不是新增。这个复合 key 不经过 `hermes_graph.serialization.validate_key()`——
   后者拒绝空串，而 `key=None` 是“无分组”的合法边身份；只有显式传入的 `key` 才做非空校验。
   现有 `Edge` 模型没有 `key` 字段；P1 若要在结果里回显 key，需要加法扩展（或只保留派生 id）。
   `create()` 保持“每次新记录”的语义。
2. **仓库级快照过滤**：`index.snapshot(scope, node_types=None, edge_types=None, *, filters=None,
   use_cache=True)`、`index.nx_graph(scope, *, filters=None)` 与 `algorithms.*` 接受可选
   `filters: dict[str, str]`，只匹配已声明的类型化字段（代码图默认
   `filters={"repository_id": "repo:..."}`）。缓存键加入排序后的 filters。不带 filters 时行为与
   ADR-7 完全一致。（`algorithms.*` 当前只接 scope 并调用 `nx_graph(scope)`，所以过滤必须同时
   下沉到 `nx_graph`，不能只在算法层做。）

理由：`code` scope 是全局的（`NodeType.scope` 是静态字段，不能为每个仓库注册一套类型），而算法
必须按仓库隔离，否则多个仓库的调用图会混在一起。用过滤器而不是动态 scope，既保持 Schema
Registry 不变，也避免“一个仓库一次 DDL”。

`code` scope 的版本在每次索引事务提交时递增，因此**任何仓库的任何文件变化都会让所有仓库的快照
缓存失效**。v1 接受这个粗粒度：快照只用于算法，查询走图遍历；如果未来需要仓库级版本，先给
Schema Registry 增加 scope 粒度，再改本决策。

### D3. 六类关系图与物理 Schema

`hermes_graph` 的类型名在 profile 数据库内全局唯一；`Symbol`、`File`、`Package`、`CONTAINS`、
`DEPENDS_ON` 都是通用词，未来记忆/知识图很可能复用。代码图因此使用**物理名**
`Code*` / `CODE_*`，公共结果再映射回 ACIE 的 `Symbol` / `CALLS`。

#### 3.1 六类图

| 图 | 节点 | 关系 | 查询 |
|---|---|---|---|
| Symbol Graph | `CodeRepository`、`CodeRevision`、`CodeFile`、`CodeModule`、`CodeSymbol`、`CodeEvidence` | `CONTAINS`、`DEFINES`、`CONTAINS_SYMBOL`、`BELONGS_TO`、`REFERENCES`、`ALIASES`、`DECLARES` | 定义、引用、符号搜索、文件骨架 |
| Call Graph | `CodeSymbol` | `CALLS`、`INSTANTIATES`、`OVERRIDES` | callers、callees、call path、影响分析 |
| Type Graph | `CodeSymbol` | `INHERITS`、`IMPLEMENTS`、`OVERRIDES`、`HAS_TYPE`、`RETURNS_TYPE` | 类型层级、实现者、重写关系 |
| Dependency Graph | `CodeModule`、`CodePackage`、`CodeFile` | `IMPORTS`、`DEPENDS_ON`、`USES_PACKAGE`、`EXPORTS`、`REEXPORTS` | 模块依赖、外部包、循环依赖 |
| Test Graph | `CodeTestCase`、`CodeSymbol` | `TESTS`、`COVERS` | 静态测试关联、覆盖率、未覆盖符号 |
| Change Graph | `CodeRevision`、`CodeSymbol` | `PARENT_OF`、`CHANGED_IN`、`DELETED_IN` | Revision 差异、符号变更/删除/移动 |

六类图共享同一套节点、边、证据和 Revision 模型；不是六个独立数据库。

#### 3.2 节点

| ACIE 实体 | 物理节点类型 | 必需 | 说明 |
|---|---|---|---|
| Repository | `CodeRepository` | 是 | 仓库；key 见 D4 |
| Revision | `CodeRevision` | 是 | 索引过的 Git 快照；Change Graph 的锚点 |
| File | `CodeFile` | 是 | 文件；索引状态也在节点上 |
| Module | `CodeModule` | 是 | 模块/命名空间/包；依赖图的节点 |
| Symbol | `CodeSymbol` | 是 | 统一符号，`kind` 区分；六类图的核心 |
| Package | `CodePackage` | 否 | 外部依赖；版本未知时 `props.version=null` |
| TestCase | `CodeTestCase` | 否 | 测试用例 |
| AnalysisRun | `CodeAnalysisRun` | 是 | 每次索引/覆盖率导入/分析运行 |
| Diagnostic | `CodeDiagnostic` | 否 | 解析/分析诊断 |
| Evidence | `CodeEvidence` | 是 | 关系证据 |

**不新增 `IndexState` 节点，也不采用 SQLite 索引状态库**：ACIE 草案 §9 建议用 SQLite 存增量
索引元数据，但 ADR-7 已提供主键查找和事务；把 `content_hash`、`indexed_hash`、`index_status`、
`parse_status`、`parser_version`、`grammar_version`、`semantic_provider_version`、
`indexed_revision` 放进 `CodeFile`，避免第三个存储和第三套一致性规则。File 的 key 由“仓库 +
路径”派生，按 key 派生 UUIDv5 后是 O(1) 主键读，不需要二级索引。原图中的 SQLite 在本实现中
被 `CodeFile` + `CodeAnalysisRun` 取代；如果 P1 基准证明主键查找不够，再以独立 ADR 引入派生缓存。

侧写字段（`primary_languages`、`signature`、`visibility`、三态布尔、`evidence_ids` 等数组/对象）
放在 `props` JSON；**只有稳定存在、需要过滤/排序的字段才声明为类型化列**。`NodeType.fields` 只支持
`STRING`/`INT64`/`DOUBLE`/`BOOL`，三态布尔（未知 ≠ false）继续留在 `props`，避免把“未知”写成
`false`。

ADR-7 当前只保证主键索引，因此 `find_symbols(name=...)`/`qualified_name` 过滤是在单类型表内的
**有界扫描**：必须带 `repository_id` 与 `limit`，返回 `truncated`，不承诺亚毫秒。按 `symbol_id`
精确取符号走 UUIDv5 主键，是 O(1)。P2 记录扫描基准；若需要二级索引，先验证 Ladybug 支持，再按
ADR-7 的加法迁移引入。

#### 3.3 关系

| ACIE 关系 | 物理关系 | 起点 → 终点 | 所属图 |
|---|---|---|---|
| CONTAINS | `CODE_CONTAINS` | CodeRepository → CodeFile | Symbol |
| DEFINES | `CODE_DEFINES` | CodeFile → CodeSymbol | Symbol |
| CONTAINS_SYMBOL | `CODE_CONTAINS_SYMBOL` | CodeSymbol → CodeSymbol | Symbol |
| BELONGS_TO | `CODE_BELONGS_TO` | CodeSymbol → CodeModule | Symbol |
| IN_REVISION | `CODE_IN_REVISION` | CodeFile → CodeRevision | Symbol（v1 不写，见下） |
| REFERENCES | `CODE_REFERENCES` | CodeSymbol → CodeSymbol | Symbol |
| CALLS | `CODE_CALLS` | CodeSymbol → CodeSymbol | Call |
| READS / WRITES | `CODE_READS` / `CODE_WRITES` | CodeSymbol → CodeSymbol | Symbol |
| INHERITS / IMPLEMENTS / OVERRIDES | `CODE_INHERITS` / `CODE_IMPLEMENTS` / `CODE_OVERRIDES` | CodeSymbol → CodeSymbol | Type |
| INSTANTIATES | `CODE_INSTANTIATES` | CodeSymbol → CodeSymbol | Call |
| RETURNS_TYPE / HAS_TYPE | `CODE_RETURNS_TYPE` / `CODE_HAS_TYPE` | CodeSymbol → CodeSymbol | Type |
| IMPORTS | `CODE_IMPORTS` | CodeFile → CodeModule | Dependency |
| DEPENDS_ON | `CODE_DEPENDS_ON` | CodeModule → CodeModule | Dependency |
| USES_PACKAGE | `CODE_USES_PACKAGE` | CodeModule → CodePackage | Dependency |
| EXPORTS / REEXPORTS | `CODE_EXPORTS` / `CODE_REEXPORTS` | CodeModule → CodeSymbol | Dependency |
| TESTS | `CODE_TESTS` | CodeTestCase → CodeSymbol | Test |
| COVERS | `CODE_COVERS` | CodeTestCase → CodeSymbol | Test |
| GENERATED_FROM / ALIASES | `CODE_GENERATED_FROM` / `CODE_ALIASES` | CodeSymbol → CodeSymbol | Symbol |
| DECLARES | `CODE_DECLARES` | CodeFile → CodeModule | Symbol |
| PARENT_OF | `CODE_PARENT_OF` | CodeRevision → CodeRevision | Change |
| CHANGED_IN | `CODE_CHANGED_IN` | CodeSymbol → CodeRevision | Change |
| DELETED_IN | `CODE_DELETED_IN` | CodeSymbol → CodeRevision | Change |

`TESTS` 与 `COVERS` 必须分开：`TESTS` 是静态分析识别出的测试目标，`COVERS` 必须有测试运行或
覆盖率报告作为证据。`COVERS` 的起点必须是 `CodeTestCase`，所以覆盖率报告必须带 **per-test
context**（例如 `pytest-cov --cov-context=test` 或 `coverage.py` 的 contexts）；只有聚合行覆盖、
无法归因到具体测试用例的报告（普通 lcov/c8 聚合输出）**不产生 `COVERS`**，返回
`CoverageContextMissingError`，避免伪造 `TestCase → Symbol`。

`IN_REVISION` 在 v1 **只注册类型、不写入**：`CodeFile.indexed_revision` 已记录归属，写
`IN_REVISION` 会在每次 commit 累积一条新边；当前图只保留最新事实，历史由 Change Graph 表达。
`PARENT_OF`/`CHANGED_IN`/`DELETED_IN` 在 P3 写入，见 D7。

**未解析的调用/引用不写成边**：Ladybug 的关系表要求两端节点存在，ACIE 允许 `target_id=null`，
但物理上无法表达。v1 把未解析表达式保留为 `CodeEvidence`（`evidence_kind=call_expression` /
`reference_expression`，`props.target_qualified_name`），查询结果通过 `metadata.unresolved_count`
和证据列表暴露；解析成功后再补边。这样既遵守 ACIE“保留调用表达式证据”，又不伪造端点。

### D4. ID 与 Key：稳定身份、定位器、版本号分离

`hermes_graph` 的 `Node.id` 是存储身份（UUID），`Node.key` 是业务身份。代码图在两者之上再暴露
ACIE 的三种 ID：

| ACIE ID | 形态 | 用途 |
|---|---|---|
| `symbol_id` | `sym_<UUIDv7>`，首次识别时生成 | 跨文件引用、公共 API 的稳定身份 |
| `symbol_locator` | `{repository_id, file_path, qualified_name, kind, language}` | 搜索、匹配、恢复；**不是身份** |
| `symbol_version_id` | `f"{symbol_id}@{revision_id}@{content_hash}"` | 当前版本定位；Change Graph 的 `before_version_id` 也用它 |

节点 `key` 规则：

| 类型 | key | 说明 |
|---|---|---|
| CodeRepository | `git:<脱敏规范化 remote>`，无 remote 时 `path:<sha256(abs root)[:16]>` | 仓库移动且无 remote 会产生新身份，文档写明 |
| CodeRevision | `<repo_key>@<commit_hash>`；脏工作树为 `<repo_key>@<commit_hash>@dirty` | 干净/脏快照分开，脏索引不覆盖干净 Revision |
| CodeFile | `<repo_key>@<相对路径，POSIX 分隔符>` | 路径大小写按平台规范化；不把 revision 放进 key |
| CodeModule | `<repo_key>@<language>:<qualified_name>` | |
| CodeSymbol | `<symbol_id>` | 改名/移动先尝试身份匹配；失败才新建 |
| CodePackage | `<ecosystem>:<name>@<version 或 unknown>` | 同一生态多版本可共存 |
| CodeTestCase | `<repo_key>@<path>::<qualified_name>` | |
| CodeEvidence | `<repo_key>@<path>#<span>:<kind>:<tool>:<excerpt_hash>` | 同一位置同一工具重复索引不新增证据 |
| CodeAnalysisRun | `run_<UUIDv7>` | 每次索引/覆盖率导入一条 |
| CodeDiagnostic | `<repo_key>@<path>#<line>:<col>:<code>:<tool>` | |

`remote` 在进入 key/props 前必须脱敏：去掉 userinfo（`user:token@`）、query、fragment，host 小写，
去掉尾部 `/` 与 `.git`。`CodeRepository.props.remote_url` 只保存脱敏结果或省略；原始 remote 不写图、
不写日志。带凭据的 remote 如果脱敏后仍有歧义，退化为 `path:` 标识。

节点上的 `repository_id` 字段与查询信封使用同一个公共 ID：`repo:` + `sha256(repo_key)[:16]`。
它不包含 remote/path 原文，适合跨系统引用；过滤、日志和结果都用它，不用 `CodeRepository` 的
内部 UUID。

**符号身份匹配**（对应原文档 §8.3 第 5 条）：

1. 同一路径重新索引：`(kind, qualified_name, container_symbol)` 完全一致 → 复用 `symbol_id`；
2. 同一路径内改名但 `content_hash` 与 `signature` 一致、且同容器只剩一个候选 → 复用，记录
   `props.identity_method="content_hash"`；
3. 跨文件移动（旧符号消失、新符号出现）：仅当 `kind`、`language`、`signature`、`content_hash`
   全部一致，且候选唯一 → 复用 `symbol_id`，写 `CODE_CHANGED_IN`（`change_kind="moved"`），保留
   `before_file_id`/`after_file_id`；
4. 其余情况 → 新建 `symbol_id`；如果存在多个相似候选，写 `CodeDiagnostic` 记录候选，**不**建立
   `ALIASES` 边，也不错误合并。

**不做**基于 AST 相似度、调用关系相似度的自动合并：那需要历史版本和更贵的分析，且错误合并会
污染所有消费者。宁可多一个符号，也不要两个不同实体共享身份。

边 key = `(type, source_id, target_id)`，通过 `edges.upsert` 派生稳定 ID。同一对符号之间的多次
调用/引用归一化为一条边，多个调用点进入 `props.evidence_ids`。边 key 不包含证据，否则每次新增
证据都会产生新边。

公共 API 以 `symbol_id` 为规范标识；`node_id`（UUID）只作为内部/调试字段返回。`get_symbol`
接受 `symbol_id`，内部必须调用 `hermes_graph.serialization.derived_id("CodeSymbol", symbol_id)`
（与 `nodes.upsert` 使用的是同一个函数，namespace 为 `hermes_graph:v1:{type}:{key}`）后走主键。
**禁止手写 `uuid5(..., f"CodeSymbol:{symbol_id}")` 之类的另一套 namespace**，否则永远查不到。
如果未来 `derived_id` 不再是稳定内部缝，P1 把它提升为 `nodes.get_by_key(type, key)`；代码图不复制
派生逻辑。

### D5. 证据与置信：独立节点、状态可解释、`verified` 只给运行时覆盖

Evidence 是节点，不是边上的几个普通属性。每条关系边在 `props.evidence_ids` 中保存证据节点 ID
（JSON 数组），证据节点自身记录：

```
evidence_kind        call_expression | reference_expression | definition | import |
                     inheritance | type_reference | test_annotation | coverage |
                     lsp_definition | lsp_reference | diagnostic
source_tool          tree-sitter | lsp:<server_id> | coverage.py-context |
                     pytest-cov-context | static_analysis
source_tool_version  工具/grammar/覆盖率格式版本
language             语言
file_id, start_line, end_line, start_column, end_column
excerpt_hash         sha256(源码片段)，不保存源码
analysis_run_id      产生它的运行
revision_id          该证据所属的 Revision；覆盖率/诊断必须与当前快照一致
observed_at           UTC
```

坐标统一为 **1-based 行、1-based 列**；Tree-sitter（0-based）与 LSP（0-based）在适配器边界转换。
证据 ID/位置的稳定性保证重复索引不会产生重复证据。

`resolution_status`：`unresolved` / `inferred` / `resolved` / `verified` / `ambiguous` /
`invalidated`。

- `resolved`：LSP 定义/引用解析，或语法解析 + 导入解析得到唯一目标；
- `inferred`：静态规则推断（导入别名、同文件作用域、语言约定），结果必须标注；
- `ambiguous`：多个候选且没有足够证据裁决，返回候选列表，**不得**当作唯一目标；
- `unresolved`：只有调用/引用表达式，没有边；
- `verified`：**只用于 `COVERS`**，由覆盖率报告（运行证据）支撑；静态 CALLS/REFERENCES 即使
  经 LSP 解析也停在 `resolved`。`verified` 不等于绝对正确，只表示有运行时证据；
- `invalidated`：证据冲突或被新证据取代，不参与任何策略。

`confidence` 只是排序提示，不是概率：

| status | 默认 confidence | `confidence_method` |
|---|---|---|
| resolved | 0.8 | `status_mapping` |
| inferred | 0.5 | `status_mapping` |
| ambiguous | 0.3 | `status_mapping` |
| unresolved | 0.0 | `status_mapping` |
| verified（仅 COVERS） | 1.0 | `runtime_coverage` |

这些是策略常量，不随语言变化；不同语言的解析器质量差异用 `resolution_method` 和
`source_tool_version` 表达，不用“看起来更精确”的数字掩盖。

Agent 使用策略：

| `confidence_policy` | 允许的 status | 用途 |
|---|---|---|
| `strict` | resolved + verified（仅 COVERS） | 影响分析/自动修改建议的默认 |
| `balanced` | strict + inferred（标注） | 普通查询默认 |
| `exploratory` | balanced + ambiguous（候选）+ unresolved（仅证据） | 探索/调试 |

`invalidated` 的证据不参与任何策略；已删除符号的边在 `include_deleted=false`（默认）时同样过滤。

### D6. Language Registry：一种 descriptor 与一个通用抽取器，四个内置，其余按需安装

所有语言共用**同一种** descriptor schema（实现为 `LanguageDescriptor`）与**一个**通用抽取器
（`adapters/languages/extractor.py`）：没有每语言 `.py` 抽取器，也没有 `_EXTRACTORS` 分派表。
descriptor 声明识别键、grammar、query 路径、`extraction` 语法档（节点表、字段名与模式开关）、
capabilities 与规则映射；抽取器运行 descriptor 的 `tree_sitter_queries` 定位候选节点，再用
`extraction` 把节点映射为 ACIE 实体，grammar 由 `grammar_package` 动态导入。新语言只增加
descriptor、其 `.scm` 查询与 `extraction` 块（以及已安装的 grammar wheel），不修改 Python 代码。

v1 实际读取的字段：`tree_sitter_queries`、`kind_map`、`module_naming`、`visibility_rules`
（Python 导出规则）、`test_detection`、`capabilities` 与 `extraction`
（`module_mode`、`grammar_loader`、`container_fields`、`container_kinds`、`method_parents`、
`value_defs`、`call_name_fields`、`instantiation_nodes`、`import_mode`、`import_nodes`、
`type_name_nodes`、`test_mode`、`export_mode`、`self_container_fields`、`language_kind_from`、
`owner_relations`、`structural_interfaces`）。`container_rules`、`signature_rules` 与
`import_resolution` 仍进入 schema 校验，但当前抽取器不读取：签名统一取
`header_before_body`，导入解析由 `import_mode` 完成。没有 `extraction` 块的 descriptor 可以出现在
`language list`，索引却只能按 `unsupported` 跳过，不产生猜测性事实；不存在“猜一个扩展名就套
Tree-sitter”的隐式回退。语言注册文件有**两个位置**，按顺序合并：

1. 包内 `hermes_codegraph/adapters/languages/languages.yaml`（内置 registry，始终加载）；
2. profile 的 `get_hermes_home()/code-intelligence/tree-sitter/languages.yaml`（扩展 registry，
   存在时加载）。

**profile 文件不重复加载内置 registry 已加载的语言**：按 `language` 去重；profile 中与内置
`language` 同名的条目被跳过（不覆盖、不重复注册），`status`/`doctor` 报告 skipped 数；profile
可补充新语言：descriptor、`extraction` 与相对 profile 根目录的 `.scm` 查询一起校验，grammar 由
`grammar_package` 动态导入，必须先安装对应 wheel。内置文件不会被复制到 profile，也不是
bootstrap 模板。两个文件使用同一结构（同一 descriptor schema）：

```yaml
schema_version: 1
languages:
  - language: python
    ...
  - language: typescript
    ...
```

`languages` 是一个**列表**：每种语言一个 item，item 内用 `language` 区分；第二种语言是第二个
list item，不是第二个文件。`languages[]` 的每个元素都是一个完整描述符，字段如下：

```yaml
language: python
aliases: [py]
extensions: [.py, .pyi]
filenames: []                 # 例如 Dockerfile
grammar_package: tree-sitter-python
grammar_version: 0.25.0
grammar_install: builtin      # builtin | on_demand:<extra>
parser: parsers/python/parser.so   # 可选；仅 tree-sitter CLI 用；代码图忽略、不 dlopen
semantic_provider: pyright    # agent/lsp 的 server_id；null = 无语义增强
tree_sitter_queries:          # 值相对各自 registry 根目录的 .scm 路径
  symbols: queries/python/symbols.scm
  imports: queries/python/imports.scm
  calls: queries/python/calls.scm
  types: queries/python/types.scm
  tests: queries/python/tests.scm
kind_map: {...}               # 可选；language_kind → ACIE kind
container_rules: {...}        # 可选；类/模块/命名空间嵌套
visibility_rules: {...}
signature_rules: {...}
module_naming: {...}
import_resolution: {...}
test_detection: {...}
extraction:                   # 通用抽取器的语法档；所有语言同一结构
  module_mode: stem           # stem | directory | rust
  grammar_loader: default     # default | typescript（TS/TSX 语法选择）
  container_fields: {class_definition: name}
  container_kinds: {class_definition: class}
  method_parents: {function_definition: [class]}
  value_defs: {assignment: python_assignment}
  call_name_fields: {attribute: attribute}
  import_mode: python         # python | typescript | go | rust
  import_nodes: [import_statement]
  type_name_nodes: [identifier]
  test_mode: python
  export_mode: underscore     # underscore | uppercase | rust | always
capabilities:                 # 缺省视为 false；内置 registry 显式声明全部键
  symbols: true
  references: true
  calls: true
  types: true
  inheritance: true
  tests: true
  change: true
parser_version: "..."         # grammar + adapter/parser 版本，参与增量失效
```

- 两个文件都只允许 `schema_version` 与 `languages`；`schema_version` 必须是 `1`；`languages`
  非空；单文件内重复的 `language` id/alias/extension、未知字段、缺必填字段或类型错误拒绝该文件；
- 合并顺序固定：内置 registry 先加载；profile 中 `language` 已存在时跳过该条目（不覆盖、不重复
  注册）；profile 的 alias/extension/filename 与已加载条目冲突时跳过并报告 conflict；profile
  新增语言仍使用同一 descriptor schema 并由同一通用抽取器处理；缺 `extraction` 块或 grammar
  未安装时，索引记 `unsupported`；
- `tree_sitter_queries` 的键是 `symbols/imports/calls/types/tests`，值必须是非空 `.scm` 路径，
  相对**各自 registry 根目录**，拒绝绝对路径与 `..`；引用的查询文件必须存在且非空；
- `parser` 可选，仅作为 tree-sitter CLI 的元数据；代码图运行时**忽略**该字段，不 `dlopen`、不
  校验其存在性（用户可编辑的 `.so` 不得成为代码执行入口）；
- `extraction` 可省略（省略则该语言索引为 `unsupported`）；出现时只接受固定键集合，节点表/字段
  名/列表/布尔值类型错误拒绝整个文件；profile 与内置 registry 使用同一校验；
- 规则映射（`kind_map`/`container_rules`/`visibility_rules`/`signature_rules`/`module_naming`/
  `import_resolution`/`test_detection`）可省略；省略时该语言不使用对应规则，不回退到通用规则；
- `capabilities` 缺键视为 `false`；内置 registry 必须显式声明全部键。

**内置四种**（`[codegraph]` extra 的内置 registry 固定携带）：

| 语言 | Grammar | 内置 LSP（`agent/lsp` server_id） | 测试识别 |
|---|---|---|---|
| Python | `tree-sitter-python==0.25.0` | `pyright` | pytest/unittest |
| TypeScript/TSX | `tree-sitter-typescript==0.23.2` | `typescript` | jest/vitest/node:test |
| Go | `tree-sitter-go==0.25.0` | `gopls` | `_test.go` |
| Rust | `tree-sitter-rust==0.24.2` | `rust-analyzer` | `#[test]` |

内置四种的抽取输出必须覆盖以下最小集合；这是 fixture/验收契约，不是 registry 加载器的运行时
校验：`load_registry_file(require_all_capabilities=True)` 只强制内置 descriptor 的必填字段与
`capabilities` 键齐全，`kind_map`/`container_rules`/`visibility_rules`/`signature_rules`/
`module_naming`/`import_resolution`/`test_detection` 都是可选字段，缺失不会拒绝文件，也不会让
索引失败或回退到通用规则：

| 语言 | 符号 kind | 导入 | 类型关系 | 测试 |
|---|---|---|---|---|
| Python | class/function/method/module/variable/constant/decorator | `import`/`from ... import` | 基类、类型注解 | pytest/unittest |
| TypeScript/TSX | class/interface/type_alias/enum/function/method/property | `import`/`export`/`reexport` | extends/implements/类型注解 | jest/vitest/node:test |
| Go | struct/interface/type_alias/func/method/field/constant | `import` | 嵌入字段、接口实现 | `_test.go`/`Test*` |
| Rust | struct/enum/trait/impl/fn/method/mod/const | `use` | impl Trait/trait bounds | `#[test]` |

表中的语言细节是抽取输出契约，由通用抽取器、descriptor 的 `extraction` 块与 `capabilities` 共同
保证，不是 descriptor 字段清单；具体 Tree-sitter query 在
各自 registry 的 `queries/`（内置包内 / profile 的 `tree-sitter/queries/`），AST 节点名留在
`extraction` 的节点表里，不进入公共 API。

**其他 grammar 按需安装**：

- 每个额外语言一个 `codegraph-<language>` extra，内含该语言的 grammar 与必要的 Python 绑定；
- `hermes code language install <language>` 只调用 `pm.ensure_import("codegraph-<language>")`，
  不执行裸 `pip`/`uv`，也不在运行时改 `sys.path`；进入安装前先用 `pm.store.current_target()`
  判定目标（`linux-arm64-musl`/`linux-*-bionic` 等已知缺口返回结构化 unavailable，不把 uv
  resolver 错误抛给用户）；
- 安装成功后，`code_intel.languages` 必须包含该语言 id 才会参与索引；`language list` 显示
  installed / enabled 两列，避免“装了 grammar 但没启用”的静默状态；
- 额外语言的 descriptor、`extraction` 与查询文件随包或 profile registry 提供：descriptor 用
  `grammar_install: on_demand:codegraph-<language>`，`language install` 据此找到 extra；grammar
  不进入 `[codegraph]` 基线，未安装时 `language list` 显示 `available=false` 与安装命令；只写
  descriptor 而缺 `extraction` 或 grammar 时，索引记 `unsupported`；
- 每个额外 extra 在 `pm/extras.py` 有独立 tuple anchor（如 `tree_sitter_java`），
  `pm.available("codegraph-java")` 可独立判断。

**LSP 按需安装**：

- 内置 LSP 是 `agent/lsp/servers.py` 的 `SERVERS` 注册表；它们的获取/安装沿用
  `agent/lsp/install.py` 与 `lsp.install_strategy`（`auto` 把二进制链接到
  `<HERMES_HOME>/lsp/bin/`，npm 配方装在 `<HERMES_HOME>/lsp/node_modules`）；
- 其他 LSP 通过 `lsp.servers` 自定义项或手动安装二进制接入，按需启动；代码图**不新增** LSP
  安装器、不新增 PM LSP 包；
- 语言描述符里的 `semantic_provider` 只是 `server_id` 引用；server 不可用时语义增强降级，
  语法索引继续。

**语法与语义的边界**：

- `--semantic` 是索引时显式 opt-in（默认关闭），因为 LSP 冷启动、项目装载和逐符号请求的成本
  远高于语法解析；
- `lsp.enabled` 仍按现有默认（true）工作；代码图只是**索引时**不主动调用 LSP，除非
  `code_intel.semantic: true`。已经因写文件而启动的 LSP 进程可以被复用，但代码图不负责其生命周期；
- 只在 host filesystem 后端尝试：等同于 `file_operations_lint._lsp_local_only` 的
  `env_uses_host_filesystem(env)` 判断；Docker/SSH/Modal/Daytona/限定性本地 sandbox 上不启动
  LSP，只跑纯语法索引；
- 只对 `LSPService.enabled_for(path)` 为真、且 workspace trust 允许的文件启用；trust 规则完全沿用
  `lsp.trusted_workspaces` 与 `UNTRUSTED_SAFE_SERVERS`，代码图**不得**扩大信任范围；
- 在 `agent/lsp` 上增加窄查询 API（返回普通 dict/list，不泄露 `LSPClient`）：
  `query_definition(path, line, col)`、`query_references(...)`、`query_implementation(...)`、
  `document_symbols(path)`、`workspace_symbols(query, limit)`；`LSPClient` 已声明
  `definition`/`references`/`documentSymbol`/`hover` 能力（**没有** `implementation`），请求入口目前
  只有私有 `_send_request*`；v1 不要求新增进程管理，但 `query_implementation` 必须补
  `textDocument.implementation` 能力。`LSPService` 现有公开面只有诊断生命周期（`enabled_for`/
  `get_diagnostics_sync`/`start_edit_check`/`get_status`），没有符号查询。全部带超时，失败返回空而不是异常。

**边升级以 occurrence（source symbol + 证据 span）为单位**：

1. LSP 目标与现有 `inferred` 目标相同：`edges.upsert` 命中同一 key，追加 LSP 证据，状态升为
   `resolved`；
2. LSP 目标与 `inferred` 目标不同：从旧边移除该 occurrence 的证据并标 `invalidated`；旧边若已
   无其它有效证据则删除；再 upsert LSP 边并标 `resolved`。**不得**让旧 `inferred` 边和新
   `resolved` 边同时活跃；
3. 多个候选目标：每个候选一条边，全部 `resolution_status="ambiguous"`，在 `props` 记录
   `candidate_count`/`candidate_rank`，共享同一 occurrence 证据；不为多个目标伪造一条单一边；
4. LSP 结果与推断冲突且无法裁决：相关边标 `ambiguous` 并保留双方证据，`balanced` 不返回，
   `exploratory` 返回候选；不按“LSP 一定对”静默覆盖。

**可选工具层**：

- ast-grep：通过现有 PM 安装，提供结构搜索/规则校验；它**不生成**符号/关系边，避免同一关系
  有两套提取器；D15 只接受它作为 `reference_expression`/诊断证据。未安装时
  `hermes code find --pattern` 报告 unavailable，符号查询不受影响；
- Semgrep/CodeQL：作为可选 `AnalysisAdapter` 把结果写成 `CodeDiagnostic`，不参与 Symbol/Call
  事实；每次导入的 `CodeAnalysisRun` 记录 `revision_id`/`snapshot_hash`，与当前快照不一致时返回
  `RevisionMismatchError` 并拒绝写入，不把旧提交的发现挂到当前符号；未安装时跳过；
- ripgrep/文本搜索：复用现有 `search_files`，代码图不新建全文索引。

### D7. 增量索引协议：按文件事务、八条规则、当前图 + Change Graph

本节对应 ACIE 草案 §8 的索引状态、文件更新流程与八条必须规则。

**索引状态**（写在 `CodeFile`，不依赖 SQLite）：

```
content_hash, indexed_hash, index_status, parse_status,
parser_version, grammar_version, semantic_provider_version,
indexed_revision, size_bytes, line_count
```

**扫描**：

- 仓库根 = `--root`，缺省用 `git rev-parse --show-toplevel`；不是 Git 仓库时用当前目录并明确标记；
- Git 仓库优先用 `git ls-files -co --exclude-standard -z`（跟踪文件 + 未忽略的未跟踪文件），
  子模块目录作为边界跳过；非 Git 仓库用受控遍历 + 内置排除；
- 内置排除：`.git/`、`.hg/`、`.svn/`、`node_modules/`、`.venv/`、`venv/`、`dist/`、`build/`、
  `vendor/`、`target/`、`.next/`、覆盖率目录；**强制排除 `.env`、`*.pem`、`*.key`、`id_rsa*`、
  `credentials*`**，不把密钥当代码索引；
- 上限（默认，可配置）：单文件 1 MiB、单仓库 20,000 文件、总源码 128 MiB、单仓库 1,000,000
  符号；超限明确报 `IndexLimitError`，已完成的文件保留，不假装完整；
- 二进制检测（NUL 字节）→ `parse_status="unsupported"`，只保留 File 元数据；
- 单文件解析超时（默认 10 秒）→ `parse_status="failed"` + `CodeDiagnostic`，不阻塞整个仓库；
  超时阈值是内部常量，不新增配置项。

**索引运行与可见性**：

- `index` 开始时创建 `CodeAnalysisRun(kind="index", status="running", target_revision=...)`；目标
  `CodeRevision` 不存在时创建为 `props.status="indexing"`、`snapshot_hash=null`，已存在的 Revision
  不改成 indexing（避免隐藏上一份完整事实）；
- 每文件/批次事务只提交图事实；查询看到 `index_status="indexing"`，`strict` 拒绝，`balanced` 标
  `partial`，**不返回完整 `snapshot_hash`**；只有所有批次成功后，最后一个事务才写
  `snapshot_hash`、`status="complete"`、给所有 `CodeFile`（含本次跳过的）盖 `indexed_revision`、
  写 `PARENT_OF`/`CHANGED_IN`/`DELETED_IN`，并把 `CodeAnalysisRun.status="completed"`；
- 失败/中断：run=`failed`，新 Revision 保持 `indexing/failed`，**不写任何变更边**；查询 `strict`
  拒绝，`balanced` 标 `partial`；下一次 index 复用目标 Revision 继续，成功后一次提交。旧 Revision
  的 `snapshot_hash` 在本次运行期间不代表当前图，查询不得把它当完整版本。

**每个文件**：

1. 计算 `content_hash`；若 `content_hash == CodeFile.indexed_hash`、`index_status == "complete"` 且
   `parser_version` / `grammar_version` / `semantic_provider_version` 一致 → **跳过解析**，只把
   该文件计入本次 snapshot，最终事务统一更新 `indexed_revision`。**不比较 `revision_id`**：
   跨 commit 内容未变的文件必须复用（§8“可以复用未变化的内容”）；`patched` 文件强制重解析，
   以便全量索引回收 D15 的补丁；`stale`/`partial`/`indexing` 同样不得跳过，因为它们不代表
   上一份完整事实；
2. 上次解析失败时 `indexed_hash` 仍是上一次成功的 hash，因此 `content_hash != indexed_hash`
   会触发重试；只有内容回到上次成功版本、版本号一致且 `index_status == "complete"` 时才跳过；
3. 解析在**事务外**完成（CPU 密集，不能持有写锁）；
4. 一个写事务内重建该文件的图：删除 **File 作为 source 的所有边**（`CODE_DEFINES`、
   `CODE_IMPORTS`、`CODE_DECLARES` 等）以及**该文件符号作为 source 的所有边**；对本次消失的
   符号，删除其双向关联边与证据；再 upsert File/Symbol/Module/TestCase/Diagnostic/Evidence，
   用 `edges.upsert` 写边。File-sourced 边不能漏删，否则被移除的符号/import 仍可达；
5. 文件批量提交：默认每 100 个文件或 5 秒提交一次，避免每文件一次 scope 版本递增；批次失败整体
   回滚，已提交批次保留；
6. 成功解析后更新 `CodeFile.content_hash=content_hash`、`indexed_hash=content_hash`、
   `parse_status="success"`、`index_status="complete"`；失败时更新 `content_hash`（当前内容）、
   `parse_status="failed"`、`index_status="stale"`，**不更新 `indexed_hash`**，保留上一份成功事实。

**八条规则与本实现的对应**：

| 规则 | 实现 |
|---|---|
| 1. 同一文件重复索引幂等 | `content_hash == indexed_hash` + `index_status == "complete"` + parser/grammar/provider 版本一致才跳过；`edges.upsert` + 稳定 symbol/evidence ID |
| 2. 文件删除清理定义和证据 | 写 `DELETED_IN`；删除 File 及其符号来源的边与证据；符号标 `symbol_status="deleted"` |
| 3. 解析失败不能静默覆盖上次成功索引 | 失败只更新 `content_hash`，保留 `indexed_hash` 与旧符号/关系；`parse_status="failed"`、`index_status="stale"`、写 Diagnostic |
| 4. 符号删除必须处理关联边 | 删除以该符号为 source 或 target 的边；写 `DELETED_IN`；保留符号节点供 Change Graph |
| 5. 符号移动需要尝试身份匹配 | D4 的唯一 `content_hash`+`signature` 匹配；失败新建并记录候选诊断 |
| 6. 依赖关系变更更新受影响模块 | 文件 import 集合变化时重算该文件的 `IMPORTS`/模块级 `DEPENDS_ON`/`USES_PACKAGE` |
| 7. 分析器版本变化允许重新分析 | `parser_version`/`grammar_version`/`semantic_provider_version` 不一致即重索引该语言文件 |
| 8. Revision 不一致的数据不得混合查询 | 当前图只存最新快照；`indexing`/`partial` 时 `strict` 拒绝；只有 `complete`/`patched` 才返回 `snapshot_hash`（`patched` 见 D15）；历史全图查询拒绝 |

**Revision 与 Change Graph**：

- 每个索引运行绑定一个目标 `CodeRevision`（干净 key = commit；脏 key = `commit@dirty`）；不存在时以
  `status="indexing"` 创建，成功后写 `snapshot_hash`（该 Revision 所有 `CodeFile.indexed_hash` 的
  排序哈希；有 D15 补丁时追加 `CodeRepository.props.patch_seq`）与 `status="complete"`；已存在的
  complete Revision 在重索引期间保留旧 `snapshot_hash`，直到最终事务更新。`props` 还记录
  `commit_hash`、`branch`、`is_dirty`、`indexed_at`；
- 新的干净 Revision 写 `CODE_PARENT_OF` 到**上一次干净索引的 Revision**（不一定是 Git 第一父提交；
  `props.git_parent` 保留 Git 父提交），支持分支切换后的变更比较；dirty Revision 不写父边；
- 符号的 content_hash/位置/名称变化写 `CODE_CHANGED_IN`（`change_kind=added|modified|moved`，
  `before_hash`/`after_hash`、`before_file_id`/`after_file_id`）；
- 符号消失写 `CODE_DELETED_IN`；旧 Revision 节点**保留**（Change Graph 需要）。`hermes code prune`
  默认保留当前 + 最近 50 个干净 Revision（`--keep N` 可调，`--older-than` 可叠加），当前脏
  Revision 只保留一个，旧 dirty Revision 在 prune 中删除；删除顺序：先删指向/来自被删 Revision 的
  `CHANGED_IN`/`DELETED_IN`/`PARENT_OF` 边及其证据，再删 Revision 节点；**不能**用“未被引用”判断，
  因为每个旧 Revision 都被 Change Graph 引用；
- **Change Graph 只对干净工作树（`is_dirty=false`）的 Revision 写入**：脏工作树索引只更新当前图
  与 `snapshot_hash`，不写 `CHANGED_IN`/`DELETED_IN`/`PARENT_OF`，避免同一 commit 下每次保存生成
  伪历史；下一次干净提交再与上一个干净 Revision 比较；
- `IN_REVISION` 仍不写：File 只有当前事实，`indexed_revision` 表示它属于哪个快照；
- **历史全图查询不支持**：请求非当前 Revision 的 Symbol/Call 图返回 `RevisionUnavailableError`，
  不拿当前图冒充历史。Change Graph 查询可以跨 Revision，但只返回变更边/差异，不返回当时完整图。

**覆盖率导入**：只接受带 per-test context 的报告（`coverage.py` contexts、pytest-cov context）；
`coverage import` 要求 `--revision`（或报告自带匹配元数据），并把 `revision_id`/`snapshot_hash`
写入 `CodeAnalysisRun` 与每条 `CodeEvidence`。报告 Revision 与当前 complete 快照不一致时返回
`RevisionMismatchError`，不写 `COVERS`；聚合报告（普通 lcov/c8、无 context 的 JSON）返回
`CoverageContextMissingError`，不写假边。覆盖率导入只影响 Test Graph，不改变 Symbol/Call 事实。

**失败**：

- 解析失败时保留上一份成功的符号与关系，`index_status="stale"`；不能静默清空；
- 某一文件失败不中止整个仓库索引；`status` 汇总失败文件；
- 索引进程被中断时，已提交文件有效，未提交文件保持旧状态；下一次索引按 hash 补齐；
- 失败/中断不得删除旧 `CodeRevision` 或写 `CHANGED_IN`/`DELETED_IN`；Change Graph 只反映完整
  提交的 Revision。

### D8. 公共查询 API：稳定信封、有界深度、仓库绑定、不暴露 Cypher

```python
from hermes_codegraph import CodeIntelligence

ci = CodeIntelligence.open(root=None)           # 缺省当前目录；解析 Git 根并绑定一个仓库

ci.index(semantic=False)
ci.status()
ci.languages()                                  # 内置 + 可安装语言及 available 状态
ci.install_language("java")                     # 走 pm.ensure_import("codegraph-java")
ci.import_coverage(path, revision=..., format="auto")  # 必须匹配当前 complete 快照

ci.find_symbols(name=..., kind=..., language=..., limit=20)
ci.get_symbol(symbol_id)
ci.get_definition(symbol_id)
ci.get_references(symbol_id, policy="balanced", limit=50)
ci.get_callers(symbol_id, depth=2, policy="strict")
ci.get_callees(symbol_id, depth=2, policy="balanced")
ci.get_call_path(source_id, target_id, max_depth=5)
ci.get_type_hierarchy(symbol_id, direction="both", depth=3)
ci.get_implementations(symbol_id)
ci.get_dependencies(module_id, depth=2)
ci.get_cycles(scope="modules")                   # modules | types
ci.get_impact(symbol_id, depth=3, include=("callers", "references", "dependencies",
                                           "implementations", "tests"), policy="strict")
ci.get_tests_for_symbol(symbol_id)
ci.get_coverage(symbol_id)                      # COVERS；无覆盖率时为空并标注
ci.get_diagnostics(path=None, rule=None, severity=None, limit=50)  # Semgrep/解析诊断
ci.get_changes(symbol_id, from_revision=None, to_revision=None)
ci.get_revision_changes(revision_id)
ci.build_context(target=symbol_id, budget_tokens=6000)
```

**仓库绑定**：一个 `CodeIntelligence` 实例只操作一个仓库；`open(root=None)` 在打开图数据库前解析
`root`（默认当前目录）的 Git 根，解析结果写入 `repository_id` 并在实例生命周期内固定。查询方法
不接受 `repo=` 覆盖，`CodeQueryResult.repository_id` 因此始终是单值。需要跨仓库时分别
`open(root=...)`；不能在一次查询里混合两个仓库的结果。CLI 的 `--root` 传给 `open()`，不是传给
`index()`；`index`/`status`/查询命令共用同一个绑定。

所有方法返回统一信封：

```python
@dataclass(frozen=True)
class CodeQueryResult:
    schema_version: str          # "1.0"
    query_id: str                # query_<UUIDv7>
    repository_id: str           # ACIE 公共 ID
    revision_id: str
    snapshot_hash: str | None    # indexing/partial 时为 None，complete/patched 才有完整值
    results: list[dict]          # 公共实体（ACIE 名称）
    metadata: CodeQueryMetadata

@dataclass(frozen=True)
class CodeQueryMetadata:
    total: int
    truncated: bool
    index_status: str            # complete | patched | partial | indexing | stale | missing
    stale: bool
    freshness: str               # exact | head_changed | dirty_unknown | unknown
    unresolved_count: int
    coverage_available: bool     # 是否导入过覆盖率；get_coverage 为空时区分“无覆盖”与“未导入”
    policy: str                  # strict | balanced | exploratory
    analysis_timestamp: datetime
    limits: dict[str, int]       # depth/max_nodes/max_results 实际生效值
```

规则：

- `depth` 有上限（默认 2，最大 5）；访问节点数上限默认 10,000；满足上限时返回
  `truncated=true`，**不**返回部分结果假装完整；
- 每条结果带 `resolution_status`、`resolution_method`、`confidence`、`confidence_method`、
  `evidence`（最多返回 N 条，按文件/位置排序）；
- 图查询只走 `hermes_graph` 的类型化 API 与内部参数化 Cypher；`CodeIntelligence` 不暴露
  `query.cypher`，调用方不接触图数据库语言；
- 排序确定：先按仓库、文件路径、行号，再按 `symbol_id`；测试与 UI 不漂移；
- `get_impact` 的 `include` 必须显式；默认 `strict`；`implementations` 走
  `IMPLEMENTS`/`OVERRIDES`；
- `get_changes` 只返回 Change Graph 的差异边，不返回历史完整图；`from_revision`/`to_revision`
  缺省为相邻两个**干净**已索引 Revision；dirty Revision 没有变更历史；
- `get_coverage` 只返回带 per-test context 的 `COVERS` 边及其证据；没有覆盖率导入或只有聚合
  报告时返回空并带 `metadata.coverage_available=false`，不把 `TESTS` 当覆盖；
- 所有源码引用只返回路径、行号、`excerpt_hash`；需要正文时由 `build_context` 读取当前文件并
  校验 hash，hash 不匹配则只给路径。

### D9. CLI：`hermes code ...`，JSON 输出，延迟导入

`hermes_cli/subcommands/code.py` 注册 parser，处理函数内延迟导入 `hermes_codegraph`，
`hermes code --help` 不触发 `[codegraph]`/`[graph]` 导入；`"code"` 加入
`main._BUILTIN_SUBCOMMANDS`，避免每次调用先付插件发现成本。

| 子命令 | 作用 | 写入 |
|---|---|---|
| `hermes code index [--root PATH] [--semantic] [--full] [--json]` | 增量索引（`--full` 忽略 hash 全量） | 是 |
| `hermes code status [--root PATH]` | 仓库、Revision、`snapshot_hash`、文件/符号/边计数、失败文件、stale、DB 大小 | 否 |
| `hermes code language list` | 内置 + 可安装语言、grammar/LSP 可用性 | 否 |
| `hermes code language install <lang>` | `pm.ensure_import("codegraph-<lang>")` | 是（环境） |
| `hermes code find <name> [--kind] [--language] [--pattern]` | 符号搜索；`--pattern` 用 ast-grep（可选） | 否 |
| `hermes code symbol <symbol_id>` | 符号详情 + 定义 + 类型关系 | 否 |
| `hermes code refs <symbol_id> [--policy]` | 引用 | 否 |
| `hermes code callers <symbol_id> [--depth]` / `callees` | 调用图 | 否 |
| `hermes code path <src> <dst>` | 调用路径 | 否 |
| `hermes code types <symbol_id>` | 继承/实现/重写/类型注解 | 否 |
| `hermes code impact <symbol_id> [--depth] [--policy]` | 影响范围 | 否 |
| `hermes code deps <module_id>` | 依赖 | 否 |
| `hermes code cycles [--scope modules\|types]` | 循环依赖 | 否 |
| `hermes code tests <symbol_id>` | 静态测试关联 | 否 |
| `hermes code coverage import <file> --revision <rev> [--format auto]` | 导入带 per-test context 的覆盖率；Revision 必须匹配当前 complete 快照，聚合报告拒绝 | 是 |
| `hermes code coverage show <symbol_id>` | 覆盖率查询 | 否 |
| `hermes code diagnostics [--path] [--rule] [--severity] [--limit]` | Semgrep/解析诊断（只读；受 `code_intel.semgrep.enabled` 控制） | 否 |
| `hermes code changes <symbol_id> [--from] [--to]` | Change Graph | 否 |
| `hermes code context <symbol_id> [--budget-tokens]` | Agent 上下文文本 | 否 |
| `hermes code prune [--keep N] [--older-than] [--yes]` | 清理历史/软删数据；先删变更边再删 Revision | 是 |
| `hermes code doctor` | 只读诊断：extras、Schema 版本、语言 Registry、索引一致性、LSP/Semgrep 可用性 | 否 |

- 所有命令都接受 `--root PATH`（默认为当前目录的 Git 根），并在打开图之前绑定一个仓库；跨仓库
  操作必须分别调用。`--root` 由 `CodeIntelligence.open()` 消费，不传给 `index()`；
- `coverage import` 的 `--format` 只接受 `auto`、`coverage-py-context`、`pytest-cov-context`；
  `lcov`/`c8` 聚合输出、无 per-test context 的 JSON 明确拒绝（`CoverageContextMissingError`），
  且 `--revision` 必须匹配当前 complete 快照；
- 正常结果 JSON 到 stdout，错误 JSON 到 stderr，非零退出码；
- `status`/`doctor` 用 `pm.available("graph")` 与 `pm.available("codegraph")` 探测，**不安装**，
  并附带 `pm.store.current_target()`（musl aarch64/bionic 的已知 wheel 缺口在打开图之前报告）；
  数据命令在打开数据库前 `pm.ensure_import("graph")` / `pm.ensure_import("codegraph")`，失败时
  给 `pm.install_hint`；`import hermes_codegraph` 本身不导入
  `ladybug`/`networkx`/`tree_sitter`；
- `--profile` 复用现有 CLI profile 选择；
- `hermes code index` 本身不在模型回合内运行；P6 的 post-turn reconciler 会在最终回答后后台刷新
  变更文件（D16），但仍是显式的增量更新，不是守护进程；没有 `--watch`（watch 需要守护进程和跨
  进程写代理，另立 ADR）。

### D10. 模型工具：默认开启的只读 `code_intel`（写回见 D15）

**决定**：代码智能默认进入模型工具面，不要求用户先启用 toolset；extras 已安装时从会话开始就在
默认工具面里（新装 extras 的生效时机见下）。代价是默认 API 请求多两个工具 schema（只读查询 +
受证据约束的写回；extras 缺失时由 `check_fn` 过滤掉）。

- 工具名 `code_intel`（只读）与 `code_graph_update`（D15 写回），toolset `codegraph`，两个工具
  + `action` 枚举，而不是十几把工具；
- `toolsets.py` 增加
  ```python
  TOOLSETS["codegraph"] = _ts("Code graph queries and evidence-backed updates (symbols, calls, "
                              "types, dependencies, tests, changes, context)",
                              ["code_intel", "code_graph_update"])
  ```
  `code_intel` 与 `code_graph_update` **都加入** `_HERMES_CORE_TOOLS`，因此所有 `hermes-*` 默认
  bundle 携带它们；`TOOLSETS["codegraph"]` 保留为两者的归属，支持
  `disabled_toolsets: [codegraph]`。同时必须在
  `hermes_cli/tools_config.py` 的 `CONFIGURABLE_TOOLSETS` 增加 `codegraph` 条目（默认选中，
  **不**加入 `_DEFAULT_OFF_TOOLSETS`），否则 `_get_platform_tools()` 的显式列表不会把这两个工具
  带进会话。首次发布还要把 `codegraph` 放进 `_RECENTLY_SHIPPED_TOOLSETS`：
  它在发布周期内把新默认 toolset 补到已经保存了 `platform_toolsets` 显式列表的 profile 上
  （条件是其工具 ⊆ 该平台默认 composite；两个工具进入 core 后满足），用户主动取消后
  `known_builtin_toolsets` 会记录拒绝、不再回填。`code_intel.enabled` 默认 `true` 是总开关，
  `code_intel.update` 默认 `true` 只控制写工具；CLI 不受两者影响；
- 只读：不提供 `index`/`install_language`/`coverage import`/`prune`，索引失败或缺失返回结构化
  `not_indexed`，并提示用户运行 `hermes code index`。图写入不属于本工具：D15 的
  `code_graph_update` 是唯一写入口，`code_intel` 永不写图；
- 门控 `check_fn` 只回答**可达性/opt-in**：`code_intel.enabled` 为真 + `pm.available("graph")` +
  `pm.available("codegraph")`；不打开数据库、不做索引、不读取仓库。`pm.available("graph")` 只证明
  `ladybug` 可导入（`pm/extras.py::ANCHORS["graph"]="ladybug"`，networkx 不是 anchor），需要
  networkx 的 `get_cycles`/`get_type_hierarchy`/`get_impact` 必须在调用处把
  `AlgorithmUnavailableError` 翻译成结构化 `unavailable`，不能把 networkx 加进门控（否则 ladybug
  已装、networkx 缺失时连只读查询都消失）。结果按 `(check_fn, check_fn_cache_scope())` 做 TTL
  缓存（`tools/registry.py::_check_fn_cache`；单 profile 为进程级 `None`，多路 gateway 用路由
  profile 的 home override，**不是** `hermes_home_key()`），TTL 30 秒、成功后 60 秒失败宽限；
  该 TTL 只决定重建时是否重新探测，memo 命中时不会重跑 `check_fn`；
- **可用性刷新**：`get_tool_definitions()` 的 memo 键包含 registry generation + `config.yaml` 版本 +
  profile scope，**不含 check_fn 结果**；`agent/turn_context.py::_refresh_mcp_tools_between_turns`
  只在 `tools.mcp_tool` 已导入且注册了 MCP 工具时刷新，且带 `preserve_prefix=True`（只追加/保留
  已发 schema），不是通用 toolset 重建。因此配置翻转（`code_intel.enabled`/`update`）在下次
  `get_tool_definitions()` 因 `config_version` 变化生效；**会话内新装 extras 不会只因回合边界刷新
  出现**，必须经 `reprobe_tool_availability()`（CLI、TUI RPC、gateway 的 `/reload-mcp` 都会调用它，
  清 check_fn 缓存与 tool-defs memo）或重启/新会话。显式 reload 不传 `preserve_prefix`，是用户
  主动触发的整段重预填；两者都不重写系统提示或历史消息；
- 工具结果有行数/字节/深度上限，错误结构化；`impact` 默认 `strict`；
- 不修改系统提示、不注入历史消息；`context` 的输出只是本次工具结果（D11）。

P4 才注册 `code_intel`；`code_graph_update` 在 P5（D15）。P1–P3 的默认工具面逐字不变。P4 起，
extras 已安装时默认工具面包含只读 `code_intel`；P5 起同时包含受证据约束的
`code_graph_update`。extras 缺失时 `check_fn` 使两者都不出现，`get_tool_definitions()` 与首条 API
请求不受影响。

### D11. 上下文构建：预算、排序、可追溯，不污染 prompt cache

`build_context(target, budget_tokens)` 是确定性的：

1. 目标符号定义（签名 + 位置 + 代码片段，若 hash 匹配）；
2. 直接调用者与被调用者（按 policy 过滤，最多 depth=1）；
3. 直接引用者；
4. 类型关系（继承/实现/重写）与依赖模块；
5. 相关测试（`TESTS`）和覆盖率（`COVERS`，如有）；
6. 同文件/同模块的必要声明（类型、常量）。

排序键：目标 > 直接调用 > 直接引用 > 类型/依赖 > 测试 > 上下文声明；同层级按文件路径、行号排序。
每个片段带 `file:line` 与 `symbol_id` 引用，便于模型继续查询。超出预算时截断并返回
`truncated=true`，**不**把半个签名当成完整定义。

`build_context` 的结果：

- 通过工具结果返回时，只属于当前回合；
- 未来接入记忆/Advisor 时，只能作为当前回合的追加内容（`prefetch` → `api_content`），不能改系统
  提示、不能中途换工具面、不能重写历史消息；
- 不自动在每轮注入代码上下文；索引与检索不改变 model 的 cached prefix。

### D12. 存储、作用域与算法：一个 `code` scope，按仓库过滤

- 所有代码图节点/关系注册到 `code` scope；第一次 `index` 时调用 `schema.register` 注册类型，
  重复注册幂等；注册变更会递增 `code` 版本；
- Schema 版本 `codegraph_schema_version` 存在 `CodeRepository.props`，当前为 `"1.0"`；遇到更高
  版本拒绝写入并提示升级 Hermes，不做隐式降级；
- 迁移只允许加法（新增类型/可空列）；破坏性迁移只能走显式的
  `hermes graph schema migrate --destructive` 并先 `admin.backup()`（ADR-7 D4）。**该子命令当前尚未
  实现**（`hermes graph schema` 只有 `list`/`register`），因此 P1 不依赖它，也不得自行实现破坏性
  迁移；需要时先补 ADR-7 的 CLI，再在这里消费；
- 算法：`get_cycles`、`get_type_hierarchy`、`get_impact` 的图算法通过
  `hermes_graph.index.snapshot(..., filters={"repository_id": repo})` 或 `algorithms.*(filters=...)`
  执行；NetworkX 仍是临时计算层，**不写回**；
- `CODE_DEPENDS_ON` 的循环检测在 Module 子图上做；`CODE_INHERITS`/`CODE_IMPLEMENTS` 的循环/层级
  在 Type 子图上做；`CODE_CALLS` 的路径/影响在 Symbol 子图上做；三种图分开；
- Change Graph 用图查询/时间线，不用 NetworkX；
- 算法结果排序确定，Louvain 等随机算法固定 seed（ADR-7 已有约定）。

### D13. 安全与隐私：本地、排除密钥、按不可信输入处理

- 纯语法索引与查询（默认路径）只落本地 profile 目录；extras 已安装时不联网、不遥测、不上传，
  不执行被索引仓库的代码或构建脚本。首次 `pm.ensure_import` 的依赖安装是纯语法路径上的唯一例外，
  `status`/`doctor` 不触发安装；
- `--semantic` **不在上述保证内**：它复用 `agent/lsp`，可能启动 rust-analyzer/gopls/pyright 等
  进程，这些进程会按 LSP 语义执行项目构建脚本、`build.rs`/proc-macro 或读取项目配置；
  `lsp.install_strategy: auto` 还可能在首次使用时下载 server 到 `<HERMES_HOME>/lsp/bin/`。需要
  离线或零代码执行的用户必须保持 `code_intel.semantic: false` 并设 `lsp.install_strategy: off`；
  沙箱/远程后端（`_lsp_local_only` 为假）只跑纯语法索引，不尝试 LSP；
- `code_intel.semgrep.enabled=true` 且 `mode=auto` 时，Semgrep 会从 registry 拉取推荐规则
  （首次/缓存失效时联网），但始终 `--metrics=off`，不上报 metrics/project URL；离线或可复现场景
  必须用 `mode=custom_dir`，只读 `get_hermes_home()/code-intelligence/semgrep/`；Semgrep 不执行
  仓库代码、不写文件（无 `--autofix`）；
- 代码图不加载 profile `languages.yaml` 的 `parser` 字段：该字段只供 tree-sitter CLI 使用，代码图
  不 `dlopen` 用户可编辑的 `.so`/`.dll`/`.dylib`；语法解析只使用已安装的 Python grammar 包；
- 所有源码按**不可信输入**处理：大小/数量/深度上限，二进制跳过，符号链接必须 realpath 在仓库根
  内（越界跳过），拒绝设备文件/FIFO；
- 强制排除密钥类文件（D7）；不读取 `.env`、证书、私钥、凭据文件；证据只存 hash 和位置，不存
  完整源码；仓库 remote 的 userinfo/token 在入库前剥离（D4），图里不保存凭据；
- 覆盖率文件同样按不可信输入解析：只接受白名单格式、限制文件大小和行数，不执行覆盖率工具
  生成的任何内容；
- LSP 进程沿用 `agent/lsp` 的 workspace trust：未信任仓库只允许 `UNTRUSTED_SAFE_SERVERS`，
  或直接跳过语义增强；代码图不新增“为了索引先信任仓库”的开关；
- 跨 profile 读取是安全缺陷：索引路径由 `GraphClient.open()` 的 `get_hermes_home()` 决定，
  测试用两个临时 `HERMES_HOME` 断言互不可见；
- 代码内容可能包含提示注入文本；`build_context` 的片段用明确围栏和 `file:line` 标注，工具结果
  不是指令；v1 不把代码片段写进系统提示；
- 不读取 `agent.secret_scope`，不把凭据写进图。

### D14. 依赖、配置与分阶段落地

**基线依赖**：`[codegraph]` extra，精确 pin，包含**四个内置 grammar**：

```toml
[project.optional-dependencies]
codegraph = [
  "tree-sitter==0.26.0",
  "tree-sitter-python==0.25.0",
  "tree-sitter-typescript==0.23.2",
  "tree-sitter-go==0.25.0",
  "tree-sitter-rust==0.24.2",
]
```

- 四个内置 grammar 是基线，不逐个拆 extra；若目标平台缺任一 grammar wheel，`[codegraph]` 是
  **原子** 的（`pm/extras.py` 的 tuple anchor 要求五个 anchor 全部可导入）：
  `pm.available("codegraph")` 为 false，`hermes code` 报告 `unavailable` 并给 `install_hint`；
  不源码构建、不拖垮 core，也不承诺部分安装。Android 与 musl aarch64 没有 `tree-sitter` wheel
  （见外部依据），这两类平台上 `[codegraph]` 直接 unavailable：grammar 依赖带
  `sys_platform != 'android'` 标记，并在 `[tool.hermes.extras-platforms]` 增加
  `codegraph = "sys_platform != 'android'"`（与现有 `graph` 门一致，且必须满足
  `tests/pm/test_extras.py::test_declared_extra_gates_match_dependency_selection` 的
  “gate 与依赖 marker 选择一致”断言）。musl aarch64 不能靠 PEP 508 marker 表达（marker 环境没有
  libc 变量），`pm.available()` 也不做 wheel 探测：它只能表现为 anchor 不可导入，因此
  `status`/`doctor` 与 `language install` 必须先读 `pm.store.current_target()`
  （`linux-arm64-musl`/`linux-*-bionic`）给出明确 gap，而不是让 `ensure_import()` 的 uv 解析错误
  冒泡；`hermes pm install --extra codegraph` 在 musl aarch64 上会以 resolver 错误失败，这是已知限制；
- 其他语言按需安装：每个语言一个 `codegraph-<language>` extra，例如
  `codegraph-java = ["tree-sitter-java==<P2 实测后固定的版本>"]`。版本必须在 P2 通过实际 wheel
  验证后精确 pin，本 ADR 不编造未验证的版本号；每个 extra 在 `pm/extras.py` 有独立 tuple anchor；
- `[codegraph]` 与 `[graph]`（Ladybug + NetworkX）是相互独立的 extra，extras 不能互相依赖，用户必须
  把两者都装上；`pm.ensure_import("codegraph")` 之后仍需 `pm.ensure_import("graph")`，两者缺一都给
  `install_hint`，不自动跨 extra 安装；
- `pm/extras.py` 增加
  `"codegraph": ("tree_sitter", "tree_sitter_python", "tree_sitter_typescript", "tree_sitter_go", "tree_sitter_rust")`
  的 tuple 锚点（不是一个模块名），让 `pm.available("codegraph")` 与“全有或全无”一致；
  `pyproject.toml` 的 `[tool.setuptools.packages.find].include` 增加 `hermes_codegraph` 与
  `hermes_codegraph.*`；`[tool.setuptools.package-data]` 增加
  `hermes_codegraph = ["adapters/languages/languages.yaml",
  "adapters/languages/queries/**/*.scm"]`，sealed wheel 必须携带内置 registry 与 `.scm` 查询文件；
  profile `tree-sitter/languages.yaml` 不随包发布，存在时作为第二个 registry 加载；
- `pm/code_intelligence_packages.py` 已有 ast-grep / tree-sitter / CodeQL / Semgrep 安装项；
  Semgrep 是 `PythonToolPackage`，只通过 `hermes pm install semgrep` 安装，不执行裸 `pip`/`uv`；
  其平台缺口（darwin-x64、win32-arm64、linux-arm64-bionic）由 PM 结构化报告。规则不随包固定：
  `mode=auto` 用 registry 推荐规则，`mode=custom_dir` 用 profile 下的
  `code-intelligence/semgrep/`。代码图不依赖这些 CLI 才能索引；LSP server 的获取/安装只走
  `agent/lsp/install.py`，不新增 PM 包。

**配置**（`config.yaml`，默认值进 `hermes_cli/config_defaults.py`，不新增 `HERMES_*` 环境变量）：

`code_intel` 是唯一的配置域；`codegraph` 只作为 extra/toolset/包名，不再作为配置键：

```yaml
code_intel:
  enabled: true           # 代码智能系统总开关：模型工具、自动刷新、语义增强、Semgrep 诊断
  update: true            # P5：是否暴露 code_graph_update（写回仍受证据/并发校验）
  auto_update: true       # P6：最终回答后后台刷新本仓库变更文件（只处理 hash 不同的文件）
  semantic: false         # 索引时是否调用 agent/lsp 做语义增强
  semgrep:
    enabled: false        # 危险模式诊断；需 `hermes pm install semgrep`
    mode: auto            # auto | custom_dir
                          # auto: --config auto（registry 推荐规则；首次可能联网）
                          # custom_dir: --config <HERMES_HOME>/code-intelligence/semgrep/
  languages: []           # 额外启用的语言 id；内置四种始终启用
  max_file_bytes: 1048576
  max_files: 20000
  max_total_bytes: 134217728
  max_symbols: 1000000
  exclude: []             # 追加的 gitignore 风格模式
```

- `code_intel.enabled=false` 关闭模型工具、post-turn 自动刷新、LSP 语义增强与 Semgrep 诊断；
  `hermes code ...` CLI 仍可用于索引/诊断，并在 `status`/`doctor` 中报告 `enabled=false`；
- `code_intel.semgrep.enabled` 只在 `code_intel.enabled=true` 时生效；semgrep 未安装或平台
  不支持时诊断跳过并给 install hint，不阻塞语法索引；
- `code_intel.semgrep.mode` 只接受 `auto`（默认）与 `custom_dir`；其他值在配置加载时拒绝。
  `auto` 用 Semgrep registry 推荐规则（首次/缓存失效时可能联网，始终 `--metrics=off`）；
  `custom_dir` 只读 `get_hermes_home()/code-intelligence/semgrep/`，离线可用；目录缺失/为空时
  诊断跳过并写 `CodeAnalysisRun`，不影响索引；

不提供索引路径配置：图数据库路径由 `GraphClient.open()` 固定为
`get_hermes_home()/graph/graph.lbdb`，代码图不能把数据指到 profile 之外。

**分阶段**：

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| **P1** | `hermes_codegraph/` 骨架、Schema 注册、内置 `languages.yaml` + profile 扩展 registry 的合并/去重、Scanner、Python/TypeScript 提取器、Symbol Graph、Evidence、按文件增量索引、`index/status/doctor` CLI | 两语言符号/定义/证据/幂等/失败保留通过；重复索引节点与边计数不变；`import hermes_codegraph` 不导入重依赖；profile 中重复 `language` 被跳过并计数，坏元素拒绝该文件 |
| **P2** | Go/Rust、Call/Type/Dependency Graph、静态 `TESTS`、影响/路径/循环、结果信封、查询 CLI、仓库级快照过滤、按需 grammar 安装 | 四语言与六类图的节点/边 fixture 通过；深度/节点/结果上限与 `truncated` 正确；算法按仓库隔离；`language install` 走 PM |
| **P3** | Change Graph（`PARENT_OF`/`CHANGED_IN`/`DELETED_IN`、跨文件移动匹配）、覆盖率导入与 `COVERS`、Revision 变更查询 | 八条增量规则全部通过；符号移动唯一匹配复用、歧义不合并；覆盖率导入只产生 `COVERS`；历史全图查询被拒绝 |
| **P4** | `agent/lsp` 窄查询 API、语义增强、ast-grep 结构搜索、Semgrep 危险模式诊断（受 `code_intel.semgrep.enabled` 控制）、`code_intel` 工具、内置 skill | 可信/未信任仓库行为正确；LSP 缺席时纯语法索引完整；Semgrep 关闭/缺失时诊断跳过且索引成功；冲突目标不留活跃旧边；extras 缺失时默认工具面逐字不变，extras 已安装时默认包含只读 `code_intel`；会话内新装 extras 经 `reprobe_tool_availability()`（显式 reload）或重启出现；不重写系统提示/历史 |
| **P5** | `code_graph_update`（`refresh_file`/`apply_evidence`/`invalidate`）、乐观并发、`patched` 状态、审计与冲突优先级 | 单文件更新提交后下一查询立即可见；无证据/越界/过期 hash 全部拒绝且零部分写入；冲突不覆盖高优先级事实；全量 index 回收冲突补丁并写 Diagnostic；`code_intel.update=false` 只隐藏写工具 |
| **P6** | post-turn reconciler：`run_conversation` 单出口钩子、变更探测、单飞、预算、`stale`/`partial` 回退 | 回合改文件后下一次查询自动看到新事实；无变更零写入；锁冲突不影响回合；子代理不触发；脏树不写变更边 |

### D15. 代码图更新：分析事实回写、按文件事务、乐观并发

**事实来源**：Syntax（Tree-sitter、ast-grep）、Semantic（LSP、Language Adapters）、Analysis
（Semgrep、CodeQL）、Search（ripgrep、Symbol Search）是图事实的**唯一来源**；图只是这些层结果的
物化投影。批量 `hermes code index` 会落后于回合内的新观测，因此增加写工具
`code_graph_update`，把分析工具刚返回的事实立即写回对应仓库。

**工具与开关**：`code_graph_update`，toolset `codegraph`，默认开启；与 `code_intel` 共用
`check_fn`（`code_intel.enabled` + `pm.available("graph")` + `pm.available("codegraph")`），
另受 `code_intel.update`（默认 `true`）控制。实例绑定 `CodeIntelligence.open()` 的仓库，只能写
`code` scope 中已声明的类型。`code_graph_update` 同样加入 `_HERMES_CORE_TOOLS`，与
`TOOLSETS["codegraph"]` 一起默认出现；代价是默认请求多一个写工具 schema。

**动作**：

1. `refresh_file(path, semantic=false, semgrep=false)`：单文件权威重建——重算 hash、重新解析
   （`semantic=true` 时复用 D6 的 LSP；`semgrep=true` 时运行 D17 的 Semgrep），按 D7 的 per-file
   事务删除该文件及其符号作为 source 的边/证据，重建 File/Symbol/Module/TestCase/Diagnostic/
   Evidence；更新 `content_hash`/`indexed_hash`/`indexed_revision` 与 `snapshot_hash`。这是默认
   修复路径；`code_graph_update` 与 post-turn 固定 `semgrep=false`，不触发规则网络；
2. `apply_evidence(path, facts[])`：只写入**分析工具观测到**的事实。每条 fact 必须带
   `evidence{source_tool, source_tool_version, span, excerpt_hash, observed_at}`、
   `expected_revision_id`、`expected_content_hash`；kind 限于
   definition/reference/call/type/import/test/diagnostic。来源能力按下表校验；不匹配返回
   `EvidenceRejectedError`；目标符号必须已存在（否则先
   `refresh_file` 目标文件）；写 `CodeEvidence`，用 `edges.upsert` 建/更新边。`resolution_status`
   只能是 `inferred`/`resolved`（`verified` 只由覆盖率导入产生）。文件标
   `index_status="patched"`，写 `CodeAnalysisRun(kind="model_update")`。本动作**不更新**
   `indexed_hash`：`patched` 文件在下一次 `index`/`refresh_file` 时强制重解析（D7 的 hash 跳过
   规则不适用），由解析结果回收补丁。`apply_evidence` 只允许文件
   `content_hash == indexed_hash` 且 `index_status ∈ {complete, patched}`；否则必须先
   `refresh_file`；
3. `invalidate(ids[], reason)`：把证据/边标 `invalidated`；边已无有效证据则删除；不删除符号节点。

| 层 | 工具 | 可写入图的事实 |
|---|---|---|
| Syntax | Tree-sitter | definition/import/call（`inferred`） |
| Syntax | ast-grep | `reference_expression`/诊断证据；不单独建立 `resolved` 边 |
| Semantic | LSP / Language Adapters | definition/reference/implementation（`resolved`） |
| Analysis | Semgrep / CodeQL | `CodeDiagnostic` |
| Search | ripgrep | 文本匹配证据（`reference_expression`）；不单独建边 |
| Search | Symbol Search | 只读查询，不作为写入来源 |

只有 Tree-sitter 与 LSP 能创建/替换符号或关系边；ast-grep/ripgrep 只写证据，Semgrep/CodeQL
只写诊断，均不得单独建边。

**不提供**：任意 Cypher、任意新节点/边类型、`delete`/`rename`/`force`、跨仓库写、全量重建
（全量仍走 `hermes code index`）。

**冲突与优先级**：

- 每次写必须带 `expected_revision_id` + `expected_content_hash`（取自 `code_intel` 结果或文件
  读取）；不匹配返回 `GraphConflictError`（含当前值）且不写；磁盘与图不一致时只允许
  `refresh_file`。为此 `code_intel` 的 file/symbol 结果必须带 `content_hash`、`indexed_hash`、
  `index_status`，否则模型无法安全构造前置条件；
- 优先级沿用 D5：verified > resolved > inferred > ambiguous。`apply_evidence` 不得降级既有事实；
  同等/更低优先级冲突标 `ambiguous` 并返回冲突，不覆盖；更高优先级覆盖时旧证据 `invalidated`，
  无边有效证据则删边；
- `refresh_file` 与后续全量 `index` 的解析结果**高于**模型回写：重算后冲突的补丁证据被
  `invalidated` 并写 `CodeDiagnostic`。补丁是临时物化，不是历史事实；
- `strict` 接受 `patched`（`snapshot_hash` 已重算），仍拒绝 `partial`/`indexing`/`stale`。

**事务、可见性与审计**：单文件/单批一事务，沿用 `GraphClient` 的写锁、超时与回滚；提交即递增
`code` scope 版本、失效快照缓存，下一次 `code_intel` 查询立即可见。`patched` 的
`snapshot_hash` 按 `sorted(indexed_hash) + patch_seq` 重算，保证图变化对消费者可见：
`patch_seq` 存在 `CodeRepository.props`，每次 `apply_evidence`/`invalidate` 递增，全量 `index`
重置为 0；`refresh_file` 只更新该文件的 `indexed_hash`，不清零其他文件的补丁。下一次全量
`index`/`refresh_file` 后，被回收文件的补丁消失，hash 回到对应口径。返回
`updated`/`removed`/`conflicts`/`snapshot_hash`/`analysis_run_id`；`CodeAnalysisRun` 记录动作、
工具、事实数、前后 hash。不保存源码，只存证据 hash/span。

**安全**：模型不是事实来源——`source_tool="model_observation"` 永远拒绝；`excerpt_hash` 必须匹配
当前文件字节、span 必须在文件内；路径 realpath 在仓库根内并通过 D7 排除规则；单次事实数/字节
上限；不执行仓库代码；不读密钥文件；写锁失败不重试到死。

**失败**：`GraphLockedError`（写锁）、`EvidenceRejectedError`（工具/证据/hash 非法）、
`GraphConflictError`（revision/hash 不匹配）、`ValidationError`（fact schema）；全部零部分写入。

### D16. 回合结束后的自动增量更新（post-turn reconciler）

**触发点**：`agent/conversation_loop.py::run_conversation` 是回合的唯一出口——成功、partial/error、
interrupt、retry 耗尽、tool-limit、preflight、codex runtime 的 envelope 都经过它（`finalize_turn`
只是通用循环的路径；`codex_app_server` 在它之前 `return`，脚本 prelude、
`agent/turn_preflight.py::_done("return", ...)` 的 compression deferred/exhausted、
runaway-repetition、`recover_empty_response`/retry 的 early return 也不经过它）。因此在
`settle_held_final()`/`export_current_turn_boundary()` 之后、`return result` 之前调用
`schedule_codegraph_post_turn(agent, result=..., turn_id=...)`，一次承载所有终结路径。入队本身
**零 I/O**：只读已加载的 `code_intel.auto_update` 与 `hermes_home_key`/workspace/turn_id，**不跑
`git status`、不解析仓库根、不打开图**。每个**顶层**回合在 `final_response` 非空且未失败时触发
一次（interrupted 但已有最终文本也算）；非顶层回合用现有谓词排除：
`is_delegated_child_context()`；`agent._turn_origin` 覆盖 `build_cache_parity_fork` 的
background review / side-question fork；curator 直接构造 `AIAgent`，只设
`_memory_write_origin="background_review"` 而 `_turn_origin` 为空，因此还要
`getattr(agent, "_memory_write_origin", "assistant_tool") != "assistant_tool"`；
`agent._delegate_depth` 是 `agent/delegation_backends.py` 使用的同类判定。结果字典形状不统一：
`finalize_turn` 带 `failed`，codex/prelude/early-return 带 `completed`/`partial`/`error`；判定必须
是“`final_response` 非空 且 `not failed` 且 `not error`”，**不加** `not interrupted`（interrupted
但已交付文本的回合同样要刷新），也不能只读某一个键。

**仓库与 availability 门**：后台任务先绑定发起回合的 profile，检查 `pm.available("graph")` 与
`pm.available("codegraph")`；再解析 workspace 根的仓库、计算 `repository_id` 并做
`CodeRepository` 主键存在性检查。仓库尚未索引（没有 `CodeRepository`）→ 直接 no-op，**不创建空
图库、不跑 `git status`**。只有已索引仓库才进入变更探测。

**一次性进程**：`agent/oneshot_footprint.py::is_single_query_session` 为真（one-shot/cron 在回答后
立即退出）时不入 daemon 队列，改为在 `run_conversation` `return result` 前执行**有界同步 flush**
（内部常量：≤2s、≤20 文件）；超预算立即停止，写 `CodeAnalysisRun(status="truncated")`，剩余文件保持
`stale`，下一次会话/CLI 兜底；flush 不改变退出码。交互式 CLI、gateway、TUI、Desktop 仍走后台
队列。

**范围**：当前会话 workspace 根绑定的一个仓库；只处理 `git status --porcelain -z` 中新增/修改/
删除的路径，遵守 D7 的排除与上限；非 Git 仓库或无法解析根时 no-op。只处理
`content_hash != CodeFile.indexed_hash` 的文件；空变更集 no-op。

**动作**：按 P5 的 `refresh_file` 单文件事务逐个更新；删除文件走 D7 删除语义；批提交递增 `code`
scope 版本。默认 `semantic=false`，只有 `code_intel.semantic: true` 才调用 LSP；post-turn 固定
`semgrep=false`，不运行 Semgrep，诊断由显式 `hermes code index` 刷新。单回合预算（内部
常量：文件数/时间）内完成；超限的文件保持 `stale`，仓库 metadata 标 `partial`，下一回合按 hash
继续，全量 `hermes code index` 仍可兜底。脏工作树只更新当前图，不写 `PARENT_OF`/`CHANGED_IN`/
`DELETED_IN`（D7）。

**并发与失败**：后台任务解析 root 后按 `(hermes_home_key, repository_id)` single-flight；拿到槽的
才执行 `git status`/hash diff，重复触发直接合并返回，不重复探测；已有任务则合并，不无限排队；
`GraphLockedError`/写锁超时跳过，下一回合重试，不自动开第二个写者；任务显式绑定发起回合的
profile scope，不读 launch profile。异常只写 `CodeAnalysisRun(kind="post_turn", status="failed")`
和日志，**绝不影响回合结果**；已提交文件保留。

**开关**：`code_intel.auto_update` 默认 `true`；`false` 时不调度。`code_intel.enabled=false` 同样
不调度。该任务只写图，不调用模型、不联网、不执行仓库代码；post-turn 不运行 Semgrep，避免后台
规则拉取与额外成本。

**为什么不是每轮全量 index**：只探测变更文件；全量索引仍是显式 CLI。P5 的 `code_graph_update`
负责回合内即时修正，P6 负责回合结束后的兜底一致性；两者都幂等。

### D17. 语法/语义/危险模式诊断：三层事实，`code_intel` 统一开关

**分层**：

| 层 | 实现 | 默认 | 产物 | 边界 |
|---|---|---|---|---|
| Syntax | Tree-sitter | grammar 安装后始终启用 | Symbol/Module/Import/Call/Type/Test（`inferred`） | 不启动 LSP、不联网；未知语言不猜 |
| Syntax（结构搜索） | ast-grep | 可选 | `reference_expression`/诊断证据 | 不单独建 `resolved` 边；未安装时 `hermes code find --pattern` 报 unavailable |
| Semantic | `agent/lsp` 窄查询 API | `code_intel.semantic=false` | definition/reference/implementation（`resolved`）与 LSP 证据 | 仅 host filesystem、trust 允许的仓库；不扩大 trust、不新增 LSP 安装器；缺席时语法索引继续 |
| Risk | Semgrep | `code_intel.semgrep.enabled=false`；`mode=auto` | `CodeDiagnostic`（rule_id/severity/span/excerpt_hash） | 只写诊断，不生成/替换 Symbol/Call/Type 边；不执行仓库代码；`auto` 仅拉取规则，`custom_dir` 离线 |

**LSP 语义与语法**：LSP 不替代 Tree-sitter。索引先用语法层建立完整基线；`semantic=true` 时再按
D6 的 occurrence 规则升级边。LSP 请求超时/失败返回空并保留语法事实，不把整个文件标失败。

**Semgrep 危险模式诊断**：

- 来源：PM 安装的 `semgrep` CLI（`pm/code_intelligence_packages.py::Semgrep`）；运行时通过
  `pm.env_for("semgrep")` 组合 PATH，不把 semgrep 装进应用 venv；
- 扫描方式由 `code_intel.semgrep.mode` 决定：
  - `auto`（默认）：`--config auto`，使用 Semgrep registry 的推荐规则集；首次/缓存失效时可能
    联网拉取规则，始终 `--metrics=off`，不上报 metrics/project URL；
  - `custom_dir`：`--config <get_hermes_home()>/code-intelligence/semgrep/`，只加载该目录下的
    自定义规则；离线可用；目录缺失/为空时跳过诊断并写 `CodeAnalysisRun`，不影响索引；
- 调用：`semgrep scan --config <auto|dir> --json --metrics=off --disable-version-check`
  （单文件/批次、带超时与排除）；禁止 `semgrep ci`、`--autofix` 及任何写回；退出码只用于判断
  扫描是否完成，不把非零当索引失败；
- 触发：`code_intel.enabled=true` 且 `code_intel.semgrep.enabled=true` 时，在 `hermes code index`
  的语法解析之后按文件运行；显式 `refresh_file(semgrep=true)` 也可运行；post-turn reconciler 与
  `code_graph_update` 固定 `semgrep=false`，不在后台触发规则网络；单文件超时/非零退出只写
  `CodeAnalysisRun(status="skipped"|"failed")` 与该文件的 Diagnostic，不阻塞其余文件；
- 产物：每条 finding 是 `CodeDiagnostic`，带 `source_tool="semgrep"`、
  `source_tool_version`、`rule_id`、`severity`、`path`、`span`、`excerpt_hash`、`revision_id`、
  `snapshot_hash`；不保存源码正文；
- 查询：`CodeIntelligence.get_diagnostics(path=None, rule=None, severity=None, limit=...)` 返回
  统一信封；`code_intel` 工具可返回诊断；没有 semgrep 时返回空并带
  `diagnostics_available=false`，**不新增独立工具**，避免工具面/提示缓存抖动；
- 安全：只在 host filesystem 后端运行；不执行仓库代码；`mode=auto` 只拉取 registry 规则，
  `mode=custom_dir` 不联网；始终 `--metrics=off`，不上报 project URL；不写文件（无
  `--autofix`）；`excerpt_hash` 必须匹配当前文件，诊断绑定 Revision，快照不一致时拒绝写入
  （D15）。

**开关矩阵**：

| 配置 | 效果 |
|---|---|
| `code_intel.enabled=false` | 不注册 `code_intel`/`code_graph_update`，不调度 post-turn，不运行 LSP 语义增强与 Semgrep；CLI 仍可用 |
| `code_intel.semantic=false` | 纯语法索引；LSP 不启动 |
| `code_intel.semgrep.enabled=false` | 不运行 Semgrep；语法/语义不受影响 |
| `code_intel.semgrep.mode=auto`（默认） | `--config auto` 使用 registry 推荐规则；首次/缓存失效可能联网拉规则；`--metrics=off` |
| `code_intel.semgrep.mode=custom_dir` | `--config <HERMES_HOME>/code-intelligence/semgrep/`；离线；目录缺失/为空时跳过诊断 |
| `code_intel.semgrep.mode=<其他>` | 配置校验失败，不启动诊断 |
| `code_intel.semgrep.enabled=true` + semgrep 缺失 | 诊断跳过，`diagnostics_available=false`，给 `hermes pm install semgrep` hint；索引成功 |

## 后果

**正面**

- 六类关系图共享一套节点/边/证据/Revision 模型；Coding Agent/Reviewer/Advisor 不再各自解析代码。
- 存储、事务、scope 版本、算法复用 ADR-7，不产生第二套图实现；换存储只改 `hermes_graph`。
- Language Registry 用同一种 descriptor schema 描述所有语言，并由**一个** descriptor/query 驱动的
  通用抽取器处理；内置 registry 先加载，profile 扩展 registry 按 `language` 去重、只补充新语言；
  `languages` 列表按 `language` 区分，内置四种开箱即用，其他语言按需安装 grammar，LSP 按需安装
  server。新增语言只写 descriptor、`extraction` 与 `.scm` 查询，不新增遍历代码。
- 语法/语义/风险三层开关分离：`code_intel.enabled` 是总开关，`semantic` 与 `semgrep.enabled`
  独立 opt-in；关闭危险模式诊断不影响语法/语义索引。
- 证据与解析状态分层，模型不会把推断当事实；`verified` 只来自覆盖率运行证据。
- 增量协议与原文档八条规则一一对应，失败不覆盖、Revision 不混用。
- 重依赖仍可选；`code_intel`/`code_graph_update` 默认开启，但 extras 缺失时 `check_fn` 不注册
  它们，普通会话不付 schema 成本；extras 已安装时每次请求多两个工具 schema（一个只读、一个
  受证据约束的写回）。
- 回合内可以用分析工具的新观测立即修正图，不必等全量 `hermes code index`；下一次查询即可见，
  且补丁有 `CodeAnalysisRun`/证据审计。
- 每个顶层回合的最终回答后自动同步本回合改过的文件；下一轮查询/工具直接看到新事实，无需用户
  记得跑 CLI。

**负面 / 代价**

- 六类图、Language Registry、Change Graph 和覆盖率导入是长期维护面；每增加一种语言都要完整的
  描述符和 fixture。
- 单写者图数据库意味着索引进程与 gateway 争写锁；索引只能由 CLI/显式进程执行，不能嵌在回合里。
- 一个 `code` scope 让任何仓库的任何文件变化都使所有仓库的快照失效；仓库级版本要等 Schema
  Registry 支持更细粒度 scope。
- 当前图只保留最新 Revision；历史完整图查询不支持，Change Graph 只有差异边。真正的历史快照
  需要后续 ADR。
- Tree-sitter 原生 wheel 增加平台矩阵；基线 extra 是原子的，缺一个 wheel 就没有代码图。
- 跨文件符号移动匹配是启发式的；匹配失败会多出新符号，需要用户/未来工具清理。
- 写工具增加证据校验、优先级与并发冲突的维护面；补丁是临时物化，下一次全量索引可能覆盖它
  （冲突补丁会被 `invalidated` 并写 Diagnostic）。
- 回合后后台任务会与 gateway/其他写者争用图锁并消耗 CPU；预算超限会留下 `stale`/`partial`，
  需要下一回合或全量 CLI 收敛。

**中性**

- `hermes lsp` 继续负责写文件后的诊断；本 ADR 只增加读取定义/引用的窄 API。
- `gitnexus-explorer` 仍是可选可视化技能；本 ADR 不替换、不迁移它的浏览器 UI。
- `agent/learning_graph.py` 仍是技能/记忆的只读投影，代码图不写入它。
- `pm` 里的 ast-grep/CodeQL/Semgrep 安装项不变；代码图不把它们变成必需依赖。

## 未采纳的替代方案

| 方案 | 否决理由 |
|---|---|
| 新建独立 ACIE 服务 / 独立数据库 / HTTP/MCP 接口 | 多一个常驻服务、端口、认证、备份；与本地单进程/profile 形态冲突；复用 ADR-7 即可 |
| 用 SQLite 存索引元数据（ACIE 草案 §9 / 架构图） | 第三套存储与第三套一致性规则；File 节点按 key 派生 UUIDv5 已是 O(1) 主键读；SQLite 只允许作为未来的派生/临时队列 |
| 直接用 `ladybug.Connection` / `networkx.Graph` 写代码图 | 连接、锁、Schema、事务、快照失效全部外泄；违反 ADR-7 D2 |
| 每仓库一个图数据库或动态 scope | `NodeType.scope` 是静态字段；动态仓库 scope 需要改 Schema Registry，v1 用 filters |
| 未解析关系存成 `target_id=NULL` 的边 | Ladybug 关系表要求两端存在；用 Evidence 保留表达式，解析后再建边 |
| 把 Evidence 压成边上的 `confidence` 属性 | 丢失多来源证据（Tree-sitter / LSP / 静态推断 / 覆盖率），无法审计 |
| 让模型直接写 Cypher | 暴露存储实现、可越权读其它 scope、无法统一上限与过滤 |
| 让 `code_intel` 直接写图 | 破坏只读边界；读工具会被提示注入/误用为写入面，无法独立门控 |
| 模型直接 upsert 无证据事实 | 幻觉会污染所有消费者；D15 只接受分析工具证据并校验 `excerpt_hash` |
| 每次事实冲突跑全量 index | 延迟、写锁与成本不可控；D15 用单文件事务即时提交 |
| 每轮最终回答后跑全量 index | 延迟与写锁不可控；D16 只探测并刷新 `git status` 变更文件 |
| 在最终回答前同步更新图 | 阻塞用户可见回答；D16 在回答确定后入队后台任务 |
| 在回合出口里做 `git status`/hash diff | 大仓库可达秒级，会阻塞回答；D16 只零 I/O 入队，探测在后台 |
| one-shot/cron 也用 daemon 队列 | 进程在回答后立即退出，任务被杀；D16 改为有界同步 flush |
| watch/守护进程常驻索引 | 需要跨进程写代理、生命周期与权限模型，另立 ADR；D16 只在回合边界触发 |
| 子代理回合也触发 | 同一仓库会重复写、放大锁争用；只由顶层回合触发 |
| 默认注册 14 个代码工具 | 违反窄腰；每次请求都发送；用一个带 `action` 的只读工具即可 |
| 默认关闭 / 只在会话边界出现 | 用户明确要求默认开启；extras 已安装时从会话开始就可用，可用性由 `check_fn` 按 extras 判定；新装 extras 需显式 reload/重启 |
| 索引时自动信任仓库以启动 LSP | 绕过 `lsp.trusted_workspaces`；rust-analyzer/cargo 等会执行项目构建脚本 |
| 每次查询自动全量重索引 | 延迟不可控；会与 gateway 争写锁；用户无法预测成本 |
| 所有语言 grammar 都进基线 / 全部按需 | 前者让基线膨胀；后者让四个核心语言也要额外安装。采用“四内置 + 其余按需” |
| 用 AST 相似度自动合并跨文件符号 | 误合并会污染所有消费者；只在 content_hash/signature 唯一匹配时复用 |
| 用 `COVERS` 代替 `TESTS` | 静态关联不是执行覆盖；两者必须分开 |
| 把 `gitnexus-explorer` 的索引当后端 | 外部 Node 工具、临时数据、浏览器规模限制；不是可复用的 core 契约 |

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| Tree-sitter grammar ABI 与 `tree-sitter` 版本不匹配 | P1 在 Windows/macOS/Linux 对四种内置 grammar 做 `Parser(Language(...))` 冒烟；固定 pin，只改 extra |
| 某平台缺基线 grammar wheel | `[codegraph]` 整体标 `unavailable`；`status`/`doctor` 说明原因；不源码构建 |
| 按需 grammar extra 版本漂移 | 每个 extra 精确 pin 并在 P2 验证 wheel；`hermes pm lock` 更新；不运行时裸 pip |
| Language Registry 描述符不完整 | P1 校验每个启用语言的所有必需字段；缺 capabilities 视为 false，不用通用 fallback |
| 索引期间写锁与 gateway 冲突 | 索引只在 CLI/显式进程；`GraphLockedError` 带库路径与 remedy（当前不识别占用进程，不能承诺 PID 提示）；不自动开第二个库 |
| 索引中途查询/部分提交 | `CodeAnalysisRun` running 标记；`strict` 拒绝、`balanced` 标 partial；变更边只在最终事务写 |
| 大仓库索引超时/内存爆炸 | 文件/字节/符号上限；分批事务；解析在事务外；超限 `IndexLimitError` 并保留已完成部分 |
| 解析失败覆盖好数据 | 失败保留旧符号并标 `parse_status=failed`/`index_status=stale`；测试锁定 |
| 符号删除导致边残留 | 删除双向关联边、写 `DELETED_IN`；符号节点保留给 Change Graph，`prune` 才清理 |
| 跨文件符号移动误合并 | 只在 content_hash+signature+kind+language 唯一匹配时复用；歧义新建并写 Diagnostic |
| Change Graph 无限增长 | Revision/变更边按 `prune --older-than` 清理；当前图不依赖旧 Revision |
| 快照过滤让缓存键膨胀 | filters 只允许声明字段的等值匹配；排序后入键；默认无 filters 行为不变 |
| LSP 启动慢或挂起 | `--semantic` opt-in；每请求超时；失败不重试到死；语法索引先生效 |
| 语义增强触发网络下载或项目代码执行 | `--semantic` 默认关闭；沿用 `lsp.install_strategy` 与 trust；离线设 `off`；`_lsp_local_only` 为假时不启动 LSP |
| LSP 在未信任仓库执行项目代码 | 沿用 `UNTRUSTED_SAFE_SERVERS` 与 trust；代码图不提供绕过开关 |
| Semgrep 联网/执行仓库代码或拖慢索引 | `code_intel.semgrep.enabled` 默认关闭；`mode=auto` 只拉取 registry 规则且 `--metrics=off`；离线用 `mode=custom_dir`；不执行仓库代码、不 `--autofix`；单文件超时/失败跳过并写 `CodeAnalysisRun`，不阻塞索引 |
| profile registry 重复加载内置语言 | 内置 registry 先加载；profile 中同 `language` 条目跳过，不覆盖、不重复注册；`status`/`doctor` 报告 skipped 数 |
| profile `tree-sitter/languages.yaml` 损坏或冲突 | 单文件整文件校验；重复 `language` id/alias/extension、缺字段、未知字段、类型错误、越界路径结构化失败；profile 冲突条目跳过并报告，不覆盖内置事实 |
| remote URL 携带 token 写入图 | 入库前剥离 userinfo/query/fragment；`remote_url` 只存脱敏值；日志同样脱敏 |
| 覆盖率格式漂移/缺少 per-test context | 白名单格式、大小/行数上限、只解析不执行；只有 per-test context 才写 `COVERS`，聚合报告明确拒绝；`revision_id` 不匹配拒绝 |
| 覆盖率/诊断绑定旧 Revision | `CodeAnalysisRun` 与 `CodeEvidence` 记录 `revision_id`/`snapshot_hash`；与当前 complete 快照不一致返回 `RevisionMismatchError` |
| 代码片段包含提示注入 | 工具结果围栏 + `file:line` 标注；不写系统提示；源码不落证据 |
| 密钥文件被索引 | 强制排除 `.env`/证书/私钥；测试用临时仓库断言不出现 |
| 用户可编辑 parser `.so` 被加载 | `parser` 仅是 tree-sitter CLI 元数据；代码图忽略该字段、不 `dlopen`；语法解析只用 Python grammar 包 |
| 跨 profile 数据泄漏 | `GraphClient` 按 `hermes_home_key` 注册；两个临时 `HERMES_HOME` 隔离测试；禁止模块级单例 |
| 工具面中途变化破坏 prompt cache | 接受：extras 已安装时 `code_intel` 从会话开始就是初始快照；会话内新装 extras 需 `reprobe_tool_availability()`（显式 reload）或重启，才产生一次整段重预填；`code_intel` 是单工具，extras 安装是低频事件 |
| 索引过期被当成当前事实 | 每个结果带 `revision_id`/`snapshot_hash`/`stale`/`freshness`；`strict` 在 stale 时拒绝 |
| 模型误读/伪造分析事实 | 拒绝 `source_tool="model_observation"`；`excerpt_hash`/span 必须匹配当前文件；低优先级不能覆盖高优先级；全部写入进 `CodeAnalysisRun` |
| 补丁与后续全量索引冲突 | 全量 index/`refresh_file` 的解析结果胜出，冲突补丁证据 `invalidated` 并写 `CodeDiagnostic` |
| 并发写或部分提交 | 单文件/单批一事务、`GraphClient` 写锁与超时、失败整体回滚；提交即递增 scope 版本 |
| `patched` 被误当完整历史 | `patched` 仍带重算后的 `snapshot_hash`，但 metadata 明示；全量索引后恢复 `complete` |
| 回合后任务与 gateway 争写/拖慢进程 | 最终回答后入队、single-flight、预算上限；`GraphLockedError` 跳过并下一回合重试；不阻塞回答 |
| 后台任务绑定错 profile | 调度时显式携带 `hermes_home_key`/repo scope，任务内不读 launch profile；两个 `HERMES_HOME` 隔离测试 |
| 每轮无变更仍付费 | 先做一次 `git status`/hash 比对；空变更集直接 no-op，不写 `CodeAnalysisRun`、不递增 scope 版本 |
| one-shot/cron 退出杀死后台任务 | `is_single_query_session` 走有界同步 flush（≤2s/≤20 文件）；超预算标 `truncated`，不影响退出码 |
| 无消费者的基础设施 | P1–P2 后若 CLI 无真实使用场景，按仓库规则停止/删除；不保留死代码 |

## 验收

core 测试必须用 `scripts/run_tests.sh` 与临时 `HERMES_HOME`，不得写 `~/.hermes/`。

1. **导入边界**：干净子进程 `import hermes_codegraph` 后断言 `tree_sitter`、`ladybug`、
   `networkx` 不在 `sys.modules`；公共签名/返回值不出现底层类型。
2. **Schema 注册**：首次 `index` 注册 `Code*`/`CODE_*` 类型到 `code` scope；重复注册幂等；
   六类图的节点/关系都在；不创建 SQLite 文件。
3. **Language Registry**：自动加载两个位置——包内
   `hermes_codegraph/adapters/languages/languages.yaml` 先加载，
   `get_hermes_home()/code-intelligence/tree-sitter/languages.yaml` 后加载；`languages` 是列表，
   每个 item 用 `language:` 区分；profile 中 `language` 已由内置 registry 加载的条目被跳过
   （不重复、不覆盖），`status`/`doctor` 报告 skipped 数；内置四种 descriptor 字段完整
   （grammar、tree_sitter_queries、kind_map、import_resolution、test_detection、capabilities、
   semantic_provider）且共享同一个通用抽取器；profile 新增语言缺 `extraction` 块或 grammar 未
   安装时，索引记为 `unsupported`，不产生猜测性事实；单文件内重复
   `language` id/alias/extension、未知字段、缺必填字段、类型错误、绝对/越界 query/parser 路径
   拒绝该文件；未知语言抛 `LanguageUnsupportedError`，没有隐式 fallback。
4. **按需 grammar**：`language list` 显示内置/可安装；`language install <lang>` 只调用
   `pm.ensure_import("codegraph-<lang>")`；缺安装时结构化报错并提供 install hint；不执行裸 pip。
   当前包内只有四个内置 descriptor，可安装列为空；新增按需语言必须提供 descriptor、
   `extraction` 与查询文件。
5. **多语言抽取**：Python/TypeScript/Go/Rust fixture 的符号 `kind`、`qualified_name`、容器、
   起止位置正确；同名函数/重载不合并；未知 AST 类型落到 `unknown` 且保留 `language_kind`。
6. **证据**：每条边至少一条 Evidence，含工具名、版本、文件、行列、`excerpt_hash`；证据不保存
   完整源码；坐标 1-based 一致。
7. **幂等与跨 Revision 复用**：同一仓库连续 `index` 两次，节点/边/证据计数与 `symbol_id`
   完全一致；只改一个文件时只有该文件来源的符号/证据/边被重写；同一内容跨 commit 的未变文件
   **不重新解析**（parser 调用计数不变），只更新 `indexed_revision`；上次解析失败的文件在内容
   变化时重试，内容回到成功版本时跳过。
8. **失败保留**：故意制造解析失败，旧符号仍在、`parse_status=failed`、`index_status=stale`，
   不产生空图；修复后恢复。
9. **删除语义**：删除文件后 File 软删、符号 `symbol_status=deleted`、写 `DELETED_IN`、该文件及
   符号的关联边/证据被清理、默认查询过滤；`prune --yes` 物理删除。
10. **符号移动**：跨文件 unique content_hash+signature 匹配复用 `symbol_id` 并写
    `CHANGED_IN(change_kind=moved)`；多候选时新建符号并写 Diagnostic，不建立错误 `ALIASES`。
11. **增量八规则**：逐条覆盖：重复索引幂等、删除清理、失败不覆盖、符号删除处理边、移动匹配、
    依赖变更重算模块边、分析器版本变化重索引、Revision 不一致拒绝混查；重索引后 File-sourced
    `DEFINES`/`IMPORTS`/`DECLARES` 无残留；索引进行中/失败时 `strict` 拒绝且不返回完整
    `snapshot_hash`，变更边只在最终事务写入。
12. **解析状态**：`inferred`/`resolved`/`ambiguous`/`unresolved` 可由 fixture 稳定复现；
    `strict` 只返回 resolved/verified(COVERS)；`balanced` 标注 inferred；`exploratory` 返回候选
    与未解析证据。
13. **Revision 与 Change Graph**：查询绑定 `revision_id`/`snapshot_hash`；HEAD 变化后
    `stale=true`/`head_changed`；`strict` 返回 `IndexStaleError`；`get_changes` 返回
    `CHANGED_IN`/`DELETED_IN`/`PARENT_OF`；脏工作树索引不写变更边/父边；历史完整图查询抛
    `RevisionUnavailableError`；`prune` 默认保留当前+50，先删变更/父边再删 Revision。
14. **影响与算法**：`get_callers/callees/path/impact/cycles/types` 的深度、节点数、结果上限生效；
    超限 `truncated=true`；算法按 `repository_id` 过滤，两个仓库不互相污染；结果排序确定；
    `CodeIntelligence.open(root=A)` 只返回 A 的符号，默认 root 解析到 cwd 的 Git 根。
15. **Test Graph**：pytest/TS/Go/Rust 测试 fixture 产生静态 `TESTS`；带 per-test context 的
    覆盖率导入只产生 `COVERS`（`verified` + 覆盖率证据），聚合报告返回
    `CoverageContextMissingError` 且不写边，`--revision` 不匹配返回 `RevisionMismatchError`；
    没有导入时 `get_coverage` 为空且不把 `TESTS` 当覆盖。
16. **LSP 语义增强**：monkeypatch `LSPService` 返回定义/引用，边升级为 `resolved` 且追加
    `lsp_*` 证据；目标冲突时旧 `inferred` 边被删除或证据 `invalidated`，不同目标不并存；
    多候选时每个候选一条 `ambiguous` 边；未信任仓库不启动非安全 server；LSP 缺席或请求超时时
    索引仍完成并保留纯语法事实，不把文件标失败。
17. **工具面**：`TOOLSETS["codegraph"]` 只含 `code_intel` 与 `code_graph_update`；两者都在
    `_HERMES_CORE_TOOLS` 中，所有 `hermes-*` bundle 携带；extras 未安装时
    `get_tool_definitions()` 与首条 API 请求系统提示逐字不变；extras 已安装且
    `code_intel.enabled=true`（默认）时默认工具面包含两者；同一会话内安装 extras 后必须经
    `reprobe_tool_availability()`（CLI/TUI/gateway 的 `/reload-mcp`）或重启才出现，仅回合边界刷新
    不得被断言为已出现；配置翻转（`code_intel.enabled`/`update`）在下次
    `get_tool_definitions()` 因 `config_version` 变化生效；已保存显式 `platform_toolsets`
    列表的 profile 在首次发布周期内通过 `_RECENTLY_SHIPPED_TOOLSETS` 获得 `codegraph`，用户取消后
    `known_builtin_toolsets` 阻止回填；`disabled_toolsets: [codegraph]` 能移除两者；
    `code_intel.update=false` 只隐藏 `code_graph_update`；`code_intel` 没有写入路径；
    `pm.available("graph")` 为真但 networkx 缺失时，算法查询返回结构化 `unavailable` 而只读查询
    仍可用。
18. **Profile 隔离**：两个临时 `HERMES_HOME` 各自索引同一路径仓库，A 的符号在 B 不可见；数据库
    路径都在各自 home 下；关闭 A 不影响 B。
19. **安全**：extras 已安装时，纯语法索引与查询不发起网络请求、不执行仓库代码；超大文件、二进制、
    越界符号链接、FIFO、`.env`/私钥文件被跳过；`--semantic` 的 LSP 启动/安装按 D6/D13 单独验收
    （未信任仓库、非 host 后端不启动），不混入“零网络/零执行”断言。
20. **CLI**：`index/status/language/find/symbol/refs/callers/callees/path/types/impact/deps/cycles/
    tests/coverage/diagnostics/changes/context/prune/doctor` 全路径 JSON 输出；错误走 stderr 且
    退出码非零；`prune` 无 `--yes` 拒绝；`diagnostics` 在 Semgrep 关闭/缺失时返回空并标注可用性。
21. **发布包**：wheel 含 `hermes_codegraph/`、内置 `languages.yaml` 与 `queries/**/*.scm`；
    `_BUILTIN_SUBCOMMANDS` 含 `"code"`；
    `hermes code --help` 不触发插件发现、不导入 extras；缺任一内置 grammar wheel 的目标上
    `pm.available("codegraph")` 为 false，不部分安装。
22. **基准**：P1/P2 记录 5k 文件仓库的索引吞吐、查询延迟、DB 大小；默认上限（20k 文件、
    128 MiB、1M 符号）在超限时明确失败，不 OOM。
23. **图更新工具**：`refresh_file` 对过期文件重解析并在提交后让下一次查询看到新事实；
    `apply_evidence` 缺证据、`source_tool="model_observation"`、`excerpt_hash` 不符、span 越界、
    目标符号不存在时全部拒绝且零写入；`expected_revision_id`/`expected_content_hash` 不匹配返回
    `GraphConflictError`；更高优先级覆盖并 `invalidated` 旧证据，同等/更低冲突标 `ambiguous` 且
    不覆盖；`invalidate` 删除已无有效证据的边但不删符号；写锁失败整体回滚；`patched` 被 `strict`
    接受且 `snapshot_hash` 非空；下一次全量 index 回收冲突补丁并写 `CodeDiagnostic`；
    `code_intel.update=false` 时 `code_graph_update` 不出现而 `code_intel` 不受影响。
24. **回合后增量更新**：顶层回合修改文件后，无需 CLI，下一次 `code_intel` 查询看到新事实；无变更时
    不产生 `CodeAnalysisRun`、不递增 scope 版本；`auto_update=false` 或 `code_intel.enabled=false`
    时不调度；delegated child 不触发；`GraphLockedError` 时回合正常返回且下一回合重试；超过预算
    的文件标 `stale`、仓库标 `partial`，`strict` 拒绝直到重试或全量 index；脏工作树不写
    `PARENT_OF`/`CHANGED_IN`/`DELETED_IN`；任务绑定发起回合的 profile（两个 `HERMES_HOME` 隔离）。
25. **触发线程与进程形态**：`run_conversation` 的调度点内不调用 `git status`/不打开图（monkeypatch
    探测函数断言未被调用），全部探测在后台任务；`codex_app_server`、脚本 prelude、preflight 的
    compression deferred/exhausted、runaway-repetition 等 early-return envelope 同样触发**一次**
    （用结果字典断言不重复）；interrupted 但已交付最终文本的回合触发，纯 interrupted 无文本不触发；
    未索引仓库（无 `CodeRepository`）no-op 且不创建空库；`is_single_query_session` 的 one-shot/cron
    在 `run_conversation` 返回前执行有界同步 flush，超预算写
    `CodeAnalysisRun(status="truncated")` 且不改退出码；background review / side-question 的 fork
    （`_turn_origin`）、curator（`_memory_write_origin="background_review"`，`_turn_origin` 为空）与
    delegated child 不触发。
26. **配置域与危险模式诊断**：`config.yaml` 只有 `code_intel` 域，不新增 `codegraph` 配置键；
    `code_intel.enabled=false` 时模型工具不注册、post-turn 不调度、LSP/Semgrep 不运行，CLI 仍可用
    并在 `status`/`doctor` 报告 `enabled=false`；`code_intel.semgrep.enabled=false` 时 Semgrep
    不运行且语法/语义索引不受影响；开启但 semgrep 缺失/平台不支持时索引成功、
    `diagnostics_available=false` 并给 `hermes pm install semgrep` hint；Semgrep 只调用
    `pm.env_for("semgrep")` 提供的 CLI；`mode=auto`（默认）使用 `--config auto` 的 registry
    推荐规则，`mode=custom_dir` 使用 `--config <HERMES_HOME>/code-intelligence/semgrep/`；
    两种模式都带 `--metrics=off`，禁止 `semgrep ci`/`--autofix`；`custom_dir` 离线，目录缺失/为空
    时跳过诊断；`mode` 其他值配置校验失败；post-turn 与 `code_graph_update` 固定
    `semgrep=false`，后台不拉取规则；finding 只产生 `CodeDiagnostic`，不产生
    Symbol/Call/Type 边；`get_diagnostics` 与 `hermes code diagnostics` 返回统一信封并绑定
    `revision_id`/`snapshot_hash`。

## 实现落点

本 ADR **修改 Hermes core**，不创建独立仓库：

```
hermes-agent/
├── hermes_codegraph/            # 新 core 子系统
│   ├── __init__.py              # 只导出公共类型
│   ├── api.py                   # CodeIntelligence facade
│   ├── models.py                # ACIE 公共实体、CodeQueryResult、SymbolLocator
│   ├── config.py                # CodeGraphLimits、exclude 规则
│   ├── exceptions.py            # CodeGraphError 体系
│   ├── schema.py                # Code* / CODE_* 类型注册与版本
│   ├── languages.py             # LanguageRegistry、内置+profile registry 合并/去重/校验
│   ├── scanner.py               # git/文件遍历、hash、排除
│   ├── indexer.py               # 按文件事务、批量提交、删除/失败语义
│   ├── resolver.py              # imports/作用域/测试关联/类型关系
│   ├── change.py                # Revision、CHANGED_IN/DELETED_IN、移动匹配
│   ├── coverage.py              # 覆盖率解析与 COVERS
│   ├── context.py               # build_context、token 预算
│   ├── query.py                 # 查询服务、置信策略、上限
│   ├── semantic.py              # agent/lsp 复用与证据合并
│   ├── update.py                # P5：refresh_file/apply_evidence/invalidate、冲突与审计
│   ├── auto_update.py           # P6：变更探测、single-flight、预算、post-turn 调度
│   └── adapters/
│       ├── languages/           # languages.yaml + queries/**/*.scm + extractor.py (single engine)
│       └── analysis/            # ast-grep / semgrep / codeql 可选适配
├── hermes_graph/services/edges.py   # 加法：upsert
├── hermes_graph/services/index.py   # 加法：snapshot filters
├── hermes_graph/services/algorithms.py  # 加法：filters 透传
├── agent/lsp/manager.py         # 加法：definition/references/implementation/documentSymbols
├── agent/conversation_loop.py   # P6：run_conversation 单出口延迟导入并调度 post-turn reconciler
├── hermes_cli/subcommands/code.py   # `hermes code` 子命令
├── hermes_cli/main.py           # _build_cli_parser 注入 + _BUILTIN_SUBCOMMANDS 加 "code"
├── hermes_cli/tools_config.py   # CONFIGURABLE_TOOLSETS 加 codegraph；首次发布加 _RECENTLY_SHIPPED_TOOLSETS
├── hermes_cli/config_defaults.py    # code_intel 配置节（enabled + semgrep.enabled）
├── pm/extras.py                 # codegraph + 按需语言 anchor
├── tools/code_intel_tool.py     # P4：只读、服务门控；registry.register(toolset="codegraph")
├── tools/code_graph_update_tool.py  # P5：证据约束的图回写；registry.register(toolset="codegraph")
├── toolsets.py                  # TOOLSETS["codegraph"]=两工具；两者进 _HERMES_CORE_TOOLS
├── pyproject.toml               # [codegraph] + 按需语言 extras + packages.find include
├── uv.lock                      # `hermes pm lock`
├── docs/adr/ADR-8-code-intelligence.md
├── tests/hermes_codegraph/      # core 测试
└── website/docs/                # 用户文档（CLI/API）
```

对接 Hermes 时只使用现有缝：

| 面 | 现有接口 | 用法 |
|---|---|---|
| 存储/事务/算法 | `hermes_graph.GraphClient` | 复用；不 import `ladybug`/`networkx` |
| Schema | `graph.schema.register_node/register_edge` | 注册 `Code*`/`CODE_*`；加法迁移 |
| 语义 | `agent.lsp.manager.LSPService` | 新增窄查询方法；沿用 trust/install/idle |
| 语法搜索 | `pm/code_intelligence_packages.py` 的 ast-grep | 可选；只作证据/诊断，不建边 |
| 风险诊断 | `pm/code_intelligence_packages.py` 的 Semgrep | `mode=auto` 用 registry 推荐规则，`mode=custom_dir` 用 profile 目录；受 `code_intel.semgrep.enabled` 控制；只写 `CodeDiagnostic` |
| 依赖 | `[codegraph]` + 按需语言 extras + `pm.available`/`ensure_import` | 精确 pin；status 只探测 |
| 打包 | `packages.find.include` + `package-data` | 加 `hermes_codegraph`/`hermes_codegraph.*`、内置 `languages.yaml` 与 `queries/**/*.scm` |
| CLI | `hermes_cli/subcommands/code.py` + `main._build_cli_parser()` + `_BUILTIN_SUBCOMMANDS` | 延迟导入，快路径 |
| 配置 | `hermes_cli/config_defaults.py` 的 `code_intel` | 总开关 + `semgrep.enabled`；无新环境变量 |
| 模型工具 | `tools/registry.py` 的 `check_fn` | P4/P5 两个工具：只读查询 + 证据约束回写；默认开启、按 extras 门控 |
| 图回写 | `hermes_codegraph/indexer.py` + `CodeEvidence` + `edges.upsert` | 单文件事务、证据校验、乐观并发（D15） |
| 回合触发 | `agent/conversation_loop.py::run_conversation` | P6 在单出口延迟导入 `schedule_codegraph_post_turn`；后台 single-flight，不阻塞回答 |
| 数据目录 | `get_hermes_home()` | 沿用 `graph/graph.lbdb`；不新增路径 |
| 测试 | `scripts/run_tests.sh`、临时 `HERMES_HOME` | 不写真实 profile |

## 未决问题（不在本 ADR 内）

1. **历史完整图快照**：当前图只保留最新 Revision；真正的历史 Symbol/Call 全图查询需要存储增长
   与快照格式，另立 ADR。Change Graph 只提供差异边。
2. **运行时追踪**：除覆盖率外的动态调用目标、`verified` 扩展；涉及插桩、隐私和开销。
3. **SCIP 适配器**：ACIE 草案明确 v1 不作为依赖；未来可作为独立 `SourceTool` 提供更高质量证据。
4. **按需语言清单**：P2 固定首批额外语言与精确 pin；每个语言需要完整 descriptor 和 wheel 验证。
5. **仓库级 scope 版本**：当前 `code` scope 全局失效；若 Snapshot 成本成为问题，先改 Schema
   Registry 的 scope 粒度。
6. **自动索引时机**：post-write hook、watch 守护进程、cronjob；都需要解决写锁、延迟与
   prompt cache 边界。
7. **跨仓库/单体大库合并图**：当前按仓库过滤；跨仓库依赖和 monorepo 分区需要新的身份模型。
8. **Desktop/TUI 可视化**：代码图浏览器、调用图布局；属于 UI 决策，不在 core。
9. **向量/混合检索**：符号语义搜索与 `build_context` 排序；与现有记忆检索策略可能重叠，另立
   ADR。
10. **`gitnexus-explorer` 的存续**：本 ADR 不迁移、不删除；若代码图 CLI/查询覆盖其使用场景，
    再由 skill 层决定。
