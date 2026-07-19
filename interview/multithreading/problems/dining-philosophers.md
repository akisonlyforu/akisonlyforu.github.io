---
layout: post
title: Dining Philosophers (LC 1226)
date: 2026-07-19
description: >-
  THE vehicle for deadlock. You're expected to (a) show the deadlock cycle, (b) name the four Coffman conditions, (c) present at least two fixes and say which condition each…
categories: interview multithreading problems
---

Part of the [Guarded State](/interview/multithreading/patterns/guarded-state/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [LeetCode 1226](https://leetcode.com/problems/the-dining-philosophers/), the Little Book of Semaphores ch. 4, every OS course ever.

### Problem

Five philosophers sit around a table, one fork between each pair. To eat, a philosopher needs BOTH adjacent forks. Each philosopher loops: think, pick up forks, eat, put down forks. Design the fork protocol so that nobody starves and the system never deadlocks.

### Constraints

- A fork is held by at most one philosopher at a time.
- The naive protocol (everyone picks up left fork, then right) must be understood as the canonical deadlock before you fix it.

### Clarify before solving

- How many philosophers can eat simultaneously? (Floor(5/2) = 2, your solution shouldn't be more restrictive than necessary, but a correct-but-conservative solution is acceptable if you name the trade-off.)
- Is starvation-freedom required or just deadlock-freedom? (Usually deadlock-freedom; know the difference.)

### Why this problem matters

THE vehicle for deadlock. You're expected to (a) show the deadlock cycle, (b) name the four Coffman conditions, (c) present at least two fixes and say which condition each fix breaks. That structure, cycle, conditions, targeted break, is reusable on every deadlock question you'll ever get, including "tell me about a deadlock you've seen in production".

---

## Strategy

### Classify

Multi-resource acquisition. The danger isn't a data race (each fork is trivially a mutex), it's the **acquisition ORDER** across multiple locks.

### Invariant

Each fork held by ≤1 philosopher; a philosopher eats only while holding both adjacent forks; the waits-for graph never contains a cycle.

### First: construct the deadlock (do this before fixing anything)

All five simultaneously grab their left fork. Now everyone holds one fork and waits for the right one, which is someone else's left. Waits-for graph: P0→P1→P2→P3→P4→P0. A cycle. Nobody can proceed, nobody will release. Check the four Coffman conditions: mutual exclusion (forks are exclusive) ✓, hold-and-wait (holding left, waiting for right) ✓, no preemption (can't snatch a fork) ✓, circular wait (the cycle) ✓. All four hold → deadlock is possible. **Every fix works by breaking exactly one condition, always say which.**

### The three standard fixes

1. **Resource ordering** (breaks circular wait). Number the forks 0..4. Every philosopher picks up their LOWER-numbered fork first. Four philosophers order left-then-right, but one (the one between fork 4 and fork 0) is forced right-then-left. A cycle now needs everyone to wait for a higher-numbered fork while holding a lower one, but the ordering makes a closed loop of "higher" impossible. This is also the production answer for "lock A then B in one code path, B then A in another": **define a global lock order.**
2. **Limit diners to N-1** (breaks hold-and-wait at the system level). A multiplex, Semaphore(4), around the table: at most 4 philosophers may even reach for forks. With 5 forks among ≤4 fork-grabbers, the pigeonhole principle guarantees someone gets both forks, eats, and releases. Simple to reason about; slightly conservative.
3. **tryLock with backoff** (breaks hold-and-wait per philosopher). Grab left; try right; if unavailable, PUT LEFT BACK and retry later. No one ever holds-and-waits indefinitely. Cost: theoretical livelock (all five could cycle in lockstep), mention randomized backoff as the mitigation. This maps to real systems: tryLock + timeout is how you defend when you can't control lock order.

### Which to code in an interview

Resource ordering, shortest and the most transferable idea. Offer the other two verbally.

### Starvation note (senior differentiator)

Deadlock-freedom ≠ starvation-freedom: under fix 1 an unlucky philosopher can lose the race repeatedly. If asked: fair semaphores/locks (FIFO queuing) or the N-1 semaphore in fair mode. Don't volunteer complexity, answer when probed.

### Pitfalls

1. Presenting a fix without showing the deadlock first, you skipped the understanding.
2. "Just use one global mutex for the whole table", correct but serializes all eating (one diner at a time instead of two); name that cost if you offer it as a baseline.
3. In fix 3, retrying without releasing the left fork, that IS hold-and-wait; you fixed nothing.

### Check your understanding

1. Recite the four Coffman conditions and, for each of the three fixes, which one it breaks.
2. Why exactly can't a waits-for cycle exist under global lock ordering? Prove it in two sentences.
3. Construct the livelock in fix 3 concretely. Why does randomized backoff break it?
4. Production translation: two services each lock rows in tables A and B in opposite orders. Which fix applies and how?

### Transfers to

Every multi-lock deadlock question, bank transfer problem (lock both accounts, order by account id!), conceptual #6, and your "deadlock war story" prep.
