---
layout: post
title: The Rows That Never Left
date: 2026-07-20
description: Soft-deleting a row (UPDATE deleted_at instead of DELETE) doesn't make the row go away, it makes it permanent. A Postgres benchmark across 50 churn cycles shows the soft-delete table's heap at 26.7x the size of the hard-delete equivalent for the same 5,000 active rows, the query cost that comes with it, and the partial index that gets most of it back.
categories: [postgres, performance, databases]
---

Every team that's been burned by a bad `DELETE` eventually lands on the same fix: stop deleting. Add a `deleted_at` column, filter it out in every query, keep the row forever. It's an easy sell, you get an audit trail for free, undelete becomes a one-line `UPDATE`, and nobody has to explain to a customer why their data is actually gone. Six months later the table that used to answer in a fraction of a millisecond is doing real work on every request, and nobody in the room touched the query. They touched the delete, or rather, they stopped touching it at all.

Soft delete doesn't fail loud. It doesn't throw, it doesn't error, it just quietly keeps every row you have ever written and asks your indexes to filter around a table that only ever gets bigger. That's a decision people make once, in a design doc, and then never measure again. If you've ever added a `deleted_at` column and called the job done, this is for you.

## The problem

`DELETE` and soft-delete look interchangeable at the call site, both just remove a row from what your app can see, but they are not interchangeable underneath. A real `DELETE` marks the tuple dead and `VACUUM` reclaims that space for reuse by the next insert, so a table that deletes as much as it inserts stays roughly flat. A soft delete is an `UPDATE`, and from Postgres's point of view the row is still live, it just has a timestamp in a column now. `VACUUM` has nothing to reclaim, because nothing was removed. The row sits in the heap forever, every index on the table carries it forever, and every query that only wants the "active" rows has to filter it back out at query time instead of the table simply not having it anymore.

I built two identical order tables to put a number on that.

## Two tables, same schema

`hard_delete_orders` and `soft_delete_orders`, same columns, same primary key, same `created_at` index, both with a `deleted_at timestamptz null` for schema parity. "Removing" a row means `DELETE` on one table and `UPDATE ... SET deleted_at = now()` on the other. Fifty churn cycles, each one inserting 5,000 rows and then removing 4,900 of those same freshly-inserted rows, chosen at random across the whole batch rather than only the oldest ones. That last part matters, if only old rows churned, a query for recent rows would never see the dead ones and the whole effect would hide. Both tables get a plain `VACUUM ANALYZE` after every cycle, the realistic autovacuum-equivalent, not a defrag.

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
  .cb-bar-row { display: grid; grid-template-columns: minmax(7.5rem, 1.3fr) minmax(6rem, 4fr) minmax(4.6rem, 0.9fr); gap: 0.55rem; align-items: center; margin: 0.4rem 0; font-size: 0.78rem; }
  .cb-track { height: 0.72rem; overflow: hidden; border-radius: 999px; background: var(--cb-grid); }
  .cb-fill { display: block; width: var(--value); min-width: 2px; height: 100%; border-radius: inherit; background: var(--bar, var(--cb-blue)); }
  .cb-value { color: var(--cb-muted); text-align: right; font-variant-numeric: tabular-nums; }
  .cb-svg { display: block; width: 100%; height: auto; overflow: visible; }
  .cb-svg text { fill: var(--cb-muted); font: 12px system-ui, sans-serif; }
  .cb-svg .grid { stroke: var(--cb-grid); stroke-width: 1; }
  .cb-svg .p999 { fill: none; stroke: var(--cb-orange); stroke-width: 3; stroke-linejoin: round; }
  .cb-svg .p50 { fill: none; stroke: var(--cb-blue); stroke-width: 3; stroke-linejoin: round; }
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
    .cb-bar-row { grid-template-columns: minmax(6rem, 1.3fr) minmax(5rem, 3fr) minmax(3.6rem, 0.8fr); gap: 0.4rem; }
  }
  </style>
  <h3>Table heap size across 50 churn cycles</h3>
  <svg class="cb-svg" viewBox="0 0 640 250" role="img" aria-labelledby="sd-tl-title sd-tl-desc">
    <title id="sd-tl-title">Soft-delete heap climbs steadily; hard-delete heap stays nearly flat</title>
    <desc id="sd-tl-desc">Over 50 churn cycles, hard-delete's table heap grows from about 0.8MB to 1.6MB while soft-delete's grows from about 1.6MB to 42.3MB.</desc>
    <line class="grid" x1="80" y1="210" x2="600" y2="210" />
    <line class="grid" x1="80" y1="120" x2="600" y2="120" />
    <line class="grid" x1="80" y1="30"  x2="600" y2="30" />
    <text x="50" y="214">0</text>
    <text x="24" y="124">20MB</text>
    <text x="24" y="34">40MB</text>
    <polyline class="p999" points="90,203 132,188 184,169 236,150 288,132 340,113 392,94 444,76 496,57 548,38 600,20" />
    <polyline class="p50"  points="90,206 132,206 184,206 236,205 288,205 340,205 392,204 444,204 496,204 548,203 600,203" />
    <text x="70" y="230">cycle 1</text>
    <text x="300" y="230">cycle 25</text>
    <text x="565" y="230">cycle 50</text>
  </svg>
  <div class="cb-legend">
    <span><span class="cb-swatch" style="--swatch:var(--cb-orange)"></span>soft-delete</span>
    <span><span class="cb-swatch" style="--swatch:var(--cb-blue)"></span>hard-delete</span>
  </div>
  <figcaption>hard_delete_orders climbs from 0.80 MB to 1.59 MB over 50 cycles and settles around 5,034 live rows, VACUUM reclaims every physically deleted row's space for the next insert. soft_delete_orders climbs from 1.62 MB to 42.30 MB, carrying all 250,000 rows ever inserted as still-live tuples, because nothing was ever removed for VACUUM to reclaim. That's a 26.7x heap for holding the same 5,000 "currently active" rows. Measured on PostgreSQL 16.14, results in benchmarks/soft-delete-vs-hard-delete/results/bloat_over_time.csv.</figcaption>
</figure>

Twenty-seven times the disk for the same working set isn't the whole story though, disk is cheap. The part that costs you is what a bigger table and a bigger index do to the query that has to walk them.

## Where it actually shows up in a query

A primary-key lookup doesn't care how bloated the table is, it's one index descent either way. What every app actually does over and over is the other query, "give me the active rows," the one that has to walk an index and skip past whatever's dead along the way.

<figure class="cache-bench">
  <h3>PK lookup vs active-rows query latency, p50</h3>
  <div class="cb-bar-row">
    <span>hard, pk lookup</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 40.8%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">0.205 ms</span>
  </div>
  <div class="cb-bar-row">
    <span>soft, pk lookup</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 42.1%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">0.212 ms</span>
  </div>
  <div class="cb-bar-row">
    <span>hard, active rows</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 54.4%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">0.274 ms</span>
  </div>
  <div class="cb-bar-row">
    <span>soft, active rows</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">0.503 ms</span>
  </div>
  <figcaption>PK lookups land within a few percent either way, 0.205ms vs 0.212ms p50, a point lookup is one index descent regardless of table size. The active-rows query (<code>WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 50</code>) is where it shows up: 0.274ms on hard-delete vs 0.503ms on soft-delete, about 1.8x. <code>EXPLAIN (ANALYZE, BUFFERS)</code> on the soft-delete side reports <code>Rows Removed by Filter: 1948</code>, it walks past 1,948 dead rows in the index to find 50 live ones, versus 0 for hard-delete, whose index only ever contains live rows. Measured on PostgreSQL 16.14, 1,000 iterations per query, results in benchmarks/soft-delete-vs-hard-delete/results/query_latency.csv and explain_summary.csv.</figcaption>
</figure>

That's the whole mechanism in one number. The index isn't wrong, it's doing exactly what a b-tree does, walking entries in order and testing the filter on each one. It's just that on the soft-delete table, an increasing fraction of what it walks past is dead weight it has no way to skip, because from the index's point of view a soft-deleted row looks exactly like a live one until the filter runs.

## The fix: index only the rows you still call live

The column that's making the index slow, `deleted_at`, is also the column that tells you exactly which rows to leave out of it.

```sql
CREATE INDEX soft_delete_orders_active_created_at_idx
  ON soft_delete_orders (created_at DESC)
  WHERE deleted_at IS NULL;
```

A partial index only ever contains rows matching its predicate, so it doesn't grow with the soft-deleted rows at all, it grows with your active set, same as hard-delete's index does.

<figure class="cache-bench">
  <h3>Secondary index size, full vs partial</h3>
  <div class="cb-bar-row">
    <span>soft, full index</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">9,976 KB</span>
  </div>
  <div class="cb-bar-row">
    <span>soft, partial index</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 1.3%; --bar: var(--cb-green);"></span></span>
    <span class="cb-value">128 KB</span>
  </div>
  <div class="cb-bar-row">
    <span>hard, reference</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 83.7%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">8,352 KB</span>
  </div>
  <figcaption>The partial index is 128 KB against the original full index's 9,976 KB, 77.9x smaller, it only carries the ~5,000 rows still live instead of all 250,000 ever inserted. hard-delete's own index sits at 8,352 KB for comparison, that number is its own story below. Measured on PostgreSQL 16.14, results in benchmarks/soft-delete-vs-hard-delete/results/partial_index_fix.csv.</figcaption>
</figure>

One honest wrinkle before the payoff: hard-delete's index at 8,352 KB is a lot bigger than you'd expect for 5,034 rows, bigger even than it has any right to be next to the fresh 128 KB partial index covering roughly the same row count. That's not a soft-delete effect, it's a separate b-tree phenomenon. `pgstatindex()` on hard-delete's own `created_at` index shows `avg_leaf_density` of 1.6% and `leaf_fragmentation` of 97%. A monotonically increasing key under heavy insert-and-delete churn bloats a b-tree through page fragmentation regardless of delete strategy, new inserts always land on a fresh page at the end, and a plain `VACUUM` reclaims dead entries but never rebalances the mostly-empty pages left behind. It's a real Postgres gotcha worth knowing about on its own, but it doesn't touch the partial index, which is freshly built and unfragmented, which is exactly why 128 KB is the clean number to trust here.

<figure class="cache-bench">
  <h3>Active-rows p99 latency, before and after the partial index</h3>
  <div class="cb-bar-row">
    <span>soft, full index</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 100%; --bar: var(--cb-orange);"></span></span>
    <span class="cb-value">1.915 ms</span>
  </div>
  <div class="cb-bar-row">
    <span>soft, partial index</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 57.1%; --bar: var(--cb-green);"></span></span>
    <span class="cb-value">1.094 ms</span>
  </div>
  <div class="cb-bar-row">
    <span>hard, reference</span>
    <span class="cb-track"><span class="cb-fill" style="--value: 34.9%; --bar: var(--cb-blue);"></span></span>
    <span class="cb-value">0.668 ms</span>
  </div>
  <figcaption>p99 drops from 1.915ms to 1.094ms after adding the partial index, 1.75x, landing close to hard-delete's own reference number on the same query. The single sampled <code>EXPLAIN (ANALYZE, BUFFERS)</code> execution goes from 1,993 shared buffers and 0.239ms to 39 shared buffers and 0.020ms, and <code>Rows Removed by Filter</code> drops from 1,948 to 0. Measured on PostgreSQL 16.14, 1,000 iterations, results in benchmarks/soft-delete-vs-hard-delete/results/partial_index_fix.csv and explain_active_rows_soft_partial.txt.</figcaption>
</figure>

51x fewer buffers touched on that sampled execution, 12x faster on it specifically. The partial index doesn't just get smaller, it turns "scan past ~2,000 dead rows to find 50 live ones" into "read 39 buffers and you're done."

## The takeaway

Soft delete isn't free, it's deferred. The cost doesn't show up on the day you ship the `deleted_at` column, it shows up months later as a table that never gets smaller and an index that has to work around dead weight it can't skip. The fix isn't "stop soft-deleting," an audit trail is usually the entire point of choosing it in the first place. The fix is a partial index on whatever query path actually reads your active rows, `WHERE deleted_at IS NULL` (or your equivalent) is a predicate Postgres can push down to the index instead of filtering row by row at query time. Add it when you add the column, not after the table's 250,000 rows deep and someone's asking why the dashboard got slow.

And know what it doesn't fix: the partial index shrinks the index and speeds up the query, it does nothing about the heap itself. The table in this benchmark is still sitting at 42.3 MB carrying every row it's ever held, that number doesn't move unless something eventually hard-deletes or archives rows past their retention window. Soft delete without a purge job isn't really a decision anymore, it's just a table nobody ever finishes deleting from.

The harness, all three experiments and the raw CSVs, is [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/soft-delete-vs-hard-delete). These are laptop numbers meant to show the mechanism and the size of each effect, not capacity planning for your table, run it against your own schema and churn pattern before you quote a bloat ratio to anyone.
