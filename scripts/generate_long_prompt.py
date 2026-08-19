#!/usr/bin/env python3
"""
Generate a long, realistic ~15,000-token prompt (a code-review packet: design
doc + numbered review comments + meeting transcript + a final task) so we can
test LLMLingua compression on something closer to a real long-context prompt
than the short paragraph used earlier.

Usage:
    python3 scripts/generate_long_prompt.py -o prompts/input_prompt.md --target-tokens 15000
"""

import argparse
import random
from pathlib import Path

random.seed(42)

BACKGROUND = """# Design Review Packet: Query Result Caching Layer

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

"""

REVIEW_COMMENT_TEMPLATES = [
    "In `{file}:{line}`, the cache key is built by concatenating `{field}` with the "
    "query string using a plain string join. Two semantically different queries "
    "could collide here if `{field}` values themselves contain the join separator. "
    "Recommend using a structured hash (e.g. SHA-256 over a canonical JSON "
    "representation of the query + params) instead of string concatenation.",
    "`{file}:{line}` — the TTL default of 60 seconds is hardcoded in three separate "
    "places ({file}, `cache_config.py`, and the integration test fixture). These "
    "should be consolidated into a single source of truth so a future TTL change "
    "doesn't require hunting down every call site.",
    "There's a potential race condition in `{file}:{line}`: two concurrent requests "
    "for the same uncached key can both miss the cache and both issue the same "
    "expensive query to the database (a classic 'cache stampede'). Consider adding "
    "a per-key lock or a request-coalescing mechanism so only one of the concurrent "
    "misses actually hits the database.",
    "`{file}:{line}` reads the cache without holding any lock, and `{file2}:{line2}` "
    "writes to it from a background eviction thread. On most JVM/CPython "
    "implementations dict access is atomic-ish but the LRU bookkeeping (moving an "
    "entry to the front of the eviction list) is not — under concurrent access this "
    "can corrupt the eviction order and evict a hot key prematurely.",
    "Memory accounting in `{file}:{line}` estimates entry size using "
    "`sys.getsizeof(value)`, which does not account for the size of nested objects "
    "referenced by `value`. For query results containing lists of dataclasses, "
    "actual memory usage could be several times higher than what the cache believes "
    "it is tracking, which risks blowing past the 512MB budget silently.",
    "`{file}:{line}` — when a cached entry expires, is the memory actually freed "
    "immediately, or does it linger until the next eviction sweep? If eviction "
    "sweeps only run every N seconds, worst-case memory usage could temporarily "
    "exceed the configured budget by a meaningful margin under bursty traffic.",
    "The monitoring hook in `{file}:{line}` increments a `cache_hit` counter but "
    "there's no corresponding `cache_miss` counter, which means we can't compute "
    "hit rate directly from the metrics — we'd have to infer misses from database "
    "query volume, which is fragile. Recommend adding both counters explicitly.",
    "`{file}:{line}` — stale data could be served here if the underlying row is "
    "updated by a different service (e.g. via a batch job) that doesn't know about "
    "this cache and therefore never triggers an invalidation. Should we consider a "
    "shorter TTL for tables known to be touched by batch jobs, or a pub/sub "
    "invalidation channel for cross-service writes?",
    "In `{file}:{line}`, the LRU eviction list is implemented as a plain Python "
    "list with `list.remove()` calls on every access to reorder it. `list.remove()` "
    "is O(n), so under a cache with tens of thousands of entries, every cache hit "
    "becomes an O(n) operation. An `OrderedDict` or a doubly linked list + hash map "
    "would give O(1) reordering.",
    "`{file}:{line}` catches a bare `except Exception` around the cache lookup and "
    "falls back to querying the database on any error, which is a reasonable "
    "fail-safe, but the exception is silently swallowed with no logging. If the "
    "cache starts failing systematically (e.g. a serialization bug), we'd have no "
    "signal until someone notices the database load spike.",
    "Should we validate that `{field}` cannot itself be attacker-controlled in a way "
    "that lets a client force excessive cache key cardinality (a cache poisoning / "
    "cache-busting DoS vector)? `{file}:{line}` accepts `{field}` directly from "
    "request query parameters without any bound on cardinality.",
    "`{file}:{line}` — the TTL override annotation `@cache_ttl(seconds=N)` is only "
    "read at class-definition time via a decorator, so changing the TTL requires a "
    "code deploy. Given how often we've had to tune cache behavior for individual "
    "endpoints during incidents, should this be a runtime-configurable value "
    "instead (e.g. sourced from a feature-flag service)?",
]

FILES = [
    "query_cache.py", "cache_config.py", "lru_evictor.py", "cache_metrics.py",
    "repository/user_repository.py", "repository/order_repository.py",
    "middleware/cache_middleware.py", "serializers/query_result_serializer.py",
]

FIELDS = ["user_id", "tenant_id", "query_hash", "endpoint_name", "request_params", "shard_key"]

MEETING_SPEAKERS = ["Priya (backend lead)", "Marcus (SRE)", "Wei (author)", "Dana (staff eng)", "Sam (product)"]

MEETING_LINES = [
    "I think the single-instance cache is fine for v1 as long as we're honest in the "
    "runbook that different instances can serve different answers for up to one TTL "
    "window after a write — that's the tradeoff we're explicitly accepting here.",
    "Can we get a load test against a staging replica before this goes out? I want "
    "to see actual p99 numbers under something close to production traffic shape, "
    "not just the synthetic benchmark in the PR description.",
    "The cache stampede scenario worries me more than anything else in this review "
    "— if a hot key expires during a traffic spike, we could see a thundering herd "
    "against the database at the worst possible moment, which is exactly the "
    "failure mode this feature is supposed to prevent.",
    "Agreed on stampede risk. I'd block on that one specifically — everything else "
    "in this review feels like it can be a fast-follow, but a stampede under load "
    "could turn a routine deploy into an incident.",
    "What's our story for the batch-job staleness problem? A lot of our tables get "
    "touched by the nightly reconciliation job outside of the normal API write "
    "path, and this cache has no visibility into that at all right now.",
    "Short term I'd say: tag those specific tables with a much shorter TTL, maybe "
    "5-10 seconds, and accept the staleness window. Longer term we probably want "
    "some kind of invalidation signal, but that's a bigger project.",
    "On the memory accounting concern — I ran a quick experiment and confirmed "
    "`sys.getsizeof` undercounts nested collections by roughly 3-4x on our typical "
    "query result shapes, so the 512MB budget is more like a 150MB budget in "
    "practice. We should fix the size estimation before this ships.",
    "Do we have alerting on eviction rate? If eviction rate suddenly spikes it "
    "could indicate either a memory pressure problem or a cardinality explosion "
    "from the cache-busting concern raised in the review comments.",
    "Not yet — that's a gap. I'll add an alert threshold once we've got a few days "
    "of baseline eviction-rate data from staging to calibrate against.",
    "For the O(n) list.remove() issue, how bad is this in practice at our current "
    "cache sizes? If we're capping at, say, 50,000 entries, is O(n) per hit "
    "actually going to show up in the latency budget?",
    "I profiled it locally at 50k entries and saw about 0.3ms added per cache hit "
    "from the list reordering alone, which eats a meaningful chunk of our 5ms p99 "
    "target. Switching to OrderedDict should bring that down close to zero.",
    "Let's make the OrderedDict swap a blocking requirement then, not a fast-follow "
    "— it directly threatens one of the stated goals in the design doc.",
    "One more thing: the bare except-and-swallow around cache lookups needs at "
    "least a metrics counter and a rate-limited log line before this ships. Silent "
    "failure modes are how we end up debugging a database load spike three weeks "
    "from now with zero signal about the actual cause.",
    "Agreed across the board. Let's get: (1) request coalescing for cache misses, "
    "(2) OrderedDict-based LRU, (3) accurate memory accounting, (4) error logging "
    "on cache lookup failures, and (5) a hit/miss counter pair with dashboards. "
    "Everything else can land as a fast-follow.",
    "I'll pick up the OrderedDict swap and the memory accounting fix today — should "
    "have a follow-up PR by tomorrow. Marcus, can you own the metrics/alerting "
    "side once the counters exist?",
    "Yep, I'll own dashboards and the eviction-rate alert once the hit/miss "
    "counters land. Should be quick once the underlying data exists.",
]

TASK = """
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
"""


def build_document(target_tokens: int) -> str:
    parts = [BACKGROUND]

    parts.append("## Code Review Comments\n")
    i = 1
    while True:
        template = REVIEW_COMMENT_TEMPLATES[i % len(REVIEW_COMMENT_TEMPLATES)]
        file1, file2 = random.sample(FILES, 2)
        comment = template.format(
            file=file1, file2=file2,
            line=random.randint(12, 480), line2=random.randint(12, 480),
            field=random.choice(FIELDS),
        )
        parts.append(f"{i}. {comment}\n")
        i += 1
        # rough running estimate: ~0.75 words per token, check periodically
        if i % 10 == 0:
            current_words = sum(len(p.split()) for p in parts)
            if current_words > target_tokens * 0.75 * 0.55:  # ~55% of budget for comments
                break

    parts.append("\n## Meeting Transcript (PR review sync, 30 min)\n")
    j = 0
    while True:
        speaker = MEETING_SPEAKERS[j % len(MEETING_SPEAKERS)]
        line = MEETING_LINES[j % len(MEETING_LINES)]
        parts.append(f"**{speaker}:** {line}\n")
        j += 1
        current_words = sum(len(p.split()) for p in parts)
        if current_words > target_tokens * 0.75 * 0.92:  # leave room for TASK section
            break
        if j > 200:  # safety valve
            break

    parts.append(TASK)
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Generate a long realistic prompt for compression testing.")
    parser.add_argument("-o", "--output", default="prompts/input_prompt.md")
    parser.add_argument("--target-tokens", type=int, default=15000)
    args = parser.parse_args()

    doc = build_document(args.target_tokens)
    Path(args.output).write_text(doc, encoding="utf-8")

    word_count = len(doc.split())
    print(f"Wrote {args.output}: {word_count} words (~{int(word_count / 0.75)} estimated tokens)")


if __name__ == "__main__":
    main()
