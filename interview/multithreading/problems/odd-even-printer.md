---
layout: post
title: Odd-Even Printer (classic warm-up)
date: 2026-07-19
description: >-
  This is FooBar wearing a different shirt, but it's THE most common first concurrency question in a live screen, and interviewers watch for four specific things: shared lock…
categories: interview multithreading problems
---

Part of the [Ordering & Turn-Taking](/interview/multithreading/patterns/ordering/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Classic phone-screen question (banks, Uber, everywhere).

### Problem

Two threads print numbers 1 to N: thread ODD prints 1, 3, 5...; thread EVEN prints 2, 4, 6... Output must be 1, 2, 3, 4, ... in order.

### Constraints

- Solve with `synchronized` + `wait()/notifyAll()`, interviewers ask this specifically to see if you know the monitor idiom, not semaphores.
- No busy-waiting, no sleeps.

### Clarify before solving

- Should each thread print its own numbers, or is any assignment fine? (Own numbers, that's the point.)
- Range known upfront? (Yes, 1..N.)

### Why this problem matters

This is FooBar wearing a different shirt, but it's THE most common first concurrency question in a live screen, and interviewers watch for four specific things: shared lock, `while` (not `if`), `notifyAll` after state change, and clean termination at N. It's your chance to demonstrate the condition-loop template is reflex.

---

## Strategy

### Classify

Ordering, 2 threads, repeated → baton via shared state ([Template 1](/interview/multithreading/mt-framework/)).

### Invariant

Only the thread whose parity matches the current number may print; the counter increases by exactly 1 per print.

### Mental model

Unlike FooBar (two semaphores), here use **one shared counter as the baton itself**. A single lock guards `current`. Each thread's turn is *derivable from the state*: ODD may go iff `current` is odd. So the condition loop is: lock; while (current has wrong parity) wait; print current; current++; notifyAll; unlock. Both threads run the same logic with opposite parity checks.

This is an important idea upgrade from FooBar: **the signal is not a separate object, the guarded state itself tells each thread whether it may proceed.** Most real-world concurrency looks like this.

### Termination (where most candidates stumble)

The loop must exit when current > N, including the thread that is *waiting* when the other thread finishes. Standard shape: outer loop `while (true)`, inside the lock re-check `if (current > N) { notifyAll(); return; }` after every wake, and also after incrementing. The final `notifyAll` before returning is what frees the other thread from its last wait. Forgetting it = program prints everything correctly then hangs. This exact bug is what the interviewer is waiting to see.

### Correctness argument

All reads/writes of `current` happen inside one monitor → no race, and monitor rules give visibility. Parity alternates on each increment → strict alternation. Every state change is followed by `notifyAll` → no lost wakeup. Exit condition checked under the lock after every wake → clean termination.

### Pitfalls

1. `if` instead of `while` → spurious wakeup prints wrong parity or double-prints.
2. `notify()`, works here with only 2 threads but say why you default to `notifyAll` anyway (with 2 threads notify happens to be safe; the habit isn't).
3. Checking `current > N` outside the lock → race on the last number.
4. Two locks (one per thread) → they can't guard one variable; instant race. One shared state = one lock, always.

### Check your understanding

1. Explain "the state is the signal", how does this differ from FooBar's two semaphores, and when is each style better?
2. Reconstruct the termination handling from scratch: which thread can be left sleeping, and what statement frees it?
3. Generalize to N threads round-robin (thread i prints i, i+N, 2i+N...): what changes? (Answer shape: condition becomes `current % T == myId`; everything else identical, that's bank #20 solved for free.)

### Transfers to

N-thread round-robin printing (direct generalization), FizzBuzz, any "take turns based on shared state" question.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/ordering/odd-even-printer).
