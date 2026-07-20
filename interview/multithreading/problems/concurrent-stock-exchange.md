---
layout: post
title: Concurrent Stock Exchange (Matching Engine)
date: 2026-07-19
description: >-
  The whole match is a multi-step read-modify-write across two priority structures that must be one linearization point, and the invariant it protects, no crossed book, refuses to…
categories: interview multithreading problems
---

Part of the [Concurrent Data Structures](/interview/multithreading/patterns/concurrent-data-structures/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [enginebogie, multi-threaded concurrent stock exchange](https://enginebogie.com/public/question/multi-threaded-concurrent-stock-exchange-application-thread-safe/1235). A staff-level design round that starts "make it thread-safe" and turns into "now make it fast without lying about correctness."

### Problem

Many client threads submit limit orders (buy or sell, a price and a quantity, for a symbol) against a shared order book. The book keeps **bids sorted descending** and **asks sorted ascending**, so the best bid and best ask sit at the front of each side. When the book *crosses*, best bid price ≥ best ask price, the engine matches: it produces a trade at the resting order's price, decrements both parties' remaining quantities, removes whichever order is fully filled, and repeats until the book no longer crosses. It must preserve **price-time priority**, orders at a better price match first, and within one price level the oldest resting order matches first (FIFO). No order may be lost, duplicated, double-filled, or left in a corrupt partial state, and every participant's filled quantity and balance must stay consistent.

### Constraints

- **Many symbols, many concurrent clients.** AAPL and TSLA share nothing; two orders on the *same* symbol contend for the same book.
- **Partial fills.** A large order sweeps several resting orders and may rest with its remainder; a resting order may be filled by several incoming orders over time.
- **Cancels race matches.** A cancel for an order that is, at that instant, being matched must resolve to exactly one outcome, cancelled *or* filled, never both, never half.
- **The book must never be observably crossed.** Any reader (market-data snapshot, another matcher) must never see best-bid ≥ best-ask as a resting state.
- Correct under arbitrary preemption: a client suspended mid-submit must not wedge the engine or leave a torn book.

### Clarify before solving

- **One symbol or a whole exchange?** Decides whether the unit of contention is the engine or the per-symbol book, this is the striping question and you should ask it first.
- **Limit orders only, or market/stop/IOC/FOK too?** Market and stop orders change the matching *rule*, not the concurrency structure; say you will design the concurrency for limit orders and the rest slots into the same critical section.
- **Is throughput or latency the product?** A correct single-lock engine may be enough; "single-writer per symbol on a lock-free intake queue" is the answer only when you have measured the lock as the ceiling. Ask before reaching for the disruptor.
- **What is the trade price, resting or incoming?** Standard is the resting (passive) order's price; it does not change the threading but you must state it so filled-quantity accounting is unambiguous.
- **Do we need a strict total order of trades, or per-symbol order?** Per-symbol is almost always the real requirement, and it is dramatically cheaper.

### Why this problem matters

It is the family's cleanest example of an invariant that **does not decompose**. A hash map shards because "each key lives in one bin" is a per-bin clause. A matching engine's core clause, "the book is never crossed", spans *both* priority structures and every order in them at once: you cannot make it true bin-by-bin because a match reads the top of the bid heap, reads the top of the ask heap, mutates both, and possibly loops, all of which must appear to happen at a single instant. Recognizing that the match is one indivisible read-modify-write across two structures, and therefore one linearization point, is the entire graded insight. Everything after that, "so a lock around the match, striped by symbol" and "so real exchanges use a single writer per symbol", is a consequence you derive rather than a fact you recite.

---

## Strategy

### Classify

Concurrent data structure whose invariant is **global to the book and does not shard**. Contrast the queue, whose two ends are independent state; here the two sides of the book are *coupled* precisely at the point where work happens. This is §7 of the playbook, a structure with a genuinely global clause, and the senior move is either accept a lock on it or renegotiate the granularity, not pretend to shard it.

### Invariant

- **No crossed book:** at every observable instant, best-bid price < best-ask price (or one side empty). This is the one that spans everything.
- Price-time priority: a resting order matches only after every strictly-better price and every earlier order at its own price is exhausted.
- Conservation: across a trade, total quantity is preserved, filler's decrement equals fillee's decrement equals the trade quantity; no order is filled beyond its remaining quantity; no order appears in the book after it reaches zero.
- Each submitted order is admitted exactly once and each cancel resolves to exactly one terminal outcome.

### Mental model

One clerk per symbol standing at a two-sided pegboard, bids pegged high-to-low on the left, asks low-to-high on the right. An incoming order is handed to the clerk, who compares the two top pegs, and while they cross, tears off matched pairs, writes a trade ticket, and re-pegs any remainder. Nobody else touches *that* pegboard while the clerk works, that is what makes "never crossed" observable only between whole handlings. Different symbols are different clerks at different boards who never interfere. The exchange's throughput is "how many boards" (symbols), never "how many hands per board".

### Design ([Template 1](/interview/multithreading/mt-framework/) is the honest baseline: understand it, don't recite it)

State per symbol: a max-structure for bids (`PriorityQueue`/tree keyed by price desc, then arrival sequence) and a min-structure for asks, plus an index from orderId to its node for cancels.

**Baseline, and say this first.** The whole match, the loop that peeks both tops, trades, decrements, evicts, and repeats, sits inside **one lock per symbol**. `match(order)` acquires `book[symbol].lock`, runs the crossing loop to completion, releases. The lock *is* the linearization point: the order takes effect atomically at the instant the critical section that admitted-and-matched it commits, and no reader observes a crossed intermediate. Correct, obvious, and the right first answer. Then **stripe by symbol**: a lock per book, or a lock array indexed by `spread(hash(symbol)) & (K-1)`, so AAPL and TSLA run fully in parallel while contention is confined to same-symbol flow. This is exactly lock striping with the symbol as the natural shard key; per-symbol serialization is not a limitation, it is what price-time priority *requires*, since FIFO within a symbol is a total order and a total order has a serialization point by definition.

**Cancel** takes the same per-symbol lock, looks up the node, and either removes it or observes it already gone/filled, one lock, so the cancel-vs-match race collapses into "whoever holds the lock first wins", exactly one outcome. Do not give cancels a separate lock; two locks over one invariant is the classic decomposition error.

**Why lock-free matching is the wrong reach, and say why.** The lock-free recipe needs the invariant to fit in one CAS. This one cannot: a match is a *variable-length* sequence of coupled mutations to two structures, so there is no single word whose swap linearizes it, and any partial publication exposes a crossed book, the very thing forbidden. You would be inventing multi-structure transactional memory. Name that and move on.

**What real exchanges actually do, the payoff.** Keep the per-symbol serialization, remove the lock from the hot path: a **single writer thread per symbol** (or per shard of symbols) owns the book with *no* lock at all, fed by a lock-free intake queue, the LMAX **disruptor** ring buffer. Producers (client threads) publish orders lock-free into the ring (the SPSC/MPSC publication from the bounded-queue problem); the single consumer drains them and mutates the book with zero synchronization because it is the only writer, so "never crossed" holds trivially, there is no second writer to cross it. You have traded a contended lock for a single-writer discipline and gotten mechanical sympathy (one core, hot cache, predictable branch behaviour) for free. This is the [single-writer-per-key](/interview/multithreading/patterns/concurrent-data-structures/) idea: serialize by *ownership*, not by mutual exclusion.

### Pitfalls

1. Separate locks for bids and asks (or for match and cancel). The invariant spans both; the match reads and writes both in one breath. One lock per book, always.
2. Publishing the book between two mutations of a single match, letting a reader or a second matcher see it crossed. The match is atomic or it is broken; there is no "briefly crossed".
3. Sharding *within* a symbol (e.g., a lock per price level) to "scale" a hot book. Price-time priority is a per-symbol total order; you cannot shard a total order without giving it up. If one symbol is the bottleneck, that is a business reality, not a lock you can split.
4. Reaching for lock-free matching. The invariant doesn't fit one CAS; you will either serialize accidentally or expose a crossed book.
5. Cancel and match resolving a race to *both* outcomes (order both cancelled and filled), or to a double-fill. Route both through the one linearization point.
6. Assuming a global trade order when per-symbol order is the actual requirement, buying yourself a single global bottleneck you never needed.
7. In the disruptor design, letting more than one thread write a symbol's book. The whole correctness argument is "one writer"; two writers is back to needing a lock, and you have kept none.

### Check your understanding

1. State the one invariant that refuses to shard, and prove why per-price-level locking cannot preserve it. Which read and which write are coupled?
2. Name the linearization point in (a) the single-lock design and (b) the single-writer-disruptor design. They are different mechanisms enforcing the same instant, describe each.
3. A cancel arrives while its order is mid-match. Walk the interleaving under one per-symbol lock and show exactly one terminal outcome results.
4. Why is per-symbol serialization *not* a throughput bug the way a global lock is? Answer in terms of what price-time priority demands.
5. You've measured the per-symbol lock as your ceiling on the hottest symbol. What does the disruptor change, and what does it pointedly *not* change (hint: the hottest symbol is still serial)?
6. Market and stop orders arrive. What in your concurrency design changes? (Nothing, and you should be able to say why in one sentence.)

### Transfers to

[lock-striping-and-concurrent-hashmap](/interview/multithreading/problems/lock-striping-and-concurrent-hashmap/) (the symbol is the shard key, exactly like the hash bin); [event-bus-with-per-key-ordering](/interview/multithreading/problems/event-bus-with-per-key-ordering/) (single-writer-per-key is the same serialize-by-ownership move, minus the mutual exclusion); guarded-state (one lock, one invariant, the whole match is the critical section); [lock-free-or-bounded-queue](/interview/multithreading/problems/lock-free-or-bounded-queue/) (the disruptor intake ring is that problem's SPSC/MPSC buffer put to work); double-booking-prevention and flash-sale-inventory (a fill is an atomic decrement-if-available across coupled state, the same "check-and-commit at one point" discipline, applied to inventory instead of an order book).

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/concurrent-data-structures/concurrent-stock-exchange).
