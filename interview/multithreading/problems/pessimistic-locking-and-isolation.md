---
layout: post
title: Pessimistic Locking and Isolation Levels
date: 2026-07-19
description: >-
  This is family 2, guarded state, one invariant, one lock, with the lock moved inside the database. Everything transfers: the compound operation must be atomic, the…
categories: interview multithreading problems
---

Part of the [Distributed Concurrency](/interview/multithreading/patterns/distributed-concurrency/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Senior backend rounds, AWS L6 (explicitly probes pessimistic vs optimistic and isolation levels), Stripe, Coinbase, Uber L5+. **High frequency**, and the standard follow-up to optimistic concurrency control.

### Problem

Several application instances must perform a read-decide-write over the same rows, where the decision depends on data that other transactions are concurrently changing. Examples: debit an account only if the balance covers it; assign the next available unit; enforce "at least one on-call engineer must remain" while people take themselves off rotation.

Design the update path using the database's own locking and isolation machinery, and be able to state **which anomalies each isolation level prevents and which it does not**.

### Constraints

- One relational database, many application instances.
- Some invariants are confined to a single row; at least one spans a *set* of rows.
- Transactions must be short, nothing may be held across a call to an external service.
- Deadlocks are possible and must be handled, not merely hoped away.

### Clarify before solving

- **Does the invariant live in one row, or across rows?** (The pivotal question. A single-row invariant is easy; a set-spanning one is where isolation levels start to matter.)
- **What isolation level does this database run at by default?** (Read committed on most Postgres/Oracle setups; repeatable read on MySQL/InnoDB. The default is *not* the same everywhere and the anomalies differ, asking shows you know that.)
- **Is contention high enough to justify locking over OCC?** (The same crossover question as the previous problem, asked from the other side.)
- **How long will the lock be held?** (If the answer includes a network call to a third party, the design is wrong before it starts.)
- **What should a waiter do if the row is already locked?** (Block, block with a timeout, or skip it and take another row. The last one is a real and underused option.)
- **Single JVM first?** (Yes: this is one lock around a compound operation. Say that before saying anything about the database.)

### Why this problem matters

This is family 2, guarded state, one invariant, one lock, with the lock moved inside the database. Everything transfers: the compound operation must be atomic, the invariant decides what must be guarded together, and multi-lock acquisition in inconsistent order still produces a deadlock cycle with exactly the same shape and exactly the same fix.

What the database adds, and what candidates most often cannot articulate, is **isolation levels**: a set of named, standardized trade-offs between how much concurrency you allow and which anomalies you tolerate. Being able to name lost update, dirty read, non-repeatable read, phantom, and write skew, and say which level stops which, is a concrete, checkable piece of senior knowledge that separates people who have operated databases from people who have used them. Write skew in particular is the anomaly that survives repeatable read, that per-row locks and per-row versions both miss, and that produces real production incidents.

---

## Strategy

### Classify

Guarded state (family 2) with the lock relocated into the database. One invariant → one lock; the compound operation must be atomic; the critical section is the transaction.

**Single-JVM answer first, cold:** wrap the read, the decision, and the write in one `synchronized` block or one `ReentrantLock`. The linearization point is inside the critical section. Now cross the boundary: the mutual exclusion has to be enforced by the only party all instances talk to, so the critical section becomes a **transaction**, and the lock becomes a **row lock taken by a locking read**, a select that declares up front "I intend to update these rows, hold them for me until I commit."

### Invariant

The decision and the write that depends on it are separated by no other transaction's write to the data the decision was based on. Concretely, for the classic case: the balance a debit was authorized against is the balance the debit is applied to.

### Mental model

An ordinary read takes no lock and gives you a value that may be stale by the time you write. A **locking read**, a select that declares an intent to update, takes an exclusive lock on each row it returns and holds it until the transaction ends. Any other transaction that tries to lock the same row waits. Read, decide, and write inside that window, and no one can slip between the decision and the write.

This is a mutex, with three differences worth naming:

1. **Release is automatic and guaranteed.** Commit or roll back, including if the client connection dies, and the locks go. This is the `finally` block that the rest of the distributed family doesn't get. It is the single strongest argument for putting your critical section in a database transaction rather than in a distributed lock service.
2. **The database detects deadlocks.** When two transactions form a wait cycle, the engine notices and aborts one with a specific, retryable error. You still have to *handle* it, but you don't have to detect it.
3. **The lock's granularity is not entirely yours to choose.** You ask for rows; depending on the engine, isolation level, and whether your predicate can use an index, you may get a range or gap lock covering rows that don't exist yet. That is usually what saves you (it is how phantoms are prevented) and occasionally what surprises you (an unindexed predicate can lock far more than you intended, turning a row lock into a table-shaped one).

### The anomalies, and which level prevents them

Say these in order of increasing strictness; each level prevents everything the weaker ones do, plus one more.

- **Dirty read**: reading another transaction's uncommitted write, which may then roll back, so you acted on data that never existed. Prevented from **read committed** upward. (Read uncommitted allows it and is essentially never the right choice.)
- **Non-repeatable read**: reading the same row twice in one transaction and getting different values, because someone committed in between. Allowed at read committed; prevented at **repeatable read**, which gives your transaction a stable snapshot of the rows it has read.
- **Lost update**: two transactions both read a value, both compute from it, both write; one write vanishes. This is the anomaly with the most confusing status, so be precise: it is *not* reliably prevented by read committed, and different engines handle it differently at repeatable read (some detect and abort, some don't). **Do not rely on the isolation level to prevent lost updates.** Prevent them explicitly, with a locking read (pessimistic) or a version condition (optimistic). This precision is a strong senior signal, because the sloppy answer is "repeatable read fixes lost updates," which is engine-dependent at best.
- **Phantom**: you query a *set* ("all rows matching this predicate"), someone inserts a new row matching it, and re-running the query returns a different set. Row locks cannot prevent this by construction: you cannot lock a row that doesn't exist yet. Prevented by range/gap locking or by **serializable**.
- **Write skew**: the subtle one, and the one to be able to explain cold. Two transactions read an **overlapping set**, each checks a constraint over that set, each sees it satisfied, and each writes to a **different row**. No row is written by both, so no row lock and no per-row version detects anything, yet the combined result violates the constraint. The canonical case: two on-call engineers each check "is anyone else on call?", each sees the other, and each takes themselves off. Both writes succeed; nobody is on call. Prevented only at **serializable**.

The generalization worth stating: **row-level mechanisms protect row-level invariants.** When the invariant is a property of a *set*, you need something that guards the set, serializable isolation, a range lock, or the trick of **materializing the conflict**: introduce a row that represents the set (a per-rotation lock row, a per-resource counter row) and make every participant lock *that* row. Materializing the conflict is the pragmatic production answer when serializable is too expensive, and naming it converts a textbook answer into an engineering one.

### Design reasoning

**Pessimistic vs optimistic, from this side.** Locking guarantees progress per attempt, a waiter queues and then wins, rather than losing and retrying, so total work stays linear in requests instead of amplifying under contention. That is exactly the regime where OCC falls apart, so the two are complements, not rivals: **optimistic when conflicts are rare, pessimistic when they are common.** The cost of pessimism is that you hold a lock across at least one round trip, you introduce waiting (and therefore timeouts, deadlocks, and queueing latency), and you can hurt unrelated traffic if the lock is coarse.

**Keep transactions short, and never hold a lock across an external call.** The number-one production incident in this space is a transaction that locks a row and then calls a payment provider, an email service, or another internal API. Now the lock is held for the *tail latency of someone else's system*, contention queues build, connections exhaust, and an unrelated slowdown becomes your outage. This is the single-JVM rule "never block holding a lock", but the blocking is now a third party's SLA. When the flow genuinely needs an external call in the middle, split it: a short transaction that **reserves**, the external call with no locks held, and a short transaction that **confirms**. That is exactly the reserve-then-confirm shape from the inventory problem, and this is *why* it exists.

**Deadlock, unchanged.** Two transactions that lock rows A and B in opposite orders form a cycle in the waits-for graph, the same cycle, the same four Coffman conditions, and the same production fix: **impose a global ordering on lock acquisition**, for instance always touching rows in ascending primary-key order. The two-sentence proof transfers verbatim: every waiter waits for something ordered above everything it holds, and a cycle would require someone waiting for something below, contradiction. What's new is that the database will pick a victim and abort it for you, so your code must treat the deadlock-victim error as **retryable** and retry the whole transaction (with jitter). Retrying is correct here in a way it isn't elsewhere, because the aborted transaction left no trace.

**The lock-if-free option.** Many engines let a locking read **skip rows that are already locked** instead of waiting. For queue-like tables, "grab the next unclaimed job", this is exactly right and is far better than waiting: every worker takes a *different* row and none of them queue behind each other. It turns a contention problem into a partitioning one, and it is the database-native form of atomic claiming. Underused; mentioning it is a differentiator.

**Locking reads shared vs exclusive.** An exclusive locking read blocks other readers-for-update; a shared one lets concurrent readers hold it together but blocks writers. Shared locks look attractive for "just check this exists" but they are the classic route to the **upgrade deadlock**: two transactions each hold a shared lock and each want to upgrade to exclusive, and each waits for the other to let go. This is the same read-to-write upgrade deadlock that read-write locks have in a single JVM, and the same reason `ReentrantReadWriteLock` refuses upgrades by design. If you know you will write, take the exclusive lock on the first read.

### Trade-offs

- **Isolation level**: stricter levels remove whole classes of bug you would otherwise have to reason about individually, at the cost of throughput and, at serializable, of aborted transactions your code must retry. Serializable on a hot path is expensive; serializable on a rarely-executed, high-stakes operation is often exactly right. Choose per transaction, not per application.
- **Lock granularity**: a coarse lock (one row that everything locks) is simple and correct and serializes everything; fine-grained locks scale but reintroduce ordering discipline and deadlocks. The escalation-ladder rule holds, start coarse, refine against measured contention.
- **Wait vs skip vs fail fast**: waiting maximizes throughput per unit of work but creates queues; skipping is ideal for interchangeable work items and wrong when a specific row is required; a short lock timeout that fails fast protects you from unbounded queueing at the cost of visible errors. This is the block/balk/timeout policy axis, unchanged.
- **Database transaction vs distributed lock service**: if the state is in one database, the transaction is strictly better, real mutual exclusion, guaranteed release, deadlock detection, no fencing needed. Reach for a distributed lock only for things the database cannot see.

### Pitfalls

1. **Assuming the isolation level prevents lost updates.** Engine-dependent and mostly false. Prevent them explicitly with a locking read or a version condition.
2. **Holding a lock across an external call.** Your lock hold time becomes someone else's tail latency. Split into reserve / call / confirm.
3. **Inconsistent lock ordering across code paths.** The same deadlock as family 2. Order deterministically; retry the victim.
4. **Not handling the deadlock-victim error.** It arrives as an ordinary exception and is *retryable*; treating it as fatal turns a routine event into a user-visible failure.
5. **Per-row protection for a set-spanning invariant.** Write skew sails straight through both row locks and version columns. Serializable, a range lock, or materialize the conflict.
6. **Unindexed predicate on a locking read.** Instead of locking a few rows, you may lock a great many, or effectively serialize the table. Check the plan.
7. **Shared lock then upgrade.** The upgrade deadlock, identical to the single-JVM read-write lock case. Take the exclusive lock up front.
8. **Long transactions in general.** Beyond lock hold time, they pin old row versions and bloat the engine's version store, degrading everything.
9. **Locking rows in a loop, one per iteration.** Hold time grows with the loop and the ordering becomes data-dependent, a deadlock generator. Lock the whole set in one deterministically ordered statement.

### Check your understanding

1. Give the single-JVM answer in one sentence. What are the three ways the database's version differs from a `ReentrantLock`?
2. Name the five anomalies and the lowest isolation level that prevents each. Which one is engine-dependent, and what should you do instead of relying on the level?
3. Explain write skew with a concrete example. Why do neither row locks nor version columns catch it, and name two fixes.
4. Why can't a row lock prevent a phantom?
5. Construct a two-transaction deadlock over two rows and give the two-sentence proof that ordered acquisition prevents it. What must your code do when the database picks a victim?
6. Why is holding a row lock across a payment API call an outage waiting to happen, and what is the standard restructuring?
7. When would you deliberately skip locked rows rather than wait for them?
8. State the crossover rule between optimistic and pessimistic in one sentence.

### Transfers to

Optimistic concurrency control (the other half of the same decision); flash-sale inventory and double-booking (both eventually need a short transaction around the decision); the reserve-then-confirm shape everywhere it appears; job-queue tables using skip-locked claiming; and, directly backwards, family 2's guarded state and the deadlock/lock-ordering material, which this problem is a re-run of at a different layer.
