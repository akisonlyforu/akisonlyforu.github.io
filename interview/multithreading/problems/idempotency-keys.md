---
layout: post
title: Idempotency Keys
date: 2026-07-19
description: >-
  This is the flagship question of the distributed-concurrency family and the one most likely to open a senior round at a payments company. It is also the purest test of…
categories: interview multithreading problems
---

Part of the [Distributed Concurrency](/interview/multithreading/patterns/distributed-concurrency/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Senior system-design and API-design rounds — Stripe, Coinbase, Uber L5+, AWS L6. **Very High frequency.** Stripe's canonical probe: *"how does the system prevent the same order being processed twice?"*

### Problem

Design an API endpoint that performs a non-repeatable side effect — charge a card, place an order, transfer funds — such that a client which retries the same request (because it timed out, or because the user double-clicked, or because a proxy replayed it) causes the effect to happen **exactly once**.

The client supplies an **idempotency key** with each request: an opaque, client-generated identifier representing one logical intent. Every retry of that intent carries the same key. The service is horizontally scaled across many hosts; consecutive retries may land on different hosts, and two retries may be in flight simultaneously.

### Constraints

- Multiple application instances; no shared memory between them.
- A retry may arrive while the original is still executing.
- A client cannot distinguish "the request failed" from "the response was lost" — assume it retries in both cases.
- The retry may arrive seconds later or days later.
- The stored dedup record must not grow without bound.

### Clarify before solving

- **Who generates the key, and per what?** (Per *intent*, by the client, reused across all retries. A key generated per *attempt* dedups nothing — name this trap unprompted.)
- **What is the key scoped to?** (Per API caller / account at minimum. A globally-scoped key space lets two customers collide, or lets one customer probe another's results.)
- **What should a duplicate that arrives mid-flight receive?** (The interesting case. Options: block and wait, return 409 with a retry-after, or return a "processing" resource. Each has a defensible answer; committing to one and justifying it is the point.)
- **How long must the key be honoured?** (Sized to the maximum realistic retry horizon — not to what's convenient for storage.)
- **What if the same key arrives with a different request body?** (Reject. This is a client bug and silently serving the cached response hides it.)
- **Is the underlying operation naturally idempotent already?** (If it can be expressed as an absolute end state, you may need none of this. Ask first.)
- **Single JVM or many?** (Answer the single-JVM version first — see below.)

### Why this problem matters

This is the flagship question of the distributed-concurrency family and the one most likely to open a senior round at a payments company. It is also the purest test of whether a candidate's concurrency instincts survive the process boundary: the naive answer — *"look up the key; if it's not there, insert it and do the work"* — is **check-then-act**, the very first disease in the refresher, with a network round trip in the gap instead of a thread switch. Two simultaneous retries both look, both find nothing, both charge the card.

The fix is the one you already know: make the check and the act a single atomic operation. In one JVM that is `putIfAbsent` and the returned boolean is the linearization point of ownership. Across hosts it is a **unique constraint on an insert**, and the constraint violation is the boolean. Saying that sentence — *the unique index is the linearization point* — is the highest-value moment available in this family. Say the single-JVM version first, then relocate it.

---

## Strategy

### Classify

Claim-before-work (refresher mechanic 12), relocated across the process boundary. It is *not* a locking problem and it is *not* a caching problem — it is deduplication of ownership, and the mechanic is identical to the concurrent-crawler's visited-set: one atomic operation whose boolean return decides who owns the work.

**Say the single-JVM answer first, cold.** In one process this is a `ConcurrentHashMap` from key to request-state, and `putIfAbsent` is the whole design: whoever's put returns null owns the execution; everyone else is a duplicate. The linearization point is that put. Now cross the boundary: there is no shared map, so the atomic operation must be borrowed from a system all instances can see — and the one every relational database offers for free is a **unique constraint**.

### Invariant

For each `(caller, idempotency key)` pair, the side effect executes **at most once**, and every request bearing that pair eventually observes the *same* outcome — the outcome of that single execution.

Note the two halves. "At most once" is the safety property and comes from the constraint. "Every request observes the same outcome" is the reason you must *store the response*, not merely record that the key was used — otherwise the first caller gets a charge ID and the retrying caller gets nothing useful, and from the client's perspective the API is non-deterministic.

### Mental model

A **request-record table** keyed uniquely on `(scope, key)`. Each record carries a state, a fingerprint of the request body, and — once finished — the stored response.

The record has two meaningful states:

- **IN_PROGRESS** — someone has claimed this key and is executing. Written *before* the work begins.
- **COMPLETED** — the work finished; the response is stored alongside.

The critical design decision is that the record is inserted **before** the side effect, not after. Inserting after would leave a window in which the work has been done but no evidence exists, and a retry landing in that window duplicates the charge — this is the **claim-after-work** bug (refresher failure 14) with a network in the gap. Claim first, work second.

The second critical decision: **the insert is the check.** Do not read the table to see whether the key exists. Attempt the insert unconditionally and let the database's unique index arbitrate. If the insert succeeds, you are the owner. If it raises a constraint violation, someone else is or was the owner, and you switch to the duplicate-handling path. The database's choice of which concurrent insert wins is the linearization point of the entire request, and it is the only place in the design where mutual exclusion actually happens.

### Design reasoning

**Handling a duplicate depends on the existing record's state.**

- If the existing record is **COMPLETED**, return its stored response verbatim — same status code, same body. From the client's perspective the retry succeeded, which is exactly what you want; it has no way to tell (and no reason to care) that it was served from a record. Worth stating explicitly: this is why the response must be *stored*, not recomputed.
- If the existing record is **IN_PROGRESS**, the original is still running and no answer exists yet. You have three honest options, and the interviewer wants you to pick one and defend it. **Return a conflict status with a retry-after hint** is the usual production choice: it is cheap, it holds no connection open, and it tells the client the truth ("this is being processed; ask again shortly"). **Blocking until the original completes** gives the caller a nicer experience but ties up a request thread on a long operation and can cascade into thread-pool exhaustion under retry storms — the distributed echo of "never block holding a resource others need." **Returning a 202 with a status resource** is the most correct for genuinely long operations but changes the API contract. What you must *not* do is treat in-progress as "not done, so do it again" — that reintroduces the double charge, and it is the single most common wrong answer.
- If the existing record's **fingerprint differs** from the current request body, the client has reused a key for a different intent. Reject with an unprocessable-entity error rather than serving the old response. Silently returning the first response would mean the client's second, genuinely different request vanished — a data-loss bug that is almost impossible to diagnose from the outside.

**What if the owner dies mid-flight?** The record sits in IN_PROGRESS forever and the key is permanently poisoned — every retry gets a conflict and the intent can never complete. This is failure atomicity: in one JVM a `finally` block would clean up; across hosts the process may never run another instruction. So the IN_PROGRESS state needs a **lease**: a claimed-at timestamp and an expiry, after which another instance may take the record over. And now you have inherited the entire lease problem — a "stale" record may belong to a holder that is merely paused, not dead, so the takeover must itself be a conditional write (take over *only if* the record still shows the expiry you observed), and the underlying side effect ideally carries its own downstream idempotency so that even a genuine double-execution is absorbed. Notice how the requirement propagates: **the safest systems are the ones where idempotency exists at every layer, not just the edge.**

**Key scoping.** Scope the uniqueness to `(API caller or account, key)`, not to the key alone. Global scoping creates two problems: two customers can collide on a UUID-free key format, and a caller who guesses another's key can observe a stored response. Some designs add the endpoint to the scope, which prevents a client from reusing one key across two different operations; the fingerprint check covers much of the same ground, so this is a judgment call worth mentioning rather than a rule.

**Expiry.** Records must be reaped or storage grows forever, but the retention window is a **correctness** parameter, not a storage one: once the record is gone, the next retry with that key is treated as brand new and the effect happens again. Size it to the longest retry horizon you actually have — automated retries measured in minutes, but human-driven "did that go through? let me try again" measured in hours, and support-driven replays measured in days. A window of 24 hours or more is common in payments. Undersizing this is subtle because it fails only for the slow retries, which are rare, which means it fails in production and never in test.

### Trade-offs

- **Storing the full response vs storing only the resource ID.** Storing the response is fully faithful for the retrying client but bloats the table with payloads. Storing an ID and re-reading the resource is compact but can return a *later* state of the resource rather than the response the first caller got — usually acceptable, occasionally not (if the response contained a one-time token, it is not).
- **Database table vs a fast key-value store.** The relational table gives you the unique constraint and can participate in the *same transaction* as the business write — which is a real advantage, because it makes "record the key" and "do the work" atomic together. A key-value store with an atomic set-if-absent is faster and cheaper but sits outside that transaction, reopening a window where one commits and the other doesn't. If the work is a database write, keep the key in the same database.
- **Client-supplied key vs server-derived natural key.** If the domain already has a uniqueness rule — one payment per invoice per day, one booking per (resource, slot) — a unique constraint on those natural columns dedups with no extra table and no expiry policy at all, and the constraint that prevents duplicates is the same constraint that expresses the business rule. Prefer that when it exists. Client keys are the general fallback for operations with no natural key.
- **Strictness vs client friendliness on fingerprint mismatch.** Rejecting is correct and surfaces client bugs; some APIs choose to be lenient. Say which you'd pick and why.

### Pitfalls

1. **Read-then-insert** — the check-then-act disease one layer down. Two concurrent retries both read "absent," both insert, both charge. The insert must *be* the check; the unique-violation branch is half the algorithm, not error handling.
2. **Recording the key after the work** — a retry landing in the window duplicates the effect. Claim before working, always.
3. **Treating IN_PROGRESS as "go ahead and run it"** — the double charge, restored. In-progress is a distinct answer, not an absence.
4. **Key generated per attempt rather than per intent** — every retry brings a fresh key, the table fills with singletons, and dedup rate is exactly zero. This is a client-side contract failure and worth calling out in the API docs, not just the code.
5. **Dedup window smaller than the retry horizon** — the late retry (human or DLQ replay) sails through as new. Size the window to the longest replay you will ever see.
6. **Globally scoped keys** — cross-tenant collisions and cross-tenant response leakage.
7. **No lease on IN_PROGRESS** — a crashed owner poisons the key forever; every retry gets a conflict and the intent can never complete.
8. **Assuming the downstream is idempotent because you are** — your key protects your endpoint; the payment processor needs its own idempotency key from you, or a retry inside your handler duplicates the charge below your dedup layer.

### Check your understanding

1. Give the single-JVM answer in one sentence, and name the linearization point. Then name what replaces it across hosts.
2. Two retries of the same request arrive simultaneously on two different hosts. Walk the interleaving under "read then insert" and show exactly where the second charge happens.
3. Why must the response be stored rather than just the fact that the key was used? What breaks for the retrying client otherwise?
4. A duplicate arrives while the original is still executing. Name the three possible responses and defend one; then say what happens if you instead treat it as new.
5. The owning host is killed mid-execution. What state is the record in, what happens to every subsequent retry, and what mechanism fixes it — and what new problem does that mechanism introduce?
6. Why is the dedup retention window a correctness parameter rather than a storage-cost parameter?
7. When can you skip the idempotency-key table entirely?

### Transfers to

Every write API at a payments or booking company; the dedup half of exactly-once processing (same claim, applied to messages instead of requests); double-booking prevention (same constraint, on a natural key instead of a client key); concurrent-crawler visited-sets and any claim-before-work design in a single JVM; and webhook receivers, which are the same problem with the roles reversed.
