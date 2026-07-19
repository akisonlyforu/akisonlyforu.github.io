---
layout:     post
title:      The Update That Locked Rows It Never Touched
date:       2026-07-20
description:    A SQL Server UPDATE that touched rows 1 to 8000 blocked a point read of row 150000 for three full seconds. Lock escalation collapsed 8000 row locks into a single table lock, and the read baseline of 33ms became a 3001ms wait. Below the cliff, the same read was 0.5ms. Batching the update kept it there.
categories: sql-server locking contention databases
---

If you've ever watched a single point query hang for a few seconds while some unrelated batch job ran in another session, and the two were nowhere near each other in the table, this is for you. The query was reading one row by primary key. The batch job was updating a completely different range of rows. On paper they never intersect, and row-level locking is supposed to let them run right past each other. But the point read sat there and waited, and when I pulled the wait stats it was blocked on a lock the batch job never explicitly asked for.

That lock is lock escalation, and it turns a row-level update into a table-level outage.

## The problem

SQL Server starts a big write with fine-grained locks, one per row or per page, so that concurrent sessions touching other rows can keep going. That's the whole point of row locking. But locks cost memory, and holding tens of thousands of them for one statement is expensive, so when a single statement acquires roughly 5000 locks on one object, SQL Server trades them all in for one lock on the entire table. It's an optimization for the writer. For everyone else it's a wall. A table-level exclusive lock means no other session can touch any row of that table, including rows the writer never went near, until the writing transaction commits.

So the failure mode isn't "two sessions fought over the same row." It's "one session updated 8000 rows and the whole table went dark." I wanted to see the exact moment it flips, so I built a SQL Server 2022 with a 200,000-row `orders` table, a clustered primary key on `id`, and started holding transactions open on purpose.

## Watching an untouched row block

First experiment, the simplest one. Open a transaction, update rows 1 through 8000, and leave it open. Then from a second session read row 150000 by primary key, a row the update never touched, and time it.

```sql
-- session 1
BEGIN TRAN;
UPDATE orders SET amount = amount + 1 WHERE id BETWEEN 1 AND 8000;
-- (held open ~3 seconds, then COMMIT)

-- session 2, meanwhile
SELECT amount FROM orders WHERE id = 150000;
```

While session 1 held its transaction open, I looked at what it was actually holding in `sys.dm_tran_locks`:

```
resource_type   request_mode   count
KEY             -              0
PAGE            -              0
OBJECT          X              1
```

Zero row locks. Zero page locks. One exclusive lock on the whole object. The 8000 individual row locks it started with had already collapsed into a single table X lock before I even looked. And session 2, reading a row 142,000 positions away from anything the update touched, went from a 33.4ms baseline to a 3001ms wait, returning only after session 1 committed at 3010ms. It didn't slow down. It stopped, and waited out the writer.

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

<figure class="cache-bench">
  <h3>A point read of row 150000, while rows 1 to 8000 were being updated</h3>
  <div>
    <p class="cb-panel-title">point SELECT latency, ms</p>
    <div class="cb-bar-row"><span>baseline (no writer)</span><span class="cb-track"><span class="cb-fill" style="--value:1.1%;--bar:var(--cb-green)"></span></span><span class="cb-value">33.4</span></div>
    <div class="cb-bar-row"><span>during escalated update</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">3001</span></div>
  </div>
  <figcaption>The read touches a row the update never came close to, yet it blocks for the full life of the writer's transaction: 33.4ms with no writer, 3001ms while the 8000-row update held its escalated table lock (the writer held for 3010ms). Measured on SQL Server 2022, results in benchmarks/sqlserver-lock-escalation/results/.</figcaption>
</figure>

The read never had a chance to be slow at its own work. It was fast, 33ms, and then it wasn't allowed to run at all. That's the tell for escalation in production: not gradually rising latency, but reads that flatline until some writer commits.

## Where the cliff actually is

The interesting question is exactly when SQL Server decides to trade in the row locks. The documented number is around 5000 locks per statement, so I swept the update size from 500 rows up to 12000, and after each one, still inside an open transaction, I counted the locks and fired a point read at the untouched row 150000 with an 800ms lock timeout.

```
update_size   key_locks   object_mode   escalated   concurrent_read
       500          500   IX            no          0.7ms
      2000         2000   IX            no          0.7ms
      4000         4000   IX            no          2.0ms
      5000         5000   IX            no          2.5ms
      6000         6000   IX            no          0.5ms
      7000            0   X             yes          801.7ms  (timed out)
      8000            0   X             yes          803.8ms  (timed out)
     12000            0   X             yes          802.6ms  (timed out)
```

Two things surprised me here. First, the escalation didn't happen at exactly 5000. At 6000 held row locks SQL Server was still holding all 6000 of them, IX on the object, and the concurrent read went through in half a millisecond. The flip landed somewhere between 6000 and 7000, a bit north of the documented threshold, because page-lock coalescing and the exact plan shift the count around. Don't treat 5000 as a hard line, treat it as a neighborhood. Second, the cliff is genuinely a cliff. There's no ramp. 6000 rows is 0.5ms and business as usual, 7000 rows is a table lock and every other reader on the table times out.

<figure class="cache-bench">
  <h3>Row locks held, and concurrent read latency, across the escalation cliff</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">row (KEY) locks held</p>
      <div class="cb-bar-row"><span>4000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:66.7%;--bar:var(--cb-green)"></span></span><span class="cb-value">4000</span></div>
      <div class="cb-bar-row"><span>5000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:83.3%;--bar:var(--cb-green)"></span></span><span class="cb-value">5000</span></div>
      <div class="cb-bar-row"><span>6000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">6000</span></div>
      <div class="cb-bar-row"><span>7000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0</span></div>
      <div class="cb-bar-row"><span>8000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0</span></div>
      <div class="cb-bar-row"><span>12000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:0%;--bar:var(--cb-orange)"></span></span><span class="cb-value">0</span></div>
    </div>
    <div>
      <p class="cb-panel-title">concurrent read, ms (800ms timeout)</p>
      <div class="cb-bar-row"><span>4000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:0.25%;--bar:var(--cb-blue)"></span></span><span class="cb-value">2.0</span></div>
      <div class="cb-bar-row"><span>5000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:0.31%;--bar:var(--cb-blue)"></span></span><span class="cb-value">2.5</span></div>
      <div class="cb-bar-row"><span>6000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:0.06%;--bar:var(--cb-blue)"></span></span><span class="cb-value">0.5</span></div>
      <div class="cb-bar-row"><span>7000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:99.7%;--bar:var(--cb-orange)"></span></span><span class="cb-value">801.7</span></div>
      <div class="cb-bar-row"><span>8000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">803.8</span></div>
      <div class="cb-bar-row"><span>12000 rows</span><span class="cb-track"><span class="cb-fill" style="--value:99.85%;--bar:var(--cb-orange)"></span></span><span class="cb-value">802.6</span></div>
    </div>
  </div>
  <figcaption>Left, the writer holds one row lock per row right up to 6000, then at 7000 they vanish, all collapsed into a single table X lock (shown as zero KEY locks). Right, the concurrent read of an untouched row tracks that exactly: sub-millisecond up to 6000 rows, then it slams into the 800ms lock timeout the moment escalation fires. Orange marks the escalated sizes. Measured on SQL Server 2022, results in benchmarks/sqlserver-lock-escalation/results/.</figcaption>
</figure>

The left panel is the mechanism in one picture. The row locks climb honestly, 4000, 5000, 6000, and then at 7000 they don't climb to 7000, they drop to zero, because they've been swapped for the one lock that ruins everyone's afternoon. The right panel is what your users feel. Nothing, nothing, nothing, then a timeout.

## Getting the same work done without the wall

The update still has to happen. You do have 50000 rows to touch. The fix isn't to avoid the write, it's to never let one statement cross the escalation threshold, and the plain way to do that is to break the update into batches small enough that each one stays under the cliff and releases its locks between batches.

```sql
-- instead of one statement over 50000 rows:
DECLARE @lo INT = 1;
WHILE @lo <= 50000
BEGIN
    UPDATE orders SET amount = amount + 1
    WHERE id BETWEEN @lo AND @lo + 1999;   -- 2000 rows, well under the cliff
    SET @lo += 2000;                        -- each batch commits on its own
END
```

To measure it I ran a background reader hammering point selects at rows the update never covers, for the full duration of the write, and compared three ways of doing the same 50000-row update: one big statement, 2000-row batches, and one big statement with escalation turned off on the table (`ALTER TABLE orders SET (LOCK_ESCALATION = DISABLE)`). Each writer held its locks for about the same three seconds of wall time, so the only variable is how the locking behaves.

<figure class="cache-bench">
  <h3>Reader throughput and worst-case latency, same 50,000-row update three ways</h3>
  <div class="cb-panels">
    <div>
      <p class="cb-panel-title">reads completed during the write</p>
      <div class="cb-bar-row"><span>one big update</span><span class="cb-track"><span class="cb-fill" style="--value:8.7%;--bar:var(--cb-orange)"></span></span><span class="cb-value">457</span></div>
      <div class="cb-bar-row"><span>2000-row batches</span><span class="cb-track"><span class="cb-fill" style="--value:99.5%;--bar:var(--cb-green)"></span></span><span class="cb-value">5210</span></div>
      <div class="cb-bar-row"><span>escalation disabled</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-green)"></span></span><span class="cb-value">5234</span></div>
    </div>
    <div>
      <p class="cb-panel-title">worst read latency, ms</p>
      <div class="cb-bar-row"><span>one big update</span><span class="cb-track"><span class="cb-fill" style="--value:100%;--bar:var(--cb-orange)"></span></span><span class="cb-value">1007.5</span></div>
      <div class="cb-bar-row"><span>2000-row batches</span><span class="cb-track"><span class="cb-fill" style="--value:0.7%;--bar:var(--cb-green)"></span></span><span class="cb-value">7.1</span></div>
      <div class="cb-bar-row"><span>escalation disabled</span><span class="cb-track"><span class="cb-fill" style="--value:0.27%;--bar:var(--cb-green)"></span></span><span class="cb-value">2.7</span></div>
    </div>
  </div>
  <figcaption>The single update lets the reader complete only 457 selects in the window and stalls three of them out to a 1007ms lock timeout. Batching into 2000-row chunks lets the reader complete 5210 selects, none blocked, worst case 7.1ms. Disabling escalation does about the same, 5234 selects, worst case 2.7ms. Roughly 11x the reader throughput for the same write. Measured on SQL Server 2022, results in benchmarks/sqlserver-lock-escalation/results/.</figcaption>
</figure>

One note on why I'm quoting the worst case and the blocked count instead of a percentile. In the single-update run the reader's p99 was a tidy 2.4ms, which looks completely fine, because the three reads that ate a full second are less than 1% of the samples and hide under the 99th percentile entirely. The p99 lies here. The blocked count and the max are where the pain actually lives, and they say three reads timed out and the worst waited over a second. When you're hunting escalation, don't trust the percentile, count the timeouts.

Batching wins on the number that matters, reader throughput, without giving up anything. Disabling escalation on the table wins by a hair more, because it never takes the table lock at all, but it does it by holding all 50000 fine-grained locks in memory for the duration, and that memory pressure is its own tax at real scale. Batching gets you almost the identical result and never holds more than a couple thousand locks at once, which is why it's the default reach.

## The takeaway

Lock escalation is a memory optimization that most people meet as an availability incident. One statement crosses roughly 5000 locks on an object, SQL Server swaps its row locks for a single table lock, and every other session on that table blocks until it commits, even the ones reading rows the writer never touched. Treat that 5000 as approximate, because coalescing pushed my flip up past 6000.

If a big write is starving your readers, the plain fix is to batch it into chunks that stay under the cliff and commit between them, which on my box turned 457 completed reads into 5210 and dropped the worst read from a 1007ms timeout to 7ms. `LOCK_ESCALATION = DISABLE` does about the same but pays for it in lock memory, so keep it for the specific table where you've measured that batching isn't an option. And the one thing to remember when you're staring at wait stats and a query is blocked on a row nobody else is using: the writer didn't lock that row, it locked the table, and it did it to save memory. The harness is [on GitHub](https://github.com/akisonlyforu/akisonlyforu.github.io/tree/master/benchmarks/sqlserver-lock-escalation) if you want to watch your own escalation cliff. These are laptop numbers under emulation, the shape of the thing, not a capacity statement about your server.
