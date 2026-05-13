---
layout:     post
title:      Everything I Got Wrong About Rate Limiting
date:       2026-07-18
description:    A rate limiter is three lines and everyone writes the same fixed-window counter, myself included. Then a boundary burst walks through, Redis eviction hands out free quota, and a stale replica makes the decision disagree with the headers. Here's the algorithm menu, the atomic Lua the counter actually needs, and every sharp edge that has personally bitten me.
categories: rate-limiting redis distributed-systems api
---

The first rate limiter I ever shipped was three lines and I was quietly proud of it. It ran fine for months. Then one morning a single client pushed far more traffic through a window edge than the limit was meant to allow, and around the same time another client got a `429` while the response headers I handed back cheerfully told it there was quota left. I spent that morning staring at two facts that couldn't both be true and slowly realising the three lines had been lying to me the whole time.

A rate limiter is three lines. Increment a counter, put a TTL on it, reject the request if the counter is over the limit. You'll write it, I wrote it, everyone writes the same three lines and they all pass in the demo. Then a real client with a real burst shows up, then you put Redis behind it and add a replica, and you find out the three lines were hiding about eight different ways to be wrong. This is the stuff I wish someone had handed me before I learned it the expensive way, one support ticket at a time.

I wanted numbers before keeping any of the dramatic ones in this post, so I put a Redis primary and replica in Docker and ran all five failures on this laptop. The [limiter implementations, harness, raw CSVs, and exact command are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/rate-limiter). These are comparisons on one machine, not production capacity numbers. The two-command race is swept across an injected 0/5/10/25 ms gap, and the replica test deliberately pauses replication for 0/10/25/50 ms. Both timing knobs are in the charts because hiding the amplification would make the numbers useless.

<style>
.rl-bench {
  --rl-bg: #f7f9fb;
  --rl-text: #333333;
  --rl-muted: #666666;
  --rl-grid: rgba(0, 0, 0, 0.12);
  --rl-blue: #0076df;
  --rl-orange: #d65f3c;
  --rl-green: #23856d;
  --rl-purple: #7b5bb5;
  margin: 1.8rem 0;
  padding: 1rem 1.1rem;
  border: 1px solid var(--rl-grid);
  border-radius: 8px;
  background: var(--rl-bg);
  color: var(--rl-text);
}
.rl-bench h3 { margin: 0 0 1rem; color: var(--rl-text); font-size: 1rem; }
.rl-bench figcaption { margin-top: 0.9rem; color: var(--rl-muted); font-size: 0.82rem; line-height: 1.45; }
.rl-panels { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.25rem; }
.rl-panel-title { margin: 0 0 0.55rem; color: var(--rl-muted); font-size: 0.78rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
.rl-bar-row { display: grid; grid-template-columns: minmax(7rem, 1.3fr) minmax(7rem, 4fr) minmax(4.4rem, 0.9fr); gap: 0.55rem; align-items: center; margin: 0.45rem 0; font-size: 0.78rem; }
.rl-track { position: relative; height: 0.72rem; overflow: hidden; border-radius: 999px; background: var(--rl-grid); }
.rl-track.reference::after { content: ""; position: absolute; left: var(--reference); top: -2px; bottom: -2px; width: 2px; background: var(--rl-text); opacity: 0.8; }
.rl-fill { display: block; width: var(--value); min-width: 2px; height: 100%; border-radius: inherit; background: var(--bar, var(--rl-blue)); }
.rl-value { color: var(--rl-muted); text-align: right; font-variant-numeric: tabular-nums; }
.rl-svg { display: block; width: 100%; height: auto; overflow: visible; }
.rl-svg text { fill: var(--rl-muted); font: 12px system-ui, sans-serif; }
.rl-svg .grid { stroke: var(--rl-grid); stroke-width: 1; }
.rl-svg .series-a { fill: none; stroke: var(--rl-orange); stroke-width: 3; stroke-linejoin: round; stroke-linecap: round; }
.rl-svg .series-b { fill: none; stroke: var(--rl-blue); stroke-width: 3; stroke-linejoin: round; stroke-linecap: round; }
.rl-svg .series-c { fill: none; stroke: var(--rl-green); stroke-width: 3; stroke-linejoin: round; stroke-linecap: round; }
.rl-legend { display: flex; flex-wrap: wrap; gap: 1rem; margin-top: 0.5rem; color: var(--rl-muted); font-size: 0.78rem; }
.rl-swatch { width: 0.8rem; height: 0.22rem; margin-right: 0.3rem; display: inline-block; vertical-align: middle; background: var(--swatch); }
@media (prefers-color-scheme: dark) {
  .rl-bench {
    --rl-bg: #252525;
    --rl-text: #e0e0e0;
    --rl-muted: #b0b0b0;
    --rl-grid: rgba(255, 255, 255, 0.14);
    --rl-blue: #4dabf7;
    --rl-orange: #ff8a65;
    --rl-green: #51cf66;
    --rl-purple: #b197fc;
  }
}
:root[data-theme="dark"] .rl-bench {
  --rl-bg: #252525;
  --rl-text: #e0e0e0;
  --rl-muted: #b0b0b0;
  --rl-grid: rgba(255, 255, 255, 0.14);
  --rl-blue: #4dabf7;
  --rl-orange: #ff8a65;
  --rl-green: #51cf66;
  --rl-purple: #b197fc;
}
@media (max-width: 620px) {
  .rl-panels { grid-template-columns: 1fr; }
  .rl-bar-row { grid-template-columns: minmax(6.5rem, 1.3fr) minmax(5rem, 3fr) minmax(4rem, 0.9fr); gap: 0.4rem; }
}
</style>

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

The thing that caught me off guard early is that "rate limiting" isn't one algorithm, it's about five, and they trade accuracy for memory and smoothness in different places. People say "rate limit" and mean any of these:

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

The point of the section is that picking the algorithm is an accuracy-vs-memory-vs-smoothness call, the same way caching is a staleness-vs-freshness call. Fixed window ends up being everyone's default because it's the cheapest to run, and cheap is about the only thing it's automatically good at.

## The window that lets through double

The fixed-window counter has one flaw baked into its shape, and it's the first thing that bit me. The window resets on a hard boundary. So a client that's paying attention sends its whole limit at the very end of one window and its whole limit again at the very start of the next:

```
benchmark limit = 100 / 2 seconds

1.990s   ├─ 100 requests ─┤          window A counter → 100, allowed
2.010s   ├─ 100 requests ─┤          window B counter → 100, allowed
         └──── 200 admitted inside one rolling 2s interval ────┘
```

That is what the harness measured: all 200 got through the fixed window, a 100% overshoot. The sliding counter admitted 101, a 1% overshoot from the approximation. Under the uniform stream, both peaked at exactly 100. For an abuse limit, that seam-aware client is the whole reason the algorithm choice matters.

<figure class="rl-bench">
  <h3>Peak admits in any rolling two-second interval</h3>
  <div class="rl-bar-row"><span>Fixed window</span><span class="rl-track reference" style="--reference:50%"><span class="rl-fill" style="--value:100%;--bar:var(--rl-orange)"></span></span><span class="rl-value">200</span></div>
  <div class="rl-bar-row"><span>Sliding counter</span><span class="rl-track reference" style="--reference:50%"><span class="rl-fill" style="--value:50.5%;--bar:var(--rl-blue)"></span></span><span class="rl-value">101</span></div>
  <figcaption>Limit 100 per two seconds; the black marker is the promised limit. Both algorithms peaked at 100 under the uniform stream, so this chart shows the seam-aware burst.</figcaption>
</figure>

The sliding-window counter fixes most of this without a full log, you keep this window's count and last window's count and blend them by how much of the previous window is still in view:

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

It is smooth. After its first warm-up second, the fixed counter admitted `8, 8, 8, 8, 8, 8, 2, 0, 0, 0` in each set of 100 ms buckets. The sliding counter settled at five every bucket. The approximation error was not small in this workload, though. Against the exact log, it disagreed on 1,102 of 2,400 individual decisions, 45.92%, even though both algorithms admitted the same 1,500 requests overall. They spent the same quota at different moments.

<figure class="rl-bench">
  <h3>Admits per 100 ms under 80 offered requests/second</h3>
  <svg class="rl-svg" viewBox="0 0 700 270" role="img" aria-labelledby="smooth-title smooth-desc">
    <title id="smooth-title">Fixed-window sawtooth compared with a sliding counter</title>
    <desc id="smooth-desc">After warm-up, fixed window repeatedly admits eight requests in early buckets and zero in late buckets. Sliding counter admits five in every bucket.</desc>
    <line class="grid" x1="50" y1="45" x2="680" y2="45"></line>
    <line class="grid" x1="50" y1="132" x2="680" y2="132"></line>
    <line class="grid" x1="50" y1="220" x2="680" y2="220"></line>
    <line class="grid" x1="260" y1="35" x2="260" y2="220"></line>
    <line class="grid" x1="470" y1="35" x2="470" y2="220"></line>
    <text x="42" y="49" text-anchor="end">8</text>
    <text x="42" y="136" text-anchor="end">4</text>
    <text x="42" y="224" text-anchor="end">0</text>
    <text x="50" y="243">0s</text>
    <text x="260" y="243" text-anchor="middle">1s</text>
    <text x="470" y="243" text-anchor="middle">2s</text>
    <text x="680" y="243" text-anchor="end">3s</text>
    <polyline class="series-a" points="50,45 71,45 92,45 113,45 134,45 155,45 176,176 197,220 218,220 239,220 260,45 281,45 302,45 323,45 344,45 365,45 386,176 407,220 428,220 449,220 470,45 491,45 512,45 533,45 554,45 575,45 596,176 617,220 638,220 659,220"></polyline>
    <polyline class="series-b" points="50,45 71,45 92,45 113,45 134,45 155,45 176,176 197,220 218,220 239,220 260,111 281,111 302,111 323,111 344,111 365,111 386,111 407,111 428,111 449,111 470,111 491,111 512,111 533,111 554,111 575,111 596,111 617,111 638,111 659,111"></polyline>
  </svg>
  <div class="rl-panels">
    <div class="rl-legend">
      <span><i class="rl-swatch" style="--swatch:var(--rl-orange)"></i>Fixed window</span>
      <span><i class="rl-swatch" style="--swatch:var(--rl-blue)"></i>Sliding counter</span>
    </div>
    <div>
      <p class="rl-panel-title">Counter vs exact log</p>
      <div class="rl-bar-row"><span>Disagreement</span><span class="rl-track"><span class="rl-fill" style="--value:45.917%;--bar:var(--rl-purple)"></span></span><span class="rl-value">45.92%</span></div>
    </div>
  </div>
  <figcaption>Thirty virtual seconds, limit 50/second, eight offered requests per 100 ms. The exact log and counter each admitted 1,500; the disagreement bar measures which individual requests got different answers.</figcaption>
</figure>

Two keys instead of one, both with a short TTL. I still reach for it when smooth output matters, but I no longer describe the approximation as small without measuring the traffic shape first.

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

Two requests went through and the counter says `1`. The limiter quietly under-counts and clients sail past the limit you swore you set. Nothing throws. You find out from a graph, not an exception.

I forced 30 expired-window rollovers with eight clients colliding on each one, then filled whatever quota the counter claimed was left. With no injected delay, the naive pair admitted 3,018 against a budget of 3,000, a 0.60% leak. At a disclosed 25 ms gap between reading the reset and writing the new state, it admitted 3,100, a 3.33% leak. The Lua version admitted exactly 3,000 at every point in the sweep.

<figure class="rl-bench">
  <h3>Quota leaked by the two-command reset race</h3>
  <div class="rl-panels">
    <div>
      <p class="rl-panel-title">At the 25 ms injected gap</p>
      <div class="rl-bar-row"><span>Two commands</span><span class="rl-track"><span class="rl-fill" style="--value:83.325%;--bar:var(--rl-orange)"></span></span><span class="rl-value">3.33%</span></div>
      <div class="rl-bar-row"><span>Atomic Lua</span><span class="rl-track"><span class="rl-fill" style="--value:0%;--bar:var(--rl-green)"></span></span><span class="rl-value">0.00%</span></div>
    </div>
    <div>
      <svg class="rl-svg" viewBox="0 0 700 250" role="img" aria-labelledby="race-title race-desc">
        <title id="race-title">Overshoot versus injected read-to-reset gap</title>
        <desc id="race-desc">Naive overshoot rises from 0.6 percent at zero gap to 3.33 percent at 25 milliseconds. Atomic Lua stays at zero.</desc>
        <line class="grid" x1="60" y1="50" x2="650" y2="50"></line>
        <line class="grid" x1="60" y1="125" x2="650" y2="125"></line>
        <line class="grid" x1="60" y1="200" x2="650" y2="200"></line>
        <text x="52" y="54" text-anchor="end">4%</text>
        <text x="52" y="129" text-anchor="end">2%</text>
        <text x="52" y="204" text-anchor="end">0%</text>
        <text x="60" y="224">0ms</text>
        <text x="178" y="224" text-anchor="middle">5</text>
        <text x="296" y="224" text-anchor="middle">10</text>
        <text x="650" y="224" text-anchor="end">25ms</text>
        <polyline class="series-a" points="60,178 178,155 296,141 650,75"></polyline>
        <polyline class="series-c" points="60,200 178,200 296,200 650,200"></polyline>
      </svg>
      <div class="rl-legend">
        <span><i class="rl-swatch" style="--swatch:var(--rl-orange)"></i>Two commands</span>
        <span><i class="rl-swatch" style="--swatch:var(--rl-green)"></i>Atomic Lua</span>
      </div>
    </div>
  </div>
  <figcaption>Limit 100 per window, 30 controlled rollovers, eight racing clients. The gap is deliberate timing amplification; the 0 ms point is the localhost baseline.</figcaption>
</figure>

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

The fix is to never *derive* the reset time, store it. Write the absolute reset timestamp as a value (that's what the Lua script above does), and return that exact number to the client every time. It costs you one more key's worth of memory and it makes the header rock-steady, because you're reading back a stored fact instead of subtracting across two clocks every time.

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
                                     build headers → Remaining: at least 80
```

I measured the contradiction as a rejection where the fresh primary result still reported at least 80 of 100 requests remaining. Even the localhost baseline produced 3 contradictions in 120 decisions, 2.50%. With replication deliberately paused for 50 ms, that became 44 in 120, 36.67%. Making the decision and headers from the primary's one atomic result produced zero contradictions at every lag.

<figure class="rl-bench">
  <h3>Rejected while the fresh result still showed ≥80 remaining</h3>
  <svg class="rl-svg" viewBox="0 0 700 260" role="img" aria-labelledby="replica-title replica-desc">
    <title id="replica-title">Contradiction rate versus injected replica lag</title>
    <desc id="replica-desc">Replica-based decisions rise from 2.5 percent contradictions at zero injected lag to 36.67 percent at 50 milliseconds. Atomic primary decisions stay at zero.</desc>
    <line class="grid" x1="60" y1="50" x2="650" y2="50"></line>
    <line class="grid" x1="60" y1="125" x2="650" y2="125"></line>
    <line class="grid" x1="60" y1="200" x2="650" y2="200"></line>
    <text x="52" y="54" text-anchor="end">40%</text>
    <text x="52" y="129" text-anchor="end">20%</text>
    <text x="52" y="204" text-anchor="end">0%</text>
    <text x="60" y="225">0ms</text>
    <text x="178" y="225" text-anchor="middle">10</text>
    <text x="355" y="225" text-anchor="middle">25</text>
    <text x="650" y="225" text-anchor="end">50ms</text>
    <polyline class="series-a" points="60,191 178,166 355,131 650,63"></polyline>
    <polyline class="series-c" points="60,200 178,200 355,200 650,200"></polyline>
  </svg>
  <div class="rl-legend">
    <span><i class="rl-swatch" style="--swatch:var(--rl-orange)"></i>Decide from replica</span>
    <span><i class="rl-swatch" style="--swatch:var(--rl-green)"></i>Decide + report from primary Lua</span>
  </div>
  <figcaption>Three cycles and 120 decisions per point. The harness detaches replication for 0/10/25/50 ms to make localhost lag observable, then reattaches and verifies the replica after every cycle.</figcaption>
</figure>

Two fixes, both needed. Make the decision and the headers come from the *same* atomic result — the script that decides is the script whose numbers you report, no second read. And let the application own expiry: compare the stored reset timestamp to `now` yourself instead of trusting whether a key still exists on a machine that expires lazily and lags. Whether the key is still sitting there tells you nothing you can rely on; the reset time you stored does.

## Free quota from the eviction gods

Here's a subtle one. If your rate-limiter keys live in the same Redis (or Memcached) as your application cache, and that instance runs an LRU eviction policy because it's a *cache*, then under memory pressure it will evict whatever's coldest — and a rate-limiter counter for a client that's mid-window but hasn't hit in a few seconds is exactly that. The counter vanishes. The client's next request finds no key, starts a brand-new full window, and just got handed quota it never earned. During a traffic spike — precisely when the limiter matters — memory pressure is highest and this fires most.

Worse is the partial eviction. Counter and reset are two keys; the eviction policy doesn't know they're a pair. It evicts one and keeps the other, and now you've got a counter with no expiry, or a reset time pointing at a window whose count is gone. Corrupt, half-alive state that behaves differently depending on which half survived, and good luck reproducing it.

The rate limiter is not a cache and must not share a memory pool with one. Give it its own instance (or at least its own logical DB) with an eviction policy set on purpose — `noeviction`, or `volatile-ttl` so only genuinely-expiring keys ever go. You can afford to lose a cache entry, that's the whole point of a cache, but a rate-limiter counter that gets evicted mid-window is just free abuse, so don't make the two of them compete for the same memory.

## When Redis is down

Redis will be unavailable at some point, and the limiter has to decide what to do when it can't reach its own state. Two doors, both bad.

**Fail-open** — if you can't check the limit, allow the request. You stay available, but you've dropped your shield at the exact moment the backend might be under strain, and a flood walks right in on top of whatever already broke Redis.

**Fail-closed** — if you can't check, reject. Now a Redis blip becomes a total API outage, and a thing that was only ever an optimization has taken down the whole service.

Neither blanket answer is right. What I do now is fail-open *with a local fallback* — a coarse, per-process token bucket in memory that kicks in when Redis is unreachable. It won't be globally accurate (each node counts only itself), but it caps the blast radius so you're not fully naked, and it turns a shared-Redis outage into "slightly leaky limits" instead of "no limits" or "no API." And alarm loudly the moment you're running on the fallback, because it's silent by nature — everything keeps working, a bit too generously, and you'll never notice from the outside.

## One box, single-threaded

The reason any of the sharding stuff exists is a fact people forget about Redis: it executes commands on a single thread. It's not memory-bound at the scale a limiter hits, it's CPU-bound, and one instance has a ceiling on commands per second that you *will* reach if you're limiting a large enough front door. When you do, you shard.

Shard by hashing the rate-limit key to one of N clusters, so a given key always lands on the same cluster — the state for one user stays in one place and stays consistent — but the *load* spreads across clusters. Each cluster is a primary plus replicas: increments (the decisions) go to the primary, and genuinely read-only traffic that doesn't gate anything — a usage dashboard, a "remaining" number on a page — can come off replicas, as long as you never make the actual *limit decision* from a lagging replica (see three sections up for how that goes).

The trap in sharding by key: it does nothing for a single hot key. One abusive client hammering one token is one key, which is one shard, and sharding spread everyone *else* out but left that shard carrying the whole storm. It buys you aggregate throughput and does nothing for a single hot spot. A hot key needs local shedding at the edge before it ever reaches Redis, or its own fatter box — spreading the *other* keys around doesn't help the one that's actually on fire.

On this laptop, one primary handled 283,791 Lua decisions/second with keys spread across the keyspace. Promoting the replica to a second primary and splitting those keys reached 400,032/second, a 41% gain. Both containers share the same laptop, so I did not get a clean 2× and I would not size production from this number. The hot key made the point more cleanly: 318,595/second on one shard and 299,301/second with two. The second shard sat there looking decorative while every request still landed on shard zero.

<figure class="rl-bench">
  <h3>Lua limiter decisions/second as shards are added</h3>
  <div class="rl-panels">
    <div>
      <p class="rl-panel-title">Keys spread by CRC32</p>
      <div class="rl-bar-row"><span>1 primary</span><span class="rl-track"><span class="rl-fill" style="--value:70.94%;--bar:var(--rl-blue)"></span></span><span class="rl-value">283,791</span></div>
      <div class="rl-bar-row"><span>2 primaries</span><span class="rl-track"><span class="rl-fill" style="--value:100%;--bar:var(--rl-green)"></span></span><span class="rl-value">400,032</span></div>
    </div>
    <div>
      <p class="rl-panel-title">Every request on one hot key</p>
      <div class="rl-bar-row"><span>1 primary</span><span class="rl-track"><span class="rl-fill" style="--value:79.64%;--bar:var(--rl-orange)"></span></span><span class="rl-value">318,595</span></div>
      <div class="rl-bar-row"><span>2 primaries</span><span class="rl-track"><span class="rl-fill" style="--value:74.82%;--bar:var(--rl-purple)"></span></span><span class="rl-value">299,301</span></div>
    </div>
  </div>
  <figcaption>Sixteen spawned processes, pipelines of 256 <code>EVALSHA</code> calls, three-second runs. The replica is promoted only for the two-primary cases and restored afterward. Host timing is the noisiest measurement in this post.</figcaption>
</figure>

## Charge before or after?

Last one, and it's a genuine judgment call, not a bug. Do you count the request when it *arrives* or when it *finishes*?

Count on arrival (reserve the token up front) and you protect the backend properly — nothing runs until it's paid — but you charge clients for work that didn't happen. A request that 500s on your side, or comes back `304 Not Modified` because nothing changed, still burned a token. Feels unfair, and clients notice.

Count on completion and you're fairer, but a client can have a pile of slow requests in flight, none of them counted yet, briefly blowing past the limit while they all run.

The answer I've settled on is reserve-then-refund: charge a token on the way in so the backend's always protected, and hand it back on the responses you've decided are free — a `304`, or a request you rejected at your own validation layer before it did any real work. It's a little more bookkeeping, but it gets you protection *and* fairness instead of picking one. Decide which responses are "free" deliberately, and write it down, because "why did that `304` cost me a request" is a support ticket waiting to happen.

## The mistakes, in one place

Everything above, squeezed into the list I'd actually paste into a review:

- **Fixed window on an adversarial limit** → my 100-request limit admitted 200 across the boundary. Use a sliding-window counter.
- **Counter and reset in two commands, no atomicity** → the naive version leaked 3.33% at the disclosed 25 ms gap. One Lua script.
- **Two keys with different (or missing) expiries** → half-a-window of corrupt state. Expire both at the same absolute time.
- **Reset header computed as `now + TTL`** → it wobbles across two clocks. Store the absolute reset time and return that.
- **Deciding off a replica** → 36.67% contradictory responses at the disclosed 50 ms lag. Decide and report from the same atomic result.
- **Rate-limiter keys sharing a cache's LRU pool** → eviction hands out free windows and partial evictions corrupt state. Own instance, eviction off on purpose.
- **No fallback when Redis is down** → blanket fail-open drops the shield, blanket fail-closed drops the API. Local fallback limiter, alarm on it.
- **Sharding by key and calling a hot key solved** → two primaries did no better than one for the hot key. Shed hot keys at the edge.
- **Silent about the charging model** → clients rage about quota spent on `304`s. Reserve, refund the free ones, document which are free.

## So is it worth it

After all that, yeah, I still start every new limiter as a fixed-window counter, three lines, and I still think that's right. Reach for the fancy algorithm the day the boundary burst or the memory or the smoothness actually bites, not before. What the three lines leave out isn't the counting — `INCR` and a TTL genuinely handle the counting — it's everything around the counting: the seam in the window, the two-command race, the wobbling clock, the lagging replica, the eviction that shouldn't touch you, the outage you didn't plan for, the single thread you'll outgrow, the token you charged for nothing.

And every one of those traces back to the same boring fact, the one caching taught me too: the instant you keep shared state and put it behind a network, you own its concurrency, its clocks, its failures, and its lies. No algorithm fixes that, you just pay for the state you keep. What I've come to like about the plain counter is that it doesn't pretend otherwise — all the bookkeeping sits right there in a Lua script you can read, where the sharp edges are yours to see instead of the framework's to hide. Delete-on-write has its six-item discipline; rate limiting has this one. Do these and the three lines hold up under the traffic that would otherwise find every edge for you, on a Monday, the expensive way. Ask me how I know.
