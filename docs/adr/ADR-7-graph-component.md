# ADR-7：Hermes 核心图子系统（LadybugDB + NetworkX 的统一 Graph API）

**状态**：已决定（2026-11-02）。P1–P3 已实现，P4 未开始。分阶段：**P1** core 包 `hermes_graph/`、Schema
Registry、CRUD、事务、全局 ID 目录、可选 `[graph]` extra；**P2** 快照、版本失效与 NetworkX 算法；
**P3** `hermes graph` CLI 与文档；**P4**（另立 ADR）记忆/代码图/Advisor 等组件接入。**本 ADR 的
v1 不接入任何其他 Hermes 组件。**

**关联**：根 `AGENTS.md`（窄腰、profile scope、prompt cache 不变量）；`hermes_cli/AGENTS.md`
（core CLI 子命令）；`pm/AGENTS.md` 与 `pyproject.toml`（可选 extra、精确 pin）；
`hermes_constants.py`（`get_hermes_home()`）；`tools/registry.py`（本 ADR **不**注册工具）；
`agent/memory_manager.py`、`agent/memory_provider.py`、`agent/turn_context.py`（本 ADR **不**接入，
仅作为未来集成约束）；`agent/learning_graph.py`（现有只读学习图投影，不替换）；
`scripts/run_tests.sh`、`tests/AGENTS.md`（core 测试）。

**外部依据**：Ladybug 文档 [Connections & Concurrency](https://docs.ladybugdb.com/concurrency)、
[Transactions](https://docs.ladybugdb.com/cypher/transaction)、[Indexes](https://docs.ladybugdb.com/cypher/indexes)；
PyPI `ladybug` 元数据（MIT，`>=3.10,<3.15`，0.22.1 提供 cp310–cp314 的 Windows/macOS/Linux wheel）；
NetworkX 官方文档。

**范围**：Hermes core 内的图存储/查询/遍历/算法子系统，以及它的 Python API、CLI、依赖、profile
生命周期和失败策略。v1 不接入 Memory、Knowledge、Code Graph、Advisor、`learning_graph.py`，不注册
模型工具，不提供 `MemoryProvider`，不新增 hook。消息平台、Desktop/TUI 图可视化、云端多租户、
跨进程写代理、自动 LLM 抽取都不在本 ADR 的实现范围内；它们各自需要独立的决策记录。

**本 ADR 不是**：一个插件或插件目录条目；一个 `MemoryProvider`；一个模型工具；一次现有组件的
迁移；给 `agent/learning_graph.py` 换后端。core 代码 + CLI 是 v1 的全部交付面。

## 问题

Hermes 现在没有一个统一的“图”能力，相关需求散落在四类东西上：

| 需求 | 现状 | 缺口 |
|---|---|---|
| 记忆 | 内置 `MEMORY.md` / `USER.md`；外部记忆提供者通过 `MemoryProvider` 单选接入（`memory.provider`） | 没有跨记忆条目的关系查询；每个提供者自己维护后端 |
| 知识图谱 | 插件 catalog 里的 Hindsight/Cognee/MindGraph 等各自持有图或向量后端 | 没有共享的 Schema、事务、遍历和算法 API；换后端要改调用方 |
| 代码图 | 可选技能 `gitnexus-explorer` 提供临时索引/可视化；core 无持久调用图 | 没有可供其它组件复用的 `Symbol` / `CALLS` 存储与查询 |
| 学习图 | `agent/learning_graph.py` 从技能与 `MEMORY.md` **只读投影**出节点/边，供 `/journey` 与 Desktop starmap | 不是持久图，不能写入、遍历、做算法，也不是业务 API |

**所有权与非重叠**：本 ADR 不替换上表任何现有面。`agent/learning_graph.py` 继续是只读投影；
Hindsight/Cognee/MindGraph 等继续是各自的外部记忆提供者，图组件不自动接管或迁移它们的数据；
`gitnexus-explorer` 继续是可选技能，图组件只提供可选的持久 `Symbol`/`CALLS` 适配层。现有后端要
把数据写进图，必须显式调用 SDK/CLI；没有隐式双写，也没有“安装即迁移”。替换 `learning_graph.py`
或迁移某个提供者，都需要独立 ADR。

如果让 Memory、Knowledge、Code Graph、Advisor 各自直接调用 LadybugDB 或 NetworkX，会立刻产生四个
耦合点：连接与锁、Schema 迁移、事务边界、快照失效。任何一处换实现都要改所有调用方；而且 NetworkX
的 `Graph` 对象一旦外泄，调用方会开始依赖“当前进程里的那份内存图”，与持久化事实源分叉。

同时，Hermes 有三条硬约束，决定了图组件**即使作为 core 子系统，也不能进入默认请求路径**：

1. **窄腰**：每个模型工具都进入每次 API 请求。用户决定图能力是 core 子系统；因此本 ADR 把它做成
   core 代码 + 可选 `[graph]` extra，不注册模型工具、不挂 hook、不默认安装。它不是每个会话都
   需要的基座。
2. **一个进程服务多个 profile**：`get_hermes_home()` 是 profile-aware；任何模块级单例、连接池、
   缓存都必须按 `hermes_home_key()` 绑定，否则会把 A profile 的图写进 B profile。
3. **prompt cache 不可破坏**：v1 不做图召回；未来接入只能作为当前回合的追加内容
   （`prefetch` → `api_content`），不能改系统提示、不能中途换工具面、不能重写历史消息。

Ladybug 自身的并发模型又加了一条：**一个数据库文件同时只能有一个 `READ_WRITE` `Database` 对象**
（同进程或跨进程都一样）；多个连接可以并发，写事务在系统内串行。把“打开数据库”做成随手可调的
全局函数，必然在 gateway 的多 profile/多进程拓扑里撞锁。

## 决策

### D1. 形态：core 子系统，v1 不接入其他组件

图能力是 **Hermes core 的子系统**：代码在仓库根 `hermes_graph/`，Python API 为
`from hermes_graph import GraphClient`，用户面是 `hermes graph ...` CLI。它不是插件、不进入
`plugins/`、不发布独立 entry point、不进入 `plugin-catalog/`。

v1 的消费者只有：

- `hermes graph` CLI；
- core 测试；
- 未来的集成 ADR（记忆、代码图、Advisor 等）——**不在本 ADR 实现**。Python API 是给这些未来
  接入保留的稳定契约，v1 没有其他 core 组件调用它。

明确不接入：`agent/memory_manager.py`、`agent/memory_provider.py`、`agent/advisor*`、
`agent/learning_graph.py`、`tools/registry.py`、`toolsets.py`、任何 gateway hook。v1 不注册模型
工具、不实现 `MemoryProvider`、不新增 hook、不改系统提示或工具面。这是用户决定：先把 core 子系统
和 CLI 做出来，其他组件暂不接入。

```
hermes graph CLI / tests（v1；未来 core 调用者）
                    │
                    ▼
        GraphClient（公共契约、类型、异常、分页、事务）
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
 LadybugAdapter            NetworkXEngine
（唯一事实源）             （只读派生快照）
        │                       │
   graph.lbdb              nx.MultiDiGraph
```

依赖形态：`ladybug`/`networkx` 放在 core 的可选 extra `[graph]`，**不在每次会话默认安装/导入**。
`hermes graph status`/`doctor` 只用 `pm.available("graph")` 探测；数据命令在打开数据库前调用
`pm.ensure_import("graph")`，成功后再导入适配器。`import hermes_graph` 本身不导入底层库。

理由：用户要求 core 子系统；同时用“可选 extra + 无默认工具/hook”避免把重依赖塞进每次请求路径。
如果 P1–P3 后没有任何真实消费者（CLI 也没有使用场景），按仓库“无消费者的基础设施不保留”的规则，
应停止实现或删除，而不是留下死代码。

**不采用** `local_graph` 作为导入名：它过于通用，容易与用户环境里的同名包冲突。保留草案里的
`GraphClient`、命名空间和数据模型，只改发行名/导入名。

### D2. 公共 API：Facade + Repository + Adapter，公共类型不外泄底层对象

```python
from hermes_graph import GraphClient

graph = GraphClient.open()          # 默认 <HERMES_HOME>/graph/graph.lbdb
graph.capabilities()                # 由实际注册的 adapter 动态生成
graph.health()

graph.nodes.create(type="Memory", properties={...})
graph.nodes.get(node_id)
graph.nodes.update(node_id, properties={...})
graph.nodes.delete(node_id, hard=False)
graph.nodes.upsert(type="Entity", key="entity:python", properties={...})
graph.nodes.search(type="Entity", filters={"entity_type": "Language"}, limit=20)

graph.edges.create(source_id=..., target_id=..., type="MENTIONS", properties={...})
graph.edges.get(edge_id)
graph.edges.delete(edge_id)
graph.edges.list(source_id=..., type="MENTIONS", limit=100)

graph.query.cypher("MATCH (m:Memory) WHERE m.content CONTAINS $q RETURN m LIMIT $n",
                   {"q": "ladybug", "n": 20})
graph.query.find(type="Memory", filters={"memory_type": "semantic"})
graph.query.neighbors(node_id, direction="both", depth=1)

graph.traverse.bfs(start_id=..., max_depth=2, relation_types=["MENTIONS"])
graph.traverse.dfs(...)
graph.traverse.shortest_path(source_id=..., target_id=..., weight=None)
graph.traverse.subgraph(node_ids=[...], max_depth=1)

graph.algorithms.degree(scope="knowledge")
graph.algorithms.centrality(method="pagerank", scope="knowledge", weight="confidence")
graph.algorithms.community(method="louvain", scope="knowledge")
graph.algorithms.connected_components(scope="code")
graph.algorithms.topological_sort(scope="code")

snap = graph.index.snapshot(scope="knowledge", node_types=["Entity"],
                            edge_types=["RELATES_TO"])
graph.index.invalidate(scope="knowledge")

with graph.transaction.begin() as tx:
    entity = tx.nodes.upsert(type="Entity", key="entity:ladybug", properties={...})
    memory = tx.nodes.create(type="Memory", properties={...})
    tx.edges.create(source_id=memory.id, target_id=entity.id, type="MENTIONS")
```

公共类型只有 `Node`、`Edge`、`QueryResult`、`GraphPath`、`GraphSnapshot`、`Capabilities`、
`Health` 以及 `GraphError` 体系。**Ladybug 的 `Database`/`Connection`/`QueryResult` 与 NetworkX 的
`Graph`/`DiGraph` 不得出现在公共签名或返回值里**；它们只存在于 `adapters/` 内。调用方 `import
ladybug` / `import networkx` 视为契约破坏。

命名空间与职责：

| 命名空间 | 职责 | 是否模型可见 |
|---|---|---|
| `graph.nodes` / `graph.edges` | 节点、关系 CRUD 与 upsert | 否（SDK/CLI） |
| `graph.query` | 参数化查询、受限 Cypher、邻居 | 只读经 `graph` 工具 |
| `graph.traverse` | BFS/DFS/最短路/子图 | 只读经 `graph` 工具 |
| `graph.algorithms` | 中心性、社区、连通分量、拓扑排序 | 只读经 `graph` 工具 |
| `graph.index` | 快照、刷新、失效 | 否 |
| `graph.transaction` | 原生事务上下文 | 否 |
| `graph.schema` | Schema Registry 与迁移 | 否 |
| `graph.admin` | 统计、备份、健康检查、关闭 | 否 |

`graph.capabilities()` 返回**动态**能力，不硬编码：

```python
Capabilities(
    storage=("property_graph", "cypher", "transactions"),
    algorithms=("pagerank", "betweenness", "louvain", "shortest_path"),
    features=("snapshot", "scope_version", "profile_scoped"),
)
```

如果 `networkx` 未安装，`storage` 与 `features` 仍然可用，`algorithms` 为空；`graph.algorithms.*`
抛 `AlgorithmUnavailableError`，而不是悄悄返回空结果。

### D3. 数据模型与 ID：稳定、可合并、可幂等

```python
@dataclass(frozen=True)
class Node:
    id: str
    type: str
    properties: dict[str, Any] = field(default_factory=dict)
    key: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None

@dataclass(frozen=True)
class Edge:
    id: str
    source_id: str
    target_id: str
    type: str
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None

@dataclass(frozen=True)
class QueryResult:
    items: list[Node | Edge | dict[str, Any]]
    total: int | None = None
    has_more: bool = False
    next_cursor: str | None = None
    execution_time_ms: float | None = None

@dataclass(frozen=True)
class GraphPath:
    nodes: list[Node]
    edges: list[Edge]
    length: int
    weight: float | None = None

@dataclass(frozen=True)
class GraphSnapshot:
    nodes: list[Node]
    edges: list[Edge]
    scope: str
    version: int
    created_at: datetime
```

ID 规则：

- `create()` 未给 `id` 时生成 UUIDv7（时间有序，便于范围扫描与分页）；
- `upsert(type, key, …)` 使用 `UUIDv5(NAMESPACE, f"hermes_graph:v1:{type}:{key}")`，用主键唯一性实现
  `(type, key)` 幂等。Ladybug 当前文档只保证主键索引/唯一性，二级索引支持与唯一约束属于 P1 能力
  门，因此幂等性不建立在二级索引上。`key` 不可变，改 key 等于新建节点；
- `id` 与 `key` 互斥：`upsert` 必须给 `key`；`create` 可给 `id`，不得同时给 `key`；
- 节点类型 PascalCase，关系类型 `UPPER_SNAKE_CASE`；时间统一 UTC ISO 8601；
- `properties` 必须是 JSON 可序列化值；拒绝 `bytes`、NaN/Infinity、循环引用；单个节点/关系的
  `properties` 序列化后上限默认 256 KiB（可配置），避免一行撑爆 WAL。

删除语义：

- `delete(id, hard=False)` 写 `deleted_at` 并删除该节点的邻接边（边是关系事实，不能挂在已删除的
  节点上）；所有 SDK 读路径默认过滤 `deleted_at IS NULL`；
- `delete(id, hard=True)` 物理删除节点、邻接边和 `GraphNodeRef`；用于用户明确的“清除数据”；
- 软删节点再次 `upsert(type, key, …)` 时默认复活：清除 `deleted_at` 并更新属性；hard 删除后
  `upsert` 会用同一个派生 ID 重新创建节点。两种行为都必须在测试里锁定，避免“删了又写”产生
  两个身份；
- `query.cypher()` 不自动加过滤条件，调用方要显式写 `WHERE n.deleted_at IS NULL`；文档必须写明，
  避免原始查询把软删节点又捞出来。

`Node.key` 是“业务身份”，`Node.id` 是“存储身份”。业务组件用 `upsert(type, key, …)` 表达
“同一实体再写一次”；用 `create()` 表达“每次都是一条新记录”。

### D4. 物理 Schema：每类型一张表，Registry 编译 DDL

Ladybug 是**结构化 property graph**：节点表、关系表、属性类型必须先定义。它自动为主键建 HASH
索引，文档也给出 HASH/ART 索引 DDL；但该页明确围绕**主键索引**（“automatically creates a primary
key index”、每张节点表只能有一个主键索引），任意二级属性索引不在当前文档保证范围内。因此：

- **v1 不依赖二级属性索引**；把“非主键属性索引”列为 P1 能力门，P1 实测确认后才以加法迁移引入。
- 不能把 `Node(type, properties_json)` 塞进一张通用表了事：那样失去类型列，也没有可挂主键索引的
  具体节点属性，过滤只能扫 JSON。

决策：

1. `graph.schema.register_node(NodeType(...))` / `register_edge(EdgeType(...))` 把声明写入
   `GraphSchema` 表，并编译为 `CREATE NODE TABLE` / `CREATE REL TABLE` DDL。
2. 每个节点表至少有：`id STRING PRIMARY KEY`、`key STRING`、`created_at STRING`、
   `updated_at STRING`、`deleted_at STRING`、`props STRING`（未声明属性的 JSON）。声明为可查询/
   可过滤的字段生成独立的类型化列；`properties` 仍以统一 dict 暴露。
3. v1 **不提供 `NodeType.indexes`**，也不声明任意属性索引。主键只有 `id`；`upsert` 直接从
   `(type, key)` 派生 `id`，不需要按 `key` 查找。P1 用 `EXPLAIN` 或等价手段验证 Ladybug 是否支持
   非主键属性索引；确认支持后，再以加法迁移加入索引声明。Registry 不生成未经文档确认的索引 DDL。
4. 每个关系表声明 `FROM <SourceType> TO <TargetType>`；API 在写边前校验端点类型。跨域关系由
   Schema 显式声明，例如 `MENTIONS FROM Memory TO Entity`。
5. 内部保留表：`GraphNodeRef(id PRIMARY KEY, type)`、`GraphEdgeRef(id PRIMARY KEY, type)`、
   `GraphScopeVersion(scope PRIMARY KEY, version INT64)`、`GraphSchema(name PRIMARY KEY, …)`。
   `GraphNodeRef` 让 `get(id)`/`update(id)`/`delete(id)` 无需全表扫描即可定位类型；它必须与节点
   写入在同一事务内更新。
6. 类型名在 profile 数据库内全局唯一；注册时记录 `owner`。冲突直接 `SchemaError`，不做隐式改名。
7. Schema 版本随 core 子系统版本走。v1 只允许**加法迁移**（新增类型、新增可空列）；P1 验证通过后才允许
   “新增索引”；删除/改类型必须走 `hermes graph schema migrate --destructive` 并先
   `admin.backup()`。没有自动破坏性迁移。
8. `search(type, filters)` 对 `id` 走主键查找；精确 `(type, key)` 过滤可先派生 UUIDv5 `id` 再走
   主键，任意 `key` 前缀/模式匹配仍在单类型表内做有界扫描。`upsert` 不依赖 `key` 查找，因为
   `id` 已由 `(type, key)` 派生。文档必须写明“未声明索引的字段会扫描”，避免调用方按“索引一定
   存在”设计。

示例 DDL：

```cypher
CREATE NODE TABLE Memory(
  id STRING PRIMARY KEY, key STRING, created_at STRING, updated_at STRING,
  deleted_at STRING, props STRING, content STRING, memory_type STRING
);
CREATE NODE TABLE Entity(
  id STRING PRIMARY KEY, key STRING, created_at STRING, updated_at STRING,
  deleted_at STRING, props STRING, name STRING, entity_type STRING
);
CREATE REL TABLE MENTIONS(
  FROM Memory TO Entity, id STRING, created_at STRING, props STRING, confidence DOUBLE
);
```

### D5. 事务：委托 Ladybug 原生事务，不模拟

`with graph.transaction.begin() as tx:` 直接执行 `BEGIN TRANSACTION`，正常退出 `COMMIT`，异常
`ROLLBACK`。**不得**用 Python 上下文管理器“收集操作后批量重放”来伪造事务；Ladybug 本身是
ACID、可串行化的（见 [Transactions](https://docs.ladybugdb.com/cypher/transaction)）。

约束：

- 事务对象暴露同样的 `tx.nodes` / `tx.edges` / `tx.query`，但全部绑定到同一个 `Connection`；
- 嵌套事务直接 `TransactionError`；
- Ladybug 同一时刻只允许一个写事务。进程内用一把锁串行化写事务，超时（默认 5s）返回
  `TransactionError("write_busy")`；读事务使用独立连接，可并发；
- 写事务提交时，在同一事务内递增所有受影响 scope 的 `GraphScopeVersion`；回滚不递增；
- 每个节点/关系类型在注册时声明 `scope`；写事务只递增声明过的 scope。未声明 scope 的类型拒绝
  写入（`SchemaError`），避免出现“数据变了但快照不知道”的路径；
- 一个写事务可以同时影响 `memory` 与 `knowledge` 两个 scope，必须同时递增；缺一个就会让快照
  继续使用旧版本；
- `graph.transaction.begin(read_only=True)` 映射为 `BEGIN TRANSACTION READ ONLY`，用于需要
  “多次读取之间数据库不变”的分析；
- 事务内不做网络调用、LLM 调用或长时间 CPU 计算；工具处理函数不在事件循环线程里做阻塞等待。

### D6. 生命周期：按 profile、按文件、每进程单写者

路径与注册表：

- 固定数据库路径：`get_hermes_home() / "graph" / "graph.lbdb"`。`get_hermes_home()` 每次调用都解析
  活动 profile；**禁止**缓存成模块级常量，也禁止手写 `~/.hermes`。
- v1 **不提供可配置路径**。如果未来增加 `graph.database_filename`，也只能是单段文件名，必须由
  `get_hermes_home() / "graph"` 拼接，拒绝绝对路径、分隔符、`..` 与 Windows 驱动器/UNC 前缀。
  没有这个约束就会破坏 profile 隔离。
- 进程内 `GraphClient` 注册表键为 `(hermes_home_key(), resolved_db_path)`；同一文件只创建一个
  `Database` 对象，多个 `Connection` 按操作/线程创建。`Database` 共享，`Connection` 不跨线程复用。
- `GraphClient.open(read_only=False)` 在拿不到写锁时抛 `GraphLockedError`，消息必须给出可执行的
  处置：关闭占用进程、通过占用者的 CLI/RPC 读取、或等待；**不得**静默降级为内存图、第二个数据库，
  也不建议另开 `READ_ONLY` 进程绕过。
- v1 **不实现跨进程写代理**。一个数据库文件只有一个写者：通常是长期运行的 gateway/CLI 进程；
  其它进程不得同时打开同一文件的 `READ_WRITE`；需要读时走拥有者，而不是自己开 `READ_ONLY`
  （Ladybug 文档明确说不建议 RW 与 RO 进程同时打开同一文件）。
- profile 驱逐/关闭时调用 `graph.admin.close()`；注册表提供 `close_idle(ttl)`。进程退出时
  `atexit` 兜底关闭。
- 目录权限尽力设为 0700（Windows 使用默认 ACL），日志中不打印节点内容。

这解决了“一个 gateway 进程服务多个 profile”的泄漏路径：`GraphClient` 不是全局单例，而是
`(home, path)` 的受控对象；每个 profile 只看到自己的 `graph.lbdb`。

### D7. 快照与算法：Ladybug 是事实源，NetworkX 是临时计算

- `snapshot(scope, node_types=None, edge_types=None, use_cache=True)` 从 Ladybug 读取指定 scope 的
  节点/边，构建 `nx.MultiDiGraph`；节点属性带 `node_type`，边带 `relation`，边 key 使用 `edge.id`。
  使用 `MultiDiGraph` 是因为同一对节点之间可能同时存在 `CALLS`、`REFERENCES`、`DEPENDS_ON` 等
  多条关系。快照只包含该 scope 声明的用户类型；`GraphNodeRef`、`GraphScopeVersion` 等内部表
  永不进入快照，除非调用方显式点名（不推荐）。
- `GraphSnapshot.version` 来自 `GraphScopeVersion`；缓存键为 `(db_path, scope, version)`。版本不一致
  自动重建；`graph.index.invalidate(scope)` 只删缓存，不改数据库。
- 缓存有上限：按 scope 限制节点数、边数和估算字节数（默认 100k 节点 / 500k 边，可配置）。超过上限
  抛 `SnapshotTooLargeError`，**不**尝试 OOM 后失败。
- 算法在调用线程执行；CLI 同步执行；未来模型工具接入时用 `execution_timeout` 与线程池隔离，不得
  在事件循环上阻塞。
- v1 算法与 NetworkX 映射：

| API | NetworkX | 返回 |
|---|---|---|
| `degree(scope)` | `degree_centrality` | `dict[str, float]` |
| `centrality(method="pagerank")` | `pagerank` | `dict[str, float]` |
| `centrality(method="betweenness")` | `betweenness_centrality` | `dict[str, float]` |
| `centrality(method="closeness")` | `closeness_centrality` | `dict[str, float]` |
| `community(method="louvain")` | `louvain_communities(G.to_undirected(), seed=0)` | `dict[str, int]` |
| `connected_components(scope)` | `weakly_connected_components` / `connected_components` | `list[list[str]]` |
| `shortest_path(source, target)` | `shortest_path` | `GraphPath \| None` |
| `topological_sort(scope)` | `topological_sort` | `list[str]`；有环抛 `AlgorithmError` |

- 结果按节点 ID 排序后再返回，Louvain 固定 `seed=0`，保证同一快照同一结果；否则测试和 UI 会漂移。
- `pagerank` 优先调用 `nx.pagerank`；若环境没有 scipy（NetworkX 的实现依赖它），回退到同一公式的
  纯 Python 幂迭代，不因此禁用算法。
- 算法**永不写 Ladybug**，也**不维护一份长期存活的 NetworkX 图**。每次分析都从快照来；写事务只会
  让缓存失效，不会反向同步 NetworkX。

### D8. 集成边界：v1 不接入其他 Hermes 组件

v1 不实现 `MemoryProvider`、不注册模型工具、不挂 hook、不改系统提示或工具面。具体：

- **Memory**：不 import `agent.memory_manager` / `agent.memory_provider`，不实现 `prefetch`、
  `sync_turn`、`on_memory_write`；`MEMORY.md` / `USER.md` 与外部记忆提供者完全不变。
- **Code Graph / Advisor / learning_graph**：不 import `agent.advisor*`、`agent.learning_graph`，
  不写它们的存储，也不替换它们的投影。
- **工具面**：不 import `tools.registry`，不注册 `graph` 工具，不改 `_HERMES_CORE_TOOLS` /
  `toolsets.py`；模型看不到图能力。
- **消费者**：v1 只有 CLI、Python API 的直接调用者和测试。没有隐式双写、没有“安装即迁移”。

未来接入必须另立 ADR，并复用现有缝：记忆走 `MemoryProvider` 的 `prefetch`/`sync_turn`；模型面走
service-gated tool；任何接入都必须保持 profile scope 与 prompt-cache 不变量。v1 不做自动 LLM
实体抽取；抽取质量、成本、重试、隐私和记忆污染回滚都未定义。

### D9. 用户面：`hermes graph` CLI

v1 的用户面是 core CLI，不是模型工具：

| 子命令 | 作用 | 写入 |
|---|---|---|
| `hermes graph status` | 路径、版本、scope 版本、节点/边计数、锁状态 | 否 |
| `hermes graph schema list` | 列出已注册类型、scope、版本 | 否 |
| `hermes graph schema register --file schema.json` | 注册/迁移类型（加法） | 是 |
| `hermes graph node get <id>` / `search <type> --filter k=v` | 读取节点 | 否 |
| `hermes graph node upsert <type> <key> --file props.json` | 幂等写入 | 是 |
| `hermes graph node delete <id> [--hard]` | 软删/硬删；`--hard` 需 `--yes` | 是 |
| `hermes graph edge create ...` / `delete <id>` / `list` | 关系读写 | 是 |
| `hermes graph query "<cypher>" --param k=v` | 只读 Cypher，参数化 | 否 |
| `hermes graph neighbors <id>` / `path <src> <dst>` | 遍历 | 否 |
| `hermes graph analyze --method pagerank --scope knowledge` | 算法 | 否 |
| `hermes graph snapshot --scope knowledge` | 生成/检查快照版本 | 否 |
| `hermes graph backup` / `doctor` | 备份、只读诊断 | 否 |

- 命令输出 JSON 到 stdout；错误 JSON 到 stderr；非零退出码；
- `query` 强制 D10 的只读 allowlist；写操作只走类型化子命令，不提供写 Cypher；
- `--profile` 复用现有 CLI profile 选择，不新增环境变量；
- `status`/`doctor` 只调用 `pm.available("graph")` 探测，**不安装**；其它会在真正打开数据库前调用
  `pm.ensure_import("graph")`，失败时给 `pm.install_hint("graph")`。不自动 pip 安装；
- 不注册任何模型工具；agent 想用图，只能通过 skill/用户显式执行 CLI（该 skill 不在本 ADR 内）。

### D10. 查询与 Cypher：默认只读、参数化、有界

- `graph.query.cypher(statement, params, *, read_only=True, timeout_ms=2000, max_rows=100)`；
  v1 只接受单条只读语句。语句必须以 `MATCH` / `OPTIONAL MATCH` / `UNWIND` / `WITH` / `RETURN`
  开头；拒绝 `CREATE`、`MERGE`、`SET`、`DELETE`、`DETACH`、`COPY`、`CALL`、`LOAD`、`ATTACH`、
  `BEGIN`、`COMMIT`、`ROLLBACK`、`CHECKPOINT` 等。
- 只允许参数占位符，不做字符串插值；拒绝多语句（除尾部空语句）、注释、`;` 分隔的批处理。
  Cypher 解析不靠正则拼 SQL；实现用保守的 tokenizer + 首关键字 allowlist，无法判定即拒绝。
- `max_rows` / `timeout_ms` / 最大结果字节数三道上限；超限返回 `QueryLimitError`，不截断后假装
  完整。
- `query.find()` 由 Schema 编译为参数化 `WHERE`，是 CLI `node search` 与未来接入的唯一查询路径；
  `query.cypher()` 是高级 SDK/CLI 接口，不暴露给模型。
- 原始 Cypher 在 CLI 中默认 `--read-only`；写 Cypher 不提供。写操作只能走类型化 API，以便统一
  维护 `GraphNodeRef`、scope 版本和 Schema 校验。

### D11. 安全与隐私：本地、profile 隔离、内容不可信

- 图数据库只落本地 profile 目录；不联网、不遥测、不上传。子系统不得把图内容发给第三方服务。
- 节点/关系内容可能来自用户、模型、工具结果或外部文档，一律按**不可信数据**处理：v1 没有自动召回，
  也不把图内容拼进系统提示；未来接入记忆时必须走 `<memory-context>` 围栏，工具结果不是指令。
- 图写入前不复制一套出站脱敏逻辑，也不接受凭据；未来记忆接入由 `MemoryProvider` 路径负责脱敏。
  子系统不得绕过 `agent.secret_scope` 读取凭据。
- 跨 profile 读取是安全缺陷：`GraphClient` 的键必须包含 `hermes_home_key`；测试用两个临时
  `HERMES_HOME` 断言互不可见。
- `graph.admin.backup()` 停止新连接、排空事务、`CHECKPOINT`、关闭 `Database`、复制文件、再重开；
  不在数据库仍打开时直接复制。`graph.admin.stats()` 只返回计数/大小，不返回内容。删除/导出由 CLI
  显式执行。

### D12. 依赖、配置与发行

**代码在 core，依赖是 core 的可选 extra**，不进入每次会话的默认安装/导入：

```toml
[project.optional-dependencies]
graph = [
  "ladybug==0.22.1",
  "networkx==3.6.1",
]
```

- 两个都是精确 pin，符合 core 的依赖策略；P1 先验证 0.22.1，若需换版本只改这一处并运行
  `hermes pm lock` 更新 `uv.lock`。Android/Termux 没有 Ladybug wheel，`[tool.hermes.extras-platforms]`
把它标记为不可用。
- `ladybug`：MIT；2026-11-02 查询的 PyPI 0.22.1 提供 cp310–cp314 的 Windows `win_amd64`/
  `win_arm64`、macOS 15、Linux manylinux/musllinux wheel；`requires_python` 为 `>=3.10,<3.15`，覆盖
  Hermes 的 `>=3.11,<3.15`。最低平台是 macOS 15 / glibc 2.28；更老的系统按“不可用”处理，不源码
  构建。不要把文档 CLI 的 0.11.0 与 PyPI 的 0.22.x 当成同一版本（两者版本号并不同步）。
- `networkx`：纯 Python，精确 pin 3.6.1。3.7 的 `requires_python` 是 `>=3.12,!=3.14.1`，与
  Hermes 的 `>=3.11` 不兼容，因此不能沿用传递版本的 3.7；3.6.1 覆盖 3.11–3.14（3.14.1 除外）。算法不可用时
  CRUD/查询/遍历仍可用。PageRank 在缺少 scipy 时使用等价的纯 Python 幂迭代回退（同一 alpha、
  收敛阈值与确定性排序），避免把 scipy 拖进可选 extra。
- `status`/`doctor` 用 `pm.available("graph")` 探测，不触发安装；其它数据命令在打开数据库前用
  `pm.ensure_import("graph")`，失败时给 `pm.install_hint("graph")`。`import hermes_graph` 本身不导入
  `ladybug`/`networkx`。
- v1 **不提供 `graph.*` 配置节**；数据库路径固定为 `get_hermes_home() / "graph" / "graph.lbdb"`。
  未来若加配置，也必须限制在该目录内（见 D6）。
- `hermes graph` 由 `hermes_cli/subcommands/graph.py` 的 `build_graph_parser(subparsers, cmd_graph=...)`
  注册；在 `main._build_cli_parser()` 注入 handler，并把 `"graph"` 加进
  `main._BUILTIN_SUBCOMMANDS`（否则每次 `hermes graph` 先付 500–650ms 插件发现）。该模块只注册
  parser，处理函数内延迟导入 `hermes_graph`，所以 `hermes --help` 不触发 extra。
- `pyproject.toml` 的 `[tool.setuptools.packages.find].include` 是显式白名单；必须加入
  `"hermes_graph"` 与 `"hermes_graph.*"`，否则 editable 测试通过但发布 wheel 缺包。
- 没有 `plugin.yaml`、没有 entry point、没有 `plugins/` 目录、没有 `plugin-catalog/` 条目。

### D13. 失败策略：诚实降级，不伪造

| 失败 | 行为 |
|---|---|
| `[graph]` extra 未安装 | `status`/`doctor` 用 `pm.available` 报告 `unavailable`，不安装；数据命令用 `pm.ensure_import`，失败给 `install_hint`；`import hermes_graph` 仍可用；core 启动不受影响 |
| `ladybug` 导入失败 / 无本平台 wheel | `GraphUnavailableError`；CLI 说明原因；core 启动不受影响 |
| 数据库被另一个进程锁定 | `GraphLockedError`，列出路径与处置建议；不自动开第二个库 |
| `networkx` 未安装 / 导入失败 | `capabilities().algorithms == ()`；算法调用抛 `AlgorithmUnavailableError`；CRUD 正常 |
| 快照超限 / 算法超时 | `SnapshotTooLargeError` / `AlgorithmTimeoutError`；不返回部分结果冒充完整 |
| Schema 版本不兼容 | 拒绝打开并提示迁移命令；不自动删表 |
| 事务内异常 | `ROLLBACK`，scope 版本不变；异常重新抛出为 `TransactionError` 子类 |
| 数据库损坏 | 打开失败；`hermes graph doctor` 只读检查 + 备份/恢复指引；不做静默修复 |

### D14. 分阶段落地

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| **P1** | core 包 `hermes_graph/`：模型、异常、Schema Registry、LadybugAdapter、CRUD/upsert、全局 ID 目录、原生事务、`GraphClient.open` 锁与 profile 注册表；`[graph]` extra | 单元测试覆盖 CRUD/upsert 幂等/回滚/锁冲突；`import hermes_graph` 不导入 `ladybug`；Windows cp314 基础 CRUD 通过 |
| **P2** | `SnapshotBuilder`、`MultiDiGraph`、scope 版本、LRU 缓存、NetworkX 算法、上限与确定性 | 快照版本失效、回滚不失效、Louvain 固定 seed、超限拒绝；10 万节点/50 万边基准记录 |
| **P3** | `hermes graph` CLI（`status/schema/node/edge/query/neighbors/path/analyze/snapshot/backup/doctor`）、文档、只读/写入验收 | CLI 的 CRUD/查询/算法/备份路径全过；`query` 拒绝写入；模型工具面逐字不变 |
| **P4**（另立 ADR） | Memory/Code Graph/Advisor 等接入；跨进程写代理；FTS/向量；Desktop starmap | 不在本 ADR 实现；每项独立决策与验收 |

**P2 基准**（2026-11-02，Windows 11 x64 / CPython 3.11 / 本地 NVMe；数据用 `COPY FROM` 导入）：
100k 节点、500k 边时 `snapshot()` 3.9s、`nx_graph()` 1.1s、`degree()` 1.6s、`pagerank()`
1.9s（scipy 路径；无 scipy 环境会走纯 Python 幂迭代回退）。写入侧 500k 边的 `COPY FROM` 为 1.2s；走逐条
`CREATE ... MATCH` 的测试夹具明显更慢，但那不是业务写入路径。默认上限正好是
100k 节点 / 500k 边，超过即 `SnapshotTooLargeError`。

## 后果

**正面**

- 业务组件只依赖 `GraphClient` 契约；换存储（Ladybug → 其它 property graph）或换算法引擎
  （NetworkX → rustworkx）不改调用方。
- Ladybug 的持久化/Cypher/事务与 NetworkX 的算法职责清晰；不会出现两套可独立修改的完整图。
- core 拥有稳定的 Python API 与 CLI，但图能力不在默认请求路径：extra 不默认安装、不注册工具、
  不挂 hook，未安装时对普通会话零成本。
- profile 与进程边界显式；锁冲突是可见错误，不是跨 profile 数据污染。
- 快照版本与写事务绑定，算法结果可复现、可失效。

**负面 / 代价**

- core 多一个子系统与 Schema 生命周期；调用方要学 Graph API。
- v1 没有 Memory/Code Graph/Advisor 消费者，只有 CLI 与测试；如果不产生真实使用场景，就是仓库
  明确反对的“无消费者的基础设施”，必须按 D1 的停止条件处理。
- Ladybug 当前文档只保证主键索引；属性过滤默认扫描，P1 验证前不承诺二级索引。高频过滤需要
  设计 `key`/查找节点类型。
- 单写者模型限制多进程写入；gateway + CLI 同时写需要跨进程代理（未来 ADR）。
- `networkx` 快照是内存拷贝；大图必须拒绝而不是硬扛。
- core 的可选 extra 仍要跟 Hermes 的精确 pin/`uv.lock` 节奏走，升级不是独立的。

**中性**

- `agent/learning_graph.py` 仍是只读投影；本 ADR 不替换它。未来若 Desktop starmap 改读图存储，
  另立 ADR。
- 现有 Hindsight/Cognee/MindGraph 等记忆提供者不受影响；图子系统不是记忆后端，v1 不接入它们。

## 未采纳的替代方案

| 方案 | 否决理由 |
|---|---|
| 把 LadybugDB + NetworkX 作为**默认 core 依赖 + 模型工具** | 违反窄腰；每次请求多背 schema；非普遍需求。本 ADR 选择 core 代码 + 可选 extra + 无工具 |
| 把图能力做成独立插件 | 用户明确要求 core 子系统；插件边界会让未来 core 组件接入多一层 host/import 复杂度 |
| 业务组件直接调用 `ladybug.Connection` / `nx.Graph` | 连接、锁、Schema、事务、快照失效全部外泄；换实现要改所有调用方 |
| 只用 NetworkX 内存图，不持久化 | 进程重启即丢；无法做跨会话记忆/知识/代码图 |
| 只用 SQLite 邻接表 + 递归 CTE | 无 property graph 类型、Cypher、事务化图算法；跨域查询要自己写递归；不如专用图库 |
| 用 Neo4j / 图服务 | 需要常驻服务、端口、认证、备份；与 Hermes 的本地优先/单进程形态不匹配 |
| 用 Ladybug MCP server 作为内部接口 | 多一次 stdio/HTTP 跳转；事务/profile/锁不在同一进程；MCP 工具面与模型工具重复 |
| 一张通用 `Node(id,type,props_json)` + `Edge(...)` 表 | Ladybug 要求结构化 Schema；通用表失去类型列，无法把 `(type,key)` 映射成各类型自己的主键，属性过滤退化为 JSON 扫描 |
| 用 `upsert(type,key)` 生成随机 ID + 二级唯一索引 | Ladybug 当前文档只保证主键唯一性，二级索引的唯一约束未验证；UUIDv5 用主键唯一性实现 `(type,key)` 幂等 |
| 把 `networkx` 作为长期存活的第二份图 | 双写/同步必然分叉；快照按版本派生才可证明一致 |
| 自动把每轮对话抽取成图 | 质量、成本、重试、隐私都未定义；v1 不接入记忆 |
| 在会话中途注册 `graph` 工具 | 破坏 prompt cache；未来接入必须在会话边界 |

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| Ladybug 项目较新、0.x API 可能变动 | Adapter 隔离；P1 固定实测 wheel；升级只改 `adapters/ladybug/`；能力探测而非硬编码版本分支 |
| 原生 wheel 在某个平台缺失 | `GraphUnavailableError`；CLI 显示不可用及原因；不源码构建、不拖垮 core |
| 单写者锁在多进程拓扑里冲突 | 明确 `GraphLockedError`；未来 ADR 才做代理；文档写清“一个文件一个写者” |
| Schema 迁移破坏数据 | 仅加法迁移；破坏性迁移需显式 CLI + 备份；`GraphSchema` 记录版本 |
| 快照内存爆炸 | 节点/边/字节上限；超限拒绝；算法在隔离线程执行并超时 |
| 算法结果不确定导致 UI/测试漂移 | 节点排序、Louvain `seed=0`、快照版本固定 |
| 图内容被当成指令注入 | v1 无自动召回/工具；未来接入必须走记忆围栏，图内容不写系统提示 |
| profile 泄漏 | `GraphClient` 按 `hermes_home_key` 注册；临时 `HERMES_HOME` 隔离测试；禁止模块级单例 |
| 无消费者的基础设施 | D1 停止条件：P1–P3 后若 CLI 也没有真实使用场景，停止/删除，不保留死代码 |
| 工具面膨胀 | v1 不注册工具；未来接入必须是 service-gated，且不得中途改变工具面 |
| 依赖供应链 | core 可选 extra 精确 pin；`hermes pm lock` 更新 `uv.lock`；不引入默认依赖 |

## 验收

core 测试必须用 `scripts/run_tests.sh` 与临时 `HERMES_HOME`，不得写 `~/.hermes/`。

1. **公共边界**：干净子进程 `import hermes_graph` 后断言 `ladybug` / `networkx` 不在 `sys.modules`；
   公共方法的签名/返回值不出现底层类型（`inspect.signature` 与运行时对象断言），不会把
   `ladybug`/`networkx` 类型外泄。
2. **CRUD/upsert/删除生命周期**：`create` 生成 UUIDv7；`upsert(type,key)` 两次得到同一 `id` 且属性
   更新；`get/update/delete` 通过 `GraphNodeRef` 定位。`hard=False` 写 `deleted_at`、清邻接边，
   且 `get`/`search` 默认过滤；软删后再 `upsert` 复活同一个 `id`（`deleted_at` 清空、无重复节点）。
   `hard=True` 物理删除节点、边与 ref；之后再 `upsert` 用同一派生 ID 重新创建，仍只有一个节点。
   两种模式的 SDK 读路径都符合 D3 的过滤语义。
3. **事务**：`with begin()` 成功提交全部写入；异常回滚全部写入；嵌套事务报错；写锁超时报
   `write_busy`；只读事务写操作被拒。
4. **Scope 版本**：写事务提交后受影响 scope 版本递增；回滚不递增；一个事务改两个 scope 时两个都
   递增；`invalidate(scope)` 只清缓存。
5. **快照**：`MultiDiGraph` 保留同对节点的多条不同类型边；快照版本与 `GraphScopeVersion` 一致；
   超限抛 `SnapshotTooLargeError`；算法结果排序稳定，Louvain 固定 seed 可复现；`node_types=None`
   时 `GraphNodeRef`/`GraphEdgeRef`/`GraphScopeVersion`/`GraphSchema` 不进入快照与算法结果。
6. **Profile 隔离**：两个临时 `HERMES_HOME` 各自 `GraphClient.open()`；数据库路径分别是
   `<home>/graph/graph.lbdb`，A 写入的节点在 B 不可见；关闭 A 不影响 B；v1 没有可配置路径可以
   把数据库指到 home 之外。
7. **锁**：同一进程第二次 `open(read_only=False)` 同一路径返回同一个 `GraphClient`；跨进程模拟
   （子进程持有）返回 `GraphLockedError`，不产生第二个数据库文件。
8. **可选 extra 降级**：未安装 `[graph]` 时 `import hermes_graph` 可用；`hermes graph status` 只调
   `pm.available("graph")` 并返回 `unavailable`，monkeypatch `pm.ensure_import` 断言未被调用；
   `hermes graph --help` 不触发 extra；core 启动、其它命令和工具定义不受影响。
9. **无组件接入**：干净子进程中导入 `cli` / `model_tools` 后断言 `"hermes_graph"` 不在
   `sys.modules`，且 `ladybug` / `networkx` 不在 `sys.modules`；`get_tool_definitions()` 与首条 API
   请求的系统提示逐字不变。不读源码做“某文件没有 import”的静态断言。
10. **CLI 只读查询**：`hermes graph query` 拒绝 `CREATE`/`MERGE`/`SET`/`DELETE`/`CALL` 等写语句、
    多语句和字符串拼接；参数化查询正常；行数/超时/字节上限返回结构化错误。
11. **CLI 写入与运维**：`node upsert/get/search/delete`（软删/硬删）、`edge create/delete/list`、
    `snapshot`、`analyze`、`backup`、`doctor` 全路径通过；`--hard` 无 `--yes` 时拒绝；输出为 JSON，
    错误走 stderr 且退出码非零。
12. **平台**：Windows cp314（`win_amd64` 至少）、macOS、Linux 各跑一次导入 + 建库 + CRUD +
    查询；若某平台无 wheel，`GraphUnavailableError` 给出原因，core 仍正常启动。
13. **发布包**：构建 wheel 后断言 `hermes_graph/` 及其子模块在 wheel 内；editable 与 wheel 安装
    两种布局都能 `import hermes_graph`。这覆盖 `packages.find.include` 白名单漂移。
14. **CLI 快路径**：`"graph"` 在 `_BUILTIN_SUBCOMMANDS`；`hermes graph --help` 不调用
    `discover_plugins()`，不触发插件命令发现。

## 实现落点

本 ADR **修改 Hermes core**，不创建独立插件仓库：

```
hermes-agent/
├── hermes_graph/                # 新 core 子系统
│   ├── __init__.py              # 只导出公共类型
│   ├── client.py                # GraphClient、profile 注册表、能力探测
│   ├── context.py               # 单库上下文、连接、写锁、scope 版本
│   ├── config.py                # GraphLimits
│   ├── models.py                # Node/Edge/QueryResult/GraphPath/GraphSnapshot/Capabilities/Health
│   ├── serialization.py         # UUIDv7/UUIDv5、JSON 校验、字段类型
│   ├── exceptions.py            # GraphError 体系
│   ├── services/                # nodes/edges/query/traversal/algorithms/index/transaction/schema/admin
│   ├── adapters/ladybug/        # 唯一私有适配器（store.py）
│   └── schema/                  # definitions.py、registry.py（DDL + 加法迁移）
├── hermes_cli/subcommands/graph.py  # `hermes graph` 子命令
├── hermes_cli/main.py           # _build_cli_parser 注入 + _BUILTIN_SUBCOMMANDS 加 "graph"
├── pyproject.toml               # [project.optional-dependencies].graph + packages.find include
├── uv.lock                      # `hermes pm lock`
├── tests/hermes_graph/          # core 测试
└── website/docs/                # 用户文档（CLI/API）
```

对接 Hermes 时只使用 core 现有缝：

| 面 | 现有接口 | 用法 |
|---|---|---|
| CLI | `hermes_cli/subcommands/graph.py` + `main._build_cli_parser()` + `_BUILTIN_SUBCOMMANDS` | 注册 `hermes graph ...`；避免插件发现慢路径 |
| 依赖 | `[project.optional-dependencies].graph` + `pm.available`/`pm.ensure_import` | 精确 pin；status 只探测，数据命令按需准备 |
| 打包 | `[tool.setuptools.packages.find].include` | 加入 `hermes_graph` / `hermes_graph.*`，保证 wheel 含包 |
| 数据目录 | `get_hermes_home()` | 固定 `<home>/graph/graph.lbdb`；跟随活动 profile |
| 生命周期 | `graph.admin.close()` / `atexit` | 关闭 `Database`，释放文件锁 |
| 模型工具 | **不接入** | 不 import `tools.registry`，不改 `toolsets.py` |
| 记忆 | **不接入** | 不 import `agent.memory_*`，不实现 `MemoryProvider` |
| 其它组件 | **不接入** | 不 import `agent.advisor*`、`agent.learning_graph` |
| 测试 | `scripts/run_tests.sh`、临时 `HERMES_HOME` | 不写真实 profile |

## 未决问题（不在本 ADR 内）

1. ~~`ladybug` 的最终 pin~~：已固定 0.22.1 并在 Windows cp311/cp314 实测；文档 CLI 的 0.11.0 与 PyPI 的 0.22.x 不是同一版本号。
2. 是否提供跨进程写代理（本地 HTTP/JSON-RPC）——只在有真实多写者需求时决定。
3. 是否用 Ladybug FTS/向量扩展做召回——与现有记忆提供者的检索策略重叠，另立 ADR。
4. 是否把 `agent/learning_graph.py` 的只读投影迁到图存储——涉及 Desktop/TUI/导出，另立 ADR。
5. 是否给模型开 `graph` 工具或记忆接入——需要审批、去重、注入防护、回滚语义，另立 ADR。
6. 是否提供一个只调用 CLI 的 skill——属于 skill 层，不在本 ADR 决定。
