---
layout: post
title: Make This Class Thread-Safe
date: 2026-07-19
description: >-
  Closest to the day job of any question in the bank. Tests three senior instincts: coarse-first discipline, spotting that per-method safety does NOT make caller sequences…
categories: interview multithreading problems
---

Part of the [Guarded State](/interview/multithreading/patterns/guarded-state/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Real senior rounds. Arrives as: "here's an existing class (stack / counter / registry / tree), make it safe for concurrent use."

### Problem

You're given a working single-threaded class, e.g., a bounded stack with `push`, `pop`, `peek`, `size`, or a node-based tree with `insert`/`contains`. Make it correct under concurrent access. Then defend your locking granularity.

### Constraints

- Preserve the existing API (callers don't change).
- "Correct" includes compound operations callers might do (`if (!s.isEmpty()) s.pop()`).

### Clarify before solving

- Which operations are hot? Read-heavy or write-heavy? (Drives coarse vs fine vs read-write lock.)
- Do callers compose operations? (The check-then-act trap, may need new atomic methods like `popIfNotEmpty`.)
- Single JVM? (Yes for this question.)

### Why this problem matters

Closest to the day job of any question in the bank. Tests three senior instincts: coarse-first discipline, spotting that per-method safety does NOT make caller sequences safe, and knowing when `java.util.concurrent` already solved your problem.

---

## Strategy

### Classify

Guarded state. Invariant depends on the class, state it for THIS class before touching code (e.g., stack: `0 <= size <= capacity`, elements LIFO, no lost or duplicated element).

### The escalation ladder (walk it in order, out loud)

1. **Immutable?** If the class can be immutable (state set in constructor, final fields), it's thread-safe with zero locks. Always check first.
2. **Coarse-grained lock.** One lock (or `synchronized` methods) guarding all state. Correct by construction, trivially reviewable. **Start here. Say "I'll start coarse and only refine if there's a measured bottleneck."** That sentence is the anti-over-engineering signal the interviewer wants.
3. **Read-write lock**: if profiling/requirements say read-heavy: many readers in parallel, writers exclusive. Cost: more complexity, and RW locks only pay off when reads truly dominate and critical sections aren't tiny.
4. **Fine-grained / lock-free**: per-node locks in a tree (hand-over-hand), or CAS loops. Only on explicit request; name the cost: much harder invariants, easy to deadlock between node locks (need an ordering!), verification burden explodes.
5. **Replace with a JDK structure.** If the class is a queue/map/set, the real answer may be `ConcurrentHashMap`, `ConcurrentLinkedQueue`, `CopyOnWriteArrayList` (small, read-mostly lists). Hand-rolling what the JDK ships is a design smell, mention the swap even if the interviewer wants the manual exercise.

### The trap that fails candidates: compound operations

Making every method `synchronized` does NOT make `if (!stack.isEmpty()) stack.pop()` safe, another thread can pop between the check and the act. Per-method atomicity ≠ per-sequence atomicity. Fixes: expose an atomic compound method (`Optional<T> tryPop()`), or document that callers must hold an external lock. This is the same disease as `map.containsKey` + `map.put` vs `putIfAbsent`/`computeIfAbsent`. **Bring this up unprompted, it's the point of the question.**

### Also cover

- **Don't leak the reference**: `this` escaping during construction, or returning internal mutable collections (return copies or unmodifiable views), otherwise callers bypass your locks entirely.
- **Iteration**: iterating a coarse-locked collection means holding the lock for the whole loop (or copying out). Concurrent structures have weakly-consistent iterators instead. Know which you're offering.
- **`size()` semantics**: under concurrency, a size is a snapshot, stale the moment it returns. Callers must not treat it as a guarantee, connects directly back to the compound-op trap.

### Pitfalls

1. Jumping to fine-grained/striped locking unprompted, over-engineering, the #1 way to fail this question.
2. Mixing locked and unlocked access to the same field ("reads don't need the lock", wrong: visibility + torn compound state).
3. Two locks for entangled state (one for `size`, one for the array), invariant spans both → one lock.
4. Hand-over-hand tree locking without a parent→child acquisition order argument.

### Check your understanding

1. Why doesn't per-method synchronization compose? Give the stack interleaving.
2. When does a RW lock actually beat a plain mutex? Name both conditions.
3. You made a tree thread-safe with one lock; interviewer says "reads are 99%, contention is measured". Next move, and its new risks?
4. Which JDK types would you reach for before hand-writing: concurrent map? read-mostly list? queue?

### Transfers to

Read-heavy cache (07), thread-safe LRU, "is this code thread-safe?" code-review questions, conceptual #13.
