# soft-delete vs hard-delete bloat harness

A reproducible harness for the cost of soft-delete. Two identical `orders`
tables, same schema (`id`, `customer_id`, `status`, `amount_cents`,
`payload`, `created_at`, `deleted_at timestamptz NULL`):

- **hard_delete_orders** — "removed" rows get `DELETE`d.
- **soft_delete_orders** — "removed" rows get `UPDATE ... SET deleted_at =
  now()`, never physically deleted.

Both run against one digest-pinned PostgreSQL 16.14 instance in Docker. The
question: what does never actually removing a row cost you, and does a
partial index get most of it back?

## Churn model

Each of 50 cycles inserts 5,000 new rows, then "removes" 4,900 of those
*same freshly-inserted* rows (98% within-batch churn), chosen uniformly at
random from the batch — modelling a high-turnover entity (session records,
notifications, queue jobs, or orders that get cancelled/refunded shortly
after being placed) rather than only the oldest rows churning. Every batch,
old or new, ends up with the same dead/live density, so dead rows are
interspersed throughout the whole `created_at` range instead of concentrated
at the tail where a "give me recent active rows" query would never see them.
Both tables get a plain `VACUUM ANALYZE` (not `VACUUM FULL`) after every
cycle — the realistic autovacuum-equivalent reclaim-and-restat, not a
defrag.

Net effect after 50 cycles: `hard_delete_orders` settles around ~5,000 live
rows (VACUUM reclaims the space of every physically deleted row for reuse by
later inserts). `soft_delete_orders` has all 250,000 rows ever inserted
still sitting in the table as "live" tuples from Postgres's point of view —
soft-delete never gives that space back, because nothing is ever removed.

## Experiments

1. **Bloat under churn** (`bloat_over_time.csv`) — per-cycle
   `pg_relation_size`, `pg_total_relation_size`, PK + secondary index size,
   and `n_live_tup`/`n_dead_tup` for both tables, all 50 cycles (100 rows).
2. **Query latency after bloat** (`query_latency.csv`,
   `explain_*.txt`, `explain_summary.csv`) — 1,000 iterations each of a PK
   point lookup (`WHERE id = $1`) and the "active rows" query
   (`WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 50`), p50/p95/p99
   in ms, for both tables. `EXPLAIN (ANALYZE, BUFFERS)` captured for every
   table x query combination as mechanism evidence.
3. **Partial index fix** (`partial_index_fix.csv`,
   `explain_active_rows_soft_before_partial.txt`,
   `explain_active_rows_soft_partial.txt`) — add
   `CREATE INDEX ... ON soft_delete_orders (created_at DESC) WHERE deleted_at
   IS NULL`, then re-run the identical active-rows benchmark against: the
   original full index, the new partial index, and `hard_delete_orders` as a
   reference. Records the partial index's on-disk size against the original.

These are laptop numbers demonstrating the mechanism, not capacity-planning
numbers.

## Run it

Docker with Compose v2, plus Python 3.9+.

```bash
cd benchmarks/soft-delete-vs-hard-delete
docker compose up -d --wait          # postgres 16.14 on 127.0.0.1:55445

python3 -m venv /tmp/softdel-bench-venv && source /tmp/softdel-bench-venv/bin/activate
pip install -r requirements.txt

python benchmark.py
docker compose down -v
```

Postgres is bound to loopback on host port `55445` (chosen to avoid clashing
with other Postgres containers on the more common `5432`/`55432`/`55433`
range already used by other benchmarks in this repo). Override connection
and workload knobs without editing the script:

`PGHOST`(127.0.0.1) `PGPORT`(55445) `PGUSER`(bench) `PGPASSWORD`(bench)
`PGDATABASE`(soft_delete_bench) `RESULTS_DIR`(results/) `CYCLES`(50)
`INSERT_BATCH`(5000) `REMOVE_BATCH`(4900) `LATENCY_ITERATIONS`(1000)
`SEED`(1234).

## Results (captured run: PostgreSQL 16.14, 50 cycles)

**1 — bloat under churn** (final state, 250,000 rows ever inserted into
soft_delete_orders vs ~5,000 net-live rows in hard_delete_orders):

| table | total size | table (heap) size | secondary index | live rows |
|---|---:|---:|---:|---:|
| hard_delete_orders | 14.6 MB | 1.6 MB | 8.2 MB | 5,034 |
| soft_delete_orders | 63.1 MB | 42.3 MB | 9.7 MB | 250,000 |
| **ratio (soft/hard)** | **4.3x** | **26.7x** | 1.2x | 49.7x |

The heap ratio is the clean, uncounfounded number: **soft_delete_orders'
table is 26.7x larger** than hard_delete_orders' for holding the same 5,000
"currently active" rows, because VACUUM reclaims every hard-deleted row's
space for reuse but has nothing to reclaim from a soft-delete — the row is
still there, just flagged.

**2 — query latency after bloat** (1,000 iterations each):

| table | query | p50 | p95 | p99 |
|---|---|---:|---:|---:|
| hard_delete_orders | pk_lookup | 0.205 ms | 0.240 ms | 0.259 ms |
| hard_delete_orders | active_rows | 0.274 ms | 0.321 ms | 0.848 ms |
| soft_delete_orders | pk_lookup | 0.212 ms | 0.260 ms | 0.656 ms |
| soft_delete_orders | active_rows | **0.503 ms** | 0.575 ms | 1.126 ms |

PK lookups are close either way (point lookups don't care about bloat — one
index descent either way). The "active rows" query is where it shows up:
soft_delete_orders' `EXPLAIN (ANALYZE, BUFFERS)` reports `Rows Removed by
Filter: 1948` — it has to walk past ~1,948 soft-deleted rows in the index to
find 50 live ones — versus 0 for hard_delete_orders, whose index only ever
contains live rows. p50 is ~1.8x slower on soft_delete; p99 is noisier
(1.1–1.7x across runs) since these are all sub-millisecond, cache-resident
queries where tail latency is sensitive to scheduling jitter.

**3 — partial index fix** (same query, same 1,000 iterations, same table
state, before/after `CREATE INDEX ... WHERE deleted_at IS NULL`):

| variant | p50 | p99 | rows removed by filter | index size |
|---|---:|---:|---:|---:|
| soft_delete, original index | 0.510 ms | 1.915 ms | 1,948 | 9,976 KB |
| soft_delete, **partial index** | **0.275 ms** | **1.094 ms** | 0 | **128 KB** |
| hard_delete, reference | 0.279 ms | 0.668 ms | 0 | 8,352 KB |

The punchline: the partial index is **77.9x smaller** than the original full
index (128 KB vs 9,976 KB — it only carries the ~5,000 live rows instead of
all 250,000 ever inserted) and drops `Rows Removed by Filter` to 0 (see
`explain_active_rows_soft_partial.txt`). p99 improves **1.75x** (1.915 ms →
1.094 ms), landing close to hard_delete_orders' own reference number — the
partial index gets soft-delete back to roughly hard-delete parity on the
query that mattered, without giving up the append-only/audit-friendly
soft-delete model.

The single-query `EXPLAIN (ANALYZE, BUFFERS)` samples make the mechanism
concrete (`explain_active_rows_soft_before_partial.txt` vs
`explain_active_rows_soft_partial.txt`):

| | original index | partial index |
|---|---:|---:|
| plan | Index Scan + Filter | Index Scan (no filter) |
| rows removed by filter | 1,948 | 0 |
| shared buffers hit | 1,993 | 39 |
| execution time | 0.239 ms | 0.020 ms |

51x fewer buffers touched, 12x faster on that one sampled execution — the
partial index doesn't just get smaller, it turns "scan past ~2,000 dead rows
to find 50 live ones" into "read 39 buffers and you're done."

Full numbers: `bloat_over_time.csv` (100 rows, one per cycle per table),
`query_latency.csv`, `partial_index_fix.csv`, `explain_summary.csv`,
`explain_*.txt`, `summary.txt`, `run_metadata.csv` (Postgres version, image
digest, all params).

## What reproduced cleanly, what needed tuning

The heap-size story (experiment 1's `table_bytes` ratio) and the
partial-index-size story (experiment 3) reproduced sharply and are the two
headline numbers: 26.7x and 77.9x respectively, consistent across repeated
runs with these parameters.

Two things did **not** reproduce cleanly on the first try, and both are kept
under `results/attempts/` with a full explanation in `attempts/NOTES.md`:

- The first run vacuumed but never `ANALYZE`d the tables, so Postgres fell
  back to a default selectivity guess for `deleted_at IS NULL` and picked a
  `Seq Scan + Sort` for the *smaller* hard_delete_orders table instead of an
  index scan — making it look slower than soft_delete_orders, the opposite
  of the real mechanism. Fixed by running `VACUUM ANALYZE` every cycle
  (which is what autovacuum actually does).
- At a gentler 80% within-batch churn (5,000 insert / 4,000 remove, the
  literal numbers this benchmark started from) and 300 latency iterations,
  `rows_removed_by_filter` only reached the low hundreds — a real but small
  effect that got lost in Python/psycopg2 round-trip noise on these
  sub-millisecond, fully cache-resident queries. Raised to 98% within-batch
  churn (4,900/5,000) and 1,000 iterations with the Python GC disabled
  during timed loops to clear the noise floor.

One more honest wrinkle, visible in the checked-in numbers rather than
hidden: experiment 1's **secondary index** byte-size ratio (soft/hard) is
only ~1.2x, far less dramatic than the heap ratio. `pgstatindex()` on
hard_delete_orders' own `created_at` index shows why —
`avg_leaf_density=1.6%`, `leaf_fragmentation=97%`. A monotonically
increasing key (`created_at`, `id`) under heavy insert+delete churn bloats a
B-tree from *page fragmentation* regardless of delete strategy: new inserts
always go to a fresh page at the end, and plain `VACUUM` reclaims dead
*entries* but doesn't rebalance the mostly-empty pages left behind. That
confound hits both tables' original indexes and is a real, separate
Postgres phenomenon worth knowing about — but it doesn't touch the partial
index in experiment 3, which is a freshly built, unfragmented index and is
where the clean 77.9x number comes from.
