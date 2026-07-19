---
layout: post
title: Striped Counter (LongAdder) — The Hot Counter Problem
date: 2026-07-19
description: >-
  It is the smallest possible demonstration that correctness and scalability are separate properties, and that the JMM only speaks to the first. An atomic counter is…
categories: interview multithreading problems
---

Part of the [Concurrent Data Structures](/interview/multithreading/patterns/concurrent-data-structures/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Usually arrives as a small, deceptively easy question — "count requests across 64 threads" — whose real content is the follow-up: "throughput went *down* when we added cores; why?" Common as a warm-up or a probe in senior backend rounds; frequency claim is directional.

### Problem

A single counter is incremented by every request, on every thread, in a hot service — metrics, rate accounting, request totals. Reads are comparatively rare (a metrics scrape every few seconds). Make it correct, then make it scale.

Explain why the obvious atomic answer degrades under high thread counts, design the fix, and state precisely what the fix gives up.

### Constraints

- Increments are extremely frequent and come from many threads simultaneously.
- Reads are rare and are used for reporting.
- No increment may be lost.
- Single JVM.

### Clarify before solving

- **How contended is this actually?** The honest first question. A counter incremented a thousand times a second by four threads needs nothing clever, and saying so is part of the answer.
- **Does anyone need the value to be exact *at a specific instant*, or just eventually and approximately right?** This is the question that unlocks the design.
- **Is the counter ever used in a decision** — a limit check, an admission test, "if count < N then proceed"? If yes, a relaxed read is not merely imprecise, it is unsafe, and the whole approach changes.
- **Is the count monotonic (increments only) or does it also decrease?** Affects whether a relaxed sum can be reasoned about.
- **How many distinct counters are there?** Thousands of per-endpoint counters each with their own cell array is a memory decision worth surfacing.

### Why this problem matters

It is the smallest possible demonstration that **correctness and scalability are separate properties**, and that the JMM only speaks to the first. An atomic counter is unimpeachably correct at any thread count; it is also, at high thread counts, slower than it was with fewer threads — a result that no correctness reasoning predicts and that most candidates cannot explain. The explanation lives entirely in the cache coherence protocol, and a senior engineer working on high-throughput systems is expected to have it.

It also teaches the family's central move in its purest, least cluttered form. There is no chain to splice, no ordering to preserve, no ABA to worry about — just the observation that the *sum* is global but each *addend* need not be, so shard the addends and reconcile on read. That is exactly what the LRU cache does with recency, what a segmented map does with keys, and what a partitioned database does with rows. Learning it here, where the structure is one number, makes it recognizable everywhere else.

And it is a restraint test in both directions. Reaching for the striped counter when the counter isn't hot is over-engineering; using it for a limit check, where the relaxed read is genuinely wrong, is a correctness bug dressed as a performance win.

---

## Strategy

### Classify

Concurrent data structure reduced to its atom: **one word, one operation, maximal contention**. No structural invariant, no ordering, no traversal. Whatever is hard here is hard for reasons that have nothing to do with logic and everything to do with the machine — which is exactly why the problem is instructive.

### Invariant

The counter's value equals the total number of increments applied. Split it deliberately into two clauses, because the design lives in the gap between them:

- **Conservation (global, non-negotiable):** no increment is lost or double-counted. Every increment must eventually be reflected in the total.
- **Instantaneous readability (global, negotiable):** at any instant, a reader can obtain the exact current total.

The atomic counter provides both. The striped counter provides the first and trades away the second. Recognizing that these are *separate* clauses is the entire insight.

### Mental model

A stadium with one turnstile clicker held by one usher: every entrant must reach that one usher, so the queue at the usher *is* your throughput ceiling regardless of how many gates you opened. The fix isn't a faster usher — it's one clicker per gate, and you add up the clickers when someone actually asks for the total. The total is correct whenever you compute it; it is simply not a live figure during a rush.

The failure mode this metaphor predicts is real: with one clicker, adding gates makes things *worse*, because now more people are converging on the same usher from further away.

### Why AtomicLong degrades — the cache-line story

An atomic increment is a compare-and-set retry loop (or a hardware fetch-and-add, which has the same coherence behaviour). Either way, to modify a variable a core must hold that variable's **cache line in exclusive state**. So:

1. Core A wants to increment. It requests exclusive ownership of the line; every other core's copy is invalidated.
2. Core B wants to increment. It requests exclusive ownership; A's copy is invalidated. The line migrates across the interconnect.
3. Repeat, once per increment, forever.

The line ping-pongs between cores, and every increment costs a cache-coherence round trip — vastly more than the arithmetic. Worse, on a CAS-loop implementation, losers retry: with N threads hammering one variable, the number of failed attempts grows with N, so **throughput can decrease as you add cores.** That is the counter-intuitive result the follow-up is testing, and the explanation is the deliverable.

Note the shape: this is the *same* phenomenon as false sharing, minus the "false." There the variables were independent and shared a line by accident; here there is genuinely one variable. Either way, the cost is the line migrating, and the cure is the same — give each writer its own line.

### The fix: shard the addend, reconcile on read

The addends are independent. Nothing requires two concurrent increments to touch the same memory; they only have to *both eventually count*. So:

- Maintain an array of cells, each an independently updatable counter, each **padded onto its own cache line**.
- Each incrementing thread is steered to a cell by a per-thread probe value. Different threads hit different cells, different cache lines, no coherence traffic between them. Increments become effectively uncontended.
- On collision (a thread's CAS on its cell fails), don't just retry the same cell — **rehash the thread's probe to a different cell**. The structure adapts: contention itself is the signal that spreads threads out. The cell array also grows on sustained contention, typically bounded around the number of cores, since beyond that more cells buy nothing.
- Read = **sum the cells** (plus a base value used in the uncontended case, before any cells are allocated).

That last point is the design's other virtue: with a single thread, or no contention, there are no cells at all and the operation is a plain CAS on the base — so the striped counter degrades gracefully to the atomic counter's behaviour rather than paying a fixed overhead.

**The trade, stated exactly:** the sum is computed by reading cells one at a time while other threads are incrementing them. It is therefore not atomic with respect to concurrent increments — it returns a value that is correct for *some* set of increments, not for a single instant. Conservation holds (every increment lands in some cell and will be seen by some later sum); instantaneous readability does not. For a metric, this is irrelevant: the number was going to be stale by the time it reached your dashboard anyway.

### Why this is the same trade as sharding — say it out loud

The move is identical to what a partitioned database does, what a segmented cache does, and what a per-bin locked hash map does with its element count:

> A global aggregate over independent local updates is expensive precisely *because* it is global. Make the updates local, and pay for the aggregate only when someone asks — accepting that the aggregate is now a reconciliation rather than an observation.

Every instance of this pattern makes the same exchange: cheap writes, expensive-and-approximate reads. Recognizing it as one pattern rather than five tricks is the transferable outcome. Note also the direct kinship with the LRU cache's lossy recency buffers — there, per-thread buffers accumulate access records reconciled later under a lock; here, per-thread cells accumulate increments reconciled later by a sum. Same shape, different payload.

### When AtomicLong is fine — and when the striped counter is wrong

**Stay with the plain atomic when:**
- Contention is low. Uncontended CAS is a handful of nanoseconds; there is nothing to fix, and the cell array is just memory and indirection.
- You have many counters. Thousands of per-endpoint counters, each with a padded cell array sized to the core count, is a real memory footprint. One shared hot counter justifies striping; ten thousand cold ones do not.
- You need the return value of the increment. `getAndIncrement` returning a meaningful, unique, ordered number — sequence generation, id allocation, ticket dispensing — is a property the striped counter **cannot** provide, because there is no single sequence. This is the sharpest disqualifier and worth leading with.

**Never use the striped counter when:**
- The count gates a decision — "if count < limit, admit" — because the sum is approximate *and* the check-then-act window is now wider and less analysable. Limit enforcement wants an exact atomic read-modify-write, or a semaphore, which is the right primitive for "at most N concurrent" anyway. Reaching for a striped counter to implement a limit is a genuine correctness bug, not a trade-off.
- You need a compare-and-set on the total. There is no total to CAS.

**The honest summary:** the striped counter is a **write-optimized, read-degraded** counter. Use it where writes vastly outnumber reads and the read is a report. Otherwise use the atomic.

### Pitfalls

1. Reaching for the striped counter without evidence of contention. Over-engineering, and it costs memory and indirection.
2. Using it where the increment's return value matters (id/sequence generation). It cannot serve that.
3. Using it for limit checks or admission control. The relaxed read makes the check unsound; use an exact atomic or a semaphore.
4. Forgetting to pad the cells. You've spread the writes across the array but they still share cache lines, so you kept all the complexity and none of the benefit — the most disappointing possible outcome.
5. Assuming the sum is atomic; branching on it, or asserting on it in a test that also has live writers.
6. Building your own cell array with a fixed thread-index mapping (thread id modulo K). It doesn't adapt to actual contention, it breaks with pooled or virtual threads whose identities churn, and it's worse than the adaptive probe rehash it's imitating.
7. Sizing the cell array far beyond the core count. Cells beyond the number of concurrently incrementing threads reduce contention no further and only cost memory and summation time.
8. Making the counter `volatile` and incrementing it. Freshness is not atomicity — the oldest bug in the book, and it still shows up here.
9. Using the striped counter in a tight read loop. Every read is O(cells) and touches every cell's line, which re-introduces exactly the coherence traffic you eliminated — now on the read path.

### Check your understanding

1. Explain, in terms of cache-line ownership, why an atomic counter's throughput can *fall* as you add threads. Why is the arithmetic irrelevant to the answer?
2. Split the counter's invariant into two clauses and say which one the striped design keeps and which it trades.
3. Why does a failed CAS on a cell trigger a *probe rehash* rather than a plain retry? What is contention being used as a signal for?
4. Why is the sum not atomic against concurrent increments, and why is no increment ever lost anyway? State both properties precisely.
5. Give three workloads where the plain atomic counter is the better choice, with a distinct reason for each.
6. Why can a striped counter not generate unique sequence numbers?
7. Someone uses a striped counter to enforce "at most 100 in flight." Name two separate things wrong with that, and give the primitive they should have used.
8. You forgot the padding. Describe what your benchmark shows and why.
9. Where else in this family does the exact same "local writes, reconciled aggregate" trade appear? Name two, and say what plays the role of the cells in each.

### Transfers to

`lock-striping-and-concurrent-hashmap` (whose element count is internally striped for exactly this reason, and whose `size()` is an estimate for exactly this reason); `thread-safe-lru-cache` (lossy per-thread recency buffers are this pattern with access records instead of increments); `lock-free-or-bounded-queue` (the same cache-line analysis, applied to head and tail cursors); metrics and observability library design generally; and any conversation about sharded or partitioned storage, where "the cross-shard aggregate is the expensive part" is the same sentence at a different scale.
