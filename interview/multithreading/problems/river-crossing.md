---
layout: post
title: River Crossing (LBoS ch. 5)
date: 2026-07-19
description: >-
  Deliberate near-duplicate. Solve it AFTER Uber Ride, from scratch, without looking at your Uber solution, then compare. If your two designs are structurally identical…
categories: interview multithreading problems
---

Part of the [Group Formation](/interview/multithreading/patterns/group-formation/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Little Book of Semaphores ch. 5. Worth doing: "it IS the Uber Ride problem with different flavor text."

### Problem

Linux hackers and Microsoft serfs wait to cross a river in a rowboat that holds exactly 4. To avoid trouble, a boat may not carry 1 hacker + 3 serfs or 3 hackers + 1 serf. Valid loads: 4 hackers, 4 serfs, or 2+2. Each passenger calls `board()`; after all 4 have boarded, exactly one calls `rowBoat()`.

### Constraints

- Identical to Uber Ride: valid compositions only, board-before-row, one rower, next boatload waits.

### Clarify before solving

Same two questions as Uber Ride: greedy formation? fairness? (Same answers.)

### Why this problem matters

Deliberate near-duplicate. Solve it AFTER Uber Ride, from scratch, without looking at your Uber solution, then compare. If your two designs are structurally identical (tallies under a mutex, completing-arrival-as-dispatcher, targeted gate releases, barrier, one rower), you've extracted the pattern rather than memorized a solution. If they differ structurally, find out which one is wrong and why. This self-test is the entire point of the exercise, it's the study plan's grasp-vs-memorize checkpoint made concrete.

---

## Strategy

### Classify

Uber Ride, renamed: hackers/serfs = Democrats/Republicans, boat of 4 = car of 4, forbidden 3+1 = forbidden 3+1, rowBoat = drive. There is no new concurrency content, and noticing that within a minute IS the skill being practiced.

### Invariant

Each boatload is exactly one of {4H, 4S, 2H+2S}; all 4 board before rowBoat(); exactly one rower per trip.

### What to actually do

1. Without opening your Uber Ride solution, re-derive: tallies (`waitingH`, `waitingS`) under one mutex; each arrival checks "does my arrival complete 4-of-mine or 2+2?"; completer becomes dispatcher, decrement tallies and release party gates for exactly the chosen composition (3 permits: dispatcher sails free), all four meet at a CyclicBarrier(4), dispatcher rows.
2. Then diff against your Uber solution. Every structural difference is a question: which version is right, or are both right and the difference is policy (e.g., check order = composition preference)?
3. Write, in three sentences max, the abstract recipe both instantiate. Something like: *"Guarded tallies per type; arrival-completes-group check under the mutex with atomic decrement; targeted permits admit exactly the chosen group; reusable barrier bounds the group; one designated member performs the group action."* That paragraph is what you carry into any future variant, boat of 6, three factions, whatever.

### The one wrinkle worth a thought

the Little Book of Semaphores's version sometimes adds: the rower should be the LAST to board. With a CyclicBarrier, trivially satisfied, have the dispatcher row after `await()` returns (all 4 arrived by definition). If you used a different boarding sync, check this property explicitly. Noticing that the barrier gives it for free is a nice grasp-check.

### Pitfalls

Identical to Uber Ride's five. If you hit a DIFFERENT bug here than there, that asymmetry is your review material, it marks the part you hadn't internalized.

### Check your understanding

1. Write the abstract recipe (step 3 above) from memory, then instantiate it for: boat of 6, valid loads 6-0, 3-3, 4-2 either way. What changes? (Only the completion checks.)
2. Why does "rower boards last" come free with a barrier?
3. A colleague's solution uses `hackerQueue = Semaphore(0)` but does tally decrements AFTER releasing permits, outside the mutex. Construct the failure.

### Transfers to

This IS the transfer. After this problem, Type D is closed: every group-formation question is the recipe + a composition rule.
