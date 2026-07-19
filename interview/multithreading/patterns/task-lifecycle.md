---
layout: post
title: Task Lifecycle, Async & Parallelism Playbook
date: 2026-07-19
description: >-
  The four lifecycle questions, termination detection for self-generating work, atomic claiming, CompletableFuture composition, fork-join and Amdahl's law.
categories: interview multithreading patterns
---

Deep dive on the task-lifecycle family, companion to [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework/). Every other family asks who may proceed; this one asks where the work is and who is counting it. It also absorbs async and parallelism, which are lifecycle problems with composition sugar rather than a separate discipline.

Every other category in this bank asks "who may proceed, and who tells them?" Type F asks a different question: **what is the life story of a unit of work?** It gets born (submitted), it may be refused (rejection), it runs (on whose thread?), it may spawn children (self-generating work), it may be killed early (cancellation), and eventually the whole system must be able to say "we are finished" (completion) or "we are closing" (shutdown). Type F problems are rarely hard because of mutual exclusion — they are hard because of **accounting**: knowing at every moment how much work exists, including work you cannot see because it is in someone's hands rather than in the queue.

Problems in this family: multithreaded web crawler (LC 1242), thread pool from scratch, fan-out/fan-in service aggregation, parallel file-tree walker, "design a job processor" LLD. The crawler is the canonical hard case because workers **generate** work; the thread pool is the canonical infrastructure case because you own the lifecycle machinery itself.

---

## 1. The four lifecycle questions

Any Type F design is incomplete until you have answered all four, out loud, before coding. Interviewers probe whichever one you skip.

### Q1 — Completion: how do we KNOW we're done?

The deceptively hard one. "The queue is empty" is not an answer, because a task currently executing may be about to enqueue more work. The correct mental model: work exists in **two places** — the queue (visible) and workers' hands (invisible). Done means *both* are empty, and no mechanism exists that could refill them. Every sound completion scheme is some way of making the invisible part visible: a counter, a tree of futures, or a level boundary (Section 2).

For non-self-generating work (fixed set of N tasks known upfront), completion is easy: `CountDownLatch(N)`, or `invokeAll`, or collect N futures and join them. State which case you're in — it changes the whole design.

### Q2 — Rejection / backpressure: what happens when work arrives faster than it's processed?

An unbounded queue never says no — it just grows until OOM. That is not robustness; it is deferred failure. A **bounded** queue forces a policy at the moment of overload, and you must pick one:

- **Block the submitter** (backpressure proper) — the producer slows to the consumer's pace. Default for internal pipelines.
- **Reject** (throw / return false) — caller decides. Default for request-serving systems that must not stall.
- **Drop oldest / drop newest / caller-runs** — the `RejectedExecutionHandler` menu; caller-runs is a sneaky self-throttle (the submitting thread is busy running the task, so it can't submit more).

The interview line: "bounded queue plus an explicit rejection policy — unbounded queues hide overload until the process dies." (This is the standard criticism of `Executors.newFixedThreadPool`'s unbounded queue.)

### Q3 — Cancellation / interruption: how does in-flight work stop early?

Java has no safe way to kill a thread; cancellation is **cooperative**. The mechanism is interruption, and the rules are few (full rules in Section 6):

- Cancelling a task that is **queued, not started**: just don't run it (check a cancelled flag before executing, or remove from queue).
- Cancelling a task that is **running**: interrupt its thread; the task must be written to notice (blocking calls throw `InterruptedException`; CPU loops must poll `Thread.currentThread().isInterrupted()`).
- A task that swallows `InterruptedException` silently is uncancellable — that is a bug, not a feature.

State the cancellation policy in your invariant: "queued tasks are discarded; the running task is interrupted and abandoned" (or "allowed to finish" — either is fine, but pick one).

### Q4 — Shutdown: graceful vs immediate?

Two distinct semantics, and every design must implement (or at least name) both:

- **Graceful** (`shutdown()`): stop *accepting*, finish everything already accepted, then let workers exit. Mechanically: flip an accepting flag, then arrange for workers parked on an empty queue to wake and exit — poison pills (one per worker; FIFO guarantees real work drains first) or interrupts-as-exit-signal.
- **Immediate** (`shutdownNow()`): stop accepting, *discard* the queue, interrupt running workers, return the drained tasks so the caller knows what never ran.

The classic bug: shutdown that only sets a flag. A worker parked inside a blocking `take()` never re-reads your flag — it is asleep inside the queue's wait set. **Flags don't wake sleepers; you must poke them** (pill or interrupt). Also: the accepting-flag check in `submit` must happen under the same coordination as the enqueue, or a task slips in after shutdown began.

---

## 2. Termination detection for self-generating work (the heart)

When workers produce work as well as consume it (crawler, tree walker, dependency resolver), completion detection is the entire problem. First, internalize the failure:

> Worker A dequeues the last URL; the queue is now empty. Worker B checks the queue, sees empty, declares done. Meanwhile A's fetch returns 50 links and enqueues them. B terminated the crawl with 50 URLs unfetched. **Empty queue ≠ no work, because in-flight work is invisible to the queue.**

Three sound designs, in order of interview practicality.

### 2a. The pending-counter discipline (default answer)

Maintain one atomic counter `pending` = number of tasks that have been *committed to* but not yet *fully finished*. The whole correctness is three mechanical rules — say them verbatim:

1. **Increment BEFORE submit.** The counter must rise before the task becomes runnable. If you increment inside the task, there is a window where the task is submitted, `pending` is still 0, and an observer wrongly concludes "done".
2. **Decrement AFTER complete** — in a `finally`, after all of the task's effects (including enqueuing children) are visible.
3. **Children are counted before the parent decrements.** A parent that discovers children does `pending.increment(); submit(child)` for each child *before* its own decrement runs. Combined with rule 2's ordering, `pending` can never touch 0 while undiscovered work exists: at the moment a parent decrements, everything it spawned is already counted.

**Zero means done.** By induction: `pending > 0` whenever any committed-but-unfinished task exists; a task only finishes after registering all work it created; therefore the first time `pending` hits 0, no task is queued, none is running, and none can appear. The decrement that takes the counter to 0 is unique (atomic `decrementAndGet() == 0`), so exactly one thread observes it — that thread signals the waiter (release a semaphore, count down a latch, `complete()` a future). Never poll the counter from the main thread; the last decrementer *pushes* the done signal.

```
pending = AtomicInteger(0)
done    = one-shot signal (latch/semaphore/future)

spawn(task):                      # the ONLY way work enters the system
    pending.increment()           # rule 1: count first
    executor.submit(wrap(task))

wrap(task):
    try:
        results = task.run()          # may call spawn(child) — rule 3:
                                      # each child counted inside spawn,
                                      # before we reach the finally below
    catch (Throwable t): record(t)    # worker survival — Section 4
    finally:
        if pending.decrement() == 0:  # rule 2: last effect of the task
            done.signal()             # unique last decrementer pushes

main:
    spawn(rootTask)
    done.await()
    executor.shutdown()
```

Trade-offs: no waiting inside workers (no starvation hazard), works with any fixed pool, O(1) space. Costs: the discipline is easy to state and easy to violate — every submission path must go through `spawn`; a single raw `executor.submit` breaks the invariant silently.

### 2b. Future-tree / structured recursion

Each task submits its children and **waits on their futures**; termination = the root future completes. The task graph *is* the accounting — no counter needed. Cleanest logic, and it composes: each subtree independently knows when it's done.

The hazard: **waiting inside pool threads**. In a fixed pool of size N, if all N workers are blocked waiting on children whose tasks sit unstarted in the queue (because no worker is free to run them), nothing ever runs: **deadlock-by-starvation**. No cycle of locks — just a pool eaten by waiters. Naming this unprompted is a strong senior signal. Mitigations: `ForkJoinPool` (work-stealing; a joining worker runs pending subtasks itself), sizing the pool to the tree depth (fragile), or virtual threads (Section 7, which dissolve the problem).

### 2c. Level-order BFS (the time-is-short answer)

The **main thread** owns the frontier. Submit all of level k (`invokeAll` or collect futures), wait for all, gather the children they returned as level k+1, repeat until a level is empty. Termination is trivial — workers never enqueue anything; only the main thread does, and it always knows when a level is drained.

Trade-offs: simplest to reason about and to test; loses parallelism at level boundaries (the whole level must finish before the slowest straggler lets level k+1 start); frontier lives in main-thread memory. A perfectly respectable interview answer — offer it explicitly if time is short, and name the straggler cost to show you chose it knowingly.

**Choosing:** counter = general-purpose default; future-tree = elegant when recursion is natural AND you can name/mitigate starvation; level-BFS = simplest, minor throughput loss. Any of the three, *chosen with its trade-off stated*, is a passing answer. Termination by sleep, by polling queue-emptiness, or by timeout is a failing answer.

## 3. Atomic work-claiming / dedup

When the same work item can be discovered by multiple workers (two pages link to the same URL), someone must win a race to own it. Two rules:

**Claim atomically.** `if (!visited.contains(u)) { visited.add(u); ... }` is check-then-act: both workers pass `contains`, both fetch. The idiom is a single operation that checks and claims in one step and *tells you whether you won*: `ConcurrentHashMap.newKeySet().add(u)` returns `false` if already present. That boolean is the **linearization point** of "this item is mine" — the one atomic instant where ownership is decided; everything else in the design is ordered around it. (Same shape: `putIfAbsent`, `compareAndSet`.)

**Claim BEFORE work, not after.** Mark visited *before* fetching. If you mark after, the window between "started fetching" and "marked visited" lets a second worker start the same fetch. Claim-before-work makes duplicates impossible rather than unlikely. The mirror-image cost is benign: if a claimed task then fails and you want a retry, you must explicitly *unclaim* or re-submit — an honest follow-up answer, not a design flaw.

Interaction with the pending counter: **claim first, count second, submit third**. Only the claim winner calls `spawn`; losers do nothing. This keeps `pending` equal to the number of *distinct* claimed-but-unfinished items.

```
for child in discovered:
    if filterAccepts(child) and visited.add(child):   # claim = linearization point
        spawn(fetchTask(child))                        # winner counts + submits
```

(Apply cheap filters — hostname, depth — before claiming: a wasted claim is harmless, a wasted fetch isn't; but a claim on a filtered-out item pollutes the set.)

## 4. Worker survival: the per-task catch

A worker thread's loop must wrap each task in `try { task.run() } catch (Throwable t) { record/log }`. This is load-bearing, not defensive boilerplate: an uncaught exception **kills the worker thread**, the pool silently shrinks, and throughput decays to zero with no error message — the #1 real-world pool bug. Corollaries:

- The pending-counter decrement goes in `finally`, or one throwing task freezes termination forever (counter never reaches 0, main thread waits eternally). Failure must still *count as completion* for accounting purposes.
- Catching is not the same as *handling*: record the failure somewhere the caller can see (a collected exception list, a failed future, a metric). "Log and continue" is a policy — state it as one.
- Catch `Throwable`, not `Exception`, in the worker loop (an `Error` kills the worker just as dead); rethrow truly fatal errors after accounting if your policy demands it.

## 5. Executor sizing

Say the heuristic, then immediately caveat it:

- **CPU-bound**: threads ≈ number of cores (more just adds context-switch overhead).
- **I/O-bound**: threads ≈ cores × (1 + wait/compute). A crawler spending 95ms waiting per 5ms computing wants ~20× cores.

Then the caveat, verbatim-worthy: "these are starting points; I'd measure under realistic load and tune — the formula assumes uniform tasks and ignores memory per thread and downstream limits (the target server may cap concurrent connections before my pool does)." Heuristic + measure is a senior answer; heuristic alone is a memorized one.

## 6. Interruption propagation rules

Interruption is Java's cancellation vocabulary. Four rules cover every interview case:

1. **Interruption is a request, not a kill.** It sets a flag; blocking methods (`take`, `sleep`, `await`, `join`) respond by throwing `InterruptedException` *and clearing the flag*.
2. **Never swallow it.** `catch (InterruptedException e) {}` erases the cancellation request — upstream code can no longer see it. Either **propagate** (declare `throws` and let it fly) or **restore** (`Thread.currentThread().interrupt()` in the catch) before continuing to cleanup/exit. Restore-then-exit is the worker-loop idiom.
3. **CPU-bound loops must poll.** No blocking call = no exception = interruption invisible unless you check `isInterrupted()` at loop granularity.
4. **Only the owner interprets.** Inside pool infrastructure, interrupt means "worker, wind down"; inside a task, it means "task, cancel". A task should not assume the whole pool is dying, and pool code should re-check its own state (shutdown flag) rather than guessing why it was interrupted.

## 7. Virtual threads and the wait-inside-worker hazard

The future-tree starvation hazard (2b) exists because platform threads are scarce: a blocked worker wastes a whole scarce slot. **Virtual threads make the slot cheap**: a blocked virtual thread unmounts from its carrier, which goes on to run other virtual threads. Thread-per-task (`Executors.newVirtualThreadPerTaskExecutor()`) becomes viable, and "each task waits for its children" stops being a starvation risk at all — design 2b becomes the *natural* design instead of the dangerous one. Structured concurrency (`StructuredTaskScope`) packages exactly this: fork children, join the scope, get completion + cancellation propagation + failure collection in one construct.

What virtual threads do **not** change: dedup/claiming still needed, bounded-ness still needed (a semaphore now, since "pool size" no longer caps concurrency — a million virtual threads will happily open a million sockets), interruption rules unchanged, and CPU-bound work gains nothing. One caveat worth naming: `synchronized` blocks could pin virtual threads to carriers on older JVMs (fixed in JDK 24) — prefer `ReentrantLock` around blocking sections in virtual-thread code if targeting older runtimes.

---

## 8. Skeletons

**A. Self-generating work, pending counter** — see Section 2a's `spawn`/`wrap`/`main` skeleton. Add claiming from Section 3 inside the task's child loop.

**B. Worker-pool lifecycle (own the machinery):**

```
state: queue (bounded, blocking), workers[N], accepting flag, coordination lock

submit(task):
    under same coordination as enqueue:
        if not accepting: reject          # policy: throw / false / caller-runs
        queue.put(task)                   # blocks when full = backpressure

workerLoop:
    loop:
        task = queue.take()               # parked here when idle
        if task is POISON: break
        try: task.run()
        catch (Throwable t): record(t)    # survival: pool must not shrink

shutdown (graceful):
    accepting = false                     # under the coordination
    enqueue N poison pills                # FIFO ⇒ real tasks drain first
                                          # (alt: interrupt workers; treat
                                          #  InterruptedException in take as exit)

shutdownNow (immediate):
    accepting = false
    drained = queue.drainAll()            # return these: they never ran
    interrupt all workers                 # running tasks get the request
```

**C. Fixed fan-out/fan-in (no self-generation):**

```
futures = [executor.submit(t) for t in tasks]     # fan out
for f in futures: results.add(f.get(timeout))     # fan in — MAIN thread waits,
                                                  # not a pool thread
executor.shutdown()
```

Per-future timeout + per-future try/catch = one slow or failing branch doesn't sink the aggregation. Waiting happens on the caller's thread, so the starvation hazard of 2b never arises.

## 8b. Async composition and data parallelism (the family's second half)

Three additions complete Type F beyond coordination-by-hand. Worked problems: [parallel-api-aggregation](/interview/multithreading/problems/parallel-api-aggregation/), [fork-join-parallel-computation](/interview/multithreading/problems/fork-join-parallel-computation/), [implement-a-future](/interview/multithreading/problems/implement-a-future/).

**Async ≠ parallel.** Async frees the *waiting thread* (nothing parked during I/O); parallel uses *many workers simultaneously*. Scatter-gather is both; classify which requirement demands which before designing.

**CompletableFuture is Java's async/await.** The vocabulary (know where each runs): `supplyAsync(f, executor)` — always name the executor, commonPool is JVM-shared and CPU-sized; `thenApply` (map) vs `thenCompose` (flatMap over dependent async calls); `thenCombine`; `allOf`/`anyOf` (allOf returns Void — re-read members, now-complete `join()` doesn't block); `exceptionally`/`handle` — placement = degradation policy (per-call vs whole-pipeline); `orTimeout`/`completeOnTimeout`. Non-`*Async` continuations run on the completing thread (keep tiny); `*Async` hops to your executor. Signature bug: launch-then-`get()` per call serializes the fan-out — latency sum, not max. Internally a future is: one-shot guarded state machine + terminal-state condition loop + callback list drained exactly once outside the lock (see implement-a-future — the register-or-run-now decision must be under the lock or a racing completion drops the callback).

**Data parallelism (fork/join).** Same op over partitioned data: recursive split to a measured threshold, sequential leaves, combine by RETURN VALUE (no shared accumulator — contention-free by construction). Idiom: fork right, compute left in-line, then join. Why ForkJoinPool and not a fixed executor: work-stealing deques + a joining worker *helps* by running pending subtasks — which dissolves catalog #4 for recursive decomposition. Parallel streams = fork/join with auto-splitting; refuse them for I/O lambdas, small data, side effects, non-associative reductions. Ceiling on all of it: **Amdahl's law** — speedup ≤ 1/(serial fraction).

**Structured concurrency (21+).** `StructuredTaskScope`: blocking-style fork/join on virtual threads with built-in failure propagation (ShutdownOnFailure cancels siblings). The modern counter-answer to CF pipelines; know both, choose per runtime.

### Validation of the recipe against the three new problems

- **parallel-api-aggregation** — recipe step 1: fixed set known upfront → skeleton C, completion = allOf/join; step 2's four questions become timeout/fallback/executor-ownership policy; no claiming (step 3 skipped with reason); sizing/virtual threads land at step 6. Fits — composition replaces hand-coordination, lifecycle questions unchanged.
- **fork-join-parallel-computation** — self-generating in shape (tasks spawn tasks) but future-tree termination (§2b) with the starvation hazard REMOVED by work-stealing/helping — validating §2b's mitigation list rather than adding a mechanism. No claiming (ranges partition by construction — the no-shared-state limit case of step 3). Fits.
- **implement-a-future** — building the completion-signal machinery itself; the four lifecycle questions collapse onto one object (completion = terminal state; cancellation = third terminal state; rejection n/a). Exactly-once completion = atomic check-then-act; callbacks-outside-the-lock = the alien-call rule. Fits as the degenerate single-task case.

## 9. Derivation recipe (step-by-step)

1. **Classify the work-generation shape.** Fixed set known upfront → skeleton C, completion is just "join N futures/latch". Self-generating → skeleton A, completion needs Section 2. Building the machinery itself → skeleton B. This one question picks your skeleton.
2. **Answer the four lifecycle questions out loud** (Section 1): completion, rejection/backpressure, cancellation, shutdown. One sentence each, before code. These *are* the invariant for Type F — e.g. "every accepted task runs at most once; return only when no work is queued or in flight; submit blocks when full; graceful shutdown drains, immediate discards."
3. **Identify races on work identity.** Can the same item be discovered twice? → atomic claim, claim-before-work, boolean result as linearization point (Section 3). No duplication possible → skip; say why.
4. **Pick the termination mechanism** (self-generating only): pending counter (default) / future-tree (name starvation, or virtual threads) / level-BFS (name straggler cost). State the three counter rules if using the counter.
5. **Armor the workers.** Per-task catch-Throwable; accounting decrements in `finally`; failure policy stated (log/collect/retry — and note a retry re-increments the counter).
6. **Size the executor.** CPU vs I/O heuristic + "would measure" (Section 5). Mention virtual threads if I/O-bound and waiting-in-worker appears anywhere.
7. **Verify against the failure-mode catalog** (Section 10) — walk each one and say why your design dodges it, plus one happy-path and one contention interleaving.

## 10. Failure-mode catalog

| # | Failure | Mechanism | The fix |
|---|---------|-----------|---------|
| 1 | **Queue-empty false termination** | In-flight worker about to enqueue children; observer sees empty queue and declares done | Count in-flight work: pending counter / future tree / level ownership (§2) |
| 2 | **Claim-after-work double-processing** | Window between starting work and marking done; second worker enters it. Also: non-atomic contains+add | Claim before work, via one atomic boolean-returning op (§3) |
| 3 | **Silent worker death** | Uncaught throwable kills worker thread; pool shrinks with no error; throughput → 0 | Per-task catch-Throwable; decrement/accounting in finally (§4) |
| 4 | **Waiting-in-pool starvation deadlock** | All N fixed-pool workers block on futures of tasks that need a free worker to run | Don't wait in workers (counter), work-steal (ForkJoinPool), or virtual threads (§2b, §7) |
| 5 | **Flag-only shutdown, parked workers sleep forever** | Workers blocked inside queue.take() never re-read the flag | Poke the sleepers: N poison pills or interrupts; check flag under enqueue's coordination (§1 Q4) |

Honorable mentions to keep loaded: unbounded queue = deferred OOM (Q2); swallowed InterruptedException = uncancellable task (§6); pill count ≠ worker count = stuck shutdown (skeleton B); missing executor shutdown = non-daemon threads keep the JVM alive after main returns.

---

## 11. Validation

### 11a. Web crawler multithreaded (primary)

Running the recipe: **(1)** getUrls returns new URLs → self-generating → skeleton A. **(2)** Completion: pending counter reaches 0 (the problem's stated heart); rejection: bounded submission is a production note, LC-scale allows unbounded pool queue — say so; cancellation: not required by LC, one sentence ("interrupt workers, abandon crawl"); shutdown: `executor.shutdown()` after done-signal. **(3)** Two pages link to the same URL → `visited.add(url)` boolean is the claim; claim before fetch; hostname-filter before claim — exactly the strategy section's sub-problem 2, including its pitfall 4. **(4)** Counter as default; the strategy section's designs 1/2/3 map one-to-one onto §2a/2b/2c, with the same trade-offs (starvation for the future tree, level-boundary loss for BFS). **(5)** getUrls may throw → catch + finally-decrement, or termination hangs (strengthens the doc's "LC ignores failures" note into a mechanical requirement). **(6)** I/O-bound → cores × (1 + wait/compute), "would measure"; virtual threads mentioned for design 2 — both verbatim in the strategy section. **(7)** Catalog: #1 is the problem's raison d'être, #2 is its dedup trap, #3/#4 are its pitfalls 3 and 5.

**Verdict: fits.** Every recipe step lands on a real decision in the existing strategy; nothing in the strategy falls outside the pattern. One recipe adjustment made during validation: step 3 originally came after termination choice; the crawler shows claiming must be settled first because the claim decides *what gets counted* (only claim-winners spawn) — steps reordered to claim-then-count.

### 11b. Thread pool from scratch (transfer — lifecycle aspects)

Running the recipe: **(1)** You are building the machinery → skeleton B (the pool itself doesn't self-generate; step 4 correctly no-ops). **(2)** Completion: "done" = post-shutdown quiescence, i.e. `awaitTermination`, not a work-graph property — the four questions still apply, completion just takes its infrastructure meaning; rejection: bounded queue blocks = backpressure (the strategy's design-decision 1); cancellation: interrupt-as-exit-signal alternative; shutdown: graceful = flag + N pills, immediate = drain + interrupt — the strategy's part 3 verbatim. **(3)** No duplicate discovery of tasks (each submitted once, queue hands each to one taker) → step correctly skipped with a reason. **(5)** Per-task catch = the strategy's #1 pitfall and part 1's load-bearing catch. **(7)** Catalog #3 = strategy pitfall 1; #5 = strategy pitfall 2; pill-count mention = pitfall 3; the busy-wait pitfall 4 belongs to the bounded-resource pattern (Type C), which is fine — this problem genuinely straddles both categories.

**Verdict: transfers.** The recipe reproduces every lifecycle decision in the thread-pool strategy; the only non-covered pitfall (busy-wait polling) is a Type C concern, confirming the category boundary rather than a gap.

## What the general framework leaves out

The 5-step framework mostly holds — Step 1 classifies Type F cleanly, Step 3 already says "define completion, rejection, cancellation, and shutdown" (which this playbook expands into Section 1), and Step 5's checks 5–6 cover cancellation and lifecycle. Genuine gaps for this category:

1. **No Type F template in Step 4.** Templates 1–4 cover ordering, queues, and groups; nothing gives the pending-counter or worker-loop shape. Sections 2a and 8 fill this — Step 4 could reference them as "Template 5".
2. **Termination detection for self-generating work is invisible in the framework.** Step 2's example executor invariant covers at-most-once and shutdown but not "how do we know we're done" when workers create work — the single hardest idea in the category. The framework's "who is allowed to proceed, and who tells them?" mantra doesn't retrieve it either; the Type F mantra is "**where is the work, and who is counting it?**"
3. **Step 5 has no starvation-by-pool-exhaustion check.** Check 2 (deadlock) targets lock cycles; waiting-on-futures-inside-a-fixed-pool deadlocks with no lock anywhere. Catalog #4 should be an explicit verification item.
4. **Minor: the toolbox has no completion-signal row.** `Future`/`CompletableFuture` (and virtual threads/structured concurrency as a modern note) appear nowhere in Step 0, yet every Type F answer uses one.
