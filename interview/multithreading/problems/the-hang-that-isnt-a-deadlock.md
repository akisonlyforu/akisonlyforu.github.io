---
layout: post
title: The Hang That Isn't a Deadlock, a lost wakeup
date: 2026-07-19
description: >-
  Candidates reflexively say "deadlock" for any hang, and the dump in this problem is specifically constructed to punish that. Distinguishing *BLOCKED* (someone holds what I…
categories: interview multithreading problems
---

Part of the [Debugging & Code Review](/interview/multithreading/patterns/debugging-and-code-review/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Uber L5+ and Airbnb debugging rounds; the shape appears in any team that hand-rolled a queue before `ArrayBlockingQueue` existed. Frequently presented as *"here is the jstack output, what's wrong?"*

### The code under review (described, not shown)

A hand-rolled `WorkBuffer` used as the hand-off between an ingest tier and a processing tier.

Its state, all guarded by the object's own monitor (every method is `synchronized` on `this`):

- An `ArrayDeque<Task>` field `items`.
- An `int` field `capacity`, final after construction.

Its methods:

- `put(task)`, if `items.size() == capacity`, it calls `wait()` **inside an `if`, not a loop**. After the `if`, it adds the task to the deque and calls `notify()`.
- `take()`, if `items.isEmpty()`, it calls `wait()` **inside an `if`, not a loop**. After the `if`, it removes and returns the head, then calls `notify()`.
- `size()`, returns `items.size()`.

Both `put` and `take` catch `InterruptedException` around the `wait()` and log it at DEBUG level, then continue.

Eight producer threads and eight consumer threads share one `WorkBuffer` with capacity 16.

### The observed symptom

- The pipeline runs normally for anywhere from four minutes to nine hours, then **stops completely**. No exception, no error log, no OOM.
- CPU on the box drops to essentially zero. Memory is flat. The process is alive and responds to health checks that don't touch the buffer.
- `jstack` shows all sixteen worker threads. **Every one of them is in state `WAITING`**, parked at `Object.wait()` inside either `put` or `take`. Not one thread is `BLOCKED`.
- The JVM's thread dump contains **no `Found one Java-level deadlock` section**. Three dumps taken ten seconds apart are byte-identical apart from timestamps.
- It never reproduces with one producer and one consumer. It reproduces roughly ten times more often when the consumers are artificially slowed down.
- A previous on-call "fixed" it by adding a watchdog that restarts the pipeline every hour.

### Your task

1. From the dump alone, all `WAITING`, none `BLOCKED`, no deadlock section, say what class of bug this is and why it is *not* a deadlock.
2. Find the defects in the described code. There is more than one, and they interact.
3. Narrate the exact interleaving that reaches the frozen state, with a concrete thread count.
4. Say how you would reproduce it deterministically, and how you would confirm.
5. Give the fixes and say which you would ship.

### Clarify before diagnosing

- Are there multiple producers *and* multiple consumers? (With one of each, the wrong-party wakeup can't happen, which is exactly why it never reproduces at 1:1, and that fact is evidence, not noise.)
- Does anything else call `notify` on this object, or `wait` on it from outside? (Alien waiters on a shared monitor make `notify` unsalvageable.)
- Is the buffer ever interrupted for shutdown? (The swallowed `InterruptedException` is a separate defect with its own symptom.)
- Does the deque ever get elements added by any path other than `put`? (Any unsignalled state change is a lost wakeup of its own.)

### Why this problem matters

Candidates reflexively say "deadlock" for any hang, and the dump in this problem is specifically constructed to punish that. Distinguishing *BLOCKED* (someone holds what I want, contention or a lock cycle) from *WAITING* (nobody holds anything; I am simply waiting to be told) is the single highest-leverage diagnostic skill in the round, because it splits the entire hang hypothesis space in two on the first look. The underlying bug, `if` instead of `while`, plus `notify` where `notifyAll` was needed, is the most instructive bug in all of interview concurrency, and this is it presented from the on-call side rather than the whiteboard side.

---

## Strategy

### Classify

Bounded resource (Type C) seen from the review side: two parties waiting on opposite conditions over one structure, with one monitor and therefore **one wait set containing both kinds of waiter**. The defects are catalog #4 (`if` instead of `while`), #3 (wrong-party wakeup via `notify` with mixed waiters), and #2 (lost wakeup), and a fourth, catalog-adjacent one in the swallowed `InterruptedException`. Sweep 3 (find every wait/signal pair) is the whole diagnosis.

### The invariant being broken

*A thread returns from its wait only when its own predicate is true, and whenever a state change makes some waiter's predicate true, some such waiter is woken.* The second clause is the one the code violates: state changes happen and the signal reaches a thread whose predicate is still false, which re-checks nothing, or which consumes the signal and re-sleeps. The system then has a nonempty set of runnable-in-principle threads and no signal in flight.

### Symptom → hypothesis, straight from the dump

Read the dump before the code. Three observations, each eliminating a hypothesis:

- **Every thread `WAITING`, none `BLOCKED`.** BLOCKED means a thread is trying to *enter* a monitor someone else owns; that is the signature of contention or a lock cycle. WAITING means the thread parked itself voluntarily inside `Object.wait()` / `Condition.await()` / `park()` and **released the monitor**, nobody is holding anything against it. A hang made entirely of WAITING threads is therefore not a deadlock in the Coffman sense: there is no circular *wait-for-a-held-resource* graph, because no resource is held. Say the sentence: *"they are not stuck, they are asleep, nobody told them to wake up."*
- **No deadlock section in the dump.** The JVM detects cycles among monitors and `ReentrantLock`s automatically. Its silence is meaningful evidence *against* lock-order inversion, and it is also a warning: **the JVM can never detect a lost wakeup**, so absence of the section does not mean absence of a hang cause. This is exactly why the previous on-call reached for a watchdog, the tooling said "no deadlock" and they believed the tool over the symptom.
- **Never reproduces at 1 producer : 1 consumer; ten times more likely with slowed consumers.** With one of each, the single wait set can only ever contain one waiter of a single kind, so `notify` cannot wake the wrong party. The 1:1 immunity is a *fingerprint* of the mixed-wait-set problem. Slow consumers mean the buffer sits full, so producers pile into the wait set, raising the odds that a `notify` intended for a producer lands on another producer.

Hypothesis, stated before opening the file: **lost wakeup on a single monitor with mixed waiters, compounded by unlooped waits.**

### The defects, in severity order

**Defect 1, `notify()` on a monitor with two kinds of waiter (catalog #3).** There is one wait set holding both blocked producers and blocked consumers. `notify()` wakes *one arbitrary* waiter. A consumer that just removed an item wants to wake a producer, but may wake another consumer instead. That consumer's predicate (`items.isEmpty()`) may be false, or, with defect 2 present, it doesn't even check, and the signal is **consumed**: it was a one-shot event, it woke the wrong party, and it is gone forever. Repeat until every thread is in the wait set with no signal outstanding. That is the frozen state in the dump.

**Defect 2, `if` instead of `while` (catalog #4).** A thread that returns from `wait()` proceeds directly to act *on a predicate it verified before it slept*. Between the signal and the reacquisition of the monitor, another thread can barge in and consume the condition (the JVM's monitor is not FIFO and a newly-arriving thread can win the lock ahead of a just-woken one). So a woken consumer can call `remove` on an empty deque, and a woken producer can add past capacity. The results are a `NoSuchElementException` from the deque, or a silent capacity violation, and, critically, they make defect 1 *worse*: an `if`-waiter that is woken by mistake doesn't re-sleep politely, it does damage. The JLS also permits **spurious wakeups**, so `while` is mandatory independent of everything else. There is no correct program that waits inside an `if`.

**Defect 3, swallowed `InterruptedException`.** Catching it and logging at DEBUG erases the cancellation request: the interrupt flag was cleared by `wait()` throwing, and nothing restores it. These threads are now **uncancellable**, which is why shutdown never completes and why the watchdog had to *restart the process* rather than stop the pipeline. Fix: restore the flag (re-interrupt the current thread) and exit the loop, or declare and propagate the exception.

**Defect 4, `size()` is a snapshot presented as a fact.** It is correctly synchronized, so it is not a race, but any caller branching on it has written a check-then-act. Worth flagging in review even though it isn't the hang.

### The interleaving to narrate

Capacity 2, two producers (P1, P2), two consumers (C1, C2), the smallest instance. Buffer full with 2 items.

1. P1 enters `put`, sees full, waits. Wait set: {P1}.
2. P2 enters `put`, sees full, waits. Wait set: {P1, P2}.
3. C1 takes an item (buffer now 1) and calls `notify()`. The JVM picks **P1**. Good so far, but before P1 reacquires the monitor:
4. C2 enters `take`, takes the second item (buffer now 0), and calls `notify()`. Wait set currently holds only P2 (P1 is awake but hasn't reacquired). The JVM picks **P2**.
5. P1 and P2 both eventually reacquire and both add, buffer is back to 2. Each calls `notify()`. Both consumers are gone from the wait set (they're running), so **both notifications are delivered to the empty wait set and are silently discarded**. Notifications are not stored; a `notify` with no waiter is a no-op.
6. C1 and C2 come back around, find the buffer... this time it's full, so they proceed. Run the loop again with slightly different timing and you reach the state where two consumers are in the wait set on an empty buffer, a producer adds one item and calls `notify()`, and the JVM wakes **the other producer** that was also in the set. The producer re-checks (or with `if`, doesn't), goes back to sleep, and the one signal that should have started a consumer is consumed. Nothing else will ever produce a signal, because the producers are all asleep waiting for space and the consumers are all asleep waiting for items.

The general statement, which is the thing to say rather than the trace: **with one wait set and two kinds of waiter, `notify` can deliver a signal to a party that cannot use it; the signal is consumed and not regenerated; iterate and the system reaches a state where every thread is waiting and no signal is in flight.**

### Reproduce deterministically

- **Amplify first:** 8 producers, 8 consumers, capacity small (2–4), and **skewed speeds**, put a delay in the consumers so the buffer stays full and producers accumulate in the wait set. Run with a hard **timeout on the harness join** so the hang *fails the test in seconds* rather than wedging CI. This is the single most important testing habit for hang bugs: a test that can hang forever is worse than no test.
- **Make it deterministic:** with a test seam, hold the monitor-reacquisition of one specific thread (or single-thread-suspend it in a debugger inside the window between `wait()` returning and the act) and drive the exact sequence above with four threads. The freeze becomes reproducible on demand.
- **Confirm from the outside:** at the moment of the hang, take three dumps and check that all worker threads are `WAITING` at `Object.wait()`, none `BLOCKED`, no deadlock section, and the stacks are identical across all three. That combination *is* the confirmation, it distinguishes a lost wakeup from every other hang cause.
- One more cheap confirmation: instrument a counter of signals sent vs waiters present at signal time. A `notify` issued when the wait set holds only wrong-party waiters is a directly observable lost wakeup.

### The fixes

**Fix 1, `while` instead of `if` (mandatory, non-negotiable).** Every wait re-tests its predicate in a loop. Required for spurious wakeups, for barging, and to make any of the other fixes sound. Do this even if you also do fix 2 or 3; the other fixes are not substitutes.

**Fix 2, `notifyAll` instead of `notify`.** Wakes every waiter; each re-tests its own predicate in its `while` loop; those that can't proceed go back to sleep. No signal can be consumed by the wrong party because it is delivered to everybody. Cost: a thundering herd, all waiters wake, contend for the monitor, and most re-sleep. At sixteen threads this is irrelevant; say so, and say that you'd only care at thousands. **`while` + `notifyAll` is the minimum correct patch and the one to ship tonight.**

**Fix 3, two `Condition`s on a `ReentrantLock` (the idiomatic redesign).** `notFull` and `notEmpty` are two separate wait rooms on one lock. A producer signals `notEmpty`, a consumer signals `notFull`, always the opposite party, and each room contains only one kind of waiter, so plain `signal()` becomes safe again and the herd disappears. This is the design inside `ArrayBlockingQueue`. Articulating *"two wait sets remove the wrong-party wakeup problem, which is what makes `signal` safe here and unsafe there"* is the depth answer.

**Fix 4, delete the class.** Use `ArrayBlockingQueue`. In a real review this is the top-line recommendation: the hand-rolled buffer offers nothing the JDK's doesn't, and every line of it is a place for this bug to live. Say it first, then present fixes 1–3 for the case where the class must survive.

**Fix 5, the interrupt handling.** Propagate `InterruptedException` from `put`/`take`, or restore the flag and exit. Independent of the hang; ship it in the same change. And note that this is why the previous watchdog had to kill the process, with cancellable workers, a graceful shutdown would have been possible.

Also: delete the watchdog once fixed, or keep it as declared defense-in-depth with an alert, but never as the fix. A restart timer that hides a lost wakeup is how a four-minute freeze becomes a nine-hour one nobody noticed.

### Prove the fix

- **`while` loops:** by construction, a thread proceeds only when its predicate holds *at the moment it holds the monitor*, so no action is ever taken on a stale predicate. Spurious wakeups and barging are absorbed.
- **`notifyAll`:** every state change that could make any predicate true is followed, under the same monitor, by a wakeup delivered to **all** waiters; therefore if any waiter's predicate is true after the change, that waiter re-tests and proceeds. There is no waiter set in which a satisfiable predicate goes unexamined, so the frozen state is unreachable.
- **Two conditions:** each wait room's occupants share one predicate; a signal to that room reaches a thread that can use it; therefore one `signal` per state change suffices. (State the partition explicitly, that is the proof.)
- **Regression test:** the deterministic four-thread reproduction now completes; the stress harness with skewed speeds completes within its timeout across many runs. And the honest caveat: the stress run passing is a smoke check; the predicate-partition argument is the proof.

### Pitfalls

1. **Saying "deadlock" because it's a hang.** Nothing is held; there is no cycle. Costs the round.
2. **Fixing `notify` → `notifyAll` and leaving the `if`.** Now all waiters wake, and each acts on a predicate it checked before sleeping, you have replaced an intermittent freeze with intermittent *corruption*, which is worse.
3. **Fixing `if` → `while` and leaving `notify`.** Better (no corruption) but the freeze remains: a wrong-party wakeup is still a consumed signal. Both are needed, or fix 3.
4. **Believing the JVM's "no deadlock found".** It detects monitor cycles only. It is structurally incapable of detecting a lost wakeup.
5. **Signalling before mutating, or outside the lock.** A signal issued before the state change can be received by a thread that then re-checks a not-yet-updated predicate. Change state, then signal, both while holding the lock.
6. **Trusting a `TIMED_WAITING` variant to be fine.** Adding a timeout to the wait converts the permanent freeze into intermittent latency spikes, the bug is still there and is now *harder* to find. A timeout is a safety net, never a fix.
7. **Keeping the watchdog.** Restart-on-timer is how this bug survived nine hours at a time.

### Check your understanding

1. Define BLOCKED, WAITING and TIMED_WAITING in one sentence each, and say what hang cause each points to.
2. Why does the JVM's deadlock detector stay silent here, and what class of hang can it never find?
3. Why does the bug never appear with one producer and one consumer? What does that tell you before you read the code?
4. Narrate the freeze with 2 producers, 2 consumers, capacity 2.
5. Why is `while` required even if `notifyAll` is used and even with a single kind of waiter?
6. Why does two-`Condition`s make plain `signal()` safe where `notify()` was not?
7. What symptom would you see instead if someone "fixed" this with `wait(1000)`?
8. The swallowed `InterruptedException`, what production behaviour does it cause, and which line of the incident report does it explain?

### Transfers to

`bounded-blocking-queue` (the same object built correctly, from the other side), every hand-rolled latch/barrier/semaphore, `dining-savages` and `barbershop` (mixed waiters on one monitor is the family trait), and, via the WAITING-with-no-cycle signature, pool-exhaustion starvation, where all workers wait on futures of tasks that need a free worker. That last one produces an identical dump and is the other answer you must have loaded when you see all-WAITING-no-deadlock.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/debugging-and-code-review/the-hang-that-isnt-a-deadlock).
