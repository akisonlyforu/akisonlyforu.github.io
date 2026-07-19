---
layout:     post
title:      The Gentle Fix That Wasn't
date:       2026-07-20
description:    Rebuilding and reorganizing a 99%-fragmented SQL Server index landed at nearly the same fragmentation number, 0.16% vs 0.46%. Rebuild took 0.22 seconds and 0.4MB of transaction log. Reorganize took 5.42 seconds and about 255MB, roughly 615x more log for a comparable result.
categories: sql-server fragmentation index-maintenance databases
---

Open the maintenance plan wizard in SSMS and there are two tasks sitting next to each other, Rebuild Index and Reorganize Index, with a fragmentation threshold slider between them. Most people drag the slider to whatever the wizard suggests, somewhere around 5% for reorganize and 30% for rebuild, and never think about it again. I'd been treating the two as interchangeable for years, two roads to the same clean index, until I actually sat down to measure what each one costs to get there.

## The problem

A b-tree index degrades the same way regardless of which database sits under it. Insert rows out of key order and pages that are already full have to split, leaving half-empty pages whose leaf entries are no longer physically adjacent to their logical neighbors. Range scans that used to walk contiguous pages start jumping around instead. The fix is either REBUILD, which drops the index and writes a fresh one from scratch, or REORGANIZE, which walks the existing b-tree in place and compacts it. Both leave you with a defragmented index when they're done. What I wanted to know is how fast fragmentation actually builds, and what each fix costs to undo it, because "reorganize is the lighter-weight option" gets repeated a lot without anyone checking what lighter-weight means in seconds and megabytes of log.

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

## How fast it actually falls apart

I built a 2,000,000-row `orders` table with a clustered identity PK and a nonclustered index on `(customer_id, created_at)`, the "recent orders for this customer" index every orders table ends up with. To fragment it on purpose I made `customer_id` non-sequential, hashing a row counter through `HASHBYTES('MD5', ...)` instead of letting it increment, so every insert lands somewhere in the middle of the b-tree instead of at the end. Then I churned it in growing batches, 200 rows, then 600, then 1,400, roughly doubling each time, and read `avg_fragmentation_in_percent` off `sys.dm_db_index_physical_stats` after each one.

```sql
SELECT avg_fragmentation_in_percent, avg_page_space_used_in_percent, page_count
FROM sys.dm_db_index_physical_stats(DB_ID(), OBJECT_ID('orders'), NULL, NULL, 'DETAILED')
WHERE index_id = 2;
```

| checkpoint | cum. churn rows | frag % | page use % | pages | avg logical reads |
|---|---|---|---|---|---|
| baseline | 0 | 0.17 | 99.90 | 5,440 | 275 |
| churn-1 | 200 | 7.11 | 96.44 | 5,636 | 284 |
| churn-2 | 600 | 18.91 | 90.50 | 6,007 | 303 |
| churn-3 | 1,400 | 37.12 | 81.34 | 6,686 | 337 |
| churn-4 | 3,000 | 58.83 | 70.43 | 7,727 | 377 |
| churn-5 | 6,200 | 80.59 | 59.54 | 9,154 | 456 |
| churn-6 | 12,600 | 93.88 | 53.00 | 10,317 | 526 |
| churn-7 | 25,400 | 98.80 | 50.80 | 10,832 | 543 |
| churn-8 | 51,000 | 99.21 | 51.22 | 10,879 | 546 |

Two hundred rows against roughly 5,440 leaf pages, 0.16% of the table's own page count, was enough to push fragmentation from a clean 0.17% to 7.11%. By the time I'd churned in 51,000 rows, about 2.5% of the table, fragmentation sat at 99.21% and the index had grown from 5,440 pages to 10,879, almost double, for the same number of logical rows. Logical reads for a range scan against that index tracked the page growth almost exactly, 275 up to 546. The millisecond column barely moved the whole time, 11.5ms to 12.8ms, because at this row count the scan stays small enough to be cache-bound rather than IO-bound. Logical reads is the number telling the truth here, not the clock.

<figure class="cache-bench">
  <h3>Fragmentation and logical reads, as the churn grows</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">fragmentation %</p>
      <div class="cb-bar-row"><span>baseline</span><span class="cb-track"><span class="cb-fill" style="--value:0.17%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.17</span></div>
      <div class="cb-bar-row"><span>churn-1</span><span class="cb-track"><span class="cb-fill" style="--value:7.11%;--bar:var(--cb-green)"></span></span><span class="cb-value">7.11</span></div>
      <div class="cb-bar-row"><span>churn-2</span><span class="cb-track"><span class="cb-fill" style="--value:18.91%;--bar:var(--cb-blue)"></span></span><span class="cb-value">18.91</span></div>
      <div class="cb-bar-row"><span>churn-3</span><span class="cb-track"><span class="cb-fill" style="--value:37.12%;--bar:var(--cb-blue)"></span></span><span class="cb-value">37.12</span></div>
      <div class="cb-bar-row"><span>churn-4</span><span class="cb-track"><span class="cb-fill" style="--value:58.83%;--bar:var(--cb-orange)"></span></span><span class="cb-value">58.83</span></div>
      <div class="cb-bar-row"><span>churn-5</span><span class="cb-track"><span class="cb-fill" style="--value:80.59%;--bar:var(--cb-orange)"></span></span><span class="cb-value">80.59</span></div>
      <div class="cb-bar-row"><span>churn-6</span><span class="cb-track"><span class="cb-fill" style="--value:93.88%;--bar:var(--cb-orange)"></span></span><span class="cb-value">93.88</span></div>
      <div class="cb-bar-row"><span>churn-8</span><span class="cb-track"><span class="cb-fill" style="--value:99.21%;--bar:var(--cb-orange)"></span></span><span class="cb-value">99.21</span></div>
    </div>
    <div>
      <p class="cb-panel-title">avg logical reads (of 546 max)</p>
      <div class="cb-bar-row"><span>baseline</span><span class="cb-track"><span class="cb-fill" style="--value:50.4%;--bar:var(--cb-purple)"></span></span><span class="cb-value">275</span></div>
      <div class="cb-bar-row"><span>churn-1</span><span class="cb-track"><span class="cb-fill" style="--value:52%;--bar:var(--cb-purple)"></span></span><span class="cb-value">284</span></div>
      <div class="cb-bar-row"><span>churn-2</span><span class="cb-track"><span class="cb-fill" style="--value:55.5%;--bar:var(--cb-purple)"></span></span><span class="cb-value">303</span></div>
      <div class="cb-bar-row"><span>churn-3</span><span class="cb-track"><span class="cb-fill" style="--value:61.7%;--bar:var(--cb-purple)"></span></span><span class="cb-value">337</span></div>
      <div class="cb-bar-row"><span>churn-4</span><span class="cb-track"><span class="cb-fill" style="--value:69%;--bar:var(--cb-purple)"></span></span><span class="cb-value">377</span></div>
      <div class="cb-bar-row"><span>churn-5</span><span class="cb-track"><span class="cb-fill" style="--value:83.5%;--bar:var(--cb-purple)"></span></span><span class="cb-value">456</span></div>
      <div class="cb-bar-row"><span>churn-6</span><span class="cb-track"><span class="cb-fill" style="--value:96.3%;--bar:var(--cb-purple)"></span></span><span class="cb-value">526</span></div>
      <div class="cb-bar-row"><span>churn-8</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-purple)"></span></span><span class="cb-value">546</span></div>
    </div>
  </div>
  <figcaption>Fragmentation crosses 7% after churning in just 200 out of ~5,440 leaf pages, and passes 99% by the time ~2.5% of the table has churned. Logical reads for the range query track the page count almost exactly, roughly doubling alongside it. Measured on SQL Server 2022, results in benchmarks/sqlserver-fragmentation/results/.</figcaption>
</figure>

## REBUILD vs REORGANIZE from the same mess

To compare the two fixes fairly I built two identical tables from the same base rows and ran both through the exact same churn schedule, landing them at almost the same fragmentation, 99.16% and 99.30%. Then I ran one operation against each:

```sql
ALTER INDEX ix_orders_customer_created ON orders REBUILD;
```

```sql
ALTER INDEX ix_orders_customer_created ON orders REORGANIZE;
```

| operation | frag before | frag after | elapsed s | log growth (MB) |
|---|---|---|---|---|
| REBUILD | 99.16% | 0.16% | 0.22 | 0.41 |
| REORGANIZE | 99.30% | 0.46% | 5.42 | 254.96 |

Both did their job. Rebuild landed at 0.16% fragmented, reorganize at 0.46%, close enough to call a tie on the fragmentation number alone. What wasn't a tie was how they got there. Rebuild took 0.22 seconds and grew the transaction log by about 0.4MB. Reorganize took 5.42 seconds, 24.5x longer, and grew the log by roughly 255MB, on the order of 615x more, to land at an index that was, if anything, marginally more fragmented than the one rebuild produced.

<figure class="cache-bench">
  <h3>Same starting mess, same fragmentation result, very different bill</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">elapsed seconds</p>
      <div class="cb-bar-row"><span>REBUILD</span><span class="cb-track"><span class="cb-fill" style="--value:4.1%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.22s</span></div>
      <div class="cb-bar-row"><span>REORGANIZE</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">5.42s</span></div>
    </div>
    <div>
      <p class="cb-panel-title">log growth</p>
      <div class="cb-bar-row"><span>REBUILD</span><span class="cb-track"><span class="cb-fill" style="--value:0.2%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.41 MB</span></div>
      <div class="cb-bar-row"><span>REORGANIZE</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">254.96 MB</span></div>
    </div>
  </div>
  <figcaption>From an identical ~99% fragmented starting state, REBUILD finished in 0.22s and 0.41MB of log; REORGANIZE took 5.42s and 254.96MB, for a resulting fragmentation that was, if anything, slightly worse than REBUILD's. Measured on SQL Server 2022, database in SIMPLE recovery, results in benchmarks/sqlserver-fragmentation/results/.</figcaption>
</figure>

That gap is the mechanism, not a fluke. REBUILD under SIMPLE or BULK_LOGGED recovery is a bulk rewrite, minimally logged, because SQL Server only needs to remember that a new index replaced an old one, not how to undo it row by row. REORGANIZE never gets that shortcut. It's an in-place, page-by-page compaction, and it's always fully logged no matter the recovery model, because every row it moves is its own logged operation. Reorganize doesn't take the kind of lock an offline rebuild would, so it can run alongside other traffic, which is the whole reason it exists. It just does that by spending log space instead, and if you're shipping that log to a replica or an archive, 615x is not a rounding error.

## Does reorganize keep up when things are worse

Rebuild and reorganize from 99% fragmentation is the extreme case. I wanted to know whether reorganize's cost was specifically a high-fragmentation problem, so I ran the same pair of operations again starting from a lightly fragmented table, about 19%, churned in with 600 rows instead of 51,000.

| level | operation | frag before | frag after | elapsed s |
|---|---|---|---|---|
| low (~19%) | REBUILD | 19.07 | 0.17 | 0.16 |
| low (~19%) | REORGANIZE | 19.05 | 0.33 | 1.62 |
| high (~99%) | REBUILD | 99.16 | 0.16 | 0.22 |
| high (~99%) | REORGANIZE | 99.30 | 0.46 | 5.42 |

Rebuild's elapsed time barely cared how fragmented the starting index was, 0.16 seconds at 19% and 0.22 seconds at 99%. Reorganize's did, 1.62 seconds at 19% and 5.42 seconds at 99%, about 3.3x slower for a starting point that was more than 5x as fragmented. That's the shape you'd expect: rebuild always does the same amount of work, read everything, write a fresh copy, regardless of how messy the input is. Reorganize's amount of work scales with how much disorder it has to walk through and shuffle back into place.

<figure class="cache-bench">
  <h3>Elapsed time, low vs high starting fragmentation</h3>
  <div class="cb-bar-row"><span>REBUILD, ~19%</span><span class="cb-track"><span class="cb-fill" style="--value:3%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.16s</span></div>
  <div class="cb-bar-row"><span>REORGANIZE, ~19%</span><span class="cb-track"><span class="cb-fill" style="--value:29.9%;--bar:var(--cb-blue)"></span></span><span class="cb-value">1.62s</span></div>
  <div class="cb-bar-row"><span>REBUILD, ~99%</span><span class="cb-track"><span class="cb-fill" style="--value:4.1%;--bar:var(--cb-green)"></span></span><span class="cb-value">0.22s</span></div>
  <div class="cb-bar-row"><span>REORGANIZE, ~99%</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">5.42s</span></div>
  <figcaption>REBUILD's cost is nearly flat regardless of starting fragmentation. REORGANIZE's scales with it, about 3.3x slower going from ~19% to ~99% starting fragmentation. Measured on SQL Server 2022, results in benchmarks/sqlserver-fragmentation/results/.</figcaption>
</figure>

One thing this run didn't reproduce, and I want to be straight about it: the usual guidance is that reorganize doesn't just get slower at high fragmentation, it can also leave you meaningfully more fragmented than a rebuild would, because it's single-threaded and can lose the race against continued writes. On this benchmark, an idle single session with nothing else touching the table while reorganize ran, it fully compacted both the 19% and the 99% case, 0.33% and 0.46% after, both close to rebuild's numbers. What I measured cleanly here is the time and log cost. The residual-fragmentation gap is a real thing on a busy table with concurrent writes contending for the same pages reorganize is trying to compact, this harness just doesn't generate that kind of contention, so I'm only claiming the half I actually saw.

## What the fix actually buys the query

None of the above matters if the fix doesn't help the thing you cared about in the first place, which is how expensive the query is. I ran the same range scan fragmented, then again after each fix.

| state | avg logical reads |
|---|---|
| fragmented | 548 |
| after REBUILD | 282 |
| after REORGANIZE | 283 |

Logical reads dropped from 548 fragmented to 282 after rebuild and 283 after reorganize, both fixes buying back almost exactly the same roughly 2x reduction, which lines up with the roughly 2x page-count difference from the first experiment. Millisecond timings stayed flat and noisy across all three states, 11.7 to 12.5ms, so once again logical reads is the signal worth trusting, the clock isn't sensitive enough at this scale to show anything.

<figure class="cache-bench">
  <h3>Logical reads, fragmented vs after each fix</h3>
  <div class="cb-bar-row"><span>fragmented</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">548</span></div>
  <div class="cb-bar-row"><span>after REBUILD</span><span class="cb-track"><span class="cb-fill" style="--value:51.5%;--bar:var(--cb-green)"></span></span><span class="cb-value">282</span></div>
  <div class="cb-bar-row"><span>after REORGANIZE</span><span class="cb-track"><span class="cb-fill" style="--value:51.6%;--bar:var(--cb-blue)"></span></span><span class="cb-value">283</span></div>
  <figcaption>Either fix buys back roughly the same query improvement, logical reads dropping by about half. The choice between them isn't about which one helps the query more. Measured on SQL Server 2022, results in benchmarks/sqlserver-fragmentation/results/.</figcaption>
</figure>

## Stuff worth remembering

- Fragmentation builds faster than you'd guess on a non-sequential key. 200 rows out of 5,440 pages was enough to cross 7%, and by roughly 2.5% of the table churned it was past 99%.
- REBUILD and REORGANIZE can land at nearly the same fragmentation number and still cost wildly different amounts. In this run, rebuild took 0.22s and 0.4MB of log; reorganize took 5.42s and about 255MB, for a comparable result.
- The reason is logging, not CPU. Rebuild is a wholesale rewrite that's minimally logged under SIMPLE or BULK_LOGGED recovery. Reorganize is an in-place compaction that's always fully logged, row move by row move, regardless of recovery model.
- Reorganize's cost scales with how fragmented you start, rebuild's doesn't. Going from ~19% to ~99% starting fragmentation cost reorganize about 3.3x more time; rebuild's time barely moved.
- Either fix buys back roughly the same query improvement, about a 2x drop in logical reads in this run. The choice between them isn't about which one helps the query more, it's about what you can afford to spend to get there: log space, replica lag, lock duration.
- These are laptop numbers demonstrating the mechanism, [the SQL Server container and the script are in the repo](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/sqlserver-fragmentation). The seconds and megabytes came off my machine; the shape of the gap shows up on any SQL Server you point this at.

## The takeaway

Rebuild and reorganize both get you a clean index, so picking between them on the fragmentation percentage alone misses where the actual cost shows up, which is the transaction log and the lock behavior, not the after-number. Reorganize's appeal is that it can run online without the exclusive lock an offline rebuild takes, but it pays for that by being fully logged no matter what, and the bill scales with how messy the index already is. The traditional cutoff, reorganize under 30% fragmented and rebuild above it, caps how much log reorganize gets to burn before rebuild's flat cost wins outright. That's the reasoning worth remembering, more than the fragmentation percentage itself.
