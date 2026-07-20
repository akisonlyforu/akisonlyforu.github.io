---
layout: post
title: Flash-Sale Inventory
date: 2026-07-19
description: >-
  This is the family's contention problem, and it is where the honest engineer separates from the pattern-matcher. The invariant is trivial, stock ≥ 0, and the naive answer…
categories: interview multithreading problems
---

Part of the [Distributed Concurrency](/interview/multithreading/patterns/distributed-concurrency/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Senior system-design rounds, AWS L6 (explicitly probes flash-sale inventory patterns), Uber L5+, Coinbase, Airbnb, Stripe. **Very High frequency.**

### Problem

A limited quantity of an item, 1,000 units, goes on sale at a fixed moment. Hundreds of thousands of requests arrive within a few seconds, from many application instances. Design the inventory decrement so that:

- the item is **never oversold** (no 1,001st sale),
- the system does not collapse under the contention,
- and buyers get a timely answer.

Extend it to a realistic purchase flow where the buyer must complete payment after claiming a unit, and payment takes seconds and can fail.

### Constraints

- Many application instances, no shared memory.
- Extreme, short-lived contention on a single logical counter.
- Overselling is unacceptable; underselling (a few units unsold) is undesirable but survivable.
- The payment step is slow and external, nothing may be held across it.

### Clarify before solving

- **Is overselling absolutely forbidden, or compensable?** (Concert seats: forbidden. Some retail: oversell slightly and refund. This changes everything, and asking is a senior move.)
- **Is underselling acceptable?** (It usually is, and that permission buys you sharding.)
- **Must the answer be synchronous?** ("You got it" vs "you're in line" is a product decision with enormous engineering consequences. Raise it.)
- **How long may a claimed unit be held unpaid?** (This sets the reservation TTL.)
- **Is the count exact-on-read, or is an approximate "almost gone" fine?** (Display and truth can differ; conflating them costs you throughput for nothing.)
- **One item or many?** (Contention is per item; a thousand items with even traffic is not this problem.)
- **Single JVM first?** (Yes: a `Semaphore(1000)` or an `AtomicInteger` with a decrement-if-positive loop. Say this in one sentence before naming a datastore.)

### Why this problem matters

This is the family's contention problem, and it is where the honest engineer separates from the pattern-matcher. The invariant is trivial, `stock ≥ 0`, and the naive answer is a two-round-trip read-then-decrement, which is `if (stock > 0) stock--` with a network in the gap: the very first bug in the refresher, in the highest-traffic setting it can possibly appear in.

Fixing correctness is one line (an atomic conditional decrement). The interview is about what comes *after* correctness: a single hot row serializes every buyer, and there is no design that gives you an exact hot counter at unbounded throughput. Every real technique, sharding, reservation with TTL, queue-based admission, buys throughput by spending **exactness of the count, freshness of the read, or synchrony of the answer**. A senior answer names which of the three it is spending and why, rather than presenting a technique as free.

---

## Strategy

### Classify

Guarded state with a bounded counter, a semaphore, essentially, relocated across the process boundary, and then stressed until the relocation's cost dominates the design.

**Single-JVM answer first, cold, in one sentence:** a `Semaphore(1000)` whose `tryAcquire()` returns the boolean deciding whether this buyer gets a unit; or equivalently an `AtomicInteger` with a CAS loop that decrements only while positive. The linearization point is the permit acquisition. Say this *before* mentioning any datastore, it takes five seconds and it establishes that you know what you are relocating. Now cross the boundary: the permits must live where every instance can see them, and the acquisition must remain one atomic step.

### Invariant

`sold ≤ capacity` at all times, equivalently `remaining ≥ 0`, and the claim that succeeds is the one that decrements. The second half matters: a design where a buyer is told "yes" by one component and the decrement happens in another has two linearization points and will oversell.

### Mental model

The naive implementation is two round trips: read the stock, and if it's positive, write back one less. Between those two trips, an unbounded number of other instances do the same thing. With a thousand units and a hundred thousand concurrent buyers, essentially every one of them reads a positive value, and you sell a great deal more than you have. This is check-then-act, and the fact that the gap is a network round trip rather than a thread switch only makes it wider.

**The correct primitive is a single atomic conditional decrement**: one statement at the store that decrements the counter *only where it is currently at least one*, returning whether it applied. The condition and the mutation are the same operation, so there is no gap. The rows-affected count (or the returned new value) is the boolean the semaphore would have returned. One round trip, no read-then-write, no oversell.

Note what this does and does not fix. It makes each critical section as short as physically possible, but every buyer still serializes on that one row, so throughput is capped at roughly one decrement per row-lock round trip. Correctness is solved; **contention is not**, and the rest of the design is about contention.

### Design reasoning

### Sharding the counter

Split 1,000 units into, say, 20 buckets of 50. Each request hashes to a bucket (by user ID, or at random) and performs the atomic conditional decrement on **that bucket only**, contending with roughly a twentieth of the traffic. Throughput scales with bucket count. This is `LongAdder`'s striping, one layer up, and it has `LongAdder`'s exact trade: **cheap writes, expensive reads.**

State the costs plainly, because they are the interesting part:

- **Reads become a fan-out.** "How many remain?" is now a sum over all buckets, which is more expensive and is a snapshot that was never simultaneously true. Usually fine, display counts are advisory, but say so rather than glossing.
- **Buckets empty unevenly.** A buyer can be told "sold out" while stock remains in another bucket. Fixes: probe a few other buckets before giving up (bounded, or you rebuild the contention you were escaping), or periodically rebalance. Either way, the tail of the sale gets messier as buckets drain.
- **Undersell risk at the boundary.** If you don't probe, some units go unsold. This is why "is underselling acceptable?" was a clarifying question, the answer is what licenses sharding at all.

The one-liner: **sharding trades exactness and read cost for write throughput.**

### Reserve-then-confirm

A real purchase is not one step. The buyer claims a unit, then pays, and payment takes seconds and can fail. You cannot hold a lock, or a transaction, across the payment call; doing so ties up a row lock for the duration of a third party's tail latency, and contention will convert their slow day into your outage.

So split it:

1. **Reserve**: a short transaction that atomically decrements available stock and writes a reservation row with an expiry. Fast, no external calls.
2. **Pay**: the external call, with nothing held.
3. **Confirm**: a short transaction that finalizes the reservation into a sale.

And then the piece candidates forget: **something must reclaim abandoned reservations.** A buyer who closes the tab has taken a unit out of circulation with nobody watching. The reclaimer returns expired reservations to stock, and it **races the late confirmation**: payment succeeded at the exact moment the reservation expired and got reclaimed. Both parties are acting reasonably; the resolution must be an atomic one. Make the confirmation a **conditional write**, finalize only if the reservation is still in the reserved state, and make the reclaimer conditional in the same way. Exactly one wins; the loser handles it (the buyer whose late confirm lost gets a refund and an apology, or a retry against remaining stock). Naming this race unprompted is a strong signal, because it is the same expiry-versus-liveness problem as the distributed lock, and it is where real systems actually break.

Two refinements worth a sentence each: prefer to **reclaim lazily** where you can, when a request finds an expired reservation, reclaim it right there, rather than running a sweeper (the family-7 lazy-derivation instinct, and it avoids owning a background job). And size the TTL to the *realistic* checkout duration: too short cancels legitimate buyers mid-payment, too long strands inventory during the seconds when it matters most.

### Queue-based admission

Put every purchase attempt into a durable queue and have a small number of consumers apply them serially. The invariant becomes trivially safe, one writer per item means no contention at all, throughput becomes predictable and bounded rather than a function of how hard the internet hits you, and the system **degrades into latency instead of errors**, which is a much better failure mode.

The cost is real and is a product decision as much as an engineering one: the answer is now **asynchronous**. The buyer gets "you're in line, position 8,412," not "you got it." For a genuine flash sale this is often *better*, it is honest, it is what the physical queue metaphor implies, and it removes the incentive to hammer refresh, but it changes the UX contract and must be raised with the product owner, not assumed.

This is family 6's worker-pool answer with a durable queue in place of a `BlockingQueue`, and the same benefit: contention is replaced by serialization, and serialization at a known rate is a capacity problem rather than a correctness one.

### Shedding load before it reaches the counter

The last technique, and the one production actually leans on hardest: most of those hundred thousand requests **cannot possibly succeed**. Once 1,000 units are gone, every further request is guaranteed to fail, and letting it reach the database to find that out is pure waste. So reject early, a cached "sold out" flag at the edge, per-user rate limiting to blunt scripted hammering, and admission control that lets through only a bounded multiple of remaining stock. This costs nothing in correctness (the atomic decrement is still the arbiter; the edge is only a filter that may be stale in the *safe* direction) and removes most of the load. Candidates who go straight to sharding without mentioning that the cheapest request is the one you never serve are optimizing the wrong end.

### Trade-offs

- **Single row vs sharded**: exact, simple, and serialized versus fast, approximate, and uneven at the tail. Shard only if you have permission to undersell slightly or you're willing to pay for probing.
- **Synchronous vs queued**: an immediate yes/no with contention-bound throughput versus predictable throughput with an asynchronous answer. This is the exactness/freshness/**synchrony** axis, and queuing is what spending synchrony buys.
- **Reservation TTL**: short frees stranded inventory quickly and cancels slow legitimate buyers; long is buyer-friendly and strands units during the only minutes that matter.
- **Oversell-and-compensate**: if the business can refund, you may relax the invariant deliberately, take the throughput, and handle the rare overshoot commercially. This is a legitimate answer in some domains and an unacceptable one in others, which is why the clarifying question comes first.
- **Cache-the-count vs read-through**: a cached remaining-count is stale and cheap; the sold-out flag is the one piece of staleness that is safe in the useful direction, since a stale "in stock" merely causes a wasted attempt that the atomic decrement then rejects correctly.

### Pitfalls

1. **Read-then-decrement across two round trips**: mass oversell. The condition and the decrement must be one operation.
2. **Deciding in the application and decrementing in the store**: two linearization points; the check is worthless.
3. **Holding a lock or a transaction across payment**: a third party's latency becomes your contention, and a slow provider becomes an outage.
4. **Reservations with no reclaimer**: abandoned carts permanently consume stock and the item shows sold out while unsold.
5. **Reclaimer racing a late confirmation, resolved non-atomically**: a unit sold twice, or a paid buyer with nothing. Both sides must be conditional writes.
6. **Sharding without a probe or rebalance**: "sold out" declared with inventory sitting in other buckets.
7. **Summing shards on every request**: you've reintroduced the hot read you sharded to avoid.
8. **Unbounded optimistic retries on the hot row**: under this contention, retries are a load amplifier and a livelock. Hot keys want serialization, not optimism.
9. **No load shedding**: letting a hundred thousand guaranteed-to-fail requests reach the database.
10. **No idempotency on the purchase request**: an impatient buyer's double-click or a client retry buys two units. Every technique here still needs an idempotency key on the purchase intent, and forgetting it is common because attention is all on the counter.

### Check your understanding

1. Give the single-JVM answer in one sentence and name the linearization point.
2. Narrate the oversell interleaving under read-then-decrement, and say why the atomic conditional decrement removes it.
3. Does the atomic decrement solve contention? What does it actually bound throughput at?
4. State the sharding trade in one sentence. Name two failure modes it introduces and a fix for each.
5. Why can't you hold the reservation lock across payment? What is the standard restructuring?
6. Construct the reclaimer-versus-late-confirm race and give the atomic resolution.
7. What exactly does queue-based admission spend to get its throughput, and who has to approve that?
8. Name the three currencies every technique here spends, and say which one each technique spends.
9. Why is a stale "sold out" flag at the edge safe, and what is it safe *because of*?
10. Where does idempotency enter this problem, and why is it easy to forget here?

### Transfers to

Double-booking prevention (the same invariant with capacity 1 and a named resource); ticketing and seat holds; rate limiting under distribution (same hot-counter contention, different invariant); `LongAdder` and striped counters in a single JVM; and pessimistic locking (the short transaction around reserve and confirm is exactly that problem's shape).

Full Java solution: [on GitHub](https://github.com/akisonlyforu/Multi-Threading-Problems/tree/main/src/distributed-concurrency/flash-sale-inventory).
