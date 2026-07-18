---
layout:     post
title:      Everything I Got Wrong About Rate Limiting
date:       2026-07-18
description:    A rate limiter is three lines and everyone writes the same fixed-window counter, myself included. Then it lets a client through at double the limit on the window edge, hands out free quota when Redis evicts a key, and rejects a request while the headers swear the client has a full tank left. Here's the algorithm menu, the atomic Lua the counter actually needs, and every sharp edge that has personally bitten me.
categories: rate-limiting redis distributed-systems api
---

The first rate limiter I ever shipped was three lines and I was quietly proud of it. It ran fine for months. Then one morning a single client pushed through double the limit inside two seconds and walked away clean, and around the same time another client got a `429` while the response headers I handed back cheerfully told it there was a full tank of quota left. I spent that morning staring at two facts that couldn't both be true and slowly realising the three lines had been lying to me the whole time.

A rate limiter is three lines. Increment a counter, put a TTL on it, reject the request if the counter is over the limit. You'll write it, I wrote it, everyone writes the same three lines and they all pass in the demo. Then a real client with a real burst shows up, then you put Redis behind it and add a replica, and you find out the three lines were hiding about eight different ways to be wrong. This is the stuff I wish someone had handed me before I learned it the expensive way, one support ticket at a time.

## What it actually is

The whole job is to answer one question fast, over and over: *has this key done too much in this window?* A key is usually a user, an API token, an IP, or some combination. A window is a slice of time. Too much is a number you picked.

The classic answer is the fixed-window counter, and it really is three lines:

```python
def allowed(key, limit=100, window=60):
    count = r.incr(key)              # atomic, starts at 1 if the key is new
    if count == 1:
        r.expire(key, window)        # first hit of the window starts the clock
    return count <= limit
```

`INCR` is atomic, so a thousand concurrent requests all get distinct counts, no lost updates. The key expires on its own, so the window resets for free. Ship that and it mostly works. The rest of this post is about the word "mostly," and about what happens the day you outgrow one Redis box.

## Pick the algorithm on purpose

Here's the thing nobody tells you at the start, "rate limiting" isn't one algorithm, it's about five, and they trade accuracy for memory and smoothness in different places. People say "rate limit" and mean any of these:

| Algorithm | How it counts | I reach for it when |
|---|---|---|
| **Fixed window** | `INCR` a counter, TTL the window, reject over the limit | Simplest and cheapest, one key per window. A burst on the window boundary is acceptable. Good default for a lot of internal stuff. |
| **Sliding window log** | A sorted set of exact request timestamps; drop old ones, count what's left | I need it *exact* and I can pay for one stored entry per request. Billing, quotas people argue about. |
| **Sliding window counter** | Weight this window's count plus the previous window's, by how much of it still overlaps | I want the boundary burst gone without the memory of a full log. Cheap, approximate, smooth. This is 80% of public-facing limits. |
| **Token bucket** | Tokens refill at a steady rate, each request spends one, you can burst up to the bucket size | Bursts are a *feature* — a batch job or a webhook catching up should be allowed to spend saved-up capacity. |
| **Leaky bucket / GCRA** | One timestamp (the theoretical next-allowed time), admit at a constant smooth rate | I'm protecting a downstream that hates spikes and I want O(1) state and a hard, smooth output rate. |

The rules I actually use, stripped down:

- **Public API, mostly protecting the backend, a little burst won't hurt** → sliding-window counter, no debate. Smooths the edge, costs two counters.
- **Exactness that someone will dispute on an invoice** → sliding window log. Eat the memory, it's the only one that's actually precise.
- **Clients that legitimately burst** (nightly jobs, retry storms catching up) → token bucket, and size the bucket to the burst you're willing to eat.
- **A downstream that falls over on spikes** → GCRA / leaky bucket, admit at a flat rate no matter how bursty the input.
- **Per-user *and* per-endpoint *and* per-IP** → you're not picking one limiter, you're running three and rejecting if *any* of them trips. Say that out loud, it's a design decision, not an accident.

If you take one line from this section, it's that the algorithm is a staleness-vs-memory-vs-smoothness call, same as caching is a staleness-vs-freshness call. Fixed window is the default because it's cheap, not because it's right.

## The window that lets through double

The fixed-window counter has one flaw baked into its shape, and it's the first thing that bit me. The window resets on a hard boundary. So a client that's paying attention sends its whole limit at the very end of one window and its whole limit again at the very start of the next:

```
limit = 100 / minute

10:00:59   ├─ 100 requests ─┤          window A counter → 100, allowed
10:01:00   ├─ 100 requests ─┤          window B counter → 100, allowed
           └──── 200 requests in about 2 seconds ────┘
```

You promised 100 a minute and you just served 200 in two seconds, right across the seam. For an abuse limit that's the difference between "protected" and "not." The sliding-window counter fixes this without a full log, you keep this window's count and last window's count and blend them by how much of the previous window is still in view:

```python
def allowed_sliding(key, limit=100, window=60, now=None):
    now = now or time.time()
    cur_bucket  = int(now // window)
    elapsed     = (now % window) / window            # 0.0 → 1.0 through this window
    cur   = int(r.get(f"{key}:{cur_bucket}")   or 0)
    prev  = int(r.get(f"{key}:{cur_bucket-1}") or 0)
    estimate = prev * (1 - elapsed) + cur            # weighted, decays as we move on
    return estimate < limit
```

It's an approximation, but the error is small and it's *smooth*, no seam to game. Two keys instead of one, both with a short TTL. Worth it almost every time you're limiting something adversarial.

## Two commands, one race

The three-line version is safe because `INCR` does everything atomically. The moment you outgrow it — you want an explicit reset timestamp, or a negative window, or you split the counter and its expiry into two keys — you've handed yourself a check-then-act race, and you'll ship it at least once. I have, more than once.

Say you store the counter and the window's reset time as two separate keys, and on each request you check whether the window's expired and reset it if so:

```
Request A                          Request B
---------                          ---------
GET reset → in the past
                                   GET reset → in the past
SET count = 0                      (both saw an expired window,
SET reset = now + 60                both decide to open a fresh one)
INCR count → 1
                                   SET count = 0     ← wipes A's increment
                                   SET reset = now + 60
                                   INCR count → 1
```

Two requests went through and the counter says `1`. Every racing pair leaks a little, and under real concurrency on a hot key that's constant. The limiter quietly under-counts and clients sail past the limit you swore you set. Nothing throws. You find out from a graph, not an exception.

The fix is to make the read-decide-reset-increment sequence one atomic step, and Redis gives you exactly that with a Lua script — it runs to completion with nothing interleaved:

```lua
-- KEYS[1] = counter key   KEYS[2] = reset key
-- ARGV[1] = now (unix s)   ARGV[2] = window (s)
local reset = tonumber(redis.call('GET', KEYS[2]))
if (not reset) or reset <= tonumber(ARGV[1]) then
  reset = tonumber(ARGV[1]) + tonumber(ARGV[2])
  redis.call('SET', KEYS[1], 0)
  redis.call('SET', KEYS[2], reset)
  redis.call('EXPIREAT', KEYS[1], reset + 1)          -- die a second after the window,
  redis.call('EXPIREAT', KEYS[2], reset + 1)          -- both keys together, never one alone
end
local count = redis.call('INCR', KEYS[1])
return {count, reset}                                  -- one call decides AND reports
```

Two things I got wrong here that are worth stealing. Expire *both* keys, at the *same* absolute time, one second past the window — never let the counter outlive its reset or the other way round, half-a-window is corrupt state that's miserable to reason about. And return `count` *and* `reset` from the one call, because you're about to need both for the response headers, and reading them a second time is the next bug.

## The reset time that wobbles

APIs hand the client a header saying when its window resets — `X-RateLimit-Reset`. Mine wobbled. Same client, back-to-back requests, and the reset time it got back jittered by a second: `10:01:00` on one request, `10:01:01` on the next. Nothing was actually wrong with the limit, but a client doing careful backoff off that header couldn't trust it, and I couldn't explain it.

The cause was that I was computing the reset time as `now + TTL(key)`. The TTL came from Redis over the network; `now` came from the app process. Two different clocks, two different moments, a few milliseconds and a rounding boundary apart. Every so often they landed on different seconds and the header twitched.

The fix is to never *derive* the reset time, store it. Write the absolute reset timestamp as a value (that's what the Lua script above does), and return that exact number to the client every time. It costs you one more key's worth of memory and it makes the header rock-steady, because it's a stored fact now, not a subtraction across two clocks. Don't recompute a thing you can just remember.

## Rejected, with a full tank

This is the one that started the whole bad morning. To keep the decision path cheap I read the counter from a Redis *replica* (reads are plentiful, spread them out) and only sent the increment to the primary. Sounds reasonable. It produced responses that rejected the request *and* reported a full quota remaining, in the same breath.

Two things conspire here. Replicas lag the primary. And — the part I didn't know — a Redis replica doesn't expire keys on its own clock; it waits for the primary to tell it a key is gone. So a replica can happily serve you an expired-but-not-yet-deleted window:

```
Decision (from replica)              Reporting (from primary)
--------------------                 ------------------------
GET count → 100   (at the limit —
   but this window already expired
   on the primary; the replica just
   hasn't been told to drop it yet)
decide: REJECT, 429
                                     INCR count on primary → window's
                                     actually fresh, count = 1
                                     build headers → Remaining: 99
```

You rejected off stale data and reported off fresh data, and the client gets a `429` next to `Remaining: 99`. Two fixes, both needed. Make the decision and the headers come from the *same* atomic result — the script that decides is the script whose numbers you report, no second read. And let the application own expiry: compare the stored reset timestamp to `now` yourself instead of trusting whether a key still exists on a machine that expires lazily and lags. The presence of a key is not the truth. The stored reset time is.

## Free quota from the eviction gods

Here's a subtle one. If your rate-limiter keys live in the same Redis (or Memcached) as your application cache, and that instance runs an LRU eviction policy because it's a *cache*, then under memory pressure it will evict whatever's coldest — and a rate-limiter counter for a client that's mid-window but hasn't hit in a few seconds is exactly that. The counter vanishes. The client's next request finds no key, starts a brand-new full window, and just got handed quota it never earned. During a traffic spike — precisely when the limiter matters — memory pressure is highest and this fires most.

Worse is the partial eviction. Counter and reset are two keys; the eviction policy doesn't know they're a pair. It evicts one and keeps the other, and now you've got a counter with no expiry, or a reset time pointing at a window whose count is gone. Corrupt, half-alive state that behaves differently depending on which half survived, and good luck reproducing it.

The rate limiter is not a cache and must not share a memory pool with one. Give it its own instance (or at least its own logical DB) with an eviction policy set on purpose — `noeviction`, or `volatile-ttl` so only genuinely-expiring keys ever go. A cache is a thing you can afford to lose; a rate-limiter counter that vanishes is free abuse. Don't let them fight over the same bytes.

## When Redis is down

Redis will be unavailable at some point, and the limiter has to decide what to do when it can't reach its own state. Two doors, both bad.

**Fail-open** — if you can't check the limit, allow the request. You stay available, but you've dropped your shield at the exact moment the backend might be under strain, and a flood walks right in on top of whatever already broke Redis.

**Fail-closed** — if you can't check, reject. Now a Redis blip becomes a total API outage, and a thing that was only ever an optimization has taken down the whole service.

Neither blanket answer is right. What I do now is fail-open *with a local fallback* — a coarse, per-process token bucket in memory that kicks in when Redis is unreachable. It won't be globally accurate (each node counts only itself), but it caps the blast radius so you're not fully naked, and it turns a shared-Redis outage into "slightly leaky limits" instead of "no limits" or "no API." And alarm loudly the moment you're running on the fallback, because it's silent by nature — everything keeps working, a bit too generously, and you'll never notice from the outside.

## One box, single-threaded

The reason any of the sharding stuff exists is a fact people forget about Redis: it executes commands on a single thread. It's not memory-bound at the scale a limiter hits, it's CPU-bound, and one instance has a ceiling on commands per second that you *will* reach if you're limiting a large enough front door. When you do, you shard.

Shard by hashing the rate-limit key to one of N clusters, so a given key always lands on the same cluster — the state for one user stays in one place and stays consistent — but the *load* spreads across clusters. Each cluster is a primary plus replicas: increments (the decisions) go to the primary, and genuinely read-only traffic that doesn't gate anything — a usage dashboard, a "remaining" number on a page — can come off replicas, as long as you never make the actual *limit decision* from a lagging replica (see three sections up for how that goes).

The trap in sharding by key: it does nothing for a single hot key. One abusive client hammering one token is one key, which is one shard, and sharding spread everyone *else* out but left that shard carrying the whole storm. Sharding fixes aggregate throughput, not a hot spot. A hot key needs local shedding at the edge before it ever reaches Redis, or its own fatter box — spreading the *other* keys around doesn't help the one that's actually on fire.

## Charge before or after?

Last one, and it's a genuine judgment call, not a bug. Do you count the request when it *arrives* or when it *finishes*?

Count on arrival (reserve the token up front) and you protect the backend properly — nothing runs until it's paid — but you charge clients for work that didn't happen. A request that 500s on your side, or comes back `304 Not Modified` because nothing changed, still burned a token. Feels unfair, and clients notice.

Count on completion and you're fairer, but a client can have a pile of slow requests in flight, none of them counted yet, briefly blowing past the limit while they all run.

The answer I've settled on is reserve-then-refund: charge a token on the way in so the backend's always protected, and hand it back on the responses you've decided are free — a `304`, or a request you rejected at your own validation layer before it did any real work. It's a little more bookkeeping, but it gets you protection *and* fairness instead of picking one. Decide which responses are "free" deliberately, and write it down, because "why did that `304` cost me a request" is a support ticket waiting to happen.

## The mistakes, in one place

Everything above, squeezed into the list I'd actually paste into a review:

- **Fixed window on an adversarial limit** → 2× the limit straddles the boundary. Use a sliding-window counter.
- **Counter and reset in two commands, no atomicity** → racing requests reset each other and the limit leaks. One Lua script.
- **Two keys with different (or missing) expiries** → half-a-window of corrupt state. Expire both at the same absolute time.
- **Reset header computed as `now + TTL`** → it wobbles across two clocks. Store the absolute reset time and return that.
- **Deciding off a replica** → reject-with-full-tank, because replicas lag and expire lazily. Decide and report from the same atomic result.
- **Rate-limiter keys sharing a cache's LRU pool** → eviction hands out free windows and partial evictions corrupt state. Own instance, eviction off on purpose.
- **No fallback when Redis is down** → blanket fail-open drops the shield, blanket fail-closed drops the API. Local fallback limiter, alarm on it.
- **Sharding by key and calling a hot key solved** → it isn't; one key is one shard. Shed hot keys at the edge.
- **Silent about the charging model** → clients rage about quota spent on `304`s. Reserve, refund the free ones, document which are free.

## So is it worth it

After all that, yeah, I still start every new limiter as a fixed-window counter, three lines, and I still think that's right. Reach for the fancy algorithm the day the boundary burst or the memory or the smoothness actually bites, not before. What the three lines leave out isn't the counting — `INCR` and a TTL genuinely handle the counting — it's everything around the counting: the seam in the window, the two-command race, the wobbling clock, the lagging replica, the eviction that shouldn't touch you, the outage you didn't plan for, the single thread you'll outgrow, the token you charged for nothing.

And every one of those traces back to the same boring fact, the one caching taught me too: the instant you keep shared state and put it behind a network, you own its concurrency, its clocks, its failures, and its lies. No algorithm fixes that, you just pay for the state you keep. What I've come to like about the plain counter is that it doesn't pretend otherwise — all the bookkeeping sits right there in a Lua script you can read, where the sharp edges are yours to see instead of the framework's to hide. Delete-on-write has its six-item discipline; rate limiting has this one. Do these and the three lines hold up under the traffic that would otherwise find every edge for you, on a Monday, the expensive way. Ask me how I know.
