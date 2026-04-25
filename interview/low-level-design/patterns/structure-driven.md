---
layout: post
title: Structure-Driven Problems Playbook
date: 2026-07-12
description: When the data structure is the design, 15 recurring structures, how OO still shows up when there's no variation package, and five concurrency disciplines for dense shared state.
categories: interview lld patterns
---

Deep dive on structure-driven problems, companion to [What do you actually do in a LLD Interview?](/interview/low-level-design/lld-framework/). This is the category where roughly a third of the question bank lives: caches and data stores, parsers, time-and-geo indexes, order books, and concurrency drills. No archetype recipe applies; there is no strategy folder to reach for. You win by naming the right structure, stating its invariants, and building it correctly.

## 1. Recognizing structure-driven problems

The tell is in the requirements, not the domain. When the problem statement contains **complexity bounds**, "O(1) get and put", "O(log n) insert and cancel", "top-k efficiently", "query any time range fast", or a **grammar** ("parse this JSON subset", "validate this cron expression"), the interviewer is not grading your class diagram. They are grading whether you can pick the one structure that meets the bound and keep its invariants intact under mutation. A second tell: the entity list is embarrassingly short. An LRU cache has a Node and a list; an order book has a PriceLevel; a median store has two heaps. If your entity pass produces two thin classes and you feel the urge to invent more, stop, you're in this category.

What wins here is correct structure, articulated invariants, and working code. Not patterns. A `Strategy` interface wrapped around a broken doubly-linked list scores zero. A hand-rolled map+DLL whose map and list provably always agree scores full marks. Invariants do double duty exactly as always: they're your validation checks, and later, your lock boundaries ("map and list always agree" is precisely the thing a lock must protect).

The senior move is to declare the category out loud early: **"The design here is the structure itself; I'm not adding pattern scaffolding. The variation, if it comes, will be a swap of the structure behind a stable API, so I'll keep the API clean and the structure private."** That sentence is the same signal as correctly placing a Strategy elsewhere. You identified where the variation lives (nowhere in code) and declined the folder.

One nuance: some problems live next to structure-driven ones without being structure-driven themselves. A pluggable-eviction cache genuinely has an eviction Strategy axis; a rate limiter has an algorithm-family axis (token bucket vs sliding window). The test: if the interviewer's likely follow-up is "swap the algorithm," you want the interface; if it's "now make it O(1)" or "now make it thread-safe," you're structure-driven.

## 2. The recurring structure inventory

Fifteen structures cover the entire category. Learn each as: the structure, the one-sentence move you narrate, and the problems it wins.

| # | Structure | The move | Canonical problems |
|---|---|---|---|
| 1 | **HashMap + hand-rolled doubly-linked list** | Map gives O(1) lookup; DLL gives O(1) reorder/evict; invariant: map and list always agree | LRU cache, recently-viewed listings, clipboard history, browser history |
| 2 | **TreeMap price/time ladder + handle map** | Sorted keys give O(log n) best/floor/ceiling; a second Map<id, node> gives O(1) cancel into intrusive per-level lists | Limit order book, stock matching, consistent-hash ring, calendar floor-lookups, sorted file listings |
| 3 | **Two heaps (max-heap low / min-heap high)** | Rebalance so sizes differ ≤1 and every low ≤ every high; median read off the tops | Median store; sliding-window median (add delayed deletion) |
| 4 | **Heap + lock/condition (DelayQueue shape)** | PriorityQueue by readyAt; taker `awaitNanos(min remaining)`; offer signals if new head, no oversleep | Delayed task queue, TTL cache sweeper, job scheduler, retry-with-backoff |
| 5 | **Ring buffer of per-interval counters** | Fixed array of time buckets, index = (ts/granularity) mod size; O(1) ingest, O(window) query; stale buckets lazily reset | Heatmaps, trending counters, monitoring windows, sliding-window rate counters |
| 6 | **Trie** | Walk to prefix node, collect beneath; per-node counts or cached top-k lists turn O(subtree) into O(prefix + k) | Dictionary, autocomplete, typeahead suggestion, word games |
| 7 | **Inverted index inside sealed time segments** | term → posting list per bucket; AND = intersection, OR = merge; active segment takes writes, sealed segments are immutable | Search engine, log search |
| 8 | **DAG + topological recompute (in-degree/DFS)** | Edges = dependencies; cycle check at edge-insert (reject with path); change propagates in topo order from dirty node | Spreadsheet, workflow engine, task planners |
| 9 | **Adjacency graph + BFS/Dijkstra** | Graph immutable snapshot; traversal parametrized by edge filter + weight function so the router never branches on mode | Maps navigation, connection suggestions, delivery routing |
| 10 | **Bit array + hash family** | k probes via double hashing (h1 + i·h2); bits set-only, so no false negatives, the monotonicity is the invariant | Bloom filter, seen-URL filters |
| 11 | **Overlay stacks (layered maps)** | Read walks layers top-down (null marker = delete); write goes to top layer; commit merges into parent, rollback pops | KV store with transactions, layered config/feature flags |
| 12 | **Recursive descent / explicit state machine** | Grammar functions mirror productions OR enumerate states out loud; errors carry position; never partial output | JSON parser, CSV parser, markdown parser, cron parser, regex compile phase |
| 13 | **Geo grid buckets (cell → entity set)** | Hash lat/lng to a cell; radius query = candidate cells (including neighbors, the boundary bug) then exact-distance filter; moves are per-entity atomic bucket swaps | Nearby search, geo lookups under heatmaps; grid vs geohash vs quadtree is the discussion |
| 14 | **Append-only version chains + binary search** | Per-key sorted (ts, value) list, append keeps it sorted for free; read = floor via binary search; snapshots = version watermarks | Time-based KV, versioned KV, snapshot store / MVCC |
| 15 | **DP over sequences (LCS / edit distance)** | O(nm) table, reconstruct the path; name Myers (diff) or BK-tree/SymSpell (spelling) as the production answer without building it | Diff tool, regex matcher, spell-check suggestion distance |

Composites exist: an LSM store is rows 14+10+7 stacked (memtable + blooms + sealed tables); a distributed cache is rows 2+1 (ring to pick node, LRU inside it); a hand-rolled concurrent hash map is row 1's map with lock striping as the entire point.

## 3. How OO still shows up

"No variation package" does not mean "no design." The structure gets wrapped, and the wrapping is where the framework's steps still run:

- **Clean public API on one class.** `MedianStore.save(n)/getMedian()`, `OrderBook.insert/cancel/bestBid`, `Parser.parse(String) → JsonObject`. The API returns domain objects and throws custom exceptions (`ParseException` with position; `OrderNotFoundException`).
- **Invariants live in one place.** Every mutation path goes through the class that owns the structure, so "size ≤ capacity" or "level aggregate = Σ resting orders" is checkable at one choke point. This is the structure-driven version of "models get behavior."
- **Entities stay thin, invariants do the work.** Nodes, PriceLevels, TrieNodes, don't apologize for that. Spend the time writing the invariant comment block instead: for an order book, "aggregate qty per level = sum of resting orders; modify-down keeps queue priority, modify-up re-queues." That block is worth more than three extra classes.
- **Patterns appear only where they're natural, and you name the rarity.** Composite for a file-system tree or a markdown AST (textbook cases, say so); Iterator for streaming rows (justify iterator over list: memory); Visitor for renderers over an AST ("a legitimate Visitor use, note how rare that is"). These earn points precisely because you didn't force them elsewhere.
- **Hide the structure so a later swap is possible.** An order book exposes `bestBid()`, not its TreeMap, so swapping in an array-indexed ladder is an internals change. A geo service exposes `findNearby()`, not its grid, so quadtree is a swap. This is the extensibility pitch for this category: "to change the structure, nothing outside this class moves." Same Open/Closed sentence, no interface needed until a second implementation actually exists.

## 4. Structure + concurrency

Concurrency lands hardest in this category, because the shared state is dense and hot. Five recurring disciplines, each with its narration:

**1. Single-writer discipline.** An order book and an LSM write-ahead log are the exemplars. One writer thread owns the structure, and the append/match order is the truth. Say: "matching engines are single-writer per book in real exchanges. If you force multi-writer on me, I'll talk about level-lock granularity and why it's painful." Honesty about what production systems actually do scores above heroic fine-grained locking.

**2. Seal-and-freeze (name the idea).** For read-hot, append-mostly systems, logs, search, LSM, the recurring move is: one active segment takes writes (single-writer or one lock), and once a segment seals it becomes **immutable, so queries over it need no locks at all**. Its cousin is the **volatile snapshot swap**: build an immutable ring/frame/graph, publish by swapping one volatile reference. Think a consistent-hash ring (rare membership changes, hot lookups), heatmap frames (double-buffered, no torn reads), a navigation graph. One sentence covers all of them: "readers are lock-free because they only ever see immutable, atomically-published state."

**3. CAS loops on packed state.** When the whole state fits in one immutable object or one word, `AtomicReference` + compareAndSet retry loop beats any lock. A token bucket packs tokens + lastRefill in a BucketState. Bloom filter bits use AtomicLongArray CAS-or, and that's legal only because bits are set-only and monotonic, say so out loud. Placement judgment matters too: CAS wins on a single hot shared bucket, but per-client buckets rarely contend, so a lock is fine there.

**4. "Make the LRU thread-safe" is a trap, walk in with the trade-off named.** The naive answers both lose. A coarse lock is correct but serializes everything. Naive striping corrupts the DLL because a get on stripe A reorders a list shared with stripe B. The senior answer is a fork you present: **V1 exact**, one ReentrantLock around map+list, correct, honest, low throughput. **V2 approximate**, a concurrent map + per-entry access timestamps, evict the oldest of K random samples (name Redis's actual design) or buffered reads applied in batches (name Caffeine's). The sentence that wins: "I keep the size bound exact and relax recency order to approximate, I can state exactly which invariant I traded and what I bought." Being able to say which invariant you relaxed is the entire point.

**5. Multi-key moves stay per-entity-atomic.** Geo index updates (remove from old cell + add to new) ride a per-entity `compute()`; money-transfer-style multi-entity invariants take sorted-order per-entity locks.

**Practice path.** Six concurrency drills teach every discipline above in isolation: a bounded producer-consumer queue teaches the while-guard and two Conditions; a delayed queue teaches the awaitNanos re-check loop that underlies every TTL/scheduler; a token bucket teaches the CAS shape; a concurrent LRU teaches trade-off narration; a money-transfer drill teaches lock ordering; a multithreaded web crawler teaches the pending-counter termination pattern that recurs in any parallel fan-out. Do those before anything else in this category, every concurrency paragraph above is one of them re-skinned.

## 5. Skeletons (declarations only)

The two shapes you will hand-write most often. Rehearse until the declarations pour out.

```java
// Shape 1: HashMap + intrusive doubly-linked list (LRU family)
class LruCache<K, V> {
    private static final class Node<K, V> {
        final K key; V value;
        Node<K, V> prev, next;              // intrusive links, no wrapper list
        Node(K key, V value);
    }
    private final int capacity;
    private final Map<K, Node<K, V>> index;          // O(1) lookup
    private final Node<K, V> head, tail;             // sentinels; head.next = MRU, tail.prev = LRU

    public Optional<V> get(K key);                   // hit → unlink + relink at head
    public void put(K key, V value);                 // present → update+promote; full → evict tail.prev
    public int size();
    // Invariants: index.size() == list length; size ≤ capacity; every index entry is linked.
    private void unlink(Node<K, V> n);
    private void linkAtHead(Node<K, V> n);
    private Node<K, V> evictLru();
}

// Shape 2: TreeMap ladder + handle map (order book / anything sorted with O(1) cancel)
class BookSide {
    private static final class OrderNode {           // intrusive FIFO node inside a level
        final String orderId; long qty;
        OrderNode prev, next;
        PriceLevel level;                            // back-pointer: O(1) unlink + aggregate fix
    }
    private static final class PriceLevel {
        final long price;
        long aggregateQty;                           // invariant: == Σ node qty in this level
        OrderNode head, tail;                        // FIFO = time priority
    }
    private final TreeMap<Long, PriceLevel> ladder;  // bids: reverseOrder(); asks: natural
    private final Map<String, OrderNode> handles;    // orderId → node: O(1) cancel/modify

    public void insert(String orderId, long price, long qty);   // O(log n) new level, O(1) append
    public void cancel(String orderId);                          // O(1) via handle; drop empty level
    public void modify(String orderId, long newQty);             // down: in place; up: re-queue (say the rule)
    public Optional<Long> bestPrice();                           // ladder.firstKey
    public long volumeAt(long price);
}
```

A consistent-hash ring is Shape 2 minus the handles: `TreeMap<Long, VNode>` + `ceilingEntry(hash(key))` with wraparound to `firstEntry()`, the implementation is one line; the interview is the why.

## 6. Anti-signals

- **Pattern scaffolding around a data structure.** A factory, a service interface, and a repository wrapped around a 40-line LRU tells the interviewer you have one hammer. Interfaces go where a second implementation is credible today, nowhere else.
- **Library magic you can't open up.** `LinkedHashMap(cap, 0.75f, true)` with `removeEldestEntry` solves LRU in five lines, and invites "so how does it work inside?" Use it only if you can explain access-order relinking. Otherwise hand-roll it and say you're hand-rolling because the internals are the question. Same for `PriorityQueue` (know sift-up/down costs), `DelayQueue` (know the leader-follower trick), `TreeMap` (red-black, O(log n), floor/ceiling).
- **Ignoring stated complexity bounds.** Scanning a list for the LRU victim, recomputing the median by sorting, linear-scanning orders to cancel, each violates the bound the problem stated in sentence one. Restate the bounds up front and let them choose the structure.
- **Skipping invariants because "it's just a data structure".** "Map and list always agree" unstated becomes the bug where you evict from the list but not the map. The invariant block is cheaper than the debugging.
- **Bluffing on the hard variants.** Lock-free medians, catastrophic-backtracking-proof regex with captures, exact concurrent LRU with striping, the honest "that's research territory / a different engine shape; here's what production systems do instead" beats a confident wrong sketch every time.
