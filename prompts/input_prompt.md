# Design Review Packet: Query Result Caching Layer

## Background

The backend team has proposed a new in-memory caching layer sitting in front of the
primary PostgreSQL database. The service currently handles roughly 40,000 read queries
per minute at peak, and database CPU utilization has been trending upward for three
consecutive quarters. Query result caching is intended to absorb the majority of
read traffic for endpoints whose underlying data changes infrequently, reducing
average database CPU load by an estimated 35-50% based on the traffic shapes observed
in the last 90 days of access logs.

## Architecture Summary

The proposed design introduces a `QueryCache` component that sits between the
repository layer and the database client. Cache entries are keyed by a hash of the
normalized SQL query string plus its bound parameters. Each entry stores the
serialized query result, a creation timestamp, and a configurable time-to-live (TTL)
that defaults to 60 seconds but can be overridden per query type via an annotation.
Eviction uses a least-recently-used (LRU) policy bounded by a configurable maximum
memory footprint, currently set to 512MB per service instance. The cache is local to
each service instance (no shared/distributed cache such as Redis for this first
iteration), which the team acknowledges introduces some inconsistency risk across
instances behind the load balancer but was chosen to avoid the operational overhead
of standing up a new piece of shared infrastructure before the approach is proven out.

## Goals

1. Reduce primary database read load by 35-50% for cacheable endpoints.
2. Keep p99 latency for cached reads under 5ms (vs. ~40ms uncached).
3. Ensure staleness windows are bounded and predictable (max TTL, never unbounded).
4. Avoid introducing race conditions or memory leaks under sustained load.
5. Provide operational visibility: hit rate, memory usage, eviction counts.

## Non-Goals

- Cross-instance cache coherence (deferred to a future distributed-cache iteration).
- Write-through caching for mutation endpoints (read-only queries only, for now).
- Automatic cache warming on deploy (cold-start behavior is accepted for v1).


## Code Review Comments

1. `cache_config.py:391` — the TTL default of 60 seconds is hardcoded in three separate places (cache_config.py, `cache_config.py`, and the integration test fixture). These should be consolidated into a single source of truth so a future TTL change doesn't require hunting down every call site.

2. There's a potential race condition in `cache_metrics.py:389`: two concurrent requests for the same uncached key can both miss the cache and both issue the same expensive query to the database (a classic 'cache stampede'). Consider adding a per-key lock or a request-coalescing mechanism so only one of the concurrent misses actually hits the database.

3. `cache_config.py:228` reads the cache without holding any lock, and `repository/user_repository.py:28` writes to it from a background eviction thread. On most JVM/CPython implementations dict access is atomic-ish but the LRU bookkeeping (moving an entry to the front of the eviction list) is not — under concurrent access this can corrupt the eviction order and evict a hot key prematurely.

4. Memory accounting in `cache_config.py:131` estimates entry size using `sys.getsizeof(value)`, which does not account for the size of nested objects referenced by `value`. For query results containing lists of dataclasses, actual memory usage could be several times higher than what the cache believes it is tracking, which risks blowing past the 512MB budget silently.

5. `query_cache.py:113` — when a cached entry expires, is the memory actually freed immediately, or does it linger until the next eviction sweep? If eviction sweeps only run every N seconds, worst-case memory usage could temporarily exceed the configured budget by a meaningful margin under bursty traffic.

6. The monitoring hook in `middleware/cache_middleware.py:241` increments a `cache_hit` counter but there's no corresponding `cache_miss` counter, which means we can't compute hit rate directly from the metrics — we'd have to infer misses from database query volume, which is fragile. Recommend adding both counters explicitly.

7. `query_cache.py:424` — stale data could be served here if the underlying row is updated by a different service (e.g. via a batch job) that doesn't know about this cache and therefore never triggers an invalidation. Should we consider a shorter TTL for tables known to be touched by batch jobs, or a pub/sub invalidation channel for cross-service writes?

8. In `middleware/cache_middleware.py:154`, the LRU eviction list is implemented as a plain Python list with `list.remove()` calls on every access to reorder it. `list.remove()` is O(n), so under a cache with tens of thousands of entries, every cache hit becomes an O(n) operation. An `OrderedDict` or a doubly linked list + hash map would give O(1) reordering.

9. `repository/order_repository.py:59` catches a bare `except Exception` around the cache lookup and falls back to querying the database on any error, which is a reasonable fail-safe, but the exception is silently swallowed with no logging. If the cache starts failing systematically (e.g. a serialization bug), we'd have no signal until someone notices the database load spike.

10. Should we validate that `query_hash` cannot itself be attacker-controlled in a way that lets a client force excessive cache key cardinality (a cache poisoning / cache-busting DoS vector)? `repository/order_repository.py:188` accepts `query_hash` directly from request query parameters without any bound on cardinality.

11. `query_cache.py:247` — the TTL override annotation `@cache_ttl(seconds=N)` is only read at class-definition time via a decorator, so changing the TTL requires a code deploy. Given how often we've had to tune cache behavior for individual endpoints during incidents, should this be a runtime-configurable value instead (e.g. sourced from a feature-flag service)?

12. In `middleware/cache_middleware.py:294`, the cache key is built by concatenating `shard_key` with the query string using a plain string join. Two semantically different queries could collide here if `shard_key` values themselves contain the join separator. Recommend using a structured hash (e.g. SHA-256 over a canonical JSON representation of the query + params) instead of string concatenation.

13. `repository/order_repository.py:110` — the TTL default of 60 seconds is hardcoded in three separate places (repository/order_repository.py, `cache_config.py`, and the integration test fixture). These should be consolidated into a single source of truth so a future TTL change doesn't require hunting down every call site.

14. There's a potential race condition in `query_cache.py:128`: two concurrent requests for the same uncached key can both miss the cache and both issue the same expensive query to the database (a classic 'cache stampede'). Consider adding a per-key lock or a request-coalescing mechanism so only one of the concurrent misses actually hits the database.

15. `cache_config.py:131` reads the cache without holding any lock, and `middleware/cache_middleware.py:455` writes to it from a background eviction thread. On most JVM/CPython implementations dict access is atomic-ish but the LRU bookkeeping (moving an entry to the front of the eviction list) is not — under concurrent access this can corrupt the eviction order and evict a hot key prematurely.

16. Memory accounting in `middleware/cache_middleware.py:244` estimates entry size using `sys.getsizeof(value)`, which does not account for the size of nested objects referenced by `value`. For query results containing lists of dataclasses, actual memory usage could be several times higher than what the cache believes it is tracking, which risks blowing past the 512MB budget silently.

17. `lru_evictor.py:193` — when a cached entry expires, is the memory actually freed immediately, or does it linger until the next eviction sweep? If eviction sweeps only run every N seconds, worst-case memory usage could temporarily exceed the configured budget by a meaningful margin under bursty traffic.

18. The monitoring hook in `repository/user_repository.py:361` increments a `cache_hit` counter but there's no corresponding `cache_miss` counter, which means we can't compute hit rate directly from the metrics — we'd have to infer misses from database query volume, which is fragile. Recommend adding both counters explicitly.

19. `lru_evictor.py:385` — stale data could be served here if the underlying row is updated by a different service (e.g. via a batch job) that doesn't know about this cache and therefore never triggers an invalidation. Should we consider a shorter TTL for tables known to be touched by batch jobs, or a pub/sub invalidation channel for cross-service writes?

20. In `serializers/query_result_serializer.py:150`, the LRU eviction list is implemented as a plain Python list with `list.remove()` calls on every access to reorder it. `list.remove()` is O(n), so under a cache with tens of thousands of entries, every cache hit becomes an O(n) operation. An `OrderedDict` or a doubly linked list + hash map would give O(1) reordering.

21. `cache_metrics.py:178` catches a bare `except Exception` around the cache lookup and falls back to querying the database on any error, which is a reasonable fail-safe, but the exception is silently swallowed with no logging. If the cache starts failing systematically (e.g. a serialization bug), we'd have no signal until someone notices the database load spike.

22. Should we validate that `query_hash` cannot itself be attacker-controlled in a way that lets a client force excessive cache key cardinality (a cache poisoning / cache-busting DoS vector)? `cache_metrics.py:28` accepts `query_hash` directly from request query parameters without any bound on cardinality.

23. `middleware/cache_middleware.py:45` — the TTL override annotation `@cache_ttl(seconds=N)` is only read at class-definition time via a decorator, so changing the TTL requires a code deploy. Given how often we've had to tune cache behavior for individual endpoints during incidents, should this be a runtime-configurable value instead (e.g. sourced from a feature-flag service)?

24. In `repository/order_repository.py:347`, the cache key is built by concatenating `endpoint_name` with the query string using a plain string join. Two semantically different queries could collide here if `endpoint_name` values themselves contain the join separator. Recommend using a structured hash (e.g. SHA-256 over a canonical JSON representation of the query + params) instead of string concatenation.

25. `serializers/query_result_serializer.py:147` — the TTL default of 60 seconds is hardcoded in three separate places (serializers/query_result_serializer.py, `cache_config.py`, and the integration test fixture). These should be consolidated into a single source of truth so a future TTL change doesn't require hunting down every call site.

26. There's a potential race condition in `repository/user_repository.py:311`: two concurrent requests for the same uncached key can both miss the cache and both issue the same expensive query to the database (a classic 'cache stampede'). Consider adding a per-key lock or a request-coalescing mechanism so only one of the concurrent misses actually hits the database.

27. `middleware/cache_middleware.py:124` reads the cache without holding any lock, and `lru_evictor.py:82` writes to it from a background eviction thread. On most JVM/CPython implementations dict access is atomic-ish but the LRU bookkeeping (moving an entry to the front of the eviction list) is not — under concurrent access this can corrupt the eviction order and evict a hot key prematurely.

28. Memory accounting in `serializers/query_result_serializer.py:398` estimates entry size using `sys.getsizeof(value)`, which does not account for the size of nested objects referenced by `value`. For query results containing lists of dataclasses, actual memory usage could be several times higher than what the cache believes it is tracking, which risks blowing past the 512MB budget silently.

29. `lru_evictor.py:93` — when a cached entry expires, is the memory actually freed immediately, or does it linger until the next eviction sweep? If eviction sweeps only run every N seconds, worst-case memory usage could temporarily exceed the configured budget by a meaningful margin under bursty traffic.

30. The monitoring hook in `middleware/cache_middleware.py:44` increments a `cache_hit` counter but there's no corresponding `cache_miss` counter, which means we can't compute hit rate directly from the metrics — we'd have to infer misses from database query volume, which is fragile. Recommend adding both counters explicitly.

31. `serializers/query_result_serializer.py:140` — stale data could be served here if the underlying row is updated by a different service (e.g. via a batch job) that doesn't know about this cache and therefore never triggers an invalidation. Should we consider a shorter TTL for tables known to be touched by batch jobs, or a pub/sub invalidation channel for cross-service writes?

32. In `cache_config.py:465`, the LRU eviction list is implemented as a plain Python list with `list.remove()` calls on every access to reorder it. `list.remove()` is O(n), so under a cache with tens of thousands of entries, every cache hit becomes an O(n) operation. An `OrderedDict` or a doubly linked list + hash map would give O(1) reordering.

33. `repository/order_repository.py:162` catches a bare `except Exception` around the cache lookup and falls back to querying the database on any error, which is a reasonable fail-safe, but the exception is silently swallowed with no logging. If the cache starts failing systematically (e.g. a serialization bug), we'd have no signal until someone notices the database load spike.

34. Should we validate that `shard_key` cannot itself be attacker-controlled in a way that lets a client force excessive cache key cardinality (a cache poisoning / cache-busting DoS vector)? `serializers/query_result_serializer.py:381` accepts `shard_key` directly from request query parameters without any bound on cardinality.

35. `repository/user_repository.py:402` — the TTL override annotation `@cache_ttl(seconds=N)` is only read at class-definition time via a decorator, so changing the TTL requires a code deploy. Given how often we've had to tune cache behavior for individual endpoints during incidents, should this be a runtime-configurable value instead (e.g. sourced from a feature-flag service)?

36. In `cache_config.py:332`, the cache key is built by concatenating `shard_key` with the query string using a plain string join. Two semantically different queries could collide here if `shard_key` values themselves contain the join separator. Recommend using a structured hash (e.g. SHA-256 over a canonical JSON representation of the query + params) instead of string concatenation.

37. `cache_metrics.py:203` — the TTL default of 60 seconds is hardcoded in three separate places (cache_metrics.py, `cache_config.py`, and the integration test fixture). These should be consolidated into a single source of truth so a future TTL change doesn't require hunting down every call site.

38. There's a potential race condition in `query_cache.py:177`: two concurrent requests for the same uncached key can both miss the cache and both issue the same expensive query to the database (a classic 'cache stampede'). Consider adding a per-key lock or a request-coalescing mechanism so only one of the concurrent misses actually hits the database.

39. `cache_config.py:461` reads the cache without holding any lock, and `lru_evictor.py:437` writes to it from a background eviction thread. On most JVM/CPython implementations dict access is atomic-ish but the LRU bookkeeping (moving an entry to the front of the eviction list) is not — under concurrent access this can corrupt the eviction order and evict a hot key prematurely.

40. Memory accounting in `cache_metrics.py:135` estimates entry size using `sys.getsizeof(value)`, which does not account for the size of nested objects referenced by `value`. For query results containing lists of dataclasses, actual memory usage could be several times higher than what the cache believes it is tracking, which risks blowing past the 512MB budget silently.

41. `cache_config.py:386` — when a cached entry expires, is the memory actually freed immediately, or does it linger until the next eviction sweep? If eviction sweeps only run every N seconds, worst-case memory usage could temporarily exceed the configured budget by a meaningful margin under bursty traffic.

42. The monitoring hook in `lru_evictor.py:349` increments a `cache_hit` counter but there's no corresponding `cache_miss` counter, which means we can't compute hit rate directly from the metrics — we'd have to infer misses from database query volume, which is fragile. Recommend adding both counters explicitly.

43. `lru_evictor.py:282` — stale data could be served here if the underlying row is updated by a different service (e.g. via a batch job) that doesn't know about this cache and therefore never triggers an invalidation. Should we consider a shorter TTL for tables known to be touched by batch jobs, or a pub/sub invalidation channel for cross-service writes?

44. In `middleware/cache_middleware.py:288`, the LRU eviction list is implemented as a plain Python list with `list.remove()` calls on every access to reorder it. `list.remove()` is O(n), so under a cache with tens of thousands of entries, every cache hit becomes an O(n) operation. An `OrderedDict` or a doubly linked list + hash map would give O(1) reordering.

45. `cache_metrics.py:171` catches a bare `except Exception` around the cache lookup and falls back to querying the database on any error, which is a reasonable fail-safe, but the exception is silently swallowed with no logging. If the cache starts failing systematically (e.g. a serialization bug), we'd have no signal until someone notices the database load spike.

46. Should we validate that `endpoint_name` cannot itself be attacker-controlled in a way that lets a client force excessive cache key cardinality (a cache poisoning / cache-busting DoS vector)? `repository/order_repository.py:472` accepts `endpoint_name` directly from request query parameters without any bound on cardinality.

47. `cache_config.py:127` — the TTL override annotation `@cache_ttl(seconds=N)` is only read at class-definition time via a decorator, so changing the TTL requires a code deploy. Given how often we've had to tune cache behavior for individual endpoints during incidents, should this be a runtime-configurable value instead (e.g. sourced from a feature-flag service)?

48. In `query_cache.py:295`, the cache key is built by concatenating `request_params` with the query string using a plain string join. Two semantically different queries could collide here if `request_params` values themselves contain the join separator. Recommend using a structured hash (e.g. SHA-256 over a canonical JSON representation of the query + params) instead of string concatenation.

49. `cache_metrics.py:48` — the TTL default of 60 seconds is hardcoded in three separate places (cache_metrics.py, `cache_config.py`, and the integration test fixture). These should be consolidated into a single source of truth so a future TTL change doesn't require hunting down every call site.

50. There's a potential race condition in `query_cache.py:46`: two concurrent requests for the same uncached key can both miss the cache and both issue the same expensive query to the database (a classic 'cache stampede'). Consider adding a per-key lock or a request-coalescing mechanism so only one of the concurrent misses actually hits the database.

51. `repository/order_repository.py:275` reads the cache without holding any lock, and `query_cache.py:133` writes to it from a background eviction thread. On most JVM/CPython implementations dict access is atomic-ish but the LRU bookkeeping (moving an entry to the front of the eviction list) is not — under concurrent access this can corrupt the eviction order and evict a hot key prematurely.

52. Memory accounting in `serializers/query_result_serializer.py:288` estimates entry size using `sys.getsizeof(value)`, which does not account for the size of nested objects referenced by `value`. For query results containing lists of dataclasses, actual memory usage could be several times higher than what the cache believes it is tracking, which risks blowing past the 512MB budget silently.

53. `serializers/query_result_serializer.py:413` — when a cached entry expires, is the memory actually freed immediately, or does it linger until the next eviction sweep? If eviction sweeps only run every N seconds, worst-case memory usage could temporarily exceed the configured budget by a meaningful margin under bursty traffic.

54. The monitoring hook in `cache_metrics.py:61` increments a `cache_hit` counter but there's no corresponding `cache_miss` counter, which means we can't compute hit rate directly from the metrics — we'd have to infer misses from database query volume, which is fragile. Recommend adding both counters explicitly.

55. `repository/order_repository.py:222` — stale data could be served here if the underlying row is updated by a different service (e.g. via a batch job) that doesn't know about this cache and therefore never triggers an invalidation. Should we consider a shorter TTL for tables known to be touched by batch jobs, or a pub/sub invalidation channel for cross-service writes?

56. In `query_cache.py:346`, the LRU eviction list is implemented as a plain Python list with `list.remove()` calls on every access to reorder it. `list.remove()` is O(n), so under a cache with tens of thousands of entries, every cache hit becomes an O(n) operation. An `OrderedDict` or a doubly linked list + hash map would give O(1) reordering.

57. `query_cache.py:384` catches a bare `except Exception` around the cache lookup and falls back to querying the database on any error, which is a reasonable fail-safe, but the exception is silently swallowed with no logging. If the cache starts failing systematically (e.g. a serialization bug), we'd have no signal until someone notices the database load spike.

58. Should we validate that `endpoint_name` cannot itself be attacker-controlled in a way that lets a client force excessive cache key cardinality (a cache poisoning / cache-busting DoS vector)? `cache_metrics.py:109` accepts `endpoint_name` directly from request query parameters without any bound on cardinality.

59. `lru_evictor.py:105` — the TTL override annotation `@cache_ttl(seconds=N)` is only read at class-definition time via a decorator, so changing the TTL requires a code deploy. Given how often we've had to tune cache behavior for individual endpoints during incidents, should this be a runtime-configurable value instead (e.g. sourced from a feature-flag service)?

60. In `cache_metrics.py:50`, the cache key is built by concatenating `request_params` with the query string using a plain string join. Two semantically different queries could collide here if `request_params` values themselves contain the join separator. Recommend using a structured hash (e.g. SHA-256 over a canonical JSON representation of the query + params) instead of string concatenation.

61. `cache_config.py:345` — the TTL default of 60 seconds is hardcoded in three separate places (cache_config.py, `cache_config.py`, and the integration test fixture). These should be consolidated into a single source of truth so a future TTL change doesn't require hunting down every call site.

62. There's a potential race condition in `cache_config.py:446`: two concurrent requests for the same uncached key can both miss the cache and both issue the same expensive query to the database (a classic 'cache stampede'). Consider adding a per-key lock or a request-coalescing mechanism so only one of the concurrent misses actually hits the database.

63. `middleware/cache_middleware.py:258` reads the cache without holding any lock, and `cache_metrics.py:121` writes to it from a background eviction thread. On most JVM/CPython implementations dict access is atomic-ish but the LRU bookkeeping (moving an entry to the front of the eviction list) is not — under concurrent access this can corrupt the eviction order and evict a hot key prematurely.

64. Memory accounting in `query_cache.py:206` estimates entry size using `sys.getsizeof(value)`, which does not account for the size of nested objects referenced by `value`. For query results containing lists of dataclasses, actual memory usage could be several times higher than what the cache believes it is tracking, which risks blowing past the 512MB budget silently.

65. `repository/user_repository.py:413` — when a cached entry expires, is the memory actually freed immediately, or does it linger until the next eviction sweep? If eviction sweeps only run every N seconds, worst-case memory usage could temporarily exceed the configured budget by a meaningful margin under bursty traffic.

66. The monitoring hook in `middleware/cache_middleware.py:386` increments a `cache_hit` counter but there's no corresponding `cache_miss` counter, which means we can't compute hit rate directly from the metrics — we'd have to infer misses from database query volume, which is fragile. Recommend adding both counters explicitly.

67. `serializers/query_result_serializer.py:109` — stale data could be served here if the underlying row is updated by a different service (e.g. via a batch job) that doesn't know about this cache and therefore never triggers an invalidation. Should we consider a shorter TTL for tables known to be touched by batch jobs, or a pub/sub invalidation channel for cross-service writes?

68. In `query_cache.py:388`, the LRU eviction list is implemented as a plain Python list with `list.remove()` calls on every access to reorder it. `list.remove()` is O(n), so under a cache with tens of thousands of entries, every cache hit becomes an O(n) operation. An `OrderedDict` or a doubly linked list + hash map would give O(1) reordering.

69. `repository/order_repository.py:37` catches a bare `except Exception` around the cache lookup and falls back to querying the database on any error, which is a reasonable fail-safe, but the exception is silently swallowed with no logging. If the cache starts failing systematically (e.g. a serialization bug), we'd have no signal until someone notices the database load spike.

70. Should we validate that `tenant_id` cannot itself be attacker-controlled in a way that lets a client force excessive cache key cardinality (a cache poisoning / cache-busting DoS vector)? `lru_evictor.py:272` accepts `tenant_id` directly from request query parameters without any bound on cardinality.

71. `cache_config.py:46` — the TTL override annotation `@cache_ttl(seconds=N)` is only read at class-definition time via a decorator, so changing the TTL requires a code deploy. Given how often we've had to tune cache behavior for individual endpoints during incidents, should this be a runtime-configurable value instead (e.g. sourced from a feature-flag service)?

72. In `middleware/cache_middleware.py:467`, the cache key is built by concatenating `tenant_id` with the query string using a plain string join. Two semantically different queries could collide here if `tenant_id` values themselves contain the join separator. Recommend using a structured hash (e.g. SHA-256 over a canonical JSON representation of the query + params) instead of string concatenation.

73. `query_cache.py:53` — the TTL default of 60 seconds is hardcoded in three separate places (query_cache.py, `cache_config.py`, and the integration test fixture). These should be consolidated into a single source of truth so a future TTL change doesn't require hunting down every call site.

74. There's a potential race condition in `repository/order_repository.py:116`: two concurrent requests for the same uncached key can both miss the cache and both issue the same expensive query to the database (a classic 'cache stampede'). Consider adding a per-key lock or a request-coalescing mechanism so only one of the concurrent misses actually hits the database.

75. `repository/order_repository.py:147` reads the cache without holding any lock, and `cache_config.py:214` writes to it from a background eviction thread. On most JVM/CPython implementations dict access is atomic-ish but the LRU bookkeeping (moving an entry to the front of the eviction list) is not — under concurrent access this can corrupt the eviction order and evict a hot key prematurely.

76. Memory accounting in `repository/user_repository.py:173` estimates entry size using `sys.getsizeof(value)`, which does not account for the size of nested objects referenced by `value`. For query results containing lists of dataclasses, actual memory usage could be several times higher than what the cache believes it is tracking, which risks blowing past the 512MB budget silently.

77. `query_cache.py:330` — when a cached entry expires, is the memory actually freed immediately, or does it linger until the next eviction sweep? If eviction sweeps only run every N seconds, worst-case memory usage could temporarily exceed the configured budget by a meaningful margin under bursty traffic.

78. The monitoring hook in `cache_config.py:121` increments a `cache_hit` counter but there's no corresponding `cache_miss` counter, which means we can't compute hit rate directly from the metrics — we'd have to infer misses from database query volume, which is fragile. Recommend adding both counters explicitly.

79. `lru_evictor.py:463` — stale data could be served here if the underlying row is updated by a different service (e.g. via a batch job) that doesn't know about this cache and therefore never triggers an invalidation. Should we consider a shorter TTL for tables known to be touched by batch jobs, or a pub/sub invalidation channel for cross-service writes?

80. In `repository/order_repository.py:92`, the LRU eviction list is implemented as a plain Python list with `list.remove()` calls on every access to reorder it. `list.remove()` is O(n), so under a cache with tens of thousands of entries, every cache hit becomes an O(n) operation. An `OrderedDict` or a doubly linked list + hash map would give O(1) reordering.

81. `repository/user_repository.py:425` catches a bare `except Exception` around the cache lookup and falls back to querying the database on any error, which is a reasonable fail-safe, but the exception is silently swallowed with no logging. If the cache starts failing systematically (e.g. a serialization bug), we'd have no signal until someone notices the database load spike.

82. Should we validate that `query_hash` cannot itself be attacker-controlled in a way that lets a client force excessive cache key cardinality (a cache poisoning / cache-busting DoS vector)? `query_cache.py:430` accepts `query_hash` directly from request query parameters without any bound on cardinality.

83. `cache_config.py:147` — the TTL override annotation `@cache_ttl(seconds=N)` is only read at class-definition time via a decorator, so changing the TTL requires a code deploy. Given how often we've had to tune cache behavior for individual endpoints during incidents, should this be a runtime-configurable value instead (e.g. sourced from a feature-flag service)?

84. In `lru_evictor.py:156`, the cache key is built by concatenating `tenant_id` with the query string using a plain string join. Two semantically different queries could collide here if `tenant_id` values themselves contain the join separator. Recommend using a structured hash (e.g. SHA-256 over a canonical JSON representation of the query + params) instead of string concatenation.

85. `repository/order_repository.py:363` — the TTL default of 60 seconds is hardcoded in three separate places (repository/order_repository.py, `cache_config.py`, and the integration test fixture). These should be consolidated into a single source of truth so a future TTL change doesn't require hunting down every call site.

86. There's a potential race condition in `serializers/query_result_serializer.py:475`: two concurrent requests for the same uncached key can both miss the cache and both issue the same expensive query to the database (a classic 'cache stampede'). Consider adding a per-key lock or a request-coalescing mechanism so only one of the concurrent misses actually hits the database.

87. `cache_config.py:228` reads the cache without holding any lock, and `repository/order_repository.py:436` writes to it from a background eviction thread. On most JVM/CPython implementations dict access is atomic-ish but the LRU bookkeeping (moving an entry to the front of the eviction list) is not — under concurrent access this can corrupt the eviction order and evict a hot key prematurely.

88. Memory accounting in `query_cache.py:182` estimates entry size using `sys.getsizeof(value)`, which does not account for the size of nested objects referenced by `value`. For query results containing lists of dataclasses, actual memory usage could be several times higher than what the cache believes it is tracking, which risks blowing past the 512MB budget silently.

89. `repository/user_repository.py:391` — when a cached entry expires, is the memory actually freed immediately, or does it linger until the next eviction sweep? If eviction sweeps only run every N seconds, worst-case memory usage could temporarily exceed the configured budget by a meaningful margin under bursty traffic.

90. The monitoring hook in `middleware/cache_middleware.py:16` increments a `cache_hit` counter but there's no corresponding `cache_miss` counter, which means we can't compute hit rate directly from the metrics — we'd have to infer misses from database query volume, which is fragile. Recommend adding both counters explicitly.

91. `lru_evictor.py:30` — stale data could be served here if the underlying row is updated by a different service (e.g. via a batch job) that doesn't know about this cache and therefore never triggers an invalidation. Should we consider a shorter TTL for tables known to be touched by batch jobs, or a pub/sub invalidation channel for cross-service writes?

92. In `lru_evictor.py:77`, the LRU eviction list is implemented as a plain Python list with `list.remove()` calls on every access to reorder it. `list.remove()` is O(n), so under a cache with tens of thousands of entries, every cache hit becomes an O(n) operation. An `OrderedDict` or a doubly linked list + hash map would give O(1) reordering.

93. `repository/order_repository.py:452` catches a bare `except Exception` around the cache lookup and falls back to querying the database on any error, which is a reasonable fail-safe, but the exception is silently swallowed with no logging. If the cache starts failing systematically (e.g. a serialization bug), we'd have no signal until someone notices the database load spike.

94. Should we validate that `user_id` cannot itself be attacker-controlled in a way that lets a client force excessive cache key cardinality (a cache poisoning / cache-busting DoS vector)? `cache_metrics.py:139` accepts `user_id` directly from request query parameters without any bound on cardinality.

95. `repository/order_repository.py:298` — the TTL override annotation `@cache_ttl(seconds=N)` is only read at class-definition time via a decorator, so changing the TTL requires a code deploy. Given how often we've had to tune cache behavior for individual endpoints during incidents, should this be a runtime-configurable value instead (e.g. sourced from a feature-flag service)?

96. In `lru_evictor.py:454`, the cache key is built by concatenating `tenant_id` with the query string using a plain string join. Two semantically different queries could collide here if `tenant_id` values themselves contain the join separator. Recommend using a structured hash (e.g. SHA-256 over a canonical JSON representation of the query + params) instead of string concatenation.

97. `middleware/cache_middleware.py:103` — the TTL default of 60 seconds is hardcoded in three separate places (middleware/cache_middleware.py, `cache_config.py`, and the integration test fixture). These should be consolidated into a single source of truth so a future TTL change doesn't require hunting down every call site.

98. There's a potential race condition in `middleware/cache_middleware.py:354`: two concurrent requests for the same uncached key can both miss the cache and both issue the same expensive query to the database (a classic 'cache stampede'). Consider adding a per-key lock or a request-coalescing mechanism so only one of the concurrent misses actually hits the database.

99. `cache_metrics.py:93` reads the cache without holding any lock, and `lru_evictor.py:415` writes to it from a background eviction thread. On most JVM/CPython implementations dict access is atomic-ish but the LRU bookkeeping (moving an entry to the front of the eviction list) is not — under concurrent access this can corrupt the eviction order and evict a hot key prematurely.

100. Memory accounting in `cache_config.py:458` estimates entry size using `sys.getsizeof(value)`, which does not account for the size of nested objects referenced by `value`. For query results containing lists of dataclasses, actual memory usage could be several times higher than what the cache believes it is tracking, which risks blowing past the 512MB budget silently.

101. `cache_metrics.py:430` — when a cached entry expires, is the memory actually freed immediately, or does it linger until the next eviction sweep? If eviction sweeps only run every N seconds, worst-case memory usage could temporarily exceed the configured budget by a meaningful margin under bursty traffic.

102. The monitoring hook in `repository/user_repository.py:419` increments a `cache_hit` counter but there's no corresponding `cache_miss` counter, which means we can't compute hit rate directly from the metrics — we'd have to infer misses from database query volume, which is fragile. Recommend adding both counters explicitly.

103. `cache_metrics.py:349` — stale data could be served here if the underlying row is updated by a different service (e.g. via a batch job) that doesn't know about this cache and therefore never triggers an invalidation. Should we consider a shorter TTL for tables known to be touched by batch jobs, or a pub/sub invalidation channel for cross-service writes?

104. In `repository/order_repository.py:454`, the LRU eviction list is implemented as a plain Python list with `list.remove()` calls on every access to reorder it. `list.remove()` is O(n), so under a cache with tens of thousands of entries, every cache hit becomes an O(n) operation. An `OrderedDict` or a doubly linked list + hash map would give O(1) reordering.

105. `repository/order_repository.py:272` catches a bare `except Exception` around the cache lookup and falls back to querying the database on any error, which is a reasonable fail-safe, but the exception is silently swallowed with no logging. If the cache starts failing systematically (e.g. a serialization bug), we'd have no signal until someone notices the database load spike.

106. Should we validate that `query_hash` cannot itself be attacker-controlled in a way that lets a client force excessive cache key cardinality (a cache poisoning / cache-busting DoS vector)? `repository/order_repository.py:71` accepts `query_hash` directly from request query parameters without any bound on cardinality.

107. `lru_evictor.py:147` — the TTL override annotation `@cache_ttl(seconds=N)` is only read at class-definition time via a decorator, so changing the TTL requires a code deploy. Given how often we've had to tune cache behavior for individual endpoints during incidents, should this be a runtime-configurable value instead (e.g. sourced from a feature-flag service)?

108. In `middleware/cache_middleware.py:385`, the cache key is built by concatenating `query_hash` with the query string using a plain string join. Two semantically different queries could collide here if `query_hash` values themselves contain the join separator. Recommend using a structured hash (e.g. SHA-256 over a canonical JSON representation of the query + params) instead of string concatenation.

109. `middleware/cache_middleware.py:273` — the TTL default of 60 seconds is hardcoded in three separate places (middleware/cache_middleware.py, `cache_config.py`, and the integration test fixture). These should be consolidated into a single source of truth so a future TTL change doesn't require hunting down every call site.

110. There's a potential race condition in `cache_metrics.py:34`: two concurrent requests for the same uncached key can both miss the cache and both issue the same expensive query to the database (a classic 'cache stampede'). Consider adding a per-key lock or a request-coalescing mechanism so only one of the concurrent misses actually hits the database.

111. `query_cache.py:424` reads the cache without holding any lock, and `repository/user_repository.py:287` writes to it from a background eviction thread. On most JVM/CPython implementations dict access is atomic-ish but the LRU bookkeeping (moving an entry to the front of the eviction list) is not — under concurrent access this can corrupt the eviction order and evict a hot key prematurely.

112. Memory accounting in `cache_metrics.py:232` estimates entry size using `sys.getsizeof(value)`, which does not account for the size of nested objects referenced by `value`. For query results containing lists of dataclasses, actual memory usage could be several times higher than what the cache believes it is tracking, which risks blowing past the 512MB budget silently.

113. `repository/order_repository.py:172` — when a cached entry expires, is the memory actually freed immediately, or does it linger until the next eviction sweep? If eviction sweeps only run every N seconds, worst-case memory usage could temporarily exceed the configured budget by a meaningful margin under bursty traffic.

114. The monitoring hook in `repository/user_repository.py:170` increments a `cache_hit` counter but there's no corresponding `cache_miss` counter, which means we can't compute hit rate directly from the metrics — we'd have to infer misses from database query volume, which is fragile. Recommend adding both counters explicitly.

115. `repository/order_repository.py:368` — stale data could be served here if the underlying row is updated by a different service (e.g. via a batch job) that doesn't know about this cache and therefore never triggers an invalidation. Should we consider a shorter TTL for tables known to be touched by batch jobs, or a pub/sub invalidation channel for cross-service writes?

116. In `lru_evictor.py:227`, the LRU eviction list is implemented as a plain Python list with `list.remove()` calls on every access to reorder it. `list.remove()` is O(n), so under a cache with tens of thousands of entries, every cache hit becomes an O(n) operation. An `OrderedDict` or a doubly linked list + hash map would give O(1) reordering.

117. `lru_evictor.py:303` catches a bare `except Exception` around the cache lookup and falls back to querying the database on any error, which is a reasonable fail-safe, but the exception is silently swallowed with no logging. If the cache starts failing systematically (e.g. a serialization bug), we'd have no signal until someone notices the database load spike.

118. Should we validate that `endpoint_name` cannot itself be attacker-controlled in a way that lets a client force excessive cache key cardinality (a cache poisoning / cache-busting DoS vector)? `query_cache.py:158` accepts `endpoint_name` directly from request query parameters without any bound on cardinality.

119. `repository/order_repository.py:238` — the TTL override annotation `@cache_ttl(seconds=N)` is only read at class-definition time via a decorator, so changing the TTL requires a code deploy. Given how often we've had to tune cache behavior for individual endpoints during incidents, should this be a runtime-configurable value instead (e.g. sourced from a feature-flag service)?


## Meeting Transcript (PR review sync, 30 min)

**Priya (backend lead):** I think the single-instance cache is fine for v1 as long as we're honest in the runbook that different instances can serve different answers for up to one TTL window after a write — that's the tradeoff we're explicitly accepting here.

**Marcus (SRE):** Can we get a load test against a staging replica before this goes out? I want to see actual p99 numbers under something close to production traffic shape, not just the synthetic benchmark in the PR description.

**Wei (author):** The cache stampede scenario worries me more than anything else in this review — if a hot key expires during a traffic spike, we could see a thundering herd against the database at the worst possible moment, which is exactly the failure mode this feature is supposed to prevent.

**Dana (staff eng):** Agreed on stampede risk. I'd block on that one specifically — everything else in this review feels like it can be a fast-follow, but a stampede under load could turn a routine deploy into an incident.

**Sam (product):** What's our story for the batch-job staleness problem? A lot of our tables get touched by the nightly reconciliation job outside of the normal API write path, and this cache has no visibility into that at all right now.

**Priya (backend lead):** Short term I'd say: tag those specific tables with a much shorter TTL, maybe 5-10 seconds, and accept the staleness window. Longer term we probably want some kind of invalidation signal, but that's a bigger project.

**Marcus (SRE):** On the memory accounting concern — I ran a quick experiment and confirmed `sys.getsizeof` undercounts nested collections by roughly 3-4x on our typical query result shapes, so the 512MB budget is more like a 150MB budget in practice. We should fix the size estimation before this ships.

**Wei (author):** Do we have alerting on eviction rate? If eviction rate suddenly spikes it could indicate either a memory pressure problem or a cardinality explosion from the cache-busting concern raised in the review comments.

**Dana (staff eng):** Not yet — that's a gap. I'll add an alert threshold once we've got a few days of baseline eviction-rate data from staging to calibrate against.

**Sam (product):** For the O(n) list.remove() issue, how bad is this in practice at our current cache sizes? If we're capping at, say, 50,000 entries, is O(n) per hit actually going to show up in the latency budget?

**Priya (backend lead):** I profiled it locally at 50k entries and saw about 0.3ms added per cache hit from the list reordering alone, which eats a meaningful chunk of our 5ms p99 target. Switching to OrderedDict should bring that down close to zero.

**Marcus (SRE):** Let's make the OrderedDict swap a blocking requirement then, not a fast-follow — it directly threatens one of the stated goals in the design doc.

**Wei (author):** One more thing: the bare except-and-swallow around cache lookups needs at least a metrics counter and a rate-limited log line before this ships. Silent failure modes are how we end up debugging a database load spike three weeks from now with zero signal about the actual cause.

**Dana (staff eng):** Agreed across the board. Let's get: (1) request coalescing for cache misses, (2) OrderedDict-based LRU, (3) accurate memory accounting, (4) error logging on cache lookup failures, and (5) a hit/miss counter pair with dashboards. Everything else can land as a fast-follow.

**Sam (product):** I'll pick up the OrderedDict swap and the memory accounting fix today — should have a follow-up PR by tomorrow. Marcus, can you own the metrics/alerting side once the counters exist?

**Priya (backend lead):** Yep, I'll own dashboards and the eviction-rate alert once the hit/miss counters land. Should be quick once the underlying data exists.

**Marcus (SRE):** I think the single-instance cache is fine for v1 as long as we're honest in the runbook that different instances can serve different answers for up to one TTL window after a write — that's the tradeoff we're explicitly accepting here.

**Wei (author):** Can we get a load test against a staging replica before this goes out? I want to see actual p99 numbers under something close to production traffic shape, not just the synthetic benchmark in the PR description.

**Dana (staff eng):** The cache stampede scenario worries me more than anything else in this review — if a hot key expires during a traffic spike, we could see a thundering herd against the database at the worst possible moment, which is exactly the failure mode this feature is supposed to prevent.

**Sam (product):** Agreed on stampede risk. I'd block on that one specifically — everything else in this review feels like it can be a fast-follow, but a stampede under load could turn a routine deploy into an incident.

**Priya (backend lead):** What's our story for the batch-job staleness problem? A lot of our tables get touched by the nightly reconciliation job outside of the normal API write path, and this cache has no visibility into that at all right now.

**Marcus (SRE):** Short term I'd say: tag those specific tables with a much shorter TTL, maybe 5-10 seconds, and accept the staleness window. Longer term we probably want some kind of invalidation signal, but that's a bigger project.

**Wei (author):** On the memory accounting concern — I ran a quick experiment and confirmed `sys.getsizeof` undercounts nested collections by roughly 3-4x on our typical query result shapes, so the 512MB budget is more like a 150MB budget in practice. We should fix the size estimation before this ships.

**Dana (staff eng):** Do we have alerting on eviction rate? If eviction rate suddenly spikes it could indicate either a memory pressure problem or a cardinality explosion from the cache-busting concern raised in the review comments.

**Sam (product):** Not yet — that's a gap. I'll add an alert threshold once we've got a few days of baseline eviction-rate data from staging to calibrate against.

**Priya (backend lead):** For the O(n) list.remove() issue, how bad is this in practice at our current cache sizes? If we're capping at, say, 50,000 entries, is O(n) per hit actually going to show up in the latency budget?

**Marcus (SRE):** I profiled it locally at 50k entries and saw about 0.3ms added per cache hit from the list reordering alone, which eats a meaningful chunk of our 5ms p99 target. Switching to OrderedDict should bring that down close to zero.

**Wei (author):** Let's make the OrderedDict swap a blocking requirement then, not a fast-follow — it directly threatens one of the stated goals in the design doc.

**Dana (staff eng):** One more thing: the bare except-and-swallow around cache lookups needs at least a metrics counter and a rate-limited log line before this ships. Silent failure modes are how we end up debugging a database load spike three weeks from now with zero signal about the actual cause.

**Sam (product):** Agreed across the board. Let's get: (1) request coalescing for cache misses, (2) OrderedDict-based LRU, (3) accurate memory accounting, (4) error logging on cache lookup failures, and (5) a hit/miss counter pair with dashboards. Everything else can land as a fast-follow.

**Priya (backend lead):** I'll pick up the OrderedDict swap and the memory accounting fix today — should have a follow-up PR by tomorrow. Marcus, can you own the metrics/alerting side once the counters exist?

**Marcus (SRE):** Yep, I'll own dashboards and the eviction-rate alert once the hit/miss counters land. Should be quick once the underlying data exists.

**Wei (author):** I think the single-instance cache is fine for v1 as long as we're honest in the runbook that different instances can serve different answers for up to one TTL window after a write — that's the tradeoff we're explicitly accepting here.

**Dana (staff eng):** Can we get a load test against a staging replica before this goes out? I want to see actual p99 numbers under something close to production traffic shape, not just the synthetic benchmark in the PR description.

**Sam (product):** The cache stampede scenario worries me more than anything else in this review — if a hot key expires during a traffic spike, we could see a thundering herd against the database at the worst possible moment, which is exactly the failure mode this feature is supposed to prevent.

**Priya (backend lead):** Agreed on stampede risk. I'd block on that one specifically — everything else in this review feels like it can be a fast-follow, but a stampede under load could turn a routine deploy into an incident.

**Marcus (SRE):** What's our story for the batch-job staleness problem? A lot of our tables get touched by the nightly reconciliation job outside of the normal API write path, and this cache has no visibility into that at all right now.

**Wei (author):** Short term I'd say: tag those specific tables with a much shorter TTL, maybe 5-10 seconds, and accept the staleness window. Longer term we probably want some kind of invalidation signal, but that's a bigger project.

**Dana (staff eng):** On the memory accounting concern — I ran a quick experiment and confirmed `sys.getsizeof` undercounts nested collections by roughly 3-4x on our typical query result shapes, so the 512MB budget is more like a 150MB budget in practice. We should fix the size estimation before this ships.

**Sam (product):** Do we have alerting on eviction rate? If eviction rate suddenly spikes it could indicate either a memory pressure problem or a cardinality explosion from the cache-busting concern raised in the review comments.

**Priya (backend lead):** Not yet — that's a gap. I'll add an alert threshold once we've got a few days of baseline eviction-rate data from staging to calibrate against.

**Marcus (SRE):** For the O(n) list.remove() issue, how bad is this in practice at our current cache sizes? If we're capping at, say, 50,000 entries, is O(n) per hit actually going to show up in the latency budget?

**Wei (author):** I profiled it locally at 50k entries and saw about 0.3ms added per cache hit from the list reordering alone, which eats a meaningful chunk of our 5ms p99 target. Switching to OrderedDict should bring that down close to zero.

**Dana (staff eng):** Let's make the OrderedDict swap a blocking requirement then, not a fast-follow — it directly threatens one of the stated goals in the design doc.

**Sam (product):** One more thing: the bare except-and-swallow around cache lookups needs at least a metrics counter and a rate-limited log line before this ships. Silent failure modes are how we end up debugging a database load spike three weeks from now with zero signal about the actual cause.

**Priya (backend lead):** Agreed across the board. Let's get: (1) request coalescing for cache misses, (2) OrderedDict-based LRU, (3) accurate memory accounting, (4) error logging on cache lookup failures, and (5) a hit/miss counter pair with dashboards. Everything else can land as a fast-follow.

**Marcus (SRE):** I'll pick up the OrderedDict swap and the memory accounting fix today — should have a follow-up PR by tomorrow. Marcus, can you own the metrics/alerting side once the counters exist?

**Wei (author):** Yep, I'll own dashboards and the eviction-rate alert once the hit/miss counters land. Should be quick once the underlying data exists.

**Dana (staff eng):** I think the single-instance cache is fine for v1 as long as we're honest in the runbook that different instances can serve different answers for up to one TTL window after a write — that's the tradeoff we're explicitly accepting here.

**Sam (product):** Can we get a load test against a staging replica before this goes out? I want to see actual p99 numbers under something close to production traffic shape, not just the synthetic benchmark in the PR description.

**Priya (backend lead):** The cache stampede scenario worries me more than anything else in this review — if a hot key expires during a traffic spike, we could see a thundering herd against the database at the worst possible moment, which is exactly the failure mode this feature is supposed to prevent.

**Marcus (SRE):** Agreed on stampede risk. I'd block on that one specifically — everything else in this review feels like it can be a fast-follow, but a stampede under load could turn a routine deploy into an incident.

**Wei (author):** What's our story for the batch-job staleness problem? A lot of our tables get touched by the nightly reconciliation job outside of the normal API write path, and this cache has no visibility into that at all right now.

**Dana (staff eng):** Short term I'd say: tag those specific tables with a much shorter TTL, maybe 5-10 seconds, and accept the staleness window. Longer term we probably want some kind of invalidation signal, but that's a bigger project.

**Sam (product):** On the memory accounting concern — I ran a quick experiment and confirmed `sys.getsizeof` undercounts nested collections by roughly 3-4x on our typical query result shapes, so the 512MB budget is more like a 150MB budget in practice. We should fix the size estimation before this ships.

**Priya (backend lead):** Do we have alerting on eviction rate? If eviction rate suddenly spikes it could indicate either a memory pressure problem or a cardinality explosion from the cache-busting concern raised in the review comments.

**Marcus (SRE):** Not yet — that's a gap. I'll add an alert threshold once we've got a few days of baseline eviction-rate data from staging to calibrate against.

**Wei (author):** For the O(n) list.remove() issue, how bad is this in practice at our current cache sizes? If we're capping at, say, 50,000 entries, is O(n) per hit actually going to show up in the latency budget?

**Dana (staff eng):** I profiled it locally at 50k entries and saw about 0.3ms added per cache hit from the list reordering alone, which eats a meaningful chunk of our 5ms p99 target. Switching to OrderedDict should bring that down close to zero.

**Sam (product):** Let's make the OrderedDict swap a blocking requirement then, not a fast-follow — it directly threatens one of the stated goals in the design doc.

**Priya (backend lead):** One more thing: the bare except-and-swallow around cache lookups needs at least a metrics counter and a rate-limited log line before this ships. Silent failure modes are how we end up debugging a database load spike three weeks from now with zero signal about the actual cause.

**Marcus (SRE):** Agreed across the board. Let's get: (1) request coalescing for cache misses, (2) OrderedDict-based LRU, (3) accurate memory accounting, (4) error logging on cache lookup failures, and (5) a hit/miss counter pair with dashboards. Everything else can land as a fast-follow.

**Wei (author):** I'll pick up the OrderedDict swap and the memory accounting fix today — should have a follow-up PR by tomorrow. Marcus, can you own the metrics/alerting side once the counters exist?

**Dana (staff eng):** Yep, I'll own dashboards and the eviction-rate alert once the hit/miss counters land. Should be quick once the underlying data exists.

**Sam (product):** I think the single-instance cache is fine for v1 as long as we're honest in the runbook that different instances can serve different answers for up to one TTL window after a write — that's the tradeoff we're explicitly accepting here.

**Priya (backend lead):** Can we get a load test against a staging replica before this goes out? I want to see actual p99 numbers under something close to production traffic shape, not just the synthetic benchmark in the PR description.

**Marcus (SRE):** The cache stampede scenario worries me more than anything else in this review — if a hot key expires during a traffic spike, we could see a thundering herd against the database at the worst possible moment, which is exactly the failure mode this feature is supposed to prevent.

**Wei (author):** Agreed on stampede risk. I'd block on that one specifically — everything else in this review feels like it can be a fast-follow, but a stampede under load could turn a routine deploy into an incident.

**Dana (staff eng):** What's our story for the batch-job staleness problem? A lot of our tables get touched by the nightly reconciliation job outside of the normal API write path, and this cache has no visibility into that at all right now.

**Sam (product):** Short term I'd say: tag those specific tables with a much shorter TTL, maybe 5-10 seconds, and accept the staleness window. Longer term we probably want some kind of invalidation signal, but that's a bigger project.

**Priya (backend lead):** On the memory accounting concern — I ran a quick experiment and confirmed `sys.getsizeof` undercounts nested collections by roughly 3-4x on our typical query result shapes, so the 512MB budget is more like a 150MB budget in practice. We should fix the size estimation before this ships.

**Marcus (SRE):** Do we have alerting on eviction rate? If eviction rate suddenly spikes it could indicate either a memory pressure problem or a cardinality explosion from the cache-busting concern raised in the review comments.

**Wei (author):** Not yet — that's a gap. I'll add an alert threshold once we've got a few days of baseline eviction-rate data from staging to calibrate against.

**Dana (staff eng):** For the O(n) list.remove() issue, how bad is this in practice at our current cache sizes? If we're capping at, say, 50,000 entries, is O(n) per hit actually going to show up in the latency budget?

**Sam (product):** I profiled it locally at 50k entries and saw about 0.3ms added per cache hit from the list reordering alone, which eats a meaningful chunk of our 5ms p99 target. Switching to OrderedDict should bring that down close to zero.

**Priya (backend lead):** Let's make the OrderedDict swap a blocking requirement then, not a fast-follow — it directly threatens one of the stated goals in the design doc.

**Marcus (SRE):** One more thing: the bare except-and-swallow around cache lookups needs at least a metrics counter and a rate-limited log line before this ships. Silent failure modes are how we end up debugging a database load spike three weeks from now with zero signal about the actual cause.

**Wei (author):** Agreed across the board. Let's get: (1) request coalescing for cache misses, (2) OrderedDict-based LRU, (3) accurate memory accounting, (4) error logging on cache lookup failures, and (5) a hit/miss counter pair with dashboards. Everything else can land as a fast-follow.

**Dana (staff eng):** I'll pick up the OrderedDict swap and the memory accounting fix today — should have a follow-up PR by tomorrow. Marcus, can you own the metrics/alerting side once the counters exist?

**Sam (product):** Yep, I'll own dashboards and the eviction-rate alert once the hit/miss counters land. Should be quick once the underlying data exists.

**Priya (backend lead):** I think the single-instance cache is fine for v1 as long as we're honest in the runbook that different instances can serve different answers for up to one TTL window after a write — that's the tradeoff we're explicitly accepting here.

**Marcus (SRE):** Can we get a load test against a staging replica before this goes out? I want to see actual p99 numbers under something close to production traffic shape, not just the synthetic benchmark in the PR description.

**Wei (author):** The cache stampede scenario worries me more than anything else in this review — if a hot key expires during a traffic spike, we could see a thundering herd against the database at the worst possible moment, which is exactly the failure mode this feature is supposed to prevent.

**Dana (staff eng):** Agreed on stampede risk. I'd block on that one specifically — everything else in this review feels like it can be a fast-follow, but a stampede under load could turn a routine deploy into an incident.

**Sam (product):** What's our story for the batch-job staleness problem? A lot of our tables get touched by the nightly reconciliation job outside of the normal API write path, and this cache has no visibility into that at all right now.

**Priya (backend lead):** Short term I'd say: tag those specific tables with a much shorter TTL, maybe 5-10 seconds, and accept the staleness window. Longer term we probably want some kind of invalidation signal, but that's a bigger project.

**Marcus (SRE):** On the memory accounting concern — I ran a quick experiment and confirmed `sys.getsizeof` undercounts nested collections by roughly 3-4x on our typical query result shapes, so the 512MB budget is more like a 150MB budget in practice. We should fix the size estimation before this ships.

**Wei (author):** Do we have alerting on eviction rate? If eviction rate suddenly spikes it could indicate either a memory pressure problem or a cardinality explosion from the cache-busting concern raised in the review comments.

**Dana (staff eng):** Not yet — that's a gap. I'll add an alert threshold once we've got a few days of baseline eviction-rate data from staging to calibrate against.

**Sam (product):** For the O(n) list.remove() issue, how bad is this in practice at our current cache sizes? If we're capping at, say, 50,000 entries, is O(n) per hit actually going to show up in the latency budget?

**Priya (backend lead):** I profiled it locally at 50k entries and saw about 0.3ms added per cache hit from the list reordering alone, which eats a meaningful chunk of our 5ms p99 target. Switching to OrderedDict should bring that down close to zero.

**Marcus (SRE):** Let's make the OrderedDict swap a blocking requirement then, not a fast-follow — it directly threatens one of the stated goals in the design doc.

**Wei (author):** One more thing: the bare except-and-swallow around cache lookups needs at least a metrics counter and a rate-limited log line before this ships. Silent failure modes are how we end up debugging a database load spike three weeks from now with zero signal about the actual cause.

**Dana (staff eng):** Agreed across the board. Let's get: (1) request coalescing for cache misses, (2) OrderedDict-based LRU, (3) accurate memory accounting, (4) error logging on cache lookup failures, and (5) a hit/miss counter pair with dashboards. Everything else can land as a fast-follow.

**Sam (product):** I'll pick up the OrderedDict swap and the memory accounting fix today — should have a follow-up PR by tomorrow. Marcus, can you own the metrics/alerting side once the counters exist?

**Priya (backend lead):** Yep, I'll own dashboards and the eviction-rate alert once the hit/miss counters land. Should be quick once the underlying data exists.

**Marcus (SRE):** I think the single-instance cache is fine for v1 as long as we're honest in the runbook that different instances can serve different answers for up to one TTL window after a write — that's the tradeoff we're explicitly accepting here.

**Wei (author):** Can we get a load test against a staging replica before this goes out? I want to see actual p99 numbers under something close to production traffic shape, not just the synthetic benchmark in the PR description.


## Your Task

You are the reviewing engineer. Based on the full design doc, the numbered code
review comments above, and the meeting transcript, write a structured review
summary that:

1. Lists the top 5 unresolved risks, ranked by severity, with a one-sentence
   justification for each ranking.
2. States the team's final recommendation (approve / approve with required
   changes / request changes / reject) and the specific blocking items that
   justify that recommendation, if any.
3. Lists the concrete follow-up action items, each with the engineer who owns it
   (as stated in the meeting transcript) and a short description of what "done"
   looks like for that item.
4. Flags anything discussed in the meeting that contradicts or overrides a
   decision stated earlier in the design doc, if you notice one.

Keep the summary to no more than 400 words.
