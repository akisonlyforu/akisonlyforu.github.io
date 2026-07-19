---
layout: post
title: Copy-on-Write Snapshot Registry
date: 2026-07-19
description: >-
  It is the one problem in this family that takes the third bargain. The LRU cache, the striped map, the Treiber stack, the queue and the striped counter all keep the data…
categories: interview multithreading problems
---

Part of the [Concurrent Data Structures](/interview/multithreading/patterns/concurrent-data-structures/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Typically arrives disguised as a design detail — "how do you hot-reload config without a lock on the request path?", "how do listeners get notified while someone is subscribing?", "how does the router see the new backend list?" Very high day-job relevance; frequency claim is directional.

### Problem

Design a registry that is read on essentially every request and written rarely: a set of event listeners, a feature-flag or config map, a service-discovery backend list, a routing table. Reads must be as close to free as possible — no lock, no contention, no coordination between readers. Writes (register, unregister, reload) happen orders of magnitude less often.

Then be precise about what readers actually observe, and about the conditions under which this design stops being appropriate.

### Constraints

- Reads dominate overwhelmingly (many thousands to one).
- Readers frequently *iterate* the whole collection (notify every listener, evaluate every rule), and iteration must never fail or observe a half-applied write.
- Writers are rare but must be safe against each other and against all readers.
- Collection size is modest — hundreds or low thousands of entries, not millions.
- Single JVM.

### Clarify before solving

- **What is the write rate, honestly?** This single number decides whether the design is correct or catastrophic, since the write cost is proportional to the collection size.
- **How large is the collection, and is it bounded?** The other half of the same decision.
- **Must a reader that started before a write observe the write?** In other words, is a stale-but-consistent view acceptable? Usually yes, and confirming it is what licenses the whole approach.
- **Do readers hold the collection across a long operation** (iterating while making network calls to each listener)? Affects how long a snapshot stays alive and how much garbage accumulates.
- **Are writes bulk or incremental?** A config reload replacing everything at once is far friendlier to this design than a thousand individual registrations.
- **Do readers ever mutate through the collection** — remove a dead listener while iterating? The answer determines whether a snapshot-based structure's mutating-iterator behaviour is acceptable or a trap.

### Why this problem matters

It is the one problem in this family that takes the third bargain. The LRU cache, the striped map, the Treiber stack, the queue and the striped counter all keep the data mutable and fight over how to guard it. This one refuses the premise: make every *version* of the structure immutable and mutate only a single reference. The result is the only read path in the family that costs literally nothing — no lock, no CAS, no retry, no cache-line contention between readers — and understanding *why* that is safe (a volatile read, plus data that cannot change under you) is the cleanest possible application of the memory model.

It is also the family's best lesson in the shape of a bad fit. Copy-on-write is not a general-purpose concurrent collection; it is a specialised instrument with a cost that scales with size and write rate, and it is routinely deployed by engineers who saw "thread-safe list" in the class name and read no further. Being able to state exactly where the cliff is — and to recognize that a snapshot read followed by a decision is still a check-then-act, just at version granularity — separates people who know the class from people who understand the technique.

---

## Strategy

### Classify

Concurrent data structure solved by **eliminating the shared mutable state instead of guarding it**. This is rung 0 of the guarded-state escalation ladder — "can this be immutable?" — answered creatively: the collection cannot be immutable, but every *version* of it can be, leaving exactly one mutable word in the system.

### Invariant

- At any instant, the registry's contents are exactly the immutable collection that the current reference points to.
- Every reader observes a complete, internally consistent version — never a partially applied write, never a torn structure.
- Writes are totally ordered with respect to each other: each new version is derived from the version immediately preceding it, so no update is silently lost.

Note what is *not* in the invariant: nothing says a reader observes the latest version. Readers observe *a* version that was current at some point at or before their read. That omission is deliberate and is the contract you are selling.

### Mental model

A noticeboard with exactly one pinned sheet. Readers walk up and read the sheet — no queue, no permission, no interaction with each other, and the sheet cannot change while they read it because changing it means printing a *new* sheet. A writer retypes the whole sheet with its edit applied and swaps the pin in one motion. A reader who was mid-sheet keeps reading the old one, which is complete and coherent — just historical.

The cost is right there in the metaphor: to add one line, you retype the page. With a page a day, trivial. With a page a second and a thousand lines, you are running a printing press.

### Why readers need no lock at all — the memory-model argument

This is the part to get exactly right, because it is the whole justification.

A reader does one thing: a **volatile read** of the reference. From that moment it holds a collection whose contents are immutable, so:

- **Visibility is covered** by the volatile write → volatile read happens-before edge. Everything the writer did before publishing — building the copy, filling the array — happens-before the reader's use of it (the piggyback rule).
- **Reordering is covered** by the same edge; the reader cannot see the reference before the contents.
- **Atomicity is not needed**, because after the read there is nothing to make atomic. A reference read is a single word; the data behind it never changes.
- **Iteration cannot fail.** The iterator walks a frozen version, so there is no concurrent modification to detect and nothing to throw. A reader can hold a snapshot across a slow operation — notifying listeners over the network, evaluating a long rule chain — without blocking any writer for a microsecond.

If the snapshot's fields are **final**, the final-field guarantee gives fully-initialized visibility by any publication route, making the design robust even to a sloppy handoff. Belt and braces; worth mentioning.

Contrast with every other design in this family: striped locking makes readers cheap but not free (they may still block a bin-level writer, or vice versa); lock-free reads are free but the data can change under you mid-traversal, which is why those structures offer only weakly-consistent iteration. Copy-on-write is the only one that gives readers a **true snapshot** with zero coordination — and it does so by pushing the entire cost onto the writer.

### The write path

Writers must be serialized against each other, because each new version is derived from the current one — read-modify-write, which is check-then-act at the whole-collection level. Two options:

- **A writer lock.** Take it, copy the current collection, apply the change, publish the new reference, release. Simple, and it's what `CopyOnWriteArrayList` does. Readers never touch this lock, so it doesn't matter that writers serialize.
- **A CAS retry loop on the reference.** Read the current version, build the new one, compare-and-set; retry on failure. Lock-free for writers, but note the cost: a failed attempt discards a full O(n) copy, so contended writes are O(n) *per attempt*. Prefer the lock unless there's a reason.

Either way the publication is a **single volatile write of the reference** — the linearization point of the write, and the point at which the new version becomes the truth for everyone.

**Batch aggressively.** Because cost is per-write and proportional to size, applying twenty registrations as twenty writes costs twenty copies; applying them as one bulk write costs one. For config reloads this is natural. For incremental registration it's the difference between fine and pathological — and it's why a startup path that registers 500 listeners one at a time into a copy-on-write list performs 500 copies for a total of ~125,000 element writes.

### The cost, stated plainly

- **Every write is O(n)** in time and allocates an O(n) object. Write cost scales with collection size — the opposite of every other structure here.
- **Garbage.** Each write produces a whole dead copy. With long-held reader snapshots, several versions stay live simultaneously, so the memory high-water mark is a multiple of the collection size.
- **Memory bandwidth**, which at high write rates is the real ceiling long before the allocator is.

The cliff is a product of *size × write rate*. A 50-element listener list rewritten on deploy: perfect. A 10,000-element list rewritten per request: a machine on fire, with correct results.

### When it's a trap

1. **Any non-trivial write rate.** The name says "thread-safe list" and says nothing about O(n) writes. This is the single most common misuse. If writes are frequent, you want a concurrent map or set (a concurrent-map-backed set is the usual replacement) and you want to give up snapshot iteration.
2. **The stale-snapshot decision.** A reader takes a snapshot, examines it, and then acts on the *live* registry. That is check-then-act with version granularity: the world moved between the snapshot and the act. Fine for "notify everyone who was registered when I started"; wrong for "check nobody holds this lease, then take it." The rule: a snapshot is safe to *act on* only when acting on a stale set is semantically acceptable. If the decision must be atomic with the state, this is the wrong structure — move the decision inside an atomic operation on a different structure.
3. **Mutating through the iterator.** The iterator's collection is immutable, so element-removal through it cannot work — it either throws or, worse, quietly operates on a version nobody else can see. Readers wanting to remove entries must call the registry's removal method, which produces a new version.
4. **Read-your-own-write expectations.** A writer's own subsequent read does see its write (program order plus the volatile edge), but *other* threads mid-operation do not, and a reader that captured a snapshot before your write will finish its whole operation without ever seeing it. If a caller needs "my registration is live before I return," say what "live" means and whether in-flight readers count.
5. **Unbounded growth.** Nothing evicts. A registry that accumulates listeners for objects that died is a leak with an O(n) write cost that grows over time — the leak degrades performance quadratically, not linearly. Unregistration must be a real, exercised path.
6. **Large elements or deep copies.** The copy is shallow: the array is new, the elements are shared. That's fine and intended — the *elements* must be immutable or independently thread-safe for the design to hold. If elements are mutable and shared, copy-on-write bought you nothing; a reader can still see one mutate mid-iteration.

### The variants worth naming

- **Copy-on-write list** for ordered, small, read-mostly sequences — the canonical listener list.
- **Copy-on-write set** for membership checks; note the linear-scan `contains`, which is fine at small sizes and is a second reason the size bound matters.
- **Immutable-map-in-a-volatile-field** for config: often better hand-rolled than using a copy-on-write collection, because you get O(1) lookup inside the snapshot while keeping the free read path. This is the usual answer for feature flags and routing tables, and it's worth proposing explicitly — copy-on-write is a *technique*, and the JDK's copy-on-write classes are just one packaging of it.
- **Persistent / structurally-shared data structures** (immutable maps and vectors that share most of their internals between versions) reduce the write cost from O(n) to O(log n) while keeping the free read path. Awareness-level mention; it's the principled fix when writes are too frequent for full copies but snapshot reads are non-negotiable.

### Pitfalls

1. Using it for a write-heavy collection. The defining misuse.
2. Registering many entries one at a time instead of in bulk.
3. Treating a snapshot as a live view and making a decision on it that needs to be atomic with an action.
4. Sharing mutable elements inside the snapshot — the copy is shallow, so element mutation is still a data race.
5. Making the reference non-volatile. Without the edge, a reader can see the new reference and stale contents, or never see the write at all. This is the whole mechanism; a missing `volatile` here silently removes it.
6. Two writers with no serialization, each deriving from the same base version — lost update, one registration silently vanishes.
7. Expecting a reader mid-iteration to observe a concurrent write, or writing a test that asserts it.
8. No unregistration path, so the collection grows and each write gets slower.
9. Reaching for a read-write lock instead. It's the wrong tool: readers would still coordinate (a shared-mode acquire is a contended write to the lock's own state), and writers would block readers. Copy-on-write's readers coordinate with *nothing*, which is strictly better for this access pattern — and saying why is a strong answer to "why not just use a RW lock?"

### Check your understanding

1. Why does a reader need no lock, no CAS, and no retry? Name the specific happens-before edge and the property of the data that makes the edge sufficient.
2. Why can iteration never throw a concurrent-modification exception here, and how does that contract differ from a weakly-consistent iterator?
3. A writer publishes while a reader is halfway through notifying listeners. Describe exactly what the reader observes for the rest of its loop, and argue that it is acceptable.
4. Compute the total element-copy cost of registering n entries one at a time, and explain why bulk registration is not a micro-optimization.
5. Give a use of a snapshot that is safe and one that is a check-then-act bug, and state the rule that distinguishes them.
6. The elements in the snapshot are mutable objects shared with other code. What did copy-on-write actually guarantee, and what did it not?
7. Why is a read-write lock a worse fit than copy-on-write for this access pattern? Answer in terms of what readers do to shared state.
8. The write rate rises by 100x and snapshot reads are still required. What are your options, in order?
9. Which bargain from this family does this design take, and which two does it decline? What does each of the three cost?

### Transfers to

The CAS-on-immutable-snapshot idiom used for multi-field atomic state throughout this family and the time-based family (a (value, timestamp) pair swapped as one object); service discovery and dynamic routing tables; feature-flag and config hot-reload; observer/listener registries anywhere; `thread-safe-lru-cache` by contrast — the instructive comparison being that a cache's read path *mutates*, which is precisely why it cannot use this technique; and, at system scale, the read-optimized-replica and immutable-deployment-artifact patterns, which make the same bargain about versions rather than objects.
