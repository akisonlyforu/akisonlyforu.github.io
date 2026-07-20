---
layout: post
title: Batching Aggregator (Size-or-Time Flush)
date: 2026-07-19
description: >-
  It is the cleanest instance of two conditions on one piece of guarded state, where one condition is about the data and the other is about the clock. The condition loop you…
categories: interview multithreading problems
---

Part of the [Time-Based State](/interview/multithreading/patterns/time-based/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** A recurring production-code exercise at companies that run a "build a component" round (write-behind buffers, metric flushers, bulk writers). Frequency claim, hedged: **moderate as a standalone coding ask, high as a follow-up**, it is the natural extension when an interviewer says "now make your writes efficient" or "batch these calls to the downstream API."

### Problem

`BatchingAggregator<T>` accepts items from many producer threads via `submit(item)` and flushes them downstream in batches. A batch is flushed when **either**:

- the buffer reaches `maxBatchSize`, **or**
- `maxLatencyMillis` have elapsed since the **first** item of the current batch arrived.

Whichever comes first. The downstream flush (a bulk database write, a bulk HTTP call) is slow relative to submission.

### Constraints

- No item may wait longer than `maxLatency` before its batch is flushed (modulo the flush's own duration).
- No busy-waiting and no fixed-interval polling, a timer that ticks every 10ms and checks the buffer is not an acceptable answer; the wait must be exactly as long as it needs to be and must be interruptible by an early trigger.
- The downstream call must not be made while holding the lock that producers need.
- Memory is bounded: an intake faster than the downstream can absorb must not grow the buffer forever.
- Shutdown must flush whatever partial batch exists.

### Clarify before solving

- **Backpressure policy** when the buffer (or the in-flight flush budget) is full: block the producer, reject the submit, or drop? The policy axis, and it changes `submit`'s signature.
- **Ordering**: must items be flushed in submission order? If yes, concurrent flushes are off the table (or must be serialised per key).
- **Delivery semantics**: if a flush fails, do we retry the batch (at-least-once, duplicates possible), split it, dead-letter it, or drop it?
- **Is the latency clock anchored to the first item or the last?** (Ask it as a question; the answer is "first", and the reason is the interesting part.)
- **One flusher or several?** Parallel flushes raise throughput and cost you ordering.
- **Per-key batching?** (Batching per destination/partition is common, that composes with per-key striping rather than changing this design.)

### Why this problem matters

It is the cleanest instance of **two conditions on one piece of guarded state**, where one condition is about the data and the other is about the clock. The condition loop you already know handles a single predicate; here the waiter must sleep for a computed duration, be woken early by a size trigger, be woken early by a *new deadline being established*, and treat all wake reasons identically. That is the timed-wait loop from the delayed scheduler, with a second trigger bolted on, and getting it right requires the discipline the family teaches: never trust a wakeup, always re-derive from the clock.

It also contains the single most instructive off-by-one-concept bug in the family: anchoring the deadline to the **last** item instead of the first. That version passes every test with bursty traffic and fails silently under a steady trickle, where each arrival pushes the deadline out and the batch is never flushed at all. Latency becomes unbounded while the code looks correct.

Finally, it forces the flush-outside-the-lock discipline. Holding the buffer's lock across a network round-trip freezes every producer for the duration, a self-inflicted latency spike that appears exactly when the downstream is slow.

---

## Strategy

### Classify

Time-based state, the **wait-until** branch, over a bounded-resource buffer. Say the fork out loud: this is not derive-on-read, because something must happen at a future moment *even if no caller shows up*, a lone item submitted at 3am must still be flushed `maxLatency` later, and nobody is going to call in and trigger the derivation. That requirement is exactly the family's criterion for legitimately owning a thread, and it is why this problem has a flusher and the rate limiter does not.

The mechanic being reused is the **timed-wait loop**, verbatim: `awaitNanos(deadline − now)` inside a re-check loop, producers signalling on the event that changes the deadline, claim under the lock and run outside it. The delayed scheduler's "signal on head change" reappears here as **"signal when the first item lands in an empty buffer"**, because that is the moment the deadline comes into existence, and the sleeping flusher's current nap is therefore wrong.

### Invariant

No item waits longer than `maxLatency` from its own arrival to the *start* of its batch's flush; no batch exceeds `maxBatchSize`; every accepted item is included in exactly one flush; and the buffer's size never exceeds its bound (with a stated policy for what happens at the bound).

The linearization point is the **buffer swap**: under the lock, take the current batch and install a fresh empty one, resetting the deadline. That single step is where a batch is born; everything before it is accumulation and everything after it is I/O.

### Mental model

A delivery van at a depot. It leaves when it is full, or fifteen minutes after **the first parcel** was loaded, whichever comes first. The dispatcher does not stand there checking a clock every few seconds; they set an alarm for the departure time of the parcel currently on the floor, and go do something else. Two things wake them: the van filling up (go now), and the *first* parcel of a new load arriving (there is now a departure time, and the alarm needs setting).

The anchoring rule is obvious in the depot and non-obvious in code: if you reset the fifteen minutes every time a parcel arrives, a slow steady trickle means the van never leaves. Parcel one waits forever while parcels two through nine hundred keep pushing the clock.

### Design

### The two conditions, one guarded state

The buffer, its size, and the current batch's deadline are one unit under one lock, one invariant, one lock. The flusher's predicate is a disjunction:

> flush when `size ≥ maxBatchSize` **or** `now ≥ deadline`

with `deadline` defined only while the buffer is non-empty. That "defined only when non-empty" is the structural core of the loop, and it gives the flusher exactly two wait modes:

- **Buffer empty** → wait untimed. There is no deadline to wait for; sleeping with a timeout here is a pointless wakeup, and polling here is the busy-wait the constraints forbid.
- **Buffer non-empty** → wait for `deadline − now` nanos. If that is already ≤ 0, don't wait at all; flush.

### Signalling: three moments, and which ones matter

Producers signal the flusher on:

1. **The first item entering an empty buffer.** Mandatory. The flusher is in an *untimed* wait with no deadline; the arrival creates one, and without a signal the flusher sleeps through it entirely. This is the exact analogue of the scheduler's signal-on-head-change: the sleeper's alarm is now wrong (here, absent), so it must be re-woken to recompute.
2. **The buffer reaching `maxBatchSize`.** Mandatory for latency. Without it, the flusher wakes on the *deadline* and flushes a full batch late, correct output, needlessly slow, and under high throughput you have silently reverted to time-only batching.
3. **Every other arrival.** Unnecessary, the flusher's alarm is already correct, but harmless, since the loop re-checks and re-naps. Signal-always versus signal-on-transition is a one-line throughput trade-off worth naming: always is simpler and costs a wakeup per submit; on-transition is a two-condition check in the producer. Say which you chose.

### The three wake reasons, identical treatment

The flusher returning from `awaitNanos` knows nothing about why it woke: the timeout may have elapsed, a producer may have signalled, or it may be a spurious wakeup or a race lost to a sibling flusher. **Do not branch on the reason.** Re-read size, re-read the deadline, recompute `now` from `nanoTime`, decide again. This is `while`-not-`if` with three wake reasons rather than one, and it is precisely the discipline the interviewer is checking survives the addition of a clock.

Concretely, a wake that turns out to be premature (signalled by an arrival that neither filled the batch nor changed the deadline) simply recomputes a shorter remaining nap and goes back to sleep. That must be a loop, not a straight-line "I woke, therefore flush".

### The deadline is anchored to the FIRST item

Set `deadline = now + maxLatency` when an item enters an **empty** buffer, and at no other time. Reset it only on the buffer swap.

The bug the other way is worth rehearsing because it is so plausible: refreshing the deadline on every arrival means a steady trickle of items, one every `maxLatency/2`, pushes the deadline out forever and the batch is never flushed until it happens to fill. The first item's latency is unbounded. It passes bursty tests, passes a load test, and fails on production's long tail. Being able to name this before writing anything is a strong signal.

### Flush outside the lock

Under the lock: take the current batch reference, install a fresh empty buffer, clear the deadline. Outside the lock: perform the downstream call. The lock protects the buffer, not the network.

Holding the lock across the flush blocks every producer for the duration of a network round-trip, and does so precisely when the downstream is slow, which is exactly when you most need producers to keep moving into the *next* batch. Same rule as running a scheduled task outside the scheduler's lock and running a cache loader outside `computeIfAbsent`: **claim under the lock, do the slow thing outside it.**

The swap-not-drain detail matters: swapping in a fresh buffer means producers arriving during the flush accumulate into batch n+1 with a fresh deadline, rather than contending with a half-drained collection or being blocked. It also makes the "exactly one flush per item" invariant trivially true, once swapped out, a batch is owned by exactly one flusher.

### Backpressure

If intake sustainably exceeds downstream throughput, something must give, and an unbounded buffer chooses OOM. Two places to bound, and you probably want both:

- **Buffer size**: a hard cap on accumulated-but-unflushed items.
- **In-flight flushes**: a semaphore capping concurrent downstream calls (this is a **bulkhead**: the downstream gets at most k concurrent writers, and the aggregator cannot pile up unbounded outstanding requests against a stalled dependency).

Then the policy axis, asked rather than assumed: **block** the submitting thread (real backpressure, correct for internal pipelines, a latency bug if submitters are request threads), **reject** (`submit` returns false; the caller decides), **drop** oldest or newest (acceptable for metrics and telemetry; say what you are losing), or a **bounded timed wait** then reject. There is no universally right answer; there is only the wrong answer, which is not having one.

### Ordering and parallel flushes

One flusher thread gives you flush-order = arrival-order for free. Allowing k concurrent flushes multiplies throughput and means batch n+1 can land downstream before batch n. If order matters, either keep a single flusher (and accept that downstream latency caps your throughput) or batch **per key** and flush keys independently, which is per-key striping from the event-bus problem, composed with this one. Say which constraint you were given; do not silently pick.

### Failure and delivery semantics

A flush can fail. Options, each a stated policy: retry the batch (that is the retry-with-backoff-and-jitter problem, reused wholesale, including its idempotency prerequisite, since a retried bulk write may partially have applied), split and retry to isolate a poison item, dead-letter the batch, or drop. Retrying in-line stalls subsequent flushes; retrying asynchronously means you need the in-flight bound above or retries accumulate. At-least-once with duplicates is the usual honest answer, and it makes idempotent downstream writes a requirement you are imposing, not a hope.

### Shutdown

The lifecycle question this problem always asks. Requirements:

1. **Reject new submits** after shutdown begins, under the *same* lock as the enqueue, otherwise an item slips in during the transition and is never flushed.
2. **Wake the flusher.** It is parked in `awaitNanos` and will never re-read your flag on its own, the flag-only-shutdown bug, in its timed form. Set the flag under the lock and signal.
3. **Flush the partial batch**, however small and however far from its deadline. Shutdown is a third trigger condition alongside size and time; the flusher's loop must handle "shutting down: flush whatever is there, then exit" rather than sleeping out the remaining latency.
4. **Wait, bounded, for in-flight flushes** before declaring shutdown complete, and report anything that never made it.

A `Runtime` shutdown hook is where this usually gets wired up, with a bounded wait, say so, since "we lose the last batch on deploy" is a real and common data-loss bug.

### Production equivalent

**Kafka's producer** is this design, configured (`batch.size` = the size trigger, `linger.ms` = the time trigger, `buffer.memory` + `max.block.ms` = the backpressure policy), if you can map your three parameters onto those three, say so, because it demonstrates you know you are rebuilding something. Also: JDBC batch updates, Elasticsearch's `BulkProcessor` (size, count, *and* flush interval, the same three triggers), CloudWatch `PutMetricData`, StatsD/OpenTelemetry batch span processors, and every write-behind cache. Reactor and RxJava give you `bufferTimeout(size, duration)`, which is this component as one operator. In a design round: *"batch with a size-or-time trigger, Kafka's producer already does this; I'd configure `linger.ms` rather than build it."* Hand-build only when implementation is the question.

### Pitfalls

1. **Deadline anchored to the last item**: a steady trickle never flushes; latency unbounded. Silent, and it survives testing.
2. **`Thread.sleep(maxLatency)` in a polling loop**: the sleeper is deaf: a full batch cannot wake it, so the size trigger is dead and every batch waits the full latency. The family's signature bug.
3. **Fixed-interval polling** ("check every 10ms"), busy work when idle, and average latency inflated by half the interval. It also can't be made precise without making it wasteful.
4. **Flushing while holding the lock**: every producer blocks for the duration of a network call, exactly when the network is slow.
5. **Branching on the wake reason**: treating `awaitNanos` returning as "the deadline elapsed" flushes early or flushes an empty batch. Re-derive from the clock; never trust a wakeup.
6. **Unbounded buffer**: deferred OOM; the aggregator absorbs a downstream outage silently until the process dies.
7. **Wall-clock deadlines**: an NTP correction flushes everything at once or strands a batch. `nanoTime`.
8. **No shutdown flush**: the last partial batch is lost on every deploy.
9. **Flag-only shutdown**: the flusher is asleep in a timed wait and never sees the flag; signal it.
10. **`size >= max` checked outside the lock, then swap**: check-then-act; two producers both trigger a flush and one flushes an empty or half-formed batch.
11. **Draining the buffer element-by-element under the lock instead of swapping**: longer critical section, and a second flusher can interleave into a partially drained batch.

### Check your understanding

1. Why must the latency deadline be anchored to the first item of a batch? Construct the arrival pattern where last-item anchoring gives unbounded latency, and say why testing misses it.
2. State the flusher's predicate as a disjunction and describe its two wait modes. Which one is untimed, and why is polling wrong in that mode?
3. Which producer events *must* signal the flusher, which one is optional, and what specifically breaks if the "first item into an empty buffer" signal is omitted?
4. The flusher returns from `awaitNanos`. Enumerate the possible reasons and explain why it must treat all of them identically.
5. Walk shutdown end to end: what stops accepting, what wakes the flusher, what happens to a batch that is two items long and eight seconds from its deadline, and how do you know when it is safe to exit?

### Transfers to

Write-behind caches, metric and log shippers, bulk indexers and bulk writers, request coalescing (batching N single-item lookups into one multi-get, the same triggers, with the added twist of returning a per-item future to each caller), Nagle's algorithm (the same size-or-time trade in TCP), and debounce/throttle logic in any UI. The size-or-time trigger pair is a general shape: whenever you are trading throughput against latency, you are choosing between "wait for more" and "send what you have", and this is the mechanism for having both.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/time-based/batching-aggregator).
