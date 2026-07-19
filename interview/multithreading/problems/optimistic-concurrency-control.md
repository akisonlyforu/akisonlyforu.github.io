---
layout: post
title: Optimistic Concurrency Control
date: 2026-07-19
description: >-
  This is the distributed form of compare-and-set, and it is asked so directly at AWS L6 that the expected phrase — *"put the version in the WHERE clause and retry on…
categories: interview multithreading problems
---

Part of the [Distributed Concurrency](/interview/multithreading/patterns/distributed-concurrency/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Senior backend and system-design rounds — AWS L6 (explicitly probes "version in the WHERE clause, retry on conflict"), Stripe, Airbnb, Rubrik. **Very High frequency.**

### Problem

Many application instances read the same record, compute a new value from it, and write it back. A user's profile, an account balance, a document, a job's status. Two instances that read the same starting value and both write back will produce a **lost update**: the second write silently erases the first, and neither party is told.

Design the update path so that a write which was computed from stale data cannot commit. Do it **without holding a lock across the read-compute-write interval**, since that interval may include user think-time or a slow computation and spans a network.

### Constraints

- Horizontally scaled application tier; no shared memory.
- The read and the write are separate round trips, possibly separated by seconds.
- Conflicts are expected to be rare in the normal case but can spike (a hot record).
- Lost updates are unacceptable; a rejected update is acceptable if the caller learns about it.

### Clarify before solving

- **What is the conflict rate?** (The single most important question — it decides optimistic vs pessimistic, and it is a measurement, not an opinion.)
- **Is the update a full replacement or a delta?** (A commutative delta — "add 100" — may not need OCC at all; an in-store atomic increment is exact and conflict-free. OCC is for read-modify-write, where the new value *depends on* the old.)
- **On conflict, retry silently or surface to the user?** (Retrying a machine-computed transform is right; retrying a *human's* edit silently is wrong — you'd be re-applying their intent to a document they never saw.)
- **How many retries, and with what backoff?** (Bounded, jittered. Say it unprompted.)
- **Does anything else write this record?** (A batch job or a migration that bypasses the version discipline defeats the whole scheme.)
- **Single JVM first?** (Yes — this is a CAS loop. Say that before you say "version column.")

### Why this problem matters

This is the distributed form of compare-and-set, and it is asked so directly at AWS L6 that the expected phrase — *"put the version in the WHERE clause and retry on conflict"* — is nearly a password. But the phrase alone is a shallow pass. What separates a senior answer is knowing **why** it works (the update statement's condition is the compare and the row-count is the boolean return), **when it stops working** (under contention it degrades into a retry livelock and pessimistic locking becomes *cheaper*), and **why a version rather than the value itself** — which is the ABA problem, arriving from single-JVM atomics with its name unchanged.

It is also the mechanism that quietly underpins half of this family: fencing tokens are OCC pointed at a lock, event-version dedup in stream consumers is OCC pointed at a message, and conditional writes are how you make a replay harmless. Learn it once, apply it five times.

---

## Strategy

### Classify

Compare-and-set, relocated across the process boundary. Everything you know about CAS applies without modification: it is a hardware check-then-act made atomic by the hardware, and this is the same check-then-act made atomic by the storage engine.

**Single-JVM answer first, cold:** an `AtomicReference` holding an immutable snapshot, updated by a retry loop — read the snapshot, compute the successor, `compareAndSet`, retry if it returns false. The linearization point is the successful CAS. Now cross the boundary: the compare has to happen where the data lives, so it moves into the write statement's condition, and the "did it succeed" boolean becomes the count of rows the statement affected.

### Invariant

A write commits **only if** the state it was computed from is still the current state. Equivalently: every committed version is derived from its immediate predecessor, so the version history is a chain with no forks and no silently discarded links.

The lost update is precisely a fork in that chain — two versions both derived from version 5, one of which vanishes.

### Mental model

Give the record a **version**: a counter incremented on every write. A reader takes the value *and* its version. The writer's update statement carries two things it did not carry before: it sets the version to the successor, and it **conditions the whole update on the version still being the one that was read**.

If nobody wrote in between, the condition matches, one row changes, and the caller learns it won from the affected-row count. If someone did write, the version has moved, the condition matches nothing, **zero rows change**, and the caller learns it lost — from the same row count, at no extra cost and with no extra query.

That row count is the entire mechanism. It is the boolean that `compareAndSet` returns. Candidates who write the conditional update but then don't *check the row count* have built a design that silently drops every conflicting update instead of retrying it — which is a worse lost update than the one they set out to fix, because now it is invisible on both sides.

**The compute must be pure and must happen outside.** Read, compute, attempt. If the computation has side effects, a losing attempt has already caused them and the retry causes them again. Keep everything with an external effect after the successful commit, or make it idempotent.

**The retry loop, and its bounds.** On conflict, re-read (getting the *new* version and the *new* value), recompute from that fresh state, and attempt again. Recomputing is essential — retrying the same computed value would just be re-applying a decision made on stale data, which defeats the point. The loop must be **bounded**: unbounded retries under contention consume connections and CPU while making no progress, and a caller waiting on an unbounded loop has no latency guarantee. When the bound is hit, surface the conflict rather than hiding it.

**Backoff with jitter.** Two contenders that both retry immediately will collide again; two that both retry after exactly 50ms will *also* collide again, forever, in lockstep. This is livelock, and it is the same livelock as the single-JVM `tryLock`-without-backoff bug (refresher: "randomized backoff desynchronizes"). Full jitter — a random delay in [0, cap] rather than a fixed one — is what actually breaks the symmetry.

### Design reasoning

**Why a version rather than comparing the old value?** Because comparing the value is vulnerable to **ABA**: the record was A, someone changed it to B and back to A, and your comparison sees A and concludes nothing happened. In a single JVM this is the classic atomics footnote and the fix is a versioned stamp. Here it is not a footnote — round trips are long, so the window in which a value can change and change back is enormous, and business flows routinely return values to previous states (a status going pending → active → pending; a balance going 100 → 150 → 100 after a refund). A monotonic version cannot go back, so it detects the round trip that value-comparison misses. A last-modified *timestamp* is the tempting substitute and is a bad one for two reasons: clocks across writers are not comparable, and two writes in the same clock tick are indistinguishable. Use a counter.

**When OCC wins.** When conflicts are rare, OCC is close to free: no lock is acquired, nothing is held across the network round trip, readers never block, and the failure path costs one wasted round trip that almost never happens. It is also the only workable option when the read-modify-write interval includes something you must not hold a lock across — human think-time in an edit form, a call to an external service, a long computation. This is the standard shape of the "edit and save" web flow, and the reason ETags and if-match headers exist: HTTP conditional requests are OCC at the protocol layer.

**When OCC collapses.** As contention rises, the fraction of attempts that lose rises with it, and the work spent on losing attempts is pure waste that *itself* adds load, which raises latency, which widens the read-to-write window, which raises the conflict rate further. This positive feedback loop means OCC does not degrade gracefully — it falls off a cliff. On the hot record in a flash sale, a design where every attempt retries three times is generating four times the load to do the same work. Past that point a short pessimistic lock is genuinely *cheaper*: contenders queue instead of spinning, and total work is linear in requests rather than superlinear. The senior framing: **optimistic concurrency is a bet that conflicts are rare, and like any bet it has a break-even point.** State that you'd start optimistic, measure the conflict rate, and switch for the specific hot keys that cross it — not globally.

**Where the version lives.** A single version column on the row covers invariants confined to that row. If the invariant spans several rows, a per-row version does not protect it: each row's individual update can succeed while the combination violates the rule. That is write skew, and it belongs to the pessimistic-locking-and-isolation problem — but knowing to *check whether the invariant fits inside one row* before choosing OCC is the step that keeps you out of that trap.

**OCC as idempotency.** A retried duplicate of an already-applied conditional update finds the version already advanced, matches nothing, and does nothing. So OCC gives you replay-safety for free — worth saying, because it means for read-modify-write flows you may not need a separate idempotency key at all.

### Trade-offs

- **Optimistic vs pessimistic**: no locks and no blocking, at the cost of wasted work under contention and a caller-visible conflict outcome — versus queuing and guaranteed progress per attempt, at the cost of holding a lock across a round trip and the deadlock/timeout machinery that comes with it. Contention rate is the deciding variable.
- **Silent retry vs surfacing the conflict**: retrying is right for a machine-derived transform (recompute and reapply — the user never needed to know). For a human's edit it is wrong: re-applying their intent onto a document they never saw can destroy the other person's change just as thoroughly as a lost update. For human edits, surface it — "this changed while you were editing," ideally with a merge.
- **Version column vs full-row comparison vs ETag**: the column is cheapest and immune to ABA; comparing every field is unnecessary work and still catches only value changes; an ETag is the same idea exposed over HTTP and is the right answer when the client is a browser.
- **Bounded retries**: a low bound surfaces transient conflicts to users unnecessarily; a high bound hides real contention and burns capacity. Pick a small number, and *alarm on the conflict rate* so contention becomes visible instead of silently expensive.

### Pitfalls

1. **Not checking the affected-row count** — the update is conditional but nobody looks at whether it applied. Conflicting writes are dropped silently: a worse lost update than the original, and invisible from both ends.
2. **Retrying without re-reading** — re-attempting the same computed value re-applies a stale decision. The retry must recompute from the fresh state.
3. **Unbounded retries** — under contention this is livelock plus a self-inflicted load amplifier. Bound them and surface the failure.
4. **Fixed backoff, or none** — contenders re-collide in lockstep. Full jitter.
5. **Timestamp instead of a version counter** — clock skew across writers and same-tick collisions. Also, ABA-vulnerable if the timestamp is the *record's* last-modified and something rewrites the same value.
6. **Forgetting to increment the version** — the condition matches forever and OCC silently becomes no protection at all. A trigger or an ORM's built-in versioning is more reliable than remembering.
7. **Side effects inside the compute** — losing attempts have already fired them; the retry fires them again.
8. **A bulk job that bypasses the version discipline** — one writer ignoring the protocol invalidates it for everyone. The rule has to hold for *all* writers, including migrations and admin tools.
9. **Assuming per-row versions protect a multi-row invariant** — they don't. That's write skew, and it needs a different mechanism.

### Check your understanding

1. Give the single-JVM answer in one sentence and name its linearization point. What is the linearization point in the distributed version?
2. What exactly is the boolean that `compareAndSet` returns, in the database version? What goes wrong if you don't read it?
3. Explain the ABA problem in this setting with a concrete business example, and say why a version counter fixes it and a value comparison doesn't.
4. Describe the feedback loop that makes OCC degrade non-gracefully under contention. At what point does a row lock become cheaper?
5. Why must the retry re-read rather than re-attempt the same value?
6. Two contenders retry with identical fixed backoff. What is the failure mode called, and what fixes it?
7. When should a conflict be retried silently and when must it be shown to the user?
8. Why does OCC give you idempotency for free, and what class of operation does that *not* cover?

### Transfers to

Fencing tokens (the same conditional write, guarding a lock); conditional writes in object stores and key-value stores (if-match, compare-and-swap on an item version); event-version dedup in stream consumers; HTTP ETags and if-match; single-JVM CAS loops on immutable snapshots — including the token bucket's `(tokens, timestamp)` pair, which is the same "read snapshot, compute successor, swap atomically" shape.
