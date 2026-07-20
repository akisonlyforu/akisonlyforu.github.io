---
layout: post
title: N Threads Round-Robin Printing
date: 2026-07-19
description: >-
  Forces the generalization: fixed semaphore pairs (FooBar-style) become unwieldy at T threads, while the shared-state condition loop scales without modification. If you can…
categories: interview multithreading problems
---

Part of the [Ordering & Turn-Taking](/interview/multithreading/patterns/ordering/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Phone screens. The "prove you didn't memorize odd-even" question.

### Problem

T threads, ids 0..T-1, print numbers 1..N in order. Thread with id k prints the numbers where `number % T == k+1 % T` (i.e., thread 0 prints 1, T+1, 2T+1...; thread 1 prints 2, T+2, ...). Output is 1, 2, 3, ..., N in order, each number from its assigned thread.

### Constraints

- T is a runtime parameter, your solution must work for any T, so hardcoded pairs of semaphores don't scale nicely.
- Clean termination for any N (N need not be a multiple of T).

### Clarify before solving

- Confirm the assignment rule with a small example (T=3, N=7: t0→1,4,7; t1→2,5; t2→3,6).
- All threads created upfront? (Yes.)

### Why this problem matters

Forces the generalization: fixed semaphore pairs (FooBar-style) become unwieldy at T threads, while the shared-state condition loop scales without modification. If you can articulate WHY you're switching styles as T grows, you've understood ordering problems completely, and this category is done for you.

---

## Strategy

### Classify

Ordering, parameterized thread count → shared-state condition loop (the only style that scales).

### Invariant

Number `current` is printed only by thread `(current - 1) % T`, and increments by one per print.

### Mental model

Identical to Odd-Even with the predicate generalized: each thread runs lock; while (`(current - 1) % T != myId` and current <= N) wait; check termination; print; current++; notifyAll. One lock, one counter, T waiters. Nothing else changes, that's the insight.

### The style-choice discussion (this is what's actually being tested)

- **Semaphore chain** (each thread releases the next): T semaphores in a ring, thread k releases (k+1)%T. Elegant, and each wake targets exactly the right thread, no wasted wakeups. Cost: more objects to initialize (all 0 except thread 0's = 1), termination is fiddlier (must release the whole ring at the end so everyone can exit).
- **Shared counter + notifyAll**: dead simple, uniform termination, but every increment wakes T-1 threads that mostly go back to sleep, O(T) wasted wakeups per number.

Senior answer: name both, pick the counter version for clarity at interview scale, and note the semaphore ring is the answer if T is large and wakeup cost matters. Trade-off articulated > either solution alone.

### Correctness argument

Counter version: monitor guards all state (no race, guaranteed visibility); predicate `(current-1) % T == myId` is true for exactly one thread per value (mod arithmetic partition); notifyAll after every change (no lost wakeup); termination re-checked under lock after every wake.

### Pitfalls

1. Off-by-one in the mod mapping, always verify with T=3, N=7 written out by hand before coding.
2. Termination when N % T != 0: threads whose numbers are exhausted are still waiting; the exiting thread's notifyAll must cascade so every thread re-checks and exits. Test T=3, N=7 specifically.
3. Semaphore-ring version: forgetting that the LAST printer must still release the next thread (which will see current > N and exit, then release the next...), an exit cascade, easy to drop.

### Check your understanding

1. Why does the semaphore-pair style that worked for FooBar (T=2) become awkward at T=10? Be specific about what multiplies.
2. In the ring version, exactly how does every thread come to terminate when N=7, T=3? Walk the cascade.
3. What single line changes between Odd-Even and this solution? (If your answer isn't "the predicate", revisit Odd-Even.)

### Transfers to

Completes the ordering family. Every Type A problem you'll ever see is now: identify the baton, identify who routes it, pick semaphores (targeted) vs shared state (simple), handle termination.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/ordering/n-threads-round-robin).
