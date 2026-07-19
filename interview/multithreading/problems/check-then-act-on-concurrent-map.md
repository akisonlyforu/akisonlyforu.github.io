---
layout: post
title: Check-Then-Act on a ConcurrentHashMap, "we used the concurrent one"
date: 2026-07-19
description: >-
  ConcurrentHashMap guarantees that each of its own methods is atomic. It guarantees nothing about *your sequence* of them, and that gap is where a large share of production…
categories: interview multithreading problems
---

Part of the [Debugging & Code Review](/interview/multithreading/patterns/debugging-and-code-review/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Airbnb, Coinbase and Uber review rounds; the most common false-confidence bug in real Java services. The defence *"but it's a `ConcurrentHashMap`"* is the thing being tested.

### The code under review (described, not shown)

A `SessionCache` and its neighbours, all built on `ConcurrentHashMap`. Nobody has written a `synchronized` block anywhere in the file, deliberately, the class javadoc says *"lock-free: all state lives in concurrent collections."*

**Piece 1, `getOrCreateSession(userId)`.** Checks `sessions.containsKey(userId)`; if absent, constructs a new `Session` (which allocates a socket to a downstream service) and `put`s it; then returns `sessions.get(userId)`.

**Piece 2, `incrementQuota(userId, n)`.** Reads the current quota with `get(userId)` (treating absent as zero), adds `n`, and `put`s the sum back.

**Piece 3, `registerOnce(userId)`.** Checks `registered.containsKey(userId)`; if absent, performs the registration side effect (an HTTP call to a billing service that is **not** idempotent) and then `put`s a marker.

**Piece 4, `loadProfile(userId)`.** Uses `profiles.computeIfAbsent(userId, id -> fetchProfileOverHttp(id))`, where the fetch takes 200–2000 ms and occasionally times out at 30 s.

**Piece 5, `resolveDependency(name)`.** Uses `graph.computeIfAbsent(name, n -> { ... })` where the mapping function, in order to build the value, **calls `resolveDependency` recursively for the node's dependencies**, i.e. it re-enters `computeIfAbsent` on the *same map*, sometimes for a different key, occasionally (on a cyclic config) for the same key.

**Piece 6, a metrics reporter.** Every minute it calls `sessions.size()`, and if the size exceeds a threshold it iterates the map evicting the oldest entries, logging "evicting N of M" using the earlier `size()` value.

### The observed symptom

- **Duplicate billing.** A small number of users are charged twice at signup. Support has confirmed the billing service received two identical, non-idempotent calls milliseconds apart.
- **Quota drift.** Quotas are consistently lower than the sum of granted increments. Always low, never high, and the gap scales with traffic.
- **Leaked sockets.** The service opens more downstream sockets than it has sessions; the excess never closes and the FD count climbs until the process hits its limit.
- **Latency cliff on unrelated keys.** During a downstream profile-fetch slowdown, p99 latency for requests touching *completely different user IDs* jumps from 5 ms to seconds. Thread dumps during the cliff show many threads **BLOCKED** inside `ConcurrentHashMap` internals.
- **One permanent hang** in dependency resolution on a config that turned out to contain a cycle. That thread never returned and no deadlock section appeared in the dump.
- The "evicting N of M" log lines are occasionally nonsense, evicting more entries than the map reportedly contained.

### Your task

1. For each of the six pieces, say whether it is correct and, if not, name the defect and the symptom it produces.
2. Explain the governing principle in one sentence, the reason that using a thread-safe collection did not make the class thread-safe.
3. Explain the latency cliff and the permanent hang **mechanically**, in terms of what `computeIfAbsent` actually holds while the lambda runs.
4. Give the correct idiom for each broken piece.
5. Say which defect you would fix first and why.

### Clarify before diagnosing

- Is the value construction cheap and side-effect-free, or expensive/side-effecting? (This single question decides between `computeIfAbsent` and the compute-outside-then-`putIfAbsent` idiom, and it is the question candidates skip.)
- Is the downstream registration idempotent? (If yes, the duplicate is a wasted call; if no, it's a double charge, same race, wildly different severity.)
- Must "exactly once" mean exactly once *per process* or *globally*? (No in-process map can give you the latter; say so before designing.)
- Does the metrics reporter need a consistent snapshot, or is an approximation fine? (`size()` on a concurrent map is a hint, not a fact.)

### Why this problem matters

`ConcurrentHashMap` guarantees that **each of its own methods** is atomic. It guarantees nothing about *your sequence* of them, and that gap is where a large share of production Java concurrency bugs live, because the type name reads like a promise of safety and reviewers stop looking. The second half of the problem inverts the lesson: `computeIfAbsent` is the correct atomic tool, and misusing it (long work, blocking calls, or re-entrant map access inside the lambda) converts a correctness bug into a **liveness** bug, because the map is holding a bin lock the whole time the lambda runs. Knowing both halves, that per-method atomicity doesn't compose, and that the composition primitive holds a lock you can't see, is what a senior reviewer is expected to know cold.

---

## Strategy

### Classify

Guarded state (Type B), composition section. Catalog #1 (check-then-act outside the lock), #8 (compound-operation race, safe methods, racy caller sequences) and #14 (claim-after-work) for the first half; catalog #6's **alien-call-under-lock** clause for the `computeIfAbsent` half. Sweep 2 (every check-then-act) plus the **composition audit** finds pieces 1–3 and 6; sweep 4 (lock acquisition order, generalized to "what is held while foreign code runs") finds pieces 4 and 5.

### The governing principle (say this first)

**Per-method atomicity does not compose into per-sequence atomicity.** Every method of `ConcurrentHashMap` is atomic and linearizable. The lock is taken and released *inside each call*. Between your two calls it is not held by anyone, so the world changes freely in the gap, which is precisely the same disease as an unguarded read-modify-write, one level up. The type name promises that the map's own invariants survive concurrent access; it promises nothing about **your** invariant, which spans two calls.

The corollary that catches people: this is why a `ConcurrentHashMap` is *not* a drop-in fix for a racy `HashMap`. It fixes structural corruption (see `lost-update-hunt`) and fixes nothing about your logic.

### Piece-by-piece diagnosis

**Piece 1, `containsKey` then `put` then `get` (broken; three ways).**
The check-then-act: two threads both see the key absent, both construct a `Session` (opening a socket each), both `put`. The second `put` **overwrites** the first, and the first `Session`'s socket is now referenced by nobody and never closed, that is the FD leak, exactly. Worse, two callers that arrived at almost the same moment can be handed *different* `Session` objects, so any per-session state diverges. Third defect: `containsKey` + `get` is a second check-then-act, an eviction between them returns `null` from a method that "cannot" return null.
**Fix:** `computeIfAbsent` is the natural idiom *if* construction is cheap and side-effect-free. It is not here, it opens a socket, so use the safer shape: construct outside, then `putIfAbsent`, and if `putIfAbsent` returns a non-null existing value, **close the loser** and return the winner. That "close the loser" line is the part reviewers forget and it is the actual fix for the FD leak.

**Piece 2, `get`, add, `put` (broken).**
The textbook unguarded read-modify-write, dressed in map clothing. Always-low drift scaling with traffic is its fingerprint (see `lost-update-hunt` for the full treatment).
**Fix:** `merge(userId, n, Long::sum)`, or `compute(userId, (k,v) -> v == null ? n : v + n)`, or store `AtomicLong`/`LongAdder` values obtained once via `computeIfAbsent` and then increment the value object. All three are single atomic steps. `merge` is the cleanest.

**Piece 3, `containsKey` then non-idempotent side effect then `put` (broken; highest severity).**
Two threads pass the check, **both make the billing call**, then both put the marker. This is the duplicate charge. It is also catalog #14 in its purest form: **the claim happens after the work.** The window between "decided to do it" and "recorded that I did it" is exactly as wide as the HTTP call.
**Fix:** claim *before* the work, with one atomic operation whose return value tells you whether you won: `putIfAbsent(userId, marker) == null` means you are the winner and only you perform the registration; or `registered.add(userId)` on a `ConcurrentHashMap.newKeySet()`, whose boolean return is the linearization point of "this is mine". Then handle the honest follow-up: if the registration then *fails*, the claim must be released or a retry is impossible, so the marker should be a small state object (CLAIMED → DONE / FAILED), not a bare boolean, and the fix should say so.
And say the limit out loud: this gives exactly-once **per process**. Across replicas or restarts you need a persisted uniqueness constraint or an idempotency key on the billing call. Naming the boundary of your fix is a senior move.

**Piece 4, `computeIfAbsent` with a 200–2000 ms HTTP call inside (broken; liveness).**
This is the latency cliff, and the mechanism is the point of the problem. `computeIfAbsent` is atomic *because it holds the bin's lock for the entire duration of the mapping function*. Modern `ConcurrentHashMap` locks the first node of the bin (a hash bucket), not the whole map, but not one key either. So while your 2-second fetch runs, **every other key that hashes to the same bin is blocked**, and a resize (which needs to transfer bins) is stalled behind it. Threads pile up BLOCKED inside map internals, which is exactly what the dumps show. With a 30-second timeout in the fetch, one slow downstream call holds a bin for 30 seconds. The general rule this instantiates: **never run long, blocking, or foreign code while holding a lock**, and `computeIfAbsent`'s lock counts even though you never wrote a `synchronized`.
**Fix:** the two-phase idiom. `get` first (lock-free fast path, and the overwhelmingly common case for a cache); on miss, fetch **outside** the map, then publish with `putIfAbsent`, discarding your value if someone beat you. If you need "only one thread fetches per key" (thundering-herd protection), store a `CompletableFuture` (or a `FutureTask`) as the value: `computeIfAbsent` then only allocates an empty future, cheap and non-blocking, and the winner completes it outside the lock while losers await the future. That is the standard memoizing-cache design and it is the answer that shows depth. Or simply use a real cache library (Caffeine), which does exactly this.

**Piece 5, re-entrant `computeIfAbsent` on the same map (broken; explicitly forbidden).**
The javadoc states that the mapping function must not modify the map. Recursively calling `computeIfAbsent` on the same map from inside the lambda can (a) **deadlock permanently** when the recursive call lands on the same bin, the thread waits for a lock it already holds in a form that isn't reentrant at that granularity, or (b) corrupt the map's internal state and, historically, produce an infinite loop or a lost mapping. This is the permanent hang, and the dump shows no deadlock section because it is not a cycle among monitors the JVM tracks. The cyclic config merely guarantees the same-key case; the different-key case is already unsafe by contract whenever the hash collides.
**Fix:** resolve the dependency graph **outside** the map, do the recursion first, building the value in local state, then publish leaf-to-root with `putIfAbsent`. Detect the config cycle explicitly with a visiting set, and fail with a clear error rather than hanging. Never call back into the same map from a `compute*` lambda; treat the lambda body as if it were running under a lock you didn't write, because it is.

**Piece 6, `size()` used as a fact (weak, but flag it).**
`size()` on a concurrent map is a **snapshot that is stale the instant it returns**, and the iteration that follows uses a weakly-consistent iterator, which reflects some but not necessarily all concurrent modifications and never throws `ConcurrentModificationException`. So "evicting N of M" can log an M that never simultaneously existed and an N that exceeds it. Not a corruption bug, but it is a check-then-act at the reporting layer, and any *decision* made on `size()` (like the eviction threshold) is approximate by construction.
**Fix:** either accept and document the approximation, or maintain the count yourself as a `LongAdder` if the threshold decision must be tight. Also state what the eviction loop can and cannot promise.

### Prioritisation (part 5 of the task)

Rank by blast radius, per the pattern's rubric:

1. **Piece 3, duplicate non-idempotent billing.** Silent financial corruption affecting users. Ship tonight.
2. **Piece 1, socket leak.** Unbounded resource growth ending in a process-wide FD exhaustion outage. Also silently hands different callers different sessions.
3. **Piece 5, permanent hang.** Total loss of one code path, but only on a malformed config.
4. **Piece 4, latency cliff.** Degradation, not corruption, but it makes one slow dependency into a service-wide event.
5. **Piece 2, quota drift.** Wrong numbers, bounded impact, easy fix.
6. **Piece 6, log accuracy.** Cosmetic.

Saying the ranking, and *why*, corruption above resource exhaustion above liveness above latency above cosmetics, is what distinguishes a senior review from a bug list.

### Reproduce deterministically

- **Pieces 1, 2, 3:** start gate, N threads, all calling the method with the **same key** simultaneously, and count side effects, number of `Session` objects constructed, number of billing calls issued, final quota vs expected. With 32 threads on one key you get duplicates on the first run. For 100% determinism, inject a delay between the `containsKey` and the `put` via a test seam and use two threads.
- **Piece 4:** hold the fetch open with a latch inside the mapping function, then, from another thread, hit a **different key that hashes to the same bin** (construct colliding keys deliberately) and observe it block. That is the deterministic demonstration that the lock is per-bin and not per-key.
- **Piece 5:** call it on a config with a self-cycle; it hangs immediately. Wrap the harness join in a **timeout** so the hang fails the test in seconds.
- Every harness asserts an exact invariant: exactly one `Session` per key, exactly one billing call per user, quota equals the sum of grants.

### Confirm

- Duplicate billing: an outbound-call counter incremented at the call site; it exceeds the number of distinct users.
- Socket leak: constructed-`Session` count exceeds map size; FD count grows monotonically.
- Latency cliff: dumps show threads BLOCKED with `ConcurrentHashMap` frames on the stack while one thread sits in your fetch. That combination, BLOCKED *inside a "lock-free" collection*, is the confirmation that a `compute*` lambda is holding a bin.
- Hang: RUNNABLE-or-BLOCKED thread stuck in map internals, no deadlock section, no progress across three dumps.

### Prove the fixes

- **Pieces 1–3:** each compound operation is now a single atomic map operation whose return value decides the outcome; therefore exactly one caller can observe "I created it"/"I won the claim", and the side effect is performed only on that branch. The claim is the **linearization point**, and it precedes the work, so no window exists in which two threads both believe the work is theirs.
- **Piece 2:** `merge` performs read-and-write as one atomic step, so no update is computed from a stale read; the final value is the sum by induction over the atomic operations.
- **Piece 4:** no user code runs while a bin lock is held (the lambda, if any, only allocates an empty future); therefore the map's internal locks are held for O(1) time and a slow downstream call cannot block an unrelated key.
- **Piece 5:** the map is never re-entered from inside a mapping function; the contract's precondition holds; the recursion's own termination is guaranteed by explicit cycle detection.

And state the residual honestly: the fixes give per-process exactly-once and per-key single-flight. They do not give cross-replica exactly-once, and `size()` remains an approximation.

### Pitfalls

1. **"It's a `ConcurrentHashMap`, so it's thread-safe."** The collection is; your sequence isn't. This is the sentence the problem exists to kill.
2. **Reaching for `computeIfAbsent` everywhere** without asking whether the mapping function is cheap and side-effect-free. It is the right tool exactly when the value construction is fast, pure, and never touches the map.
3. **Believing the lock is per-key.** It is per-bin, so unrelated keys collide, and it stalls resizes.
4. **Claiming after the work** instead of before. Makes duplicates likely rather than impossible.
5. **Claiming with no failure story.** A bare boolean marker makes a failed registration permanently unretryable.
6. **Wrapping the whole thing in `synchronized`** to "be safe", that discards the map's concurrency entirely and usually indicates you didn't identify which invariant needed atomicity.
7. **Fixing the correctness bugs and leaving the FD leak.** `putIfAbsent` returning a loser value means you must *close* what you built.
8. **Branching on `size()`/`containsKey` at all.** They are hints. Every decision made on them is a new check-then-act.

### Check your understanding

1. State in one sentence why a thread-safe collection did not make this class thread-safe.
2. For pieces 1, 2 and 3, narrate the two-thread interleaving and name the resulting production symptom.
3. Why must the claim precede the side effect? Construct the duplicate-billing window with a timeline.
4. What exactly does `computeIfAbsent` hold, and for how long? Why does that make *unrelated* keys slow?
5. Why is storing a `CompletableFuture` as the value better than doing the fetch inside the lambda?
6. Why is re-entering the same map from a mapping function forbidden, and why does the resulting hang not appear in the JVM's deadlock section?
7. `putIfAbsent` returned a non-null value. What must you do with the object you just built, and what happens if you don't?
8. Rank all six defects by severity and justify the ordering.
9. What is the boundary of your "exactly once" guarantee, and what would you need to extend it?

### Transfers to

`lost-update-hunt` (the same disease one level down, on a plain field), `make-a-class-thread-safe` (the compound-operation trap from the build side), the web-crawler's atomic URL claiming (`visited.add(u)`'s boolean *is* the claim, claimed before the fetch), every memoizing cache and every "register once" / "initialize once per key" path in a service. The alien-call-under-lock lesson transfers straight to `lock-order-inversion-review`'s audit-listener defect, same rule, one written by you, one hidden inside a library.
