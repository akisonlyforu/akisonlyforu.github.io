---
layout: post
title: Exactly-Once Processing
date: 2026-07-19
description: >-
  This problem forces the candidate to be honest about an impossibility, and honesty here is precisely what is being graded. Exactly-once *delivery* over an unreliable network…
categories: interview multithreading problems
---

Part of the [Distributed Concurrency](/interview/multithreading/patterns/distributed-concurrency/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Senior system-design rounds — Uber L5+, Stripe, Coinbase, Airbnb, AWS L6. **Very High frequency** wherever queues or event streams appear.

### Problem

A producer service commits a state change and emits an event. A fleet of consumers processes those events and performs side effects — send a notification, post a ledger entry, update a projection. Design the pipeline so that each logical event's effect happens **exactly once**, given that:

- the network can lose messages and acknowledgements,
- consumers can crash between processing a message and acknowledging it,
- the broker can redeliver,
- consumers rebalance, so a partition's owner can change mid-flight.

Also address: how the producer avoids the state-changed-but-no-event (and event-but-no-state-change) failure, what happens to a message that always fails, and what ordering guarantees consumers can actually rely on.

### Constraints

- Multiple consumer instances; partitions may be reassigned at any time.
- Side effects include at least one that is not naturally idempotent.
- The producer's state change and the event must not diverge.
- Throughput matters; per-message coordination across all consumers is not acceptable.

### Clarify before solving

- **Exactly-once delivery, or exactly-once effects?** (Say the difference unprompted — the first is impossible, the second is the real requirement, and conflating them is the mistake the question is looking for.)
- **Is the effect naturally idempotent?** (If it can be an absolute end state, most of the machinery disappears.)
- **What is the dedup identity?** (A broker-assigned message ID is not stable across a republish; a producer-assigned business event ID is.)
- **What ordering does the consumer actually need?** (Per-key ordering is cheap; global ordering is expensive and usually unnecessary. Ask which one the domain needs.)
- **Ack before or after processing?** (At-most-once vs at-least-once, stated as a choice rather than an accident.)
- **What should happen to a message that fails forever?** (DLQ, and then a real question: for an ordered stream, is skipping it worse than stalling?)
- **Single JVM first?** (Yes: a worker pool consuming a `BlockingQueue`, dedup via a claim on a concurrent set. Say it before naming any broker.)

### Why this problem matters

This problem forces the candidate to be honest about an impossibility, and honesty here is precisely what is being graded. Exactly-once *delivery* over an unreliable network cannot be achieved, because the sender cannot distinguish a lost message from a lost acknowledgement. A candidate who promises it has not thought about it. A candidate who says *"I'll take at-least-once delivery and make the effect idempotent, so the observable outcome is exactly-once"* has converted a vague requirement into a concrete design in one sentence.

It also contains the **dual-write problem** — commit to the database *and* publish to the broker, with no transaction spanning both — which has no safe ordering and whose standard fix, the transactional outbox, is the general template for every "atomicity across two systems" question you will ever be asked: don't span two systems, collapse the write into one.

Finally, its ordering discussion is the direct distributed analogue of the memory model: per-partition ordering is "program order within a thread," and nothing is guaranteed across partitions. Making that connection out loud is a strong senior signal.

---

## Strategy

### Classify

Task lifecycle (family 6) across the process boundary. The single-JVM shape is a worker pool draining a queue, with duplicate suppression by claim-before-work on a concurrent set — and every piece of that has a counterpart here: the queue is a broker, the workers are consumer instances, the claim is a dedup store or a conditional write, and the pending-counter discipline becomes acknowledgement discipline.

**Single-JVM answer first, cold:** a `BlockingQueue`, N worker threads, and a `ConcurrentHashMap.newKeySet()` whose `add()` returns the boolean that decides ownership. The linearization point is that add. Now cross the boundary and notice the two things that break: the queue is now a system that can redeliver, and the worker can die between doing the work and recording that it did — a gap that had no analogue in-process, because in-process the set update and the work were separated by nothing that could kill you selectively.

### Invariant

For each logical event, the side effect is applied **at most once**, and every event is eventually applied **at least once**. The conjunction is what everybody means by exactly-once, and stating it as two separate properties is useful, because they are achieved by two different mechanisms: at-least-once comes from **redelivery** (ack after processing, never before), at-most-once comes from **idempotency** (the consumer, not the broker).

### Mental model

Start from the impossibility. The sender transmits and waits for an acknowledgement. If none arrives, exactly two worlds are consistent with what it observed: the message was lost, or the message arrived and the acknowledgement was lost. **No amount of protocol distinguishes them** — any additional round trip you add has the same ambiguity at its own edge. So the sender must choose:

- **At-most-once** — never retry. No duplicates, some loss. Acceptable only when losing an event is genuinely fine (a metric sample; a best-effort cache invalidation).
- **At-least-once** — retry until acknowledged. No loss, some duplicates.

Take at-least-once, and then **spend the duplicates on the consumer side**. That is the entire architecture, and the framing sentence is worth memorizing: *exactly-once delivery is impossible; exactly-once effects are achievable via at-least-once delivery plus idempotent consumers.*

Everything else in this problem is the elaboration of that sentence in three places: the producer end (don't lose or fabricate events — the outbox), the consumer end (make the effect idempotent — dedup or conditional writes), and the edges (what fails forever, and what order things arrive in).

### Design reasoning

### Acknowledge after processing, not before

Acknowledging on receipt makes the message disappear before the work is done: crash mid-processing and the effect is lost forever. That is at-most-once, chosen by accident. Acknowledge **after** the effect is durable, and a crash mid-processing simply means redelivery — which is exactly the duplicate your idempotency layer exists to absorb. The window between "effect committed" and "ack sent" is unclosable, and that is fine: it is precisely the window that generates the duplicates you have already decided to tolerate.

### Making the consumer idempotent — three routes, in order of preference

1. **Naturally idempotent effects.** Express the effect as an absolute end state rather than a delta: "set status to shipped," "upsert this projection row," "set balance to this value." Duplicates are then free, needing no store and no expiry policy. Always check this first; it dissolves a surprising number of these problems.
2. **Conditional write on an event version.** If events for an entity carry increasing versions, the consumer writes only when the incoming version exceeds the stored one. A replay has a version that is no longer greater and is dropped. This is OCC again, and it has a valuable bonus property: it also handles **out-of-order** arrivals, not just duplicates — which matters because retries and DLQ replays break ordering anyway.
3. **A dedup store keyed on the event ID.** The general fallback. Record the ID atomically before applying, and treat a uniqueness violation as "already handled." Same mechanic as the idempotency-key problem, with a message ID instead of a client-supplied key.

The identity question is load-bearing: dedup on a **producer-assigned business event ID**, not on a broker-assigned delivery ID. Delivery IDs change on redelivery or republish, so deduping on them deduplicates nothing at exactly the moment you need it.

**The strongest version of route 3:** put the dedup record and the effect in the **same transaction**. If both live in one database, "record that I processed event E" and "apply E's effect" commit or roll back together, and the crash-in-between window disappears entirely. When they can't share a transaction — the effect is an external API call — you're back to claim-before-work with its inherent gap, and the honest answer is that the *downstream* must then supply its own idempotency (you send it your idempotency key). This is the propagation property from the idempotency-key problem: robust systems have idempotency at every layer, not just the edge.

### The dual-write problem and the transactional outbox

The producer must change state and emit an event. There is no transaction spanning the database and the broker, so consider both orderings:

- **Commit, then publish**: crash in between → state changed, no event. Downstream is permanently, silently inconsistent.
- **Publish, then commit**: the transaction rolls back → an event announcing something that never happened. Consumers act on a fiction.

Neither ordering is safe, and no amount of retry logic fixes it, because the process can die during the retry. The problem is structural: two systems, one atomicity requirement.

**The outbox** collapses it into one write. In the *same* transaction as the state change, insert a row into an outbox table. One commit, one atomic unit — either both happened or neither did. A separate relay process then reads pending outbox rows, publishes them, and marks them sent. The relay is at-least-once by construction (it can publish and die before marking), which is exactly the delivery semantic you already decided to accept, so the duplicate is absorbed by the idempotent consumer. Generalize the move out loud, because this is what the interviewer wants: **when you need atomicity across two systems, don't — collapse the write into the one system that has transactions, and derive the second effect from it asynchronously.**

The relay can read the table by polling, or tail the database's change log — the change-data-capture variant, which avoids polling latency and outbox-table write amplification at the cost of a heavier pipeline.

### Ordering: per partition, never global

Streaming systems guarantee order within a **partition** only. Partition by entity key and you get ordering for each entity, which is nearly always what the domain actually needs ("this account's events in order"), and nothing across entities, which is nearly always fine.

This is the memory model, restated: order within a thread, nothing across threads — with partition for thread. Say the analogy; it lands.

Two consequences to state:

- **Ordering is fragile in practice even within a partition.** Retries, DLQ replays, and concurrent processing within a consumer all break it. So do not build a design whose correctness depends on order; build one that *tolerates* disorder via versioned conditional writes, and treat ordering as an optimization rather than a guarantee.
- **Rebalancing creates a two-owner window.** When partitions are reassigned, the old owner may not have noticed while the new one has begun. This is the distributed-lock pause problem, wearing a different hat, and it is why consumer groups carry generation/epoch numbers — fencing tokens by another name. Naming that connection is a strong move.

### Poison messages and dead-letter queues

A message that always fails will be retried forever, blocking its partition and consuming capacity — a retry storm and a livelock at once. Bound the retries with jittered backoff, then move the message to a **dead-letter queue** with its error context and original metadata, and **alarm on DLQ depth** (a DLQ nobody watches is a silent data-loss channel with extra steps).

The judgment call worth voicing: for a **strictly ordered** stream, DLQ-and-continue means subsequent events for that key are applied *without* the failed one, which can corrupt the entity's state worse than stalling would. So the policy is per-stream: **block-on-error** where ordering is semantically required, **DLQ-and-continue** where throughput matters and events are independent. Naming this as a policy choice rather than picking one silently is what distinguishes the answer.

Also distinguish failure kinds: a transient failure (downstream is down) deserves retry with backoff; a permanent one (malformed payload, referenced entity does not exist) should go to the DLQ immediately, because retrying it is pure waste. Retrying everything uniformly is the common shortcut and it converts a bad message into a load problem.

### Trade-offs

- **At-least-once + idempotent consumer vs broker-native transactional exactly-once**: some brokers offer transactional semantics that give exactly-once *within* their own ecosystem (consume, transform, produce). That is genuinely useful and genuinely bounded — the moment the effect leaves the broker's world (an HTTP call, a write to another database), you are back to at-least-once plus idempotency. Know that boundary; candidates often over-claim what broker transactions cover.
- **Dedup store TTL**: a bounded window is cheap but lets a late replay through; unbounded is exact but grows forever. Prefer conditional-write-on-version, which needs no window at all, whenever events carry versions.
- **Outbox polling vs change-data-capture**: polling is simple and adds latency and load; CDC is low-latency and operationally heavier. Start with polling.
- **Ordering vs throughput**: strict per-key ordering means one in-flight message per key, capping parallelism at the key level; relaxing it needs version-tolerant consumers. Buy ordering only where it's required.
- **Block-on-poison vs DLQ-and-continue**: correctness of a key's state versus availability of the whole stream. State per-stream.

### Pitfalls

1. **Claiming exactly-once delivery.** It is impossible; say so and pivot to effects. Promising it is the answer this question is designed to catch.
2. **Acknowledging before processing.** Silent at-most-once; crashes lose events permanently.
3. **Dual write without an outbox.** Both orderings are broken, and no retry logic fixes them.
4. **Deduping on a broker-assigned delivery ID.** It changes on redelivery, so it fails to deduplicate precisely when it matters. Use a producer-assigned event ID.
5. **Dedup window shorter than the replay horizon.** DLQ replays and manual reprocessing arrive far later than automated retries.
6. **Assuming global ordering.** You have per-partition ordering at best, and even that is broken by retries. Build for tolerance, not dependence.
7. **Unbounded retries on a poison message.** Blocks the partition and amplifies load. Bound, then DLQ.
8. **A DLQ nobody monitors.** Data loss with a paper trail.
9. **Retrying permanent failures.** Distinguish transient from permanent; a malformed payload will never succeed.
10. **Recording processing after the effect, with no transaction.** The crash in the gap causes reprocessing. Share the transaction where possible; otherwise push idempotency downstream.
11. **Ignoring the rebalance window.** Two owners of a partition, briefly — the pause problem again, and the reason for consumer generations.

### Check your understanding

1. Why is exactly-once delivery impossible? Give the two indistinguishable worlds.
2. Give the one-sentence framing that converts the impossible requirement into a buildable design.
3. Give the single-JVM version of this pipeline and name its linearization point.
4. Both orderings of the dual write are broken — narrate each failure. How does the outbox eliminate the class rather than reduce the probability?
5. Why dedup on a producer-assigned event ID rather than the broker's delivery ID?
6. Why does a conditional write on an event version beat a dedup store, and what extra problem does it solve for free?
7. What is the distributed analogue of "program order within a thread"? What follows for how you partition?
8. When is DLQ-and-continue the wrong policy?
9. What does a broker's transactional exactly-once actually cover, and where does it stop?
10. Why does a consumer group need generation numbers, and which earlier problem is that the same as?

### Transfers to

Idempotency keys (the same dedup, on requests rather than messages); webhook delivery and receipt on both sides; CDC and replication pipelines; saga/compensation designs, which are this problem plus rollback; distributed lock and lease (the rebalance window is the pause problem); and family 6's task lifecycle, whose completion-and-accounting discipline this is a durable version of.
