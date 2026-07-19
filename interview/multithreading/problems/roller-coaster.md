---
layout: post
title: Roller Coaster (LBoS ch. 5)
date: 2026-07-19
description: >-
  Introduces the coordinator thread shape: a service thread that gathers a batch, processes it, and drains it, in strict phases. This is a real production pattern (batch…
categories: interview multithreading problems
---

Part of the [Group Formation](/interview/multithreading/patterns/group-formation/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Little Book of Semaphores ch. 5. Worth doing: maps to "batch processor" questions.

### Problem

One roller-coaster car with capacity C. Passenger threads call `board()`; the single car thread loops: wait for C passengers, `run()` the ride, then let everyone `unboard()`, repeat. Rules:

- Passengers board only when the car is boarding; the car runs only when full (exactly C).
- Passengers unboard only after the ride; the car reopens boarding only after all C have unboarded.

### Constraints

- Two synchronized phases per cycle (board → ride → unboard) that must not overlap or mix across cycles.
- The car is an ACTIVE thread with its own loop — new versus H2O/Uber where groups self-organize.

### Clarify before solving

- One car (multi-car is the famous harder variant — mention, don't attempt).
- Exactly C, not "up to C" (a batch-timeout variant is a good follow-up discussion).

### Why this problem matters

Introduces the **coordinator thread** shape: a service thread that gathers a batch, processes it, and drains it, in strict phases. This is a real production pattern (batch writers flushing every N items, micro-batching in stream processors) and the two-phase (load/drain) structure is the reusable-barrier lesson applied twice per cycle.

---

## Strategy

### Classify

Group formation with an explicit coordinator (the car). Unlike Uber Ride, no dispatcher-hat trick needed — the car IS a dedicated dispatcher thread. Simpler in one way (decisions live in one thread), new in another (two cross-thread phase handshakes per cycle).

### Invariant

Ride runs only with exactly C boarded; no boarding during ride or unboard; cycle k's passengers all unboard before cycle k+1 boards anyone.

### Mental model

Airport gate agent. Phase 1: agent opens boarding, C passengers scan through (counted), agent waits for the count. Phase 2: flight (passengers passive). Phase 3: deplane, agent counts everyone off, then resets the gate for the next flight. The agent's waits and the passengers' waits interlock like gears.

### Design

Semaphores: `boardQueue = Semaphore(0)` (car→passengers: "boarding open, C permits"), `allAboard = Semaphore(0)` (last boarder→car), `unboardQueue = Semaphore(0)` (car→passengers: "deplane"), `allAshore = Semaphore(0)` (last unboarder→car). Mutex-guarded `boarded` counter (and one for unboarded).

- Car loop: boardQueue.release(C) → allAboard.acquire() → run() → unboardQueue.release(C) → allAshore.acquire() → loop.
- Passenger: boardQueue.acquire() → board() → mutex: boarded++; if boarded == C { boarded = 0; allAboard.release(); } → unboardQueue.acquire() → unboard() → mutex: unboarded++; if C { reset; allAshore.release(); }.

Study the shape: each phase is "coordinator releases C permits; last participant through signals back." That C-permits-out / 1-signal-back handshake is the multiplex + barrier ideas fused, used twice. The car's `allAshore.acquire()` before looping is what enforces the cycle boundary — remove it and cycle k+1's boarding permits mix with cycle k's stragglers (the lapping bug, third appearance).

### Why this is safe against lapping

A passenger from cycle k who finished unboarding and loops back to ride again blocks at `boardQueue.acquire()` — no permits exist until the car completes `allAshore.acquire()` and releases the next batch. Boarding permits are only ever issued after the previous cycle fully drains. Phase separation by permit issuance timing.

### Pitfalls

1. Car releasing next boardQueue permits before allAshore → mixing cycles (find this yourself in a broken version — it's instructive).
2. Passenger counting without the mutex → two "last" passengers, allAboard released twice → next cycle's accounting corrupted (semaphore permits are counted — extras don't vanish; this poisons later cycles, not this one — which is what makes it evil to debug).
3. Counter reset by the car instead of the last passenger, or vice versa, inconsistently — pick one owner per counter.
4. Multi-car variant unprompted — it needs per-car ordering; explicitly out of scope; mention it exists.

### Check your understanding

1. Trace one full cycle with C=2 and a third passenger arriving mid-ride: where does the third wait, at which primitive?
2. What corrupts if `allAboard.release()` happens at boarded == C but WITHOUT resetting boarded, and when does the corruption bite? (Next cycle — delayed-detonation bugs are why counted permits demand care.)
3. Map to a batch write buffer: what plays the car, boardQueue, allAboard? Where would a flush-timeout slot in? (Car waits on allAboard with a timeout, runs with a partial batch — one-line policy change, big semantics change: "exactly C" becomes "up to C or T ms". This is the batch-processor interview question.)

### Transfers to

Batch processors, micro-batching pipelines, "gather N results then aggregate" fan-in — and the coordinator-thread shape generally (contrast with Uber's coordinator-hat: know when each fits — dedicated servicing loop vs symmetric peers).
