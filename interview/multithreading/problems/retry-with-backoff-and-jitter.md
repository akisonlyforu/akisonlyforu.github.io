---
layout: post
title: Retry with Backoff and Jitter
date: 2026-07-19
description: >-
  Retry is the single most common way a well-meaning client turns a degradation into an outage. A dependency has a two-second blip; every client retries; the retried load…
categories: interview multithreading problems
---

Part of the [Time-Based State](/interview/multithreading/patterns/time-based/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Appears as a small production-code exercise ("write a retry helper") and, more often, as a probe inside a system-design or debugging discussion ("your service retries, what goes wrong?"). Frequency claim, hedged: **the *concept* is asked at senior level constantly; the *implementation* occasionally.** The jitter question specifically is a well-known senior filter, because most candidates implement exponential backoff and stop there.

### Problem

`retry(operation, policy)`: attempt an operation; on a retryable failure, wait and try again with exponentially increasing delays, up to a maximum delay, a maximum attempt count, and an **overall deadline**. Delays are randomised (jittered). Return the first success, or the last failure if the budget is exhausted. Called concurrently by many request threads, in a service that may have thousands of peers doing the same thing at the same moment.

### Constraints

- Two independent budgets: a per-attempt timeout and an overall deadline. Both must be honoured; neither substitutes for the other.
- Delays must be jittered, a fixed exponential schedule is explicitly not acceptable, and you should be able to say why.
- Retryable and non-retryable failures must be distinguished by an explicit policy, not by catching everything.
- If this runs in a request-serving service, a retry must not park a pool thread doing nothing.

### Clarify before solving

- **Is the operation idempotent?** If not, retrying a timed-out call may duplicate an effect that already succeeded. This is a prerequisite, not a detail, ask it first.
- **Which failures are retryable?** Timeouts, connection resets, 429, 502/503/504, yes. 400/401/403/404 and validation errors, no; retrying them wastes the budget and hides the real bug. Does the server send `Retry-After`, and do we honour it?
- **Where does the deadline come from?** Is there a request-scoped deadline to inherit and propagate, or do we invent one?
- **Are we already retrying at another layer?** Retries at three layers multiply; agree on exactly one.
- **Blocking or async?** In a request handler, sleeping a pool thread for a 4-second backoff is a capacity bug. In a CLI or a batch job it's fine. Say which world you're in.
- **Jitter flavour**: full, equal, or decorrelated? (Have a preference and a reason.)

### Why this problem matters

Retry is the single most common way a well-meaning client turns a **degradation into an outage**. A dependency has a two-second blip; every client retries; the retried load arrives on top of the recovering dependency and knocks it over; those retries fail and are retried; the system settles into a stable state where the dependency is permanently saturated by retry traffic. Understanding that a retry policy is a *load amplifier* before it is a reliability feature is the point of this problem.

Jitter is where the concurrency insight lives. Exponential backoff without randomisation preserves the very thing that caused the problem, **correlation**. A thousand clients that failed at the same instant will retry at the same instant, forever, in synchronised waves. Adding randomness is not a rounding detail; it is the mechanism that breaks the herd. And the implementation detail underneath it, never `sleep()` for coordination when a scheduler is available, is the same rule this whole family is built on.

---

## Strategy

### Classify

Time-based state, the **wait-until** branch, the same fork as the delayed scheduler, not the derive-on-read fork of the token bucket. Nothing here accrues with elapsed time; instead an action must fire at a future moment, which means someone must be asleep with an alarm set. So the family's mechanics apply directly: **`awaitNanos`/a scheduler, never `Thread.sleep` for coordination**, all interval arithmetic on `nanoTime`, and a re-check of the remaining budget on every wake.

There is also a pleasing symmetry to state out loud: **the rate limiter shapes traffic at the server; jitter shapes traffic at the client.** Both exist to stop bursts from correlating, and one of them (a retry budget) is literally a token bucket. Same family, opposite ends of the wire.

### Invariant

At most `maxAttempts` attempts; no attempt is *started* after the deadline has passed; every wait is bounded by `min(backoff, remaining budget)`; and a non-retryable failure terminates immediately without consuming further budget. Total elapsed time is bounded by the deadline plus at most one in-flight attempt's timeout.

### Mental model

A crowd outside a shop whose door has jammed. Everyone tries the handle, everyone fails at the same second, everyone agrees to "wait a minute and try again", and in one minute the identical crowd hits the identical door in the identical instant. The schedule is correct and the outcome is unchanged, because the schedule preserved what was wrong: **everyone is in step**. Jitter is telling each person to wait a *random* amount up to a minute. The same average patience, no wave.

That picture also explains why increasing the delay alone doesn't fix it: a synchronised crowd waiting four minutes is still a synchronised crowd. Backoff reduces the *rate* of the herd; jitter destroys the *herd*.

### Design

### The loop

Attempt; classify the outcome; if it succeeded, return; if the failure is non-retryable, throw; otherwise, if attempts remain and the deadline hasn't passed, compute a delay, wait, and go again. The whole thing is a bounded loop around one decision, the interest is entirely in the four things that decision consults.

### Budgets: deadline and per-attempt timeout are different things

Two budgets, both required, and candidates routinely conflate them:

- **Per-attempt timeout** bounds a single stuck call. Without it, one hung attempt consumes the entire deadline and you never retry at all.
- **Overall deadline** bounds the *caller's* wait. Without it, "5 attempts with exponential backoff" can silently mean a 30-second worst case that nobody signed up for, sitting behind a client that gave up at 2 seconds.

The interaction is the mechanical part: before each wait, compute `remaining = deadline − now`. If `remaining ≤ 0`, stop now, do not perform an attempt you cannot afford. Otherwise wait `min(backoff, remaining)` and set the next attempt's timeout to no more than the remaining budget. An attempt whose own timeout exceeds the deadline is a guaranteed budget overrun.

The distributed extension, worth one sentence: **propagate the deadline** rather than re-inventing it at each hop (gRPC deadlines, `Deadline`/`context` semantics). Otherwise every layer generously grants itself the full budget and the total is the product of everyone's optimism.

### Jitter: why, then which

**Why** is the part that earns credit. A correlated failure produces a correlated retry. Deterministic backoff, `base × 2^n`, maps every client that failed at time t to the same retry instant, so the load arrives in spikes rather than being spread; a recovering dependency is hit by the full herd at exactly the moment it is weakest, fails again, and the herd re-forms one interval later. The system can sit in that oscillation indefinitely. Randomisation breaks the correlation, and that, not the backoff, is what actually reduces peak load.

**Which**, conceptually (all three cap at a maximum delay):

- **Full jitter**: wait a uniform random amount between zero and the current exponential ceiling. Maximum spread, lowest peak load. Cost: some retries fire almost immediately, so the *minimum* spacing guarantee is gone. This is the usual default, and AWS's widely-cited analysis of the three variants found it hard to beat on both total work and completion time.
- **Equal jitter**: half the ceiling fixed plus a random half. Keeps a guaranteed minimum wait while still decorrelating. A reasonable compromise when very early retries are harmful.
- **Decorrelated jitter**: each delay is drawn from a range anchored on the *previous actual delay* rather than on the attempt number, so the schedule is a random walk that trends upward. Spreads well and grows without the neat doubling; slightly harder to reason about and to test.

The senior framing: all three trade *spread* against *predictable minimum spacing*, and the choice matters far less than the presence of randomness at all. Say that, pick full jitter, move on. What you must not do is describe jitter as "add a little noise so it looks less robotic", the mechanism is decorrelation, and the interviewer is listening for that word or its equivalent.

### Never sleep a pool thread to wait

`Thread.sleep(backoff)` inside a request handler holds a pool thread doing nothing for the whole delay. At any scale, retries under a partial outage mean a large fraction of your threads are asleep, and the pool saturates, *you have converted a downstream problem into a local capacity outage*, which is the same failure the bulkhead exists to prevent, self-inflicted. The family rule applies: don't sleep, **schedule**. Register the next attempt on a `ScheduledExecutorService` and return a future to the caller, so no thread is held during the delay. In a CLI, a batch job, or on virtual threads (where a parked thread costs almost nothing), blocking is fine, but say which world you are in rather than defaulting.

The secondary reason the family gives for never sleeping applies too: **a sleeper is deaf.** It cannot be told that the circuit has opened, that the request was cancelled, or that the deadline was shortened. A scheduled task or a timed `await` can be cancelled; a sleeping thread can only be interrupted, and only if someone kept a handle on it.

`nanoTime` for every interval, of course, an NTP correction mid-retry either fires the next attempt immediately or strands it, and "the deadline moved backwards" is a genuinely confusing incident to debug.

### Retryability, and the prerequisite nobody states

Classify failures explicitly: transient (timeout, connection reset, 429 with `Retry-After`, 502/503/504) versus terminal (400/401/403/404, validation, serialisation errors). Retrying a terminal failure burns budget and delays the real error reaching the caller. Honour `Retry-After` when the server sends it, the server knows more than your exponent does.

Then the prerequisite: **retry is only safe when the operation is idempotent, or when you carry an idempotency key.** A POST that times out may well have succeeded, the timeout tells you nothing about the server's state, only about your patience. Retrying it duplicates the effect: the double charge, the double shipment, the double email. Reads and PUT/DELETE-shaped operations are naturally safe; writes need a client-supplied idempotency key that the server deduplicates on (this is exactly why Stripe's API has one). Volunteering this unprompted is the strongest single sentence available in this problem, and it is the one most candidates never say.

### Amplification, and the retry budget

Retries at layer A wrapping retries at layer B wrapping retries at layer C multiply: three attempts each is up to 27 calls for one user request, arriving precisely when the dependency is least able to serve them. **Retry at one layer only**, usually the one closest to the failure that knows whether the operation is idempotent, and make the other layers pass failures through.

Beyond that, cap retries as a *fraction of traffic* rather than per-request: a **retry budget**, which is a token bucket whose tokens are minted at some percentage of the success rate. When the dependency is broadly unhealthy the budget empties and retries stop, so retry traffic can never become a meaningful multiple of real traffic. That is the token bucket from this same family, reused for a different purpose, worth naming as the connection it is. Pairing retry with a **circuit breaker** does the categorical version of the same job: once the breaker opens, there is nothing left to retry.

### Production equivalent

**Failsafe** and **Resilience4j** (`Retry`) are the current JVM answers; **Guava Retrying** and Spring Retry are the older ones; AWS/Google/Azure SDKs all ship configured retry policies with jitter that you should generally not override. In a design round: *"a retry policy with full jitter and a deadline, one layer only, budget-capped, behind a circuit breaker"*, name the library and move on. Hand-build only when implementation is the question, and when you do, spend your time on the budget arithmetic and the retryability predicate, which is where the bugs are.

### Pitfalls

1. **Backoff without jitter**: the synchronised retry storm; the schedule is correct and the herd is intact.
2. **Retrying non-idempotent operations** without an idempotency key, duplicate side effects, and the timeout gave you no way to know.
3. **No overall deadline**: "5 attempts, exponential" quietly becomes a 30-second worst case behind a caller who left after 2.
4. **Per-attempt timeout missing**: one hung attempt eats the whole budget; the retry logic never runs.
5. **`Thread.sleep` in a request thread**: the pool fills with sleepers under exactly the conditions that caused the retries. Self-inflicted capacity outage.
6. **Retrying everything catchable**: 400s and validation errors burn budget and mask the real error.
7. **Nested retries across layers**: multiplicative amplification; agree on one layer.
8. **Wall-clock deadlines**: an NTP step makes the deadline move; attempts fire immediately or never.
9. **No cap on the exponential**: attempt 20 schedules a wait measured in days.
10. **Ignoring `Retry-After`**: you argue with a server that is telling you exactly what it wants.
11. **Swallowed `InterruptedException` while waiting**: the retry loop becomes uncancellable; restore the flag and exit.

### Check your understanding

1. Explain, mechanically, why exponential backoff *without* jitter fails to protect a recovering dependency. What property of the client population is the problem, and which one does jitter attack?
2. Full versus equal versus decorrelated jitter: what does each one trade away, and which would you default to?
3. Why are a per-attempt timeout and an overall deadline both necessary? Give a failure that each one catches and the other does not.
4. Your service retries a POST that timed out. Describe what may have happened on the server, and state the precondition that makes the retry safe.
5. Three layers each retry three times. How many calls can one user request produce, and what two mechanisms would you add so that retry traffic can never exceed a bounded fraction of real traffic?

### Transfers to

Every client of every network dependency: HTTP/gRPC clients, database and message-broker reconnection, S3 and object-store operations, distributed lock acquisition, polling loops, and lock-contention backoff (`tryLock` with randomised backoff is this same idea attacking livelock rather than a thundering herd, same cure, same reason). The decorrelation insight transfers to any situation where independent actors are accidentally in step: cache expiry (add jitter to TTLs or every entry expires at once), cron schedules across a fleet, health-check intervals, and metric flush timers.
