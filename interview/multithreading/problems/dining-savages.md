---
layout: post
title: Dining Savages (LBoS ch. 5)
date: 2026-07-19
description: >-
  Producer-consumer inverted: the consumers themselves trigger production, in batch, on empty, a refill-on-demand pattern you'll recognize later in the token-bucket rate…
categories: interview multithreading problems
---

Part of the [Bounded Resource](/interview/multithreading/patterns/bounded-resource/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Little Book of Semaphores, ch. 5. Worth doing: short, high value.

### Problem

A tribe of savages eats from a communal pot holding up to M servings. Each savage loops: take a serving from the pot, eat. A savage finding the pot EMPTY wakes the cook and waits until the pot is refilled. The cook loops: sleep until woken, refill the pot with exactly M servings, sleep again.

### Constraints

- Only the savage who finds the pot empty wakes the cook (not every hungry savage).
- The cook must not refill a non-empty pot.
- Servings are taken one at a time, atomically.

### Clarify before solving

- Exactly one cook, many savages.
- While the cook refills, other savages must wait too (pot is being modified).

### Why this problem matters

Producer-consumer inverted: the consumers themselves trigger production, in batch, on empty, a refill-on-demand pattern you'll recognize later in the token-bucket rate limiter (bucket = pot, refill = cook) and in batched fetch-on-miss caches. Also the cleanest exercise in "exactly one waiter plays a special role" logic.

---

## Strategy

### Classify

Bounded resource with batch refill triggered by exhaustion. One producer (cook) who must be woken exactly once per empty event.

### Invariant

servings in pot ∈ [0, M]; the cook refills only when servings == 0, and adds exactly M; every "take a serving" is atomic; between the empty-discovery and refill-completion, no savage takes anything.

### Mental model

An office coffee machine. Whoever takes the last cup flips the "brewing" sign and pings facilities; everyone arriving during brewing queues behind the sign; facilities refills and takes the sign down. Two special moments: discovering emptiness (exactly one savage does), and refill completion (releases everyone queued).

### Design (semaphore + guarded counter blend)

State: `servings` counter, mutex guarding it, `emptyPot = Semaphore(0)` (savage→cook signal), `fullPot = Semaphore(0)` (cook→savage signal).

- Savage: mutex.acquire; if servings == 0 → emptyPot.release() then fullPot.acquire() (WAIT, still conceptually at the pot, holding the mutex claim, see the subtlety below) → servings = M consumed... Actually the classic the Little Book of Semaphores shape: the empty-finder signals the cook, waits on fullPot, and on wake **resets servings = M herself** (the cook's refill happened; she's the one thread awake at the pot), then proceeds to take her serving. Other savages never see servings == 0 mid-refill because the empty-finder held the mutex throughout.
- Cook: emptyPot.acquire() → refill the physical pot → fullPot.release() → loop.

The subtlety that makes this problem worth doing: **the empty-finder waits on fullPot while holding the mutex, deliberately.** Normally "never block holding a lock" is the rule; here it's the mechanism: the held mutex is what freezes all other savages during the refill. It's safe from deadlock only because the cook (the one who'll release fullPot) never touches the mutex. Convince yourself of that dependency chain, it's the whole lesson: the no-blocking-with-locks rule is really "never block on something whose provider needs YOUR lock".

### Correctness argument

Only the mutex-holder can observe servings == 0 → exactly one emptyPot.release per empty event → cook refills exactly once per event. Others are queued on the mutex during refill → nobody takes from an empty/mid-refill pot. Cook's signal chain: empty→refill→full is strictly ordered by the two semaphores.

### Pitfalls

1. Every hungry savage signaling the cook → cook refills a non-empty pot (invariant broken). Only the empty-DISCOVERER signals.
2. Releasing the mutex before waiting on fullPot → another savage sees servings == 0 and signals the cook AGAIN → double refill.
3. Cook acquiring the savages' mutex → now the blocked-holding-mutex empty-finder waits on a cook who waits on the mutex: deadlock. The safety argument above depends on the cook never touching it.
4. Off-by-one: does the empty-finder take her serving from the NEW batch (servings = M, then take → M-1)? Decide and keep it consistent.

### Check your understanding

1. Why is blocking-while-holding-the-mutex safe HERE? State the exact dependency that must not exist (cook needs mutex) and verify it doesn't.
2. What sequence of events produces a double refill if the finder releases the mutex before fullPot.acquire()?
3. Map to token bucket: what corresponds to the pot, servings, the cook, and the empty-finder? Where does the analogy break? (Token refill is time-driven, not exhaustion-driven, no cook thread needed.)

### Transfers to

Token-bucket rate limiter (07), refill-on-miss caches, and any "one waiter plays a special coordinating role" structure (also appears in barbershop).

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/bounded-resource/dining-savages).
