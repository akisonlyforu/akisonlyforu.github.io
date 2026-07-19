---
layout: post
title: Print FooBar Alternately (LC 1115)
date: 2026-07-19
description: >-
  First encounter with ping-pong signaling: each thread's last act enables the *other* thread's next turn. This is the reusable version of the door metaphor, the doors…
categories: interview multithreading problems
---

Part of the [Ordering & Turn-Taking](/interview/multithreading/patterns/ordering/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [LeetCode 1115](https://leetcode.com/problems/print-foobar-alternately/)

### Problem

Two threads share a FooBar instance. Thread A calls `foo()` n times; thread B calls `bar()` n times. Ensure output is exactly `"foobarfoobar...foobar"` (n times).

### Constraints

- n up to ~1000; the loop runs many times, so your coordination must be **reusable**, not one-shot.
- Neither thread may busy-wait.

### Clarify before solving

- Repeated cycles (yes, this is the key difference from Print in Order).
- Who goes first? (foo, so bar's gate starts closed, foo's gate starts open.)

### Why this problem matters

First encounter with **ping-pong signaling**: each thread's last act enables the *other* thread's next turn. This is the reusable version of the door metaphor: the doors re-close automatically because acquiring a permit consumes it.

---

## Strategy

### Classify

Ordering, repeated cycles → ping-pong signaling (two crossed signals, the Little Book of Semaphores patterns 1+2).

### Invariant

The i-th "bar" never prints before the i-th "foo"; the (i+1)-th "foo" never prints before the i-th "bar". At any moment, exactly one thread is "allowed".

### Mental model

Two doors again, but now the permit is a **baton**. There is exactly one baton in the system at all times. Foo starts holding it. Each thread: wait for baton → do my print → hand baton to the *other* door. Because `acquire()` consumes the permit, the door closes itself behind you, and that's what makes this reusable with zero reset logic.

Semaphore version: `fooGate = new Semaphore(1)`, `barGate = new Semaphore(0)`. foo loop: acquire fooGate, print, release barGate. bar loop: acquire barGate, print, release fooGate.

wait/notify version: one lock, one `boolean fooTurn = true`. Each side: while not my turn → wait; print; flip flag; notifyAll. Same baton, expressed as a flag.

### Correctness argument (say this in an interview)

Total permits across both semaphores is always exactly 1 (each release is preceded by an acquire). One baton → at most one thread proceeds at a time → strict alternation. Foo starts with it → foo prints first. Every acquire is eventually followed by a release → no thread waits forever.

### Pitfalls

1. **Both semaphores initialized to 1** → both threads can run simultaneously → interleaved garbage. The total-permits argument catches this instantly.
2. **Releasing your own gate instead of the other's** → you run twice in a row, other thread starves.
3. In the wait/notify version, forgetting `notifyAll` after flipping → the other thread sleeps forever with the flag set in its favor (lost wakeup).

### Check your understanding

1. State the "exactly one permit in the system" argument without looking. Why does it prove both mutual exclusion AND alternation?
2. Why does this design need no reset between iterations, when Print in Order was one-shot?
3. Extend to three threads printing A, B, C cyclically. How many semaphores, what initial values, and who releases whom? (This is exactly LC 1116 / Fizz Buzz's shape.)

### Transfers to

Zero-Even-Odd (LC 1116) = 3-way baton with a routing decision. FizzBuzz (LC 1195) = 4-way baton where the *state* (current number) decides who gets it. Odd-even printer = this exact problem with numbers.
