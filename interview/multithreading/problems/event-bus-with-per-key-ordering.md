---
layout: post
title: Event Bus with Per-Key Ordering
date: 2026-07-19
description: >-
  It is the cleanest available exercise in partial ordering: total order is trivial (one thread) and no order is trivial (a pool); the interesting engineering lives in between…
categories: interview multithreading problems
---

Part of the [Task Lifecycle](/interview/multithreading/patterns/task-lifecycle/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** A staple of the production-code round at companies that run event-driven backends (Stripe, Coinbase, Uber payments/ledger teams). Frequency claim, hedged: **regularly reported in "build a component" rounds, essentially never in algorithm rounds.** Its distributed cousin ("Kafka gives you per-partition ordering. How would you preserve it in the consumer?") is asked at least as often as the in-JVM version.

### Problem

Build an in-process event bus. Publishers call `publish(key, event)`; subscribers registered for an event type receive events on bus-owned threads. The ordering requirement:

> Events with the **same key** must be delivered to a given subscriber **in publication order**. Events with **different keys** may be processed **concurrently**.

Keys are things like accountId, orderId, userId, high cardinality (millions possible, thousands live), unbounded and unknown upfront.

### Constraints

- A fixed, bounded set of threads. Not one thread per key, not one thread per event.
- Bounded memory: an unbounded in-memory backlog is a deferred OOM, not a design.
- A slow or failing subscriber must not silently stop the bus for everyone.
- Delivery semantics must be stated explicitly (at-most-once vs at-least-once). The problem does not pick for you.

### Clarify before solving

- **Ordering scope**: per key across all subscribers, or per (key, subscriber) pair? (Per-pair is weaker and much easier to isolate. Ask.)
- **Delivery guarantee**: at-most-once (fire and forget, drop on failure) or at-least-once (retry until the handler succeeds, and therefore duplicates are possible → handlers must be idempotent)?
- **Backpressure policy when a key's backlog is full**: block the publisher, reject the publish, or drop (oldest/newest)? This is the policy axis and the answer changes the API's signature.
- **Is the publisher allowed to block at all?** If publishes come from a request thread, blocking is a latency bug wearing a correctness costume.
- **Subscriber execution**: synchronous on the delivery thread, or is each subscriber independently isolated?
- **Key lifecycle**: do we ever get to forget a key? (Millions of one-shot keys with a permanent per-key data structure is a memory leak with a schedule.)
- **Durability / restart**: in-memory only, or is this the consumer side of a real broker? (Say "if durability is required, this is Kafka's job and I'd consume per-partition" and scope down.)

### Why this problem matters

It is the cleanest available exercise in **partial ordering**: total order is trivial (one thread) and no order is trivial (a pool); the interesting engineering lives in between, and almost every real event-driven system lives there. The problem punishes the two reflexes that come first: "spawn a thread per key" (works for 10 keys, dies at 10,000) and "just use a thread pool" (correct throughput, silently reorders same-key events under load, and the bug appears only in production because it needs two events for one key in flight simultaneously).

It also forces the isolation conversation: once you stripe keys onto shared lanes, one slow key head-of-line-blocks every other key on its lane, and you have to decide whether you care. That trade, sharing threads versus isolating failure, is the same conversation as bulkheading, one level down.

---

## Strategy

### Classify

Task lifecycle for the dispatch machinery (worker loops, backpressure, shutdown, the four lifecycle questions apply unchanged), wrapped around one extra constraint that is really a **serialization requirement per key**. Say the classification as a single sentence and the design falls out of it:

> *Per-key ordering means each key needs its own single-threaded execution lane; the engineering is entirely about how to give millions of keys a lane without giving them a thread.*

The mechanic being reused is **per-key striping**, the same move as the per-client map of rate limiters and the per-key cached future: the state is self-contained per key, the map's per-bin atomicity is your locking, and no global structure or global lock ever appears on the hot path.

### Invariant

For any key k and any subscriber s: if `publish(k, e1)` happens-before `publish(k, e2)`, then s's handling of e1 completes before s's handling of e2 begins. For keys j ≠ k, no ordering is imposed and handlers may run concurrently. Every accepted event is delivered at least once (or at most once, whichever you declared) and is never delivered concurrently with another event of the same key.

Note what the invariant does *not* say: nothing about ordering across keys, and nothing about the order in which two different subscribers see the same event. Stating the invariant this narrowly is most of the work. Candidates who write "events are processed in order" have promised a total order they cannot deliver and do not need.

### Mental model

A post office with a fixed number of sorting benches. Every letter carries a street name. A street is permanently assigned to one bench by the first letter of its name; a bench works its pile strictly front-to-back. Streets on different benches move in parallel; two letters for the same street can never be worked simultaneously, because they are physically in the same pile at the same bench. Nobody hires a clerk per street.

The cost is visible in the same picture: one street with a monstrous, slow pile stalls every other street on that bench. That is head-of-line blocking, and it is the price of striping.

### Design 1: key→lane striping (ship this)

`laneIndex = Math.floorMod(key.hashCode(), N)` for N lanes; each lane is a **bounded** blocking queue plus exactly one worker thread running the standard consumer loop. Publishing computes the lane and enqueues.

Why ordering holds, in one sentence: a key maps to exactly one lane for the process's lifetime, the lane's queue is FIFO, and the lane has exactly one consumer, so same-key events are enqueued in order into a single-consumer FIFO and therefore handled in order. That is the entire correctness argument and you should be able to deliver it in that form.

Why not `ExecutorService` with N threads and one shared queue: N consumers on one FIFO gives you *dequeue* order but not *handling* order. Two same-key events can be dequeued by two workers a microsecond apart and run concurrently. **A shared queue with multiple consumers preserves no ordering whatsoever.** This is the single most common wrong answer; be able to state it as the reason you rejected the obvious design rather than as an afterthought.

Properties to name:

- **N lanes, not N keys.** Thread count is decoupled from key cardinality, the whole point. Keys sharing a lane are the compromise.
- **Head-of-line blocking between co-striped keys.** One slow key delays unrelated keys on its lane. Mitigations: more lanes than threads is *not* one (the lane must own a thread to be serial); the real levers are (a) more lanes, reducing collision probability, (b) making handlers fast, (c) moving slow subscribers to their own lane set.
- **Skew.** A hot key (one whale account) pins one lane at 100% while others idle, and no amount of lane-count fixes it, because a single key is irreducibly serial. Say this: **per-key ordering caps your parallelism at the number of distinct active keys**, and a skewed key distribution is a throughput ceiling you cannot design away, only renegotiate (does that key really need ordering?).
- **No per-key bookkeeping at all**, so no key-lifecycle leak. Striping's quiet advantage over Design 2.

### Design 2: chained future per key

Keep `ConcurrentHashMap<Key, CompletableFuture<Void>>` holding the **tail** of each key's chain. To publish, atomically replace the tail with "the old tail, then run this handler". The append must happen inside the map's atomic `compute`, because read-tail-then-write-tail is a check-then-act that interleaves into a lost link and a lost ordering guarantee. Handlers run on a shared pool; ordering holds because each stage is *dependent on* the previous one, so the runtime never starts e2's handler until e1's has completed.

This is the same released-by-completion mechanic as the DAG scheduler, degenerating to a chain instead of a graph. Trade-offs:

- **No dedicated thread per key** and no head-of-line blocking between keys at all. A slow key occupies a pool thread only while it is actually running. Better isolation than striping.
- **The map entry is a leak** unless you remove it when the chain drains. The hygiene move: in a `compute`, if the tail you hold is already complete and is the one you last appended, remove the entry (return null). Getting this exactly right under concurrency is fiddly, the same "remove failed futures or the failure is cached forever" hygiene as the read-heavy cache, one notch harder.
- **Backpressure is awkward.** A chain has no natural length limit; you have to count per-key depth yourself to reject or block, whereas the striped design gets it free from a bounded queue.
- **Where the continuation runs** matters: non-`*Async` continuations run on the completing thread, so a chain can quietly hop your handler onto whatever thread finished the previous one. Name the executor explicitly.

**Choosing**: striped lanes for an interview and for most production: simpler, bounded by construction, trivially explainable. Chained futures when key isolation matters more than backpressure simplicity, or when handlers are already async.

### Backpressure: the policy axis, per lane

Bounded queue per lane, and one explicit policy when it fills. The four options are the same four as always: **block** the publisher (real backpressure; unacceptable if publishers are request threads), **reject** (`publish` returns false / throws, the caller decides), **drop oldest / newest** (acceptable only for telemetry-grade events; say what you are losing), or **timeout** (bounded block, then reject). Ask which; do not default to blocking out of reflex.

The subtlety unique to this problem: backpressure is **per lane**, so a full lane rejects publishes for every key striped onto it, not just the offending key. If per-key fairness matters, you need per-key depth counters on top of the lane queue, worth mentioning, not worth building unless asked.

Unbounded queues here are the standard deferred-OOM mistake, and per-key backlogs make it worse than usual: a wedged downstream means every key accumulates simultaneously.

### Slow-consumer isolation

If several subscribers share a delivery thread, the slowest one sets the pace for all of them, and a subscriber that blocks forever takes its lane's ordering guarantee down with it. Options, in increasing cost:

1. **Per-subscriber lane sets**: each subscriber gets its own striped lane array. Ordering is then per (key, subscriber), which is usually what was actually required; slow subscribers are fully isolated; the cost is threads × subscribers.
2. **Bounded per-subscriber concurrency**: a semaphore per subscriber so no subscriber can occupy more than k delivery threads. That is a **bulkhead**, exactly as in the bulkhead problem, applied inside the bus.
3. **Timeout and eject**: wrap handler invocation in a timeout, and after repeated violations disable the subscriber and log loudly. Note honestly that you cannot kill a stuck handler thread; a timeout lets you *stop waiting*, it does not free the thread. Socket/IO timeouts inside the handler are the only real fix.

And the rule that outranks all three: **never call alien code holding your lock.** Handler invocation happens outside any bus lock. The subscriber list is copied out (or is a `CopyOnWriteArrayList`) and iterated with no lock held. A handler that publishes back to the bus while you hold the registry lock is a re-entrancy deadlock waiting for its first production incident.

### Delivery semantics

- **At-most-once**: run the handler, catch `Throwable`, log, move on. The lane never stalls. Events are lost on handler failure. Perfectly reasonable for notifications and metrics.
- **At-least-once**: retry until success or a bounded attempt/deadline budget, then dead-letter. The ordering interaction is the interesting part: **an in-lane retry blocks that key's lane** (which is correct, you cannot process e2 before e1 if order matters) and also blocks every co-striped key (which is collateral damage). Retrying with backoff makes the collateral damage worse the longer it goes; the usual resolution is a small bounded retry in-lane, then dead-letter and move on, keeping the lane alive. Retry policy details are the retry-with-backoff-and-jitter problem. Reuse it by reference, including its idempotency prerequisite: at-least-once delivery only makes sense if handlers are idempotent.
- Duplicates arise from retries and from any re-delivery on restart. Say "handlers must be idempotent" as a *requirement you are imposing on subscribers*, not as a hope.

### Lifecycle (the four questions, briefly)

**Completion**: usually N/A, a bus runs forever. If a test needs "all published events drained", that's the pending-counter discipline again: increment on accept, decrement in the handler's `finally`, and the last decrementer signals a quiescence latch. **Backpressure**: covered above. **Cancellation**: an in-flight handler can be interrupted; a queued event can be dropped. **Shutdown**: flag-only shutdown fails. Lane workers are parked inside `take()` and will never re-read your flag. Poke them: one poison pill per lane (FIFO means real events drain first, which is graceful shutdown falling out for free) or interrupt each worker. Reject publishes after shutdown under the same coordination as the enqueue, or an event slips in during the transition.

### Production equivalents

Kafka gives per-partition ordering and a consumer group gives one consumer per partition. The striped-lane design is exactly that model, in-process, with hash-partitioning instead of a broker. Akka/Pekko actors give per-actor mailbox serialisation, which *is* per-key ordering with the key being the actor identity. Vert.x event bus, Disruptor (single-writer, extreme throughput), Guava `EventBus` (no ordering guarantees, know that before someone suggests it). Spring's `ApplicationEventPublisher` is synchronous by default and therefore trivially ordered and trivially non-parallel. In a design round: **"per-key ordering is what partitions are for. I'd partition by key and keep one consumer per partition."** Hand-build the striped bus only when implementation is the question.

### Pitfalls

1. **Shared pool, shared queue**: throughput fine, ordering silently gone. Reproduces only when two same-key events are in flight at once, i.e. under load, i.e. in production.
2. **Thread per key**: correct and beautiful up to a few hundred keys; at high cardinality it is an OOM and a scheduler meltdown. Have the number ready: a platform thread costs about a megabyte of stack.
3. **Unbounded lane queues**: deferred OOM; and with per-key backlogs, every key accumulates at once when downstream stalls.
4. **Non-atomic tail append in the chained design**: read tail, append, store: two publishers on one key both extend from the same tail, one link is lost and so is the ordering. Must be one `compute`.
5. **Chained-future map entries never removed**: a slow memory leak proportional to distinct keys ever seen.
6. **Handler invoked while holding the bus's lock**: alien call under lock; a handler that republishes deadlocks or re-enters.
7. **Handler exception kills the lane worker**: silent worker death; that lane's keys stop forever and nothing logs it. Catch `Throwable` per event.
8. **Rehashing lanes at runtime** (resizing the lane array to relieve skew) reassigns keys mid-flight and breaks ordering across the resize. If you must resize, drain first. Say the constraint rather than discovering it.

### Check your understanding

1. Give the exact two-event interleaving where a fixed thread pool with one shared queue reorders two same-key events, and explain why adding more workers makes it more likely rather than less.
2. State the ordering correctness argument for striped lanes in one sentence. Which of its three clauses breaks if you let a lane have two worker threads?
3. Your parallelism is capped by the number of active keys, not the number of threads. Explain why, and what you would tell a team whose traffic is 80% one key.
4. In the chained-future design, why must the tail append be a single atomic map operation? Trace two concurrent publishes on one key through the non-atomic version.
5. At-least-once delivery with in-lane retry: what does it do to the key's ordering (fine), to co-striped keys (not fine), and what requirement does it impose on subscribers?

### Transfers to

Kafka/Kinesis consumer design, actor systems, per-account ledger and payment processing, CDC pipelines, per-connection protocol handlers, and any "these must stay in order, those may not" requirement. The striping mechanic itself, map a key deterministically onto one of N serial resources, is the same tool as lock striping and per-partition state, and it transfers anywhere the constraint is "serial within a group, parallel across groups."

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/task-lifecycle/event-bus-with-per-key-ordering).
