---
layout: post
title: Print Zero Even Odd (LC 1116)
date: 2026-07-19
description: >-
  The baton now has a routing decision: after zero prints, it must decide whether to hand the baton to the odd door or the even door. This teaches you that the signaler can…
categories: interview multithreading problems
---

Part of the [Ordering & Turn-Taking](/interview/multithreading/patterns/ordering/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [LeetCode 1116](https://leetcode.com/problems/print-zero-even-odd/)

### Problem

Three threads share an instance: thread A calls `zero()`, thread B calls `even()`, thread C calls `odd()`. Output must be `0102030405...` for input n — i.e., zero before every number, odd and even numbers from their own threads.

For n = 5: output `0102030405`.

### Constraints

- The zero thread prints all the zeros; odd/even threads print only their numbers.
- Reusable across the whole sequence; no busy-waiting.

### Clarify before solving

- Exact target sequence for a small n (write out n=3: `010203` — zero, odd(1), zero, even(2), zero, odd(3)).
- Zero always goes first and between every number.

### Why this problem matters

The baton now has a **routing decision**: after zero prints, it must decide whether to hand the baton to the odd door or the even door. This teaches you that the signaler can pick *which* waiter to wake — the first step from pure alternation toward state-driven coordination.

---

## Strategy

### Classify

Ordering with 3 parties and a router → signaling with targeted handoff.

### Invariant

Sequence is strictly: zero, number, zero, number... Zero runs exactly n times; between two zeros exactly one number prints; parity of the printed number alternates starting at odd.

### Mental model

Three doors: zeroGate (starts open, 1 permit), oddGate (closed), evenGate (closed). Still exactly one baton total.

- Zero thread, iteration i (1..n): acquire zeroGate, print 0, then **route**: if i is odd → release oddGate, else → release evenGate.
- Odd thread, for each odd number: acquire oddGate, print it, release zeroGate.
- Even thread: mirror image.

The routing logic lives in the zero thread because it's the one that knows the loop index. Odd/even threads are dumb: wait, print, hand back.

### Correctness argument

One permit total across three semaphores (verify: every acquire pairs with exactly one release). Zero holds it first. Zero's release targets exactly one number-thread; that thread's release targets only zeroGate. So the cycle is forced: 0 → number → 0 → number, and the router's parity check forces 1, 2, 3... ordering.

### Pitfalls

1. **Routing from the number threads instead of zero** — both odd and even trying to decide "whose turn next" duplicates state and invites off-by-one races. Keep the decision in one place.
2. **Loop bounds**: odd thread loops ceil(n/2) times, even floors n/2. Getting these wrong leaves a thread blocked forever at the end (hang after correct-looking output — test with odd AND even n).
3. wait/notifyAll version: a single `state` variable (whose turn: ZERO/ODD/EVEN) + current number. All three wait on one lock; `notifyAll` is mandatory since the wrong thread may wake — its while-loop re-check handles it. This version is more forgiving of routing mistakes but wakes threads needlessly. Know both; say the trade-off out loud.

### Check your understanding

1. Why must the router be the zero thread (single decision point)? What specifically could go wrong with two deciders?
2. Trace n=2 through your design door-by-door: which permits exist at each step?
3. If you used one semaphore for both odd and even ("numberGate"), what breaks? (Wrong thread can grab the turn — explain why semaphores can't target a *specific* waiter but separate semaphores can.)

### Transfers to

FizzBuzz Multithreaded (LC 1195) — identical shape, 4 doors, router decides among fizz/buzz/fizzbuzz/number based on divisibility.
