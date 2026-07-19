---
layout: post
title: Circuit Breaker
date: 2026-07-19
description: >-
  It is the best small problem in the bank for the question *"can you build correct concurrent state without putting a lock on the hot path?"* A synchronized breaker is…
categories: interview multithreading problems
---

Part of the [Guarded State](/interview/multithreading/patterns/guarded-state/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Very common as a senior design-round topic; an occasional but recurring production-code exercise ("implement a circuit breaker") at companies that run a component-building round. Frequency claim, hedged: **you should expect to *discuss* one at senior level; being asked to *build* one is less common but is exactly the kind of thing Stripe/Coinbase/Rubrik-style production-code rounds pick**, because it is small enough to finish and rich enough to grade.

### Problem

`CircuitBreaker.execute(supplier)` wraps a call to a flaky dependency. Three states:

- **CLOSED**: calls pass through; outcomes are recorded.
- **OPEN**: calls are rejected immediately without touching the dependency; entered when the failure rate over a recent window crosses a threshold.
- **HALF_OPEN**: after a cooldown, a limited number of *probe* calls are allowed through to test whether the dependency has recovered. Success closes the circuit; failure re-opens it and restarts the cooldown.

Called concurrently from every request thread in the service.

### Constraints

- The happy path (CLOSED, dependency healthy) is the overwhelming majority of calls and **must not serialise**, no global lock held across the dependency call, and ideally no contended lock at all on that path.
- Transitions must be consistent: the circuit must not be simultaneously believed CLOSED by some threads and OPEN by others in a way that matters, and a single triggering observation must produce a single transition.
- The failure window is *rolling*, "50% failures in the last 10 seconds", not "50% failures since the process started".
- No timer thread flipping states in the background unless you can justify owning it.

### Clarify before solving

- **Window type**: last-N-calls (count-based) or last-T-seconds (time-based)? They behave very differently for low-traffic endpoints, ask.
- **Threshold semantics**: failure *rate* or failure *count*? Rate needs a **minimum-throughput guard**, or two calls and one failure trips at 50%.
- **What counts as a failure?** Timeouts and 5xx, yes. Client errors (400/404) are not the dependency being unhealthy, counting them trips your breaker on your own bad requests. Slow-but-successful calls: many production breakers count calls over a latency threshold as failures. Ask.
- **HALF_OPEN probe budget**: exactly one trial call, or N? What fraction must succeed to close?
- **Per what?** Per dependency, per endpoint, per host/instance? (Per-instance breakers plus a load balancer is a different and often better design.)
- **Fallback**: what does a rejected call return? A breaker with no fallback story converts slow failures into fast ones, worth having, but say it plainly.

### Why this problem matters

It is the best small problem in the bank for the question *"can you build correct concurrent state without putting a lock on the hot path?"* A `synchronized` breaker is trivially correct and completely unacceptable: you have placed a global serialisation point in front of every outbound call in the service, which is a worse availability problem than the one the breaker was added to solve. The interesting design is a state machine advanced by CAS, a counter that is cheap to write under contention, and a time-based transition that is **derived on read** rather than driven by a timer, three mechanics you already own, composed.

It also contains, in miniature, a limited-permit problem (HALF_OPEN admits at most N probes, a multiplex), a generation problem (a late probe result from a previous half-open round must not be allowed to decide the current one), and the lazy-derivation-versus-background-thread fork. Very few problems this size touch this many families.

---

## Strategy

### Classify

Guarded state (Type B): a small state machine plus a rolling counter, shared by every request thread. But name the three borrowed mechanics in the same breath, because they are what make the answer good rather than merely correct:

1. **Lazy derivation with `nanoTime`**: the OPEN→HALF_OPEN transition is *derived on read* by the next caller, not fired by a timer thread. Exactly the token bucket's refill insight: don't maintain time-dependent state, derive it when someone looks.
2. **The multiplex**: HALF_OPEN admits at most N probes. That is `Semaphore(N)`, or a CAS-bounded counter; either way it is the "at most k at once" mechanic.
3. **Generation tokens**: a probe launched in half-open round 7 must not be allowed to close the circuit if we are now in round 8. This is the reusable-barrier lapping problem wearing a different hat, and the fix is the same: **signals carry round identity**.

Say up front what this is *not*: it is not a bulkhead (which caps concurrency) and not a rate limiter (which caps frequency). A breaker is a decision over *history*.

### Invariant

At most one state transition results from any single triggering observation; while OPEN and before the cooldown elapses, no call reaches the dependency; while HALF_OPEN, at most N probe calls are in flight; and outcomes recorded during half-open round g can only cause transitions out of round g.

The linearization point of a transition is the successful `compareAndSet` on the state reference. Losers of that CAS do not retry blindly, they re-read and obey whatever the winner decided.

### Mental model

A household fuse box, with one refinement. Too many faults in a short span and the fuse blows: the circuit is dead and nothing downstream is even attempted, that is the *cheapness* of OPEN, and it is the whole point (a rejected call costs a volatile read and a comparison, versus a 20-second timeout). After a while you go and flip the switch back on, but not all the way: you let one appliance draw power and watch. If it's fine, everything comes back on. If it trips again, back off and wait longer.

The concurrency content is what happens when a hundred people reach for the switch at once. Exactly one flip should happen; the other ninety-nine should look at the switch, see it has already been flipped, and go about their business, not queue up behind a lock waiting for a turn to flip a switch that is already flipped.

### Design

### The state, as one atomic snapshot

`AtomicReference<Snapshot>` where the snapshot is an immutable `(state, transitionedAtNanos, generation)`. All three change **together**, which is the **pair rule** from the time-based family generalised to a triple: separate atomics for state and timestamp are broken, because between updating one and the other a reader sees a state paired with the wrong instant and computes a wrong cooldown. One immutable object behind one reference makes the swap indivisible.

Transitions are `compareAndSet(old, new)`. On failure, re-read and re-evaluate; do not spin blindly. This is the CAS-retry-on-immutable-snapshot shape from the escalation ladder, and note that here, unusually, it is not premature optimisation: the requirement *"must not serialise the happy path"* is stated in the problem, which is exactly the "stated reason" the ladder demands before you climb.

### The happy path does almost nothing

CLOSED, dependency healthy: one volatile read of the snapshot, one branch, run the call, record the outcome. **No lock is taken.** The only shared write is the outcome recording, and that is the thing to make cheap (below). Any design where the dependency call happens with a lock held is wrong twice over, it serialises every outbound call, and it holds a lock across an alien call of unbounded duration, which is failure mode #9 in the guarded-state catalog.

### The rolling window

Two shapes, and you should be able to argue both:

- **Count-based ring buffer**: the last N outcomes in a fixed circular array; a write is an index increment plus a slot write, and the rate is derivable from running totals. Simple, bounded, exact over "last N calls". Weakness: on a low-traffic endpoint, N calls can span an hour, so the breaker reacts to ancient history.
- **Bucketed time window**: M buckets of, say, one second each; a call increments the current bucket; the rate is the sum over the M buckets covering the last T seconds. Buckets are **rotated lazily on access**, when a call arrives and the clock says the head bucket is stale, it is reset before use. That is lazy derivation again: no sweeper thread, no lifecycle to own, exact enough at bucket granularity, free when idle. This is the usual production choice, and the granularity loss (an event's contribution disappears in one lump when its bucket rolls, rather than sliding out smoothly) is the honest cost to name.

**The concurrency cost of the counter is the real content here.** Every single call writes to it, so a naive `AtomicLong` per bucket becomes one contended cache line for the entire service, the hot path's only bottleneck, created by the thing that was supposed to protect availability. Mitigations, in order: `LongAdder` (striped internally, designed for exactly this: high write, infrequent read), or per-thread/striped counters summed on read. Both trade read cost and exactness-at-an-instant for write throughput, which is precisely the right trade when reads happen on transition checks and writes happen on every call. Name the contention, then name `LongAdder`; leading with `LongAdder` unprompted reads as over-engineering, but arriving at it from the access pattern reads as senior.

Rotation itself is a check-then-act (read head bucket's epoch, decide it is stale, reset it) and two threads can arrive together. Options: CAS the bucket's epoch so exactly one thread resets it, or accept a benign double-reset, decide deliberately, and say which, rather than leaving it unexamined.

### OPEN, and the time-based transition

While OPEN, `execute` reads the snapshot, computes `now − transitionedAt` with **`nanoTime`** (monotonic, wall-clock jumps would either un-open the circuit early or leave it open across an NTP correction), and:

- cooldown not elapsed → reject immediately. Cheap, lock-free, and this is the state the breaker exists to make fast.
- cooldown elapsed → attempt `compareAndSet(OPEN-snapshot, HALF_OPEN-snapshot with generation+1)`. The winner proceeds as the first probe; losers re-read and see HALF_OPEN, then contend for a probe permit like everyone else.

No timer thread anywhere. The transition is a function of the clock, evaluated by whoever shows up, and if nobody shows up, nothing needs to happen, which is exactly the "zero idle cost" property that makes lazy derivation win.

### HALF_OPEN as a limited-permit problem

This is the part candidates under-design. On entering half-open, exactly N probes may pass; everyone else is rejected as if OPEN. Mechanism: a `Semaphore(N)` created fresh per half-open round, or a CAS-bounded probe counter, either is fine; the property that matters is that admission is *counted*, not just *checked*. `if (probesInFlight < N) probesInFlight++` is check-then-act and admits a stampede of probes at exactly the moment the dependency is most fragile. **A recovering dependency being hit by the full request load is how a breaker turns one outage into two.**

Probe outcomes decide the round: enough successes → CLOSED with a **freshly reset window** (leaving the old failures in the window re-trips the breaker on the first new failure, a genuinely common bug); any failure (or too few successes) → OPEN, cooldown restarted, and if you use exponential cooldown growth, increased.

**The generation guard.** A probe from round g can complete long after round g ended, it was slow, which is probably why it failed. If it naively writes "close the circuit", it can close a circuit that round g+1 already re-opened. Fix: the probe carries its generation; its result is applied only via a CAS against a snapshot still bearing that generation. Same idea as a barrier's generation token, the signal carries round identity, so a lapped participant cannot corrupt the current round. Volunteering this is a strong senior marker, because it is invisible until you think about slow calls, which is the whole domain of this problem.

### Trade-offs and refinements worth a sentence each

- **Minimum throughput guard.** A rate threshold with no volume floor trips on two calls and one failure. Require a minimum number of recorded calls in the window before the rate is allowed to trip anything.
- **Slow calls as failures.** A dependency answering successfully in 20 seconds is unhealthy. Production breakers count calls exceeding a latency threshold toward the failure rate; without it, the pathological "slow but not failing" case never trips.
- **Exceptions that shouldn't count.** 4xx means *you* sent a bad request. Counting client errors means one buggy caller opens the circuit for everyone. Take an explicit predicate for "is this a failure".
- **Per-instance vs per-dependency.** If one of five downstream instances is bad, a per-dependency breaker either ignores it (rate diluted below threshold) or blocks all five. Per-host breakers behind the load balancer are finer-grained and much more common in practice than the textbook diagram suggests.
- **Breaker + bulkhead + timeout + retry.** The four compose: timeout bounds one call, bulkhead bounds concurrent damage, breaker stops calling after a pattern emerges, retry (with jitter, and *inside* the breaker's accounting) handles blips. Retries must be counted by the breaker, or a retry storm looks like healthy traffic.
- **Observability**: state-transition events and rejection counts are the metrics that make a breaker debuggable. A silent breaker produces the mystifying incident "everything is fast and everything is failing."

### Production equivalent

**Resilience4j** is the current JVM answer (its `CircuitBreaker` is built on exactly this: an atomic state machine plus a ring-buffer or time-bucket metrics window); **Hystrix** popularised the pattern and is retired, cite it as history. Envoy and service meshes implement outlier detection, which is the per-instance version, outside your process. Polly is the .NET equivalent if the conversation drifts. In a design round: *"Resilience4j circuit breaker, per dependency, with timeouts and a bulkhead."* Hand-build only when implementation is the explicit ask, and if it is, the grading is in the hot path, the window, and the half-open admission, not in drawing the three states.

### Pitfalls

1. **`synchronized execute()`**: trivially correct, and it puts a global serialisation point plus an alien call under a lock in front of every request. The requirement you ignored is in the problem statement.
2. **Separate atomics for state and timestamp**: a reader pairs the new state with the old instant and computes a nonsense cooldown. The pair (here, triple) must swap as one immutable snapshot.
3. **Wall-clock arithmetic**: an NTP correction either releases the circuit early or strands it OPEN. `nanoTime`, always.
4. **Check-then-act probe admission**: `if (inFlight < N) inFlight++` lets a thundering herd of probes hit a dependency that has just started breathing again.
5. **Window not reset on transition to CLOSED**: the old failures are still in the window, so the first new failure re-trips instantly and the circuit flaps.
6. **No minimum-throughput guard**: low-traffic endpoints trip on statistical noise.
7. **Contended single counter on the hot path**: the breaker becomes the bottleneck it was installed to prevent. `LongAdder` or striping.
8. **Counting client errors (4xx) as dependency failures**: your own bad requests open the circuit for everybody.
9. **No generation guard on late probe results**: a straggler from a previous half-open round closes a circuit that has since re-opened.
10. **Timer thread to flip OPEN→HALF_OPEN**: a lifecycle to own, a second writer to race with, and tick granularity, all for something a caller can derive in two instructions.

### Check your understanding

1. Why must `(state, transitionedAt, generation)` swap as a single unit? Construct the interleaving where two separate atomics produce a wrong cooldown decision.
2. Where exactly is the lock on your happy path? (There should be none.) What is the only shared write, and what makes it cheap under contention?
3. Ten threads arrive simultaneously at an OPEN circuit whose cooldown has just elapsed, with a probe budget of 1. Trace who transitions, who probes, and who is rejected, and say which two operations must be atomic for this to hold.
4. A probe from half-open round 7 returns "success" after round 8 has already re-opened the circuit. What goes wrong without a generation token, and which other problem in this bank has the identical bug?
5. Bucketed time window versus count-based ring buffer: give the traffic pattern that makes each one behave badly, and say which you'd ship for a low-traffic internal endpoint.

### Transfers to

Any health-based admission decision: outlier ejection and load-balancer host health, adaptive concurrency limits, feature-flag kill switches, throttling on error budgets. The mechanics transfer further than the pattern does, CAS on an immutable snapshot is the general answer to "small shared state, hot read path"; lazily-rotated buckets are the general answer to "rolling metric without a sweeper thread"; and the generation token is the general answer to "a late signal from a previous round must not decide the current one."
