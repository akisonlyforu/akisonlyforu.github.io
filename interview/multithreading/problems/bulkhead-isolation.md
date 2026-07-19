---
layout: post
title: Bulkhead Isolation (Per-Dependency Concurrency Caps)
date: 2026-07-19
description: >-
  It is the shortest path to demonstrating that you understand cascading failure, which is the thing that turns one degraded dependency into a full outage. The story is worth…
categories: interview multithreading problems
---

Part of the [Bounded Resource](/interview/multithreading/patterns/bounded-resource/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** Resilience patterns show up in senior backend rounds as either a design question ("how do you stop one bad dependency taking down the service?") or a small coding question ("cap concurrency per downstream"). Frequency claim, hedged: **as a design-round topic, very common at senior level; as a coding exercise, occasional, usually bundled with a circuit breaker and a timeout.** Treat it as a concept you must be able to argue and can implement in ten minutes, not as a memorised algorithm.

### Problem

A service calls several downstream dependencies (`payments`, `search`, `recommendations`, …) from a shared request-handling thread pool. Add per-dependency concurrency limits: at most `k_d` calls to dependency `d` may be in flight at once, and a caller that would exceed the limit is rejected (or waits briefly) rather than joining an unbounded pile-up. One slow dependency must not be able to consume the whole pool.

### Constraints

- Per-dependency limits, independently configurable, one number per dependency, not one global number.
- The limit must hold under concurrent traffic with no global lock on the hot path; this sits in front of *every* outbound call.
- Rejection must be fast and must be distinguishable to the caller from a dependency failure.
- The permit accounting must survive exceptions, timeouts, and interruption.

### Clarify before solving

- **Fail fast or queue?** (Fail fast is usually right; a queue in front of an overloaded dependency re-creates the pile-up with extra latency added. Ask, then justify.)
- **What does the caller do when rejected**: a cached/default response, a 503, a different dependency? A bulkhead with no fallback story just moves the failure.
- **Semaphore or dedicated thread pool per dependency?** They give different isolation for different costs; this is the core design question.
- **Is this per-dependency, per-endpoint, or per-tenant?** (Per-tenant caps are the same mechanic aimed at noisy-neighbour problems.)
- **Is a concurrency cap actually what's wanted, or a rate cap?** Different problem, see below; ask explicitly.
- **Are the downstream calls interruptible / do they have socket timeouts configured?** This determines whether any of this can actually recover.

### Why this problem matters

It is the shortest path to demonstrating that you understand **cascading failure**, which is the thing that turns one degraded dependency into a full outage. The story is worth telling in one breath: a dependency that normally answers in 20ms starts taking 20 seconds; each request to it now occupies a request thread a thousand times longer; the shared pool fills with threads waiting on that one dependency; requests that never touch it start queueing and then failing; health checks time out; the load balancer pulls the instance; the remaining instances absorb the load and fail the same way. **The dependency did not go down, your service did, on its behalf.**

The bulkhead is the fix, and it is nothing more than `Semaphore(k)` per dependency: the mechanic you already own, aimed at a production problem. What makes it a real interview question is the surrounding argument, why fail-fast beats queueing, why this is not a rate limiter, why it is not a circuit breaker, and what it can and cannot save you from.

---

## Strategy

### Classify

Bounded resource, and specifically the **multiplex**: `Semaphore(k)` *is* "at most k concurrent", which is the family's one-line definition of a counting semaphore. There is nothing to invent. Say the classification and the mechanism in the same sentence, *"a bulkhead is a semaphore per dependency; the design conversation is about the policy and the isolation model, not the primitive"*, and spend your time on the parts that are actually contested.

This is the degenerate one-predicate case: callers wait (or balk) on "a permit is available"; releasers never wait. Same shape as implementing a semaphore, same shape as the connection pool's permit half, with the object half deleted.

### Invariant

For each dependency d, the number of in-flight calls to d never exceeds `k_d`; every acquired permit is released exactly once, on every exit path including exceptions, timeouts, and interruption; and a caller rejected for lack of a permit performs no call to d.

Linearization point: the successful `tryAcquire`. That instant is "I am one of the k".

### Mental model

The ship's bulkhead the pattern is named after. A hull is divided into sealed compartments so that a breach floods one compartment and the ship stays up. The compartments do not prevent the breach, they bound its blast radius. Translated: the bulkhead does not make the slow dependency fast, and it does not make your calls succeed. It guarantees that the damage stops at `k_d` threads.

The compartment metaphor also explains the sizing intuition: compartments that are too large defeat the purpose (one breach floods most of the ship), and compartments that are too small waste capacity. Sum of all `k_d` should generally *exceed* the pool size, you are not partitioning the pool, you are capping each tenant's share so no single tenant can take all of it.

### Two implementations, and the trade-off

**Semaphore bulkhead**: `tryAcquire` before the call, `release` in `finally`, the call runs on the **caller's own thread**. Cheap (a CAS on the uncontended path), no thread hop, no extra pools to size, preserves `ThreadLocal` context (request ids, security context, MDC, this matters more in practice than people expect). What it cannot do: it cannot abandon a call. If the downstream socket has no timeout, the caller's thread is stuck for as long as the dependency wants, and your permit is stuck with it. The bulkhead still works, only k threads are stuck instead of all of them, but you get no per-call timeout out of it.

**Thread-pool bulkhead**: a dedicated bounded pool per dependency; the caller submits and waits on the future with a timeout. Stronger isolation (a wedged dependency's threads are *its own*, never the request threads), and the future's timeout lets the caller **stop waiting** and return a fallback. What it costs: a thread hop per call (latency, and lost thread-locals unless you propagate them), N pools to size and monitor, and the honest caveat that **abandoning the future does not free the worker thread**, the pool thread is still stuck in the same socket read. You have moved the stuck threads somewhere harmless, not eliminated them; if the dependency stays wedged, that pool saturates too and starts rejecting, which is the correct behaviour rather than a failure.

**Choosing**: semaphore by default (Resilience4j's own guidance leans this way, and Hystrix's thread-pool-per-dependency model is widely regarded as heavier than most services need). Thread pool when calls are not reliably timeout-bounded, when you need to abandon them, or when the dependency's client library is a black box. Say the trade in one sentence, *"semaphore is cheaper and keeps context; a pool buys you the ability to walk away from a call, at the cost of a hop"*, and pick one.

And the rule that outranks both: **configure connect and read timeouts on the client.** Neither bulkhead can kill a thread. Timeouts at the socket are the only thing that actually recovers a stuck call; the bulkhead bounds how many you lose while you wait for them. Volunteering this is the senior beat here, because it shows you know what the pattern does *not* do.

### Fail fast versus queue: the policy axis, with a strong default

The policy axis in this family is block / balk / timeout / reject-by-state, and for bulkheads the argument bends hard toward balk:

- **Fail fast** (`tryAcquire()` with no wait). If k callers are already in flight against a dependency that is slow, the k+1-th waiting does not help anyone, it converts a fast, cheap rejection into a slow, expensive one, and it holds a request thread while doing so. Reject, return the fallback, free the thread.
- **Short timed wait** (`tryAcquire(20ms)`). Defensible when slowness is bursty and 20ms of queueing genuinely smooths it. The number must be small and justified against your latency budget; "a small queue absorbs jitter, a large queue *is* the outage" is the sentence.
- **Block indefinitely**: never. This reintroduces the exact pile-up the bulkhead exists to prevent, one queue deeper. If you find yourself writing `acquire()` here, you have built an expensive way to do nothing.

Corollary worth stating: a bulkhead **without a fallback** does not improve availability, it improves *blast radius*. Requests to the slow dependency still fail; the win is that requests to everything else keep working. Say that explicitly so nobody thinks you are claiming more than you are.

### Bulkhead vs rate limiter vs circuit breaker

Three resilience patterns that candidates blur; distinguishing them cleanly is worth real credit.

- **Bulkhead caps concurrency**: how many at once. Permits are returned by the threads that took them. This is a semaphore; it has no clock.
- **Rate limiter caps frequency**: how many per second. Nobody returns a rate token; **time mints new ones**. This is guarded state plus a clock, and grafting a clock onto a semaphore just rebuilds the background refiller the token-bucket problem rejects. *(This is the same distinction, in the same words, as the rate-limiter strategy, reuse it verbatim, because interviewers probe exactly this boundary.)*
- **Circuit breaker stops calling at all** for a while, based on an observed failure rate. It is a state machine over history; the bulkhead is a counter over the present.

They compose, and in production you want all three plus timeouts: the timeout bounds one call, the bulkhead bounds concurrent damage, the breaker stops the bleeding after a pattern emerges, the rate limiter protects the dependency from you. Being able to say what each one catches that the others miss is the design-round answer.

### Sizing

Little's law is the honest starting point: in-flight = arrival rate × latency. A dependency taking 50ms at 200 rps needs about 10 concurrent slots at steady state; size k somewhat above that so normal jitter doesn't reject, and well below "enough to eat the pool". Then the standard caveat: **that is a starting heuristic, and I'd tune it against measured latency and rejection metrics.** The metric that tells you the setting is wrong is the rejection rate under *normal* conditions, nonzero means k is too small; a bulkhead that never rejects even during an incident means k is too large to protect anything.

Also worth naming: sum of all `k_d` deliberately exceeds the pool size. You are capping shares, not partitioning capacity, and over-subscription is what keeps utilisation high when dependencies are healthy.

### Production equivalent

**Resilience4j** (`Bulkhead` for the semaphore flavour, `ThreadPoolBulkhead` for the other) is the current answer on the JVM; **Hystrix** popularised the pattern and is retired, mention it as history and as the origin of thread-pool-per-dependency, not as a recommendation. Envoy/Istio enforce the same limits at the sidecar, outside your process, which is often the better place. In a design round: *"per-dependency bulkheads with Resilience4j, plus timeouts and a breaker."* Hand-build the semaphore wrapper only when implementation is the question, and note that when you do, it is about fifteen lines, which is itself a point worth making about how much of resilience is configuration rather than code.

### Pitfalls

1. **Release not in `finally`**: every exception permanently destroys one permit. After k exceptions the dependency is unreachable forever, with no error pointing at the cause. Identical failure mode to a leaked connection, and just as hard to diagnose.
2. **`acquire()` instead of `tryAcquire()`**: you built a queue in front of an overloaded dependency, which is what you were trying to prevent.
3. **One global bulkhead for all dependencies**: that is just a smaller thread pool. The isolation comes from the limits being *per dependency*; a shared limit lets the slow one still eat everyone's share.
4. **Rejection indistinguishable from failure**: the caller cannot tell "we chose not to call" from "the call failed", so metrics lie and the fallback logic is wrong. Distinct exception type, distinct metric.
5. **Bulkhead with no timeout on the underlying call**: permits held indefinitely; the pool never recovers even after the dependency does. The bulkhead is doing its job and the system is still down.
6. **Counting the wrong thing**: wrapping only part of the call (acquire around the connect but not the read) undercounts in-flight work.
7. **Thread-pool bulkhead with an unbounded queue**: the queue absorbs the overload invisibly and you have rebuilt the pile-up inside the isolated pool.
8. **Losing request context across the thread hop**: MDC/trace ids/security context vanish, and your incident debugging vanishes with them.

### Check your understanding

1. Tell the cascading-failure story end to end, and say exactly which step the bulkhead interrupts, and which steps it does not.
2. Bulkhead versus rate limiter in one sentence. Why can you not build a rate limiter by having a thread release semaphore permits on a timer?
3. Semaphore versus thread-pool bulkhead: name one thing each can do that the other cannot, and say which you would ship by default.
4. A permit leaks on an exception path. Describe what the service looks like from the outside two hours later, and why the stack traces do not name the cause.
5. Your bulkhead rejects 0% of requests during a major dependency incident. What does that tell you about k, and what would you change?

### Transfers to

Per-tenant and per-customer quotas (noisy-neighbour isolation), admission control at any tier, capping concurrent expensive operations (report generation, exports, image processing), limiting concurrent writers to a shared external system, and the "at most k of these at once" clause that appears inside half of all LLD designs. The mechanic is the plainest multiplex there is; what transfers is the *argument*, blast-radius bounding, fail-fast over queueing, and the discipline of releasing in `finally`.
