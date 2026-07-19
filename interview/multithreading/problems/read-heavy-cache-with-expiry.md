---
layout: post
title: Read-Heavy Cache with Expiry
date: 2026-07-19
description: >-
  Where three prior lessons converge on one API: ConcurrentHashMap idioms (make-a-class-thread-safe), lazy time-derivation (rate limiter), and per-key mutual exclusion for the…
categories: interview multithreading problems
---

Part of the [Time-Based State](/interview/multithreading/patterns/time-based/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** LLD rounds. Med frequency, very high day-job relevance.

### Problem

Thread-safe cache: `get(key)` returns the value if present and not expired, else loads it via a provided `loader(key)` (expensive: DB/network), stores with a TTL, returns it. Read-heavy: ~99% gets hit fresh entries.

### Constraints

- Correct under concurrent get/load/expiry on the same and different keys.
- The dogpile problem is in scope: N threads missing the same key at once must NOT trigger N loads (one load, others wait for it).
- Expiry: lazy (checked on read), no reaper thread unless asked.

### Clarify before solving

- TTL per entry or global? (Per-entry timestamp either way.)
- Stale-while-revalidate acceptable, or strict? (Strict baseline; SWR is a nice extension to name.)
- Max size / eviction (LRU)? (Separate concern: scope it out explicitly, mention Caffeine.)
- Loader failure policy? (Don't cache failures by default; mention negative caching.)

### Why this problem matters

Where three prior lessons converge on one API: ConcurrentHashMap idioms (make-a-class-thread-safe), lazy time-derivation (rate limiter), and per-key mutual exclusion for the dogpile (a lock per key, not one global, your first fine-grained-locking design with a real justification). Closest question in the bank to what senior backend work actually looks like.

---

## Strategy

### Classify

Asymmetric access (read-heavy) + time-derived validity + per-key exclusion for loads. Three concerns; solve them separately, then compose.

### Invariant

get never returns an expired value; for a concurrent miss-storm on one key, loader runs once (per expiry generation); loads of DIFFERENT keys never block each other.

### Mental model

Library reference desk. Each book has a freshness stamp (entry carries its OWN expiry time, lazy check on read, no librarian patrolling shelves: rate-limiter lesson). Missing/stale book: the FIRST asker fills out a fetch slip and goes to the archive; others wanting the SAME book wait on that slip, not in line for the archive themselves (dogpile prevention); askers of OTHER books are unaffected (per-key, not global).

### Design (build it up in layers, narrating)

**Layer 1: storage**: `ConcurrentHashMap<K, Entry<V>>` where Entry = (value, expiresAtNanos). Fresh hit = map read + timestamp compare: no lock at all on the hot path. That's the read-heavy answer: reads scale because CHM reads are lock-free-ish and freshness derives from the entry itself.

**Layer 2: miss handling, naive**: `if absent/stale → load → put`. Check-then-act across an EXPENSIVE act: N threads stampede the loader (dogpile). Name the disease before curing it.

**Layer 3: dogpile cure**: per-key single-flight. The classic idiom: **cache a Future, not (only) a value**: `map.computeIfAbsent(key, k -> new FutureTask(loader))`: computeIfAbsent's atomicity guarantees ONE FutureTask per key; the winner runs it, everyone (winner and losers) waits on the same future for the one result. Per-key exclusion with zero explicit locks, and different keys proceed independently. CHM's per-bin locking is your striping. (Caution to say: keep loader work OUTSIDE the computeIfAbsent lambda body's map re-entrancy: run the FutureTask after the compute returns; long work inside compute blocks the bin.)

**Expiry + single-flight composed**: entry = Future<(value, expiresAt)>. On stale: replace atomically: `map.replace(key, staleEntry, freshFutureTask)` so only one thread wins the refresh race; losers re-read and wait on the winner's future. Stale-while-revalidate variant: losers get the stale value immediately instead of waiting: one policy line, big latency win; name it.

### The discussion layer (senior points)

- Why no reaper thread: lazy expiry costs one comparison per read; a reaper adds lifecycle + races with readers for marginal memory. Add reaping only if memory pressure from dead keys is real, then mention size-bounded eviction and hand the problem to **Caffeine** (production answer: Caffeine/Guava Cache do all of this: expireAfterWrite, single-flight loads, LRU/W-TinyLFU. Build-it-yourself is the exercise; know what you're reimplementing).
- Failure: if the loader throws, REMOVE the future from the map (else the failure is cached forever and every get rethrows, a real production incident pattern). Retry falls out: next get creates a fresh future.
- Metrics hooks (hit rate): one sentence, shows production instinct.

### Pitfalls

1. Global lock around get → serialized reads; the "read-heavy" in the title was the requirement you ignored.
2. contains+get / get+put non-atomic pairs on CHM: its per-METHOD atomicity doesn't compose (make-a-class-thread-safe lesson, verbatim).
3. Dogpile unhandled: correct-looking cache, melted database on cold start. Interviewers specifically probe this.
4. Loader executed inside the computeIfAbsent mapping function → blocks the bin (and re-entrant map ops from the loader can deadlock). Create the task inside; run it outside.
5. Failed futures left cached.
6. Wall clock for TTL (third time: nanoTime).

### Check your understanding

1. Why is the fresh-hit path lock-free, and which two facts make that safe? (CHM read semantics + immutable entry with self-contained expiry.)
2. Walk 10 threads missing cold key K: exactly one loader run: trace the computeIfAbsent race and where the 9 wait.
3. Two threads find K stale simultaneously: how does replace() arbitrate? What do the losers do?
4. Why cache a Future instead of loading-then-putting a value? (The waiting-point exists BEFORE the value does, that's what the losers block on.)
5. What does Caffeine give you beyond this design, and when do you say "just use Caffeine" in an interview? (Eviction policy, weighers, refresh, stats; say it whenever implementation isn't the explicit ask.)

### Transfers to

Session stores, memoizers (this IS the classic Memoizer from JCiP), connection caches, DNS caches; the cached-future idiom transfers to any "expensive compute, many waiters" shape, and it's your best worked example for conceptual #13.
