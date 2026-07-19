---
layout: post
title: Distributed Lock and Lease
date: 2026-07-19
description: >-
  This is where the family's central lesson lands. A distributed lock looks like a mutex and is not one, and the difference is not an implementation detail of any particular…
categories: interview multithreading problems
---

Part of the [Distributed Concurrency](/interview/multithreading/patterns/distributed-concurrency/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Senior system-design rounds, Uber L5+, AWS L6, Rubrik, Coinbase. **High frequency**, and a favourite trap: the question is usually phrased as if the answer is obvious.

### Problem

Exactly one process across a fleet should perform some action at a time: run a nightly reconciliation job, act as leader for a shard, hold a resource while mutating it. Design a mutual-exclusion mechanism that works across hosts.

Then answer the follow-up the question exists for: **the holder pauses, a long garbage collection, CPU throttling, a live migration, for longer than the lock's expiry. Another process acquires the lock and begins working. The first process wakes up, still believing it holds the lock, and writes. What happens, and how do you make the system correct anyway?**

### Constraints

- No shared memory; the coordinating store is reachable over a network that can drop or delay messages.
- Any participant can crash at any moment, including while holding the lock.
- A crashed holder must not block the system forever.
- Process pause durations are **unbounded**, you may not assume a maximum.
- Clocks on different hosts are not comparable.

### Clarify before solving

- **Is a double execution incorrect, or merely wasteful?** (*The* question. Route the whole design on the answer. A duplicate cron run that wastes CPU is a different problem from a duplicate financial posting, and treating them the same is either over-engineering or a bug.)
- **What resource is actually being protected, and can it enforce anything itself?** (If the protected write can be made conditional, the lock stops being load-bearing, which is the best outcome available.)
- **How long is the protected work, relative to any expiry?** (Long work needs renewal, not a long lease.)
- **Is this really mutual exclusion, or is it leader election?** (Different guarantees, different tools.)
- **Could partitioning by key remove the need entirely?** (One owner per key needs no lock at all, ask before designing one.)
- **Single JVM first?** (Yes. Say "in one process this is a `ReentrantLock`", and then say the uncomfortable part: there is no distributed mechanism with the same guarantee.)

### Why this problem matters

This is where the family's central lesson lands. A distributed lock **looks** like a mutex and is not one, and the difference is not an implementation detail of any particular product, it is a property of the model. A mutex is held until the holder releases it. A lease is held until *someone else's clock* says it isn't, and nobody informs the holder. Because process pauses are unbounded, the gap between "your lease expired" and "you find out" is unbounded too, and during that gap two processes both believe they hold the lock.

Candidates who answer "set a key if it doesn't exist, with a TTL" have given the first sentence of the answer and stopped exactly where the interview begins. The senior answer names the pause problem before being asked, presents **fencing tokens** as the real fix, explains that fencing works by moving the arbiter from the lock to the *resource*, can discuss the Redlock debate even-handedly without picking a team, and closes with the most valuable line in the problem: **prefer designs that don't need a distributed lock at all.**

---

## Strategy

### Classify

Mutual exclusion, attempted across the process boundary, and the honest classification is that **it does not fully survive the crossing**. Every other problem in this family relocates a single-JVM mechanism and keeps its guarantee. This one relocates the mechanism and *loses* the guarantee, which is why it is the family's most instructive problem.

**Single-JVM answer first, cold:** one `ReentrantLock` (or `synchronized`), acquired and released in a `finally`. Mutual exclusion is absolute: nobody else enters until the holder releases, and if the holder is descheduled for a minute, the lock stays held and everyone else simply waits. Now cross the boundary and notice what breaks: `finally` may never run, because the holder's host may never execute another instruction. So the lock must expire on its own. And the instant it can expire without the holder's consent, mutual exclusion is gone.

### Invariant

The one you *want*: at most one process is inside the critical section at any time.

The one you can actually have with a lease: at most one process holds a **valid, non-expired grant** at any time, which is not the same thing, because a process can be inside the critical section while its grant is expired, and it has no way to know.

The one that is achievable and sufficient: **at most one process's writes are accepted by the protected resource.** Getting from the second statement to the third is what fencing does, and it is the whole design.

### Mental model

A lease is a **claim with an expiry**: a set-if-absent on a shared key with a TTL. Acquisition is atomic at the store, so exactly one claimant wins. The TTL is the substitute for `finally`, it is the only cleanup that survives the holder's death.

Two immediate consequences of the TTL, and both must be handled:

- **Release must be conditional.** A holder whose lease already expired must not delete the key, because a *new* holder now owns it, deleting it would evict a legitimate holder and hand the lock to a third. So the release must verify ownership atomically: delete only if the value still equals my token. This is a conditional write, OCC again, and it is a real bug that ships regularly.
- **Renewal, not long leases.** For work longer than the TTL, the holder **heartbeats**, extending the lease periodically (conventionally at around a third of the TTL, so a couple of missed renewals are survivable). Long leases are the wrong alternative: a crashed holder's claim then blocks everyone for its full duration. Heartbeating improves liveness, but note carefully that it **does not fix the pause problem**, because a process paused past its expiry cannot heartbeat, which is precisely the failing case.

### The pause problem, stated properly

Holder A acquires a 30-second lease and starts work. A's JVM enters a stop-the-world collection, or A's container is CPU-throttled, or A's VM is live-migrated, or A's host swaps. Forty seconds pass in the world; zero pass inside A. The lease expires. B acquires it legitimately and starts working. A resumes. Nothing has told A anything: no exception, no callback, no flag. Its lock object still says "held." A writes.

Three things to be clear about:

1. **This is not a bug in any product.** No lease implementation can fix it, because fixing it would require bounding process pause duration, which you cannot do on commodity hardware, in a container, or on a JVM.
2. **Checking the remaining lease time before writing helps and does not close the race.** The pause can occur *between* the check and the write. Do it anyway, it's cheap and it catches the common case, but do not present it as the fix, because an interviewer will ask "and what if it pauses right after the check?"
3. **Making the TTL longer does not fix it either.** It reduces the probability and lengthens the outage when a holder genuinely crashes. It trades one failure for another; it does not eliminate a class.

### Fencing tokens: the actual fix

Have the lock service issue a **strictly increasing token** with every grant: grant 33, then 34, then 35. The holder passes its token with **every write to the protected resource**, and the resource records the highest token it has accepted and **rejects any write bearing a lower one**.

Now replay the scenario. A holds token 33 and pauses. B is granted token 34, writes, and the resource records 34. A wakes and writes with 33; the resource rejects it. Two processes were in the critical section, mutual exclusion never came back, but the **invariant held**, because the resource became the arbiter.

Three observations that make this a senior answer rather than a memorized one:

- **The inversion.** Once you fence, the lock is no longer the correctness mechanism. It has become an *optimization*, it stops fifty hosts from all attempting and failing. The thing actually guaranteeing the invariant is the token check at the resource. Say this out loud; it reframes the whole problem.
- **Fencing is OCC.** "Reject writes whose token is below the highest seen" is a conditional write on a monotonically increasing version. It is the same mechanism as the version column, pointed at a lock instead of a record. That unification is worth naming.
- **The resource must participate.** This is the catch, and the reason fencing is less common in practice than it should be: if the protected resource is a third-party API or a filesystem with no notion of your token, it cannot reject anything. When the resource can't fence, you have only two honest options, restructure so the protected write is conditional on something the resource *does* understand (a version, a unique constraint), or accept the risk explicitly and document it.

Where do monotonic tokens come from? A consensus-backed coordinator gives you one naturally as part of its data model, every state change carries a globally increasing revision, and a lock's revision is a ready-made fence. A single-node store can give you one with an atomic increment, at the cost of that node being a single point of failure. Note that a *timestamp* is not a valid token: clocks are not comparable across hosts, so "later" is not well-defined.

### The Redlock debate: present both sides, don't rule

A lock on a single store node is a single point of failure: if that node dies, in-flight grants are lost and the lock's guarantees go with it. **Redlock** is the proposed multi-node algorithm, acquire the lock on a majority of N independent nodes within a bounded time window, and subtract the elapsed acquisition time from the lease's remaining validity.

**The critique** (Kleppmann, broadly): the algorithm's safety rests on bounded clock drift and bounded process pauses, neither of which is safe to assume on commodity systems. A clock jump on enough nodes can make a majority believe a lease is valid when it isn't; and a GC pause on the *client* reproduces the two-holder scenario regardless of how carefully the grant was obtained. The conclusion drawn is a dilemma: if you need the lock for **correctness**, you need fencing tokens, and once you have fencing, you no longer depend on Redlock's guarantees; if you need it only for **efficiency**, a single node is simpler and sufficient. So the algorithm occupies a band that doesn't exist.

**The defense** (Antirez, broadly): the timing assumptions Redlock makes are of the same character as those many practical systems make, and Redlock states them explicitly rather than burying them; a lock granted by a majority of independent nodes is meaningfully more available than one granted by a single node; and the pause objection is an argument against **lease-based locking in general**, it applies just as much to a lock built on a consensus store, so it is not a critique of Redlock specifically.

**What both sides agree on, and what you should say:** no lease-based lock can guarantee mutual exclusion in the presence of unbounded pauses, so if correctness depends on it, the **resource must fence**. Then add the practical note that consensus-backed coordinators are the stronger primitive, they give you genuinely monotonic revisions and session-based liveness detection, while being explicit that they are *still leases* and still need the fencing check enforced at the resource. Presenting it this way demonstrates you have engaged with the argument rather than adopted a position, and at this level that is exactly what's being measured.

### Prefer designs that don't need one

The most valuable move in this problem is to reduce the need for the lock. Run through these before designing one:

- **Make the operation idempotent.** If a duplicate execution is harmless, the lock is a cost optimization and can be best-effort.
- **Make the write conditional.** A version condition or a unique constraint means the second holder's write simply fails. The invariant is enforced by the resource; the lock is optional.
- **Partition by key.** Route all work for a key to one owner. One writer per key needs no lock at all. Do state the caveat, during a rebalance, ownership moves, and there is a window in which the old owner hasn't noticed and the new one has started. That window is the same two-holder problem, and it is why partitioned systems also carry epoch or generation numbers, which are fencing tokens by another name.
- **Use the database transaction.** If the state is in one database, a row lock gives you real mutual exclusion, guaranteed release, and deadlock detection, everything a distributed lock only approximates. Reach for a lock service only for things the database cannot see.
- **Use a queue.** Serialize the work through a single consumer per key; the queue's ordering is the exclusion.

Delivering this list unprompted is the strongest available signal on this question, because the best senior instinct here is not "which lock" but "why do I have a lock."

### Trade-offs

- **TTL length**: short reclaims a crashed holder quickly but risks expiring under a live holder that merely paused; long is stable under load but leaves the system stalled for the full TTL after a genuine crash. Heartbeating lets you take a short TTL without a short work horizon, it is usually the right answer, and its residual failure is exactly the pause case.
- **Single-node store vs consensus store**: simple, fast, and a single point of failure, versus operationally heavier, slower to acquire, and providing real monotonic revisions and session liveness. Choose on whether the lock is protecting correctness or efficiency.
- **Fencing vs not**: fencing is the only thing that makes the design correct under pauses, but it requires the resource to cooperate, which is often impossible for third-party systems. When it's impossible, say so, and either restructure the write or state the accepted risk.
- **Lock vs no lock**: every alternative above beats the lock on robustness. The lock's advantage is that it is easy to bolt onto existing code without changing the write path, which is exactly why it gets used in places where fencing is impossible.

### Pitfalls

1. **No expiry**: a crashed holder blocks the system permanently. TTL is the distributed `finally`.
2. **Unconditional release**: an expired holder deletes the key and evicts the legitimate new owner. Delete only if the value is still my token.
3. **Treating the lease as a mutex**: the pause problem, unaddressed. If you can't name it before the interviewer does, you have not answered this question.
4. **Checking remaining lease time and calling it a fix**: the pause can land between the check and the write.
5. **Longer TTL as the fix**: trades a correctness risk for an availability risk; eliminates nothing.
6. **Non-monotonic or clock-based tokens**: timestamps aren't comparable across hosts; a token must come from one authority and only increase.
7. **Fencing tokens that the resource ignores**: passing a token that nothing checks is decoration. The rejection rule at the resource is the load-bearing part.
8. **Non-reentrant deadlock against yourself**: one process's two code paths both wanting the same lock, with no reentrancy across the boundary.
9. **Reaching for a distributed lock when a transaction, a partition, or a constraint would do**: the most common over-engineering in this family.

### Check your understanding

1. Give the single-JVM answer, then say precisely which of its guarantees does not survive the crossing, and why.
2. Narrate the pause scenario end to end. Why is it not fixable by any lock implementation?
3. Explain fencing tokens. After adding them, what is the lock actually *for*?
4. Why is a fencing token a special case of optimistic concurrency control?
5. Why must release be conditional on ownership? Construct the three-process failure when it isn't.
6. Does heartbeating fix the pause problem? Explain in one sentence why not.
7. Summarize the Redlock critique and the defense in two sentences each, and state what both sides agree on.
8. Name four designs that remove the need for a distributed lock, and the caveat that attaches to the partitioning one.
9. "Is a double execution incorrect or merely wasteful?", how does each answer change the design?

### Transfers to

Leader election and shard ownership; the IN_PROGRESS lease on an idempotency record (same TTL, same takeover race); reservation TTLs in inventory and booking; partition-rebalance epochs in stream processing; consumer-group generation IDs; and, running backwards, family 2's mutex, this problem is the clearest demonstration of what the single-JVM primitive was quietly giving you for free.
