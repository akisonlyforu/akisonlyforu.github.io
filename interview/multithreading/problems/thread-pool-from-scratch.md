---
layout: post
title: Thread Pool from Scratch
date: 2026-07-19
description: >-
  Best single exercise for understanding what ExecutorService IS: your blocking queue plus N identical worker loops. After building it, executor questions (sizing, rejection…
categories: interview multithreading problems
---

Part of the [Bounded Resource](/interview/multithreading/patterns/bounded-resource/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Educative / banks. Plan marks this a depth exercise, one session max.

### Problem

Implement a tiny fixed-size thread pool: `MyPool(int nThreads)`, `submit(Runnable task)`, `shutdown()`. Submitted tasks execute on the pool's worker threads, not the caller's.

### Constraints

- Fixed N workers created at construction; workers are reused across tasks.
- `submit` after `shutdown` is rejected.
- `shutdown()` lets queued tasks finish (graceful), then workers exit. Don't build shutdownNow unless asked.

### Clarify before solving

- Bounded or unbounded task queue? (Bounded → submit can block/reject: decide and say which.)
- What happens when a task throws? (Worker must survive, catch per task.)
- Graceful vs immediate shutdown? (Graceful here.)

### Why this problem matters

Best single exercise for understanding what `ExecutorService` IS: your blocking queue plus N identical worker loops. After building it, executor questions (sizing, rejection, shutdown semantics) stop being trivia, you've held each mechanism in your hands. Deliberately a depth exercise: in a design interview the answer is "use ExecutorService", and you should say so.

---

## Strategy

### Classify

Producer-consumer where YOU built both sides: callers produce tasks, workers consume them. The pool = one blocking queue + N identical consumer loops + lifecycle state.

### Invariant

Every accepted task runs at most once on some worker; after shutdown no new tasks are accepted; workers exit only when the queue has drained (graceful).

### Mental model

A restaurant kitchen: submit = tickets on the rail (your bounded blocking queue), workers = N cooks in an endless loop of "take next ticket, cook it". Reuse falls out naturally, a cook finishing a dish just takes the next ticket; no hiring/firing per dish (that's the whole reason pools exist, thread creation is expensive, conceptual #10).

### The three parts

1. **Worker loop**: `while (running or queue non-empty) { task = queue.dequeue(); try { task.run(); } catch (Throwable t) { log; } }`. The per-task catch is load-bearing: an uncaught exception kills the worker thread and your pool silently shrinks, a real production failure mode worth mentioning by name.
2. **submit**: check shutdown flag, enqueue (blocks if bounded queue full, that's backpressure, name it).
3. **shutdown**: flip the flag (guarded/volatile), then unblock any workers parked on an empty queue. Cleanest trick: enqueue N sentinel "poison pill" tasks, each worker that dequeues one exits. Poison pills convert "wake a parked consumer to tell it to die" into ordinary queue traffic, no special signaling path needed. Alternative: interrupt the workers and treat InterruptedException in dequeue as an exit signal; more idiomatic Java, slightly more care.

### Design decisions to narrate

- **Bounded queue** → submit blocks when full: backpressure protects memory; unbounded hides overload until OOM. (This is why `Executors.newFixedThreadPool`'s unbounded queue gets criticized, nice depth remark.)
- **Shutdown state must be checked under the same coordination as enqueue**, or a task can slip in after shutdown began.
- **Task failure isolation**: one bad task must not affect the next.

### Pitfalls

1. No per-task catch → dying workers, shrinking pool, eventual zero throughput. The #1 bug.
2. Shutdown that just sets a flag → workers parked on empty queue sleep forever (they're inside dequeue's wait, the flag alone wakes nobody). You must poke them: pills or interrupts.
3. Poison pill count ≠ worker count → some workers never exit, or a pill is consumed as a "task" by an already-exiting path.
4. Busy-wait worker loop polling the queue, you rebuilt the problem your blocking queue solved.

### Check your understanding

1. Why does a worker survive a throwing task in your design? Point to the exact mechanism.
2. Walk shutdown end-to-end with 2 workers: one mid-task, one parked on empty queue. How does each learn to exit?
3. Why exactly N pills for N workers? What if a pill is enqueued while the queue still has real tasks? (Fine, FIFO means real tasks drain first: that's WHY graceful shutdown works.)
4. Map each piece to ThreadPoolExecutor: your queue = workQueue; your N = corePoolSize; your reject-after-shutdown = RejectedExecutionHandler; pills/interrupts = shutdown vs shutdownNow.

### Transfers to

All executor conceptual questions (#10, #11, #26), web crawler (uses a pool), and "design a job processor" LLD questions.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/bounded-resource/thread-pool-from-scratch).
