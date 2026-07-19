---
layout: post
title: "Multithreading 101: Everything You Must Know"
date: 2026-07-19
description: The concepts that carry every concurrency interview, atomicity, visibility, ordering, the Java Memory Model, the condition loop, semaphores, deadlock, executors, and a universal failure catalog.
categories: interview multithreading basics
---

This is the vocabulary page. No problems, no walkthroughs, just the concepts that everything else in this section stands on. If you can explain every heading below from memory, including the canonical interleaving for each bug, you are ready for any senior multithreading round.

Companion pages: [the interview framework](/interview/multithreading/mt-framework/) for the method, and the [pattern playbooks](/interview/multithreading/) for the seven families.

## 0. The one question

Every concurrency problem ever asked is one question wearing costumes:

> **Who is allowed to proceed, and who tells them?**

(With one exception: task-lifecycle problems ask "**where is the work, and who is counting it?**")

The scheduler decides who *runs*. Your code decides who *proceeds*. All coordination is building the gap between those two.

---

## 1. The three fundamental problems (concept: why threading is hard at all)

Every bug in this field is one of three diseases. Name which one you're fighting before you fix anything.

1. **Atomicity**: an operation you think is one step is actually several, and another thread interleaves between them. `count++` is read-modify-write: two threads read 5, both write 6, one update lost. **Check-then-act** is the same disease at statement scale: decide on state that changes between the decision and the action (`if (instance == null) → construct`, `if (!isEmpty) → pop`, derive-tokens → consume). The ONLY cure is making the *pair* atomic: lock, CAS, or an atomic API. Locking only the check or only the act cures nothing; the race lives in the gap.
2. **Visibility**: thread B never sees thread A's write. Without a happens-before edge, values live in registers and caches; a reader can spin forever on a stale value. `volatile` gives freshness, but **freshness ≠ atomicity** (`volatile count; count++` is still broken).
3. **Ordering**: the compiler and CPU reorder anything not constrained by happens-before. The reference to an object can become visible *before* its constructor's field writes (broken double-checked locking). Within a data race, you have NO guarantees at all.

**Concept vocabulary:** race condition, data race, critical section, atomic operation, memory barrier, instruction reordering.

## 2. Bedrock facts

- **Thread vs process**: threads share the heap (all objects); each owns its stack and program counter. Shared heap is why any of this is a problem, and why thread-confined (stack-local) data needs no locks.
- **Invariant**: the sentence that must always be true over shared state ("0 ≤ size ≤ capacity"). One invariant → one lock; state one invariant spans must be guarded together (it may be *temporarily* broken inside a critical section: that's what the critical section is FOR). Independent invariants may take independent locks: that's what fine-grained locking is, and its price is multi-lock deadlock territory.
- **Linearization point**: the single locked/atomic instruction where an operation logically takes effect (the `size++` with the array store; the CAS; the winning `add`). Pointing at it is the compact proof of atomicity.
- **Safety vs liveness**: safety = nothing bad happens (invariant holds); liveness = something good eventually happens (no deadlock/livelock/starvation). They fail independently; verify both. Most candidates only check safety.

## 3. The Java Memory Model: the six happens-before edges (concept: memory consistency model)

Memorize these; everything about visibility reduces to "is there an HB path?":

1. **Program order**: within one thread.
2. **Monitor**: unlock of M happens-before every later lock of M. (Why synchronized gives visibility, and why "lock the writes, read without the lock" is broken: the unlocked read has no incoming edge.)
3. **Volatile**: write to v happens-before every later read of v, and it carries *everything the writer did before the write* (the piggyback rule).
4. **Final fields**: an object constructed without leaking `this` publishes its final fields fully-initialized by any route. The engine of immutability.
5. **Thread lifecycle**: `start` HB everything in the thread; everything in it HB `join` returning.
6. **Class initialization**: static init runs once, safely published (the holder idiom's engine).

Also: `j.u.c` synchronizers all carry the edge: `release` HB subsequent `acquire`, queue `put` HB `take` of that element. **The baton handoff is also the visibility handoff.**

**Safe publication** (concept): writing a reference is not publishing an object. The envelope can be mailed before the letter is inside: the canonical exhibit is DCL without volatile, and you must be able to narrate the reordering. Safe routes: static final / class init, volatile / AtomicReference, a lock both sides take, a concurrent collection, or a properly-immutable object. Never let `this` escape a constructor.

## 4. The condition loop: the single most important code shape

```java
synchronized (lock) {
    while (!conditionAllowsMe)   // ALWAYS while, NEVER if
        lock.wait;
    mutateState;
    lock.notifyAll;              // after EVERY state change
}
```

Its laws, each one a classic bug when broken:

- **`while`, never `if`**: three reasons a woken thread's predicate may be false: spurious wakeups (legal per the JLS), wrong-party wakeups, and barging (a fresh thread stole the resource between signal and wake). Re-check on every wake; never trust a wakeup.
- **notifyAll by default.** `notify` wakes an arbitrary waiter; if waiters have different predicates it can wake the wrong one, which re-sleeps: the signal is consumed, everyone ends up asleep. Canonical hang: FizzBuzz at i=2, or a blocking queue with producers AND consumers in one wait set. `notify` is legal only with a one-line proof that all waiters are interchangeable.
- **Signal after mutating, under the lock.** Every state change asks: "whose predicate may have just become true?" Signal exactly them: in producer-consumer, always the *opposite* party, never your own kind.
- **`wait` releases the lock atomically with parking** and reacquires before returning: that's why the recheck reads fresh state, and why you can only wait on the monitor you hold.
- **State is what persists; notify only wakes.** A signal with no state behind it is lost if it fires before anyone waits. This is the lost-wakeup concept, and it's why flag-protocols without locks are broken and semaphore permits are not.
- **Predicate partition** (multi-role loops): all waiters' predicates must be **mutually exclusive and exhaustive** over reachable states: exclusive gives one actor, exhaustive gives progress. Check the overlap value (15 in FizzBuzz).
- **Termination**: whoever finishes must free everyone still waiting: final `notifyAll` before return, exit cascades in semaphore rings. "Correct output, then hangs" is the most common failure in ordering problems.

`ReentrantLock` + multiple `Condition`s = the same idiom with separate wait rooms per predicate, which makes single `signal` safe and removes the wrong-party problem. That sentence IS the reason ArrayBlockingQueue is built that way.

## 5. Semaphores: the other primitive (concepts: permits, ownership, persistence)

A semaphore is a guarded counter: `acquire` waits for a permit and consumes it; `release` mints one. Three properties carry all its uses:

- **Permits persist.** A release before anyone waits is never lost. This is why semaphores solve signaling/ordering (Print in Order) where bare notify cannot.
- **No ownership.** Any thread may release, unlike a mutex/ReentrantLock (owner-checked). This is exactly what enables cross-thread signaling AND the lightswitch (acquired by first-in, released by a *different* thread, last-out). When a lock must be released by a different thread than acquired it → semaphore, by necessity.
- **Counting = the resource itself.** Semaphore(n) IS "at most n concurrent" (multiplex): connection pools, bounded parallelism. Semaphore(0) IS a closed door. Producer-consumer as two semaphores (spaces/items) needs no explicit predicates at all.
- **Barging**: fresh callers can beat parked waiters to a new permit (nonfair mode: throughput-oriented; same in ReentrantLock). It's why `while` is load-bearing, and what "fair mode" trades throughput to fix.

**Mutex vs semaphore in one line:** ownership (only the locker unlocks vs anyone releases) and meaning (exclusion vs counted resource/signal).

## 6. The seven patterns: essence, mechanic, signature bug

| # | Family | Essence | Core mechanic | Signature bug |
|---|--------|---------|---------------|---------------|
| 1 | **Ordering / turn-taking** | who acts next, who tells them | the **baton**: targeted semaphores (doors) or shared-state condition loop (predicate partition) | termination hang; notify-vs-notifyAll |
| 2 | **Guarded state** | interleaved access to shared mutable state | one invariant → one lock; check-then-act atomically; escalation ladder | check-then-act race; unsafe publication |
| 3 | **Bounded resource** | two parties on OPPOSITE predicates | notFull/notEmpty seesaw; 3 expressions (monitor / 2 Conditions / semaphores); block-balk-timeout-reject policy axis | wrong-party notify hang; acquire-order deadlock |
| 4 | **Group formation** | admission (WHO joins) + boundary (groups don't overlap): separable concerns | quotas (static) or tallies+dispatcher (dynamic); reusable barrier | permit theft by lapping threads; double-selection |
| 5 | **Asymmetric access** | compatibility is a property of the PAIR | the **lightswitch**: first-in locks, last-out unlocks; priority = who starves | torn counter → two "firsts"; writer starvation shipped silently |
| 6 | **Task lifecycle** | where is the work, who is counting it | pending-counter discipline; atomic claiming; the 4 lifecycle questions | queue-empty false termination; silent worker death |
| 7 | **Time-based state** | time is an input to state | **lazy derivation** (derive-on-read) vs **wait-until** (awaitNanos loop) | unlocked derive-then-act; sleep instead of signalable wait |

## 7. The transferable mechanics: the ideas that solve everything (learn these AS concepts)

1. **The baton** (ordering): exclusivity + persistence + progress. Route taxonomy: chain / rotation / data-driven. Choice rule: parameterized thread count or data-driven turns → condition loop; targeted wakeups → semaphores.
2. **"The state is the signal."** In condition-loop designs the guarded state itself tells each thread whether to proceed: no separate signal object. Most real code looks like this.
3. **The escalation ladder** (guarded state): immutable/confined → one coarse lock → read-write lock → fine-grained/CAS → JDK structure. Never climb without a stated reason. "I'll start coarse and refine against a measured bottleneck" is the anti-over-engineering sentence.
4. **Compound operations don't compose.** Every method synchronized ≠ caller sequences safe (`isEmpty`+`pop`, `contains`+`put`). Fix: atomic compound methods (`tryPop`, `putIfAbsent`, `computeIfAbsent`). Bring it up unprompted; under concurrency, `size` is a hint, not a guarantee.
5. **The policy axis**: block / balk / timeout / reject-by-state are one-line variants of the same guarded check: a policy knob, not different designs. Always ASK which one the problem wants.
6. **The dependency-chain rule** (refined "never block holding a lock"): never block on a signal **whose provider needs a lock you hold**. Dining savages and the lightswitch deliberately block while holding a mutex: safe because the releaser (cook/writer) never touches that mutex. Verify the chain explicitly; sometimes the "smell" is the mechanism.
7. **The lightswitch**: a GROUP holds a lock through first-in/last-out on a mutex-guarded counter. Generalizes to N categories via a compatibility matrix: one room per exclusion relationship. Preceded always by: **can reads be lock-free instead** (immutable snapshot + volatile/CHM)? The best readers-writers solution often has no lock on the read path.
8. **Generations / phase separation** (the reuse problem): a reused meeting point must let waiters ask "is MY round done?", not "what's the count?", else fast threads lap and steal permits from slow ones. Two fixes: two turnstiles (phases physically never coexist) or generation tokens (signals carry round identity: how CyclicBarrier works). This one idea underlies reusable barriers, H2O, Uber Ride, roller coaster.
9. **Admission vs boundary** (group formation): argue them separately. Static composition (one valid group shape) → quota semaphores; dynamic ("or" in the rule) → tallies under a mutex + the **dispatcher hat** (the completing arrival decides, and **decide-and-decrement is one atomic step** or two dispatchers select the same waiters). Permit re-issue is centralized (barrier action): per-thread release breaks the boundary. Dispatcher releases G−1 permits (never dozes itself).
10. **Coordinator handshake**: "C permits out, 1 signal back" per phase: when the problem gives you a service thread (roller coaster, batch flusher). Boundary by permit-issuance timing.
11. **The pending-counter discipline** (termination of self-generating work): increment BEFORE submit, decrement AFTER complete in `finally`, children counted before the parent decrements; the unique last decrementer *pushes* the done signal. Because **empty queue ≠ no work**: in-flight work is invisible.
12. **Claim-before-work**: dedup via one atomic boolean-returning op (`newKeySet.add` / `putIfAbsent` / CAS): the boolean is the linearization point of ownership. Claim, then count, then submit.
13. **Lazy derivation** (time): store `(value, timestamp)`, derive current state on access: no background refiller/reaper (no lifecycle, no second writer, exact, free when idle). The pair must change **together** (one lock, or CAS on an immutable snapshot: two separate atomics are broken). Exception: pure non-mutating freshness checks are lock-free.
14. **The timed-wait loop**: `awaitNanos(headDue − now)` in a re-check loop; producers **signal on head change**; three wake reasons, identical treatment (re-peek, recompute, decide); claim under the lock, run outside it. Never `sleep` for coordination: sleepers are deaf.
15. **Cached futures / single-flight**: cache a `Future` so the waiting point exists before the value does: N concurrent misses, one load, everyone waits on the same future (dogpile prevention). Remove failed futures or the failure is cached forever.

## 8. Deadlock and liveness (concepts: Coffman conditions, livelock, starvation, fairness)

**Deadlock** = a cycle in the waits-for graph. The reusable answer structure: (a) construct the cycle concretely, (b) check the four **Coffman conditions** (mutual exclusion, hold-and-wait, no preemption, circular wait), (c) fix by breaking exactly ONE, and name which:

- **Global lock ordering** (breaks circular wait): THE production answer. Two-sentence proof: every waiter waits for a lock higher than all it holds; a cycle needs someone waiting for a lower one: contradiction.
- **tryLock + release-what-you-hold + randomized backoff** (breaks hold-and-wait). Retrying WITHOUT releasing is hold-and-wait: you fixed nothing. Residual risk: **livelock** (all contenders cycling in lockstep, active but no progress); randomized backoff desynchronizes.
- **Cap contenders at N−1** for N resources (pigeonhole guarantees progress).
- **Timeouts** as defense-in-depth when you don't own all the code.

Non-obvious deadlock shapes to check: alien code called under your lock (callbacks); `wait` while holding a *second* lock (only the waited-on monitor is released); read→write **upgrade deadlock** (two readers each waiting for the other's read-release: release, acquire write, RE-CHECK); mutex-before-counting-acquire (blocked on a semaphore whose releaser needs your mutex); and **pool-exhaustion starvation**: all N workers waiting on futures of tasks that need a free worker, a deadlock with no locks anywhere (fix: don't wait in workers, ForkJoinPool, or virtual threads).

**Starvation** ≠ deadlock: the system moves, one party never does. Every asymmetric design chooses who starves (readers-preference starves writers; the turnstile inverts it: it is writer-*preferring*, not fair; true fairness = FIFO queuing, which taxes throughput). Ship a default, NAME who starves, fix on request.

## 9. The java.util.concurrent toolbox: what each thing IS

- **synchronized / wait / notifyAll**: the monitor: mutual exclusion + condition waiting + visibility, one wait set.
- **ReentrantLock + Condition**: monitor with multiple wait rooms, `tryLock`, timed acquire, interruptible acquire, optional fairness. Unlock in `finally`, always.
- **Semaphore**: counted permits, no ownership (§5).
- **CountDownLatch**: one-shot gate; count moves one way; await-after-zero returns immediately. One-shot-ness is why it's trivially safe.
- **CyclicBarrier**: reusable meeting point of N; generation-token machinery inside; barrier action runs once per trip (your centralized reset hook). Breaks (BrokenBarrierException) if a waiter is interrupted, so peers don't hang.
- **BlockingQueue** (ArrayBlockingQueue / LinkedBlockingQueue): the bounded-resource pattern, shipped. LBQ uses two locks (head/tail) so producers and consumers don't contend.
- **ConcurrentHashMap**: per-bin locking; lock-free-ish reads; per-METHOD atomicity only (`computeIfAbsent`/`putIfAbsent`/`merge` for compound). `newKeySet` for concurrent sets.
- **Atomics / CAS**: hardware check-then-act on one variable; retry loops on immutable snapshots for multi-field; **ABA problem** at awareness level (value returned to A but world changed; versioned stamps fix). `LongAdder` for hot counters.
- **ReentrantReadWriteLock**: lightswitch shipped; nonfair default (with queued-writer mitigation), fair mode, NO upgrade (deadlock: by design), downgrade OK. **StampedLock**: optimistic reads, awareness only.
- **ThreadLocal**: per-thread state; memory-leak pitfall in pools (threads outlive tasks: remove when done).
- **Future / CompletableFuture**: a waiting-point for a result; CompletableFuture adds non-blocking composition (thenApply/thenCombine/exceptionally).

Rule: recognize everything, code from the small toolbox, and in design rounds prefer the highest-level utility that fits: hand-rolling what the JDK ships is a design smell unless implementation IS the question.

## 10. Executors and lifecycle (concepts: thread pools, backpressure, cooperative cancellation)

- **Why pools**: thread creation is expensive; pools reuse. A pool = bounded blocking queue + N worker loops: you've built one; everything below follows from that image.
- **ThreadPoolExecutor anatomy**: corePoolSize, maxPoolSize (grows only when the queue is FULL), keepAlive, workQueue, RejectedExecutionHandler (abort / caller-runs / discard / discard-oldest: caller-runs is a sneaky self-throttle).
- **Sizing**: CPU-bound ≈ cores; I/O-bound ≈ cores × (1 + wait/compute), then say "a starting heuristic; I'd measure." Unbounded queues = deferred OOM; bounded + explicit policy = real backpressure.
- **The four lifecycle questions** (answer before coding any Type F design): completion (how do we KNOW we're done), rejection/backpressure, cancellation, shutdown (graceful drains; immediate returns the drained tasks). Flags don't wake sleepers: poke parked workers with poison pills (one per worker; FIFO drains real work first) or interrupts.
- **Worker survival**: per-task catch-Throwable or the pool silently shrinks to zero: the #1 real pool bug. Accounting decrements in `finally`.
- **Interruption** (cooperative cancellation): it's a request, not a kill. Never swallow `InterruptedException`: propagate or restore the flag (`Thread.currentThread.interrupt`); CPU loops must poll `isInterrupted`; only the owner interprets the meaning.
- **Virtual threads** (Java 21+): blocked virtual threads unmount from carriers: thread-per-task becomes viable, waiting-in-worker stops being a hazard, structured concurrency packages fork/join-with-cancellation. They do NOT make shared mutable state safe, and bounding shifts from pool size to semaphores.

## 10b. Parallelism and async composition (concepts: async ≠ parallel, task vs data parallelism)

Not a separate discipline (it's Type F with composition sugar), but its vocabulary is probed on its own:

- **Concurrency vs parallelism**: dealing with many things at once (interleaving) vs doing many things at once (simultaneous execution). You can have either without the other.
- **Async ≠ parallel**: async frees the *waiting thread* (no thread parked during I/O); parallel uses *many workers*. Scatter-gather over slow services is both: launch in parallel, compose asynchronously.
- **Java's async/await is `CompletableFuture`** (no keyword). The 8-operation vocabulary: `supplyAsync(f, executor)` (always name the executor: commonPool is JVM-shared and CPU-sized, wrong for blocking I/O), `thenApply` (map) vs `thenCompose` (flatMap: dependent async calls; confusing them nests futures), `thenCombine` (merge two), `allOf`/`anyOf` (wait-all / first-wins; allOf returns Void: re-read the members), `exceptionally`/`handle` (fallback, its PLACEMENT is the degradation policy: per-call vs whole-pipeline), `orTimeout`/`completeOnTimeout` (per-call timeout, the latter with built-in fallback). Threading rule: non-`*Async` continuations run on the completing thread (often an I/O thread: keep them tiny); `*Async` hops to your executor.
- **The signature bug**: launch-then-`get` in a loop serializes everything: latency becomes the sum, not the max. Launch ALL, then compose.
- **Task vs data parallelism**: different jobs at once (scatter-gather, executors) vs same operation over partitioned data (parallel streams, ForkJoinPool). Parallel streams: CPU-bound, side-effect-free, large data only: never for I/O (commonPool starvation).
- **ForkJoinPool / work-stealing**: idle workers steal from others' deques; a joining worker runs pending subtasks itself: why fork/join doesn't self-starve the way naive wait-on-futures-in-a-fixed-pool does.
- **Amdahl's law**: speedup is capped by the serial fraction (10% serial → max 10× ever). The one-sentence answer to "why not just add more threads."
- **Structured concurrency** (Java 21, `StructuredTaskScope`): blocking-style scatter-gather on virtual threads: fork children, join the scope; failure cancels siblings (ShutdownOnFailure) without hand-wiring. The modern counter-answer to CompletableFuture pipelines; know both and choose per runtime.

Worked problem: [Parallel API Aggregation](/interview/multithreading/problems/parallel-api-aggregation/).

## 11. Time and clocks (concept: monotonic vs wall time)

`System.nanoTime` for ALL interval arithmetic (monotonic); `currentTimeMillis` jumps with NTP: token bursts, mass un-expiry, never-due tasks. Wall clock only for human-facing absolutes and persistence. A semaphore caps **concurrency**; a rate limiter caps **frequency**: permits are returned by threads, tokens are minted by time. Saying "nanoTime, because wall clock jumps" unprompted is the cheapest senior marker in the category.

## 12. The universal failure-mode catalog: the bugs, cross-family

The complete list to sweep any solution against. For each: know the interleaving, not just the name.

1. Check-then-act outside the lock (incl. `count++`, contains+put, derive-then-consume)
2. Lost wakeup: transient signal (no persisted state) or missing notify after a state change
3. Wrong-party wakeup: `notify` with mixed waiters / one semaphore for two roles
4. `if` instead of `while` around a wait (spurious wakeups, barging, lost races)
5. Unsafe publication: non-volatile DCL, `this` escape, unlocked reads of guarded state
6. Lock-order inversion → deadlock cycle; alien call under lock; wait holding a second lock
7. Fake tryLock fix (retry without releasing) and lockstep livelock (no backoff)
8. Compound-operation race: safe methods, racy caller sequences
9. Permit theft / generation mixing: reused one-shot barrier, lapping threads
10. Double-selection: decide and decrement not atomic (two dispatchers, same waiters)
11. Boundary leak: per-thread permit re-issue instead of centralized
12. Torn counter: two "first" readers both acquiring the room; unguarded "last one" counts (delayed-detonation: corrupts the NEXT cycle)
13. Queue-empty false termination: in-flight work invisible
14. Claim-after-work: duplicate processing in the window
15. Silent worker death: no per-task catch; missing release/unlock on exception paths (`finally`)
16. Flag-only shutdown: parked workers never re-read your flag
17. Pool-exhaustion starvation: waiting on futures inside a fixed pool
18. sleep for coordination: deaf to earlier arrivals; also busy-waiting (burns CPU, and unlocked flags may never be seen)
19. Wall-clock arithmetic; trusting a timed wakeup without re-deriving
20. Starvation shipped without comment: writer behind endless readers; name who starves
21. Over-engineering: fine-grained/lock-free/extra primitives unprompted. Simple-but-correct wins.

## 13. Verifying and testing (concept: why testing concurrency is different)

A passing test proves almost nothing: the schedule that breaks you may not have run. So: (1) every solution needs a **correctness argument** (permit conservation, predicate partition, HB edges, the two-sentence lock-ordering proof): the argument is the deliverable, the test is a smoke check; (2) stress properly anyway: CountDownLatch start gate to maximize contention, many iterations, assert invariants, timeouts so hangs fail fast, both parities of N, skewed thread speeds (generation bugs only show under lapping); (3) never `sleep` as proof of coordination; (4) jstack for deadlock detection, jcstress as the extra-credit mention.

## 14. The interview ceremony (60-second recap)

Clarify (how many threads? repeated → reusable? block or fail-fast? fairness? shutdown?) → **classify** into one of the 7 families, out loud, by analogy ("this is H2O-shaped") → state the **invariant** and linearization point → name the **pattern/mechanic** → code from the template, simplest correct tool first → **verify aloud**: race, deadlock (incl. the non-obvious shapes), lost wakeup, starvation, cancellation/finally, lifecycle, one happy + one contention interleaving. Prepare your race-condition war story. When stuck, return to the one question: *who is allowed to proceed, and who tells them?*

---

---

**Where to go next:** the [pattern playbooks](/interview/multithreading/) for each family's full mechanics, and [the framework](/interview/multithreading/mt-framework/) for how to run the forty-five minutes.
