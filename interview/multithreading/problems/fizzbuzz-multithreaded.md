---
layout: post
title: Fizz Buzz Multithreaded (LC 1195)
date: 2026-07-19
description: >-
  Tests whether your turn-taking scales past two threads: 4 waiters, 1 turn, and the turn-holder is decided by *data* (divisibility of the current number), not by a fixed…
categories: interview multithreading problems
---

Part of the [Ordering & Turn-Taking](/interview/multithreading/patterns/ordering/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [LeetCode 1195](https://leetcode.com/problems/fizz-buzz-multithreaded/)

### Problem

Four threads share one instance, each permanently assigned a role: `fizz()` (multiples of 3 only), `buzz()` (multiples of 5 only), `fizzbuzz()` (multiples of 15), `number()` (everything else). Together they must output classic FizzBuzz for 1..n in order.

### Constraints

- Each thread only ever prints its own kind. The scheduler decides who runs when; your code decides who *proceeds*.
- Reusable across n iterations; no busy-waiting.

### Clarify before solving

- For i=15, only fizzbuzz prints (not fizz and buzz separately).
- All four threads must terminate cleanly at n.

### Why this problem matters

Tests whether your turn-taking scales past two threads: 4 waiters, 1 turn, and the turn-holder is decided by *data* (divisibility of the current number), not by a fixed rotation. If your Odd-Even solution was truly understood, state is the signal, this is a 10-minute problem. If it was memorized, this is where that shows.

---

## Strategy

### Classify

Ordering, 4 threads, data-driven turn → condition loop on shared state (Odd-Even generalized).

### Invariant

For each i in 1..n, exactly one thread (the one matching i's divisibility class) prints, and i advances only after that print.

### Mental model

Same as Odd-Even: one lock, one shared counter `i`, and each thread's guard is a predicate on the state:

- number: `i % 3 != 0 && i % 5 != 0`
- fizz: `i % 3 == 0 && i % 5 != 0`
- buzz: `i % 5 == 0 && i % 3 != 0`
- fizzbuzz: `i % 15 == 0`

Each thread: lock; while (not my predicate AND i <= n) wait; if i > n → notifyAll and exit; print; i++; notifyAll. The four predicates are mutually exclusive and cover every integer, say that sentence to the interviewer; it's the whole correctness argument for "exactly one printer per number".

### Why notifyAll is non-negotiable here

After i++ the next eligible thread might be ANY of the other three. `notify()` picks an arbitrary waiter, if it picks a thread whose predicate is false, that thread re-checks, sleeps again, and the eligible thread was never woken. System hangs with all four asleep. This problem is the cleanest demonstration of the notify-vs-notifyAll bug, be ready to narrate that exact interleaving; it's a favorite follow-up.

### Semaphore alternative (mention, don't default to)

A "director" style also works: number thread owns the counter and releases the right thread's semaphore per i. But then the number thread is doing double duty (printer + router), and LeetCode's structure (4 symmetric methods) fights you. The shared-predicate design is symmetric and simpler. Choosing it, and saying why, is the senior move.

### Pitfalls

1. Wrong predicate exclusivity (fizz firing on 15), always write the four predicates down and check 15 explicitly.
2. Termination: any thread can be the one waiting when i passes n. Every thread must re-check `i > n` after every wake and notifyAll on exit (same discipline as Odd-Even).
3. Printing outside the lock "for performance", for a println this only invites reordering bugs; keep it simple, note the trade-off aloud.

### Check your understanding

1. Give the two-sentence proof that exactly one thread prints each number.
2. Narrate the exact hang interleaving if `notify()` were used with i=2 (number's turn) and fizz gets woken.
3. What changes if a 5th thread joins printing multiples of 7? (Predicates must be rewritten to stay exclusive and exhaustive, enumerate the new classes. The pattern doesn't change; the case analysis does.)

### Transfers to

Any "N workers, data decides whose turn" problem; also the gateway to guarded-state problems where predicates guard actions rather than turns.
