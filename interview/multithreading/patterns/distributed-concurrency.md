---
layout: post
title: Distributed Concurrency & Idempotency Playbook
date: 2026-07-19
description: >-
  What breaks when concurrency crosses the process boundary, the single-node to distributed translation table, idempotency keys, leases and fencing tokens, and exactly-once effects.
categories: interview multithreading patterns
---

Deep dive on distributed concurrency, companion to [What do you actually do in a Multithreading interview?](/interview/multithreading/mt-framework/). This is the standard senior follow-up, "now run it on fifty hosts", and every mechanism here is a primitive you already own, relocated. Answer the single-JVM version first, always.

The family where **the threads are no longer in your JVM**. Seven problems live here, idempotency keys, optimistic concurrency control, pessimistic locking & isolation, distributed lock & lease, exactly-once processing, flash-sale inventory, double-booking prevention, and they are all the same seven problems you already solved in families 1–7, re-asked after the interviewer says the sentence that opens this whole category:

> *"Great. Now run this on fifty hosts."*

Everything you know still applies. Invariant, check-then-act, linearization point, CAS, at-most-once, condition loop, the vocabulary transfers **completely**. What changes is where the atomicity lives, what a "thread" can do to you when it stops responding, and the fact that you can no longer read a variable to find out what is true.

**The ordering rule that governs this entire family, stated once and repeated in every problem doc: answer the single-JVM version first, cold, then cross the boundary.** A candidate who reaches for Redis before they have said "in one JVM this is `compareAndSet` on an `AtomicReference`, and the linearization point is the CAS" has skipped the part the interviewer is grading. The distributed answer is only impressive *on top of* the local one; on its own it reads as pattern-matching. Say the local answer, name its linearization point, then say "the moment this runs on more than one host, that linearization point has to move somewhere both hosts can see, here is where I'd put it."

---

## 1. What actually changes at the process boundary

Four things, and every failure mode in this family is one of them wearing a costume.

**1. There is no shared memory.** In one JVM, `synchronized` works because all threads see one heap and the monitor gives you a happens-before edge. Across hosts there is no heap and no monitor. Every piece of shared state must live in something *outside* all the participants, a database row, a Redis key, a queue partition, a consensus log, and every operation on it is a **network round trip that may fail in the middle**. Your critical section is no longer an instruction range; it is a conversation with a third party.

Consequence: **you cannot invent a mutual-exclusion primitive out of nothing.** You must borrow atomicity from a system that has it. The whole family reduces to "which external system owns the linearization point, and what atomic operation does it offer me?", a unique index, a conditional update, an atomic increment, a compare-and-set, a transaction.

**2. Partial failure is the normal case.** In a single JVM, if a method call fails you get an exception; if the JVM dies, everything dies together. Across a network there is a third outcome that has no single-machine analogue: **you sent the request, and you do not know whether it happened.** Timeout means "no answer," not "no effect." The server may have committed and died before replying; the reply may have been dropped.

This one fact generates the entire idempotency half of this family. Because the caller cannot know, the caller must **retry**. Because the caller retries, the callee must tolerate **duplicates**. "Exactly once" is not something you achieve by being careful with the network; it is something you achieve by making the *second* attempt harmless.

**3. Clocks are unreliable and not comparable.** Family 7 taught you that wall clocks jump within one machine. Across machines it is worse: two hosts' clocks differ by an unknown amount (skew), drift at different rates, and get corrected by NTP in both directions. `nanoTime` does not help, it is monotonic *within a JVM* and meaningless across JVMs.

Consequence: **you may not use timestamps to decide who won.** "Last write wins by timestamp" silently loses data when the loser's clock was ahead. "The lock expires at T" is a statement about *someone's* clock. Time is usable for *hints* (expiry as a garbage-collection heuristic, TTL as a liveness backstop) and unusable as the *arbiter of correctness*. When correctness needs an ordering, use a **logical** one: a version number, a sequence number, a monotonic token issued by a single authority.

**4. Processes stop without dying.** A thread that is descheduled for 200ms is invisible in a single JVM because the lock it holds is still held. Across the boundary, a process that pauses, GC, page fault, CPU steal, a container being throttled, the VM being live-migrated, is **indistinguishable from a crashed process**, and the only tool the system has for "is it dead?" is a timeout. So the system will eventually declare it dead and hand its work to someone else. And then it wakes up, still believing it holds the lock, and writes.

This is the single most important fact in the family, and it is the reason Section 4 exists. **A lease is not a mutex.** A mutex is held until you release it; a lease is held until *someone else's clock* says you no longer hold it, and nobody tells you.

---

## 2. The translation table: single-node → distributed

This is the table to have in your head. Read it left to right in the interview, literally say "the single-JVM answer is X; across hosts, X becomes Y."

| Single-JVM mechanism | Distributed counterpart | What you give up |
|---|---|---|
| `synchronized` / `ReentrantLock` | **Lease with TTL + fencing token** (or: redesign so you don't need one) | Mutual exclusion is no longer guaranteed, only *fenced* writes are. Two holders can coexist; only one can commit. |
| `compareAndSet` / CAS loop | **Conditional write**: update ... where version = n; or a conditional-put / if-match-etag | Round-trip latency per attempt; retries cost real time, so retries need bounds and jitter. |
| `AtomicLong.incrementAndGet` | **DB atomic decrement with a guard predicate**, or a **sharded counter** summed on read | Exactness vs throughput: one row is exact and contended; N shards are fast and need a fan-out to read. |
| `wait()` / `Condition.await()` | **Queue / polling / notification (webhook, change stream, pub-sub)** | No lost-wakeup guarantee for free, you need durable state to poll, because a missed notification is silent. |
| `notifyAll()` | Publish an event; consumers are at-least-once | The signal may be delivered twice or zero times; the *state* must be the truth, not the signal. |
| happens-before edge (JMM) | **Causality + per-key ordering guarantees** (partition ordering, causal tokens, read-your-writes) | Global ordering is expensive; you usually buy ordering per key and none across keys. |
| Volatile read | **Read from the primary / a quorum read / read-your-writes token** | A replica read may be stale by an unbounded amount; "I just wrote it" does not mean you can read it. |
| Immutability / safe publication | Immutable events in a log; append-only records | Storage grows; you need compaction and a snapshot story. |
| `ConcurrentHashMap.putIfAbsent` (claim-before-work) | **Unique constraint on an insert** | The claim costs a round trip and can fail *ambiguously* (did my insert land?). |
| Thread confinement | **Partitioning / sharding by key**, one owner per key | Rebalancing: ownership moves, and during the move two owners may briefly believe they own the key. |
| try-finally release | **TTL expiry** (the finally block that runs even if the host is on fire) | Expiry is a guess; too short breaks correctness, too long breaks liveness. |
| Deadlock via lock ordering | **Deadlock via row-lock ordering in the DB**, same cycle, same fix | The DB may detect and abort a victim for you; you must handle "your transaction was chosen as the deadlock victim" as a retryable error. |

Two rows deserve extra emphasis because they are where candidates lose points:

- **`putIfAbsent` → unique constraint.** Family 6's claim-before-work mechanic, "dedup via one atomic boolean-returning op; the boolean is the linearization point of ownership", is *exactly* the idempotency-key design, one layer down. The unique index is the CAS. Saying that sentence is the single highest-value moment in this whole family.
- **finally → TTL.** In one JVM, `finally` always runs. Across hosts, the holder may never run anything again. TTL is the only cleanup mechanism that survives the holder's death, which is why every distributed claim, lock, and reservation in this family carries one, and why every one of them then has to answer "what if it expires while the work is still running?"

---

## 3. The idempotency toolkit

An operation is **idempotent** when applying it twice has the same effect as applying it once. Note carefully: *the same effect*, not *the same code path*. The second attempt is allowed to do something completely different (return a cached result, throw a duplicate error, no-op) as long as the observable state afterwards is identical.

Four ways to get there, in rough order of preference:

**1. Make the operation naturally idempotent.** Absolute assignment rather than relative mutation: "set status = SHIPPED" is idempotent; "increment shipped_count" is not. "Set balance = 500" is idempotent; "add 100 to balance" is not. When you can express the intent as a desired end state rather than a delta, duplicates are free and you need no bookkeeping at all. Always ask this question first, a surprising number of "we need an idempotency key" problems dissolve here.

**2. Make the write conditional on a version.** `update ... set x = ..., version = n+1 where id = ? and version = n`, the replay's condition no longer matches, so it does nothing. This is CAS, and it gives you idempotency and lost-update protection in the same move. It requires the caller to have read a version, which means it fits "read, decide, write back" flows and not fire-and-forget commands.

**3. Deduplicate on a client-supplied key.** The caller generates a unique key per *logical intent* and sends it with every retry of that intent. The server records the key atomically before doing the work; a second arrival with the same key is recognized and short-circuited. This is the general hammer, it works for operations that are not naturally idempotent and have no natural version. It costs you a table, a scoping decision, and an expiry policy.

**4. Deduplicate on a server-derived natural key.** Sometimes the domain already contains a uniqueness rule: one seat per showing, one booking per (resource, time slot), one payment per (invoice, amount, day). Then the unique constraint on the domain table *is* the dedup, and you need no separate key at all. This is strictly better than (3) when it applies, because the constraint that prevents duplicates is the same constraint that expresses the business rule, one mechanism, not two.

**The recurring trap, in all four:** the check and the act must be one step. "Look up whether this key exists; if not, insert it and do the work" is `if (!map.containsKey(k)) map.put(k, v)` with a network in the gap. Two concurrent retries, which is the *expected* traffic pattern here, since retries are often triggered by the same timeout, both find nothing and both proceed. **The insert must be the check.** Let the unique constraint fail, and treat the constraint violation as the answer, not as an error. The constraint violation *is* your `compareAndSet` returning false, and the point at which the database decides which of the two inserts wins is the **linearization point** of the whole operation.

---

## 4. Leases, fencing, and why a distributed lock is not a lock

Take a lock with a TTL. Holder A acquires it, begins work, and then experiences a 30-second stop-the-world GC pause (or its container is throttled, or its host is live-migrated). The lease expires. Holder B acquires the lock legitimately and starts working. A wakes up. Nothing has told A anything, from inside A's process, no time has passed and it still holds a valid lock object. A writes.

Now two writers are in the critical section, and every argument you made about mutual exclusion is void. **This is not a bug in any particular lock implementation. It is a property of the model:** you cannot bound a process's pause, so you cannot bound the gap between "the lease expired" and "the holder notices."

Three responses, and a senior answer names all three:

**Response 1, fencing tokens (the real fix).** The lock service issues a **monotonically increasing token** with each grant. Every write to the protected resource carries the token, and the resource **rejects any write whose token is lower than the highest it has already seen**. Now A wakes up with token 33, B holds token 34 and has already written; A's write is rejected by the storage layer. Mutual exclusion was never restored, but *correctness* was, because the resource became the arbiter. Note what this really means: the lock stopped being the mechanism and became an *optimization* (it reduces wasted work), while the fencing check at the resource is what actually guarantees the invariant. That inversion is the insight.

Note also that the fencing check is itself a version-comparison conditional write, Section 2's CAS row. Fencing and OCC are the same mechanism pointed at different problems.

**Response 2, make the work idempotent or transactional instead.** If the protected operation is a conditional write on a version, or is guarded by a unique constraint, then a second concurrent holder is harmless: one commits, the other's condition fails. The lock becomes purely an efficiency measure ("don't have fifty hosts all attempt and fail"). This is the design to prefer, and the reason the last line of Section 8 is "prefer designs that don't need a distributed lock."

**Response 3, accept the risk explicitly, and say so.** For work that is advisory (only one host should run this cron job; a duplicate run is wasteful but not incorrect), a TTL lock without fencing is fine, and pretending otherwise is over-engineering. The distinction that matters: **is a double execution incorrect, or merely wasteful?** Answer that before choosing a mechanism. Interviewers reward the candidate who asks it.

### Leases and heartbeats

A **lease** is a lock with an expiry that the holder can renew. Heartbeating (renewing the lease every TTL/3, say) keeps a long-running healthy holder from losing its claim, and lets a crashed holder's claim lapse quickly. It improves liveness and *does not fix* the pause problem, a holder paused past its expiry cannot heartbeat, which is exactly the case that hurts. Two practical rules: the holder should **check remaining lease time before each externally visible write** (cheap, catches the common case, does not close the race), and the TTL should be chosen relative to the *work granularity*, not the total job length, long jobs should renew, not take a long lease.

### The Redlock debate: present both sides, don't rule

A single-Redis lock is a single point of failure: if that node dies holding grants, the lock is lost. Redlock is the proposed multi-node algorithm, acquire on a majority of N independent Redis nodes within a bounded time, and count the elapsed acquisition time against the lease validity.

*The critique* (Kleppmann's, broadly): the algorithm's safety depends on bounded clock drift and bounded process pauses, and neither is a safe assumption on commodity systems. Clock jumps on any node can make a majority believe a lease is valid when it is not; a GC pause on the client reintroduces the two-holder scenario regardless of how the grant was obtained. The conclusion drawn is that if you need the lock for *correctness*, you need fencing tokens, and once you have fencing tokens you no longer need Redlock's guarantees; if you need it only for *efficiency*, a single Redis instance is simpler and adequate.

*The defense* (Antirez's, broadly): the assumptions Redlock makes are of the same kind that many practical distributed systems make; the timing bounds are made explicit rather than hidden; a system that is asked for a lock and returns one on a majority of independent nodes is meaningfully more available than one node; and the pause objection applies to essentially every lease-based system including ZooKeeper-based ones, so it is an argument about leases in general rather than about Redlock specifically.

*What both sides agree on*, and what you should say: **a lease-based lock alone cannot guarantee mutual exclusion in the presence of unbounded pauses; if correctness depends on it, the resource must fence.** Then note that consensus-backed coordinators (ZooKeeper, etcd) are the stronger primitive because they give you a genuinely monotonic fencing number (a zxid / mod-revision) and session-based liveness, but that they are still leases and still need the fencing check to be enforced *at the resource*. Present it this way and you have shown you understand the argument rather than picked a team; interviewers at this level are testing exactly that.

---

## 5. Ordering, delivery, and "exactly once"

**Exactly-once *delivery* over an unreliable network is impossible**, this is not an engineering shortfall, it is a consequence of the fact that the sender cannot distinguish "message lost" from "acknowledgement lost." The sender must choose:

- **At-most-once**: send, never retry. No duplicates, possible loss.
- **At-least-once**: retry until acknowledged. No loss, possible duplicates.

**Exactly-once *effects* are achievable**, and that is what everyone actually means: at-least-once delivery plus an idempotent consumer (Section 3) or a consumer-side dedup store. The framing is worth memorizing as a sentence, because saying it converts a vague requirement into a concrete design: *"I can't get exactly-once delivery; I'll take at-least-once delivery and make the effect idempotent, so the observable outcome is exactly-once."*

Three mechanisms hang off this:

**Ordering is per-partition, not global.** Streaming systems guarantee order only within a partition (or per key, if you partition by key). This is the distributed analogue of the JMM's "program order within one thread, nothing across threads." Design consequence: **route all events for one entity to one partition** so that entity's events are ordered, and never assume ordering across entities. If a consumer needs to tolerate out-of-order arrivals anyway (retries and DLQ replays break order), give each event a version and drop stale ones, again the conditional write.

**The dual-write problem and the transactional outbox.** "Commit to the database, then publish to the queue" is two writes with no shared transaction: crash in between and the state changed with no event, or the event fires and the transaction rolls back. There is no ordering of the two that is safe. The fix is to make them **one** write: within the same database transaction, insert the event into an *outbox* table alongside the state change. A separate relay reads the outbox and publishes, marking rows sent. The relay is at-least-once (it may publish and die before marking), which is fine, consumers are idempotent by design. The outbox converts an atomicity problem across two systems into an atomicity problem inside one, and that is the general shape of every good answer here.

**Poison messages and DLQs.** A message that always fails will be retried forever, blocking its partition and burning capacity, the distributed retry-storm equivalent of a livelock. Bound the retries, then move the message to a **dead-letter queue** with its failure context, and alarm on DLQ depth. The judgment call worth voicing: for an *ordered* stream, skipping a poison message to a DLQ means later messages for that key are applied without it, which may be worse than stalling. So the policy is per-stream: block-on-error for strict ordering, DLQ-and-continue for throughput. Naming that trade-off is the senior move.

---

## 6. Contention: sharding, reservation, and admission

Some invariants are inherently a single hot number ("stock ≥ 0"). At high concurrency, the row holding that number becomes a serialization point, and everything queues behind it. Three tools:

**Atomic conditional decrement.** Do the whole thing in one statement at the store: decrement stock by 1 *only where* stock ≥ 1, and read the affected-row count to learn whether you won. One round trip, no read-then-write gap, no lost update, no oversell. This is the correct default and it is `decrementIfPositive` implemented as a conditional write. It does not remove contention, every buyer still serializes on that row, but it makes each critical section as short as physically possible.

**Reserve-then-confirm.** Split the operation into a short reservation (decrement now, record a reservation with a TTL) and a later confirmation (payment succeeds → finalize; timeout → return the unit to stock). This is the standard e-commerce shape and it exists because the *user-facing* step (payment, seat selection) is slow, and you may not hold a lock across a human. The price is a **reclaimer**: something must return abandoned reservations, and that reclaimer races with a late confirmation, so the confirmation must itself be a conditional write against the reservation's state, and the reclaimer must be idempotent. Note the family-7 echo: reclaim lazily on read where you can, sweep only when you must.

**Sharded / bucketed inventory.** Split 1000 units into 20 buckets of 50; each request hashes to a bucket and contends only with 1/20th of the traffic. Throughput scales with bucket count. The costs are real and you must state them: reads need a fan-out sum, a bucket can be empty while stock exists elsewhere (so you need fallback probing or rebalancing), and the "sold out" answer becomes approximate near the end. This is the `LongAdder` trade-off, cheap writes, expensive reads, at system scale.

**Queue-based admission.** Put arrivals in a durable queue and have a small number of consumers apply them serially. The invariant becomes trivially safe (one writer per key), throughput becomes predictable, and the system degrades into latency rather than errors. The cost is that the response is now asynchronous: the user gets "you're in line," not "you got it," which is a product decision as much as an engineering one. This is family 6's worker-pool answer, with a durable queue instead of a `BlockingQueue`.

**The trade to name out loud:** every one of these buys throughput by weakening either exactness of the count, freshness of the read, or synchrony of the answer. There is no design that gives you a hot exact counter at unbounded throughput; pick which of the three you are spending, and say which.

---

## 7. Pseudocode skeletons

**Skeleton A, idempotent request (claim-before-work, one layer down):**

```
handle(request with idempotencyKey K, scoped to caller C):
  try:
      INSERT (C,K) with state=IN_PROGRESS, requestFingerprint=hash(body)   // the linearization point
  catch UniqueViolation:
      existing = read (C,K)
      if existing.fingerprint != hash(body):  return 422 key-reused-with-different-body
      if existing.state == COMPLETED:         return existing.storedResponse   // replay
      else:                                   return 409 in-progress, retry-after N
  // we own it
  result = doWork()                                    // must itself be safe if we die here
  UPDATE (C,K) set state=COMPLETED, storedResponse=result
  return result
```

The `catch` branch is not error handling, it is the *other half of the algorithm*. Treat it that way when you explain it.

**Skeleton B, optimistic concurrency control (CAS across the boundary):**

```
for attempt in 1..MAX:
    (state, v) = read(id)
    newState   = compute(state)                 // pure; no side effects out here
    rows = UPDATE ... SET state=newState, version=v+1 WHERE id=? AND version=v
    if rows == 1:  return SUCCESS               // linearization point
    sleep(jittered backoff)                      // full jitter, not fixed
return CONFLICT                                  // bounded: give up, surface it
```

Identical in shape to a `compareAndSet` retry loop; the differences are that each iteration costs a round trip, and that `MAX` must exist because unbounded retries under contention become a retry storm.

**Skeleton C, lease with fencing:**

```
(granted, token) = lockService.acquire(resource, ttl)    // token strictly increases per grant
if !granted: back off / skip

loop over work units:
    if lease.remaining() < safetyMargin:  renew()  else if renew fails: abort
    storage.write(data, fenceToken = token)         // storage REJECTS token < lastSeenToken
lockService.release(resource, token)                 // best-effort; TTL is the real cleanup
```

The load-bearing line is the `fenceToken` argument and the rejection rule *inside storage*. Without it, this is a lock that can be held by two processes at once.

**Skeleton D, transactional outbox:**

```
BEGIN
  apply state change
  INSERT INTO outbox (eventId, payload, state=PENDING)
COMMIT                                         // one atomic write; no dual-write window

relay (separate process, at-least-once):
  for row in outbox where PENDING:
      publish(row)                             // may publish then die → duplicate
      mark row SENT
consumer: idempotent on eventId                // which makes the duplicate harmless
```

---

## 8. Derivation recipe: from requirement to distributed design

1. **Solve it in one JVM first, out loud.** State the invariant, name the mechanism (`AtomicInteger`, one lock, `putIfAbsent`, condition loop), and **point at the linearization point**. Do not skip this even when the question is explicitly distributed, it is the step that establishes you understand what you are about to relocate. Say: *"here is the single-process answer; the linearization point is X."*
2. **Ask what a duplicate costs.** Incorrect, or merely wasteful? Correctness-critical → you need a durable arbiter (constraint, conditional write, fencing). Wasteful-only → a best-effort lock or a dedup cache may be entirely sufficient, and choosing the heavy machinery anyway is over-engineering. Also ask: **is the operation naturally idempotent?** If you can express it as an absolute end state, you may be done here.
3. **Relocate the linearization point to a system that has atomicity.** Pick one, and name why: unique constraint (claim/dedup), conditional update on a version (read-modify-write), atomic conditional decrement (counters), a transaction with row locks (multi-row invariants), a partition assignment (one owner per key), a consensus store (leadership). *One* arbiter, an invariant spanning two stores is the dual-write problem, and the answer to that is the outbox, not a second lock.
4. **Decide optimistic vs pessimistic from contention.** Low conflict rate → optimistic (no locks held, retry on conflict) wins on throughput and holds nothing across a network round trip. High conflict rate → optimistic degrades into a livelock of retries, and taking a short row lock is *cheaper*. State the crossover as a measurement, not a belief: "I'd start optimistic and switch if conflict rate is material."
5. **Handle the retry.** Every network call is retried by someone. Give the operation an idempotency key or a natural key, decide the key's **scope** (per caller, per endpoint) and **expiry** (long enough to cover every retry the client will ever make, including a human clicking again tomorrow), and define what a mid-flight duplicate returns. Add **jittered exponential backoff** and a **retry budget**, synchronized retries are a self-inflicted DDoS.
6. **Handle the pause.** For anything holding a claim across time, ask "what if this holder freezes past its TTL?" If the answer is "another holder starts and both write," you need fencing at the resource, or you need the write itself to be conditional.
7. **Sweep the failure catalog** (Section 9), and check the two ordering questions: does this need per-key ordering (partition by key), and does any state change need to become an event (outbox)?

---

## 9. Failure-mode catalog

| # | Failure | Mechanism | Fix |
|---|---|---|---|
| 1 | **Check-then-act across the network** | "Does this key exist? No → insert." Two retries of the same request both check, both find nothing, both proceed. Check-then-act with a network in the gap. | The insert *is* the check. Unique constraint; treat the violation as the answer. |
| 2 | **Non-fenced expired lock** | Holder pauses (GC/throttle/migration) past its TTL, a second holder is granted the lock, the first wakes and writes. Two writers, invariant gone. | Fencing tokens rejected at the resource; or make the protected write conditional; or accept it explicitly for advisory-only work. |
| 3 | **Lost update via read-modify-write across services** | A reads balance 100, B reads 100, A writes 130, B writes 120. B's write silently erases A's. The distributed `count++`. | Conditional write on version (OCC), or an atomic in-store delta, or a row lock held across read and write in one transaction. |
| 4 | **Retry storm without jitter** | Every client retries at the same backoff after the same timeout, so the retries arrive in a synchronized wave and re-break the recovering service. Livelock at system scale. | Full jitter on backoff, retry budgets, circuit breakers, load shedding. |
| 5 | **Dedup window too small** | The dedup store's TTL is 5 minutes; a client retries after 10, or a DLQ replay lands the next day. Duplicate applied. | Size the window to the maximum realistic retry horizon (including manual replays); prefer a natural key with no window at all. |
| 6 | **Clock-based reasoning** | Deciding who wins by timestamp, or trusting "expires at T" across hosts with skewed clocks. Newer data silently lost; leases believed valid when expired. | Logical ordering, versions, sequence numbers, tokens from one authority. Clocks are hints, never arbiters. |
| 7 | **Dual write** | State committed to the DB, event published separately; a crash between them leaves state without event or event without state. | Transactional outbox: one atomic write, then relay at-least-once to idempotent consumers. |
| 8 | **Oversell via read-then-decrement** | `if (stock > 0) stock--` split into two round trips. Instance #3 of failure 1, in the highest-traffic setting there is. | One atomic conditional decrement; check rows-affected to learn if you won. |
| 9 | **Unbounded optimistic retry under contention** | Under heavy conflict every attempt loses to someone; throughput collapses while CPU and DB load rise. Livelock. | Bound retries and surface the conflict; switch to pessimistic locking or queue-based serialization for hot keys. |
| 10 | **DB lock-order inversion** | Two transactions update rows A and B in opposite orders → deadlock; the DB aborts a victim. Family 2's bug, at the DB layer. | Deterministic ordering of row access (e.g. always ascending by primary key); keep transactions short; treat deadlock-victim as a retryable error. |
| 11 | **Long-held lock across a slow external call** | A transaction (or lease) held across a payment API call ties up a row lock for seconds; contention explodes and timeouts cascade. | Reserve-then-confirm: short transaction to reserve, external call outside it, short transaction to confirm; TTL reclaims abandonment. |
| 12 | **Stale read after your own write** | Reading a replica right after writing the primary returns the old value; the caller retries, or the UI shows the wrong state. | Read-your-writes: read the primary for the affected key, or carry a consistency token. |
| 13 | **Idempotency key scoped or generated wrongly** | Key generated per *attempt* instead of per *intent* (so retries get new keys, no dedup at all), or scoped globally so two customers collide. | Key is generated once per logical intent by the client and reused across retries; scope it per caller/account; fingerprint the body to catch reuse. |

---

## 10. Validation against all problems

### 10.1 Idempotency keys

Recipe step 1: the single-JVM version is claim-before-work, `ConcurrentHashMap.putIfAbsent(key, IN_PROGRESS)`, and the returned boolean is the linearization point of ownership (refresher mechanic 12). Step 3 relocates that atomic to a unique index on `(scope, key)`. Steps 2 and 5 supply everything else: what a duplicate costs (a second charge, correctness-critical), what a mid-flight duplicate returns (409 with retry-after, *not* a second execution and not a fabricated success), key scope, and expiry sized to the retry horizon. Catalog hits 1, 5, 13. The pattern's central sentence, *the unique constraint is the linearization point*, is this problem's whole answer. **Recipe fits with nothing left over.**

### 10.2 Optimistic concurrency control

Step 1: single-JVM answer is a CAS retry loop on an `AtomicReference` holding an immutable snapshot. Step 3 relocates the compare to the `WHERE version = n` clause; the rows-affected count is the CAS's boolean return. Step 4 is the *point* of this problem: OCC wins under low contention and collapses under high, and the candidate must say where the crossover is and what they'd measure. Skeleton B is the answer verbatim. Catalog hits 3 (the lost update OCC exists to prevent), 9 (its failure mode), 4 (jitter on the retry). The ABA discussion transfers directly from the refresher's atomics note: a monotonic version column is precisely the "versioned stamp" fix, which is *why* a version beats comparing the value itself. **Recipe fits; step 4 was written for this problem.**

### 10.3 Pessimistic locking and isolation

Step 1: single-JVM answer is one lock around the whole compound operation, family 2's default. Step 3 relocates it to row locks acquired by the transaction; step 4 says when to prefer this over 10.2. This problem stress-tested the recipe: the recipe as first written assumed the invariant spans *one* row, but isolation anomalies (write skew, phantoms) are exactly the cases where an invariant spans a *set* of rows that no single row lock covers, so step 3's "one arbiter" now reads "one arbiter, and check whether the invariant spans rows the lock doesn't cover," which is the range-lock / serializable / materialize-the-conflict discussion. Catalog hits 10 (lock ordering, unchanged from family 2), 11 (long-held locks), 3 (what isolation levels do and don't prevent). **Recipe fits after that refinement, folded in above.**

### 10.4 Distributed lock and lease

Step 1: single-JVM answer is `ReentrantLock`, and the honest framing is that there *is no* distributed counterpart with the same guarantee, which is the lesson. Step 6 is this problem: the pause question, answered with fencing (Section 4). The recipe's step-2 question ("incorrect or merely wasteful?") does the real work, because it routes advisory use cases away from the heavy machinery entirely. Catalog hits 2 and 6. Redlock lands as an evenhanded summary, not a verdict. The problem's closing move, *prefer designs that don't need one*, is recipe step 3 read backwards: if you can put the invariant behind a constraint or a conditional write, the lock was never the mechanism. **Recipe fits; this problem is where step 6 earns its place.**

### 10.5 Exactly-once processing

Step 1: single-JVM answer is a worker pool with a dedup set (family 6, mechanic 12), duplicates are prevented by claiming before working. Step 3 relocates the claim to a dedup store or, better, to the consumer's own conditional write. Section 5 supplies the honest framing, the outbox, DLQs, and per-partition ordering. Catalog hits 7 (dual write), 5 (dedup window), 4 (retry storm), 12. The framework transfer worth naming: the JMM's "order within a thread, nothing across threads" *is* "order within a partition, nothing across partitions", same shape, and partitioning by key is thread confinement. **Recipe fits.**

### 10.6 Flash-sale inventory

Step 1: single-JVM answer is a semaphore or an `AtomicInteger` with `decrementIfPositive`, say it in one sentence before mentioning any datastore. Step 3 relocates it to an atomic conditional decrement; Section 6 supplies sharding, reserve-then-confirm, and queue-based admission. Step 4's optimistic/pessimistic axis reappears as "one hot row vs N buckets." Catalog hits 8, 11, 9. This problem is where the family's honesty requirement bites hardest: there is no free lunch, and the answer is a stated trade among exactness, freshness, and synchrony (Section 6's closing line). **Recipe fits; Section 6 exists because of it.**

### 10.7 Double-booking prevention

Step 1 is unusually load-bearing here, which is why the problem is written to demand it: the single-JVM answer is one `compareAndSet` on the seat's holder reference (or one lock over the seat map), and a candidate who cannot produce that cold has not earned the distributed conversation. Step 3 relocates it to a unique constraint on `(resource, slot)`, toolkit item 4, a *natural* key, which is why this problem needs no separate idempotency key. Reserve-with-TTL comes from Section 6, and the reclaimer-vs-late-confirm race is the same conditional-write answer. Catalog hits 1, 11, 2 (if anyone proposes a lock for it). **Recipe fits, and the problem doubles as the explicit bridge from family 2, same invariant, same mechanic, new home for the linearization point.**

---

## 11. What the general framework leaves out

The 5-step framework and the seven-family taxonomy were built for one JVM, and they hold up better than you might expect, invariant, check-then-act, linearization point, claim-before-work, and the condition-loop discipline all survive translation intact. But five things are genuinely missing, and they are the things this family is graded on:

1. **No partial-failure step.** The framework's verification sweep asks about races, deadlock, lost wakeups, and starvation, all of which assume that operations either happen or don't. "The call timed out and I don't know whether it took effect" has no slot in the checklist, and it is the fact that generates half of this family. Step 5 needs an item: *what does a retry of this operation do?*
2. **Failure atomicity is assumed.** In one JVM, `finally` runs. The framework leans on that everywhere (release in `finally`, decrement in `finally`, mechanic 11's pending-counter discipline). Across hosts, the holder may never execute another instruction, so *every* claim needs an expiry, and every expiry needs an answer to "what if it fires while the work is still running?" That question has no single-JVM analogue at all.
3. **The mechanics list has no idempotency entry.** Mechanic 12 (claim-before-work) is the closest, and it is genuinely the same idea, but idempotency is the *superset*: naturally-idempotent operations, conditional writes, dedup keys, natural keys. It deserves its own numbered mechanic on par with the lightswitch and lazy derivation, because it is the single most-probed concept in senior system-design rounds.
4. **No contention-relief vocabulary at system scale.** The escalation ladder (mechanic 3) ends at "JDK structure." Its distributed continuation, atomic store operation → sharded counter → reserve-with-TTL → queue-based admission, is a real ladder with real trade-offs, and it isn't anywhere in the framework.
5. **Ordering is treated as a memory-model concern only.** Happens-before is presented as a visibility tool. Its distributed form, per-partition ordering, causality, read-your-writes, and the fact that you buy ordering per key and get none across keys, is the same concept doing much more visible work, and the framework never makes the connection.

None of this invalidates the framework; the translation table in Section 2 exists precisely because each row *is* a framework concept with a new address. The gap is that the framework stops at the process boundary, and every senior loop in this bank's target companies starts its hardest question by crossing it. This playbook is the chapter on the other side.

**And the one rule that outranks everything else in this document:** *answer the single-JVM version first, cold, then cross the boundary.* Candidates lose this family not by getting the distributed design wrong, but by never establishing that they could have solved it in one process. The distributed answer earns its credit only as a *relocation* of an answer you already gave.
