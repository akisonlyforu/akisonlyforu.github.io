---
layout: post
title: The Export That Got Slower Every Page
date: 2025-06-24
description: A CSV export paged through a million rows with LIMIT/OFFSET and slowed down the deeper it went. Deep OFFSET re-walks every row it skips, so the last page reads almost the whole table. Measured on Postgres 16.
categories: [postgres, performance, databases]
---

The export worked fine in the demo. Ten thousand rows, done before you let go of the button. Then a real account with a couple million issues asked for the same CSV, and the job that used to finish in a second took most of a minute, and the strange part was that it got *slower as it went*. The first pages flew. The last pages crawled. Same query, same page size, same table, and yet page number nine hundred took thirty times longer than page number one.

If you've ever paged through a big table with `LIMIT n OFFSET k` and watched the tail of the export drag while the head was instant, this is for you. The export wasn't getting slower because the machine was tired. It was getting slower because of what `OFFSET` actually does, which is nothing like what it reads like.

## The problem

An export streams a whole table out one page at a time. The obvious way to write it is offset pagination:

```sql
SELECT id, project_id, status, title, body, created_at
FROM issues
ORDER BY id
LIMIT 1000 OFFSET 0;      -- page 1
-- ... then OFFSET 1000, OFFSET 2000, ...
SELECT id, project_id, status, title, body, created_at
FROM issues
ORDER BY id
LIMIT 1000 OFFSET 990000; -- page 991
```

It reads like it should be cheap. "Skip 990,000 rows, give me the next 1,000." But a relational database has no way to *jump* to the 990,000th row of an ordered result. There's no address for "the row at position k". To know which row sits at offset 990,000, it has to produce the first 990,000 rows in order and throw them all away, then hand you the next thousand. Every page re-does the work of every page before it. The export as a whole is O(N²), and the cost is invisible until the table is big enough for the square to hurt.

I built a table of a million `issues` rows, about 200 bytes each, 274 MB total, and exported the whole thing three ways: naive OFFSET, keyset (remember where you were), and a server-side cursor. Page size 1,000 for all of them.

## The per-page shape gives it away

Here's the per-page latency for the OFFSET export against the keyset one, plotted across the offset. Same page size, same 1,000 rows returned every time. The only thing changing is how far into the table the page sits.

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
  .cb-panels { grid-template-columns: 1fr; }
  .cb-bar-row { grid-template-columns: minmax(6rem, 1.3fr) minmax(5rem, 3fr) minmax(3.6rem, 0.8fr); gap: 0.4rem; }
}
</style>

<figure class="cache-bench">
  <h3>Per-page latency across the export</h3>
  <svg class="cb-svg" viewBox="0 0 640 250" role="img" aria-labelledby="pg-tl-title pg-tl-desc">
    <title id="pg-tl-title">OFFSET per-page latency climbs with depth; keyset stays flat</title>
    <desc id="pg-tl-desc">OFFSET page latency rises from about 2ms at the start to over 80ms at the end of the table; keyset page latency stays flat near the bottom the whole way.</desc>
    <line class="grid" x1="80" y1="210" x2="600" y2="210" />
    <line class="grid" x1="80" y1="120" x2="600" y2="120" />
    <line class="grid" x1="80" y1="30"  x2="600" y2="30" />
    <text x="34" y="214">0</text>
    <text x="18" y="124">50ms</text>
    <text x="10" y="34">100ms</text>
    <polyline class="p999" points="80,206 132,200 184,189 236,181 288,171 340,136 392,125 444,117 496,102 548,91 600,79" />
    <polyline class="p50"  points="80,207 236,206 392,206 548,206 600,206" />
    <text x="70" y="230">offset 0</text>
    <text x="300" y="230">500k</text>
    <text x="560" y="230">1M</text>
  </svg>
  <div class="cb-legend">
    <span><span class="cb-swatch" style="--swatch:var(--cb-orange)"></span>OFFSET</span>
    <span><span class="cb-swatch" style="--swatch:var(--cb-blue)"></span>keyset</span>
  </div>
  <figcaption>Each point is the time to fetch one 1,000-row page. OFFSET climbs from ~2.3ms at the head to ~73ms near the tail; keyset holds flat around 1.2 to 3ms regardless of depth. Measured on PostgreSQL 16.14, results in benchmarks/postgres-export-pagination/results/exp1_offset_pages.csv and exp2_keyset_pages.csv.</figcaption>
</figure>

The keyset line is boring on purpose, that's the point. It's a flat smear along the floor because every keyset page does the same amount of work no matter where it is. The OFFSET line is a ramp, because every OFFSET page does *more* work than the one before it. By the deep end, OFFSET's worst pages were 37x slower than keyset's pages at the identical position in the table (88.713ms vs 2.430ms).

## Why OFFSET gets slower the deeper it goes

You don't have to take the shape on faith, `EXPLAIN (ANALYZE, BUFFERS)` says it out loud. Because the ids are a gapless identity sequence, the page at `OFFSET 990000` and the page at `WHERE id > 990000` return the exact same physical rows, so this is a clean apples-to-apples read of the same 1,000 rows fetched two ways:

```
query    position   scan          rows scanned   rows discarded   time        buffers
offset   0          Index Scan          1,000              0       0.158ms         76
offset   990000     Index Scan        991,000        990,000     121.322ms     69,310
keyset   990000     Index Scan          1,000              0       0.098ms         78
```

Read the middle row slowly. To return 1,000 rows from offset 990,000, Postgres walked the index for **991,000 rows and threw away 990,000 of them**. It touched 69,310 buffer blocks to hand back a page it could have fetched in 78. The shallow OFFSET page and the keyset page are basically free, 76 and 78 blocks, because they only touch what they return. Deep OFFSET pays for the whole prefix, every single page, and the prefix keeps growing.

That's the O(N²). Page 1 skips 0, page 2 skips 1,000, page 991 skips 990,000, and the sum of all that skipping is a triangle that scales with the square of the row count. The database isn't confused and the index isn't missing, `OFFSET` is doing precisely what it's defined to do. It's just defined to be expensive.

## What it costs end to end

Add up every page and here's the whole export, wall-clock, median of three runs each:

<figure class="cache-bench">
  <h3>Full export of 1,000,000 rows, wall-clock</h3>
  <div class="cb-bar-row"><span>OFFSET</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">35.35s</span></div>
  <div class="cb-bar-row"><span>keyset</span><span class="cb-track"><span class="cb-fill" style="--value:4.2%;--bar:var(--cb-green)"></span></span><span class="cb-value">1.50s</span></div>
  <div class="cb-bar-row"><span>server cursor</span><span class="cb-track"><span class="cb-fill" style="--value:4.4%;--bar:var(--cb-blue)"></span></span><span class="cb-value">1.54s</span></div>
  <figcaption>Same million rows, same 1,000-row pages, three pagination strategies. OFFSET took 23.6x longer than keyset for identical output. Measured on PostgreSQL 16.14, results in benchmarks/postgres-export-pagination/results/run_metadata.csv.</figcaption>
</figure>

35.35 seconds versus 1.50. Same rows out the other end, byte for byte. The only difference is that OFFSET spends most of that 35 seconds re-reading rows it already sent on earlier pages. Two identical exports, one of them 24x slower, and the slow one is the one everybody writes first because it's the one the tutorials show.

## The fix: remember where you were

Keyset pagination, sometimes called seek pagination, doesn't count rows to skip. It remembers the last id it saw and asks the index to seek straight past it:

```sql
-- first page
SELECT id, project_id, status, title, body, created_at
FROM issues
WHERE id > 0
ORDER BY id
LIMIT 1000;

-- every page after: feed in the last id from the previous page
SELECT id, project_id, status, title, body, created_at
FROM issues
WHERE id > 990000
ORDER BY id
LIMIT 1000;
```

The `WHERE id > :last_id ORDER BY id LIMIT 1000` lets the B-tree jump to the boundary and read exactly 1,000 index entries, no matter how deep you are. That's the `keyset 990000` line in the EXPLAIN above, 1,000 rows scanned, 0 discarded, 78 buffers, 0.098ms. It costs the same at row 990,000 as it does at row 1,000, which is why the blue line in the first chart never lifts off the floor.

The mechanism, side by side, is the whole story:

<figure class="cache-bench">
  <h3>Work done to fetch one 1,000-row page</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">Index rows walked</p>
      <div class="cb-bar-row"><span>OFFSET, page 1</span><span class="cb-track"><span class="cb-fill" style="--value:0.1%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1,000</span></div>
      <div class="cb-bar-row"><span>OFFSET, last page</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">991,000</span></div>
      <div class="cb-bar-row"><span>keyset, last page</span><span class="cb-track"><span class="cb-fill" style="--value:0.1%;--bar:var(--cb-green)"></span></span><span class="cb-value">1,000</span></div>
    </div>
    <div>
      <p class="cb-panel-title">Buffer blocks touched</p>
      <div class="cb-bar-row"><span>OFFSET, page 1</span><span class="cb-track"><span class="cb-fill" style="--value:0.11%;--bar:var(--cb-orange)"></span></span><span class="cb-value">76</span></div>
      <div class="cb-bar-row"><span>OFFSET, last page</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">69,310</span></div>
      <div class="cb-bar-row"><span>keyset, last page</span><span class="cb-track"><span class="cb-fill" style="--value:0.11%;--bar:var(--cb-green)"></span></span><span class="cb-value">78</span></div>
    </div>
  </div>
  <figcaption>The deep OFFSET page walks 991x more index rows and touches ~890x more buffer blocks than the keyset page returning the identical 1,000 rows. Measured on PostgreSQL 16.14, results in benchmarks/postgres-export-pagination/results/exp4_explain.csv.</figcaption>
</figure>

## The other fix: let the server hold the cursor

There's a second way out that's even simpler if all you're doing is streaming the whole table start to finish: a server-side cursor. You run one query, `DECLARE` a cursor over it, and `FETCH 1000` at a time. The server keeps its place in the single scan for you, so there's no skipping and no re-issuing the query per page:

```python
cur = conn.cursor(name="export_cur")  # named cursor -> server-side DECLARE
cur.itersize = 1000
cur.execute("SELECT id, project_id, status, title, body, created_at "
            "FROM issues ORDER BY id")
while True:
    rows = cur.fetchmany(1000)         # -> FETCH 1000
    if not rows:
        break
    write(rows)
```

One plan, one scan, streamed in batches. It came in at 1.54s, right next to keyset's 1.50s, and its per-FETCH latency was as flat as keyset's (p99 3.440ms). The tradeoff is that a cursor holds a transaction and a connection open for the entire export, so a slow consumer pins server resources the whole time, and it's a single-connection stream rather than something you can resume from a token. For "dump the whole table," it's great. For "let a client walk pages at its own pace over a stateless API," keyset is the one that survives.

## What keyset costs you

Keyset isn't free of tradeoffs, it's just cheaper where it counts:

- **You need a unique, ordered key to seek on.** An indexed `id` is perfect. If you're sorting by something non-unique like `created_at`, you have to page on a tuple, `WHERE (created_at, id) > (:last_ts, :last_id)`, and have an index that matches that order, or you'll silently skip or repeat rows at the boundaries.
- **You can't jump to "page 500".** Keyset only knows "the page after this row". If your UI has numbered page links, offset gives you random access and keyset doesn't. For an export, nobody wants page 500 in isolation, they want all of it in order, so this costs you nothing. For a paginated search UI it might.
- **The sort has to match the index.** Change the `ORDER BY` and you need a `WHERE` and an index that line up with it, or you're back to scanning.

None of that mattered for the export, which is exactly the case where keyset wins cleanly: one direction, in order, all the way through.

## The takeaway

`OFFSET` doesn't skip rows, it reads them and discards them, so any pagination that walks deep into a table pays a cost that grows with how deep it goes, and a full export walks all the way to the bottom. On a million rows that turned a 1.5-second job into a 35-second one, 24x slower for identical output, with the last pages the slowest part.

If you're paging through a table to export or process the whole thing, don't count rows to skip, remember the last key you saw and seek past it, or hold a server-side cursor if you just need a straight dump. Keep `OFFSET` for shallow, human-facing pagination where nobody clicks past page five. The tell in production is an export whose per-page time climbs as it runs, if the tail is slower than the head and the page size never changed, `OFFSET` is reading everything you already skipped.

The harness (Docker Compose, the loader, all three strategies, the EXPLAIN capture, and the raw CSVs) is [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/postgres-export-pagination). These are laptop numbers meant to show the shape of the problem, not a capacity plan, the absolute times will differ on your hardware but the ratio between the strategies is the part that travels.
