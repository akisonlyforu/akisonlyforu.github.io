---
layout: post
title: Reusable Barrier (two-turnstile)
date: 2026-07-19
description: >-
  The one-shot barrier is easy; making it REUSABLE is the famous part — the naive reset has a subtle bug (a fast thread lapping into the next round and stealing a permit from…
categories: interview multithreading problems
---

Part of the [Group Formation](/interview/multithreading/patterns/group-formation/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Little Book of Semaphores ch. 3. Week 3 milestone in the study plan.

### Problem

N threads each run a loop of rounds. In every round, each thread does some work, then waits at a barrier until ALL N have finished the round; only then may any thread start the next round. Build the barrier so it works correctly round after round.

### Constraints

- No thread may enter round k+1's critical point before every thread has passed round k's barrier.
- The same barrier object is reused every round — no re-allocating per round.
- Semaphores + a counter only (this is the the Little Book of Semaphores exercise; you'll compare with CyclicBarrier after).

### Clarify before solving

- Fixed, known N. All threads loop the same number of rounds.

### Why this problem matters

The one-shot barrier is easy; making it REUSABLE is the famous part — the naive reset has a subtle bug (a fast thread lapping into the next round and stealing a permit from the current one), and both known fixes (two turnstiles here, generations in implement-latch-or-barrier) teach the same deep idea: **waiters must be able to tell which round a signal belongs to.** Everything in this category (H2O, Uber Ride, roller coaster) stands on this mechanism.

---

## Strategy

### Classify

Group formation, N fixed, repeated rounds. Pattern 5 with the reuse problem front and center.

### Invariant

No thread starts round k+1 until all N passed round k's barrier; each round's "gate opening" releases exactly the N threads of that round.

### Build it in three attempts (do each, break each)

**Attempt 1 — count + gate.** Mutex-guarded `count`; each arrival increments; the N-th does `gate.release(N)`; everyone does `gate.acquire()`. Works ONCE. For round 2: count needs resetting — who resets it, and when? Race everywhere you put it.

**Attempt 2 — reset by the last-through.** Last thread through resets count = 0. Bug (find it yourself first): thread A passes the gate, LAPS the loop, arrives at the barrier for round 2 and increments count before slow thread B has even woken from round 1's acquire. Count is now polluted across rounds; worse, A can acquire a permit that was released FOR B — B sleeps forever. One permit pool, two rounds' waiters: the signals don't say which round they belong to. Same disease as the CyclicBarrier generation bug — one mechanism, two costumes.

**Attempt 3 — two turnstiles (the the Little Book of Semaphores fix).** Two gates. Gate 1 admits threads INTO the checkpoint: opens when all N have arrived, and — crucially — gate 2 is confirmed closed before anyone proceeds. Gate 2 releases them OUT and re-arms gate 1. A fast thread lapping around finds gate 1 CLOSED again (it closed behind the group) and must wait for the next full muster — it cannot lap into the current round. Mechanically: first counter/turnstile opens exit when count hits N; second turnstile (count back down to 0) reopens entry. The two phases can never overlap, which is exactly the guarantee attempt 2 lacked.

### The idea to extract

A reusable coordination point needs **phase separation**: arriving-for-round-k and leaving-round-k must be distinguishable states, or fast threads corrupt slow ones. Two turnstiles separate the phases physically; CyclicBarrier's generation token separates them logically; both answer the waiter's real question — "is MY round done?" — rather than "what's the count?".

### Correctness sketch

Phase 1: entry open, exit closed; nobody proceeds until N arrive. Flip: exit opens, entry closed. Phase 2: exactly the N in-flight threads drain through exit; last one flips back. A lapping thread meets a closed entry — blocked until next round legitimately forms. No cross-round permit theft is possible because permits for exit exist only during phase 2 and are consumed exactly N times before entry reopens.

### Pitfalls

1. Shipping attempt 2 — it passes light tests; only loop-heavy stress with mixed thread speeds exposes it. This is the study plan's "a passing stress test never proves correctness" lesson in the flesh.
2. Forgetting the mutex on the counter (increment isn't atomic).
3. In interviews: hand-rolling this when `CyclicBarrier` exists and semantics match. Build it here to understand; USE CyclicBarrier in answers ([Template 4](/interview/multithreading/mt-framework/)), and let "reusable → CyclicBarrier or two-turnstile, here's why naive reset breaks" be your spoken depth.

### Check your understanding

1. Reconstruct attempt 2's permit-theft interleaving with N=2 (fast A, slow B), step by step, no notes.
2. In the two-turnstile design, exactly where is a lapping fast thread stopped, and by what state?
3. State the shared principle behind two-turnstiles and CyclicBarrier generations in one sentence.
4. Why does H2O (barrier of 3, reused per molecule) need this and not a one-shot barrier?

### Transfers to

H2O, Uber Ride, roller coaster — all reuse a barrier across groups. This mechanism is the single most important thing in Type D.
