---
layout: post
title: Distributed Rate Limiter
date: 2026-07-19
description: >-
  Cap a client to N requests per window across a whole fleet with no shared memory. The single-node answer is a lock around a token bucket; the distributed one relocates its…
categories: interview multithreading problems
---

Part of the [Distributed Concurrency & Idempotency](/interview/multithreading/patterns/distributed-concurrency/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** [Design a distributed rate limiter](https://enginebogie.com/public/question/design-a-distributed-rate-limiter/364), senior system-design rounds. **High frequency.** The [multi-API and combined rate limiter](https://enginebogie.com/public/question/design-multi-api-and-combined-rate-limiter/4354) variant (different limits per endpoint, plus a global cap the sum must respect) is the natural follow-up once the single-limit version is solid.

### Problem

A client may make at most **N requests per window** (say 100/min). Your service runs on many app servers behind a load balancer, and a client's requests land on arbitrary servers. There is no shared memory between servers. Admit a request if the client is under its limit, reject (429) otherwise, and make the limit hold across the whole fleet, not per server.

### Constraints

- Correct under many app servers with no shared heap; requests for one client fan out across all of them.
- The check sits in the request hot path, so it must be fast, one round trip, not several.
- The rate store can be slow, unreachable, or partitioned, and you must have an answer for that.
- A single very popular client (a hot key) must not serialize the whole fleet behind one counter.

### Clarify before solving

- **Per-client, per-API, or global?** (Start one client, one limit. Multi-API = one counter per (client, route) plus a combined cap, the linked variant; say it, then scope down.)
- **Is an occasional overshoot acceptable, or is N a hard ceiling?** (*The* routing question. A little slop buys you cheap approximate designs; a hard cap forces the atomic-op-on-shared-store discipline.)
- **Fixed window, sliding window, or token bucket?** (Boundary bursts vs cost vs burst smoothing, decide deliberately, don't default.)
- **Single JVM first?** (Yes. In one process this is [the token-bucket limiter](/interview/multithreading/problems/rate-limiter-token-bucket/) behind one lock; say that before you say Redis.)

### Why this problem matters

It is the [token bucket](/interview/multithreading/problems/rate-limiter-token-bucket/) after the interviewer says the family's opening line, "now run it on fifty hosts." The single-node core is a solved problem; the whole difficulty is that the check-then-act race you already tamed with a lock is now a race *over the network*, and the lock has to move to a place all fifty hosts can see. Candidates who jump straight to "put a counter in Redis" have skipped the part being graded: naming the local linearization point and then relocating it.

---

## Strategy

### Classify

Guarded state plus a clock (Type B + time), relocated across the process boundary. NOT a semaphore problem, a semaphore caps **concurrency**; a rate limiter caps **frequency**, and time mints new permits. The distributed twist is that the guarded state no longer lives in a heap you can `synchronized` over; it lives in an external store, and the atomicity has to be borrowed from that store.

### Single-JVM answer first, cold

One token bucket (or a counter) behind one lock, or a CAS loop on an immutable `(tokens, lastRefill)` pair in an `AtomicReference`. **The linearization point is the lock release / the successful `compareAndSet`**, the instant the token is deducted. State it exactly like that. This is the whole of [the single-node problem](/interview/multithreading/problems/rate-limiter-token-bucket/); do not re-derive it here, just name it and its linearization point.

Now cross the boundary and watch it break. Run that same limiter *independently on each of k servers*, each admitting N/window locally, and the client gets **k × N** effective rate. Local correctness does not compose. Each node's linearization point is in its own heap, invisible to the others, so the fleet has k limits, not one. The fix is forced: **there must be one linearization point that every node shares.**

### Invariant

Across all nodes, admitted requests for a client in any window ≤ N. One counter, one arbiter, one atomic decision per request, wherever that decision physically happens.

### Relocate the linearization point to the store

Put the counter in a shared store (Redis, typically) and make the read-modify-write **one atomic operation at the store**, one round trip or one server-side script. This is the entire crux, so be precise about *why*:

- **`INCR` + `EXPIRE`.** `INCR key` returns the new count atomically; if it exceeds N, reject. Set the TTL to the window length on first touch so the counter self-cleans. The subtlety: `INCR` then a separate `EXPIRE` is **two** commands, and if the process dies between them the key never expires and the client is throttled forever. Set the expiry atomically (the `SET ... EX` / `NX` variants, or a script) so the counter and its lifetime are one write.
- **A Lua script for check-and-decrement.** The moment the decision is "read the count, compare to N, then conditionally write," a plain `GET` followed by `INCR` is **check-then-act with a network in the gap**, exactly the race this whole site is about, now stretched over a wire. Two concurrent requests both `GET` 99, both decide "under 100," both `INCR` to 100 and 101: overshoot. Redis runs a Lua script single-threaded and atomically, so the read, the compare, and the write are one indivisible server-side step. **The script is the relocated lock.**

Contrast the two flavors of atomicity out loud: local correctness came from a JVM monitor / CAS; distributed correctness comes from *a single server executing a script serially*.

### Fixed vs sliding vs token bucket, at the store

- **Fixed window** (`INCR` on a per-window key): cheapest, one counter. Flaw: **boundary burst.** A client can send N at 00:59 and N at 01:00 and pass both windows, 2N in a two-second span. Acceptable when the cap is soft.
- **Sliding-window log**: store a sorted set of request timestamps, drop those older than the window, count the rest. Exact, no boundary burst, but O(N) memory per client and heavier per request. Reach for it only when precision is required.
- **Sliding-window counter**: keep the current and previous fixed-window counts and weight the previous one by how far into the current window you are. Smooths the boundary at roughly fixed-window cost; the usual production compromise.
- **Token bucket in Redis**: a Lua script holding `tokens` and `last_refill_ts` in a hash, doing lazy refill (derive tokens from elapsed time), then conditional decrement, all in the one script. This is the single-node lazy-refill trick with the arithmetic executed *inside the store* instead of inside your JVM. Gives smooth rate plus a bounded burst.

### Failure modes unique to distribution

- **Store latency in the hot path.** Every admission is now a network round trip. Keep it to one; co-locate the store; consider a short local pre-check that only *short-circuits obvious rejects*, never grants alone.
- **The store is down.** A real decision, name it: **fail-open** (admit everything, protect availability, lose the limit, the usual choice for user-facing traffic where rate limiting is a guardrail not a gate) vs **fail-closed** (reject, protect the thing behind the limiter, correct when the limit guards a fragile or expensive downstream). There is no default; ask what the limit is *for*.
- **Clock skew across nodes.** Sliding windows compare timestamps, and two app servers' wall clocks differ by an unknown amount. Prefer timestamps assigned by the **store** (one clock) over each app server stamping its own; never let a distributed correctness decision hinge on comparing clocks across hosts.
- **Hot-key contention.** A popular client hammers one counter and it becomes a serialization point for the whole fleet. **Shard the counter** into m sub-counters (`key:0..m-1`), each request hits one at random, admit while the *sum* is under N. Throughput scales m×; the cost is that the sum is approximate near the limit and reads must fan out, the exactness-for-throughput trade this family always makes. State it.

### Pitfalls

1. Running the local limiter per node and calling it done, k nodes give k × N. The counter must be shared.
2. `GET`-then-`INCR` as two round trips, check-then-act over the network; both requests pass at the boundary. One atomic op or one script.
3. `INCR` without an atomic `EXPIRE`, a leaked key throttles the client forever; the count and its TTL must be one write.
4. Fixed window when the requirement is a hard ceiling, the boundary burst delivers 2N. Use sliding-window counter or log.
5. Each app server stamping sliding-window timestamps with its own clock, skew corrupts the window. Stamp at the store.
6. No answer for "Redis is down", fail-open vs fail-closed is a required decision, not an afterthought.
7. One hot counter for a whale client, sharded/approximate counting is the escape, at a stated cost to exactness.

### Check your understanding

1. Give the single-JVM limiter and its linearization point in one sentence, then say precisely why running it per node breaks the fleet-wide limit.
2. Where exactly is the overshoot if you implement the check as `GET` then `INCR`? Which two requests, at which counts?
3. Why must the `INCR` and the `EXPIRE` be one atomic write? Construct the leak when they aren't.
4. Fixed vs sliding-window counter vs sliding-window log, one line each on the trade you're making.
5. Redis is unreachable for 10 seconds. Argue fail-open, then fail-closed, and say what fact about the limit's purpose decides it.
6. A single client sends 40% of all traffic. What breaks, and what does sharding the counter cost you in exchange for fixing it?

### Transfers to

[Token bucket](/interview/multithreading/problems/rate-limiter-token-bucket/) (the single-node core this relocates); [distributed lock and lease](/interview/multithreading/problems/distributed-lock-and-lease/) (same atomic-op-on-a-shared-store discipline, and the Lua-script-as-lock idea); idempotency keys (atomic check-and-set on a shared store); optimistic concurrency control (the network check-then-act, guarded by a conditional write); flash-sale inventory (a shared decrementing counter under contention, sharded when hot). The unifying line: relocate the linearization point to a store that offers one atomic operation, and let that operation be the lock.
