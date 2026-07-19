---
layout: post
title: Implement CountDownLatch OR CyclicBarrier
date: 2026-07-19
description: >-
  The latch is easy; the barrier is not — and the gap BETWEEN them is the lesson. Reusability forces the "generation" idea (how does a thread know the wait it's in belongs to…
categories: interview multithreading problems
---

Part of the [Bounded Resource](/interview/multithreading/patterns/bounded-resource/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Banks / FAANG. Alternative to implement-semaphore — pick ONE of the two exercises.

### Problem

Pick one:

- **MyCountDownLatch(int n)**: `await()` blocks until `countDown()` has been called n times. One-shot — once open, open forever.
- **MyCyclicBarrier(int n)**: `await()` blocks until n threads have arrived, then ALL are released and the barrier resets for the next round of n.

Using only `synchronized`/`wait`/`notifyAll`.

### Constraints

- No busy-waiting.
- Latch: `await()` after the count already hit zero returns immediately. `countDown()` below zero is a no-op.
- Barrier: must be safely reusable — the generation problem is the entire difficulty.

### Clarify before solving

- One-shot or reusable? (This single question determines which primitive you're building and ~80% of the difficulty.)
- Latch: can count-downers also await? (Yes, they're independent roles — like workers signaling a coordinator.)

### Why this problem matters

The latch is easy; the barrier is not — and the gap BETWEEN them is the lesson. Reusability forces the "generation" idea (how does a thread know the wait it's in belongs to round k, not round k+1?), which is the same reasoning as the Little Book of Semaphores' two-turnstile reusable barrier and the reason [Template 4](/interview/multithreading/mt-framework/) uses CyclicBarrier for H2O. Build the latch, then attempt the barrier, then read about generations.

---

## Strategy

### MyCountDownLatch (the warm-up)

Guarded counter, one condition: "count == 0".

- `await`: lock; while (count > 0) wait; done.
- `countDown`: lock; if (count > 0) count--; if now zero → notifyAll (fling the gate open for everyone).

Invariant: count never increases; once zero, every current and future await returns immediately. One-shot-ness makes this trivial — the state moves in one direction only, so there's no "wrong round" to confuse a waiter. About 12 lines. The interview content is the semantics: awaiters and counters are separate roles; the count can't be reset (that's what makes it a latch and not a barrier).

### MyCyclicBarrier (where the real lesson lives)

Naive attempt: count arrivals; the n-th arrival notifyAlls and resets count to 0. **Find the bug yourself before reading on.**

The bug — **generation mixing**: the n-th thread resets count and notifies. A released thread loops around and calls await again (new round) BEFORE some slow thread from the old round has even woken from wait(). The fast thread increments count for round 2; the slow thread wakes, re-checks its `while (count < n)` — but count now reflects round 2's progress. Waiters from two different rounds are now indistinguishable, waiting on the same counter. Threads can wait forever or be released early. This is exactly why the Little Book of Semaphores needs the two-turnstile construction and why "a bare count+gate barrier is one-shot" ([Template 4](/interview/multithreading/mt-framework/)'s warning).

The fix — **a generation token**: an object or int identifying the current round. Each awaiting thread captures the generation on entry and waits `while (myGeneration == currentGeneration)`. The n-th arrival flips currentGeneration (new object / increment), resets count, notifyAlls. Woken threads see the generation CHANGED → their round completed → exit the loop. Round-2 arrivals capture the NEW generation, so old and new waiters can never be confused, even sharing one monitor. Elegant: the predicate is "has my round ended?" not "how many arrived?" — the count no longer needs to be meaningful to sleepers.

Invariant: threads captured under generation g are all released by the g→g+1 flip and only by it; count always refers to the current generation only.

### Pitfalls

1. The generation-mixing bug shipped as "working" — it passes light testing; only contention with immediate re-await exposes it. (Stress test: n threads each looping await 10,000 times with a start gate.)
2. Waiting on the count (`while (count < n)`) instead of the generation — that IS the bug, restated.
3. Latch: notifyAll on every countDown instead of only at zero — correct but wasteful; know why (waiters' predicate can only become true at zero).
4. Real CyclicBarrier breaks the barrier if a waiter is interrupted (BrokenBarrierException) so peers don't hang forever. Mention it; don't build it.

### Check your understanding

1. Narrate generation mixing with n=2 and threads A (fast, loops immediately) and B (slow to wake). Where exactly does B's re-check go wrong?
2. Why does the one-shot latch have no such problem? (State moves one way; no reuse → no rounds.)
3. What does capturing `myGeneration` before waiting actually accomplish — what question does the waiter now ask? 
4. Connect: why does [Template 4](/interview/multithreading/mt-framework/) pair CyclicBarrier with admission semaphores for H2O instead of a hand-rolled count+gate?

### Transfers to

Reusable barrier = the mechanism inside H2O, Uber Ride, roller coaster. Generation reasoning also underlies phaser/epoch designs — recognizing "which round does this waiter belong to?" is a genuinely senior skill.
