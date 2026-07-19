---
layout: post
title: Bounded Blocking Queue / Producer-Consumer (LC 1188)
date: 2026-07-19
description: >-
  Contains the entire wait/notify discipline in one problem: two distinct wait conditions, guarded by one invariant, with every classic bug available (lost wakeup, if vs while…
categories: interview multithreading problems
---

Part of the [Bounded Resource](/interview/multithreading/patterns/bounded-resource/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [LeetCode 1188](https://leetcode.com/problems/design-bounded-blocking-queue/), the Little Book of Semaphores ch. 4, and virtually every senior loop. **The single most-asked concurrency coding question anywhere.**

### Problem

Implement a queue with fixed capacity and blocking semantics:

- `enqueue(x)`: if full, block until space frees up.
- `dequeue()`: if empty, block until an element arrives.
- `size()`.

Multiple producers and multiple consumers call it concurrently.

### Constraints

- Blocking, not failing — callers wait, they don't get exceptions or nulls.
- No busy-waiting; no `Thread.sleep`.
- FIFO order.

### Clarify before solving

- One producer/consumer or many? (Design for many — it changes notify discipline.)
- Bounded, right? (The bound is what creates the producer-side wait — unbounded would halve the problem.)
- Interruption behavior? (Propagate `InterruptedException` — say it, it's a senior signal.)

### Why this problem matters

Contains the entire wait/notify discipline in one problem: two distinct wait conditions, guarded by one invariant, with every classic bug available (lost wakeup, `if` vs `while`, notify vs notifyAll). If you can build, break, and defend this from a blank editor, you're prepared for most of what a concurrency screen throws at you. Milestone problem for Week 2 and again in Week 6.

---

## Strategy

### Classify

Bounded resource: two parties waiting on OPPOSITE conditions over one shared structure. Producers wait on "not full"; consumers wait on "not empty".

### Invariant

`0 <= size <= capacity`; every enqueued element is dequeued exactly once, FIFO. Linearization point: the add/remove of the element under the lock.

### Mental model

A parking garage with `capacity` spaces. Arriving cars (producers) wait at the entrance barrier when full; leaving cars (consumers) raise the entrance barrier as they exit. Two DIFFERENT barriers, raised by the OPPOSITE party — that crossing is the heart of the problem: **a consumer signals producers; a producer signals consumers.** Never your own kind.

### Version 1 — wait/notify (learn this first)

One lock, one internal `ArrayDeque`. enqueue: lock; while (full) wait; add; **notifyAll**; unlock. dequeue: mirror. Why `notifyAll` and not `notify`: with one monitor there is ONE wait set containing BOTH producers and consumers. `notify` might wake a producer when a producer just added (still full for producers... worse: wrong party entirely) — the woken thread re-checks its while, sleeps again, and the signal was consumed by the wrong party. All threads end up asleep: system hangs. Be able to narrate this interleaving cold — it is THE most instructive bug in all of interview concurrency.

### Version 2 — ReentrantLock + two Conditions (the idiomatic upgrade)

`notFull` and `notEmpty` as separate Conditions on one Lock = two separate wait rooms. Now enqueue signals `notEmpty` (exactly the party that could progress) and can use `signal()` instead of `signalAll()` safely — each wait room contains only one kind of waiter. This is [Template 3](/interview/multithreading/mt-framework/) and the design inside `ArrayBlockingQueue`. Articulating "two wait sets remove the wrong-party wakeup problem" is precisely the understanding this problem exists to test.

### Version 3 — two semaphores (the the Little Book of Semaphores lens)

`empty = Semaphore(capacity)`, `full = Semaphore(0)`, plus a mutex for the queue itself. enqueue: empty.acquire → locked add → full.release. Beautiful symmetry: the semaphores COUNT the resource directly, no explicit condition checks at all. Do it once to see the same invariant expressed three ways — that's the grasp-not-memorize payoff.

### And the production answer

`ArrayBlockingQueue` / `LinkedBlockingQueue`. In a design round, say so and move on; hand-roll only when implementation IS the question. Know that `LinkedBlockingQueue` uses two locks (head/tail) so producers and consumers don't contend — a nice depth remark.

### Pitfalls

1. `notify()` with a single monitor — hang under multiple producers AND consumers (see above).
2. `if` instead of `while` — a woken producer must re-check fullness: another producer may have raced in first.
3. Signaling before mutating, or outside the lock-held state change — inconsistent view.
4. Semaphore version: taking the mutex BEFORE `empty.acquire` — deadlock (holding the mutex while blocking on the semaphore; consumers need the mutex to release you).
5. Swallowing `InterruptedException` in a blocking call — propagate it.

### Check your understanding

1. Narrate the notify-hang interleaving with 2 producers + 2 consumers, step by step.
2. Why exactly does Version 2 make `signal()` safe where Version 1's `notify()` wasn't?
3. In Version 3, why must the semaphore acquire come BEFORE the mutex? Construct the deadlock the other way.
4. What breaks if `size()` returns without acquiring the lock, and does it matter? (Stale value; usually acceptable — say why.)

### Transfers to

Thread pool (worker queue IS this), rate limiter (permits), dining savages (pot = queue of servings), delayed scheduler (queue + time condition), barbershop (bounded waiting room). This problem is the trunk of the whole Type C tree.
