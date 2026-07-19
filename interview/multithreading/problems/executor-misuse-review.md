---
layout: post
title: Executor Misuse Review — five bugs in one file, ranked
date: 2026-07-19
description: >-
  Every defect here is individually well known and individually boring. The round is not testing recall; it is testing whether you can look at a real file with five problems…
categories: interview multithreading problems
---

Part of the [Debugging & Code Review](/interview/multithreading/patterns/debugging-and-code-review/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** AWS L6 and Rubrik "review this production service" rounds; Uber L5+ operational-readiness reviews. This is the grab-bag problem — it is graded less on *finding* the defects than on **prioritising** them.

### The code under review (described, not shown)

An `IngestService` that has been in production for two years. It is the service the team is least happy with, but nobody can say exactly why.

**Construction.** It builds its executor with the standard fixed-pool factory: `Executors.newFixedThreadPool(16)`. It is a singleton, constructed at startup, and its threads are created with default (non-daemon) settings. The service has no `close()`, no `shutdown()`, and nothing registers a JVM shutdown hook.

**Submission.** A `submit(record)` method called by the HTTP layer on every request. It wraps the record in a `Runnable` and hands it to the executor. There is no capacity check and no rejection handling; the call is treated as if it always succeeds instantly.

**The task body.** Each task:
- Reads a per-request tenant id and stores it in a `static ThreadLocal<TenantContext>` so that deep helper code can read it without threading it through parameters. The context object holds the tenant's config and a reference to a decoded request payload (a few hundred KB). It is **set at the top of the task and never removed**.
- Performs a blocking call into a downstream store with no timeout.
- Catches `InterruptedException` around that blocking call and logs it at DEBUG, then continues into the next step of the task.
- Has no other exception handling. If the parsing step throws a `RuntimeException`, nothing catches it.

**A second path.** A `submitBatch(records)` method submits each record as its own task and then, **from inside a task already running on the same executor**, calls `get()` on each of those futures to assemble a batch result.

**Metrics.** The service exposes a gauge that reports `executor.getQueue().size()`, and a dashboard alerts when it exceeds 10,000. The alert has never fired.

### The observed symptoms

Collected from two years of incident reports, in the order the team filed them:

- **"Throughput slowly decays."** After several days of uptime, the service processes fewer records per second with the same input rate. Restarting fixes it for another few days. No errors in the log. A thread dump taken late in the cycle shows **fewer pool threads than it did at startup**.
- **"Latency is fine and then we OOM."** Under a downstream slowdown, response times stay normal for several minutes — the HTTP layer is happy — and then the process dies with an `OutOfMemoryError`. The queue-size dashboard, checked afterward, showed a number in the millions. The 10,000 alert did fire, once, on a graph nobody was watching.
- **"Deploys take eleven minutes."** After the main thread finishes, the JVM does not exit. Deployment tooling waits for a graceful stop and eventually SIGKILLs it. Sometimes in-flight records are lost as a result.
- **"We can't cancel anything."** A hung batch could not be cancelled; cancelling the futures had no effect and the tasks ran to completion (or didn't) regardless.
- **"Heap dumps show tenant data for tenants that aren't in flight."** Retained-size analysis points at the pool's threads holding large object graphs long after the corresponding requests completed.
- **One total stall.** During a batch-heavy hour, the service stopped processing entirely. CPU zero. Thread dump: all sixteen pool threads **WAITING**, no thread **BLOCKED**, no deadlock section.

### Your task

1. Identify every defect. There are at least five distinct ones.
2. Match each symptom to its cause. Every symptom above is explained by exactly one defect.
3. Explain the last one — the total stall — mechanically, and say why the dump has no deadlock section.
4. Give the fix for each, and the **prioritised order** in which you would land them, with your reasoning for the ranking.
5. Give the review checklist you would apply to the *next* service, so this class of file never merges again.

### Clarify before diagnosing

- What is the acceptable behaviour under overload — shed load, block the caller, or queue? (You cannot choose a rejection policy without this, and "queue forever" is a choice nobody made deliberately.)
- Must in-flight records survive a deploy, or is at-least-once delivery from upstream assumed? (Decides how much shutdown machinery is warranted.)
- Are tasks CPU-bound or I/O-bound? (16 threads is a guess until you know; it also determines whether the batch path can ever be made safe on a fixed pool.)
- Does the downstream call have a server-side timeout? (No client timeout plus no server timeout equals an unbounded hold on a pool thread.)

### Why this problem matters

Every defect here is individually well known and individually boring. The round is not testing recall; it is testing whether you can look at a real file with five problems and produce a **ranked, justified remediation plan** — because that is the actual job. A candidate who lists all five in arbitrary order has done a linter's work. A candidate who says *"silent worker death and the unbounded queue first, because one silently degrades to zero throughput and the other turns a downstream blip into a process death; cancellability next because it blocks every future incident response; the ThreadLocal leak and the missing shutdown after that, and here's why the leak isn't first even though it's the scariest-sounding"* has done an engineer's.

---

## Strategy

### Classify

Task execution / lifecycle (Type F) seen from the review side. Sweep 5 (**find every lifecycle path**) is the whole diagnosis, run repeatedly over one file: follow a unit of work from submission, through overload, through failure, through cancellation, through shutdown, and — the one everybody forgets — through *what it leaves behind on a pooled thread*. Catalog #15 (silent worker death), #16 (flag-only / absent shutdown), #17 (pool-exhaustion starvation), plus the two review-specific additions: **unbounded queue hiding overload** and **`ThreadLocal` leak on a pooled thread**.

### The invariant being broken

There isn't a single one, which is itself the lesson: **Type F correctness is an accounting property spread across four questions**, and this file answers none of them. Ask them out loud before diagnosing — *how do we know a task completed? what happens when work arrives faster than we process it? how does in-flight work stop early? what happens at shutdown?* — and each unanswered question maps to a defect.

### Defect-by-defect

### D1 — No per-task catch: silent worker death (catalog #15)

The task body has no `try/catch` around its work. An uncaught `RuntimeException` propagates out of the `Runnable`. With `execute`-style submission that kills the worker thread; the pool quietly replaces it, but with `submit`, the throwable is **captured into the `Future` and never observed** because nobody calls `get()` on it — so it vanishes with no log line at all. Either way the failure is invisible. The dump showing *fewer pool threads than at startup* is the tell for the thread-death variant, and it is the direct explanation of **"throughput slowly decays, restart fixes it"**: the pool erodes one worker per unhandled exception until throughput approaches zero, with an empty error log the whole time.

**Fix:** wrap every task body in `try { ... } catch (Throwable t) { record and continue }` — `Throwable`, not `Exception`, because an `Error` kills a worker just as dead. "Record" means somewhere a human sees: a counter, a metric, an error log at ERROR. Also set an `UncaughtExceptionHandler` via a custom `ThreadFactory` as a backstop, and — the institutional fix — never use bare `submit()` without either observing the future or wrapping the body. Catching is not handling: state the policy (log and drop / retry / dead-letter) explicitly.

### D2 — Unbounded queue hiding overload

`Executors.newFixedThreadPool` uses a `LinkedBlockingQueue` with **no capacity bound**. This is the single most criticized factory method in the JDK. The queue never says no; it grows until the heap is gone. That is not robustness, it is **deferred failure** — and it is worse than failing fast, because the failure arrives minutes later, far from the cause, as a process death rather than a rejected request.

It explains **"latency is fine and then we OOM"** exactly: the HTTP layer sees fast responses because `submit` returns immediately no matter how deep the backlog is. There is no backpressure signal anywhere — the caller cannot tell the difference between "processed" and "enqueued behind two million records". By the time anyone notices, the heap is the queue.

It also explains why the alert was useless: the gauge was correct and the threshold was reasonable, but the queue crosses 10,000 and reaches millions in the same minute, so an alert on queue depth is a smoke detector inside the fire.

**Fix:** construct the `ThreadPoolExecutor` explicitly with a **bounded** queue (`ArrayBlockingQueue` of a size derived from the latency budget, not a round number) and an explicit `RejectedExecutionHandler`. Then pick the policy deliberately, and this is the design conversation the round wants:
- **`CallerRunsPolicy`** — the submitting thread runs the task, which self-throttles the HTTP layer. Sneaky and effective for internal pipelines.
- **`AbortPolicy`** (throw) — the request-serving answer: turn overload into a fast 503 with a retry-after, which is honest load shedding.
- **Block the submitter** (via a semaphore around submission, since the executor won't block for you) — true backpressure; correct for internal pipelines, dangerous on a request thread.
The interview line: *"bounded queue plus an explicit rejection policy — an unbounded queue hides overload until the process dies."* And add the operational fix: alert on queue **latency** (age of the head item) rather than depth, because depth is a level and latency is what users feel.

### D3 — Swallowed `InterruptedException`: uncancellable tasks

Catching `InterruptedException` and logging at DEBUG **erases the cancellation request**. The throw already cleared the interrupt flag; nothing restores it; the task carries on into its next step as if nothing happened. Every downstream blocking call in the same task will now also fail to see the interrupt, because the flag is gone.

This explains **"we can't cancel anything"**: `Future.cancel(true)` interrupts the worker, the interrupt lands, and the task eats it. It also compounds D5 — shutdown interrupts workers and the workers ignore them, so `shutdownNow()` is equally powerless.

**Fix:** either **propagate** (declare `throws InterruptedException` and let it fly, which is right for library code) or **restore and exit** (re-interrupt the current thread, then return / break out of the loop, which is the task-body idiom). Never catch-and-continue. Add a client **timeout** on the blocking downstream call while you're there — no client timeout and no server timeout means a single hung socket occupies a pool thread indefinitely, which is a slow-motion version of D4.

### D4 — `get()` on futures from inside a pool task: pool-exhaustion starvation (catalog #17)

`submitBatch` runs on the same executor and blocks on the futures of tasks that need a free worker to run. With 16 workers, once 16 batch tasks are simultaneously blocked in `get()`, there is no worker left to execute the sub-tasks they are waiting for. Nothing will ever complete. **This is the total stall.**

Why there is no deadlock section: it is not a cycle among monitors. Nothing is *held*. Every thread is parked in `Future.get()` waiting to be told — hence all sixteen **WAITING**, none **BLOCKED**, and the JVM's detector, which finds monitor and `ReentrantLock` cycles only, has nothing to report. This is precisely the signature from `the-hang-that-isnt-a-deadlock`, arriving from a completely different cause, and holding both causes in mind when you see that dump is the point of the exercise: **all-WAITING-no-cycle means either a lost wakeup or pool starvation.**

**Fix, in preference order:** (a) don't wait inside pool threads — restructure so the caller's own thread does the fan-in, or use a pending-counter/completion-signal design; (b) use `CompletableFuture.allOf` composition so nothing blocks at all; (c) submit sub-tasks to a **separate** executor from the one the coordinator runs on (a bulkhead — never let a pool wait on itself); (d) `ForkJoinPool`, whose work-stealing lets a joining worker run pending subtasks itself; (e) virtual threads, where a blocked task unmounts its carrier and the scarcity that causes the starvation disappears.

### D5 — `ThreadLocal` set and never removed on a pooled thread

The context is set at the top of the task and never cleared. Pool threads live for the lifetime of the process, so the value survives the task and stays reachable from the thread's `ThreadLocalMap` until the *next* task on that thread overwrites it — or forever, if that thread goes idle. Two consequences, and the second is worse than the memory:

1. **Memory retention.** Sixteen threads each pinning a few hundred KB of decoded payload plus a tenant config graph, indefinitely. This is **"heap dumps show tenant data for tenants that aren't in flight"**, and retained-size analysis pointing at pool threads is the classic fingerprint. (In a container with a redeployed classloader this becomes a full classloader leak — worth a sentence.)
2. **Correctness and security.** A task that reads the context *before* setting it — or one on a code path that forgets to set it — sees the **previous tenant's** context. Cross-tenant data exposure from a leftover `ThreadLocal` is a real class of security incident. Say this: it upgrades the defect from "memory hygiene" to "isolation failure".

**Fix:** always `try { set(...); ... } finally { remove(); }`. Better, wrap it once in a task decorator so no individual task can forget, and make the raw `submit` private. Note that `ThreadLocal`'s weak-key design does *not* save you — the key is weakly referenced, the **value is not**, and the entry lives until the map is cleaned by a later operation on that same thread, which may never happen. Best of all: pass the context explicitly, or use a scoped mechanism designed for it.

### D6 — No shutdown at all

Pool threads are **non-daemon** by default, so the JVM will not exit after `main` returns. Nothing calls `shutdown()`, nothing registers a shutdown hook. This explains **"deploys take eleven minutes"** and the lost records: the deploy tooling waits for a graceful exit that can never happen, then SIGKILLs, and everything queued or in flight dies without a trace.

**Fix:** implement lifecycle properly and name both semantics. **Graceful:** `shutdown()` (stop accepting, let queued work drain) followed by `awaitTermination(bounded timeout)`, then `shutdownNow()` as the escalation, then a second `awaitTermination`, then log whatever `shutdownNow()` returned — those are the tasks that never ran and someone needs to know. Register that sequence as a JVM shutdown hook and expose it as a `close()`. Optionally mark threads daemon via a `ThreadFactory` as a backstop, but a daemon flag is a way to *stop caring* about in-flight work, not a substitute for draining it. And note the D3 interaction: none of `shutdownNow()`'s interrupts do anything until the swallowed `InterruptedException` is fixed.

### D7 — Minor but flag it: unnamed threads, no sizing rationale

The default `ThreadFactory` produces `pool-1-thread-7`, which makes every thread dump harder to read than it needs to be — name threads after the pool's purpose; it costs one line and pays for itself on the first incident. And 16 is an unexplained constant: state the heuristic (CPU-bound ≈ cores; I/O-bound ≈ cores × (1 + wait/compute)) and then the caveat — *these are starting points, I'd measure under realistic load* — because the task is a blocking downstream call and 16 is almost certainly the wrong number for it.

### The prioritised plan (the graded part)

Rank by blast radius: silent data/throughput corruption first, then unbounded resource growth, then operability, then hygiene.

1. **D1 (per-task catch).** One line, no design debate, and it is the difference between an incident you can see and one you can't. Ship first because *everything else is harder to diagnose while failures are invisible* — it is the fix that makes the other fixes verifiable.
2. **D2 (bounded queue + rejection policy).** The only defect that reliably kills the process. Needs a product decision (shed vs block), so start that conversation immediately even though it lands second.
3. **D3 (interrupt handling).** Cheap, and it is a **prerequisite** for D6 being effective — an un-interruptible task makes graceful shutdown a fiction. Ordering fixes by dependency, not just severity, is the thing to say here.
4. **D4 (pool self-waiting).** Causes total outage but only under batch-heavy load; the fix is a real refactor (separate executor is the fast mitigation — land the bulkhead now, the composition rewrite later).
5. **D5 (ThreadLocal leak).** Sounds the scariest and is genuinely a cross-tenant risk, but it is bounded by pool size and has not produced a user-visible incident. **If the isolation analysis shows a real cross-tenant read path, this jumps to #1** — say that conditional out loud; a security-severity defect outranks everything, and knowing when to re-rank is the senior signal.
6. **D6 (shutdown).** Costs deploy time and loses in-flight work, but it is contained to deploys and has a manual workaround.
7. **D7 (naming, sizing).** Land with any of the above.

### Reproduce and confirm

Per defect, the cheapest deterministic demonstration:

- **D1:** submit N tasks of which some throw; assert the completed count equals N and watch it not. Confirm with `getPoolSize()` before and after, or thread names in a dump.
- **D2:** submit far faster than the pool drains and watch `getQueue().size()` and heap climb while `submit` still returns instantly. That instant return under a growing backlog *is* the demonstration that there is no backpressure.
- **D3:** submit a task, cancel it with `mayInterruptIfRunning`, assert it stops within a timeout. It won't.
- **D4:** pool of 2, submit 2 coordinator tasks each waiting on a sub-task. Instant, 100%-reproducible stall — and this is the good news about D4: unlike most concurrency bugs it is **deterministic**, so a unit test with a timeout catches it forever. Confirm with the all-WAITING-none-BLOCKED-no-deadlock-section dump.
- **D5:** run tasks, let the pool idle, take a heap dump, and check retained size rooted at the pool threads. Or, more damningly, run a task that *reads* the context without setting it and assert it sees nothing — it will see the previous tenant's.
- **D6:** run `main` to completion and observe the JVM not exiting; `jstack` shows live non-daemon pool threads.

Every harness gets a **timeout**, because three of these six failure modes are hangs and a test that hangs the suite is worse than no test.

### Prove the fixes

Type F proofs are accounting arguments, not lock-coverage ones:

- Every task's body is wrapped, so no throwable escapes to the worker loop; therefore the pool size is invariant over time and every failure is recorded.
- The queue is bounded, so backlog memory is bounded by capacity × record size; overload produces a rejection at a known threshold instead of an OOM at an unknown one.
- Every `InterruptedException` either propagates or restores the flag, so a cancellation request is never lost; therefore `cancel(true)` and `shutdownNow()` terminate work within a bounded time.
- No task blocks on a future produced by its own executor, so a free worker always exists for any task another task is waiting on; therefore the starvation stall is unreachable. (State it as the invariant: **no pool waits on itself.**)
- Every `ThreadLocal.set` is paired with a `remove` in a `finally` in a single decorator, so no value survives its task; therefore no cross-task retention and no cross-tenant read.
- Shutdown flips accepting off, drains within a bounded timeout, escalates to interruption, and reports undrained work; therefore the process exits and nothing is lost silently.

### The review checklist for the next service (part 5)

Ask these of any file that touches an executor, in this order:

1. Is the queue **bounded**, and is the rejection policy **chosen** rather than defaulted? (Never `Executors.newFixedThreadPool`/`newCachedThreadPool` in production code — construct `ThreadPoolExecutor` explicitly.)
2. Does **every** task body have a `catch (Throwable)` and a stated failure policy?
3. Is every `InterruptedException` propagated or restored — and does every blocking call have a **timeout**?
4. Does any task ever `get()`/`join()` on work submitted to **its own** executor? (Bulkhead: coordinators and workers never share a pool.)
5. Is every `ThreadLocal.set` paired with a `remove` in a `finally`, ideally in a decorator?
6. Is there a `shutdown()` / `awaitTermination` / `shutdownNow` sequence, wired to a shutdown hook, and are undrained tasks logged?
7. Are threads **named**, and is the pool size justified by a heuristic plus an intent to measure?
8. Are the metrics right — queue **latency** and rejection count, not just depth?
9. Is every acquire/release, increment/decrement and set/clear in a `finally`?

Nine questions, and they are the same nine every time. That reusability is the deliverable.

### Pitfalls

1. **Listing the bugs without ranking them.** That is a linter's output, and it is the specific failure mode this problem is built to expose.
2. **Ranking by scariness rather than blast radius** — the `ThreadLocal` leak sounds worst and isn't, *unless* the cross-tenant read path is real.
3. **Missing the dependency ordering** — fixing shutdown before interrupt handling produces a graceful shutdown that doesn't work.
4. **Calling the total stall a deadlock.** No lock is held. It is starvation, and the dump proves it.
5. **"Just make the pool bigger."** It moves the D4 stall to a higher batch count and does nothing else.
6. **Marking threads daemon to fix the deploy.** That trades an eleven-minute deploy for silently discarded in-flight work.
7. **Trusting `submit`'s `Future` to surface errors** that nobody ever calls `get()` on.
8. **Not asking the overload question.** You cannot pick a rejection policy without knowing whether the system should shed or slow.

### Check your understanding

1. Match all six symptoms to their defects, one each.
2. Why does an unbounded queue make latency look *good* right up until the OOM?
3. Explain the total stall mechanically. Why is there no deadlock section, and what is the *other* common cause of an identical dump?
4. What does swallowing `InterruptedException` do to the interrupt flag, and which two other defects does it silently disable?
5. Why doesn't `ThreadLocal`'s weak key prevent the leak?
6. Give the full graceful-shutdown sequence, including what you do with `shutdownNow()`'s return value.
7. Give your ranked remediation order and defend the position of the `ThreadLocal` leak. What single finding would move it to first?
8. Name the three rejection policies and the situation each is right for.
9. Which of these six defects is deterministically testable, and why is that a big deal?

### Transfers to

`thread-pool-from-scratch` (the same lifecycle questions from the build side — every defect here is a rule that problem teaches), `web-crawler-multithreaded` (D1 and D4 are its pitfalls 3 and 5, and the crawler's pending-counter is the D4 fix generalized), `parallel-api-aggregation` (fan-in must happen on the caller's thread, which is D4 stated as a design rule), and every production readiness review you will ever sit in. The nine-question checklist is the single most directly reusable artifact in this folder.
