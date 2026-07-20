---
layout: post
title: Thread-Safe LRU Cache
date: 2026-07-19
description: >-
  It is the cleanest example in the whole bank of a structure whose invariant *refuses to decompose*. The hash map half shards beautifully; the recency ordering half is one…
categories: interview multithreading problems
---

Part of the [Concurrent Data Structures](/interview/multithreading/patterns/concurrent-data-structures/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** The most frequently reported question in this family in senior backend/infra loops, it shows up both as an LLD exercise and as a system-design sub-problem. Treat the frequency claim as directional: it reflects how often the shape recurs, not a measured tally.

### Problem

Design a fixed-capacity LRU cache with `get(key)` and `put(key, value)`, safe for many concurrent threads. When `put` would exceed capacity, evict the least-recently-used entry. Both operations should be O(1) in the single-threaded sense.

Then defend your concurrency design: what serializes, what doesn't, and what you would change if the cache became the hottest object in the process.

### Constraints

- Fixed capacity; eviction on insert when full.
- `get` counts as a use, it changes what "least recently used" means.
- O(1) amortized per operation in the classic single-threaded formulation (hash map + doubly-linked list).
- Correct under concurrent `get`/`put`/eviction on the same key and on different keys.
- Single JVM, single process.

### Clarify before solving

- **What is the read:write ratio, and what throughput are we defending against?** Determines whether one lock is a real problem or an imagined one, and whether any of the sophistication below is justified.
- **Does eviction have to be exactly LRU, or is "approximately the coldest entry" acceptable?** This is the single most important clarifying question in the problem. Exactness is the thing that serializes you.
- **Is `get` allowed to be a no-op on recency under contention** (i.e. may a recency update be dropped)? Follows directly from the previous question.
- **Do we need an exact `size()`, or a bound that is respected eventually?** Strict capacity is a global clause; "never more than capacity + small slack" is much cheaper.
- **Any TTL / expiry?** Say explicitly that this is a separate axis from capacity eviction, and that combining them is a different problem.
- **Is a per-key loader in scope** (miss → compute → store)? If yes, the single-flight concern arrives too, and should be solved separately rather than folded into the eviction design.
- **Would using an existing library be acceptable in production?** Ask it, then keep building, but the answer shapes what you say at the end.

### Why this problem matters

It is the cleanest example in the whole bank of a structure whose invariant *refuses to decompose*. The hash map half shards beautifully; the recency ordering half is one list with one head and one tail, and no amount of striping makes a total order stop being a total order. So the problem forces the two senior moves this family exists to teach: recognizing that `get`, the operation everyone calls "the read path", is actually a write, and recognizing that when a global ordering is the bottleneck, the productive question is not "how do I lock it better" but "how exact does the ordering need to be?"

It also punishes the two most common failure directions equally. Wrapping everything in one lock and never naming the cost reads as unaware. Producing a lock-free eviction scheme unprompted, for a cache nobody measured, reads as reckless. The passing answer walks between them out loud.

---

## Strategy

### Classify

Concurrent data structure with a **composite invariant that only half-decomposes**: a per-key lookup clause (local, shards perfectly) welded to a total recency ordering (global, refuses to shard). Everything interesting comes from the weld.

### Invariant

Write it in four clauses, because the whole design is deciding which ones are local:

1. Every key present in the cache maps to exactly one entry, and that entry is in the recency structure exactly once. *(Local per key, but note it couples the two structures.)*
2. Entry count never exceeds capacity. *(Global.)*
3. The recency structure orders entries by last access, most recent first. *(Global, this is the serialization point.)*
4. Eviction removes an entry that is at (or near) the cold end, and removes it from **both** structures or neither. *(Global, and the "or neither" is where the bugs live.)*

Clauses 1 and 4 together are the reason a naive "just use a ConcurrentHashMap plus a synchronized list" is wrong: the map and the list must change *together*, so an operation that touches both has one invariant spanning both, the one-invariant-one-lock rule from the guarded-state family, applied across two data structures instead of two fields.

### Mental model

A library with a returns shelf. Finding a book is trivially parallel, every reader walks to a different aisle, and aisles don't interfere (that's the map). But the shelf that records "most recently touched, in order" is a *single physical queue* with one front and one back. Every reader who touches any book must also walk to that one shelf and move a card to the front. The aisles scale; the card shelf is a doorway everyone squeezes through.

The design question is therefore never "how do I make the aisles faster." It is: *does every reader really have to visit the card shelf on every single read, and does the card order really have to be exact?*

### Why the doubly-linked list makes coarse locking tempting

The classic single-threaded design is a hash map from key to node, plus a doubly-linked list where head is most-recently-used. `get` unlinks a node and relinks it at the head; `put` links a new node at the head and unlinks the tail on overflow. Each of those is a handful of pointer writes, and *all of them touch the shared head, the shared tail, or both.*

Three consequences, worth stating explicitly:

- **The critical section is tiny.** A few pointer assignments, no I/O, no allocation on the hit path. An uncontended lock acquire costs on the order of the pointer writes themselves. This is a genuine argument for one lock, not a concession.
- **The critical section is also unavoidable on every operation.** Because `get` reorders, there is no "read path" that skips the list. So the tiny lock is taken by 100% of traffic, the contention scales with total throughput, not with write rate.
- **Splitting the lock in two (one for the map, one for the list) is exactly wrong.** Clause 1 spans both structures. A thread holding the map lock can observe a node the list no longer contains, or vice versa, a torn invariant, the guarded-state family's failure mode #3, dressed as an optimization.

So the honest baseline is one lock over both structures, and it is a *good* baseline. The failure is not choosing it; the failure is choosing it silently.

### `get()` mutates: say this before anything else

Interviewers listen for whether the candidate notices unprompted. The cache is "read-heavy" only in the sense that most operations are lookups; from the structure's point of view, **every operation is a write**, because every lookup reorders. Consequences that cascade from this one fact:

- A read-write lock buys you nothing. Readers would all need the write lock anyway. Reaching for `ReentrantReadWriteLock` here is the tell that the candidate hasn't noticed.
- "Lock-free reads" cannot be promised without also saying what happens to the recency update.
- The obvious escape, *drop some recency updates*, is not a hack; it is the design (see below).

### Design, in layers

**Layer 0, one lock over map + list.** Correct by construction; every clause holds; linearization point is the pointer swap under the lock. Say: *"I'll start with one lock because the critical section is a few pointer writes, and refine only against measured contention."* Then immediately name the ceiling: every operation in the process serializes through this monitor, so the cache's throughput is capped at roughly one operation per critical section, regardless of core count.

**Layer 1, separate the lookup from the ordering.** Move the key→node mapping into a concurrent map so lookups (and misses) don't touch the ordering lock at all. Now the lock covers only the recency list and the eviction decision. This is a real win, the map operation is often the more expensive half, and it costs nothing in exactness. The subtlety: the node's value field must be safely published (final where possible, volatile if mutable), because readers now reach nodes without taking the list lock, and clause 1's "in both structures or neither" now has to be maintained across two independently-lockable things. Order the operations so that a node is discoverable only when it is fully consistent, and on eviction, remove from the map first so a concurrent lookup either finds a live node or misses cleanly.

**Layer 2, stop paying for exact recency on every hit.** Two classic relaxations, both worth naming:

- **Sampled / probabilistic promotion:** only update recency on some fraction of hits, or only when the node is not already near the head. A node accessed twice in a microsecond does not need two promotions. This alone removes most of the lock traffic for hot keys, which are exactly the keys causing the contention.
- **Deferred recency via buffers (the production answer):** each thread appends the accessed node to a small per-thread (or striped) ring buffer instead of touching the list. Buffers are **lossy**, if a buffer is full, the record is *dropped*. Periodically, or when a buffer crosses a threshold, one thread `tryLock`s the ordering lock and drains the pending records, applying them to the list in batch; if it fails to acquire, it does nothing and moves on. The read path now has no blocking at all, and the ordering lock is touched by one thread at a time, amortized over many accesses.

Say why dropping is safe: recency is a *hint about future usefulness*, not a correctness property. Losing some accesses degrades the hit rate marginally; it cannot corrupt the cache. That is the §7 "relax the contract" move of this family, and it is the entire reason production caches are fast.

**Layer 3, the write path.** Writes and evictions must still be exact enough to respect capacity, so they take the ordering lock (or are queued into a write buffer drained the same way, with capacity enforced at drain time and a small slack allowed above capacity in between). Be explicit about which you chose and what slack it implies.

### Striped / segmented alternatives, and their honest limitation

The tempting shortcut: partition the cache into K independent segments by key hash, each a complete small LRU with its own lock. Contention drops by ~K. This is a legitimate, widely-used design, and you should be able to state both its properties:

- It is **not an LRU.** It is K independent LRUs. A globally hot key can be evicted from its segment while a globally cold key survives in a less-crowded one. The eviction quality degrades gracefully with good hashing and enough segments, but the *contract* has changed and you must say so.
- Capacity becomes per-segment. Skewed key distributions mean some segments thrash while others sit half-empty. Mention that this is the same weakness striping always has: it converts a global property into a statistical one.

Contrast this with the buffered-recency design, which keeps a *single* global ordering and relaxes only the *timeliness* of updates to it. Segmenting relaxes which entry gets evicted; buffering relaxes when the ordering learns about an access. The second usually preserves hit rate better, which is why high-end caches went that way.

### What the library-grade designs actually do

Worth naming, because it signals you know where the ceiling is: the lineage from `ConcurrentLinkedHashMap` to Caffeine solves exactly this problem with (a) a concurrent map for lookup, (b) lossy striped read buffers plus a write buffer, (c) a single ordering structure maintained by whichever thread wins a `tryLock` on the drain, and (d) an admission policy smarter than LRU (frequency-aware, so a one-hit scan doesn't evict your working set). The honest closing line in an interview is: *"in production I would use Caffeine, and this is what it's doing internally."* Saying it early sounds like dodging; saying it after you've derived the design sounds like judgment.

### When is one lock fine? (Answer this; it's frequently the real question)

One lock is fine, often better, when:
- The cache is not on the hottest path in the process, or throughput is well below what a few-hundred-nanosecond critical section supports.
- Correctness of eviction order actually matters (a cache whose eviction has side effects, closing connections, flushing buffers, where "approximately the coldest" is not acceptable).
- The code must be reviewable by people who will maintain it. Buffered lossy recency is a lot of machinery to justify without a profile.

The failure is never "you used one lock." It is "you used one lock and did not know what it cost," or "you built a lock-free promotion scheme for a cache handling 200 requests a second."

### Pitfalls

1. Two locks, one for the map and one for the list, torn invariant; the map and list must move together or the cache leaks entries and returns nodes that are no longer resident.
2. Reaching for a read-write lock. Betrays not noticing that `get` mutates.
3. `ConcurrentHashMap` + a synchronized list and declaring victory: the per-method atomicity of the map does not compose with the list operation. The pair `map.get` then `list.moveToHead` is a check-then-act across two structures; the node can be evicted in the gap and get resurrected into the list, a leak that grows past capacity forever.
4. Eviction that removes from the list but not the map (or the reverse) on an exception path. Everything that touches both needs the removal in a `finally`, or an ordering where a partial state is still a valid state.
5. Evicting while holding the lock across a user-supplied removal listener, an alien call under a lock (guarded-state failure mode #9). Collect victims under the lock, invoke callbacks outside it.
6. Claiming "O(1)" and then implementing recency with a timestamp scan for the minimum on eviction, that's O(n) per eviction and it also reintroduces a global scan under the lock.
7. Segmented LRU presented as an exact LRU without naming the change in contract.
8. Unbounded write buffers in the deferred design, you replaced a bounded lock wait with an unbounded memory growth. Bound them and drop or block explicitly.
9. Building any of layers 2–3 unprompted, without being asked to scale.

### Check your understanding

1. Why does the doubly-linked list resist striping when the hash map doesn't? Answer in terms of local versus global invariant clauses.
2. Give the exact interleaving where `ConcurrentHashMap` + a separately-synchronized recency list leaves an entry in the list after it was evicted from the map. What grows without bound?
3. Why does a read-write lock not help here? What would have to change about the problem for it to start helping?
4. What precisely do you lose by dropping recency updates from a full per-thread buffer, and why is that loss acceptable? Name the property that is *not* lost.
5. Segmented LRU versus buffered global LRU: state what each one relaxes, and construct an access pattern where the segmented version evicts a hotter entry than the global version would.
6. You've built layer 1 (concurrent map + locked list). A reader now reaches a node without holding the list lock. Which happens-before edge makes the node's value safe to read, and what would break if the value field were a plain mutable field?
7. The interviewer says throughput is 500 ops/sec and the team is three people. What do you build, and what sentence do you say about it?

### Transfers to

Read-heavy cache with expiry (07), the *complementary* half of the same production object: that problem solves loading and staleness (per-entry expiry, the cached-future single-flight idiom, dogpile prevention) and explicitly scopes eviction out; this one solves eviction and scopes loading out. Together they are Caffeine. Also transfers to: connection pools with idle eviction, page/buffer caches, session stores with capacity limits, any admission-control structure where "the ordering is the bottleneck" is the answer, and, conceptually, to `striped-counter-longadder`, which relaxes the *same* kind of global clause (an exact total) in the same way (per-thread accumulation, reconciled later).

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/concurrent-data-structures/thread-safe-lru-cache).
