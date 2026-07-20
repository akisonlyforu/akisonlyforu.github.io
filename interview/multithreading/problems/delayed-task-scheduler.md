---
layout: post
title: Delayed Task Scheduler
date: 2026-07-19
description: >-
  The bounded blocking queue with time as a third dimension: consumers wait not just for "an item" but for "the earliest item's TIME". The new skill is the timed wait that can…
categories: interview multithreading problems
---

Part of the [Time-Based State](/interview/multithreading/patterns/time-based/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** FAANG LLD. Med-High frequency.

### Problem

Implement `schedule(Runnable task, long delayMs)`: run each task after (approximately, never before) its delay elapses. One or more worker threads execute due tasks. Support many pending tasks with different due times, scheduled concurrently.

### Constraints

- A task never runs EARLY; runs as close to due time as practical.
- Workers must not busy-poll the clock.
- New tasks can arrive at any time, including one that becomes the new EARLIEST (the crux).

### Clarify before solving

- One worker or several? (Start with one; N is a small extension.)
- Recurring tasks? (Extension, re-schedule after run.)
- What if a task's due time passed while workers were busy? (Run ASAP, late is acceptable; never early.)

### Why this problem matters

The bounded blocking queue with time as a third dimension: consumers wait not just for "an item" but for "the earliest item's TIME". The new skill is the **timed wait that can be preempted by a newer, earlier task**, `Condition.awaitNanos` + re-check loop. This is how `ScheduledThreadPoolExecutor` and every timer wheel/cron system works at heart; interviewers use it to see whether your condition-loop discipline survives adding a clock.

---

## Strategy

### Classify

Producer-consumer where readiness is temporal: an item is consumable only when `now >= dueTime`. Structure: priority queue (min-heap by dueTime) + lock + condition + timed wait.

### Invariant

No task runs before its due time; the earliest due task is the next to run; every scheduled task eventually runs.

### Mental model

An oven timer shelf. Dishes (tasks) carry due-stamps; the cook (worker) always inspects the SOONEST dish: not due yet → nap exactly until its due time; due → take and serve. Someone slides in a dish due EVEN SOONER → poke the cook awake to re-inspect. The poke-on-earlier-arrival is the entire difficulty.

### Design

`PriorityQueue<Task>` ordered by dueTime, `ReentrantLock`, one `Condition available`.

- Worker loop: lock; loop { if queue empty → available.await(); else peek earliest: delay = due − now; if delay > 0 → **available.awaitNanos(delay)**; else → poll, unlock, RUN OUTSIDE THE LOCK, relock/continue }.
- schedule(): lock; offer; **if the new task is now the head → available.signal()** (an earlier-than-everything task arrived; the napping worker's timer is too long, wake it to re-inspect). Signaling always is simpler and merely wastes a wakeup; know both and say the trade-off.

Why the re-check loop is load-bearing (this is the interview): a worker waking from awaitNanos knows NOTHING, maybe its task is due, maybe an earlier task arrived, maybe spurious wakeup, maybe another worker (N>1) already grabbed the head. So: wake → re-peek → recompute → decide. Never carry assumptions across a wait. Your Template-1 `while` discipline, now with three reasons instead of one.

### Details that separate seniors

1. **Run tasks OUTSIDE the lock.** Holding it during task.run() blocks all scheduling and other workers for the task's duration. (Same principle as "don't hold locks across I/O".)
2. **nanoTime for arithmetic**: wall clock jumps (same point as rate limiter; it recurs on every time-based design).
3. **Multiple workers work unchanged**: the poll-under-lock makes task claiming atomic; a woken worker finding the head gone just re-loops. Mention `ScheduledThreadPoolExecutor`'s leader-follower refinement (one designated waiter, others park unconditionally, avoids thundering timed-waiters) as awareness, don't build it.
4. **Ties**: equal due times → break by sequence number for FIFO fairness (and heap comparator stability).
5. **Production answer**: `ScheduledThreadPoolExecutor` / `DelayQueue`, this exercise is their internals. Say it.

### Pitfalls

1. `sleep(delay)` instead of awaitNanos, a sleeping thread can't be signaled about a new earlier task (holds no lock, hears no condition): the new task runs LATE by up to the old delay. The central bug this question hunts.
2. Busy-polling the head every few ms, "no busy-wait" applies to time too.
3. Running the task under the lock (see above).
4. schedule() forgetting to signal when the head changes → same late-run bug as #1 by another path.
5. awaitNanos returning early (spurious/racing signal) treated as "task is due", must recompute from the clock, never trust the wakeup.

### Check your understanding

1. Walk the earlier-task-arrives scenario: worker napping 60s for task A; task B due in 1s is scheduled. Trace both threads through lock/condition to B running on time.
2. Three reasons a timed waiter must re-check everything on wake, list them.
3. Why does multi-worker "just work" here, and what's the thundering-herd inefficiency the leader-follower pattern fixes?
4. Extend to recurring tasks in one sentence. (After run, recompute due, re-offer, signal-if-head, reuse everything.)
5. What pair of JDK classes makes this whole exercise a two-liner, and what does each contribute?

### Transfers to

Timer wheels, cron systems, retry-with-backoff queues, cache TTL eviction threads, heartbeat monitors, all are this loop. Completes the arc: guarded state → + queue → + time. Nothing in the interview canon is structurally beyond this.

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/time-based/delayed-task-scheduler).
