---
layout: post
title: Concurrent Data Structures Playbook
date: 2026-07-19
description: >-
  Does the invariant decompose? Lock striping, lock-free CAS and immutable snapshots, the ABA problem, false sharing, and when to renegotiate the specification itself.
categories: interview multithreading patterns
---

Deep dive on concurrent data structures, companion to [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework/). This is the guarded-state family asked again with a throughput requirement attached, and it is the only family where the machine — cache lines, contended CAS retries — decides the design.

The family where the state is no longer a field or two but a **structure** — a map, a list, a stack, a queue, a counter — and the requirement is no longer "be correct" but "be correct *and* let N cores through at once." Every problem here starts where the guarded-state family stops: you already know the coarse lock is correct; the question is what you may safely give up to stop serializing on it.

The three questions of the guarded-state family still apply (what state is shared, what invariant spans it, what guard makes it atomic). This family adds a fourth, and it is the one being graded:

4. **Which part of the invariant is genuinely global, and which parts only *looked* global?**

Because a coarse lock is the answer to a structure whose invariant is global. Almost none of them are. A hash map's invariant ("every key appears in exactly one bin, in that bin's chain") is *per-bin*, not per-map — and the entire design of `ConcurrentHashMap` falls out of noticing that. Conversely, an LRU cache's recency ordering *is* global — one list, one head, one tail — which is why LRU is hard and hash maps are easy, and why the interview asks about LRU.

---

## 1. The family's core tension

There are exactly three ways to stop serializing on one lock, and they are not variations of each other — they are different bargains with different currencies.

| Strategy | The move | You pay in |
|---|---|---|
| **Fine-grained / striped locking** | Shatter one lock into K locks over K disjoint pieces of state | Multi-lock protocol (ordering, deadlock), cross-shard operations become hard/approximate, resize is a nightmare |
| **Lock-free (CAS)** | Replace the lock with an atomic instruction and a retry loop | Retry waste under contention, ABA, memory reclamation, an invariant that must fit in ONE word |
| **Immutability / copy-on-write** | Never mutate; publish a whole new version via one volatile/atomic reference swap | O(n) per write, garbage, readers may act on a snapshot that is already stale |

**The tension in one sentence:** *fine-grained locking scales writes but multiplies the invariants you must reason about; lock-free scales contention but shrinks the invariant to what a single CAS can cover; immutability makes reads free but makes writes proportional to size.* Every problem in this family is a choice among those three, and the senior answer names all three and justifies the pick with the read/write ratio and the structure's size.

The **read:write ratio and the structure's size are the two inputs that decide it.** Memorize the decision:

- Read-mostly and small (config, listener lists, routing tables) → **immutability**. Zero read cost, and O(n) writes are irrelevant when writes are rare.
- Mixed and large, with a natural key → **striping**. The key gives you the shard function for free.
- Tiny state (one counter, one head pointer) with brutal contention → **CAS**, or **per-thread accumulation** if you can relax the read (see `LongAdder`).
- Anything with a **global ordering** requirement (LRU recency, FIFO fairness, total order) → the ordering is the bottleneck, not the data; either accept one lock, or *relax the ordering contract*.

That last bullet is the family's deepest lesson and §7 is about it.

## 2. What a structure adds beyond guarded state

Four things change when the guarded thing has shape:

**(a) The invariant becomes composite.** A stack's invariant is "LIFO order, no lost or duplicated element, size matches the chain." A map's is "each key in exactly one bin; the chain in that bin is well-formed; count reflects the chain lengths." Composite invariants can be *decomposed*, and decomposition is exactly what buys concurrency — but only along the seams where clauses are genuinely independent. This is §1's "one invariant → one lock, independent invariants may take independent locks" from the guarded-state family, applied to a structure with many natural seams.

**(b) Aggregate queries become lies.** `size()`, `isEmpty()`, `contains()`, and iteration cross *every* shard. Under fine-grained locking, no single lock covers them, so either you take all K locks (serializing the world) or you accept an estimate. The JDK accepts the estimate. Anyone branching on it has written a check-then-act (guarded-state failure mode #11) — the estimate is a hint, never a guarantee.

**(c) Structural mutation vs value mutation.** Changing a value in a node touches one word. Changing the *shape* — splicing a node out of a list, resizing a table, rebalancing a tree — touches several pointers that must appear to change together. This is why lists and trees are much harder than maps of independent cells, and why `ConcurrentHashMap` has a resize protocol but `AtomicLong` does not.

**(d) Reads may be writes.** The signature trap of this family. An LRU `get` mutates recency; a cache `get` bumps a hit counter; a `contains` on a self-adjusting structure splays. If the "read" mutates, the read path cannot be lock-free by wishing — you must either make the mutation lock-free/lossy or admit the read path takes a lock. **Ask "does any read mutate?" before designing the read path.**

## 3. Fine-grained locking, mechanically

### Deriving the shards

Take the composite invariant, write one clause per line, and ask of each pair: *can these two clauses be violated by operations that never touch each other's state?* Clauses that share no state are independent → one lock each. For a hash table, "bin i is a well-formed chain containing exactly the keys hashing to i" is one clause **per bin**, and the clauses share nothing. That is the whole derivation of per-bin locking. Nothing about hashing is magic; the hash function is just a cheap way to name the shard.

Striping is the coarsened version: K locks for N cells, `lock[hash(key) % K]`. Use it when N is huge or unbounded and one lock object per cell is wasteful. K is a tuning knob; contention falls roughly as 1/K until you hit the number of concurrent threads, past which more stripes buy nothing.

### The three costs, always named

1. **Cross-shard operations.** Anything spanning two shards (moving a key, a two-key transaction, `size()`) needs either multiple locks *in a global order* (guarded-state §5 — the deadlock machinery is unchanged and still yours to recite) or a relaxed contract. Most concurrent structures choose "relaxed contract" and that is why their aggregate methods are weak.
2. **Rehash / resize.** The one operation whose invariant *is* global: every element must move, and readers must find elements during the move. Real designs solve it by making the transition observable and cooperative — a per-bin forwarding marker that says "this bin has moved; go look in the new table," with arriving threads *helping* migrate rather than blocking. Know the shape; you will not be asked to implement it, you will be asked "what happens to a reader during resize?"
3. **Per-method atomicity does not compose — still.** Striping does not repeal the compound-operation trap. `containsKey` then `put` is two shard-lock acquisitions with a gap between. The cure is the same as it always was: an atomic compound method that does check and act inside one shard's critical section — `putIfAbsent`, `computeIfAbsent`, `compute`, `merge`. **Every concurrent-map answer must say this sentence unprompted.**

## 4. Lock-free, mechanically

### The shape

Every lock-free operation is the same loop: read the current state into a local, compute the new state as a *pure function* of what you read, CAS it in, retry from the top if the CAS fails. The CAS is simultaneously the check ("nothing changed since my read") and the act ("install my version") — that is why it cures check-then-act without a lock: hardware makes the pair atomic (guarded-state skeleton S8, unchanged).

The retry loop is not a lock in disguise: a failed CAS means *someone else made progress*. That is the definition of lock-free — the system as a whole always advances, even if an individual thread starves. Contrast:

- **Wait-free**: every thread finishes in a bounded number of steps (rare, expensive; `LongAdder`'s per-cell increment is effectively this in the uncontended case).
- **Lock-free**: some thread always makes progress; individuals may retry forever.
- **Obstruction-free**: a thread makes progress if it eventually runs alone.
- **Blocking**: a suspended thread can stop everyone (any mutex — this is the real argument for lock-free: a thread descheduled inside a critical section, or killed, or page-faulted, freezes the structure).

**Say the honest version out loud:** lock-free wins where a thread holding a lock could be preempted at the worst moment (interrupt handlers, GC-sensitive paths, hard latency tails), and where the critical section is a single pointer swing. It **loses** to a plain mutex when contention is high and the retry loop burns CPU recomputing work it will throw away, or when the operation is long (an uncontended mutex acquire is tens of nanoseconds; a livelocked CAS loop is unbounded waste). "Lock-free is faster" is cargo cult; "lock-free is *non-blocking*" is the true claim.

### ABA, in full

The CAS checks a *value*, and you wanted it to check *"nothing happened."* Those differ whenever a value can be removed and later restored.

```
Stack: top -> A -> B -> C

T1: reads top = A, reads A.next = B. Preempted before its CAS.
T2: pops A            (top -> B -> C)
T2: pops B            (top -> C)
T2: pushes A back     (top -> A -> C)     // A reused, A.next now C
T1: resumes. CAS(top, expected=A, new=B)  // SUCCEEDS — top is A again!
    top -> B, and B.next still points at C... but B was popped and is gone.
```

The structure is now corrupt: a popped node is back on the stack, or (in the mirror case) live nodes are lost. **The CAS succeeded and the invariant broke.** That is the whole lesson: *CAS proves the value is unchanged, not that the world is unchanged.*

Cures, in order of how often they're the right answer:

1. **Version stamps** — CAS the pair (pointer, counter) atomically, incrementing the counter on every change (`AtomicStampedReference`, or a double-width CAS / packed 64-bit word in native code). ABA becomes impossible because the counter never repeats in practice.
2. **Don't reuse nodes** — allocate a fresh node per push. In a garbage-collected runtime this is the default and it is why **ABA is largely a non-issue for a JVM Treiber stack**: a node still referenced by T1's local variable cannot be freed, cannot be reallocated, and therefore cannot come back as a *different* object at the same address. The JVM's GC is doing safe memory reclamation for you, invisibly. Say this explicitly — it's the difference between a memorized answer and an understood one.
3. **Immutable payloads / whole-snapshot CAS** — if you CAS a reference to a fresh immutable object every time, there is no address to reuse.

But note the residual: even on the JVM, ABA can bite if you CAS on a *value* with a small domain (a version-less state enum, an int) rather than on freshly allocated references. GC saves you from address reuse, not from value reuse.

### Memory reclamation (why non-GC languages suffer)

In C++, T1 holds a raw pointer to node A and is about to dereference `A.next`. T2 pops A and frees it. T1 dereferences freed memory — a use-after-free, not merely ABA. Lock-free structures in non-GC languages therefore need an explicit reclamation scheme: **hazard pointers** (each thread publishes the nodes it is currently dereferencing; a reclaimer only frees nodes no one has published), **epoch-based reclamation** (free only what was retired before the oldest active epoch), or reference counting (expensive). The one-line summary for an interview: *"garbage collection is a memory-reclamation scheme, and lock-free data structures need one; the JVM hands it to you free, which is why lock-free algorithms are dramatically easier to write correctly in Java than in C++."*

## 5. Immutability and copy-on-write

The third bargain. Never mutate the structure; build a whole new one and swap a single reference.

Readers do a **plain volatile read of the reference** and then touch only immutable state, so:
- No lock, no CAS, no retry, no contention among readers at all — reads scale perfectly and linearly.
- Safe publication is free via the volatile write → volatile read happens-before edge (and via final fields if the snapshot's fields are final).
- Iteration cannot throw `ConcurrentModificationException` — the iterator holds a snapshot nobody can change, ever.

Writers pay: copy the whole array/map (O(n)), apply the change, write the new reference. Two writers racing must be serialized — by a writer lock (that's `CopyOnWriteArrayList`) or by a CAS retry loop on the reference (that's the read-modify-write-on-immutable-snapshot idiom, and note the retry re-copies, so contended writes are O(n) *per attempt*).

**When it's a trap:** any non-trivial write rate. A COW list with 10k listeners and a write per second is fine; with a write per request it is a memory-bandwidth heater and a GC pressure source. Also: the read is a snapshot, so a reader's decision can be based on a version that no longer exists — fine for "notify all current listeners," fatal for "check the balance then debit it" (check-then-act, again, at the version level).

This is the direct descendant of the guarded-state ladder's rung 0 (*eliminate the shared mutable state*): you cannot make the whole structure immutable, so you make every *version* of it immutable and mutate only the pointer.

## 6. Memory-model notes for this family

Everything from the guarded-state family's happens-before list still holds. Three additions specific to structures:

1. **A successful CAS is both a volatile read and a volatile write** on that variable. So it carries the piggyback rule in both directions: everything the CASing thread did *before* the CAS is visible to any thread that subsequently reads that variable, and the CAS's read of the variable sees the latest write. This is what makes a Treiber push safe: the node's fields are written, then the CAS publishes the node, and a popper who reads `top` gets a full happens-before path to those field writes. **A failed CAS is (at minimum) a volatile read** — which is why a retry loop always re-reads fresh state and never spins on a stale local.
2. **Node fields should be final where possible.** A node whose `item` is final is safely published by any route (final-field guarantee), which removes an entire class of "I published the node before writing its payload" bugs. Where the field must be mutable (a `next` pointer), it must be volatile or written through an atomic — otherwise a reader traversing the chain has no edge to it.
3. **False sharing is a performance failure, not a correctness one** — but in this family it is the *dominant* cost and you must be able to name it. Two independent variables landing on the same 64-byte cache line make every write to either one invalidate the other's cached copy on every other core (cache-line ping-pong). This is why striped counters pad their cells, why ring-buffer producer and consumer sequence counters live on separate lines, and what `@Contended` (and manual padding) exists for. The memory model says nothing about it; the machine says everything.

## 7. Relaxing the contract — the senior move

When the structure has a **global** clause (total ordering, exact size, a single head), no amount of sharding helps: the global clause is the serialization point. At that wall you have exactly two options, and picking the second is what distinguishes a senior answer:

**Option A — accept the lock on that clause.** Often correct! One lock over an LRU cache is genuinely fine if the critical section is a handful of pointer swaps and your traffic doesn't saturate it. Ship it, name the ceiling, move on.

**Option B — weaken the contract until the clause stops being global.**

- Exact LRU → **approximate** LRU. Nobody's SLA says "evict precisely the least-recently-used entry"; it says "keep the hit rate high." That single concession converts recency from a strictly-ordered global list into a *statistical* signal, which can be maintained lazily, in per-thread buffers, with drops allowed. This is what production caches actually do.
- Exact size → **estimate**. Sum of per-shard counts, read without a global lock.
- Strongly-consistent iteration → **weakly-consistent iteration**: the iterator traverses the structure as it exists, reflects some updates made after creation and not others, never throws, and never repeats or skips an element that was present the whole time. That is a *documented, useful* contract; it's just not a snapshot.
- Total FIFO order across producers → per-producer order plus a global sequence assigned by one atomic increment.

**The framing to say out loud:** *"the ordering requirement is what's serializing us; how exact does it have to be?"* Interviewers in this family are usually waiting for exactly that question.

## 8. Pseudocode skeletons

Shapes to code from. Fill in the invariant, not the ceremony.

**C1 — Striped locking:**
```
locks[K]; buckets[K]           // K disjoint pieces, one lock each
op(key):
    i = spread(hash(key)) % K
    lock locks[i]:
        act on buckets[i]      // invariant is LOCAL to bucket i
// size(): sum buckets without holding all locks  -> ESTIMATE, say so
// cross-shard op: acquire in ascending index order -> no cycle
```

**C2 — Atomic compound op on a shared map (the anti-check-then-act):**
```
map.compute(key, (k, old) -> f(old))     // check + act inside the bin's lock
// NOT: if (map.containsKey(k)) map.put(k, f(map.get(k)))
```

**C3 — Treiber stack (lock-free LIFO):**
```
volatile top

push(v):
    node = new Node(v)                    // fresh node: no ABA, no reuse
    loop:
        node.next = top.get()             // read
        if top.CAS(node.next, node): return   // check+act; linearization point

pop():
    loop:
        cur = top.get()
        if cur == null: return EMPTY
        if top.CAS(cur, cur.next): return cur.item
// ABA: safe here ONLY because nodes are freshly allocated and GC'd.
// Reuse nodes (or CAS a small-domain value) -> need a version stamp.
```

**C4 — CAS on an immutable snapshot (multi-field state, lock-free):**
```
volatile AtomicReference<Snapshot> ref     // Snapshot has final fields

update(delta):
    loop:
        old = ref.get()
        new = old.with(delta)              // pure; allocates
        if ref.CAS(old, new): return
// whole-object swap => all fields change together, atomically
```

**C5 — Copy-on-write registry (read-mostly):**
```
volatile snapshot = immutableList()

read():   return snapshot                  // no lock at ALL; volatile read
iterate(): for x in snapshot: ...          // frozen version; never CME

add(x):
    lock writerLock:                       // serialize writers only
        snapshot = snapshot + x            // copy O(n), then ONE volatile write
```

**C6 — Striped counter (hot-write, cold-read):**
```
cells[K] (padded to cache lines); base

inc():
    c = cells[probeForThisThread()]
    if !c.CAS(c.v, c.v+1): rehash probe; retry    // contention spreads threads out
sum():
    return base + sum(cells)              // NOT atomic vs concurrent incs -> estimate
```

**C7 — Bounded ring buffer with sequence counters (SPSC/Disruptor shape):**
```
buffer[N]  (N power of two)
volatile producerSeq, consumerSeq         // on SEPARATE cache lines (padding/@Contended)

publish(v):
    next = producerSeq + 1
    while next - consumerSeq > N: spin/park        // full: producer waits on consumer
    buffer[next & (N-1)] = v
    producerSeq = next                    // volatile write = publication + HB edge

consume():
    while consumerSeq == producerSeq: spin/park    // empty
    v = buffer[(consumerSeq+1) & (N-1)]   // volatile read of producerSeq gave us the edge
    consumerSeq = consumerSeq + 1
    return v
// no CAS at all in single-producer/single-consumer: each counter has ONE writer
```

**C8 — LRU with the recency list under one lock, reads relaxed:**
```
map: key -> node          (concurrent)
list: doubly-linked, head=MRU              (guarded by listLock)

get(k):
    node = map.get(k)                      // lock-free lookup
    if node == null: return MISS
    recordAccess(node)                     // <-- the mutating "read"
    return node.value

recordAccess(node):                        // strict version:
    lock listLock: moveToHead(node)
                                           // relaxed version:
    ringBuffer[threadSlot].offerOrDrop(node)   // lossy; drained under listLock
                                               // by whoever wins tryLock
```

## 9. The derivation recipe

Run in order. Steps 1–3 are the guarded-state recipe compressed; 4 onward is what this family adds.

1. **Name the structure's operations and their frequencies.** reads/writes ratio, hot keys, expected size, thread count. Without these you cannot justify anything, and "I'd measure first" is the honest opener.
2. **Write the composite invariant, one clause per line.** Include the aggregate clauses (size, ordering) separately — they are the ones that will hurt.
3. **Baseline: one coarse lock.** State that it's correct, state what it costs (all operations serialized), and *do not leave it until you have a reason*. The escalation ladder from the guarded-state family is unchanged; this family is just rungs 3 and 4 in detail.
4. **Ask: does any read mutate?** If yes, the "read path" is a write path — say so now, before you promise lock-free reads.
5. **Classify each clause as LOCAL or GLOBAL.** Local = touches state that operations on other keys/indices never touch. Global = spans everything (total ordering, exact count, single head/tail, resize).
6. **For the LOCAL clauses, pick the bargain** (§1): striping if there's a natural key; CAS if the clause fits in one word; immutability if reads dominate and n is small.
7. **For the GLOBAL clauses, run §7:** either accept a lock on that clause alone (and keep it off the local paths), or negotiate the contract down (approximate / estimate / weakly-consistent) — and state explicitly what the caller loses.
8. **Audit compound operations.** Which caller sequences will realistically be written across your now-independent pieces? Provide atomic compound methods for each (`computeIfAbsent`, `merge`, `tryPop`, `getAndAdd`); document that everything else is a check-then-act on a hint.
9. **Audit the memory model.** For every unlocked read path: which happens-before edge makes the data visible and fully-constructed? (volatile read, CAS, final fields, the concurrent structure's own guarantee.) For every published node: were its fields written before the publishing store?
10. **Audit the machine.** Where do two hot, independently-written variables share a cache line? Pad them. Where does a retry loop recompute expensive work? Bound it or fall back to a lock.
11. **Audit multi-lock paths.** Any operation holding two stripes → global acquisition order, plus the Coffman recital. (Unchanged from the guarded-state family.)
12. **Say the JDK answer.** `ConcurrentHashMap`, `LongAdder`, `CopyOnWriteArrayList`, `ConcurrentLinkedQueue`, `ArrayBlockingQueue`, `Caffeine`. Hand-rolling what the JDK ships is a design smell unless implementation *is* the question — and even then, name what you're reimplementing.
13. **Verify by narration.** One happy path, one contention path, one interleaving that the coarse version allowed and yours must still forbid. Point at the linearization point of every operation.

## 10. Failure-mode catalog

| # | Failure mode | Signature | Cure |
|---|---|---|---|
| 1 | Compound op across shards | `containsKey`+`put`, `get`+`put` on a CHM | `computeIfAbsent` / `merge` / `compute` (§3) |
| 2 | Aggregate treated as truth | branching on `size()`, `isEmpty()` of a striped/lock-free structure | it's an estimate; use an atomic compound op for decisions |
| 3 | Two atomics, one invariant | `AtomicInteger size` + `AtomicReference head` updated separately | one CAS over an immutable snapshot holding both (§8 C4) |
| 4 | ABA | CAS on a reused node or a small-domain value succeeds; structure corrupts | version stamp; fresh allocation; whole-snapshot CAS (§4) |
| 5 | Use-after-free in lock-free traversal | (non-GC) reader dereferences a node another thread freed | hazard pointers / epochs; on the JVM, GC covers it — say why |
| 6 | Reads that mutate, assumed free | LRU `get` promised as lock-free but bumps recency | make the mutation lossy/deferred, or admit the lock (§2d) |
| 7 | Global clause sharded anyway | per-shard LRU claimed as "an LRU" without saying it's approximate | name the relaxation explicitly (§7) |
| 8 | False sharing | padded-nothing counters/sequences; throughput collapses with thread count | pad to cache lines / `@Contended` (§6.3) |
| 9 | Lock-free as premature optimization | CAS loop with no measurement, high contention, long body | uncontended locks are cheap; measure, then choose (§4) |
| 10 | Unbounded retry / livelock | contended CAS loop makes no net progress, burns CPU | backoff; or fall back to a lock under detected contention |
| 11 | Publication without an edge | node linked in before its fields are written; non-volatile `next` | write fields first, publish via CAS/volatile; final fields where possible (§6) |
| 12 | COW under write load | copy-on-write list with per-request writes | wrong structure — CHM/`newKeySet`, or a different read strategy (§5) |
| 13 | Snapshot staleness treated as consistency | read a COW snapshot, decide, act on the live structure | that's check-then-act at version level; move the decision inside an atomic op |
| 14 | Iterator contract confusion | expecting a snapshot from a weakly-consistent iterator (or vice versa) | know which contract you're offering; document it |
| 15 | Resize ignored | fine-grained map design with no answer for "what happens during rehash?" | forwarding markers + helping migration; at least know the shape (§3) |
| 16 | Over-engineering | striped/lock-free structure produced unprompted | start coarse, escalate on stated evidence — unchanged from guarded state |

---

## Validation against all problems

The recipe (§9) applied to each of the six, checking it produces the known-good shape.

### 10.1 thread-safe-lru-cache
Step 1: read-dominated, but *every* read mutates recency. Step 2 clauses: (i) key→node mapping — **local per key**; (ii) node payload — local; (iii) *the recency ordering* — **global**, one list, one head, one tail; (iv) size ≤ capacity — global. Step 4 **fires immediately and is the whole problem**: `get` is a write. Step 5 splits cleanly into local (i, ii) and global (iii, iv). Step 6 gives a concurrent map for the lookup. Step 7 is the fork in the road: accept one lock on the list (correct, and honestly fine at moderate throughput — say so) *or* relax exactness — lossy per-thread recency buffers drained opportunistically under `tryLock`, which is what production caches do. Step 8 catches the classic `get`-then-`put` compound race on the eviction path. **Verdict: the recipe forces the candidate to discover that the doubly-linked list is the serialization point and that the only escapes are "accept it" or "approximate it" — which is exactly the graded insight.** ✓

### 10.2 lock-striping-and-concurrent-hashmap
Step 2 decomposes into one clause per bin, sharing nothing — step 5 marks all of them **local**, with only resize and count global. Step 6 picks striping with the hash as the shard function (derived, not memorized). Step 7 handles the two globals: count → estimate (per-cell counters summed), resize → forwarding markers plus helping. Step 8 is the headline lesson (`putIfAbsent`/`compute*`/`merge`), step 9 explains why reads need no lock at all (volatile node fields + the CAS/volatile edges), step 14 in the catalog covers weakly-consistent iterators. **Verdict: the entire design of CHM falls out of steps 2/5/6 with nothing memorized; the interview-critical parts (compound ops, size(), iterators) are steps 7–8.** ✓

### 10.3 lock-free-stack-treiber
Step 2: one clause, and it fits in one word — `top` plus a chain of immutable-enough nodes. Step 5: entirely local to `top`. Step 6 picks **CAS** because the invariant is single-word — this is the rare case where lock-free is genuinely the natural answer, not an escalation. Step 9 audits publication (fields before the CAS; the CAS carries the edge). §4's ABA discussion is forced by the "is my CAS checking value or history?" question in step 6, and step 9's reclamation audit produces the GC-vs-hazard-pointers contrast. Step 3 remains the honest counterweight: a `synchronized` stack is shorter, easier, and faster under high contention. **Verdict: recipe produces the algorithm, the ABA analysis, and the "when does this actually win" honesty in one pass.** ✓

### 10.4 lock-free-or-bounded-queue
Step 2: two ends. Unlike the stack, head and tail are *separate* state → step 5 marks them as two local clauses, which is the entire reason a queue can let a producer and a consumer run without contending. That single observation generates both branches: the linked (Michael–Scott) design, where the two-step "link the node, then swing the tail" forces the *helping* protocol; and the ring buffer, where two sequence counters with one writer each need no CAS whatsoever in the single-producer/single-consumer case. Step 10 (audit the machine) **fires hardest here** — head and tail on one cache line destroys the benefit you just designed for, which is the whole reason padding/`@Contended` belongs to this problem. Step 7 covers the bounded/unbounded contract (backpressure vs OOM — the bounded-resource family's policy axis, reused). **Verdict: recipe explains why queues split where stacks can't, and step 10 supplies the false-sharing content that makes this problem distinct from 10.3.** ✓

### 10.5 striped-counter-longadder
Step 2: one clause, one word, and step 6 says CAS — which is `AtomicLong`, and which is **correct** until contention makes the cache line the bottleneck. Step 10 diagnoses it: every increment invalidates the line on every other core, so throughput *falls* as threads rise. Then step 5 gets re-run with the key insight: the sum's clause is global, but *the addend's* clause is per-thread and local — so shard the addend. Step 7 relaxes the read: `sum()` is not atomic against concurrent increments, and for a metric that is fine. **Verdict: the recipe reproduces `LongAdder` as "sharding applied to a counter," and — importantly — reproduces the condition under which you should NOT use it (uncontended, or you need an exact atomic read-modify-write like a limit check).** ✓

### 10.6 copy-on-write-snapshot-registry
Step 1: writes are rare, reads are constant, n is small. Step 4: reads do not mutate. Step 5: the clause ("the set of listeners is exactly what the last successful write produced") is global — you cannot shard a list nobody keys into. Step 6 therefore picks the third bargain, **immutability**, and step 9 shows why readers need no lock: a volatile read plus final fields gives the whole happens-before path. Step 7 names what the caller loses (snapshot staleness) and catalog #12/#13 name when it becomes a trap (write-heavy usage; decisions made on a stale snapshot). **Verdict: the recipe lands on the one bargain the other five problems never take, which is why this problem belongs in the set.** ✓

**Recipe adjustment made during validation:** the first draft's step 5 asked only "is the clause local or global." That is sufficient for 10.2–10.6 but under-serves 10.1, where the mutating read is the crux and would otherwise surface only late, as a pitfall. Step 4 ("does any read mutate?") was promoted ahead of the local/global split for exactly that reason. Step 10 (audit the machine) was likewise promoted from a footnote after 10.4 and 10.5 both turned out to be *dominated* by cache-line effects rather than by lock semantics.

---

## What the general framework leaves out

The five-step ceremony (clarify → classify → invariant → pattern → verify) and the guarded-state ladder get you to "one coarse lock, correctly." Four things this family needs that they do not supply:

1. **No step asks whether the invariant *decomposes*.** The framework asks you to state the invariant; it never asks you to write it clause-by-clause and test each pair for independence. Yet that decomposition is the entire design act here — it is what tells you a hash map shards and an LRU list does not. Recipe steps 2 and 5 supply it; nothing upstream does.

2. **No vocabulary for negotiating the contract.** The framework treats the specification as given. This family's best answers *change the specification*: approximate LRU, estimated size, weakly-consistent iteration, per-producer ordering. "How exact does this have to be?" is a design question the ceremony has no slot for, and it is frequently the thing being graded (§7).

3. **The machine is invisible to it.** Cache lines, false sharing, allocation pressure, retry waste, and the cost curve of contended CAS versus an uncontended mutex are not correctness properties, so no correctness-oriented framework mentions them — yet they decide every choice in this family. `LongAdder` exists for a reason the JMM cannot express. Recipe step 10 is the patch.

4. **Progress guarantees are a missing axis.** The framework's liveness vocabulary is deadlock/livelock/starvation, which is about *your* code's protocol. It has no way to say "this structure keeps working if a thread is preempted mid-operation," which is the actual argument for lock-free and the actual argument against it (unbounded individual retries). §4's wait-free/lock-free/obstruction-free/blocking ladder is the missing scale.

What transfers intact and does most of the work: the check-then-act disease and its cure (a CAS is just a hardware-atomic check-then-act); the compound-operations-don't-compose lesson, which striping *amplifies* rather than repeals; the happens-before edge list, with CAS added as a volatile-read-and-write; the multi-lock ordering discipline, unchanged, for cross-shard operations; the escalation ladder, of which this entire family is a magnified rung 3 and rung 4; and the anti-over-engineering sentence, which matters here more than anywhere else — the most common failure in this family is building a lock-free structure for a workload that a `synchronized` block would have served at a tenth the risk.
