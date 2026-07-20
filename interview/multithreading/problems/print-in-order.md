---
layout: post
title: Print in Order (LC 1114)
date: 2026-07-19
description: >-
  The scheduler gives you zero ordering guarantees between threads. The only way one thread runs "after" another is if it waits for a signal that the other thread sends. Every…
categories: interview multithreading problems
---

Part of the [Ordering & Turn-Taking](/interview/multithreading/patterns/ordering/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Difficulty:** Easy, but it is the atom of all concurrency. Everything else builds on this.
**Source:** [LeetCode 1114](https://leetcode.com/problems/print-in-order/)

### Problem

A class has three methods: `first()`, `second()`, `third()`. Three different threads will each call one of them, you don't control which thread runs when, or in what order the scheduler starts them.

Guarantee that `first()` always executes before `second()`, and `second()` before `third()`, no matter how the threads are scheduled.

```
Input: threads call in order (third, first, second)
Output: still prints "first" "second" "third"
```

### Constraints and rules

- You cannot change the calling code; you only control the class internals.
- Each method is called exactly once.
- Busy-waiting (spinning in a loop checking a flag) is considered a wrong answer in an interview even if it "works".

### Clarify before solving (say these out loud in an interview)

- Called once or repeatedly? (Here: once, so one-shot primitives are fine.)
- Is blocking acceptable? (Yes, second/third must block until their turn.)

### Why this problem matters

The scheduler gives you zero ordering guarantees between threads. The only way one thread runs "after" another is if it **waits for a signal** that the other thread **sends**. Every ordering problem, however dressed up, reduces to: who waits, and who signals.

---

## Strategy

### Step 1: Classify

Ordering problem (Type A). "X must happen before Y" = signaling pattern (the Little Book of Semaphores pattern #1).

### Step 2: Invariant

`second()` must not run its body until `first()` has finished; `third()` must not run until `second()` has finished. Two "happened already?" facts, so two gates.

### Step 3: The mental model

Think of two closed doors:

- Door 1 sits in front of `second()`. Only `first()` can open it, and it does so as its last act.
- Door 2 sits in front of `third()`. Only `second()` can open it.

A thread arriving at a closed door **sleeps** (the OS parks it, no CPU burned). Opening a door wakes the sleeper. If the door was opened before the sleeper even arrived, the arriving thread walks straight through, this is the crucial property: **the signal must persist even if it's sent before anyone is waiting.** (This is exactly what a semaphore permit gives you, and what a naive boolean-flag-plus-nothing does not.)

### Step 4: Two ways to build the doors

**Way 1, Semaphores (cleanest fit).** Two semaphores, both starting at 0 permits ("door closed"). `first()`: do work, release door1. `second()`: acquire door1, do work, release door2. `third()`: acquire door2, do work. A release before the acquire is fine, the permit waits.

**Way 2, synchronized + wait/notifyAll (the fundamental skill).** One lock object, one shared `int stage = 1`. Each method enters the lock and loops: `while (stage != myTurn) wait();`, then does its work, increments `stage`, calls `notifyAll()`. The `stage` variable IS the persisted signal, which is why this works even if `third()` arrives first.

Do both ways. Way 2 teaches you the condition-loop template you'll reuse everywhere.

### Step 5: Why the naive attempts fail (understand each failure)

1. **Plain boolean flags, no lock/volatile:** thread B may never *see* thread A's write (visibility), and spinning on a flag burns CPU. Two separate sins: visibility and busy-waiting.
2. **`if` instead of `while` around `wait()`:** spurious wakeups exist, a thread can wake without being notified. The condition must be re-checked upon every wake.
3. **`notify()` instead of `notifyAll()`:** with three threads on one lock, `notify` might wake the *wrong* one (e.g., stage becomes 2 but `third()` gets woken); it re-checks, goes back to sleep, and nobody ever wakes `second()`. Hang.
4. **Sleeping to "give first() time to run"**: `Thread.sleep()` is a prayer, not a guarantee. Never coordinate with sleep.

### Verify (out loud)

- Adversarial order: `third()` arrives first → blocks at door 2 (0 permits / stage != 3). Then `second()` → blocks at door 1. Then `first()` → runs, opens door 1 → second runs, opens door 2 → third runs. Correct.
- No deadlock: waiting is one-directional (3 waits on 2 waits on 1; 1 waits on nobody).
- Signal persistence: if `first()` finishes before `second()` starts, the permit / stage value is sitting there waiting. No lost wakeup.

### Check your understanding (answer without looking)

1. Why does a semaphore initialized to 0 model a "closed door" and why does an early `release()` not get lost?
2. What exactly goes wrong with `if (stage != 2) wait();`, describe the interleaving.
3. Why is busy-waiting on a `volatile boolean` technically *correct* here but still a bad answer?
4. Follow-up they may ask: make it reusable for repeated first→second→third cycles. What breaks in your one-shot design, and what would need to reset?

### Transfers to

FooBar (LC 1115) = this pattern ping-ponging in a loop. Odd-even printer = same. Any "callback after init" or "step B after step A" question.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/ordering/print-in-order).
