---
layout: post
title: Lock-Free Queue / Bounded Ring Buffer
date: 2026-07-19
description: >-
  A queue is the first structure in this family whose two ends are genuinely *separate* pieces of state. That single structural difference from a stack, one hook versus two…
categories: interview multithreading problems
---

Part of the [Concurrent Data Structures](/interview/multithreading/patterns/concurrent-data-structures/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Appears in two guises, "implement a non-blocking queue" in low-level rounds, and "design the hand-off between our ingest threads and our processing threads" in system-design rounds. The ring-buffer/Disruptor variant shows up wherever latency is the product (trading, streaming, telemetry pipelines). Frequency claim is directional.

### Problem

Design a queue that lets producers and consumers proceed without a shared global lock. Cover both branches and know when each is right:

- **Unbounded linked queue**: the Michael–Scott design, at concept level: two atomic ends, why enqueue is a *two-step* update, and what arriving threads must do when they observe a half-finished one.
- **Bounded ring buffer**: a fixed power-of-two array with head and tail sequence counters, Disruptor-style: how fullness and emptiness are derived from the counters, and why the single-producer/single-consumer case needs no atomic read-modify-write at all.

Then explain why this structure's performance is decided by cache lines rather than by lock semantics.

### Constraints

- Multiple producers and multiple consumers by default; be ready to specialise to single-producer and/or single-consumer and say what that buys.
- Bounded variant: capacity is fixed; the policy on a full queue must be stated (block, spin, drop, reject) rather than assumed.
- Unbounded variant: no capacity, and therefore no backpressure, this is a liability, not a feature, and must be named.
- Correct under arbitrary preemption; a suspended producer must not stall consumers.

### Clarify before solving

- **Bounded or unbounded?** The most consequential question in the problem, because it is really a question about backpressure. An unbounded queue converts overload into an out-of-memory error at a time of its choosing.
- **How many producers and how many consumers?** Single-producer/single-consumer permits a dramatically simpler and faster design. Ask before assuming the general case.
- **What is the policy when the queue is full / empty?** Block, spin, time out, drop-oldest, drop-newest, reject the caller. This is the policy axis from the bounded-resource family and it is orthogonal to the data structure.
- **Is strict FIFO required across all producers, or only per-producer ordering?** A global total order is a global invariant; per-producer order plus a sequence number is much cheaper.
- **Is this actually latency-critical, or would `ArrayBlockingQueue` / `LinkedBlockingQueue` do?** Ask it plainly. The answer usually is that the JDK queue would do, and knowing that is part of the answer.
- **Do consumers need to see every element, or is a slow consumer allowed to be dropped/lapped?** Decides whether the consumer sequence is authoritative for producer flow control.

### Why this problem matters

A queue is the first structure in this family whose two ends are genuinely *separate* pieces of state. That single structural difference from a stack, one hook versus two, is what allows a producer and a consumer to operate simultaneously without ever touching the same variable, and it is the reason queues, not stacks, are the workhorse of concurrent systems. Being able to say *why* a queue splits where a stack cannot is the conceptual core of the question.

It is also the problem where the machine finally becomes visible. Two counters that are logically independent will destroy each other's throughput if they land on the same cache line, and no amount of memory-model reasoning predicts that, the JMM has nothing to say about it. A candidate who designs a beautiful two-ended queue and never mentions padding has optimized the part that wasn't the bottleneck. This is the one problem in the bank where "know your hardware" is a first-class requirement rather than a flourish.

Finally, the enqueue-in-two-steps problem in the linked design introduces *helping*, a thread that finds the structure mid-update completes someone else's operation rather than waiting for them. That idea, that a non-blocking structure has no one to wait for and so must be repairable by whoever arrives next, is the deepest thing lock-free programming has to teach.

---

## Strategy

### Classify

Concurrent data structure with **two independent access points**. That is the whole difference from the Treiber stack, and everything downstream, the concurrency you gain, the two-step update you must handle, the false sharing you must avoid, follows from it.

### Invariant

- Elements are removed in the order they were inserted (FIFO by linearization order).
- Every enqueued element is dequeued exactly once: never lost, never duplicated, never returned before it was fully written.
- The structure is always traversable: at no observable instant is the chain broken or the array slot ambiguous.
- Bounded variant additionally: the number of live elements never exceeds capacity, and a producer never overwrites a slot a consumer has not yet read.

The last clause is the interesting one for the ring buffer, because it is expressed entirely as **arithmetic on two counters** rather than as structural state.

### Mental model

Two people working a conveyor belt from opposite ends. The loader at one end and the picker at the other never touch the same item, until the belt is empty or full, at which point they suddenly do, and must coordinate. Everything good about queues comes from that separation; every hard case in the design is one of the two boundary conditions where the ends meet.

Contrast the stack: one hook, both operations, permanent collision. A stack cannot split; a queue splits for free.

### Branch A: the unbounded linked queue (Michael–Scott), at concept level

State: a head pointer and a tail pointer, plus a permanent **dummy node** at the front. The dummy is not decoration, it means the queue is never structurally empty, so head and tail never need to be null and the empty case doesn't require special-casing the pointer updates. That's the trick worth remembering.

**Dequeue** is a single CAS: advance head past the dummy, and the node it pointed to becomes the new dummy. Straightforward, mirrors the stack pop.

**Enqueue is two steps and this is the crux.** To append you must (1) link the new node onto the current last node's `next`, and (2) swing the tail pointer to it. There is no single atomic instruction that does both. So there is an observable intermediate state: the node is linked but the tail pointer is stale, pointing one behind.

You cannot fix this by holding a lock (you have none) and you cannot ask the enqueuer to hurry (it may have been preempted between the two steps, indefinitely). The only available answer is **helping**: any thread that arrives and observes tail's `next` as non-null knows the tail is lagging, and *before doing its own work, it CASes the tail forward on the stalled thread's behalf.* Then it retries its own operation.

Extract the principle, because it transfers: **a non-blocking structure has nobody to wait for, so every observable intermediate state must be repairable by whoever arrives next.** A lock-based design gets to have unobservable intermediate states; a lock-free one must publish only states that are either final or fixable.

Consequences to name:
- The structure is genuinely unbounded, so it offers **no backpressure**. Under sustained overload, memory grows until the process dies. This is `ConcurrentLinkedQueue`'s real liability and it should be stated whenever the structure is proposed.
- `size()` requires a traversal and is an estimate, O(n) and stale. Never poll it in a loop.
- One allocation per element, plus GC pressure, plus pointer-chasing that defeats hardware prefetching. This is why the array-based design exists.

### Branch B: the bounded ring buffer with sequence counters

State: a fixed array whose length is a power of two, plus two monotonically increasing counters, one for the producer, one for the consumer. They **never wrap and never decrease**; the array index is derived by masking the low bits (`sequence & (capacity - 1)`), which is why the power-of-two requirement exists and why it's a bitmask rather than a modulo.

Everything derives from the counters:
- **Element count** = producerSequence − consumerSequence.
- **Full** = that difference has reached capacity.
- **Empty** = the two are equal.

Two things this buys that the linked design cannot:
- **No allocation per element and contiguous memory.** Consumers walk the array linearly, which the hardware prefetcher loves. This is most of the latency advantage in practice.
- **Bounded by construction**, so the full condition is a real backpressure signal rather than a memory leak. What to *do* on full is the policy axis from the bounded-resource family, block on a condition, spin/park with a wait strategy, time out, drop-oldest, or reject the caller, and it is a one-line policy knob, not a different design. Always ask which one; never silently pick.

**The single-producer / single-consumer specialisation is the payoff.** If exactly one thread writes the producer counter and exactly one writes the consumer counter, then each counter has **exactly one writer**, and a variable with one writer needs no compare-and-set at all. A plain volatile write suffices, and volatile writes are dramatically cheaper than CAS. The protocol becomes:

- Producer: check fullness by reading the consumer counter, write the element into the slot, then **publish by writing the producer counter**. That volatile write is the release; a consumer's volatile read of the same counter is the acquire, and the happens-before edge carries the element write. Ordering is everything: element first, counter second. Reverse it and the consumer reads a slot that hasn't been filled, the same publication bug as double-checked locking without volatile, in a different costume.
- Consumer: read the producer counter; if it's ahead, read the slot, then advance its own counter to release the space.

For **multiple** producers you reintroduce contention on the producer counter: producers CAS to claim a sequence number, write into their claimed slot, and then need a way to publish *out of order completion*, either an available-sequence array or a second cursor that only advances past contiguous completed slots. Say this exists and is the hard part; the single-producer case is where the design shines, and a common production answer is to funnel many threads into one producer rather than to make the producer multi-writer.

### False sharing: the part that decides the outcome

The two counters are logically independent, which is the entire point of the design. But a cache line is typically 64 bytes, and two 8-byte counters declared next to each other land on the same line. The coherence protocol works at line granularity, so:

- The producer writes its counter → the line is invalidated in the consumer's core cache.
- The consumer reads its own counter → cache miss, must re-fetch the line the producer just dirtied.
- Repeat, millions of times per second.

The variables never conflict logically and the machine treats them as if they do. This is **false sharing**, and it can cost an order of magnitude of throughput. It is invisible to the memory model, the code is *correct*, so nothing in a correctness review will catch it. Cures:

- **Padding**: surround each hot counter with enough filler that it occupies a cache line alone. Historically done with dummy fields; note that naive padding fields can be eliminated by the JIT, which is why hand-padding is fiddly.
- **`@Contended`** (JDK annotation, requires the corresponding JVM flag to be enabled): asks the JVM to isolate the field on its own cache line. The supported mechanism, and the one to name.

Say the general rule, because it applies far beyond queues: **any two independently and frequently written variables must be on different cache lines.** It's why striped counters pad their cells, and it's the reason a beautifully sharded design can perform no better than a global lock.

Two related machine-level notes worth one sentence each: consumers should **batch**, read the producer counter once and drain everything available, amortizing the volatile read over many elements; and a spinning consumer should use a **wait strategy** appropriate to the deployment (busy-spin for lowest latency at the cost of a burned core, yield/park for a shared machine). Busy-spinning by default on a multi-tenant box is a real production mistake.

### Choosing between them, and against them

- **Just use `ArrayBlockingQueue`** when you need a bounded handoff with blocking semantics and your throughput is anywhere near normal. It's one lock plus two conditions, the bounded-resource pattern, shipped, and it is correct, obvious, and fast enough for the overwhelming majority of systems.
- **`LinkedBlockingQueue`** when producers and consumers must not contend: it uses separate head and tail locks, which is the *lock-based* expression of the same two-ends insight this whole problem rests on. Worth naming, because it shows the insight is about the structure, not about being lock-free.
- **`ConcurrentLinkedQueue`** when you need non-blocking and can genuinely tolerate unboundedness. Rare; usually the unboundedness is a bug waiting for a traffic spike.
- **Hand-rolled ring buffer / Disruptor** when latency percentiles are the product and you have measured the JDK queue as the bottleneck. Otherwise it is the family's most seductive over-engineering.

### Pitfalls

1. Unbounded queue chosen by default, so overload becomes an out-of-memory error instead of backpressure. The single most common real-world version of this mistake.
2. Publishing the sequence counter before writing the element. Consumer reads a slot that isn't populated.
3. Non-power-of-two capacity with a modulo, then wondering where the throughput went, and, worse, hand-rolled wrap arithmetic that breaks on counter overflow. Monotonic counters plus masking sidesteps both.
4. Counters adjacent in memory. False sharing; correct and slow.
5. Assuming the single-producer optimization while more than one thread can call `enqueue`. Two writers to a non-atomic counter lose updates and overwrite slots. This assumption must be *enforced*, not documented.
6. In the linked queue: not helping the tail forward, and instead spinning until the other thread finishes. That is blocking behaviour in a non-blocking structure, and it deadlocks in effect if the other thread is descheduled.
7. Polling `size()` on a linked concurrent queue in a hot loop, O(n) traversal, and the answer is stale anyway.
8. Busy-spinning consumers on a shared or oversubscribed machine.
9. Treating "lock-free" as "fast." A single-lock `ArrayBlockingQueue` frequently beats a naive multi-producer lock-free queue under real contention.
10. Forgetting that the queue is a handoff and inherits the bounded-resource family's policy questions (what on full, what on empty, how do we shut down, how do consumers learn there's no more work).

### Check your understanding

1. Why can a queue let a producer and consumer run without contending, when a stack cannot? Answer in terms of the state each operation touches.
2. In the linked design, why is enqueue two steps, and why can't you make it one? What must a thread that observes the intermediate state do, and why is *waiting* not an option?
3. What is the dummy node for? Describe what breaks without it.
4. In the ring buffer, derive full and empty from the two counters, and explain why the counters are monotonic rather than wrapped.
5. Single-producer/single-consumer: why is a plain volatile write enough where the general case needs a CAS? Name the happens-before edge that publishes the element, and the ordering rule the producer must obey.
6. Two counters, both correct, throughput terrible. Diagnose it, and give two fixes.
7. Your ring buffer is full. Enumerate the five policies and name a system for which each is the right one.
8. When would you pick `ArrayBlockingQueue` over everything in this document? Answer without saying "simplicity" alone.

### Transfers to

The bounded-resource family in its entirety, this is that pattern's data structure, and the full/empty policy axis is imported wholesale; `lock-free-stack-treiber` (same CAS discipline, one end instead of two); `striped-counter-longadder` (the same false-sharing analysis, applied to counters instead of cursors); thread-pool internals (a pool is a bounded queue plus worker loops, so every question here is a question about your executor); and any log/telemetry/event-sourcing pipeline design, where "bounded with an explicit drop policy" versus "unbounded and hope" is the same decision at system scale.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/concurrent-data-structures/lock-free-or-bounded-queue).
