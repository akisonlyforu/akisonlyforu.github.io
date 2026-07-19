---
layout: post
title: Double-Booking Prevention
date: 2026-07-19
description: >-
  This is the explicit bridge from family 2 back into the distributed world, and it is written to force the ordering the whole family depends on. In one JVM this is a solved…
categories: interview multithreading problems
---

Part of the [Distributed Concurrency](/interview/multithreading/patterns/distributed-concurrency/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Senior system-design rounds, Airbnb, Uber L5+, Stripe, Coinbase, AWS L6. **Very High frequency**, and the most common concrete framing of the whole family.

### Problem

A single, named, indivisible resource must be claimed by **exactly one** customer: one seat for one showing, one driver for one ride request, one room for one date range, one parking spot for one slot. Many requests for the same resource arrive concurrently across many application instances.

Design the claim so that two customers can never both believe they hold it. Then extend it to a realistic flow where the customer must confirm, pay, accept, sign, after claiming, with a hold that expires if they don't.

**Answer the single-process version first**, in full, before any distributed machinery. The interviewer is checking that you can, and the ordering is part of the grade.

### Constraints

- Many application instances; requests for one resource can land anywhere.
- The confirmation step is slow (human or external) and may never complete.
- Double-booking is a hard failure, it is visible to two paying customers and cannot be silently compensated.
- Leaving a resource unclaimed because a customer abandoned the flow is unacceptable for long.

### Clarify before solving

- **Is the resource a single named unit, or a fungible one of many?** (Named unit: this problem, and uniqueness is the arbiter. One-of-many: the inventory-counter problem instead. Getting this classification right is the first thing said.)
- **Is there already a natural uniqueness rule in the domain?** (Usually yes, `(resource, time slot)`. If so, you need no separate idempotency key: the constraint that prevents doubles is the same one that states the business rule.)
- **How long is the hold, and what happens when it expires?** (Sets the reservation TTL and requires a reclaimer.)
- **Are the slots discrete or arbitrary intervals?** (Discrete slots: one uniqueness constraint solves it. Arbitrary overlapping intervals: the invariant spans *rows*, which is a strictly harder problem, see the strategy.)
- **What does the loser see?** (A clean "already taken," not a stack trace and not a silent no-op.)
- **Retries?** (The same customer retrying must not lose the resource they just claimed.)

### Why this problem matters

This is the explicit bridge from family 2 back into the distributed world, and it is written to force the ordering the whole family depends on. In one JVM this is a solved, small problem: one `compareAndSet` on the seat's holder reference, null to customer, and the boolean it returns *is* the arbitration. One lock over the seat map is the equally correct, simpler alternative. A candidate should produce that in fifteen seconds with the linearization point named.

Then the interviewer says "now fifty hosts," and the answer is a **relocation, not a redesign**: the CAS moves into a unique constraint on `(resource, slot)`, and the constraint violation is the boolean that `compareAndSet` returned. Same invariant, same mechanic, new home for the linearization point.

Candidates who skip the local answer and open with a distributed lock have done two things wrong at once: they've reached for the weakest available primitive (a lease, which cannot guarantee mutual exclusion under pauses) when the strongest one, a uniqueness constraint, which is absolute, was sitting right there; and they've skipped the step that demonstrates they understand *why* the distributed version works.

---

## Strategy

### Classify

Guarded state (family 2) with capacity one: a claim-before-work on a *named* resource. Distinguish it immediately from the inventory problem, there, units are fungible and the invariant is a counter bound; here, the resource is a specific identified thing and the invariant is **uniqueness**. Uniqueness is a stronger and cheaper thing to enforce than a counter bound, which is why this problem has a better answer than that one.

### The single-JVM answer: say this first, in full

The state is a map from resource to holder. The claim is:

> `compareAndSet(seat, null, customer)`, swap the holder from "unclaimed" to this customer, atomically. The boolean it returns is the arbitration: `true` means you booked it, `false` means someone else did.

The linearization point is the CAS. There is no retry loop, a failed CAS here is not a conflict to retry, it is the *answer* ("taken"), which is a meaningful difference from the OCC problem and worth stating. The equally correct alternative is one lock around a check-then-set over the map, which is simpler to explain and fine at this scale; or `putIfAbsent` on a concurrent map from resource to holder, which is the same thing with a nicer name.

**Deliver this cold, in one breath, with the linearization point named, before saying a single word about databases.** Then say: *"across hosts, that CAS has to live somewhere all instances can see, the cheapest correct home is a uniqueness constraint."*

### Invariant

For each `(resource, slot)`, at most one confirmed holder exists, ever. And, the half candidates drop, a customer who is told "you got it" **is** that holder. Two separate mechanisms, one saying yes and another recording the claim, is two linearization points and a double-booking waiting to happen.

### Mental model, distributed

Create a booking table with a **unique constraint on `(resource, slot)`**. To claim, **insert**. If the insert succeeds, you hold it. If it violates the constraint, someone else does, and you tell the customer so.

The properties worth naming explicitly, because they are why this beats every alternative:

- **The constraint violation is the CAS's false return.** Identical semantics; the database's index is doing the arbitration that `compareAndSet` did in-process. Say this sentence.
- **It is absolute, not advisory.** Unlike a lease, there is no expiry, no clock, no pause window, and no fencing needed. The index cannot be wrong. A pathologically paused instance that wakes up and inserts still gets rejected, because the constraint doesn't care how long anything took.
- **The rule and the enforcement are the same object.** "One booking per seat per showing" is both the business rule and the index definition. No second mechanism can drift out of sync with the first.
- **It is idempotent for free**: sort of, and precisely. A retry from the *same* customer also violates the constraint and is told "taken," which is wrong from the customer's perspective: they took it, and now they're told they didn't. So include the holder in the record and, on violation, **read the existing row**: if the holder is the same customer, this is their own retry and the correct response is success. That check is the natural-key form of an idempotency key, and it is why this problem needs no separate one.

**Do not read-then-insert.** "Is this seat taken? No → insert" is check-then-act with a network in the gap, and two simultaneous requests both read empty and both insert. Let the insert *be* the check and treat the violation as the answer, not as an error. This is the same sentence as the idempotency-key problem, and it is the family's most repeated lesson for a reason.

### Design reasoning

### Reserve with a TTL

The customer claims a seat and then pays, which takes time and may never finish. You cannot hold anything across that, so the claim carries a state and an expiry: a **hold** that becomes a **booking** on confirmation and lapses if it doesn't.

That reopens the uniqueness question: an expired hold's row still exists and still occupies the unique constraint, so a new claimant's insert fails against a dead hold. Two ways to handle it, and both are defensible:

- **Reclaim lazily**: when a claim fails against an existing row, check whether that row is an expired hold, and if so, take it over with a **conditional update**, claim it only if it is still in the same expired-hold state you observed. Exactly one takeover wins. No background job, and the family-7 instinct (derive on read; don't run a sweeper you don't need) applies unchanged.
- **Sweep**: a periodic job deletes expired holds. Simpler to reason about, but you now own a job, and the window between expiry and sweep is time the resource is falsely unavailable.

Either way, the takeover **races the late confirmation**, the customer's payment succeeded at the instant their hold expired and was reclaimed. Both sides must be conditional writes against the hold's observed state so exactly one wins; the loser is handled commercially (refund, or re-offer). This is the same race as the inventory reclaimer and the same shape as the lease-expiry problem, and noticing that it is the *same* race in a third costume is the kind of pattern recognition the family is teaching.

### When the invariant spans rows

Discrete slots, seat 14A for the 7pm showing, fit inside one row, so one unique constraint solves the problem completely. **Arbitrary intervals do not.** "This room from the 3rd to the 7th" overlaps "the 5th to the 9th," but they are different rows with different values, and no equality-based unique index catches an overlap.

Say this out loud, because it is the trapdoor in this question: **uniqueness constraints enforce equality, not overlap.** The invariant now spans a *set* of rows, which is the write-skew territory from the pessimistic-locking problem, two bookings each check "does anything overlap me?", each sees nothing, and each inserts. Row-level mechanisms miss it. Three honest fixes:

1. **Discretize.** Model availability as fixed units (nights, hour slots) and insert one row per unit in one transaction. The invariant collapses back into per-row uniqueness, which is why almost every real booking system does this. Best answer when the domain allows it.
2. **An exclusion constraint over ranges**, where the database supports it, the engine enforces non-overlap directly, which is the range-lock answer packaged as a constraint.
3. **Materialize the conflict.** Introduce a row representing the resource itself and have every booking lock *that* row first, serializing all bookings for that resource. Simple, portable, and it costs you concurrency per resource, usually fine, because bookings for one room are rare, and this is the pragmatic fallback when neither of the first two is available.

### Why not a distributed lock

Because a uniqueness constraint is **strictly stronger** and cheaper. A lease can be held by two processes at once under a pause; an index cannot be violated by anything, ever. If you find yourself proposing a distributed lock for a problem whose invariant a constraint could express, you have chosen a weaker primitive for more operational work. Say that explicitly if the interviewer offers you a lock, it is one of the clearest demonstrations available that you understand the earlier problem in this family rather than just its vocabulary.

### The matching-flavour variant

"One driver to one ride" adds a wrinkle: the resource isn't chosen by the customer, it's *selected* by the system, and several dispatchers may pick the same driver simultaneously. The selection is a hint; the **claim is the truth**. Select optimistically, attempt the claim, and on failure select again, a retry loop where each attempt targets a different resource. The invariant is unchanged and the constraint still arbitrates; what's new is that losing is routine and recovery means picking someone else, not failing the request. Bound the attempts and jitter them, or a thundering herd of dispatchers will all chase the same nearest driver in lockstep.

### Trade-offs

- **Unique constraint vs pessimistic row lock**: the constraint is lock-free, absolute, and needs no ordering discipline, but it only expresses equality-based rules. A lock handles set-spanning invariants at the cost of contention and deadlock discipline. Constraint first; lock when the invariant doesn't fit in a row.
- **Lazy reclaim vs sweeper**: lazy avoids owning a job and reclaims exactly when someone cares; a sweeper makes availability accurate for *browsing* users too (the seat map shows it free again without anyone attempting a claim). Real systems often do both, lazy for correctness, sweep for display.
- **Hold TTL**: short frees abandoned resources fast and cancels slow legitimate customers mid-payment; long is customer-friendly and strands scarce resources.
- **Discretize vs range constraint**: discretizing simplifies everything and constrains the product (no arbitrary check-in times); a range constraint is exact and ties you to a specific engine's capability.
- **Fail-fast vs auto-retry on loss**: for a customer-chosen seat, tell them it's taken and let them choose again. For a system-chosen driver, retrying automatically is right. Same invariant, opposite UX, and the difference is who made the choice.

### Pitfalls

1. **Read-then-insert**: two claimants both see it free. The insert must be the check.
2. **Opening with a distributed lock**: a weaker primitive than the constraint that was available, plus the entire pause problem, adopted voluntarily.
3. **Skipping the single-JVM answer**: you've failed the part of the question that was actually being graded, even if the distributed design is right.
4. **Treating the constraint violation as a 500**: it is an expected, meaningful outcome ("already taken"), not an error. Catch it specifically and translate it.
5. **Not distinguishing the requester's own retry**: the customer who claimed it is told they didn't get it. Store the holder and check it on violation.
6. **Expired holds occupying the constraint with no takeover path**: the resource is permanently unbookable.
7. **Reclaim racing a late confirmation, resolved non-atomically**: double-booking through the back door, at the one moment nobody is watching.
8. **Assuming a unique index prevents overlapping intervals**: it enforces equality only. Discretize, use an exclusion constraint, or materialize the conflict.
9. **Two linearization points**: a service that decides availability and another that records the booking. The recording *is* the decision.
10. **Unbounded, unjittered retry in the matching variant**: every dispatcher chasing the same driver in lockstep.

### Check your understanding

1. Give the single-JVM answer in one sentence with the linearization point. Why is there no retry loop around this CAS, unlike in OCC?
2. What replaces the CAS across hosts, and what plays the role of its boolean return?
3. Why is a uniqueness constraint strictly stronger than a distributed lock for this invariant?
4. Two requests for the same seat arrive simultaneously under a read-then-insert design. Where exactly is the double booking?
5. The same customer retries their own claim. What happens naively, and what makes it right?
6. An expired hold still occupies the constraint. Give two reclaim strategies and the race that both must resolve atomically.
7. Why doesn't a unique index prevent overlapping date ranges? Name the anomaly and three fixes.
8. How does the driver-matching variant differ, and what stays exactly the same?
9. Name the three earlier problems in this family whose mechanics reappear here, and where.

### Transfers to

Seat and ticket booking; hotel and room reservations; ride/driver matching and any assignment problem; username and handle registration (uniqueness with no TTL at all, the simplest instance); idempotency keys (the same constraint, on a client-supplied key instead of a natural one); flash-sale inventory (the fungible sibling); and backwards into family 2's guarded state, of which this is the exact distributed image.
