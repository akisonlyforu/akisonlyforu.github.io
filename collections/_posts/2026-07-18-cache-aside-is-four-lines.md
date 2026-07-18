---
layout:     post
title:      Everything I Got Wrong About Cache-Aside
date:       2026-07-18
description:    Cache-aside is four lines and everyone writes it the same way, myself included. Then it serves a stale row until its TTL, melts the database when one hot key expires, and fills up Redis at 3am. Here's the pattern, when I actually reach for it, and every sharp edge that has personally bitten me.
categories: caching redis distributed-systems performance
---

The first cache I ever shipped ran clean for months. Then traffic climbed past some threshold, concurrency got high enough, and one morning it started quietly serving stale data and hammering the database it was supposed to be protecting. I spent that morning staring at a dashboard trying to work out how a thing built to make the database do *less* work had talked it into doing more.

## The problem

Cache-aside is four lines that everyone writes the same way, and it holds up right until the load gets real. Then it serves a stale row after a write, melts the database when one hot key expires under a burst, and quietly caches the absence of rows that were never there. This is every one of those failure modes reproduced on a real Redis and Postgres, and the small, boring discipline that keeps the four lines honest.

Cache-aside is four lines. Check the cache, on a miss read the database, put the result in the cache, return it. You'll write it, I wrote it, everyone writes the same four lines and they all work in the demo. Then concurrency and real traffic show up and you find out the four lines were hiding about six different ways to be wrong. This is the stuff I wish someone had told me before I learned it the expensive way, one page at a time.

I wanted numbers before writing the rest of this, so I put Postgres and Redis in Docker and ran every failure mode below on this laptop. The whole [harness, schema, raw CSVs, and exact command are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/cache-aside). Treat these as comparisons between two runs on one machine, not as capacity numbers. Both services are one loopback hop away, the rows are tiny, and the race test deliberately lines an expiry up with a write. I widened the reader's DB-to-cache gap by 0, 5, and 20 ms and plotted all three instead of quietly picking the one that made the chart exciting.

The first result was a useful slap. Across 20,000 Zipfian reads, the cache hit 82.28% and cut Postgres reads from 20,000 to 3,544. It was also slower here: p50 went from 0.83 ms to 1.70 ms and p99 from 2.83 ms to 10.19 ms. Local Postgres answering a primary-key lookup is brutally cheap; adding Redis and Python bookkeeping costs more than it saves. In a real service I care about the DB work that disappeared, and I measure latency against the actual query and network instead of assuming Redis has magic in it.

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
.cb-panels { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.25rem; }
.cb-panel-title { margin: 0 0 0.55rem; color: var(--cb-muted); font-size: 0.78rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
.cb-bar-row { display: grid; grid-template-columns: minmax(6.5rem, 1.2fr) minmax(7rem, 4fr) minmax(3.8rem, 0.8fr); gap: 0.55rem; align-items: center; margin: 0.42rem 0; font-size: 0.78rem; }
.cb-track { height: 0.72rem; overflow: hidden; border-radius: 999px; background: var(--cb-grid); }
.cb-fill { display: block; width: var(--value); min-width: 2px; height: 100%; border-radius: inherit; background: var(--bar, var(--cb-blue)); }
.cb-value { color: var(--cb-muted); text-align: right; font-variant-numeric: tabular-nums; }
.cb-group { padding-top: 0.8rem; border-top: 1px solid var(--cb-grid); }
.cb-group:first-of-type { padding-top: 0; border-top: 0; }
.cb-group-label { margin: 0 0 0.35rem; color: var(--cb-muted); font-size: 0.78rem; font-weight: 700; }
.cb-svg { display: block; width: 100%; height: auto; overflow: visible; }
.cb-svg text { fill: var(--cb-muted); font: 12px system-ui, sans-serif; }
.cb-svg .grid { stroke: var(--cb-grid); stroke-width: 1; }
.cb-svg .fixed { fill: none; stroke: var(--cb-orange); stroke-width: 3; stroke-linejoin: round; }
.cb-svg .jittered { fill: none; stroke: var(--cb-blue); stroke-width: 3; stroke-linejoin: round; }
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
@media (max-width: 620px) {
  .cb-panels { grid-template-columns: 1fr; }
  .cb-bar-row { grid-template-columns: minmax(6rem, 1.3fr) minmax(5rem, 3fr) minmax(3.6rem, 0.8fr); gap: 0.4rem; }
}
</style>

<figure class="cache-bench">
  <h3>Local baseline: fewer Postgres reads, higher p99</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">DB reads / 20k requests</p>
      <div class="cb-bar-row"><span>Uncached</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">20,000</span></div>
      <div class="cb-bar-row"><span>Cached</span><span class="cb-track"><span class="cb-fill" style="--value:17.72%;--bar:var(--cb-blue)"></span></span><span class="cb-value">3,544</span></div>
    </div>
    <div>
      <p class="cb-panel-title">p99 request latency</p>
      <div class="cb-bar-row"><span>Uncached</span><span class="cb-track"><span class="cb-fill" style="--value:27.8%;--bar:var(--cb-orange)"></span></span><span class="cb-value">2.83 ms</span></div>
      <div class="cb-bar-row"><span>Cached</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-blue)"></span></span><span class="cb-value">10.19 ms</span></div>
    </div>
  </div>
  <figcaption>20,000 requests, 32 workers, 100,000 rows, Zipf α=1.2. The cached run hit 82.28%; loopback Redis still added more latency than this tiny local query cost.</figcaption>
</figure>

## What it actually is

Cache-aside (people also call it *lazy loading*) puts your application in charge. The cache and the database don't know each other exists, you're the one holding both their hands.

Read path:

- Look in the cache.
- Hit → return it, done.
- Miss → read the database, put it in the cache, return it.

Write path:

- Write the database.
- Delete the cache entry.

That's the whole thing. Two nice properties fall straight out of it. Only data someone actually asked for ends up cached, that's the "lazy" part, you never waste memory on rows nobody reads. And the cache is never the source of truth, so if it's cold, empty, or the whole Redis box is on fire, your app still works, it just gets slower and leans on the database. That second property is the real reason cache-aside is the default almost everywhere, the cache is an optimization you can lose, not a dependency you can't.

```python
def get_user(user_id):
    key = f"user:{user_id}"
    cached = r.get(key)
    if cached is not None:
        return deserialize(cached)
    user = db.query("SELECT * FROM users WHERE id = %s", user_id)   # miss → source of truth
    r.set(key, serialize(user), ex=300)                             # populate, 5 min TTL
    return user

def update_user(user_id, changes):
    db.execute("UPDATE users SET ... WHERE id = %s", user_id)
    r.delete(f"user:{user_id}")                                     # invalidate, don't update
```

Ship that and it mostly works. The rest of this post is about the word "mostly."

## When I actually reach for it

Here's the thing nobody tells you when you're starting out, "caching" isn't one decision, it's five, and they have different names and different failure modes. People say cache and mean any of these:

| Strategy | On a write | I reach for it when |
|---|---|---|
| **Cache-aside** (lazy) | Write the DB, delete the key, walk away | Read-heavy, a few seconds of staleness won't hurt anyone, only a hot subset is worth caching, and the cache and DB are separate systems (Redis + Postgres). This is 80% of the time. |
| **Read-through** | You don't do it by hand, it's write-through's other half and the cache does the loading | I want the cache-loading logic to live inside the cache layer, not smeared across app code |
| **Write-through** | Write hits the cache, and it writes through to the DB before the call returns | I need to read my own write immediately and I can pay for the slower write |
| **Write-behind** (write-back) | Write hits the cache and returns instantly, the DB catches up later on its own time | Write-heavy, and I can stomach a window where a crash loses the last few writes |
| **Write-around** | Writes go straight to the DB and skip the cache completely | Freshly written data rarely gets read back soon, so I don't want it clogging the cache |

Cache-aside naturally pairs with write-around, you write the database, invalidate, and let the *next reader* decide whether the data is hot enough to earn a spot in the cache. You don't pay to cache a write nobody reads back.

The rules I actually use, stripped down:

- **Read-heavy, staleness of a few seconds is fine** (profiles, product catalog, config, feed metadata) → cache-aside, no debate.
- **Expensive to compute, read a lot** (an aggregation, a permission set, a fanned-out timeline) → cache-aside, but cache the *computed* thing, not the raw rows you rebuild it from.
- **Must read your own write this instant** (account balance right after a transfer) → this is where cache-aside fights you. Use write-through or just don't cache that read.
- **Write-heavy, rarely re-read** → don't cache it at all. Every write invalidates, every read repopulates something that gets invalidated before the next read even shows up. You pay the full cost of a cache for a hit rate near zero.
- **Read-once data** (a one-off report, a paginated scan) → skip it. You'll fill the cache with entries that never get a second hit and evict the things that would have.

If you take one line from this section, it's this, the question is never "should I add a cache." It's "how stale can *this specific read* be before someone's angry, and is it read enough to be worth the trouble." Caching is not free and it is not automatic correctness, I've made both of those assumptions and paid for both.

## Delete on write. Not update.

When a write happens, delete the cache key. Do not compute the fresh value and `SET` it, no matter how clean it looks. I know the `SET` is tempting, you already have the new data right there in your hand, why throw it away. Two reasons.

One, updating on write hands you a concurrency race I'll show you in the next section. A delete that loses a race just causes a cache miss, and a miss is *correct behavior*. A stale `SET` is a bug that sits there for the whole TTL.

Two, and this is the one that actually got me, updating on write means the cached value now gets built by two different code paths. The read path joins three tables to build the object. The write path only has the one row it just touched. They drift. Give it a few months and you're debugging why a cached user looks different depending on whether the last thing that happened to it was a read or a write, and that is a genuinely miserable afternoon. Delete on write, let the next read rebuild it through the one population path, keep your sanity.

## The bug you'll ship at least once

This is the one. If you write cache-aside, you will ship this bug, I've shipped it more than once and I know what I'm doing now. It needs two requests hitting the same key at the same time:

```
Reader (T1)                        Writer (T2)
-----------                        -----------
GET user:42  → miss
SELECT ... → V1 (the old value)
                                   UPDATE users ... → V2 (new value)
                                   DEL user:42        (cache is empty, no-op)
SET user:42 = V1  (5 min TTL)
```

Read that top to bottom. The reader missed, went to the database, and got the old value. Before it could write that back, the writer updated the database and deleted the key, except the key was already empty so the delete did nothing. Then the reader, holding a value that's now stale, cheerfully writes V1 into the cache. With the `ex=300` from the code above, that value can sit there until the five-minute TTL cleans it up.

I used a one-second TTL in the harness because I have only so much life left to spend watching a deliberately broken cache. At a 5 ms injected DB-to-`SET` delay, the naive cache disagreed with Postgres for 96.98% of the measured three-second run. At 20 ms it was 91.41%. The exact percentage depends on how expiry, reads, and writes interleave; the important part is how long the late stale value survives after it wins.

Nothing throws. Nothing logs. The write "succeeded." You find out when a customer swears up and down they changed a setting and it didn't take, and you can't reproduce it because the TTL already expired and the cache healed itself. That bug is a ghost.

<figure class="cache-bench">
  <h3>Stale wall-clock time as the reader's DB-to-SET gap grows</h3>
  <div class="cb-group">
    <p class="cb-group-label">0 ms injected delay</p>
    <div class="cb-bar-row"><span>Naive, 1s TTL</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0.00%</span></div>
    <div class="cb-bar-row"><span>Short, 60ms TTL</span><span class="cb-track"><span class="cb-fill" style="--value:0.177%;--bar:var(--cb-blue)"></span></span><span class="cb-value">0.18%</span></div>
    <div class="cb-bar-row"><span>Double-delete</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.00%</span></div>
    <div class="cb-bar-row"><span>Version/CAS</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-purple)"></span></span><span class="cb-value">0.00%</span></div>
  </div>
  <div class="cb-group">
    <p class="cb-group-label">5 ms injected delay</p>
    <div class="cb-bar-row"><span>Naive, 1s TTL</span><span class="cb-track"><span class="cb-fill" style="--value:96.98%;--bar:var(--cb-orange)"></span></span><span class="cb-value">96.98%</span></div>
    <div class="cb-bar-row"><span>Short, 60ms TTL</span><span class="cb-track"><span class="cb-fill" style="--value:24.64%;--bar:var(--cb-blue)"></span></span><span class="cb-value">24.64%</span></div>
    <div class="cb-bar-row"><span>Double-delete</span><span class="cb-track"><span class="cb-fill" style="--value:3.634%;--bar:var(--cb-green)"></span></span><span class="cb-value">3.63%</span></div>
    <div class="cb-bar-row"><span>Version/CAS</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-purple)"></span></span><span class="cb-value">0.00%</span></div>
  </div>
  <div class="cb-group">
    <p class="cb-group-label">20 ms injected delay</p>
    <div class="cb-bar-row"><span>Naive, 1s TTL</span><span class="cb-track"><span class="cb-fill" style="--value:91.405%;--bar:var(--cb-orange)"></span></span><span class="cb-value">91.41%</span></div>
    <div class="cb-bar-row"><span>Short, 60ms TTL</span><span class="cb-track"><span class="cb-fill" style="--value:25.297%;--bar:var(--cb-blue)"></span></span><span class="cb-value">25.30%</span></div>
    <div class="cb-bar-row"><span>Double-delete</span><span class="cb-track"><span class="cb-fill" style="--value:7.983%;--bar:var(--cb-green)"></span></span><span class="cb-value">7.98%</span></div>
    <div class="cb-bar-row"><span>Version/CAS</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-purple)"></span></span><span class="cb-value">0.00%</span></div>
  </div>
  <figcaption>Each expiry was lined up 2 ms before a write so the overlap would repeat. Sixteen readers, one writer, 5 ms sampling. Cache misses count as non-stale time; samples that caught Postgres changing between the before/after reads were discarded.</figcaption>
</figure>

The window is tiny, it's just the gap between the reader's DB read and its cache write. But "tiny window on a hot key under real concurrency" means it gets hit constantly. Here's how I deal with it, cheapest first:

- **Just bound it with a TTL.** A modest TTL means the staleness heals itself. In my run the 60 ms TTL cut stale wall-clock time to 24.64% at the 5 ms delay, down from 96.98% with the one-second TTL. This is why cache-aside always wants a TTL even when you're invalidating explicitly, it's there to quietly clean up after this race and after every invalidation you forgot to write. Pick the bound your data can actually tolerate and you may be able to stop here.
- **Delayed double-delete.** The writer updates the DB, deletes the key, then a beat later deletes it *again* to kill off any late stale write that snuck in:

  ```python
  def update_user(user_id, changes):
      db.execute("UPDATE users SET ... WHERE id = %s", user_id)
      r.delete(f"user:{user_id}")
      schedule_after(0.5, lambda: r.delete(f"user:{user_id}"))  # kill the late stale set
  ```

  It's ugly and it only shrinks the window instead of closing it, but it's cheap, needs zero coordination, and it's all over high-write systems for exactly that reason.
- **Version the set.** The reader grabs a version token before it reads the DB, the writer bumps the version, and the reader's `SET` only lands if the version hasn't moved (Redis `WATCH`/`MULTI`, or a Lua script). This actually closes the window. It also makes every single read carry a version check to fix a race that hits a handful of keys, so I reserve it for the specific keys where stale is genuinely not allowed, and let the plain TTL cover everything else.

One more thing on ordering that took me too long to get right, **invalidate after the DB commit, never before.** Delete the cache before you commit and a reader can slip in, miss, read the *still-old* uncommitted value, and repopulate it, then your commit lands and the cache is stale again. Same family of bug, different door. And never, ever populate the cache from inside an open transaction, if that transaction rolls back the cache is now holding a value that never existed in the database, and good luck ever explaining that one in a postmortem.

## The stampede

A hot key expires. The homepage config, a product that just went viral, the current leaderboard. Requests all check the cache, a chunk of them see the same miss, and they fire the same expensive query at the database together. The database that was happily serving them from cache a moment ago now eats identical concurrent queries, latency spikes, the connection pool saturates, and if the query's heavy enough the database tips over, and *then* every other key's traffic piles on top. One key's expiry took down the whole thing. This has a few names, thundering herd, dogpile, cache stampede, they're all the same fire.

I warmed one key for 200 ms, waited for it to expire, then released 500 readers through a barrier. The loader had a deliberate 10 ms database delay. The naive version reached Postgres 66 times before the first refill became visible. Single-flight cut that to one. Its p99 was actually worse, 176.69 ms against 127.65 ms, because 499 threads were polling and waiting on one winner. That is a trade I'll make when the alternative is letting a burst multiply the expensive work 66 times.

<figure class="cache-bench">
  <h3>One hot-key expiry, 500 readers</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">Loader hits on Postgres</p>
      <div class="cb-bar-row"><span>Naive</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">66</span></div>
      <div class="cb-bar-row"><span>Single-flight</span><span class="cb-track"><span class="cb-fill" style="--value:1.515%;--bar:var(--cb-green)"></span></span><span class="cb-value">1</span></div>
    </div>
    <div>
      <p class="cb-panel-title">Burst p99 latency</p>
      <div class="cb-bar-row"><span>Naive</span><span class="cb-track"><span class="cb-fill" style="--value:72.25%;--bar:var(--cb-orange)"></span></span><span class="cb-value">127.65 ms</span></div>
      <div class="cb-bar-row"><span>Single-flight</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">176.69 ms</span></div>
    </div>
  </div>
  <figcaption>The PostgreSQL pool was capped at 32 connections. Loader hits are counted in the application immediately before the query; the single-flight waiters poll Redis at 1 ms intervals.</figcaption>
</figure>

Three ways out, pick by how much staleness you can live with.

**Single-flight lock** — exactly one request gets to recompute, everyone else waits:

```python
def get_with_lock(key, loader, ttl=300, lock_ttl=10):
    val = r.get(key)
    if val is not None:
        return deserialize(val)

    lock_key = f"lock:{key}"
    if r.set(lock_key, "1", nx=True, ex=lock_ttl):     # I won the right to recompute
        try:
            val = loader()                             # only this one request hits the DB
            r.set(key, serialize(val), ex=jitter(ttl))
            return val
        finally:
            r.delete(lock_key)
    else:
        time.sleep(0.05)                               # someone else is loading, back off
        val = r.get(key)
        return deserialize(val) if val is not None else loader()  # last-resort fallthrough
```

**Probabilistic early expiration (XFetch)** — the clever one. Instead of waiting for the TTL to hit zero, you refresh the key a little *early*, with a probability that climbs as expiry gets closer, so one lucky request rebuilds it while everyone else is still being served the perfectly good live value. Store how long the recompute took (`delta`) next to the value, and refresh early when:

```
now - delta * beta * ln(rand())  >=  expiry_time      # beta ~1, crank it up to refresh earlier
```

No lock, no herd, no coordination, the randomness spreads the single refresh out over time on its own. This is my favourite when I can afford to stash a little metadata per key.

**Stale-while-revalidate** — serve the expired value right now and kick off the refresh in the background. Nobody waits, the price is one round of knowingly-stale responses. Perfect for content, wrong for anything where a stale answer actually hurts someone.

## The keys that don't exist

Cache-aside only ever caches things that *exist*. Ask for `user:99999999` that isn't in the database, and every request misses the cache and hits the database, forever, because there's nothing to populate, a miss on a missing row is a permanent miss. Point a scraper or a broken client or someone actually malicious at a stream of nonexistent IDs and congratulations, you've built a straight pipe to the database that walks right past the cache. This one's called cache penetration and it's nastier than the stampede because it doesn't even need a hot key.

Fix is to cache the *absence* too, short TTL, with a sentinel so you can tell "not cached" apart from "cached as nothing":

```python
NULL_SENTINEL = "\x00__null__"

def get_user(user_id):
    key = f"user:{user_id}"
    cached = r.get(key)
    if cached == NULL_SENTINEL:
        return None                                    # known-absent, don't touch the DB
    if cached is not None:
        return deserialize(cached)

    user = db.query("SELECT * FROM users WHERE id = %s", user_id)
    if user is None:
        r.set(key, NULL_SENTINEL, ex=30)               # short TTL, absence changes more easily
        return None
    r.set(key, serialize(user), ex=300)
    return user
```

Two things to watch. Keep that negative TTL short, a row that doesn't exist right now is way more likely to start existing soon than an existing row is to change, and you do not want to be serving "user not found" for five minutes after someone just signed up. And the write path has to delete the sentinel when the row *is* created, otherwise "doesn't exist" outlives the thing it's talking about. If the bad keyspace is genuinely huge or adversarial, put a bloom filter in front of the whole thing, it'll tell you "definitely not here" without burning a cache entry on every junk key.

## Don't lean on the TTL

Quick thing about the TTL, because it's doing two jobs and people tend to set it for the wrong one. Keeping the cache fresh isn't really what it's there for, that's delete-on-write's job. Where the TTL earns its keep is catching everything delete-on-write misses, the stale-set race up above, a delete that failed because Redis blipped for a second, the write path that forgot to invalidate a key it should have, and you will forget one eventually. So when you pick the number, pick it off how long you can stand this data being wrong, not off how long it's technically still valid.

And jitter it. Please jitter it. If you cache five thousand keys during a traffic ramp and stamp every one with `ex=300`, they all expire in the same second five minutes later, and that synchronized mass-expiry is a stampede across your entire keyspace at once, self-inflicted. A little randomness smears them out:

```python
def jitter(ttl, pct=0.1):
    spread = int(ttl * pct)
    return ttl + rand_between(-spread, spread)         # 270–330s instead of a hard 300
```

I tried exactly that with 5,000 keys and a two-second TTL. The fixed run shoved 5,305 loader fall-throughs into one 100 ms bucket. That is more calls than keys because readers overlapped before another thread's refill became visible, which is the little stampede hiding inside the big one. With ±10% jitter, expiry spread across five buckets and the peak fell to 1,325, a 75% drop.

<figure class="cache-bench">
  <h3>DB loader fall-throughs per 100 ms after a batch fill</h3>
  <svg class="cb-svg" viewBox="0 0 700 270" role="img" aria-labelledby="ttl-chart-title ttl-chart-desc">
    <title id="ttl-chart-title">Fixed TTL compared with ten percent jitter</title>
    <desc id="ttl-chart-desc">The fixed TTL produces 5,305 loader fall-throughs in one bucket. Jitter spreads the load across five buckets with a peak of 1,325.</desc>
    <line class="grid" x1="50" y1="56" x2="680" y2="56"></line>
    <line class="grid" x1="50" y1="138" x2="680" y2="138"></line>
    <line class="grid" x1="50" y1="220" x2="680" y2="220"></line>
    <line class="grid" x1="365" y1="40" x2="365" y2="220"></line>
    <line class="grid" x1="444" y1="40" x2="444" y2="220"></line>
    <line class="grid" x1="523" y1="40" x2="523" y2="220"></line>
    <text x="42" y="60" text-anchor="end">5k</text>
    <text x="42" y="142" text-anchor="end">2.5k</text>
    <text x="42" y="224" text-anchor="end">0</text>
    <text x="365" y="242" text-anchor="middle">1.6s</text>
    <text x="444" y="242" text-anchor="middle">2.0s</text>
    <text x="523" y="242" text-anchor="middle">2.4s</text>
    <polyline class="fixed" points="50,220 404,220 424,46 444,220 680,220"></polyline>
    <polyline class="jittered" points="50,220 365,220 385,190 404,180 424,177 444,180 463,209 483,220 680,220"></polyline>
  </svg>
  <div class="cb-legend">
    <span><i class="cb-swatch" style="--swatch:var(--cb-orange)"></i>Fixed 2s TTL</span>
    <span><i class="cb-swatch" style="--swatch:var(--cb-blue)"></i>±10% jitter</span>
  </div>
  <figcaption>Five thousand keys populated together, 24 readers scanning in 100-key batches. The short TTL makes the run practical; it is the expiry-wave shape that matters here.</figcaption>
</figure>

It's three lines. I've been on the wrong side of that 5,305-call cliff, do the three lines.

## The stuff that bites on the second deploy

These don't show up on day one, they wait for the second deploy, which is somehow worse.

**Version your key namespace.** Prefix keys with a schema version, `v3:user:42`. The day you change the shape of a cached object, add a field, swap pickle for JSON, change a nested type, every old entry still sitting in Redis becomes a landmine. New code reads an old-shaped blob and either crashes or, so much worse, quietly deserializes into something wrong. Bump the prefix to `v4:` and every old entry is instantly just a miss that repopulates in the new shape. No migration, no flushing the whole cache, no poisoned reads. (And don't `pickle` across deploys, use a format that isn't welded to your class definitions.)

**Eviction is not invalidation, and the default will hurt you.** Set `maxmemory-policy` to `allkeys-lru` or `allkeys-lfu`. Cache-aside bounds memory through TTL, but a TTL is a *time* limit, not a *size* limit, and a burst of unique keys can fill Redis long before anything expires. On the default policy (`noeviction`), a full Redis doesn't evict, it starts *rejecting writes*, which means your cache-population `SET`s begin failing and every read turns into a miss right when you're already under memory pressure, i.e. the worst possible moment. Pick the eviction policy on purpose. Don't inherit the default and find out during an incident.

## The mistakes, in one place

Everything above, squeezed into the list I'd actually paste into a code review:

- **`SET` the new value on write instead of deleting it** → two code paths build the cached value and they drift, plus you reopen the stale-set race. Delete.
- **No TTL because "I invalidate explicitly"** → the one delete you miss is stale *forever*. Set a TTL anyway.
- **Invalidate before the DB commit** → a reader repopulates the old value in the gap. Invalidate after commit.
- **Populate the cache inside an open transaction** → a rollback leaves the cache holding a value that never existed.
- **Identical TTLs on a batch of keys** → synchronized mass-expiry, a stampede you built yourself. Jitter them.
- **No single-flight or early-refresh on hot keys** → one expiry became 66 duplicate loaders in my 500-reader run.
- **Not caching the misses** → a stream of nonexistent keys is a direct line past the cache to the DB.
- **Caching misses with a long TTL and no invalidate-on-create** → "not found" outlives the thing being found.
- **No key version prefix** → your next serialization change poisons every existing entry.
- **`noeviction` and an unbounded keyspace** → Redis fills, `SET`s start failing, the cache goes cold under load.

## So is it worth it

After all that, yeah, I still reach for cache-aside first and it's still the right default. It's simple, the cache isn't load-bearing so losing it doesn't take you down, and it only ever caches the stuff something actually asked for. The four lines were never really the problem, they just leave out everything I listed above. And every one of those things traces back to the same boring fact, the moment you cache something you're keeping two copies of it, and two copies of anything drift apart eventually. No pattern anywhere fixes that, caching just costs what it costs. What I've come to like about cache-aside is that it doesn't pretend otherwise, all the bookkeeping sits right there in your code where you can see it.

## The takeaway

So the actual discipline is small and kind of boring. Delete on write, keep a jittered TTL running underneath as a backstop, single-flight your hot keys, cache your misses, version your keys, set an eviction policy on purpose. Do those six things and the four lines hold up under the traffic that would otherwise find every one of these edges for you, on a Monday, the expensive way. Ask me how I know.
