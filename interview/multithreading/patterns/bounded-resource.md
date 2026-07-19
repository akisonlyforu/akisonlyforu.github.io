---
layout: post
title: Bounded Resource & Producer-Consumer Playbook
date: 2026-07-19
description: >-
  Two parties on opposite predicates, the three ways to express it, block versus balk versus timeout, signaling discipline, and how consumer loops end.
categories: interview multithreading patterns
---

Deep dive on the bounded-resource family, companion to [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework/). Producer-consumer is the single most-asked concurrency question anywhere, and this family is its whole neighbourhood.

The family-level implementation strategy for every problem in this family. Read it once for the shape, then again after each problem to see the shape reappear. The claim this playbook defends: **every Type C problem is a guarded counter plus a decision about who waits, who signals, and what happens when the resource isn't there.** Master that sentence and the six problems here become one problem in six costumes.

---

## 1. The skeleton: two opposite wait conditions

The defining structure of this family is not "a queue". It is **two parties blocked on OPPOSITE predicates over the same state**:

- Producers wait on **notFull** ("there is space") and their action *creates* work.
- Consumers wait on **notEmpty** ("there is work") and their action *creates* space.

Draw it as a seesaw over one counter:

```
            size == 0                      size == capacity
  consumers wait here  <---- size ---->  producers wait here
            ^                                      ^
   a PRODUCER's add wakes them        a CONSUMER's remove wakes them
```

Each party's progress is exactly what unblocks the *other* party. That crossing is the heart of the pattern, and it immediately yields the signaling law of Section 4: **you signal the opposite party, never your own kind.**

Everything else in the family is a variation on this skeleton:

- Remove one side's bound (unbounded queue) → only one wait condition survives (semaphore, latch: Section 7).
- Change the wait policy to "don't wait" → balking (barbershop: Section 3).
- Make one waiter do something extra on a boundary condition → special roles (dining savages, barbershop: Section 5).
- Make the consumer an infinite loop you own → lifecycle/shutdown (thread pool: Section 6).

The **invariant** is always a range: `0 <= count <= capacity` (or `count >= 0` when one side is unbounded). The **linearization point** is always the single locked mutation of that count/structure. State both before coding. That's [Step 2](/interview/multithreading/mt-framework/).

---

## 2. Three expressions of the same skeleton

You can express "two opposite wait conditions" three ways. Learn all three ON THE BLOCKING QUEUE, because there they are purest; then everywhere else you just pick the one that fits.

### Expression A: single monitor + notifyAll (the baseline)

One lock, one wait set, everyone in the same room.

```
state: queue, capacity, one monitor

put(x):                          take():
  synchronized:                    synchronized:
    while (size == capacity)         while (size == 0)
      wait()                           wait()
    add x                            x = remove
    notifyAll()                      notifyAll()
                                     return x
```

Why `notifyAll` and never `notify` here: the ONE wait set contains BOTH producers and consumers. A `notify` can hand the wakeup to the wrong party, who re-checks its `while`, finds its own predicate still false, and goes back to sleep. The signal is consumed and gone. Under multiple producers AND consumers this ends with everyone asleep: the wrong-party hang (Section 10, F2). `notifyAll` is the blunt fix: wake everyone, let the `while` loops sort out who actually proceeds.

**Trade-offs**: simplest to write and defend; the default when asked to build from `synchronized`. Cost: thundering herd, every state change wakes all waiters, most of whom re-sleep. Fine at interview scale; name the cost.

### Expression B: Lock + two Conditions (the idiomatic upgrade)

Same lock, but two SEPARATE wait rooms, one per predicate.

```
state: lock, notFull = lock.newCondition(), notEmpty = lock.newCondition()

put(x):                          take():
  lock                             lock
    while (full)  notFull.await()    while (empty) notEmpty.await()
    add x                            x = remove
    notEmpty.signal()                notFull.signal()
  unlock (finally)                 unlock (finally)
```

Each room now contains only one kind of waiter, so `signal()` (wake ONE) is safe: whoever wakes, their predicate is the one that just became true. This is [Template 3](/interview/multithreading/mt-framework/) and the design inside `ArrayBlockingQueue`.

**Trade-offs**: precise signaling, no herd; slightly more API. The interview sentence that shows understanding: *"two wait sets remove the wrong-party problem, which is what makes single-signal safe."*

### Expression C: counting semaphores + mutex (the Little Book of Semaphores lens)

Stop *checking* the count. Make semaphores BE the count.

```
state: spaces = Semaphore(capacity), items = Semaphore(0), mutex, queue

put(x):                          take():
  spaces.acquire()                 items.acquire()
  mutex { add x }                  mutex { x = remove }
  items.release()                  spaces.release()
```

No explicit predicate anywhere: `acquire` on a zero semaphore IS "wait until my predicate holds", and `release` IS the cross-party signal. The two semaphores are the two wait conditions, reified as counters. The mutex only protects the structure itself.

**Trade-offs**: beautiful symmetry, permits persist (a release before anyone waits is never lost: no lost-wakeup class of bugs), and it composes naturally with special-role signaling (Section 5). Cost: the count now lives in the semaphores, so a compound predicate ("empty AND shop open") doesn't fit, so you fall back to A/B, or bolt a guarded counter alongside (as barbershop does). Also carries the strict ordering rule of Section 8.

**Choosing**: asked to hand-build with wait/notify → A. Two-plus distinct waiter kinds and free choice of tools → B. Signaling between distinct roles, rendezvous, or "the count IS the resource" → C. In a design round: none of the above. Reach for `ArrayBlockingQueue` and say so (anti-over-engineering rule 7).

---

## 3. The policy axis: what happens when the resource isn't there

Fix the guarded state; vary ONLY the reaction to "predicate false". Four policies:

| Policy | Shape at the check | Canonical example |
|---|---|---|
| **Block** | `while (!p) wait/await/acquire` | blocking queue, savages, pool workers |
| **Balk** | `if (!p) { unlock; return/leave; }` | barbershop customer at a full shop |
| **Timeout** | `awaitNanos` / `tryAcquire(t)` in the loop; give up when time's gone | `offer(x, timeout)`, lock with deadline |
| **Reject by state** | `if (shutdown) throw`: refusal keyed to lifecycle, not fullness | pool's submit-after-shutdown |

These are one-line variations on the same guarded check. Blocking vs balking is NOT two different designs, it's one design with a policy knob. Internalize that and half of LLD concurrency questions become policy discussions: "bounded queue, and when full we block / shed load / wait 50ms then shed, pick per requirement." Always ASK which policy the interviewer wants (Framework's 0–5 min questions); the barbershop exists precisely to stop you from reflexively blocking.

Timeout footgun worth naming: on timeout expiry you must re-check the predicate one last time under the lock (you may have been signaled and timed out simultaneously), and a timed-out semaphore path must not leak a permit it half-took.

---

## 4. Signaling discipline: who signals whom

Rules, in order of importance:

1. **Signal the opposite party.** A producer's add can only make *consumers'* predicate true; a consumer's remove can only make *producers'* predicate true. Signaling your own kind is at best noise, at worst (with `notify`) a stolen wakeup.
2. **Signal after mutating, while holding the lock.** The waiter's re-check must see the new state. Signal-before-mutate or signal-outside-the-critical-section lets a waiter wake, check stale state, and re-sleep just before the state actually changes.
3. **Signal exactly when a predicate may have become true.** Each state change, ask: "whose wait condition did I just possibly satisfy?" Signal them; nobody else. (Latch refinement: countDown only notifies at zero, the waiters' predicate can't become true any earlier.)
4. **One wait room → notifyAll. One-predicate-per-room → signal is safe.** This is Section 2's A-vs-B distinction as a rule.
5. **Cross-ROLE signaling wants semaphores.** When the parties are distinct roles rather than symmetric producers/consumers (savage→cook, customer→barber), a `Semaphore(0)` per directed signal is cleaner than shared conditions, because the permit persists if the receiver wasn't waiting yet (the doorbell chime that the barber hears even if it rang mid-cleanup). Semaphores having no ownership is exactly what permits this cross-thread use. A mutex can't do it.

---

## 5. Special-role waiters

Two problems in this family exist to teach one refinement each:

**The empty-discoverer (dining savages).** When a boundary condition (empty pot) needs a one-time reaction (wake the cook), exactly ONE thread must own that reaction: the thread that *observed the transition* under the mutex. Every other hungry savage must be excluded until the reaction completes. The Little Book of Semaphores mechanism: the discoverer signals the cook and then **waits on the refill semaphore while still holding the mutex**. Deliberately. The held mutex is what freezes all other savages during the refill, guaranteeing exactly one empty-observation → exactly one refill. This is a *disciplined exception* to "never block holding a lock": see Section 8 for why it's safe.

**The sleeping consumer (barbershop).** A consumer with nothing to consume isn't spinning or polling, it's parked on `customersReady.acquire()`, i.e., a semaphore whose count is the pending work. The "sleeping barber" is nothing more exotic than your blocking-queue consumer blocked on `notEmpty`, expressed as Expression C. The extra content in barbershop is the per-service **rendezvous**: work handoff isn't fire-and-forget, so each transaction adds a paired handshake (`barberReady` / `cutDone`) so that exactly one customer is in the chair and both sides observe completion.

The generalization: when a problem says "X does something special when Y happens" or "X sleeps until needed", you are still in the Type C skeleton, you're adding either a *unique-observer* (mutex-guarded transition detection) or a *directed wake-up* (a Semaphore(0) whose permits persist).

---

## 6. Lifecycle: how consumer loops end

The moment you own the consumer loop (thread pool workers), you own its death. A `running` flag alone is NOT shutdown: workers parked inside `dequeue()`'s wait never re-read your flag. Flipping it wakes nobody (Section 10, F8). You must *poke* the sleepers. Two mechanisms:

**Poison pills.** Enqueue N sentinel tasks for N workers; a worker that dequeues a pill exits its loop. The elegance: shutdown becomes ordinary queue traffic: no special signaling path, and FIFO means real tasks drain before the pills arrive, which is exactly graceful-shutdown semantics falling out for free. Count discipline matters: exactly one pill per worker.

**Interrupts.** `worker.interrupt()` each thread; the blocking `dequeue` throws `InterruptedException`, which the loop treats as "exit" (after checking whether to drain remaining work, per policy). More idiomatic Java (`shutdownNow` does this), works even when the queue is full (a pill can't be enqueued into a full queue, worth saying), but requires every blocking point in the loop to handle interruption coherently.

Either way, two invariants: **submit-after-shutdown is rejected under the same coordination as enqueue** (or a task slips in during the transition), and **the worker loop catches per-task exceptions**: an uncaught throw kills the worker and the pool silently shrinks (the #1 real-world pool bug).

---

## 7. Building the primitives themselves

When the problem is "implement Semaphore / CountDownLatch / CyclicBarrier", nothing new appears: each is the [Template 1](/interview/multithreading/mt-framework/) condition loop wrapped around one counter, with a different predicate. This family degenerates gracefully: a one-sided bound is just the skeleton with one party's wait condition deleted.

| Primitive | Guarded state | Wait predicate | Signal moment | Extra idea |
|---|---|---|---|---|
| Semaphore | `permits` | `permits > 0` | every release | no ownership; barging |
| CountDownLatch | `count` (one-way ↓) | `count == 0` | at zero only | one-shot → trivially safe |
| CyclicBarrier | `count` + **generation** | `myGen == currentGen` still | on round completion | **the generation problem** |

**Semaphore**: `acquire` = lock; while permits == 0 wait; permits--. `release` = lock; permits++; notifyAll. Fifteen lines. The content is the semantics: no ownership check (that's what enables cross-thread signaling and distinguishes it from a mutex), and barging: a fresh caller can grab the permit before a parked waiter wakes, which is precisely why the `while` is load-bearing.

**Latch**: same loop, predicate `count == 0`, and the state moves in one direction only. One-way state is why it's trivially safe: there is no "wrong round" for a waiter to be confused about.

**Barrier, the generation problem**: the naive reusable barrier ("n-th arrival resets count and notifyAlls") is broken. A fast released thread re-awaits for round k+1 and increments `count` *before* a slow round-k thread has woken; the slow thread's re-check `while (count < n)` now reads round-k+1's counter. Waiters from two rounds are indistinguishable and can hang or release early. The fix is to change the QUESTION the waiter asks: capture a **generation token** on entry and wait `while (myGen == currentGen)`; the n-th arrival flips the generation, resets count, notifyAlls. "Has my round ended?" cannot be confused across rounds even in one shared wait set. This is the same reasoning as the Little Book of Semaphores' two-turnstile barrier and why [Template 4](/interview/multithreading/mt-framework/) warns that a bare count+gate is one-shot.

The meta-lesson: after building these, "which primitive do I need?" becomes "which guarded state and predicate do I need?" Which was the real question all along.

---

## 8. Ordering: semaphore-acquire before mutex

In Expression C and its relatives there is a strict ordering rule:

**Acquire the counting semaphore BEFORE the mutex.** The counting acquire is where you may block for a long time; the mutex protects a short structural mutation.

The deadlock the other way, concretely (blocking queue, mutex-first producer):

1. Producer takes `mutex`, then blocks on `spaces.acquire()` (queue is full).
2. The only threads that will ever `spaces.release()` are consumers, inside their `mutex { remove }` section.
3. Consumers block on the mutex the producer holds. Producer waits on consumers; consumers wait on producer. Frozen.

The underlying rule is more precise than "never block holding a lock": **never block on a signal whose provider needs the lock you hold.** State it that way, because dining savages deliberately breaks the naive rule and is still correct: the empty-finder blocks on `fullPot` while holding the savages' mutex, and it is safe *only because the cook (the sole releaser of `fullPot`) never touches that mutex*. Verify the dependency chain explicitly: who releases what I'm blocked on, and do they need anything I hold? If no path leads back to you, blocking while holding is legal (and in savages, it's the mechanism: the held mutex is what excludes other savages during the refill).

---

## 9. The derivation recipe

Apply in order; each step is one or two sentences out loud.

1. **Name the resource and its counter(s).** Write the range invariant: `0 <= count <= capacity` (or one-sided). Name the linearization point (the locked mutation).
2. **List the parties and each party's wait predicate.** "Producers proceed when …; consumers proceed when …". Two opposite predicates → full skeleton. One predicate → degenerate case (primitive-building, Section 7).
3. **Choose the unavailable-policy per operation** (Section 3): block / balk / timeout / reject-by-state. Ask the interviewer; don't assume block.
4. **Pick the expression** (Section 2): monitor+notifyAll (hand-build baseline), Lock+2 Conditions (2+ waiter kinds, precise signaling), semaphores+mutex (count-is-the-resource, cross-role signaling, rendezvous). Or the JDK class, in a design round.
5. **Write each operation as: counting-acquire → lock → check/mutate → signal → unlock.** Ordering rule: counting acquire before mutex, UNLESS blocking-while-holding is deliberate and you can prove the releaser never needs your lock (Section 8).
6. **Assign signals.** For every state change: "whose predicate may have just become true?" Signal exactly them: the opposite party, never your own kind. Directed role-to-role signals get a `Semaphore(0)` each.
7. **Check for special structure.** Exactly-one-observer of a boundary transition (discoverer)? Waiters reused across rounds (generation token)? Paired handoff (rendezvous handshake)? Owned consumer loops (shutdown: pills or interrupts + per-item catch)?
8. **Run the failure-mode catalog** (Section 10) as your Step-5 verification.

---

## 10. Failure-mode catalog

Run these as a checklist; each is a one-line test.

- **F1: Lost wakeup.** Signal fired when no one was waiting *and the signal doesn't persist* (flag protocols, condition signal with no waiter). Cure: check-then-wait atomically under the lock, or use semaphores, whose permits persist by construction.
- **F2: Wrong-party notify hang.** Single monitor + `notify` with two waiter kinds: the wakeup lands on a thread whose predicate is still false; it re-sleeps; the signal is consumed. Everyone ends up asleep. Cure: `notifyAll`, or two Conditions. Be able to narrate the 2-producer/2-consumer interleaving cold.
- **F3: `if` instead of `while`.** Between signal and wake, a barging or racing peer can re-falsify the predicate (steal the permit, take the last chair); spurious wakeups exist too. The woken thread must re-check. `while`, always.
- **F4: Mutex-before-counting-acquire deadlock.** Section 8's inversion: blocked on a semaphore whose releaser needs your mutex. Cure: acquire-then-lock ordering, or the explicit dependency-chain proof when holding is deliberate.
- **F5: Counter races minting phantom resources.** Any count updated outside the lock, or by the wrong thread at the wrong time: two savages both seeing `servings == 0` (mutex released before the refill wait) → double refill, M extra servings; the customer decrementing `waiting` instead of the barber → transient undercount lets the shop over-admit; check-then-act on fullness without the mutex → two customers in the last chair. Cure: every read-modify-write of the counter under the lock, and pin *which thread* is allowed to make each transition.
- **F6: Signal before/outside the state change.** The waiter wakes, sees stale state, re-sleeps, then the state changes with no further signal. Cure: mutate, then signal, lock held.
- **F7: Generation mixing.** Reused barrier where waiters watch the count: round-k+1 arrivals corrupt the predicate round-k sleepers re-check. Passes light testing; dies under contention with immediate re-await. Cure: generation token; wait on "has my round ended?".
- **F8: Flag-only shutdown.** Consumers parked inside a blocking take never re-read your flag. Cure: poison pills or interrupts (Section 6).
- **F9: Unhandled per-item failure in a consumer loop.** One throwing task kills the worker; the pool silently shrinks to zero. Cure: per-task catch inside the loop.
- **F10: Swallowed interruption / leaked lock or permit on exception.** Blocking calls propagate `InterruptedException`; every lock release in `finally`; a failed multi-step acquire returns what it half-took.

---

## 11. Validation against all problems

The recipe of Section 9, applied to each problem in this family.

### Bounded blocking queue

(1) Resource = queue slots, `0 <= size <= capacity`, linearization = locked add/remove. (2) Two parties, opposite predicates: the skeleton verbatim. (3) Policy: block on both sides (the problem says so). (4) All three expressions apply; this is THE problem to do all three on. (5) Expression C shows acquire-before-mutex; A/B are lock-then-check. (6) Producer signals `notEmpty`, consumer signals `notFull`: cross-party, textbook. (7) No special structure. (8) F2, F3, F4, F6, F10 are exactly this problem's pitfall list. **Recipe fits with nothing left over. As it should: this problem is the trunk.**

### Thread pool from scratch

(1) Resource = queued tasks (and, per policy, queue capacity). (2) Callers produce, N identical workers consume: skeleton, with the queue typically delegated to the previous problem. (3) Policy: submit blocks (bounded → backpressure) AND rejects-by-state after shutdown, two policies coexisting on one operation; the recipe's "per operation" phrasing covers this. (4) Expression: reuse the blocking queue; the pool adds no new coordination expression. (5–6) Inherited from the queue. (7) This problem lives in step 7: owned consumer loops → per-task catch, and shutdown → N pills or interrupts; shutdown check under the same coordination as enqueue. (8) F8, F9, F10 dominate. **Recipe fits; steps 5–6 are pass-through because the queue is a component. The recipe correctly localizes the new work in step 7.**

### Implement a semaphore

(1) Resource = permits, `permits >= 0`, one-sided (no upper bound). (2) One party waits (`permits > 0`); releasers never wait: the degenerate one-predicate case the recipe names in step 2. (3) Block; `tryAcquire` is the balk variant, one line of policy on the same guarded state, exactly Section 3's claim. (4) Monitor + notifyAll (mandated: you're building the primitive; Expression C would be circular). (5) Single lock, no counting-acquire: step 5 degenerates to lock→check→mutate→signal. (6) Release signals acquirers; with one waiter kind, `notify` is *defensible*: the recipe's "signal exactly whose predicate became true" explains both why it works here and why it breaks the moment a second condition shares the monitor. (7) No ownership check is a *feature* to state, not structure to build. (8) F3 (barging is the vivid `while` justification), F5. **Recipe fits in degenerate form; the degenerate path had to be explicit in step 2 for this to be smooth, and it is.**

### Implement latch or barrier

Latch: (1) `count`, one-way down. (2) One predicate, `count == 0`. (3) Block. (4) Monitor. (6) notifyAll at zero only (signal exactly when the predicate can become true). Trivial by the recipe, correctly so; the problem file says the same. Barrier: identical until step 7, where "waiters reused across rounds?" fires → generation token, predicate becomes `myGen == currentGen`. (8) F7 is the entire problem. **Recipe fits; step 7's generation prompt is what separates the easy half from the hard half, matching the problem's own framing that "the gap between them is the lesson."**

### Dining savages

(1) Resource = servings, `0 <= servings <= M`; refill only at zero, adds exactly M. (2) Savages wait on "servings > 0 (or refill done)"; the cook waits on "pot empty": opposite predicates between asymmetric roles. (3) Block. (4) Cross-role directed signals → Expression C flavor: `emptyPot`, `fullPot` as Semaphore(0)s plus a guarded counter. (5) **The stress test for the recipe**: the empty-finder blocks on `fullPot` while HOLDING the mutex. The naive rule would forbid it; the recipe's step-5 escape clause requires the proof "the releaser (cook) never touches my mutex", which holds, and holding the mutex is the mechanism that freezes other savages. (6) Only the discoverer signals the cook, "signal exactly whose predicate became true", where the observation is mutex-guarded so exactly one thread makes it. (7) Empty-discoverer special role, named in step 7. (8) F4's refined form and F5 (double refill) are the pitfall list. **Recipe fits, but only because step 5 carries the dependency-chain exception rather than a flat "never block holding a lock". An earlier draft with the flat rule failed here and was fixed.**

### Barbershop

(1) Resource = places in the shop, `customers <= n+1`, guarded `waiting` counter. (2) Customers wait for the barber; the barber waits for customers: opposite predicates, asymmetric roles. (3) **Policy: balk**, the full shop turns customers away; the recipe's step 3 forces this question before coding, which is the problem's core trap for blocking-queue-reflex candidates. (4) Cross-role signaling + persistence requirement (the chime must not be lost if the barber is mid-cut) → semaphores: `customersReady`, plus rendezvous pair `barberReady`/`cutDone`. (5) Counting waits and the mutexed counter don't nest here (customer releases the mutex before chiming): no ordering hazard, but the balk path MUST release the mutex before leaving (F10's leak family). (6) Customer→barber chime; barber→customer call-up and completion: every signal directed at the party whose predicate changed. (7) Sleeping consumer = parked-on-semaphore barber; paired handoff → rendezvous handshake, both prompted by step 7. (8) F1 (why the flag protocol loses the chime and the semaphore doesn't), F5 (unlocked full-check; `waiting--` in the wrong thread). **Recipe fits; steps 3 and 7 do the problem-specific work, which is exactly where this problem's teaching content lives.**

**Verdict**: the recipe derives all six solutions without dead steps. Two refinements were load-bearing and are baked in above: the explicit degenerate one-predicate path in step 2 (semaphore/latch), and the dependency-chain exception in step 5 (savages).

---

## What the general framework leaves out

The 5-step framework classifies these problems correctly (Type C → multiplex + condition waiting), and Templates 1 and 3 are Expressions A and B of this playbook. For this category it is *nearly* sufficient. Gaps a candidate would feel:

1. **The policy axis is a clarifying question, not a design dimension.** The framework's 0–5 min list asks "must waiting block or fail fast?", but offers no template or vocabulary for balk/timeout/reject as one-line variants of the same guarded check. Barbershop punishes this gap. (Section 3 here.)
2. **The "never block holding a lock" heuristic (Step 5, check 2) is too blunt for this family.** Dining savages is CORRECT and deliberately blocks while holding the mutex; the framework's checklist would flag it as a deadlock smell with no way to discharge the flag. The refined rule, "never block on a signal whose provider needs a lock you hold; otherwise prove the dependency chain", is missing. (Section 8.)
3. **Signaling discipline is implicit.** "Signal the opposite party, never your own kind" and "signal after mutating, under the lock, exactly to whoever's predicate became true" are visible in Template 3's code but never stated as rules; the wrong-party notify hang is the single most instructive failure in the family and the framework doesn't name it. (Section 4.)
4. **No shutdown mechanism for hand-built consumer loops.** Step 5's lifecycle check asks the right question ("who owns the executor, how is it shut down") but gives no mechanism when YOU are the executor: poison pills vs interrupts, flag-only shutdown as a named bug, per-task catch. Thread-pool-from-scratch needs all three. (Section 6.)
5. **The generation problem is warned about but not taught.** Template 4's note says a bare count+gate barrier is unsafe to reuse; there is no recipe for the fix (capture a generation token; wait on "has my round ended?"). Implement-a-barrier requires it. (Section 7.)

None of these invalidate the framework. They are Type-C-specific depth that this playbook supplies beneath it.
