---
layout: post
title: The Uber Ride Problem
date: 2026-07-19
description: >-
  H2O with a twist that changes the design: multiple valid compositions, so admission can't be static semaphore capacities, someone must DECIDE which composition to form…
categories: interview multithreading problems
---

Part of the [Group Formation](/interview/multithreading/patterns/group-formation/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Uber interviews via [Educative](https://www.educative.io/courses/java-multithreading-for-senior-engineering-interviews/uber-ride-problem). High frequency at Uber. Same shape as the Little Book of Semaphores River Crossing.

### Problem

Riders are Democrats or Republicans, each on their own thread calling `seatDemocrat()` / `seatRepublican()`. A car seats exactly 4 and may depart only with an acceptable composition: **4 Democrats, or 4 Republicans, or 2 of each.** (Never 3+1.) A rider blocks until they're part of a departing car. Exactly one rider per car calls `drive()` after all 4 are seated.

### Constraints

- Compositions other than 4D / 4R / 2D+2R must never depart: 3+1 is the forbidden case.
- All 4 riders seated before drive(); next car's riders must not seat until the current car departs.
- Exactly one drive() call per car.

### Clarify before solving

- Must a rider take the FIRST car they could legally join, or may the system hold them? (Standard: greedy, form a car as soon as any valid composition exists among waiters.)
- Fairness/FIFO among same-party riders? (Not required, say you noticed.)

### Why this problem matters

H2O with a twist that changes the design: **multiple valid compositions**, so admission can't be static semaphore capacities: someone must DECIDE which composition to form, based on who's waiting. That "decider" role (the last rider completing a valid group) is the new concept. Nail this and you've covered the hardest thing Uber commonly asks in this space.

---

## Strategy

### Classify

Group formation with a CHOICE of compositions. Compare H2O out loud: there, capacities (2,1) statically encode the only valid group, so semaphores alone do admission. Here 4D, 4R, 2+2 are all legal. Static capacities can't express "or". **New requirement: a decision point.**

### Invariant

Each departing car seats exactly one of {4D, 4R, 2D+2R}; seat-to-departure is exclusive per car (no mixing of two cars' riders); exactly one drive() per car.

### Mental model

A dispatcher's waiting lounge. Arriving riders join their party's line and doze. Each arrival ALSO checks the whiteboard tallies: "does my arrival complete a valid car?" If yes, THIS rider becomes the **dispatcher**: picks the composition, wakes exactly the right sleepers (3 others), everyone boards, dispatcher calls drive(). If no, doze. The dispatcher role is not a separate thread, it's a hat the completing rider wears. (You've seen this "one waiter plays a special role" move in Dining Savages.)

### Design

State under one mutex: `waitingD`, `waitingR` counters. Gates: `demGate = Semaphore(0)`, `repGate = Semaphore(0)`. Boarding sync: `CyclicBarrier(4)` (riders of one car meet before drive).

seatDemocrat (Republican mirrors):

1. mutex.acquire; waitingD++.
2. Check completions in a fixed order. Am I the 4th of: (a) waitingD ≥ 4 → release demGate×3, waitingD -= 4, I'm dispatcher; (b) waitingD ≥ 2 && waitingR ≥ 2 → release demGate×1 + repGate×2, waitingD -= 2, waitingR -= 2, dispatcher.
3. If dispatcher: **hold the mutex through the wake-ups and decrements** (they're one atomic decision), release mutex, await barrier, drive().
4. If not: release mutex, acquire my party's gate (doze), await barrier, seat.

Key correctness points, each worth saying in the interview:

- **Decide-and-decrement atomically under the mutex.** Tallies must be reduced the moment the composition is chosen, so a simultaneous arrival can't count the same waiters into a second car. The gate-releases target exactly the chosen composition. Permits are "boarding passes", counted precisely.
- **The barrier prevents drive() before all 4 seat.** Permits only say "you're selected"; seated-ness needs the rendezvous.
- **Why no 3+1 ever departs**: compositions are only ever formed by the two checks above; neither can select 3+1. The forbidden case is excluded by construction, not by checking: the strongest kind of argument.
- **Car boundary**: the next car's dispatcher can only be an arrival that finds tallies AFTER this car's decrements. Selected riders are already excluded. The barrier is reusable (CyclicBarrier) for successive cars. If pressed on strict "no seating during boarding", add a boarding lock held dispatcher-to-drive; base version doesn't need it: the tallies already exclude selected riders. Don't add it unprompted.

### Pitfalls

1. Checking completion without decrementing under the same lock → double-selection of the same waiters by two racing dispatchers. THE bug of this problem.
2. Static semaphore capacities à la H2O → cannot express "or"; forces 2+2 always or deadlocks on 4-of-a-kind. If you catch yourself doing this, you've missed the problem's point.
3. Dispatcher releases 4 permits including themselves → 5 riders in a car (dispatcher doesn't need a permit, they never doze).
4. drive() by everyone / by the wrong rider: dispatcher-only, decided at selection time.
5. Check order (a) before (b) is a policy choice (prefer same-party cars); name it, don't agonize. Greedy correctness is what's asked.

### Check your understanding

1. Why exactly can't semaphore capacities alone solve this, when they solved H2O? One sentence.
2. Construct the double-count bug: two riders arrive simultaneously completing overlapping compositions. What state protection kills it?
3. Why 3 permits, not 4, in case (a)? Who is the 4th?
4. Prove no 3+1 car: what's the exhaustive argument?
5. River Crossing (the Little Book of Semaphores): 4-person boat, no 1+3 of hackers/serfs. Map every piece of your design onto it. (It's the same problem, verifying that IS the understanding test.)

### Transfers to

River crossing (identical), roller coaster (single composition, capacity batch), any "match players into a valid game lobby" / "batch requests by compatible type" design. The dispatcher-hat idea reappears across matchmaking systems.
