---
layout: post
title: Single-Flight Cache Refresh (Thundering Herd)
date: 2026-07-19
description: >-
  The expiry problem's evil twin: the instant a hot key rots, hundreds of readers miss together and every one of them fires the same expensive load. Collapse N loads into one…
categories: interview multithreading problems
---

Part of the [Time-Based State](/interview/multithreading/patterns/time-based/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [EngineBogie, two-tier cache refresh](https://enginebogie.com/public/question/multithreading-two-tier-cache-refresh-system/1560). High day-job relevance, the failure mode that takes production down at 3am.

### Problem

You have a read-heavy cache with per-entry TTL. A hot key expires. In the same millisecond, hundreds of concurrent `get(key)` calls all observe the miss and all launch the same expensive backend load. The backend, sized for the cache hit rate, gets N simultaneous identical queries and falls over. This is the **thundering herd** (a.k.a. cache stampede, dogpile). Design `get(key)` so that when a key needs (re)computation, **exactly one thread** does the work and every other concurrent caller either waits for that one result or is served slightly-stale data. This is **request coalescing / single-flight**.

### Constraints

- On a concurrent miss-storm for one key, the loader runs **once**, not N times. Different keys never block each other.
- Fresh-hit path stays cheap (this is still read-heavy: ~99% of calls hit fresh entries).
- Loader can fail. Awaiting readers must all learn of the failure; the next `get` must be free to retry.
- Extension in scope: two-tier (L1 local per node + L2 shared like Redis), where the herd reappears at the shared layer.

### Clarify before solving

- Must waiters block for the fresh value, or is stale-while-revalidate acceptable? (Ask: latency vs freshness. SWR is usually the right answer for a hot key.)
- Single node or a fleet? (Single-node single-flight collapses per-node; a fleet still stampedes L2: the two-tier extension.)
- Cache load failures: retry policy, negative caching? (Don't poison the map; see failure section.)
- Is this the same as [read-heavy cache with expiry](/interview/multithreading/problems/read-heavy-cache-with-expiry/)? (Say: that problem has the TTL substrate; THIS problem is specifically the herd-collapse on top of it.)

### Why this problem matters

The expiry problem gives you the mechanics of *when* an entry is stale. This one gives you the mechanics of *what a crowd does the instant it goes stale*. The gap between them is one atomic operation and it is the entire question. Get it wrong and the cache is correct on every unit test and still melts the database on the first cold key under load, a real, named, recurring production incident.

---

## Strategy

### Classify

Time-based state (per-entry TTL, derive-on-read) **plus** per-key single-flight for the recompute. The expiry half is the [read-heavy cache](/interview/multithreading/problems/read-heavy-cache-with-expiry/); the new half is admission control on the *load*: at most one loader in flight per key.

### Invariant

**At most one in-flight computation per key at any instant.** N concurrent misses on key K produce exactly one loader run; every other misser observes the same in-flight result. Loads of different keys are independent. A failed load leaves no residue: the next `get` starts a fresh computation.

### Mental model

A crowded bakery, one popular loaf, the tray just went empty. Without coordination all fifty customers rush the kitchen and shout the same order. The fix: the first person to reach the empty tray hangs a **"baking now" ticket** on it. Everyone arriving after reads the ticket and *waits by the tray* instead of storming the kitchen. When the loaf comes out the ticket comes down and the tray refills. The ticket is a promise-of-a-loaf that exists **before the loaf does**, which is exactly what lets the crowd wait on something. Stale-while-revalidate is the same bakery selling yesterday's loaf to anyone who'd rather not wait while the fresh one bakes.

### The core primitive: cache a Future, not a value

Keep a per-key **in-flight map**, `ConcurrentHashMap<K, Future<Value>>`. On a miss:

- The first misser **atomically installs** a Future: `computeIfAbsent(key, k -> new FutureTask<>(loader))` (or `putIfAbsent`). CHM's compute is a single CAS-like step; **that install is the linearization point** of the whole operation.
- The winner runs the FutureTask (computes, completes it). Every concurrent misser gets the **same** Future back from the map and calls `.get()` on it. N misses collapse to one load: everyone blocks on the one future the winner is filling.
- On completion, **remove the in-flight entry** so the map holds only pending work.

The waiting point exists *before the value does*: that is the whole trick. You cannot make a hundred threads wait on a value that hasn't been computed, but you can make them wait on the *Future* of it. (This is exactly the shared future readers await in [implement-a-future](/interview/multithreading/problems/implement-a-future/); the cache is that primitive plus a map.)

### The crux race: check-and-install must be atomic

The tempting shape is *"is someone already loading this key? if not, start loading."* Written as two steps, `if (!inFlight.containsKey(k)) inFlight.put(k, newFuture)`, it is **check-then-act across a gap**, and under a storm every thread passes the check before any thread does the act, so you get the herd back, one future per thread, N loads. The check and the install must be **one atomic per-key operation**. That is precisely what `computeIfAbsent` / `putIfAbsent` buy you: the "did someone start it" and the "start it" happen indivisibly, so exactly one thread wins the install and the rest read the winner's future. This is the [check-then-act-on-concurrent-map](/interview/multithreading/problems/check-then-act-on-concurrent-map/) family in its purest form; CHM's per-bin atomicity is your only lock. (Caution to say aloud: create the FutureTask *inside* the lambda but **run it outside** the compute: long loader work inside the mapping function blocks the CHM bin and re-entrant map calls from the loader can deadlock.)

### Stale-while-revalidate: don't make them wait

Blocking every misser on the loader is correct but couples read latency to load latency. For a hot key that's a stall spike on every expiry. The knob: keep the stale entry, and when it goes stale let **one** thread refresh in the background while everyone else is **served the stale value immediately**. Nobody blocks; the herd is still collapsed (single-flight guards the refresh); the price is bounded staleness. Frame it explicitly as the **latency-vs-freshness** trade-off and let the interviewer pick. The refresh race is arbitrated the same way: one winner installs the refresh future (`replace(key, stale, freshFuture)`), losers keep serving stale.

### Failure and negative caching (the poisoned-future trap)

If the single load throws, the awaiting readers are all blocked on that one future, so they must **all** see the failure (a failed FutureTask surfaces the exception to every `.get()`). But you must **not leave the failed future in the map**: remove it on failure, or the failure is cached forever and every future `get` re-throws while the key never recovers, the same poisoned-future incident from the expiry problem. Remove-on-failure means retry falls out for free: the next `get` finds no in-flight entry and starts a clean one. If you *do* want to damp a hard-down backend, that's deliberate **negative caching** (cache the failure with a short TTL), a policy choice, not an accident, and it composes with [retry-with-backoff-and-jitter](/interview/multithreading/problems/retry-with-backoff-and-jitter/) on the single loader so the one retry-er doesn't hammer.

### Two-tier: the herd moves upstairs

L1 local (per-node) single-flight collapses the storm *within* a node. But a fleet of M nodes each running one loader still hits the shared L2 (Redis) with M concurrent identical loads, a second, smaller stampede at the shared layer. Two clean answers, name both: (1) **single-flight per tier**: each node coalesces to one L2 read, and L2 itself (or the DB behind it) is fronted by its own coalescing; (2) a **distributed lock / lease** so exactly one node across the fleet recomputes and writes L2 while the others read the freshly-written value or serve stale, the same at-most-one-writer invariant, lifted from per-key-in-a-JVM to per-key-across-the-cluster. That's [distributed-lock-and-lease](/interview/multithreading/problems/distributed-lock-and-lease/): the lease's TTL is what stops a dead lock-holder from wedging the key forever.

### Contrast with plain read-heavy-cache-with-expiry

Same TTL substrate, same lazy `now < expiresAt` freshness check, same CHM storage. The difference is the *load*: the expiry problem can get away with load-then-put because it isn't being asked about the storm; this problem's entire reason to exist is that under concurrency load-then-put **is** the bug.

### Pitfalls

1. `containsKey` then `put` (or check-then-load-then-put): check-then-act across the miss, the herd survives. Must be `computeIfAbsent`/`putIfAbsent`.
2. Caching the **value** instead of the **future**: there's no value yet at the moment the losers need something to wait on; the waiting point must precede the value.
3. Loader run **inside** the compute lambda: blocks the bin, risks re-entrant deadlock. Create inside, run outside.
4. Failed future left in the map: failure cached forever, key never recovers, every `get` re-throws.
5. Forgetting to remove the completed entry: stale future served past its TTL (or a memory leak of dead futures).
6. Solving only L1 and calling it done: the fleet still stampedes L2; name the second tier.
7. Wall clock for the TTL (this family's recurring sin: nanoTime for intervals).

### Check your understanding

1. Walk 100 threads missing cold key K: which one CAS wins the install, where do the other 99 block, and how many loader runs happen? (One; the losers block on the winner's future; one.)
2. Why cache a Future rather than load-then-put a value? (The thing the losers wait on has to exist before the value does.)
3. Two threads find K stale at the same instant under SWR: how is the single refresh arbitrated, and what do the readers get meanwhile? (`replace(stale, freshFuture)`, one winner; readers get the stale value.)
4. The loader throws. What must every awaiter see, and what must happen to the map entry, and why does skipping the removal cause an outage? (All see the exception; entry removed; else the failure is cached forever.)
5. You've collapsed the storm on one node. A hundred nodes each fire one load at Redis. What now? (Single-flight per tier, or a distributed lease so one node recomputes for the fleet.)
6. State the invariant in one line. (At most one in-flight computation per key at a time.)

### Transfers to

[read-heavy cache with expiry](/interview/multithreading/problems/read-heavy-cache-with-expiry/) (the TTL/expiry substrate this sits on), [implement-a-future](/interview/multithreading/problems/implement-a-future/) (the shared future the crowd awaits), [check-then-act-on-concurrent-map](/interview/multithreading/problems/check-then-act-on-concurrent-map/) (the atomic install that IS the fix), [distributed-lock-and-lease](/interview/multithreading/problems/distributed-lock-and-lease/) (single-flight lifted to the shared L2 tier), [retry-with-backoff-and-jitter](/interview/multithreading/problems/retry-with-backoff-and-jitter/) (so the one loader's failure retry doesn't itself become a herd). The cached-future idiom is the same Memoizer from JCiP. Any "expensive compute, many identical waiters" shape is this problem wearing different clothes.
