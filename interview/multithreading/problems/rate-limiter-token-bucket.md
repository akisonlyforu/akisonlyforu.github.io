---
layout: post
title: Thread-Safe Rate Limiter (Token Bucket)
date: 2026-07-19
description: >-
  The most-asked design-coding hybrid in the bank. Fuses guarded state (Type B) with TIME as an input: the token count is a function of "now", which creates a new flavor of…
categories: interview multithreading problems
---

Part of the [Time-Based State](/interview/multithreading/patterns/time-based/) family. Read the problem, then close the page and attempt it for fifteen minutes before reading the strategy, which deliberately contains no full solution.

## The problem

**Source:** LLD/hybrid rounds. **Very High frequency**. Target: correct in 30 minutes while explaining aloud.

### Problem

`RateLimiter(int capacity, double refillPerSecond)` with `boolean tryAcquire()`: allow a request if a token is available (consume it), else reject. Tokens refill continuously at the given rate up to `capacity` (the burst limit). Called concurrently from many request threads.

### Constraints

- Thread-safe under heavy concurrent tryAcquire.
- Non-blocking API first (reject, don't wait). Blocking `acquire()` is the follow-up.
- No background refill thread unless you can justify it (hint: you don't need one).

### Clarify before solving

- Per-client or global? (Start global; per-client = map of limiters, say it, then scope down.)
- Burst semantics: full bucket at t=0? (Convention: yes, capacity = allowed burst.)
- What clock? (`System.nanoTime`: monotonic; wall clock jumps. Saying this unprompted is a senior marker.)
- Single JVM (distributed = different question; name Redis+Lua in one sentence if asked).

### Why this problem matters

The most-asked design-coding hybrid in the bank. Fuses guarded state (Type B) with TIME as an input: the token count is a function of "now", which creates a new flavor of check-then-act race (compute-tokens-then-consume). Also the cleanest place to show the lazy-refill insight: deriving state on demand instead of maintaining it with a thread.

---

## Strategy

### Classify

Guarded state + clock. NOT a semaphore problem. Say why immediately: a semaphore caps CONCURRENCY (how many at once); a rate limiter caps FREQUENCY (how many per second). Nobody "releases" a rate token: time mints new ones. ([Step 1](/interview/multithreading/mt-framework/)'s note, made yours.)

### Invariant

tokens ∈ [0, capacity] at all observable times; a request succeeds iff, at its linearization point, derived-tokens ≥ 1, and it consumes exactly 1.

### The key insight: lazy refill

No background thread topping up the bucket. Instead store `tokens` and `lastRefillTime`, and on each tryAcquire DERIVE the current balance: elapsed = now − lastRefill; tokens = min(capacity, tokens + elapsed × rate); lastRefill = now. Then: if tokens ≥ 1 → consume, allow; else reject.

Why lazy beats a refiller thread (rehearse this): no thread to size/own/shut down (lifecycle for free), no refill-vs-consume race between two writers, exact-to-the-nanosecond accounting instead of tick granularity, and zero cost while idle. "State as a function of time, computed on read", same trick as the expiring cache, and the opposite pole from Dining Savages' cook (exhaustion-driven refill needs an agent; time-driven doesn't).

### Concurrency design

The whole read-derive-consume must be atomic, otherwise two threads both derive tokens=1 and both allow (check-then-act with a derivation inside). Two implementations:

1. **synchronized tryAcquire**: one lock around ~6 lines of arithmetic. Correct, obvious, fast enough for almost anything (uncontended locks are cheap; say it, don't apologize). **Ship this in the interview.**
2. **CAS loop on an immutable (tokens, timestamp) pair** in an AtomicReference: read state → compute new state → compareAndSet, retry on failure. Lock-free, mention-worthy as the follow-up answer; know the shape, don't lead with it (over-engineering trap; also two separate atomics for tokens and time is BROKEN, the pair must change together, which is exactly why the reference holds both).

### Follow-ups to be ready for

- **Blocking acquire()**: compute wait time = (1 − tokens)/rate, sleep/await that long, re-derive. Or condition-wait with timed await. Don't spin.
- **Per-client**: `ConcurrentHashMap<ClientId, RateLimiter>` + computeIfAbsent (its atomicity is the point, connects to #13).
- **Sliding window / fixed window / leaky bucket**: one sentence each; token bucket = smooth rate + bounded burst, the usual production pick (Guava RateLimiter).
- **Distributed**: state moves to Redis; atomicity moves to a Lua script (same invariant, new home, nice closing line).
- **Guava exists**: in a design round say "I'd use Guava's RateLimiter; here's how I'd build it if asked."

### Pitfalls

1. `System.currentTimeMillis()`, wall clock: NTP jumps → token bursts or droughts. nanoTime.
2. Deriving tokens outside the lock, consuming inside: the race is between derive and consume; both go inside.
3. Integer token math with fractional refill rates: accumulate as double or track in nano-token units; losing fractions under-delivers the rate.
4. Background refill thread: works, but you now own a thread lifecycle for no benefit; expect the "why?" and have no answer.
5. Forgetting the min(capacity, ...) clamp: idle bucket accrues unbounded burst.

### Check your understanding

1. Semaphore vs rate limiter in one sentence: concurrency vs frequency.
2. Two threads race tryAcquire with 1 token left: where exactly is the double-allow if derivation is unlocked?
3. Why must (tokens, lastRefill) update atomically TOGETHER? What breaks with two separate atomic fields?
4. Derive the blocking acquire's sleep formula. Why re-derive after waking instead of trusting it? (Others consumed meanwhile.)
5. Sketch the Redis version's division of labor in two sentences.

### Transfers to

Expiring cache (same lazy time-derivation), delayed scheduler (time-ordered waiting), connection-pool checkout with timeout, and all "X per second" LLD asks.
