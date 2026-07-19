---
layout: post
title: In-Memory SQS-Like Queue
date: 2026-07-19
description: >-
  A bounded blocking queue is the easy half. The real content is visibility timeout, redelivery, and the delete-after-requeue race that forces you to say "at-least-once" out loud…
categories: interview multithreading problems
---

Part of the [Bounded Resource & Producer-Consumer](/interview/multithreading/patterns/bounded-resource/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [EngineBogie: implement an in-memory multi-threaded queue like SQS](https://enginebogie.com/public/question/implement-in-memory-multi-threaded-queue-like-sqs/691). A staff-level favourite because it looks like the blocking queue and then isn't.

### Problem

Build an in-memory queue with SQS semantics, not a plain blocking queue:

- `send(msg)`: enqueue a message. Blocks (or balks, ask) if the queue is at capacity.
- `receive()`: return a visible message AND make it **invisible** for a visibility timeout instead of deleting it. Returns a receipt handle. Blocks if nothing is visible.
- `delete(receiptHandle)`: acknowledge, remove the in-flight message permanently.
- If the visibility timeout expires with no `delete`, the message becomes **visible again** and can be redelivered.

A message is not gone when received, only hidden. It is gone only when acked. Many producers, many consumers.

### Constraints

- Bounded capacity; consumers block/wait, no busy-wait, no `Thread.sleep` to coordinate.
- Timeout expiry must not need a polling loop per message: a scheduled sweep or a `DelayQueue`, not a spin.
- Best-effort / FIFO-ish ordering is fine; strict global FIFO under redelivery is not required (a redelivered message re-enters visible, it doesn't jump to its old slot).
- At-least-once delivery. NOT exactly-once. Say this to the interviewer before you write a line.

### Clarify before solving

- On `send` when full: block, balk, or timeout? (The [policy axis](/interview/multithreading/patterns/bounded-resource/) question, ask it.)
- Is a receipt handle single-use? (Yes, a stale handle from a timed-out message must not delete whatever got redelivered under a new handle.)
- What happens to a message that keeps timing out forever? (Real SQS has a redrive/dead-letter policy after N receives; mention it, it's a maturity signal.)

### Why this problem matters

It's the single-node shadow of the distributed exactly-once problem. Strip the network away and the same impossibility survives: the consumer can crash between "did the work" and "acked", so you must choose redelivery (at-least-once) or drop (at-most-once); exactly-once needs idempotency at the consumer, not cleverness in the queue. This is the problem that forces a candidate to state that trade-off instead of hand-waving it.

---

## Strategy

### Classify

Bounded resource / producer-consumer (Type C) **with a time dimension bolted on**. The visible-message core is [the bounded blocking queue](/interview/multithreading/problems/bounded-blocking-queue/) verbatim: two parties, opposite predicates, `send` waits on notFull, `receive` waits on notEmpty. Everything beyond that is a second guarded structure keyed by time: an in-flight set with per-message expiry deadlines. Name both halves to the interviewer separately so they see you've decomposed it.

### Invariant

Every sent message is in exactly one of three states: **visible**, **in-flight** (received, deadline pending), or **deleted** (gone). `receive` moves visible → in-flight and stamps a deadline; `delete` moves in-flight → deleted; expiry moves in-flight → visible. The counting invariant: a message is delivered **at least once**, and delivered *more* than once only when its deadline fires before its `delete` lands. The linearization point of `receive` is the single atomic visible→in-flight transition.

### Mental model

A library with a returns shelf. `send` puts a book on the shelf. `receive` doesn't give the book away, it lends it and writes a due-date in the ledger. `delete` is the borrower saying "I bought it, retire it." If the due-date passes with no purchase, the librarian re-shelves the book for the next patron, who has no idea it was ever out. The bug that defines the problem: the borrower's "I bought it" slip arrives *after* the librarian already re-shelved and re-lent the book. Two patrons now hold the same title. The queue cannot prevent this; only the borrowers being idempotent can make it harmless.

### Design (bounded queue + a time-indexed in-flight structure)

Two structures under one coordination scheme:

1. **The visible queue.** A bounded blocking queue exactly as in [that problem](/interview/multithreading/problems/bounded-blocking-queue/): `ReentrantLock` + `notFull`/`notEmpty` conditions, or two semaphores. This is the part you already know cold.
2. **The in-flight set**, keyed by receipt handle, each entry carrying a deadline. Expiry is driven by a `DelayQueue` (or a single scheduled sweeper thread, or a timer wheel), never a per-message polling loop. When a deadline pops, that message flips back to visible and signals `notEmpty`.

The two rules that carry the whole design:

- **`receive` is one atomic step.** Pick a visible message, generate a receipt handle, stamp a deadline, insert into the in-flight set, arm its expiry, all under the same lock as the take from the visible queue. If picking and stamping aren't atomic, two consumers can grab the same message, or a message can sit "received but never expiring".
- **`delete` and expiry race for the same message, and `delete` must lose gracefully.** The receipt handle is the arbiter: `delete(h)` removes the in-flight entry **only if `h` still identifies a live entry**. If expiry already re-queued the message, `h` is stale, `delete` is a no-op, and the redelivered copy (under a *new* handle) is untouched. Generate a fresh handle on every `receive` so a stale one can never match.

### Why it's only at-least-once (say this out loud)

Walk the interviewer through the unavoidable interleaving: consumer A receives message m (deadline T). A is slow. At T, the sweeper re-queues m; consumer B receives it. A finally calls `delete`, too late, its handle is stale, no-op. m was processed twice. You cannot close this window from inside the queue: shrinking the timeout makes it worse, lengthening it stalls redelivery of genuinely-dead consumers. The only real fix lives at the consumer: **idempotent processing keyed by message id** (see [idempotency keys](/interview/multithreading/problems/idempotency-keys/)). Exactly-once is a consumer property, not a queue feature. Stating this crisply is the entire point of the question.

### Pitfalls

1. Building a plain bounded blocking queue and calling it done, no ack, no visibility, no redelivery. That's the *sub*problem; the interviewer asked for the superset. Contrast them explicitly.
2. `receive` picks the message and stamps the deadline in two steps → a second consumer picks the same visible message, or the deadline is never armed. Make the transition atomic under the lock.
3. `delete` blindly removes by message identity instead of by handle → the stale ack from a timed-out consumer deletes a *redelivered* copy that a different consumer is mid-processing. Ack must match the live receipt handle or no-op.
4. Reusing / not rotating receipt handles → the stale-handle guard can't tell old from new. Fresh handle per receive.
5. Busy-waiting or per-message sleeps to detect expiry → use one `DelayQueue`/sweeper. Also: the sweeper flipping a message to visible must signal `notEmpty`, or a blocked consumer sleeps through a redelivery ([lost-wakeup](/interview/multithreading/patterns/bounded-resource/), F1/F8).
6. Claiming exactly-once. There is no exactly-once here. At-least-once + idempotent consumer.

### Check your understanding

1. Trace the duplicate-delivery race step by step: A receives, sweeper fires, B receives, A deletes. At which line does A's handle become stale, and what stops A's delete from harming B?
2. Why must the pick-visible / stamp-deadline / insert-in-flight sequence be a single atomic step? Construct the failure if it isn't.
3. The sweeper re-queues an expired message. Which condition must it signal, and what bug appears if it forgets?
4. Where would you add a dead-letter policy (message received N times → sidelined), and which structure owns the receive-count?
5. Someone proposes "hold a lock across the consumer's processing so delete and expiry can't race." Why is that wrong in a real system, and what does it cost even in-memory?

### Transfers to

[Bounded blocking queue](/interview/multithreading/problems/bounded-blocking-queue/) (the visible-message core, this problem minus time). [Delayed task scheduler](/interview/multithreading/problems/delayed-task-scheduler/) (the exact same expiry/`DelayQueue`/timer-wheel machinery, here driving visibility instead of task firing). [Read-heavy cache with expiry](/interview/multithreading/problems/read-heavy-cache-with-expiry/) (the same "sweep entries whose deadline passed" concern). [Exactly-once processing](/interview/multithreading/problems/exactly-once-processing/) and [idempotency keys](/interview/multithreading/problems/idempotency-keys/) (why this is only at-least-once, and where the real fix lives). Recognise the shape: a producer-consumer queue is the trunk; add a clock and you get half the distributed-systems interview.
