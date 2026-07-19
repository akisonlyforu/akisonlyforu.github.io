---
layout: post
title: Connection Pool
date: 2026-07-19
description: >-
  It is the most honest test of whether "bounded resource" is a pattern you *own* or a phrase you have heard, because the coordination is easy and everything that actually…
categories: interview multithreading problems
---

Part of the [Bounded Resource](/interview/multithreading/patterns/bounded-resource/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** A perennial LLD/production-code ask ("implement a connection pool / object pool"). Frequency claim, hedged: **high wherever the round is "write a component we could ship", and it is one of the few problems that appears in both LLD and system-design rounds**, in the latter as "how does your service talk to the database?", where the correct answer is a configured HikariCP, not a hand-built pool.

### Problem

`ConnectionPool(int maxSize, Supplier<Connection> factory)` with `Connection borrow()` / `void release(Connection c)`. At most `maxSize` connections exist. A caller that finds none available waits (or times out, or is rejected, you decide, having asked). Connections are validated before being handed out; broken ones are discarded and replaced. Called concurrently from many request threads.

### Constraints

- Hard cap: the pool must never create more than `maxSize` connections, even under a burst of concurrent borrows.
- A borrowed connection belongs to exactly one caller until it is returned.
- Connection creation is expensive and can fail; validation is cheap-ish but not free.
- The pool must survive callers that throw, callers that forget to return, and shutdown while connections are outstanding.

### Clarify before solving

- **Wait policy on exhaustion**: block indefinitely, block with a timeout, or fail fast? (Timed acquisition is the production default, an unbounded wait turns a slow database into a hung service. Ask, then pick timeout and say why.)
- **Eager or lazy creation**: fill the pool at construction, or create on demand up to the cap? (Lazy + cap is usual; eager warms latency but slows startup and can fail at boot.)
- **Validation**: on borrow, on return, or on idle? What is the test (a ping query, a socket check)? How stale is too stale?
- **Fairness**: FIFO for waiters, or is barging acceptable? (Barging is the default and it means a caller can starve, name it.)
- **Leak policy**: is "never returned" a bug we detect and log, a bug we recover from by reclaiming, or an unbounded outage?
- **Max lifetime / idle eviction**: do connections get retired on age? (Real pools do; databases and load balancers kill long-lived connections underneath you.)
- **Shutdown**: wait for outstanding borrows, or close everything now and let borrowers fail?

### Why this problem matters

It is the most honest test of whether "bounded resource" is a pattern you *own* or a phrase you have heard, because the coordination is easy and everything that actually breaks pools is in the parts candidates skip: the return path, the exception path, and the shutdown path. Almost every real pool outage is one of three things, a caller that never returned a connection, a validation or creation failure that lost a permit, or an unbounded wait that turned a degraded dependency into a hung service, and none of them are visible in the happy path you write in the first five minutes.

It is also where the **borrow/return invariant** shows its teeth: a semaphore's permits are only meaningful if every acquire is matched by exactly one release, and a pool is a machine for making that matching go wrong. Getting `finally` right, and getting the accounting right when the thing you took turns out to be broken, is the whole exercise.

---

## Strategy

### Classify

Bounded resource, Type C, in its purest multiplex form: **a `Semaphore(maxSize)` that says how many callers may be inside, plus a queue holding the actual connections.** Say that decomposition first, it is the classification, and it also tells you why the two halves are separate. The semaphore counts *permission to hold a connection*; the queue holds *objects*. They are not the same thing, and keeping them distinct is what lets you handle a connection that turns out to be broken (you keep the permit, you replace the object).

This is Expression C from the family PATTERN, counting semaphore plus a structure guarded by a short critical section, and the family's ordering rule applies verbatim: **acquire the counting semaphore before taking the structural lock**, never the other way round.

Unlike the blocking queue, only one wait condition survives: nobody ever waits for the pool to have *space* (returning a connection never blocks). This is the degenerate one-predicate case the family recipe names, the seesaw with one side removed.

### Invariant

At all times: `(connections idle in the pool) + (connections currently borrowed) ≤ maxSize`; every connection object is held by at most one borrower; every successful `borrow()` is matched by exactly one `release()` of that same connection; and no connection is handed out that failed validation.

The linearization point of a borrow is the successful `tryAcquire`/`acquire` on the permit, that is the instant "a slot is mine". Taking the object out of the idle queue afterwards cannot fail for lack of capacity, because the permit already guaranteed a slot exists.

### Mental model

A tool-hire counter with a fixed number of hire tickets. You take a ticket (the permit) before you go to the racks; the ticket, not the rack, is what limits how many people can be using tools. At the rack you either take a tool that's there or the counter makes you a new one, either way you already had permission. When you bring the tool back you hand back the ticket, and if the tool came back broken the counter bins it, keeps your ticket for the next person, and makes a fresh one on demand.

The failure the shop actually suffers is nobody stealing tools, it is customers who wander off with the *ticket*. Then the counter is empty of tickets while the racks are full of tools, and every new customer waits forever on a shop that has everything it needs.

### Design

**Permit first, then object.** `borrow()`: acquire a permit (with the chosen policy, see below); then, under a short lock (or via a `ConcurrentLinkedQueue`, since the permit already enforces the bound), poll an idle connection. If none is idle, create one via the factory. Validate. Return it to the caller.

Note the ordering consequence: **the permit count, not the queue's contents, is the bound.** A caller holding a permit but finding the idle queue empty is not an error, it means every existing connection is borrowed and there is room to make one more. This is why lazy creation is free in this design.

**Creation and validation happen outside the pool lock.** Both are slow (a TCP connect, a round-trip ping). Holding the structural lock across them serialises the pool exactly when it is under pressure, the same rule as claiming a task under the scheduler's lock and running it outside, and the same rule as building a `FutureTask` inside `computeIfAbsent` but running it outside. Take the candidate out under the lock; validate with no lock held; then hand over or discard-and-retry.

**Validation on borrow, and the accounting when it fails.** If the candidate fails its liveness test: close it, do **not** release the permit, loop and create a replacement. The permit represents your slot; the broken object was a tenant of that slot, not the slot itself. Getting this backwards, releasing the permit on a failed validation and then re-acquiring, is a correctness bug (transient over-admission) and a liveness bug (you can lose the slot to a barging caller and end up waiting despite having been in). Bound the replacement loop; if creation itself keeps failing, release the permit and propagate the failure rather than spinning.

Validation cost is real: a ping per borrow doubles the round-trips on short queries. The production compromise is *validate only if idle longer than X*, a lazy freshness check, structurally identical to the cache's `now < expiresAt` comparison, and cheap for the same reason (a pure comparison against a self-contained timestamp needs no lock).

**Release.** Return the object to the idle queue (or close it if the pool is shut down, or if the connection is past its max lifetime, or if the caller marked it broken), then release the permit. Order matters: make the object available *before* the permit, or a woken waiter can find the queue empty and be forced into an unnecessary creation. Not a correctness bug, the permit still bounds you, but a needless connection churn worth naming.

### The policy axis, one more time

Same knob as everywhere in this family, and here it has direct operational consequences:

- **Block**: `acquire()`. Simple, and a hung database becomes a hung service: every request thread parks in the pool and the process stops answering health checks. Almost never what you want at the edge.
- **Timeout**: `tryAcquire(t)`. The production default. A saturated pool degrades into fast, visible failures instead of a silent hang. Two footguns the family PATTERN already names: on expiry you must not leave a half-taken permit behind, and a timed acquire that expires *simultaneously with* a release must not lose that release (the JDK semaphore handles this; a hand-rolled condition loop must re-check the predicate one final time under the lock before giving up).
- **Fail fast**: `tryAcquire()` with no wait. Correct when the caller has a cheap fallback (a cache, a degraded response).
- **Reject by state**: after `shutdown()`, borrow throws regardless of available permits. A lifecycle refusal, not a capacity one; the two coexist on the same method.

Also: nonfair semaphores **barge**, a fresh caller can take the permit a parked waiter was just signalled for. Under sustained saturation an unlucky caller can starve. Ship nonfair (throughput), *name the starvation*, and offer fair mode as the one-line fix with its throughput cost. "Ship a default, name who starves" is the family's standing instruction.

### Leaks: the thing that actually takes pools down

A caller that borrows and never returns permanently destroys one permit. Do that `maxSize` times and the pool is dead with no exception ever thrown anywhere: every subsequent borrow blocks or times out, and the stack traces all point at innocent callers waiting, never at the guilty one that leaked. **This is why pool exhaustion is so hard to debug in production, and saying so is the senior beat in this problem.**

Defences, in order:

1. **Return in `finally`.** Non-negotiable. The exception path is the leak path, a caller that throws mid-query and returns nothing is the canonical case.
2. **Make the correct thing automatic.** Hand back a wrapper whose `close()` returns to the pool, so try-with-resources does the right thing by construction. An API that requires discipline will eventually meet someone without any.
3. **Leak detection.** Record borrow time (and optionally the borrower's stack) per outstanding connection; a periodic sweeper logs anything held longer than a threshold. This is the **one place in these families where a background thread genuinely earns its keep**: the requirement is "something must happen at a future moment even though no caller is asking", which is the wait-until branch, not the derive-on-read branch. Compare with the rate limiter, where a background refiller was rejected precisely because there was always a caller to derive on. Same taxonomy, opposite answer, and being able to say *why* they differ is the point.
4. **Reclaim on timeout**: forcibly take a connection back from an over-long holder. Dangerous (the holder may still be using it) and rarely correct; mention as an option you'd decline.

### Shutdown

Reject new borrows first, under the same coordination as the borrow path, otherwise a borrow slips through during the transition. Then either drain gracefully (wait, bounded, for outstanding connections to come home; close idle ones as they arrive) or close everything immediately and let in-flight users fail. Both need a decision about connections that come back *after* shutdown: close them on release rather than returning them to the idle set. And the `finally` releases still have to work correctly against a shut-down pool, a release that throws because the pool is closed is a leak in the shutdown path.

### Health checking without holding the pool lock

If you add periodic idle-connection health checks, the sweeper must play by the same rules as a borrower: acquire a permit, take a candidate out, test it **outside** the lock, then return or replace it. A sweeper that iterates the idle collection while holding the pool lock and pings each connection blocks every borrower for the duration of N network round-trips, a self-inflicted outage that only manifests when the network is already slow, which is exactly when you least want it.

### Production equivalent

**HikariCP** is the answer for JDBC (and it is fast largely because it is ruthless about doing almost nothing on the borrow path); Apache Commons Pool 2 is the generic object-pool; Netty and every HTTP client ship a connection pool. In a design round: *"I'd use HikariCP and size it from the database's connection limit and the service's concurrency, pools are one of the few places where the library is meaningfully better than what I'd write."* Hand-build only when implementation is the explicit question. The sizing conversation is worth one sentence too: pool size is bounded by what the *database* can serve, not by your thread count, and an oversized pool moves the queue from your process to the database, where it is harder to see.

### Pitfalls

1. **Release outside `finally`**: the exception path leaks a permit; N exceptions later the pool is dead.
2. **Releasing the permit when validation fails**: transient over-admission, plus you can lose your slot to a barger.
3. **Creating or validating under the pool lock**: the pool serialises exactly when it is contended.
4. **Structural lock before the counting acquire**: the family's F4 deadlock: you hold the lock everyone needs in order to release the permit you are waiting for.
5. **Unbounded `borrow()`**: a degraded database becomes a hung service; every thread parked in the pool.
6. **Double release**: releasing the same connection twice mints a phantom permit and breaks the cap. Guard with an ownership check or a one-shot wrapper.
7. **Counting the idle queue as the bound** ("if queue is empty, create one"), check-then-act with no cap; a burst of concurrent borrows on a cold pool creates far more connections than `maxSize`.
8. **No max lifetime**: connections silently killed by a firewall or the database look alive locally and fail on first use; validation-on-borrow is what catches this, which is why "validation is optional" is wrong.
9. **Shutdown that leaves borrowers blocked forever**: waiters parked on a semaphore do not re-read your `closed` flag; you must poke them (release sentinel permits or interrupt) exactly as with poison pills for a worker pool.

### Check your understanding

1. Why are the permit and the connection object separate concepts? Give the concrete scenario where conflating them produces a wrong answer.
2. Trace `maxSize` concurrent borrows on a cold, empty pool. Where exactly is the cap enforced, and why is the idle queue being empty not a problem?
3. Validation fails on a borrowed candidate. Say precisely what happens to the object, the permit, and the loop, and what goes wrong under each of the two plausible alternative answers.
4. Construct the deadlock that arises from taking the pool's structural lock before acquiring a permit.
5. A service reports "all requests timing out after 30s, no errors in the database logs." Explain the leak mechanism, why the stack traces are all innocent, and what instrumentation would have named the guilty caller.

### Transfers to

Any object pool (threads, buffers, sessions, HTTP connections, PTY handles), semaphore-based admission control, license/seat management, and the checkout-with-timeout shape generally. The borrow/return-in-`finally` discipline and the permit-versus-object distinction transfer to every "acquire a scarce thing, use it, give it back" API you will ever write, and the leak story is the same story as an unreleased lock or an unbalanced `LongAdder`, one level up.
