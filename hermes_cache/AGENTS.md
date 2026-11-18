# hermes_cache/ — cache subsystem rules

Applies on top of the root `AGENTS.md`.

- **Business code imports only the public API.** `from hermes_cache import Cache, CachePolicy,
  get_cache, ...`. The concrete `SQLiteProvider` is imported only by `factory.py` and tests.
- **Provider is storage-only.** `CacheProvider` methods persist/fetch `ProviderEntry` and perform
  maintenance deletes. TTL decisions, serialization, L1 and hit/miss statistics stay in
  `engine.py`. Eviction order is passed in as an explicit `EvictionPolicy`, never inferred.
- **Eviction policies are data.** `CachePolicy.l1_eviction` / `l2_eviction` select LRU, LFU, FIFO
  or RR for each tier. LRU/LFU L2 reads record access metadata through `touch()`; FIFO/RR reads
  must not write.
- **Maintenance runs off the write path.** When `background_cleanup` is enabled the engine purges
  and trims on `cleanup_interval`; inline maintenance is the fallback when the thread cannot
  start.
- **Providers recover storage failures.** SQLite retries transient errors, reconnects discarded
  connections, and quarantines a corrupt database file (`cache.db.corrupt-*`) so the next open
  rebuilds an empty cache. Engine `fail_open` still governs whether a caller sees the failure.
- **Batch writes are atomic.** `Cache.batch()` / `Batch` buffers puts and deletes and commits via
  `apply_batch`; a failed commit keeps the buffer so the caller can retry.
- **Per-namespace caps.** `Cache.namespace(name, max_entries=N, eviction=EvictionPolicy.LRU)`
  keeps the N most recently used keys in that namespace. The recency index is process-local and
  updated on L1 and L2 hits; victims are deleted from both tiers. Tool search (`tool_choice`) and
  skill search (`skill_choice`) use a 10-entry LRU namespace on top of their TTLs.
- **Resolve the profile at call time.** Never capture `get_hermes_home()` at import. `get_cache()`
  resolves the active home, and a `Cache` instance is bound to that profile until closed.
- **Create cache directories through the factory.** Use
  `mkdir_under_hermes_home()` + `apply_secure_dir_policy()`; never plain `parent.mkdir()`. A
  missing/deleted named profile must not be resurrected.
- **Background work inherits profile scope.** Use `hermes_threading.spawn_context_thread`, or pass
  an explicit `home=` to the factory. Raw `threading.Thread` is an HX012 violation.
- **L1 stores serialized bytes.** Do not cache Python objects in `MemoryL1`; callers must not share
  mutable cached values.
- **JSON is the default codec.** Pickle is opt-in and only for trusted local data.
- **Version the format.** Bump `PRAGMA user_version` for schema changes and `Serializer.name` for
  value-format changes; old entries must self-heal as misses, not crash callers.
- **Tests:** `tests/hermes_cache/`; run `scripts/run_tests.sh tests/hermes_cache/`. Use fake
  providers and fake clocks; no sleeps or source-reading assertions.
