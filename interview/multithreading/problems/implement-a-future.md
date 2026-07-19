---
layout: post
title: Implement a Future / Promise
date: 2026-07-19
description: >-
  Demystifies CompletableFuture the way implement-a-semaphore demystified permits: a future is a one-shot guarded state machine (PENDING → COMPLETED/FAILED) + a…
categories: interview multithreading problems
---

Part of the [Task Lifecycle](/interview/multithreading/patterns/task-lifecycle/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Banks / senior screens ("build a Promise with wait/notify"); bridges Week-2 skills to the async world.

### Problem

Implement `MyPromise<T>` with:

- `T get()`: block until a result is available, then return it (every subsequent get returns immediately).
- `T get(long timeoutMs)`: same, but give up after the timeout.
- `complete(T value)`: deliver the result; only the FIRST completion wins.
- `completeExceptionally(Throwable t)`: deliver a failure; get() rethrows it.
- Follow-up: `thenApply(Function<T,R>)`: register a callback that runs when the value arrives (returning a new MyPromise).

Using only `synchronized`/`wait`/`notifyAll`.

### Constraints

- Many threads may call get() concurrently; many may race to complete(), exactly one wins.
- A completion BEFORE any get() must not be lost (the persistence property again).
- Callbacks registered after completion run immediately; registered before, run at completion. No callback runs twice.

### Clarify before solving

- Which thread runs the callbacks? (Simplest and acceptable: the completing thread, but SAY it, it's the design decision the follow-up exists for.)
- Is cancel() in scope? (Usually not; one sentence about cancelled-as-a-third-terminal-state suffices.)

### Why this problem matters

Demystifies CompletableFuture the way implement-a-semaphore demystified permits: a future is **a one-shot guarded state machine (PENDING → COMPLETED/FAILED) + a wait-for-terminal-state condition loop + a callback list drained exactly once**. After building it, thenApply/allOf stop being API trivia. You know what's inside. Also the cleanest place to see "exactly-once completion" as a check-then-act race you already know how to kill.

---

## Strategy

### Classify

One-shot guarded state machine + condition loop + callback registry. Everything is Week-2 machinery; the only new idea is draining callbacks exactly once.

### Invariant

State moves PENDING → exactly one of {COMPLETED(value), FAILED(error)}, once, forever; every get() returns/throws per the terminal state; every registered callback runs exactly once, after the terminal state exists.

### Mental model

A sealed envelope on a notice board. Waiters sleep by the board; the first person to pin an envelope wins (later pinners are ignored); pinning wakes everyone; anyone arriving later just reads it, the envelope persists (one-shot latch semantics: like CountDownLatch(1) carrying a payload). Callback slips taped to the board before the envelope arrives get executed by the pinner; slips added after are executed by whoever tapes them.

### Design

State under one lock: `status` (enum), `value`, `error`, `callbacks` (list).

- **get()**: lock; `while (status == PENDING) wait();` then return value or rethrow error (wrapped, say you'd mirror ExecutionException semantics). The condition-loop template with predicate "terminal state reached".
- **complete(v)**: lock; `if (status != PENDING) return false;` set value + status; `notifyAll()`; grab-and-clear the callback list; unlock; **run callbacks OUTSIDE the lock**. The `if != PENDING → return` under the lock IS the exactly-once guard, check-then-act made atomic, the same kill you've used all along.
- **completeExceptionally**: mirror.
- **timed get()**: `wait(remaining)` in the loop, recomputing remaining from `nanoTime` each wake (three wake reasons (timeout, notify, spurious), identical treatment: re-check; this is the timed-wait discipline from the scheduler, in miniature). On expiry with status still PENDING → throw TimeoutException.
- **thenApply(f)**: lock; if PENDING → add a callback that completes a child promise with f(value) (or forwards the error), return child; if already terminal → unlock, run f now, return an already-completed child. The register-or-run-now decision must be made under the lock, or a completion racing your registration drops the callback (a lost wakeup, callback-flavored).

### The design decisions that ARE the interview

1. **Callbacks outside the lock.** User code under your lock = alien call under lock (refresher bug #6): it can block, throw, or re-enter your promise (thenApply inside a callback → deadlock on a non-reentrant design, or surprise recursion on a reentrant one). Grab-list-then-release is the idiom.
2. **Who runs the callback**: completing thread (simple, but a slow callback now taxes the completer, CompletableFuture's non-Async behavior), or a handed-in executor (that's exactly what `thenApplyAsync(f, ex)` is). Naming this trade-off = understanding why the Async variants exist.
3. **Exactly-once**: both completion paths and the drain must go through the same PENDING check under the same lock. Two racing completes: one wins the CAS-like check, the loser returns false, narrate it.
4. **Error transparency**: callbacks receive failures too (forward to the child), or a failed parent with a value-only callback silently strands the child forever, the async version of a lost signal.

### Pitfalls

1. Running callbacks under the lock (see above, THE bug of this problem).
2. `if` instead of `while` in get(): spurious wakeup returns a null value from a PENDING promise.
3. Registration racing completion: check-then-add not atomic → callback never runs.
4. Timed get recomputing nothing: `wait(timeout)` returning early (notify/spurious) and treating return-as-expiry, or waiting the full original timeout again after each wake.
5. Second complete() overwriting the value: terminal states are immutable.
6. Losing the error path in thenApply chains: children of failed parents must fail, not hang.

### Check your understanding

1. Point to the exactly-once linearization point in complete(). What does the loser of the race observe?
2. Why must the register-vs-run-now decision in thenApply happen under the lock? Construct the dropped-callback interleaving.
3. What precisely goes wrong running callbacks under the lock if a callback calls thenApply on the same promise?
4. Map each piece to CompletableFuture: your callbacks list ↔ the completion stack; your run-now path ↔ calling thenApply on a completed future; your executor decision ↔ Async variants; your error forwarding ↔ exceptionally/handle.
5. How would cancel() fit? (Third terminal state; complete-family methods all lose to it; get throws CancellationException, one more branch, zero new machinery.)

### Transfers to

Every CompletableFuture API question (you've built the internals), the cached-future idiom (the cache stores exactly this object, the waiting point before the value), and "how does async signaling work under the hood" follow-ups.
