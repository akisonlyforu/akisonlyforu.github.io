---
layout: post
title: Topic-Based Pub/Sub Broker (Kafka-lite)
date: 2026-07-19
description: >-
  The problem that makes you say out loud what a read actually does: a queue destroys on read, a broker does not, and the entire design turns on that one word. The log persists and each reader replays at its own offset…
categories: interview multithreading problems
---

Part of the [Task Lifecycle, Async & Parallelism](/interview/multithreading/patterns/task-lifecycle/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [enginebogie's multi-threaded topic-based message broker](https://enginebogie.com/public/question/design-and-implement-a-multi-threaded-topic-based-message-broker-kafka-pub-sub-queue/632), a "build a component" round staple at teams that run event-driven backends. It is the in-JVM shadow of the Kafka design question, and the version that actually makes you write the concurrency down rather than gesture at a broker.

### Problem

Build an in-memory Kafka-lite. Concurrent publishers call `publish(topic, message)`; each **topic is an append-only, ordered log**. Subscribers register per topic and read via **consumer groups**: every group receives every message once (fan-out across groups), and within a group the topic's partitions are load-balanced across the group's consumers. Delivery is **at-least-once**, tracked by per-consumer **offsets**: each consumer records how far it has read, messages are **not** removed on read, and the offset advances only when the consumer acks.

### Constraints

- Per-topic (or per-partition) ordering is preserved end to end.
- A fixed, bounded set of threads. Not one thread per subscriber, not one per message.
- The log is durable across reads: it is retention-bounded, not consumption-bounded. Multiple independent readers replay the same log at different positions.
- At-least-once, stated explicitly: a crash between "processed" and "committed offset" replays, so **handlers must be idempotent**. The problem does not pick the guarantee for you.
- Bounded memory: an unbounded log is a deferred OOM. Retention (by size, by age, or a bounded buffer) is part of the design, not an afterthought.

### Clarify before solving

- **Partitions, or just topics?** Ordering is per partition; a single-partition topic is the easy case. Ask whether topics are partitioned, because partitioning is where all the parallelism and all the rebalancing live.
- **Offset commit timing**: commit *before* processing (at-most-once, lose on crash) or *after* (at-least-once, duplicate on crash)? This one word is the delivery guarantee. Make the interviewer say which.
- **Group membership**: static (assignment fixed upfront) or dynamic (consumers join/leave and the group rebalances)? Dynamic rebalancing is the genuinely hard coordination; scope it explicitly.
- **Retention policy**: bounded ring per partition (drop oldest, a slow consumer loses data) or an unbounded-until-retention log (slow consumer just lags)? Different failure modes; name which you're building.
- **Backpressure**: does `publish` ever block, or is the log the buffer and slow consumers simply fall behind?

### Why this problem matters

It is the problem that forces you to say what a **read** does. The reflex from the queue problems is destructive read: take removes. Here the log **persists** and the offset is a per-reader cursor, so N independent consumers replay the same bytes at their own pace and nobody's read affects anybody else's. Get that contrast wrong and you've built a queue with extra steps; get it right and fan-out, replay, and consumer groups all fall out of one idea: *the data is immutable and the position is per reader.*

It also separates two things candidates blur: the **append** (a write, needs serialization per partition) and the **read** (over an immutable prefix, needs no lock at all). Seeing that asymmetry is most of the design.

---

## Strategy

### Classify

Task lifecycle for the delivery machinery (worker loops, backpressure, shutdown, the four lifecycle questions apply unchanged), wrapped around a **per-partition single-writer log with per-consumer cursors**. Say it as one sentence and the structure appears:

> *A partition is the unit of ordering AND the unit of parallelism; one serialized appender per partition removes every write race, and reads are lock-free over the immutable committed prefix.*

The mechanic being reused is the immutable-prefix read: the same move as the [copy-on-write snapshot registry](/interview/multithreading/problems/copy-on-write-snapshot-registry/). Writers extend a structure; readers see a consistent prefix with no lock, because the bytes they read never mutate after they're published.

### Invariant

For a partition p and any consumer c: c observes exactly the messages at offsets it has not yet committed, in append order, and each is delivered at least once. Formally: appends to p are totally ordered (single writer); a committed offset o means every message at index `< o` has been fully processed by c; a read returns the contiguous run starting at c's current offset over the **published prefix** only.

Note what it does *not* say: nothing about ordering across partitions, and nothing forbidding redelivery. Stating it this narrowly is the work. "Messages are delivered in order" over-promises a total order across the topic that a partitioned log does not and need not provide.

### Mental model

A library with numbered shelves (partitions). New books are placed strictly in order at the end of a shelf by the one librarian assigned to that shelf, so no two people ever fight over a slot. Any number of readers walk the shelf independently, each holding a bookmark (offset) at their own place; one reader's progress moves nobody else's bookmark, and a book stays on the shelf after it's read. Reading groups (consumer groups) split the shelves among their members so each group covers every shelf exactly once. Books are only cleared from the front when the shelf is full or the book is old enough (retention), never because someone read it.

### Design: single-writer partition log + per-consumer offsets (ship this)

Each partition is an **append-only list** with exactly one appender. Serialize appends per partition (a per-partition lock, or a single-threaded writer, or an atomic tail index with CAS-published entries). The published length is a volatile/atomic "committed size"; readers snapshot that length and read indices below it with no lock, because entries at those indices are immutable once counted.

Why appends must be single-writer per partition: two concurrent appenders racing on the tail index give you a lost write or a reordered log, and reordering *is* the bug the whole problem exists to prevent. One serialized appender per partition kills the race by construction, and it's cheap because partitions parallelize: N partitions give N independent single-writer logs.

Why reads need no lock: a consumer reads only indices below the published length, and those entries never change after publication. This is the copy-on-write-registry argument in different clothing: **immutability turns concurrent reads into a non-problem.** The only shared read state is the published-length counter, and a single volatile read of it is the whole synchronization cost.

Offsets live per (consumer, partition), not in the log. The log is written once and read many; each consumer owns a small map of partition→offset. Nothing about a consumer's cursor touches the log's write path, which is why readers scale independently of writers and of each other.

Properties to name:

- **Partition = ordering unit = parallelism unit.** Same-partition messages are strictly ordered; cross-partition messages are not. Parallelism is capped at the partition count, and a key that must stay ordered pins to one partition (hash the key), so a hot key is an irreducibly serial lane, exactly the skew ceiling from the event bus.
- **Retention, not consumption, bounds memory.** A bounded ring drops the oldest when full (a lagging consumer loses data, say so); an unbounded-until-retention log lets a slow consumer lag without loss but grows until the retention window trims it. Pick one out loud; they fail differently.
- **Non-destructive read is the whole point.** Contrast with the [SQS-like in-memory queue](/interview/multithreading/problems/sqs-like-in-memory-queue/), where take removes and a message belongs to exactly one consumer. Here the log persists and every group replays it; deleting on read would collapse fan-out.

### Consumer groups and offset commit (the two hard parts)

**Group semantics.** Every group gets every message once (fan-out across groups). *Within* a group, the topic's partitions are distributed across the group's consumers so each partition is owned by exactly one consumer at a time; that's how a group load-balances while keeping per-partition order. Offsets are therefore **per group per partition**: two groups reading the same topic hold independent cursors.

**Rebalancing is the coordination problem.** Deciding who owns which partition when consumers join or leave is the genuinely hard part, and the correctness rule is: a partition must be owned by at most one consumer in a group at any instant, or its order breaks. The interview-honest move is to name it, sketch the simplest sound scheme (freeze assignment while reassigning: pause the partition, let the current owner commit, hand ownership over, resume), and note that production Kafka runs a full group-coordinator protocol. Do not build the protocol unless asked; state the invariant it protects.

**Offset commit is a check-then-act, and its ordering IS the delivery guarantee:**

- **Commit AFTER processing** → at-least-once. A crash between process and commit replays the last message; duplicates are possible, so handlers must be idempotent.
- **Commit BEFORE processing** → at-most-once. A crash after commit loses the in-flight message; no duplicates, but gaps.

There is no third option without a distributed transaction across the handler and the offset store. Say which side of the process/commit line you commit on, and you have stated the guarantee precisely; waffling here is the tell that a candidate hasn't internalized what at-least-once costs.

### Backpressure

Two honest choices. **Bounded per-partition buffer**: publishers can be throttled or rejected when the partition is full, real backpressure, the [bounded blocking queue](/interview/multithreading/problems/bounded-blocking-queue/) applied per partition. **Unbounded log with retention**: `publish` never blocks, slow consumers simply lag, and memory is bounded by the retention window rather than by consumption. The first protects memory by pushing back on producers; the second protects producers by dropping the oldest history. Kafka picks the second; name your choice and its loss mode.

### Pitfalls

1. **Destructive read.** Removing a message when a consumer reads it turns the broker back into a queue and destroys fan-out. The log persists; only the offset moves.
2. **Multiple appenders per partition.** Concurrent tail writes reorder or lose entries, breaking the one invariant the problem is about. One serialized writer per partition, always.
3. **Locking reads.** Taking a lock to read an immutable prefix is wasted contention; a single volatile read of the published length is the entire read-side synchronization.
4. **Global offset instead of per-consumer.** One shared cursor makes consumers destructively compete and kills independent replay. Offsets are per (group, partition).
5. **Commit-before-process by accident** (e.g. advancing the offset as you hand the message to the handler), silently at-most-once, silent data loss on failure. The commit point is a deliberate decision, not a side effect of the read.
6. **Two consumers owning one partition mid-rebalance.** Order breaks and messages double-process without idempotency. Ownership is exclusive per partition per group; reassign only after the old owner commits.
7. **Unbounded log with no retention.** Deferred OOM. Retention by size or age is part of the design.

### Check your understanding

1. State the exact difference between this broker's read and the SQS queue's read, and explain why fan-out to multiple groups is trivial here and impossible there.
2. Give the correctness argument for lock-free reads in one sentence. Which single shared variable still needs synchronization, and why is one volatile read of it enough?
3. Commit-after-process vs commit-before-process: trace a crash in the window between the two operations for each, and say which guarantee each yields.
4. Why is a partition simultaneously the unit of ordering and the unit of parallelism? What caps your throughput when one key carries 80% of traffic, and can more partitions fix it?
5. During a rebalance, what must be true about partition ownership at every instant, and what breaks if two consumers in one group briefly both own a partition?

### Transfers to

The append-per-partition-with-per-consumer-cursor is the model behind Kafka/Kinesis consumers, CDC pipelines, and event-sourced stores. Specifically: [event-bus-with-per-key-ordering](/interview/multithreading/problems/event-bus-with-per-key-ordering/) is the same single-writer-per-key ordering with no offsets (it dispatches then forgets, no replay); the [SQS-like in-memory queue](/interview/multithreading/problems/sqs-like-in-memory-queue/) is the destructive-read contrast; the [bounded blocking queue](/interview/multithreading/problems/bounded-blocking-queue/) is per-partition buffering; [exactly-once processing](/interview/multithreading/problems/exactly-once-processing/) is this problem's offset-commit ordering taken to its conclusion; and the [copy-on-write snapshot registry](/interview/multithreading/problems/copy-on-write-snapshot-registry/) is the immutable-prefix read that makes the log's readers lock-free. The unifying idea: **make the data immutable and the position per-reader, and concurrency stops being about the data at all.**
