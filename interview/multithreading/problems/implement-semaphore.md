---
layout: post
title: Implement a Semaphore (using wait/notify)
date: 2026-07-19
description: >-
  You've USED semaphores for weeks by now; building one collapses the mystery: a semaphore is just a guarded counter, the condition-loop template wrapped around an int. After…
categories: interview multithreading problems
---

Part of the [Bounded Resource](/interview/multithreading/patterns/bounded-resource/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Classic. Optional depth exercise, the plan says pick this OR implement-latch-or-barrier, not both.

### Problem

Implement `MySemaphore(int permits)` with `acquire()` (block while no permits, then take one) and `release()` (add a permit, wake a waiter) using only `synchronized`/`wait`/`notifyAll`.

### Constraints

- No busy-waiting.
- `release()` may be called by a thread that never acquired (semaphores have no ownership, that's a feature).
- Support initial permits = 0 (the "closed door" signaling case).

### Clarify before solving

- Fairness required? (Baseline: no. Know what unfairness means here: barging.)
- Can permits exceed the initial count? (For a counting semaphore: yes, releases just add. Say you're matching `java.util.concurrent.Semaphore` semantics.)

### Why this problem matters

You've USED semaphores for weeks by now; building one collapses the mystery: a semaphore is just a guarded counter, the condition-loop template wrapped around an int. After this, "which primitive do I need?" becomes "which guarded state do I need?", which is the real question anyway.

---

## Strategy

### Classify

Guarded counter with one wait condition ("permits > 0"). [Template 1](/interview/multithreading/mt-framework/), nearly verbatim.

### Invariant

`permits >= 0` at all observable times; each acquire that returns decremented exactly one permit; permits change only under the lock.

### Mental model

A jar of tokens behind glass. acquire: lock the case; while jar empty, wait; take a token; unlock. release: lock; drop a token in; announce (notifyAll); unlock. That's the entire implementation, maybe 15 lines. The exercise's value is noticing HOW LITTLE there is: the primitive you've leaned on all month is the Week-2 template with an int.

### What deserves actual thought

1. **notifyAll vs notify**: with only-acquirers waiting on one condition, `notify` after a single release looks sufficient: one permit, one waiter woken. It's defensible for a counting semaphore, but becomes wrong the moment anyone adds a second wait condition or a multi-permit `release(n)` to the same monitor. Default notifyAll; state the reasoning. This nuance IS the interview.
2. **No ownership**: no check that the releaser previously acquired. Contrast with a mutex/ReentrantLock, where unlock by a non-owner throws. This asymmetry is why semaphores can do cross-thread signaling (Print in Order!) and mutexes cannot. Conceptual #22 answered from first principles.
3. **Barging (unfairness)**: a thread calling acquire at the exact moment of a release can grab the fresh permit before a long-parked waiter wakes up. That's unfair but correct, and it's how nonfair `java.util.concurrent` locks behave too (it improves throughput). Fair version = queue of waiting threads served FIFO: describe, don't build (the Little Book of Semaphores' FifoSema if curious).
4. **release() before any acquire / permits above initial**: fine, permits is just a counter. This IS the "signal persists" property that made semaphores solve Print in Order.

### Pitfalls

1. `if` instead of `while`: with barging, a woken thread may find the permit already stolen; must re-check. This design makes the reason for `while` viscerally concrete.
2. Decrementing permits outside the lock or after exiting the loop without atomicity with the check.
3. Trying to make acquire "fair" with extra flags without a real queue, half-fairness that breaks the simple invariant. Don't.

### Check your understanding

1. Point to the exact line where barging happens and explain why `while` saves correctness.
2. Why would a `MySemaphore` used as a mutex (permits=1) not detect "unlock by wrong thread"? Is that a bug? (No, no-ownership is the defining semantic difference.)
3. Sketch (words only) how you'd add fairness. What new state appears? (A FIFO queue of waiters; release hands a permit to the head specifically.)
4. Implement `tryAcquire()` mentally: what changes? (Same lock, no wait. Return false instead. Notice how blocking vs failing is a one-line policy change on the same guarded state.)

### Transfers to

Demystifies every j.u.c primitive: CountDownLatch is a guarded counter counting DOWN with waiters released at zero; CyclicBarrier adds a generation reset. Same template, different predicate, which is exactly the point.
