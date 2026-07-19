---
layout: post
title: Reader-Writer Lock (implement one)
date: 2026-07-19
description: >-
  Home of the lightswitch pattern, first-in locks the room, last-out unlocks, which is the reusable trick for "a GROUP holds a lock collectively". And the starvation…
categories: interview multithreading problems
---

Part of the [Asymmetric Access](/interview/multithreading/patterns/asymmetric-access/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Classic, the Little Book of Semaphores ch. 4, Educative. High frequency.

### Problem

Implement a lock with two modes: `readLock()/readUnlock()` and `writeLock()/writeUnlock()`. Any number of readers may hold the lock simultaneously; a writer holds it exclusively (no readers, no other writers).

Follow-up (always comes): your version starves writers, fix it.

### Constraints

- Readers never block each other.
- Writer exclusivity is absolute.
- No busy-waiting.

### Clarify before solving

- Read-heavy workload assumed? (It's the only reason RW locks exist, say so.)
- Reentrant? Upgrade/downgrade? (Out of scope for the base; know that upgrade (read→write) deadlocks naively, two upgraders each wait for the other's read release.)
- Which starvation policy: readers-preference first, then fix for writers.

### Why this problem matters

Home of the **lightswitch** pattern, first-in locks the room, last-out unlocks, which is the reusable trick for "a GROUP holds a lock collectively". And the starvation follow-up is the cleanest venue to show you think about liveness, not just safety. The pattern reappears in caches, search/insert/delete, unisex bathroom, and the parallel-crossing version of traffic light.

---

## Strategy

### Classify

Asymmetric access. Two safety rules: readers exclude only writers; writers exclude everyone.

### Invariant

(activeReaders > 0) implies no active writer; at most one active writer; never both.

### Mental model: the lightswitch

A room whose door lock is controlled by a light switch: the FIRST person entering flips the light on (locks out the janitor/writer); people stream in and out freely; the LAST one out flips it off (janitor may enter). The group collectively holds one lock through two special members: first-in and last-out.

### Design v1: readers preference

State: `readers` counter + mutex guarding it; `roomEmpty = Semaphore(1)` (the room lock).

- writeLock: roomEmpty.acquire(). writeUnlock: release. (Writers are simple tenants.)
- readLock: mutex; readers++; if readers == 1 → roomEmpty.acquire() (first-in flips the switch, POSSIBLY BLOCKING, while holding the mutex: same deliberate move as Dining Savages; safe because the holder (a writer) never touches the reader mutex); mutex.release.
- readUnlock: mutex; readers--; if readers == 0 → roomEmpty.release(); mutex.release.

Verify v1: two readers, second sees readers==2, never touches roomEmpty: parallel reads ✓. Reader then writer, writer blocks on roomEmpty until last reader out ✓. Writer then reader, first reader blocks inside readLock holding the reader-mutex, so subsequent readers queue behind ✓.

### The starvation problem (the actual interview)

In v1, readers arriving while a writer WAITS still get in (they see readers ≥ 1 and stream past roomEmpty). Continuous reader traffic → the writer never sees an empty room. Safety intact; liveness broken for writers. State it unprompted.

**Fix, the turnstile.** Add `turnstile = Semaphore(1)`. Every thread (reader or writer) must pass it: readers acquire+release immediately on entry to readLock (walk through); a writer acquires it and HOLDS it until after its write completes. Effect: a waiting writer blocks the turnstile → new readers pile up OUTSIDE, existing readers drain, room empties, writer proceeds; on writeUnlock the turnstile reopens and the piled-up readers flood in. Writers no longer starve. (This inverts the bias: a stream of writers can now starve readers, full fairness needs FIFO queuing à la ReentrantReadWriteLock(fair). Name the residual imbalance; don't build fairness.)

### The production answer

`ReentrantReadWriteLock` (know: nonfair default, fair mode, upgrade unsupported/deadlock, downgrade supported) or `StampedLock` for optimistic reads (mention only). Hand-roll when asked to; reach for the JDK in design answers. Also say when RW locks LOSE: short critical sections or write-heavy load, the bookkeeping costs more than it saves; plain mutex wins.

### Pitfalls

1. readers++ outside the mutex, torn counter, two "first" readers both acquiring roomEmpty: deadlock.
2. Last-out forgetting roomEmpty.release (early-return/exception path), writers locked out forever. In real code: unlock in finally.
3. Claiming the turnstile version is "fair", it's writer-preferring, not fair.
4. Offering upgrade support casually, naive upgrade deadlocks; say why (two readers both waiting to upgrade → each waits for the other to release read).

### Check your understanding

1. Recite the lightswitch: who flips on, who flips off, why must the counter be mutex-guarded?
2. Construct writer starvation in v1 as a concrete timeline of overlapping readers.
3. Walk the turnstile fix: writer arrives during 3 active readers + readers keep arriving. What happens to each party, in order?
4. Why is blocking on roomEmpty while holding the reader-mutex safe here? (Same dependency argument as Dining Savages, writer never needs that mutex. If you can transfer the argument, it's yours.)
5. When would you refuse an RW lock in favor of a plain mutex?

### Transfers to

Read-heavy cache (07), search/insert/delete, unisex bathroom (categories as "reader groups"), traffic-light parallel crossing, and conceptual #14/#15.
