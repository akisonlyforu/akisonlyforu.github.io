---
layout: post
title: Lock Striping and How ConcurrentHashMap Works
date: 2026-07-19
description: >-
  This is where lock striping stops being a buzzword and becomes a derivation. The map's invariant decomposes into one independent clause per bin — and once a candidate can…
categories: interview multithreading problems
---

Part of the [Concurrent Data Structures](/interview/multithreading/patterns/concurrent-data-structures/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** A staple follow-up rather than a standalone coding exercise — it typically arrives after "make this map thread-safe" as "…and why is `ConcurrentHashMap` better than `Collections.synchronizedMap`?" Very common in senior Java rounds; frequency claim is directional.

### Problem

You have a hash map that must serve many threads. Explain and design how to avoid a single global lock: derive per-bin (or striped) locking from the map's invariant, then account for the hard parts — what happens during a resize, what `size()` can honestly mean, what an iterator can honestly promise, and which caller-visible operations are *still* unsafe despite every method being atomic.

You may be asked to sketch a striped map yourself, or to explain `ConcurrentHashMap`'s design and justify its trade-offs. Both are the same reasoning.

### Constraints

- Lookups must not block each other, and ideally must not block against writers to other keys.
- Writes to different keys should proceed concurrently.
- The map is resizable — capacity is not fixed up front.
- Callers will compose operations (read-then-write on the same key); the design must give them a safe way to do that.
- Single JVM.

### Clarify before solving

- **Is the ask "design a striped map" or "explain ConcurrentHashMap"?** They differ mainly in how much of the resize protocol you're expected to cover.
- **What are the aggregate requirements?** Does anyone need an exact `size()`, or a consistent iteration snapshot? If yes, the whole design changes — and the honest answer may be "then you don't want this structure."
- **What compound operations will callers perform?** get-then-put, increment-a-counter-per-key, insert-if-absent. Each has an atomic API and the design should name it.
- **Are null keys or values needed?** Worth asking, because the answer ("`ConcurrentHashMap` forbids them") has a concurrency reason behind it, not an arbitrary one.
- **Read:write ratio, key skew, expected thread count?** Key skew decides whether striping actually helps; a single scorching-hot key defeats every sharding scheme.

### Why this problem matters

This is where lock striping stops being a buzzword and becomes a derivation. The map's invariant decomposes into one independent clause per bin — and once a candidate can *show* that decomposition, per-bin locking is not a trick they memorized, it's the only sensible conclusion. Everything else in the family (striped counters, segmented caches, per-key single-flight) is the same move applied to different state.

It is also the best available vehicle for three uncomfortable truths that separate people who use concurrent collections from people who understand them: that a concurrent structure's aggregate queries are estimates rather than facts, that its iterators offer a weaker contract than snapshot iteration and this is a feature, and that swapping in `ConcurrentHashMap` does absolutely nothing for the compound-operation races in the calling code. That last one is the single most common real-world bug this family produces, and interviewers probe for it deliberately.

---

## Strategy

### Classify

Concurrent data structure whose composite invariant decomposes **almost completely** into independent local clauses. The opposite of the LRU cache: here the seams are everywhere, and the only global clauses left over are the count and the table itself. That makes this the family's cleanest positive example of fine-grained locking.

### Invariant

- Every key is present in exactly one bin — the one its hash selects — and appears at most once in that bin's chain.
- Each bin's chain (or tree) is well-formed: no cycles, no dangling links, no partially-linked node visible to a traversal.
- The map's element count equals the sum of the bins' chain lengths. *(This is the one global clause, and it is the one we will give up.)*
- During a resize, every key is findable: in the old table, in the new table, or via a marker pointing from one to the other — never in neither.

### Mental model

A wall of numbered mailboxes. The postal rule (a letter goes in the box its address hashes to) means two clerks working on different boxes cannot possibly interfere — not because they're being careful, but because there is no shared object between them. One lock per box isn't an optimization applied to a global structure; it's an accurate description of where the structure's actual constraints live. The global lock was the lie.

Two things are still global: counting all the letters in the building (you'd have to hold every box still, or accept an estimate), and moving the whole wall to a bigger building (the resize).

### Deriving per-bin locking (do this, don't recite it)

Write the invariant clause-by-clause as above, then ask of each pair of clauses: *can an operation on bin i violate a clause about bin j?* It cannot — the bins share no state, and a key's bin is a deterministic function of its hash. So the clauses are independent, and independent invariants may take independent guards. That's the guarded-state family's decomposition rule, and it lands here with no residue.

**Striping is the coarsened version.** If one lock object per bin is too many objects, use K locks and map bin → `lock[i % K]`. Contention falls roughly as 1/K, up to the number of concurrently active threads; beyond that, more stripes buy nothing but memory. Striping is what you build by hand; per-bin locking (synchronizing on the first node of the bin) is what modern `ConcurrentHashMap` does, which is striping taken to its limit with zero extra objects.

**The hash matters.** Real key hash codes cluster (small integers, sequential ids, strings with common prefixes). If clustering puts everything in a few bins, you have a global lock with extra steps. This is why implementations *spread* the hash (mixing high bits down) before masking — a concurrency concern, not just a lookup-quality one. Say it; it's a detail that shows you've thought past the diagram.

### Why reads take no lock at all

The part people skip. Lookups traverse a chain without acquiring anything, and this is safe because of the memory model, not luck:

- Node `next` pointers and value fields are **volatile** (or written through atomics), so a traversing reader has a happens-before edge to whatever the last writer did — no stale or half-written links.
- New nodes are **fully constructed before being linked in**. The store that makes a node reachable is the publication point, and everything the writer did before it is visible to anyone who reads that pointer. Publish-then-initialize would be the bug; initialize-then-publish is the rule.
- Removal splices a node out, but a reader already holding a reference to that node keeps reading a coherent, if now-detached, node. It sees a value that *was* correct at some point in the traversal — the definition of the weak contract, below.

So: **writers lock one bin, readers lock nothing.** That's the whole performance story, and it's why the structure beats `synchronizedMap` by more than a factor of K on read-heavy workloads — `synchronizedMap` serializes *reads against reads*, which is pure waste.

### Resize under concurrency

The one clause that is genuinely global, and the part candidates most often have no answer for. The naive approach — take every lock, rehash, release — is correct and is what a hand-rolled striped map usually does; say that first, because it's honest and simple. Then describe the real design:

- Allocate the new table, and migrate **bin by bin**. A migrated bin has its old slot replaced with a **forwarding marker** that says "this bin has moved; look in the new table."
- A reader arriving at a forwarding marker follows it and continues its lookup in the new table. It never blocks and never misses a key.
- A writer arriving at a forwarding marker doesn't block either — it **helps**: it takes on a range of bins to migrate, then retries its own operation. Under load this makes resize faster rather than slower, and it means no thread is ever parked waiting for a resize it could be doing.

The lesson to extract out loud: *when the global clause is unavoidable, make the transition observable and let arriving threads cooperate with it, rather than making them wait for it.* That's a transferable technique, not a `ConcurrentHashMap` trivia item.

### size() is an estimate — and that's the right choice

To make `size()` exact you would need to freeze every bin at once. The alternative is per-stripe counters summed on demand, with the sum read without any global lock. Consequences to state:

- The returned value was true at *some* point during the summation, possibly at no single instant if writes are concurrent. It is a **hint**.
- Any caller branching on it — "if size < capacity then put" — has written a check-then-act. Same disease as always, one level up; the cure is an atomic compound operation or an explicit lock around the pair, not a fresher `size()`.
- `isEmpty()` has the same property and is *slightly* stronger in practice (it can short-circuit), but it is still a hint.
- Under contention the per-stripe counters themselves become the bottleneck if implemented as one shared `AtomicLong` — which is exactly why the count is itself striped internally, and why this problem's sibling (`striped-counter-longadder`) exists. Note that the counter got the same treatment the map did.

### Weakly-consistent iterators

The contract, stated precisely: an iterator traverses the structure as it exists during the traversal. It reflects *some* modifications made after it was created and not necessarily others. It never throws `ConcurrentModificationException`, never returns an element twice, and never skips an element that was present for the entire traversal. It is **not** a snapshot.

Compare the three available contracts, because knowing which you're offering is the point:
- **Fail-fast** (plain `HashMap`): detects concurrent modification and throws — a debugging aid, not a safety mechanism, and explicitly best-effort.
- **Snapshot** (copy-on-write structures): a frozen version; consistent, but O(n) per write to produce.
- **Weakly consistent** (`ConcurrentHashMap`, `ConcurrentLinkedQueue`): no locking, no throwing, no consistency guarantee across the whole traversal.

Aggregate operations like `forEach`, `reduce`, and the bulk search methods have the same weak semantics. They're useful for metrics and maintenance sweeps; they're wrong for anything that needs a consistent view of the whole map at one instant. If a caller needs that, they need a different structure — say so rather than pretending the map can provide it.

### Compound operations still need compute*/merge — the headline lesson

Every method being atomic gives you nothing about *sequences of methods*. The classic broken pattern is a read, a decision, and a write:

- "if the key isn't there, put a new value" → two operations, another thread can insert in the gap → use `putIfAbsent` or `computeIfAbsent`.
- "get the counter, add one, put it back" → lost updates, exactly like `count++` → use `merge` or `compute`.
- "if the value equals X, replace it with Y" → use the two-argument `replace` (a CAS at map level).
- "remove only if it still maps to this value" → use the two-argument `remove`.

These exist precisely because the check and the act must happen inside the bin's critical section. That is the guarded-state cure — check-then-act made atomic — delivered as API surface.

Two cautions worth volunteering:
- **Keep the mapping function short and side-effect-free.** It runs while the bin is locked, so expensive work inside it blocks every other key in that bin; and re-entrant operations on the same map from inside the function can deadlock or corrupt the structure. The `cached-future idiom` from the read-heavy cache problem is the canonical way to have it both ways: create a cheap future inside the function, run the expensive work outside it.
- **`computeIfAbsent`'s atomicity is per key, and that is genuinely the point** — it is the cheapest per-key mutual exclusion available in the JDK, and it's why per-key single-flight needs no explicit lock map.

### Why null is forbidden

Small, but it's a real question and it has a real answer. In a plain map, a `get` returning null is ambiguous — absent, or present with a null value — and you disambiguate with `containsKey`. Under concurrency that disambiguation is a check-then-act: the mapping can change between the two calls, so the ambiguity is *unresolvable*. Banning nulls removes an entire class of races rather than documenting around them.

### Pitfalls

1. Swapping in a concurrent map and believing the calling code is now safe. It isn't; the compound races are untouched. This is the #1 real-world bug in the family.
2. Branching on `size()` / `isEmpty()` / `containsKey` as if they were facts.
3. Expensive or re-entrant work inside a `compute`/`merge` mapping function.
4. Assuming iteration gives a consistent snapshot; building a report or a balance check on top of it.
5. Hand-rolled striping with no plan for resize — either take all locks (say so) or implement forwarding, but don't leave it unaddressed.
6. Striping with an unspread hash, so real-world keys collapse into a few stripes.
7. Cross-key operations (move a key, transactional two-key update) done with two stripe locks and no global acquisition order — the lock-order-inversion deadlock, unchanged from the guarded-state family.
8. Assuming striping helps a hot-key workload. One key is one bin is one lock; sharding cannot help skew, only spread. The fix for a hot key is a different design (per-thread accumulation, or caching the value outside the map).
9. Claiming `ConcurrentHashMap` is "lock-free." Reads are; writes take a bin lock. Precision matters here.

### Check your understanding

1. Derive per-bin locking from the invariant without mentioning `ConcurrentHashMap`. Which clause fails to decompose, and what did you do about it?
2. Why can a reader traverse a chain with no lock? Name the two memory-model facts that make it safe, and the ordering rule a writer must obey when linking a new node.
3. A reader arrives at a bin mid-resize. Trace what it does. Now a writer arrives — what does it do differently, and why is that better than waiting?
4. State exactly what `size()` returns under concurrent modification, and give a caller pattern that misuses it.
5. Define the weakly-consistent iterator contract in one sentence, then name a use case it serves well and one it silently breaks.
6. Rewrite each of these as a single atomic call: insert-if-absent; increment-per-key counter; replace-if-equal; remove-if-equal.
7. Why does `computeIfAbsent` give you per-key mutual exclusion for free, and what must you *not* do inside its mapping function?
8. Your striped map has 16 stripes and 64 threads, and throughput is no better than a global lock. Give three distinct explanations and how you'd distinguish them.

### Transfers to

Every other problem in this family (the striping move is the same in `striped-counter-longadder`, in segmented LRU, and in per-key single-flight); the read-heavy cache with expiry (07), whose entire read path rests on the "reads take no lock, `computeIfAbsent` gives per-key exclusion" facts derived here; make-a-class-thread-safe (02), of which this is the scaled-up sequel; and any database-sharding or partitioned-service design conversation, where "the aggregate query is the part that doesn't shard" is the same observation at a different altitude.
