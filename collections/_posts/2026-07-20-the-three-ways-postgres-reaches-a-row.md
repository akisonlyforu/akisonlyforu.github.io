---
layout:     post
title:      The Three Ways Postgres Reaches a Row
date:       2026-07-20
description:    The same point lookup ran at 134 ms with a Seq Scan and 0.22 ms with an Index Scan, 600x faster touching 8 buffers instead of 41,667. A covering index did nothing until I ran VACUUM, and past 10% selectivity the index lost to the full scan it was supposed to replace. Measured on PostgreSQL 16.14 over 5,000,000 rows.
categories: postgres database performance indexes
---

Everyone's first fix for a slow query is "add an index," and it usually works, right up until it doesn't. What that reflex hides is that Postgres has more than one way to actually reach the rows you asked for, and the index only helps in some of them. I built the three ways on a single 5,000,000-row table this week and watched the identical predicate get 600x faster, then do nothing at all, then get *slower* than the full table scan it was supposed to beat.

## The problem

When you run `SELECT ... WHERE user_id = 500001`, Postgres has to decide how to find those rows, and it has three real options. It can read the whole table and throw away everything that doesn't match (a **Seq Scan**). It can walk a B-tree index to find the matching row locations and then fetch each row from the heap (an **Index Scan**). Or, if every column you asked for lives in the index itself, it can answer from the index alone and never touch the heap (an **Index Only Scan**). Those three do wildly different amounts of work for the same result, and which one you get depends on whether the right index exists, whether the index covers your columns, whether you've vacuumed recently, and how many rows your predicate actually matches. Get the wrong one and a query that should touch eight disk pages touches forty thousand.

<figure class="cache-bench">
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
.cb-bar-row { display: grid; grid-template-columns: minmax(7rem, 1.3fr) minmax(6rem, 4fr) minmax(4.2rem, 0.9fr); gap: 0.55rem; align-items: center; margin: 0.42rem 0; font-size: 0.78rem; }
.cb-track { height: 0.72rem; overflow: hidden; border-radius: 999px; background: var(--cb-grid); }
.cb-fill { display: block; width: var(--value); min-width: 2px; height: 100%; border-radius: inherit; background: var(--bar, var(--cb-blue)); }
.cb-value { color: var(--cb-muted); text-align: right; font-variant-numeric: tabular-nums; }
.cb-svg { display: block; width: 100%; height: auto; overflow: visible; }
.cb-svg text { fill: var(--cb-muted); font: 12px system-ui, sans-serif; }
.cb-svg .grid { stroke: var(--cb-grid); stroke-width: 1; }
.cb-svg .idx { fill: none; stroke: var(--cb-blue); stroke-width: 3; stroke-linejoin: round; }
.cb-svg .seq { fill: none; stroke: var(--cb-orange); stroke-width: 3; stroke-linejoin: round; }
.cb-svg .plan { fill: none; stroke: var(--cb-green); stroke-width: 3; stroke-linejoin: round; stroke-dasharray: 6 4; }
.cb-legend { display: flex; flex-wrap: wrap; gap: 1rem; margin-top: 0.5rem; color: var(--cb-muted); font-size: 0.78rem; }
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
}
</style>
  <h3>Point lookup: one user_id, 5 rows out of 5,000,000</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">Median latency (ms)</p>
      <div class="cb-bar-row"><span>Seq Scan</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">134.0</span></div>
      <div class="cb-bar-row"><span>Index Scan</span><span class="cb-track"><span class="cb-fill" style="--value:0.16%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.22</span></div>
    </div>
    <div>
      <p class="cb-panel-title">Buffers touched</p>
      <div class="cb-bar-row"><span>Seq Scan</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">41,667</span></div>
      <div class="cb-bar-row"><span>Index Scan</span><span class="cb-track"><span class="cb-fill" style="--value:0.02%;--bar:var(--cb-green)"></span></span><span class="cb-value">8</span></div>
    </div>
  </div>
  <figcaption>Median over 100 reps, cache warm. The Seq Scan reads all 41,667 pages of the table to return 5 rows; the Index Scan touches 8. Measured on PostgreSQL 16.14, results in benchmarks/postgres-scan-methods/results/.</figcaption>
</figure>

## What I built

One table, `events`, five million rows, seeded deterministically so anyone can reproduce it (no `random()`, everything derived from `id` arithmetic). About a million distinct `user_id` values, roughly five rows each. A `status` column that's `id % 5`, and a `bucket` column that's `id % 1000`, which I'll use later to dial selectivity. On disk the whole thing is 616 MB, of which the heap is 326 MB. Every query was warmed once and then timed steady-state, parallelism and JIT turned off, so the numbers isolate the scan method and not the core count or the JIT warming up.

```sql
CREATE TABLE events (
  id          bigint PRIMARY KEY,
  user_id     bigint NOT NULL,
  status      smallint NOT NULL,
  amount      integer NOT NULL,
  created_at  timestamptz NOT NULL,
  bucket      int NOT NULL
);
```

## Seq Scan vs Index Scan

Start with no index on `user_id` and ask for one user. Five rows come back, and Postgres reads the entire table to find them:

```
Seq Scan on events  (cost=0.00..104167.50 rows=6 width=34) (actual time=14.391..131.850 rows=5 loops=1)
  Filter: (user_id = 500001)
  Rows Removed by Filter: 4999995
  Buffers: shared hit=12401 read=29266
  I/O Timings: shared read=16.661
Execution Time: 131.865 ms
```

`Rows Removed by Filter: 4999995` is the whole story. It looked at every row, kept five, threw away the other 4,999,995, and touched 41,667 buffers doing it. Median over 100 runs was 134.0 ms. Now add the index and ask the exact same question:

```
Index Scan using idx_events_user_id on events  (cost=0.43..24.52 rows=5 width=34) (actual time=0.010..0.013 rows=5 loops=1)
  Index Cond: (user_id = 500001)
  Buffers: shared hit=3 read=5
  I/O Timings: shared read=0.008
Execution Time: 0.021 ms
```

Eight buffers, 0.22 ms median, no rows removed by filter because the index went straight to the five that matched. That's 600x on the median latency for changing nothing but whether the index exists. This is the case the "just add an index" reflex was built for, and when your predicate is this selective the reflex is exactly right.

## The covering index that did nothing until I vacuumed

Here's where it stops being obvious. The Index Scan above was fast, but it still did two steps: walk the index to find *where* the rows are, then go to the heap to actually *read* them. If the only column you need is already in the index, that second step is pure waste, and Postgres can skip it with an Index Only Scan. So I asked for just the `id` over a range of a thousand users, 5,005 rows, first with the plain `(user_id)` index:

```
Index Scan using idx_events_user_id on events  (actual time=0.006..1.889 rows=5005 loops=1)
  Index Cond: ((user_id >= 500000) AND (user_id <= 501000))
  Buffers: shared hit=5015
```

Then I built a covering index, `(user_id) INCLUDE (id)`, so the index carries the `id` too. Same query. I expected the heap fetches to vanish. They didn't:

```
Index Only Scan using idx_events_user_id_covering on events  (actual time=0.013..2.285 rows=5005 loops=1)
  Index Cond: ((user_id >= 500000) AND (user_id <= 501000))
  Heap Fetches: 5005
  Buffers: shared hit=5005 read=22
```

It says **Index Only Scan** right there in the plan, and it still went to the heap 5,005 times, once per row, touching *more* buffers than the plain index it was supposed to improve on. The reason is the visibility map. Postgres can only trust the index copy of a row if it knows that row is visible to every transaction, and it tracks that per page in the visibility map, which only gets populated by `VACUUM`. On a freshly loaded table the map is empty, so an "index only" scan has to check the heap for every single row to confirm visibility. It isn't really index-only until the visibility map says those pages are safe to trust, and only `VACUUM` sets that. One command later:

```
Index Only Scan using idx_events_user_id_covering on events  (actual time=0.007..0.345 rows=5005 loops=1)
  Index Cond: ((user_id >= 500000) AND (user_id <= 501000))
  Heap Fetches: 0
  Buffers: shared hit=2024
```

`Heap Fetches: 0`. Buffers touched dropped from 5,015 to 2,024, and the only thing that changed between the two Index Only Scans was a `VACUUM events`.

<figure class="cache-bench">
  <h3>Covering query: SELECT id WHERE user_id BETWEEN a AND b, 5,005 rows</h3>
  <div class="cb-bar-row"><span>Index Scan (plain)</span><span class="cb-track"><span class="cb-fill" style="--value:99.8%;--bar:var(--cb-orange)"></span></span><span class="cb-value">5,015</span></div>
  <div class="cb-bar-row"><span>Index Only, before VACUUM</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-purple)"></span></span><span class="cb-value">5,027</span></div>
  <div class="cb-bar-row"><span>Index Only, after VACUUM</span><span class="cb-track"><span class="cb-fill" style="--value:40.3%;--bar:var(--cb-green)"></span></span><span class="cb-value">2,024</span></div>
  <figcaption>Buffers touched. The plain Index Scan hits the heap once per row. The covering index calls itself an Index Only Scan but does 5,005 heap fetches until VACUUM sets the visibility map, after which heap fetches go to 0 and buffers halve. The before-VACUUM plan is kept under results/attempts/. Measured on PostgreSQL 16.14, results in benchmarks/postgres-scan-methods/results/.</figcaption>
</figure>

## When the index loses to a full scan

The point lookup made the index look like a free 600x. It isn't, and the reason is that an Index Scan pays a random heap access for every row it returns, while a Seq Scan reads the table sequentially and only pays once for the whole thing. When you're fetching five rows, random access is nothing. When you're fetching a million, all those random fetches add up to more work than just reading the table start to finish. So I indexed `bucket` and swept a range predicate, `SELECT sum(amount) FROM events WHERE bucket < n`, from 0.1% of the table up to 90%, and at each point I forced a pure Index Scan and forced a Seq Scan and timed both.

<figure class="cache-bench">
  <h3>Index Scan vs Seq Scan as the predicate widens (log-log)</h3>
  <svg class="cb-svg" viewBox="0 0 640 250" role="img" aria-labelledby="xo-title xo-desc">
    <title id="xo-title">A forced Index Scan starts far faster than a Seq Scan but crosses over around 15% selectivity and keeps climbing</title>
    <desc id="xo-desc">At 0.1% selectivity the forced Index Scan runs at about 1ms versus the Seq Scan's roughly 150ms. The Index Scan climbs steeply with selectivity, crossing the nearly flat Seq Scan line between 10% and 20%, and reaching 1529ms at 90% where the Seq Scan is 265ms. The planner's own choice, a Bitmap Heap Scan, stays below both across the low and mid range.</desc>
    <line class="grid" x1="80" y1="210" x2="600" y2="210" />
    <line class="grid" x1="80" y1="155.5" x2="600" y2="155.5" />
    <line class="grid" x1="80" y1="100.9" x2="600" y2="100.9" />
    <line class="grid" x1="80" y1="46.4" x2="600" y2="46.4" />
    <text x="44" y="214">1ms</text>
    <text x="38" y="159.5">10ms</text>
    <text x="32" y="104.9">100ms</text>
    <text x="26" y="50.4">1000ms</text>
    <polyline class="seq" points="80,91.4 133,91.3 203,91.0 256,88.1 379.1,90.6 432,89.8 485,88.8 555.1,85.5 600,77.8" />
    <polyline class="plan" points="80,194.7 133,187.5 203,178.1 256,169.4 379.1,141.7 432,128.1 485,113.5 555.1,87.4 600,81.5" />
    <polyline class="idx" points="80,208.0 133,192.0 203,170.6 256,151.6 379.1,116.1 432,98.8 485,82.1 555.1,58.2 600,36.4" />
    <text x="66" y="230">0.1%</text>
    <text x="244" y="230">1%</text>
    <text x="420" y="230">10%</text>
    <text x="584" y="230">90%</text>
  </svg>
  <div class="cb-legend">
    <span><span class="cb-swatch" style="--swatch:var(--cb-blue)"></span>forced Index Scan</span>
    <span><span class="cb-swatch" style="--swatch:var(--cb-orange)"></span>forced Seq Scan</span>
    <span><span class="cb-swatch" style="--swatch:var(--cb-green)"></span>planner's own pick</span>
  </div>
  <figcaption>Median ms over 20 reps at each selectivity. The Index Scan wins up to 10% (109ms vs 159ms) and loses by 20% (221ms vs 166ms); the Seq Scan is nearly flat because it reads the whole table regardless. Measured on PostgreSQL 16.14, results in benchmarks/postgres-scan-methods/results/.</figcaption>
</figure>

The Seq Scan line is almost flat, hovering around 150ms whether the predicate matches 5,000 rows or 4.5 million, because it does the same full-table read either way. The Index Scan starts a hundred times faster and climbs with a slope that never lets up: 109ms at 10% selectivity, 221ms at 20%, and a brutal 1,529ms at 90%, more than five times slower than just scanning the whole table. Somewhere around 15% the two lines cross, and past that the index is a liability.

The interesting part is that the planner never actually walks into that trap. Left to its own choice it never picked the pure Index Scan for these ranges at all. Below 90% it reached for a **Bitmap Heap Scan**, which builds a bitmap of matching heap pages from the index first and then reads those pages in physical order, turning the index's random heap access back into something sequential:

```
Bitmap Heap Scan on events  (actual time=7.576..35.663 rows=500000 loops=1)
  Recheck Cond: (bucket < 100)
  Heap Blocks: exact=10000
  ->  Bitmap Index Scan on idx_events_bucket  (actual time=6.860..6.860 rows=500000 loops=1)
        Index Cond: (bucket < 100)
```

That green dashed line stays below both the pure index and the seq scan across the whole low and mid range, and only at 90% does the planner give up on the index entirely and switch to a plain Seq Scan. The planner isn't blundering into that crossover, it's choosing sides every time from the row estimates `ANALYZE` gave it. Which is also why stale statistics hurt so much: if the planner thinks a predicate matches 5,000 rows when it really matches 5 million, it'll pick the index scan that the chart above says costs you 1.5 seconds.

## The takeaway

There are three ways Postgres reaches a row, and the index is only the fastest one in a specific window. A Seq Scan costs the same no matter how selective your predicate is; an Index Scan is nearly free when you're fetching a handful of rows and turns punishing as that fraction grows; an Index Only Scan is the cheapest of all but only once `VACUUM` has set the visibility map, and a covering index is doing nothing for you until then. Read your `EXPLAIN (ANALYZE, BUFFERS)` output for the parts that don't lie about work done: `Rows Removed by Filter`, `Heap Fetches`, and `Buffers` touched. Those three lines tell you which of the three scans you actually got, and whether the index you added is helping or quietly costing you a full table scan and then some. Keep `ANALYZE` current so the planner's estimate matches reality, and don't assume that because the plan says "Index Only Scan," it's actually staying out of the heap.

The full harness, the deterministic seed, and every EXPLAIN plan including the ones I kept as [attempts](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/postgres-scan-methods) are on GitHub. These are laptop numbers meant to show the mechanics of each scan method, not capacity-planning figures for your production box.
