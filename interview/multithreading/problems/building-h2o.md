---
layout: post
title: Building H2O (LC 1117)
date: 2026-07-19
description: >-
  First problem where admission control (WHO may enter: 2 H + 1 O) and group synchronization (all three proceed TOGETHER, then the next group) are separate concerns needing…
categories: interview multithreading problems
---

Part of the [Group Formation](/interview/multithreading/patterns/group-formation/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [LeetCode 1117](https://leetcode.com/problems/building-h2o/), the Little Book of Semaphores ch. 5. Direct warm-up for the Uber Ride problem.

### Problem

Hydrogen threads call `hydrogen(releaseHydrogen)`, oxygen threads call `oxygen(releaseOxygen)`. Threads must group into water molecules: each molecule = exactly 2 H + 1 O. A thread waits until a full molecule can form; all three "bond" (call their release callbacks) as a group, and the NEXT molecule's threads must not start bonding until the current three have all bonded.

### Constraints

- Exactly 2 H + 1 O per molecule, never 3 H, never 2 O.
- Molecule boundaries are strict: the three bonds of molecule k complete before any bond of molecule k+1.
- Threads arrive in arbitrary order and quantity (input guarantees a valid total).

### Clarify before solving

- Do the 2 H's bond in any order within a molecule? (Yes, only the composition and the boundary matter.)
- Repeated forever? (Yes → the barrier must be reusable.)

### Why this problem matters

First problem where admission control (WHO may enter: 2 H + 1 O) and group synchronization (all three proceed TOGETHER, then the next group) are separate concerns needing separate tools. Getting that separation crisp is what makes Uber Ride, the harder sibling, straightforward.

---

## Strategy

### Classify

Group formation: fixed composition (2H+1O), repeating. Two sub-problems, name them separately to the interviewer:

1. **Admission**: at most 2 H and 1 O may be "in the current molecule" at once.
2. **Boundary**: the admitted three bond together; nobody from molecule k+1 bonds before molecule k completes.

### Invariant

Between consecutive barrier openings, exactly 2 releaseHydrogen and 1 releaseOxygen calls occur.

### Mental model

A nightclub with two doors: H-door with capacity 2, O-door capacity 1 (bouncers = semaphores). Inside, the three guests meet at a table (barrier); when the third sits down, they toast (bond), and AS THEY LEAVE they hand their entry passes back to the bouncers, readmitting the next group. The passes-returned-on-exit step is the reuse mechanism.

### Design ([Template 4](/interview/multithreading/mt-framework/): understand it, don't recite it)

`hSlots = Semaphore(2)`, `oSlots = Semaphore(1)`, `CyclicBarrier(3)` whose barrier action releases 2 hSlots + 1 oSlot.

- hydrogen: hSlots.acquire → releaseHydrogen.run() → barrier.await().
- oxygen: oSlots.acquire → releaseOxygen.run() → barrier.await().

Why bonding before the barrier is safe: at any instant at most 2 H + 1 O hold slots (semaphores enforce it), and no slots are re-issued until the barrier action runs, so everything bonded between two barrier-openings is exactly one molecule's worth. The boundary invariant holds even though bonds happen "before" the barrier. Walk this argument yourself until it's obvious; it's the crux.

Why the barrier action (not the threads) releases the permits: it runs exactly once per trip, after all 3 arrived, before any are released, a single atomic "reset" point with no race about who reissues permits or when (compare: the reusable-barrier attempt-2 bug).

### The wait/notify alternative (know it exists)

Guarded counters (hInside, oInside) with condition "room in the molecule for my kind" get admission right, but the boundary logic (nobody re-enters until all 3 bonded) re-derives the two-turnstile machinery by hand. Fine as an exercise; in an interview the semaphores+barrier version is shorter and each piece has one job. Say that trade-off aloud.

### Pitfalls

1. Semaphores initialized (2,1) but permits released by each thread individually after bonding → a fast H re-enters while molecule k's O hasn't bonded: boundary broken. Release must be centralized in the barrier action.
2. Barrier size 2 or a barrier per element type, the barrier is the MOLECULE (3), not the element.
3. Believing bond-inside-barrier-await is required and contorting the code, see the safety argument above.
4. One-shot gate instead of CyclicBarrier → second molecule deadlocks or mixes (the lapping bug again).

### Check your understanding

1. Prove the boundary invariant: why can't a bond from molecule k+1 interleave before molecule k's third bond? Which two facts combine?
2. Why must the barrier action, not the exiting threads, release the slots? Construct the failure otherwise.
3. Trace arrival order O, H, H: who waits where? Then H, H, H, O: where does the third H wait, and why doesn't it poison the first molecule?
4. What changes for CO2 (1 C + 2 O)? (Nothing structural, swap the numbers. If you see that instantly, you've grasped it.)

### Transfers to

Uber Ride (multiple valid compositions, the one genuinely new idea), river crossing (boat of 4 with composition rules), roller coaster (capacity batches). H2O is the template; the siblings vary the admission rule.
