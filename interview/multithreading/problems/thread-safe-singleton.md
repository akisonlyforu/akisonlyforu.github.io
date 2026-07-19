---
layout: post
title: Thread-Safe Singleton
date: 2026-07-19
description: >-
  It's the standard vehicle for testing whether you understand safe publication: writing a reference is not the same as publishing a fully-constructed object. The interviewer…
categories: interview multithreading problems
---

Part of the [Guarded State](/interview/multithreading/patterns/guarded-state/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Classic. Asked as coding + a JMM discussion in one.

### Problem

Implement lazy initialization: `getInstance()` creates the singleton on first call, returns the same instance ever after — correct under arbitrary concurrent callers.

Then the real question arrives: "Here's double-checked locking without `volatile` — what's wrong with it?"

### Constraints

- Lazy (not created at class-load of the accessor... unless you use the holder idiom, which is the point).
- No per-call locking cost in the common path (that's why naive `synchronized getInstance()` gets a follow-up).

### Clarify before solving

- Is laziness actually required? (Often no — then a plain `static final` field ends the question.)
- Is this Java specifically? (The volatile/JMM discussion is Java-specific.)

### Why this problem matters

It's the standard vehicle for testing whether you understand **safe publication**: writing a reference is not the same as publishing a fully-constructed object. The interviewer cares about your explanation of the broken version far more than your working version.

---

## Strategy

### Classify

Guarded state — specifically the *safe publication* corner of it. The race is check-then-act on "does the instance exist yet".

### Invariant

At most one instance is ever constructed; no caller ever observes a partially-constructed instance.

### The answer ladder (give them in this order)

1. **Eager `static final` field** — if laziness isn't required, done. JVM class-init guarantees thread safety. Say this first; it shows you don't over-engineer.
2. **Initialization-on-demand holder** — a private static inner class holding the `static final` instance. The JVM defers inner-class initialization until first access → lazy AND thread-safe with zero synchronization code, because class initialization is guaranteed by the JLS to be safely published. **This is your recommended answer.**
3. **Enum singleton** — same guarantees plus serialization safety; mention it exists.
4. **Double-checked locking (DCL)** — explain it, don't lead with it.

### Why broken DCL breaks (the part to truly grasp)

DCL: check null without lock → lock → re-check → construct → assign. Without `volatile` on the field, the constructor's writes and the reference assignment can be **reordered** (compiler/CPU are free to publish the reference before the object's fields are written — no happens-before edge exists for the unlocked first read). Thread B's unlocked check sees non-null and returns an object whose fields may be garbage. The bug is invisible in testing and catastrophic in production — that's why the interviewer loves it.

`volatile` fixes it: the volatile write of the reference happens-after all constructor writes, and the volatile read in the fast path happens-before any use. Write → read edge = safe publication.

### Mental model

"Publishing" an object is like mailing an envelope: without a memory barrier you might mail the envelope before putting the letter in. `volatile`/`final`/monitor-release are the rules that force "letter in before envelope sent".

### Pitfalls

1. Answering DCL first — signals memorization; the holder idiom is simpler and superior.
2. Claiming `synchronized getInstance()` is "too slow" without nuance — modern JVM contention cost is small; the honest answer is "it's fine for most uses; holder is better anyway".
3. Fumbling *why* volatile is needed — "visibility" alone is incomplete; the key word is **reordering** of constructor writes vs reference publication.

### Check your understanding

1. Explain the exact reordering that breaks non-volatile DCL, and which happens-before edge volatile adds.
2. Why is the holder idiom thread-safe with no explicit synchronization? What JLS mechanism guarantees it?
3. When would eager init be the wrong answer? (Expensive construction + possibly never used, or instance needs runtime config.)

### Transfers to

Every "lazy init" or "cache the expensive object" question; conceptual questions #5 (happens-before) and #27 (safe publication). Also `computeIfAbsent` — same check-then-act race, solved by the map instead.
