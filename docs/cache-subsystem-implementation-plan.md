# Hermes Cache 子系统实施计划

> 状态：实施计划（Implementation Plan）。
> 实施状态：PR1–PR5 已落地；PR6（sticker_cache）保持条件性，未执行一次性导入前不切换。
> 目标：为 Hermes 增加 provider-based embedded cache，落点 `hermes_cache/`。
> 本文只定义实施顺序、契约、文件级任务与验收，不含实现代码。
> `[verified]` = 来自本仓库代码；`[design]` = 本计划的取舍。

---

## 0. TL;DR

1. **路径是 `hermes_cache/`，不是 `hermes/cache/`。** 根目录已有同名启动文件 `hermes`，Windows 上文件与目录不能共存；`hermes_agent/` 只是模块重命名兼容层，也不适合放新子系统。
2. **Provider 是唯一可替换点。** `Cache` 只依赖 `CacheProvider` 协议；只有 `factory.py` 和测试 import `SQLiteProvider`，业务代码只 import `hermes_cache` 顶层 API。
3. **Factory 必须支持 policy/serializer。** `get_cache(home=None, policy=None, serializer=None)` 内部构造默认 provider；registry key 包含 `(home_key, db_path, policy, serializer type/name)`。
4. **v1 后端是 SQLite。** 独立 `cache/cache.db`，WAL + `busy_timeout`，per-thread connection；不放进 `state.db`。
5. **L1 是可选加速层。** 进程内 LRU + TTL + 字节上限；存序列化 bytes，不存 Python 对象，避免缓存污染。
6. **核心 PR 不迁移现有缓存。** 首个 adopter 是 `plugin_toolset_keys.json`；`sticker_cache` 必须带一次性 JSON import 才能迁移；`model_catalog`、`schema_columns` 暂不迁移。
7. **缓存目录走 profile guard。** `get_cache()` 用 `mkdir_under_hermes_home(cache_dir)` + `apply_secure_dir_policy(cache_dir, home=root)`；`SQLiteProvider` 不自行 `mkdir`，避免复活已删除 profile，也避免 WAL/SHM 落在权限不当的目录。
8. **背景插件发现线程必须继承 profile context。** PR5 把 `start_background_plugin_discovery` 的裸 `threading.Thread` 换成 `spawn_context_thread`，并把 caller home 显式传给 persist；否则 `get_cache()` 会写 launch profile。
9. **零新依赖。** 只使用标准库 `sqlite3`、`json`、`pickle`、`threading`、`dataclasses`。

---

## 1. 事实基础

### 1.1 路径冲突 `[verified]`

仓库根目录存在一个名为 `hermes` 的 Python 启动文件：

```text
hermes-agent/hermes        # from hermes_cli.main import main
```

由此得出：

- 不能在 Windows 上同时创建 `hermes/` 目录与 `hermes` 文件。
- 即使只在 POSIX 上创建，顶层 `hermes` 包也会与安装入口、第三方 `hermes` 包产生命名冲突。
- `hermes_agent/__init__.py` 明确写着：

  ```python
  """The Hermes Agent package; today it holds only the module-rename compatibility layer."""
  ```

  因此 `hermes_agent/cache/` 也不适合作为新子系统落点。

**决策**：新包使用 `hermes_cache/`，与现有 `hermes_graph/`、`hermes_codegraph/`、`hermes_platform/` 命名风格一致。

### 1.2 打包方式 `[verified]`

`pyproject.toml` 使用显式 include：

```toml
[tool.setuptools.packages.find]
include = [
  "agent",
  "agent.*",
  "hermes_graph",
  "hermes_graph.*",
  "hermes_codegraph",
  "hermes_codegraph.*",
  "hermes_platform",
  "hermes_platform.*",
  # ...
]
```

**修改**：增加

```toml
  "hermes_cache",
  "hermes_cache.*",
```

否则 wheel 静默缺包，源码 checkout 能跑、安装版 `ModuleNotFoundError`。

### 1.3 现有缓存盘点 `[verified]`

| 现有缓存 | 位置 | 形态 | v1 决策 |
|---|---|---|---|
| `schema_columns.json` | `$HERMES_HOME/cache/` | JSON + DDL hash | 不迁移；启动关键路径、极小数据、可重算 |
| `model_catalog.json` | `$HERMES_HOME/cache/` | JSON + mtime + SWR | 不迁移；需要 stale-while-revalidate |
| `plugin_toolset_keys.json` | `$HERMES_HOME/cache/` | 两个 `list[str]` | **首个 adopter** |
| `worktree_merge_verdicts.json` | `$HERMES_HOME/cache/` | dict + 1000 entry cap | 后续；当前是整份 dict 快照 |
| `sticker_cache.json` | `$HERMES_HOME/` | dict + vision 结果 | 条件迁移；必须带一次性 import |
| `provider_models_cache.json` | `$HERMES_HOME/` | JSON + 跨进程文件锁 | 不迁移；锁语义独立 |

### 1.4 现有 SQLite 约定 `[verified]`

`hermes_state` 已经展示了本仓库的 SQLite 运行时约定：

- WAL、`busy_timeout`、连接生命周期、fork/进程边界都需要显式处理。
- `state.db` 有会话、消息、FTS、修复、迁移语义，**不是可丢弃缓存**。

**决策**：缓存使用独立 `$HERMES_HOME/cache/cache.db`，不与 `state.db` 共享表或连接。

### 1.5 测试与代码健康 `[verified]`

- 测试位置镜像源码树：`tests/hermes_cache/`。
- 运行方式必须是 `scripts/run_tests.sh tests/hermes_cache/`，不用裸 `pytest`。
- 测试不能写 `~/.hermes/`；autouse fixture 会把 `HERMES_HOME` 重定向到临时目录。
- 代码健康目标：单文件 ≤ 2000 行，单函数 ≤ 300 行，CC ≤ 20，嵌套 ≤ 6。
- 不允许 source-reading 测试；用行为测试。

### 1.6 命名 profile 保护 `[verified]`

`hermes_cli/AGENTS.md` 明确：枚举是纯读，**不得从 served path `mkdir` 一个 profile home**；`SessionDB`、logging、cron 等写路径都走 `mkdir_under_hermes_home` / `_ensure_cron_dir`，拒绝 deleted/missing named profile（#94590）。

`hermes_constants.mkdir_under_hermes_home(path)` 的行为：

1. 先 `assert_named_profile_home_live(target)`，拒绝缺失或 tombstoned 的命名 profile。
2. 再通过 `make_user_dir(...)` 创建目录。

目录权限由 `hermes_constants.apply_secure_dir_policy(path, home=...)` 统一处理：默认 owner-only `0700`，但 managed install、container、显式 `HERMES_HOME_MODE` 的共享策略优先。

结论：`cache/` 目录必须由 **factory 层**用 `mkdir_under_hermes_home` 创建；`SQLiteProvider` 不得裸 `parent.mkdir()`。否则缓存初始化会复活一个已删除的 profile，且 WAL/SHM 侧车文件可能落在权限不当的目录里。

### 1.7 背景线程的 profile scope `[verified]`

`hermes_cli/plugins.py:start_background_plugin_discovery` 当前用裸 `threading.Thread(target=_run, ...)`；`_run` 最终调用 `_persist_plugin_toolset_keys()`。

ContextVars 不跨裸 `Thread`。在网关这种一个进程服务多个 profile 的场景里，后台线程里的 `get_hermes_home()` 会解析 **launch profile**，而不是发起 `start_background_plugin_discovery()` 的请求 profile。这正是根 `AGENTS.md` 的 profile-scope 规则和 HX012 要禁止的默认 profile 泄漏。

PR5 必须：

1. 用 `hermes_threading.spawn_context_thread(_run, name="plugin-discovery")` 替换裸 `threading.Thread`。
2. 在 spawn 前 `home = get_hermes_home()`，把 `home` 传给 `_persist_plugin_toolset_keys(home)`；`get_cache(home=home)` 使用显式 profile，作为第二道保险。
3. 增加测试：profile-scoped caller 触发后台发现后，写入的是该 profile 的 `cache/cache.db`，launch profile 的 DB 不出现或不更新。

---

## 2. 设计决策

| # | 决策 | 含义 |
|---|---|---|
| D1 | 包路径 `hermes_cache/` | 不使用 `hermes/cache/`，不使用 `hermes_agent/cache/` |
| D2 | Provider 边界 | `Cache`/Engine 只依赖 `CacheProvider` 协议；SQLite 只在 factory/测试出现 |
| D3 | Factory 支持 policy/serializer | 业务代码不需要 import `SQLiteProvider` 就能定制策略 |
| D4 | 同步 API | SQLite 同步；async 调用方用 `asyncio.to_thread` |
| D5 | 独立 SQLite DB | `$HERMES_HOME/cache/cache.db`，可安全删除重建 |
| D6 | L1 存序列化 bytes | 避免调用方共享可变对象；命中时重新反序列化 |
| D7 | 默认 JSON | `PickleSerializer` 显式 opt-in，只允许可信数据 |
| D8 | 默认 fail-open | 缓存故障不阻断业务；严格模式可抛 `ProviderError` |
| D9 | v1 不加 `cache.*` 配置段 | 默认值固定在 `CachePolicy`；需要不同预算的调用方直接传 policy |
| D10 | 首个 adopter 是 plugin toolset keys | 数据可重算、无付费产物、已有冷启动回退 |
| D11 | sticker 迁移必须带 import | 描述来自付费 vision 调用，冷切换会丢失 |
| D12 | 核心 PR 不迁移现有缓存 | 先建立子系统，再逐个 adopter |
| D13 | 缓存目录由 factory 创建 | `mkdir_under_hermes_home` + `apply_secure_dir_policy`；provider 不 `mkdir` |
| D14 | 背景插件发现继承 profile | `spawn_context_thread` + 显式 home；禁止裸 `Thread` |

---

## 3. 目标架构

```text
Application Layer
  Agent / Advisor / Memory / Search / Code Intelligence
        |
        v
Unified Cache API
  Cache / Namespace / Batch
        |
        v
Cache Engine
  L1 Memory (LRU + TTL)
  TTL / Policy / Statistics / Serialization / Error Handling
        |
        v
Provider Interface (CacheProvider)
        |
        +--> SQLiteProvider (v1)
        +--> LMDB / DiskCache / RocksDB (future)
```

### 3.1 模块职责

| 模块 | 文件 | 职责 | 禁止 |
|---|---|---|---|
| Cache API | `hermes_cache/api.py` | `Cache`、`Namespace`、`Batch`，参数校验 | 不 import `providers.sqlite` |
| Cache Engine | `hermes_cache/engine.py` | L1/L2 顺序、TTL 判定、序列化、统计、错误策略、维护 | 不写 SQL |
| Serializer | `hermes_cache/serializers.py` | 值 ↔ bytes，提供 codec name | 不处理 TTL/key |
| Policy | `hermes_cache/policy.py` | 默认 TTL、L1 容量、L2 容量、维护间隔、错误策略 | 不持有运行时状态 |
| Namespace | `hermes_cache/api.py` | 绑定 namespace + 默认 TTL 的薄门面 | 不复制 Engine 逻辑 |
| Provider | `hermes_cache/providers/base.py` | `ProviderEntry`、`CacheProvider` 纯存储 CRUD | 不判定 TTL、不序列化、不做 L1 |
| SQLite Provider | `hermes_cache/providers/sqlite.py` | schema、连接、SQL、purge/trim | 不含业务策略 |
| Factory | `hermes_cache/factory.py` | 默认 DB 路径、provider 选择、进程内 registry | 不 import-time 解析 HERMES_HOME |

依赖方向：

```text
api ──> engine ──> providers.base
 │         ├──> l1
 │         ├──> serializers
 │         ├──> policy
 │         └──> models / errors
 └──> policy / serializers / errors

factory ──> api + providers.sqlite + hermes_constants
```

---

## 4. 公共契约

### 4.1 Cache API

```python
MISSING = object()
DEFAULT_NAMESPACE = "default"

class Cache:
    def __init__(
        self,
        provider: CacheProvider,
        *,
        serializer: Serializer | None = None,
        policy: CachePolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None: ...

    @property
    def closed(self) -> bool: ...

    def get(
        self,
        key: str,
        default: Any = None,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        bypass_l1: bool = False,
    ) -> Any: ...

    def set(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl: float | None = None,
    ) -> bool: ...

    def delete(self, key: str, *, namespace: str = DEFAULT_NAMESPACE) -> bool: ...

    def contains(self, key: str, *, namespace: str = DEFAULT_NAMESPACE) -> bool: ...

    def get_many(
        self, keys: Sequence[str], *, namespace: str = DEFAULT_NAMESPACE
    ) -> dict[str, Any]: ...

    def set_many(
        self,
        values: Mapping[str, Any],
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl: float | None = None,
    ) -> int: ...

    def delete_many(
        self, keys: Sequence[str], *, namespace: str = DEFAULT_NAMESPACE
    ) -> int: ...

    def get_or_set(
        self,
        key: str,
        factory: Callable[[], Any],
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl: float | None = None,
    ) -> Any: ...

    def clear(self, *, namespace: str | None = None) -> int: ...

    def purge_expired(self, *, limit: int = 1000) -> int: ...

    def namespace(
        self, name: str, *, default_ttl: float | None = None
    ) -> Namespace: ...

    def batch(self, *, namespace: str = DEFAULT_NAMESPACE) -> Batch: ...

    def stats(self) -> CacheStats: ...

    def provider_stats(self) -> ProviderStats: ...

    def close(self) -> None: ...
```

`Namespace`：

```python
class Namespace:
    def get(self, key: str, default: Any = None, *, bypass_l1: bool = False) -> Any: ...
    def set(self, key: str, value: Any, *, ttl: float | None = None) -> bool: ...
    def delete(self, key: str) -> bool: ...
    def contains(self, key: str) -> bool: ...
    def get_many(self, keys: Sequence[str]) -> dict[str, Any]: ...
    def set_many(self, values: Mapping[str, Any], *, ttl: float | None = None) -> int: ...
    def delete_many(self, keys: Sequence[str]) -> int: ...
    def clear(self) -> int: ...
    def batch(self) -> Batch: ...
```

`Batch`：

```python
class Batch:
    def set(self, key: str, value: Any, *, ttl: float | None = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def get(self, key: str, default: Any = None) -> Any: ...
    def commit(self) -> int: ...
    def rollback(self) -> None: ...
    def __enter__(self) -> Batch: ...
    def __exit__(self, exc_type, exc, tb) -> None: ...
```

语义：

- `ttl=None` 使用 `Namespace.default_ttl`，再回退到 `CachePolicy.default_ttl`。
- `ttl=0` 明确表示永不过期。
- `ttl<0` 或非有限值抛 `InvalidTTLError`。
- `key` 必须是非空 `str`，无 NUL，UTF-8 长度不超过 `CachePolicy.max_key_bytes`。
- `namespace` 必须是非空 `str`，无 NUL，长度不超过 128 字节。
- `get_many` 只返回命中的 key；`contains` 用来区分“缓存了 None”和“未命中”。
- `get_or_set` 内部用 `MISSING` 哨兵，因此可以正确缓存 `None`。
- `Batch` 绑定单个 namespace；读操作先查 pending puts，再查 pending deletes，最后查 Engine。
- `Batch.commit` 调 `provider.apply_batch(puts, deletes)`；成功后统一更新 L1；失败不更新 L1。
- `Batch` 不是线程安全的；每个线程用自己的 batch。

### 4.2 Factory

```python
# hermes_cache/factory.py
def default_cache_path(home: Path | None = None) -> Path: ...

def get_cache(
    *,
    home: Path | None = None,
    policy: CachePolicy | None = None,
    serializer: Serializer | None = None,
) -> Cache: ...

def close_all_caches() -> None: ...
def reset_cache_registry() -> None: ...
```

实现：

```python
def get_cache(*, home=None, policy=None, serializer=None):
    from hermes_constants import (
        apply_secure_dir_policy,
        assert_named_profile_home_live,
        get_hermes_home,
        hermes_home_key,
        mkdir_under_hermes_home,
    )

    root = Path(home) if home is not None else get_hermes_home()
    resolved_policy = policy if policy is not None else CachePolicy()
    resolved_serializer = serializer if serializer is not None else JSONSerializer()
    cache_dir = root / "cache"
    # Cheap live/tombstone guard on every call; mkdir + chmod only on registry miss.
    assert_named_profile_home_live(cache_dir)
    path = cache_dir / "cache.db"

    key = (
        hermes_home_key(root),
        os.path.normcase(os.path.abspath(str(path))),
        resolved_policy,
        type(resolved_serializer),
        resolved_serializer.name,
    )
    with _REGISTRY_LOCK:
        cache = _REGISTRY.get(key)
        if cache is None or cache.closed:
            mkdir_under_hermes_home(cache_dir)
            apply_secure_dir_policy(cache_dir, home=root)
            cache = Cache(
                SQLiteProvider(path),
                policy=resolved_policy,
                serializer=resolved_serializer,
            )
            _REGISTRY[key] = cache
        return cache
```

约束：

- `CachePolicy` 必须是 frozen/hashable dataclass；等价 policy 共享同一个 registry entry。
- `Serializer.name` 必须唯一标识编码格式和配置版本，例如 `json.v1`；不同配置不能复用同一个 name。
- 同一 DB、不同 policy/serializer 会得到不同 `Cache` 实例，共享 SQLite 文件但各自持有 L1；文档明确。
- `get_cache()` 内部构造默认 provider；业务代码不接触 `SQLiteProvider`。
- 直接构造 `Cache(SQLiteProvider(...))` 只用于测试、自定义 backend 或底层集成，不属于业务层用法。
- 未来如需自定义 provider，增加 `get_cache(provider_factory=...)`，不把具体 provider 暴露给业务代码。
- `get_cache()` 在调用时解析 `get_hermes_home()`，禁止 import-time 路径。
- `cache/` 目录必须由 factory 通过 `mkdir_under_hermes_home(cache_dir)` 创建，拒绝缺失/已删除的命名 profile；不能用裸 `parent.mkdir()` 复活 profile。
- 每次 `get_cache()` 先 `assert_named_profile_home_live(cache_dir)`；`mkdir_under_hermes_home` + `apply_secure_dir_policy` 只在 registry miss/closed 重建时执行，避免热路径重复 syscall。
- 创建后调用 `apply_secure_dir_policy(cache_dir, home=root)`：默认 0700，managed/container/`HERMES_HOME_MODE` 的共享策略仍然生效；WAL/SHM 侧车文件因此位于受保护目录内。
- `SQLiteProvider` 不负责创建父目录；直接使用它时由调用方准备目录，避免存储层绕过 profile guard。
- `atexit` 注册 `close_all_caches()`；测试用 `reset_cache_registry()`。

`hermes_cache/AGENTS.md` 增加硬规则：

> 业务代码只能 import `hermes_cache` 顶层公共 API。只有 `factory.py` 和测试可以 import `hermes_cache.providers.sqlite`。

### 4.3 CachePolicy

```python
@dataclass(frozen=True, slots=True)
class CachePolicy:
    default_ttl: float | None = None
    l1_enabled: bool = True
    max_l1_entries: int = 2048
    max_l1_bytes: int = 64 * 1024 * 1024
    l1_max_age: float | None = 5.0
    max_value_bytes: int = 32 * 1024 * 1024
    max_key_bytes: int = 4096
    maintenance_every_writes: int = 256
    max_l2_bytes: int | None = None
    fail_open: bool = True
    raise_on_corrupt: bool = False
```

校验规则：

- `default_ttl` 为 `None` 或 ≥ 0。
- `max_l1_bytes`、`max_value_bytes`、`max_key_bytes` 为正整数；`max_l1_entries` 为非负整数，0 表示禁用 L1。
- `l1_max_age` 为 `None` 或 > 0。
- `maintenance_every_writes` ≥ 0；0 表示关闭自动维护。
- `max_l2_bytes` 为 `None` 或 > 0。
- `fail_open`、`raise_on_corrupt` 必须是 `bool`。

v1 不加 `cache.*` 配置段。需要不同预算的调用方直接传 `CachePolicy`；以后要暴露给用户，再在 `factory.py` 读 config。

### 4.4 CacheStats

```python
@dataclass(frozen=True, slots=True)
class CacheStats:
    l1_hits: int = 0
    l2_hits: int = 0
    misses: int = 0
    expired: int = 0
    writes: int = 0
    deletes: int = 0
    evictions_l1: int = 0
    errors: int = 0
    batches: int = 0
    purges: int = 0

    @property
    def hits(self) -> int: ...
    @property
    def hit_rate(self) -> float: ...
```

统计是进程内的；`provider_stats()` 返回条目数和字节数，不跨进程聚合。

### 4.5 Serializer

```python
class Serializer(Protocol):
    name: str
    def dumps(self, value: Any) -> bytes: ...
    def loads(self, payload: bytes) -> Any: ...
```

- `JSONSerializer`：默认，`name="json.v1"`。`ensure_ascii=False`、紧凑分隔符、`allow_nan=False`。只支持 JSON 类型；bytes/datetime 需调用方自行编码。
- `PickleSerializer`：`name="pickle.v1"`，仅用于可信本地数据；docstring 明确警告反序列化可执行代码，禁止用于外部输入。
- `RawSerializer`：`name="raw.v1"`，只接受 bytes-like，读取返回 `bytes`，适合二进制结果。
- 切换 serializer 会让旧条目的 `codec` 不匹配，Engine 自动删除并按 miss 处理。

### 4.6 Provider

```python
@dataclass(frozen=True, slots=True)
class ProviderEntry:
    namespace: str
    key: str
    payload: bytes
    codec: str
    created_at: float
    expires_at: float | None

@dataclass(frozen=True, slots=True)
class ProviderStats:
    entries: int
    size_bytes: int

class CacheProvider(Protocol):
    def get(self, namespace: str, key: str) -> ProviderEntry | None: ...
    def get_many(
        self, namespace: str, keys: Sequence[str]
    ) -> dict[str, ProviderEntry]: ...

    def set(self, entry: ProviderEntry) -> None: ...
    def set_many(self, entries: Sequence[ProviderEntry]) -> None: ...

    def delete(self, namespace: str, key: str) -> bool: ...
    def delete_many(self, namespace: str, keys: Sequence[str]) -> int: ...
    def delete_namespace(self, namespace: str) -> int: ...
    def clear(self) -> int: ...

    def apply_batch(
        self,
        puts: Sequence[ProviderEntry],
        deletes: Sequence[tuple[str, str]],
    ) -> None: ...

    def purge_expired(self, now: float, limit: int = 1000) -> int: ...
    def trim_to_bytes(self, max_bytes: int, now: float) -> int: ...
    def stats(self) -> ProviderStats: ...
    def close(self) -> None: ...
```

边界：

- Provider 只存 `expires_at`，不判断是否过期；过期判定在 Engine。
- `purge_expired(now, limit)` 与 `trim_to_bytes(max_bytes, now)` 是存储维护操作：Engine 决定何时调用、阈值多少，Provider 只执行删除。
- Provider 不反序列化 payload；`codec` 只作为元数据保存。
- Provider 不维护 L1，不记录 cache hit/miss。
- 未来 LMDB/RocksDB 用长度前缀编码 `(namespace, key)` 或 hash，细节留在 provider 内。
- 可选 `BaseCacheProvider` 提供 `get_many`/`set_many`/`delete_many` 的默认循环实现，SQLite 覆盖为单事务版本。

### 4.7 错误处理

| 场景 | 默认 `fail_open=True` | `fail_open=False` |
|---|---|---|
| provider 读失败 | log + 返回 default | 抛 `ProviderError` |
| provider 写失败 | log + 返回 `False` | 抛 `ProviderError` |
| payload 损坏 / codec 不匹配 | 删条目 + 按 miss | 抛 `SerializationError` |
| 非法 key / TTL | 总是抛 | 总是抛 |
| 使用已关闭 Cache | 总是抛 `CacheClosedError` | 总是抛 |

序列化失败（`set` 时）始终抛 `SerializationError`，因为这是调用方传了 serializer 不支持的值，属于编程错误，不应静默吞掉。

---

## 5. SQLite Provider 设计

### 5.1 Schema

```sql
PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS cache_entries (
    namespace  TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    payload    BLOB    NOT NULL,
    codec      TEXT    NOT NULL,
    created_at REAL    NOT NULL,
    expires_at REAL,
    size_bytes INTEGER NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE INDEX IF NOT EXISTS idx_cache_entries_expires
    ON cache_entries(expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cache_entries_created
    ON cache_entries(created_at);
```

设计点：

- 使用普通 rowid 表，不用 `WITHOUT ROWID`：大 BLOB 走 overflow page，避免 payload 内联进主键索引。
- `PRIMARY KEY (namespace, key)` 同时覆盖 namespace 前缀删除。
- `expires_at IS NULL` 表示永不过期。
- `size_bytes = len(payload)`，用于 `stats` 和容量裁剪。
- `codec` 记录 `json.v1` / `pickle.v1` / `raw.v1`；Engine 读到不匹配的 codec 时删除该条并按 miss 处理。
- `PRAGMA user_version` 做 schema 迁移；读到更高版本时抛 `ProviderError`，由运维删除 `cache.db`，不做危险自动降级。

### 5.2 连接策略

```python
sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
```

每个线程一个连接，放在 `threading.local()`；不要全进程共用一个连接。

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
```

- WAL + `busy_timeout` 支持多进程读写。
- `synchronous=NORMAL` 对可丢弃缓存足够；不需要 `state.db` 的 FULL/checkpoint 策略。
- 写批量用 `BEGIN IMMEDIATE`，`executemany`，commit/rollback。
- `get_many` / `delete_many` 按 500 个 key 分块，规避 SQLite 变量上限。
- `purge_expired` 用子查询限制数量，避免 `DELETE ... LIMIT` 的编译选项依赖：

```sql
DELETE FROM cache_entries
WHERE rowid IN (
    SELECT rowid FROM cache_entries
    WHERE expires_at IS NOT NULL AND expires_at <= ?
    LIMIT ?
);
```

- `trim_to_bytes`：先 `SUM(size_bytes)`；超限先删过期，再按 `created_at ASC` 删最旧条目。v1 是 FIFO 近似，不在每次 `get` 写 `last_access`，避免读放大。
- `close()` 关闭当前进程所有已登记连接，幂等。
- 父目录由 `factory.get_cache()` 用 `mkdir_under_hermes_home` + `apply_secure_dir_policy` 创建；`SQLiteProvider` 不调用 `parent.mkdir()`，避免绕过 profile tombstone 检查。
- DB 文件在 POSIX 上创建后 `chmod 0o600`；Windows 跳过。WAL/SHM 侧车文件位于已按 `apply_secure_dir_policy` 处理的 `cache/` 目录内，不会 world-readable。缓存可能含 prompt、memory 片段，不应暴露。
- POSIX 上注册 `os.register_at_fork(after_in_child=...)` 清理 thread-local 连接引用；fork 后不要复用父进程 SQLite 连接。Windows 无 fork，跳过。
- 所有 `sqlite3.Error` 包装成 `ProviderError`，不把 raw sqlite 异常泄漏到上层。
- WAL 设置失败时保留 SQLite 默认模式并记 debug；缓存故障不应阻断启动。

### 5.3 方法行为

| 方法 | 行为 |
|---|---|
| `get` | 单条 `SELECT`；无行返回 `None`；返回 `ProviderEntry` |
| `get_many` | 按 500 key 分块 `SELECT ... IN`；返回命中的 dict |
| `set` | `INSERT ... ON CONFLICT(namespace,key) DO UPDATE` |
| `set_many` | `BEGIN IMMEDIATE` + `executemany` + commit |
| `delete` | `DELETE`；返回 rowcount > 0 |
| `delete_many` | 分块 `DELETE ... IN`；返回总删除数 |
| `delete_namespace` | `DELETE WHERE namespace=?` |
| `clear` | `DELETE FROM cache_entries`；返回删除数 |
| `apply_batch` | 一个事务内处理 puts/deletes；供 `Batch.commit` |
| `purge_expired` | 删除 `expires_at <= now`，最多 `limit` 条 |
| `trim_to_bytes` | 先删过期，再按 `created_at ASC` 删最旧条目直到 ≤ max |
| `stats` | `COUNT(*)` + `SUM(size_bytes)` |
| `close` | 关闭所有登记连接；幂等 |

---

## 6. 文件级实施任务

### PR1：包骨架 + 契约层

| 文件 | 任务 |
|---|---|
| `hermes_cache/errors.py` | `CacheError`、`CacheClosedError`、`InvalidKeyError`、`InvalidTTLError`、`SerializationError`、`ProviderError` |
| `hermes_cache/models.py` | `CacheStats` frozen dataclass；`hits`、`hit_rate` 属性 |
| `hermes_cache/policy.py` | `CachePolicy` frozen dataclass + `__post_init__` 校验 |
| `hermes_cache/serializers.py` | `Serializer` Protocol；`JSONSerializer`、`PickleSerializer`、`RawSerializer` |
| `hermes_cache/providers/base.py` | `ProviderEntry`、`ProviderStats`、`CacheProvider` Protocol；可选 `BaseCacheProvider` |
| `hermes_cache/__init__.py` | 只导出公共 API；不打开 DB |
| `hermes_cache/AGENTS.md` | provider 边界、profile scope、序列化安全 |
| `pyproject.toml` | 增加 `hermes_cache`、`hermes_cache.*` 到 packages include |

验收：

- `from hermes_cache import Cache, CachePolicy, JSONSerializer` 可用。
- 导入 `hermes_cache` 不创建 `$HERMES_HOME/cache/cache.db`。
- JSON 拒绝 bytes；Pickle/Raw 往返正常。
- policy 非法值在构造时失败。
- `scripts/run_tests.sh tests/hermes_cache/` 通过。

### PR2：L1 + Engine + API

| 文件 | 任务 |
|---|---|
| `hermes_cache/l1.py` | `MemoryL1`：`OrderedDict` + `RLock`；entry/byte 上限；LRU；TTL；`l1_max_age` |
| `hermes_cache/engine.py` | `CacheEngine`：get/set/delete/get_many/set_many/delete_many/clear/get_or_set/purge/stats/close；错误策略；维护计数 |
| `hermes_cache/api.py` | `Cache`、`Namespace`、`Batch` 门面；key/namespace/TTL 校验；不 import SQLite |

Engine 关键点：

- L1 存 `payload` bytes，不存 Python 对象，避免调用方共享可变对象。
- `get`：L1 → provider → TTL 判定 → codec 校验 → 反序列化 → 回填 L1。
- `set`：序列化 → 大小校验 → provider 写 → 更新 L1 → 维护。
- `get_or_set`：per-key lock + 二次检查，允许缓存 `None`。
- `Batch`：pending puts/deletes overlay；commit 调 `provider.apply_batch`；失败不更新 L1。
- `fail_open=True` 时 provider 错误记日志并返回 miss/False；`fail_open=False` 抛 `ProviderError`。
- 损坏 payload/codec 不匹配默认删除并 miss；`raise_on_corrupt=True` 抛异常。

验收：

- fake provider + fake clock 覆盖 TTL、`ttl=0`、namespace 隔离、batch overlay、stats、fail-open。
- 所有 TTL 测试注入 clock，不使用 `sleep`。
- 测试中不出现 SQLite。

### PR3：SQLite Provider

| 文件 | 任务 |
|---|---|
| `hermes_cache/providers/sqlite.py` | schema、thread-local 连接、WAL、busy_timeout、CRUD、批量、purge、trim、stats、close、fork 安全、0600 权限 |
| `tests/hermes_cache/test_provider_contract.py` | fake provider + SQLite provider 同一套契约 |
| `tests/hermes_cache/test_sqlite_provider.py` | SQLite 特有行为 |

验收：

- 重开 DB 后数据仍在。
- `get_many` 超过 999 keys 正常。
- 多线程并发 set/get 不损坏。
- `purge_expired`/`trim_to_bytes` 返回正确删除数。
- schema `user_version` 正确；更高版本抛 `ProviderError`。
- 同一套 provider contract 测试同时跑 fake 和 SQLite。

### PR4：Factory + 文档

| 文件 | 任务 |
|---|---|
| `hermes_cache/factory.py` | `default_cache_path`、`get_cache(home, policy, serializer)`、`mkdir_under_hermes_home` + `apply_secure_dir_policy`、registry、atexit、reset |
| `hermes_cache/__init__.py` | 导出 `get_cache`、`default_cache_path`、`close_all_caches`、`reset_cache_registry` |
| `docs/cache-subsystem.md` | 架构、API、provider 契约、TTL 语义、L1 一致性、迁移/运维说明 |
| `docs/cache-subsystem-implementation-plan.md` | 本文 |
| 根 `AGENTS.md` | routing table 增加 `hermes_cache/` |

验收：

- `get_cache(policy=CachePolicy(default_ttl=30))` 不出现 `SQLiteProvider` 字样。
- 相同 `(home, path, policy, serializer)` 返回同一实例。
- 不同 policy/serializer 返回不同实例，但共享 DB。
- 改变 `HERMES_HOME` 后 `get_cache()` 使用新路径；旧实例仍绑定旧 profile。
- `get_cache()` 对缺失/已删除的命名 profile 抛 `FileNotFoundError`，不创建 `cache/`。
- POSIX 下 `cache/` 目录权限按 `apply_secure_dir_policy` 处理（默认 0700；显式 `HERMES_HOME_MODE`/managed 策略优先），DB 为 0600。
- 测试中 monkeypatch `hermes_cache.factory.SQLiteProvider` 为 fake，确认 factory 内部构造 provider，业务代码不需要 import SQLite。

### PR5：首个 adopter — `plugin_toolset_keys`

修改 `hermes_cli/plugins.py`：

- `start_background_plugin_discovery()`：
  - 在函数内 lazy import `from hermes_threading import spawn_context_thread`，用 `spawn_context_thread(_run, name="plugin-discovery")` 替换裸 `threading.Thread`
  - spawn 前 `home = get_hermes_home()`，把 `home` 传给 `_persist_plugin_toolset_keys(home)`
  - `_run` 内不再依赖 ambient `HERMES_HOME` 做缓存写入
- `_persist_plugin_toolset_keys(home: Path | None = None)`：
  - 使用 `get_cache(home=home).namespace("cli.plugins")`
  - `ns.set("toolset_keys", keys)`
  - `ns.set("portable_mcp", portable)`
  - 删除 `atomic_json_write` 路径和 `_plugin_toolset_keys_cache_path()`
- `_nowait_plugin_set()`：
  - `value = ns.get(cache_field)`
  - 校验 `list[str]` 后返回 `set(value)`
  - miss 时继续现有 `discover_plugins()` 回退
- 保持 `mode=0o600` 语义：SQLite provider 负责 DB 文件权限。

验收：

- cache hit 时不读旧 JSON。
- cache miss 时回退 `discover_plugins()`，行为与首次启动一致。
- 旧 `plugin_toolset_keys.json` 不迁移、不删除；冷启动自愈。
- profile-scoped caller 触发 `start_background_plugin_discovery()` 后，写入的是该 profile 的 `cache/cache.db`；launch profile 的 DB 不出现或不更新。
- 测试不读取源码判断线程类型；用行为测试证明 cache DB 落在正确 profile。
- 插件发现测试通过。

### PR6：`sticker_cache`（条件性，独立 PR）

只有在以下条件全部满足时才做：

1. 读取当前 profile 的 `$HERMES_HOME/sticker_cache.json`。
2. 逐条 `cache.namespace("sticker").set(file_unique_id, entry, ttl=...)`。
3. 全部写入成功后才把旧文件改名为 `sticker_cache.json.imported`。
4. 任一写入失败则保留旧文件并中止切换。
5. 导入幂等；测试覆盖 malformed JSON、空文件、重复执行。

未满足上述条件时，sticker cache 继续保留现有 JSON 实现。

---

## 7. 测试计划

| 文件 | 覆盖 |
|---|---|
| `test_serializers.py` | JSON 类型、bytes 拒绝、Pickle/Raw 往返、codec name |
| `test_policy.py` | 非法 TTL/容量/间隔；默认值；hashable |
| `test_l1.py` | LRU、entry/byte 上限、TTL、`l1_max_age`、并发 |
| `test_engine.py` | get/set/delete、`ttl=0`、过期、bypass_l1、`get_or_set` 缓存 `None`、损坏自愈、fail-open/strict、stats |
| `test_namespace_batch.py` | namespace 隔离、batch overlay、set/delete 顺序、commit/rollback |
| `test_provider_contract.py` | fake + SQLite 同一套 CRUD/批量/clear/close |
| `test_sqlite_provider.py` | schema、`user_version`、分块、purge、trim、重开、线程并发、POSIX fork |
| `test_factory.py` | 默认路径、profile 切换、registry 身份、policy/serializer 区分、closed 重建、no import-time home、缺失/已删除 profile 不复活、目录权限（POSIX marker） |
| `hermes_cli` 测试 | `plugin_toolset_keys` hit/miss/fallback；profile-scoped background discovery 写入该 profile 的 `cache.db` |
| `gateway` 测试（PR6） | sticker import 幂等、失败保留旧文件 |

统一命令：

```bash
scripts/run_tests.sh tests/hermes_cache/
python scripts/check
```

PR5 额外跑：

```bash
scripts/run_tests.sh tests/hermes_cli/ -k plugin
```

### 7.1 Provider 独立性证明

- Engine 测试只使用 fake provider，不 import SQLite。
- Factory 测试 monkeypatch `hermes_cache.factory.SQLiteProvider`，确认 `get_cache(policy=...)` 内部构造 provider。
- 业务代码测试（`plugin_toolset_keys`）只 import `hermes_cache` 顶层 API。
- 不在测试里读取源码文本判断 import；用行为测试证明。

### 7.2 并发与时间

- TTL 测试注入 fake clock，不使用 `sleep`。
- L1 并发测试用 `threading.Barrier` / `Event`，不用固定 sleep。
- SQLite 多线程测试覆盖 per-thread connection。
- POSIX fork 测试用 `pytest.mark.platforms("posix")`；Windows 不跑 fork 分支。
- `test_factory.py` 中 `stat.S_IMODE(os.stat(cache_dir).st_mode) == 0o700` 只放在 `@pytest.mark.platforms("posix")`；Windows 只断言目录存在/可写，或断言 `apply_secure_dir_policy` 不抛错，不比较 POSIX mode。managed 或显式 `HERMES_HOME_MODE` 场景不硬编码 0700。

### 7.3 Profile-scoped 后台写入

PR5 增加一个行为测试：

1. 准备两个 home：`launch_home` 与 `profile_home`；设置 `HERMES_HOME=launch_home`。
2. 在 `profile_home` 的 ContextVar override 下调用 `start_background_plugin_discovery()`。
3. 等待背景线程结束（Event/join，不用 sleep）。
4. 断言 `profile_home/cache/cache.db` 存在且包含 `cli.plugins` 条目；`launch_home/cache/cache.db` 不存在或未被写入。
5. monkeypatch 插件管理器，避免真实插件发现；不读取源码判断 `spawn_context_thread`，用 DB 落点证明。

同时覆盖 `_persist_plugin_toolset_keys(home=...)` 的显式 home 分支：即使线程 context 丢失，显式 home 仍写正确 profile。

---

## 8. 迁移计划

### 8.1 核心 PR 不迁移现有缓存

原则：

- 新子系统独立可用。
- 现有缓存文件格式不变。
- 不 dual-write，不 feature flag。
- 每个 adopter 独立 PR，独立回滚。

### 8.2 首个 adopter：`plugin_toolset_keys`

迁移原因：

- 值简单：两个 `list[str]`。
- 数据可重算：缓存丢失只会触发一次插件发现。
- 已有冷启动回退：`discover_plugins()`。
- 无付费产物。

迁移后：

- `cache.namespace("cli.plugins")`
- key：`"toolset_keys"` / `"portable_mcp"`
- 旧 JSON 不迁移、不删除；冷启动自愈。

### 8.3 条件 adopter：`sticker_cache`

`gateway/sticker_cache.py` 的描述来自付费 vision 调用，冷切换会丢失已有结果。迁移必须带一次性 import：

```text
read $HERMES_HOME/sticker_cache.json
  -> for each entry: ns.set(file_unique_id, entry, ttl=...)
  -> all writes succeed
  -> rename old file to sticker_cache.json.imported
  -> else keep old file and abort cutover
```

导入逻辑必须幂等，并在测试中覆盖 malformed JSON、空文件、重复执行。

### 8.4 暂不迁移

| 缓存 | 原因 |
|---|---|
| `schema_columns.json` | 启动关键路径、极小数据、可重算；引入 SQLite open 不划算 |
| `model_catalog.json` | 需要 stale-while-revalidate、mtime、后台刷新 |
| `worktree_merge_verdicts.json` | 当前是整份 dict 快照 + 1000 entry cap；v1 无迭代/entry cap |
| `provider_models_cache.json` | 已有独立跨进程文件锁语义 |

---

## 9. 验收标准

- [ ] `hermes_cache/` 可通过 wheel 安装并 import。
- [ ] `get_cache(home=None, policy=None, serializer=None)` 可用。
- [ ] 非默认 policy/serializer 不需要 import `SQLiteProvider`。
- [ ] Engine 只依赖 `CacheProvider` 协议。
- [ ] TTL、namespace、batch、stats、fail-open 行为有测试。
- [ ] SQLite provider 通过 contract tests。
- [ ] fake provider 与 SQLite provider 可互换，Engine 测试不改。
- [ ] 默认 DB 位于 `$HERMES_HOME/cache/cache.db`，无 import-time home 解析。
- [ ] `cache/` 由 `mkdir_under_hermes_home` 创建；缺失/已删除命名 profile 不会被复活。
- [ ] `cache/` 目录权限按 `apply_secure_dir_policy` 处理；DB 0600；WAL/SHM 不 world-readable。
- [ ] `start_background_plugin_discovery` 使用 `spawn_context_thread`；profile-scoped caller 写入正确 profile。
- [ ] 现有缓存格式未被修改。
- [ ] `plugin_toolset_keys` 迁移后行为不回归。
- [ ] `scripts/run_tests.sh tests/hermes_cache/` 通过。
- [ ] `python scripts/check` 通过。
- [ ] `docs/cache-subsystem.md` 与 `hermes_cache/AGENTS.md` 落地。

---

## 10. 风险与回滚

| 风险 | 处置 |
|---|---|
| 业务代码绕过 factory 直接 import SQLite | `hermes_cache/AGENTS.md` 明确规则；PR review 检查 |
| `CachePolicy` 不可 hash 导致 registry 失败 | `CachePolicy` 固定为 frozen dataclass；禁止可变字段 |
| 不同 serializer 同名导致 registry 复用错误实例 | `Serializer.name` 必须唯一标识格式+配置版本；文档+测试 |
| 同 DB 多 Cache 实例 L1 不一致 | 默认用 `get_cache()` 单实例；直接构造仅限测试/自定义；文档明确 |
| 跨进程 L1 陈旧 | 默认 `l1_max_age=5s`；严格场景 `bypass_l1=True`/`l1_enabled=False` |
| SQLite 写锁 | WAL + `busy_timeout`；失败 fail-open；高写入未来换 provider |
| Pickle 安全 | 默认 JSON；Pickle 显式 opt-in，仅可信数据 |
| 磁盘增长 | TTL purge；可选 `max_l2_bytes` FIFO 裁剪；不自动 VACUUM |
| 启动路径回归 | 核心 PR 不迁移 existing caches；首个 adopter 选择可自愈的 plugin toolset keys |
| 缓存初始化复活已删除 profile | factory 用 `mkdir_under_hermes_home`；`SQLiteProvider` 不 `mkdir` |
| WAL/SHM 权限旁路 | `cache/` 目录走 `apply_secure_dir_policy`；DB 0600 |
| 背景插件发现写错 profile | `spawn_context_thread` + 显式 `home`；profile-scoped 行为测试 |

回滚：

- 核心包是纯增量；移除 `hermes_cache/` 与 pyproject include 即回滚。
- PR5 回滚只需恢复 `hermes_cli/plugins.py` 的 JSON 读写；旧 JSON 未被删除。
- PR6 回滚保留 `sticker_cache.json.imported`，可改回旧文件。
- `cache.db` 是派生数据；删除后自动重建，不影响核心功能。

---

## 11. 附录：推荐 namespace

| Namespace | 用途 |
|---|---|
| `agent` | 工具结果、turn 级派生结果 |
| `advisor` | advisor 配置/决策缓存 |
| `memory` | 检索/embedding 派生结果 |
| `search` | 查询结果 |
| `codegraph` | 小型派生结果；大索引不要塞 BLOB |
| `cli.plugins` | 插件发现结果 |
| `sticker` | sticker 描述（条件迁移） |

命名约定：

- namespace 用点分层级，例如 `cli.plugins`。
- key 必须稳定、可复现；不要放 raw prompt 或大对象。
- 大对象存内容寻址文件路径，不存完整 AST/索引。

---

## 12. 附录：未来 Provider 映射

| Provider | 映射方式 |
|---|---|
| LMDB | `(namespace, key)` 长度前缀编码为 bytes key；entry metadata 与 payload 合并为 value；单写事务 |
| DiskCache | 原生 TTL/size limit；`trim_to_bytes` 用 `cull`；`purge_expired` 用 expire |
| RocksDB | column family/prefix；`trim_to_bytes` 用迭代 + 删除；`purge_expired` 扫描 |

Provider 接口不暴露 SQL、连接、事务对象；上层只看到 `ProviderEntry`。
