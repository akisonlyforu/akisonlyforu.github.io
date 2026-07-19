# SQL Server index fragmentation harness

This harness reproduces index fragmentation on a real SQL Server 2022 and
measures what REBUILD and REORGANIZE actually do about it - the two
remediation paths described in Microsoft's [Reorganize and rebuild
indexes](https://learn.microsoft.com/en-us/sql/relational-databases/indexes/reorganize-and-rebuild-indexes)
guidance.

A single `orders` table: a clustered PK on an `IDENTITY` `id`, plus a
nonclustered index on `(customer_id, created_at)` - a realistic "recent
orders for this customer" index. `customer_id` is derived from
`HASHBYTES('MD5', row_counter)` rather than `NEWID()`, so it is
non-sequential (the classic fragmentation trigger for a nonclustered index)
but fully **deterministic**: two tables built with the same base row count
and the same churn schedule land in an identical physical fragmentation
state, which is what makes the REBUILD vs REORGANIZE comparison in
experiment B apples-to-apples without needing a snapshot/copy mechanism.

Four experiments against a 2,000,000-row `orders` table:

- **A. Build fragmentation and measure it** - starting from a fresh index at
  `FILLFACTOR = 100`, apply 8 churn batches of increasing size (200 through
  25,600 new rows with hashed, non-sequential `customer_id`). Each batch
  lands on already-full leaf pages and forces page splits. At each of the 9
  checkpoints (baseline + 8 churn steps), record `avg_fragmentation_in_percent`
  and `avg_page_space_used_in_percent` from `sys.dm_db_index_physical_stats`,
  plus the average logical reads and server-side elapsed time (from
  `sys.dm_exec_query_stats`) for a representative range-scan query.
- **B. REBUILD vs REORGANIZE** - two independent tables, built with the
  identical base load and churn schedule, reach the same ~99% fragmented
  state. `ALTER INDEX ... REBUILD` on one, `ALTER INDEX ... REORGANIZE` on
  the other. Measures elapsed time, resulting fragmentation, and transaction
  log growth (`DBCC SQLPERF(LOGSPACE)`, delta bracketed by explicit
  `CHECKPOINT`s under `SIMPLE` recovery).
- **C. Does REORGANIZE keep up at high fragmentation?** - the same
  REBUILD/REORGANIZE comparison repeated at a *low* starting fragmentation
  level (~19%, churned with just 600 rows) and compared against experiment
  B's *high* level (~99%).
- **D. Post-fix query performance** - the experiment A/B query, run
  fragmented, after REBUILD, and after REORGANIZE.

pymssql does not surface `SET STATISTICS IO`/`TIME` messages (they come back
as TDS info messages, not result rows - confirmed empirically), so query cost
is read from `sys.dm_exec_query_stats` instead. That DMV turned out to have
its own sharp edge: a big churn batch or a REBUILD/REORGANIZE invalidates the
cached plan for the measurement query, and SQL Server can *evict* the old
plan-cache row rather than leave it in place, which corrupts a naive
before/after delta. The harness works around this by running
`DBCC FREEPROCCACHE` immediately before each measurement window and reading
the post-loop totals directly - no delta arithmetic, so a mid-loop recompile
can't produce a bogus negative number.

These are laptop measurements demonstrating the mechanism, not production
capacity numbers. The image is amd64-only and runs under emulation on arm64
hosts.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/sqlserver-fragmentation
docker compose up -d --wait          # SQL Server on 127.0.0.1:11434

python3 -m venv /tmp/mssql-frag-venv && source /tmp/mssql-frag-venv/bin/activate
pip install -r requirements.txt

python benchmark.py | tee results/summary.txt
docker compose down -v
```

## Results

The checked-in run is SQL Server 2022 (16.0.4265.3), 2,000,000 base rows,
200,000 distinct customers, 15 query repetitions per measurement. Total
runtime: 62.1s.

### A - fragmentation builds fast on a packed, non-sequential index

| checkpoint | cum. churn rows | frag % | page use % | pages | avg logical reads | avg ms |
|---|---|---|---|---|---|---|
| baseline | 0 | 0.17 | 99.90 | 5,440 | 275 | 11.50 |
| churn-1 | 200 | 7.11 | 96.44 | 5,636 | 284 | 11.45 |
| churn-2 | 600 | 18.91 | 90.50 | 6,007 | 303 | 11.46 |
| churn-3 | 1,400 | 37.12 | 81.34 | 6,686 | 337 | 11.69 |
| churn-4 | 3,000 | 58.83 | 70.43 | 7,727 | 377 | 11.37 |
| churn-5 | 6,200 | 80.59 | 59.54 | 9,154 | 456 | 11.73 |
| churn-6 | 12,600 | 93.88 | 53.00 | 10,317 | 526 | 11.44 |
| churn-7 | 25,400 | 98.80 | 50.80 | 10,832 | 543 | 11.85 |
| churn-8 | 51,000 | 99.21 | 51.22 | 10,879 | 546 | 12.79 |

Inserting non-sequential keys equal to just **200** out of ~5,440 leaf pages
(0.16% of baseline page count) already pushes fragmentation past 7%; by
~1% of the table's row count churned, fragmentation is past 99% and page
count has roughly doubled (5,440 -> 10,879). Logical reads for the range
query track page count almost exactly (275 -> 546, a ~2x increase). The
millisecond column is close to flat - at this scale the scan is small enough
to stay CPU/cache-bound rather than IO-bound, so **logical reads is the
clean signal here, not wall-clock time**.

### B - REBUILD vs REORGANIZE from an identical ~99% fragmented state

| operation | frag before | frag after | page use % after | elapsed s | log growth (MB) |
|---|---|---|---|---|---|
| REBUILD | 99.16% | **0.16%** | 99.93% | **0.22s** | **0.41 MB** |
| REORGANIZE | 99.30% | 0.46% | 99.61% | 5.42s | 254.96 MB |

REORGANIZE took **24.5x** as long and generated **616x** the transaction log
of REBUILD, for a comparable fragmentation outcome. This is the mechanism
behind the doc's guidance: REBUILD is minimally logged under `SIMPLE`/
`BULK_LOGGED` recovery because it's a wholesale rewrite, while REORGANIZE is
always fully logged because it's an in-place, page-by-page compaction -
every row move is its own logged operation.

### C - REORGANIZE's *cost* degrades with starting fragmentation; REBUILD's doesn't

| level | operation | frag before | frag after | elapsed s |
|---|---|---|---|---|
| low (~19%) | REBUILD | 19.07% | 0.17% | 0.16s |
| low (~19%) | REORGANIZE | 19.05% | 0.33% | 1.62s |
| high (~99%) | REBUILD | 99.16% | 0.16% | 0.22s |
| high (~99%) | REORGANIZE | 99.30% | 0.46% | 5.42s |

REBUILD's elapsed time barely moves with starting fragmentation (0.16s ->
0.22s), because it always does the same thing: read everything, write a
fresh copy. REORGANIZE's time scales up sharply with the amount of disorder
it has to walk and compact (1.62s -> 5.42s, ~3.3x, for going from 19% to 99%
starting fragmentation).

**Honest caveat on this experiment:** the doc's guidance is partly about
REORGANIZE's fragmentation-reduction *effectiveness* degrading at high
starting fragmentation, not just its cost. In this harness - an idle,
single-session benchmark where REORGANIZE runs to completion with zero
concurrent writes - it fully compacted the index at both fragmentation
levels (0.33% and 0.46% resulting fragmentation, both comparable to
REBUILD's). That did **not** reproduce the "REORGANIZE leaves you
meaningfully more fragmented than REBUILD" outcome; what reproduced cleanly
instead, and just as dramatically, is the time/log cost asymmetry above. On
a real production system with concurrent writes continuing during a
multi-minute single-threaded REORGANIZE, or on a much larger table where
REORGANIZE can't finish in one pass, the residual-fragmentation gap the doc
describes would very plausibly show up - this benchmark just isn't built to
create sustained concurrent churn, so that particular half of the guidance
is asserted by Microsoft's doc but not independently confirmed by this run.

### D - query cost, fragmented vs after each fix

| state | avg logical reads | avg elapsed ms |
|---|---|---|
| fragmented | 548 | 11.72 |
| after REBUILD | 282 | 12.50 |
| after REORGANIZE | 283 | 11.98 |

Logical reads roughly halve after either fix (548 -> ~282-283), matching the
~2x page-count reduction from compaction. As in experiment A, the
millisecond delta is small and noisy at this row count / hardware - fixed
per-query overhead dominates wall-clock time for a scan this size, so
logical reads is the number worth citing, not the millisecond column.

## Files

- `summary.txt` - the captured console run used above.
- `fragmentation_over_time.csv` - experiment A: fragmentation, page-space-used,
  page count, and query cost at each of the 9 checkpoints.
- `rebuild_vs_reorganize.csv` - experiment B: REBUILD vs REORGANIZE from the
  identical high-fragmentation state.
- `reorganize_by_level.csv` - experiment C: REBUILD/REORGANIZE at low vs high
  starting fragmentation.
- `query_performance.csv` - experiment D: range-query cost in the three
  states.
- `run_metadata.csv` - SQL Server version, image digest, row counts, churn
  totals, query parameters, total runtime.

## Notes on what didn't reproduce cleanly on the first pass

The underlying fragmentation mechanism reproduced cleanly on the very first
real run against the container. The instrumentation didn't: an earlier
version measured query cost with a naive before/after delta on
`sys.dm_exec_query_stats`, matched by a `LIKE '%index_name%'` pattern. That
pattern also matched this harness's *own* monitoring queries (which mention
the index name in their `WHERE` clauses), and separately, plan-cache
eviction around big churn batches and around REBUILD/REORGANIZE could delete
the "before" row out from under the delta, producing a nonsensical negative
count once per run. Both were fixed (exact-text match; `DBCC FREEPROCCACHE`
immediately before each measurement window, reading post-loop totals
directly) before the run captured above - no numbers in these CSVs come from
the broken version.
