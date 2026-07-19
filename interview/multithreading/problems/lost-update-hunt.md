---
layout: post
title: Lost Update Hunt — "totals drift under load"
date: 2026-07-19
description: >-
  This is the most-reported concurrency bug in production software, and it is the one this round exists to test. It has four properties that make it a perfect interview…
categories: interview multithreading problems
---

Part of the [Debugging & Code Review](/interview/multithreading/patterns/debugging-and-code-review/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Stripe's bug-bash round (the canonical reported defect: *lost updates under high concurrency traced to an unguarded read-modify-write*). Variants at Coinbase (ledger balances), Uber (per-driver trip counters), AWS (metrics aggregation).

### The code under review (described, not shown)

A `MetricsAggregator` service object. It is constructed once at startup and the same instance is injected into every request handler, so **every request thread calls into the same object**.

Its state:

- A plain `long` field `totalRequests`.
- A plain `long` field `totalLatencyMicros`.
- A plain `HashMap<String, Long>` field `perEndpointCount`, mapping endpoint name to a running count.
- A `final` `String` field `serviceName`, set in the constructor and never written again.

Its methods:

- `record(endpoint, latencyMicros)` — called on every request. It increments `totalRequests` by one; adds `latencyMicros` to `totalLatencyMicros`; then reads the current count for `endpoint` out of `perEndpointCount` (treating a missing key as zero), adds one, and puts the new value back.
- `snapshot()` — called once a minute by a reporting thread. It reads all three fields and returns a report object containing the totals and a copy of the per-endpoint map.
- `reset()` — called by an admin endpoint. Sets both longs to zero and clears the map.

There is no `synchronized`, no lock, and no `volatile` anywhere in the class. There is a comment on `record` that reads *"hot path — kept lock-free for throughput"*.

A sibling class, `LedgerAccount`, has the identical shape with a `long balance` field and a `deposit(amount)` method that reads `balance`, adds `amount`, and writes it back. Treat it as the same bug wearing a more expensive costume.

### The observed symptom

- Under low traffic in staging, reported totals match the load generator exactly.
- In production at ~4,000 requests/second across 32 cores, the reported `totalRequests` for a minute is consistently **2–8% lower** than the request count recorded by the load balancer for the same minute.
- The deficit **grows with concurrency** and is **always low, never high**.
- Occasionally the reporting thread throws an unexpected exception from inside the map read, and once the service reported a per-endpoint count for an endpoint that had been removed from the config months earlier.
- Nobody can reproduce it locally. Adding a log line inside `record` makes the drift shrink to near zero.

### Your task

1. Name the defect precisely, as an interleaving — not as a label.
2. Explain every part of the symptom, including why the drift is always *low*, why it scales with concurrency, why logging "fixes" it, and what the reporting-thread exception is.
3. Describe how you would reproduce it **deterministically**.
4. Give the fix options with their trade-offs, and say which you would ship.
5. State the argument that proves the fix correct.

### Clarify before diagnosing

- Is the drift ever *high*? (Always-low is diagnostic: increments overwrite each other; nothing invents extra.)
- Is `snapshot()` allowed to be slightly stale, or must it be a consistent point-in-time view? (This decides whether the fix needs a lock or just atomics.)
- Are the two longs required to be mutually consistent — does anything divide latency by count? (If yes, the invariant spans two fields and no single atomic can cover it.)
- What's the actual throughput requirement? (The "lock-free for throughput" comment is an unmeasured claim; ask for the number.)

### Why this problem matters

This is the most-reported concurrency bug in production software, and it is the one this round exists to test. It has four properties that make it a perfect interview artifact: the broken code looks obviously fine, the symptom is quantitative rather than catastrophic, the naive fix (`volatile`) is wrong in a way that reveals whether the candidate understands the difference between *freshness* and *atomicity*, and the correct fix depends on a question about the invariant that most candidates never ask. If you can only debug one thing cold, debug this.

---

## Strategy

### Classify

Guarded state (Type B) seen from the review side. The defect is catalog #1 — check-then-act outside the lock — in its miniature form, the **unguarded read-modify-write**. Sweep 1 (find shared mutable state, note what guards each field) and sweep 2 (find every check-then-act) locate it in under a minute.

### The invariant being broken

*After N successful calls to `record`, `totalRequests` equals N, `totalLatencyMicros` equals the sum of all N latencies, and the per-endpoint counts sum to N.* The linearization point should be a single instant at which the counter moves from k to k+1. In the broken code there is no such instant — the increment is three machine steps (load, add, store) with the world free to run in between.

### Symptom → hypothesis

Read the symptom list as evidence, and let each line kill hypotheses:

- **Always low, never high.** This is the strongest signal in the report. A lost update *overwrites* a concurrent increment; it can never manufacture one. Immediately rules out double-counting, retry-without-idempotency, and duplicate delivery — all of which run high. Combined with "scales with concurrency", the hypothesis is fixed before you read a line of code: **two threads read the same value and both write back their own +1; one increment vanishes.**
- **Scales with concurrency.** The probability that a second thread lands inside the load-add-store window grows with the number of threads in flight. 2–8% at 32 cores is exactly the right order of magnitude.
- **Logging makes it disappear.** The log call is a synchronized I/O operation: it both widens the time outside the window (so the odds of two threads being *inside* it simultaneously fall) and, incidentally, adds happens-before edges. This is the classic heisenbug tell — say the words "the observation perturbs the schedule" and move on.
- **Exception from the reporting thread inside the map read.** This is a *second* bug and you must call it out separately: `HashMap` is not merely racy on values, it is **structurally unsafe** under concurrent modification. Concurrent `put`s during a resize can corrupt the bucket chain — historically producing an infinite loop and 100% CPU, and in modern JDKs typically lost entries, resurrected entries, or a spurious exception. That is what the "count for a deleted endpoint" observation is: a resurrected entry from a corrupted table. **A shared `HashMap` is a strictly more serious defect than the lost counter, because it corrupts the data structure and not just a value.**

Also note what the hypothesis is *not*. It is not "the field needs `volatile`". Volatile would give every thread a fresh read and still lose updates, because nothing stops a second thread from running between your fresh read and your write. Say this unprompted — it is a graded moment.

### Reproduce deterministically

Three levels, and you should offer all three.

**Level 1 — amplify.** A stress harness: T threads (more than cores, e.g. 64), each calling `record` I times (e.g. 100,000), all released together by a **start gate** latch so they overlap instead of running sequentially. Assert the final total equals exactly `T × I`. Give the whole harness a **timeout** so a hang fails fast. This fails within one run and shows the deficit scaling as you raise T.

**Level 2 — make it 100%.** Introduce a test-only seam (a protected hook, a subclass, or a debug flag) that yields or sleeps a millisecond **between the read and the write** of the counter. With two threads and that delay, every single increment pair collides and the final total is exactly 2 instead of the expected large number. This is the strongest confirmation available without special tooling, because it demonstrates you know precisely *which* interleaving is at fault rather than merely that something is racy. Re-running this exact test after the fix is your regression test.

**Level 3 — tooling.** jcstress for the minimal two-thread case (it enumerates the observed result set, so "we saw 1 where only 2 is legal" is machine-produced evidence). ErrorProne's `@GuardedBy` annotation on the fields turns the unguarded accesses into compile errors — worth proposing as the *institutional* fix so the next instance of this bug never merges.

### Confirm the diagnosis

- The deficit is a monotonically increasing function of thread count, and always ≤ 0 relative to the true count.
- The deficit shrinks toward zero as you insert delay *outside* the window and grows toward 100% as you insert delay *inside* it. That asymmetry is the signature; nothing but a read-modify-write race behaves that way.
- A thread dump is unhelpful here and you should say so: there is no hang, nothing is BLOCKED or WAITING. **Thread dumps diagnose liveness, not correctness** — the instruments for a drift bug are assertions and injected delays, not `jstack`.

### The fixes

Three, and the right answer depends on the clarifying question about whether the two longs must be mutually consistent.

**Fix A — atomics (F1).** Replace the counters with `AtomicLong` and use `incrementAndGet` / `addAndGet`; replace the map with a `ConcurrentHashMap<String, LongAdder>` (or `ConcurrentHashMap<String, Long>` with `merge(endpoint, 1L, Long::sum)`). Each compound operation becomes a single atomic step. Correct **if and only if** each field's invariant stands alone. Cheap, lock-free on the hot path, preserves the spirit of the original comment.

For a genuinely hot counter, `LongAdder` beats `AtomicLong` under contention: it spreads increments over per-cell stripes so threads stop fighting for one cache line, at the cost of `sum()` being an accumulation over cells that is only exact at quiescence. Since `snapshot()` runs once a minute and tolerates a hair of skew, `LongAdder` is the right pick — and *saying why you picked it* is the depth marker here.

**Fix B — one lock (F2).** Make `record`, `snapshot` and `reset` all `synchronized` on the same monitor. This is the correct fix **if any consumer divides total latency by total count**, because that invariant spans two fields and no per-field atomic can maintain it — under Fix A a snapshot can read a count from after an update and a latency from before it, producing an average that is off. Same argument for `snapshot()` needing a *consistent* view of the map and the totals together. The cost is that the hot path now serializes; name it, and note that the critical section is a handful of nanoseconds so the "lock-free for throughput" comment was never backed by a measurement.

**Fix C — remove the shared state (F0).** Accumulate per-thread (thread-confined counters, or a striped array indexed by thread, or the metrics library's own per-thread accumulators) and merge only in `snapshot()`. Zero contention on the hot path by construction, because there is no shared mutable state to guard. This is what real metrics libraries do, and it's `LongAdder`'s design generalized. Propose it as the answer if the throughput requirement is real. Its cost: snapshot becomes O(threads), and reset needs care.

**What to ship:** Fix A with `LongAdder` and a `ConcurrentHashMap`, *unless* the answer to "does anything divide latency by count?" is yes — then Fix B, or Fix A plus a single `AtomicReference` to an immutable (count, latency) pair updated by CAS, which restores the cross-field invariant without a lock. Also fix the `HashMap` regardless of which counter fix you choose; that one is not optional and it outranks the counter bug in severity.

### Prove the fix

The correctness argument, not the test result:

- **Fix A:** every mutation of the counter is a single atomic read-modify-write instruction; there is no window between the read and the write for another thread to occupy; therefore after N calls the value is N by induction on the atomic operations, each of which is totally ordered with respect to the others.
- **Fix B:** every read and every write of every field happens while holding the same monitor; therefore all critical sections are totally ordered; therefore each `record` observes the state left by the previous one and no update is computed from a stale read. The monitor also supplies the happens-before edge that makes the reporting thread's reads fresh — which the atomics fix must supply per-field.
- **Fix C:** each counter is touched by exactly one thread, so there is no shared mutable state and no race is possible; the merge in `snapshot` reads each per-thread cell under whatever edge the publication provides.

Then the regression test: the level-2 deterministic reproduction (injected delay in the window) now produces the exact expected total, every time. And state the honest limit: the level-1 stress test going green proves nothing on its own — it is the argument plus the deterministic test that constitute the proof.

### Pitfalls

1. **Reaching for `volatile`.** Freshness is not atomicity. `volatile long count; count++` loses updates exactly as before. This is the single most common wrong answer and interviewers plant the field type to invite it.
2. **Fixing the counters and leaving the `HashMap`.** The map bug is worse — it corrupts the structure, not just a value — and it is the one that explains the reporting-thread exception. Missing it means you explained only part of the symptom.
3. **Making each field atomic when the invariant spans two fields.** Per-field atomicity does not compose into cross-field consistency. Ask the clarifying question first.
4. **Declaring the composition fixed.** Even with a `ConcurrentHashMap`, a caller doing get-then-put is still racy — see `check-then-act-on-concurrent-map`. Use `merge`/`compute`, not get-then-put.
5. **Not addressing `reset()`.** Under Fix A, `reset` clears three things non-atomically, so a concurrent `snapshot` can see two fields zeroed and one not. Either lock it or accept and document the skew.
6. **Leading with the fix.** The three lines of fix are worth less than the interleaving narration, the always-low reasoning, and the deterministic reproduction.

### Check your understanding

1. Narrate the losing interleaving for two threads at the machine-step level (load, add, store), and say exactly which increment is lost.
2. Why is the drift always low and never high? What class of bug *would* run high?
3. Why does adding a log line reduce the drift, and why is that not a fix?
4. Under Fix A, construct the interleaving where a snapshot reports an impossible average latency. What invariant did the atomics fail to protect?
5. Why is `LongAdder` faster than `AtomicLong` under contention, and what does it give up?
6. The reporting thread threw from inside a map read and once saw a deleted endpoint. Explain both from the `HashMap` resize mechanism.
7. Your stress test passes 1,000 times after the fix. What has it proved? (Answer: that those schedules didn't fail. The proof is the lock-coverage argument.)

### Transfers to

Every ledger, balance, quota, inventory-count, seat-reservation and rate-limit bug you will ever be paged for. The identical shape appears in `check-then-act-on-concurrent-map` (one level up, in composition), in the guarded-state family's `make-a-class-thread-safe` (the same fix from the build side), and in `visibility-bug-no-lock` (the sibling defect where the problem is freshness rather than atomicity — solve both and you own the distinction).
