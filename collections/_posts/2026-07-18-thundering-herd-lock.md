---
layout:     post
title:      Our Fix for the Thundering Herd Was a Lock
date:       2026-07-18
description:    A hot cached price expires, the herd hits the database, so we wrapped the recompute in a SET NX lock. The p99 didn't budge, and the day a pod died mid-recompute the tail hit 3.5 seconds. Here's why the lock was the wrong tool, measured, and what actually fixed it.
categories: redis caching thundering-herd operations
---

We had a pricing endpoint that cached an expensive aggregate for 60 seconds. It was expensive enough, close to a third of a second to compute, that we absolutely did not want to run it on every request, so we cached it and moved on. Then every sixty seconds the cache entry expired, and because every app instance had cached it at roughly the same moment, they all missed at the same moment, and the whole fleet stampeded the database at once. Classic thundering herd.

The fix looked obvious. Put a lock around the recompute so only one request does the work and everyone else waits for it. We shipped `SET NX` as a recompute lock, watched the database load spike flatten out, and felt good about it. The p99 did not get better. And a couple of weeks later, when a pod got killed mid-recompute during a deploy, the p99 didn't just stay bad, it went to three and a half seconds.

I rebuilt the whole thing to understand why the obvious fix was the wrong one. 64 concurrent workers hammering one hot key, a recompute that takes about 300 ms, a 2-second TTL to keep the run short, and four strategies measured back to back.

<style>
.cache-bench {
  --cb-bg: #f7f9fb;
  --cb-text: #333333;
  --cb-muted: #666666;
  --cb-grid: rgba(0, 0, 0, 0.12);
  --cb-blue: #0076df;
  --cb-orange: #d65f3c;
  --cb-green: #23856d;
  --cb-purple: #7b5bb5;
  margin: 1.8rem 0;
  padding: 1rem 1.1rem;
  border: 1px solid var(--cb-grid);
  border-radius: 8px;
  background: var(--cb-bg);
  color: var(--cb-text);
}
.cache-bench h3 { margin: 0 0 1rem; color: var(--cb-text); font-size: 1rem; }
.cache-bench figcaption { margin-top: 0.9rem; color: var(--cb-muted); font-size: 0.82rem; line-height: 1.45; }
.cb-bar-row { display: grid; grid-template-columns: minmax(8rem, 1.3fr) minmax(6rem, 4fr) minmax(4.4rem, 0.9fr); gap: 0.55rem; align-items: center; margin: 0.42rem 0; font-size: 0.78rem; }
.cb-track { height: 0.72rem; overflow: hidden; border-radius: 999px; background: var(--cb-grid); }
.cb-fill { display: block; width: var(--value); min-width: 2px; height: 100%; border-radius: inherit; background: var(--bar, var(--cb-blue)); }
.cb-value { color: var(--cb-muted); text-align: right; font-variant-numeric: tabular-nums; }
.cb-svg { display: block; width: 100%; height: auto; overflow: visible; }
.cb-svg text { fill: var(--cb-muted); font: 12px system-ui, sans-serif; }
.cb-svg .grid { stroke: var(--cb-grid); stroke-width: 1; }
.cb-svg .lock { fill: none; stroke: var(--cb-orange); stroke-width: 2.5; stroke-linejoin: round; }
.cb-svg .prob { fill: none; stroke: var(--cb-green); stroke-width: 2.5; stroke-linejoin: round; }
.cb-legend { display: flex; gap: 1rem; margin-top: 0.5rem; color: var(--cb-muted); font-size: 0.78rem; }
.cb-swatch { width: 0.8rem; height: 0.22rem; margin-right: 0.3rem; display: inline-block; vertical-align: middle; background: var(--swatch); }
@media (prefers-color-scheme: dark) {
  .cache-bench {
    --cb-bg: #252525;
    --cb-text: #e0e0e0;
    --cb-muted: #b0b0b0;
    --cb-grid: rgba(255, 255, 255, 0.14);
    --cb-blue: #4dabf7;
    --cb-orange: #ff8a65;
    --cb-green: #51cf66;
    --cb-purple: #b197fc;
  }
}
:root[data-theme="dark"] .cache-bench {
  --cb-bg: #252525;
  --cb-text: #e0e0e0;
  --cb-muted: #b0b0b0;
  --cb-grid: rgba(255, 255, 255, 0.14);
  --cb-blue: #4dabf7;
  --cb-orange: #ff8a65;
  --cb-green: #51cf66;
  --cb-purple: #b197fc;
}
</style>

## The herd, to have a baseline

With a plain TTL and no coordination, the key expires, all 64 workers miss at the same instant, and all 64 recompute. Between expiries every read is a cache hit and comes back in well under a millisecond. On the expiry, everyone eats a full recompute. The p99 came out at 302 ms, and it's a sawtooth: near zero for two seconds, then a spike every time the key turns over. The database, meanwhile, takes 64 recomputes in a single burst each cycle, which is the load spike we actually set out to kill.

## The lock, which didn't do what we thought

So we did the obvious thing. One request wins a lock and recomputes, everyone else waits for it and reads the fresh value:

```python
def get_price(r):
    v = r.get(KEY)
    if v is not None:
        return v
    if r.set(KEY + ":lock", "1", nx=True, ex=3):   # I won, I recompute
        v = recompute()
        r.set(KEY, v, px=2000)
        r.delete(KEY + ":lock")
        return v
    while True:                                     # I lost, I wait for the winner
        v = r.get(KEY)
        if v is not None:
            return v
        time.sleep(0.01)
```

The database load did drop, exactly one recompute per cycle instead of 64. But look at what happened to the latency: p99 came out at 286 ms, basically identical to the herd's 302. Of course it did. Every waiter is still blocked for the length of one recompute, the only thing that changed is they're waiting on a lock holder instead of on their own query. We didn't remove the 300 ms wait, we just elected one process to do the work and made everyone else queue behind it. The lock reduced database load and did nothing at all for the thing users actually feel.

<figure class="cache-bench">
  <h3>p99 over time: the lock (our fix) vs probabilistic refresh</h3>
  <svg class="cb-svg" viewBox="0 0 640 250" role="img" aria-labelledby="herd-p99-title herd-p99-desc">
    <title id="herd-p99-title">p99 latency across the run for the lock vs probabilistic strategies</title>
    <desc id="herd-p99-desc">The lock spikes to about 300ms every time the key expires; the probabilistic strategy stays flat near the bottom.</desc>
    <line class="grid" x1="80" y1="210" x2="600" y2="210" />
    <line class="grid" x1="80" y1="120" x2="600" y2="120" />
    <line class="grid" x1="80" y1="30"  x2="600" y2="30" />
    <text x="34" y="214">0</text>
    <text x="20" y="124">165 ms</text>
    <text x="20" y="34">330 ms</text>
    <polyline class="lock" points="90,41 129,209 168,44 208,209 247,45 286,209 325,209 365,45 404,206 443,45 482,209 522,42 561,210 600,210" />
    <polyline class="prob" points="90,39 129,209 168,209 208,207 247,209 286,209 325,209 365,209 404,207 443,209 482,209 522,205 561,210 600,210" />
  </svg>
  <div class="cb-legend">
    <span><span class="cb-swatch" style="--swatch:var(--cb-orange)"></span>lock (recompute mutex)</span>
    <span><span class="cb-swatch" style="--swatch:var(--cb-green)"></span>probabilistic early refresh</span>
  </div>
  <figcaption>Per-second p99 across the run. The lock spikes to ~300ms on every expiry, same sawtooth the plain herd has; probabilistic refresh stays flat near 2ms. Both spike once at the very start, because the cache is genuinely cold on the first request. Measured on Redis 7.4.9, 64 workers, results in benchmarks/cache-stampede/results/.</figcaption>
</figure>

And then there's the failure we didn't think about until it happened in production. The lock holder is a single process, and processes die. If the holder gets killed after it takes the lock but before it writes the value, the lock just sits there until its own TTL runs out, and every single waiter blocks that entire time. I modeled it by killing the holder mid-recompute one time in five, and the tail went exactly where you'd expect:

<figure class="cache-bench">
  <h3>Worst stall in the run (max latency)</h3>
  <div class="cb-bar-row"><span>herd</span><span class="cb-track"><span class="cb-fill" style="--value:10.2%;--bar:var(--cb-blue)"></span></span><span class="cb-value">359 ms</span></div>
  <div class="cb-bar-row"><span>lock</span><span class="cb-track"><span class="cb-fill" style="--value:9%;--bar:var(--cb-blue)"></span></span><span class="cb-value">317 ms</span></div>
  <div class="cb-bar-row"><span>lock + holder crash</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">3,516 ms</span></div>
  <div class="cb-bar-row"><span>probabilistic</span><span class="cb-track"><span class="cb-fill" style="--value:9%;--bar:var(--cb-green)"></span></span><span class="cb-value">316 ms</span></div>
  <figcaption>The moment a lock holder died mid-recompute, every waiter blocked until the 3-second lock TTL expired, and the worst request in the run took 3,516 ms. The lock didn't just fail to fix p99, it added a brand-new way to stall the whole fleet on one process's death.</figcaption>
</figure>

3,516 ms. The lock took a load problem and handed us a latency problem and a single point of failure, and left the p99 exactly where it was.

## What actually fixed it: refresh before anyone's waiting

The real problem was never "too many processes recompute at once." It was "the recompute happens at the exact moment a request needs the value, so somebody always waits for it." So stop waiting for expiry. Refresh the value a little *before* it expires, in the background, while the cached copy is still perfectly good to serve.

The trick is deciding who refreshes and when without coordinating. Each reader, on every hit, rolls a random number and refreshes early if it comes up. The closer the value is to expiring, and the more expensive it was to compute last time, the higher the chance any given reader volunteers. So exactly one reader (near enough) tends to refresh ahead of the crowd, and it does it in the background, so its own request doesn't block either:

```python
def get_price(r):
    raw = r.get(KEY)
    if raw is None:                                 # genuinely cold, rare under load
        return recompute_and_store(r)
    d = json.loads(raw)   # {value, delta (last compute time), exp (expiry)}
    if time.time() - d["delta"] * BETA * math.log(random.random()) >= d["exp"]:
        background_refresh(r)                        # volunteer, but don't wait for it
    return d["value"]                                # serve the still-valid value now
```

That `delta * log(random())` term is the whole idea: it's a random amount of time, weighted by how long the recompute takes, that pulls the effective refresh moment earlier. A reader that happens to roll a large value refreshes well ahead of expiry; most readers roll small and just serve the cache. The key never actually goes cold under load, because someone always tops it up first.

The p99 came out at 2.3 ms. The sawtooth is gone completely, you can see it flat along the bottom of the chart above. The database still only sees about one recompute per cycle, same as the lock. Nobody blocks, nobody stampedes, and there's no holder whose death takes everyone down, because the refresh is best-effort and the cache is never actually empty when it happens.

## The other half: synchronized expiries manufacture the herd

There's a second bug hiding underneath all of this, and it's the reason the herd was so sharp in the first place. Every instance cached the value at about the same time with the same TTL, so every instance's copy expired at the same time. Synchronized expiry is what turns "a cache miss" into "sixty-four simultaneous cache misses."

Jitter fixes that. If you spread the TTLs out by a random amount, the expiries desynchronize and the miss, if you get one at all, is spread over time instead of landing all at once. I set 300 keys with the same TTL and then with a TTL jittered by plus or minus fifty percent, and counted how many expired inside the same quarter-second window:

<figure class="cache-bench">
  <h3>Keys expiring in the same window (herd size)</h3>
  <div class="cb-bar-row"><span>same TTL</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">300</span></div>
  <div class="cb-bar-row"><span>±50% jittered TTL</span><span class="cb-track"><span class="cb-fill" style="--value:15.7%;--bar:var(--cb-green)"></span></span><span class="cb-value">47</span></div>
  <figcaption>With one shared TTL, all 300 keys expired in a single 250ms window. With the TTL jittered by ±50%, the fullest window held 47. The herd doesn't need a locking strategy if it never gathers in the first place.</figcaption>
</figure>

All 300 at once became at most 47. Probabilistic refresh handles the single hot key; jitter handles the fact that lots of keys were set to die together.

## Stuff worth remembering

- A recompute lock does not fix the latency of a stampede. It serializes it. Every waiter still blocks for one recompute, so your p99 stays about where it was; all you actually bought was lower database load. If load was your problem the lock helps, but if latency was your problem it doesn't, and it's easy to conflate the two.
- The lock also adds a single point of failure. When the holder dies between taking the lock and writing the value, everyone waits the full lock TTL. In my run that was a 3,516 ms stall out of a workload whose p99 was otherwise 300 ms.
- Probabilistic early recomputation is the fix for the latency. One reader refreshes ahead of expiry in the background, so the value is never cold when a request arrives and nobody blocks. It sits right on top of a normal cache read, a few lines, no coordination.
- Jitter your TTLs. Seeding many instances or many keys with the same absolute expiry is what builds the herd; a little randomness in the TTL stops them from expiring in lockstep.
- These are laptop numbers demonstrating the mechanism, [the load generator and the four strategies are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/cache-stampede). The milliseconds are from my machine; the shapes hold anywhere.

Reaching for a lock when a herd is trampling your database is the right instinct about the problem and the wrong tool for it. The recompute you actually want isn't the one you carefully allowed only one process to run. It's the one that already finished, a moment before anybody asked.
