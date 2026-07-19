---
layout: post
title: Time-Based State Playbook
date: 2026-07-19
description: >-
  State as a function of time, lazy derivation versus wait-until, enlarged derive-then-act atomicity, monotonic clocks, and the timed-wait loop.
categories: interview multithreading patterns
---

Deep dive on the time-based family, companion to [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework/). Rate limiters, schedulers and expiring caches all sit here, and they share one design fork, one atomicity rule and one clock rule.

The family where **time is an input to your state**, not just something that passes while threads contend. Three problems live here (token bucket rate limiter, delayed task scheduler, read-heavy cache with expiry), and they share one design fork, one enlarged-atomicity rule, one clock rule, and one wait mechanic. Learn those four things and every problem in the family is an application, not a new problem.

---

## 1. The defining move: state as a function of time

In an ordinary Type B problem, state changes only when a thread changes it. Here, state changes **by itself, continuously**: tokens accrue as seconds pass; cache entries silently rot; a scheduled task becomes runnable the instant its due time arrives. The naive reading of "changes by itself" is "so I need a thread to keep it up to date": a refiller topping up tokens, a reaper sweeping expired entries. That instinct is the trap this whole category sets.

The senior move is **lazy derivation**: don't maintain time-dependent state: *derive* it at the moment someone looks. Store a base value plus the timestamp of the last derivation, and on each access compute what the state *is now*:

- **Lazy refill** (rate limiter): store `(tokens, lastRefill)`; on tryAcquire compute `tokens = min(capacity, tokens + (now − lastRefill) × rate)`, then decide.
- **Lazy expiry** (cache): store `(value, expiresAt)` in the entry itself; on get, one comparison `now < expiresAt` tells you if it's fresh. No patrol.

Why lazy wins, every time it applies (rehearse this list, it's the answer to "why no background thread?"):

1. **No lifecycle.** A refiller/reaper thread must be sized, owned, started, and shut down; lazy derivation is just arithmetic on the caller's thread.
2. **No second writer.** A maintenance thread races the request threads: you'd need locking to protect state *from your own helper*.
3. **Exact accounting.** A thread ticks at some granularity; derivation is exact to the nanosecond at the moment of the read.
4. **Zero idle cost.** Nobody asks, nothing computes.

### When a thread IS required: wait-until

Lazy derivation has one hard limit: it only fires **when someone accesses the state**. If the requirement is that something *happens at a future moment even though nobody is calling in* (run this task in 60 seconds), then derivation on read cannot help, because there is no read. Someone must be awake (or asleep with an alarm set) at the due moment. That's the scheduler: the one problem in the family that legitimately owns a worker thread, and that worker does a **timed condition wait**, not a poll (Section 5).

So the family's fork is:

- Time changes **what the state IS** when observed → **derive-on-read**, no thread.
- Time changes **WHEN an action must fire**, unprompted → **wait-until**, a waiting thread with a timed wait.

And they compose: a *blocking* `acquire()` on the rate limiter is derive-on-read plus a bounded wait-until ("derive; not enough; compute the deficit's ETA; awaitNanos that long; re-derive").

---

## 2. Enlarged atomicity: derive-then-act is ONE step

Every Type B problem has check-then-act. This family enlarges it: the check is now a *computation* (derive current state from the clock) and the derivation, the decision, and the mutation must be **one atomic step**.

The canonical race (rate limiter, 1 token left): thread A derives tokens = 1 outside any lock; thread B derives tokens = 1; both see "≥ 1", both consume, two requests pass on one token. The bug isn't a torn read: it's that *derivation and consumption were separated*. Both go inside the lock; the linearization point is the locked (or CAS-committed) derive-and-consume.

**The pair rule.** The state is `(value, timestamp)` and they must change *together*. Two separate atomics, `AtomicLong tokens` + `AtomicLong lastRefill`, are broken no matter how clever the ordering: between your update of one and the other, a second thread reads a value paired with the wrong timestamp and derives garbage (double-counted elapsed time → minted phantom tokens, or vice versa). Each field is individually atomic; the *invariant spans both*, so the atomicity must too. Hence the two correct shapes:

1. **One lock around the whole derive-then-act** (~6 lines of arithmetic under `synchronized`). Ship this in the interview; uncontended locks are cheap, say it, don't apologize.
2. **CAS on an immutable snapshot**: an `AtomicReference<(value, stamp)>`; read snapshot → compute successor → compareAndSet → retry on failure. The immutable pair is *why* this works: the reference swap changes both fields indivisibly. Know the shape as the lock-free follow-up; don't lead with it.

**The read-only exception** (this is why the cache's hot path needs no lock): if the derivation is a *pure comparison against an immutable snapshot* and success requires **no mutation** (a fresh cache hit reads `(value, expiresAt)`, compares, returns), then there is nothing to make atomic *with* the check. Enlarged atomicity is required exactly when the act **mutates state based on the derived value** (consume a token, replace a stale entry). Fresh hit: lock-free. Stale refresh: back to atomic arbitration (`replace(key, stale, freshFuture)`, one winner).

---

## 3. Clock discipline: monotonic time or bust

`System.currentTimeMillis()` is **wall clock**: NTP corrections, leap smearing, and manual changes make it jump, backward or forward. A backward jump makes elapsed time negative (token drought, entries un-expiring, tasks never due); a forward jump mints a token burst or expires everything at once. `System.nanoTime()` is **monotonic**: meaningless as an absolute date, guaranteed non-decreasing within a JVM, exactly right for *intervals*, which is all this family ever computes (elapsed since refill, remaining until due, remaining until expiry).

Rule: **all interval arithmetic on nanoTime; wall clock only for human-facing absolute times or values crossing process boundaries** (a due date serialized to a DB). And because nanoTime can be any value including negative, compare with subtraction (`t2 − t1 > 0`), never `t2 > t1` raw across wrap.

Why this recurs in *every* problem here: the family's defining move puts a clock read inside your invariant. Any problem whose correctness depends on `now` inherits the question "which now?", so saying "nanoTime, because wall clock jumps" unprompted is the cheapest senior marker in the category. You will say it three times across the three problems; that's not repetition, that's the pattern.

---

## 4. Rate vs concurrency: why this is not a semaphore

The classification error interviewers watch for. A `Semaphore(n)` caps **concurrency** (how many are *inside at once*) and its permits are recycled: whoever finishes *releases* one. A rate limiter caps **frequency** (how many *per second*) and nobody releases a rate token: **time mints new ones**. A semaphore has no clock; grafting one on (a thread releasing permits every 100ms) just rebuilds the background-refiller you rejected in Section 1, with tick granularity and a lifecycle to own.

One sentence, delivered early: *"A semaphore caps how many at once; a rate limiter caps how many per second (permits are returned by threads, tokens are minted by time), so this is guarded state plus a clock, not a multiplex."* This is the framework's Step 1 footnote made concrete; own it.

---

## 5. Timed condition waits, mechanically

The wait-until branch needs a thread that sleeps *exactly until the earliest due moment* but can be woken *early* if the situation changes. That is `Condition.awaitNanos(delay)` inside a lock, never `Thread.sleep`.

**Why never sleep for coordination** (the central bug of the scheduler question): a thread in `sleep(60s)` holds no lock and listens on no condition. When a task due in 1s arrives, there is *no way to tell the sleeper*: the new task runs up to 59s late. `awaitNanos` releases the lock while waiting and is signalable: same nap, but with a doorbell.

**The three wake reasons.** A thread returning from `awaitNanos` knows *nothing* about why it woke:

1. **Timeout elapsed:** the head *may* now be due (or may have been replaced meanwhile).
2. **Signaled:** someone changed the world, probably a new earlier head.
3. **Spurious wakeup, or lost race:** the JVM woke it for no reason, or (N workers) a sibling already polled the head.

All three get the identical treatment: **re-peek, recompute delay from the clock, decide again**. Never branch on *why* you woke; the loop makes the reason irrelevant. This is Template 1's `while`-not-`if` discipline with three wake reasons instead of one: the interviewer is checking whether your condition-loop reflex survives adding a clock.

**Preemption by earlier arrival: signal on head change.** When `schedule()` inserts a task, the napping worker's alarm is set for the *old* head's due time. If the new task becomes the new head (earlier than everything), that alarm is now too long: `signal()` to force a re-inspection. If it's not the new head, the current alarm is still correct, signaling anyway is *safe* (the loop re-checks and re-naps) but wasteful; signal-always vs signal-on-head-change is a one-line trade-off worth saying aloud.

**Run work outside the lock.** The lock protects the queue, not the task. `poll` under the lock (atomic claiming: this is also why N workers need no extra code), `run()` outside it; holding the lock through `run()` freezes all scheduling and every other worker for the task's duration.

---

## 6. Per-key time-based state

Both derive-on-read designs scale out per key without new machinery, because the state is *self-contained*, a `(value, timestamp)` unit that carries everything needed to derive itself:

- **Map of limiters**: per-client rate limiting is `ConcurrentHashMap<ClientId, RateLimiter>` + `computeIfAbsent`, its atomicity guarantees one limiter per client; each limiter is independently locked, so clients never contend with each other.
- **Cached futures with self-contained expiry**: the cache entry is `Future<(value, expiresAt)>`; `computeIfAbsent(key, k -> new FutureTask(loader))` guarantees one loader per key per generation (single-flight: the waiting point exists *before* the value does, which is what the dogpile losers block on), and the expiry riding inside the entry means a fresh hit needs nothing but the entry itself. Create the FutureTask *inside* the compute lambda, **run it outside**: long work inside the lambda blocks the CHM bin.

The common thread: no global registry of "what expires when," no global lock. Each unit derives its own state; the map's per-bin atomicity is your striping. (A reaper thread would need exactly the global view you just avoided.)

---

## 7. Pseudocode skeletons

**Skeleton A: lazy derivation (derive-on-read):**

```
state: snapshot(value, stamp)          // ONE unit; the pair changes together

tryAct():
  atomically {                          // lock, or CAS-loop on immutable snapshot
      now     = monotonicNow()
      derived = advance(value, now - stamp)      // e.g. min(cap, value + elapsed*rate)
                                                 // or: fresh = (now < stamp+ttl)
      if canAct(derived):
          state = (consume(derived), now)        // act + derivation commit together
          return ALLOW
      else:
          state = (derived, now)                 // still bank the derivation
          return REJECT
  }
```

`advance` must **clamp** (`min(capacity, …)`) and keep **fractional precision** (double, or nano-token integer units). If `canAct` needs no mutation on success (pure freshness check on an immutable entry), the atomic block collapses to a plain read: Section 2's exception.

**Skeleton B: timed-wait loop (wait-until):**

```
worker():
  lock
  loop:
      if heap.isEmpty():        available.await()
      else:
          head  = heap.peek()                    // min-heap by dueTime
          delay = head.due - monotonicNow()
          if delay > 0:         available.awaitNanos(delay)   // NOT sleep
          else:
              task = heap.poll()                 // claim atomically, under lock
              unlock;  task.run();  relock       // NEVER run under the lock
  // every wake, whatever the reason: fall through to re-peek + recompute

schedule(task):
  lock
  heap.offer(task)
  if task == heap.peek():       available.signal()   // head changed: preempt the nap
  unlock
```

---

## 8. Derivation recipe: from time requirement to design

Given a problem where time appears in the requirements, run these steps:

1. **Locate the time dependency.** Ask: does time change *what the state is* when someone looks (quantity accrues, validity decays)? Or does time dictate *when an action fires*, even with no caller present? First → derive-on-read. Second → wait-until. Both → compose (blocking acquire).
2. **Derive-on-read branch:** define the snapshot `(value, timestamp)` and the pure function `advance(value, elapsed)`. Write the invariant over the *derived* value ("derived tokens ∈ [0, cap]; success consumes exactly 1").
3. **Wait-until branch:** order pending work by due time (min-heap), give the waiter `awaitNanos(headDue − now)` in a re-check loop, and make every producer **signal when the head changes**. Claim under the lock; run outside it.
4. **Choose the atomicity vehicle.** Does acting on the derived value *mutate* state? Yes → one lock around derive-then-act (default), or CAS on the immutable snapshot (follow-up). No (pure read of an immutable unit) → lock-free read. Per-key state → CHM + computeIfAbsent, one self-contained unit per key; expensive per-key work → cache a Future so the waiting point precedes the value.
5. **Fix the clock.** nanoTime for every interval; wall clock only at human/persistence boundaries. Say it unprompted.
6. **Sweep the failure catalog** (Section 9) as your Step-5 verification, alongside the standard race/deadlock/lost-wakeup checklist.

---

## 9. Failure-mode catalog

| # | Failure | Mechanism | Fix |
|---|---------|-----------|-----|
| 1 | **Unlocked derive-then-consume** | Two threads both derive "1 token", both consume → double-allow. Also: two *separate* atomics for value and stamp: pair read torn, elapsed time double-counted. | Derivation + decision + mutation in one lock/CAS; snapshot the pair as one unit. |
| 2 | **Wall-clock arithmetic** | NTP jump → negative or inflated elapsed → token bursts/droughts, mass un-/re-expiry, tasks never due. | nanoTime for all intervals. |
| 3 | **sleep() for coordination** | Sleeper is deaf: a newer, earlier task can't preempt it → runs late by up to the old delay. Same bug via a second door: `schedule()` that forgets to signal on head change. | awaitNanos in a re-check loop + signal-on-head-change. |
| 4 | **Missing capacity clamp** | Idle bucket accrues unbounded tokens → an hour of idle becomes an unbounded burst. | `min(capacity, …)` inside `advance`. |
| 5 | **Fractional-token truncation** | Integer token math with fractional rates silently drops fractions → delivered rate < configured rate. | Accumulate as double or in nano-token units. |
| 6 | **Running tasks/loaders under the lock** | Task under the scheduler lock freezes all scheduling; loader inside computeIfAbsent blocks the CHM bin (and re-entrant map calls can deadlock). | Claim/create under the lock; execute outside. |
| 7 | **Trusting the wakeup** | Treating awaitNanos return as "my task is due", but the wake had three possible causes. | Re-peek and recompute from the clock, always. |
| 8 | **Gratuitous background thread** | Refiller/reaper adds lifecycle, a second writer, and tick granularity for zero benefit. | Lazy derivation; add maintenance only for demonstrated memory pressure (then: Caffeine). |

---

## 10. Validation against all problems

### 10.1 Rate limiter (token bucket)

Recipe step 1: time changes *what the state is* (the token quantity accrues with elapsed time) and tryAcquire is caller-driven. **Derive-on-read**, no thread (matches the problem's own hint: "no background refill thread"). Step 2: snapshot `(tokens, lastRefill)`; `advance = min(capacity, tokens + elapsed × rate)`; invariant over derived tokens. Step 4: success consumes a token (mutation) so lock around derive-then-consume (Skeleton A verbatim); CAS-on-immutable-pair as the stated follow-up, and the pair rule explains why two atomics are broken. Step 5: nanoTime, exactly the strategy section's pitfall 1. Catalog hits 1, 2, 4, 5, 8: all five of the strategy's pitfalls. Follow-ups fall out of the pattern: blocking acquire = compose with a bounded wait-until (Section 1); per-client = map of limiters (Section 6). **Recipe fits with nothing left over.**

### 10.2 Delayed task scheduler

Recipe step 1: time dictates *when* (tasks must fire with no caller present), so **wait-until**: this is the family's one legitimate thread owner. Step 3 produces the strategy section's entire design: min-heap by dueTime, awaitNanos re-check loop, signal-on-head-change for the preempting earlier arrival, poll-under-lock (which is also why N workers need no extra code), run-outside-lock. The three wake reasons (Section 5) are the strategy's "re-check loop is load-bearing" point, itemized. Catalog hits 3, 6, 7 plus busy-polling (a variant of 3), covering all five of the strategy's pitfalls. Step 5: nanoTime again, as the strategy's "it recurs" remark predicts. **Recipe fits; the wait-until branch was written for this problem and validates cleanly.**

### 10.3 Read-heavy cache with expiry

Recipe step 1: time changes *validity* (entries decay) and gets are caller-driven: **derive-on-read** (lazy expiry, no reaper, the problem's own constraint). Step 2: the snapshot is the entry `(value, expiresAt)`; `advance` degenerates to a freshness comparison. Step 4 is where this problem stress-tested the recipe: a fresh hit *mutates nothing*, so the read-only exception (Section 2) applies and the hot path is lock-free. The initial recipe draft demanded a lock around every derive-then-act, which would have contradicted the read-heavy requirement; step 4 now asks "does acting mutate?" first, and the recipe validates. The stale path *does* mutate (replace the entry) → atomic arbitration via `replace(stale, freshFuture)`, one winner per expiry generation. Per-key + dogpile = Section 6's cached-future idiom exactly (single-flight, waiting point before the value, run FutureTask outside the compute). Catalog hits 1 (compose-then-act on CHM), 2/6 (wall clock, loader in the lambda), 8 (reaper); the loader-failure pitfall (remove failed futures) is cache-specific policy, rightly living in the problem doc rather than the pattern. **Recipe fits after the step-4 mutation-question refinement, already folded in above.**

---

## What the general framework leaves out

The 5-step framework spends one sentence on this family (Step 1: "Time-based designs … combine Type B with a clock. A semaphore caps concurrency; it does not by itself enforce a rate per second."). Honest assessment: **that sentence is correct but not sufficient**: it classifies, but every load-bearing mechanic of the category lives outside the framework:

1. **No classification row / no design fork.** Time-based problems span Types B, C, and E as a *modifier*; the derive-on-read vs wait-until fork (the family's first design decision) appears nowhere. Deserves either its own row or a footnote upgraded to the fork.
2. **No timed-wait template.** Template 1 teaches the untimed `while + wait` loop; `awaitNanos` + re-check with three wake reasons + signal-on-head-change is a genuinely different (and heavily interviewed) shape: Skeleton B should be a Template 5.
3. **Step 3's seven patterns don't contain lazy derivation.** "State as a function of time, computed on read" is a mechanic of the same rank as Lightswitch or Turnstile; a candidate mapping token bucket onto pattern 3 (Mutex) gets the lock right but misses the insight the question exists to probe.
4. **Step 5's checklist has no clock item.** "Which clock, and what happens if it jumps?" and "does any timed waiter trust its wakeup?" belong beside race/deadlock/lost-wakeup: they are this family's equivalents.
5. **Toolbox omission (minor).** `DelayQueue` / `ScheduledThreadPoolExecutor` are the "prefer the standard utility" answers here, as `BlockingQueue`/`ExecutorService` are for Type C/F; worth a line in Step 0.

None of these invalidate the framework: invariant, linearization point, and condition-loop discipline carry over intact (the linearization point *is* the locked derive-then-act; the `while` discipline *is* the re-check loop). The gap is that the framework under-weights a category that contains the most-asked hybrid question. This playbook is the missing chapter.
