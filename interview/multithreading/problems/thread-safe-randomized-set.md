---
layout: post
title: Thread-Safe RandomizedSet (Insert/Delete/GetRandom O(1))
date: 2026-07-19
description: >-
  The single-threaded trick is an array plus a value→index map, and remove is a swap-with-last. The concurrent twist: that remove is a composite read-modify-write across TWO…
categories: interview multithreading problems
---

Part of the [Concurrent Data Structures](/interview/multithreading/patterns/concurrent-data-structures/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [EngineBogie: thread-safe O(1) insert/delete/get/getRandom](https://enginebogie.com/public/question/thread-safe-o-1-insert-delete-get-and-getrandom-data-structure/2371), the concurrent version of [LeetCode 380, Insert Delete GetRandom O(1)](https://leetcode.com/problems/insert-delete-getrandom-o1/). The single-threaded version is a warm-up; the interest is entirely in what concurrency does to the `remove`.

### Problem

Design a set supporting three operations, each **O(1) average**, correct under concurrent access from many threads:

- `insert(x)`: add `x`, return false if already present.
- `remove(x)`: delete `x`, return false if absent.
- `getRandom()`: return a uniformly random present element.

The single-threaded trick you must know cold: a dynamic **array** holding the elements, plus a **HashMap `value → index`** into that array. `insert` appends and records the index. `getRandom` picks `array[rand(size)]`, uniform for free. `remove` is the clever one, you can't pop from the middle in O(1), so you **swap the target with the last element, fix the moved element's index in the map, then pop the tail.** That swap-remove is the entire reason this problem is interesting once threads arrive.

### Constraints

- All three operations O(1) average, so `getRandom` must index an array directly; you cannot iterate a set or reservoir-sample a linked structure.
- `getRandom` must be uniform over exactly the currently-present elements, which means it needs a **globally consistent** view of `size` and the array contents at its instant.
- Many readers (`getRandom`) and many writers (`insert`/`remove`); state the read/write mix, it decides how much the lock hurts.
- Duplicates policy: it's a set, so `insert` of a present value is a no-op returning false. Confirm this rather than assuming multiset.

### Clarify before solving

- **Is `getRandom` hot relative to the mutations?** If reads dominate you'll be tempted by a `ReadWriteLock`; the trap below is why that's more delicate than it looks.
- **Uniform, or is approximate-uniform acceptable?** A momentarily stale `size` biases the distribution slightly. Usually nobody's SLA cares, and saying so is the senior move, but ask.
- **Must `remove` returning true mean the element is gone for every subsequent `getRandom`?** i.e. is linearizability required, or is eventual visibility fine? Almost always the former.
- **Bounded size, or unbounded growth?** Decides array-resize contention, not the core design.

### Why this problem matters

It is the cleanest example in the bank of a **composite invariant that does not decompose.** Every other "make it fast" instinct, striping, lock-free CAS, per-key locks, is defeated by one structural fact: a single `remove` must atomically mutate the map (two entries), the array (two slots), and the size, and `getRandom` must observe all of them consistently. There is no seam to cut along. The graded insight is recognizing that, resisting the lock-free reflex, and defending a single lock as the *correct* answer rather than a lazy one, then knowing exactly why the `ReadWriteLock` "optimization" is subtler than it appears.

---

## Strategy

### Classify

Concurrent data structure (family B / concurrent-data-structures) with a **twinned invariant across two structures plus an aggregate (`size`) that `getRandom` reads globally.** Run the family recipe: write the invariant clause-by-clause, ask which clauses are local, discover that none are, and let that failure *be* the answer.

### Invariant

For every present value `x`: `map[x] == i` **and** `array[i] == x`, and the map has exactly one entry per array slot in `[0, size)`. The two clauses are welded, you cannot change one without the other, and `getRandom` depends on `array[0..size)` all satisfying it simultaneously. The linearization point of each op is the single instant the whole tuple `(map, array, size)` flips from old to new.

### Mental model

A coat-check: a rack of numbered pegs (the array) and a ledger mapping each ticket to its peg (the map). To remove a coat from the middle of the rack you move the *last* coat into the vacated peg and cross out the tail, and you must correct the ledger for the coat you moved, otherwise its ticket now points at an empty peg. A patron asking for "any random coat" (`getRandom`) must not glance at the rack while an attendant is mid-move: they'd see a peg that's been half-vacated or a ledger that disagrees with the rack. One attendant, one patron-glance, never overlapping. That "never overlapping" is the whole design.

### Design ([Template 1](/interview/multithreading/mt-framework/) shape: one lock, three guarded methods)

The honest answer is **a single lock** (a `synchronized` block, or a `ReentrantLock`) around all three operations. That is not a cop-out; it is what the invariant demands, and you should say so with a straight face. Each op's critical section is a handful of array/map writes, tens of nanoseconds, so the lock is rarely the bottleneck a real workload hits.

Why the mutations can't be sharded: `insert` appends at `size`, so it touches the *single shared tail*; `remove`'s swap touches an **arbitrary existing slot plus the tail**, two slots that belong to no shared key or shard. There is no partition of the array under which two writers never collide, `remove` can move any element into any hole. Striping by value hash buys nothing, because the array index of a value changes on every unrelated `remove`, and `getRandom` needs a consistent `size` across all stripes anyway. The aggregate `size` is global by construction (§the family's global-clause wall).

**The `ReadWriteLock` "optimization" and its trap.** The tempting move: `getRandom` takes the read lock (many concurrent random picks), `insert`/`remove` take the write lock. Reads scaling is real *only if* `getRandom` truly reads and mutations truly reindex under mutual exclusion, which they do here, so a `ReadWriteLock` is in fact a legitimate refinement, **provided you get one thing right**: `getRandom` must hold the read lock for the *entire* pick, the `rand(size)` and the `array[i]` read together, because between reading `size` and indexing, a concurrent `remove` could shrink the array and make your index out of range or point at a swapped-in different element. And crucially, **`remove` is a writer, full stop**, its swap mutates an existing occupied slot, so it can never be demoted to the read side no matter how "read-like" it feels. If you catch yourself reasoning "remove mostly just reads the map," that's the error, the swap-write is the operation. State the read/write ratio out loud: a `ReadWriteLock` only pays when `getRandom` genuinely dominates and the write critical section is short; otherwise the plain single lock wins on simplicity and lower overhead.

### The false lock-free instinct (name it, then kill it)

The reflex from `lock-free-stack-treiber`: "it's an array with a head/size counter, just CAS the size like a Treiber stack pushes/pops the top." This is a trap, and articulating *why* is most of the interview.

- A stack push/pop is a **single-cell** change: one CAS on `top` publishes the whole operation, and the invariant fits in one word. That's exactly when lock-free is the natural answer.
- `remove` here is **three writes that must appear atomic**: correct `map[last]` to point at the hole, write `array[hole] = last`, decrement `size`, and delete `map[x]`. No single CAS covers a map entry *and* two array slots *and* the counter. You'd need a multi-word / whole-snapshot CAS, i.e. copy-on-write the entire structure per mutation (§the immutability bargain), which is O(n) per write and throws away the O(1) the problem demands.
- Worse, the swap-remove **mutates an existing slot** (`array[hole]`), so even a would-be CAS on `size` doesn't linearize the operation, a concurrent `getRandom` between the slot-write and the size-decrement sees a corrupt tuple. The Treiber trick works because pop only *reads* `top.next` and swings one pointer; nothing here is that clean.

So: single-cell CAS works for the stack, and does **not** work here, and the reason is the composite, multi-slot, cross-structure write. That contrast is the answer they want.

### Pitfalls

1. Reaching for a `ConcurrentHashMap` for the map and calling it done, the map being thread-safe does **not** make the map-plus-array pair atomic; a `getRandom` can see the map updated but the array not yet (or vice versa). Per-structure thread-safety never composes into cross-structure atomicity.
2. `getRandom` reading `size` and then `array[i]` under *separate* lock acquisitions (or none), a `remove` in the gap makes `i` stale: out-of-bounds, or a uniform pick over the wrong set.
3. Treating `remove` as read-mostly and letting it take a read lock in a `ReadWriteLock` design, its swap is a write to an occupied slot; demoting it corrupts the array.
4. Striping by value and losing a consistent `size`/array for `getRandom`, plus the moved element's index living in a different stripe than the hole.
5. The CAS-the-size-like-Treiber instinct, defeated by the multi-slot swap that no single atomic covers.
6. Forgetting to fix the moved (last) element's map entry after the swap, the classic single-threaded bug, now also a visibility bug if the two writes aren't published together.

### Check your understanding

1. Write the invariant as two welded clauses and prove no partition of the array lets two writers avoid collision. Which operation is the one that can touch any slot?
2. `getRandom` under a read lock: exactly which two reads must be inside the *same* lock hold, and what does a concurrent `remove` do to you if they aren't?
3. Why is `remove` a writer even though "most of it is map lookups"? Point at the specific write that forbids the read lock.
4. Contrast with the Treiber stack: why does one CAS linearize a pop but no single CAS linearizes this `remove`? Answer in terms of how many cells change and in which structures.
5. When, concretely, does a `ReadWriteLock` beat the single lock here, and when does it just add overhead? Give the read/write ratio that flips the decision.
6. Someone proposes copy-on-write (whole-snapshot CAS) to go lock-free. What does it cost per operation, and which problem constraint does that violate?

### Transfers to

`thread-safe-lru-cache` (the same map-plus-second-structure dual-invariant, where the second structure, a linked list, is likewise the un-shardable serialization point); `lock-free-stack-treiber` (the deliberate contrast, single-cell CAS is genuinely right there and genuinely wrong here); `make-a-class-thread-safe` (this *is* that exercise, one lock guarding a compound invariant, applied to two collaborating fields); and `lock-striping-and-concurrent-hashmap` (the technique this problem shows you cannot use, because the array index is not a stable shard key).
