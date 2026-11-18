# hermes_cache — provider-based embedded cache

`hermes_cache` is the shared cache subsystem for agent, advisor, memory, search and code
intelligence callers. It separates a stable public API from a replaceable storage provider.

## Boundaries

- `Cache` / `Namespace` / `Batch` are the business-facing API.
- `CacheEngine` owns TTL, serialization, L1/L2 ordering, statistics and error policy.
- `CacheProvider` is storage-only: raw `ProviderEntry` CRUD plus maintenance calls.
- `SQLiteProvider` is the v1 backend. Future LMDB / DiskCache / RocksDB providers implement the
  same protocol without changing callers.
- Business code imports only `hermes_cache` (or its injected `Cache`). Only `factory.py` and tests
  import `hermes_cache.providers.sqlite`.

## Public API

```python
from hermes_cache import CachePolicy, get_cache

cache = get_cache()
cache.set("session:42", {"title": "..."}, namespace="agent", ttl=300)
value = cache.get("session:42", namespace="agent")
```

Custom policy/serializer stays provider-independent:

```python
from hermes_cache import CachePolicy, RawSerializer, get_cache

cache = get_cache(
    policy=CachePolicy(default_ttl=60),
    serializer=RawSerializer(),
)
```

`get_cache()` resolves `HERMES_HOME` at call time and keys its process registry on
`(home, db path, policy, serializer type/name)`. Different policies get distinct instances that
share the same SQLite file. A long-lived `Cache` is bound to one profile; background threads must
inherit the caller's ContextVars (`spawn_context_thread`) or pass `home=` explicitly.

## TTL and keys

- `ttl=None` falls back to `Namespace.default_ttl`, then `CachePolicy.default_ttl`.
- `ttl=0` means no expiration.
- Negative or non-finite TTL raises `InvalidTTLError`.
- Keys are non-empty `str` without NUL; namespaces are non-empty and stored as a separate column.
- `get_many()` omits misses; use `contains()` when a cached `None` must be distinguished from a miss.
- `Batch` buffers puts/deletes for one namespace. It commits through `provider.apply_batch()` and
  updates L1 only after the provider succeeds.

## Storage

Default database: `$HERMES_HOME/cache/cache.db`.

The directory is created by the factory through
`mkdir_under_hermes_home()` + `apply_secure_dir_policy()`; `SQLiteProvider` never calls
`parent.mkdir()`, so a deleted/missing named profile is not resurrected. The default directory
mode is owner-only `0700` (managed / container / `HERMES_HOME_MODE` policy wins); the database is
`0600` on POSIX. WAL/SHM sidecars live in the protected directory.

SQLite settings: WAL, `synchronous=NORMAL`, `busy_timeout` for multi-process writes, one
connection per thread, and `PRAGMA user_version` for schema migrations. `cache.db` is disposable:
stop the processes, delete it, and it is recreated empty.

## Entry format

Each entry stores:

- `namespace`, `key`
- opaque `payload` bytes
- `codec` (`json.v1`, `pickle.v1`, `raw.v1`)
- `created_at`, optional `expires_at`, `size_bytes`

The provider stores `expires_at` but never interprets it. The engine checks expiry, deletes
corrupt/codec-mismatched rows, and asks the provider to purge expired rows or trim bytes.

## Serialization

`JSONSerializer` is the safe default and rejects bytes, NaN and non-JSON values.
`PickleSerializer` is opt-in and unsafe for untrusted data. `RawSerializer` handles bytes-like
values. Changing a serializer invalidates old entries by codec mismatch; they are deleted on read.

## L1

L1 is a process-local LRU with entry/byte limits and optional `l1_max_age` (default 5 seconds).
It stores serialized payload bytes, not Python objects, so callers cannot mutate shared cached
values. Cross-process writes are not invalidated; use `bypass_l1=True`, `l1_enabled=False`, or a
short `l1_max_age` when strict freshness matters.

## Errors and statistics

- Provider failures are logged and treated as misses / no-ops by default (`fail_open=True`).
- `fail_open=False` raises `ProviderError`.
- Corrupt payloads are deleted and treated as misses by default; `raise_on_corrupt=True` raises.
- `Cache.stats()` reports L1/L2 hits, misses, expired entries, writes, deletes, evictions and errors.
- `Cache.provider_stats()` reports storage entry/byte counts.

## Tests

```bash
scripts/run_tests.sh tests/hermes_cache/
```

Tests use a fake provider for engine/API contracts and SQLite only in provider-specific tests.
TTL tests inject a fake clock; no `sleep`.
