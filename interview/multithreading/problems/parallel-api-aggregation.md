---
layout: post
title: Parallel API Aggregation (Scatter-Gather with CompletableFuture)
date: 2026-07-19
description: >-
  The most common async question in Java interviews, and the place where "launch multiple jobs, wait for results" gets tested with production concerns attached: timeout…
categories: interview multithreading problems
---

Part of the [Task Lifecycle](/interview/multithreading/patterns/task-lifecycle/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Very common senior Java rounds ("call N services in parallel"); the practical stand-in for "does Java have async/await?"

### Problem

Build a product-page aggregator: given a productId, call three slow services, `priceService`, `reviewService`, `inventoryService`, IN PARALLEL, combine their results into one response. Requirements:

- Total latency ≈ slowest call, not the sum.
- Per-call timeout (e.g., 300ms); a timed-out or failed non-critical call degrades gracefully (default value), a critical one fails the whole request.
- The caller's thread must not block while calls are in flight (async endpoint).

### Constraints

- Use `CompletableFuture` (this IS Java's async/await, say so).
- Know which thread executes each stage, interviewers probe this specifically.
- No raw Thread creation; a bounded executor or virtual threads.

### Clarify before solving

- Which calls are critical vs degradable? (Drives exceptionally vs fail-fast.)
- Combine needs ALL results (allOf) or first-wins (anyOf, e.g., mirrored replicas)?
- Total-budget timeout in addition to per-call?
- Java 21 available? (Virtual threads + structured concurrency make the blocking style viable again, the modern alternative answer.)

### Why this problem matters

The most common async question in Java interviews, and the place where "launch multiple jobs, wait for results" gets tested with production concerns attached: timeout, fallback, thread control, exception semantics. It also forces the concept most candidates fumble: **async ≠ parallel**, async is about not blocking a thread while waiting; parallel is about doing work simultaneously. This problem is both, and you should say which requirement demands which.

---

## Strategy

### Classify

Type F, fixed fan-out/fan-in (no self-generating work, termination is trivial: join N known futures). The new content is **composition**: expressing the waiting, combining, timing-out, and falling-back declaratively instead of parking threads.

### Invariant

Response is built from exactly one outcome per service (result, fallback, or failure-propagation per that service's policy); total latency bounded by max(call latencies) + combine, never the sum; no stage runs on a thread you didn't intend.

### Mental model

Three waiters take three orders to the kitchen simultaneously; the expediter assembles the plate when all three return. Async means the MAÎTRE D' doesn't stand at the kitchen door while waiting, he seats other guests, and the plate-assembly is a callback triggered by the last dish. CompletableFuture is the order ticket: a placeholder for a result that doesn't exist yet, onto which you pin "what to do when it arrives."

### The composition vocabulary (Java's async/await)

The whole skill is ~8 operations; know what each does and WHERE it runs:

- `supplyAsync(supplier, executor)`, launch. ALWAYS pass an explicit executor (default ForkJoinPool.commonPool is shared JVM-wide, a noisy neighbor and sized for CPU work, not blocking I/O calls).
- `thenApply(f)`, map the result. `thenCompose(f)`, flatMap when f itself returns a future (chaining dependent async calls; using thenApply there nests futures, the classic confusion, know the difference cold).
- `thenCombine(other, f)`, merge two independent futures.
- `allOf(f1..fn)`, wait-for-all; returns Void, so re-read each future (they're complete, `join()` is now non-blocking) in the continuation. `anyOf`, first-wins.
- `exceptionally(f)` / `handle(f)`, per-stage fallback; place it ON the individual service call to degrade just that service, not on the combined future (placement = policy).
- `orTimeout(t, unit)` / `completeOnTimeout(default, t, unit)`, per-call timeout, the second IS timeout+fallback in one.
- Threading rule: non-`*Async` continuations run on whichever thread completed the previous stage (often the I/O thread, keep them tiny); `*Async` variants hop to the executor you name. Never run heavy or blocking work in a non-async continuation.

### The design (narrate in this order)

1. Launch all three with supplyAsync on a bounded executor, scatter.
2. Per-call policy: inventory (degradable) → `completeOnTimeout(defaultInventory, 300ms)` + `exceptionally(t -> defaultInventory)`; price (critical) → `orTimeout` and let failure propagate.
3. Gather: `allOf(p, r, i).thenApply(v -> assemble(p.join(), r.join(), i.join()))`.
4. Return the combined future, the endpoint thread was never blocked (that's the async part; the scatter was the parallel part, say both).
5. Lifecycle unchanged from Type F: who owns the executor, bounded queue, shutdown.

### The modern counter-answer (Java 21): mention it unprompted

Virtual threads + `StructuredTaskScope`: write plain BLOCKING code (three forks, `scope.join()`, read results), and blocking is cheap because virtual threads unmount while waiting. Failure/cancellation propagation comes built in (ShutdownOnFailure cancels siblings when a critical call fails, with CompletableFuture you must wire that yourself). Trade-off sentence: "CompletableFuture = explicit dataflow, no thread held during waits, but callback-shaped; structured concurrency = sequential-looking code, needs 21+. I'd pick per team/runtime." Knowing BOTH and choosing is the senior answer.

### Parallelism concepts to name here (interviewers attach them to this question)

- **Async ≠ parallel**: async frees the waiting thread; parallel uses many workers. I/O-bound scatter = both; a single slow call wrapped in a future = async only.
- **Task vs data parallelism**: this problem is task parallelism (different jobs). Data parallelism (same op over a big collection) → parallel streams / ForkJoinPool: fine for CPU-bound, side-effect-free ops on large data; wrong for I/O (commonPool starvation). ForkJoinPool = work-stealing (idle workers steal from others' deques; a joining worker runs subtasks itself, why fork/join doesn't self-starve like naive future-trees).
- **Amdahl's law**: speedup capped by the serial fraction, 10% serial caps you at 10× regardless of cores. One sentence, big senior signal when discussing "why not just add threads".

### Pitfalls

1. Blocking `.get()` right after launching each future in sequence, you've serialized the calls; latency = sum. Launch ALL, then compose.
2. Default commonPool for blocking I/O calls, starves every parallel stream and fork/join task in the JVM.
3. Heavy work in non-async continuations, runs on the completing (I/O) thread.
4. `exceptionally` on the combined future when the requirement was per-service degradation, one failed review kills the page.
5. `allOf` + forgetting it returns Void, re-read the member futures.
6. thenApply where thenCompose is needed, `CompletableFuture<CompletableFuture<X>>`.
7. No timeout anywhere, one hung service hangs every request thread's future forever.

### Check your understanding

1. Why does launching-then-getting in a loop serialize? Where exactly must all launches happen relative to the first join?
2. thenApply vs thenCompose, construct the type signatures that force the difference.
3. A non-async thenApply after an HTTP-client future: which thread runs it, and why does that matter?
4. How does StructuredTaskScope.ShutdownOnFailure change the failure story vs hand-wired allOf?
5. When would parallel streams be the WRONG tool even with 8 idle cores? (I/O-bound tasks; small data; shared-mutable-state lambdas.)

### Transfers to

Any "call N things, combine" (API gateways, batch enrichment, replica hedging via anyOf), retry/hedged-request patterns, and the executor-lifecycle discussion of the web crawler. Completes Type F: crawler = self-generating work, thread pool = the machinery, this = fixed fan-out with composition.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/task-lifecycle/parallel-api-aggregation).
