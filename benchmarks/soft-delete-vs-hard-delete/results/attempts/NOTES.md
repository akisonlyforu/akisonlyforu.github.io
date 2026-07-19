# Non-reproducing / superseded attempts

Both logs below are real captured runs of this benchmark (not fabricated),
kept for honesty about what it took to get a clean signal.

## 01-console.log — missing ANALYZE, wrong plan for hard_delete_orders

First full run. The churn loop ran `VACUUM` after every cycle but not
`ANALYZE`, so `pg_stats` for both tables' `deleted_at` column never got real
selectivity data. Without stats, Postgres falls back to a fixed default
selectivity guess for `IS NULL`, applied identically to both tables
regardless of their true composition. That default guess happened to make
the planner pick a `Seq Scan + Sort` for the small hard_delete_orders table
(cost ~1611) instead of the obviously-cheaper `Index Scan Backward` on its
created_at index — see the `active_rows` EXPLAIN in the log: `Seq Scan on
hard_delete_orders ... Filter: (deleted_at IS NULL) ... rows=50000`. That
made hard_delete_orders' "active rows" query *slower* than soft_delete's
(p99 ratio 0.08x), the exact opposite of the mechanism the post is about —
an artifact of stale statistics, not of bloat.

Fix: the churn loop now runs `VACUUM ANALYZE`, matching what autovacuum
actually does in production. See `benchmark.py`'s module docstring for the
same explanation in-repo.

## 02-console.log — correct plans, but a weak/noisy latency signal

After the ANALYZE fix, and after switching the removal query so dead rows
are interspersed throughout the created_at range (see "within-batch removal"
in `benchmark.py`), both tables picked the right plan (Index Scan for both),
and `rows_removed_by_filter` correctly showed the soft_delete_orders query
skipping dead rows. But at the default 5,000-insert / 4,000-remove split
(80% within-batch churn) and 300 latency iterations, the actual number of
dead rows skipped (~200) was small enough that the real Postgres-side cost
difference (tens of microseconds) got lost in Python/psycopg2 round-trip
noise: p99 ratio bounced between sub-1x and low-1.x x depending on the run.

Fix: raised REMOVE_BATCH to 4,900 (98% within-batch churn — realistic for a
high-turnover entity like sessions/notifications/queue jobs, not just
"orders"), raised LATENCY_ITERATIONS to 1,000, and disabled the Python GC
during timed loops. That pushes `rows_removed_by_filter` into the
low-thousands, which clears the noise floor and reproduces consistently
across runs (see the checked-in `../summary.txt`).
