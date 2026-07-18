---
layout:     post
title:      The Lie in pg_stats
date:       2026-07-18
description:    Postgres had the right index and still walked 18 million rows for a LIMIT 1 query. I built the failure locally, followed the planner's estimate back into pg_stats, and fixed it with a higher per-column statistics target.
categories: postgres query-planner databases performance
---

If you've ever stared at a Postgres plan and wondered why it walked straight past the index you built for that exact query, this is for you. I wanted to reproduce one of the nastier versions of that failure, the `ORDER BY ... LIMIT 1` trap, small enough that anyone could run it locally and argue with the same planner I was arguing with.

## The problem

Postgres had an index built for exactly this lookup and refused to use it, scanning about eighteen million rows in primary-key order to answer a query that returns one. The reason was a single number in the planner's statistics that was wrong by 130x, and under a `LIMIT` that one wrong number is enough to turn a sub-millisecond query into a four-second one. This is me reproducing that failure from scratch and following the bad estimate back to where it lives.

My first 5 million rows behaved perfectly. Slightly rude when you're trying to write a post about a broken plan, but good news for the benchmark. I pushed it to 20 million rows, still fine. The trap finally fired when I kept the column 82% NULL and tightened each session into bursts of 180 events. No planner cost knobs, no disabled scan types, no hand-written plan. Postgres 16 looked at a real index, chose the primary key anyway, and removed 17,999,000 rows by filter before finding the one I asked for.

The [Docker harness, deterministic seed, raw CSVs, and untouched EXPLAIN output are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/pg-stats). These are laptop numbers from one Docker container, the mechanism transfers, the absolute timings do not. I left the two failed shapes in the README too, because quietly pretending the 5 million row version worked would make for a cleaner story and a useless benchmark.

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
  <h3>What ANALYZE thought n_distinct was</h3>
  <svg class="cb-svg" viewBox="0 0 640 250" role="img" aria-labelledby="pgstats-estimate-title pgstats-estimate-desc">
    <title id="pgstats-estimate-title">n_distinct estimate by statistics target</title>
    <desc id="pgstats-estimate-desc">The estimate rises from 6,544 at target 100 to 20,003 at target 2000 and 20,000 in both target-5000 samples. The real distinct count is 20,000.</desc>
    <line class="grid" x1="80" y1="210" x2="570" y2="210" />
    <line class="grid" x1="80" y1="120" x2="570" y2="120" />
    <line class="grid" x1="80" y1="30" x2="570" y2="30" />
    <text x="18" y="214">0</text>
    <text x="18" y="124">10k</text>
    <text x="18" y="34">20k</text>
    <line class="fixed" x1="80" y1="30" x2="570" y2="30" />
    <polyline class="jittered" points="110,151 325,30 540,30" />
    <circle cx="110" cy="151" r="5" style="fill:var(--cb-blue)" />
    <circle cx="325" cy="30" r="5" style="fill:var(--cb-blue)" />
    <circle cx="540" cy="30" r="5" style="fill:var(--cb-blue)" />
    <text x="91" y="238">100</text>
    <text x="303" y="238">2000</text>
    <text x="518" y="238">5000</text>
    <text x="84" y="141">6,544</text>
    <text x="291" y="20">20,003</text>
    <text x="506" y="50">20,000</text>
  </svg>
  <div class="cb-legend">
    <span><span class="cb-swatch" style="--swatch:var(--cb-blue)"></span>pg_stats estimate</span>
    <span><span class="cb-swatch" style="--swatch:var(--cb-orange)"></span>real count: 20,000</span>
  </div>
  <figcaption>One ANALYZE at targets 100 and 2000, then two independently captured samples at 5000. The default estimate missed the real count by 3.056x; target 2000 was already close enough to fix this plan.</figcaption>
</figure>

## The table I used

The shape is an admin audit trail. Human activity carries a session ID, automated activity from CDC and scheduled jobs does not, so most of the column is NULL. A support session arrives as a tight burst and the table is append-only, which leaves equal values sitting beside each other on the same few pages.

```sql
CREATE TABLE audit_events (
    id           BIGSERIAL PRIMARY KEY,
    entity_type  TEXT NOT NULL,
    entity_id    BIGINT NOT NULL,
    session_id   UUID,
    event_type   TEXT NOT NULL,
    payload      JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE admin_sessions (
    id             UUID PRIMARY KEY,
    admin_email    TEXT NOT NULL,
    ip_address     INET NOT NULL
);

CREATE INDEX idx_audit_events_session_id ON audit_events (session_id);
```

The final seed has 20,000,000 events, 20,000 real non-null session IDs, and exactly 180 contiguous events per session. The target session is number 18,000 in insertion order, late enough that a primary-key walk has a long afternoon ahead of it.

The query is the kind of thing an admin console does all the time, jump to the first event in one session:

```sql
SELECT ae.*, s.admin_email, s.ip_address
FROM audit_events ae
JOIN admin_sessions s ON s.id = ae.session_id
WHERE ae.session_id = 'c92b26f6-8689-c7af-56b0-b08721897732'
ORDER BY ae.id ASC
LIMIT 1;
```

## Following the bad plan

This is the target-100 plan exactly as Postgres printed it. I did not trim the ugly bit, the ugly bit is why we're here.

```
Limit  (cost=0.72..1386.49 rows=1 width=92) (actual time=1409.718..1409.719 rows=1 loops=1)
  Buffers: shared read=223880
  I/O Timings: shared read=228.696
  ->  Nested Loop  (cost=0.72..763557.35 rows=551 width=92) (actual time=1409.717..1409.718 rows=1 loops=1)
        Buffers: shared read=223880
        I/O Timings: shared read=228.696
        ->  Index Scan using audit_events_pkey on audit_events ae  (cost=0.44..763542.15 rows=551 width=61) (actual time=1409.693..1409.693 rows=1 loops=1)
              Filter: (session_id = 'c92b26f6-8689-c7af-56b0-b08721897732'::uuid)
              Rows Removed by Filter: 17999000
              Buffers: shared read=223877
              I/O Timings: shared read=228.689
        ->  Materialize  (cost=0.29..8.31 rows=1 width=47) (actual time=0.021..0.021 rows=1 loops=1)
              Buffers: shared read=3
              I/O Timings: shared read=0.008
              ->  Index Scan using admin_sessions_pkey on admin_sessions s  (cost=0.29..8.30 rows=1 width=47) (actual time=0.017..0.017 rows=1 loops=1)
                    Index Cond: (id = 'c92b26f6-8689-c7af-56b0-b08721897732'::uuid)
                    Buffers: shared read=3
                    I/O Timings: shared read=0.008
Planning Time: 0.062 ms
Execution Time: 1409.730 ms
```

There it is, `Rows Removed by Filter: 17999000`. Postgres walked the primary key in `id` order because that gives it the ordering for free, checked `session_id` on every row, and found the first match after 17,999,000 misses. The session index was sitting right there.

The reason is the `LIMIT 1`. Postgres discounts an ordered scan when it believes plenty of rows will match, roughly by `rows_needed / rows_available`. If 551 rows should match and I only need one, surely the primary-key walk will bump into one early. Except this session has 180 rows and its first event lives near the end of the append-only table. The planner cannot know that last part from ordinary column stats, and the bad distinct estimate made its bet three times worse.

<figure class="cache-bench">
  <h3>The LIMIT trap in one picture</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">Statistics target 100</p>
      <div class="cb-bar-row"><span>Estimated</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">551</span></div>
      <div class="cb-bar-row"><span>Actual</span><span class="cb-track"><span class="cb-fill" style="--value:32.67%;--bar:var(--cb-blue)"></span></span><span class="cb-value">180</span></div>
    </div>
    <div>
      <p class="cb-panel-title">Statistics target 5000</p>
      <div class="cb-bar-row"><span>Estimated</span><span class="cb-track"><span class="cb-fill" style="--value:32.49%;--bar:var(--cb-green)"></span></span><span class="cb-value">179</span></div>
      <div class="cb-bar-row"><span>Actual</span><span class="cb-track"><span class="cb-fill" style="--value:32.67%;--bar:var(--cb-blue)"></span></span><span class="cb-value">180</span></div>
    </div>
  </div>
  <figcaption>The equality estimate fell from 551 rows to 179 against 180 real matches. That was enough to make the LIMIT discount stop flattering the primary-key scan.</figcaption>
</figure>

## The lie in pg_stats

I followed the 551 back to the column statistics:

```sql
SELECT null_frac, n_distinct
FROM pg_stats
WHERE tablename = 'audit_events'
  AND attname = 'session_id';
```

At target 100, my run had `null_frac = 0.819667` and `n_distinct = 6544`. The table had 20,000 real distinct non-null values, so `pg_stats` was short by 3.056x.

Postgres estimates equality rows roughly like this when the value is not in the most-common-values list:

```
rows ≈ reltuples × (1 − null_frac) / n_distinct
     ≈ 20,003,698 × (1 − 0.819667) / 6,544
     ≈ 551
```

That lands exactly on the plan's estimate. Once I saw that, the mystery index choice stopped being mysterious, the planner was doing reasonable arithmetic with a bad input.

At the default statistics target of 100, `ANALYZE` aims for a sample of about 30,000 rows and estimates distinct values with the Haas–Stokes estimator. The physical layout is what makes this awkward. Each sampled block can show the estimator the same session ID over and over because all 180 events for that session were inserted together. It sees far less variety than the table really has, then underestimates `n_distinct`.

## The fix

I raised the statistics target on this column and analyzed again:

```sql
ALTER TABLE audit_events
    ALTER COLUMN session_id SET STATISTICS 2000;
ANALYZE audit_events;
```

That run estimated `n_distinct = 20003` against the real 20,000. The plan flipped immediately, estimated 180 matching rows, read the 180 real rows through `idx_audit_events_session_id`, sorted that tiny set by `id`, and returned the first one.

I also ran target 5000 with two independent `ANALYZE` passes, because sampled stats move and I wanted to see whether the estimate held:

```sql
ALTER TABLE audit_events
    ALTER COLUMN session_id SET STATISTICS 5000;
ANALYZE audit_events;
ANALYZE audit_events;
```

Both samples landed on exactly 20,000. Target 5000 was unnecessary for this local table, 2000 had already fixed the plan, but keeping both samples made the convergence visible instead of treating one lucky sample as a law of nature.

Here is the final target-5000 plan, again copied straight from the captured file:

```
Limit  (cost=718.75..718.75 rows=1 width=92) (actual time=0.042..0.042 rows=1 loops=1)
  Buffers: shared hit=9
  ->  Sort  (cost=718.75..719.19 rows=179 width=92) (actual time=0.041..0.042 rows=1 loops=1)
        Sort Key: ae.id
        Sort Method: top-N heapsort  Memory: 25kB
        Buffers: shared hit=9
        ->  Nested Loop  (cost=6.11..717.85 rows=179 width=92) (actual time=0.007..0.026 rows=180 loops=1)
              Buffers: shared hit=9
              ->  Index Scan using admin_sessions_pkey on admin_sessions s  (cost=0.29..8.30 rows=1 width=47) (actual time=0.002..0.002 rows=1 loops=1)
                    Index Cond: (id = 'c92b26f6-8689-c7af-56b0-b08721897732'::uuid)
                    Buffers: shared hit=3
              ->  Bitmap Heap Scan on audit_events ae  (cost=5.82..707.76 rows=179 width=61) (actual time=0.005..0.012 rows=180 loops=1)
                    Recheck Cond: (session_id = 'c92b26f6-8689-c7af-56b0-b08721897732'::uuid)
                    Heap Blocks: exact=3
                    Buffers: shared hit=6
                    ->  Bitmap Index Scan on idx_audit_events_session_id  (cost=0.00..5.78 rows=179 width=0) (actual time=0.003..0.003 rows=180 loops=1)
                          Index Cond: (session_id = 'c92b26f6-8689-c7af-56b0-b08721897732'::uuid)
                          Buffers: shared hit=3
Planning Time: 0.042 ms
Execution Time: 0.050 ms
```

<figure class="cache-bench">
  <h3>Same query, corrected column statistics</h3>
  <div class="cb-bar-row"><span>Target 100</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1,409.730 ms</span></div>
  <div class="cb-bar-row"><span>Target 5000</span><span class="cb-track"><span class="cb-fill" style="--value:0.00355%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.050 ms</span></div>
  <figcaption>Each captured plan got the same one-query warm-up. The local run dropped from 1,409.730 ms and 223,880 shared-buffer reads to 0.050 ms and 9 shared-buffer hits. That is about 28,195x on this laptop; compare the plans and buffer work, don't treat it as a capacity claim.</figcaption>
</figure>

## Stuff worth remembering

- A higher statistics target costs real work. `ANALYZE` samples more rows, takes longer, and stores a larger statistics object. If the `ALTER TABLE` sits inside a larger transaction, the lock lives as long as that transaction does too.
- Treat a major-version upgrade as a statistics reset even if your migration tooling carries the column setting across. Verify the per-column target and run `ANALYZE` before traffic comes back, otherwise you can reintroduce the same plan after a maintenance window and spend a morning wondering why history has a sense of humour.
- Run `ANALYZE` more than once while diagnosing this. It resamples, and one clean estimate can be luck. I care about whether the plan is stable across samples, not whether one run made my chart prettier.
- This failure shape is not exclusive to Postgres. Any cost-based optimizer working from sampled cardinality estimates can get talked into a bad plan when equal values are physically clustered. Append-only data grouped by a foreign key is where I now check the estimate before blaming the index.

## The takeaway

The bit I keep coming back to is `Rows Removed by Filter`. When that number is enormous under a `LIMIT`, the index may be fine, the query may be fine, and the planner may simply believe it will get lucky much earlier than the data allows. Check the estimated row count against `pg_stats`, then fix the estimate the planner is actually using.
