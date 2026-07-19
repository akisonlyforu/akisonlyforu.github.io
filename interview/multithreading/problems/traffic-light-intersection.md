---
layout: post
title: Traffic Light Controlled Intersection (LC 1279)
date: 2026-07-19
description: >-
  Smallest possible "protect one piece of shared state" problem — the shared state is just whichRoadIsGreen. Worth 20 minutes to see that not every concurrency problem needs…
categories: interview multithreading problems
---

Part of the [Guarded State](/interview/multithreading/patterns/guarded-state/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [LeetCode 1279](https://leetcode.com/problems/traffic-light-controlled-intersection/) (premium). Verdict: Low frequency — do once for completeness.

### Problem

An intersection of road A (cars 1: left→right, 2: right→left) and road B (cars 3, 4). One traffic light per road; exactly one light is green at any time. Cars arrive on threads and call `carArrived(carId, roadId, direction, turnGreen, crossCar)`. A car may cross only when its road's light is green; if it's red, turn it green (which turns the other red).

### Constraints

- Lights must never both be green.
- Cars on a green road cross without touching the light.

### Clarify before solving

- Is fairness required (can road A hog the light)? (Not required by the problem — say you noticed.)
- Can two cars on the same green road cross concurrently? (Problem allows it; crossCar is provided by the harness.)

### Why this problem matters

Smallest possible "protect one piece of shared state" problem — the shared state is just `whichRoadIsGreen`. Worth 20 minutes to see that not every concurrency problem needs patterns: sometimes one mutex around one variable IS the whole answer, and recognizing that is itself the skill.

---

## Strategy

### Classify

Pure guarded state. One shared variable: `greenRoad`. The only hazard is check-then-act on it.

### Invariant

At most one road's light is green; a car crosses only while its road is green.

### Mental model

The race: car on road B sees red → decides to flip the light — but between "see" and "flip", a car on road A also checked. Without atomicity, both could act on stale views and you'd get interleaved green-flips mid-crossing. So: the check ("is my road green?"), the flip (turnGreen), and the crossing must happen under **one mutex**. Then the invariant holds trivially — that's the entire solution: lock; if greenRoad != myRoad { turnGreen; greenRoad = myRoad; } crossCar; unlock.

### The design discussion worth having

Holding the lock *during* crossCar serializes all crossings — even two cars on the same green road, which the problem permits concurrently. The simple version is correct but conservative. Optimizing (letting same-road cars cross in parallel) needs a reader-writer-flavored design: same-road cars share access, the flip is exclusive — recognizable as a lightswitch. Interview answer: ship the simple version, NAME the conservatism, describe the optimization only if asked. Correct-and-simple first is the senior move on a Low-frequency question.

### Correctness argument

All reads and writes of `greenRoad`, plus the crossing, are inside one mutex → no interleaving can observe or produce two greens; a car crosses strictly after its road became green and before it could change.

### Pitfalls

1. Checking the light outside the lock, then locking to flip — the classic check-then-act race, the exact bug this question exists to catch.
2. Two locks (one per road) — the invariant spans both lights; one invariant, one lock.
3. Over-engineering the parallel-crossing version unprompted, burning your 20 minutes.

### Check your understanding

1. Where exactly is the check-then-act race if the check is unlocked? Give the interleaving.
2. Why one lock and not one per road?
3. How would the lightswitch pattern make same-road crossings parallel? What new starvation risk appears? (A road with endless traffic never yields — writer-starvation in disguise.)

### Transfers to

Any "flip shared mode safely" problem; a warm-up for readers-writers (05) where the parallel version of this exact idea is developed fully.
